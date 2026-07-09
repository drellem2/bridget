#!/usr/bin/env bash
# bridget's full test suite. GPL-3.0-or-later. See LICENSE.
#
# Every tests/ entry point is invoked from here. If you add one, add it here —
# a suite nothing runs is a suite that passes.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
python3 -m py_compile bridget
python3 -m py_compile bridget_core/*.py
bash -n install.sh
python3 tests/test_core.py
python3 tests/test_env_defaults.py
python3 tests/test_channels.py
python3 tests/test_threading.py
python3 tests/test_secrets.py
# Actually executes install.sh against a throwaway $HOME (--no-venv skips the
# one step that needs the network). Source-greps cannot see a symlink, a 0600,
# or the --setup awk rewrite.
python3 tests/test_install.py
# Shells out to the real mg; self-skips when the mg on PATH lacks correlation
# IDs. Hand-authored References fixtures cannot catch a thread that splits on
# the second hop — only mg writes those headers the way mg writes them.
python3 tests/test_mg_threading.py
# Drives handle_command on a fresh install. Needs the real discord module, so it
# self-skips when ~/.pogo/venv-bridget is absent.
bash tests/smoke-fresh-install.sh
echo "test.sh: ok"
