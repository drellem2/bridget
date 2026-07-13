#!/usr/bin/env bash
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

# bridget's full test suite.
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
# The task-transition diff. Guards the DM flood: `mg list --json --all` emits
# some ids twice (live + archived tombstone), and a line-by-line diff
# re-announced them on every single poll.
python3 tests/test_task_transitions.py
# The inbound send path (mg-3f94): a message the human types — as a `mail`/
# `idea:`/`bug:` DM or a mapped-channel chat — reaches the agent's `--body`
# verbatim, while every label cut from it (the ack echo, the mg title) carries
# a visible '…'. Stubs discord; runs under system python3.
python3 tests/test_dm_echo.py
# The read-only 'on your plate' view (mg-3358): `mine` renders
# `mg list --assignee=human` into Discord, separating outstanding from resolved,
# and — the conservative-first-cut guarantee — mutates nothing. Stubs discord.
python3 tests/test_assigned_view.py
# The watch_task_transitions silent-death fix (mg-3499): a single transient
# `mg list` timeout must not kill the watcher thread, and a liveness heartbeat
# whose mtime ticks every cycle must go stale only when the watcher is truly
# dead. Injects the timeout and kills a real watcher BY PID; no live Discord.
python3 tests/test_watcher_liveness.py
# bridget-supervise + the launchd plist template. Calls no launchctl, so it
# runs on Linux too.
python3 tests/test_launchd.py
python3 tests/test_secrets.py
# Drives the real script under a venv that provably lacks discord.py, so the
# re-exec into ~/.pogo/venv-bridget is exercised, not just described.
python3 tests/test_venv_reexec.py
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
