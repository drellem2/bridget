#!/usr/bin/env bash
# Wrapper for tests/smoke-fresh-install.py — activates the bridget
# venv (which has discord.py) and runs the test. Skips with a clear
# message if the venv doesn't exist.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$HOME/.pogo/venv-bridget"
if [[ ! -d "$VENV" ]]; then
    echo "skip: $VENV not found — run install.sh first" >&2
    exit 0
fi
exec "$VENV/bin/python3" "$REPO/tests/smoke-fresh-install.py"
