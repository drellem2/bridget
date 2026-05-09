#!/usr/bin/env python3
"""Smoke test: bridget with only required Discord env vars set.

Asserts no command emits the "is unavailable: set ..." config-error
pattern that mg-26f7 removed. Re-introducing such a branch would fail
this test.
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
    (env_dir / 'bridget.env').write_text(
        'DISCORD_BOT_TOKEN=fake-token-for-smoke-test\n'
        'DISCORD_USER_ID=1\n'
        'DISCORD_SERVER_ID=2\n'
    )

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
