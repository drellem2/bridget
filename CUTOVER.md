# Cutover

This is the runbook for switching a running Pogo↔Discord bridge over to
bridget. It serves two audiences:

- **Existing user migrating from `pogo-discord-bridge`** (the personal
  bridge install). Follow every section. The env migration in §3 and the
  launchd plist edit in §4 are the load-bearing steps; everything else is
  verification.
- **Fresh installer with no prior bridge** (e.g. Daniel on a new mac). Skip
  §1 and the env-migration table in §3 — instead, `git clone` the repo and
  fill in the three required `DISCORD_*` values from scratch. §2 (install.sh),
  §4 (launchd plist), and §6 (smoke test) still apply. §7 and §8 are no-ops.

> Throughout this runbook, substitute your bridget checkout path for any
> `<bridget>` placeholder, and your home directory for `/Users/yourname`
> in launchd plist examples.

The personal `pogo-discord-bridge` install is **not touched** by this
procedure — rollback (§8) is just a plist edit and a launchd reload.

## 1. Pre-flight check

Verify all 9 feature ports are on `origin/main`. Expected commit subjects
(any order):

- `mg-90e6` — port `quiet` command
- `mg-77d7` — port `nudge` command
- `mg-6a13` — port `bug:` prefix parser
- `mg-dd6f` — port `mail` command
- `mg-f6ba` — port `agents` command
- `mg-afdd` — port `watch_task_transitions`
- `mg-afa8` — port `watch_idea_claims`
- `mg-b4b7` — port `restart` command
- `mg-cac6` — port `balance` command

plus the v0.1 scaffold (`mg-2fd8`).

```bash
cd <bridget>
git checkout main && git pull --ff-only
git log --oneline origin/main | head -20
```

If any of the 9 are missing, stop — the cutover assumes feature parity with
the personal bridge.

## 2. Run install.sh

```bash
cd <bridget>
./install.sh
```

What this does:

- Creates `~/.pogo/venv-bridget/` if missing; installs `requirements.txt`
  into it.
- Symlinks `~/.pogo/bin/bridget` → the `bridget` script in your checkout.
- Seeds `~/.pogo/bridget.env` from `bridget.env.example` **only if**
  `~/.pogo/bridget.env` does not already exist.

`install.sh` is idempotent — safe to re-run. An existing
`~/.pogo/bridget.env` is **never** overwritten.

## 3. Migrate env config

Copy values from the old `~/.pogo/discord-bridge.env` into the new
`~/.pogo/bridget.env`. Most keys map 1:1.

| Old key (`discord-bridge.env`) | New key (`bridget.env`) | Notes |
|---|---|---|
| `DISCORD_BOT_TOKEN` | `DISCORD_BOT_TOKEN` | Verbatim copy. |
| `DISCORD_USER_ID` | `DISCORD_USER_ID` | Verbatim. |
| `DISCORD_SERVER_ID` | `DISCORD_SERVER_ID` | Verbatim. |
| `POGO_INBOX_REPO` (if set) | `POGO_INBOX_REPO` | Verbatim. |
| `POGO_DESIGNS_DIR` (if set) | `POGO_DESIGNS_DIR` | Verbatim. |
| `POGO_MAIL_DIR` (if set) | `POGO_MAIL_DIR` | Verbatim. |
| `MG_BIN` (if set) | `MG_BIN` | Verbatim. |
| — | `POGO_BIN` | New. Default: PATH lookup. Set only if `pogo` isn't on PATH under launchd. |
| — | `POGO_MAIL_RECIPIENT` | New. Default: `mayor`. Set only if non-default. |
| — | `BRIDGET_REPO_DIR` | New. Default: self-detected from script location. Set only if your symlink layout is unusual. |

The three new keys can stay commented in the env file — defaults work.

A one-liner to do the initial copy of carryover values:

```bash
grep -E '^(DISCORD_|POGO_|MG_BIN)' ~/.pogo/discord-bridge.env >> ~/.pogo/bridget.env
```

This appends the matching lines blindly — if `~/.pogo/bridget.env` was
already populated (e.g. by `install.sh` seeding it from the template), open
the file afterwards and dedupe by hand. The later occurrence of a key wins
in bridget's parser, but a clean file is easier to audit.

## 4. Update launchd plist

Edit `~/Library/LaunchAgents/com.pogo.discord-bridge.plist`. Three strings
change: the `ProgramArguments` entry, `StandardOutPath`, and
`StandardErrorPath`. Keep the `Label` as `com.pogo.discord-bridge` so any
external references (scripts, dashboards, muscle memory) keep working.

The minimal diff:

```diff
-<string>/Users/yourname/.pogo/bin/pogo-discord-bridge</string>
+<string>/Users/yourname/.pogo/bin/bridget</string>
 ...
-<string>/Users/yourname/.pogo/discord-bridge.log</string>
+<string>/Users/yourname/.pogo/bridget.log</string>
 ...
-<string>/Users/yourname/.pogo/discord-bridge.err.log</string>
+<string>/Users/yourname/.pogo/bridget.err.log</string>
```

For confidence, compare your file to these full before/after renderings.

**Before:**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.pogo.discord-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/yourname/.pogo/bin/pogo-discord-bridge</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>/Users/yourname/.pogo/discord-bridge.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/yourname/.pogo/discord-bridge.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>/Users/yourname</string>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
```

**After:**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.pogo.discord-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/yourname/.pogo/bin/bridget</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>/Users/yourname/.pogo/bridget.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/yourname/.pogo/bridget.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>/Users/yourname</string>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
```

(For a fresh install, substitute `/Users/yourname` with your actual home
directory in every plist string. The `PATH` entry above is a sensible
default — if `pogo` or `mg` live somewhere else (e.g. `~/go/bin` or a
custom prefix), prepend that directory before `/usr/local/bin`. launchd
does not source your shell rc files, so the `PATH` here is the only one
the service will see.)

## 5. Reload launchd

```bash
launchctl unload ~/Library/LaunchAgents/com.pogo.discord-bridge.plist
launchctl load   ~/Library/LaunchAgents/com.pogo.discord-bridge.plist
```

Verify:

```bash
launchctl list | grep com.pogo.discord-bridge
```

Expected output: a line with a non-zero PID and exit code `0`. If you see
PID `-` or a non-zero exit code, check `~/.pogo/bridget.err.log` (see §9).

## 6. Smoke test in Discord

DM the bot and verify each of these:

- `?` — help text shows the full bridget command list (approve / reject /
  revise / explain / next / read / dismiss / status / agents / nudge /
  quiet / mail / restart / balance / idea: / bug: / help). The personal
  bridge had a `refuel` command; bridget does **not** — its absence here is
  expected.
- `agents` — crew agent list with cycle data, e.g. `last cycle Xs ago, next
  cycle in Ys`.
- `status` — global pull view: unread mail + in-flight work. There should
  be **no** refuel / USD line (refuel was removed in the v1 cutover).
- `mail status check` — bridget replies `✓ mailed mayor: "status check"`.
  Cross-check from the shell with `mg mail list mayor`; the new mail should
  be there.
- `balance` — assuming agents are healthy, replies
  `✅ no credit balance errors detected ...`.

If any command misbehaves or the bot stays silent, jump to §8 (Rollback).

## 7. Archival

**Don't archive yet.** Wait one week of stable bridget operation as a
safety margin against unexpected issues. After that:

```bash
mv <pogo-discord-bridge-checkout> <pogo-discord-bridge-checkout>.archive
```

The old log files (`~/.pogo/discord-bridge.log`,
`~/.pogo/discord-bridge.err.log`) and old env file
(`~/.pogo/discord-bridge.env`) stay in place — there's no auto-cleanup.
Remove or `gzip` them by hand whenever you feel like it.

## 8. Rollback

If bridget misbehaves and you need to fall back to the personal bridge:

1. Revert the plist edit from §4. The three changed strings flip back to
   `pogo-discord-bridge` (program path) and `discord-bridge.log` /
   `discord-bridge.err.log` (log paths).
2. Reload launchd:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.pogo.discord-bridge.plist
   launchctl load   ~/Library/LaunchAgents/com.pogo.discord-bridge.plist
   ```
3. Verify the old bridge is back:
   ```bash
   launchctl list | grep com.pogo.discord-bridge
   ```
   Same expectations as §5 — non-zero PID, exit code `0`.
4. File the bridget bug from Discord: `bug: <description>`. Include the
   relevant chunk of `~/.pogo/bridget.err.log` if there's a stack trace.

The personal `pogo-discord-bridge` install is untouched throughout the
cutover — there's nothing to restore beyond the plist itself.

## 9. Edge cases / FAQ

- **`install.sh` says "venv already exists at ~/.pogo/venv-bridget"** —
  harmless; `install.sh` is idempotent and re-uses the existing venv.
- **`launchctl load` reports "service already loaded"** — you skipped the
  unload, or a prior unload didn't finish. Run `launchctl unload <plist>`
  first, then `launchctl load <plist>`.
- **Discord doesn't respond after the reload** — check
  `~/.pogo/bridget.err.log` for stack traces. Most common cause: a missing
  or wrong env value in `~/.pogo/bridget.env` — re-check `DISCORD_BOT_TOKEN`,
  `DISCORD_USER_ID`, `DISCORD_SERVER_ID` against the old
  `~/.pogo/discord-bridge.env`.
- **`mg` not found errors in the bridget log** — launchd doesn't source
  your shell rc files, so its `PATH` may not reach wherever `mg` is
  installed. Either add the directory to the `PATH` entry inside the plist
  (see §4), or set `MG_BIN=/absolute/path/to/mg` in `~/.pogo/bridget.env`.
  Same applies to `pogo` / `POGO_BIN`.
- **Old logs grow forever** — bridget writes to `bridget.log` /
  `bridget.err.log`, so the old `discord-bridge.*` files stop growing the
  moment the launchd reload completes. Manual rotate at your leisure
  (e.g. `gzip ~/.pogo/discord-bridge.log`).
- **`restart` Discord command does nothing useful right after cutover** —
  expected if you ran the cutover by hand on the host. From this point on,
  `restart` keeps you in sync with `origin/main` — see the "Remote restart"
  section of [`README.md`](README.md) for the bootstrap caveat.
