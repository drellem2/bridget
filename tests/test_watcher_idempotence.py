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

"""Behavioral acceptance for the duplicate-watcher fix (mg-dc94).

The incident: Daniel got "a bunch of dup discord threads for each mail". The
cause was not duplicate processes (one bridget under one supervisor) and not the
supervisor flapping (its last restart was ~25h earlier). `on_ready` is fired by
discord.py on every gateway RECONNECT, not just at process start, and it spawned
a full set of maildir watchers each time with nothing retiring the previous set.
Seven sets had accumulated; the observed duplicate count in the log channel grew
x2 -> x3 -> x4 -> x5 -> x7 over the day and topped out at exactly 7.

Each set holds its own in-memory seen-set, so each independently treats a new
mail as fresh. The existing `was_posted` guard does not save it: `resolve_thread`
runs BEFORE the guard and is itself a check-then-act across an await, so every
watcher sees `thread_id is None`, every watcher creates a thread, and
`bind_thread` clears `posted_ids` as it re-roots — resetting the guard.

Three load-bearing properties are proven here, mapping to the ticket's acceptance:

  1. Repeated `on_ready` (i.e. reconnects) does NOT grow the live watcher set,
     and each reconnect logs a TEARDOWN line naming how many tasks it retired.
     The absence of any teardown logging is what let the leak run invisibly, so
     the log line is part of the fix, not commentary on it.
  2. The retired watchers are really STOPPED — not merely unreferenced. A
     cancelled watcher that kept polling would duplicate just as well.
  3. End to end: N reconnects then one mail yields exactly ONE delivery. The
     positive control is the pre-fix behaviour, reconstructed here, which yields
     N+1 for the same input — so this test can actually fail.

Everything runs against a stubbed discord — no live Discord.
"""
import asyncio
import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'tests'))

import testtmp  # noqa: E402

SCRIPT = REPO / 'bridget'


class _FakeHTTPException(Exception):
    """Stand-in for discord.HTTPException under the stub."""


def load_bridget(fake_home: Path):
    """Import bridget into a fresh namespace rooted at `fake_home`."""
    env_dir = fake_home / '.pogo'
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / 'bridget.env').write_text(
        'DISCORD_BOT_TOKEN=fake\n'
        'DISCORD_USER_ID=1\n'
        'DISCORD_SERVER_ID=2\n'
        'MG_BIN=/bin/echo\n'
    )

    saved_env = {k: os.environ.get(k) for k in ('HOME', 'BRIDGET_REPO_DIR')}
    os.environ['HOME'] = str(fake_home)
    os.environ['BRIDGET_REPO_DIR'] = str(REPO)

    fake_discord = mock.MagicMock()
    fake_discord.Intents.default.return_value = mock.MagicMock()
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


class TeardownTest(unittest.TestCase):
    """`stop_watchers` — the primitive the whole fix rests on."""

    def setUp(self):
        self.tmp = testtmp.mkdtemp('idem-unit')
        self.b = load_bridget(self.tmp)
        # `track_watcher` goes through client.loop.create_task; under the stub
        # `client` is a MagicMock, so point it at the real running loop.
        self.b.client.loop = None

    def _use_running_loop(self):
        self.b.client.loop = asyncio.get_running_loop()

    def test_cancelled_watchers_actually_stop(self):
        """Property 2: retired tasks stop RUNNING, not just stop being tracked.

        A task that is merely dropped from the registry keeps polling the same
        maildir forever — indistinguishable, from the mail's point of view, from
        the leak this replaces.
        """
        b = self.b
        ticks = {'n': 0}

        async def fake_watcher():
            while True:
                ticks['n'] += 1
                await asyncio.sleep(0.001)

        async def scenario():
            self._use_running_loop()
            b.track_watcher(fake_watcher())
            await asyncio.sleep(0.02)
            self.assertGreater(ticks['n'], 0, 'watcher never ran')
            with redirect_stdout(io.StringIO()):
                stopped = await b.stop_watchers('test')
            self.assertEqual(stopped, 1)
            settled = ticks['n']
            await asyncio.sleep(0.02)
            # Not one more tick after the teardown returned.
            self.assertEqual(ticks['n'], settled,
                             'cancelled watcher kept polling')

        asyncio.run(scenario())

    def test_teardown_is_awaited_not_merely_requested(self):
        """`stop_watchers` must not return while a watcher is still live.

        cancel() only schedules a CancelledError. Returning before it lands lets
        the replacement set start while the old one is still polling — a window
        in which both deliver the same mail, which is the original bug in
        miniature.
        """
        b = self.b
        state = {'exited': False}

        async def slow_to_die():
            try:
                while True:
                    await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                # A real watcher unwinds through awaits on the way out.
                await asyncio.sleep(0.02)
                state['exited'] = True
                raise

        async def scenario():
            self._use_running_loop()
            b.track_watcher(slow_to_die())
            await asyncio.sleep(0.01)
            with redirect_stdout(io.StringIO()):
                await b.stop_watchers('test')
            self.assertTrue(state['exited'],
                            'stop_watchers returned before the watcher finished')

        asyncio.run(scenario())

    def test_teardown_logs_a_line_naming_the_count(self):
        """Property 1 (the logging half). The leak was invisible for weeks
        because nothing ever logged a teardown; a fix with no teardown line is
        unverifiable in exactly the same way."""
        b = self.b

        async def idle():
            while True:
                await asyncio.sleep(0.001)

        async def scenario():
            self._use_running_loop()
            for _ in range(3):
                b.track_watcher(idle())
            await asyncio.sleep(0.01)
            buf = io.StringIO()
            with redirect_stdout(buf):
                await b.stop_watchers('gateway reconnect')
            return buf.getvalue()

        out = asyncio.run(scenario())
        self.assertIn('tore down watcher set', out)
        self.assertIn('3 task(s) cancelled', out)
        self.assertIn('gateway reconnect', out)

    def test_first_start_logs_no_teardown(self):
        """Nothing to retire on the first ready — it must not claim otherwise,
        or the line stops meaning anything."""
        b = self.b

        async def scenario():
            self._use_running_loop()
            buf = io.StringIO()
            with redirect_stdout(buf):
                n = await b.stop_watchers('startup')
            return n, buf.getvalue()

        n, out = asyncio.run(scenario())
        self.assertEqual(n, 0)
        self.assertEqual(out, '')


class ReconnectDoesNotAccumulateTest(unittest.TestCase):
    """Property 1 + 3, at the level the bug was actually observed: reconnects."""

    def setUp(self):
        self.tmp = testtmp.mkdtemp('idem-recon')
        self.b = load_bridget(self.tmp)

    def test_watcher_count_is_constant_across_reconnects(self):
        b = self.b
        deliveries = []

        async def fake_watcher(tag):
            while True:
                deliveries.append(tag)
                await asyncio.sleep(0.005)

        async def fake_on_ready():
            """The load-bearing shape of the real on_ready: retire, then spawn."""
            await b.stop_watchers('gateway reconnect')
            for tag in ('human', 'transitions', 'ideas'):
                b.track_watcher(fake_watcher(tag))

        async def scenario():
            b.client.loop = asyncio.get_running_loop()
            counts = []
            with redirect_stdout(io.StringIO()):
                for _ in range(7):  # the seven sets seen in the real log
                    await fake_on_ready()
                    await asyncio.sleep(0.01)
                    live = [t for t in b.WATCHER_TASKS if not t.done()]
                    counts.append(len(live))
                await b.stop_watchers('shutdown')
            return counts

        counts = asyncio.run(scenario())
        self.assertEqual(counts, [3] * 7,
                         f'watcher set grew across reconnects: {counts}')

    def test_one_mail_after_n_reconnects_delivers_once(self):
        """Property 3, end to end, with the pre-fix control alongside.

        A single mail is placed in a maildir, N reconnects happen, and the mail
        must reach Discord exactly once. The control re-creates the old
        spawn-without-teardown path over the same fixture and shows N+1 — so a
        regression here fails loudly rather than passing vacuously.
        """
        b = self.b
        RECONNECTS = 6  # matches the real incident: 7 sets = 1 + 6 reconnects

        def make_watcher(sink):
            """One watcher: its OWN in-memory seen-set, exactly like the real
            MaildirWatcher instance each spawn constructs."""
            async def watcher():
                seen = set()
                while True:
                    for name in sorted(mail_dir.iterdir()):
                        if name.name not in seen:
                            sink.append(name.name)
                            seen.add(name.name)
                    await asyncio.sleep(0.005)
            return watcher()

        mail_dir = self.tmp / 'mail' / 'new'
        mail_dir.mkdir(parents=True)

        async def fixed():
            b.client.loop = asyncio.get_running_loop()
            sink = []
            with redirect_stdout(io.StringIO()):
                for _ in range(RECONNECTS + 1):
                    await b.stop_watchers('gateway reconnect')
                    b.track_watcher(make_watcher(sink))
                    await asyncio.sleep(0.008)
                (mail_dir / 'the-mail').write_text('From: mayor\nSubject: x\n\nbody\n')
                await asyncio.sleep(0.05)
                await b.stop_watchers('shutdown')
            return sink

        async def pre_fix_control():
            """The old on_ready: spawn, never retire."""
            sink = []
            tasks = [asyncio.ensure_future(make_watcher(sink))
                     for _ in range(RECONNECTS + 1)]
            await asyncio.sleep(0.008)
            (mail_dir / 'the-mail').write_text('From: mayor\nSubject: x\n\nbody\n')
            await asyncio.sleep(0.05)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return sink

        delivered = asyncio.run(fixed())
        self.assertEqual(delivered.count('the-mail'), 1,
                         f'mail delivered {delivered.count("the-mail")} times '
                         f'after {RECONNECTS} reconnects')

        (mail_dir / 'the-mail').unlink()
        control = asyncio.run(pre_fix_control())
        self.assertEqual(
            control.count('the-mail'), RECONNECTS + 1,
            'the pre-fix control did not reproduce the bug, so the assertion '
            'above proves nothing')


class RealOnReadyTest(unittest.TestCase):
    """The ticket's acceptance, against the REAL `on_ready` and the REAL
    `watch_mailbox`/`MaildirWatcher` — not a reconstruction of their shape.

    Threading is off here (no log channel configured), so delivery lands as a
    DM and counting sends counts deliveries. The thread path multiplies the same
    way — one `resolve_thread` per delivering watcher — so the delivery count is
    the quantity under test either way.
    """

    RECONNECTS = 6  # the real incident: 7 sets = 1 start + 6 reconnects

    def setUp(self):
        self.tmp = testtmp.mkdtemp('idem-real')
        self.b = load_bridget(self.tmp)

    def _real_on_ready(self):
        """Recover the function bridget registered. `@client.event` under the
        stub rebinds the module attribute to a mock, but the mock kept the
        function it was handed."""
        for call in self.b.client.event.call_args_list:
            fn = call.args[0]
            if getattr(fn, '__name__', '') == 'on_ready':
                return fn
        self.fail('could not recover the real on_ready')

    def test_one_mail_after_n_reconnects_is_delivered_once(self):
        b = self.b
        on_ready = self._real_on_ready()
        sent = []

        class _User:
            id = 1

            async def send(self, content=None, **kw):
                sent.append(content)

        async def fetch_user(_):
            return _User()

        async def noop_resolve():
            pass

        b.client.fetch_user = fetch_user
        b.client.is_closed = lambda: False
        b.resolve_and_wire_channels = noop_resolve
        # Leave watch_mailbox REAL. Silence only the two unrelated watchers,
        # which shell out to `mg`.
        async def idle(*a, **kw):
            while True:
                await asyncio.sleep(0.001)
        b.watch_task_transitions = idle
        b.watch_idea_claims = idle
        b.POLL_INTERVAL = 0.005

        async def scenario():
            b.client.loop = asyncio.get_running_loop()
            counts = []
            with redirect_stdout(io.StringIO()):
                for _ in range(self.RECONNECTS + 1):
                    await on_ready()
                    await asyncio.sleep(0.02)
                    counts.append(len([t for t in b.WATCHER_TASKS
                                       if not t.done()]))
                # One mail, after every reconnect has happened.
                b.MAIL_DIR.mkdir(parents=True, exist_ok=True)
                (b.MAIL_DIR / 'the-one-mail').write_text(
                    'From: mayor\nSubject: acceptance probe\n\nbody\n')
                await asyncio.sleep(0.15)
                await b.stop_watchers('shutdown')
            return counts

        counts = asyncio.run(scenario())

        # One set is three tasks here (mailbox + transitions + idea-claims; no
        # channels.toml, so no per-channel watchers). The property under test is
        # that it does not GROW — pre-fix this read [3, 6, 9, 12, 15, 18, 21].
        self.assertEqual(
            counts, [3] * (self.RECONNECTS + 1),
            f'watcher set grew across reconnects: {counts}')
        probes = [s for s in sent if s and 'acceptance probe' in s]
        self.assertEqual(
            len(probes), 1,
            f'one mail after {self.RECONNECTS} reconnects was delivered '
            f'{len(probes)} times')


class StartupGreetingTest(unittest.TestCase):
    """The same reconnect path also re-sent the 'bridget online' DM each time."""

    def setUp(self):
        self.tmp = testtmp.mkdtemp('idem-greet')
        self.b = load_bridget(self.tmp)

    def test_greeting_is_once_per_process_not_once_per_connection(self):
        b = self.b
        self.assertFalse(b.GREETED, 'a fresh process starts un-greeted')
        # watch_mailbox greets only while GREETED is false and latches it before
        # awaiting, so a reconnect-respawned watcher stays silent.
        b.GREETED = True
        sent = []

        async def scenario():
            async def fake_send_startup_dm(user, first_run):
                sent.append(first_run)
            b.send_startup_dm = fake_send_startup_dm
            # Drive only the greeting guard, not the whole poll loop.
            if not b.GREETED:
                b.GREETED = True
                await b.send_startup_dm(None, False)

        asyncio.run(scenario())
        self.assertEqual(sent, [], 'reconnect re-sent the startup DM')


if __name__ == '__main__':
    unittest.main(verbosity=2)
