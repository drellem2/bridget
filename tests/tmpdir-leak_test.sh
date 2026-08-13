#!/usr/bin/env bash
# Copyright (C) 2026 Daniel Miller
# SPDX-License-Identifier: GPL-3.0-or-later
#
# bridget is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. bridget is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
# You should have received a copy of the GNU General Public License along with
# bridget. If not, see <https://www.gnu.org/licenses/>.
#
# The $TMPDIR leak guard (mg-1f20). Ported from pogo's
# scripts/tmpdir-leak_test.sh (mg-de3c).
#
# THE MEASUREMENT IS THE TEST. Count $TMPDIR's entries, run a suite that creates
# fixtures, count again. That is the whole acceptance criterion, and it is the
# only detector this failure has: a leak that surfaces months later as a full
# disk is reported by nothing. On 2026-08-13 the host reached 100% capacity with
# 204Mi free and every merge gate on the box died with Errno 28 — presenting as
# a random branch defect, because the gate that dies is whichever one happens to
# be running when the disk crosses.
#
# Measured on this tree before the fix: ONE `./test.sh` left 444 directories in
# a pinned $TMPDIR, every one of them a fixture nothing would ever delete, 209
# of them `bridget-thread-test-*` — the prefix p60eb named when it measured that
# the remaining leak on this box was not pogo's.
#
# WHAT IS ASSERTED
#
#   Test 2  POSITIVE CONTROL, and it runs first. The check is a count, so "the
#           count did not grow" is worth nothing until "the count grows when
#           something leaks" has been shown by the same code.
#   Test 3  POSITIVE CONTROL for the SHAPE: the pre-fix call, verbatim, still
#           moves the count. This is what tests/*.py used to say.
#   Test 4  A COLD $TMPDIR gains EXACTLY ONE entry — testtmp's root.
#   Test 5  A WARM $TMPDIR gains NOTHING. The acceptance criterion verbatim.
#   Test 6  The sweep RECLAIMS. Nesting alone would move 444 entries one level
#           down rather than removing them, so repeated runs must not grow the
#           root's own contents either.
#   Test 7  RATCHET: no test file calls tempfile.mkdtemp / TemporaryDirectory
#           directly. The slice below cannot run every suite in a few seconds,
#           and a guard whose coverage is a hand-maintained list is one new test
#           file away from being wrong. This closes it by construction.
#   Test 8  RATCHET: no teardown ignores its own removal errors. That is
#           finding 3 — the reason the largest thing in a leaked nest was never
#           reclaimed AND nothing said so.
#
# WITH ARGUMENTS it measures THAT command instead of the slice:
#
#     tests/tmpdir-leak_test.sh ./test.sh
#
# which is the whole suite, and the number in the header above. It is not the
# default because ./test.sh runs this file, and a suite that runs itself does
# not terminate.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1" >&2; }

# The root tests/testtmp.py owns. READ from the module rather than duplicated,
# so renaming it there cannot leave this file asserting a name nothing writes.
ROOT_NAME="$(sed -n "s/^ROOT_NAME = '\(.*\)'$/\1/p" "$HERE/testtmp.py")"
if [ -z "$ROOT_NAME" ]; then
    echo "SETUP FAILURE: could not read ROOT_NAME from tests/testtmp.py" >&2
    exit 1
fi

# Between them these exercise every shape a bridget test uses to get a
# directory: the module-level fake-$HOME loader (threading, channels,
# env_defaults), the tmpdir() helper (core), and the per-case setUp (core). They
# run in about ten seconds; the ratchet in test 7 covers the rest.
SLICE=(tests/test_core.py tests/test_channels.py tests/test_env_defaults.py tests/test_threading.py)
CUSTOM=("$@")

# Everything this file writes lives here. A guard about leaked temp directories
# that leaked its own would be its own defect, so it takes the same remedy it is
# guarding rather than an exception to it: the directory comes from testtmp, is
# nested under the one swept root, and carries a pid — so the trap below is the
# fast path and the next run's sweep is the guarantee. `mktemp -d` here would
# have been one more top-level entry that survives a kill forever.
#
# The trap is armed BEFORE the first write, and the signals are named: EXIT
# alone does not fire on SIGTERM.
WORK="$(python3 -c "import sys; sys.path.insert(0, '$HERE'); import testtmp; print(testtmp.mkdtemp('leakguard'))")"
if [ -z "$WORK" ] || [ ! -d "$WORK" ]; then
    echo "SETUP FAILURE: testtmp.mkdtemp did not give this file a directory" >&2
    exit 1
fi
trap 'rm -rf "$WORK"' EXIT INT TERM HUP

# count_entries prints the number of TOP-LEVEL entries in a directory. Top-level
# is the whole point: the defect is $TMPDIR's entry count, not its depth.
count_entries() { find "$1" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' '; }

# list_entries prints them, so a failure names what appeared rather than only
# how many.
list_entries() { find "$1" -mindepth 1 -maxdepth 1 -exec basename {} \; | sort; }

# run_suite runs the fixture-creating suite with $TMPDIR pinned to its argument.
# Output is captured; a suite that did not run creates no fixtures and would
# sail through every count in this file, so a non-zero exit is reported as a
# SETUP failure rather than as a pass.
run_suite() {
    local tmp=$1 log=$2 rc=0
    (
        cd "$REPO" || exit 1
        if [ "${#CUSTOM[@]}" -gt 0 ]; then
            TMPDIR="$tmp" "${CUSTOM[@]}" || exit 1
        else
            # each file its own process, exactly as ./test.sh runs them — which
            # is the shape that matters, since the sweep is per-process
            for f in "${SLICE[@]}"; do
                TMPDIR="$tmp" python3 "$f" || exit 1
            done
        fi
    ) >"$log" 2>&1 || rc=$?
    return $rc
}

echo "=== \$TMPDIR leak guard (mg-1f20) ==="
echo "    testtmp root: $ROOT_NAME"

# --- Test 1: this file's own syntax ----------------------------------------
echo ""
echo "Test 1: Script syntax check"
if bash -n "${BASH_SOURCE[0]}" 2>/dev/null; then
    pass "tmpdir-leak_test.sh has valid bash syntax"
else
    fail "tmpdir-leak_test.sh has syntax errors"
fi

# --- Test 2: POSITIVE CONTROL — the counter detects a planted leak ----------
echo ""
echo "Test 2: POSITIVE CONTROL — the count detects a planted directory"
control="$WORK/control"
mkdir -p "$control"
before=$(count_entries "$control")
mktemp -d "$control/bridget-leaked-fixture.XXXXXX" >/dev/null
after=$(count_entries "$control")
if [ "$after" -gt "$before" ]; then
    pass "a planted directory moves the count $before -> $after"
else
    fail "the count did not move when a directory was planted ($before -> $after); every other assertion in this file is vacuous"
fi

# --- Test 3: POSITIVE CONTROL — the pre-fix call still leaks ----------------
# Test 2 proves the counter works. This proves it works against the DEFECT: the
# exact line twenty test files used to carry.
echo ""
echo "Test 3: POSITIVE CONTROL — the pre-fix \`tempfile.mkdtemp(prefix=...)\` moves it"
prefix_control="$WORK/prefix-control"
mkdir -p "$prefix_control"
before=$(count_entries "$prefix_control")
TMPDIR="$prefix_control" python3 -c \
    "import tempfile; tempfile.mkdtemp(prefix='bridget-thread-test-')" >/dev/null 2>&1
after=$(count_entries "$prefix_control")
leaked=$(find "$prefix_control" -mindepth 1 -maxdepth 1 -name 'bridget-*' | wc -l | tr -d ' ')
if [ "$after" -gt "$before" ] && [ "$leaked" -ge 1 ]; then
    pass "the pre-fix call leaks a bridget-prefixed entry ($before -> $after)"
else
    fail "the pre-fix call did not leak ($before -> $after); this file is measuring the wrong thing"
fi

# --- Test 4: a cold TMPDIR gains exactly one entry --------------------------
echo ""
echo "Test 4: a COLD \$TMPDIR gains exactly one entry, and it is the testtmp root"
cold="$WORK/cold"
mkdir -p "$cold"
if ! run_suite "$cold" "$WORK/cold.log"; then
    fail "SETUP: the fixture-creating suite did not pass, so this file measured nothing"
    tail -n 40 "$WORK/cold.log" >&2
else
    n=$(count_entries "$cold")
    if [ "$n" -eq 1 ] && [ "$(list_entries "$cold")" = "$ROOT_NAME" ]; then
        pass "one entry after a cold run, and it is $ROOT_NAME"
    else
        fail "a cold run left $n top-level entries, want exactly 1 ($ROOT_NAME):"
        list_entries "$cold" | sed 's/^/        /' >&2
    fi
fi

# --- Test 5: the acceptance criterion, verbatim -----------------------------
echo ""
echo "Test 5: a WARM \$TMPDIR is UNCHANGED by a run that creates fixtures"
before=$(count_entries "$cold")
before_bridget=$(find "$cold" -mindepth 1 -maxdepth 1 -name 'bridget-*' | wc -l | tr -d ' ')
if ! run_suite "$cold" "$WORK/warm.log"; then
    fail "SETUP: the fixture-creating suite did not pass on the warm run"
    tail -n 40 "$WORK/warm.log" >&2
else
    after=$(count_entries "$cold")
    after_bridget=$(find "$cold" -mindepth 1 -maxdepth 1 -name 'bridget-*' | wc -l | tr -d ' ')
    if [ "$after" -eq "$before" ] && [ "$after_bridget" -eq "$before_bridget" ]; then
        pass "entry count unchanged across a run: $before -> $after (bridget-prefixed: $before_bridget -> $after_bridget)"
    else
        fail "entry count grew across a run: $before -> $after. Entries now:"
        list_entries "$cold" | sed 's/^/        /' >&2
    fi
fi

# --- Test 6: the sweep reclaims --------------------------------------------
echo ""
echo "Test 6: repeated runs do not grow the testtmp root"
inner=$(count_entries "$cold/$ROOT_NAME")
if ! run_suite "$cold" "$WORK/sweep.log"; then
    fail "SETUP: the fixture-creating suite did not pass on the third run"
    tail -n 40 "$WORK/sweep.log" >&2
else
    grown=$(count_entries "$cold/$ROOT_NAME")
    # Not "== inner": the last process to touch the root leaves its own entry
    # behind for the next run to reap, so the steady state is a small constant
    # rather than zero. What must not happen is growth PER RUN — and the slice
    # is several processes, so the constant is bounded by their number.
    if [ "$grown" -le "$((inner + ${#SLICE[@]}))" ]; then
        pass "root contents steady across runs: $inner -> $grown"
    else
        fail "root contents grew $inner -> $grown across one run; the sweep is not reclaiming:"
        list_entries "$cold/$ROOT_NAME" | sed 's/^/        /' >&2
    fi
fi

# --- Test 7: RATCHET — nothing reaches for tempfile directly ----------------
# The slice above cannot run every suite in ten seconds, and a guard whose
# coverage is a hand-maintained list of files is one new test away from being
# wrong. This closes that by construction: the leak cannot be reintroduced
# without failing here BY NAME.
echo ""
echo "Test 7: RATCHET — no test file creates a temp directory outside testtmp"
offenders=$(cd "$REPO" && grep -n 'tempfile\.\(mkdtemp\|TemporaryDirectory\|mkstemp\|NamedTemporaryFile\)' \
    tests/*.py 2>/dev/null | grep -v '^tests/testtmp\.py:' | grep -v '^tests/tmpdir-leak' || true)
if [ -z "$offenders" ]; then
    pass "every tests/*.py directory comes from testtmp"
else
    fail "these call tempfile directly; use testtmp.mkdtemp / testtmp.TemporaryDirectory:"
    echo "$offenders" | sed 's/^/        /' >&2
fi

# --- Test 8: RATCHET — no teardown ignores its own errors -------------------
# Finding 3, made unrepeatable. `shutil.rmtree(..., ignore_errors=True)` stops
# at the first unremovable entry and says nothing, so a nest containing a
# read-only directory — Go writes its module cache 0444 inside 0555, and a fake
# $HOME collects one the moment a test shells out to `go build` — is never
# reclaimed and nothing reports it.
echo ""
echo "Test 8: RATCHET — no teardown ignores the errors from its own removal"
ignorers=$(cd "$REPO" && grep -n 'ignore_errors=True' tests/*.py 2>/dev/null \
    | grep -v '^tests/testtmp\.py:' | grep -v '^tests/test_testtmp\.py:' || true)
if [ -z "$ignorers" ]; then
    pass "no test teardown discards its own removal errors"
else
    fail "these swallow removal errors; use testtmp.rmtree, which raises:"
    echo "$ignorers" | sed 's/^/        /' >&2
fi

echo ""
echo "=== $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
