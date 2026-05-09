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
| `POGO_MAIL_DIR`      | no  | Parent of `new/` and `cur/`. Default: `~/.macguffin/mail/human`. |
| `POGO_DESIGNS_DIR`   | no  | Directory of `mg-XXXX.md` design docs (read by `next`). Default: `~/.pogo/designs`. |
| `POGO_INBOX_REPO`    | no  | Repo where `idea:`, `bug:`, and `next` file new items. Default: `~/.pogo/inbox`. |
| `POGO_MAIL_RECIPIENT` | no | Default recipient for `mail` command. Default: `mayor`. |
| `BRIDGET_REPO_DIR`   | no  | Override for the bridget git checkout. Default: self-detected from the script's location (works for the install.sh-managed symlink). |

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

Process environment variables override values in the env file, so a
launchd/systemd unit can inject overrides without editing the file.

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
- `mail <subject>\n<body>` — send a mail to the configured recipient (default `mayor`; override via `POGO_MAIL_RECIPIENT`). Without a newline, the whole text becomes the subject.
- `dismiss mg-XXXX` — mark all unread mail about an mg-id as read.
- `dismiss all` — inbox-zero everything.
- `status` — global pull view (unread mail + in-flight work).
- `agents` — list crew agents and health.
- `balance` — check whether any agent is hitting credit balance errors.
- `nudge <agent> [reason]` — wake a stalled agent.
- `restart` — git pull + restart bridget (after merging a PR; see [Remote restart](#remote-restart)).
- `quiet <true|false> [HH:MM HH:MM]` — toggle agent quiet hours (default 23:00–06:00).
- `help` (or `?`) — print this list inside Discord.

bridget only acts on DMs from the user whose ID is in `DISCORD_USER_ID`;
messages from anyone else are ignored.

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
snowflake = "1234567890123456789"
agent     = "mayor"
direction = "both"                            # optional
kinds     = ["mail", "task-transitions"]      # optional
```

| Field | Required | Purpose |
|---|---|---|
| `snowflake` | yes | Discord channel ID, as a quoted string of digits. |
| `agent` | yes | Pogo agent name. Inbound non-verb messages are mailed to this agent; outbound events for this agent fan out to the channel. |
| `direction` | no (default `both`) | `inbound`, `outbound`, or `both`. |
| `kinds` | no (default all) | Subset of `["mail", "task-transitions", "idea-claims"]`. Controls which outbound classes fan out to this channel. |

The `<name>` in `[channels.<name>]` is a local label used only in error
messages — it does not have to match the agent name or the channel name.

### Routing rules

- **Inbound (channel → agent).** Workflow verbs (`approve` / `reject` /
  `revise` / `explain` / `next` / `idea:` / `bug:`) keep routing through
  `POGO_WORKFLOW_AGENT` exactly as they do in DMs — design coordination
  doesn't change identity based on which channel you typed in. Free-form text
  in an inbound-mapped channel becomes `mg mail send <channel-agent>` with
  the first line as subject and the rest as body.
- **Outbound (agent → channel).** When a watcher would normally DM the user
  about an event involving an agent that has an outbound mapping, bridget
  posts to the mapped channel *instead of* DMing — so channels declutter your
  DMs rather than duplicate them. Events whose agent has no mapping (or whose
  `kind` is excluded) fall back to DM, exactly as in v1.0.0.
- **Bot setup.** The bot must be a member of the guild containing each mapped
  channel and have permission to read message history and send messages
  there. The required Discord intents (`guilds`, `guild_messages`) are
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

For v0.1, bridget is just a long-running Python process — supervise it however
you'd supervise any other foreground service. A few options:

- **macOS (launchd):** wrap `~/.pogo/bin/bridget` in a `~/Library/LaunchAgents/`
  plist with `RunAtLoad`, `KeepAlive`, and `StandardOutPath` /
  `StandardErrorPath` set to log files under `~/.pogo/`.
- **Linux (systemd):** a user unit (`~/.config/systemd/user/bridget.service`)
  with `ExecStart=%h/.pogo/bin/bridget`, `Restart=always`, then
  `systemctl --user enable --now bridget`.
- **Quick-and-dirty:** `nohup ~/.pogo/bin/bridget >>~/.pogo/bridget.log 2>&1 &`.

Bundled launchd / systemd templates and an `install.sh --service=...` flag are
on the roadmap; for now, write the unit yourself. PRs welcome.

## Troubleshooting

When bridget is running under a supervisor, stderr is the first place to look.
With the launchd / systemd templates in [Running as a service](#running-as-a-service),
that's whatever path you set for `StandardErrorPath` (launchd) or whatever
`journalctl --user -u bridget` returns (systemd). Foreground runs print
straight to your terminal.

Common failure modes:

- **`could not find the mg binary on PATH`** — pogo isn't installed, or its
  `bin/` isn't on the PATH that bridget sees (this is common under launchd,
  which runs with a minimal PATH). Set `MG_BIN` (and optionally `POGO_BIN`)
  in `~/.pogo/bridget.env` to absolute paths.
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
- **`restart` says git pull failed** — the bridget checkout has uncommitted
  changes or a divergent branch. Resolve manually in the repo; bridget keeps
  running on the old code in the meantime.

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
