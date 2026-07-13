<!--
Copyright (C) 2026 Daniel Miller
SPDX-License-Identifier: GPL-3.0-or-later
Written for this fork of cloverross/bridget; not present upstream.
-->

# Outstanding-vs-resolved UX

Design proposal for mg-3358. Owner: pm-pogo. Status: **first cut shipped
(`mine` view); the two mutating pieces below are proposals awaiting Daniel's
sign-off — nothing in §2 or §3 is shipped.**

## The papercut

Daniel, 2026-07-10, on the Discord bridge:

> missing a simple way to tell what I've already responded to or resolved, and
> what's assigned to me / outstanding. Right now the only signal is whether a
> thread is read. … it would be nice if old threads archived themselves or
> something. idk what the right ux is.

Read/unread is the *only* axis the bridge exposes today. But "read" and
"resolved" are different facts: Daniel reads a mail to decide what to do, and
the thing he decided may still be outstanding. The bridge already sits next to
the system that *does* know the difference — `mg` tracks status
(`available`/`claimed`/`pending`/`done`) and `assignee` — so the fix is mostly
about *rendering* that truth into Discord, not inventing new state.

Three asks, in increasing order of how much they touch Daniel's live surface:

1. A resolved-vs-outstanding distinction beyond read/unread.
2. An "assigned to me / outstanding" view.
3. Auto-archiving old or resolved threads to keep the active surface clean.

The safe first cut delivers #1 and #2 as a **read-only** view. #3, and the
write-side of #1 (marking a thread resolved), are proposed here and deliberately
**not** shipped — see the guardrail below.

### Guardrail: the bridge is Daniel's live fleet-tracking surface

Anything that *mutates* what Daniel sees — archiving a thread, flipping a thread
to "resolved", marking mail read on his behalf — is editing the surface he uses
to track the whole fleet in real time. A false positive there is not a cosmetic
bug: an auto-archive that fires on a thread he still needs *removes his only
pointer to outstanding work*. So the two mutating behaviours (§2, §3) ship only
behind an explicit opt-in and only after Daniel signs off on the exact trigger.
The read-only view (§1) has no such hazard: worst case it shows a stale list,
which the next `mine` refreshes.

## §1 — Read-only "on your plate" view — **shipped**

New DM command: **`mine`** (aliases `outstanding`, `plate`, `assigned to me`).

```
🧑 On your plate — 2 outstanding
• [mg-b0cc] pending · pogo: APPLY mg-945c: cut over architect crew agent…
• [mg-3358] claimed · bridget: bridget UX: surface resolved-vs-outstanding…

📋 Awaiting your approval (1):
• approval needed: design for mg-1f2a

✅ Recently resolved: 4 (assigned to you, now done)
```

- **Source of truth:** `mg list --assignee=human --json`. No new state, no new
  store, no sync problem — the view is a pure function of what `mg` reports at
  call time.
- **Outstanding** = items in `pending` / `claimed` / `available`, ordered so the
  ones most likely to be waiting on Daniel lead (pending → claimed → available).
- **Awaiting approval** folds in the same approval-request mails `status`
  already scans for, because "an agent is blocked on my decision" is the other
  way work lands on him.
- **Resolved** is shown only as a trailing *count*, not a list: it answers "is
  the outstanding list actually shrinking?" without burying the plate under
  everything he's ever closed.
- **Read-only.** Marks nothing read, archives nothing, touches no thread. It is
  the always-safe slice of all three asks.

This lives beside `status` (which is the *fleet* pull view: all unread mail +
all in-flight work). `mine` is the *me* pull view: just what is assigned to or
waiting on Daniel.

## §2 — Thread-level "resolved" marking — **proposal, needs sign-off**

To carry the resolved/outstanding distinction into the *thread* surface (not
just the `mine` list), the bridge would reflect an item's `mg` status onto its
Discord thread. Options, cheapest first:

- **(a) Reaction mirror (recommended).** When a conversation's originating
  `mg` item flips to `done`, the bridge adds a ✅ reaction (or a `[resolved]`
  title prefix) to the thread's root message. Reversible, non-destructive, and
  it reuses the reaction-ack mechanism already built for thread replies
  (mg-aefb). A thread whose item reopens loses the ✅.
- **(b) Explicit human verb.** `resolve <thread>` / `resolve mg-XXXX` — Daniel
  marks a thread handled himself. Zero false positives (he's the trigger) but
  it's manual, which is the friction he's trying to escape.
- **(c) Discord-native archive on resolve.** Discord threads can be archived via
  the API. Powerful but this is the intrusive one — see §3.

**Recommendation:** ship (a) behind a new opt-in env flag (default off), keyed on
the existing conversation→mg-item map. Keep (b) as the manual escape hatch. Do
**not** couple resolve to archive until §3 is agreed. Open question for Daniel:
should "resolved" track `mg done`, or should it be a bridge-local flag he
controls independently? (The former is automatic but yokes his view to agent
actions; the latter is manual but his.)

## §3 — Auto-archive old / resolved threads — **proposal, needs sign-off**

The most intrusive ask, and the one most able to hide outstanding work. Design
constraints:

- **Never archive an outstanding thread.** Archive only threads whose `mg` item
  is `done`/`archived`, or that have had no activity for N days *and* carry no
  outstanding item. "Old" alone is not sufficient — an idle thread may still be
  the pointer to a blocked task.
- **Reversible & announced.** Archiving a Discord thread hides it but does not
  delete it; a reply un-archives it. The bridge should log each auto-archive to
  the log channel ("archived 3 resolved threads: …") so an archive is never
  silent.
- **Opt-in, conservative default.** New env flag (e.g.
  `BRIDGET_AUTOARCHIVE_DONE_AFTER_DAYS`, unset = off). `mg archive --days N`
  already exists for the mg side; the bridge's job is only the Discord-thread
  mirror of that, on the same or a longer horizon.
- **Dry-run first.** Ship a `mine --stale` / `archive --dry-run` that *lists*
  what auto-archive would remove, so Daniel can watch it be right for a week
  before enabling the real thing.

**Recommendation:** implement dry-run + the log-channel announcement first;
enable actual archiving only after Daniel confirms the dry-run picks the right
threads on his real workspace. Tie the trigger to `mg` status
(`done`/`archived`), not to raw thread age.

## Sequencing

1. ✅ `mine` read-only view (this PR).
2. ⏸ §2 (a) reaction mirror behind an opt-in flag — after sign-off.
3. ⏸ §3 dry-run listing — after (2) proves the status→thread mapping is right.
4. ⏸ §3 real auto-archive — only once the dry-run is trusted.

Steps 2–4 are each gated on Daniel confirming the behaviour on his live surface,
per the guardrail above.
