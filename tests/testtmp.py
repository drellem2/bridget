#!/usr/bin/env python3
# Copyright (C) 2026 Daniel Miller
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Written for this fork of cloverross/bridget; not present upstream.
#
# bridget is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. bridget is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
# You should have received a copy of the GNU General Public License along with
# bridget. If not, see <https://www.gnu.org/licenses/>.

"""The temp directories bridget's TEST HARNESS creates for itself.

Ported from pogo's internal/testtmp (mg-de3c) after that fix measured that the
remaining $TMPDIR leak on this box was not pogo's: pogo's own prefixes stop on
2026-08-12 ~14:30, 2,451 directories leaked that day were somebody else's, and
`bridget-thread-test-*` was among the named producers. Measured here on
2026-08-13, before this module: 184 `bridget-thread-test-*` directories in one
$TMPDIR, plus a dozen other `bridget-*` prefixes, every one of them a fixture
nothing would ever delete.

# Why a module and not a `finally:`

The natural repair is to pair each `tempfile.mkdtemp()` with a teardown, and it
does not hold. Three findings from p60eb, all of which survive the port to
Python:

1.  **Cleanup that hangs off the end of a run is skipped exactly when runs
    fail.** Go's version of this was `os.RemoveAll` after `m.Run()`, skipped by
    a panic, a `-timeout` expiry or a kill. Python's version is
    `unittest.main()`, which calls `sys.exit` — `atexit` handlers do run there,
    but nothing runs under SIGKILL, and a suite killed by a harness timeout is
    the common case, not the exotic one. Tests are killed when they hang, which
    is when they are being run most.

2.  **Helpers exit past their own cleanup on the failure arm.** A loader that
    builds a fake `$HOME` and then `sys.exit(1)`s on a bad probe leaks it every
    time it reports the failure it exists to report.

3.  **The one worth reading twice.** `shutil.rmtree(..., ignore_errors=True)` —
    which is what four of bridget's teardowns said — stops at the first
    unremovable entry and says nothing. A tree containing a read-only directory
    (Go's module cache writes 0444 files inside 0555 directories; a fake `$HOME`
    collects one the moment a test shells out to `go build`) is not removable by
    an ordinary rmtree at all: the file mode is irrelevant, unlink needs write
    on the PARENT. So the largest thing in the nest was never reclaimed, and the
    ignored error is why nothing said so.

So the recovery cannot depend on the leaking process running any code. It
depends on the NEXT process, which is why this module sweeps on the way in
rather than tidying on the way out.

# What it does

One directory in $TMPDIR — `root()` — with every harness directory nested
inside it, named for the process that owns it. That alone fixes the shape:
$TMPDIR's entry count stops growing with the number of test runs and becomes 1.

Nesting alone would not bound the disk, so the root is swept. `reap()` runs once
per process, on the first `mkdtemp()` call, and the rule it applies is
OWNERSHIP:

  - the name encodes a pid and that process is alive — keep, at any age;
  - the name encodes a pid and that process is gone — remove;
  - the name encodes no pid — remove once it is older than STALE_AFTER.

Ownership rather than age, because age is the reading that gets this wrong in
the expensive direction. This box runs several polecats and a refinery gate
concurrently, and bridget's own `./test.sh` runs twenty-odd python processes in
sequence — at any instant a sibling entry is very likely a LIVE run's fixtures.
A sweep that deleted those would surface as a branch defect, which is the
failure this module exists to stop, arriving by a new route. A pid answers "is
anyone still using this" directly, and signal 0 answers it without a race worth
naming: a process that dies between the listing and the removal needed the
directory right up until it didn't.

The cost is that a crashed run's fixtures are gone by the next `./test.sh`
rather than sitting in $TMPDIR for a while. That is the intended trade: the
instrument for a failed test is its output, and keeping unowned state around on
the chance someone looks is the habit that produced the full disk.

# What it deliberately does not do

It does not touch anything outside `root()`. It has no opinion about $TMPDIR at
large — reclaiming what has already leaked is a separate, careful operation from
stopping the leak, and this module is the second one. In particular the 1,242
`tmp.*` directories of unknown provenance are not ours to delete.
"""
import errno
import itertools
import os
import shutil
import stat
import sys
import time
from pathlib import Path

# The single directory this module owns inside $TMPDIR.
#
# Short and unmistakably ours: a human staring at an `ls $TMPDIR` that has gone
# wrong should be able to tell in one line whether bridget is the cause. It
# keeps the `bridget-` prefix every leaked fixture had, so the same one-line
# grep that measured the leak measures the fix.
ROOT_NAME = 'bridget-test-tmp'

# How long an entry whose name encodes NO pid must have been idle before reap()
# will remove it.
#
# It is the fallback rule, not the main one — every name this module writes
# carries a pid, so an entry reaching this branch was put here by something else
# or by a version of this module that predates the naming. Two hours, against a
# full `./test.sh` that measures in minutes; the margin is caution about the
# mtime reading, which does not advance for writes NESTED inside a directory,
# only for entries created or removed directly in it.
STALE_AFTER = 2 * 60 * 60

_seq = itertools.count(1)
_root_cache = None
_reaped = False


def root() -> Path:
    """The directory this module owns inside $TMPDIR, created on first use.

    Resolved once per process. $TMPDIR is read at that moment, so a test that
    pins TMPDIR must do so before the first mkdtemp() call.
    """
    global _root_cache
    if _root_cache is not None:
        return _root_cache
    path = Path(tempdir()) / ROOT_NAME
    # islink, and it runs BEFORE mkdir rather than instead of it. $TMPDIR is
    # per-user and 0700 on darwin, but Python falls back to a world-writable
    # /tmp when TMPDIR is unset — which is the case in CI — and there a
    # pre-planted symlink at this name would have the sweep deleting a directory
    # tree of somebody else's choosing. mkdir(exist_ok=True) follows the link and
    # reports success, so the refusal has to be explicit.
    if path.is_symlink():
        raise RuntimeError(
            f'testtmp: {path} is a symlink; refusing to create or sweep through it')
    # 0o700: these hold fixtures that stand in for a user's $HOME, their
    # bridget.env (which carries a bot token) and their mail.
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _root_cache = path
    return path


def tempdir() -> str:
    """$TMPDIR, or the platform default.

    Read through os.environ on every call rather than through tempfile's
    module-level cache, because tempfile resolves the directory ONCE per process
    and a test that pins TMPDIR after any other code has already made a temp file
    would silently get the old value. The leak guard pins TMPDIR per invocation,
    so this distinction is the difference between measuring the pinned directory
    and measuring the developer's own.
    """
    for var in ('TMPDIR', 'TEMP', 'TMP'):
        value = os.environ.get(var)
        if value:
            return value
    return '/tmp'


def mkdtemp(purpose: str) -> Path:
    """Create and return a directory private to this process, named for purpose.

    Drop-in for `tempfile.mkdtemp(prefix=...)`, with two differences that are the
    entire point: the directory is nested inside root() instead of sitting
    directly in $TMPDIR, and it is reclaimed by the next run rather than never.

    purpose is a short label naming what the directory holds ('threading',
    'dedup'), not a path: it appears verbatim in the name, so it must contain no
    dot and no separator. An unparseable name is one reap() can only age out,
    which silently converts a pid-owned entry into a two-hour one — so such a
    label is rejected rather than written.

    The first call in a process also runs reap(). That is the only sweep
    trigger: it costs one listing of a directory holding, at steady state, the
    live test processes on this box.
    """
    global _reaped
    if not purpose:
        raise ValueError('testtmp: empty purpose')
    if any(c in purpose for c in ('.', '/', os.sep)):
        raise ValueError(
            f'testtmp: purpose {purpose!r} must not contain a dot or a separator')
    parent = root()
    if not _reaped:
        _reaped = True
        reap(parent)

    path = parent / entry_name(purpose, os.getpid(), next(_seq))
    # mkdir without exist_ok, because "it already exists" has to be an answer
    # here rather than a success.
    #
    # pids are reused. The sweep keeps any entry whose pid is alive, so once this
    # process has been given a recycled pid, the DEAD namesake's directory reads
    # as live and is kept — and exist_ok would then hand this run a fake $HOME
    # belonging to a run that ended days ago, whose stale mail and settings would
    # look like a defect in whatever test read them. It cannot be a directory
    # this process made: the counter is monotonic and this value has not been
    # issued before. So it is the namesake's, its owner is provably gone, and
    # clearing it is the one correct move.
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        rmtree(path)
        path.mkdir(mode=0o700)
    return path


def entry_name(purpose: str, pid: int, n: int) -> str:
    """The on-disk name reap() parses back.

    purpose first so `ls` sorts by what a directory IS, which is what a human
    reading a swollen root wants grouped; pid second so ownership is one field
    away rather than a search.
    """
    return f'{purpose}.{pid}.{n}'


def owner_pid(name: str):
    """The pid encoded in an entry name, or None when the name is not ours."""
    parts = name.split('.')
    if len(parts) != 3:
        return None
    try:
        pid = int(parts[1])
    except ValueError:
        return None
    return pid if pid > 0 else None


def pid_alive(pid: int) -> bool:
    """Whether pid names a live process.

    Signal 0 is the portable existence probe: it delivers nothing and reports
    whether it COULD have. EPERM is a live process owned by another user and is
    therefore an ALIVE answer, not an error — reading it as "gone" is how a sweep
    deletes a directory belonging to something it merely cannot signal.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as e:
        return e.errno != errno.ESRCH
    return True


def reap(parent=None) -> None:
    """Remove entries in parent that no live process owns.

    See the module docstring for the rule and why it is ownership rather than
    age. A removal that fails is REPORTED on stderr and then dropped: a sweep
    that cannot delete something has lost nothing the caller can act on, and it
    must never be the reason a test fails — but finding 3 above is precisely
    what silence buys, so it does not get to be silent.

    Takes its root as an argument so the behaviour that matters — that a LIVE
    owner's entry survives — can be observed against a fixture root.
    """
    parent = root() if parent is None else Path(parent)
    try:
        entries = sorted(os.listdir(parent))
    except OSError:
        return
    cutoff = time.time() - STALE_AFTER
    for name in entries:
        path = parent / name
        pid = owner_pid(name)
        if pid is not None:
            if not pid_alive(pid):
                _reap_one(path)
            continue
        try:
            if path.lstat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        _reap_one(path)


def _reap_one(path: Path) -> None:
    try:
        rmtree(path)
    except OSError as e:
        print(f'testtmp: could not reclaim {path}: {e}', file=sys.stderr)


def rmtree(path) -> None:
    """Remove a tree, including one an ordinary rmtree cannot.

    Two differences from `shutil.rmtree(..., ignore_errors=True)`, which is what
    this replaces:

    It makes directories writable as it goes. A read-only DIRECTORY (0555) is
    what stops a removal, not a read-only file — unlink is authorised by the
    parent directory's mode, so a tree of 0444 files inside 0755 directories
    deletes fine while one 0555 directory halts the whole walk. Go's module
    cache is written exactly that way, and a fake $HOME collects one under
    go/pkg/mod the first time a test shells out to `go build`; that is how the
    largest thing in a leaked nest came to be the part that never got reclaimed.

    And it RAISES. A teardown whose failure is invisible is the thing that let
    this grow to a full disk with nothing on the host reporting it.
    """
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return
    # Top-down: descending needs +x, which even a 0555 directory grants; what is
    # missing is +w on the directory whose entries are about to be unlinked.
    for dirpath, dirnames, _ in os.walk(path, topdown=True):
        _make_writable(dirpath)
        for name in dirnames:
            child = os.path.join(dirpath, name)
            if not os.path.islink(child):
                _make_writable(child)
    shutil.rmtree(path)


def _make_writable(path) -> None:
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return
    want = mode | stat.S_IRWXU
    if want != mode:
        try:
            os.chmod(path, want)
        except OSError:
            pass


class TemporaryDirectory:
    """`tempfile.TemporaryDirectory`, nested under root() and reaped.

    Drop-in for the stdlib class in both the shapes this repo uses it: as a
    context manager yielding a path string, and as an object held on a test case
    with `.name` and `.cleanup()` registered through `addCleanup`.

    Unlike the stdlib class it does not warn-and-clean from a finalizer, because
    that finalizer is exactly the mechanism that does not run under a kill. The
    guarantee here is the sweep, and cleanup() is the fast path.
    """

    def __init__(self, purpose: str = 'tmp'):
        self.name = str(mkdtemp(purpose))
        self._cleaned = False

    def cleanup(self) -> None:
        if not self._cleaned:
            self._cleaned = True
            rmtree(self.name)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, *exc) -> bool:
        self.cleanup()
        return False

    def __repr__(self) -> str:
        return f'<testtmp.TemporaryDirectory {self.name!r}>'
