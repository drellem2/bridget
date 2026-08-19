# bridget

A Pogo ↔ Discord bridge. Watches your local pogo mailbox
(`~/.macguffin/mail/human/new/`) and DMs you on Discord whenever new mail
arrives, and listens for command DMs back from you (approve / reject / file
ideas / read mail / etc.) — routing them to `mg`. It's a one-file Python
service driven by a small env file, so you can run it under launchd, systemd,
nohup, or whatever supervisor you like.

## Prerequisites

- **pogo** installed, with `mg` on your `PATH`. (If `mg` is in a non-standard
  location, set `MG_BIN` in the env file — see below.)
- A canonical pogo mail layout at `~/.macguffin/mail/human/{new,cur}/`, or set
  `POGO_MAIL_DIR` to the parent of `new/` and `cur/`.
- **Python 3.10+** with `venv` available (`python3 -m venv ...`).
- A **Discord bot** with the "Message Content" privileged intent enabled
  ([Discord developer portal](https://discord.com/developers/applications)),
  installed in a server you control. You need three values:
  - The bot token.
  - Your own Discord user ID (snowflake — bridget only DMs and only listens to
    this user).
  - The Discord server (guild) ID the bot lives in.

  In Discord, enable Developer Mode (Settings → Advanced → Developer Mode),
  then right-click your name / the server icon → "Copy ID".

## Roadmap & known bugs

The full v2 roadmap and known-bugs list, mirrored from [ROADMAP.md](ROADMAP.md) and [KNOWN_BUGS.md](KNOWN_BUGS.md). Both files are the canonical source — update them (and this README section) in the same PR if you change roadmap or bug state. See [CONTRIBUTING.md](CONTRIBUTING.md).

### v2 Roadmap

Current planned work for bridget v2. Completed items are removed in the same PR that closes them, so this file always reflects what's still ahead.

#### P2 — Hardening / polish (file when v1 parity is in)

##### 7. Document `POGO_INBOX_REPO` / `POGO_DESIGNS_DIR` in install flow
- README/install.sh should call out these env vars explicitly so users don't hit the silent-404 trap before P1 ships. Currently the `bridget.env.example` mentions them but install.sh doesn't actively prompt or warn.
- **Filing:** `idea: install.sh should warn or prompt when POGO_INBOX_REPO/POGO_DESIGNS_DIR are unset (until v2 sensible-defaults ships)`

### Known bugs

Open bugs against `bridget` that are deferred to v2 design. Maintained alongside mg state (the maintainer's local work tracker); update this file in the same PR that adds, dispatches, or closes a bug.

#### Open (deferred to v2 design)

| mg-id | Summary |
|---|---|
| mg-d531 | mail-read state desync after credit outage — bridge keeps moving mails to `cur/` while mayor is stalled |
| mg-db58 | `balance` command false-negative on credit errors — returns ✅ when credit-exhaustion text is present in agent output |
| mg-ddb5 | `nudge` falsely reports agents active when credits exhausted — returns ✓ on PTY delivery without checking diagnose state |

## Quick start

1. Clone and run the installer:
   ```bash
   git clone https://github.com/drellem2/bridget.git
   cd bridget
   ./install.sh
   ```
   (This is a fork of [cloverross/bridget](https://github.com/cloverross/bridget)
   — see [Fork status](#fork-status) below for what differs and why.)
   `install.sh` is idempotent — it creates `~/.pogo/venv-bridget/`, installs
   `discord.py`, symlinks `~/.pogo/bin/bridget` to the script in your clone,
   and seeds `~/.pogo/bridget.env` from `bridget.env.example` (if no env file
   exists yet). Re-running it after a `git pull` is the supported upgrade path.
2. Edit your config:
   ```bash
   $EDITOR ~/.pogo/bridget.env
   ```
   At minimum, fill in `DISCORD_BOT_TOKEN`, `DISCORD_USER_ID`, and
   `DISCORD_SERVER_ID`. See [Configuration](#configuration) for optional keys.
3. Smoke-test in the foreground:
   ```bash
   ~/.pogo/bin/bridget
   ```
   You should see `logged in as <bot> (id=…)` and a startup DM in Discord.
   Stop with Ctrl-C once that works.
4. Run under a supervisor for the long term — launchd on macOS, systemd on
   Linux, or `nohup` for quick-and-dirty. See
   [Running as a service](#running-as-a-service) for templates.
5. If something goes wrong, see [Troubleshooting](#troubleshooting).

## Configuration

All config lives in `~/.pogo/bridget.env`. See
[`bridget.env.example`](bridget.env.example) for the full template.

| Key | Required? | Purpose |
|---|---|---|
| `DISCORD_BOT_TOKEN`  | yes | Discord bot token. |
| `DISCORD_USER_ID`    | yes | Your Discord user ID — bridget DMs and only listens to this user. |
| `DISCORD_SERVER_ID`  | yes | Guild the bot is installed in. |
| `MG_BIN`             | no  | Absolute path to `mg`. Default: resolved via `PATH`. |
| `POGO_BIN`           | no  | Absolute path to `pogo`. Default: resolved via `PATH`. |
| `POGO_MAIL_DIR`      | no  | Parent of `new/` and `cur/` that mail is **delivered** from. Default: `~/.macguffin/mail/human`. Point at the representative's **output** box if you run one — see below. |
| `BRIDGET_APPROVAL_MAILBOX` | no | Mailbox the approval **scan** reads, as a bare name resolved beside the delivery box under the same mail root. Default: `human`. Deliberately does not follow a recipient re-point — see below. |
| `POGO_DESIGNS_DIR`   | no  | Directory of `mg-XXXX.md` design docs (read by `next`). Default: `~/.pogo/designs`. |
| `POGO_INBOX_REPO`    | no  | Repo where `idea:`, `bug:`, and `next` file new items. Default: `~/.pogo/inbox`. |
| `POGO_MAIL_RECIPIENT` | no | Default recipient for `mail` command. Default: `mayor`. |
| `POGO_REPRESENTATIVE` | no | Mailbox of a representative crew agent, if you run one. Empty (default) = no representative, no behaviour change. See "Running behind a representative". |
| `BRIDGET_REPO_DIR`   | no  | Override for the bridget git checkout. Default: self-detected from the script's location (works for the install.sh-managed symlink). |
| `BRIDGET_VENV_DIR`   | no  | Virtualenv holding `discord.py`; bridget re-execs into its interpreter when `discord` isn't importable. Default: `~/.pogo/venv-bridget` (what `install.sh` builds). |

### Behavioural knobs

These all default to the v1.0.0 hard-coded behavior — set them only when
your install needs to diverge.

| Key | Default | Purpose |
|---|---|---|
| `POGO_WORKFLOW_AGENT` | `architect` | Recipient/assignee for the workflow verbs (`approve`, `reject`, `revise`, `explain`, `next mg-XXXX`) and filing commands (`idea:`, `bug:`). Override if design coordination routes through a non-architect agent. |
| `POGO_INBOX_TAG` | `pogo-inbox` | Base tag stamped on `idea:`, `bug:`, and `next` items. Inline `[scope]` tags from the user are still appended. |
| `BRIDGET_POLL_INTERVAL` | `5` | Polling interval (seconds) for the mailbox / task-transitions / idea-claims watchers. |
| `BRIDGET_QUIET_RESPECTS_OUTBOUND` | `false` | When `true`, watchers consult `~/.pogo/quiet.json` and suppress DMs while quiet hours are active. Inbound DMs are always processed. |
| `BRIDGET_APPROVAL_RE` | `^Subject: approval needed ` | Regex matched against the first `Subject:` header to flag a mail as an approval request in `status`. |
| `BRIDGET_RESTART_CMD` | `bash build.sh` | Shell command run from `BRIDGET_REPO_DIR` to validate a fresh checkout before the `restart` verb respawns the process. |
| `BRIDGET_CREW_PATTERN` | `^(architect\|mayor\|human\|pm-.*\|)$` | Regex applied to `assignee` to decide whether the `claimed by …` annotation is suppressed. Anything matching = crew agent (suppressed); anything not matching = polecat. |

### Threading knobs

Unset, these leave bridget behaving exactly as it did before threads existed.
See [Conversation threads](#conversation-threads-optional).

| Key | Default | Purpose |
|---|---|---|
| `BRIDGET_LOG_CHANNEL_ID` | *(unset)* | Guild text channel where conversation threads are rooted. Unset = threading off. |
| `BRIDGET_DM_POLICY` | `all` | `all` / `curated` / `none` — how much mail reaches your DMs. Anything but `all` requires a log channel. |
| `BRIDGET_CORRELATION_IDS` | `auto` | `auto` / `on` / `off` — whether replies thread via `mg mail send --in-reply-to`. |

### Duplication-limit knobs

See [The duplication limit](#the-duplication-limit).

| Key | Default | Purpose |
|---|---|---|
| `BRIDGET_DEDUP_WINDOW` | `900` | Seconds before a repeated alert may notify again. Each further notice doubles the wait. `0` switches the limit off. |
| `BRIDGET_DEDUP_MAX_WINDOW` | `14400` | Ceiling on that doubling. Must be >= `BRIDGET_DEDUP_WINDOW`. |
| `BRIDGET_DEDUP_TTL` | `86400` | Quiet time after which a condition counts as new again. Must be >= `BRIDGET_DEDUP_WINDOW`. |
| `BRIDGET_RELAY_HEARTBEAT` | `3600` | Seconds between `relay: N delivered …` lines in `bridget.log` when idle — the delivery path's positive record. `0` switches it off, restoring a log whose silence means nothing. |
| `BRIDGET_WEDGE_ESCALATE_AFTER` | `120` | Seconds of unbroken delivery failure before the outage is escalated **out of band** — a `pogo events` record and a mail to the mayor's maildir, never over Discord. `0` switches it off, restoring an outage nobody hears. |
| `BRIDGET_WEDGE_SELFHEAL_AFTER` | `300` | Seconds before bridget exits `75` and lets `bridget-supervise` respawn it. `0` switches the self-heal off. |
| `BRIDGET_WEDGE_REPEAT` | `3600` | How often a still-unresolved outage re-escalates. |
| `BRIDGET_WEDGE_RESTART_BUDGET` | `3` | Self-heals allowed per `BRIDGET_WEDGE_BUDGET_WINDOW`, counted in `~/.pogo/bridget.selfheal.json` so the count survives the restarts it counts. `0` switches the self-heal off. |
| `BRIDGET_WEDGE_BUDGET_WINDOW` | `3600` | The window that budget is measured over. |

Process environment variables override values in the env file, so a
launchd/systemd unit can inject overrides without editing the file.

### Secrets

`~/.pogo/bridget.env` holds your bot token. bridget reads it into memory and
hands it to discord.py — it is never printed, logged, or written anywhere else,
and `discord.py`'s own logging is disabled (`log_handler=None`).

- `install.sh` creates the file `chmod 600`, and tightens the permissions on
  every run if it finds them looser.
- bridget warns on startup if the file is readable beyond its owner.
- `install.sh --setup` prompts for the token with terminal echo **off** and
  writes it via a `600` temp file. **No part of the token is ever echoed** — not
  a prefix, not a suffix, not its length; a partial token is still a leaked
  token, and installer output routinely lands in logs and transcripts. To catch a
  paste error anyway, the installer validates the token's *shape* (three
  dot-separated base64url parts) and tells you if it doesn't match. The token
  never appears in `argv` (readable by any user via `ps`) or in shell history.
- A test (`tests/test_secrets.py`) fails the build if a Discord-token-shaped
  string, or any real value for a secret key, is ever committed.

## Commands (DM the bot)

- `approve mg-XXXX` — approve a design (auto-clears related mails).
- `reject mg-XXXX <reason>` — shelve idea + clear mails.
- `revise mg-XXXX <feedback>` — request changes (auto-unshelves; clears mails).
- `explain mg-XXXX <what>` — ask architect to elaborate without redesigning.
- `next mg-XXXX` — file the next Roadmap task from this design as a new idea.
  *(Requires `POGO_DESIGNS_DIR` and `POGO_INBOX_REPO`.)*
- `read mg-XXXX` — print the latest mail referencing this id.
- `idea: <text>` — file a new idea. *(Requires `POGO_INBOX_REPO`.)*
- `idea: [tag] <text>` — file with an extra scope tag (e.g. `[bridget]`).
- `bug: <text>` — file a new bug (existing software is broken, not a new feature). *(Requires `POGO_INBOX_REPO`.)*
- `bug: [tag] <text>` — file a bug with an extra scope tag (e.g. `[discord-bridge]`).
- `mail <subject>\n<body>` — send a mail to the configured recipient (default `mayor`; override via `POGO_MAIL_RECIPIENT`). Without a newline, the whole text becomes the *body*, under a subject derived from it. Nothing you type is ever dropped: the body always carries the message whole, and the subject is a bounded label that, when shortened, says so — `… [truncated N chars; full text in body]`.
- `dismiss mg-XXXX` — mark all unread mail about an mg-id as read.
- `dismiss all` — inbox-zero everything.
- `status` — global pull view (unread mail + in-flight work).
- `mine` (or `outstanding`) — a read-only "what's on my plate" view: the work
  items assigned to you, split into **outstanding** (anything not yet done) and
  a trailing count of the **recently resolved**, plus any approval requests
  awaiting your decision. Source of truth is `mg list --assignee=human`; the
  command mutates nothing. See [Outstanding-vs-resolved UX](docs/outstanding-ux.md)
  for the design and the thread-level marking / auto-archive follow-ons.
- `agents` — list crew agents and health.
- `balance` — check whether any agent is hitting credit balance errors.
- `nudge <agent> [reason]` — wake a stalled agent.
- `restart` — git pull + restart bridget (after merging a PR; see [Remote restart](#remote-restart)).
- `quiet <true|false> [HH:MM HH:MM]` — toggle agent quiet hours (default 23:00–06:00).
- `settings` — show the DM policy, muted conversations, and threading state.
- `dupes` (or `duplicates` / `suppressed`) — the repeated alerts the
  [duplication limit](#the-duplication-limit) is holding back: how often each
  condition has fired, how often you were told, and how many repeats were
  suppressed.
- `dm <all|curated|none>` — change how much mail reaches your DMs, live.
- `mute all` / `unmute all` — silence every DM. With a log channel, mail still
  threads into it. Without one the DM was your only surface, so mail is held in
  the maildir until you `unmute all` — held, never dropped.
- `help` (or `?`) — print this list inside Discord.

Inside a conversation thread (see below) you can also just **type a reply** — it
gets mailed back to whoever started the conversation — or `mute` / `unmute` that
one conversation without naming it.

bridget only acts on DMs from the user whose ID is in `DISCORD_USER_ID`;
messages from anyone else are ignored.

## Conversation threads (optional)

By default bridget DMs you every mail, in one flat stream. Turn on threading and
it becomes a two-surface UX: a **log channel** that holds the firehose, with one
**thread per conversation**, and a **DM inbox** you can curate down to just the
things that want a decision from you.

```
BRIDGET_LOG_CHANNEL_ID=123456789012345678
BRIDGET_DM_POLICY=curated
```

### Why a channel, and not threads in the DM

Discord threads only exist inside guild **text channels** — a DM channel cannot
host one. (This is where Discord differs from Slack, where any message can root
a thread.) So the log channel is where conversations live, and the DM keeps its
job as the place bridget taps you on the shoulder. A DM card links straight to
its thread.

The thread is the full-text reading surface: a reply too long for one Discord
message (~1900 chars) is **split across several messages** in order, never
truncated, so the tail of a long answer is not lost. Splits fall on the softest
boundary at or before the limit — a blank line, else a line, else a sentence,
else a word — so a break lands mid-word only for a single token longer than a
whole message. The DM card stays a compact preview by design; the whole text is
in the thread (or a `read <mg-id>` away).

The bot needs **View Channel**, **Send Messages**, **Create Public Threads**, and
**Send Messages in Threads** on that channel. Point `BRIDGET_LOG_CHANNEL_ID` at a
text channel; a category, voice channel, or DM will be reported at startup rather
than silently swallowing your mail.

### How a conversation is identified

Each mail carries a `Message-Id`, and a reply carries `In-Reply-To` plus a
`References` chain (macguffin gh#66). A conversation is keyed on the id of the
message that rooted it, and bridget keeps a **message-id index** of every
message it has folded in — including the replies it sends itself. An arriving
mail is matched against that index by walking its ancestry (`In-Reply-To`, then
`References` newest-first, then its own id); it joins the conversation that owns
the first id bridget recognizes, and roots a new one only when it recognizes
none. The map lives in `~/.pogo/bridget.conversations.json` and survives
restarts — otherwise a restart would orphan every open thread and root a
duplicate for the next message.

The index is not an optimization. `mg mail send --in-reply-to X` is a stateless
primitive: it seeds `References: [X]` and nothing else. Only the first reply in
a chain therefore names the root — from the second hop on, `References[0]` is
merely the parent. Keying on `References[0]` alone would give you one thread per
message from the second round-trip onward. It also means the 20-id cap macguffin
puts on `References` costs nothing: bridget never needs the chain to reach back
to the root, only to reach a message it has already seen.

Mail with no correlation headers at all (anything written before gh#66) keys on
its maildir filename, which is the value macguffin would have used as its id
anyway. Such a mail simply becomes a conversation of one. Nothing breaks.

### Replying

Type into a thread and bridget mails it back to the agent on the other end,
threading the reply onto the conversation with `mg mail send --in-reply-to`.

**Everything you type is body.** The reply goes out under
`Re: <conversation subject>`, however many lines you wrote. This differs from
the `mail` verb, which takes your first line as the subject — there you are
composing and have to name the thing; in a thread the subject is already known,
and taking your first sentence for it would break the agent's subject continuity
and read as a non-sequitur in its inbox.

Inside a thread, what you type is a **reply** unless it is unmistakably a
command: a workflow verb carrying an mg-id (`approve mg-1234`, `read mg-abcd`),
or an `idea:` / `bug:` prefix. Bare words are not commands there — "status is
green, ship it" is a reply, not a request for a status dump, and "dismiss all of
that noise" will not inbox-zero you. In a DM, every command works as always.

You always get an explicit acknowledgement — and for a reply, it rides on
**your own message as a reaction**, not as a text post that would clutter the
thread you're reading:

| | |
|---|---|
| 👀 | bridget saw your message (added the instant you send it). |
| ✅ | delivered — the mail went out. Replaces the 👀. |
| ❌ | undeliverable — there's nowhere to send it, or `mg` refused. Replaces the 👀, and the reason is posted as text so the failure is never a bare cross. |

`⚠️ ambiguous` is still text: it means bridget can't tell which conversation you
meant (e.g. you typed in the log channel instead of in a thread), so it lists
the candidates for you to pick from — something a single reaction can't do.

Silence is never an outcome: if a reply didn't go, bridget says so.

bridget only needs the **Add Reactions** permission in the log channel to draw
these (no extra gateway intent — it adds reactions, it doesn't listen for
yours). Without it, the ✅/❌ simply don't appear; delivery is unaffected.

`--in-reply-to` ships in macguffin gh#66 and is **not** required. bridget probes
`mg mail send --help` once (`BRIDGET_CORRELATION_IDS=auto`) and uses the flag if
it exists. Without it, replies still deliver — they just arrive as new top-level
mail instead of joining the conversation. If mg is swapped underneath a running
bridget and starts rejecting the flag, the send is retried once without it rather
than reported as undeliverable. `settings` shows which mode is active.

### The calm inbox

`BRIDGET_DM_POLICY` decides how much of the firehose interrupts you:

| Policy | Effect |
|---|---|
| `all` | Every mail DMs you. The default, and what bridget always did. |
| `curated` | Only mail matching `BRIDGET_APPROVAL_RE` — i.e. mail that wants a decision — DMs you. Everything else lands in the log channel. |
| `none` | Nothing DMs you. The log channel is the only surface. |

`curated` and `none` require a log channel; without one, bridget refuses to start
rather than silently drop the mail it would have suppressed.

Change it live with `dm curated`. Mute a single conversation by typing `mute` in
its thread, or everything with `mute all`. **Muting silences the DM, never the
thread** — a muted conversation keeps its full record in the log channel, so
muting can never lose mail. Live state is in `~/.pogo/bridget.settings.json`.

With **no** log channel there is no second surface, so `mute all` (and quiet
hours) can't quietly divert mail. bridget therefore stops *consuming* mail while
you're unreachable: it stays in the maildir, `status` still counts it, and
`unmute all` delivers what arrived meanwhile. Silence is a pause, never a delete.

### What bridget never does to your maildir

The watchers are **observe-only**: they read `<mailbox>/new/` and never move,
rename, or delete anything in it. `mg mail read` owns that transition. If
displaying a mail in chat also marked it read, every mail you glanced at on your
phone would vanish from your real inbox. De-duplication is therefore a persisted
seen-set of maildir filenames (`~/.pogo/bridget.seen`), not the directory itself.

Because a delivered mail *stays* in `new/`, the seen-set may only forget a
filename once that file has actually left `new/`. It is garbage-collected by
presence, never trimmed by age — trimming by age would re-surface the oldest
still-unread mail as "new" on the very next poll, and every poll thereafter.

Delivery is **at-least-once**: if Discord rejects a send (rate limit, 5xx), the
mail is un-seen and retried on the next poll rather than silently consumed.

The `dismiss` and `read` commands do mark mail read — because you asked them to.

## The duplication limit

Fleet watchers repeat themselves. A measured snapshot of one `human/new/` held
**1404 unread**, in which a single alert appeared 31 times and the eight loudest
subjects accounted for 109 messages. bridget is the one chokepoint every sender
crosses on the way to Discord — pogod, `hey-feed`, `doctor`, the watchdog scripts
and `gh-intake-watch` all write to that maildir directly — so the limit lives
here rather than in any one watcher's mailer.

**A condition is `(sender, subject with digit runs folded to N)`.** Folding
matters more than it looks: the three loudest ack-watch rows in that snapshot
were one condition whose fire count drifted, and a limit keyed on the literal
subject would have caught almost none of it.

```
ack-watch: FLEET BLACKOUT — 90 fires delivered in the last 3h0m0s, NONE completed   ×13
ack-watch: FLEET BLACKOUT — 92 fires delivered in the last 3h0m0s, NONE completed   ×9
ack-watch: FLEET BLACKOUT — 91 fires delivered in the last 3h0m0s, NONE completed   ×5
```

**mg-ids are not folded.** `approval needed mg-4fc0` and `approval needed
mg-9a13` are two different decisions waiting on you; folding them would suppress
the second — the mail the limit has least right to hold back.

**A first occurrence is never delayed and never dropped.** Only repeats are
held, and the wait between notices doubles — 15 minutes, 30, 60, 120, up to
4 hours — so a condition that is still firing keeps saying so at a decaying rate
instead of going quiet. A condition unseen for 24 hours counts as new again.

**Nothing suppressed is untraceable.** Every held firing gets a `dedup:` line in
the log, the next notice leads with `🔁 still happening — notice #N, M repeats
suppressed since T`, `dupes` shows the standing tally, and `status` carries the
count. The mail itself is untouched: bridget is observe-only, so all 31 copies
are still in `human/new/` and `mg mail list human` still shows every one.

Replaying that measured snapshot through the delivery path turns **114 alerts
into 10 DMs and 7 threads** — one thread per condition rather than one per
firing, since a repeat notice is folded into the thread its first occurrence
opened (`tests/test_dedup.py`).

Set `BRIDGET_DEDUP_WINDOW=0` to switch the limit off and deliver every repeat.

Two things the limit deliberately does **not** do:

- It does not rate-limit a **reply**. A reply normalises to the same key as the
  mail it answers (`Re: ` is stripped), so limiting it would silence a live
  conversation rather than a repeating watcher. Only mail that roots its own
  conversation and names no ancestor is eligible.
- It does not count a **failed** send as a notice. The limiter decides and
  records in two steps, and the record is written only after the mail reaches a
  surface — otherwise a transient Discord outage would be laundered into a
  silently swallowed alert by the very limit meant to prevent losing alerts.

## Quiet hours

Quiet hours are a shared signal to crew agents (architect, mayor, etc.) that
they should skip polling during a configured window — e.g. so background
sweeps don't churn overnight. bridget owns the toggle; agents read the same
state file and decide what to do with it.

Toggle from Discord:

- `quiet` (or `quiet status`) — show the current state.
- `quiet true` — enable, using the previously-stored window (default
  23:00–06:00).
- `quiet false` — disable; the window is preserved for next enable.
- `quiet true 23:00 06:00` — enable with an explicit window. Times must match
  `HH:MM` (24-hour).

State lives at `~/.pogo/quiet.json`. This file is **shared with crew agents**,
not bridget-private — don't move or rename it. It's runtime state; not
committed to the repo.

## Task transition notifications

bridget pushes a Discord DM when a polecat task transitions to one of the
notable statuses:

- `🚀 claimed mg-XXXX [by <assignee>]: <title>`
- `✅ done mg-XXXX: <title>`
- `📦 shelved mg-XXXX: <title>`

State lives at `~/.pogo/bridget.task-states.json` (runtime; not committed).
The first run after deleting the cache silently re-primes — bridget records
current status without DMing, so you don't get a flood of notifications for
work that's already in flight. Only ideas/bugs/etc. with `type=task` trigger
notifications; other types are filtered out.

The watcher polls in two tiers so it doesn't walk the whole macguffin archive
every few seconds. A cheap **hot poll** (`mg list --json`, no `--all`) runs
every `BRIDGET_POLL_INTERVAL` and catches `claimed`/`done`, which `mg list`
shows by default. The expensive `--all` **full diff** — which alone can see
`shelved`/archived items, and so walks every archive tombstone — runs only
every `BRIDGET_FULL_POLL_INTERVAL` seconds (default 60) and catches `📦 shelved`
transitions. The full diff keeps its own cache at
`~/.pogo/bridget.task-states-full.json`. The trade-off: a `📦 shelved` notice
can lag by up to `BRIDGET_FULL_POLL_INTERVAL` rather than one poll interval.
Dropping `--all` from the hot poll is what stopped it feeding the `mg
list`-timeout storm that once wedged delivery for ~70h (mg-e5b8, mg-4fc0).

## Per-channel agent routing (optional)

By default, bridget is DM-only: every command and every notification flows
through DMs with the user named in `DISCORD_USER_ID`. If you want one Discord
*channel* per agent — `#mayor` for mayor, `#architect` for architect, etc., the
"open-claw" shape — add `~/.pogo/bridget.channels.toml` and bridget will route
inbound messages and outbound notifications per channel. Without that file,
bridget behaves bit-identically to v1.0.0 — this is purely opt-in.

### Where the config lives

`~/.pogo/bridget.channels.toml`. **Outside** the bridget checkout, alongside
your `bridget.env` — `install.sh` never touches it, and re-running the
installer or pulling new code can't clobber your channel mappings.

A copy-pasteable starter ships in the repo as
[`bridget.channels.toml.example`](bridget.channels.toml.example) — copy it to
`~/.pogo/bridget.channels.toml`, swap in your real channel snowflakes and
agent names, and restart bridget.

### Finding a channel snowflake

Snowflakes are the 18–20 digit numeric IDs Discord uses internally. To get
one:

1. Discord → User Settings → Advanced → enable **Developer Mode**.
2. Right-click the channel name in the sidebar → **Copy Channel ID**.
3. Paste it as a quoted string in the TOML (`"1234567890123456789"`). Quote
   it to avoid integer-precision quirks in TOML parsers.

Server-wide IDs and user IDs use the same Copy ID gesture — those are what
fill `DISCORD_SERVER_ID` and `DISCORD_USER_ID` in `bridget.env`.

### Schema

```toml
[channels.<name>]
snowflake = "1234567890123456789"             # optional — omit to auto-create
agent     = "mayor"
direction = "both"                            # optional
kinds     = ["mail", "task-transitions"]      # optional
channel   = "mayor-ops"                       # optional — created channel name
```

| Field | Required | Purpose |
|---|---|---|
| `snowflake` | no | Discord channel ID, as a quoted string of digits. **Omit it to have bridget create the channel on startup** (see below). |
| `agent` | yes | Pogo agent name. Inbound non-verb messages are mailed to this agent; outbound events for this agent fan out to the channel. |
| `direction` | no (default `both`) | `inbound`, `outbound`, or `both`. |
| `kinds` | no (default all) | Subset of `["mail", "task-transitions", "idea-claims"]`. Controls which outbound classes fan out to this channel. |
| `channel` | no (default: `<name>`) | Discord channel *name* to create / resolve-by-name when there is no snowflake. Sanitised to Discord's rules (lowercase, `[a-z0-9_-]`). |

The `<name>` in `[channels.<name>]` is a local label used only in error
messages — it does not have to match the agent name or the channel name.

### Auto-create (no snowflake needed)

You no longer have to pre-create a channel and hand-copy its snowflake. Leave
`snowflake` out and bridget ensures the channel exists on startup:

1. It looks for a text channel whose name matches (`channel`, or the `<name>`
   label lowercased). If one exists, it **adopts** it.
2. Otherwise it **creates** the text channel. The bot needs the *Manage
   Channels* permission in the guild for this step.
3. The resulting channel ID is written to `~/.pogo/bridget.channel-ids.json`
   (owner-only) so restarts resolve the **same** channel and never make a
   duplicate.

That persisted ID takes precedence over a `snowflake` you later hand-type for
the same `<name>`. And if a hand-typed snowflake can't be resolved (wrong ID,
channel deleted), bridget resolves the entry by name — adopt-or-create — rather
than retrying a dead ID forever. Already-valid snowflakes are untouched: static
routing behaves exactly as before.

### Routing rules

- **Inbound (channel → agent).** Workflow verbs (`approve` / `reject` /
  `revise` / `explain` / `next` / `idea:` / `bug:`) keep routing through
  `POGO_WORKFLOW_AGENT` exactly as they do in DMs — design coordination
  doesn't change identity based on which channel you typed in. Free-form text
  in an inbound-mapped channel becomes `mg mail send <channel-agent>` with the
  **whole message as the body** and a bounded, derived label as the subject.
  Chat is talking, not composing mail: there is no subject line, only a first
  sentence, and lifting that sentence into the subject once let an agent read a
  mid-clause fragment as a complete instruction.
- **Outbound (agent → channel).** When a watcher would normally DM the user
  about an event involving an agent that has an outbound mapping, bridget
  posts to the mapped channel *instead of* DMing — so channels declutter your
  DMs rather than duplicate them. Events whose agent has no mapping (or whose
  `kind` is excluded) fall back to DM, exactly as in v1.0.0.
- **Bot setup.** The bot must be a member of the guild containing each mapped
  channel and have permission to read message history and send messages
  there. Auto-created entries (no `snowflake`) additionally need the *Manage
  Channels* permission so bridget can create the channel. The required Discord
  intents (`guilds`, `guild_messages`) are
  non-privileged and bridget enables them automatically. The author check
  still pins to `DISCORD_USER_ID` — only your messages are processed in
  mapped channels; messages from anyone else in the same channel are
  ignored.
- **Fallback when config is missing.** No `bridget.channels.toml` (or a file
  that fails to parse / contains no valid entries) means bridget runs in
  pure DM mode — every notification DMs `DISCORD_USER_ID`, no channel is
  read, and the bot ignores all guild messages. Errors during load are
  printed to stderr and never crash bridget.
- **Python version.** Per-channel routing requires Python 3.11+ (for
  `tomllib`). On 3.10 the file is ignored with a one-line stderr warning;
  bridget keeps running in DM mode.

### Worked example: adding a new agent → channel pair

Say you want messages typed in `#mayor` to reach the `mayor` agent, and
mayor's mail / task-transition notifications to land in the same channel.

1. Create `#mayor` in your guild and enable Developer Mode in Discord.
2. Right-click `#mayor` → **Copy Channel ID** → say it gives you
   `987654321098765432`.
3. Append to `~/.pogo/bridget.channels.toml` (creating the file from the
   `.example` if it doesn't exist):

   ```toml
   [channels.mayor]
   snowflake = "987654321098765432"
   agent     = "mayor"
   direction = "both"
   ```

4. Make sure the bot is a member of the same guild and has *Read Message
   History* + *Send Messages* on `#mayor`. (Bot membership is set in
   the [Discord developer portal](https://discord.com/developers/applications);
   permissions can be granted via the channel's settings or the bot's role.)
5. Restart bridget (`restart` from a DM, or kick the supervisor).
6. Verify: type "hello" in `#mayor`. You should see it land in mayor's
   mailbox as `mg mail send mayor --from=human --subject=hello`. Conversely,
   `mg mail send mayor --from=human --subject="test"` from another shell
   should produce a `📬 mail to mayor: test` post in the channel within
   `BRIDGET_POLL_INTERVAL` seconds.

If the channel stays silent in either direction, check stderr — bridget
prints a clear line for parse errors, missing intents, and permission
failures.

## Idea claim notifications

bridget pushes a Discord DM when the architect claims an idea:

- `🧠 architect claimed mg-XXXX: <title>`

State lives at `~/.pogo/bridget.idea-claims.json` (runtime; not committed).
The first run after deleting the cache silently re-primes — only ideas newly
appearing in `mg list --status=claimed` after that point produce a DM. Only
items with `type=idea` trigger notifications; tasks and other types are
filtered out.

## Remote restart

The `restart` Discord command upgrades a running bridget to the latest
`origin/main` without touching the host. The flow is: `git pull --ff-only` in
the bridget checkout, run `build.sh` as a syntax check, then `os._exit(0)` so
the supervisor (launchd / systemd) respawns the process.

bridget self-detects its checkout from `Path(__file__).resolve().parent`, which
works whenever `~/.pogo/bin/bridget` is the install.sh-managed symlink to the
script in your clone. Set `BRIDGET_REPO_DIR` in `bridget.env` only if you run
bridget from an unusual setup where that resolution doesn't land on the repo
root.

If the pull or syntax check fails, bridget reports the stderr in Discord and
keeps running on the old code — you don't get stranded.

**Bootstrap caveat.** The first `restart` after merging a PR that itself
modifies the `restart` command must be done manually on the host (since the
running bridge is still on the old code). After that, `restart` keeps you in
sync.

## Running as a service

bridget is a long-running Python process. None of the options below need to know
where the venv is: bridget's shebang finds the system interpreter, notices
`discord` is missing there, and re-execs itself into `BRIDGET_VENV_DIR`
(default `~/.pogo/venv-bridget`).

### macOS (launchd)

```bash
bash install.sh --launchd
```

That renders [`com.pogo.bridget.plist.example`](com.pogo.bridget.plist.example)
into `~/Library/LaunchAgents/`, bootstraps it, and **kickstarts** it. Check on it
with:

```bash
launchctl print gui/$(id -u)/com.pogo.bridget | grep -E 'state|runs|pended'
```

Two things about this setup are not what you'd expect, and both were measured
rather than assumed.

**The job runs `bridget-supervise`, not `bridget`.** [`bridget-supervise`](bridget-supervise)
is a small wrapper whose only job is to restart bridget when bridget exits. That
sounds like exactly what `KeepAlive` is for — but see below.

**`KeepAlive` is a best-effort outer net, not a guarantee, and the kickstart is
mandatory.** launchd distinguishes *demand* spawns (`launchctl kickstart`) from
*nondemand* spawns — `RunAtLoad`, a `KeepAlive` restart, and a `StartInterval`
fire are all nondemand. Under sustained system load launchd defers nondemand
spawns and says so in a field almost nobody reads:

```
$ launchctl print gui/501/com.pogo.bridget | grep -E 'state|runs|pended'
	state = not running
	runs = 1
	pended nondemand spawn = inefficient
```

That deferral is not a short delay. A job killed with `SIGTERM` was observed
still `not running` 115 seconds later, and two sibling agents
(`com.pogo.watchdog`, `com.pogo.gh-issues`) sat in exactly that state for roughly
4.8 hours, `KeepAlive=true` notwithstanding, until a human kickstarted them by
hand. A `bootstrap` alone routinely leaves a brand-new job at `runs = 0`,
`pended … = speculative`, which never spawns at all.

`kickstart` is never pended, which is why `install.sh --launchd` ends with one.
And because a bridget crash would otherwise hand the restart to the same pending
machinery, the wrapper takes that job instead: launchd only has to spawn the
wrapper once.

The plist deliberately omits `StartInterval` (its fire is a nondemand spawn too,
so it is pended alongside everything else) and `ProcessType` (`Interactive`
changes the reported spawn type and nothing else). Neither defeats the pending.

**Residual risk, stated plainly:** if anything kills the *wrapper*, restarting it
is launchd's job, and that restart is subject to the same deferral. Nothing
expressible in a plist fixes that; it needs a live process to issue
`launchctl kickstart`. If bridget is ever mysteriously absent, this is why, and
the cure is:

```bash
launchctl kickstart gui/$(id -u)/com.pogo.bridget
```

### What the supervisor will and will not supervise

`bridget-supervise` runs `$BRIDGET_BIN`, default `~/.pogo/bin/bridget` — the
symlink `install.sh` drops into your checkout. That default is *durable*: it
outlives the supervisor. Not every path is.

On 2026-07-10 a supervisor was started with `BRIDGET_BIN` pointing inside a
throwaway `~/.pogo/polecats/<id>/` worktree. The worktree was reaped eight
minutes later, and because the path was resolved once at startup and re-exec'd
unchanged, launchd (`KeepAlive`, `ThrottleInterval=10`) respawned the supervisor
into `FATAL: no bridget at …` every ten seconds for eighteen minutes. Nothing
raised a hand: `launchctl list` showed a pid the whole time, because a pid is
what a respawn loop has a lot of. A human noticed and re-pointed the path.

So the supervisor now:

- **refuses to exec anything under `~/.pogo/polecats/`** (symlinks resolved, so
  a `~/.pogo/bin/bridget` symlink into a worktree is caught too);
- **re-checks the target before every spawn**, not once at startup, which is
  what catches a path that was durable when it started and is not any more;
- **falls back to `~/.pogo/bin/bridget`** and keeps supervising, rather than
  dying, whenever it rejects the target and that default is usable. The one
  exception is a `BRIDGET_BIN` that is merely *missing at startup*: nothing is
  running yet, so there is nothing to preserve, and quietly substituting a
  different binary for the one you named is worse than refusing;
- **says so**, every time, on stdout and stderr and by mailing `mg` agent
  `mayor`. A supervisor that cannot exec its target and mentions this to no one
  is the actual defect; the pinned path was only the trigger.

The mail is rate-limited to one per `BRIDGET_ALERT_COOLDOWN` seconds (default
900) via a stamp at `~/.pogo/bridget-supervise.alert`. It has to be on disk:
every launchd respawn is a fresh process, so nothing held in memory could
rate-limit anything, and 360 identical mails an hour is its own kind of silence.

| Variable | Default | |
|---|---|---|
| `BRIDGET_BIN` | `~/.pogo/bin/bridget` | what to supervise |
| `BRIDGET_ALLOW_EPHEMERAL_BIN` | `0` | set to `1` to supervise a worktree path anyway — for smoke-testing a build in place, and deliberately not the default, because the outage came from a `BRIDGET_BIN` nobody chose to set |
| `BRIDGET_ALERT_TO` | `mayor` | `mg` recipient for the alert |
| `BRIDGET_ALERT_CMD` | (unset) | a program run as `cmd <subject> <body>` instead of `mg mail send` |
| `BRIDGET_ALERT_COOLDOWN` | `900` | seconds between alerts |
| `BRIDGET_ALERT_STAMP` | `~/.pogo/bridget-supervise.alert` | where that cooldown is remembered |

### Which revision is it actually running?

A durable target says nothing about the code inside it. `bridget-supervise` execs
the bridget file **in your working checkout**, on whatever branch that checkout
happens to be on.

On 2026-08-11 that branch was `representative-relay-mg-65d2`, two commits behind
`origin/main`, because that is where a previous session left it. The refinery
merged the duplication limit (mg-5521) to main, the MERGED mail arrived, the
process was healthy — and the fix was not running, and never would have been.
Restarting bridget did not help: the restart faithfully re-ran the same
feature-branch code. A human moved the branch by hand (mg-6ca7).

That is worth more than a note because of two properties. It is **silent** —
nothing named a revision anywhere, so no reader could tell a stale run from a
current one. And it **survives a restart** — "restart it" is the standard remedy
for "the fix isn't live", and here it confirms the wrong state instead of
correcting it.

So every spawn now names the revision it started:

```
[2026-08-11T20:26:41Z] supervise: starting bridget (spawn #1) at 3a821ca on main (current with origin/main)
[2026-08-11T20:26:41Z] supervise: starting bridget (spawn #2) at afe7898 on representative-relay-mg-65d2 — 2 commit(s) behind origin/main
```

and a checkout behind the deploy ref says so on stdout, on stderr, and by mailing
`human` — the fix that is not running is the human's, so this is user-facing mail
rather than coordination.

**"Behind its tracking branch" is deliberately not the check.** The branch this
was found on had no upstream at all, and every branch that did have one was level
with it: a tracking-branch check would have printed nothing on the day this
happened. What is wrong is being behind the branch bridget is *deployed* from, so
that is the comparison — `BRIDGET_DEPLOY_REF`, `origin/main` by default. Nothing
here fetches: a supervisor's restart path is no place for a network call that can
hang, and the merge that produced the stale checkout also updated the local ref.

Where the repair is provably lossless, it makes it — `git checkout main &&
git merge --ff-only origin/main` — and **only** when all of these hold:

- **HEAD is a strict ancestor of the deploy ref.** The branch then carries no
  commit main does not already have, so leaving it discards nothing and the
  branch ref still points exactly where it did. Ahead or diverged: report, never
  touch.
- **No modified or staged tracked file.** Someone may legitimately be mid-test in
  there. A supervisor that discards a human's work is strictly worse than one
  that runs old code.
- **The checkout is not a polecat worktree.** A polecat that opted in with
  `BRIDGET_ALLOW_EPHEMERAL_BIN=1` is testing its own build; fast-forwarding it
  would destroy exactly the mid-test case the previous rule protects.

When any of those fails, nothing is touched and the refusal is **loud**, naming
both commands that would fix it by hand. The defect here was silence; a silent
refusal is the same defect with better manners.

One thing the supervisor cannot repair in place is **itself**. bridget is
re-exec'd on every spawn, so it picks up a new checkout for free; the supervisor
is already running, and bash reads its own source lazily. So when a fast-forward
changes `bridget-supervise`, it says so and names the kickstart that activates it:

```bash
launchctl kickstart -k gui/$(id -u)/com.pogo.bridget
```

It does not re-exec itself. That would close the gap and open a worse one — the
new copy is unverified, and if it fails to start, the respawn is the pended
nondemand spawn this whole wrapper exists to avoid.

This check runs before **every** spawn, not once at startup, for the same reason
the target is re-resolved before every spawn: a merge lands while bridget is up,
and the restart after it is the moment that either activates the new code or
silently re-runs the old.

| Variable | Default | |
|---|---|---|
| `BRIDGET_ACTIVATION` | `ff` | `ff` fast-forwards when the three rules above hold; `warn` reports everything and touches nothing; `off` is the pre-mg-6ca7 behaviour — runs whatever the checkout holds and says nothing about it |
| `BRIDGET_DEPLOY_REF` | `origin/main` | the revision bridget is deployed from. Falls back to the local branch of the same name when there is no such remote-tracking ref |
| `BRIDGET_REVISION_ALERT_TO` | `human` | `mg` recipient for revision reports (separate from `BRIDGET_ALERT_TO`, which is about the target path) |
| `BRIDGET_REVISION_ALERT_STAMP` | `~/.pogo/bridget-supervise.revision` | its own cooldown stamp, so a path alert and a revision alert cannot throttle each other |
| `BRIDGET_GIT` | `git` | git to run the checks with |

Related, and not a substitute: the `restart` Discord verb does `git pull --ff-only`
in `BRIDGET_REPO_DIR` and then exits so the supervisor respawns it. That is the
*deliberate* activation path and it reports its failures to you in Discord. This
one covers every restart nobody typed — a crash, a reboot, a `kickstart`.

### Is it actually alive?

Three instruments, narrowest last: a loop heartbeat, a delivery heartbeat, and
a `relay:` line in the log that says how much has been carried. Read the log
line for *what happened*; read the heartbeat files for *what is happening right
now*, which is the question a reaper asks.

The task-transition watcher
touches a dedicated heartbeat file at the top of **every** cycle, so its mtime is
a true liveness signal for the watcher thread:

```bash
stat -f '%Sm' ~/.pogo/health/bridget.heartbeat    # should be within a poll interval
```

This ticks even on a cycle where `mg list` timed out — the watcher retries
rather than dying (a single transient timeout, which fires even at rest, once
silently killed the watcher and left the channel pipe dark for 44 minutes while
the process stayed up). A frozen heartbeat therefore means the watcher is
genuinely dead, which is exactly what pogod's tier-1 reaper keys on: declare the
job to `[reaper]` as `com.pogo.bridget|~/.pogo/health/bridget.heartbeat|<period>`
(pick a period comfortably above the poll interval) and it will `launchctl
kickstart` a stale watcher instead of trusting KeepAlive, which only reacts to
process *exit*.

#### The loop is alive — but is mail actually being delivered?

`bridget.heartbeat` proves the task-transition **loop** is scheduled. It does
**not** prove mail is reaching Discord, and the difference is not academic: a
*storm* of `mg list` timeouts once blocked the shared event loop long enough that
Discord dropped the gateway socket and outbound delivery died for **~70h** — yet
`bridget.heartbeat` kept ticking the whole time, because the poller that touches
it does not need Discord to run (mg-e5b8). Nothing watching that file could have
caught the wedge. So there is a second, narrower heartbeat:

```bash
stat -f '%Sm' ~/.pogo/health/bridget.delivery.heartbeat   # DELIVERY liveness
```

The mail watcher touches this **only at the end of a poll cycle that actually
delivered — or had nothing to deliver — with no send failure.** The moment mail
is queued and every send fails (Discord down, rate-limited, socket dropped), or
the delivery loop stops iterating at all, its mtime **freezes** while
`bridget.heartbeat` may keep advancing. Declare it to the reaper the same way,
alongside the loop heartbeat:

```
com.pogo.bridget|~/.pogo/health/bridget.delivery.heartbeat|<period>
```

Watch **both**: the loop beat catches a dead *watcher thread*; the delivery beat
catches a *wedged delivery path* that leaves the thread alive. Liveness has to
reflect the work, not just the loop. (Under the hood, the storm that caused this
also no longer freezes the loop: the pollers now run `mg` off the event loop, so
a hung `mg` degrades the pollers without starving delivery — see CHANGELOG.)

#### The same answer, in the file you actually grep

Both heartbeats above are mtimes. An mtime answers "is delivery alive *now*" to
whoever thinks to `stat` it — and answers nothing at all to the far more common
reader, who greps `~/.pogo/bridget.log` and reasons from what they find. Until
v1.7 that reader had nothing to find, because **bridget logged delivery only
when it went wrong**: a dead relay and an idle one produced a byte-identical
zero. So the delivery path now leaves a positive record (mg-7c1b):

```bash
grep 'relay:' ~/.pogo/bridget.log | tail -3
[2026-08-11T20:29:03Z] relay: 0 delivered in the last 0s (0 total since 2026-08-11T20:29:03Z)
[2026-08-11T20:41:12Z] relay: 3 delivered in the last 729s (3 total since 2026-08-11T20:29:03Z)
[2026-08-11T21:41:12Z] relay: 0 delivered in the last 3600s (3 total since 2026-08-11T20:29:03Z)
```

Read it like this:

- **A line with `0 delivered` is the load-bearing one.** It is the beat firing
  with nothing to report, and it is the only thing that makes a *later* silence
  mean death rather than a quiet day. A per-mail line could not say this.
- **A line only appears for a cycle that reached Discord without a send
  failure.** During a wedge the beat stops, exactly as
  `bridget.delivery.heartbeat` freezes — and for the same reason. A beat that
  ticked through an outage would read as a positive across it, which is how the
  ~70h wedge went unseen.
- **The exception lines are in the other file.** `bridget.log` is
  `StandardOutPath`; `will retry`/`send failed` go to stderr, so they land in
  `~/.pogo/bridget.err.log`. The *stop* was once documented here as the signal;
  that was measured false over a 71.6-hour outage whose reader filed it as "a
  3-day hole nobody has explained", so an unhealthy cycle now writes its own
  `relay-stall:` line on the same hourly cadence (mg-879c).
- **`grep -c 'relay:'` returning 0 now means something** — either bridget has
  not started since the upgrade, delivery has never completed a healthy cycle,
  or the record is switched off. The last of those would be a new ambiguity, so
  it is stated in the same file at every start: `delivery record: on — …` or
  `delivery record: OFF (BRIDGET_RELAY_HEARTBEAT=0) — an idle relay and a dead
  one look identical in this log`. Grep `delivery record:` before reading
  anything into a zero.
- **The beat comes from the human-mail watcher**, and counts every outbound
  surface — the per-agent channel watchers increment the same ledger. So the
  count is "what bridget carried", while the *presence* of a line is specifically
  "the human-DM delivery loop completed a healthy cycle". A channel watcher that
  died on its own would not stop the beat; use `bridget.err.log` for that.
- **The count does not include a repeat the duplication limit held**, because
  that mail reached nothing. It gets its own `dedup:` line in the same file, so
  the two lines together are the whole story and neither overstates.

Volume is bounded in both directions, deliberately: at most one line per hour
when idle (`BRIDGET_RELAY_HEARTBEAT`, default `3600` — 24 lines a day), and at
most one per minute when mail is flowing, with the count folded in. A burst of
33 alarms is one line reading `relay: 33 delivered`, not 33 lines. A record that
buried the exception lines it sits among would have traded one unreadable file
for another.

**It takes effect on the next start of the bridget process**, like the log
stamps below — a bridget that is already up writes no `relay:` lines until it
restarts, so on a running install the first one dates the upgrade.

#### When an outage stops being bridget's problem

The record above makes an outage legible to someone reading `bridget.log`. On
2026-08-19 nobody was. Delivery failed 100% for eight minutes — the same aiohttp
DNS failure on `discord.com:443` that had already happened three times that
fortnight, most recently for 71.6 hours — while the host resolved `discord.com`
5/5 from a shell. It produced no alert, no mail, no event and no change in any
health surface, and `supervise` recorded the eventual exit as `rc=143 after
639957s (healthy run)`. The message stuck in the retry loop was pogod's own
`AGENTS ARE FAILING EVERY TURN` escalation, whose only recipient is the human —
so the fleet-health alarm and the only transport that alarm has failed together.
He found out by noticing that nothing had reacted (mg-3f08).

So past a threshold bridget stops writing the outage down and starts reporting
it, on surfaces that do not depend on the transport that is broken:

```bash
grep 'delivery-wedge' ~/.pogo/bridget.log
[2026-08-19T07:32:00Z] delivery-wedge: 8 mail undelivered for 120s (24 cycles) \
  since 2026-08-19T07:30:00Z — alarm #1 — still retrying; will restart after 300s
[2026-08-19T07:35:00Z] delivery-selfheal: exiting 75 after 300s of failed \
  delivery (60 cycles since 2026-08-19T07:30:00Z); self-heal 1/3 in the last 3600s
```

- **Two surfaces, and the redundancy is the point.** `pogo events emit
  --type=bridget_delivery_wedged` is what a health surface reads; a mail to the
  mayor is what an agent reads. Either alone is a single-recipient escalation
  whose path can break, which is the defect this outage demonstrated live
  (drellem2/pogo#148). **If both refuse, a `delivery-wedge-unreported:` line
  goes to stdout *and* stderr naming what each said** — an escalation path that
  can fail silently is the defect it was built to remove, one level up.
- **The report carries the cause, not just the fact.** The last delivery error
  verbatim, the number of mail waiting, and an in-process resolver probe of
  `discord.com`. That probe is the discriminator the mayor reached for by hand:
  the host resolving fine while this process cannot is what makes it resolver
  state *inside* bridget. It is recorded and never branched on — see
  `bridget_core/wedgewatch.py` for why gating a restart on it is wrong in both
  directions.
- **Then it restarts itself**, exiting `75` for `bridget-supervise`, which names
  that code rather than logging it as a crash. Restarting is cheap and it is the
  only remedy with a measured success rate: all four occurrences ended in one,
  none ended any other way, the supervisor respawns in 5s, and a mail is
  committed to `bridget.seen` only once it has actually landed — so nothing
  behind the wedge is lost.
- **At most three restarts an hour, counted in a file.** An in-memory cap on
  restarts is reset by the restart, which is a flap wearing a rate limiter's
  clothes, so the ledger is `~/.pogo/bridget.selfheal.json`. A genuine network
  outage therefore costs three respawns and then stops, leaving a live bridget
  escalating rather than a flapping one. **A restart that cannot be recorded is
  refused, not taken** — a budget that fails open is not a budget.
- **A refused restart alarms exactly like a granted one.** The moment bridget
  decides it will *not* act is the moment worth reporting; without that, the
  first alarm's "will restart after 300s" would be a promise quietly broken.
- **No self-heal is ever silent**, whatever the knobs say: a restart forces an
  escalation out even with `BRIDGET_WEDGE_ESCALATE_AFTER=0`. A process that
  vanishes and returns with fresh counters leaves the same nothing-happened
  trace as the wedge itself.
- **Grep `delivery wedge watch:` at startup** before reading anything into an
  absence of `delivery-wedge:` lines — the same rule as `delivery record:`, and
  for the same reason.
- **It watches the human-DM loop only.** A per-agent channel watcher failing has
  its own `agent-mail:` record, and a delivery loop that has stopped *iterating*
  raises nothing here — nothing counts cycles that do not happen. That one is
  `~/.pogo/bridget.delivery.heartbeat` going stale. So "no `delivery-wedge:`"
  does not mean "no outage anywhere".

(`~/.pogo/bridget.task-states.json` also advances on a normal poll, but it is
*not* a reliable heartbeat: on an `mg list` timeout the watcher skips the write
and retries, so the task-states mtime freezes during exactly the burst you would
want to detect. `~/.pogo/bridget.seen` is not a heartbeat either — it is only
rewritten when mail actually arrives.)

### Reading the log

Every line bridget and `bridget-supervise` write to `~/.pogo/bridget.log` (and
`bridget.err.log`) is prefixed with an ISO-8601 UTC stamp — the same
`[2026-08-09T09:58:06Z] ` the rest of the pogo fleet's logs use, so one anchored
pattern works across all of them:

```bash
grep -cE '^\[2026-08-09' ~/.pogo/bridget.log     # lines from that UTC day
```

Multi-line messages are stamped on **every** line, continuations included, so a
date grep returns whole messages rather than their first lines. Supervisor lines
read `[<stamp>] supervise: …`.

**This did not used to be true, and the old behaviour is worth knowing about if
you are reading an archived log.** Before v2 (mg-35b1) bridget's own lines
carried no date at all, so `grep -cE '2026-08-0[789]' ~/.pogo/bridget.log`
answered `0` on a live, actively-written file. The zero meant *this format has no
dates*; it read as *nothing happened in those three days*, and nothing in the
output distinguished the two. An investigation into whether a dormant code path
had cost any missed Discord replies could establish the exposure and not the
cost, purely because of that.

Two consequences of how the change lands:

- **It takes effect on the next start of the bridget process**, not on deploy.
  The stamp is installed over the running process's stdout/stderr at startup, so
  a bridget that is already up keeps writing undated lines until it is restarted.
- **Existing log content cannot be retro-stamped** — the dates were never
  recorded, and inventing them would be worse than their absence. Leave the file
  alone; the first `[`-prefixed line *is* the boundary between the two eras, and
  it is unambiguous. If you would rather the timestamped era start in a clean
  file, rotate it **as part of the restart**, never while bridget is running:
  launchd holds an open descriptor on the file it opened, so a `mv` on a live
  log leaves the process appending to the renamed inode while the fresh file
  stays empty — a rotation that looks done and silently is not.

  ```bash
  launchctl bootout gui/$(id -u)/com.pogo.bridget
  mv ~/.pogo/bridget.log ~/.pogo/bridget.log.undated
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pogo.bridget.plist
  ```

### Linux (systemd)

A user unit (`~/.config/systemd/user/bridget.service`) with
`ExecStart=%h/.pogo/bin/bridget` and `Restart=always`, then
`systemctl --user enable --now bridget`. systemd's `Restart=always` is reliable,
so the supervisor wrapper is unnecessary there.

### Quick-and-dirty

`nohup ~/.pogo/bin/bridget >>~/.pogo/bridget.log 2>&1 &`

## Running behind a representative (optional)

A pogo deployment may put a **representative** crew agent between the fleet and
you: it owns `human` as its inbox, strips internal identifiers, makes agents
supply the takeaway, collapses a burst of nine mails about one incident into
one, and writes a separate terminal box.

Three keys matter to bridget when that is running.

**`POGO_MAIL_DIR` — point it at the representative's OUTPUT box.** Not at
`human`. Two reasons, and the second is easy to miss:

1. `human` is now the representative's work queue, not the box a person reads.
2. bridget **moves** what it reads from `new/` to `cur/`. Left pointing at
   `human`, it would silently satisfy the fail-open deadman that watches that
   box for mail nobody processed — the backstop would see a clean queue and
   never fire, and a dead representative would mean silence.

Everything on the **delivery** path follows it: the DM watcher, `cur/`, and
`read <mg-id>`.

**`BRIDGET_APPROVAL_MAILBOX` — leave it alone.** The approval scan is a second
reader with a different job, and it must *not* follow that re-point (mg-18bf).

It used to. Both readers were one variable, so pointing `POGO_MAIL_DIR` at the
representative's output box moved the scan there too — and that box holds
**rewritten** subjects, because rewriting them is the representative's job. Its
prompt forbids holding or dropping an approval request but requires the rewrite,
and forbids internal identifiers in a subject. Nothing it writes matches
`BRIDGET_APPROVAL_RE`, so `status` and `mine` would report

> **Awaiting your approval:** *(nothing)*

on a plate with approvals on it — at exactly the moment you started relying on
the relay, and reading identically to a genuinely empty one.

So the scan resolves against the mail **root** rather than the recipient:
`~/.macguffin/mail/<BRIDGET_APPROVAL_MAILBOX>/new`, default `human`. Move the
whole root and it comes along; re-point the recipient and it stays where the
approval requests are. That is safe because the scan is a *pull view*: it fires
no notification, and it never moves a file out of `new/`, so the fail-open
deadman watching that queue still sees whatever the representative has not
processed.

Its zero now says which zero it is — no such directory, an empty directory, or
mail read and none of it matched (naming the count and both knobs). `settings`
and the startup log print which box it resolved to and whether that differs from
the delivery box.

One gap this does **not** close: `BRIDGET_DM_POLICY=curated` promotes an
approval to a DM using the same regex against mail on the *delivery* path, so
behind a subject-rewriting representative it will not fire either. Use `all`, or
set `BRIDGET_APPROVAL_RE` to match what your representative writes.

**`POGO_REPRESENTATIVE` — the representative's own mailbox.** Setting it files a
marked **copy** of every reply you send into that box.

Outbound agent mail routes through the representative; your replies do not.
They are your own words, and putting a rewriter in the middle of them would be
worse than the problem the representative solves. But with outbound relayed and
inbound invisible, the representative has no record of what it has already told
you or what you have already settled — it re-summarises answered questions, and
agents receive replies referring to text it wrote rather than text they sent.
Thread integrity breaks in one direction only. The copy closes that without
putting the representative in the path of your words.

The copy goes to the representative's own box, never to `human`: `human` is
deadman-watched, so copying your words there would make them a candidate for
being notified back at you whenever the representative ran slow.

The copy is best-effort. Your reply is already delivered by the time it runs, so
a failure is logged to stderr and ignored — it never turns a delivered message
into a reported failure. Register the box first (`mg mail register <name>`); `mg`
refuses a recipient it has never seen.

## Troubleshooting

When bridget is running under a supervisor, stderr is the first place to look.
With the launchd / systemd templates in [Running as a service](#running-as-a-service),
that's whatever path you set for `StandardErrorPath` (launchd) or whatever
`journalctl --user -u bridget` returns (systemd). Foreground runs print
straight to your terminal.

Common failure modes:

- **bridget isn't running, and nothing in the logs says why** — under launchd,
  check `launchctl print gui/$(id -u)/com.pogo.bridget` for a
  `pended nondemand spawn` line. If it is there, launchd has deferred the spawn
  rather than failed it; `runs` will be stuck and `state = not running`.
  `launchctl kickstart gui/$(id -u)/com.pogo.bridget` starts it immediately. See
  [Running as a service](#running-as-a-service) for why this happens.
- **`could not find the mg binary on PATH`** — pogo isn't installed, or its
  `bin/` isn't on the PATH that bridget sees (this is common under launchd,
  which runs with a minimal PATH). Set `MG_BIN` (and optionally `POGO_BIN`)
  in `~/.pogo/bridget.env` to absolute paths.
- **`discord.py is not importable, and there is no venv interpreter`** — you
  are running an interpreter without `discord.py` and bridget could not find a
  venv to hand off to. Run `./install.sh` to build `~/.pogo/venv-bridget`, or
  point `BRIDGET_VENV_DIR` at a venv you manage yourself.
- **`config file not found: ~/.pogo/bridget.env`** — re-run `./install.sh`
  from the repo, or copy `bridget.env.example` to `~/.pogo/bridget.env`
  manually.
- **`missing required key(s) in ~/.pogo/bridget.env`** — fill in the three
  `DISCORD_*` values; they're all required.
- **`DISCORD_USER_ID and DISCORD_SERVER_ID must be integers`** — these are
  Discord *snowflake IDs*, not usernames. Enable Developer Mode in Discord,
  right-click the user / server, and "Copy ID".
- **Bot logs in but never DMs you** — most likely the "Message Content"
  privileged intent isn't enabled on the bot in the Discord developer portal,
  or the bot isn't a member of the server in `DISCORD_SERVER_ID`.
- **No mail notifications** — verify `~/.macguffin/mail/human/new/` exists
  (or whatever you set `POGO_MAIL_DIR` to). bridget skips mail-watching
  silently when the directory is missing.
- **"Awaiting your approval" is empty and you don't believe it** — run
  `settings`. The `Approval scan:` line names the directory being read; the view
  itself distinguishes a missing directory, an empty one, and one whose mail
  matched nothing. If it says *none matched*, either `BRIDGET_APPROVAL_MAILBOX`
  points at the wrong box or `BRIDGET_APPROVAL_RE` doesn't match the subjects
  your fleet writes.
- **The representative never learns what you replied** — `POGO_REPRESENTATIVE`
  names a mailbox `mg` has never seen. `mg mail send` refuses an unregistered
  recipient, so every inbound copy fails; bridget logs one line to stderr per
  occurrence and delivers your reply regardless. Fix with
  `mg mail register <name>`.
- **`restart` says git pull failed** — the bridget checkout has uncommitted
  changes or a divergent branch. Resolve manually in the repo; bridget keeps
  running on the old code in the meantime.
- **Threads aren't being created** — the bot needs *Create Public Threads* and
  *Send Messages in Threads* on `BRIDGET_LOG_CHANNEL_ID`, and the channel must
  be a guild **text** channel. bridget prints the reason at startup and falls
  back to DMing you, so mail is never lost while you fix it.
- **Replies arrive as new top-level mail, not in the conversation** — your `mg`
  predates gh#66 and has no `--in-reply-to`. `settings` will show
  `Correlation IDs: off (detected)`. Upgrade macguffin; nothing else to do.
- **Every mail shows up as several identical threads** — count the
  `watcher set live: N task(s)` lines in the log. `N` must be the *same* after
  every reconnect; if it climbs, a watcher set is leaking and each accumulated
  set delivers the mail once (mg-dc94). Each retirement should log
  `tore down watcher set: …` — a run of `spawned …` lines with no teardown
  between them is the signature. Restarting bridget clears the accumulated
  watchers and returns delivery to 1× immediately. Note that repeated
  `logged in as …` blocks are normal on a flaky network: discord.py re-runs its
  ready path on every gateway reconnect, and reconnecting is the correct
  response to the connection dropping.

## Architecture

bridget is split so that the chat platform is a leaf, not the trunk:

```
bridget_core/          transport-agnostic. Imports no chat library at all.
  mail.py              maildir parsing; Message-Id / In-Reply-To / References;
                       conversation-key derivation
  mailbox.py           observe-only maildir scanning + persisted seen-set
  conversations.py     conversation <-> thread map + message-id index,
                       persisted across restarts
  settings.py          live-reloadable mute / DM-policy state
  ratelimit.py         the duplication limit: fold a repeated alert to the
                       condition it describes, deliver the first occurrence,
                       hold and count the rest
  mgshim.py            the mg CLI seam: detect --in-reply-to, degrade if absent
  acks.py              delivered / ambiguous / undeliverable outcomes

bridget                the Discord presentation adapter: DM cards, guild
                       threads, the command surface, discord.py wiring
```

Everything that would be identical for a Slack or Matrix bridge lives in
`bridget_core`. Porting to another platform is a new adapter, not a rewrite.
`tests/test_core.py` deliberately does **not** stub `discord`, so the split is
enforced by the test suite rather than by good intentions: if a Discord type
leaks into the core, that suite stops importing.

## Project status

**v1.0 — feature parity with the original author's personal install.** Should
work on any macOS or Linux machine with Python 3.10+, pogo installed, and a
Discord bot. Issues and patches that improve portability or add platform
support are welcome.

## Fork status

This repository is a maintained fork of
[cloverross/bridget](https://github.com/cloverross/bridget). It exists so that
operators whose pogo installs diverge from cloverross's defaults — different
agent names, additional notification channels, generalized config — can run a
consistent build without each holding a private fork. Upstream is the
authoritative source for the core single-user DM bridge; this fork layers
configurable defaults and an optional channel-routing mode on top.

### What differs from upstream

- **P1 — env-key generalizations** (commit `f6ef795`, tag `p1-fork-layer`).
  Seven optional env keys with defaults that exactly reproduce upstream
  behavior, so operators with non-default agent names / tags / build scripts
  can diverge via overrides instead of patches:
  `POGO_WORKFLOW_AGENT`, `POGO_INBOX_TAG`, `BRIDGET_POLL_INTERVAL`,
  `BRIDGET_QUIET_RESPECTS_OUTBOUND`, `BRIDGET_APPROVAL_RE`,
  `BRIDGET_RESTART_CMD`, `BRIDGET_CREW_PATTERN`. See
  [Behavioural knobs](#behavioural-knobs) for the table.
- **P2 — per-channel agent routing** (commit `08532ce`, tag
  `p2-fork-layer`). Optional `~/.pogo/bridget.channels.toml` enabling the
  "open-claw" shape — one Discord channel per agent — with bidirectional
  fan-out and a `kinds` filter. See [Per-channel agent
  routing](#per-channel-agent-routing-optional) for the schema and a worked
  walkthrough.

Both layers are strictly additive: empty/missing config = exactly the
upstream behavior. The intent is to land each upstream once it's baked in
operationally; until then, the fork is the staging ground.

### Where the design lives

The architectural rationale (why an env-key layer, why a TOML routing file,
why fork-then-PR rather than PR-first) is in
`docs/bridget-integration-design.md` in the maintainer's pogo repository, not
here — it's design correspondence, not a project artifact for downstream
operators. If you've adopted the fork and want context, request access from
the maintainer (see [AUTHORS](AUTHORS)).

### Upstreaming

The intended trajectory is to PR each layer back to cloverross/bridget once
it's seen a few weeks of operator use. P1 (env-key generalizations) is the
near-term candidate; P2 (channel routing) follows after operational
evidence. Until then, fork divergence is bounded — see git tags
`p1-fork-layer` / `p2-fork-layer` for the cumulative set of fork-only
commits at each layer.

### License compatibility

bridget is GPL-3.0-or-later (see [LICENSE](LICENSE)) and this fork
preserves both the upstream license and the original copyright header in
the `bridget` script. Any redistribution — fork-of-fork or otherwise — must
remain GPL-compatible. Original authorship is recorded in
[AUTHORS](AUTHORS).

## License

GPL-3.0-or-later. See [LICENSE](LICENSE). Authorship and fork lineage in
[AUTHORS](AUTHORS).
