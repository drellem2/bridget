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

"""The inbound DM/channel send path: Daniel types a message, bridget mails it to
an agent, and echoes back what it did.

mg-3f94. Daniel reported that a *long* DM he *sent* came back looking truncated
with no '…' marker, unlike the mail he *receives* (which mg-2635 already marks).
The report matters because a channel that silently drops the tail of an
instruction is the mg-7e0c hazard: the agent could act on a command with its
guard clause gone.

These tests pin the two halves of the fix:

  * the PAYLOAD — what the agent actually receives via `--body` — is the human's
    text *verbatim*, every byte, on every inbound path; and
  * every human-facing LABEL derived from that text (the ack echo, the mg item
    title) is elided *visibly*, with a '…', never a bare slice.

Stubs `discord` so this runs under system python3 (no venv-bridget required).
"""
import importlib.util
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
    fake_home = Path(tempfile.mkdtemp(prefix='bridget-dm-echo-test-'))
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


def body_of(call_args) -> str:
    """The `--body=` argument mg was actually handed for this send."""
    argv = call_args.args[0]
    for a in argv:
        if a.startswith('--body='):
            return a[len('--body='):]
    raise AssertionError(f'no --body in {argv!r}')


def title_of(call_args) -> str:
    """The `--title=` argument mg was handed (idea:/bug: create an item)."""
    argv = call_args.args[0]
    for a in argv:
        if a.startswith('--title='):
            return a[len('--title='):]
    raise AssertionError(f'no --title in {argv!r}')


# A single-line instruction whose dangerous half is its tail. Under the old bare
# `[:60]` echo the human saw only "delete the staging bucket unless it still has
# the nightl" — the "leave it alone" condition vanished with no marker.
LONG_INSTRUCTION = (
    'delete the staging bucket unless it still has the nightly backups from '
    'before the migration, in which case leave it alone and ping me first'
)


class InboundPayloadIsVerbatim(unittest.TestCase):
    """The agent must receive every byte the human typed — the whole point."""

    @classmethod
    def setUpClass(cls):
        cls.bridget = load_bridget()

    def test_mail_verb_sends_the_full_body(self):
        with mock.patch.object(self.bridget, 'run_mg',
                               return_value=(0, '', '')) as rm:
            self.bridget.handle_command('mail ' + LONG_INSTRUCTION)
        # Nothing the human typed may be missing from what the agent receives.
        self.assertIn(LONG_INSTRUCTION, body_of(rm.call_args))

    def test_idea_verb_sends_the_full_body(self):
        with mock.patch.object(self.bridget, 'run_mg',
                               return_value=(0, 'mg-9999', '')) as rm:
            self.bridget.handle_command('idea: ' + LONG_INSTRUCTION)
        self.assertEqual(body_of(rm.call_args), LONG_INSTRUCTION)

    def test_bug_verb_sends_the_full_body(self):
        with mock.patch.object(self.bridget, 'run_mg',
                               return_value=(0, 'mg-9999', '')) as rm:
            self.bridget.handle_command('bug: ' + LONG_INSTRUCTION)
        self.assertEqual(body_of(rm.call_args), LONG_INSTRUCTION)

    def test_channel_chat_sends_the_full_body(self):
        with mock.patch.object(self.bridget, 'run_mg',
                               return_value=(0, '', '')) as rm:
            self.bridget.send_channel_chat_mail(LONG_INSTRUCTION, 'some-agent')
        self.assertEqual(body_of(rm.call_args), LONG_INSTRUCTION)


class HumanFacingLabelsAreVisiblyElided(unittest.TestCase):
    """Every label cut from the human's own text must carry a '…' — the
    asymmetry Daniel saw was that his *sent* echoes had none while his
    *received* mail (mg-2635) did."""

    @classmethod
    def setUpClass(cls):
        cls.bridget = load_bridget()

    def test_mail_echo_marks_a_truncated_subject(self):
        with mock.patch.object(self.bridget, 'run_mg', return_value=(0, '', '')):
            reply = self.bridget.handle_command('mail ' + LONG_INSTRUCTION)
        self.assertIn('…', reply)
        # The dangerous reading — a clause that looks complete but isn't — must
        # not survive: the bare 60-char prefix no longer stands alone.
        self.assertNotIn('delete the staging bucket unless it still has the nightl"',
                         reply)

    def test_mail_echo_leaves_a_short_subject_untouched(self):
        with mock.patch.object(self.bridget, 'run_mg', return_value=(0, '', '')):
            reply = self.bridget.handle_command('mail ship the release notes')
        self.assertIn('ship the release notes', reply)
        self.assertNotIn('…', reply)

    def test_channel_chat_echo_marks_a_truncated_subject(self):
        with mock.patch.object(self.bridget, 'run_mg', return_value=(0, '', '')):
            reply = self.bridget.send_channel_chat_mail(LONG_INSTRUCTION, 'agent')
        self.assertIn('…', reply)

    def test_idea_title_is_visibly_elided_not_bare_sliced(self):
        limit = self.bridget.MG_TITLE_LABEL_LIMIT
        with mock.patch.object(self.bridget, 'run_mg',
                               return_value=(0, 'mg-1', '')) as rm:
            self.bridget.handle_command('idea: ' + LONG_INSTRUCTION)
        title = title_of(rm.call_args)
        self.assertTrue(title.endswith('…'), title)
        # No length regression: the label stays within the cap it always had.
        self.assertLessEqual(len(title), limit)

    def test_bug_title_is_visibly_elided_not_bare_sliced(self):
        limit = self.bridget.MG_TITLE_LABEL_LIMIT
        with mock.patch.object(self.bridget, 'run_mg',
                               return_value=(0, 'mg-1', '')) as rm:
            self.bridget.handle_command('bug: ' + LONG_INSTRUCTION)
        title = title_of(rm.call_args)
        self.assertTrue(title.endswith('…'), title)
        self.assertLessEqual(len(title), limit)

    def test_short_idea_title_is_untouched(self):
        with mock.patch.object(self.bridget, 'run_mg',
                               return_value=(0, 'mg-1', '')) as rm:
            self.bridget.handle_command('idea: add a dark mode toggle')
        title = title_of(rm.call_args)
        self.assertEqual(title, 'add a dark mode toggle')
        self.assertNotIn('…', title)


if __name__ == '__main__':
    unittest.main(verbosity=2)
