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

"""bridget's shebang is `/usr/bin/env python3`, so every documented way to run
it — the ~/.pogo/bin/bridget symlink, the launchd plist, the systemd unit —
lands on the *system* interpreter, which has no discord.py. It lives in the venv
install.sh builds. bridget therefore re-execs itself into that interpreter.

These tests drive the real script in a subprocess under an interpreter that
provably lacks discord.py, so the re-exec is exercised rather than described.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / 'bridget'

# The stub stands in for discord.py. It exits during import: bridget's module
# body runs `client.run(TOKEN)` under `__main__`, and this test has no business
# reaching Discord. Exiting here proves the two things the re-exec must achieve
# — the import resolved, and it resolved in the interpreter we handed off to.
STUB_DISCORD = '''\
import os
import sys
print('STUB-IMPORTED', sys.executable, os.environ.get('BRIDGET_VENV_REEXEC', ''))
sys.stdout.flush()
os._exit(7)
'''

STUB_EXIT_CODE = 7


class VenvReexecTest(unittest.TestCase):
    """Subprocess tests against a discord-less interpreter."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='bridget-reexec-'))

        # An interpreter that definitely cannot import discord. --without-pip
        # keeps this offline, like the rest of the suite.
        cls.clean_venv = cls.tmp / 'clean-venv'
        subprocess.run(
            [sys.executable, '-m', 'venv', '--without-pip', str(cls.clean_venv)],
            check=True, capture_output=True,
        )
        cls.clean_python = cls.clean_venv / 'bin' / 'python3'

        # Guard against the premise silently rotting: if this interpreter can
        # import discord, every assertion below would pass vacuously.
        probe = subprocess.run(
            [str(cls.clean_python), '-c', 'import discord'],
            capture_output=True, text=True,
        )
        assert probe.returncode != 0, 'clean venv unexpectedly has discord.py'

        # A stand-in for ~/.pogo/venv-bridget: same interpreter, but with the
        # discord stub on its path.
        cls.stub_dir = cls.tmp / 'stub'
        cls.stub_dir.mkdir()
        (cls.stub_dir / 'discord.py').write_text(STUB_DISCORD)

        cls.fake_venv = cls.tmp / 'venv-bridget'
        (cls.fake_venv / 'bin').mkdir(parents=True)
        wrapper = cls.fake_venv / 'bin' / 'python3'
        wrapper.write_text(
            '#!/bin/sh\n'
            f'PYTHONPATH="{cls.stub_dir}" exec "{cls.clean_python}" "$@"\n'
        )
        wrapper.chmod(0o755)

        # A venv-shaped directory whose interpreter still cannot import discord.
        cls.broken_venv = cls.tmp / 'broken-venv'
        (cls.broken_venv / 'bin').mkdir(parents=True)
        broken = cls.broken_venv / 'bin' / 'python3'
        broken.write_text(f'#!/bin/sh\nexec "{cls.clean_python}" "$@"\n')
        broken.chmod(0o755)

        cls.fake_home = cls.tmp / 'home'
        (cls.fake_home / '.pogo').mkdir(parents=True)
        env_file = cls.fake_home / '.pogo' / 'bridget.env'
        env_file.write_text(
            'DISCORD_BOT_TOKEN=not-a-real-token\n'
            'DISCORD_USER_ID=1\n'
            'DISCORD_SERVER_ID=2\n'
            # Keep load_config hermetic: it dies if it cannot resolve mg.
            'MG_BIN=/bin/echo\n'
        )
        env_file.chmod(0o600)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_bridget(self, **env_overrides):
        env = dict(os.environ)
        env.pop('BRIDGET_VENV_REEXEC', None)
        env.pop('PYTHONPATH', None)
        env['HOME'] = str(self.fake_home)
        env.update(env_overrides)
        return subprocess.run(
            [str(self.clean_python), str(SCRIPT)],
            capture_output=True, text=True, env=env, timeout=60,
        )

    def test_reexecs_into_venv_when_discord_missing(self):
        """The whole point: an interpreter without discord.py hands off to one
        that has it, and the import then succeeds."""
        r = self.run_bridget(BRIDGET_VENV_DIR=str(self.fake_venv))
        self.assertEqual(r.returncode, STUB_EXIT_CODE, r.stderr)
        self.assertIn('STUB-IMPORTED', r.stdout)
        # The guard is set in the child, proving we arrived by re-exec and not
        # because the first interpreter could import discord after all.
        self.assertTrue(r.stdout.strip().endswith('1'), r.stdout)

    def test_dies_when_no_venv_interpreter(self):
        r = self.run_bridget(BRIDGET_VENV_DIR=str(self.tmp / 'does-not-exist'))
        self.assertEqual(r.returncode, 1)
        self.assertIn('no venv interpreter', r.stderr)

    def test_guard_stops_an_exec_loop(self):
        """A venv that exists but lacks discord.py must fail once, not spin."""
        r = self.run_bridget(
            BRIDGET_VENV_DIR=str(self.broken_venv),
            BRIDGET_VENV_REEXEC='1',
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn('still not importable', r.stderr)

    def test_broken_venv_execs_exactly_once(self):
        """Without the guard pre-set, bridget execs into the broken venv, which
        re-runs the script; the guard must then stop it rather than loop."""
        r = self.run_bridget(BRIDGET_VENV_DIR=str(self.broken_venv))
        self.assertEqual(r.returncode, 1)
        self.assertIn('still not importable', r.stderr)


class DiscordImportableTest(unittest.TestCase):
    """discord_importable() must honor a sys.modules stub. Every other suite
    injects one there, and find_spec does not consult sys.modules — miss this
    and importing bridget under test re-execs the test runner into a venv."""

    def test_sys_modules_stub_is_honored(self):
        fake_home = Path(tempfile.mkdtemp(prefix='bridget-importable-'))
        (fake_home / '.pogo').mkdir(parents=True)
        env_file = fake_home / '.pogo' / 'bridget.env'
        env_file.write_text(
            'DISCORD_BOT_TOKEN=fake\n'
            'DISCORD_USER_ID=1\n'
            'DISCORD_SERVER_ID=2\n'
            'MG_BIN=/bin/echo\n'
        )
        env_file.chmod(0o600)

        saved_home = os.environ.get('HOME')
        saved_discord = sys.modules.get('discord')
        saved_bridget = sys.modules.pop('bridget', None)
        os.environ['HOME'] = str(fake_home)

        fake_discord = mock.MagicMock()
        fake_discord.Intents.default.return_value = mock.MagicMock()
        sys.modules['discord'] = fake_discord
        try:
            loader = SourceFileLoader('bridget', str(SCRIPT))
            spec = importlib.util.spec_from_loader('bridget', loader)
            bridget = importlib.util.module_from_spec(spec)
            # If the stub were missed, this exec_module would os.execv away and
            # the test process would never return here.
            loader.exec_module(bridget)
            self.assertTrue(bridget.discord_importable())
        finally:
            if saved_home is None:
                os.environ.pop('HOME', None)
            else:
                os.environ['HOME'] = saved_home
            if saved_discord is not None:
                sys.modules['discord'] = saved_discord
            else:
                sys.modules.pop('discord', None)
            if saved_bridget is not None:
                sys.modules['bridget'] = saved_bridget
            else:
                sys.modules.pop('bridget', None)
            shutil.rmtree(fake_home, ignore_errors=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
