#!/usr/bin/env bash
# run.sh — ha-mcp-bridge startup
# All installs happen here (at container start), never in the Dockerfile.

# npm global bin must be in PATH so both shell and python subprocesses agree.
NPM_BIN="$(npm bin -g 2>/dev/null || echo /usr/local/bin)"
export PATH="${NPM_BIN}:/usr/local/bin:${PATH}"

# Install GitHub Copilot CLI if not already present.
if ! command -v copilot >/dev/null 2>&1; then
    echo "[ha-mcp-bridge] Installing @github/copilot via npm..."
    npm install -g @github/copilot
    echo "[ha-mcp-bridge] Install exit code: $?"
fi

# Log what we have.
COPILOT_PATH="$(command -v copilot 2>/dev/null || echo '')"
if [ -n "${COPILOT_PATH}" ]; then
    echo "[ha-mcp-bridge] copilot ready at: ${COPILOT_PATH}"
else
    echo "[ha-mcp-bridge] WARNING: copilot not found — terminal will show an error"
fi

exec python3 main.py