"""GitHub Copilot chat completions client.

Why these headers?
------------------
The Copilot API (`api.githubcopilot.com`) validates Editor-Version and
Editor-Plugin-Version headers.  Using the VS Code extension identifiers is the
standard approach for third-party clients and matches the client ID used in
auth.py.

Session token lifecycle
-----------------------
`auth.get_copilot_token()` transparently refreshes the ~30-minute session token
when it is within 5 minutes of expiry, so callers here never need to worry about
token rotation.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

import auth

logger = logging.getLogger("ha_mcp_bridge.copilot")

COPILOT_API_URL = "https://api.githubcopilot.com/chat/completions"
MODEL = "gpt-4o"

# System prompt injected at the start of every conversation.
# Keeps Copilot focused on Home Assistant assistance.
HA_SYSTEM_PROMPT = (
    "You are GitHub Copilot, integrated into a Home Assistant smart home system "
    "via the HA MCP Bridge add-on. Help the user understand and control their "
    "Home Assistant instance — automations, entities, integrations, scripts, "
    "and dashboards. Be concise, practical, and friendly. When the user wants to "
    "control a device or create an automation, describe the exact YAML or service "
    "call they need."
)


def chat(messages: list[dict]) -> str:
    """Send a chat completions request and return the assistant reply text.

    Args:
        messages: List of {"role": "user"|"assistant", "content": str} dicts
                  representing the conversation history (newest last).

    Raises:
        RuntimeError: If not authenticated, or if the API returns an error.
    """
    token = auth.get_copilot_token()
    if not token:
        raise RuntimeError(
            "Not authenticated with GitHub Copilot. "
            "Open the HA MCP Bridge panel and sign in first."
        )

    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": HA_SYSTEM_PROMPT}, *messages],
        "stream": False,
        "n": 1,
    }).encode("utf-8")

    req = urllib.request.Request(COPILOT_API_URL, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    # Required by the Copilot API — identifies the integration.
    req.add_header("Copilot-Integration-Id", "vscode-chat")
    req.add_header("Editor-Version", "vscode/1.85.0")
    req.add_header("Editor-Plugin-Version", "copilot-chat/0.12.2")
    req.add_header("User-Agent", "GitHubCopilotChat/0.12.2")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        logger.error("Copilot API HTTP %d: %s", exc.code, body[:200])
        raise RuntimeError(f"Copilot API returned HTTP {exc.code}: {body[:200]}") from exc

    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError("Copilot API returned an empty choices list")

    return choices[0]["message"]["content"]
