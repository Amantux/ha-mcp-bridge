#!/usr/bin/env bash
# run.sh — ha-mcp-bridge startup script
# Runs AFTER the image is built, in a live network environment.
# Installs the GitHub Copilot CLI here (not in the Dockerfile) so that
# a network failure never breaks the image build.

# Install copilot CLI if not already present.
# Ref: https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli
if ! command -v copilot > /dev/null 2>&1; then
    echo "[ha-mcp-bridge] Installing GitHub Copilot CLI..."
    npm install -g @github/copilot \
        && echo "[ha-mcp-bridge] copilot installed: $(copilot --version 2>&1 | head -1)" \
        || echo "[ha-mcp-bridge] WARNING: copilot install failed — PTY terminal will show an error"
else
    echo "[ha-mcp-bridge] copilot already installed: $(copilot --version 2>&1 | head -1)"
fi

exec python3 main.py