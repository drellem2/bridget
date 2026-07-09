#!/usr/bin/env python3
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
