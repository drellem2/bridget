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

"""Behavioral acceptance for the RECEIVING half's instrument (mg-8961).

The defect had two halves and they fail differently.

**The record.** `grep -n "print(" bridget`, restricted to `on_message` /
`handle_command` / `reply_in_conversation` / `handle_channel_message`, returned
zero. A message from Daniel that arrived, was handled, was refused, or was
ignored all wrote the same nothing, so the file could not answer "was it
received?" at all. R1-R6 below drive the REAL router and show each branch
writing itself down; R5 is the PRE-FIX CONTROL, which switches the receipts off
and reproduces the byte-identical silence.

**The loss.** Two of Daniel's DMs died on 2026-08-19 inside the mg-3f08
resolver wedge:

    08-15T15:55:06Z  DM  "mail have pm-onethird mail me an update please"  -> mayor/  OK
    08-19T07:37:05Z  DM  "Mail pause one third once the executive ..."     -> NOWHERE
    08-19T07:39:59Z  DM  "Mail pause one third once the executive ..."     -> NOWHERE

The gateway was down, the process was SIGTERMed and re-IDENTIFYed five seconds
later, and Discord replays nothing across a fresh IDENTIFY. A log line cannot
record what never arrived — so C1 reproduces exactly that shape (messages that
reach the REST history and never reach `on_message`) and requires that the
catch-up sweep recovers them, and C7 is the PRE-FIX CONTROL: with the sweep off
the same two messages stay lost, and all bridget can do is say so.

Everything the ticket asks to be provable is paired with the way it can fail:

    R1-R4  every inbound branch writes a receipt      (the DM, the unrecognised
                                                       command, the silently
                                                       ignored channel, a thread)
    R5     receipts off -> the silence, reproduced    (PRE-FIX CONTROL)
    R6     a handler that RAISES still leaves a done  (why there are two lines)
    G1-G5  RESUME and re-IDENTIFY read differently, and the tokens do not
           collide with the outbound ones mg-879c added
    S1-S5  the resume point is monotonic, owner-only, and survives a corrupt row
    P1-P3  the plan bootstraps, resumes, and clamps — and NAMES the clamp
    C1-C9  the sweep recovers the measured loss, exactly once, within its
           bounds, says when a bound bit, survives an unreadable surface, and
           cannot double-replay when two sweeps race

C9 is the remedy examined for its own defect: `on_ready` is dispatched as a
task and fires again on every reconnect, so two sweeps can overlap — a
check-then-act over the same resume point, which is the shape of the loss being
repaired.

Everything runs against a stubbed discord — no live Discord, no live mg.
"""
import asyncio
import importlib.util
import io
import json
import os
import stat
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'tests'))
sys.path.insert(0, str(REPO))

import testtmp  # noqa: E402

from bridget_core.inbound import (  # noqa: E402
    GatewayJournal,
    SeenStore,
    channel_surface,
    dm_surface,
    plan_catchup,
)

SCRIPT = REPO / 'bridget'

#: The configured human. Everything not from this id is dropped before the
#: receipt — see the comment in `on_message` for why that is a cost, not a win.
HUMAN = 4242


class FakeHTTPException(Exception):
    """Stand-in for discord.HTTPException under the stub."""


class FakeForbidden(FakeHTTPException):
    """Stand-in for discord.Forbidden — the mg-8614 Manage-Channels shape."""


class FakeNotFound(FakeHTTPException):
    """Stand-in for discord.NotFound."""


class FakeObject:
    """Stand-in for `discord.Object(id=...)`, the `after=` cursor."""

    def __init__(self, id):        # noqa: A002 — discord.py's own signature
        self.id = id


class FakeAuthor:
    def __init__(self, id, bot=False):   # noqa: A002
        self.id = id
        self.bot = bot


class FakeMessage:
    def __init__(self, id, content, channel, author=None):   # noqa: A002
        self.id = id
        self.content = content
        self.channel = channel
        self.author = author if author is not None else FakeAuthor(HUMAN)
        self.reactions = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def remove_reaction(self, emoji, member):
        if emoji in self.reactions:
            self.reactions.remove(emoji)


class _History:
    """An async iterator over a fixed message list."""

    def __init__(self, messages):
        self._messages = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        # A real REST page awaits I/O, and the suspension is load-bearing for
        # C9: without a genuine yield point two "concurrent" sweeps would run
        # to completion one after the other and the race could not be staged.
        await asyncio.sleep(0)
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class _Channel:
    """A Discord channel that remembers what it holds and what was sent to it."""

    def __init__(self, id, messages=None):   # noqa: A002
        self.id = id
        self.messages = list(messages or [])
        self.sent = []
        self.history_raises = None
        self.history_calls = []
        self.expose_last_message_id = True

    @property
    def last_message_id(self):
        if not self.expose_last_message_id:
            return None
        return max((m.id for m in self.messages), default=None)

    def history(self, limit=100, after=None, oldest_first=False):
        self.history_calls.append({'limit': limit,
                                   'after': None if after is None else after.id,
                                   'oldest_first': oldest_first})
        if self.history_raises is not None:
            raise self.history_raises
        msgs = sorted(self.messages, key=lambda m: m.id)
        if after is not None:
            msgs = [m for m in msgs if m.id > after.id]
        if not oldest_first:
            msgs = list(reversed(msgs))
        return _History(msgs[:limit])

    async def send(self, text):
        self.sent.append(text)
        return FakeMessage(id=10 ** 18, content=text, channel=self)


class FakeDMChannel(_Channel):
    pass


class FakeTextChannel(_Channel):
    pass


class FakeThread(_Channel):
    pass


class FakeUser:
    def __init__(self, dm_channel=None):
        self.id = HUMAN
        self.dm_channel = dm_channel
        self.created = 0

    async def create_dm(self):
        self.created += 1
        if self.dm_channel is None:
            self.dm_channel = FakeDMChannel(id=900)
        return self.dm_channel


def load_bridget(fake_home: Path, env: dict | None = None,
                 channels_toml: str = ''):
    """Import bridget into a fresh namespace rooted at `fake_home`.

    The discord stub carries REAL classes for `DMChannel`, `Thread` and
    `Object`, because the router's branches are `isinstance` checks: a
    MagicMock as the second argument to `isinstance` raises, so a MagicMock
    stub cannot exercise the routing at all.
    """
    env_dir = fake_home / '.pogo'
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / 'bridget.env').write_text(
        'DISCORD_BOT_TOKEN=fake\n'
        f'DISCORD_USER_ID={HUMAN}\n'
        'DISCORD_SERVER_ID=2\n'
        'MG_BIN=/bin/echo\n'
    )
    (env_dir / 'bridget.env').chmod(0o600)   # else every load warns on stderr
    if channels_toml:
        (env_dir / 'bridget.channels.toml').write_text(channels_toml)

    keys = ('HOME', 'BRIDGET_REPO_DIR', 'BRIDGET_INBOUND_RECEIPTS',
            'BRIDGET_INBOUND_CATCHUP', 'BRIDGET_INBOUND_CATCHUP_LIMIT',
            'BRIDGET_INBOUND_CATCHUP_MAX_AGE', 'BRIDGET_LOG_CHANNEL_ID')
    saved_env = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    os.environ['HOME'] = str(fake_home)
    os.environ['BRIDGET_REPO_DIR'] = str(REPO)
    for k, v in (env or {}).items():
        os.environ[k] = str(v)

    fake_discord = mock.MagicMock()
    fake_discord.Intents.default.return_value = mock.MagicMock()
    fake_discord.HTTPException = FakeHTTPException
    fake_discord.Forbidden = FakeForbidden
    fake_discord.NotFound = FakeNotFound
    fake_discord.Object = FakeObject
    fake_discord.DMChannel = FakeDMChannel
    fake_discord.Thread = FakeThread
    fake_discord.TextChannel = FakeTextChannel
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


def registered(b, name):
    """Recover a handler bridget registered with `@client.event`.

    Under the stub `client` is a MagicMock, so the decorator swallowed the
    function; the call list is where it survives (the idiom
    `tests/test_watcher_idempotence.py` established).
    """
    for call in b.client.event.call_args_list:
        fn = call.args[0] if call.args else None
        if getattr(fn, '__name__', '') == name:
            return fn
    raise AssertionError(f'could not recover the real {name}')


def capture(coro_factory):
    """Run a coroutine, returning (stdout, stderr) as text."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        asyncio.run(coro_factory())
    return out.getvalue(), err.getvalue()


# ==========================================================================
# R — the receipt
# ==========================================================================

class ReceiptTest(unittest.TestCase):
    """R. Every inbound branch writes itself down, and off it writes nothing."""

    def setUp(self):
        self.tmp = testtmp.mkdtemp('inbound-receipt')
        self.b = load_bridget(self.tmp)
        self.b.handle_command = mock.Mock(return_value='pong')

    def _dm(self, text, mid=7001):
        ch = FakeDMChannel(id=900)
        msg = FakeMessage(id=mid, content=text, channel=ch)
        ch.messages.append(msg)
        return ch, msg

    def test_R1_a_dm_writes_a_recv_and_a_done(self):
        """R1. The path that carried both lost messages, and logged nothing."""
        ch, msg = self._dm('mail mayor hello')
        out, _ = capture(lambda: self.b.route_message(msg))
        self.assertIn(f'inbound: recv id={msg.id} surface=dm:{HUMAN} chars=16',
                      out)
        self.assertIn(f'inbound: done id={msg.id} surface=dm:{HUMAN} '
                      f'route=dm outcome=replied', out)
        self.assertEqual(ch.sent, ['pong'])

    def test_R2_an_unrecognised_command_is_visible_as_something_other_than_silence(self):
        """R2. The ticket's words. Before this, a typo and a working command
        were the same zero bytes in the log."""
        self.b.handle_command = mock.Mock(return_value=self.b.UNRECOGNIZED_REPLY)
        _, msg = self._dm('maail mayor hello')
        out, _ = capture(lambda: self.b.route_message(msg))
        self.assertIn('outcome=unrecognised', out)

    def test_R3_an_unmapped_channel_names_itself_instead_of_returning(self):
        """R3. The `return` with no log at all — "I typed at it and nothing
        happened" now has an answer, and the answer names the channel."""
        ch = FakeTextChannel(id=555)
        msg = FakeMessage(id=7003, content='hello', channel=ch)
        out, _ = capture(lambda: self.b.route_message(msg))
        self.assertIn('route=ignored-unmapped', out)
        self.assertIn('outcome=ignored', out)
        self.assertIn('555', out)
        self.assertEqual(ch.sent, [], 'an unmapped channel must stay silent '
                                      'in Discord — the record is in the log')

    def test_R4_a_thread_reply_records_the_ack_outcome(self):
        """R4. The one path that already had a receipt — the reaction on
        Daniel's own message — now has one a grep can reach too."""
        thread = FakeThread(id=8100)
        msg = FakeMessage(id=7004, content='thanks', channel=thread)
        conv = mock.Mock()
        self.b.CONVERSATIONS.by_thread = mock.Mock(return_value=conv)
        self.b.thread_command_reply = mock.Mock(return_value=None)
        self.b.reply_in_conversation = mock.Mock(
            return_value=self.b.acks.delivered('mayor'))
        out, _ = capture(lambda: self.b.route_message(msg))
        self.assertIn('route=thread-reply', out)
        self.assertIn('outcome=ack-ok', out)

    def test_R4b_a_thread_we_do_not_own_is_ignored_but_recorded(self):
        thread = FakeThread(id=8101)
        msg = FakeMessage(id=7005, content='not ours', channel=thread)
        self.b.CONVERSATIONS.by_thread = mock.Mock(return_value=None)
        out, _ = capture(lambda: self.b.route_message(msg))
        self.assertIn('route=thread-foreign', out)
        self.assertIn('outcome=ignored', out)

    def test_R5_PRE_FIX_CONTROL_receipts_off_reproduces_the_silence(self):
        """R5. THE CONTROL. With BRIDGET_INBOUND_RECEIPTS=0 a handled message
        and a silently-ignored one produce byte-identical output — which is the
        state this ticket describes, reproduced on demand."""
        tmp = testtmp.mkdtemp('inbound-receipt-off')
        b = load_bridget(tmp, env={'BRIDGET_INBOUND_RECEIPTS': '0'})
        b.handle_command = mock.Mock(return_value='pong')

        dm = FakeDMChannel(id=900)
        handled = FakeMessage(id=7006, content='mail mayor hi', channel=dm)
        ignored = FakeMessage(id=7007, content='hi',
                              channel=FakeTextChannel(id=556))
        handled_out, _ = capture(lambda: b.route_message(handled))
        ignored_out, _ = capture(lambda: b.route_message(ignored))

        self.assertEqual(handled_out, ignored_out)
        self.assertEqual(handled_out, '', 'the pre-fix log said nothing at all')
        self.assertIn('BRIDGET_INBOUND_RECEIPTS=0', b.inbound_status_line(),
                      'a reader who greps `inbound:` and gets zero must be '
                      'able to tell "nothing arrived" from "switched off"')

    def test_R6_a_handler_that_raises_still_leaves_a_disposition(self):
        """R6. Why there are two lines rather than one.

        A single line emitted after routing cannot record a message whose
        handler died — and dying mid-handle is exactly the failure class this
        ticket is about. The `recv` is written before any routing decision, and
        the `done` is written from a `finally`.
        """
        boom = RuntimeError('mg wedged')
        self.b.handle_command = mock.Mock(side_effect=boom)
        _, msg = self._dm('mail mayor hi', mid=7008)

        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(RuntimeError):
                asyncio.run(self.b.route_message(msg))
        text = out.getvalue()
        self.assertIn(f'inbound: recv id={msg.id}', text)
        self.assertIn('outcome=error', text)
        self.assertIn('RuntimeError: mg wedged', text)

    def test_R7_on_message_still_drops_bots_and_strangers_before_the_receipt(self):
        """R7. The stated cost of the author filter, pinned so it is a choice
        rather than a regression: a stranger's message writes nothing."""
        on_message = registered(self.b, 'on_message')
        dm = FakeDMChannel(id=900)
        for author in (FakeAuthor(HUMAN, bot=True), FakeAuthor(HUMAN + 1)):
            msg = FakeMessage(id=7009, content='hi', channel=dm, author=author)
            out, _ = capture(lambda m=msg: on_message(m))
            self.assertEqual(out, '')

    def test_R8_the_live_path_and_the_sweep_share_one_router(self):
        """R8. `on_message` must delegate, not duplicate. A sweep with its own
        copy of the routing rules drifts, and the drift is only ever visible on
        the reconnect path — the one nobody watches."""
        on_message = registered(self.b, 'on_message')
        _, msg = self._dm('mail mayor hi', mid=7010)
        with mock.patch.object(self.b, 'route_message',
                               new=mock.AsyncMock()) as router:
            asyncio.run(on_message(msg))
        router.assert_called_once_with(msg)


# ==========================================================================
# G — gateway state
# ==========================================================================

class GatewayJournalTest(unittest.TestCase):
    """G. "No inbound because nobody typed" vs "no inbound because we were not
    connected" — the ambiguity mg-879c removed on the outbound side."""

    def setUp(self):
        self.tmp = testtmp.mkdtemp('inbound-gateway')
        self.b = load_bridget(self.tmp)

    def test_G1_a_resume_says_nothing_was_missed(self):
        clock = [1000.0]
        j = GatewayJournal(clock=lambda: clock[0])
        j.identified()
        clock[0] += 60
        j.disconnected()
        clock[0] += 45
        line = self.b.format_gateway(j.resumed())
        self.assertIn('RESUMED', line)
        self.assertIn('45s', line)
        self.assertIn('nothing was missed', line)

    def test_G2_a_re_identify_says_the_gap_was_never_delivered(self):
        """G2. The load-bearing line. This is the transition Daniel's two
        messages died across, and it printed `logged in as` and nothing else."""
        clock = [1000.0]
        j = GatewayJournal(clock=lambda: clock[0])
        j.identified()
        clock[0] += 60
        j.disconnected()
        clock[0] += 300
        event = j.identified()
        line = self.b.format_gateway(event)
        self.assertTrue(event.lossy)
        self.assertIn('IDENTIFY #2', line)
        self.assertIn('5m00s', line)
        self.assertIn('replays nothing', line)

    def test_G3_the_first_identify_of_a_run_is_not_a_loss(self):
        j = GatewayJournal(clock=lambda: 1000.0)
        event = j.identified()
        self.assertFalse(event.lossy)
        self.assertIn('first session of this run',
                      self.b.format_gateway(event))
        self.assertEqual(self.b.format_identify_notice(event), '')

    def test_G4_a_disconnect_says_inbound_is_dead(self):
        clock = [1000.0]
        j = GatewayJournal(clock=lambda: clock[0])
        j.connected()
        clock[0] += 7200
        line = self.b.format_gateway(j.disconnected())
        self.assertIn('DISCONNECTED', line)
        self.assertIn('2h00m', line)
        self.assertIn('inbound is dead', line)

    def test_G5_the_tokens_do_not_collide_with_the_outbound_ones(self):
        """G5. mg-879c's whole point was that `grep -c 'relay:'` must count
        deliveries and only deliveries. The same has to hold here, in both
        directions, or one instrument silently counts another's lines."""
        clock = [1000.0]
        j = GatewayJournal(clock=lambda: clock[0])
        j.identified()
        j.disconnected()
        lines = [self.b.format_gateway(j.connected()),
                 self.b.format_gateway(j.resumed()),
                 self.b.format_inbound_receipt('dm:1', 5, 10),
                 self.b.format_inbound_disposition('dm:1', 5, 'dm', 'replied'),
                 self.b.format_catchup(
                     self.b.CatchupResult(surface='dm:1', fetched=0), None)]
        for line in lines:
            self.assertNotIn('relay:', line)
            self.assertNotIn('relay-stall:', line)
            self.assertNotIn('agent-mail:', line)
        gateway = [ln for ln in lines if ln.startswith('gateway:')]
        inbound = [ln for ln in lines if ln.startswith('inbound:')]
        catchup = [ln for ln in lines if ln.startswith('inbound-catchup:')]
        self.assertEqual(len(gateway), 2)
        self.assertEqual(len(inbound), 2)
        self.assertEqual(len(catchup), 1)
        # `inbound-catchup:` must not be counted by a grep for `inbound:`.
        self.assertNotIn('inbound:', catchup[0])


# ==========================================================================
# S — the resume point
# ==========================================================================

class SeenStoreTest(unittest.TestCase):
    """S. The state that makes recovery possible, and its failure modes."""

    def setUp(self):
        self.tmp = testtmp.mkdtemp('inbound-seen')
        self.path = self.tmp / 'bridget.inbound-seen.json'

    def test_S1_marking_is_monotonic(self):
        """S1. A replayed old message must not rewind the resume point — that
        would make the NEXT sweep re-fetch everything after it, which is a
        replay loop dressed as recovery."""
        store = SeenStore(self.path)
        self.assertTrue(store.mark('dm:1', 500))
        self.assertFalse(store.mark('dm:1', 400))
        self.assertEqual(store.last_seen('dm:1'), 500)
        self.assertTrue(store.mark('dm:1', 600))
        self.assertEqual(store.last_seen('dm:1'), 600)

    def test_S2_it_persists_owner_only_and_reloads(self):
        SeenStore(self.path).mark('dm:1', 12345)
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600, 'state files carry who the human talks to')
        self.assertEqual(SeenStore(self.path).load().last_seen('dm:1'), 12345)

    def test_S3_one_corrupt_row_does_not_discard_the_others(self):
        """S3. Dropping the lot would re-bootstrap every surface and hide a
        real gap behind a clean start."""
        self.path.write_text(json.dumps(
            {'version': 1, 'seen': {'dm:1': 'not-a-number', 'channel:9': '77'}}))
        store = SeenStore(self.path).load()
        self.assertEqual(store.last_seen('channel:9'), 77)
        self.assertIsNone(store.last_seen('dm:1'))

    def test_S4_an_unreadable_file_bootstraps_and_says_so(self):
        self.path.write_text('{ not json')
        store = SeenStore(self.path).load()
        self.assertTrue(store.load_error)
        self.assertFalse(store.known('dm:1'))

    def test_S5_snowflakes_order_by_time(self):
        early = SeenStore.snowflake_for(1_755_000_000)
        late = SeenStore.snowflake_for(1_755_000_600)
        self.assertLess(early, late)
        self.assertAlmostEqual(SeenStore.snowflake_time(late),
                               1_755_000_600, delta=1)

    def test_S6_an_unwritable_state_file_costs_replays_not_the_bridge(self):
        """S6. Failing the inbound path to protect its own bookkeeping would be
        worse than the bookkeeping being stale."""
        bad = self.tmp / 'nope' / 'deep'
        bad.mkdir(parents=True)
        bad.chmod(0o500)
        store = SeenStore(bad / 'seen.json')
        try:
            store.mark('dm:1', 99)
            self.assertTrue(store.write_error)
            self.assertEqual(store.last_seen('dm:1'), 99)
        finally:
            bad.chmod(0o700)


# ==========================================================================
# P — the plan
# ==========================================================================

class CatchupPlanTest(unittest.TestCase):
    """P. What one surface's sweep asks for, decided before it asks."""

    def setUp(self):
        self.tmp = testtmp.mkdtemp('inbound-plan')
        self.store = SeenStore(self.tmp / 'seen.json')

    def test_P1_an_unknown_surface_bootstraps(self):
        """P1. "No state" must not mean "replay everything": the first start
        after an upgrade would re-run Daniel's whole DM history."""
        plan = plan_catchup(self.store, 'dm:1', limit=50, max_age=86400)
        self.assertTrue(plan.bootstrap)
        self.assertIsNone(plan.after)

    def test_P2_a_known_surface_resumes_from_its_mark(self):
        now = 1_755_600_000.0
        self.store.mark('dm:1', SeenStore.snowflake_for(now - 60))
        plan = plan_catchup(self.store, 'dm:1', limit=50, max_age=86400, now=now)
        self.assertFalse(plan.bootstrap)
        self.assertEqual(plan.after, self.store.last_seen('dm:1'))
        self.assertEqual(plan.floor_reason, '')

    def test_P3_a_stale_mark_is_clamped_and_the_clamp_is_named(self):
        """P3. A sweep that returns 0 after silently skipping a week reads
        exactly like a sweep that found nothing to do."""
        now = 1_755_600_000.0
        self.store.mark('dm:1', SeenStore.snowflake_for(now - 7 * 86400))
        plan = plan_catchup(self.store, 'dm:1', limit=50, max_age=86400, now=now)
        self.assertGreater(plan.after, self.store.last_seen('dm:1'))
        self.assertIn('max_age', plan.floor_reason)


# ==========================================================================
# C — the sweep: the only part that can recover what never arrived
# ==========================================================================

class CatchupTest(unittest.TestCase):
    """C. THE ACCEPTANCE. Reproduces the 2026-08-19 loss and recovers it."""

    #: The two messages, verbatim from the REST sweep that found them.
    LOST = ['Mail pause one third once the executive has signed off',
            'Mail pause one third once the executive has signed off']

    def setUp(self):
        self.tmp = testtmp.mkdtemp('inbound-catchup')
        self.b = load_bridget(self.tmp)
        self.commands = []
        self.b.handle_command = mock.Mock(
            side_effect=lambda text: self.commands.append(text) or 'ok')
        self.dm = FakeDMChannel(id=900)
        self.user = FakeUser(dm_channel=self.dm)
        self.b.client.get_channel = mock.Mock(return_value=None)

    def _msg(self, mid_at, text):
        return FakeMessage(id=SeenStore.snowflake_for(mid_at), content=text,
                           channel=self.dm)

    def _now(self):
        import datetime
        return datetime.datetime.now(datetime.timezone.utc).timestamp()

    def test_C1_the_sweep_recovers_a_message_the_gateway_never_delivered(self):
        """C1. The measured shape: two DMs exist in the REST history and were
        never dispatched to `on_message`, because the socket was down and
        Discord replays nothing across the re-IDENTIFY that followed."""
        now = self._now()
        seen = self.b.INBOUND_SEEN
        seen.mark(dm_surface(HUMAN), SeenStore.snowflake_for(now - 600))
        for i, text in enumerate(self.LOST):
            self.dm.messages.append(self._msg(now - 300 + i, text))

        out, _ = capture(lambda: self.b.catchup_inbound(self.user))

        self.assertEqual(self.commands, self.LOST,
                         'both lost messages must reach the command surface')
        self.assertIn('replayed=2', out)
        self.assertIn('via=catchup', out)
        self.assertIn('2 message(s) recovered', out)

    def test_C2_a_second_sweep_replays_nothing(self):
        """C2. The resume point is persisted per MESSAGE, so the recovery is
        not a duplicate factory across the reconnects that trigger it."""
        now = self._now()
        self.b.INBOUND_SEEN.mark(dm_surface(HUMAN),
                                 SeenStore.snowflake_for(now - 600))
        self.dm.messages.append(self._msg(now - 300, self.LOST[0]))

        capture(lambda: self.b.catchup_inbound(self.user))
        self.assertEqual(len(self.commands), 1)
        out, _ = capture(lambda: self.b.catchup_inbound(self.user))
        self.assertEqual(len(self.commands), 1, 'the sweep replayed twice')
        self.assertIn('fetched=0 replayed=0', out)

    def test_C3_first_sight_adopts_the_head_and_replays_nothing(self):
        """C3. An upgrade must not mail agents from months-old commands."""
        now = self._now()
        for i in range(3):
            self.dm.messages.append(self._msg(now - 3600 + i, f'mail mayor {i}'))
        out, _ = capture(lambda: self.b.catchup_inbound(self.user))
        self.assertEqual(self.commands, [])
        self.assertIn('first sight', out)
        self.assertIn('replayed nothing', out)
        self.assertEqual(self.b.INBOUND_SEEN.last_seen(dm_surface(HUMAN)),
                         max(m.id for m in self.dm.messages))

    def test_C4_a_capped_sweep_says_it_was_capped_and_names_the_knob(self):
        """C4. The no-silent-caps rule. A bounded sweep that reports nothing
        about its bound reads as complete coverage."""
        tmp = testtmp.mkdtemp('inbound-catchup-cap')
        b = load_bridget(tmp, env={'BRIDGET_INBOUND_CATCHUP_LIMIT': '2'})
        b.handle_command = mock.Mock(return_value='ok')
        dm = FakeDMChannel(id=900)
        user = FakeUser(dm_channel=dm)
        b.client.get_channel = mock.Mock(return_value=None)
        now = self._now()
        b.INBOUND_SEEN.mark(dm_surface(HUMAN), SeenStore.snowflake_for(now - 600))
        for i in range(5):
            dm.messages.append(FakeMessage(
                id=SeenStore.snowflake_for(now - 300 + i),
                content=f'mail mayor {i}', channel=dm))
        out, _ = capture(lambda: b.catchup_inbound(user))
        self.assertIn('CAPPED at limit=2', out)
        self.assertIn('BRIDGET_INBOUND_CATCHUP_LIMIT', out)

    def test_C5_a_stale_message_is_skipped_and_counted(self):
        """C5. A `mail` command from last week is not a request any more."""
        now = self._now()
        self.b.INBOUND_SEEN.mark(dm_surface(HUMAN),
                                 SeenStore.snowflake_for(now - 8 * 86400))
        self.dm.messages.append(self._msg(now - 7 * 86400, 'mail mayor stale'))
        self.dm.messages.append(self._msg(now - 60, 'mail mayor fresh'))
        out, _ = capture(lambda: self.b.catchup_inbound(self.user))
        self.assertEqual(self.commands, ['mail mayor fresh'])
        self.assertIn('max_age', out)

    def test_C6_an_unreadable_surface_is_reported_and_does_not_abort_the_rest(self):
        """C6. The mg-8614 Manage-Channels shape: a surface bridget cannot read
        is a surface where loss is undetectable, so it is a finding — and one
        unreadable channel must not cost the recovery of every other one."""
        now = self._now()
        mapped = FakeTextChannel(id=1234)
        mapped.history_raises = FakeForbidden('403 Missing Permissions')
        self.b.CHANNELS_BY_SNOWFLAKE[1234] = {
            'agent': 'pm-onethird', 'direction': 'both', 'kinds': {'mail'}}
        self.b.client.get_channel = mock.Mock(
            side_effect=lambda cid: mapped if cid == 1234 else None)
        self.b.INBOUND_SEEN.mark(dm_surface(HUMAN),
                                 SeenStore.snowflake_for(now - 600))
        self.b.INBOUND_SEEN.mark(channel_surface(1234),
                                 SeenStore.snowflake_for(now - 600))
        self.dm.messages.append(self._msg(now - 60, self.LOST[0]))

        out, _ = capture(lambda: self.b.catchup_inbound(self.user))
        self.assertIn('UNREADABLE', out)
        self.assertIn('403 Missing Permissions', out)
        self.assertEqual(self.commands, [self.LOST[0]],
                         'the readable surface still recovered its message')

    def test_C7_PRE_FIX_CONTROL_catchup_off_loses_the_same_message(self):
        """C7. THE CONTROL. With the sweep off the 08-19 messages stay lost —
        and the only thing bridget can honestly do is say so, which the ticket
        asks for by name."""
        tmp = testtmp.mkdtemp('inbound-catchup-off')
        b = load_bridget(tmp, env={'BRIDGET_INBOUND_CATCHUP': '0'})
        seen = []
        b.handle_command = mock.Mock(side_effect=lambda t: seen.append(t) or 'ok')
        dm = FakeDMChannel(id=900)
        user = FakeUser(dm_channel=dm)
        now = self._now()
        dm.messages.append(FakeMessage(id=SeenStore.snowflake_for(now - 60),
                                       content=self.LOST[0], channel=dm))
        out, _ = capture(lambda: b.catchup_inbound(user))
        self.assertEqual(seen, [], 'the pre-fix world loses it, as measured')
        self.assertEqual(out, '')

        clock = [1000.0]
        j = GatewayJournal(clock=lambda: clock[0])
        j.identified()
        j.disconnected()
        clock[0] += 300
        notice = b.format_identify_notice(j.identified())
        self.assertIn('BRIDGET_INBOUND_CATCHUP=0', notice)
        self.assertIn('will NOT be recovered', notice)
        self.assertIn('BRIDGET_INBOUND_CATCHUP=0', b.catchup_status_line())

    def test_C8_someone_elses_message_is_skipped_but_still_advances_the_mark(self):
        """C8. Otherwise every sweep re-fetches the same foreign traffic
        forever and the cap eventually eats the messages that matter."""
        now = self._now()
        self.b.INBOUND_SEEN.mark(dm_surface(HUMAN),
                                 SeenStore.snowflake_for(now - 600))
        stranger = FakeMessage(id=SeenStore.snowflake_for(now - 120),
                               content='hi', channel=self.dm,
                               author=FakeAuthor(HUMAN + 1))
        self.dm.messages.append(stranger)
        out, _ = capture(lambda: self.b.catchup_inbound(self.user))
        self.assertEqual(self.commands, [])
        self.assertIn('skipped-not-yours=1', out)
        self.assertEqual(self.b.INBOUND_SEEN.last_seen(dm_surface(HUMAN)),
                         stranger.id)

    def test_C9_two_concurrent_sweeps_do_not_double_replay(self):
        """C9. THE REMEDY, EXAMINED FOR ITS OWN DEFECT.

        `on_ready` is dispatched as a task and fires again on every reconnect,
        so a reconnect during a slow sweep runs two over the same surfaces —
        each reading a resume point the other has not written yet. That is a
        check-then-act race, which is the shape of the loss being repaired, so
        the sweep is serialised rather than given the benefit of the doubt.
        """
        now = self._now()
        self.b.INBOUND_SEEN.mark(dm_surface(HUMAN),
                                 SeenStore.snowflake_for(now - 600))
        self.dm.messages.append(self._msg(now - 60, self.LOST[0]))

        async def scenario():
            await asyncio.gather(self.b.catchup_inbound(self.user),
                                 self.b.catchup_inbound(self.user))

        out, _ = capture(scenario)
        self.assertEqual(self.commands, [self.LOST[0]],
                         'a racing sweep replayed the message twice')

    def test_C11_a_message_that_ERRORED_is_not_marked_seen(self):
        """C11. THE REMEDY, EXAMINED FOR ITS OWN DEFECT (2).

        Advancing the resume point past a message whose handling died would drop
        it from recovery — this ticket's exact defect, reintroduced by its own
        bookkeeping. Retry is bounded for free by the monotonic mark: any later
        message advances past it, so a poison message is retried only while it
        is still the newest thing on the surface.
        """
        now = self._now()
        mark = SeenStore.snowflake_for(now - 600)
        self.b.INBOUND_SEEN.mark(dm_surface(HUMAN), mark)
        msg = self._msg(now - 60, 'mail mayor boom')
        self.b.handle_command = mock.Mock(side_effect=RuntimeError('mg wedged'))

        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(RuntimeError):
                asyncio.run(self.b.route_message(msg))
        self.assertEqual(self.b.INBOUND_SEEN.last_seen(dm_surface(HUMAN)), mark)
        self.assertIn('not marked seen', out.getvalue())

    def test_C12_the_sweep_skips_what_the_live_path_already_routed(self):
        """C12. THE REMEDY, EXAMINED FOR ITS OWN DEFECT (3).

        The gateway can deliver a message while the sweep is paging. Trusting
        the plan taken before the first fetch would have both paths route it —
        loss traded for duplication, which is still a defect.
        """
        now = self._now()
        self.b.INBOUND_SEEN.mark(dm_surface(HUMAN),
                                 SeenStore.snowflake_for(now - 600))
        live = self._msg(now - 60, self.LOST[0])
        self.dm.messages.append(live)
        # The live path got there first, exactly as it would mid-sweep.
        capture(lambda: self.b.route_message(live))
        self.assertEqual(self.commands, [self.LOST[0]])
        out, _ = capture(lambda: self.b.catchup_inbound(self.user))
        self.assertEqual(self.commands, [self.LOST[0]],
                         'the sweep re-routed what the gateway had delivered')

    def test_C13_an_unwired_inbound_channel_says_it_is_not_swept(self):
        """C13. A channel configured for inbound whose snowflake never resolved
        (the mg-8614 Manage-Channels shape) is not in CHANNELS_BY_SNOWFLAKE at
        all, so it is invisible to the sweep. Leaving it out silently is this
        ticket's defect one level up."""
        self.b.CHANNELS['pm-onethird'] = {
            'name': 'pm-onethird', 'snowflake': None, 'agent': 'pm-onethird',
            'direction': 'both', 'kinds': {'mail'}, 'channel': None,
            'channel_name': 'pm-onethird'}
        out, _ = capture(lambda: self.b.catchup_inbound(self.user))
        self.assertIn('NOT SWEPT', out)
        self.assertIn('pm-onethird', out)

    def test_C10_the_sweep_asks_discord_for_exactly_what_it_planned(self):
        """C10. The cursor is the ticket's own `?after=<last_seen_id>`; if the
        sweep quietly dropped it, every reconnect would page whole history."""
        now = self._now()
        mark = SeenStore.snowflake_for(now - 600)
        self.b.INBOUND_SEEN.mark(dm_surface(HUMAN), mark)
        self.dm.messages.append(self._msg(now - 60, self.LOST[0]))
        capture(lambda: self.b.catchup_inbound(self.user))
        call = self.dm.history_calls[-1]
        self.assertEqual(call['after'], mark)
        self.assertTrue(call['oldest_first'],
                        'replay must follow the order he typed them in')
        self.assertEqual(call['limit'], self.b.INBOUND_CATCHUP_LIMIT)


class ReadyWiringTest(unittest.TestCase):
    """The instrument has to be REACHED, not merely written. A status line
    nobody prints and a sweep nobody calls are the same as not having them."""

    def setUp(self):
        self.tmp = testtmp.mkdtemp('inbound-ready')
        self.b = load_bridget(self.tmp)

    def test_on_ready_journals_the_identify_and_runs_the_sweep(self):
        b = self.b
        on_ready = registered(b, 'on_ready')
        user = FakeUser(dm_channel=FakeDMChannel(id=900))

        async def fake_fetch_user(_):
            return user

        swept = []

        async def fake_catchup(u):
            swept.append(u)
            return []

        b.client.fetch_user = fake_fetch_user
        b.client.user = mock.Mock(id=1)
        b.catchup_inbound = fake_catchup
        b.resolve_and_wire_channels = mock.AsyncMock()
        b.stop_watchers = mock.AsyncMock(return_value=0)
        b.track_watcher = mock.Mock()

        out, _ = capture(lambda: on_ready())
        self.assertIn('gateway: session IDENTIFY #1', out)
        self.assertIn('inbound record:', out)
        self.assertIn('inbound catch-up:', out)
        self.assertEqual(swept, [user], 'on_ready never ran the sweep')

    def test_the_status_line_names_the_residual_thread_gap(self):
        """A reader must not have to infer from silence that threads are
        outside the sweep — that inference is this ticket's whole subject."""
        line = self.b.catchup_status_line()
        self.assertIn('THREADS are not swept', line)

    def test_gateway_handlers_are_registered(self):
        for name in ('on_connect', 'on_disconnect', 'on_resumed'):
            registered(self.b, name)


if __name__ == '__main__':
    unittest.main(verbosity=2)
