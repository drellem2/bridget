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
                 env_overrides: dict | None = None,
                 channel_ids: dict | None = None):
    """Import bridget into a fresh module namespace with a clean fake HOME and
    optional channels.toml + env overrides. Returns the imported module.

    `channel_ids`, if given, seeds ~/.pogo/bridget.channel-ids.json (the
    persisted name->snowflake registry) before import, so tests can exercise the
    "resolve a persisted id on restart" path."""
    import json as _json
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
    if channel_ids is not None:
        (env_dir / 'bridget.channel-ids.json').write_text(_json.dumps(channel_ids))

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

    def test_missing_snowflake_loads_deferred_for_autocreate(self):
        # Slice A (mg-2fea): a snowflake-less entry is no longer skipped — it
        # loads with snowflake=None, absent from the inbound index until
        # resolve_and_wire_channels() mints it a channel at startup.
        b = load_bridget(channels_toml='''
[channels.mayor-ops]
agent = "mayor"
direction = "both"
''')
        self.assertIn('mayor-ops', b.CHANNELS)
        entry = b.CHANNELS['mayor-ops']
        self.assertIsNone(entry['snowflake'])
        self.assertEqual(entry['agent'], 'mayor')
        self.assertEqual(entry['channel_name'], 'mayor-ops')
        # No id yet → not in the snowflake index...
        self.assertEqual(b.CHANNELS_BY_SNOWFLAKE, {})
        # ...but outbound fan-out (keyed by agent) still applies.
        self.assertIn('mayor', b.OUTBOUND_BY_AGENT)

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


class ChannelsStatusLineTest(unittest.TestCase):
    """The `settings` / startup one-liner that makes DM-only mode visible, so a
    missing bridget.channels.toml is self-diagnosable rather than a silent
    "everything lands in the log channel"."""

    def test_no_file_reports_off_and_names_the_file(self):
        b = load_bridget(channels_toml=None)
        line = b.channels_status_line()
        self.assertIn('off', line)
        self.assertIn('DM-only', line)
        # Names the exact file the operator must create to turn routing on.
        self.assertIn(str(b.CHANNELS_FILE), line)

    def test_empty_channels_table_reports_off(self):
        b = load_bridget(channels_toml='')
        self.assertIn('off', b.channels_status_line())

    def test_configured_channels_report_on_with_agents(self):
        b = load_bridget(channels_toml='''
[channels.mayor-ops]
snowflake = "9999"
agent = "mayor"
direction = "both"

[channels.ops]
snowflake = "8888"
agent = "ops"
direction = "inbound"
''')
        line = b.channels_status_line()
        self.assertIn('on', line)
        self.assertIn('2 channel(s)', line)
        self.assertIn('mayor', line)
        self.assertIn('ops', line)

    def test_render_settings_includes_routing_status(self):
        b = load_bridget(channels_toml=None)
        out = b.render_settings(b.SETTINGS.summary())
        self.assertIn('Per-channel routing:', out)
        self.assertIn('off', out)


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


class ShippedExampleTest(unittest.TestCase):
    """The bridget.channels.toml.example shipped at the repo root must be a
    valid config — operators copy-paste it as their starting point."""

    def test_example_parses_and_exercises_all_directions(self):
        example = (REPO / 'bridget.channels.toml.example').read_text()
        b = load_bridget(channels_toml=example)
        # All three directions should show up: at least one inbound-only,
        # one outbound-only, and one both. The example loses its teaching
        # value if any direction goes missing.
        directions = {entry['direction'] for entry in b.CHANNELS.values()}
        self.assertEqual(directions, {'inbound', 'outbound', 'both'},
                         f'example must cover every direction; got {directions}')
        # 'both' and 'inbound' entries should appear in the snowflake index;
        # 'outbound' and 'both' entries should appear in the agent index.
        self.assertTrue(b.CHANNELS_BY_SNOWFLAKE,
                        'example must include at least one inbound channel')
        self.assertTrue(b.OUTBOUND_BY_AGENT,
                        'example must include at least one outbound channel')


class _FakeChannel:
    def __init__(self, cid, name, guild):
        self.id = cid
        self.name = name
        self.guild = guild


class _FakeGuild:
    """Minimal stand-in for discord.Guild: name-keyed text channels plus a
    create hook that records what bridget minted."""

    def __init__(self, gid=2, channels=None):
        self.id = gid
        self.text_channels = list(channels or [])
        self.created: list = []
        self._next = 700000000000000000

    def get_channel(self, snowflake):
        for c in self.text_channels:
            if c.id == snowflake:
                return c
        return None

    async def create_text_channel(self, name, reason=None):
        self._next += 1
        ch = _FakeChannel(self._next, name, self)
        self.text_channels.append(ch)
        self.created.append(ch)
        return ch


def _install_discord_stubs(b, guild, fetch_channel=None):
    """Give bridget's MagicMock `discord`/`client` the real bits the resolution
    path needs: exception classes it catches, a working utils.get, and a
    guild-returning client."""
    import types

    b.discord.NotFound = type('NotFound', (Exception,), {})
    b.discord.Forbidden = type('Forbidden', (Exception,), {})
    b.discord.HTTPException = type('HTTPException', (Exception,), {})

    def _get(iterable, **attrs):
        for item in iterable:
            if all(getattr(item, k, None) == v for k, v in attrs.items()):
                return item
        return None

    b.discord.utils = types.SimpleNamespace(get=_get)

    async def _default_fetch(snowflake):
        raise b.discord.NotFound()

    b.client.get_guild = lambda gid: guild if gid == b.SERVER_ID else None
    b.client.fetch_channel = fetch_channel or _default_fetch


class ChannelIdRegistryTest(unittest.TestCase):
    """The persisted name->snowflake registry (CHANNEL_IDS_FILE)."""

    def test_save_then_load_round_trips(self):
        b = load_bridget(channels_toml='')
        b.save_channel_id('mayor-ops', 424242424242424242)
        self.assertEqual(
            b.load_channel_ids(), {'mayor-ops': 424242424242424242})
        # File is owner-only (0600), like every other bridget state file.
        mode = b.CHANNEL_IDS_FILE.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_load_missing_file_is_empty(self):
        b = load_bridget(channels_toml='')
        self.assertEqual(b.load_channel_ids(), {})

    def test_corrupt_registry_does_not_raise(self):
        b = load_bridget(channels_toml='')
        b.CHANNEL_IDS_FILE.write_text('{not json')
        self.assertEqual(b.load_channel_ids(), {})


class ChannelNameDerivationTest(unittest.TestCase):
    def test_derives_slug_from_label(self):
        b = load_bridget(channels_toml='')
        self.assertEqual(
            b.discord_channel_name({'name': 'Mayor Ops!'}), 'mayor-ops')

    def test_explicit_channel_key_wins(self):
        b = load_bridget(channels_toml='')
        self.assertEqual(
            b.discord_channel_name({'name': 'x', 'channel': 'Cool Room'}),
            'cool-room')

    def test_empty_slug_falls_back(self):
        b = load_bridget(channels_toml='')
        self.assertEqual(
            b.discord_channel_name({'name': '!!!'}), 'bridget-channel')


class PersistedIdPrecedenceTest(unittest.TestCase):
    """A registry id (bridget's own creation) wins over a toml snowflake for the
    same name, so a channel bridget minted is never re-created because someone
    left a stale hand-typed id in the toml."""

    def test_registry_overrides_toml_snowflake(self):
        b = load_bridget(
            channels_toml='''
[channels.mayor-ops]
snowflake = "1111"
agent = "mayor"
direction = "both"
''',
            channel_ids={'mayor-ops': 999888777666555444},
        )
        entry = b.CHANNELS['mayor-ops']
        self.assertEqual(entry['snowflake'], 999888777666555444)
        self.assertIn(999888777666555444, b.CHANNELS_BY_SNOWFLAKE)
        self.assertNotIn(1111, b.CHANNELS_BY_SNOWFLAKE)

    def test_toml_used_when_no_registry_entry(self):
        b = load_bridget(channels_toml='''
[channels.mayor-ops]
snowflake = "1111"
agent = "mayor"
direction = "both"
''')
        self.assertEqual(b.CHANNELS['mayor-ops']['snowflake'], 1111)


class EnsureChannelTest(unittest.TestCase):
    """resolve_and_wire_channels / ensure_channel: create-if-missing, adopt by
    name, resolve-persisted-on-restart, and no-op for a live snowflake."""

    def _run(self, coro):
        import asyncio
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_missing_snowflake_creates_and_persists(self):
        b = load_bridget(channels_toml='''
[channels.mayor-ops]
agent = "mayor"
direction = "both"
''')
        guild = _FakeGuild(gid=b.SERVER_ID)
        _install_discord_stubs(b, guild)
        self._run(b.resolve_and_wire_channels())

        # A real channel was created, named from the label.
        self.assertEqual(len(guild.created), 1)
        created = guild.created[0]
        self.assertEqual(created.name, 'mayor-ops')
        entry = b.CHANNELS['mayor-ops']
        # Wired: entry carries the id, inbound routing registered.
        self.assertEqual(entry['snowflake'], created.id)
        self.assertIs(b.CHANNELS_BY_SNOWFLAKE[created.id], entry)
        # Persisted: survives restart.
        self.assertEqual(b.load_channel_ids(), {'mayor-ops': created.id})

    def test_restart_resolves_persisted_id_without_duplicate(self):
        # Simulate the post-create restart: the id is in the registry, the
        # channel exists in the guild. No new channel must be minted.
        created_id = 700000000000000123
        b = load_bridget(
            channels_toml='''
[channels.mayor-ops]
agent = "mayor"
direction = "both"
''',
            channel_ids={'mayor-ops': created_id},
        )
        # Loaded straight from the registry.
        self.assertEqual(b.CHANNELS['mayor-ops']['snowflake'], created_id)
        existing = _FakeChannel(created_id, 'mayor-ops', None)
        guild = _FakeGuild(gid=b.SERVER_ID, channels=[existing])
        existing.guild = guild
        _install_discord_stubs(b, guild)
        self._run(b.resolve_and_wire_channels())
        self.assertEqual(guild.created, [], 'must not re-create a live channel')
        self.assertEqual(b.CHANNELS['mayor-ops']['snowflake'], created_id)

    def test_invalid_snowflake_creates_then_reuses_by_name(self):
        # An entry with a bogus snowflake and no registry id: first boot must
        # create a channel; a second boot (channel now exists by name) must
        # adopt it rather than duplicate — even if the registry were lost.
        b = load_bridget(channels_toml='''
[channels.mayor-ops]
snowflake = "1234"
agent = "mayor"
direction = "both"
''')
        guild = _FakeGuild(gid=b.SERVER_ID)  # 1234 does not exist here
        _install_discord_stubs(b, guild)
        self._run(b.resolve_and_wire_channels())
        self.assertEqual(len(guild.created), 1)
        created = guild.created[0]
        self.assertEqual(b.CHANNELS['mayor-ops']['snowflake'], created.id)

        # Second boot: wipe the registry to prove name-adoption is what stops
        # the duplicate, then reset the entry to its unresolved state.
        b.CHANNEL_IDS_FILE.unlink()
        b.CHANNELS['mayor-ops']['snowflake'] = 1234
        b.CHANNELS_BY_SNOWFLAKE.clear()
        self._run(b.resolve_and_wire_channels())
        self.assertEqual(len(guild.created), 1, 'no duplicate on restart')
        self.assertEqual(b.CHANNELS['mayor-ops']['snowflake'], created.id)

    def test_live_snowflake_is_noop_no_create(self):
        live = _FakeChannel(5555, 'mayor-ops', None)
        b = load_bridget(channels_toml='''
[channels.mayor-ops]
snowflake = "5555"
agent = "mayor"
direction = "both"
''')
        guild = _FakeGuild(gid=b.SERVER_ID, channels=[live])
        live.guild = guild
        _install_discord_stubs(b, guild)
        self._run(b.resolve_and_wire_channels())
        self.assertEqual(guild.created, [])
        self.assertEqual(b.CHANNELS['mayor-ops']['snowflake'], 5555)

    def test_adopts_existing_channel_by_name(self):
        # No snowflake, but a channel with the target name already exists: adopt
        # it, don't create a second one.
        preexisting = _FakeChannel(8888, 'mayor-ops', None)
        b = load_bridget(channels_toml='''
[channels.mayor-ops]
agent = "mayor"
direction = "both"
''')
        guild = _FakeGuild(gid=b.SERVER_ID, channels=[preexisting])
        preexisting.guild = guild
        _install_discord_stubs(b, guild)
        self._run(b.resolve_and_wire_channels())
        self.assertEqual(guild.created, [])
        self.assertEqual(b.CHANNELS['mayor-ops']['snowflake'], 8888)
        self.assertEqual(b.load_channel_ids(), {'mayor-ops': 8888})


if __name__ == '__main__':
    unittest.main()
