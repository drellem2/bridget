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

"""Guards for the 'no hardcoded secrets' requirement.

These are cheap, blunt, and run on every commit. They exist because the failure
mode they catch — a real bot token pasted into a file and pushed — is silent,
irreversible (the token is burned the moment it hits a remote), and exactly the
kind of thing a hurried edit introduces.
"""
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def tracked_files() -> list[Path]:
    """Every file git tracks. Untracked scratch files are not our problem."""
    out = subprocess.run(['git', 'ls-files', '-z'], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout
    return [REPO / n for n in out.split('\0') if n]


#: A Discord bot token is three dot-separated base64url chunks; the first is the
#: bot's user-id snowflake, so it starts with a run of digits when decoded. This
#: matches the shape without needing a real one to test against.
DISCORD_TOKEN_RE = re.compile(r'\b[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,40}\b')

#: `KEY=value` for a secret-ish key, where value is neither empty nor a comment
#: nor an obvious placeholder. Horizontal whitespace only — `\s` would match the
#: newline and capture the following line as the "value".
ASSIGNED_SECRET_RE = re.compile(
    r'^(?!#)[ \t]*(DISCORD_BOT_TOKEN|GH_TOKEN|OPENAI_API_KEY|AWS_SECRET_ACCESS_KEY)'
    r'[ \t]*=[ \t]*(.+)$',
    re.MULTILINE,
)

PLACEHOLDERS = {'', 'fake', 'x', 'changeme', 'your-token-here', '<token>', 'xxx'}

SKIP_SUFFIXES = {'.png', '.jpg', '.gif', '.ico', '.pyc'}


class TestNoCommittedSecrets(unittest.TestCase):
    def setUp(self):
        self.files = [p for p in tracked_files()
                      if p.suffix not in SKIP_SUFFIXES and p.is_file()]
        self.assertGreater(len(self.files), 5, 'git ls-files returned suspiciously little')

    def _read(self, p: Path) -> str:
        try:
            return p.read_text()
        except (UnicodeDecodeError, OSError):
            return ''

    def test_no_discord_token_shaped_string_is_committed(self):
        for p in self.files:
            if p.name == 'test_secrets.py':
                continue  # this file describes the pattern, by necessity
            hit = DISCORD_TOKEN_RE.search(self._read(p))
            self.assertIsNone(
                hit, f'{p.relative_to(REPO)} contains a Discord-token-shaped string')

    def test_no_secret_key_is_assigned_a_real_value(self):
        for p in self.files:
            if p.name == 'test_secrets.py':
                continue
            for key, value in ASSIGNED_SECRET_RE.findall(self._read(p)):
                value = value.strip().strip('"\'')
                # The env template legitimately ships `DISCORD_BOT_TOKEN=` with
                # nothing after it, and tests set obvious fakes.
                self.assertIn(
                    value.lower(), PLACEHOLDERS,
                    f'{p.relative_to(REPO)} assigns {key} a non-placeholder value')

    def test_env_example_ships_an_empty_token(self):
        text = (REPO / 'bridget.env.example').read_text()
        self.assertRegex(text, re.compile(r'^DISCORD_BOT_TOKEN=[ \t]*$', re.MULTILINE))

    def test_env_example_documents_every_key_bridget_reads(self):
        """A knob nobody can discover is a knob nobody uses."""
        source = (REPO / 'bridget').read_text()
        example = (REPO / 'bridget.env.example').read_text()
        keys = set(re.findall(r"lookup\('([A-Z0-9_]+)'\)", source))
        self.assertIn('BRIDGET_LOG_CHANNEL_ID', keys, 'sanity: regex found nothing')
        for key in sorted(keys):
            self.assertRegex(
                example, re.compile(rf'^#?{re.escape(key)}=', re.MULTILINE),
                f'{key} is undocumented in bridget.env.example')

    def test_bridget_never_prints_the_token(self):
        """No print/log statement may take TOKEN or the token config value."""
        source = (REPO / 'bridget').read_text()
        for bad in ("print(TOKEN", "print(f'{TOKEN", 'print(f"{TOKEN',
                    "CONFIG['token']}", 'stderr) if TOKEN'):
            self.assertNotIn(bad, source)

    def test_token_reaches_only_discord_client_run(self):
        source = (REPO / 'bridget').read_text()
        uses = [ln.strip() for ln in source.splitlines()
                if re.search(r'\bTOKEN\b', ln) and not ln.strip().startswith('#')]
        # Two legitimate uses: binding it, and handing it to discord.py.
        self.assertEqual(
            sorted(uses),
            sorted(["TOKEN = CONFIG['token']", 'client.run(TOKEN, log_handler=None)']),
            f'TOKEN referenced somewhere unexpected: {uses}')


class TestEnvFilePermissions(unittest.TestCase):
    def test_loose_permissions_are_warned_about(self):
        import importlib.util
        from importlib.machinery import SourceFileLoader

        # Import just the helper, without running module-level config loading.
        loader = SourceFileLoader('bridget_perm_probe', str(REPO / 'bridget'))
        spec = importlib.util.spec_from_loader('bridget_perm_probe', loader)
        src = (REPO / 'bridget').read_text()
        ns: dict = {}
        # Execute only the function definition we care about.
        start = src.index('def warn_if_world_readable')
        end = src.index('def load_env_file')
        exec('import sys\nfrom pathlib import Path\n' + src[start:end], ns)
        warn = ns['warn_if_world_readable']

        d = Path(tempfile.mkdtemp(prefix='bridget-perm-'))
        loose, tight = d / 'loose.env', d / 'tight.env'
        loose.write_text('DISCORD_BOT_TOKEN=x\n')
        tight.write_text('DISCORD_BOT_TOKEN=x\n')
        os.chmod(loose, 0o644)
        os.chmod(tight, 0o600)

        self.assertTrue(warn(loose), 'a 644 token file must warn')
        self.assertFalse(warn(tight), 'a 600 token file must not warn')
        self.assertFalse(warn(d / 'missing.env'), 'a missing file must not warn')

    def test_installer_chmods_the_env_file(self):
        install = (REPO / 'install.sh').read_text()
        self.assertIn('chmod 600 "$ENV_FILE"', install)

    def test_installer_reads_the_token_with_echo_off(self):
        install = (REPO / 'install.sh').read_text()
        self.assertIn('read -rs token', install)

    def test_installer_never_echoes_the_token_or_any_part_of_it(self):
        """A partial token is still a leaked token — no prefix, no suffix."""
        install = (REPO / 'install.sh').read_text()
        self.assertNotIn('echo "$token"', install)
        self.assertNotIn("printf '%s' \"$token\"", install)
        # No bash substring/expansion of $token may reach an output statement.
        self.assertNotRegex(install, r'(log|warn|echo|printf)[^\n]*\$\{token[:#%]')
        self.assertNotRegex(install, r'(log|warn|echo|printf)[^\n]*"\$token"')
        self.assertIn('DISCORD_BOT_TOKEN set (value hidden)', install)

    def test_installer_validates_token_shape_without_disclosing_it(self):
        install = (REPO / 'install.sh').read_text()
        self.assertIn('token_shape_ok', install)

    def test_installer_passes_the_token_via_env_not_argv(self):
        """argv is world-readable in `ps`; the env of another uid is not."""
        install = (REPO / 'install.sh').read_text()
        self.assertIn('VALUE="$value" awk', install)


class TestStateFilePermissions(unittest.TestCase):
    """A8. The state files hold no token, but they do hold mail subjects, agent
    names, and which conversations the human muted. Under the default umask of
    022 a plain write_text lands them at 0644."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix='bridget-state-perm-'))

    def _mode(self, p: Path) -> int:
        return stat.S_IMODE(p.stat().st_mode)

    def test_write_state_is_owner_only(self):
        from bridget_core.statefile import write_state
        p = self.d / 'x.json'
        write_state(p, '{}')
        self.assertEqual(self._mode(p), 0o600)
        self.assertEqual(p.read_text(), '{}')

    def test_write_state_tightens_an_existing_loose_file(self):
        from bridget_core.statefile import write_state
        p = self.d / 'x.json'
        p.write_text('old')
        os.chmod(p, 0o644)
        write_state(p, 'new')
        self.assertEqual(self._mode(p), 0o600)

    def test_write_state_leaves_no_temp_file_behind(self):
        from bridget_core.statefile import write_state
        write_state(self.d / 'x.json', '{}')
        self.assertEqual([p.name for p in self.d.iterdir()], ['x.json'])

    def test_a_directory_write_state_creates_is_owner_only(self):
        from bridget_core.statefile import write_state
        p = self.d / 'fresh' / 'x.json'
        write_state(p, '{}')
        self.assertEqual(self._mode(p.parent), 0o700)

    def test_an_existing_directory_is_left_alone(self):
        """~/.pogo is pogo's, not bridget's. install.sh tightens it where the
        user can see it happen; the library does not re-permission it silently."""
        from bridget_core.statefile import write_state
        os.chmod(self.d, 0o755)
        write_state(self.d / 'x.json', '{}')
        self.assertEqual(self._mode(self.d), 0o755)

    def test_every_state_store_lands_at_0600(self):
        """The end-to-end claim, through the real store classes."""
        from bridget_core import ConversationStore, MaildirWatcher, SettingsStore

        conv = self.d / 'conversations.json'
        ConversationStore(conv).record('k1', subject='secret subject', agent='mayor')
        self.assertEqual(self._mode(conv), 0o600)

        settings = self.d / 'settings.json'
        SettingsStore(settings).mute('k1')
        self.assertEqual(self._mode(settings), 0o600)

        seen = self.d / 'bridget.seen'
        (self.d / 'new').mkdir()
        MaildirWatcher(self.d / 'new', seen).prime()
        self.assertEqual(self._mode(seen), 0o600)

    def test_installer_tightens_the_pogo_directory(self):
        install = (REPO / 'install.sh').read_text()
        self.assertIn('chmod 700 "$POGO_DIR"', install)


class TestLicenseHeaders(unittest.TestCase):
    """A10. Every source file carries a copyright line, an SPDX tag, and the
    warranty disclaimer; a file that upstream also wrote carries both copyrights
    and the modification notice GPL-3.0 section 5(a) asks for.

    Enforced rather than documented, because the failure mode is a new file
    shipping with no notice at all — which is what happened to every file in
    bridget_core/ and tests/."""

    SOURCE_GLOBS = ('bridget', 'build.sh', 'test.sh', 'install.sh',
                    'bridget_core/*.py', 'tests/test_*.py',
                    'tests/smoke-fresh-install.py', 'tests/smoke-fresh-install.sh')

    def sources(self):
        found = []
        for pattern in self.SOURCE_GLOBS:
            found.extend(sorted(REPO.glob(pattern)))
        self.assertGreater(len(found), 15, 'sanity: globs matched suspiciously little')
        return found

    def _authors(self, path: Path) -> set:
        out = subprocess.run(['git', 'log', '--format=%aN', '--', str(path)],
                             cwd=REPO, capture_output=True, text=True, check=True).stdout
        return {a for a in out.split('\n') if a}

    @staticmethod
    def _header(path: Path) -> str:
        """The leading comment block, unwrapped into one whitespace-normalized
        line. Scanning the whole file would make this very file fail, since it
        necessarily quotes the notices it checks; normalizing means a reflowed
        comment does not."""
        lines = []
        for line in path.read_text().splitlines():
            if line.startswith('#!'):
                continue
            if not line.startswith('#'):
                break
            lines.append(line.lstrip('#').strip())
        return ' '.join(' '.join(lines).split())

    def test_every_source_file_has_an_spdx_tag(self):
        for p in self.sources():
            self.assertIn('SPDX-License-Identifier: GPL-3.0-or-later', self._header(p),
                          f'{p.relative_to(REPO)} has no SPDX tag')

    def test_every_source_file_has_a_copyright_line(self):
        for p in self.sources():
            self.assertIn('Copyright (C) 2026', self._header(p),
                          f'{p.relative_to(REPO)} has no copyright line')

    def test_every_source_file_disclaims_warranty(self):
        """The GPL appendix asks for it, and the one-line 'see LICENSE' pointer
        the repo used to carry was not it."""
        for p in self.sources():
            self.assertIn('WITHOUT ANY WARRANTY', self._header(p),
                          f'{p.relative_to(REPO)} has no warranty disclaimer')

    def test_an_upstream_file_carries_both_copyrights_and_a_modification_notice(self):
        seen = 0
        for p in self.sources():
            if 'cloverross' not in self._authors(p):
                continue
            seen += 1
            head = self._header(p)
            self.assertIn('Copyright (C) 2026 Clover Ross', head,
                          f'{p.relative_to(REPO)} drops the upstream copyright')
            self.assertIn('Copyright (C) 2026 Daniel Miller', head,
                          f'{p.relative_to(REPO)} drops the fork copyright')
            self.assertIn('Modified in 2026', head,
                          f'{p.relative_to(REPO)} lacks a GPL 5(a) modification notice')
        self.assertGreater(seen, 0, 'sanity: no upstream-derived file found')

    def test_a_fork_only_file_does_not_claim_upstream_authorship(self):
        seen = 0
        for p in self.sources():
            if 'cloverross' in self._authors(p):
                continue
            seen += 1
            self.assertNotIn('Copyright (C) 2026 Clover Ross', self._header(p),
                             f'{p.relative_to(REPO)} credits upstream for a file it never saw')
        self.assertGreater(seen, 0, 'sanity: no fork-only file found')

    def test_authors_does_not_overstate_the_file_headers(self):
        """AUTHORS used to claim the bridget script carried "the canonical
        GPL-3.0-or-later notice". It carried a one-line pointer."""
        authors = (REPO / 'AUTHORS').read_text()
        self.assertNotIn('carries\nthe canonical GPL-3.0-or-later notice', authors)
        self.assertIn('SPDX-License-Identifier', authors)


class TestGitignoreCoversAnInTreeEnvFile(unittest.TestCase):
    """A9. The real env file lives at ~/.pogo/bridget.env, out of tree. But
    `cp bridget.env.example bridget.env` in the checkout is the obvious thing to
    try, and the next `git add -A` would commit a bot token."""

    def test_a_dotenv_file_in_the_checkout_is_ignored(self):
        r = subprocess.run(['git', 'check-ignore', '-q', 'bridget.env'], cwd=REPO)
        self.assertEqual(r.returncode, 0, 'bridget.env is not gitignored')

    def test_the_example_is_still_tracked(self):
        r = subprocess.run(['git', 'check-ignore', '-q', 'bridget.env.example'], cwd=REPO)
        self.assertEqual(r.returncode, 1, 'the example template must stay tracked')
        self.assertIn(REPO / 'bridget.env.example', tracked_files())


if __name__ == '__main__':
    unittest.main(verbosity=2)
