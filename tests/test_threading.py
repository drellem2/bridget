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

"""Tests for the Discord threading adapter: thread-per-conversation, the calm
DM inbox, live mute/settings, and the delivery/ambiguity/undeliverable acks.

Unlike the other suites, the `discord` stub here is built from real classes
rather than a MagicMock: the adapter dispatches on `isinstance(channel,
discord.Thread)` and catches `discord.HTTPException`, neither of which a
MagicMock can satisfy.
"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / 'bridget'


# --- a `discord` module real enough to isinstance against ------------------

class FakeHTTPException(Exception):
    pass


class FakeNotFound(FakeHTTPException):
    pass


class FakeForbidden(FakeHTTPException):
    pass


class FakeDMChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content):
        self.sent.append(content)


class FakeThread:
    def __init__(self, thread_id, archived=False):
        self.id = thread_id
        self.archived = archived
        self.sent = []
        self.unarchived = False

    async def send(self, content):
        if self.archived:
            raise FakeHTTPException('cannot send to archived thread')
        self.sent.append(content)

    async def edit(self, archived=None):
        if archived is False:
            self.archived = False
            self.unarchived = True


class FakeMessage:
    """A message posted in the log channel; threads hang off it."""

    def __init__(self, channel, content, next_thread_id):
        self.channel = channel
        self.content = content
        self._next_thread_id = next_thread_id

    async def create_thread(self, name=None, **kw):
        thread = FakeThread(self._next_thread_id)
        thread.name = name
        self.channel.threads.append(thread)
        # A real thread is immediately resolvable via the client, which is what
        # lets a later message in the same conversation find it again.
        if self.channel.client is not None:
            self.channel.client.channels[thread.id] = thread
        return thread


class FakeTextChannel:
    """A guild text channel — the only kind that can host threads."""

    def __init__(self, channel_id, client=None):
        self.id = channel_id
        self.client = client
        self.sent = []
        self.threads = []
        self._next_thread_id = 9000

    async def send(self, content):
        self.sent.append(content)
        self._next_thread_id += 1
        return FakeMessage(self, content, self._next_thread_id)

    async def create_thread(self, **kw):  # marker: hasattr(channel,'create_thread')
        raise NotImplementedError


class FakeUser:
    def __init__(self, user_id=1):
        self.id = user_id
        self.sent = []

    async def send(self, content):
        self.sent.append(content)


class FakeAllowedMentions:
    """Mirrors discord.AllowedMentions closely enough that the adapter's
    `none()` call is observable — the whole point of A2 is that bridget never
    constructs a Client without it."""

    def __init__(self, everyone=True, users=True, roles=True):
        self.everyone = everyone
        self.users = users
        self.roles = roles

    @classmethod
    def none(cls):
        return cls(everyone=False, users=False, roles=False)


class FakeClient:
    def __init__(self, *a, **kw):
        self.channels = {}
        self._closed = False
        self.allowed_mentions = kw.get('allowed_mentions')

    def event(self, fn):
        return fn

    def is_closed(self):
        return self._closed

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        if cid in self.channels:
            return self.channels[cid]
        raise FakeNotFound(f'no channel {cid}')


def fake_discord_module():
    m = types.ModuleType('discord')
    m.HTTPException = FakeHTTPException
    m.NotFound = FakeNotFound
    m.Forbidden = FakeForbidden
    m.DMChannel = FakeDMChannel
    m.Thread = FakeThread
    m.TextChannel = FakeTextChannel
    m.User = FakeUser
    m.Message = FakeMessage
    m.Client = FakeClient
    m.AllowedMentions = FakeAllowedMentions

    class _Intents:
        @staticmethod
        def default():
            return types.SimpleNamespace(
                message_content=False, dm_messages=False,
                guilds=False, guild_messages=False,
            )

    m.Intents = _Intents
    return m


# --- loader ----------------------------------------------------------------

def load_bridget(env_overrides: dict | None = None, env_file_extra: str = ''):
    fake_home = Path(tempfile.mkdtemp(prefix='bridget-thread-test-'))
    env_dir = fake_home / '.pogo'
    env_dir.mkdir(parents=True)
    env_path = env_dir / 'bridget.env'
    env_path.write_text(
        'DISCORD_BOT_TOKEN=fake\n'
        'DISCORD_USER_ID=1\n'
        'DISCORD_SERVER_ID=2\n' + env_file_extra
    )
    # As install.sh would leave it — otherwise every load prints the
    # world-readable warning and drowns the test output.
    os.chmod(env_path, 0o600)
    (fake_home / '.macguffin' / 'mail' / 'human' / 'new').mkdir(parents=True)

    keys = {'HOME', 'BRIDGET_REPO_DIR', 'BRIDGET_LOG_CHANNEL_ID', 'BRIDGET_DM_POLICY'}
    if env_overrides:
        keys |= set(env_overrides)
    saved_env = {k: os.environ.get(k) for k in keys}
    for k in ('BRIDGET_LOG_CHANNEL_ID', 'BRIDGET_DM_POLICY'):
        os.environ.pop(k, None)
    os.environ['HOME'] = str(fake_home)
    os.environ['BRIDGET_REPO_DIR'] = str(REPO)
    for k, v in (env_overrides or {}).items():
        os.environ[k] = v

    saved_discord = sys.modules.get('discord')
    sys.modules['discord'] = fake_discord_module()
    saved_bridget = sys.modules.pop('bridget', None)
    for mod in [m for m in sys.modules if m.startswith('bridget_core')]:
        sys.modules.pop(mod, None)

    try:
        loader = SourceFileLoader('bridget', str(SCRIPT))
        spec = importlib.util.spec_from_loader('bridget', loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        module._fake_home = fake_home
        return module
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


LOG_CHANNEL = '555'


def load_threaded(**kw):
    return load_bridget(env_overrides={'BRIDGET_LOG_CHANNEL_ID': LOG_CHANNEL, **kw})


def mail(subject='hello', sender='mayor', body='b', msg_id='', in_reply_to='', refs=''):
    d = {'from': sender, 'subject': subject, 'body': body,
         'message_id': msg_id, 'in_reply_to': in_reply_to,
         'references': refs.split() if refs else [], 'date': ''}
    return d


# --- config ----------------------------------------------------------------

class TestConfig(unittest.TestCase):
    def test_threading_off_by_default(self):
        b = load_bridget()
        self.assertFalse(b.THREADS_ENABLED)
        self.assertIsNone(b.LOG_CHANNEL_ID)
        self.assertEqual(b.SETTINGS.dm_policy, 'all')

    def test_log_channel_enables_threading(self):
        b = load_threaded()
        self.assertTrue(b.THREADS_ENABLED)
        self.assertEqual(b.LOG_CHANNEL_ID, 555)

    def test_non_integer_log_channel_is_rejected(self):
        with self.assertRaises(SystemExit):
            load_bridget(env_overrides={'BRIDGET_LOG_CHANNEL_ID': 'not-a-snowflake'})

    def test_unknown_dm_policy_is_rejected(self):
        with self.assertRaises(SystemExit):
            load_threaded(BRIDGET_DM_POLICY='screaming')

    def test_curated_policy_without_log_channel_is_refused(self):
        """Otherwise suppressed mail would have nowhere to go."""
        with self.assertRaises(SystemExit):
            load_bridget(env_overrides={'BRIDGET_DM_POLICY': 'curated'})

    def test_curated_policy_with_log_channel_is_accepted(self):
        self.assertEqual(load_threaded(BRIDGET_DM_POLICY='curated').SETTINGS.dm_policy, 'curated')

    def test_env_file_can_carry_the_threading_keys(self):
        b = load_bridget(env_file_extra='BRIDGET_LOG_CHANNEL_ID=777\nBRIDGET_DM_POLICY=none\n')
        self.assertEqual(b.LOG_CHANNEL_ID, 777)
        self.assertEqual(b.SETTINGS.dm_policy, 'none')

    def test_no_secret_defaults_are_baked_in(self):
        """A missing env file must fail loudly, never fall back to a literal."""
        src = SCRIPT.read_text()
        self.assertNotIn('DISCORD_BOT_TOKEN=', src.replace("'DISCORD_BOT_TOKEN'", ''))
        self.assertIn("lookup('DISCORD_BOT_TOKEN')", src)


# --- A2: nothing bridget posts may ping anyone -----------------------------

class TestAllowedMentions(unittest.TestCase):
    """Everything bridget renders is text an agent wrote. A mail subject of
    `@everyone the build is broken` must read as those characters."""

    def test_client_is_constructed_with_mentions_suppressed(self):
        am = load_bridget().client.allowed_mentions
        self.assertIsNotNone(am, 'discord.Client built without allowed_mentions')
        self.assertFalse(am.everyone)
        self.assertFalse(am.users)
        self.assertFalse(am.roles)


# --- A5: rendering lives in the adapter ------------------------------------

class TestRenderAck(unittest.TestCase):
    """The Discord presentation of a core `Ack`. The core owns the outcome; the
    emoji, the `**bold**` and the character budgets are ours."""

    def setUp(self):
        self.b = load_bridget()
        self.acks = self.b.acks

    def test_delivered_names_the_agent_and_subject(self):
        out = self.b.render_ack(self.acks.delivered('mayor', 'design review'))
        self.assertIn('✅', out)
        self.assertIn('mayor', out)
        self.assertIn('design review', out)

    def test_delivered_flags_threading_only_when_replying(self):
        self.assertIn('threaded', self.b.render_ack(
            self.acks.delivered('mayor', 's', in_reply_to='id-1')))
        self.assertNotIn('threaded', self.b.render_ack(self.acks.delivered('mayor', 's')))

    def test_delivered_elides_a_long_subject(self):
        out = self.b.render_ack(self.acks.delivered('mayor', 'x' * 100))
        self.assertIn('…', out)
        self.assertLess(len(out), 100 + 40)

    def test_delivered_with_no_subject_omits_the_quotes(self):
        self.assertNotIn('"', self.b.render_ack(self.acks.delivered('mayor')))

    def test_ambiguous_with_no_candidates_tells_the_human_what_to_do(self):
        out = self.b.render_ack(self.acks.ambiguous([]))
        self.assertIn('mail <subject>', out)

    def test_ambiguous_lists_candidates_and_caps_the_list(self):
        cands = [(f'conv {i}', f'k{i}') for i in range(8)]
        out = self.b.render_ack(self.acks.ambiguous(cands))
        self.assertIn('**8**', out)
        self.assertIn('and 3 more', out)
        self.assertIn('conv 0', out)
        self.assertNotIn('conv 7', out)

    def test_ambiguous_appends_a_hint(self):
        self.assertIn('try the thread',
                      self.b.render_ack(self.acks.ambiguous([], hint='try the thread')))

    def test_undeliverable_surfaces_the_reason_and_the_agent(self):
        out = self.b.render_ack(
            self.acks.undeliverable('mg exited 1: no such mailbox', agent='ghost'))
        self.assertIn('❌', out)
        self.assertIn('ghost', out)
        self.assertIn('no such mailbox', out)

    def test_undeliverable_with_empty_reason_still_says_something(self):
        self.assertIn('unknown error', self.b.render_ack(self.acks.undeliverable('')))

    def test_undeliverable_truncates_a_huge_stderr_dump(self):
        self.assertLess(len(self.b.render_ack(self.acks.undeliverable('x' * 5000))), 400)

    def test_undeliverable_without_an_agent_omits_the_to_clause(self):
        self.assertNotIn(' to ', self.b.render_ack(self.acks.undeliverable('boom')))


class TestRenderSettings(unittest.TestCase):
    def setUp(self):
        self.b = load_bridget()

    def test_reports_the_active_policy_and_mute_all(self):
        self.b.SETTINGS.set_dm_policy('all')
        out = self.b.render_settings(self.b.SETTINGS.summary())
        self.assertIn('⚙️', out)
        self.assertIn('`all`', out)
        self.assertIn('Mute all DMs: `false`', out)

    def test_no_mutes_says_none(self):
        self.assertIn('Muted conversations: none',
                      self.b.render_settings(self.b.SETTINGS.summary()))

    def test_muted_conversations_are_labelled(self):
        self.b.SETTINGS.mute('k1')
        out = self.b.render_settings(self.b.SETTINGS.summary({'k1': 'design review'}))
        self.assertIn('design review', out)

    def test_only_muted_conversations_are_labelled(self):
        """`summary()` looks up labels for muted keys only; the store may hold
        thousands of conversations."""
        b = self.b
        b.CONVERSATIONS.record('k1', subject='muted one', agent='mayor')
        b.CONVERSATIONS.record('k2', subject='not muted', agent='pm')
        b.SETTINGS.mute('k1')
        out = b.render_settings(b.SETTINGS.summary(
            {k: b.conversation_label(b.CONVERSATIONS.get(k))
             for k in b.SETTINGS.muted if b.CONVERSATIONS.get(k)}))
        self.assertIn('muted one', out)
        self.assertNotIn('not muted', out)

    def test_a_muted_conversation_the_store_forgot_falls_back_to_its_key(self):
        self.b.SETTINGS.mute('gone')
        out = self.b.render_settings(self.b.SETTINGS.summary({}))
        self.assertIn('gone', out)

    def test_the_muted_list_is_capped_by_the_adapter(self):
        for i in range(15):
            self.b.SETTINGS.mute(f'k{i:02d}')
        out = self.b.render_settings(self.b.SETTINGS.summary())
        self.assertIn('… and 5 more', out)
        self.assertEqual(out.count('    – '), self.b.SETTINGS_MUTED_LIMIT)


# --- A3: a body cannot escape its code fence -------------------------------

class TestCodeFenceIsInescapable(unittest.TestCase):
    def setUp(self):
        self.b = load_bridget()

    def _fences(self, text):
        """Runs of 3+ backticks — exactly what Discord treats as a fence."""
        import re
        return re.findall('`{3,}', text)

    def test_a_body_containing_a_fence_does_not_close_the_block(self):
        body = 'here is code:\n```python\nprint("hi")\n```\ndone'
        card = self.b.format_mail_card(mail(body=body))
        # Only our own opening and closing fences survive as real fences.
        self.assertEqual(len(self._fences(card)), 2)
        self.assertTrue(card.rstrip().endswith('```'))

    def test_the_backticks_are_still_visible_to_the_reader(self):
        """We defuse by inserting a zero-width space, not by deleting."""
        card = self.b.format_mail_card(mail(body='a ``` b'))
        self.assertEqual(card.count('`'), 3 + 3 + 3)  # body's 3 + our 2 fences
        self.assertIn(self.b.ZERO_WIDTH_SPACE, card)

    def test_a_longer_backtick_run_is_defused_too(self):
        card = self.b.format_mail_card(mail(body='`````'))
        self.assertEqual(len(self._fences(card)), 2)

    def test_one_and_two_backticks_are_left_alone(self):
        """Inline code inside a block is literal; only 3+ opens a fence."""
        self.assertEqual(self.b.defuse_fences('a `x` and ``y``'), 'a `x` and ``y``')

    def test_a_subject_containing_a_fence_cannot_open_one(self):
        """The header sits *outside* the code block, so a ``` in the subject
        opens a fence rather than closing ours. Defusing the body is not enough."""
        card = self.b.format_mail_card(mail(subject='fix the ```make``` target'))
        self.assertEqual(len(self._fences(card)), 2)
        self.assertTrue(card.rstrip().endswith('```'))

    def test_a_sender_name_containing_a_fence_cannot_open_one(self):
        card = self.b.format_mail_card(mail(sender='```evil```'))
        self.assertEqual(len(self._fences(card)), 2)

    def test_truncation_never_severs_the_closing_fence(self):
        card = self.b.format_mail_card(mail(body='x' * 50_000))
        self.assertLessEqual(len(card), self.b.DISCORD_MSG_LIMIT)
        self.assertTrue(card.rstrip().endswith('```'))
        self.assertEqual(len(self._fences(card)), 2)

    def test_a_pathological_subject_cannot_squeeze_out_the_body(self):
        card = self.b.format_mail_card(mail(subject='S' * 5000, body='the body'))
        self.assertLessEqual(len(card), self.b.DISCORD_MSG_LIMIT)
        self.assertIn('the body', card)
        self.assertTrue(card.rstrip().endswith('```'))

    def test_the_thread_link_footer_survives_a_long_body(self):
        card = self.b.format_mail_card(mail(body='x' * 50_000),
                                       thread_link='https://discord.com/x')
        self.assertLessEqual(len(card), self.b.DISCORD_MSG_LIMIT)
        self.assertTrue(card.endswith('https://discord.com/x'))
        self.assertEqual(len(self._fences(card)), 2)


# --- DM gating (the calm inbox) --------------------------------------------

class TestShouldDM(unittest.TestCase):
    def test_policy_all_dms_everything(self):
        b = load_bridget()
        self.assertTrue(b.should_dm(mail(), 'k1'))

    def test_policy_none_dms_nothing(self):
        b = load_threaded(BRIDGET_DM_POLICY='none')
        self.assertFalse(b.should_dm(mail(), 'k1'))
        self.assertFalse(b.should_dm(mail(subject='approval needed for x'), 'k1'))

    def test_policy_curated_dms_only_decisions(self):
        b = load_threaded(BRIDGET_DM_POLICY='curated')
        self.assertTrue(b.should_dm(mail(subject='approval needed for mg-1'), 'k1'))
        self.assertFalse(b.should_dm(mail(subject='FYI: build is green'), 'k1'))

    def test_muted_conversation_is_not_dmed(self):
        b = load_bridget()
        b.SETTINGS.mute('k1')
        self.assertFalse(b.should_dm(mail(), 'k1'))
        self.assertTrue(b.should_dm(mail(), 'k2'))

    def test_mute_all_suppresses_every_dm(self):
        b = load_bridget()
        b.SETTINGS.set_mute_all(True)
        self.assertFalse(b.should_dm(mail(subject='approval needed x'), 'k1'))

    def test_quiet_hours_outrank_policy(self):
        b = load_threaded(BRIDGET_QUIET_RESPECTS_OUTBOUND='true')
        with mock.patch.object(b, 'is_quiet_now', return_value=True):
            self.assertFalse(b.should_dm(mail(subject='approval needed x'), 'k1'))

    def test_quiet_hours_ignored_when_knob_is_off(self):
        b = load_bridget()
        with mock.patch.object(b, 'is_quiet_now', return_value=True):
            self.assertTrue(b.should_dm(mail(), 'k1'))

    def test_wants_attention_uses_the_approval_regex(self):
        b = load_bridget()
        self.assertTrue(b.wants_attention(mail(subject='approval needed for mg-1')))
        self.assertFalse(b.wants_attention(mail(subject='status update')))

    def test_wants_attention_honors_a_custom_approval_regex(self):
        b = load_bridget(env_overrides={'BRIDGET_APPROVAL_RE': r'^Subject: URGENT'})
        self.assertTrue(b.wants_attention(mail(subject='URGENT: fire')))
        self.assertFalse(b.wants_attention(mail(subject='approval needed for mg-1')))


# --- delivery / threading --------------------------------------------------

class TestDeliverMail(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b = load_threaded()
        self.channel = FakeTextChannel(555, client=self.b.client)
        self.b.client.channels[555] = self.channel
        self.user = FakeUser()

    async def test_first_mail_roots_a_thread_and_dms(self):
        await self.b.deliver_mail(self.user, 'f1', mail(msg_id='id-1'))
        self.assertEqual(len(self.channel.threads), 1)
        self.assertEqual(len(self.user.sent), 1)
        self.assertIn('discord.com/channels/2/', self.user.sent[0])

    async def test_thread_name_is_the_subject(self):
        await self.b.deliver_mail(self.user, 'f1', mail(subject='design review', msg_id='id-1'))
        self.assertEqual(self.channel.threads[0].name, 'design review')

    async def test_reply_lands_in_the_same_thread(self):
        await self.b.deliver_mail(self.user, 'f1', mail(msg_id='id-1'))
        await self.b.deliver_mail(
            self.user, 'f2',
            mail(subject='Re: hello', msg_id='id-2', in_reply_to='id-1', refs='id-1'))
        self.assertEqual(len(self.channel.threads), 1, 'reply rooted a duplicate thread')
        self.assertEqual(len(self.channel.threads[0].sent), 2)

    async def test_a_reply_naming_only_its_parent_joins_the_same_thread(self):
        """Past the first hop, `mg mail send --in-reply-to X` writes
        `References: [X]` — the parent, not the root. The store's message-id
        index is the only thing that ties id-3 back to the conversation.
        See tests/test_mg_threading.py, which proves mg really does this."""
        await self.b.deliver_mail(self.user, 'f1', mail(msg_id='id-1'))
        await self.b.deliver_mail(
            self.user, 'f2',
            mail(subject='Re: hello', msg_id='id-2', in_reply_to='id-1', refs='id-1'))
        await self.b.deliver_mail(
            self.user, 'f3',
            mail(subject='Re: hello', msg_id='id-3', in_reply_to='id-2', refs='id-2'))
        self.assertEqual(len(self.channel.threads), 1,
                         'the second round-trip rooted a duplicate thread')
        self.assertEqual(len(self.b.CONVERSATIONS), 1)
        self.assertEqual(len(self.channel.threads[0].sent), 3)

    async def test_a_conversation_keeps_the_key_it_was_rooted_on(self):
        await self.b.deliver_mail(self.user, 'f1', mail(msg_id='id-1'))
        await self.b.deliver_mail(
            self.user, 'f2', mail(msg_id='id-2', in_reply_to='id-1', refs='id-1'))
        self.assertEqual(list(self.b.CONVERSATIONS.keys()), ['id-1'])

    async def test_a_redelivered_mail_rejoins_its_own_thread(self):
        """A DM failure makes the watcher retry the mail. It must not root a
        second thread for a message already in one."""
        await self.b.deliver_mail(self.user, 'f1', mail(msg_id='id-1'))
        await self.b.deliver_mail(self.user, 'f1', mail(msg_id='id-1'))
        self.assertEqual(len(self.channel.threads), 1)

    async def test_unrelated_mail_roots_a_separate_thread(self):
        await self.b.deliver_mail(self.user, 'f1', mail(subject='a', msg_id='id-1'))
        await self.b.deliver_mail(self.user, 'f2', mail(subject='b', msg_id='id-9'))
        self.assertEqual(len(self.channel.threads), 2)

    async def test_pre_gh66_mail_threads_on_its_filename(self):
        """No correlation headers at all: safe degrade, one thread per mail."""
        await self.b.deliver_mail(self.user, 'file-a', mail())
        await self.b.deliver_mail(self.user, 'file-b', mail())
        self.assertEqual(len(self.channel.threads), 2)
        self.assertIn('file-a', self.b.CONVERSATIONS.keys())

    async def test_conversation_map_survives_restart(self):
        """The point of persisting: don't orphan a live Discord thread."""
        await self.b.deliver_mail(self.user, 'f1', mail(msg_id='id-1'))
        thread_id = self.channel.threads[0].id

        reborn = self.b.ConversationStore(self.b.CONVERSATIONS_FILE)
        conv = reborn.get('id-1')
        self.assertEqual(conv.thread_id, thread_id)
        self.assertEqual(conv.agent, 'mayor')

    async def test_muted_conversation_still_threads_but_does_not_dm(self):
        self.b.SETTINGS.mute('id-1')
        await self.b.deliver_mail(self.user, 'f1', mail(msg_id='id-1'))
        self.assertEqual(len(self.channel.threads), 1)
        self.assertEqual(self.channel.threads[0].sent and True, True)
        self.assertEqual(self.user.sent, [], 'muted conversation sent a DM')

    async def test_curated_policy_threads_everything_dms_decisions(self):
        b = load_threaded(BRIDGET_DM_POLICY='curated')
        channel = FakeTextChannel(555, client=b.client)
        b.client.channels[555] = channel
        user = FakeUser()
        await b.deliver_mail(user, 'f1', mail(subject='FYI: green', msg_id='id-1'))
        await b.deliver_mail(user, 'f2', mail(subject='approval needed x', msg_id='id-2'))
        self.assertEqual(len(channel.threads), 2, 'log channel is the durable record')
        self.assertEqual(len(user.sent), 1, 'only the decision should interrupt')
        self.assertIn('approval needed', user.sent[0])

    async def test_archived_thread_is_reopened_not_duplicated(self):
        """Discord auto-archives idle threads and refuses sends to them."""
        await self.b.deliver_mail(self.user, 'f1', mail(msg_id='id-1'))
        thread = self.channel.threads[0]
        thread.archived = True

        await self.b.deliver_mail(
            self.user, 'f2', mail(msg_id='id-2', in_reply_to='id-1', refs='id-1'))
        self.assertTrue(thread.unarchived)
        self.assertEqual(len(self.channel.threads), 1)
        self.assertEqual(len(thread.sent), 2)

    async def test_deleted_thread_is_re_rooted(self):
        await self.b.deliver_mail(self.user, 'f1', mail(msg_id='id-1'))
        # The human deleted the thread: neither cache nor fetch resolves it.
        thread_id = self.channel.threads[0].id
        del self.b.client.channels[thread_id]

        await self.b.deliver_mail(
            self.user, 'f2', mail(msg_id='id-2', in_reply_to='id-1', refs='id-1'))
        self.assertEqual(len(self.channel.threads), 2)

    async def test_unusable_log_channel_falls_back_to_dm(self):
        """Mail must never be silently dropped because Discord misbehaved."""
        self.b.client.channels.clear()
        await self.b.deliver_mail(self.user, 'f1', mail(msg_id='id-1'))
        self.assertEqual(len(self.user.sent), 1)
        self.assertNotIn('reply in thread', self.user.sent[0])
        self.assertIn('log channel unreachable', self.user.sent[0])

    async def test_log_channel_that_cannot_host_threads_is_reported(self):
        self.b.client.channels[555] = FakeDMChannel()  # no create_thread
        await self.b.deliver_mail(self.user, 'f1', mail(msg_id='id-1'))
        self.assertEqual(len(self.user.sent), 1, 'mail lost when channel cannot thread')

    async def test_stranded_mail_overrides_a_silencing_dm_policy(self):
        """The mail is already marked seen. If the thread failed AND the policy
        suppresses the DM, nothing would ever surface it again."""
        b = load_threaded(BRIDGET_DM_POLICY='none')
        user = FakeUser()
        b.client.channels.clear()  # log channel unreachable
        await b.deliver_mail(user, 'f1', mail(subject='FYI: green', msg_id='id-1'))
        self.assertEqual(len(user.sent), 1, 'mail vanished under dm_policy=none')
        self.assertIn('log channel unreachable', user.sent[0])

    async def test_stranded_mail_overrides_a_mute(self):
        self.b.client.channels.clear()
        self.b.SETTINGS.mute('id-1')
        await self.b.deliver_mail(self.user, 'f1', mail(msg_id='id-1'))
        self.assertEqual(len(self.user.sent), 1, 'muted mail vanished when thread failed')

    async def test_stranded_mail_overrides_quiet_hours(self):
        b = load_threaded(BRIDGET_QUIET_RESPECTS_OUTBOUND='true')
        user = FakeUser()
        b.client.channels.clear()
        with mock.patch.object(b, 'is_quiet_now', return_value=True):
            await b.deliver_mail(user, 'f1', mail(msg_id='id-1'))
        self.assertEqual(len(user.sent), 1, 'mail vanished during quiet hours')

    async def test_healthy_thread_does_not_override_the_policy(self):
        """The override is only for the stranded case — don't undo the calm inbox."""
        b = load_threaded(BRIDGET_DM_POLICY='none')
        channel = FakeTextChannel(555, client=b.client)
        b.client.channels[555] = channel
        user = FakeUser()
        await b.deliver_mail(user, 'f1', mail(msg_id='id-1'))
        self.assertEqual(len(channel.threads), 1)
        self.assertEqual(user.sent, [], 'dm_policy=none must still suppress a healthy delivery')

    async def test_threading_off_and_quiet_still_suppresses(self):
        """With no log channel there is nothing to be stranded from; quiet wins."""
        b = load_bridget(env_overrides={'BRIDGET_QUIET_RESPECTS_OUTBOUND': 'true'})
        user = FakeUser()
        with mock.patch.object(b, 'is_quiet_now', return_value=True):
            await b.deliver_mail(user, 'f1', mail(msg_id='id-1'))
        self.assertEqual(user.sent, [])

    async def test_threading_disabled_dms_without_thread_link(self):
        b = load_bridget()
        user = FakeUser()
        await b.deliver_mail(user, 'f1', mail(msg_id='id-1'))
        self.assertEqual(len(user.sent), 1)
        self.assertNotIn('discord.com/channels', user.sent[0])


class TestNoDoublePostOnRedelivery(unittest.IsolatedAsyncioTestCase):
    """A1. `post_to_thread` runs before `user.send`. When the DM fails the mail
    is redelivered next poll — and, before the `posted_ids` guard, posted into
    the thread a second time.

    The suite's older at-least-once tests mock `deliver_mail` itself, so
    `post_to_thread` never runs and the duplicate never shows up. These drive
    the real thing."""

    async def _threaded(self):
        b = load_threaded()
        channel = FakeTextChannel(555, client=b.client)
        b.client.channels[555] = channel
        return b, channel

    async def test_a_failed_dm_then_a_retry_posts_to_the_thread_once(self):
        b, channel = await self._threaded()

        class Failing(FakeUser):
            async def send(self, content):
                raise FakeHTTPException('429 rate limited')

        m = mail(msg_id='id-1')
        self.assertFalse(await b.deliver_mail(Failing(), 'f1', m))
        self.assertEqual(len(channel.threads), 1)
        self.assertEqual(len(channel.threads[0].sent), 1)

        # The retry. The thread already holds this mail.
        user = FakeUser()
        self.assertTrue(await b.deliver_mail(user, 'f1', m))
        self.assertEqual(len(channel.threads), 1, 'the retry rooted a second thread')
        self.assertEqual(len(channel.threads[0].sent), 1,
                         'the retry posted the same mail into the thread twice')
        self.assertEqual(len(user.sent), 1, 'the retry never reached the DM')

    async def test_the_retry_still_carries_the_thread_link(self):
        """The guard short-circuits the post; it must not lose the URL that
        tells the human where their mail landed."""
        b, channel = await self._threaded()

        class Failing(FakeUser):
            async def send(self, content):
                raise FakeHTTPException('503')

        m = mail(msg_id='id-1')
        await b.deliver_mail(Failing(), 'f1', m)
        user = FakeUser()
        await b.deliver_mail(user, 'f1', m)
        self.assertIn(f'/{channel.threads[0].id}', user.sent[0])

    async def test_a_crash_before_commit_redelivers_without_duplicating(self):
        """The A1+A7 pair, together. The watcher redelivers (A7); the store
        keeps the redelivery out of the thread (A1)."""
        b, channel = await self._threaded()
        m = mail(msg_id='id-1')
        await b.deliver_mail(FakeUser(), 'f1', m)   # delivered, never committed
        await b.deliver_mail(FakeUser(), 'f1', m)   # replayed after the crash
        self.assertEqual(len(channel.threads[0].sent), 1)

    async def test_a_redelivery_re_roots_a_thread_the_human_deleted(self):
        """The guard is consulted after resolve_thread, not before. Skipping the
        post because we once put this mail in a thread that no longer exists
        would lose it from the durable record entirely."""
        b, channel = await self._threaded()

        class Failing(FakeUser):
            async def send(self, content):
                raise FakeHTTPException('429')

        m = mail(msg_id='id-1')
        await b.deliver_mail(Failing(), 'f1', m)
        old_thread = channel.threads[0]
        self.assertEqual(len(old_thread.sent), 1)

        # The human deletes the thread; Discord 404s on fetch.
        del b.client.channels[old_thread.id]

        user = FakeUser()
        self.assertTrue(await b.deliver_mail(user, 'f1', m))
        self.assertEqual(len(channel.threads), 2, 'the deleted thread was not re-rooted')
        new_thread = channel.threads[1]
        self.assertEqual(len(new_thread.sent), 1, 'the mail never reached the new thread')
        self.assertIn(f'/{new_thread.id}', user.sent[0])

    async def test_re_rooting_clears_the_posted_set(self):
        b, channel = await self._threaded()
        m = mail(msg_id='id-1')
        await b.deliver_mail(FakeUser(), 'f1', m)
        self.assertTrue(b.CONVERSATIONS.was_posted('id-1', 'id-1'))
        b.CONVERSATIONS.bind_thread('id-1', 999999)
        self.assertFalse(b.CONVERSATIONS.was_posted('id-1', 'id-1'),
                         'a new thread inherited the old thread posted-set')

    async def test_rebinding_the_same_thread_id_keeps_the_posted_set(self):
        b, channel = await self._threaded()
        m = mail(msg_id='id-1')
        await b.deliver_mail(FakeUser(), 'f1', m)
        tid = channel.threads[0].id
        b.CONVERSATIONS.bind_thread('id-1', tid)
        self.assertTrue(b.CONVERSATIONS.was_posted('id-1', 'id-1'))

    async def test_a_failed_thread_post_is_retried_not_skipped(self):
        """`mark_posted` runs only on success. A post recorded optimistically
        would trade a duplicate for a drop, which is the wrong way round."""
        b, channel = await self._threaded()
        m = mail(msg_id='id-1')

        with mock.patch.object(b, 'post_to_thread', new=mock.AsyncMock(return_value='')):
            self.assertTrue(await b.deliver_mail(FakeUser(), 'f1', m))
        self.assertFalse(b.CONVERSATIONS.was_posted('id-1', 'id-1'))

        await b.deliver_mail(FakeUser(), 'f1', m)
        self.assertEqual(len(channel.threads[0].sent), 1, 'the thread post was never retried')

    async def test_a_distinct_mail_in_the_same_conversation_still_posts(self):
        b, channel = await self._threaded()
        await b.deliver_mail(FakeUser(), 'f1', mail(msg_id='id-1'))
        await b.deliver_mail(FakeUser(), 'f2', mail(msg_id='id-2', in_reply_to='id-1'))
        self.assertEqual(len(channel.threads), 1)
        self.assertEqual(len(channel.threads[0].sent), 2)


class TestAtLeastOnceDelivery(unittest.IsolatedAsyncioTestCase):
    """`poll()` hands out mail without marking it seen; the watcher commits only
    after a send lands. A transient Discord 429/5xx must therefore leave the
    mail uncommitted so the next poll returns it again."""

    async def test_dm_send_failure_reports_false_so_the_caller_can_retry(self):
        b = load_bridget()

        class Failing(FakeUser):
            async def send(self, content):
                raise FakeHTTPException('429 rate limited')

        ok = await b.deliver_mail(Failing(), 'f1', mail(msg_id='id-1'))
        self.assertFalse(ok, 'a failed DM must not report success')

    async def test_successful_dm_reports_true(self):
        b = load_bridget()
        self.assertTrue(await b.deliver_mail(FakeUser(), 'f1', mail(msg_id='id-1')))

    async def test_suppressed_mail_reports_true_and_is_not_retried(self):
        """Suppression is a decision, not a failure. Retrying it would loop."""
        b = load_threaded(BRIDGET_DM_POLICY='curated')
        channel = FakeTextChannel(555, client=b.client)
        b.client.channels[555] = channel
        ok = await b.deliver_mail(FakeUser(), 'f1', mail(subject='FYI', msg_id='id-1'))
        self.assertTrue(ok)

    async def test_failed_dm_is_uncommitted_and_redelivered_next_poll(self):
        b = load_bridget()
        sent = []
        attempts = {'n': 0}

        async def flaky_deliver(_u, filename, _m):
            attempts['n'] += 1
            if attempts['n'] == 1:
                return False          # transient failure
            sent.append(filename)
            return True

        watcher = mock.MagicMock()
        watcher.primed = True
        watcher.poll.side_effect = [[('f1', mail(msg_id='id-1'))],
                                    [('f1', mail(msg_id='id-1'))], []]
        closed = iter([False, False, True, True])

        with mock.patch.object(b, 'MaildirWatcher', return_value=watcher), \
             mock.patch.object(b, 'deliver_mail', side_effect=flaky_deliver), \
             mock.patch.object(b, 'send_startup_dm', new=mock.AsyncMock()), \
             mock.patch.object(b.client, 'is_closed', side_effect=lambda: next(closed)), \
             mock.patch.object(b.asyncio, 'sleep', new=mock.AsyncMock()):
            await b.watch_mailbox(FakeUser())

        # The failed attempt commits nothing; only the successful retry does.
        watcher.commit.assert_called_once_with('f1')
        self.assertEqual(sent, ['f1'], 'mail was not retried after a failed send')

    async def test_exception_during_delivery_commits_nothing(self):
        b = load_bridget()
        watcher = mock.MagicMock()
        watcher.primed = True
        watcher.poll.side_effect = [[('f1', mail(msg_id='id-1'))], []]
        closed = iter([False, True, True])

        with mock.patch.object(b, 'MaildirWatcher', return_value=watcher), \
             mock.patch.object(b, 'deliver_mail', side_effect=RuntimeError('boom')), \
             mock.patch.object(b, 'send_startup_dm', new=mock.AsyncMock()), \
             mock.patch.object(b.client, 'is_closed', side_effect=lambda: next(closed)), \
             mock.patch.object(b.asyncio, 'sleep', new=mock.AsyncMock()):
            await b.watch_mailbox(FakeUser())

        watcher.commit.assert_not_called()

    async def test_a_delivered_mail_is_committed(self):
        b = load_bridget()
        watcher = mock.MagicMock()
        watcher.primed = True
        watcher.poll.side_effect = [[('f1', mail(msg_id='id-1'))], []]
        closed = iter([False, True, True])

        with mock.patch.object(b, 'MaildirWatcher', return_value=watcher), \
             mock.patch.object(b, 'send_startup_dm', new=mock.AsyncMock()), \
             mock.patch.object(b.client, 'is_closed', side_effect=lambda: next(closed)), \
             mock.patch.object(b.asyncio, 'sleep', new=mock.AsyncMock()):
            await b.watch_mailbox(FakeUser())

        watcher.commit.assert_called_once_with('f1')


class TestNeverConsumeUnsurfaceableMail(unittest.IsolatedAsyncioTestCase):
    """With threads off the DM is the only push surface. Polling mail we cannot
    push anywhere would mark it seen and lose it, so the watcher must not poll."""

    def _run_one_cycle(self, b):
        watcher = mock.MagicMock()
        watcher.primed = True
        watcher.poll.return_value = []
        closed = iter([False, True, True])
        return watcher, closed

    async def test_mute_all_with_threads_off_defers_instead_of_consuming(self):
        b = load_bridget()
        b.SETTINGS.set_mute_all(True)
        watcher, closed = self._run_one_cycle(b)
        with mock.patch.object(b, 'MaildirWatcher', return_value=watcher), \
             mock.patch.object(b, 'send_startup_dm', new=mock.AsyncMock()), \
             mock.patch.object(b.client, 'is_closed', side_effect=lambda: next(closed)), \
             mock.patch.object(b.asyncio, 'sleep', new=mock.AsyncMock()):
            await b.watch_mailbox(FakeUser())
        watcher.poll.assert_not_called()

    async def test_quiet_hours_with_threads_off_defers_instead_of_consuming(self):
        b = load_bridget(env_overrides={'BRIDGET_QUIET_RESPECTS_OUTBOUND': 'true'})
        watcher, closed = self._run_one_cycle(b)
        with mock.patch.object(b, 'MaildirWatcher', return_value=watcher), \
             mock.patch.object(b, 'is_quiet_now', return_value=True), \
             mock.patch.object(b, 'send_startup_dm', new=mock.AsyncMock()), \
             mock.patch.object(b.client, 'is_closed', side_effect=lambda: next(closed)), \
             mock.patch.object(b.asyncio, 'sleep', new=mock.AsyncMock()):
            await b.watch_mailbox(FakeUser())
        watcher.poll.assert_not_called()

    async def test_mute_all_with_threads_on_still_polls_and_threads(self):
        """The log channel is a surface, so consuming the mail is safe."""
        b = load_threaded()
        b.SETTINGS.set_mute_all(True)
        watcher, closed = self._run_one_cycle(b)
        with mock.patch.object(b, 'MaildirWatcher', return_value=watcher), \
             mock.patch.object(b, 'send_startup_dm', new=mock.AsyncMock()), \
             mock.patch.object(b.client, 'is_closed', side_effect=lambda: next(closed)), \
             mock.patch.object(b.asyncio, 'sleep', new=mock.AsyncMock()):
            await b.watch_mailbox(FakeUser())
        watcher.poll.assert_called()

    async def test_normal_state_polls(self):
        b = load_bridget()
        watcher, closed = self._run_one_cycle(b)
        with mock.patch.object(b, 'MaildirWatcher', return_value=watcher), \
             mock.patch.object(b, 'send_startup_dm', new=mock.AsyncMock()), \
             mock.patch.object(b.client, 'is_closed', side_effect=lambda: next(closed)), \
             mock.patch.object(b.asyncio, 'sleep', new=mock.AsyncMock()):
            await b.watch_mailbox(FakeUser())
        watcher.poll.assert_called()

    def test_dm_globally_suppressed_matrix(self):
        b = load_bridget()
        self.assertFalse(b.dm_globally_suppressed())
        b.SETTINGS.set_mute_all(True)
        self.assertTrue(b.dm_globally_suppressed())
        b.SETTINGS.set_mute_all(False)
        self.assertFalse(b.dm_globally_suppressed())

    def test_per_conversation_mute_is_not_global(self):
        """A single muted conversation must not stop the watcher polling."""
        b = load_bridget()
        b.SETTINGS.mute('k1')
        self.assertFalse(b.dm_globally_suppressed())


class TestConversationRecordingIsThreadOnly(unittest.IsolatedAsyncioTestCase):
    async def test_no_conversation_file_written_when_threads_are_off(self):
        b = load_bridget()
        await b.deliver_mail(FakeUser(), 'f1', mail(msg_id='id-1'))
        self.assertEqual(len(b.CONVERSATIONS), 0)
        self.assertFalse(b.CONVERSATIONS_FILE.exists(),
                         'wrote a conversation map for a disabled feature')

    async def test_conversation_recorded_when_threads_are_on(self):
        b = load_threaded()
        b.client.channels[555] = FakeTextChannel(555, client=b.client)
        await b.deliver_mail(FakeUser(), 'f1', mail(msg_id='id-1'))
        self.assertEqual(len(b.CONVERSATIONS), 1)


class TestWatchMailboxRobustness(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b = load_threaded()
        self.channel = FakeTextChannel(555, client=self.b.client)
        self.b.client.channels[555] = self.channel

    async def test_one_bad_mail_does_not_swallow_the_rest_of_the_batch(self):
        """poll() marks the whole batch seen up front, so an exception on mail 1
        would strand mails 2..n forever."""
        user = FakeUser()
        batch = [('f1', mail(msg_id='id-1')),
                 ('f2', mail(msg_id='id-2')),
                 ('f3', mail(msg_id='id-3'))]
        delivered = []

        async def flaky(_user, filename, _mail):
            if filename == 'f1':
                raise RuntimeError('boom')
            delivered.append(filename)

        watcher = mock.MagicMock()
        watcher.primed = True
        # One populated poll, then empty forever; is_closed() ends the loop.
        watcher.poll.side_effect = [batch, []]
        closed = iter([False, True, True])

        with mock.patch.object(self.b, 'MaildirWatcher', return_value=watcher), \
             mock.patch.object(self.b, 'deliver_mail', side_effect=flaky), \
             mock.patch.object(self.b, 'send_startup_dm', new=mock.AsyncMock()), \
             mock.patch.object(self.b.client, 'is_closed', side_effect=lambda: next(closed)), \
             mock.patch.object(self.b.asyncio, 'sleep', new=mock.AsyncMock()):
            await self.b.watch_mailbox(user)

        self.assertEqual(delivered, ['f2', 'f3'], 'a bad mail swallowed the batch behind it')


# --- inbound: replies in threads --------------------------------------------

class TestReplyInConversation(unittest.TestCase):
    def setUp(self):
        self.b = load_threaded()
        # Assume an mg that speaks --in-reply-to; the seam itself is exercised
        # in TestCorrelationIdSeam.
        self.b.MG_CAPS.mode = 'on'
        self.b.CONVERSATIONS.record('id-1', subject='design review', agent='mayor',
                                    message_id='id-7')
        self.conv = self.b.CONVERSATIONS.get('id-1')

    def test_reply_threads_with_in_reply_to(self):
        """The correlation-ID seam, wired: this is what gh#66 bought."""
        with mock.patch.object(self.b, 'run_mg', return_value=(0, '', '')) as run:
            ack = self.b.reply_in_conversation('looks good', self.conv)
        args = run.call_args[0][0]
        self.assertIn('--in-reply-to=id-7', args)
        self.assertIn('mail', args)
        self.assertIn('mayor', args)
        self.assertIn('--from=human', args)
        self.assertTrue(ack.ok)
        self.assertTrue(ack.in_reply_to, 'the reply was not threaded')

    def test_reply_subject_defaults_to_re_conversation(self):
        with mock.patch.object(self.b, 'run_mg', return_value=(0, '', '')) as run:
            self.b.reply_in_conversation('looks good', self.conv)
        args = run.call_args[0][0]
        self.assertIn('--subject=Re: design review', args)
        self.assertIn('--body=looks good', args)

    def test_multiline_reply_keeps_the_conversation_subject(self):
        """A4, decided. This previously sent `--subject=ship it --body=after CI
        passes`, mirroring the `mail` verb's first-line-is-the-subject
        convention. In a thread the subject is already known and the human is
        replying, not composing: every line is body, and the subject continues
        the conversation."""
        with mock.patch.object(self.b, 'run_mg', return_value=(0, '', '')) as run:
            self.b.reply_in_conversation('ship it\nafter CI passes', self.conv)
        args = run.call_args[0][0]
        self.assertIn('--subject=Re: design review', args)
        self.assertIn('--body=ship it\nafter CI passes', args)

    def test_a_reply_never_stacks_re_prefixes(self):
        """`conv.subject` has had its `Re:` stripped by thread_title."""
        with mock.patch.object(self.b, 'run_mg', return_value=(0, '', '')) as run:
            self.b.reply_in_conversation('ok', self.conv)
        subject = next(a for a in run.call_args[0][0] if a.startswith('--subject='))
        self.assertEqual(subject, '--subject=Re: design review')

    def test_an_unnamed_conversation_still_gets_a_subject(self):
        """mg refuses a blank --subject."""
        self.b.CONVERSATIONS.record('id-9', subject='', agent='mayor')
        conv = self.b.CONVERSATIONS.get('id-9')
        with mock.patch.object(self.b, 'run_mg', return_value=(0, '', '')) as run:
            self.b.reply_in_conversation('hi', conv)
        self.assertIn('--subject=Re: mail from mayor', run.call_args[0][0])

    def test_channel_chat_puts_the_whole_message_in_the_body(self):
        """Channel chat is talking, not composing mail, so the body gets it all.

        This reverses an earlier decision (chat took its subject from the first
        line, like the `mail` verb). A chat message has no subject line — only a
        first sentence — and lifting that sentence into the subject is how
        mg-7e0c read a mid-clause fragment ("…you can delete and recreate if")
        as a complete instruction. The subject is now a derived label; the body
        is the message.
        """
        with mock.patch.object(self.b, 'run_mg', return_value=(0, '', '')) as run:
            self.b.send_channel_chat_mail('ship it\nafter CI passes', 'mayor')
        args = run.call_args[0][0]
        self.assertIn('--body=ship it\nafter CI passes', args)
        # One-line label, whitespace collapsed; the body still holds every byte.
        self.assertIn('--subject=ship it after CI passes', args)

    def test_the_mail_verb_still_splits_subject_from_body(self):
        """A4 is scoped to in-thread replies. The `mail` verb is the one place
        the human deliberately composes a subject, so its split is honoured."""
        with mock.patch.object(self.b, 'run_mg', return_value=(0, '', '')) as run:
            self.b.handle_command('mail ship it\nafter CI passes')
        args = run.call_args[0][0]
        self.assertIn('--subject=ship it', args)
        self.assertIn('--body=after CI passes', args)

    def test_reply_without_known_message_id_omits_the_flag(self):
        """Pre-gh#66 conversation: degrade to a top-level mail, don't fail."""
        self.b.CONVERSATIONS.record('id-2', subject='old', agent='mayor')
        conv = self.b.CONVERSATIONS.get('id-2')
        with mock.patch.object(self.b, 'run_mg', return_value=(0, '', '')) as run:
            ack = self.b.reply_in_conversation('hi', conv)
        self.assertFalse(any(a.startswith('--in-reply-to') for a in run.call_args[0][0]))
        self.assertTrue(ack.ok)
        self.assertFalse(ack.in_reply_to, 'the reply was threaded unexpectedly')

    def test_failed_send_is_undeliverable_with_the_reason(self):
        with mock.patch.object(self.b, 'run_mg', return_value=(1, '', 'no such mailbox')):
            ack = self.b.reply_in_conversation('hi', self.conv)
        self.assertEqual(ack.kind, 'undeliverable')
        self.assertIn('no such mailbox', ack.reason)
        self.assertEqual(ack.agent, 'mayor')

    def test_empty_reply_is_undeliverable(self):
        with mock.patch.object(self.b, 'run_mg') as run:
            ack = self.b.reply_in_conversation('   ', self.conv)
        run.assert_not_called()
        self.assertEqual(ack.kind, 'undeliverable')

    def test_unknown_sender_is_undeliverable(self):
        self.b.CONVERSATIONS.record('id-3', subject='s', agent='?')
        conv = self.b.CONVERSATIONS.get('id-3')
        with mock.patch.object(self.b, 'run_mg') as run:
            ack = self.b.reply_in_conversation('hi', conv)
        run.assert_not_called()
        self.assertEqual(ack.kind, 'undeliverable')

    def test_reply_targets_the_newest_message_not_the_root(self):
        self.b.CONVERSATIONS.record('id-1', message_id='id-9')
        conv = self.b.CONVERSATIONS.get('id-1')
        with mock.patch.object(self.b, 'run_mg', return_value=(0, '', '')) as run:
            self.b.reply_in_conversation('hi', conv)
        self.assertIn('--in-reply-to=id-9', run.call_args[0][0])

    def test_reply_asks_mg_for_the_id_it_assigned(self):
        with mock.patch.object(self.b, 'run_mg', return_value=(0, '{}', '')) as run:
            self.b.reply_in_conversation('hi', self.conv)
        self.assertIn('--json', run.call_args[0][0])

    def test_the_sent_reply_is_folded_into_the_conversation(self):
        """So the agent's answer — which names *this* id and nothing older —
        resolves back to this conversation instead of rooting a new thread."""
        with mock.patch.object(self.b, 'run_mg', return_value=(0, '{"msg_id":"out-1"}', '')):
            ack = self.b.reply_in_conversation('looks good', self.conv)
        self.assertTrue(ack.ok)
        conv = self.b.CONVERSATIONS.get('id-1')
        self.assertIn('out-1', conv.message_ids)
        self.assertEqual(conv.last_message_id, 'out-1',
                         'a second reply must thread onto the first, not behind it')
        self.assertEqual(self.b.CONVERSATIONS.resolve(['out-1']), 'id-1')

    def test_a_send_that_reports_no_id_still_delivers(self):
        """Degrade by one hop of threading, never by failing a sent reply."""
        with mock.patch.object(self.b, 'run_mg', return_value=(0, 'who knows', '')):
            ack = self.b.reply_in_conversation('hi', self.conv)
        self.assertTrue(ack.ok)
        self.assertEqual(self.b.CONVERSATIONS.get('id-1').last_message_id, 'id-7')

    def test_a_failed_send_is_not_folded_in(self):
        with mock.patch.object(self.b, 'run_mg', return_value=(1, '{"msg_id":"out-1"}', 'boom')):
            ack = self.b.reply_in_conversation('hi', self.conv)
        self.assertFalse(ack.ok)
        self.assertNotIn('out-1', self.b.CONVERSATIONS.get('id-1').message_ids)


class TestCorrelationIdSeam(unittest.TestCase):
    """The bridge must never hard-depend on `mg mail send --in-reply-to`.

    That flag ships in some builds of mg and not others, and a stray
    `go install` swaps the binary under a running bridge. If we passed the flag
    blindly, every threaded reply would come back `undeliverable`.
    """

    def setUp(self):
        self.b = load_threaded()
        self.b.CONVERSATIONS.record('id-1', subject='s', agent='mayor', message_id='id-7')
        self.conv = self.b.CONVERSATIONS.get('id-1')

    def _force(self, supported: bool):
        self.b.MG_CAPS.mode = 'on' if supported else 'off'

    def test_flag_omitted_when_mg_lacks_the_capability(self):
        self._force(False)
        with mock.patch.object(self.b, 'run_mg', return_value=(0, '', '')) as run:
            ack = self.b.reply_in_conversation('hi', self.conv)
        self.assertNotIn('--in-reply-to=id-7', run.call_args[0][0])
        self.assertNotIn('--json', run.call_args[0][0])
        self.assertTrue(ack.ok, 'reply must still deliver without threading')
        self.assertFalse(ack.in_reply_to, 'the reply was threaded unexpectedly')

    def test_an_mg_that_rejects_json_alone_downgrades_rather_than_failing(self):
        """With no parent id to thread onto, `--json` goes out on its own, so it
        is the flag an ancient mg names. `mode='on'` skips the help probe, which
        is exactly how an operator gets here."""
        self._force(True)
        self.b.CONVERSATIONS.record('id-4', subject='s', agent='mayor')
        conv = self.b.CONVERSATIONS.get('id-4')
        outcomes = [(1, '', 'unknown flag: --json'), (0, '', '')]
        with mock.patch.object(self.b, 'run_mg', side_effect=outcomes) as run:
            ack = self.b.reply_in_conversation('hi', conv)
        self.assertTrue(ack.ok, 'a reply that plainly went out was reported undeliverable')
        self.assertEqual(run.call_count, 2)
        self.assertNotIn('--json', run.call_args[0][0])

    def test_flag_passed_when_mg_has_the_capability(self):
        self._force(True)
        with mock.patch.object(self.b, 'run_mg', return_value=(0, '', '')) as run:
            ack = self.b.reply_in_conversation('hi', self.conv)
        self.assertIn('--in-reply-to=id-7', run.call_args[0][0])
        self.assertTrue(ack.in_reply_to, 'the reply was not threaded')

    def test_capability_is_autodetected_from_mg_help(self):
        help_with = 'Flags:\n      --in-reply-to string   MSG-ID\n'
        with mock.patch.object(self.b, 'run_mg', return_value=(0, help_with, '')):
            self.b.MG_CAPS = self.b.MgCapabilities(self.b._probe_mg_help)
            self.assertTrue(self.b.MG_CAPS.correlation_ids)

        help_without = 'Flags:\n      --body string   body\n'
        with mock.patch.object(self.b, 'run_mg', return_value=(0, help_without, '')):
            self.b.MG_CAPS = self.b.MgCapabilities(self.b._probe_mg_help)
            self.assertFalse(self.b.MG_CAPS.correlation_ids)

    def test_stale_capability_retries_without_the_flag(self):
        """mg was rebuilt mid-session and now rejects the flag we probed for."""
        self._force(True)
        results = [(1, '', 'Error: unknown flag: --in-reply-to'), (0, '', '')]
        with mock.patch.object(self.b, 'run_mg', side_effect=results) as run:
            ack = self.b.reply_in_conversation('hi', self.conv)

        self.assertEqual(run.call_count, 2)
        self.assertIn('--in-reply-to=id-7', run.call_args_list[0][0][0])
        self.assertNotIn('--in-reply-to=id-7', run.call_args_list[1][0][0])
        self.assertTrue(ack.ok, 'stale capability must not surface as undeliverable')

    def test_stale_capability_downgrade_sticks_for_later_replies(self):
        self.b.MG_CAPS.mode = 'auto'
        self.b.MG_CAPS._probed = True
        results = [(1, '', 'Error: unknown flag: --in-reply-to'), (0, '', ''), (0, '', '')]
        with mock.patch.object(self.b, 'run_mg', side_effect=results) as run:
            self.b.reply_in_conversation('hi', self.conv)
            self.b.reply_in_conversation('again', self.conv)
        self.assertEqual(run.call_count, 3, 'second reply should not retry the bad flag')
        self.assertNotIn('--in-reply-to=id-7', run.call_args_list[2][0][0])

    def test_a_real_send_failure_is_still_undeliverable(self):
        """Don't swallow genuine errors as if they were a stale flag."""
        self._force(True)
        with mock.patch.object(self.b, 'run_mg', return_value=(1, '', 'no such mailbox')) as run:
            ack = self.b.reply_in_conversation('hi', self.conv)
        self.assertEqual(run.call_count, 1, 'must not retry a non-flag error')
        self.assertEqual(ack.kind, 'undeliverable')
        self.assertIn('no such mailbox', ack.reason)

    def test_correlation_ids_mode_is_configurable(self):
        self.assertEqual(load_threaded(BRIDGET_CORRELATION_IDS='off').MG_CAPS.mode, 'off')
        self.assertEqual(load_threaded(BRIDGET_CORRELATION_IDS='on').MG_CAPS.mode, 'on')

    def test_bogus_correlation_ids_mode_is_rejected(self):
        with self.assertRaises(SystemExit):
            load_threaded(BRIDGET_CORRELATION_IDS='maybe')

    def test_settings_reports_the_capability(self):
        self._force(False)
        self.assertIn('Correlation IDs', self.b.handle_command('settings'))
        self.assertIn('off (forced)', self.b.handle_command('settings'))


class TestHandleThreadMessage(unittest.TestCase):
    def setUp(self):
        self.b = load_threaded()
        # Pin the capability so a lazy probe doesn't consume a mocked run_mg call.
        self.b.MG_CAPS.mode = 'on'
        self.b.CONVERSATIONS.record('id-1', subject='design review', agent='mayor',
                                    message_id='id-7')
        self.conv = self.b.CONVERSATIONS.get('id-1')

    def test_mute_in_thread_needs_no_argument(self):
        out = self.b.handle_thread_message('mute', self.conv)
        self.assertIn('muted', out.lower())
        self.assertTrue(self.b.SETTINGS.is_muted('id-1'))

    def test_mute_is_idempotent(self):
        self.b.handle_thread_message('mute', self.conv)
        self.assertIn('Already muted', self.b.handle_thread_message('mute', self.conv))

    def test_unmute_in_thread(self):
        self.b.SETTINGS.mute('id-1')
        self.b.handle_thread_message('unmute', self.conv)
        self.assertFalse(self.b.SETTINGS.is_muted('id-1'))

    def test_unmute_when_not_muted_says_so(self):
        self.assertIn('not muted', self.b.handle_thread_message('unmute', self.conv))

    def test_free_text_becomes_a_reply(self):
        with mock.patch.object(self.b, 'run_mg', return_value=(0, '', '')) as run:
            out = self.b.handle_thread_message('sounds good to me', self.conv)
        run.assert_called_once()
        self.assertIn('delivered', out)

    def test_workflow_verb_still_routes_to_the_workflow_agent(self):
        """`approve mg-1234` must mean the same thing in a thread as in a DM."""
        with mock.patch.object(self.b, 'run_mg', return_value=(0, 'ok', '')) as run:
            self.b.handle_thread_message('approve mg-1234', self.conv)
        joined = ' '.join(run.call_args_list[0][0][0])
        self.assertNotIn('--in-reply-to', joined)

    def test_help_in_thread_names_the_agent(self):
        self.assertIn('mayor', self.b.handle_thread_message('help', self.conv))

    def test_prose_starting_with_a_command_word_is_a_reply_not_a_command(self):
        """The bug: `handle_command` matches `status` with startswith, so a
        genuine reply was swallowed and the agent never heard it."""
        for text in ('status is green, ship it',
                     'dm the client tomorrow',
                     'restart the deploy when you can',
                     'nudge me if it stalls',
                     'agents are all idle now',
                     'balance looks fine',
                     'quiet down, this is fine',
                     'settings look right to me',
                     'dismiss all of that, it was noise'):
            with self.subTest(text=text):
                with mock.patch.object(self.b, 'run_mg', return_value=(0, '', '')) as run:
                    out = self.b.handle_thread_message(text, self.conv)
                self.assertIn('delivered', out, f'{text!r} was intercepted as a command')
                args = run.call_args[0][0]
                self.assertEqual(args[:3], ['mail', 'send', 'mayor'])

    def test_dismiss_all_in_a_thread_does_not_touch_the_maildir(self):
        """`dismiss all of that` as prose must never inbox-zero the human."""
        with mock.patch.object(self.b, 'mark_mail_read') as marker, \
             mock.patch.object(self.b, 'run_mg', return_value=(0, '', '')):
            self.b.handle_thread_message('dismiss all of that noise', self.conv)
        marker.assert_not_called()

    def test_unambiguous_verbs_with_an_mg_id_are_still_commands(self):
        for text in ('approve mg-1234', 'reject mg-abcd because no',
                     'revise mg-9be0 please', 'explain mg-4c6b the seam',
                     'read mg-0001', 'dismiss mg-0002'):
            with self.subTest(text=text):
                self.assertTrue(self.b.THREAD_VERB_RE.match(text))

    def test_idea_and_bug_prefixes_are_still_commands(self):
        self.assertTrue(self.b.THREAD_VERB_RE.match('idea: add dark mode'))
        self.assertTrue(self.b.THREAD_VERB_RE.match('bug: it crashes'))

    def test_prose_does_not_match_the_verb_regex(self):
        for text in ('status is green', 'dismiss all of that', 'next steps are clear',
                     'read the docs', 'approve of this approach'):
            with self.subTest(text=text):
                self.assertIsNone(self.b.THREAD_VERB_RE.match(text))


# --- inbound: on_message routing --------------------------------------------

class TestOnMessageRouting(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b = load_threaded()
        self.b.MG_CAPS.mode = 'on'
        self.b.CONVERSATIONS.record('id-1', subject='design review', agent='mayor',
                                    message_id='id-7')
        self.b.CONVERSATIONS.bind_thread('id-1', 9001)

    def _msg(self, channel, content, author_id=1, bot=False):
        return types.SimpleNamespace(
            author=types.SimpleNamespace(id=author_id, bot=bot),
            channel=channel, content=content)

    async def test_reply_in_known_thread_is_mailed(self):
        thread = FakeThread(9001)
        with mock.patch.object(self.b, 'run_mg', return_value=(0, '', '')) as run:
            await self.b.on_message(self._msg(thread, 'looks good'))
        self.assertIn('--in-reply-to=id-7', run.call_args[0][0])
        self.assertIn('delivered', thread.sent[0])

    async def test_message_in_unknown_thread_is_ignored(self):
        thread = FakeThread(4242)
        with mock.patch.object(self.b, 'run_mg') as run:
            await self.b.on_message(self._msg(thread, 'hello'))
        run.assert_not_called()
        self.assertEqual(thread.sent, [])

    async def test_free_text_in_log_channel_gets_an_ambiguity_ack(self):
        channel = FakeTextChannel(555)
        await self.b.on_message(self._msg(channel, 'yes do that'))
        self.assertIn('ambiguous', channel.sent[0])
        self.assertIn('design review', channel.sent[0])

    async def test_command_in_log_channel_still_works(self):
        channel = FakeTextChannel(555)
        await self.b.on_message(self._msg(channel, 'help'))
        self.assertNotIn('ambiguous', channel.sent[0])

    async def test_bot_messages_ignored(self):
        thread = FakeThread(9001)
        await self.b.on_message(self._msg(thread, 'hi', bot=True))
        self.assertEqual(thread.sent, [])

    async def test_other_users_ignored(self):
        thread = FakeThread(9001)
        await self.b.on_message(self._msg(thread, 'hi', author_id=999))
        self.assertEqual(thread.sent, [])

    async def test_dm_still_routes_to_handle_command(self):
        dm = FakeDMChannel()
        await self.b.on_message(self._msg(dm, 'help'))
        self.assertIn('approve mg-XXXX', dm.sent[0])


# --- settings commands -------------------------------------------------------

class TestSettingsCommands(unittest.TestCase):
    def test_settings_reports_threading_off(self):
        b = load_bridget()
        self.assertIn('Threads: `off`', b.handle_command('settings'))

    def test_settings_reports_threading_on(self):
        b = load_threaded()
        out = b.handle_command('settings')
        self.assertIn('Threads: `on`', out)
        self.assertIn('<#555>', out)

    def test_dm_command_changes_policy_live(self):
        b = load_threaded()
        self.assertIn('curated', b.handle_command('dm curated'))
        self.assertEqual(b.SETTINGS.dm_policy, 'curated')

    def test_dm_command_rejects_unknown_policy(self):
        b = load_threaded()
        self.assertIn('unknown DM policy', b.handle_command('dm loud'))
        self.assertEqual(b.SETTINGS.dm_policy, 'all')

    def test_dm_command_refuses_to_silence_without_a_log_channel(self):
        b = load_bridget()
        self.assertIn('needs a log channel', b.handle_command('dm none'))
        self.assertEqual(b.SETTINGS.dm_policy, 'all')

    def test_bare_dm_reports_current_policy(self):
        self.assertIn('`all`', load_bridget().handle_command('dm'))

    def test_mute_all_and_unmute_all(self):
        b = load_bridget()
        self.assertIn('muted', b.handle_command('mute all'))
        self.assertTrue(b.SETTINGS.mute_all)
        self.assertIn('unmuted', b.handle_command('unmute all'))
        self.assertFalse(b.SETTINGS.mute_all)

    def test_bare_mute_in_a_dm_explains_itself(self):
        self.assertIn('inside a conversation thread', load_bridget().handle_command('mute'))

    def test_bare_unmute_explains_rather_than_unmuting_everything(self):
        """`mute` only explained while `unmute` silently acted on all DMs."""
        b = load_bridget()
        b.SETTINGS.set_mute_all(True)
        out = b.handle_command('unmute')
        self.assertIn('inside a conversation thread', out)
        self.assertTrue(b.SETTINGS.mute_all, 'bare `unmute` unmuted everything')

    def test_mute_all_without_a_log_channel_does_not_promise_threading(self):
        """It used to claim 'Mail still threads into the log channel' with no
        log channel configured — a plain falsehood."""
        b = load_bridget()
        out = b.handle_command('mute all')
        self.assertNotIn('still threads into the log channel', out)
        self.assertIn('only surface', out)
        self.assertIn('held', out)

    def test_mute_all_with_a_log_channel_does_promise_threading(self):
        b = load_threaded()
        self.assertIn('still threads into the log channel', b.handle_command('mute all'))

    def test_settings_persist_across_restart(self):
        b = load_threaded()
        b.handle_command('dm curated')
        reborn = b.SettingsStore(b.SETTINGS_FILE)
        self.assertEqual(reborn.dm_policy, 'curated')

    def test_help_lists_the_new_commands(self):
        out = load_bridget().handle_command('help')
        for token in ('settings', 'dm <all|curated|none>', 'mute all'):
            self.assertIn(token, out)

    def test_unrecognized_still_unrecognized(self):
        b = load_bridget()
        self.assertEqual(b.handle_command('flibbertigibbet'), b.UNRECOGNIZED_REPLY)


if __name__ == '__main__':
    unittest.main(verbosity=2)
