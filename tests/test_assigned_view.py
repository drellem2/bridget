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

"""The read-only 'on your plate' view (mg-3358).

`mine` renders `mg list --assignee=human` into Discord: the work items still
outstanding, the approval requests awaiting a decision, and a count of what's
resolved. The two facts these tests pin:

  * the view is READ-ONLY — it runs exactly one `mg list` and never a mutating
    mg/pogo call, marks no mail read, and archives nothing; and
  * outstanding (not-done) and resolved (done) items are separated, so the
    distinction Daniel asked for beyond read/unread actually shows up.

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
    fake_home = Path(tempfile.mkdtemp(prefix='bridget-assigned-test-'))
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


def ndjson(*items) -> str:
    return '\n'.join(json.dumps(d) for d in items) + '\n'


SAMPLE = ndjson(
    {'id': 'mg-aaaa', 'status': 'claimed', 'title': 'fix the retry loop',
     'assignee': 'human', 'repo': '/Users/x/dev/bridget'},
    {'id': 'mg-bbbb', 'status': 'available', 'title': 'wire up the thing',
     'assignee': 'human', 'repo': '/Users/x/dev/pogo'},
    {'id': 'mg-cccc', 'status': 'pending', 'title': 'blocked on review',
     'assignee': 'human', 'repo': '/Users/x/dev/bridget'},
    {'id': 'mg-dddd', 'status': 'done', 'title': 'already handled this',
     'assignee': 'human', 'repo': '/Users/x/dev/bridget'},
)


class AssignedViewIsReadOnly(unittest.TestCase):
    """The whole point of the conservative first cut: it touches nothing."""

    @classmethod
    def setUpClass(cls):
        cls.bridget = load_bridget()

    def test_runs_exactly_one_mg_list_and_nothing_mutating(self):
        with mock.patch.object(self.bridget, 'run_mg',
                               return_value=(0, SAMPLE, '')) as rm, \
             mock.patch.object(self.bridget, 'run_pogo',
                               return_value=(0, '', '')) as rp, \
             mock.patch.object(self.bridget, 'mark_mail_read') as mmr, \
             mock.patch.object(self.bridget, 'scan_pending_approvals',
                               return_value=[]):
            self.bridget.handle_command('mine')
        # One mg call, and it is a read: `list --assignee=human`.
        self.assertEqual(rm.call_count, 1)
        argv = rm.call_args.args[0]
        self.assertEqual(argv[0], 'list')
        self.assertIn('--assignee=human', argv)
        # No verb here should be able to send mail, nudge, or mark read.
        rp.assert_not_called()
        mmr.assert_not_called()

    def test_aliases_all_reach_the_same_view(self):
        for verb in ('mine', 'outstanding', 'plate', 'assigned to me'):
            with mock.patch.object(self.bridget, 'run_mg',
                                   return_value=(0, SAMPLE, '')), \
                 mock.patch.object(self.bridget, 'scan_pending_approvals',
                                   return_value=[]):
                reply = self.bridget.handle_command(verb)
            self.assertIn('On your plate', reply,
                          f'{verb!r} did not reach the assigned view')


class AssignedViewSeparatesOutstandingFromResolved(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.bridget = load_bridget()

    def test_outstanding_items_are_listed_resolved_is_only_counted(self):
        with mock.patch.object(self.bridget, 'run_mg',
                               return_value=(0, SAMPLE, '')), \
             mock.patch.object(self.bridget, 'scan_pending_approvals',
                               return_value=[]):
            reply = self.bridget.handle_command('mine')
        # Three outstanding, surfaced by id.
        self.assertIn('3 outstanding', reply)
        for iid in ('mg-aaaa', 'mg-bbbb', 'mg-cccc'):
            self.assertIn(iid, reply)
        # The done item is counted as resolved, not listed as outstanding.
        self.assertIn('Recently resolved:** 1', reply)
        self.assertNotIn('mg-dddd', reply)

    def test_outstanding_ordered_pending_then_claimed_then_available(self):
        with mock.patch.object(self.bridget, 'run_mg',
                               return_value=(0, SAMPLE, '')), \
             mock.patch.object(self.bridget, 'scan_pending_approvals',
                               return_value=[]):
            reply = self.bridget.handle_command('mine')
        self.assertLess(reply.index('mg-cccc'), reply.index('mg-aaaa'),
                        'pending should sort before claimed')
        self.assertLess(reply.index('mg-aaaa'), reply.index('mg-bbbb'),
                        'claimed should sort before available')

    def test_approvals_are_folded_in(self):
        with mock.patch.object(self.bridget, 'run_mg',
                               return_value=(0, SAMPLE, '')), \
             mock.patch.object(self.bridget, 'scan_pending_approvals',
                               return_value=['approval needed: design mg-1f2a']):
            reply = self.bridget.handle_command('mine')
        self.assertIn('Awaiting your approval (1)', reply)
        self.assertIn('mg-1f2a', reply)

    def test_empty_plate_still_renders(self):
        with mock.patch.object(self.bridget, 'run_mg',
                               return_value=(0, '', '')), \
             mock.patch.object(self.bridget, 'scan_pending_approvals',
                               return_value=[]):
            reply = self.bridget.handle_command('mine')
        self.assertIn('0 outstanding', reply)
        self.assertIn('nothing assigned to you', reply)

    def test_mg_failure_is_surfaced_not_swallowed(self):
        with mock.patch.object(self.bridget, 'run_mg',
                               return_value=(1, '', 'mg exploded')):
            reply = self.bridget.handle_command('mine')
        self.assertIn('✗', reply)
        self.assertIn('mg exploded', reply)


if __name__ == '__main__':
    unittest.main(verbosity=2)
