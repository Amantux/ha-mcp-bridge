"""GitHub device-flow OAuth token management.

Flow
----
1. Client calls start_device_flow()  returns {user_code, verification_uri, device_code, interval}
2. User visits verification_uri and enters user_code in a browser.
3. Client polls poll_device_token(device_code) every `interval` seconds until
   it returns a token dict; call save_token() to persist it.
4. The stored GitHub OAuth token is used directly as a Bearer credential for
   the GitHub Models API - no secondary token exchange needed.

Why this client ID?
-------------------
`Iv1.b507a08c87ecfe98` is the public client ID of the GitHub Copilot VS Code
extension, used by many open-source clients for device-flow auth.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger("ha_mcp_bridge.auth")

GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"
GITHUB_DEVICE_URL = "https://github.com/login/device/code"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"

# Persisted under /data (mapped by Supervisor; survives restarts).
TOKEN_PATH = Path("/data/github_token.json")


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------

def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Device flow
# ---------------------------------------------------------------------------

def start_device_flow() -> dict:
    """Initiate GitHub device flow.

    Returns a dict with keys: device_code, user_code, verification_uri,
    expires_in, interval.
    """
    return _post_form(GITHUB_DEVICE_URL, {
        "client_id": GITHUB_CLIENT_ID,
        "scope": "read:user",
    })


def poll_device_token(device_code: str) -> dict | None:
    """Poll for the OAuth access token.

    Returns the token dict (contains 'access_token') when the user has approved
    the request, or None if still pending.
    Raises RuntimeError on terminal errors (expired, denied, etc.).
    """
    result = _post_form(GITHUB_TOKEN_URL, {
        "client_id": GITHUB_CLIENT_ID,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    })
    if "access_token" in result:
        return result
    error = result.get("error", "")
    if error in ("expired_token", "access_denied"):
        raise RuntimeError(f"Device flow ended: {error}")
    # "authorization_pending" or "slow_down" -> caller should retry
    return None


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

def save_token(token_data: dict) -> None:
    TOKEN_PATH.write_text(json.dumps(token_data))
    logger.info("GitHub OAuth token saved to %s", TOKEN_PATH)


def load_token() -> dict | None:
    if not TOKEN_PATH.exists():
        return None
    try:
        return json.loads(TOKEN_PATH.read_text())
    except Exception as exc:
        logger.warning("Could not read token file: %s", exc)
        return None


def get_github_token() -> str | None:
    data = load_token()
    return data.get("access_token") if data else None


def is_authenticated() -> bool:
    return get_github_token() is not None


def revoke() -> None:
    """Delete the persisted token."""
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()
    logger.info("GitHub token revoked")


# ---------------------------------------------------------------------------
# User info
# ---------------------------------------------------------------------------

def get_username() -> str | None:
    """Return the GitHub username for the stored token, or None."""
    token = get_github_token()
    if not token:
        return None
    try:
        return _get_json(GITHUB_USER_URL, token).get("login")
    except Exception:
        return None
