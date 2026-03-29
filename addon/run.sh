#!/usr/bin/env bash
# run.sh — ha-mcp-bridge startup script
# Runs AFTER the image is built, in a live network environment.
# Installs the GitHub Copilot CLI here (not in the Dockerfile) so that
# a network failure never breaks the image build.

# Add npm global bin to PATH so 'command -v copilot' and python subprocesses
# can both find it consistently.
NPM_BIN="$(npm bin -g 2>/dev/null || echo /usr/local/bin)"
export PATH="${NPM_BIN}:/usr/local/bin:${PATH}"

# Install copilot CLI if not already present.
# Ref: https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli
if ! command -v copilot > /dev/null 2>&1; then
    echo "[ha-mcp-bridge] Installing GitHub Copilot CLI via npm..."
    if npm install -g @github/copilot; then
        COPILOT_PATH="$(command -v copilot 2>/dev/null || echo 'not found')"
        echo "[ha-mcp-bridge] copilot installed at: ${COPILOT_PATH}"
    else
        echo "[ha-mcp-bridge] WARNING: copilot install failed — PTY terminal will show an error"
    fi
else
    COPILOT_PATH="$(command -v copilot)"
    echo "[ha-mcp-bridge] copilot already present at: ${COPILOT_PATH}"
fi

exec python3 main.py