"""GitHub Models API client (replaces the unofficial Copilot internal endpoint).

Why GitHub Models instead of the Copilot internal API?
-------------------------------------------------------
The `/copilot_internal/v2/token` exchange endpoint returns HTTP 403 unless the
account has an active Copilot Individual/Business subscription AND the OAuth
client ID is on GitHub's allowlist.  Both conditions are brittle for a home
automation add-on.

GitHub Models (`models.inference.ai.azure.com`) is the official, rate-limited
API for AI models via GitHub.  It accepts the GitHub OAuth token **directly** as
a Bearer credential — no token-exchange step, no subscription required beyond
GitHub Models access (free tier available).  The request/response format is
identical to the OpenAI chat completions API.

Reference: https://docs.github.com/en/github-models/use-github-models/integrating-ai-models-into-your-development-workflow
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

import auth

logger = logging.getLogger("ha_mcp_bridge.copilot")

GITHUB_MODELS_URL = "https://models.inference.ai.azure.com/chat/completions"
MODEL = "openai/gpt-4o"

HA_SYSTEM_PROMPT = (
    "You are GitHub Copilot, integrated into a Home Assistant smart home system "
    "via the HA MCP Bridge add-on. Help the user understand and control their "
    "Home Assistant instance — automations, entities, integrations, scripts, "
    "and dashboards. Be concise, practical, and friendly. When the user wants to "
    "control a device or create an automation, describe the exact YAML or service "
    "call they need."
)


def chat(messages: list[dict]) -> str:
    """Call GitHub Models chat completions and return the assistant reply.

    Uses the GitHub OAuth token directly as a Bearer credential.
    No token exchange needed — simpler and more reliable than the
    Copilot internal endpoint.

    Args:
        messages: [{role: "user"|"assistant", content: str}, …] newest last.

    Raises:
        RuntimeError: Not authenticated, or API error.
    """
    token = auth.get_github_token()
    if not token:
        raise RuntimeError(
            "Not authenticated with GitHub. "
            "Open the HA MCP Bridge panel and sign in first."
        )

    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": HA_SYSTEM_PROMPT}, *messages],
        "stream": False,
        "n": 1,
    }).encode("utf-8")

    req = urllib.request.Request(GITHUB_MODELS_URL, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "ha-mcp-bridge/0.1.4")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        logger.error("GitHub Models API HTTP %d: %s", exc.code, body[:300])
        if exc.code == 401:
            raise RuntimeError("GitHub token rejected. Try signing out and back in.") from exc
        if exc.code == 403:
            raise RuntimeError(
                "Access denied. Make sure your GitHub account has GitHub Models access "
                "(github.com/marketplace/models)."
            ) from exc
        raise RuntimeError(f"GitHub Models API returned HTTP {exc.code}: {body[:200]}") from exc

    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError("GitHub Models API returned an empty choices list")

    return choices[0]["message"]["content"]

