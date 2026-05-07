#!/usr/bin/env bash
# bridget has no compile step. Validate Python syntax.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
python3 -m py_compile bridget
echo "build.sh: syntax ok"
