#!/usr/bin/env python3
"""Tests for bridget_core — the transport-agnostic bridge core.

Note what this file does *not* do: stub `discord`. The core must import and
pass its whole suite with no chat platform present at all. If someone later
reaches for a Discord type inside bridget_core, this suite stops importing and
says so.

Covers:
- correlation-ID parsing (Message-Id / In-Reply-To / References) + folding
- conversation-key derivation, including pre-gh#66 mail with no headers
- ConversationStore persistence, thread binding, restart survival, pruning
- SettingsStore defaults, live reload, mute semantics
- MaildirWatcher observe-only invariant, priming, seen-set de-dup
- the ack outcome model
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bridget_core import (  # noqa: E402
    ConversationStore,
    MaildirWatcher,
    SettingsStore,
    ambiguous,
    conversation_key,
    delivered,
    parse_mail,
    undeliverable,
)
from bridget_core.acks import AMBIGUOUS, DELIVERED, UNDELIVERABLE  # noqa: E402
from bridget_core.mail import reply_target, thread_title  # noqa: E402
from bridget_core.mgshim import (  # noqa: E402
    MgCapabilities,
    build_send_args,
    help_advertises_in_reply_to,
    is_unknown_flag_error,
)


def tmpdir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


class TestNoTransportImport(unittest.TestCase):
    def test_core_does_not_import_discord(self):
        """The whole point of the core/adapter split."""
        self.assertNotIn('discord', sys.modules)


class TestParseMail(unittest.TestCase):
    def test_legacy_mail_without_correlation_headers(self):
        """Mail written before gh#66 landed. Must parse, with empty ids."""
        mail = parse_mail('From: pm-pogo\nSubject: hello\nDate: 2026-07-09T01:52:11Z\n\nbody here\n')
        self.assertEqual(mail['from'], 'pm-pogo')
        self.assertEqual(mail['subject'], 'hello')
        # splitlines()/join drops the trailing newline — legacy shape, kept.
        self.assertEqual(mail['body'], 'body here')
        self.assertEqual(mail['message_id'], '')
        self.assertEqual(mail['in_reply_to'], '')
        self.assertEqual(mail['references'], [])

    def test_missing_from_and_subject_keep_sentinel(self):
        mail = parse_mail('Date: x\n\nbody')
        self.assertEqual(mail['from'], '?')
        self.assertEqual(mail['subject'], '?')

    def test_message_id_parsed(self):
        raw = 'Message-Id: 1783607775012992000.88130.2000\nFrom: 55f9\nSubject: FYI\n\nb'
        mail = parse_mail(raw)
        self.assertEqual(mail['message_id'], '1783607775012992000.88130.2000')

    def test_reply_headers_parsed(self):
        raw = (
            'Message-Id: id-3\n'
            'From: mayor\n'
            'Subject: Re: thing\n'
            'In-Reply-To: id-2\n'
            'References: id-1 id-2\n'
            '\n'
            'reply body'
        )
        mail = parse_mail(raw)
        self.assertEqual(mail['in_reply_to'], 'id-2')
        self.assertEqual(mail['references'], ['id-1', 'id-2'])

    def test_folded_references_header(self):
        """References caps at 20 ids and may wrap. RFC-5322 folding."""
        raw = (
            'Message-Id: id-9\n'
            'References: id-1 id-2\n'
            '  id-3 id-4\n'
            '\tid-5\n'
            'From: a\n'
            '\n'
            'body'
        )
        mail = parse_mail(raw)
        self.assertEqual(mail['references'], ['id-1', 'id-2', 'id-3', 'id-4', 'id-5'])
        self.assertEqual(mail['from'], 'a')

    def test_body_containing_colon_lines_is_not_eaten(self):
        mail = parse_mail('From: a\nSubject: s\n\nkey: value in body\nmore')
        self.assertEqual(mail['body'], 'key: value in body\nmore')

    def test_empty_body(self):
        self.assertEqual(parse_mail('From: a\nSubject: s\n\n')['body'], '')

    def test_headers_only_no_blank_line(self):
        mail = parse_mail('From: a\nSubject: s')
        self.assertEqual(mail['from'], 'a')
        self.assertEqual(mail['body'], '')


class TestConversationKey(unittest.TestCase):
    def test_root_of_chain_wins(self):
        mail = parse_mail('Message-Id: id-3\nIn-Reply-To: id-2\nReferences: id-1 id-2\n\nb')
        self.assertEqual(conversation_key(mail), 'id-1')

    def test_direct_reply_without_chain_uses_parent(self):
        mail = parse_mail('Message-Id: id-2\nIn-Reply-To: id-1\n\nb')
        self.assertEqual(conversation_key(mail), 'id-1')

    def test_root_message_keys_on_itself(self):
        mail = parse_mail('Message-Id: id-1\nFrom: a\n\nb')
        self.assertEqual(conversation_key(mail), 'id-1')

    def test_headerless_mail_falls_back_to_filename(self):
        """Pre-gh#66 mail. macguffin uses the filename as the id anyway."""
        mail = parse_mail('From: a\nSubject: s\n\nb')
        self.assertEqual(conversation_key(mail, fallback='1783.1.2'), '1783.1.2')

    def test_whole_chain_collapses_to_one_key(self):
        root = parse_mail('Message-Id: id-1\n\nb')
        mid = parse_mail('Message-Id: id-2\nIn-Reply-To: id-1\nReferences: id-1\n\nb')
        leaf = parse_mail('Message-Id: id-3\nIn-Reply-To: id-2\nReferences: id-1 id-2\n\nb')
        keys = {conversation_key(m) for m in (root, mid, leaf)}
        self.assertEqual(keys, {'id-1'})

    def test_reply_target_is_the_message_itself_not_the_root(self):
        leaf = parse_mail('Message-Id: id-3\nIn-Reply-To: id-2\nReferences: id-1 id-2\n\nb')
        self.assertEqual(reply_target(leaf), 'id-3')

    def test_reply_target_falls_back_to_filename(self):
        self.assertEqual(reply_target(parse_mail('From: a\n\nb'), fallback='fn'), 'fn')


class TestThreadTitle(unittest.TestCase):
    def test_strips_re_prefix_so_thread_names_match(self):
        a = thread_title(parse_mail('Subject: design review\n\nb'))
        b = thread_title(parse_mail('Subject: Re: design review\n\nb'))
        self.assertEqual(a, b)

    def test_strips_repeated_re(self):
        self.assertEqual(thread_title(parse_mail('Subject: Re: RE: x\n\nb')), 'x')

    def test_truncates_to_limit(self):
        title = thread_title(parse_mail('Subject: ' + 'x' * 200 + '\n\nb'))
        self.assertLessEqual(len(title), 90)

    def test_missing_subject_names_the_sender(self):
        self.assertEqual(thread_title(parse_mail('From: mayor\n\nb')), 'mail from mayor')


class TestConversationStore(unittest.TestCase):
    def setUp(self):
        self.path = tmpdir('bridget-conv-') / 'conversations.json'

    def test_record_creates_and_persists(self):
        store = ConversationStore(self.path)
        store.record('k1', subject='hello', agent='mayor', message_id='m1')
        self.assertEqual(store.get('k1').agent, 'mayor')
        self.assertTrue(self.path.exists())

    def test_survives_restart(self):
        """The reason the map is on disk at all: Discord threads outlive us."""
        store = ConversationStore(self.path)
        store.record('k1', subject='hello', agent='mayor', message_id='m1')
        store.bind_thread('k1', 999)

        reborn = ConversationStore(self.path)
        conv = reborn.get('k1')
        self.assertEqual(conv.thread_id, 999)
        self.assertEqual(conv.subject, 'hello')
        self.assertEqual(conv.last_message_id, 'm1')
        self.assertEqual(reborn.by_thread(999).key, 'k1')

    def test_by_thread_reverse_lookup(self):
        store = ConversationStore(self.path)
        store.record('k1', agent='mayor')
        store.bind_thread('k1', 42)
        self.assertEqual(store.by_thread(42).agent, 'mayor')
        self.assertIsNone(store.by_thread(43))

    def test_subject_and_agent_are_set_once(self):
        """A later message must not rename the thread or redirect replies."""
        store = ConversationStore(self.path)
        store.record('k1', subject='original', agent='mayor', message_id='m1')
        store.record('k1', subject='Re: original', agent='architect', message_id='m2')
        conv = store.get('k1')
        self.assertEqual(conv.subject, 'original')
        self.assertEqual(conv.agent, 'mayor')

    def test_last_message_id_advances(self):
        """So a reply threads onto the newest message, not the root."""
        store = ConversationStore(self.path)
        store.record('k1', message_id='m1')
        store.record('k1', message_id='m2')
        self.assertEqual(store.get('k1').last_message_id, 'm2')
        self.assertEqual(store.get('k1').message_ids, ['m1', 'm2'])

    def test_duplicate_message_id_not_appended_twice(self):
        store = ConversationStore(self.path)
        store.record('k1', message_id='m1')
        store.record('k1', message_id='m1')
        self.assertEqual(store.get('k1').message_ids, ['m1'])

    def test_rebinding_thread_clears_stale_reverse_index(self):
        store = ConversationStore(self.path)
        store.record('k1')
        store.bind_thread('k1', 1)
        store.bind_thread('k1', 2)
        self.assertIsNone(store.by_thread(1))
        self.assertEqual(store.by_thread(2).key, 'k1')

    def test_bind_thread_on_unknown_key_is_a_noop(self):
        store = ConversationStore(self.path)
        self.assertIsNone(store.bind_thread('nope', 1))

    def test_forget(self):
        store = ConversationStore(self.path)
        store.record('k1')
        store.bind_thread('k1', 7)
        self.assertTrue(store.forget('k1'))
        self.assertIsNone(store.by_thread(7))
        self.assertFalse(store.forget('k1'))

    def test_prune_drops_oldest_first(self):
        ticks = iter(f'2026-01-01T00:00:{i:02d}+00:00' for i in range(60))
        store = ConversationStore(self.path, max_conversations=3, clock=lambda: next(ticks))
        for k in ('a', 'b', 'c', 'd'):
            store.record(k)
        self.assertNotIn('a', store)
        self.assertEqual(set(store.keys()), {'b', 'c', 'd'})

    def test_pruned_thread_leaves_no_reverse_entry(self):
        ticks = iter(f'2026-01-01T00:00:{i:02d}+00:00' for i in range(60))
        store = ConversationStore(self.path, max_conversations=1, clock=lambda: next(ticks))
        store.record('a')
        store.bind_thread('a', 100)
        store.record('b')
        self.assertIsNone(store.by_thread(100))

    def test_malformed_file_yields_empty_store(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('{ not json')
        self.assertEqual(len(ConversationStore(self.path)), 0)

    def test_missing_file_yields_empty_store(self):
        self.assertEqual(len(ConversationStore(self.path)), 0)

    def test_atomic_write_leaves_no_tmp_file(self):
        store = ConversationStore(self.path)
        store.record('k1')
        leftovers = list(self.path.parent.glob('*.tmp'))
        self.assertEqual(leftovers, [])

    def test_on_disk_schema_is_versioned(self):
        store = ConversationStore(self.path)
        store.record('k1', subject='s', agent='mayor', message_id='m1')
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw['version'], 1)
        self.assertIn('k1', raw['conversations'])
        self.assertNotIn('key', raw['conversations']['k1'])


class TestSettingsStore(unittest.TestCase):
    def setUp(self):
        self.path = tmpdir('bridget-settings-') / 'settings.json'

    def test_defaults_preserve_existing_behavior(self):
        s = SettingsStore(self.path)
        self.assertEqual(s.dm_policy, 'all')
        self.assertFalse(s.mute_all)
        self.assertFalse(s.is_muted('anything'))

    def test_env_default_policy_honored(self):
        self.assertEqual(SettingsStore(self.path, default_dm_policy='curated').dm_policy, 'curated')

    def test_bogus_env_default_falls_back_to_all(self):
        self.assertEqual(SettingsStore(self.path, default_dm_policy='wat').dm_policy, 'all')

    def test_mute_and_unmute_one_conversation(self):
        s = SettingsStore(self.path)
        self.assertTrue(s.mute('k1'))
        self.assertFalse(s.mute('k1'))
        self.assertTrue(s.is_muted('k1'))
        self.assertFalse(s.is_muted('k2'))
        self.assertTrue(s.unmute('k1'))
        self.assertFalse(s.unmute('k1'))
        self.assertFalse(s.is_muted('k1'))

    def test_mute_all_covers_every_conversation(self):
        s = SettingsStore(self.path)
        s.set_mute_all(True)
        self.assertTrue(s.is_muted('whatever'))
        self.assertTrue(s.is_muted(''))

    def test_empty_key_is_not_muted_by_conversation_mute(self):
        s = SettingsStore(self.path)
        s.mute('k1')
        self.assertFalse(s.is_muted(''))

    def test_settings_survive_restart(self):
        s = SettingsStore(self.path)
        s.mute('k1')
        s.set_dm_policy('curated')
        reborn = SettingsStore(self.path)
        self.assertEqual(reborn.dm_policy, 'curated')
        self.assertTrue(reborn.is_muted('k1'))

    def test_reject_unknown_dm_policy(self):
        s = SettingsStore(self.path)
        self.assertFalse(s.set_dm_policy('loud'))
        self.assertEqual(s.dm_policy, 'all')

    def test_live_reload_picks_up_external_edit(self):
        """`mute` must take effect on the next watcher poll, no restart."""
        s = SettingsStore(self.path)
        s.save()
        self.path.write_text(json.dumps({
            'version': 1, 'dm_policy': 'none', 'mute_all': True, 'muted': ['k9'],
        }))
        self.assertTrue(s.reload_if_changed())
        self.assertEqual(s.dm_policy, 'none')
        self.assertTrue(s.is_muted('k9'))

    def test_reload_is_a_noop_when_unchanged(self):
        s = SettingsStore(self.path)
        s.save()
        self.assertFalse(s.reload_if_changed())

    def test_malformed_file_yields_defaults(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('nonsense{')
        self.assertEqual(SettingsStore(self.path).dm_policy, 'all')

    def test_unknown_policy_on_disk_is_ignored_not_adopted(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({'dm_policy': 'screaming'}))
        self.assertEqual(SettingsStore(self.path).dm_policy, 'all')

    def test_describe_labels_muted_conversations(self):
        s = SettingsStore(self.path)
        s.mute('k1')
        out = s.describe({'k1': 'design review'})
        self.assertIn('design review', out)

    def test_describe_falls_back_to_key_when_subject_unknown(self):
        s = SettingsStore(self.path)
        s.mute('k1')
        self.assertIn('k1', s.describe())

    def test_describe_reports_the_active_policy(self):
        s = SettingsStore(self.path)
        s.set_dm_policy('curated')
        self.assertIn('curated', s.describe())


class TestMaildirWatcher(unittest.TestCase):
    def setUp(self):
        root = tmpdir('bridget-maildir-')
        self.new = root / 'new'
        self.new.mkdir(parents=True)
        self.cur = root / 'cur'
        self.cur.mkdir(parents=True)
        self.seen_file = root / 'seen'

    def write_mail(self, name: str, body: str = 'hi', msg_id: str = '') -> Path:
        header = f'Message-Id: {msg_id}\n' if msg_id else ''
        p = self.new / name
        p.write_text(f'{header}From: mayor\nSubject: s\n\n{body}\n')
        return p

    def test_fresh_install_is_not_primed(self):
        self.assertFalse(MaildirWatcher(self.new, self.seen_file).primed)

    def test_prime_adopts_backlog_without_delivering(self):
        self.write_mail('1')
        self.write_mail('2')
        w = MaildirWatcher(self.new, self.seen_file)
        self.assertEqual(w.prime(), 2)
        self.assertEqual(w.poll(), [])
        self.assertTrue(w.primed)

    def test_poll_returns_new_mail_once(self):
        w = MaildirWatcher(self.new, self.seen_file)
        w.prime()
        self.write_mail('3', msg_id='id-3')
        got = w.poll()
        self.assertEqual([n for n, _ in got], ['3'])
        self.assertEqual(got[0][1]['message_id'], 'id-3')
        self.assertEqual(w.poll(), [])

    def test_poll_is_ordered_oldest_first(self):
        w = MaildirWatcher(self.new, self.seen_file)
        w.prime()
        for n in ('30', '10', '20'):
            self.write_mail(n)
        self.assertEqual([n for n, _ in w.poll()], ['10', '20', '30'])

    def test_observe_only_never_moves_files(self):
        """The hard invariant: macguffin owns new/. We only read it."""
        w = MaildirWatcher(self.new, self.seen_file)
        w.prime()
        self.write_mail('5')
        w.poll()
        self.assertTrue((self.new / '5').exists(), 'mail was moved out of new/')
        self.assertEqual(list(self.cur.iterdir()), [], 'bridge wrote into cur/')

    def test_seen_set_survives_restart(self):
        w = MaildirWatcher(self.new, self.seen_file)
        w.prime()
        self.write_mail('7')
        w.poll()
        reborn = MaildirWatcher(self.new, self.seen_file)
        self.assertEqual(reborn.poll(), [], 'restart re-delivered the backlog')

    def test_unparseable_mail_is_skipped_not_retried(self):
        w = MaildirWatcher(self.new, self.seen_file)
        w.prime()
        (self.new / 'bad').write_bytes(b'\xff\xfe\x00 invalid utf-8')
        self.assertEqual(w.poll(), [])
        self.assertEqual(w.poll(), [])  # not a hot loop

    def test_dotfiles_ignored(self):
        w = MaildirWatcher(self.new, self.seen_file)
        w.prime()
        (self.new / '.DS_Store').write_text('junk')
        self.assertEqual(w.poll(), [])

    def test_missing_maildir_is_tolerated(self):
        w = MaildirWatcher(self.new.parent / 'nope', self.seen_file)
        self.assertEqual(w.poll(), [])

    def test_unsee_allows_retry_after_failed_send(self):
        w = MaildirWatcher(self.new, self.seen_file)
        w.prime()
        self.write_mail('9')
        self.assertEqual(len(w.poll()), 1)
        w.unsee('9')
        self.assertEqual([n for n, _ in w.poll()], ['9'])

    def test_seen_set_is_bounded_and_keeps_newest(self):
        w = MaildirWatcher(self.new, self.seen_file, max_seen=3)
        w.seen = {'1', '2', '3', '4', '5'}
        w.save_seen()
        self.assertEqual(w.seen, {'3', '4', '5'})

    def test_bounded_seen_set_does_not_redeliver_old_mail(self):
        """Dropped names are older than everything retained, and their files
        are older than every future mail, so they can never reappear as new."""
        w = MaildirWatcher(self.new, self.seen_file, max_seen=2)
        for n in ('1', '2', '3'):
            self.write_mail(n)
        w.prime()
        self.assertEqual(w.seen, {'2', '3'})


#: Verbatim `mg mail send --help` from a build WITHOUT gh#66. Note the long
#: description is absent here; the Flags block is the contract.
HELP_WITHOUT = """Send a message to an agent's mailbox.

Usage:
  mg mail send AGENT [flags]

Flags:
      --body string      message body (required)
      --from string      sender name (required)
  -h, --help             help for send
      --json             emit a single JSON object instead of human-formatted output
      --subject string   message subject (required)
"""

#: Verbatim `mg mail send --help` from a build WITH gh#66.
HELP_WITH = """Send a message to an agent's mailbox.

Every delivered message carries a Message-Id equal to its MSG-ID. Pass
--in-reply-to MSG-ID to mark this message as a reply.

Usage:
  mg mail send AGENT [flags]

Flags:
      --body string          message body (required)
      --from string          sender name (required)
  -h, --help                 help for send
      --in-reply-to string   MSG-ID this message replies to
      --json                 emit a single JSON object instead of human-formatted output
      --subject string       message subject (required)
"""

#: The trap: prose advertises the flag, the Flags block does not define it.
#: An mg build really did ship this way, and passing the flag to it errors.
HELP_PROSE_ONLY = """Send a message to an agent's mailbox.

Pass --in-reply-to MSG-ID to mark this message as a reply: it writes In-Reply-To
and seeds References.

Usage:
  mg mail send AGENT [flags]

Flags:
      --body string      message body (required)
      --from string      sender name (required)
      --subject string   message subject (required)
"""


class TestHelpProbe(unittest.TestCase):
    def test_detects_the_flag_in_the_flags_block(self):
        self.assertTrue(help_advertises_in_reply_to(HELP_WITH))

    def test_absent_flag_detected(self):
        self.assertFalse(help_advertises_in_reply_to(HELP_WITHOUT))

    def test_prose_mention_alone_does_not_count(self):
        """The flag-set is the contract, not the description."""
        self.assertFalse(help_advertises_in_reply_to(HELP_PROSE_ONLY))

    def test_empty_help_is_not_a_capability(self):
        self.assertFalse(help_advertises_in_reply_to(''))


class TestUnknownFlagDetection(unittest.TestCase):
    def test_recognizes_cobra_unknown_flag_error(self):
        self.assertTrue(is_unknown_flag_error('Error: unknown flag: --in-reply-to'))

    def test_other_errors_are_not_unknown_flag(self):
        self.assertFalse(is_unknown_flag_error('Error: no such mailbox'))

    def test_unknown_flag_for_a_different_flag_does_not_match(self):
        self.assertFalse(is_unknown_flag_error('Error: unknown flag: --colour'))

    def test_empty_stderr(self):
        self.assertFalse(is_unknown_flag_error(''))


class TestMgCapabilities(unittest.TestCase):
    def test_auto_probes_help_once(self):
        calls = []

        def probe():
            calls.append(1)
            return HELP_WITH

        caps = MgCapabilities(probe)
        self.assertTrue(caps.correlation_ids)
        self.assertTrue(caps.correlation_ids)
        self.assertEqual(len(calls), 1, 'probe should be cached')

    def test_auto_detects_absence(self):
        self.assertFalse(MgCapabilities(lambda: HELP_WITHOUT).correlation_ids)

    def test_mode_on_skips_the_probe(self):
        def probe():
            raise AssertionError('should not probe when forced on')

        self.assertTrue(MgCapabilities(probe, mode='on').correlation_ids)

    def test_mode_off_skips_the_probe(self):
        def probe():
            raise AssertionError('should not probe when forced off')

        self.assertFalse(MgCapabilities(probe, mode='off').correlation_ids)

    def test_failing_probe_degrades_to_off(self):
        def probe():
            raise OSError('mg not found')

        self.assertFalse(MgCapabilities(probe).correlation_ids)

    def test_downgrade_sticks(self):
        """mg got rebuilt mid-session; the cached probe is now a lie."""
        caps = MgCapabilities(lambda: HELP_WITH)
        self.assertTrue(caps.correlation_ids)
        caps.downgrade()
        self.assertFalse(caps.correlation_ids)

    def test_downgrade_does_not_override_a_forced_on(self):
        caps = MgCapabilities(lambda: HELP_WITHOUT, mode='on')
        caps.downgrade()
        self.assertTrue(caps.correlation_ids)

    def test_bogus_mode_falls_back_to_auto(self):
        self.assertTrue(MgCapabilities(lambda: HELP_WITH, mode='sideways').correlation_ids)

    def test_describe_reports_state_and_how(self):
        self.assertEqual(MgCapabilities(lambda: HELP_WITH).describe(), 'on (detected)')
        self.assertEqual(MgCapabilities(lambda: HELP_WITH, mode='off').describe(), 'off (forced)')


class TestBuildSendArgs(unittest.TestCase):
    def test_basic_args(self):
        args = build_send_args('mayor', 'subj', 'body')
        self.assertEqual(args[:3], ['mail', 'send', 'mayor'])
        self.assertIn('--from=human', args)
        self.assertIn('--subject=subj', args)
        self.assertIn('--body=body', args)

    def test_in_reply_to_appended_only_when_given(self):
        self.assertNotIn('--in-reply-to=', ' '.join(build_send_args('a', 's', 'b')))
        self.assertIn('--in-reply-to=id-1',
                      build_send_args('a', 's', 'b', in_reply_to='id-1'))

    def test_empty_body_becomes_placeholder(self):
        self.assertIn('--body=(no body)', build_send_args('a', 's', ''))

    def test_subject_is_truncated(self):
        args = build_send_args('a', 'x' * 500, 'b')
        subject = next(a for a in args if a.startswith('--subject='))
        self.assertEqual(len(subject) - len('--subject='), 200)

    def test_sender_is_overridable(self):
        self.assertIn('--from=bot', build_send_args('a', 's', 'b', sender='bot'))


class TestAcks(unittest.TestCase):
    def test_delivered_names_the_agent(self):
        ack = delivered('mayor', 'design review')
        self.assertEqual(ack.kind, DELIVERED)
        self.assertTrue(ack.ok)
        self.assertIn('mayor', ack.text)
        self.assertIn('design review', ack.text)

    def test_delivered_flags_threading_when_replying(self):
        self.assertIn('threaded', delivered('mayor', 's', in_reply_to='id-1').text)
        self.assertNotIn('threaded', delivered('mayor', 's').text)

    def test_delivered_truncates_long_subject(self):
        self.assertIn('…', delivered('mayor', 'x' * 100).text)

    def test_ambiguous_with_no_candidates_tells_the_human_what_to_do(self):
        ack = ambiguous([])
        self.assertEqual(ack.kind, AMBIGUOUS)
        self.assertFalse(ack.ok)
        self.assertIn('mail <subject>', ack.text)

    def test_ambiguous_lists_candidates_and_caps_the_list(self):
        cands = [(f'conv {i}', f'k{i}') for i in range(8)]
        ack = ambiguous(cands)
        self.assertIn('**8**', ack.text)
        self.assertIn('and 3 more', ack.text)
        self.assertEqual(len(ack.candidates), 8)

    def test_undeliverable_surfaces_the_reason(self):
        ack = undeliverable('mg exited 1: no such mailbox', agent='ghost')
        self.assertEqual(ack.kind, UNDELIVERABLE)
        self.assertFalse(ack.ok)
        self.assertIn('ghost', ack.text)
        self.assertIn('no such mailbox', ack.text)

    def test_undeliverable_with_empty_reason_still_says_something(self):
        self.assertIn('unknown error', undeliverable('').text)

    def test_undeliverable_truncates_a_huge_stderr_dump(self):
        self.assertLess(len(undeliverable('x' * 5000).text), 400)


if __name__ == '__main__':
    unittest.main(verbosity=2)
