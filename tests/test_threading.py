#!/usr/bin/env python3
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


class FakeClient:
    def __init__(self, *a, **kw):
        self.channels = {}
        self._closed = False

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
    (env_dir / 'bridget.env').write_text(
        'DISCORD_BOT_TOKEN=fake\n'
        'DISCORD_USER_ID=1\n'
        'DISCORD_SERVER_ID=2\n' + env_file_extra
    )
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

    async def test_log_channel_that_cannot_host_threads_is_reported(self):
        self.b.client.channels[555] = FakeDMChannel()  # no create_thread
        await self.b.deliver_mail(self.user, 'f1', mail(msg_id='id-1'))
        self.assertEqual(len(self.user.sent), 1, 'mail lost when channel cannot thread')

    async def test_threading_disabled_dms_without_thread_link(self):
        b = load_bridget()
        user = FakeUser()
        await b.deliver_mail(user, 'f1', mail(msg_id='id-1'))
        self.assertEqual(len(user.sent), 1)
        self.assertNotIn('discord.com/channels', user.sent[0])


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
        self.assertIn('threaded', ack.text)

    def test_reply_subject_defaults_to_re_conversation(self):
        with mock.patch.object(self.b, 'run_mg', return_value=(0, '', '')) as run:
            self.b.reply_in_conversation('looks good', self.conv)
        args = run.call_args[0][0]
        self.assertIn('--subject=Re: design review', args)
        self.assertIn('--body=looks good', args)

    def test_multiline_reply_splits_subject_and_body(self):
        with mock.patch.object(self.b, 'run_mg', return_value=(0, '', '')) as run:
            self.b.reply_in_conversation('ship it\nafter CI passes', self.conv)
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
        self.assertNotIn('threaded', ack.text)

    def test_failed_send_is_undeliverable_with_the_reason(self):
        with mock.patch.object(self.b, 'run_mg', return_value=(1, '', 'no such mailbox')):
            ack = self.b.reply_in_conversation('hi', self.conv)
        self.assertEqual(ack.kind, 'undeliverable')
        self.assertIn('no such mailbox', ack.text)
        self.assertIn('mayor', ack.text)

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
        self.assertTrue(ack.ok, 'reply must still deliver without threading')
        self.assertNotIn('threaded', ack.text)

    def test_flag_passed_when_mg_has_the_capability(self):
        self._force(True)
        with mock.patch.object(self.b, 'run_mg', return_value=(0, '', '')) as run:
            ack = self.b.reply_in_conversation('hi', self.conv)
        self.assertIn('--in-reply-to=id-7', run.call_args[0][0])
        self.assertIn('threaded', ack.text)

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
        self.assertIn('no such mailbox', ack.text)

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
        """`approve mg-1` must mean the same thing in a thread as in a DM."""
        with mock.patch.object(self.b, 'run_mg', return_value=(0, 'ok', '')) as run:
            self.b.handle_thread_message('approve mg-1234', self.conv)
        joined = ' '.join(run.call_args_list[0][0][0])
        self.assertNotIn('--in-reply-to', joined)

    def test_help_in_thread_names_the_agent(self):
        self.assertIn('mayor', self.b.handle_thread_message('help', self.conv))


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
