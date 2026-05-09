# Bridget v2 Roadmap

Gaps between bridget and the retiring personal `pogo-discord-bridge` install. Compiled 2026-05-09 after the cutover.

Citations are `file:line`. "Filing line" = the exact text Clover can paste into Discord; bridget's parser handles `bug:` and `idea:` prefixes.

---

## P0 — Confirmed regressions (parity blockers)

### 1. `status` doesn't list mg-IDs of unread mail
- **Old:** `pogo-discord-bridge:522-532` extracts mg-IDs from each unread mail's subject/body and renders inline (`• [mg-abc, mg-def] architect: design approval request`). Shipped as `mg-25e6` on 2026-05-07.
- **New:** `bridget:615` shows count only. The extraction loop was dropped.
- **Filing:** `bug: status command shows count of unread mail but not mg-IDs (regression of mg-25e6)`

### 2. `read mg-XXXX` doesn't auto-mark mail as read
- **Old:** `pogo-discord-bridge:704-710` moves the file from `new/` → `cur/` after rendering. Shipped as `mg-7154`.
- **New:** `bridget:729` returns content without the rename — mail keeps showing unread on next `status`.
- **Filing:** `bug: read mg-XXXX no longer marks mail as read after viewing (regression of mg-7154)`

### 3. `status` doesn't nudge the mayor
- **Old:** `pogo-discord-bridge:940` calls `run_pogo(['nudge', 'crew-mayor', 'status check from human via bridge'])`. Shipped as `mg-68bf`.
- **New:** `bridget:1037` returns the summary without the nudge.
- **Filing:** `bug: status command no longer nudges crew-mayor (regression of mg-68bf)`

### 4. README must surface the roadmap + known bugs list
- New bridget users currently have no in-repo signal of v2 priorities or known bugs — they have to discover issues by hitting them. The roadmap and the live `bug`-tagged work-item list should both be reflected in `README.md`, and should stay current.
- **Required behavior:** on every push to bridget that adds/changes a roadmap item or known bug, the README sections must be updated in the same PR. Encode this as a contributor expectation (CONTRIBUTING.md or PR template) so it can't drift.
- **Filing:** `idea: bridget README must include the v2 roadmap + known-bugs list, kept current on every push (contributor expectation enforced via CONTRIBUTING/PR template)`

---

## P1 — Hidden bugs surfaced by retiring the personal bridge

The personal bridge's hardcoded paths were masking a class of bridget bugs: optional env vars stayed unset because nobody hit the failure path while both bridges ran. After cutover, every user-facing command that depends on those vars silently 404s with a "set the env var" error, until the user populates `~/.pogo/bridget.env`.

**Concrete symptom on Clover's machine (2026-05-09 post-cutover):** `~/.pogo/bridget.env` shipped as the example template + 3 required Discord credentials, with every optional path commented out. `idea:`, `bug:`, and `next` Discord commands were all returning `✗ ... is unavailable: set ...` errors. Resolved at session-time by uncommenting `POGO_DESIGNS_DIR` and `POGO_INBOX_REPO` in `~/.pogo/bridget.env` and restarting bridget. **Other unreviewed users will hit the same wall.**

### 5. v2 design: zero-config sensible defaults
- **Principle:** every optional env var should have a sane default that makes bridget work out-of-the-box for a fresh install. Env override remains for power users (Clover with iCloud paths, deployments, etc.).
- **Clover's preferred default shape:** "wherever the bridget script lives on disk." Likely interpretations: defaults adjacent to `~/.pogo/` (e.g. `~/.pogo/designs/`, `~/.pogo/inbox/`) or relative to `BRIDGET_REPO_DIR`. Final pick is a v2 design call — what matters is that *something* works without env config.
- **Vars to design defaults for:**
  - `POGO_DESIGNS_DIR` — currently no default → `next` and design-reading fails
  - `POGO_INBOX_REPO` — currently no default → `idea:`, `bug:`, `next` fails
  - (review remaining optional vars during design — startup-time discovery is the trap)
- **Audit suggestion:** v2 design should include a "fresh-install smoke test" — boot bridget with only the 3 required Discord vars set, exercise every command, confirm none returns a "set this env var" error. The test would have caught this hours after the personal bridge was hardcoded.
- **Filing:** `idea: bridget v2 — sensible zero-config defaults for POGO_DESIGNS_DIR and POGO_INBOX_REPO so fresh installs work out of the box (the personal bridge's hardcoded paths were masking the gap; surfaced post-cutover when idea:/bug:/next commands all 404'd)`

---

## P2 — Hardening / polish (file when v1 parity is in)

### 6. Startup welcome message coverage
- `bridget:356-371` mentions `agents`, `balance`, `restart`, `bug:` — but not `nudge`, `quiet`, `next`, `explain`, `read`, `dismiss`. The personal bridge was even less comprehensive, so this isn't a regression — but it's worth a one-line filing for completeness.
- **Filing:** `idea: expand bridget's startup welcome message to cover all commands (nudge/quiet/next/explain/read/dismiss missing)`

### 7. Document `POGO_INBOX_REPO` / `POGO_DESIGNS_DIR` in install flow
- README/install.sh should call out these env vars explicitly so users don't hit the silent-404 trap before P1 ships. Currently the `bridget.env.example` mentions them but install.sh doesn't actively prompt or warn.
- **Filing:** `idea: install.sh should warn or prompt when POGO_INBOX_REPO/POGO_DESIGNS_DIR are unset (until v2 sensible-defaults ships)`

---

## Cleared (no action needed)

- **mg-afd8 architect-claim ping**: present in bridget at `bridget:407-452`, behaviorally equivalent to `pogo-discord-bridge:307-352`. Not a regression.
- **mail watcher seen-priming on startup**: present in bridget at `bridget:350-354`, equivalent to `pogo-discord-bridge:185-187`.
- **revise-marks-all-read**: present in bridget at `bridget:706`, equivalent to `pogo-discord-bridge:623`.
- **mail recipient configurability** (`POGO_MAIL_RECIPIENT`): bridget already configurable, personal bridge was hardcoded. Improvement, not regression.
- **`next` and `explain` commands**: bridget has them, personal bridge never did. New features.
- **Config validation, config defaults, status truncation budget, hardcoded-path elimination**: all OSS improvements bridget already shipped.
