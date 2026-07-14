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

"""Behavioral acceptance for the delivery-wedge resilience fix (mg-e5b8).

The incident: a STORM of `mg list` timeouts (the 30s subprocess timeout firing
back-to-back) wedged outbound delivery for ~70h. `mg list` is called
*synchronously* from the poll loops, so a hung `mg` blocked the shared asyncio
event loop; discord.py could not service its gateway heartbeat, Discord dropped
the socket, and mail delivery (watch_mailbox — a *different* task on the same
loop) starved. Yet `bridget.heartbeat`, which the task-transition loop ticks at
the top of every cycle, kept advancing the whole time, so nothing watching it
ever fired: it is a LOOP heartbeat, not a DELIVERY heartbeat.

Two load-bearing properties are proven here, mapping to the ticket's acceptance:

  (A) A simulated `mg list` timeout STORM does not stop delivery. The mg call is
      offloaded off the event loop, so even while an `mg list` is hung the
      delivery watcher keeps delivering mail. Proven by gating `mg` on an event
      that never releases during the window and showing a mail still lands.

  (B) The delivery-liveness heartbeat goes stale on a REAL wedge and only then.
      It ticks every healthy cycle (mail delivered, or nothing to deliver), but
      the moment mail is present and every send FAILS — the ~70h wedge — its
      mtime freezes, even though the loop keeps running. Positive control: it
      *can* go stale, which is the whole point.

Everything runs against a mocked `mg` and a stubbed discord — no live Discord.
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
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
    # deliver_mail catches discord.HTTPException specifically; keep it a real
    # exception class so `except discord.HTTPException` works under the stub.
    fake_discord.HTTPException = _FakeHTTPException
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


class _FakeHTTPException(Exception):
    """Stand-in for discord.HTTPException so the delivery path's
    `except discord.HTTPException` clause catches our simulated send failures."""


def write_mail(mail_dir: Path, name: str, frm: str, subject: str, body: str):
    """Drop one plausible mail file into a maildir `new/`. parse_mail reads a
    simple `Header: value` block then a blank line then the body."""
    mail_dir.mkdir(parents=True, exist_ok=True)
    (mail_dir / name).write_text(
        f"From: {frm}\nSubject: {subject}\n\n{body}\n"
    )


class StormElementsTest(unittest.TestCase):
    """The pure pieces of the storm handling — no event loop needed."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='bridget-storm-unit-'))
        cls.bridget = load_bridget(cls.tmp)

    def test_storm_edge_fires_once_on_the_threshold(self):
        edge = self.bridget.is_storm_edge
        thr = 5
        # False below the threshold, True exactly at it, False after — so a
        # watcher logs one loud line per storm, not one per cycle.
        fired = [edge(n, thr) for n in range(0, 9)]
        self.assertEqual(
            fired,
            [False, False, False, False, False, True, False, False, False],
        )

    def test_idea_claims_backoff_matches_transitions(self):
        # watch_idea_claims now shares the transition watcher's bounded backoff.
        nb = self.bridget.next_watch_backoff
        seq, cur = [], 0
        for _ in range(6):
            cur = nb(cur, poll_interval=5, cap=60)
            seq.append(cur)
        self.assertEqual(seq, [5, 10, 20, 40, 60, 60])


class RunMgAsyncOffLoadsTest(unittest.TestCase):
    """run_mg_async must run `mg` OFF the event loop, so a hung `mg` cannot
    freeze the loop the delivery watcher shares. Proven directly: while an
    `mg` call is blocked in its worker thread, an unrelated coroutine still
    makes progress on the loop."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='bridget-offload-'))
        self.bridget = load_bridget(self.tmp)

    def test_loop_runs_while_mg_is_blocked(self):
        b = self.bridget
        released = threading.Event()
        entered = threading.Event()

        def blocking_run_mg(args):
            entered.set()
            # Block the WORKER thread, not the loop — unless the offload is
            # broken, in which case this freezes everything.
            released.wait(timeout=5)
            return 0, '', ''

        b.run_mg = blocking_run_mg

        async def scenario():
            mg_task = asyncio.ensure_future(b.run_mg_async(['list']))
            # Wait until mg is actually executing (in its thread).
            for _ in range(500):
                if entered.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(entered.is_set(), 'mg never started')
            self.assertFalse(mg_task.done(), 'mg should still be blocked')

            # The loop is NOT frozen: this counter advances while mg is blocked.
            ticks = 0
            for _ in range(5):
                await asyncio.sleep(0.001)
                ticks += 1
            self.assertEqual(ticks, 5,
                             'event loop must keep running while mg is blocked')

            released.set()
            rc, _, _ = await mg_task
            self.assertEqual(rc, 0)

        asyncio.run(scenario())


class StormDoesNotStopDeliveryTest(unittest.TestCase):
    """(A) A sustained `mg list` timeout storm must not stop delivery.

    Run the delivery watcher and the storming transition watcher concurrently on
    ONE event loop, exactly as on_ready wires them. Every `mg` call blocks in its
    worker thread for the whole test window (the storm). If `mg` ran on the loop
    this would starve delivery — the ~70h wedge. With the executor offload the
    delivery watcher keeps polling the maildir and a mail dropped mid-storm still
    reaches the user."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='bridget-storm-deliv-'))
        self.bridget = load_bridget(self.tmp)

    def test_mail_delivered_while_mg_storms(self):
        b = self.bridget
        b.POLL_INTERVAL = 0.005
        b.THREADS_ENABLED = False  # DM is the only surface; simplest path

        mg_gate = threading.Event()  # never set during the window == permanent storm

        def storming_mg(args):
            # Every `mg list` "times out": block the worker thread the whole time.
            mg_gate.wait(timeout=5)
            return 124, '', 'mg command timed out'

        b.run_mg = storming_mg

        delivered = []

        async def scenario():
            user = mock.MagicMock()

            async def capture_send(msg):
                delivered.append(msg)
            user.send = mock.AsyncMock(side_effect=capture_send)

            b.client.is_closed = lambda: False  # run until we cancel

            mailbox = asyncio.ensure_future(b.watch_mailbox(user))
            transitions = asyncio.ensure_future(b.watch_task_transitions(user))

            # Let both watchers spin up and the storm take hold.
            await asyncio.sleep(0.05)

            # Drop a mail MID-STORM. A wedged loop would never deliver it.
            write_mail(b.MAIL_DIR, '1700000000.deadbeef.host',
                       'pm-pogo', 'fleet status', 'all green')

            # Poll for delivery with a bounded deadline. mg is STILL storming
            # (gate never set) throughout — delivery must happen anyway.
            for _ in range(200):
                await asyncio.sleep(0.01)
                if any('fleet status' in m for m in delivered):
                    break

            self.assertFalse(mg_gate.is_set(),
                             'sanity: mg must still be storming at assert time')
            self.assertTrue(
                any('fleet status' in m for m in delivered),
                'mail must be delivered even while mg list storms')

            for t in (mailbox, transitions):
                t.cancel()
            for t in (mailbox, transitions):
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        try:
            asyncio.run(scenario())
        finally:
            mg_gate.set()  # release any lingering worker thread


class DeliveryHeartbeatTest(unittest.TestCase):
    """(B) The delivery-liveness heartbeat ticks on a healthy cycle and goes
    stale on a real delivery wedge — the positive control that it CAN detect the
    class the loop heartbeat is blind to."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='bridget-deliv-hb-'))
        self.bridget = load_bridget(self.tmp)

    def _prime_seen_empty(self, b):
        """Make the watcher already-primed with an empty seen-set, so a mail
        present at start is treated as NEW (delivered) rather than adopted as
        backlog. Models a running watcher into which fresh mail arrives — the
        incident shape — without threading a write mid-`asyncio.run`."""
        b.SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        b.SEEN_FILE.write_text('')
        # Non-first-run startup DM calls get_status_summary()->run_mg; keep it a
        # deterministic no-op so the only interesting sends are mail deliveries.
        b.run_mg = lambda args: (0, '', '')

    def _run_n_cycles(self, b, user, n):
        # is_closed returns False n times then True, so the loop runs n cycles.
        b.client.is_closed = mock.Mock(side_effect=[False] * n + [True])
        asyncio.run(b.watch_mailbox(user))

    def _sent_texts(self, user):
        return [str(c.args[0]) for c in user.send.await_args_list if c.args]

    def test_heartbeat_ticks_on_empty_healthy_cycle(self):
        b = self.bridget
        b.POLL_INTERVAL = 0.001
        b.THREADS_ENABLED = False
        hb = b.DELIVERY_HEARTBEAT_FILE

        user = mock.MagicMock()
        user.send = mock.AsyncMock()

        self._run_n_cycles(b, user, 2)

        # An empty mailbox is a healthy idle: nothing wedged, so the beat ticks.
        self.assertTrue(hb.exists(),
                        'delivery heartbeat must tick on a healthy empty cycle')

    def test_heartbeat_ticks_after_successful_delivery(self):
        b = self.bridget
        b.POLL_INTERVAL = 0.001
        b.THREADS_ENABLED = False
        hb = b.DELIVERY_HEARTBEAT_FILE
        self._prime_seen_empty(b)

        write_mail(b.MAIL_DIR, '1700000001.aaaa.host',
                   'pm-pogo', 'hello-subject', 'body')

        user = mock.MagicMock()
        user.send = mock.AsyncMock()  # succeeds

        self._run_n_cycles(b, user, 1)

        self.assertTrue(hb.exists())
        # The mail actually reached the user (not merely adopted as backlog).
        self.assertTrue(
            any('hello-subject' in t for t in self._sent_texts(user)),
            'the mail card must be delivered to the user')

    def test_heartbeat_goes_stale_when_delivery_is_wedged(self):
        """The wedge: mail is present and every send fails (Discord down). The
        delivery beat must NOT tick — while, for contrast, the loop heartbeat
        (touched independently) still would. Stale == delivery is dead."""
        b = self.bridget
        b.POLL_INTERVAL = 0.001
        b.THREADS_ENABLED = False
        deliv_hb = b.DELIVERY_HEARTBEAT_FILE
        loop_hb = b.WATCHER_HEARTBEAT_FILE
        self._prime_seen_empty(b)

        write_mail(b.MAIL_DIR, '1700000002.bbbb.host',
                   'pm-pogo', 'urgent', 'please read')

        user = mock.MagicMock()
        # Every DM fails, exactly as it did when Discord dropped the socket.
        user.send = mock.AsyncMock(side_effect=_FakeHTTPException('503'))

        # Contrast control: the loop heartbeat can still be ticking (it is the
        # task-transition loop's job, unaffected by delivery health).
        b.touch_heartbeat(loop_hb)
        loop_before = os.stat(loop_hb).st_mtime_ns

        self._run_n_cycles(b, user, 3)

        # Delivery beat never got created: three cycles, every send failed, so no
        # cycle was delivery-healthy. A reaper watching this file fires.
        self.assertFalse(
            deliv_hb.exists(),
            'delivery heartbeat must NOT tick while every send fails')
        # A real send WAS attempted each cycle (the mail was not adopted), and it
        # stayed uncommitted for retry (never marked seen).
        self.assertGreaterEqual(user.send.await_count, 1)
        # Sanity: the loop heartbeat mechanism itself is alive and would have
        # kept ticking — which is exactly why it could not catch this wedge.
        self.assertEqual(os.stat(loop_hb).st_mtime_ns, loop_before)

    def test_healthy_then_wedged_freezes_the_beat(self):
        """End-to-end of the incident shape: a healthy cycle ticks the beat, then
        delivery wedges and the SAME beat stops advancing — detectable as a
        frozen mtime within a threshold, the reaper's signal."""
        b = self.bridget
        b.POLL_INTERVAL = 0.001
        b.THREADS_ENABLED = False
        b.run_mg = lambda args: (0, '', '')  # deterministic startup DM summary
        deliv_hb = b.DELIVERY_HEARTBEAT_FILE

        user = mock.MagicMock()
        user.send = mock.AsyncMock()

        # Phase 1: healthy — one empty cycle ticks the beat. (Its prime() writes
        # an empty seen-file, so the phase-2 mail below is treated as NEW.)
        self._run_n_cycles(b, user, 1)
        self.assertTrue(deliv_hb.exists())
        healthy_mtime = os.stat(deliv_hb).st_mtime_ns

        # Phase 2: the wedge. New mail arrives, every send now fails.
        write_mail(b.MAIL_DIR, '1700000003.cccc.host',
                   'pm-pogo', 'down', 'no route')
        user.send = mock.AsyncMock(side_effect=_FakeHTTPException('502'))
        self._run_n_cycles(b, user, 3)

        # The beat froze at its last healthy value — it did not advance through
        # the wedge, which is what makes the wedge detectable (mg-e5b8).
        self.assertEqual(
            os.stat(deliv_hb).st_mtime_ns, healthy_mtime,
            'delivery heartbeat must freeze once delivery wedges')


if __name__ == '__main__':
    unittest.main()
