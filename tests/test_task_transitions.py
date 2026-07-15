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

"""Tests for reconcile_task_states — the task-transition diff.

The bug this exists to prevent (mg-0655): `mg list --json --all` emits some ids
twice, once live and once archived. A line-by-line diff announced the first
record and stored the second, so every poll re-announced the same transition.
Daniel's DM channel received the same three "shelved" messages every 5 seconds.

Stubs `discord` so this runs under system python3 (no venv-bridget required).
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / 'bridget'


def load_bridget():
    """Import bridget into a fresh namespace with a clean fake HOME."""
    fake_home = Path(tempfile.mkdtemp(prefix='bridget-transitions-test-'))
    env_dir = fake_home / '.pogo'
    env_dir.mkdir(parents=True)
    (env_dir / 'bridget.env').write_text(
        'DISCORD_BOT_TOKEN=fake\n'
        'DISCORD_USER_ID=1\n'
        'DISCORD_SERVER_ID=2\n'
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


def dump(*tasks: dict) -> str:
    """Render task records the way `mg list --json --all` does: one per line."""
    return '\n'.join(json.dumps(t) for t in tasks) + '\n'


def task(tid: str, status: str, **kw) -> dict:
    return {'id': tid, 'type': 'task', 'status': status,
            'title': f'title of {tid}', **kw}


ANNOUNCE = frozenset({'claimed', 'done', 'shelved'})


class ReconcileTaskStatesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridget = load_bridget()

    def reconcile(self, out, previous):
        return self.bridget.reconcile_task_states(out, previous, ANNOUNCE)

    # --- the regression this file exists for ---------------------------------

    def test_duplicate_id_with_conflicting_status_never_announces(self):
        """An id emitted as both `shelved` and `archived` is ambiguous."""
        out = dump(task('mg-4b2a', 'shelved'), task('mg-4b2a', 'archived'))
        current, announce = self.reconcile(out, {})
        self.assertEqual(announce, [], 'ambiguous duplicate must not announce')
        self.assertEqual(current, {'mg-4b2a': 'archived'}, 'last record wins')

    def test_duplicate_id_is_stable_across_repeated_polls(self):
        """The flood: poll the same duplicate listing repeatedly, announce never."""
        out = dump(task('mg-4b2a', 'shelved'), task('mg-4b2a', 'archived'),
                   task('mg-7387', 'shelved'), task('mg-7387', 'archived'))
        states = {}
        for cycle in range(5):
            states, announce = self.reconcile(out, states)
            self.assertEqual(announce, [], f'cycle {cycle} re-announced')

    def test_duplicate_order_does_not_change_the_announcement(self):
        """Rule 2 makes the decision independent of mg's emission order."""
        forward = dump(task('mg-1', 'shelved'), task('mg-1', 'archived'))
        reverse = dump(task('mg-1', 'archived'), task('mg-1', 'shelved'))
        _, a1 = self.reconcile(forward, {})
        _, a2 = self.reconcile(reverse, {})
        self.assertEqual((a1, a2), ([], []))

    # --- the behaviour the fix must not break --------------------------------

    def test_genuine_transition_announces_once(self):
        out = dump(task('mg-aaaa', 'available'))
        states, announce = self.reconcile(out, {})
        self.assertEqual(announce, [])

        out2 = dump(task('mg-aaaa', 'claimed'))
        states, announce = self.reconcile(out2, states)
        self.assertEqual([t['id'] for t in announce], ['mg-aaaa'])

        # Polling again with no change must be silent.
        _, announce = self.reconcile(out2, states)
        self.assertEqual(announce, [])

    def test_unannounced_status_is_recorded_but_silent(self):
        out = dump(task('mg-bbbb', 'archived'))
        current, announce = self.reconcile(out, {})
        self.assertEqual(announce, [])
        self.assertEqual(current, {'mg-bbbb': 'archived'})

    def test_disappeared_ids_are_dropped_from_state(self):
        current, _ = self.reconcile(dump(task('mg-gone', 'done')),
                                    {'mg-old': 'claimed'})
        self.assertNotIn('mg-old', current)

    def test_non_task_types_and_junk_lines_are_ignored(self):
        out = (dump({'id': 'mg-idea', 'type': 'idea', 'status': 'claimed'})
               + 'not json at all\n'
               + '\n'
               + dump({'type': 'task', 'status': 'done'})        # no id
               + dump({'id': 'mg-nostatus', 'type': 'task'}))    # no status
        current, announce = self.reconcile(out, {})
        self.assertEqual(current, {})
        self.assertEqual(announce, [])

    def test_announce_carries_the_whole_record_for_formatting(self):
        out = dump(task('mg-cccc', 'done', assignee='mayor'))
        _, announce = self.reconcile(out, {'mg-cccc': 'claimed'})
        self.assertEqual(len(announce), 1)
        self.assertEqual(announce[0]['assignee'], 'mayor')
        self.assertEqual(announce[0]['title'], 'title of mg-cccc')

    def test_agreeing_duplicates_still_announce(self):
        """Two identical records are not a conflict."""
        out = dump(task('mg-dddd', 'done'), task('mg-dddd', 'done'))
        _, announce = self.reconcile(out, {})
        self.assertEqual([t['id'] for t in announce], ['mg-dddd'])


class SplitPollAnnounceStatusesTest(unittest.TestCase):
    """mg-4fc0 split the one --all poll into a cheap non---all hot poll plus a
    throttled --all full diff. Each owns a partition of the announce statuses,
    keyed on whether `mg list` shows the status without --all. This guards the
    partition and the routing consequence: the hot poll must never announce a
    status it cannot see, and the full diff must not double-announce one the hot
    poll already owns."""

    @classmethod
    def setUpClass(cls):
        cls.bridget = load_bridget()

    def test_partition_is_a_clean_cover_of_every_formatted_status(self):
        b = self.bridget
        formats = frozenset(b._TRANSITION_FORMATS)
        # Together they cover every announceable status...
        self.assertEqual(b.HOT_TRANSITION_STATUSES | b.FULL_TRANSITION_STATUSES,
                         formats)
        # ...and they never overlap, so no transition is announced twice.
        self.assertEqual(b.HOT_TRANSITION_STATUSES & b.FULL_TRANSITION_STATUSES,
                         frozenset())

    def test_hot_owns_the_statuses_mg_shows_without_all(self):
        # claimed and done are visible in a plain `mg list`; shelved is not.
        self.assertIn('claimed', self.bridget.HOT_TRANSITION_STATUSES)
        self.assertIn('done', self.bridget.HOT_TRANSITION_STATUSES)
        self.assertNotIn('shelved', self.bridget.HOT_TRANSITION_STATUSES)

    def test_full_owns_only_the_hidden_statuses(self):
        # shelved is only visible with --all; done must stay hot-only.
        self.assertIn('shelved', self.bridget.FULL_TRANSITION_STATUSES)
        self.assertNotIn('done', self.bridget.FULL_TRANSITION_STATUSES)
        self.assertNotIn('claimed', self.bridget.FULL_TRANSITION_STATUSES)

    def test_hot_poll_announces_claimed_and_done(self):
        b = self.bridget
        # available -> claimed, diffed with the hot partition, announces.
        _, a = b.reconcile_task_states(
            dump(task('mg-h1', 'claimed')), {'mg-h1': 'available'},
            b.HOT_TRANSITION_STATUSES)
        self.assertEqual([t['id'] for t in a], ['mg-h1'])
        # claimed -> done also announces on the hot poll (mg lists done by default).
        _, a = b.reconcile_task_states(
            dump(task('mg-h2', 'done')), {'mg-h2': 'claimed'},
            b.HOT_TRANSITION_STATUSES)
        self.assertEqual([t['id'] for t in a], ['mg-h2'])

    def test_full_poll_announces_shelved_but_not_done(self):
        b = self.bridget
        # A shelve is invisible to the hot poll (the id just vanishes); the full
        # --all diff is the only place it can be announced.
        _, a = b.reconcile_task_states(
            dump(task('mg-f1', 'shelved')), {'mg-f1': 'available'},
            b.FULL_TRANSITION_STATUSES)
        self.assertEqual([t['id'] for t in a], ['mg-f1'])
        # done is NOT in the full partition, so the full diff stays silent on it
        # even though --all shows done — the hot poll already announced it.
        _, a = b.reconcile_task_states(
            dump(task('mg-f2', 'done')), {'mg-f2': 'claimed'},
            b.FULL_TRANSITION_STATUSES)
        self.assertEqual(a, [])

    def test_full_poll_every_is_a_positive_throttle(self):
        b = self.bridget
        self.assertGreaterEqual(b.FULL_POLL_EVERY, 1)
        # Default config: 60s full interval over a 5s poll → every 12 cycles.
        self.assertEqual(
            b.FULL_POLL_EVERY,
            max(1, round(b.FULL_POLL_INTERVAL / b.POLL_INTERVAL)))


class FormatTransitionTitleTest(unittest.TestCase):
    """The card carries a *label*; the full title lives in the mg item. A trim
    is fine, but a bare slice clips mid-word and reads as a whole sentence — the
    mg-7615 failure ('…I want it to be mo'). So a trimmed title must show a '…'
    the reader can act on. See mg-2635."""

    @classmethod
    def setUpClass(cls):
        cls.bridget = load_bridget()

    def test_short_title_is_untouched(self):
        line = self.bridget.format_transition(
            {'id': 'mg-1', 'title': 'ship it'}, 'done')
        self.assertIn('ship it', line)
        self.assertNotIn('…', line)

    def test_long_title_is_visibly_elided_not_bare_sliced(self):
        limit = self.bridget.TASK_TITLE_LABEL_LIMIT
        long = 'x' * (limit + 40)
        line = self.bridget.format_transition(
            {'id': 'mg-2', 'title': long}, 'claimed')
        # A reader must be able to tell the title was cut.
        self.assertIn('…', line)
        # The label stays within the cap it always had (no length regression).
        self.assertNotIn('x' * (limit + 1), line)

    def test_title_exactly_at_the_cap_is_not_marked(self):
        limit = self.bridget.TASK_TITLE_LABEL_LIMIT
        exact = 'y' * limit
        line = self.bridget.format_transition(
            {'id': 'mg-3', 'title': exact}, 'done')
        self.assertIn(exact, line)
        self.assertNotIn('…', line)

    def test_missing_title_falls_back_without_error(self):
        line = self.bridget.format_transition({'id': 'mg-4'}, 'shelved')
        self.assertIn('mg-4', line)


if __name__ == '__main__':
    unittest.main(verbosity=2)
