# Contributing to bridget

## Tracking work

bridget uses [mg](https://github.com/drellem2/macguffin) as the maintainer's
local work tracker — bugs, ideas, designs, and tasks all live there. Because
external contributors don't have access to that local state, bug and roadmap
state is mirrored into in-repo markdown so the README is self-contained:
[ROADMAP.md](ROADMAP.md) tracks v2 priorities and
[KNOWN_BUGS.md](KNOWN_BUGS.md) lists currently-broken behaviors.

## PRs that change roadmap or known-bug state

If your PR adds, changes, dispatches, or closes a bug or roadmap item, update
`KNOWN_BUGS.md` and/or `ROADMAP.md` in the same PR — don't leave the in-repo
copies out of sync with the change. The PR template's checkbox is the
enforcement surface; please tick it (or note that the PR doesn't touch
roadmap/bug state) before requesting review.

## Style and testing

bridget is a single-file Python script targeting Python 3.10+. Match the
existing style in `bridget` — there's no separate formatter or linter
configured. Before opening a PR, run `./test.sh` from the repo root; it's a
`py_compile` smoke check that catches import-time syntax errors. There's no
real test suite yet, so manual verification against a live Discord bot is
expected for behavior changes.

### Temp directories in tests

Tests must not call `tempfile.mkdtemp()` or `tempfile.TemporaryDirectory()`
directly — use `testtmp.mkdtemp('<purpose>')` and
`testtmp.TemporaryDirectory('<purpose>')` from [tests/testtmp.py](tests/testtmp.py),
and `testtmp.rmtree()` instead of `shutil.rmtree(..., ignore_errors=True)`.
`tests/tmpdir-leak_test.sh` fails the build if you don't.

The reason is not tidiness. A teardown paired with each `mkdtemp` is skipped by
exactly the runs that need it — a crash, a harness timeout, a kill — so the
directories accumulate fastest when tests fail, which is when they are run most.
Before this convention one `./test.sh` left **444** directories in `$TMPDIR`;
they are what took this machine to 100% capacity, where every merge gate died
with `Errno 28` and looked like a defect in whichever branch happened to be
running. `testtmp` nests every fixture under one swept root and reclaims by
**pid ownership**, so recovery does not depend on the leaking process running any
code. The module docstring has the full reasoning.

After `./test.sh` passes, you can also run
`./tests/smoke-fresh-install.sh` to exercise every Discord command
path against a fresh-install env fixture. It boots bridget with only
the three required Discord vars set, calls `handle_command` for
each command, and asserts that no result matches the deleted
"is unavailable: set ..." config-error pattern. Recommended
before merging anything that touches `handle_command` or
`load_config`.
