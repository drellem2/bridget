# Bridget known bugs

Current open and in-flight bugs against `~/DUGLocal/bridget/bridget`. Maintained alongside mg state (the maintainer's local work tracker); update this file in the same PR that adds, dispatches, or closes a bug.

## In flight (fix in PR, not yet landed)

| mg-id | Status | Summary |
|---|---|---|
| mg-3fe5 | polecat task mg-4e83 | `status` command shows count of unread mail but not mg-IDs (regression of mg-25e6) |
| mg-1a65 | polecat task mg-602d | `read mg-XXXX` no longer marks mail as read after viewing (regression of mg-7154) |
| mg-3782 | polecat task mg-fad3 | `status` command no longer nudges crew-mayor (regression of mg-68bf) |

## Open (deferred to v2 design)

| mg-id | Summary |
|---|---|
| mg-d531 | mail-read state desync after credit outage — bridge keeps moving mails to `cur/` while mayor is stalled |
| mg-db58 | `balance` command false-negative on credit errors — returns ✅ when credit-exhaustion text is present in agent output |
| mg-ddb5 | `nudge` falsely reports agents active when credits exhausted — returns ✓ on PTY delivery without checking diagnose state |
