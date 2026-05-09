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

Gaps between bridget (`~/DUGLocal/bridget/bridget`) and the retiring personal bridge (`~/DUGLocal/pogo-discord-bridge/pogo-discord-bridge`). Compiled 2026-05-09 after the cutover.

Citations are `file:line`. "Filing line" = the exact text Clover can paste into Discord; bridget's parser handles `bug:` and `idea:` prefixes.

---

#### P0 — Confirmed regressions (parity blockers)

##### 1. `status` doesn't list mg-IDs of unread mail
- **Old:** `pogo-discord-bridge:522-532` extracts mg-IDs from each unread mail's subject/body and renders inline (`• [mg-abc, mg-def] architect: design approval request`). Shipped as `mg-25e6` on 2026-05-07.
- **New:** `bridget:615` shows count only. The extraction loop was dropped.
- **Filing:** `bug: status command shows count of unread mail but not mg-IDs (regression of mg-25e6)`

##### 2. `read mg-XXXX` doesn't auto-mark mail as read
- **Old:** `pogo-discord-bridge:704-710` moves the file from `new/` → `cur/` after rendering. Shipped as `mg-7154`.
- **New:** `bridget:729` returns content without the rename — mail keeps showing unread on next `status`.
- **Filing:** `bug: read mg-XXXX no longer marks mail as read after viewing (regression of mg-7154)`

##### 3. `status` doesn't nudge the mayor
- **Old:** `pogo-discord-bridge:940` calls `run_pogo(['nudge', 'crew-mayor', 'status check from human via bridge'])`. Shipped as `mg-68bf`.
- **New:** `bridget:1037` returns the summary without the nudge.
- **Filing:** `bug: status command no longer nudges crew-mayor (regression of mg-68bf)`

##### 4. README must surface the roadmap + known bugs list
- New bridget users currently have no in-repo signal of v2 priorities or known bugs — they have to discover issues by hitting them. The roadmap and the live `bug`-tagged work-item list should both be reflected in `~/DUGLocal/bridget/README.md`, and should stay current.
- **Required behavior:** on every push to bridget that adds/changes a roadmap item or known bug, the README sections must be updated in the same PR. Encode this as a contributor expectation (CONTRIBUTING.md or PR template) so it can't drift.
- **Filing:** `idea: bridget README must include the v2 roadmap + known-bugs list, kept current on every push (contributor expectation enforced via CONTRIBUTING/PR template)`

---

#### P1 — Hidden bugs surfaced by retiring the personal bridge

The personal bridge's hardcoded paths were masking a class of bridget bugs: optional env vars stayed unset because nobody hit the failure path while both bridges ran. After cutover, every user-facing command that depends on those vars silently 404s with a "set the env var" error, until the user populates `~/.pogo/bridget.env`.

**Concrete symptom on Clover's machine (2026-05-09 post-cutover):** `~/.pogo/bridget.env` shipped as the example template + 3 required Discord credentials, with every optional path commented out. `idea:`, `bug:`, and `next` Discord commands were all returning `✗ ... is unavailable: set ...` errors. Resolved at session-time by uncommenting `POGO_DESIGNS_DIR` and `POGO_INBOX_REPO` in `~/.pogo/bridget.env` and restarting bridget. **Other unreviewed users will hit the same wall.**

##### 5. v2 design: zero-config sensible defaults
- **Principle:** every optional env var should have a sane default that makes bridget work out-of-the-box for a fresh install. Env override remains for power users (Clover with iCloud paths, deployments, etc.).
- **Clover's preferred default shape:** "wherever the bridget script lives on disk." Likely interpretations: defaults adjacent to `~/.pogo/` (e.g. `~/.pogo/designs/`, `~/.pogo/inbox/`) or relative to `BRIDGET_REPO_DIR`. Final pick is a v2 design call — what matters is that *something* works without env config.
- **Vars to design defaults for:**
  - `POGO_DESIGNS_DIR` — currently no default → `next` and design-reading fails
  - `POGO_INBOX_REPO` — currently no default → `idea:`, `bug:`, `next` fails
  - (review remaining optional vars during design — startup-time discovery is the trap)
- **Audit suggestion:** v2 design should include a "fresh-install smoke test" — boot bridget with only the 3 required Discord vars set, exercise every command, confirm none returns a "set this env var" error. The test would have caught this hours after the personal bridge was hardcoded.
- **Filing:** `idea: bridget v2 — sensible zero-config defaults for POGO_DESIGNS_DIR and POGO_INBOX_REPO so fresh installs work out of the box (the personal bridge's hardcoded paths were masking the gap; surfaced post-cutover when idea:/bug:/next commands all 404'd)`

---

#### P2 — Hardening / polish (file when v1 parity is in)

##### 6. Startup welcome message coverage
- `bridget:356-371` mentions `agents`, `balance`, `restart`, `bug:` — but not `nudge`, `quiet`, `next`, `explain`, `read`, `dismiss`. The personal bridge was even less comprehensive, so this isn't a regression — but it's worth a one-line filing for completeness.
- **Filing:** `idea: expand bridget's startup welcome message to cover all commands (nudge/quiet/next/explain/read/dismiss missing)`

##### 7. Document `POGO_INBOX_REPO` / `POGO_DESIGNS_DIR` in install flow
- README/install.sh should call out these env vars explicitly so users don't hit the silent-404 trap before P1 ships. Currently the `bridget.env.example` mentions them but install.sh doesn't actively prompt or warn.
- **Filing:** `idea: install.sh should warn or prompt when POGO_INBOX_REPO/POGO_DESIGNS_DIR are unset (until v2 sensible-defaults ships)`

---

#### Cleared (no action needed)

- **mg-afd8 architect-claim ping**: present in bridget at `bridget:407-452`, behaviorally equivalent to `pogo-discord-bridge:307-352`. Not a regression.
- **mail watcher seen-priming on startup**: present in bridget at `bridget:350-354`, equivalent to `pogo-discord-bridge:185-187`.
- **revise-marks-all-read**: present in bridget at `bridget:706`, equivalent to `pogo-discord-bridge:623`.
- **mail recipient configurability** (`POGO_MAIL_RECIPIENT`): bridget already configurable, personal bridge was hardcoded. Improvement, not regression.
- **`next` and `explain` commands**: bridget has them, personal bridge never did. New features.
- **Config validation, config defaults, status truncation budget, hardcoded-path elimination**: all OSS improvements bridget already shipped.

### Known bugs

Current open and in-flight bugs against `~/DUGLocal/bridget/bridget`. Maintained alongside mg state (the maintainer's local work tracker); update this file in the same PR that adds, dispatches, or closes a bug.

#### In flight (fix in PR, not yet landed)

| mg-id | Status | Summary |
|---|---|---|
| mg-3fe5 | polecat task mg-4e83 | `status` command shows count of unread mail but not mg-IDs (regression of mg-25e6) |
| mg-1a65 | polecat task mg-602d | `read mg-XXXX` no longer marks mail as read after viewing (regression of mg-7154) |
| mg-3782 | polecat task mg-fad3 | `status` command no longer nudges crew-mayor (regression of mg-68bf) |

#### Open (deferred to v2 design)

| mg-id | Summary |
|---|---|
| mg-d531 | mail-read state desync after credit outage — bridge keeps moving mails to `cur/` while mayor is stalled |
| mg-db58 | `balance` command false-negative on credit errors — returns ✅ when credit-exhaustion text is present in agent output |
| mg-ddb5 | `nudge` falsely reports agents active when credits exhausted — returns ✓ on PTY delivery without checking diagnose state |

## Quick start

1. Clone and run the installer:
   ```bash
   git clone https://github.com/CloverRoss/bridget.git
   cd bridget
   ./install.sh
   ```
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

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
