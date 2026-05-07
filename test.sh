#!/usr/bin/env bash
# No real test suite yet — just a smoke check that the script parses.
# Compiling proves no import-time SyntaxError; runtime config errors are
# verified separately by running bridget with no env file present.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
python3 -m py_compile bridget
echo "test.sh: ok (py_compile passed)"
