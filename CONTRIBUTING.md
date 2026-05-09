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
