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
import os
import sys
import tempfile
import unittest
import unittest.mock
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
from bridget_core.conversations import MAX_MESSAGE_IDS  # noqa: E402
from bridget_core.mail import (  # noqa: E402
    _is_field_name,
    _split_headers,
    correlation_candidates,
    reply_target,
    thread_title,
)
from bridget_core.mgshim import (  # noqa: E402
    MgCapabilities,
    build_send_args,
    help_advertises_in_reply_to,
    is_unknown_flag_error,
    parse_sent_message_id,
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

    # -- A6: the separator is the first colon, not the first colon-space -----

    def test_an_empty_value_header_does_not_swallow_the_rest(self):
        """RFC 5322 makes the space after the colon optional. Keying on ': '
        turned `Subject:` into a body line and lost every header after it —
        including In-Reply-To, so the mail rooted a fresh thread."""
        raw = ('Message-Id: id-1\nFrom: agent-x\nSubject:\n'
               'In-Reply-To: id-0\n\nreal body')
        mail = parse_mail(raw)
        self.assertEqual(mail['subject'], '')
        self.assertEqual(mail['in_reply_to'], 'id-0')
        self.assertEqual(mail['body'], 'real body')

    def test_a_header_with_no_space_after_the_colon(self):
        mail = parse_mail('Message-Id:id-1\nFrom:agent-x\n\nbody')
        self.assertEqual(mail['message_id'], 'id-1')
        self.assertEqual(mail['from'], 'agent-x')

    def test_crlf_line_endings_parse(self):
        mail = parse_mail('Message-Id: id-1\r\nFrom: a\r\nIn-Reply-To: id-0\r\n\r\nbody')
        self.assertEqual(mail['message_id'], 'id-1')
        self.assertEqual(mail['in_reply_to'], 'id-0')
        self.assertEqual(mail['body'], 'body')

    def test_a_colon_in_a_subject_value_is_not_a_separator(self):
        mail = parse_mail('Subject: Re: build: broken\nFrom: a\n\nbody')
        self.assertEqual(mail['subject'], 'Re: build: broken')
        self.assertEqual(mail['from'], 'a')

    def test_a_headerless_body_line_with_a_colon_starts_the_body(self):
        """`hello: world` has a space in its 'field name', so it is not one."""
        headers, body = _split_headers('From: a\nhello there: world\n\nrest')
        self.assertEqual(set(headers), {'from'})
        self.assertEqual(body, 'hello there: world\n\nrest')

    def test_header_values_are_stripped(self):
        mail = parse_mail('Subject:   padded   \nFrom: a\n\nbody')
        self.assertEqual(mail['subject'], 'padded')

    def test_a_leading_continuation_with_no_header_starts_the_body(self):
        headers, body = _split_headers('  indented first line\nFrom: a\n\nbody')
        self.assertEqual(headers, {})
        self.assertEqual(body, '  indented first line\nFrom: a\n\nbody')

    def test_field_name_predicate(self):
        self.assertTrue(_is_field_name('In-Reply-To'))
        self.assertTrue(_is_field_name('X_Weird!Header'))
        self.assertFalse(_is_field_name(''))
        self.assertFalse(_is_field_name('has space'))
        self.assertFalse(_is_field_name('has:colon'))
        self.assertFalse(_is_field_name('tab\there'))

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


class TestCorrelationCandidates(unittest.TestCase):
    """The ids a message offers up when asking "do you already know me?"."""

    def test_ordered_nearest_ancestor_first(self):
        mail = parse_mail('Message-Id: id-4\nIn-Reply-To: id-3\nReferences: id-1 id-2 id-3\n\nb')
        self.assertEqual(correlation_candidates(mail),
                         ['id-3', 'id-2', 'id-1', 'id-4'])

    def test_deduplicated_preserving_first_position(self):
        mail = parse_mail('Message-Id: id-2\nIn-Reply-To: id-1\nReferences: id-1\n\nb')
        self.assertEqual(correlation_candidates(mail), ['id-1', 'id-2'])

    def test_a_reply_seeded_by_mg_offers_its_parent(self):
        """What `mg mail send --in-reply-to` actually writes past the first hop:
        References is the parent alone, and the root appears nowhere."""
        mail = parse_mail('Message-Id: id-9\nIn-Reply-To: id-8\nReferences: id-8\n\nb')
        self.assertEqual(correlation_candidates(mail), ['id-8', 'id-9'])

    def test_own_id_is_offered_so_a_redelivery_rejoins_its_conversation(self):
        mail = parse_mail('Message-Id: id-1\n\nb')
        self.assertEqual(correlation_candidates(mail), ['id-1'])

    def test_headerless_mail_offers_only_the_filename(self):
        self.assertEqual(correlation_candidates(parse_mail('From: a\n\nb'), fallback='fn'),
                         ['fn'])

    def test_no_ids_at_all_offers_nothing(self):
        self.assertEqual(correlation_candidates(parse_mail('From: a\n\nb')), [])


class TestThreadTitle(unittest.TestCase):
    def test_strips_re_prefix_so_thread_names_match(self):
        a = thread_title(parse_mail('Subject: design review\n\nb'))
        b = thread_title(parse_mail('Subject: Re: design review\n\nb'))
        self.assertEqual(a, b)

    def test_strips_repeated_re(self):
        self.assertEqual(thread_title(parse_mail('Subject: Re: RE: x\n\nb')), 'x')

    def test_subject_of_only_re_falls_back_to_the_sender(self):
        """Otherwise the thread is named 'Re:', which tells the human nothing."""
        self.assertEqual(thread_title(parse_mail('From: mayor\nSubject: Re:\n\nb')),
                         'mail from mayor')
        self.assertEqual(thread_title(parse_mail('From: mayor\nSubject: Re: \n\nb')),
                         'mail from mayor')

    def test_does_not_eat_a_subject_that_merely_starts_with_re(self):
        self.assertEqual(thread_title(parse_mail('Subject: rescue the build\n\nb')),
                         'rescue the build')

    def test_truncates_to_the_limit_the_caller_asks_for(self):
        title = thread_title(parse_mail('Subject: ' + 'x' * 200 + '\n\nb'), limit=90)
        self.assertLessEqual(len(title), 90)
        self.assertTrue(title.endswith('…'))

    def test_no_limit_means_no_truncation(self):
        """Discord's 100-char thread-name cap is the adapter's, not the core's."""
        title = thread_title(parse_mail('Subject: ' + 'x' * 200 + '\n\nb'))
        self.assertEqual(len(title), 200)

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
        self.assertEqual(raw['version'], 2)
        self.assertIn('k1', raw['conversations'])
        self.assertNotIn('key', raw['conversations']['k1'])


class TestStoreDefensiveBranches(unittest.TestCase):
    """A16. The `except`/`isinstance` arms that only a corrupt file reaches.
    They exist so a hand-edited state file degrades instead of crashing the
    bridge; untested, that is an aspiration rather than a property."""

    def setUp(self):
        self.dir = tmpdir('bridget-defensive-')
        self.path = self.dir / 'conversations.json'

    def test_a_non_dict_conversation_entry_is_skipped_not_fatal(self):
        self.path.write_text(json.dumps({
            'version': 2,
            'conversations': {'good': {'subject': 's'}, 'bad': 'not-an-object', 'worse': [1]},
        }))
        store = ConversationStore(self.path)
        self.assertEqual(set(store.keys()), {'good'})

    def test_a_conversations_value_that_is_not_an_object_yields_an_empty_store(self):
        self.path.write_text(json.dumps({'version': 2, 'conversations': ['nope']}))
        self.assertEqual(len(ConversationStore(self.path)), 0)

    def test_a_top_level_json_array_yields_an_empty_store(self):
        self.path.write_text('[]')
        self.assertEqual(len(ConversationStore(self.path)), 0)

    def test_a_store_write_leaves_no_temp_file(self):
        store = ConversationStore(self.path)
        store.record('k1', subject='s', agent='a', message_id='m1')
        self.assertEqual([p.name for p in self.dir.iterdir()], ['conversations.json'])

    def test_a_non_list_muted_yields_no_mutes(self):
        p = self.dir / 'settings.json'
        p.write_text(json.dumps({'version': 1, 'muted': {'k1': True}, 'dm_policy': 'all'}))
        self.assertEqual(SettingsStore(p).muted, set())

    def test_an_unreadable_seen_file_yields_an_empty_set(self):
        """mailbox._load_seen's OSError arm: a directory where a file belongs,
        an EACCES, an EIO. The bridge must still start."""
        seen = self.dir / 'seen-as-a-directory'
        seen.mkdir()
        (self.dir / 'new').mkdir()
        w = MaildirWatcher(self.dir / 'new', seen)
        self.assertEqual(w.seen, set())


class TestPostedGuard(unittest.TestCase):
    """A1. Delivery is at-least-once, so a mail can arrive at the adapter
    twice. `posted_ids` is what stops the second arrival duplicating the
    thread post."""

    def setUp(self):
        self.path = tmpdir('bridget-posted-') / 'conversations.json'
        self.store = ConversationStore(self.path)
        self.store.record('k1', subject='s', agent='mayor', message_id='m1')

    def test_a_recorded_message_is_not_yet_posted(self):
        """Recording folds an id into the conversation index. It says nothing
        about whether the adapter managed to render it."""
        self.assertFalse(self.store.was_posted('k1', 'm1'))

    def test_mark_posted_then_was_posted(self):
        self.store.mark_posted('k1', 'm1')
        self.assertTrue(self.store.was_posted('k1', 'm1'))

    def test_posted_state_survives_restart(self):
        self.store.mark_posted('k1', 'm1')
        self.assertTrue(ConversationStore(self.path).was_posted('k1', 'm1'))

    def test_a_v1_file_loads_with_no_posted_ids(self):
        self.path.write_text(json.dumps({
            'version': 1,
            'conversations': {'k9': {'thread_id': 5, 'subject': 's', 'agent': 'a',
                                     'last_message_id': 'm9', 'message_ids': ['m9'],
                                     'updated_at': ''}},
        }))
        store = ConversationStore(self.path)
        self.assertEqual(store.get('k9').posted_ids, [])
        self.assertFalse(store.was_posted('k9', 'm9'))

    def test_mark_posted_is_idempotent(self):
        self.store.mark_posted('k1', 'm1')
        self.store.mark_posted('k1', 'm1')
        self.assertEqual(self.store.get('k1').posted_ids, ['m1'])

    def test_mark_posted_on_an_unknown_conversation_is_a_noop(self):
        self.store.mark_posted('nope', 'm1')
        self.assertFalse(self.store.was_posted('nope', 'm1'))

    def test_an_empty_message_id_is_never_posted(self):
        self.store.mark_posted('k1', '')
        self.assertFalse(self.store.was_posted('k1', ''))

    def test_posted_ids_are_bounded(self):
        for i in range(MAX_MESSAGE_IDS + 10):
            self.store.mark_posted('k1', f'p{i}')
        conv = self.store.get('k1')
        self.assertEqual(len(conv.posted_ids), MAX_MESSAGE_IDS)
        self.assertEqual(conv.posted_ids[-1], f'p{MAX_MESSAGE_IDS + 9}')


class TestConversationStoreResolve(unittest.TestCase):
    """The message-id index: what keeps a thread alive past its first
    round-trip, once `References` stops naming the root."""

    def setUp(self):
        self.path = tmpdir('bridget-resolve-') / 'conversations.json'
        self.store = ConversationStore(self.path)

    def test_resolves_a_recorded_message_to_its_conversation(self):
        self.store.record('k1', message_id='m1')
        self.assertEqual(self.store.resolve(['m1']), 'k1')

    def test_resolves_the_key_itself(self):
        self.store.record('k1')
        self.assertEqual(self.store.resolve(['k1']), 'k1')

    def test_first_known_candidate_wins(self):
        self.store.record('k1', message_id='m1')
        self.assertEqual(self.store.resolve(['unknown', 'm1', 'k1']), 'k1')

    def test_unknown_ids_resolve_to_nothing(self):
        self.store.record('k1', message_id='m1')
        self.assertIsNone(self.store.resolve(['m9', 'm8']))
        self.assertIsNone(self.store.resolve([]))

    def test_an_outbound_reply_keeps_the_chain_resolvable(self):
        """The bug, in miniature. The agent's next reply names `out-1`, an id
        the store only knows because it recorded the reply it sent."""
        self.store.record('k1', agent='mayor', message_id='in-1')
        self.store.record('k1', message_id='out-1')
        self.assertEqual(self.store.resolve(['out-1']), 'k1')

    def test_index_survives_a_restart(self):
        self.store.record('k1', message_id='m1')
        self.assertEqual(ConversationStore(self.path).resolve(['m1']), 'k1')

    def test_forget_drops_the_ids(self):
        self.store.record('k1', message_id='m1')
        self.store.forget('k1')
        self.assertIsNone(self.store.resolve(['m1', 'k1']))

    def test_prune_drops_the_ids(self):
        ticks = iter(f'2026-01-01T00:00:{i:02d}+00:00' for i in range(60))
        store = ConversationStore(self.path, max_conversations=1, clock=lambda: next(ticks))
        store.record('a', message_id='m-a')
        store.record('b', message_id='m-b')
        self.assertIsNone(store.resolve(['m-a', 'a']))
        self.assertEqual(store.resolve(['m-b']), 'b')

    def test_trimmed_message_ids_leave_no_index_entry(self):
        """`message_ids` is capped; the index must be capped with it, or it is
        the unbounded map the cap exists to prevent."""
        from bridget_core.conversations import MAX_MESSAGE_IDS
        for i in range(MAX_MESSAGE_IDS + 5):
            self.store.record('k1', message_id=f'm{i}')
        self.assertIsNone(self.store.resolve(['m0']), 'a trimmed id still indexed')
        self.assertEqual(self.store.resolve([f'm{MAX_MESSAGE_IDS + 4}']), 'k1')
        self.assertEqual(self.store.resolve(['k1']), 'k1',
                         'the key itself must never be trimmed away')

    def test_an_id_reclaimed_by_another_conversation_is_not_stolen_back(self):
        self.store.record('k1', message_id='m1')
        self.store.record('k2', message_id='m1')
        self.store.forget('k1')
        self.assertEqual(self.store.resolve(['m1']), 'k2')


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

    def test_reload_detects_an_edit_within_the_same_clock_second(self):
        """On a coarse-mtime filesystem a same-second hand edit was invisible,
        so the operator's `mute` appeared to do nothing. Compare (mtime_ns, size)."""
        s = SettingsStore(self.path)
        s.save()
        stat_before = self.path.stat()
        # Simulate a coarse filesystem: same whole-second mtime, different content.
        self.path.write_text(json.dumps({'version': 1, 'dm_policy': 'none',
                                         'mute_all': True, 'muted': ['k9']}))
        os.utime(self.path, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns + 1))
        self.assertTrue(s.reload_if_changed())
        self.assertTrue(s.is_muted('k9'))

    def test_reload_detects_a_size_change_at_identical_mtime(self):
        s = SettingsStore(self.path)
        s.save()
        st = self.path.stat()
        self.path.write_text(json.dumps({'version': 1, 'muted': ['k1', 'k2', 'k3'],
                                         'dm_policy': 'all', 'mute_all': False}))
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns))  # identical mtime
        self.assertTrue(s.reload_if_changed(), 'size change went unnoticed')
        self.assertTrue(s.is_muted('k2'))

    def test_reload_survives_a_deleted_settings_file(self):
        s = SettingsStore(self.path)
        s.save()
        self.path.unlink()
        self.assertTrue(s.reload_if_changed())
        self.assertEqual(s.dm_policy, 'all')

    def test_malformed_file_yields_defaults(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('nonsense{')
        self.assertEqual(SettingsStore(self.path).dm_policy, 'all')

    def test_unknown_policy_on_disk_is_ignored_not_adopted(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({'dm_policy': 'screaming'}))
        self.assertEqual(SettingsStore(self.path).dm_policy, 'all')

    def test_summary_labels_muted_conversations(self):
        s = SettingsStore(self.path)
        s.mute('k1')
        self.assertEqual(s.summary({'k1': 'design review'})['muted'],
                         [('k1', 'design review')])

    def test_summary_falls_back_to_key_when_subject_unknown(self):
        s = SettingsStore(self.path)
        s.mute('k1')
        self.assertEqual(s.summary()['muted'], [('k1', 'k1')])

    def test_summary_reports_the_active_policy(self):
        s = SettingsStore(self.path)
        s.set_dm_policy('curated')
        self.assertEqual(s.summary()['dm_policy'], 'curated')

    def test_summary_does_not_cap_the_muted_list(self):
        """How many to show is the adapter's decision."""
        s = SettingsStore(self.path)
        for i in range(15):
            s.mute(f'k{i}')
        self.assertEqual(len(s.summary()['muted']), 15)

    def test_summary_sorts_muted_keys(self):
        s = SettingsStore(self.path)
        s.mute('kb')
        s.mute('ka')
        self.assertEqual([k for k, _ in s.summary()['muted']], ['ka', 'kb'])


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

    def test_poll_returns_new_mail_once_it_is_committed(self):
        w = MaildirWatcher(self.new, self.seen_file)
        w.prime()
        self.write_mail('3', msg_id='id-3')
        got = w.poll()
        self.assertEqual([n for n, _ in got], ['3'])
        self.assertEqual(got[0][1]['message_id'], 'id-3')
        w.commit('3')
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
        w.commit('7')
        reborn = MaildirWatcher(self.new, self.seen_file)
        self.assertEqual(reborn.poll(), [], 'restart re-delivered the backlog')

    # -- A7: the at-most-once crash window ---------------------------------
    #
    # `unsee()` covers a graceful send failure. It structurally cannot cover a
    # SIGKILL between "seen-set hits the disk" and "mail hits the chat" — a
    # killed process does not get to run `unsee()`. These pin the ordering that
    # closes that window: nothing is persisted until the caller commits.

    def test_poll_does_not_persist_the_seen_set(self):
        w = MaildirWatcher(self.new, self.seen_file)
        w.prime()
        self.write_mail('9')
        w.poll()
        self.assertNotIn('9', self.seen_file.read_text().split())

    def test_a_crash_between_poll_and_delivery_redelivers(self):
        """The whole point of A7. Before the fix this mail was seen forever."""
        w = MaildirWatcher(self.new, self.seen_file)
        w.prime()
        self.write_mail('9', msg_id='id-9')
        self.assertEqual([n for n, _ in w.poll()], ['9'])
        # SIGKILL here: no commit, no unsee, no graceful anything.
        reborn = MaildirWatcher(self.new, self.seen_file)
        self.assertEqual([n for n, _ in reborn.poll()], ['9'],
                         'a mail polled but never delivered was lost')

    def test_committing_one_mail_does_not_commit_the_rest_of_the_batch(self):
        """A batch is committed per-message. Persisting the whole batch when the
        first one lands would strand the tail on a crash mid-batch."""
        w = MaildirWatcher(self.new, self.seen_file)
        w.prime()
        for n in ('1', '2', '3'):
            self.write_mail(n)
        self.assertEqual([n for n, _ in w.poll()], ['1', '2', '3'])
        w.commit('1')
        reborn = MaildirWatcher(self.new, self.seen_file)
        self.assertEqual([n for n, _ in reborn.poll()], ['2', '3'])

    def test_an_unparseable_mail_is_persisted_immediately(self):
        """Nothing will ever commit it — it is not deliverable. Without an
        eager persist it would be re-read and re-logged on every poll forever."""
        w = MaildirWatcher(self.new, self.seen_file)
        w.prime()
        (self.new / 'bad').write_bytes(b'\xff\xfe\x00 invalid utf-8')
        w.poll()
        self.assertIn('bad', self.seen_file.read_text().split())

    def test_commit_is_idempotent(self):
        w = MaildirWatcher(self.new, self.seen_file)
        w.prime()
        self.write_mail('4')
        w.poll()
        w.commit('4')
        w.commit('4')
        self.assertEqual(self.seen_file.read_text().split().count('4'), 1)

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

    def test_unsee_allows_retry_after_a_committed_mail(self):
        """Since poll() no longer marks a message seen, unsee() is only
        load-bearing for a mail already committed — one the caller has decided
        to redeliver after the fact."""
        w = MaildirWatcher(self.new, self.seen_file)
        w.prime()
        self.write_mail('9')
        self.assertEqual(len(w.poll()), 1)
        w.commit('9')
        self.assertEqual(w.poll(), [])
        w.unsee('9')
        self.assertEqual([n for n, _ in w.poll()], ['9'])
        self.assertNotIn('9', self.seen_file.read_text().split())

    def test_unsee_of_an_uncommitted_mail_is_a_noop(self):
        w = MaildirWatcher(self.new, self.seen_file)
        w.prime()
        self.write_mail('9')
        w.poll()
        w.unsee('9')          # the ordinary failed-send path; nothing to undo
        self.assertEqual([n for n, _ in w.poll()], ['9'])

    def test_seen_set_never_forgets_mail_still_in_new(self):
        """The bug this replaced: trimming the seen-set by age re-surfaced old
        mail as new on the next poll — forever, because observe-only means the
        file never leaves new/."""
        for n in ('1', '2', '3'):
            self.write_mail(n)
        w = MaildirWatcher(self.new, self.seen_file, gc_threshold=2)
        w.prime()
        self.assertEqual(w.seen, {'1', '2', '3'}, 'a still-present mail was forgotten')
        self.assertEqual(w.poll(), [], 'old mail was re-delivered')
        self.assertEqual(w.poll(), [], 'and re-delivered again')

    def test_seen_set_collects_names_whose_mail_left_new(self):
        """Once `mg mail read` moves a message to cur/, its name is dead weight."""
        for n in ('1', '2', '3'):
            self.write_mail(n)
        w = MaildirWatcher(self.new, self.seen_file, gc_threshold=2)
        w.prime()
        (self.new / '1').rename(self.cur / '1')
        (self.new / '2').rename(self.cur / '2')
        w.save_seen()
        self.assertEqual(w.seen, {'3'}, 'dead names were not collected')

    def test_seen_set_may_exceed_the_threshold_when_new_is_large(self):
        """Correctness beats the bound: if new/ holds more unread than the
        threshold, every name must still be remembered."""
        for n in range(5):
            self.write_mail(f'{n}')
        w = MaildirWatcher(self.new, self.seen_file, gc_threshold=2)
        w.prime()
        self.assertEqual(len(w.seen), 5)
        self.assertEqual(w.poll(), [])

    def test_unreadable_mail_is_skipped_not_retried_forever(self):
        """A non-vanish OSError (EACCES/EIO) left the file in new/ and unseen,
        so every poll re-read and re-logged it. It is retried a bounded number
        of times — the fault may be transient — and then given up on."""
        from bridget_core.mailbox import MAX_READ_ATTEMPTS
        w = MaildirWatcher(self.new, self.seen_file)
        w.prime()
        p = self.write_mail('9')
        p.chmod(0o000)
        try:
            for attempt in range(MAX_READ_ATTEMPTS):
                self.assertEqual(w.poll(), [])
                if attempt < MAX_READ_ATTEMPTS - 1:
                    self.assertNotIn('9', w.seen, 'gave up on the first bad read')
            self.assertEqual(w.poll(), [], 'unreadable mail was retried in a hot loop')
        finally:
            p.chmod(0o644)
        self.assertIn('9', w.seen)

    def test_a_transient_read_error_does_not_lose_the_mail(self):
        """The invariant is "never drop mail". One bad read on a network
        filesystem must not mark a healthy mail seen forever."""
        w = MaildirWatcher(self.new, self.seen_file)
        w.prime()
        p = self.write_mail('9', msg_id='id-9')
        p.chmod(0o000)
        try:
            self.assertEqual(w.poll(), [])
        finally:
            p.chmod(0o644)
        self.assertNotIn('9', w.seen)
        got = w.poll()
        self.assertEqual([n for n, _ in got], ['9'], 'a healthy mail was lost to one bad read')
        self.assertEqual(got[0][1]['message_id'], 'id-9')

    def test_the_failure_count_resets_after_a_good_read(self):
        from bridget_core.mailbox import MAX_READ_ATTEMPTS
        w = MaildirWatcher(self.new, self.seen_file)
        w.prime()
        p = self.write_mail('9')
        for _ in range(MAX_READ_ATTEMPTS - 1):
            p.chmod(0o000)
            self.assertEqual(w.poll(), [])
            p.chmod(0o644)
            self.assertEqual(len(w.poll()), 1, 'a good read must clear the failure count')

    def test_vanished_mail_is_not_marked_seen(self):
        """macguffin moved it to cur/ between listing and reading. It is gone
        from new/, will never be listed again, and needs no seen entry."""
        w = MaildirWatcher(self.new, self.seen_file)
        w.prime()
        self.write_mail('8')
        real_read = Path.read_text

        def vanish(self_path, *a, **k):
            if self_path.name == '8':
                raise FileNotFoundError(2, 'gone')
            return real_read(self_path, *a, **k)

        with unittest.mock.patch.object(Path, 'read_text', vanish):
            self.assertEqual(w.poll(), [])
        self.assertNotIn('8', w.seen)


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

    def test_detects_a_flag_printed_with_a_shorthand_alias(self):
        """cobra prints `  -r, --flag string` when a shorthand is registered."""
        help_text = (
            'Usage:\n  mg mail send AGENT [flags]\n\n'
            'Flags:\n'
            '      --body string          body\n'
            '  -r, --in-reply-to string   MSG-ID this replies to\n'
        )
        self.assertTrue(help_advertises_in_reply_to(help_text))

    def test_a_similarly_named_flag_does_not_match(self):
        help_text = 'Flags:\n      --in-reply-to-file string   path\n'
        self.assertFalse(help_advertises_in_reply_to(help_text))


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

    def test_json_requested_only_when_the_caller_wants_the_id(self):
        self.assertNotIn('--json', build_send_args('a', 's', 'b'))
        self.assertIn('--json', build_send_args('a', 's', 'b', want_msg_id=True))


class TestParseSentMessageId(unittest.TestCase):
    """The bridge must learn the id of the reply it just sent — the agent's
    answer will name that id and no other."""

    def test_reads_msg_id_from_json(self):
        out = ('{"msg_id":"1783.88.9","from":"human","to":"mayor",'
               '"mailbox_created":false,"in_reply_to":""}')
        self.assertEqual(parse_sent_message_id(out), '1783.88.9')

    def test_falls_back_to_the_delivered_line(self):
        """Older mg, or a caller that didn't ask for --json. The maildir
        basename *is* the message id."""
        out = 'Delivered: human → mayor/new/1783.88.9  (new mailbox created)'
        self.assertEqual(parse_sent_message_id(out), '1783.88.9')

    def test_unparseable_output_yields_no_id_rather_than_raising(self):
        """One hop of lost threading beats telling the human that a reply which
        plainly went out was undeliverable."""
        for out in ('', '   ', 'surprise!', '{"other":"shape"}', '[1,2]', '{'):
            self.assertEqual(parse_sent_message_id(out), '', repr(out))


class TestAcks(unittest.TestCase):
    """An Ack is data: `kind` plus the facts behind it. What it *looks like* is
    the adapter's business — see TestRenderAck in test_threading.py."""

    def test_delivered_carries_the_agent_and_subject(self):
        ack = delivered('mayor', 'design review')
        self.assertEqual(ack.kind, DELIVERED)
        self.assertTrue(ack.ok)
        self.assertEqual(ack.agent, 'mayor')
        self.assertEqual(ack.subject, 'design review')
        self.assertEqual(ack.in_reply_to, '')

    def test_delivered_records_what_it_threads_onto(self):
        self.assertEqual(delivered('mayor', 's', in_reply_to='id-1').in_reply_to, 'id-1')

    def test_delivered_does_not_truncate_the_subject(self):
        """A character budget is Discord's, not the core's."""
        self.assertEqual(delivered('mayor', 'x' * 100).subject, 'x' * 100)

    def test_ambiguous_with_no_candidates(self):
        ack = ambiguous([])
        self.assertEqual(ack.kind, AMBIGUOUS)
        self.assertFalse(ack.ok)
        self.assertEqual(ack.candidates, [])

    def test_ambiguous_keeps_every_candidate(self):
        cands = [(f'conv {i}', f'k{i}') for i in range(8)]
        ack = ambiguous(cands)
        self.assertEqual(len(ack.candidates), 8, 'the core must not cap the list')

    def test_ambiguous_carries_a_hint(self):
        self.assertEqual(ambiguous([], hint='try harder').hint, 'try harder')

    def test_undeliverable_carries_the_reason_verbatim(self):
        ack = undeliverable('  mg exited 1: no such mailbox  ', agent='ghost')
        self.assertEqual(ack.kind, UNDELIVERABLE)
        self.assertFalse(ack.ok)
        self.assertEqual(ack.agent, 'ghost')
        self.assertEqual(ack.reason, 'mg exited 1: no such mailbox')

    def test_undeliverable_does_not_truncate_a_huge_stderr_dump(self):
        self.assertEqual(len(undeliverable('x' * 5000).reason), 5000)


class TestCoreCarriesNoPresentation(unittest.TestCase):
    """A5. `bridget_core` is the half of the bridge that would be identical
    under Slack. Discord's markdown (`**bold**`) and emoji are the adapter's.

    This is a tripwire, not a proof — it greps. It exists because the drift it
    catches is the easy kind: someone adds one `f'✅ {agent}'` to a core module
    because that is where the data already is."""

    PRESENTATION = ('**', '✅', '⚠️', '❌', '📬', '⚙️', '🔕', '🔔', '💬', '•', '↳')

    def _core_modules(self):
        return sorted((REPO / 'bridget_core').glob('*.py'))

    @staticmethod
    def _code_only(src: str) -> str:
        """The module with its docstrings and comments removed. Prose is allowed
        to *name* `**bold**`; code is not allowed to emit it."""
        import ast
        tree = ast.parse(src)
        for node in ast.walk(tree):
            body = getattr(node, 'body', None)
            if not isinstance(body, list) or not body:
                continue
            first = body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                body.pop(0)
        return ast.unparse(tree)   # ast.unparse drops comments for free

    def test_no_core_module_renders_discord_markup(self):
        offenders = []
        for path in self._core_modules():
            code = self._code_only(path.read_text())
            offenders.extend(f'{path.name}: {t!r}' for t in self.PRESENTATION if t in code)
        self.assertEqual(offenders, [], f'presentation leaked into the core: {offenders}')

    def test_the_tripwire_would_actually_fire(self):
        """Guard the guard: a stripped module must still contain its code."""
        code = self._code_only("'''docstring with ** bold **'''\nX = '✅ hi'\n")
        self.assertNotIn('docstring', code)
        self.assertIn('✅', code)

    def test_no_core_module_imports_a_chat_library(self):
        for path in self._core_modules():
            self.assertNotIn('import discord', path.read_text())

    def test_thread_title_has_no_built_in_limit(self):
        """Discord caps a thread name at 100. The core does not know that."""
        long = thread_title(parse_mail('Subject: ' + 'x' * 500 + '\n\nb'))
        self.assertEqual(len(long), 500)
        self.assertEqual(len(thread_title(parse_mail('Subject: ' + 'x' * 500 + '\n\nb'),
                                          limit=90)), 90)


if __name__ == '__main__':
    unittest.main(verbosity=2)
