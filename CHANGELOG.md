# Changelog

All notable changes to bridget will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Thread-per-conversation UX and a transport-agnostic core. Every addition is
opt-in: with no new keys set, bridget behaves exactly as v1.x did.

### Added

- **Conversation threads.** `BRIDGET_LOG_CHANNEL_ID` roots one Discord thread
  per mail conversation in a guild text channel. Conversations are keyed on the
  message that rooted them and matched by a message-id index over every message
  bridget has seen — including the replies it sends — so a whole reply chain
  resolves to one thread however many round-trips it runs. The map persists in
  `~/.pogo/bridget.conversations.json` and survives restarts, so threads are
  never orphaned. Unset = threading off.
- **The calm inbox.** `BRIDGET_DM_POLICY` = `all` (default) / `curated` /
  `none`. `curated` DMs only mail that wants a decision; everything else lands
  in the log channel. Refused without a log channel, so suppressed mail always
  has somewhere to go.
- **Reply in thread.** Type into a conversation thread and bridget mails it back
  to the agent on the other end, threaded onto the conversation.
- **Explicit acks.** Every inbound reply resolves to delivered ✅ / ambiguous ⚠️
  / undeliverable ❌. Silence is never an outcome.
- **Live mute/settings.** `settings`, `dm <policy>`, `mute all` / `unmute all`,
  and bare `mute` / `unmute` inside a thread. Persisted to
  `~/.pogo/bridget.settings.json` and hot-reloaded. Muting silences the DM but
  never the thread, so muting cannot lose mail.
- **`bridget_core`** — the transport-agnostic core (maildir watching, mg shim,
  conversation map, settings, acks, state-file writes), importing no chat
  library and rendering nothing. It returns outcomes and facts; the adapter
  turns them into emoji and `**bold**`. A Slack or Matrix port is a new adapter,
  not a rewrite. `tests/test_core.py` does not stub `discord`, and
  `TestCoreCarriesNoPresentation` fails the build if presentation drifts back
  in, so the split is enforced by the suite rather than merely intended.
- **`BRIDGET_CORRELATION_IDS`** (`auto` / `on` / `off`) — whether to thread
  replies with `mg mail send --in-reply-to` (macguffin gh#66). `auto` probes
  `mg mail send --help` once. Replies always deliver; without the flag they
  simply arrive as new top-level mail. A send rejected with `unknown flag`
  downgrades the capability and retries once, so an `mg` swapped underneath a
  running bridget never surfaces as a spurious undeliverable.
- **`install.sh --setup`** — masked prompts for the Discord credentials. Token
  entry has terminal echo off and **no part of the token is echoed back** (a
  partial token is still a leaked token); the installer validates its *shape*
  instead, so a paste error is still caught. The value never reaches `argv` or
  shell history.
- **`tests/test_secrets.py`** — fails the build if a Discord-token-shaped string
  or any real secret value is committed, if a config key is undocumented, or if
  `TOKEN` is referenced anywhere but `client.run`.

### Security

- **Nothing bridget posts can ping anyone.** `discord.Client` was constructed
  without `allowed_mentions`, so a mail subject containing `@everyone` or `@here`
  pinged the server if the bot had Mention Everyone. Everything bridget renders
  is text somebody else wrote — mail subjects and bodies composed by agents, and
  `mg`'s own output — so mentions are now suppressed at the client, where it
  holds for every send rather than at each call site that has to remember.
- **State files are owner-only.** `~/.pogo/bridget.{conversations,settings,
  task-states,idea-claims}.json`, `bridget.seen`, and `quiet.json` were written
  at `0644` under the default umask. None hold the bot token, but they do hold
  mail subjects, agent names, and which conversations you muted. All six now go
  through one atomic writer that creates the temp file `0600` *before* it holds
  anything. `install.sh` tightens `~/.pogo` itself to `0700`.
- **`.gitignore` covers `*.env`.** The real env file lives at
  `~/.pogo/bridget.env`, out of tree — but `cp bridget.env.example bridget.env`
  in the checkout is the obvious thing to try, and the next `git add -A` would
  commit a bot token. (`bridget.env.example` is unaffected.)

### Fixed

- **Every documented way to run bridget crashed on `import discord`.** The
  shebang is `/usr/bin/env python3`, so the `~/.pogo/bin/bridget` symlink from
  the quick start, the launchd plist and the systemd unit all ran under the
  *system* interpreter — which has no `discord.py`. It is in the venv
  `install.sh` builds, and nothing ever used that venv. bridget now re-execs
  itself into `BRIDGET_VENV_DIR` (default `~/.pogo/venv-bridget`) when
  `discord` is not importable, guarded against an exec loop, and still reports
  config errors under whichever interpreter you started it with. An install
  that put `discord.py` on the system interpreter (`install.sh --no-venv`)
  imports it directly and never re-execs.

- **Threading no longer collapses after the first round-trip.** Conversations
  were keyed on `References[0]`, assumed to be the reply-chain root. It is not:
  `mg mail send --in-reply-to X` seeds `References: [X]` — the parent — and it
  is the primitive both bridget and the agents reply through. The second and
  every subsequent inbound mail therefore rooted a fresh Discord thread, mutes
  keyed on the old conversation stopped applying, and the conversation map grew
  one entry per message. No mail was ever lost. Conversations are now matched by
  a message-id index, and bridget records the id `mg` assigns each reply it
  sends — the agent's answer names that id and nothing older.
  `tests/test_mg_threading.py` drives a real `mg` through two round-trips, under
  both commands an agent answers with (`mg mail send --in-reply-to` and `mg mail
  reply`, which lose the root at different hops); the hand-authored `References`
  fixtures that let this ship could not have caught it, because none of them
  carried the header shape `mg` actually writes.

  A consequence worth stating plainly: **conversation identity no longer depends
  on the subject line.** Nothing keys on it. A reply whose first line becomes the
  outbound subject could not have moved a message out of its conversation. That
  made the multi-line-reply behaviour purely cosmetic, and free to decide on its
  own merits — see "a multi-line reply in a thread" under Changed.

- **Mail delivery is at-least-once, and a redelivery cannot duplicate a thread
  post.** `poll()` persisted the seen-set *before* the caller delivered.
  `unsee()` covers a graceful send failure but structurally cannot cover a
  SIGKILL, OOM, or power loss in the window between the seen-set hitting the
  disk and the mail hitting Discord — a killed process does not get to run
  `unsee()`. That mail was marked seen forever and, because bridget is
  observe-only, it stayed unread in `new/` where nothing would ever surface it
  again. The watcher now commits a mail only once it has landed. Making
  redelivery routine exposed a second bug: `post_to_thread` runs before
  `user.send`, so a failed DM re-posted the same mail into the thread. The
  conversation store tracks which messages it has already rendered. The DM
  itself is not deduplicated — Discord offers no idempotency key — so the trade
  is explicit: a duplicate DM is recoverable by reading it twice, a lost mail is
  not. In the same spirit, a mail that fails to *read* (an EIO or EACCES from a
  network filesystem) is now retried across a few polls before the watcher gives
  up on it, rather than being marked seen — and so lost — on the first bad read.
  And a redelivery whose thread the human has since deleted re-roots the thread
  and posts there, instead of trusting a record about a thread that is gone.
- **A code block cannot be escaped by the text inside it.** A mail body
  containing a triple backtick closed bridget's code fence early, and the
  remainder rendered as raw markdown. Agents post code constantly, so this was
  the common case, not the adversarial one. Truncation of a long mail now
  happens inside the fence, too — chopping the rendered string is how the
  closing fence used to get lost.
- **A header with an empty value no longer swallows the headers after it.**
  `_split_headers` keyed on `': '` (colon-space), but RFC 5322 makes that space
  optional; `Subject:` read as a body line, taking `In-Reply-To` with it, and
  the mail rooted a fresh thread. Latent rather than live — `mg` requires a
  non-empty `--subject` — but not a property worth depending on.

### Changed

- **A multi-line reply in a thread keeps the conversation's subject.** Typing
  two lines into a thread used to send the first as `--subject` and the rest as
  `--body`, mirroring the `mail` verb. Both lines are now body, and the subject
  is always `Re: <conversation subject>`. Composing and replying are different
  acts: a new mail needs a subject and the human supplies it; a thread reply
  does not, because the subject is on the thread they are looking at. The old
  behaviour broke subject continuity for the agent and put the human's first
  sentence in its inbox as a non-sequitur. `send_channel_chat_mail` — free-form
  text in a mapped channel, where the human *is* composing — still splits.

- **The core no longer renders.** `Ack` carries `kind` plus the facts behind it
  and no longer truncates a subject or a stderr dump; `SettingsStore.describe()`
  became `summary()`, a dict; `thread_title(limit=...)` no longer bakes in
  Discord's 100-character thread-name cap. `render_ack()` and `render_settings()`
  in the adapter hold everything that moved. Internal — no user-visible change.
- **`install.sh --no-venv`** skips venv creation and the dependency install, the
  only step that needs the network. It exists so the installer can be tested by
  actually running it (`tests/test_install.py`), and is useful if you manage the
  venv yourself.
- **Every source file carries a copyright line, an SPDX tag, and the GPL's
  warranty disclaimer**, with a GPL-3.0 §5(a) modification notice on the files
  that came from upstream. `AUTHORS` describes what is there rather than
  overstating it.

- `install.sh` now tightens `~/.pogo/bridget.env` to `600` on every run, not
  only the run that created it. bridget warns at startup if it is readable
  beyond its owner.
- The maildir watchers are now explicitly observe-only, sharing one audited
  implementation (`bridget_core.mailbox`). They read `new/` and never move
  anything out of it; `mg mail read` owns that transition. The `dismiss` and
  `read` commands still mark mail read, because the user asked them to.
- The seen-set is written atomically and garbage-collected by *presence* — a
  filename is forgotten only once its message has left `new/`. It is never
  trimmed by age: because reads are observe-only, a delivered message stays in
  `new/`, so dropping its name re-delivers it on the next poll and every poll
  after that.
- Delivery is at-least-once. A Discord send failure (rate limit, 5xx) now
  un-sees the mail and retries on the next poll instead of consuming it.
- With no log channel, `mute all` and quiet hours stop the watcher consuming
  mail rather than swallowing it, so it is delivered once you are audible again.
- Mail suppressed by quiet hours is no longer invisible when threading is on:
  it still lands in the log channel.
- Inside a conversation thread, only unambiguous commands (a workflow verb with
  an mg-id, or an `idea:`/`bug:` prefix) are treated as commands. Everything
  else is a reply, so "status is green, ship it" reaches the agent instead of
  printing a status dump, and "dismiss all of that" cannot inbox-zero the human.
- Bare `unmute` now explains itself instead of unmuting every DM, mirroring
  bare `mute`.

## [1.0.0] - 2026-05-09

Feature parity with the original personal pogo-discord-bridge install. First
release suitable for external use.

### Added

- `quiet <true|false> [HH:MM HH:MM]` — toggle agent quiet hours; writes shared
  system state to `~/.pogo/quiet.json`.
- `nudge <agent> [reason]` — wake a stalled agent via `pogo nudge`. Adds
  `POGO_BIN` env key.
- `bug: <text>` and `bug: [tag] <text>` — file a bug-type work item (mirrors
  the `idea:` parser).
- `mail <subject>\n<body>` — send a mail to a configurable recipient. Adds
  `POGO_MAIL_RECIPIENT` env key (default: `mayor`).
- `agents` — list crew agents with status, health, and last/next cycle data
  (reads `~/.pogo/agent-status/<name>.json`).
- Task transition notifications — Discord DMs on task claim/done/shelve.
  Cache: `~/.pogo/bridget.task-states.json`.
- Idea claim notifications — Discord DMs when the architect claims a new
  idea. Cache: `~/.pogo/bridget.idea-claims.json`.
- `restart` — `git pull` + syntax check + `os._exit` so the supervisor
  (launchd / systemd) respawns from the updated tree. Adds optional
  `BRIDGET_REPO_DIR` env key (defaults to self-detect from `__file__`).
- `balance` — check whether any agent is hitting Anthropic credit-balance
  errors (regex-matches `recent_output_tail`; known limitation: false
  negatives on ANSI-encoded output, deferred to a future v1.x).

### Documentation

- README sweep: complete Commands list, Configuration table, Quick start, and
  Troubleshooting sections.
- `bridget.env.example`: covers all current env keys with comments.
- `CUTOVER.md`: step-by-step migration guide for users coming from the
  personal pogo-discord-bridge install (also useful as a fresh-install
  walkthrough).

## [0.1.0] - 2026-05-07

Initial scaffold. Generalized pogo↔Discord bridge with env-driven config:
`approve`, `reject`, `revise`, `explain`, `next`, `read`, `idea:`, `dismiss`,
`status`, `help`. GPL-3.0 license.
