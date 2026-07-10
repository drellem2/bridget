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


def fake_bridget(tmp: Path, body: str, name: str = 'fake-bridget') -> Path:
    """A stand-in for the bridget script, so no Discord token is ever needed."""
    p = tmp / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('#!/bin/bash\n' + textwrap.dedent(body))
    p.chmod(0o755)
    return p


def alert_sink(tmp: Path) -> Path:
    """Stand in for `mg mail send mayor`, recording subject+body to a file.

    Every test in this module sets BRIDGET_ALERT_CMD to one of these. The
    supervisor's default notifier really does shell out to `mg`, and a suite
    that mails the mayor on each run is a suite nobody runs twice.
    """
    sink = tmp / 'alerts'
    cmd = tmp / 'alert-cmd'
    cmd.write_text('#!/bin/bash\nprintf "%s\\n%s\\n---\\n" "$1" "$2" >> ' + str(sink) + '\n')
    cmd.chmod(0o755)
    return cmd


def supervise_env(tmp: Path, **overrides) -> dict:
    """Base env: alerts captured, never mailed; alert stamp inside the tmpdir.

    Every inherited BRIDGET_* is dropped first. A developer with BRIDGET_BIN
    exported — the very habit that caused mg-1679 — would otherwise silently
    steer the tests that exist to catch it.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith('BRIDGET_')}
    env['BRIDGET_ALERT_CMD'] = str(alert_sink(tmp))
    env['BRIDGET_ALERT_STAMP'] = str(tmp / 'alert.stamp')
    env.update(overrides)
    return env


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
                env=supervise_env(tmp, BRIDGET_BIN=str(child),
                                  BRIDGET_MIN_BACKOFF='0', BRIDGET_MAX_SPAWNS='3',
                                  BRIDGET_HEALTHY_RUNTIME='9999'),
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
                env=supervise_env(tmp, BRIDGET_BIN=str(child),
                                  BRIDGET_MIN_BACKOFF='0', BRIDGET_MAX_SPAWNS='3',
                                  BRIDGET_HEALTHY_RUNTIME='9999'),
                capture_output=True, text=True, timeout=30)
            self.assertIn('(too fast)', r.stdout)
            self.assertNotIn('(healthy run)', r.stdout)

    def test_missing_bridget_is_fatal_not_a_hot_loop(self):
        """A path the operator named that does not exist is not silently swapped.

        Nothing is running yet, so there is no service to preserve by falling
        back to $HOME/.pogo/bin/bridget — and papering over a bad BRIDGET_BIN
        would leave someone debugging a bridget they did not start.
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            durable = fake_bridget(tmp, 'exit 5\n', name='.pogo/bin/bridget')
            r = subprocess.run(
                ['bash', str(SUPERVISE)],
                env=supervise_env(tmp, HOME=str(tmp), BRIDGET_BIN=str(tmp / 'nope')),
                capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 1)
            self.assertIn('FATAL', r.stdout)
            self.assertTrue(durable.exists(), 'fixture sanity: a fallback existed')
            self.assertNotIn('fell back', r.stdout,
                             'a missing target at startup must not silently '
                             'substitute the durable default')


class SuperviseEphemeralTargetTest(unittest.TestCase):
    """BRIDGET_BIN must never be pinned to a path that outlives nothing.

    A supervisor started against `~/.pogo/polecats/<id>/bridget` kept that path
    after the worktree was reaped, and launchd (KeepAlive, ThrottleInterval=10)
    respawned it into `FATAL: no bridget at …` every ten seconds for eighteen
    minutes on 2026-07-10. `launchctl list` showed a pid throughout and
    `pogo doctor --check` stayed green, so nothing raised a hand: a human found
    it. These tests pin all three defenses and, just as importantly, the noise
    (mg-1679).
    """

    def polecat_bin(self, tmp: Path, marker: Path, rc: int = 5) -> Path:
        return fake_bridget(tmp, f'echo ephemeral >> {marker}\nexit {rc}\n',
                            name='.pogo/polecats/0655/bridget')

    def durable_bin(self, tmp: Path, marker: Path, rc: int = 5) -> Path:
        return fake_bridget(tmp, f'echo durable >> {marker}\nexit {rc}\n',
                            name='.pogo/bin/bridget')

    def alerts(self, tmp: Path) -> str:
        sink = tmp / 'alerts'
        return sink.read_text() if sink.exists() else ''

    def test_refuses_to_supervise_a_path_inside_a_polecat_worktree(self):
        """Even a working binary. Ephemeral is invalid by construction."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            marker = tmp / 'ran'
            child = self.polecat_bin(tmp, marker)
            r = subprocess.run(
                ['bash', str(SUPERVISE)],
                env=supervise_env(tmp, HOME=str(tmp), BRIDGET_BIN=str(child)),
                capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 1)
            self.assertFalse(marker.exists(),
                             'the supervisor executed an ephemeral target')
            self.assertIn('polecats', r.stdout)
            self.assertIn('mg-1679', self.alerts(tmp))

    def test_refuses_when_the_durable_symlink_resolves_into_a_worktree(self):
        """The botched install: BRIDGET_BIN is unset and the default is a lie.

        `~/.pogo/bin/bridget` is a symlink install.sh drops into the checkout.
        Run install.sh from a polecat worktree and it points there instead. The
        literal path looks durable; only the resolved one tells the truth.
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            marker = tmp / 'ran'
            real = self.polecat_bin(tmp, marker)
            link = tmp / '.pogo' / 'bin' / 'bridget'
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(real)

            r = subprocess.run(
                ['bash', str(SUPERVISE)],
                env=supervise_env(tmp, HOME=str(tmp)),
                capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 1)
            self.assertFalse(marker.exists(),
                             'a symlink into a worktree defeated the guard')
            self.assertIn('polecats', r.stdout)

    def test_an_ephemeral_target_falls_back_to_the_durable_default(self):
        """Refusing the path and dying are separable. Only refuse the path."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            marker = tmp / 'ran'
            child = self.polecat_bin(tmp, marker)
            self.durable_bin(tmp, marker)

            r = subprocess.run(
                ['bash', str(SUPERVISE)],
                env=supervise_env(tmp, HOME=str(tmp), BRIDGET_BIN=str(child),
                                  BRIDGET_MAX_SPAWNS='1'),
                capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 5, 'the durable bridget ran')
            self.assertEqual(marker.read_text().split(), ['durable'])
            self.assertIn('fell back', self.alerts(tmp))

    def test_the_override_supervises_the_worktree_path_deliberately(self):
        """A polecat smoke-testing its own build opts in, explicitly."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            marker = tmp / 'ran'
            child = self.polecat_bin(tmp, marker)
            r = subprocess.run(
                ['bash', str(SUPERVISE)],
                env=supervise_env(tmp, HOME=str(tmp), BRIDGET_BIN=str(child),
                                  BRIDGET_ALLOW_EPHEMERAL_BIN='1',
                                  BRIDGET_MAX_SPAWNS='1'),
                capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 5)
            self.assertEqual(marker.read_text().split(), ['ephemeral'])
            self.assertEqual(self.alerts(tmp), '', 'an opt-in must not page anyone')

    def test_a_target_deleted_mid_life_falls_back_instead_of_dying(self):
        """The startup guard cannot see this: the path was fine when we checked.

        This is why the target is re-resolved before every spawn rather than
        pinned once — a worktree is reaped while its bridget is supervised.
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            marker = tmp / 'ran'
            pinned = fake_bridget(
                tmp, f'echo pinned >> {marker}\nrm -f "$0"\nexit 1\n',
                name='live/bridget')
            self.durable_bin(tmp, marker)

            r = subprocess.run(
                ['bash', str(SUPERVISE)],
                env=supervise_env(tmp, HOME=str(tmp), BRIDGET_BIN=str(pinned),
                                  BRIDGET_MIN_BACKOFF='0', BRIDGET_MAX_SPAWNS='2',
                                  BRIDGET_HEALTHY_RUNTIME='9999'),
                capture_output=True, text=True, timeout=30)
            self.assertFalse(pinned.exists(), 'fixture sanity: the target vanished')
            self.assertEqual(r.returncode, 5, 'the durable bridget ran')
            self.assertEqual(marker.read_text().split(), ['pinned', 'durable'])
            self.assertIn('fell back', self.alerts(tmp))

    def test_a_target_that_turns_ephemeral_mid_life_falls_back(self):
        """Durable at 14:40, a symlink into a worktree at 14:48."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            marker = tmp / 'ran'
            worktree = self.polecat_bin(tmp, marker)
            # $0 is the symlink, so the child repoints the name it was invoked
            # by and never unlinks the file bash is reading it from.
            stage = fake_bridget(
                tmp,
                f'echo pinned >> {marker}\nrm -f "$0"\nln -s {worktree} "$0"\nexit 1\n',
                name='stage/bridget')
            pinned = tmp / 'live' / 'bridget'
            pinned.parent.mkdir(parents=True, exist_ok=True)
            pinned.symlink_to(stage)
            self.durable_bin(tmp, marker)

            r = subprocess.run(
                ['bash', str(SUPERVISE)],
                env=supervise_env(tmp, HOME=str(tmp), BRIDGET_BIN=str(pinned),
                                  BRIDGET_MIN_BACKOFF='0', BRIDGET_MAX_SPAWNS='2',
                                  BRIDGET_HEALTHY_RUNTIME='9999'),
                capture_output=True, text=True, timeout=30)
            self.assertTrue(pinned.is_symlink(), 'fixture sanity: it repointed')
            self.assertEqual(r.returncode, 5)
            self.assertEqual(marker.read_text().split(), ['pinned', 'durable'],
                             'the supervisor followed a symlink into a worktree')
            self.assertIn('fell back', self.alerts(tmp))


class SuperviseAlertTest(unittest.TestCase):
    """A supervisor that cannot exec its target must not fail quietly.

    The 2026-07-10 outage was not caused by the pinned path. It was caused by
    the pinned path failing in a `launchctl`-green, `pogo doctor`-green, 10s
    respawn loop that emitted nothing anyone read (mg-1679).
    """

    def vanishing_bin(self, tmp: Path) -> Path:
        return fake_bridget(tmp, 'rm -f "$0"\nexit 1\n', name='live/bridget')

    def run_until_fatal(self, tmp: Path, **overrides):
        return subprocess.run(
            ['bash', str(SUPERVISE)],
            env=supervise_env(tmp, HOME=str(tmp),
                              BRIDGET_BIN=str(self.vanishing_bin(tmp)),
                              BRIDGET_MIN_BACKOFF='0',
                              BRIDGET_HEALTHY_RUNTIME='9999', **overrides),
            capture_output=True, text=True, timeout=30)

    def test_an_unrunnable_target_with_no_fallback_is_loud_on_both_streams(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            r = self.run_until_fatal(tmp)
            self.assertEqual(r.returncode, 1)
            self.assertIn('FATAL', r.stdout)
            self.assertIn('FATAL', r.stderr,
                          'launchd routes stderr to bridget.err.log, which is '
                          'where a human tails a dying daemon')
            self.assertIn('cannot run bridget', (tmp / 'alerts').read_text(),
                          'the mayor was never told')

    def test_the_alert_is_rate_limited_across_respawns(self):
        """launchd respawns us every 10s. 360 mails an hour is its own silence.

        The stamp has to be on disk: each respawn is a fresh process, so an
        in-memory counter would rate-limit exactly nothing.
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            first = self.run_until_fatal(tmp)
            second = self.run_until_fatal(tmp)   # same stamp: the next respawn

            self.assertEqual([first.returncode, second.returncode], [1, 1])
            self.assertEqual((tmp / 'alerts').read_text().count('---'), 1,
                             'the second respawn re-mailed inside the cooldown')
            self.assertIn('throttled', second.stdout)
            self.assertIn('FATAL', second.stdout,
                          'throttling the mail must never throttle the log')

    def test_the_cooldown_expires(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self.run_until_fatal(tmp, BRIDGET_ALERT_COOLDOWN='0')
            self.run_until_fatal(tmp, BRIDGET_ALERT_COOLDOWN='0')
            self.assertEqual((tmp / 'alerts').read_text().count('---'), 2)

    def test_a_broken_notifier_does_not_stop_the_supervisor_from_exiting(self):
        """Alerting is best-effort. `mg` may not even be on launchd's PATH."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            r = subprocess.run(
                ['bash', str(SUPERVISE)],
                env=supervise_env(tmp, HOME=str(tmp),
                                  BRIDGET_BIN=str(tmp / 'nope'),
                                  BRIDGET_ALERT_CMD=str(tmp / 'no-such-notifier')),
                capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 1)
            self.assertIn('FATAL', r.stdout)

    def test_a_hung_notifier_cannot_hold_the_supervisor_open(self):
        """`mg` is allowed to be slow. Dying is not allowed to wait for it.

        Output goes to /dev/null rather than a pipe: an orphaned notifier would
        otherwise hold the pipe open and this test would measure the notifier's
        lifetime instead of the supervisor's.
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            hung = fake_bridget(tmp, 'sleep 60\n', name='hung-notifier')
            began = time.time()
            r = subprocess.run(
                ['bash', str(SUPERVISE)],
                env=supervise_env(tmp, HOME=str(tmp),
                                  BRIDGET_BIN=str(tmp / 'nope'),
                                  BRIDGET_ALERT_CMD=str(hung),
                                  BRIDGET_ALERT_TIMEOUT='2'),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
            elapsed = time.time() - began
            self.assertEqual(r.returncode, 1)
            self.assertLess(elapsed, 30,
                            'the supervisor waited out a wedged notifier')


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
                env=supervise_env(tmp, BRIDGET_BIN=str(child)),
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
                env=supervise_env(tmp, BRIDGET_BIN=str(child),
                                  BRIDGET_MIN_BACKOFF=str(backoff),
                                  BRIDGET_HEALTHY_RUNTIME='9999'),
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
                env=supervise_env(tmp, BRIDGET_BIN=str(child),
                                  BRIDGET_MIN_BACKOFF='0'),
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
