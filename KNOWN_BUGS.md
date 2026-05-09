# Bridget known bugs

Open bugs against `bridget` that are deferred to v2 design. Maintained alongside mg state (the maintainer's local work tracker); update this file in the same PR that adds, dispatches, or closes a bug.

## Open (deferred to v2 design)

| mg-id | Summary |
|---|---|
| mg-d531 | mail-read state desync after credit outage — bridge keeps moving mails to `cur/` while mayor is stalled |
| mg-db58 | `balance` command false-negative on credit errors — returns ✅ when credit-exhaustion text is present in agent output |
| mg-ddb5 | `nudge` falsely reports agents active when credits exhausted — returns ✓ on PTY delivery without checking diagnose state |
