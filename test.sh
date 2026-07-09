#!/usr/bin/env bash
# Light test suite: a parse check plus the env-defaults regression test.
# The env-defaults test stubs `discord` so it runs without venv-bridget.
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
# Shells out to the real mg; self-skips when the mg on PATH lacks correlation
# IDs. Hand-authored References fixtures cannot catch a thread that splits on
# the second hop — only mg writes those headers the way mg writes them.
python3 tests/test_mg_threading.py
echo "test.sh: ok"
