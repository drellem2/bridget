#!/usr/bin/env bash
# Idempotent installer for bridget.
#
# - Creates ~/.pogo/venv-bridget/ if missing and installs requirements.txt.
# - Symlinks ~/.pogo/bin/bridget → this repo's bridget script.
# - Seeds ~/.pogo/bridget.env from bridget.env.example if no env file exists.
# - Verifies `mg` is on PATH and prints next-step instructions.
#
# With --setup, additionally prompts for the Discord credentials and writes them
# into the env file. The bot token is read with the terminal echo off and is
# never printed, logged, or passed on a command line — only a masked
# confirmation (last 4 characters) is shown back.
set -euo pipefail

SETUP=0
for arg in "$@"; do
    case "$arg" in
        --setup) SETUP=1 ;;
        -h|--help)
            printf 'usage: install.sh [--setup]\n\n'
            printf '  --setup   prompt for Discord credentials (token entry is masked)\n'
            exit 0 ;;
        *) printf 'install.sh: unknown argument %s\n' "$arg" >&2; exit 2 ;;
    esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$HOME/.pogo/venv-bridget"
BIN_DIR="$HOME/.pogo/bin"
BIN_LINK="$BIN_DIR/bridget"
ENV_FILE="$HOME/.pogo/bridget.env"
ENV_EXAMPLE="$REPO_DIR/bridget.env.example"
SCRIPT="$REPO_DIR/bridget"

log()  { printf '[install] %s\n'  "$*"; }
warn() { printf '[install] WARN: %s\n' "$*" >&2; }
die()  { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

[[ -f "$SCRIPT"      ]] || die "missing bridget script at $SCRIPT"
[[ -f "$ENV_EXAMPLE" ]] || die "missing template at $ENV_EXAMPLE"

if ! command -v python3 >/dev/null 2>&1; then
    die "python3 not found on PATH"
fi

# 1. venv
if [[ ! -d "$VENV_DIR" ]]; then
    log "creating venv at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
else
    log "venv already exists at $VENV_DIR"
fi

log "installing requirements"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"

# 2. symlink ~/.pogo/bin/bridget → repo script
mkdir -p "$BIN_DIR"
if [[ -L "$BIN_LINK" ]]; then
    current_target="$(readlink "$BIN_LINK")"
    if [[ "$current_target" == "$SCRIPT" ]]; then
        log "symlink $BIN_LINK already points to $SCRIPT"
    else
        log "replacing symlink $BIN_LINK ($current_target → $SCRIPT)"
        rm "$BIN_LINK"
        ln -s "$SCRIPT" "$BIN_LINK"
    fi
elif [[ -e "$BIN_LINK" ]]; then
    warn "$BIN_LINK exists and is not a symlink — leaving it alone."
    warn "remove it manually and re-run install.sh if you want bridget there."
else
    log "creating symlink $BIN_LINK → $SCRIPT"
    ln -s "$SCRIPT" "$BIN_LINK"
fi

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
    warn "install pogo (https://github.com/CloverRoss/pogo) and ensure mg is reachable,"
    warn "or set MG_BIN in $ENV_FILE."
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
  5. To run bridget under a process supervisor (launchd / systemd / nohup),
     see the "Running as a service" section in README.md.

EOF
