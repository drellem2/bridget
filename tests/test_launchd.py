#!/usr/bin/env python3
# Copyright (C) 2026 Daniel Miller
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Written for this fork of cloverross/bridget; not present upstream.
#
# bridget is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. bridget is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
# You should have received a copy of the GNU General Public License along with
# bridget. If not, see <https://www.gnu.org/licenses/>.

"""Tests for the persistence layer: bridget-supervise and the launchd plist.

bridget-supervise is what actually keeps bridget alive. launchd's KeepAlive
cannot be relied on: a KeepAlive restart is a "nondemand" spawn, and launchd
defers those under load (`pended nondemand spawn = inefficient`) — measured at
115s and still not respawned, which is how com.pogo.watchdog stayed dead for
hours (mg-50e0). So the wrapper's restart loop is load-bearing and gets tested
like it. These run anywhere: nothing here calls launchctl.
"""
import os
import plistlib
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SUPERVISE = REPO / 'bridget-supervise'
PLIST_EXAMPLE = REPO / 'com.pogo.bridget.plist.example'


def fake_bridget(tmp: Path, body: str) -> Path:
    """A stand-in for the bridget script, so no Discord token is ever needed."""
    p = tmp / 'fake-bridget'
    p.write_text('#!/bin/bash\n' + textwrap.dedent(body))
    p.chmod(0o755)
    return p


class SuperviseRestartTest(unittest.TestCase):
    def test_restarts_the_child_until_max_spawns(self):
        """The whole point: bridget dies, the wrapper starts it again."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            marks = tmp / 'runs'
            child = fake_bridget(tmp, f"""
                echo run >> {marks}
                exit 7
            """)
            r = subprocess.run(
                ['bash', str(SUPERVISE)],
                env={**os.environ, 'BRIDGET_BIN': str(child),
                     'BRIDGET_MIN_BACKOFF': '0', 'BRIDGET_MAX_SPAWNS': '3',
                     'BRIDGET_HEALTHY_RUNTIME': '9999'},
                capture_output=True, text=True, timeout=30)
            self.assertEqual(marks.read_text().count('run'), 3,
                             'wrapper must restart the child, not exit with it')
            self.assertEqual(r.returncode, 7, "wrapper exits with the child's code")
            self.assertIn('spawn #3', r.stdout)

    def test_a_fast_exit_backs_off_and_a_healthy_run_does_not(self):
        """Backoff doubles only for children that die immediately."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            child = fake_bridget(tmp, 'exit 1\n')
            r = subprocess.run(
                ['bash', str(SUPERVISE)],
                env={**os.environ, 'BRIDGET_BIN': str(child),
                     'BRIDGET_MIN_BACKOFF': '0', 'BRIDGET_MAX_SPAWNS': '3',
                     'BRIDGET_HEALTHY_RUNTIME': '9999'},
                capture_output=True, text=True, timeout=30)
            self.assertIn('(too fast)', r.stdout)
            self.assertNotIn('(healthy run)', r.stdout)

    def test_missing_bridget_is_fatal_not_a_hot_loop(self):
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(
                ['bash', str(SUPERVISE)],
                env={**os.environ, 'BRIDGET_BIN': str(Path(td) / 'nope')},
                capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 1)
            self.assertIn('FATAL', r.stdout)


class SuperviseSignalTest(unittest.TestCase):
    def test_sigterm_is_forwarded_to_the_child_and_honored(self):
        """`launchctl bootout` and `kickstart -k` must be able to stop us."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            got = tmp / 'child-signal'
            child = fake_bridget(tmp, f"""
                trap 'echo TERM >> {got}; exit 143' TERM
                echo ready >> {tmp}/ready
                while true; do sleep 0.1; done
            """)
            proc = subprocess.Popen(
                ['bash', str(SUPERVISE)],
                env={**os.environ, 'BRIDGET_BIN': str(child)},
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                deadline = time.time() + 15
                while not (tmp / 'ready').exists() and time.time() < deadline:
                    time.sleep(0.1)
                self.assertTrue((tmp / 'ready').exists(), 'child never started')

                proc.send_signal(signal.SIGTERM)
                out, _ = proc.communicate(timeout=20)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)

            self.assertEqual(proc.returncode, 143, 'wrapper must exit 128+SIGTERM')
            self.assertIn('got SIGTERM', out)
            self.assertTrue(got.exists(), 'SIGTERM was not forwarded to bridget')
            self.assertIn('TERM', got.read_text())

    def test_sigterm_during_the_backoff_sleep_is_honored_promptly(self):
        """Stopping the wrapper must not have to wait out the backoff.

        The other signal tests here signal the wrapper while bridget is up, so
        it is parked in `wait "$child"` — and `wait` is interruptible. The
        backoff is the other half of its life, and bash runs a trap only once
        the current *foreground* command returns: a plain `sleep "$backoff"`
        left SIGTERM unhandled for the rest of the backoff (2m44s, observed).
        launchd escalates an unanswered SIGTERM to SIGKILL, so that window ends
        with a dead supervisor and an orphaned bridget.
        """
        backoff = 30
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ran = tmp / 'ran'
            child = fake_bridget(tmp, f"""
                echo run >> {ran}
                exit 1
            """)
            proc = subprocess.Popen(
                ['bash', str(SUPERVISE)],
                env={**os.environ, 'BRIDGET_BIN': str(child),
                     'BRIDGET_MIN_BACKOFF': str(backoff),
                     'BRIDGET_HEALTHY_RUNTIME': '9999'},
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                deadline = time.time() + 15
                while not ran.exists() and time.time() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ran.exists(), 'child never started')
                time.sleep(1)  # let the wrapper reach its backoff sleep

                sent = time.time()
                proc.send_signal(signal.SIGTERM)
                try:
                    out, _ = proc.communicate(timeout=backoff // 2)
                except subprocess.TimeoutExpired:
                    self.fail('SIGTERM ignored during backoff — the wrapper is '
                              'sleeping in the foreground, so bash defers the trap')
                elapsed = time.time() - sent
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)

            self.assertLess(elapsed, backoff / 2,
                            'wrapper waited out its backoff before honoring SIGTERM')
            self.assertEqual(proc.returncode, 143, 'wrapper must exit 128+SIGTERM')
            self.assertIn('got SIGTERM', out)

    def test_the_wrapper_does_not_restart_after_being_terminated(self):
        """A terminating wrapper must not resurrect the child on its way out."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            starts = tmp / 'starts'
            child = fake_bridget(tmp, f"""
                echo start >> {starts}
                trap 'exit 143' TERM
                while true; do sleep 0.1; done
            """)
            proc = subprocess.Popen(
                ['bash', str(SUPERVISE)],
                env={**os.environ, 'BRIDGET_BIN': str(child),
                     'BRIDGET_MIN_BACKOFF': '0'},
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                deadline = time.time() + 15
                while not starts.exists() and time.time() < deadline:
                    time.sleep(0.1)
                proc.send_signal(signal.SIGTERM)
                proc.communicate(timeout=20)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
            time.sleep(0.5)
            self.assertEqual(starts.read_text().count('start'), 1,
                             'wrapper restarted the child while terminating')


class PlistTemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = PLIST_EXAMPLE.read_text()
        cls.rendered = cls.raw.replace('__HOME__', '/Users/testuser')
        cls.parsed = plistlib.loads(cls.rendered.encode())

    def test_parses_under_a_strict_xml_parser(self):
        """`plutil -lint` is lenient; plistlib/expat is not. Both must pass."""
        self.assertEqual(self.parsed['Label'], 'com.pogo.bridget')

    def test_comments_contain_no_double_hyphen(self):
        """XML forbids `--` inside a comment. plutil accepts it; expat rejects it."""
        for comment in re.findall(r'<!--(.*?)-->', self.raw, re.DOTALL):
            self.assertNotIn('--', comment,
                             'a double-hyphen in an XML comment breaks strict parsers')

    def test_runs_the_supervisor_not_bridget_directly(self):
        self.assertEqual(self.parsed['ProgramArguments'],
                         ['/Users/testuser/.pogo/bin/bridget-supervise'])

    def test_keepalive_and_runatload_are_set(self):
        self.assertIs(self.parsed['RunAtLoad'], True)
        self.assertIs(self.parsed['KeepAlive'], True)
        self.assertEqual(self.parsed['ThrottleInterval'], 10)

    def test_startinterval_and_processtype_are_absent(self):
        """Both were measured not to defeat launchd's spawn pending (mg-0655).

        A StartInterval fire is itself a nondemand spawn, so it is pended too;
        ProcessType=Interactive changes the reported spawn type and nothing else.
        Shipping either would imply a guarantee neither provides.
        """
        self.assertNotIn('StartInterval', self.parsed)
        self.assertNotIn('ProcessType', self.parsed)

    def test_no_home_placeholder_survives_rendering(self):
        self.assertNotIn('__HOME__', self.rendered)

    def test_carries_no_secrets(self):
        """The token lives in ~/.pogo/bridget.env (0600). Plists are world-readable."""
        env = self.parsed['EnvironmentVariables']
        self.assertNotIn('DISCORD_BOT_TOKEN', env)
        for key, value in env.items():
            self.assertNotIn('TOKEN', key.upper(), f'{key} looks like a secret')
            self.assertNotRegex(str(value), r'[A-Za-z0-9_-]{50,}',
                                f'{key} holds something token-shaped')

    def test_path_reaches_mg_and_a_real_python(self):
        path = self.parsed['EnvironmentVariables']['PATH'].split(':')
        self.assertIn('/Users/testuser/go/bin', path, 'mg lives here')
        self.assertIn('/opt/homebrew/bin', path, 'python3 lives here')
        self.assertEqual(self.parsed['EnvironmentVariables']['HOME'],
                         '/Users/testuser')

    def test_logs_land_under_dot_pogo(self):
        self.assertEqual(self.parsed['StandardOutPath'],
                         '/Users/testuser/.pogo/bridget.log')
        self.assertEqual(self.parsed['StandardErrorPath'],
                         '/Users/testuser/.pogo/bridget.err.log')


if __name__ == '__main__':
    unittest.main(verbosity=2)
