#!/usr/bin/env python3
"""Tests for P2 per-channel agent routing.

Covers:
- ~/.pogo/bridget.channels.toml schema parsing (missing, valid, malformed)
- Inbound routing: handle_channel_message — workflow verbs vs free-form chat
- Outbound routing: outbound_targets lookup by agent + kind
- Backwards compat: empty config = no behaviour change

Stubs `discord` so this runs with system python3 (no venv-bridget required).
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


def load_bridget(channels_toml: str | None = None,
                 env_overrides: dict | None = None):
    """Import bridget into a fresh module namespace with a clean fake HOME and
    optional channels.toml + env overrides. Returns the imported module."""
    fake_home = Path(tempfile.mkdtemp(prefix='bridget-channels-test-'))
    env_dir = fake_home / '.pogo'
    env_dir.mkdir(parents=True)
    (env_dir / 'bridget.env').write_text(
        'DISCORD_BOT_TOKEN=fake\n'
        'DISCORD_USER_ID=1\n'
        'DISCORD_SERVER_ID=2\n'
    )
    if channels_toml is not None:
        (env_dir / 'bridget.channels.toml').write_text(channels_toml)

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


class ChannelsLoaderTest(unittest.TestCase):
    """Schema parsing — missing, valid, malformed configs."""

    def test_missing_file_returns_empty_dicts(self):
        b = load_bridget(channels_toml=None)
        self.assertEqual(b.CHANNELS, {})
        self.assertEqual(b.CHANNELS_BY_SNOWFLAKE, {})
        self.assertEqual(b.OUTBOUND_BY_AGENT, {})

    def test_empty_file_returns_empty_dicts(self):
        b = load_bridget(channels_toml='')
        self.assertEqual(b.CHANNELS, {})
        self.assertEqual(b.CHANNELS_BY_SNOWFLAKE, {})
        self.assertEqual(b.OUTBOUND_BY_AGENT, {})

    def test_single_channel_both_directions(self):
        b = load_bridget(channels_toml='''
[channels.mayor-ops]
snowflake = "1234567890123456789"
agent = "mayor"
direction = "both"
''')
        self.assertEqual(set(b.CHANNELS.keys()), {'mayor-ops'})
        entry = b.CHANNELS['mayor-ops']
        self.assertEqual(entry['snowflake'], 1234567890123456789)
        self.assertEqual(entry['agent'], 'mayor')
        self.assertEqual(entry['direction'], 'both')
        self.assertEqual(entry['kinds'], b.VALID_KINDS)
        # Both maps populated
        self.assertIn(1234567890123456789, b.CHANNELS_BY_SNOWFLAKE)
        self.assertIn('mayor', b.OUTBOUND_BY_AGENT)
        self.assertEqual(len(b.OUTBOUND_BY_AGENT['mayor']), 1)

    def test_inbound_only_skips_outbound_index(self):
        b = load_bridget(channels_toml='''
[channels.architect-design]
snowflake = "9999999999999999999"
agent = "architect"
direction = "inbound"
''')
        self.assertIn(9999999999999999999, b.CHANNELS_BY_SNOWFLAKE)
        self.assertNotIn('architect', b.OUTBOUND_BY_AGENT)

    def test_outbound_only_skips_inbound_index(self):
        b = load_bridget(channels_toml='''
[channels.pm-pogo-digest]
snowflake = "5555555555555555555"
agent = "pm-pogo"
direction = "outbound"
''')
        self.assertNotIn(5555555555555555555, b.CHANNELS_BY_SNOWFLAKE)
        self.assertIn('pm-pogo', b.OUTBOUND_BY_AGENT)

    def test_kinds_filter_subset(self):
        b = load_bridget(channels_toml='''
[channels.architect-claims]
snowflake = "7777777777777777777"
agent = "architect"
direction = "outbound"
kinds = ["idea-claims"]
''')
        entry = b.CHANNELS['architect-claims']
        self.assertEqual(entry['kinds'], frozenset({'idea-claims'}))

    def test_multiple_channels_independent(self):
        b = load_bridget(channels_toml='''
[channels.mayor]
snowflake = "1111"
agent = "mayor"
direction = "both"

[channels.architect]
snowflake = "2222"
agent = "architect"
direction = "inbound"

[channels.pm-pogo]
snowflake = "3333"
agent = "pm-pogo"
direction = "outbound"
kinds = ["mail", "task-transitions"]
''')
        self.assertEqual(set(b.CHANNELS.keys()),
                         {'mayor', 'architect', 'pm-pogo'})
        self.assertEqual(set(b.CHANNELS_BY_SNOWFLAKE.keys()), {1111, 2222})
        self.assertEqual(set(b.OUTBOUND_BY_AGENT.keys()), {'mayor', 'pm-pogo'})

    def test_malformed_entry_skipped_others_loaded(self):
        # Bad direction in 'broken' should be skipped; 'good' should still load.
        b = load_bridget(channels_toml='''
[channels.broken]
snowflake = "1111"
agent = "x"
direction = "sideways"

[channels.good]
snowflake = "2222"
agent = "y"
direction = "both"
''')
        self.assertNotIn('broken', b.CHANNELS)
        self.assertIn('good', b.CHANNELS)

    def test_missing_snowflake_skipped(self):
        b = load_bridget(channels_toml='''
[channels.broken]
agent = "x"
direction = "both"
''')
        self.assertEqual(b.CHANNELS, {})

    def test_missing_agent_skipped(self):
        b = load_bridget(channels_toml='''
[channels.broken]
snowflake = "1111"
direction = "both"
''')
        self.assertEqual(b.CHANNELS, {})

    def test_invalid_kind_skipped(self):
        b = load_bridget(channels_toml='''
[channels.broken]
snowflake = "1111"
agent = "x"
direction = "outbound"
kinds = ["mail", "moonbeams"]
''')
        self.assertNotIn('broken', b.CHANNELS)

    def test_default_direction_is_both(self):
        b = load_bridget(channels_toml='''
[channels.bare]
snowflake = "1111"
agent = "x"
''')
        self.assertEqual(b.CHANNELS['bare']['direction'], 'both')

    def test_duplicate_inbound_snowflake_keeps_first(self):
        b = load_bridget(channels_toml='''
[channels.first]
snowflake = "1111"
agent = "a"
direction = "inbound"

[channels.second]
snowflake = "1111"
agent = "b"
direction = "inbound"
''')
        # Both entries load, but only the first wins the snowflake index.
        self.assertEqual(len(b.CHANNELS), 2)
        self.assertEqual(b.CHANNELS_BY_SNOWFLAKE[1111]['name'], 'first')

    def test_outbound_targets_filters_by_kind(self):
        b = load_bridget(channels_toml='''
[channels.mail-only]
snowflake = "1111"
agent = "agent-x"
direction = "outbound"
kinds = ["mail"]

[channels.transitions-only]
snowflake = "2222"
agent = "agent-x"
direction = "outbound"
kinds = ["task-transitions"]
''')
        mail_targets = b.outbound_targets('agent-x', 'mail')
        trans_targets = b.outbound_targets('agent-x', 'task-transitions')
        idea_targets = b.outbound_targets('agent-x', 'idea-claims')
        self.assertEqual(len(mail_targets), 1)
        self.assertEqual(mail_targets[0]['name'], 'mail-only')
        self.assertEqual(len(trans_targets), 1)
        self.assertEqual(trans_targets[0]['name'], 'transitions-only')
        self.assertEqual(idea_targets, [])

    def test_outbound_targets_unmapped_agent_returns_empty(self):
        b = load_bridget(channels_toml='')
        self.assertEqual(b.outbound_targets('mayor', 'mail'), [])


class HandleChannelMessageTest(unittest.TestCase):
    """Inbound routing: workflow verbs continue to route through
    WORKFLOW_AGENT; free-form chat becomes a mail to the channel's agent."""

    def _setup(self):
        return load_bridget(channels_toml='''
[channels.mayor-ops]
snowflake = "9999"
agent = "mayor"
direction = "inbound"
''')

    def test_workflow_verb_routes_to_workflow_agent_not_channel_agent(self):
        b = self._setup()
        entry = b.CHANNELS['mayor-ops']
        # `approve mg-...` should still mail WORKFLOW_AGENT (architect by
        # default), NOT the channel-mapped agent (mayor).
        with mock.patch.object(b, 'run_mg', return_value=(0, '', '')) as rm:
            reply = b.handle_channel_message('approve mg-deadbeef', entry)
        sent_args = rm.call_args.args[0]
        self.assertIn('mail', sent_args)
        self.assertIn('send', sent_args)
        # Recipient is the third positional after `mail send`
        recipient_idx = sent_args.index('send') + 1
        self.assertEqual(sent_args[recipient_idx], b.WORKFLOW_AGENT)
        self.assertIn('approve', reply)

    def test_free_form_text_routes_to_channel_agent(self):
        b = self._setup()
        entry = b.CHANNELS['mayor-ops']
        with mock.patch.object(b, 'run_mg', return_value=(0, '', '')) as rm:
            reply = b.handle_channel_message('what is the queue today?', entry)
        sent_args = rm.call_args.args[0]
        self.assertIn('mail', sent_args)
        self.assertIn('send', sent_args)
        recipient_idx = sent_args.index('send') + 1
        # The channel agent (mayor), NOT WORKFLOW_AGENT (architect default).
        self.assertEqual(sent_args[recipient_idx], 'mayor')
        self.assertIn('mailed', reply)

    def test_free_form_multiline_splits_subject_body(self):
        b = self._setup()
        entry = b.CHANNELS['mayor-ops']
        with mock.patch.object(b, 'run_mg', return_value=(0, '', '')) as rm:
            b.handle_channel_message(
                'short subject\nlonger body line 1\nbody line 2',
                entry,
            )
        sent_args = rm.call_args.args[0]
        joined = '\n'.join(sent_args)
        self.assertIn('--subject=short subject', joined)
        self.assertIn('longer body line 1', joined)

    def test_help_command_is_recognized_in_channel(self):
        # Recognized, non-mail-emitting verbs (like `help`) should still produce
        # their normal reply when typed in a mapped channel — not get treated as
        # free-form chat.
        b = self._setup()
        entry = b.CHANNELS['mayor-ops']
        reply = b.handle_channel_message('help', entry)
        self.assertIn('Commands:', reply)


class BackwardsCompatTest(unittest.TestCase):
    """No bridget.channels.toml = bit-identical to v1.0.0/P1."""

    def test_no_channels_file_keeps_module_state_empty(self):
        b = load_bridget(channels_toml=None)
        self.assertEqual(b.CHANNELS, {})
        self.assertEqual(b.CHANNELS_BY_SNOWFLAKE, {})
        self.assertEqual(b.OUTBOUND_BY_AGENT, {})

    def test_no_channels_file_handle_command_unchanged(self):
        b = load_bridget(channels_toml=None)
        # An obvious unrecognized message still returns the legacy fallback.
        self.assertEqual(b.handle_command('zzzgibberish'),
                         b.UNRECOGNIZED_REPLY)
        self.assertIn('Unrecognized', b.UNRECOGNIZED_REPLY)


if __name__ == '__main__':
    unittest.main()
