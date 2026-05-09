# Bridget v2 Roadmap

Current planned work for bridget v2. Completed items are removed in the same PR that closes them, so this file always reflects what's still ahead.

## P2 — Hardening / polish (file when v1 parity is in)

### 7. Document `POGO_INBOX_REPO` / `POGO_DESIGNS_DIR` in install flow
- README/install.sh should call out these env vars explicitly so users don't hit the silent-404 trap before P1 ships. Currently the `bridget.env.example` mentions them but install.sh doesn't actively prompt or warn.
- **Filing:** `idea: install.sh should warn or prompt when POGO_INBOX_REPO/POGO_DESIGNS_DIR are unset (until v2 sensible-defaults ships)`
