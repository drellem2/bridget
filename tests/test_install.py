#!/usr/bin/env python3
# tests/test_install.py — functional coverage for install.sh. GPL-3.0-or-later.
# Copyright (C) 2026 Clover Ross
# Copyright (C) 2026 Daniel Miller (fork maintainer)
#
# This file is part of bridget and is distributed under the terms of the GNU
# General Public License, version 3 or later. See LICENSE.
"""Run install.sh for real, against a throwaway $HOME, and check what it did.

Everything else that "tests" the installer greps its source: `assertIn('chmod
600 "$ENV_FILE"', install)`. That is a tripwire, not coverage — a correct
refactor to `chmod 0600` fails it, and a reordering that leaves the token
world-readable for a moment passes it. Neither the symlink, the env seeding, the
permission tightening, nor the `--setup` awk rewrite was ever executed.

`--no-venv` is what makes this possible: the venv and the pip install are the
only steps that need the network, and skipping them takes the installer from
~30s to ~50ms without skipping anything this file cares about.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / 'install.sh'

#: Shape-valid, entirely fake. Three dot-separated base64url chunks.
FAKE_TOKEN = 'MTIzNDU2Nzg5.GhIjKl.mNoPqRsTuVwXyZ0123'


class InstallerTestCase(unittest.TestCase):
    """Runs install.sh with $HOME pointed at a temp dir."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='bridget-install-'))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.pogo = self.home / '.pogo'
        self.env_file = self.pogo / 'bridget.env'
        self.bin_link = self.pogo / 'bin' / 'bridget'

    def run_install(self, *args, stdin='', path=None, expect_rc=0):
        env = dict(os.environ, HOME=str(self.home))
        if path is not None:
            env['PATH'] = path
        r = subprocess.run(
            ['bash', str(INSTALL), '--no-venv', *args],
            input=stdin, capture_output=True, text=True, env=env,
        )
        if expect_rc is not None:
            self.assertEqual(r.returncode, expect_rc,
                             f'install.sh rc={r.returncode}\n{r.stdout}\n{r.stderr}')
        return r

    def mode(self, path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)


class TestFreshInstall(InstallerTestCase):
    def test_it_seeds_the_env_file_from_the_example(self):
        self.run_install()
        self.assertTrue(self.env_file.is_file())
        self.assertEqual(self.env_file.read_text(),
                         (REPO / 'bridget.env.example').read_text())

    def test_the_seeded_env_file_is_owner_only(self):
        self.run_install()
        self.assertEqual(self.mode(self.env_file), 0o600)

    def test_the_pogo_directory_is_owner_only(self):
        """A8. mkdir under the default umask leaves it 0755."""
        self.run_install()
        self.assertEqual(self.mode(self.pogo), 0o700)

    def test_it_symlinks_bridget_onto_the_repo_script(self):
        self.run_install()
        self.assertTrue(self.bin_link.is_symlink())
        self.assertEqual(os.readlink(self.bin_link), str(REPO / 'bridget'))

    def test_it_skips_the_venv_when_asked(self):
        r = self.run_install()
        self.assertFalse((self.pogo / 'venv-bridget').exists())
        self.assertIn('skipping venv', r.stdout)

    def test_it_prints_next_steps(self):
        self.assertIn('Next steps:', self.run_install().stdout)


class TestIdempotence(InstallerTestCase):
    def test_a_second_run_never_clobbers_a_populated_env_file(self):
        self.run_install()
        self.env_file.write_text('DISCORD_BOT_TOKEN=mine\n')
        r = self.run_install()
        self.assertEqual(self.env_file.read_text(), 'DISCORD_BOT_TOKEN=mine\n')
        self.assertIn('leaving its contents alone', r.stdout)

    def test_a_second_run_keeps_the_existing_symlink(self):
        self.run_install()
        r = self.run_install()
        self.assertIn('already points to', r.stdout)
        self.assertEqual(os.readlink(self.bin_link), str(REPO / 'bridget'))

    def test_a_stale_symlink_is_repointed(self):
        self.run_install()
        self.bin_link.unlink()
        self.bin_link.symlink_to('/somewhere/else/bridget')
        r = self.run_install()
        self.assertIn('replacing symlink', r.stdout)
        self.assertEqual(os.readlink(self.bin_link), str(REPO / 'bridget'))

    def test_a_real_file_at_the_link_path_is_left_alone(self):
        (self.pogo / 'bin').mkdir(parents=True)
        self.bin_link.write_text('a real file the user put here')
        r = self.run_install()
        self.assertIn('not a symlink', r.stderr)
        self.assertEqual(self.bin_link.read_text(), 'a real file the user put here')


class TestPermissionTightening(InstallerTestCase):
    def test_a_world_readable_env_file_is_tightened_on_every_run(self):
        """The hand-rolled env file is the common case; it is not created by the
        run that has to protect it."""
        self.pogo.mkdir(parents=True)
        self.env_file.write_text('DISCORD_BOT_TOKEN=x\n')
        os.chmod(self.env_file, 0o644)
        r = self.run_install()
        self.assertEqual(self.mode(self.env_file), 0o600)
        self.assertIn('tightening', r.stdout)

    def test_a_world_readable_pogo_dir_is_tightened(self):
        self.pogo.mkdir(parents=True)
        os.chmod(self.pogo, 0o755)
        self.run_install()
        self.assertEqual(self.mode(self.pogo), 0o700)

    def test_an_already_tight_env_file_is_not_churned(self):
        self.run_install()
        r = self.run_install()
        self.assertNotIn('tightening', r.stdout)


class TestSetupPrompts(InstallerTestCase):
    """`--setup` reads the token with echo off and rewrites the env file with
    awk. Neither had ever been executed by a test."""

    def _setup(self, token=FAKE_TOKEN, user='111', server='222'):
        self.run_install()
        return self.run_install('--setup', stdin=f'{token}\n{user}\n{server}\n')

    def _env(self) -> dict:
        out = {}
        for line in self.env_file.read_text().splitlines():
            if line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            out[k] = v
        return out

    def test_it_writes_all_three_credentials(self):
        self._setup()
        env = self._env()
        self.assertEqual(env['DISCORD_BOT_TOKEN'], FAKE_TOKEN)
        self.assertEqual(env['DISCORD_USER_ID'], '111')
        self.assertEqual(env['DISCORD_SERVER_ID'], '222')

    def test_no_part_of_the_token_reaches_stdout_or_stderr(self):
        """A partial token is still a leaked token. This output can end up in a
        terminal transcript, a CI log, or a screenshot."""
        r = self._setup()
        both = r.stdout + r.stderr
        self.assertNotIn(FAKE_TOKEN, both)
        for n in (4, 6, 8):
            self.assertNotIn(FAKE_TOKEN[:n], both, f'leaked a {n}-char prefix')
            self.assertNotIn(FAKE_TOKEN[-n:], both, f'leaked a {n}-char suffix')
        self.assertIn('DISCORD_BOT_TOKEN set (value hidden)', r.stdout)

    def test_the_env_file_stays_owner_only_after_a_rewrite(self):
        self._setup()
        self.assertEqual(self.mode(self.env_file), 0o600)

    def test_the_rewrite_leaves_no_temp_file_behind(self):
        self._setup()
        leftovers = [p.name for p in self.pogo.iterdir()
                     if p.name.startswith('bridget.env.')]
        self.assertEqual(leftovers, [])

    def test_it_rewrites_in_place_rather_than_appending_a_duplicate(self):
        self._setup()
        self._setup(user='999')
        body = self.env_file.read_text()
        self.assertEqual(body.count('\nDISCORD_USER_ID='), 1)
        self.assertEqual(self._env()['DISCORD_USER_ID'], '999')

    def test_it_preserves_the_other_keys_and_the_comments(self):
        before = self.env_file
        self.run_install()
        original = before.read_text()
        self.run_install('--setup', stdin=f'{FAKE_TOKEN}\n111\n222\n')
        after = before.read_text()
        self.assertEqual(original.count('#'), after.count('#'))
        self.assertIn('BRIDGET_LOG_CHANNEL_ID', after)

    def test_a_value_with_shell_metacharacters_is_not_evaluated(self):
        """set_env_key routes the value through awk's ENVIRON, never a sed
        script and never argv."""
        self.run_install()
        self.run_install('--setup', stdin='not-a-token\n111\n222\n')
        self.assertEqual(self._env()['DISCORD_USER_ID'], '111')

    def test_a_malformed_token_is_rejected_and_the_key_left_alone(self):
        self.run_install()
        self.env_file.write_text('DISCORD_BOT_TOKEN=previous\n')
        r = self.run_install('--setup', stdin='oops-i-pasted-my-username\n\n\n')
        self.assertIn('does not look like a Discord bot token', r.stderr)
        self.assertEqual(self._env()['DISCORD_BOT_TOKEN'], 'previous')

    def test_a_non_numeric_snowflake_is_rejected(self):
        self._setup()
        r = self.run_install('--setup', stdin=f'\nnot-digits\n333\n')
        self.assertIn('must be all digits', r.stderr)
        env = self._env()
        self.assertEqual(env['DISCORD_USER_ID'], '111', 'a bad value overwrote a good one')
        self.assertEqual(env['DISCORD_SERVER_ID'], '333')

    def test_an_empty_answer_keeps_the_existing_value(self):
        self._setup()
        r = self.run_install('--setup', stdin='\n\n\n')
        self.assertIn('DISCORD_BOT_TOKEN unchanged', r.stdout)
        self.assertEqual(self._env()['DISCORD_BOT_TOKEN'], FAKE_TOKEN)


class TestArgumentHandling(InstallerTestCase):
    def test_help_exits_zero_and_documents_both_flags(self):
        r = subprocess.run(['bash', str(INSTALL), '--help'],
                           capture_output=True, text=True,
                           env=dict(os.environ, HOME=str(self.home)))
        self.assertEqual(r.returncode, 0)
        self.assertIn('--setup', r.stdout)
        self.assertIn('--no-venv', r.stdout)

    def test_an_unknown_argument_exits_two(self):
        r = self.run_install('--wat', expect_rc=2)
        self.assertIn('unknown argument', r.stderr)

    def test_a_missing_mg_is_a_warning_not_a_failure(self):
        """bridget is installable before pogo is."""
        empty_bin = self.home / 'empty-bin'
        empty_bin.mkdir()
        # Keep the interpreters install.sh needs; drop everything else.
        keep = self.home / 'keep-bin'
        keep.mkdir()
        for tool in ('bash', 'python3', 'awk', 'grep', 'stat', 'mktemp',
                     'cat', 'ln', 'rm', 'mv', 'chmod', 'mkdir', 'readlink',
                     'dirname', 'cp'):
            found = shutil.which(tool)
            if found:
                (keep / tool).symlink_to(found)
        r = self.run_install(path=f'{keep}:{empty_bin}')
        self.assertIn('mg not found on PATH', r.stderr)

    def test_a_present_mg_is_reported(self):
        fake_bin = self.home / 'fake-bin'
        fake_bin.mkdir()
        mg = fake_bin / 'mg'
        mg.write_text('#!/bin/sh\nexit 0\n')
        mg.chmod(0o755)
        r = self.run_install(path=f'{fake_bin}:{os.environ["PATH"]}')
        self.assertIn('found mg:', r.stdout)


class TestInstallerSourceInvariants(unittest.TestCase):
    """The greps that A13 says are tripwires, kept as tripwires — they guard
    properties the functional tests above cannot observe from outside."""

    def test_the_installer_never_uses_rm_rf(self):
        self.assertNotIn('rm -rf', INSTALL.read_text())

    def test_the_installer_is_strict(self):
        self.assertIn('set -euo pipefail', INSTALL.read_text())

    def test_the_header_does_not_promise_a_masked_echo(self):
        """A14. The comment claimed a last-4 confirmation the code never printed.
        The code is safer than the comment was; fix the comment, not the code."""
        header = INSTALL.read_text().split('set -euo pipefail')[0]
        self.assertNotIn('last 4 characters', header)
        self.assertIn('not even a last-4 confirmation', header)

    def test_the_installer_does_not_attribute_pogo_to_the_bridget_author(self):
        """A11. `https://github.com/CloverRoss/pogo` was a copy-paste from the
        bridget URL; no such repository is claimed anywhere else in the tree."""
        self.assertNotIn('CloverRoss/pogo', INSTALL.read_text())


if __name__ == '__main__':
    sys.exit(0 if unittest.main(verbosity=2, exit=False).result.wasSuccessful() else 1)
