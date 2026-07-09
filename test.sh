#!/usr/bin/env bash
# Light test suite: a parse check plus the env-defaults regression test.
# The env-defaults test stubs `discord` so it runs without venv-bridget.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
python3 -m py_compile bridget
python3 tests/test_core.py
python3 tests/test_env_defaults.py
python3 tests/test_channels.py
python3 tests/test_threading.py
echo "test.sh: ok"
