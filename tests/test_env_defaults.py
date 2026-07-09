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

"""Test env-key fallback: defaults must reproduce bridget v1.0.0 behavior
exactly when no overrides are set, and overrides must take effect when set.

Stubs the `discord` module so this test runs with system python3 — no
venv-bridget required.
"""
import importlib.util
import os
import re
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / 'bridget'


def load_bridget(env_overrides: dict | None = None):
    """Import bridget into a fresh module namespace with a clean fake HOME and
    optional env overrides. Returns the imported module."""
    fake_home = Path(tempfile.mkdtemp(prefix='bridget-env-test-'))
    env_dir = fake_home / '.pogo'
    env_dir.mkdir(parents=True)
    (env_dir / 'bridget.env').write_text(
        'DISCORD_BOT_TOKEN=fake\n'
        'DISCORD_USER_ID=1\n'
        'DISCORD_SERVER_ID=2\n'
    )

    keys_we_set = {'HOME', 'BRIDGET_REPO_DIR'}
    if env_overrides:
        keys_we_set.update(env_overrides.keys())
    saved_env = {k: os.environ.get(k) for k in keys_we_set}
    os.environ['HOME'] = str(fake_home)
    os.environ['BRIDGET_REPO_DIR'] = str(REPO)
    if env_overrides:
        for k, v in env_overrides.items():
            os.environ[k] = v

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


class EnvDefaultsTest(unittest.TestCase):
    """With no P1 overrides, every value must equal the v1.0.0 hard-coded
    behavior — these are the upstream-compat anchors."""

    def test_workflow_agent_default_is_architect(self):
        b = load_bridget()
        self.assertEqual(b.WORKFLOW_AGENT, 'architect')

    def test_inbox_tag_default_is_pogo_inbox(self):
        b = load_bridget()
        self.assertEqual(b.INBOX_TAG, 'pogo-inbox')

    def test_poll_interval_default_is_5(self):
        b = load_bridget()
        self.assertEqual(b.POLL_INTERVAL, 5)

    def test_quiet_respects_outbound_default_is_false(self):
        b = load_bridget()
        self.assertFalse(b.QUIET_RESPECTS_OUTBOUND)

    def test_approval_re_default_matches_legacy_prefix(self):
        b = load_bridget()
        self.assertIsNotNone(b.APPROVAL_RE.match('Subject: approval needed mg-deadbeef'))
        self.assertIsNone(b.APPROVAL_RE.match('Subject: status update'))

    def test_restart_cmd_default_is_bash_build_sh(self):
        b = load_bridget()
        self.assertEqual(b.RESTART_CMD, ['bash', 'build.sh'])

    def test_crew_pattern_default_matches_legacy_set(self):
        b = load_bridget()
        for crew in ('architect', 'mayor', 'human', ''):
            self.assertIsNotNone(
                b.CREW_PATTERN.match(crew),
                f'default CREW_PATTERN should match legacy crew name {crew!r}',
            )
        for polecat in ('polecat-foo', 'cat-mg-1234', 'random-name'):
            self.assertIsNone(
                b.CREW_PATTERN.match(polecat),
                f'default CREW_PATTERN should NOT match polecat name {polecat!r}',
            )

    def test_crew_pattern_matches_pm_dash_assignees(self):
        """The widening: any pm-* prefix counts as crew, not a polecat."""
        b = load_bridget()
        for crew in ('pm-pogo', 'pm-discord-bridge', 'pm-foo'):
            self.assertIsNotNone(
                b.CREW_PATTERN.match(crew),
                f'default CREW_PATTERN should match pm- crew name {crew!r}',
            )


class EnvOverridesTest(unittest.TestCase):
    """Each override must take effect end-to-end on the relevant module
    constant. Together these prove the env keys are wired through, not just
    declared."""

    def test_workflow_agent_override(self):
        b = load_bridget({'POGO_WORKFLOW_AGENT': 'pm-pogo'})
        self.assertEqual(b.WORKFLOW_AGENT, 'pm-pogo')

    def test_inbox_tag_override(self):
        b = load_bridget({'POGO_INBOX_TAG': 'daniel-creator'})
        self.assertEqual(b.INBOX_TAG, 'daniel-creator')

    def test_poll_interval_override(self):
        b = load_bridget({'BRIDGET_POLL_INTERVAL': '15'})
        self.assertEqual(b.POLL_INTERVAL, 15)

    def test_quiet_respects_outbound_override(self):
        b = load_bridget({'BRIDGET_QUIET_RESPECTS_OUTBOUND': 'true'})
        self.assertTrue(b.QUIET_RESPECTS_OUTBOUND)

    def test_approval_re_override(self):
        b = load_bridget({'BRIDGET_APPROVAL_RE': r'^Subject: please approve '})
        self.assertIsNotNone(b.APPROVAL_RE.match('Subject: please approve mg-x'))
        self.assertIsNone(b.APPROVAL_RE.match('Subject: approval needed mg-x'))

    def test_restart_cmd_override(self):
        b = load_bridget({'BRIDGET_RESTART_CMD': 'make build'})
        self.assertEqual(b.RESTART_CMD, ['make', 'build'])

    def test_crew_pattern_override(self):
        b = load_bridget({
            'BRIDGET_CREW_PATTERN': r'^(coordinator-.*|lead-.*|architect|)$',
        })
        self.assertIsNotNone(b.CREW_PATTERN.match('coordinator-foo'))
        self.assertIsNotNone(b.CREW_PATTERN.match('lead-bar'))
        self.assertIsNone(b.CREW_PATTERN.match('pm-pogo'))


if __name__ == '__main__':
    unittest.main()
