#!/usr/bin/env bash
# Copyright (C) 2026 Clover Ross
# Copyright (C) 2026 Daniel Miller
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Modified in 2026 by Daniel Miller, whose fork this is. What changed and
# when is recorded in AUTHORS and CHANGELOG.md (GPL-3.0 section 5(a)).
#
# bridget is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. bridget is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
# You should have received a copy of the GNU General Public License along with
# bridget. If not, see <https://www.gnu.org/licenses/>.

# Idempotent installer for bridget.
#
# - Creates ~/.pogo/venv-bridget/ if missing and installs requirements.txt.
# - Symlinks ~/.pogo/bin/bridget → this repo's bridget script.
# - Seeds ~/.pogo/bridget.env from bridget.env.example if no env file exists.
# - Verifies `mg` is on PATH and prints next-step instructions.
#
# With --setup, additionally prompts for the Discord credentials and writes them
# into the env file. The bot token is read with the terminal echo off and is
# never printed, logged, or passed on a command line. *No* part of it is echoed
# back — not even a last-4 confirmation, because a partial token is still a
# leaked token and this output may land in a terminal transcript. A paste error
# is caught by validating the token's shape instead (see token_shape_ok).
set -euo pipefail

SETUP=0
NO_VENV=0
LAUNCHD=0
for arg in "$@"; do
    case "$arg" in
        --setup) SETUP=1 ;;
        --no-venv) NO_VENV=1 ;;
        --launchd) LAUNCHD=1 ;;
        -h|--help)
            printf 'usage: install.sh [--setup] [--no-venv] [--launchd]\n\n'
            printf '  --setup     prompt for Discord credentials (token entry is masked)\n'
            printf '  --no-venv   skip venv creation and dependency install. Everything\n'
            printf '              else still runs. This is the only step that needs the\n'
            printf '              network, so it is what the test suite skips; also useful\n'
            printf '              if you manage the venv yourself.\n'
            printf '  --launchd   (macOS) install, bootstrap and kickstart the\n'
            printf '              com.pogo.bridget LaunchAgent so bridget runs at login\n'
            printf '              and restarts when it crashes.\n'
            exit 0 ;;
        *) printf 'install.sh: unknown argument %s\n' "$arg" >&2; exit 2 ;;
    esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$HOME/.pogo/venv-bridget"
BIN_DIR="$HOME/.pogo/bin"
BIN_LINK="$BIN_DIR/bridget"
SUPERVISE_LINK="$BIN_DIR/bridget-supervise"
ENV_FILE="$HOME/.pogo/bridget.env"
ENV_EXAMPLE="$REPO_DIR/bridget.env.example"
SCRIPT="$REPO_DIR/bridget"
SUPERVISE_SCRIPT="$REPO_DIR/bridget-supervise"
PLIST_LABEL="com.pogo.bridget"
PLIST_EXAMPLE="$REPO_DIR/$PLIST_LABEL.plist.example"
PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"

log()  { printf '[install] %s\n'  "$*"; }
warn() { printf '[install] WARN: %s\n' "$*" >&2; }
die()  { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

[[ -f "$SCRIPT"           ]] || die "missing bridget script at $SCRIPT"
[[ -f "$SUPERVISE_SCRIPT" ]] || die "missing supervisor at $SUPERVISE_SCRIPT"
[[ -f "$ENV_EXAMPLE"      ]] || die "missing template at $ENV_EXAMPLE"
if (( LAUNCHD )); then
    [[ "$(uname -s)" == "Darwin" ]] || die "--launchd is macOS-only (this is $(uname -s))"
    [[ -f "$PLIST_EXAMPLE" ]] || die "missing plist template at $PLIST_EXAMPLE"
fi

if ! command -v python3 >/dev/null 2>&1; then
    die "python3 not found on PATH"
fi

# 1. venv
if (( NO_VENV )); then
    log "skipping venv and dependency install (--no-venv)"
else
    if [[ ! -d "$VENV_DIR" ]]; then
        log "creating venv at $VENV_DIR"
        python3 -m venv "$VENV_DIR"
    else
        log "venv already exists at $VENV_DIR"
    fi

    log "installing requirements"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"
fi

# 2. symlink ~/.pogo/bin/{bridget,bridget-supervise} → repo scripts
#
# A pre-existing *regular file* at either path is left alone: it may be a copy
# an operator deliberately placed there (bridget-supervise is stand-alone enough
# to be copied), and clobbering it would silently discard their edit.
link_bin() {
    # No `basename`: install.sh must run under the minimal PATH launchd and the
    # installer test give it, and coreutils is not guaranteed to be on it.
    local target="$1" link="$2" name="${2##*/}"
    if [[ -L "$link" ]]; then
        local current_target
        current_target="$(readlink "$link")"
        if [[ "$current_target" == "$target" ]]; then
            log "symlink $link already points to $target"
        else
            log "replacing symlink $link ($current_target → $target)"
            rm "$link"
            ln -s "$target" "$link"
        fi
    elif [[ -e "$link" ]]; then
        warn "$link exists and is not a symlink — leaving it alone."
        warn "remove it manually and re-run install.sh if you want $name there."
    else
        log "creating symlink $link → $target"
        ln -s "$target" "$link"
    fi
}

mkdir -p "$BIN_DIR"
link_bin "$SCRIPT" "$BIN_LINK"
link_bin "$SUPERVISE_SCRIPT" "$SUPERVISE_LINK"

# 3. seed env file (never overwrite a populated one)
if [[ ! -e "$ENV_FILE" ]]; then
    log "seeding $ENV_FILE from bridget.env.example"
    mkdir -p "$(dirname "$ENV_FILE")"
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
else
    log "$ENV_FILE already exists — leaving its contents alone"
fi

file_mode() {
    stat -f '%OLp' "$1" 2>/dev/null || stat -c '%a' "$1" 2>/dev/null || echo ''
}

# The env file holds the bot token. Enforce owner-only on every run, not just
# on the run that created it: a hand-rolled env file is the common case, and a
# 644 token is a real leak on a shared host.
if [[ -e "$ENV_FILE" ]]; then
    mode="$(file_mode "$ENV_FILE")"
    if [[ -n "$mode" && "$mode" != "600" ]]; then
        log "tightening $ENV_FILE permissions ($mode → 600); it holds your bot token"
        chmod 600 "$ENV_FILE"
    fi
fi

# The directory holding it, likewise. mkdir under the default umask of 022
# leaves ~/.pogo at 0755, so the env file's own 600 is the only thing between a
# co-tenant and your bot token — and bridget's state files next to it name every
# agent you talk to and every conversation you muted.
POGO_DIR="$(dirname "$ENV_FILE")"
if [[ -d "$POGO_DIR" ]]; then
    mode="$(file_mode "$POGO_DIR")"
    if [[ -n "$mode" && "$mode" != "700" ]]; then
        log "tightening $POGO_DIR permissions ($mode → 700)"
        chmod 700 "$POGO_DIR"
    fi
fi

# 3b. optional interactive setup — masked token entry
#
# Values are written with a temp file + mv so an interrupted run cannot leave a
# half-written env file, and the temp file is created 600 before it holds
# anything. The token never appears in argv, in the terminal, or in shell history.
set_env_key() {
    local key="$1" value="$2" tmp
    tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
    chmod 600 "$tmp"
    if grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
        # Rewrite in place via awk so the value never becomes a sed script.
        VALUE="$value" awk -v k="$key" \
            'BEGIN{FS=OFS="="} $1==k && !done {print k "=" ENVIRON["VALUE"]; done=1; next} {print}' \
            "$ENV_FILE" > "$tmp"
    else
        cat "$ENV_FILE" > "$tmp" 2>/dev/null || true
        printf '%s=%s\n' "$key" "$value" >> "$tmp"
    fi
    mv "$tmp" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
}

token_shape_ok() {
    # Catch a paste error without disclosing any part of the secret. A Discord
    # bot token is three dot-separated base64url chunks. We never echo the
    # value, a prefix, or a suffix — a partial token is still a leaked token,
    # and this output may be captured in a log or a terminal transcript.
    [[ "$1" =~ ^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$ ]]
}

if (( SETUP )); then
    log "interactive setup — press Enter to keep an existing value"
    printf '\n'

    printf 'Discord bot token (input hidden): '
    IFS= read -rs token || true
    printf '\n'
    if [[ -n "$token" ]]; then
        if token_shape_ok "$token"; then
            set_env_key DISCORD_BOT_TOKEN "$token"
            log "DISCORD_BOT_TOKEN set (value hidden)"
        else
            warn "that does not look like a Discord bot token (expected three"
            warn "dot-separated parts). Leaving DISCORD_BOT_TOKEN unchanged —"
            warn "re-run 'bash install.sh --setup' to try again."
        fi
        unset token
    else
        log "DISCORD_BOT_TOKEN unchanged"
    fi

    for key in DISCORD_USER_ID DISCORD_SERVER_ID; do
        printf '%s (snowflake, digits only): ' "$key"
        IFS= read -r value || true
        if [[ -z "$value" ]]; then
            log "$key unchanged"
        elif [[ "$value" =~ ^[0-9]+$ ]]; then
            set_env_key "$key" "$value"
            log "$key set to $value"
        else
            warn "$key must be all digits — got '$value'. Leaving it unchanged."
        fi
    done
    printf '\n'
fi

# 4. verify mg
if command -v mg >/dev/null 2>&1; then
    log "found mg: $(command -v mg)"
else
    warn "mg not found on PATH."
    warn "mg ships with pogo (the maintainer's own repository — bridget does not"
    warn "vendor it). Install pogo and ensure mg is reachable, or set MG_BIN in"
    warn "$ENV_FILE."
fi

# 5. launchd agent (opt-in, macOS only)
#
# The kickstart at the end is not belt-and-braces, it is the only thing that
# reliably starts the job. `bootstrap` merely loads the plist; the RunAtLoad
# spawn that should follow is a "nondemand" spawn, and launchd defers those
# under system load — `launchctl print` then shows `runs = 0` next to
# `pended nondemand spawn = speculative`, indefinitely. `kickstart` is a demand
# spawn and is never pended. Same reason bridget-supervise exists; see its
# header and the README's "Running as a service".
if (( LAUNCHD )); then
    launchd_state() {
        launchctl print "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null \
            | awk '/^\tstate =/{s=$3} /^\truns =/{r=$3} END{printf "%s runs=%s", (s==""?"unknown":s), (r==""?"?":r)}'
    }

    log "rendering $PLIST_EXAMPLE → $PLIST_DEST"
    mkdir -p "$HOME/Library/LaunchAgents"
    # launchd does not expand $HOME inside plist strings, so bake it in.
    sed "s|__HOME__|$HOME|g" "$PLIST_EXAMPLE" > "$PLIST_DEST"
    plutil -lint "$PLIST_DEST" >/dev/null || die "rendered plist is malformed: $PLIST_DEST"

    # Reloading a live job is how you pick up a changed plist; bootout fails
    # when nothing is loaded, which is fine and not an error.
    launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST" \
        || die "launchctl bootstrap failed for $PLIST_DEST"
    launchctl kickstart "gui/$(id -u)/$PLIST_LABEL" \
        || die "launchctl kickstart failed for $PLIST_LABEL"

    sleep 1
    state="$(launchd_state)"
    if [[ "$state" == running* ]]; then
        log "$PLIST_LABEL is $state"
    else
        warn "$PLIST_LABEL did not reach state=running (got: $state)."
        warn "inspect with: launchctl print gui/$(id -u)/$PLIST_LABEL"
        warn "logs: $HOME/.pogo/bridget.log and $HOME/.pogo/bridget.err.log"
    fi
fi

cat <<EOF

[install] Done.

Next steps:
  1. Fill in DISCORD_BOT_TOKEN, DISCORD_USER_ID, DISCORD_SERVER_ID — either edit
     $ENV_FILE directly, or re-run with masked prompts:
         bash install.sh --setup
  2. (Optional) Turn on thread-per-conversation and the calm DM inbox:
       - BRIDGET_LOG_CHANNEL_ID — guild text channel to root threads in
       - BRIDGET_DM_POLICY      — all (default) | curated | none
     See the "Conversation threads" section in README.md.
  3. (Optional) Override default paths in $ENV_FILE if your design docs or
     inbox repo live elsewhere:
       - POGO_DESIGNS_DIR — default: ~/.pogo/designs
       - POGO_INBOX_REPO  — default: ~/.pogo/inbox
     See bridget.env.example for the full list of optional keys.
  4. Run bridget:
         $BIN_LINK
     The script reads its config from $ENV_FILE on startup.
  5. To keep it running (macOS), install the LaunchAgent:
         bash install.sh --launchd
     That renders $PLIST_LABEL, bootstraps it, and kickstarts it. bridget then
     runs under $SUPERVISE_LINK, which restarts it if it
     crashes. Check on it with:
         launchctl print gui/\$(id -u)/$PLIST_LABEL | grep -E 'state|runs'
     For systemd / nohup, see "Running as a service" in README.md.

EOF
