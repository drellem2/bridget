# Changelog

All notable changes to bridget will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Thread-per-conversation UX and a transport-agnostic core. Every addition is
opt-in: with no new keys set, bridget behaves exactly as v1.x did.

### Added

- **Auto-create per-agent channels.** A `[channels.<name>]` entry may now omit
  `snowflake`: on startup bridget adopts an existing text channel of the target
  name or creates one (needs *Manage Channels*), wires it to the agent, and
  persists the resulting ID to `~/.pogo/bridget.channel-ids.json` so restarts
  resolve the same channel and never duplicate it. The registry ID wins over a
  later hand-typed snowflake for the same name, and an unresolvable snowflake
  now falls through to resolve-by-name (adopt-or-create) instead of retrying a
  dead ID forever. A new `channel` key overrides the created channel's name.
  Already-valid snowflakes are untouched — static routing is unchanged. Removes
  the manual snowflake hand-wiring that was the setup friction (mg-2fea, Slice
  A). Per-topic channels (Slice B) remain out of scope.
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
- **A launchd agent, and a supervisor that does the work `KeepAlive` claims to.**
  `install.sh --launchd` renders `com.pogo.bridget.plist.example`, bootstraps it,
  and kickstarts it. The job runs the new `bridget-supervise` wrapper, which
  restarts bridget when bridget exits.

  The kickstart and the wrapper both exist for the same measured reason: launchd
  defers *nondemand* spawns — `RunAtLoad`, `KeepAlive` restarts, `StartInterval`
  fires — reporting `pended nondemand spawn = inefficient` (or `speculative`). A
  `SIGTERM`ed job was observed still `not running` 115 seconds later; two sibling
  agents sat that way for ~4.8 hours until kickstarted by hand; and `bootstrap`
  alone leaves a fresh job at `runs = 0` indefinitely. Only `launchctl kickstart`,
  a demand spawn, is never pended. So the wrapper keeps launchd out of the restart
  path entirely. `StartInterval` and `ProcessType` are deliberately absent from
  the plist: both were tested, neither defeats the pending. See "Running as a
  service" in the README, which also documents the one residual case a plist
  cannot fix.
- **`tests/test_launchd.py`** — covers the wrapper's restart loop, backoff, and
  SIGTERM forwarding, plus the plist template: that it parses under a *strict*
  XML parser (`plutil -lint` accepts a `--` inside a comment; `plistlib` does
  not), carries no secrets, and still omits `StartInterval` / `ProcessType`.
- **`tests/test_task_transitions.py`** — covers the task-transition diff,
  including the duplicate-id regression below.

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

- **The task-transition watcher died silently on a transient `mg list`
  timeout.** A single `mg command timed out` — which fires even at rest, where
  `mg list` benchmarks at ~0.01s, so it is a transient flake and not contention —
  could stop `watch_task_transitions` while the bridget *process* stayed alive
  and logged in. launchd/KeepAlive and `bridget-supervise` only react to process
  *exit*, so nothing restarted it: the pogo channel went silent for 44 minutes
  until a manual `launchctl kickstart`. The watcher now catches the timeout,
  backs off (growing from one poll interval, capped), and retries — the thread
  never exits on a timeout.

  It also touches a dedicated liveness heartbeat, `~/.pogo/health/bridget.heartbeat`,
  at the top of **every** cycle (including timeout cycles), so a dead watcher is
  detectable as a stale mtime — the exact signal pogod's tier-1 reaper (mg-d18b)
  keys on, closing the KeepAlive blind spot. Declare the job as
  `com.pogo.bridget|~/.pogo/health/bridget.heartbeat|<period>` under `[reaper]`
  to have a stale watcher kickstarted automatically. `tests/test_watcher_liveness.py`
  injects one timeout and proves the watcher keeps posting and keeps ticking,
  then kills the watcher by PID and proves the heartbeat goes stale. Timeout
  *tuning* was deliberately not treated as the fix: the trigger is a flake, so
  the thread must survive regardless of the timeout value (mg-3499).

- **Inbound messages were silently truncated at 200 characters.** Free-form chat
  in a mapped channel put the *entire message* in `--subject` whenever it had no
  newline, leaving the body as the literal string `(no body)`. `build_send_args`
  then sliced that subject to 200 characters with a bare `subject[:200]` — no
  error, no marker, `mg` exiting 0. Subjects are bounded; bodies are not.

  This is not cosmetic. A reply authorizing a repository deletion reached the
  mayor as *"Sleep wake is not a critical repo you can delete and recreate if"*
  — stopping mid-clause, on the condition, and reading as a complete sentence.
  **A truncated authorization can invert its own meaning:** "you can delete and
  recreate *if* ⟨condition⟩" and "you can delete and recreate" are different
  instructions, and only the second one arrived. Nothing was destroyed, because
  the mayor declined to act irreversibly on a sentence that stopped mid-clause.
  That caution should not have been the only safeguard.

  The 200-character cap was bridget's own invention, not a limit it was obeying:
  mg stores a 500-character subject and returns it intact, which
  `test_mg_itself_stores_an_overlong_subject` now pins. So the fix does not need
  to split or mark anything — it needs to stop putting the payload in the
  header. `compose_subject_body` now guarantees that **every byte the human
  typed survives into the subject or the body**, and `build_send_args` never
  truncates a body. The subject became what it always was: a bounded, one-line
  label — control characters, which mg rejects in a header, become spaces while
  ordinary spacing is left as typed — and, when it must be shortened, elided
  with an explicit, truthful marker rather than a bare slice:

      … [truncated 4843 chars; full text in body]

  A bare `[:200]` leaves a grammatical sentence that merely happens to stop
  early, and the reader gets no signal. That matters more than it sounds,
  because truncating an instruction is biased toward the dangerous reading:
  English puts the imperative first and the guard clause last ("delete it
  *unless* …", "go ahead, *but* not production"), so clipping the tail strips
  the condition and keeps the command. The result stays syntactically
  plausible. The marker is the signal that a condition may have been stripped.

  Channel chat no longer takes its subject from the first line either — a
  reversal of an earlier decision. A person chatting has no subject line, only a
  first sentence, and promoting that sentence to the subject is the same defect
  wearing a different hat. The `mail` verb still honours an explicit
  `subject⏎body` split, because there the human really is composing mail, and it
  now routes through `build_send_args` instead of hand-rolling its own argv.

- **Outbound task-title labels were bare-sliced with no truncation marker.** The
  inbound fix above stopped the *dangerous* case (a payload silently clipped in
  a mail header). Its point-3 audit noted the outbound path — an mg task/idea
  title rendered onto a Discord card — is safer, because the full title always
  survives in the mg item the card names. But three of those renders trimmed the
  title with a bare `(title or '?')[:80]`: transition announcements
  (`format_transition`), idea-claim announcements, and the status board's
  in-flight list. A label may be trimmed, but the trim must be *visible* — a bare
  slice clips mid-word and reads as a whole sentence, exactly the mg-7615 card
  whose title stopped at *"…I want it to be mo"*. If that card is the only place
  the task is seen, a reader acts on a sentence that ends at "mo". All three now
  route through `_elide`, so an over-long title ends with a `…` the reader can
  act on — the same visible-marker principle as the inbound fix, sized for a card
  title where the mg id already points at the full text. (mg-2635)

- **A message the human *sent* echoed back bare-sliced, with no `…` marker.**
  Daniel reported (2026-07-10) that a long DM he sent came back looking cut off,
  while the mail he *receives* is correctly marked (mg-2635). The report was a
  real asymmetry: the payload was never in danger — `mail`/`idea:`/`bug:` DMs and
  mapped-channel chat all reach the agent's `--body` verbatim, as
  `test_dm_echo.py`'s `InboundPayloadIsVerbatim` now pins — but four *labels*
  derived from that text were still trimmed with a bare slice. The `mail` and
  channel-chat acks echoed `subject[:60]`, and the `idea:`/`bug:` verbs cut the
  mg item title with `body.splitlines()[0][:60]`. Each clips the tail with no
  signal, so a person watching their own instruction come back stopped mid-clause
  reads it as complete — the same dangerous reading mg-7e0c and mg-2635 close,
  arriving on the one surface still missing the marker. All four now route through
  `_elide`, so an over-long echo or title ends with a `…`; the full text still
  reaches the agent, and the label finally admits it is only a label. (mg-3f94)

- **`bridget-supervise` could pin itself to a deleted worktree and respawn into
  a FATAL forever, silently.** `BRIDGET_BIN` was resolved once at startup and
  re-exec'd unchanged on every restart, so a supervisor started against
  `~/.pogo/polecats/<id>/bridget` kept that path after the worktree was reaped:
  launchd (`KeepAlive`, `ThrottleInterval=10`) then respawned it into
  `FATAL: no bridget at …` every ten seconds until a human re-pointed the path
  by hand — eighteen minutes, on 2026-07-10. It emitted no alert, and neither
  `launchctl list` nor `pogo doctor --check` could see it, because both ask
  whether a process exists and a respawn loop always has one. Three changes,
  because none of them subsumes the others: the supervisor refuses to exec any
  target resolving under `~/.pogo/polecats/` (`BRIDGET_ALLOW_EPHEMERAL_BIN=1`
  overrides, for smoke-testing a build in place); it re-checks the target before
  *every* spawn rather than pinning it, which is what catches a path that goes
  away — or a `~/.pogo/bin/bridget` symlink that gets repointed into a worktree
  — after startup; and when it rejects the target it falls back to the durable
  `~/.pogo/bin/bridget` and keeps supervising instead of dying. A `BRIDGET_BIN`
  merely *missing at startup* still refuses to start, since nothing is running
  yet and substituting a different binary for the one the operator named would
  be its own bug. Above all it is now loud: any target it will not exec is
  logged to stderr as well as stdout and mailed to `mayor`, rate-limited to one
  per `BRIDGET_ALERT_COOLDOWN` (900s) through an on-disk stamp, since every
  respawn is a fresh process and 360 mails an hour would be its own silence.

- **`bridget-supervise` ignored SIGTERM for the whole of its restart backoff.**
  bash runs a trap only once the current *foreground* command returns, so the
  backoff's `sleep "$backoff"` deferred the `on_signal` handler for up to
  `BRIDGET_MAX_BACKOFF` (300s default); 2m44s was measured. That contradicted
  the script's own promise that `launchctl bootout` and `kickstart -k` work, and
  it was not merely slow: launchd escalates an unanswered SIGTERM to SIGKILL, so
  stopping or restarting the job mid-backoff killed the supervisor and left
  bridget running with nothing watching it — the exact failure the wrapper
  exists to prevent. The sleep now runs in the background under `wait`, which
  *is* interruptible, and the handler reaps it instead of orphaning it. The
  existing signal tests all happened to signal the wrapper while bridget was up,
  where it parks in `wait "$child"`; the backoff was the untested half.

- **bridget re-sent the same task transitions to the user's DM every five
  seconds, forever.** `mg list --json --all` emits some ids *twice* — once live,
  once as an archived tombstone (`mg-4b2a`, `mg-7387` and `mg-913e` each appeared
  as both `shelved` and `archived`). `watch_task_transitions` diffed and assigned
  `states[tid] = status` line by line, so it announced the first record and then
  stored the second; the next poll saw the stored status contradicted by the
  first record again and re-announced. About 95 of the last 100 DMs in the
  maintainer's inbox were three "📦 shelved" messages on repeat.

  The listing is now reconciled to one status per id *before* the diff, in the
  new `reconcile_task_states`. An id whose records disagree is recorded but never
  announced, which also makes the outcome independent of the order `mg` happens
  to emit duplicates in. Genuine transitions still announce exactly once.
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
