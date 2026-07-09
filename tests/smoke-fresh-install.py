#!/usr/bin/env python3
# Copyright (C) 2026 Clover Ross
# Copyright (C) 2026 Daniel Miller
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Modified in 2026 by Daniel Miller, whose fork this is. What changed and
# when is recorded in AUTHORS and CHANGELOG.md (GPL-3.0 section 5(a)).
#
# bridget is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. bridget is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
# You should have received a copy of the GNU General Public License along with
# bridget. If not, see <https://www.gnu.org/licenses/>.

"""Smoke test: every command works on a freshly-installed bridget.

Drives `handle_command` against a throwaway $HOME holding nothing but the three
required Discord keys — the state a user is in the moment install.sh finishes.
Asserts no command emits the "is unavailable: set ..." config-error pattern that
mg-26f7 removed. Re-introducing such a branch would fail this test.

This does **not** run install.sh; it fabricates the state install.sh leaves
behind. `tests/test_install.py` is what actually executes the installer. The two
are complements: that one checks the installer produces this state, this one
checks bridget is usable once it has.

Needs the real `discord` module, so it runs out of the bridget venv via
`smoke-fresh-install.sh` and skips when that venv is absent.
"""
import importlib.util
import os
import re
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / 'bridget'

CONFIG_ERROR_RE = re.compile(r'is unavailable.*set', re.IGNORECASE)


def main() -> int:
    fake_home = Path(tempfile.mkdtemp(prefix='bridget-smoke-'))
    os.environ['HOME'] = str(fake_home)
    env_dir = fake_home / '.pogo'
    env_dir.mkdir(parents=True)
    env_file = env_dir / 'bridget.env'
    env_file.write_text(
        'DISCORD_BOT_TOKEN=fake-token-for-smoke-test\n'
        'DISCORD_USER_ID=1\n'
        'DISCORD_SERVER_ID=2\n'
    )
    # As install.sh leaves it — otherwise bridget correctly warns about a
    # world-readable token file and the warning drowns the smoke output.
    os.chmod(env_file, 0o600)

    # bridget has no .py extension, so spec_from_file_location can't infer
    # a loader — pass SourceFileLoader explicitly.
    loader = SourceFileLoader('bridget', str(SCRIPT))
    spec = importlib.util.spec_from_loader('bridget', loader)
    bridget = importlib.util.module_from_spec(spec)
    loader.exec_module(bridget)

    cases = [
        'help', '?', 'commands',
        'next mg-deadbeef',
        'idea: smoke test',
        'bug: smoke test',
        'read mg-deadbeef',
        'dismiss mg-deadbeef',
        'mail smoke subject\nsmoke body',
        'status',
        'agents',
    ]

    failures = []
    for cmd in cases:
        out = bridget.handle_command(cmd)
        if CONFIG_ERROR_RE.search(out):
            failures.append((cmd, out))

    if failures:
        print('SMOKE FAILED — config-error patterns found:')
        for cmd, out in failures:
            print(f'  {cmd!r}\n    -> {out!r}')
        return 1

    print(f'SMOKE OK: {len(cases)} commands exercised, '
          f'no config-error patterns matched.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
