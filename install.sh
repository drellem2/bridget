#!/usr/bin/env bash
# Idempotent installer for bridget.
#
# - Creates ~/.pogo/venv-bridget/ if missing and installs requirements.txt.
# - Symlinks ~/.pogo/bin/bridget → this repo's bridget script.
# - Seeds ~/.pogo/bridget.env from bridget.env.example if no env file exists.
# - Verifies `mg` is on PATH and prints next-step instructions.
set -euo pipefail

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
    log "$ENV_FILE already exists — leaving it alone"
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
  1. Edit $ENV_FILE — fill in DISCORD_BOT_TOKEN, DISCORD_USER_ID, DISCORD_SERVER_ID.
  2. (Optional) Override default paths in $ENV_FILE if your design docs or
     inbox repo live elsewhere:
       - POGO_DESIGNS_DIR — default: ~/.pogo/designs
       - POGO_INBOX_REPO  — default: ~/.pogo/inbox
     See bridget.env.example for the full list of optional keys.
  3. Run bridget:
         $BIN_LINK
     The script reads its config from $ENV_FILE on startup.
  4. To run bridget under a process supervisor (launchd / systemd / nohup),
     see the "Running as a service" section in README.md.

EOF
