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

"""Behavioral acceptance for the watch_task_transitions silent-death fix (mg-3499).

The incident: a single transient `mg list` timeout (fires even at rest, mg lists
in ~0.01s) killed the task-transition watcher thread. The bridget *process* stayed
alive and logged in, so launchd/KeepAlive/bridget-supervise never restarted it —
the pogo channel went silent for 44 minutes until a manual kickstart.

Two load-bearing properties are proven here, exactly as pm-pogo framed them:

  1. Inject ONE `mg list` timeout and prove the watcher retries, does NOT exit,
     and KEEPS POSTING transitions afterward — and that the liveness heartbeat's
     mtime keeps ticking across the timeout cycle.
  2. Kill the watcher BY PID and prove the heartbeat then goes stale (its mtime
     stops advancing), so a dead watcher is DETECTABLE the way the d18b reaper
     detects it: state-file mtime freshness against a known period.

Everything runs against a mocked `mg` and a stubbed discord — no live Discord.
"""
import asyncio
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / 'bridget'


def load_bridget(fake_home: Path):
    """Import bridget into a fresh namespace rooted at `fake_home`."""
    env_dir = fake_home / '.pogo'
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / 'bridget.env').write_text(
        'DISCORD_BOT_TOKEN=fake\n'
        'DISCORD_USER_ID=1\n'
        'DISCORD_SERVER_ID=2\n'
        # A real file so config validation passes; run_mg is mocked anyway.
        'MG_BIN=/bin/echo\n'
    )

    saved_env = {k: os.environ.get(k) for k in ('HOME', 'BRIDGET_REPO_DIR')}
    os.environ['HOME'] = str(fake_home)
    os.environ['BRIDGET_REPO_DIR'] = str(REPO)

    fake_discord = mock.MagicMock()
    fake_discord.Intents.default.return_value = mock.MagicMock()
    saved_discord = sys.modules.get('discord')
    sys.modules['discord'] = fake_discord
    saved_bridget = sys.modules.pop('bridget', None)

    try:
        loader = SourceFileLoader('bridget', str(SCRIPT))
        spec = importlib.util.spec_from_loader('bridget', loader)
        bridget = importlib.util.module_from_spec(spec)
        loader.exec_module(bridget)
        return bridget
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if saved_discord is not None:
            sys.modules['discord'] = saved_discord
        else:
            sys.modules.pop('discord', None)
        if saved_bridget is not None:
            sys.modules['bridget'] = saved_bridget
        else:
            sys.modules.pop('bridget', None)


def task_line(tid, status, **kw):
    return json.dumps({'id': tid, 'type': 'task', 'status': status,
                       'title': f'title of {tid}', **kw})


class BackoffEscalationTest(unittest.TestCase):
    """The retry backoff grows and is capped — the defensible, pure part."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='bridget-backoff-test-'))
        cls.bridget = load_bridget(cls.tmp)

    def test_backoff_grows_from_poll_interval_and_caps(self):
        nb = self.bridget.next_watch_backoff
        # Grows from one poll interval, doubling, until the cap holds it.
        seq, cur = [], 0
        for _ in range(6):
            cur = nb(cur, poll_interval=5, cap=60)
            seq.append(cur)
        self.assertEqual(seq, [5, 10, 20, 40, 60, 60])

    def test_backoff_resets_from_zero(self):
        nb = self.bridget.next_watch_backoff
        self.assertEqual(nb(0, poll_interval=3, cap=60), 3)


class SurvivesTimeoutTest(unittest.TestCase):
    """Inject ONE `mg list` timeout: the watcher must retry, keep posting, and
    keep its heartbeat mtime ticking across the timeout."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='bridget-survive-test-'))
        self.bridget = load_bridget(self.tmp)

    def test_single_timeout_is_survived_and_posting_resumes(self):
        b = self.bridget
        # Pre-seed the state cache so the watcher is primed (posts on tick one)
        # and mg-x transitioning available->claimed is a genuine announcement.
        b.save_task_states({'mg-x': 'available'})

        # Tiny but non-zero poll so consecutive heartbeat mtimes are distinct.
        b.POLL_INTERVAL = 0.001

        # Cycle 1: a lone `mg list` timeout (rc 124). Cycle 2: mg-x is now
        # claimed -> must post. Then close the client to end the loop.
        hb_mtimes = []
        heartbeat = b.WATCHER_HEARTBEAT_FILE

        def fake_run_mg(args):
            # Recorded AFTER touch_heartbeat() ran at the top of this cycle.
            hb_mtimes.append(os.stat(heartbeat).st_mtime_ns)
            if len(hb_mtimes) == 1:
                return 124, '', 'mg command timed out'
            return 0, task_line('mg-x', 'claimed') + '\n', ''

        b.run_mg = fake_run_mg
        b.client.is_closed = mock.Mock(side_effect=[False, False, True])

        user = mock.MagicMock()
        user.send = mock.AsyncMock()

        asyncio.run(b.watch_task_transitions(user))

        # Survived the timeout: both cycles ran (the thread did not exit on the
        # 124), the second producing a real listing.
        self.assertEqual(len(hb_mtimes), 2,
                         'watcher must run a second cycle after the timeout')

        # Kept posting: the post-timeout transition reached the user.
        self.assertEqual(user.send.await_count, 1)
        posted = user.send.await_args.args[0]
        self.assertIn('mg-x', posted)
        self.assertIn('claimed', posted)

        # Heartbeat ticked across the timeout: the mtime advanced from the
        # timeout cycle to the next one.
        self.assertTrue(heartbeat.exists())
        self.assertGreater(hb_mtimes[1], hb_mtimes[0],
                           'heartbeat mtime must keep ticking across the timeout')

    def test_heartbeat_ticks_even_when_every_cycle_times_out(self):
        """A sustained timeout burst still keeps the heartbeat alive — the whole
        point: a degraded watcher is distinguishable from a dead one."""
        b = self.bridget
        b.POLL_INTERVAL = 0.001
        heartbeat = b.WATCHER_HEARTBEAT_FILE

        hb_mtimes = []

        def always_timeout(args):
            hb_mtimes.append(os.stat(heartbeat).st_mtime_ns)
            return 124, '', 'mg command timed out'

        b.run_mg = always_timeout
        b.client.is_closed = mock.Mock(side_effect=[False, False, False, True])

        user = mock.MagicMock()
        user.send = mock.AsyncMock()

        asyncio.run(b.watch_task_transitions(user))

        # Three timeout cycles all ran (never exited) and each ticked the beat.
        self.assertEqual(len(hb_mtimes), 3)
        self.assertGreater(hb_mtimes[-1], hb_mtimes[0])


# The driver a real subprocess runs: a watcher whose mg always returns an empty
# listing, ticking its heartbeat every poll until the process is killed.
_WATCHER_DRIVER = r'''
import asyncio, importlib.util, os, sys
from importlib.machinery import SourceFileLoader
from unittest import mock

fake_discord = mock.MagicMock()
fake_discord.Intents.default.return_value = mock.MagicMock()
sys.modules['discord'] = fake_discord

loader = SourceFileLoader('bridget', os.environ['BRIDGET_SCRIPT'])
spec = importlib.util.spec_from_loader('bridget', loader)
bridget = importlib.util.module_from_spec(spec)
loader.exec_module(bridget)

bridget.POLL_INTERVAL = 0.05
bridget.client.is_closed = lambda: False        # run until killed
bridget.run_mg = lambda args: (0, '', '')        # empty board, no posting

user = mock.MagicMock()
user.send = mock.AsyncMock()
asyncio.run(bridget.watch_task_transitions(user))
'''


class KilledWatcherGoesStaleTest(unittest.TestCase):
    """Kill the watcher BY PID and prove the heartbeat mtime stops advancing —
    the signal the d18b reaper keys on to restart a silently-dead watcher."""

    def _mtime_ns(self, path):
        return os.stat(path).st_mtime_ns

    def test_heartbeat_stale_after_pid_kill(self):
        tmp = Path(tempfile.mkdtemp(prefix='bridget-kill-test-'))
        pogo = tmp / '.pogo'
        pogo.mkdir(parents=True)
        (pogo / 'bridget.env').write_text(
            'DISCORD_BOT_TOKEN=fake\n'
            'DISCORD_USER_ID=1\n'
            'DISCORD_SERVER_ID=2\n'
            'MG_BIN=/bin/echo\n'
        )
        heartbeat = pogo / 'health' / 'bridget.heartbeat'

        driver = tmp / 'driver.py'
        driver.write_text(_WATCHER_DRIVER)

        env = dict(os.environ)
        env['HOME'] = str(tmp)
        env['BRIDGET_REPO_DIR'] = str(REPO)
        env['BRIDGET_SCRIPT'] = str(SCRIPT)

        proc = subprocess.Popen(
            [sys.executable, str(driver)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            # Wait for the heartbeat to appear and confirm it is ADVANCING while
            # the watcher lives (mtime moves between two reads).
            deadline = time.monotonic() + 10
            first = None
            while time.monotonic() < deadline:
                if heartbeat.exists():
                    if first is None:
                        first = self._mtime_ns(heartbeat)
                    elif self._mtime_ns(heartbeat) > first:
                        break
                time.sleep(0.02)
            else:
                out, err = proc.communicate(timeout=5)
                self.fail('heartbeat never advanced while watcher was alive; '
                          f'stderr:\n{err.decode(errors="replace")}')

            self.assertIsNone(proc.poll(), 'watcher should still be running')

            # Kill BY PID — the launchd/pkill-free way the reaper never has to,
            # simulating the silent death (SIGKILL: no graceful shutdown).
            alive_mtime = self._mtime_ns(heartbeat)
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)

            # Let several poll intervals pass. A live watcher would have ticked
            # ~6 times; a dead one touches nothing.
            time.sleep(0.6)
            stale_mtime = self._mtime_ns(heartbeat)

            self.assertEqual(
                stale_mtime, alive_mtime,
                'heartbeat mtime must stop advancing once the watcher is dead')
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()


if __name__ == '__main__':
    unittest.main()
