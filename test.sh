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
# The harness's own temp directories (mg-1f20). tests/testtmp.py nests every
# fixture under ONE swept root and reaps by pid ownership, because the teardown
# that would otherwise do it is skipped by exactly the runs that need it: a
# panic, a harness timeout, a kill. Before it, one ./test.sh left 444
# directories in $TMPDIR — 209 of them bridget-thread-test-* — and this box
# reached 100% capacity with every merge gate dying on Errno 28.
python3 tests/test_testtmp.py
python3 tests/test_core.py
python3 tests/test_env_defaults.py
# The representative relay's INBOUND seam (mg-65d2). Outbound agent mail routes
# through a representative crew agent; Daniel's replies do not — they are his own
# words. So the representative gets a marked COPY, filed to its own box rather
# than to `human` (which a fail-open deadman watches, and would notify his own
# words back at him). Off by default; a failed copy never fails the relay.
python3 tests/test_representative_copy.py
python3 tests/test_channels.py
python3 tests/test_threading.py
# The live-thread bound (mg-27e0). bridget opened one Discord thread per
# conversation and closed none: 966 accumulated in ONE channel, an unbounded
# population worth capping on its own merits. (mg-27e0 also blamed that count
# for Daniel's client failing to render #log; mg-2ab2 refuted that — the count
# was 971 and rising when it came back. The bound is hygiene, not that fix.)
# Proves the cap holds at BOTH ways into the open set (a fresh create, and a wake out of
# the archive — bounding only creation leaves the busier path unbounded), that a
# concurrent burst cannot outrun it, that a restart cannot re-inflate past it,
# and that eviction archives rather than deletes so an evicted conversation
# reopens its own thread. Carries a pre-fix control that reproduces the
# unbounded population with the cap switched off. Stubs discord.
python3 tests/test_thread_cap.py
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
# The approval scan's own directory (mg-18bf): the scan and the DM watcher were
# one variable, so step 4 of the representative cutover — which re-points
# POGO_MAIL_DIR at the representative's output box because the watcher MUST move
# — dragged the scan onto a box of REWRITTEN subjects that BRIDGET_APPROVAL_RE
# cannot match by construction. "Awaiting your approval" would have read zero and
# looked exactly like an empty plate. Carries a pre-fix control that reproduces
# the silent zero, pins that a ROOT move still carries the scan while a RECIPIENT
# re-point does not, and proves the three zero-approval worlds now render
# differently. Stubs discord.
python3 tests/test_approval_scan.py
# The watch_task_transitions silent-death fix (mg-3499): a single transient
# `mg list` timeout must not kill the watcher thread, and a liveness heartbeat
# whose mtime ticks every cycle must go stale only when the watcher is truly
# dead. Injects the timeout and kills a real watcher BY PID; no live Discord.
python3 tests/test_watcher_liveness.py
# The delivery-wedge resilience fix (mg-e5b8): a STORM of `mg list` timeouts must
# not stop outbound delivery (mg now runs off the event loop, so a hung mg cannot
# starve the delivery watcher), and a NEW delivery-liveness heartbeat must go
# stale the moment mail stops reaching Discord — the ~70h wedge the loop
# heartbeat was blind to. Storms mg, wedges delivery; no live Discord.
python3 tests/test_delivery_liveness.py
# The delivery path's positive record (mg-7c1b): bridget.log recorded delivery
# only when it went wrong, so a dead relay and an idle one produced a
# byte-identical zero — and two agents drew conclusions from `grep -c` returning
# 0 against it on one night, one of them false. Proves the "relayed N since T"
# beat appears on a real relay AND on a healthy idle, is absent when the loop is
# stopped and when it turns with every send failing, and is bounded in volume.
# Carries a pre-fix control that reproduces the byte-identical zero. Greps a real
# file with real grep; stubs discord.
python3 tests/test_relay_record.py
# When a delivery outage stops being bridget's problem (mg-3f08). The 2026-08-19
# resolver wedge failed 100% of sends for eight minutes while the host resolved
# discord.com 5/5, and produced no alert, no mail, no event and no change in any
# health surface — the message stuck in its retry loop was pogod's own
# "AGENTS ARE FAILING EVERY TURN" escalation to the human. Proves the outage
# reaches TWO surfaces that are not Discord (the circularity is the whole
# point), that the escalation's own failure is itself reported, that bridget
# exits 75 for the supervisor to respawn, and that the restart budget survives
# the restarts it counts and fails CLOSED. Stubs discord, mg and pogo.
python3 tests/test_delivery_escalation.py
# The RECEIVING half's instrument (mg-8961). The inbound path had ZERO log
# statements anywhere — on_message, handle_command, reply_in_conversation,
# handle_channel_message — so a message from Daniel that arrived, was handled,
# was refused, or was ignored all wrote the same nothing. Two of his DMs died on
# 2026-08-19 inside the mg-3f08 resolver wedge and only a read-only Discord REST
# sweep could prove it. Proves every inbound branch now writes a receipt, that a
# handler which RAISES still leaves one, that RESUME and re-IDENTIFY read
# differently (Discord replays across the first and not the second), and that a
# bounded REST catch-up on on_ready recovers the measured loss exactly once,
# within its bounds, saying when a bound bit. Carries TWO pre-fix controls — the
# receipts off, reproducing the byte-identical silence, and the sweep off,
# reproducing the loss. Stubs discord; no live Discord, no live mg.
python3 tests/test_inbound_record.py
# The duplicate-watcher fix (mg-dc94): `on_ready` fires on every gateway
# RECONNECT, and used to spawn a fresh watcher set each time with no teardown —
# seven sets accumulated and each delivered the same mail, so one mail became
# seven Discord threads. Proves the live set stays constant across reconnects,
# that retired watchers actually stop, and that the teardown is LOGGED. Carries
# a pre-fix control that reproduces N+1 delivery. Stubs discord.
python3 tests/test_watcher_idempotence.py
# The duplication limit (mg-5521): 1404 unread, one alert repeated 31 times, and
# three of the loudest rows one condition whose fire count drifted 90/91/92 —
# so the key normalises digits, and preserves mg-ids, which are identity rather
# than drift. Replays the measured subject counts through deliver_mail and
# asserts the flood shrinks; proves a first occurrence is never held and a
# FAILED send is never counted as one. Stubs discord.
python3 tests/test_dedup.py
# The undateable log (mg-35b1): every line bridget and its supervisor emit must
# carry the fleet's `[<ISO-8601 UTC>] ` prefix, because a log whose absence of a
# date match is indistinguishable from an absence of events is an instrument
# that cannot return a negative. Drives the real script to a startup failure —
# no venv, no token — and carries a pre-fix control that reproduces the zero.
python3 tests/test_log_timestamps.py
# bridget-supervise + the launchd plist template. Calls no launchctl, so it
# runs on Linux too.
python3 tests/test_launchd.py
# The activation story (mg-6ca7): bridget-supervise execs the file in the working
# checkout, on whatever branch that checkout is on — so a merged fix sat unrun
# while the merge succeeded, the MERGED mail arrived and the process stayed
# healthy. The failure was silent AND survived a restart, which is what makes it
# dangerous: "restart it" confirmed the wrong state. Proves the new code actually
# runs after a fast-forward, carries a pre-fix control that reproduces the stale
# run, and pins every refusal — dirty tree, unmerged commits, polecat worktree —
# as loud rather than silent. Builds real git checkouts; no network, no launchctl.
python3 tests/test_activation.py
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
# THE MEASUREMENT (mg-1f20). Everything above it asserts behaviour; this counts
# $TMPDIR before and after a run and fails on growth. It runs LAST because the
# thing it measures is what the rest of this file leaves behind, and it is here
# at all because a leak that surfaces months later as a full disk has no other
# detector — nothing on this host reported the disk until a build died.
bash tests/tmpdir-leak_test.sh
echo "test.sh: ok"
