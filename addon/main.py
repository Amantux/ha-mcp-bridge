"""HA MCP Bridge ΓÇö PTY bridge for the GitHub Copilot CLI.

Architecture
------------
aiohttp on port 8099:
  GET  /              xterm.js terminal UI
  GET  /ws            WebSocket <-> PTY running `copilot`
  GET  /health        JSON health
  GET  /auth/status   {authenticated, username, copilot_available}
  POST /auth/start    start `gh auth login`; return {code, url, lines}
  POST /auth/poll     poll auth progress; return {lines, done, authenticated}
  POST /chat          non-interactive Copilot call for HA conversation agent

PTY bridge (no fork)
--------------------
Uses subprocess.Popen(stdin=slave, stdout=slave, stderr=slave) instead of
os.fork() + os.execvpe() ΓÇö avoids DeprecationWarning in multi-threaded
asyncio ("This process is multi-threaded, use of fork() may lead to
deadlocks") and is safe with aiohttp's thread-pool executor.

Resize protocol (binary WebSocket frames):
  first byte 0x01 + big-endian uint16 rows + uint16 cols

Auth flow
---------
1. UI loads: polls /auth/status.
   - If not authed with gh: renders an inline device-code card.
   - User clicks "Sign in with GitHub" ΓåÆ POST /auth/start ΓåÆ shows code + URL.
   - UI polls /auth/poll every 2 s; when done it auto-connects the terminal.
2. After gh auth the stored token is forwarded to `copilot` as GH_TOKEN so
   the copilot CLI can skip its own /login on first launch.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import re
import struct
import subprocess
import termios
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import aiohttp
import aiohttp.web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ha_mcp_bridge")

OPTIONS_PATH     = Path("/data/options.json")
STATIC_DIR       = Path(__file__).parent / "static"
SUPERVISOR_API   = "http://supervisor"
HA_API           = "http://supervisor/core/api"
GH_CONFIG_DIR    = "/data/gh"
start_time       = time.time()

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
ANSI             = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

_COPILOT_PATH: str | None = None  # cached path to copilot binary

# Conversation history keyed by conversation_id (multi-turn chat support).
_conversation_histories: dict[str, list[dict]] = {}
_MAX_HISTORY_TURNS = 10  # keep last N exchanges (user+assistant pairs)

# ---------------------------------------------------------------------------
# Home Assistant MCP tools — exposed to Copilot as OpenAI function tools
# ---------------------------------------------------------------------------

HA_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "ha_get_states",
            "description": (
                "Get the current state of Home Assistant entities. "
                "Optionally filter by domain (e.g. 'light', 'switch', 'sensor', 'climate'). "
                "Returns entity_id, state, and key attributes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Optional domain filter e.g. 'light', 'switch', 'sensor', 'climate', 'media_player'. Omit to get all."
                    },
                    "entity_id": {
                        "type": "string",
                        "description": "Optional specific entity_id to get (e.g. 'light.living_room'). If set, domain is ignored."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ha_call_service",
            "description": (
                "Call a Home Assistant service to control devices. "
                "Examples: turn lights on/off, set thermostat temperature, lock doors, play media. "
                "Common: domain='light' service='turn_on' entity_id='light.living_room' data={'brightness':200}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Service domain e.g. 'light', 'switch', 'climate', 'media_player', 'lock', 'script', 'automation'"
                    },
                    "service": {
                        "type": "string",
                        "description": "Service name e.g. 'turn_on', 'turn_off', 'toggle', 'set_temperature', 'lock', 'unlock'"
                    },
                    "entity_id": {
                        "type": "string",
                        "description": "Target entity_id (e.g. 'light.living_room') or comma-separated list"
                    },
                    "service_data": {
                        "type": "object",
                        "description": "Additional service data e.g. {\"brightness\": 200, \"temperature\": 72, \"color_temp\": 300}"
                    }
                },
                "required": ["domain", "service"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ha_get_history",
            "description": "Get the recent state history for a specific entity over the last N hours.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The entity_id to get history for (e.g. 'sensor.temperature')"
                    },
                    "hours": {
                        "type": "number",
                        "description": "How many hours of history to retrieve (default 3, max 24)"
                    }
                },
                "required": ["entity_id"]
            }
        }
    },
]


def _ha_headers() -> dict:
    return {"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"}


def _call_ha_tool(name: str, arguments: dict) -> str:
    """Execute an HA tool call and return the result as a string."""
    try:
        if name == "ha_get_states":
            entity_id = arguments.get("entity_id", "").strip()
            domain    = arguments.get("domain", "").strip()
            if entity_id:
                url = f"{HA_API}/states/{entity_id}"
                req = urllib.request.Request(url, headers=_ha_headers())
                with urllib.request.urlopen(req, timeout=10) as r:
                    s = json.loads(r.read())
                return json.dumps({"entity_id": s["entity_id"], "state": s["state"],
                                   "attributes": s.get("attributes", {})}, indent=2)
            else:
                url = f"{HA_API}/states"
                req = urllib.request.Request(url, headers=_ha_headers())
                with urllib.request.urlopen(req, timeout=15) as r:
                    states = json.loads(r.read())
                if domain:
                    states = [s for s in states if s["entity_id"].startswith(domain + ".")]
                # Return condensed view
                result = [{"entity_id": s["entity_id"], "state": s["state"],
                           "friendly_name": s.get("attributes", {}).get("friendly_name", "")}
                          for s in states[:80]]
                return json.dumps(result, indent=2)

        elif name == "ha_call_service":
            domain   = arguments["domain"]
            service  = arguments["service"]
            svc_data = dict(arguments.get("service_data") or {})
            entity_id = arguments.get("entity_id", "")
            if entity_id:
                svc_data["entity_id"] = entity_id
            url     = f"{HA_API}/services/{domain}/{service}"
            payload = json.dumps(svc_data).encode()
            req = urllib.request.Request(url, data=payload, method="POST",
                                         headers=_ha_headers())
            with urllib.request.urlopen(req, timeout=15) as r:
                changed = json.loads(r.read())
            changed_ids = [s.get("entity_id") for s in (changed or [])]
            return json.dumps({"success": True, "changed": changed_ids})

        elif name == "ha_get_history":
            entity_id = arguments["entity_id"]
            hours     = min(float(arguments.get("hours", 3)), 24)
            from datetime import datetime, timedelta, timezone
            since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            url   = f"{HA_API}/history/period/{since}?filter_entity_id={entity_id}&minimal_response=true"
            req   = urllib.request.Request(url, headers=_ha_headers())
            with urllib.request.urlopen(req, timeout=15) as r:
                history = json.loads(r.read())
            # Flatten and trim
            flat = []
            for timeline in (history or []):
                for entry in timeline[-20:]:
                    flat.append({"t": entry.get("last_changed", ""), "state": entry.get("state", "")})
            return json.dumps(flat, indent=2)

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        logger.error("HA tool %s HTTP %s: %s", name, exc.code, body[:200])
        return json.dumps({"error": f"HA API error {exc.code}: {body[:200]}"})
    except Exception as exc:
        logger.error("HA tool %s failed: %s", name, exc)
        return json.dumps({"error": str(exc)})

# ---------------------------------------------------------------------------
# Options / Supervisor discovery
# ---------------------------------------------------------------------------

def load_options() -> dict:
    if not OPTIONS_PATH.exists():
        return {}
    try:
        return json.loads(OPTIONS_PATH.read_text())
    except json.JSONDecodeError:
        return {}


OPTIONS = load_options()
HOST = str(OPTIONS.get("host", "0.0.0.0"))
PORT = int(OPTIONS.get("port", 8099))


def register_discovery() -> None:
    if not SUPERVISOR_TOKEN:
        logger.warning("SUPERVISOR_TOKEN not set ΓÇö skipping discovery.")
        return
    payload = json.dumps({
        "service": "ha_mcp_bridge",
        "config": {"host": "127.0.0.1", "port": PORT},
    }).encode()
    req = urllib.request.Request(
        f"{SUPERVISOR_API}/discovery", data=payload, method="POST",
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            uuid = json.loads(resp.read()).get("data", {}).get("uuid", "?")
            logger.info("Discovery registered: uuid=%s", uuid)
    except Exception as exc:
        logger.error("Discovery failed: %s", exc)


# ---------------------------------------------------------------------------
# gh / copilot env helpers
# ---------------------------------------------------------------------------

def _gh_env() -> dict:
    """Env for gh CLI subprocess calls (no GH_TOKEN so gh uses its own store)."""
    env = {**os.environ, "GH_CONFIG_DIR": GH_CONFIG_DIR, "NO_COLOR": "1"}
    env.pop("GH_TOKEN", None)
    return env


def _gh_token() -> str | None:
    """Return the OAuth token stored by `gh auth`, or None."""
    try:
        r = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True, text=True, env=_gh_env(), timeout=5,
        )
        t = r.stdout.strip()
        return t or None
    except Exception:
        return None


def _npm_global_bin() -> str:
    """Return the npm global bin directory (works even if not in PATH)."""
    try:
        r = subprocess.run(["npm", "bin", "-g"],
                           capture_output=True, text=True, timeout=10)
        p = r.stdout.strip()
        if p and os.path.isdir(p):
            return p
    except Exception:
        pass
    for p in ('/usr/local/bin', '/root/.npm-global/bin',
              '/usr/local/lib/node_modules/.bin'):
        if os.path.isdir(p):
            return p
    return '/usr/local/bin'


def _copilot_env() -> dict:
    """Env for the copilot PTY - forward gh token when available."""
    npm_bin = _npm_global_bin()
    existing_path = os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')
    env = {**os.environ,
           'PATH':          f'{npm_bin}:/usr/local/bin:{existing_path}',
           'GH_CONFIG_DIR': GH_CONFIG_DIR,
           'TERM':          'xterm-256color',
           'COLORTERM':     'truecolor'}
    token = _gh_token()
    if token:
        env['GH_TOKEN']     = token
        env['GITHUB_TOKEN'] = token
    return env


# ---------------------------------------------------------------------------
# Auth status / device-flow helpers
# ---------------------------------------------------------------------------

def get_auth_status() -> dict:
    try:
        r = subprocess.run(
            ["gh", "auth", "status", "--hostname", "github.com"],
            capture_output=True, text=True, env=_gh_env(), timeout=10,
        )
        authed = r.returncode == 0
        user = ""
        for line in (r.stdout + r.stderr).splitlines():
            if " as " in line:
                user = line.split(" as ")[-1].strip().lstrip("@").split()[0]
        return {
            "authenticated":    authed,
            "username":         user,
            "copilot_available": _check_copilot(),
        }
    except Exception as exc:
        return {"authenticated": False, "username": "",
                "copilot_available": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# GitHub Device OAuth Flow (pure Python — no gh subprocess for auth)
# ---------------------------------------------------------------------------
# gh writes interactive output directly to /dev/tty, bypassing stdout=PIPE,
# so we can never reliably capture the device code from a subprocess.
# Instead we implement the Device Flow ourselves: POST to GitHub, get the
# user_code + device_code directly, poll for the access token, and write
# the result to the gh config file so existing _gh_token() logic still works.
# ---------------------------------------------------------------------------

GH_OAUTH_CLIENT_ID = "Iv1.b507a08c87ecfe98"   # gh CLI's public OAuth App

# Server-side auth state — written by background poll thread, read by /auth/poll endpoint.
# This avoids the browser making outbound GitHub calls (rate-limiting, slow_down issues).
_auth_result: dict = {
    "done": False, "authenticated": False, "username": "", "lines": [],
}
_poll_thread: threading.Thread | None = None


def _save_gh_token(token: str) -> str:
    """Write OAuth token to the gh config dir; return GitHub username."""
    username = ""
    try:
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/json",
                "User-Agent": "ha-mcp-bridge/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            username = json.loads(resp.read()).get("login", "")
    except Exception as exc:
        logger.warning("Could not fetch GitHub username: %s", exc)

    config_dir = Path(GH_CONFIG_DIR)
    config_dir.mkdir(parents=True, exist_ok=True)
    content = "github.com:\n"
    content += f"    oauth_token: {token}\n"
    content += "    git_protocol: https\n"
    if username:
        content += f"    user: {username}\n"
    (config_dir / "hosts.yml").write_text(content)
    logger.info("GitHub token saved for @%s", username or "unknown")
    return username


def _device_poll_worker(device_code: str, interval: int, expires_at: float) -> None:
    """Background thread: polls GitHub until authorized, expired, or error."""
    global _auth_result, _poll_thread
    logger.info("Auth poll worker started (interval=%ds)", interval)
    while True:
        now = time.time()
        if now > expires_at:
            _auth_result = {"done": True, "authenticated": False, "username": "",
                            "lines": ["Code expired — please try again"]}
            logger.warning("Device code expired")
            break

        time.sleep(interval)

        payload = urllib.parse.urlencode({
            "client_id":   GH_OAUTH_CLIENT_ID,
            "device_code": device_code,
            "grant_type":  "urn:ietf:params:oauth:grant-type:device_code",
        }).encode()
        req = urllib.request.Request(
            "https://github.com/login/oauth/access_token",
            data=payload,
            headers={"Accept": "application/json", "User-Agent": "ha-mcp-bridge/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
        except Exception as exc:
            logger.warning("Token poll HTTP error: %s — retrying", exc)
            continue

        error = data.get("error", "")
        logger.info("Token poll response: error=%r keys=%s", error, list(data.keys()))

        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = min(interval + 5, 30)
            logger.info("slow_down received — new interval=%ds", interval)
            continue
        if error:
            desc = data.get("error_description", error)
            logger.warning("Device flow terminal error: %s", desc)
            _auth_result = {"done": True, "authenticated": False, "username": "",
                            "lines": [desc]}
            break

        access_token = data.get("access_token", "")
        if not access_token:
            logger.warning("Unexpected poll response (no token, no error): %s", data)
            continue

        try:
            username = _save_gh_token(access_token)
        except Exception as exc:
            logger.error("_save_gh_token failed: %s", exc)
            username = ""
        logger.info("Auth complete: @%s", username or "unknown")
        _auth_result = {"done": True, "authenticated": True, "username": username,
                        "lines": [f"Authenticated as @{username}"]}
        break

    _poll_thread = None


def start_auth_login() -> dict:
    """Start GitHub OAuth Device Flow; launch background poll thread."""
    global _auth_result, _poll_thread

    # Reset state
    _auth_result = {"done": False, "authenticated": False, "username": "", "lines": []}

    # Kill any existing poll thread (can't truly kill threads, but it will exit on its next iteration)
    if _poll_thread and _poll_thread.is_alive():
        logger.info("Replacing existing poll thread")

    payload = urllib.parse.urlencode({
        "client_id": GH_OAUTH_CLIENT_ID,
        "scope":     "read:user",
    }).encode()
    req = urllib.request.Request(
        "https://github.com/login/device/code",
        data=payload,
        headers={"Accept": "application/json", "User-Agent": "ha-mcp-bridge/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        logger.error("Device flow request failed: %s", exc)
        raise RuntimeError(f"Failed to contact GitHub: {exc}")

    if data.get("error"):
        msg = data.get("error_description") or data.get("error", "Unknown error")
        logger.error("GitHub device/code error: %s", msg)
        raise RuntimeError(msg)

    device_code      = data.get("device_code", "")
    user_code        = data.get("user_code", "")
    verification_uri = data.get("verification_uri", "https://github.com/login/device")
    interval         = int(data.get("interval", 5))
    expires_in       = int(data.get("expires_in", 900))

    if not device_code or not user_code:
        raise RuntimeError(f"GitHub did not return a device code. Response: {data}")

    expires_at = time.time() + expires_in
    logger.info("Device flow started: user_code=%s interval=%d expires_in=%d", user_code, interval, expires_in)

    _poll_thread = threading.Thread(
        target=_device_poll_worker,
        args=(device_code, interval, expires_at),
        daemon=True,
    )
    _poll_thread.start()

    return {"code": user_code, "url": verification_uri, "lines": []}


def poll_auth() -> dict:
    """Return current auth state (set by background poll thread)."""
    # If no flow has ever started, check actual gh auth status
    global _auth_result
    if not _poll_thread and not _auth_result.get("done"):
        s = get_auth_status()
        if s.get("authenticated"):
            _auth_result = {"done": True, "authenticated": True,
                            "username": s.get("username", ""), "lines": []}
    return dict(_auth_result)


# ---------------------------------------------------------------------------
# copilot binary helpers
# ---------------------------------------------------------------------------

def _copilot_path() -> str | None:
    """Return the full path to the copilot binary, or None. Cached after first call."""
    global _COPILOT_PATH
    if _COPILOT_PATH is not None:
        return _COPILOT_PATH or None
    import shutil
    npm_bin = _npm_global_bin()
    search_path = f'{npm_bin}:/usr/local/bin:/usr/bin:/bin'
    found = shutil.which('copilot', path=search_path)
    _COPILOT_PATH = found if found else ''
    if found:
        logger.info('copilot binary: %s', found)
    else:
        logger.warning('copilot not found in PATH=%s', search_path)
    return found or None


def _check_copilot() -> bool:
    """Return True if the copilot binary is available."""
    return bool(_copilot_path())


def _ensure_copilot() -> None:
    """Safety-net install in case run.sh's npm install was skipped or failed."""
    if _check_copilot():
        return
    logger.info("copilot not found — attempting npm install -g @github/copilot")
    try:
        r = subprocess.run(
            ["npm", "install", "-g", "@github/copilot"],
            capture_output=True, text=True, timeout=180,
        )
        if r.returncode != 0:
            logger.error("npm install failed (exit %s): %s",
                         r.returncode, r.stderr[-500:])
        elif _check_copilot():
            logger.info("copilot installed successfully")
            _COPILOT_PATH = None  # reset cache so next check re-scans
        else:
            logger.error("npm install succeeded but copilot still not runnable")
    except FileNotFoundError:
        logger.error("npm not found — nodejs/npm missing from image")
    except Exception as exc:
        logger.error("copilot install error: %s", exc)


# ---------------------------------------------------------------------------
# WebSocket <-> PTY  (subprocess.Popen ΓÇö no os.fork())
# ---------------------------------------------------------------------------

async def ws_terminal(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    """Bridge xterm.js over WebSocket to a real PTY running `copilot`.

    Uses subprocess.Popen with slave_fd as stdin/stdout/stderr so we get a
    proper controlling TTY without calling os.fork() from a multi-threaded
    asyncio process (which would trigger DeprecationWarning and can deadlock).
    """
    ws = aiohttp.web.WebSocketResponse()
    await ws.prepare(request)

    if not _check_copilot():
        await ws.send_bytes(
            b"\r\n\x1b[31m[ha-mcp-bridge] copilot binary not found.\x1b[0m\r\n"
            b"Check add-on logs. The binary may be installing in the background;\r\n"
            b"close and reopen this panel in ~30 seconds.\r\n"
        )
        await ws.close()
        return ws

    # Open a PTY pair.
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))



    # Spawn the REPL wrapper script — this keeps the session alive.
    # copilot is a one-shot CLI (exits after each response); the wrapper
    # loops to give a persistent interactive terminal experience.
    repl = Path(__file__).parent / "copilot-repl.sh"
    env = _copilot_env()
    proc = subprocess.Popen(
        ["/bin/sh", str(repl)],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        start_new_session=True,
        env=env,
    )
    os.close(slave_fd)   # parent no longer needs the slave end

    loop  = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def _on_readable() -> None:
        try:
            data = os.read(master_fd, 4096)
            loop.call_soon_threadsafe(queue.put_nowait, data)
        except OSError:
            loop.remove_reader(master_fd)
            loop.call_soon_threadsafe(queue.put_nowait, None)

    loop.add_reader(master_fd, _on_readable)

    async def pty_to_ws() -> None:
        while True:
            chunk = await queue.get()
            if chunk is None:
                await ws.close(code=1000, message=b"session ended")
                return
            try:
                await ws.send_bytes(chunk)
            except Exception:
                return

    reader_task = asyncio.create_task(pty_to_ws())

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                data: bytes = msg.data
                # Resize frame: 0x01 + big-endian uint16 rows + uint16 cols
                if len(data) >= 5 and data[0] == 0x01:
                    rows, cols = struct.unpack(">HH", data[1:5])
                    try:
                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                                    struct.pack("HHHH", rows, cols, 0, 0))
                    except OSError:
                        pass
                else:
                    try:
                        os.write(master_fd, data)
                    except OSError:
                        break
            elif msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    os.write(master_fd, msg.data.encode())
                except OSError:
                    break
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break
    finally:
        loop.remove_reader(master_fd)
        reader_task.cancel()
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass

    return ws


# ---------------------------------------------------------------------------
# GitHub Copilot Chat API  (used by the HA conversation agent via /chat)
# ---------------------------------------------------------------------------
# Flow:
#  1. gh auth token  -> short-lived GitHub OAuth token (with "copilot" scope)
#  2. GET github.com/copilot_internal/v2/token  -> Copilot bearer token
#  3. POST api.githubcopilot.com/chat/completions (OpenAI-compatible)
# ---------------------------------------------------------------------------

_copilot_api_token_cache: dict = {}


def _get_copilot_api_token() -> tuple[str, str]:
    """Return (token, auth_scheme) for the Copilot Chat API.

    Strategy:
      1. Try GET copilot_internal/v2/token  →  short-lived copilot token (Bearer).
      2. If 404 (no Copilot subscription tier OR endpoint moved), fall back to
         the raw GitHub OAuth token with the 'token' scheme, which api.githubcopilot.com
         also accepts for accounts with Copilot access.
      3. If both fail, raise RuntimeError with a clear message.

    Result is cached until 60 s before expiry.
    """
    global _copilot_api_token_cache
    now = time.time()
    cached = _copilot_api_token_cache
    if cached.get("token") and cached.get("expires_at", 0) > now + 60:
        return cached["token"], cached.get("scheme", "Bearer")

    gh_token = _gh_token()
    if not gh_token:
        raise RuntimeError(
            "Not authenticated with GitHub. "
            "Sign in via the Copilot panel in the HA sidebar."
        )

    # --- Attempt 1: internal token exchange -----------------------------------
    req = urllib.request.Request(
        "https://api.github.com/copilot_internal/v2/token",
        headers={
            "Authorization": f"token {gh_token}",
            "Accept": "application/json",
            "User-Agent": "ha-mcp-bridge/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        token = data.get("token")
        if token:
            from datetime import datetime
            expires_str = data.get("expires_at", "")
            try:
                expires_at = datetime.fromisoformat(
                    expires_str.replace("Z", "+00:00")).timestamp()
            except Exception:
                expires_at = now + 1740  # 29 min fallback
            _copilot_api_token_cache = {
                "token": token, "scheme": "Bearer", "expires_at": expires_at}
            logger.info("Copilot internal token obtained (expires in %.0f s)",
                        expires_at - now)
            return token, "Bearer"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.warning(
                "copilot_internal/v2/token returned 404 — "
                "falling back to direct OAuth token auth. "
                "(Ensure your GitHub account has an active Copilot subscription.)"
            )
        else:
            body = exc.read().decode(errors="replace")
            logger.error("Copilot token exchange HTTP %s: %s", exc.code, body[:300])
            raise RuntimeError(
                f"GitHub returned HTTP {exc.code} during Copilot token exchange. "
                "Ensure your account has Copilot access and re-authenticate via "
                "the sidebar (sign out then sign back in)."
            )

    # --- Attempt 2: OAuth token directly --------------------------------------
    # api.githubcopilot.com accepts GitHub OAuth tokens directly (scheme "token").
    # Cache for 5 min so we retry the internal exchange periodically.
    _copilot_api_token_cache = {
        "token": gh_token, "scheme": "token", "expires_at": now + 300}
    logger.info("Using GitHub OAuth token directly with Copilot API (fallback mode)")
    return gh_token, "token"


def _run_copilot_chat(prompt: str, history: list[dict] | None = None) -> str:
    """Call GitHub Copilot Chat API with HA tool support.
    Runs the full tool-call loop: Copilot can call HA tools (get states,
    call services, get history) and we execute them before returning."""
    token, scheme = _get_copilot_api_token()

    system_msg = {
        "role": "system",
        "content": (
            "You are GitHub Copilot, an AI assistant embedded in Home Assistant. "
            "You have access to real-time Home Assistant tools to query device states, "
            "control devices, and view history. Use them whenever the user asks about "
            "their home, devices, sensors, or wants to control something. "
            "Be concise and practical."
        ),
    }
    messages: list[dict] = [system_msg]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    api_headers = {
        "Authorization": f"{scheme} {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "GitHubCopilotChat/0.12.0",
        "Editor-Version": "vscode/1.85.1",
        "Editor-Plugin-Version": "copilot-chat/0.12.0",
        "Openai-Organization": "github-copilot",
        "openai-intent": "conversation-panel",
    }

    # Tool-call loop: keep calling until Copilot stops requesting tool calls.
    for _iteration in range(8):   # hard cap — prevents runaway loops
        payload = json.dumps({
            "model": "gpt-4o",
            "messages": messages,
            "tools": HA_TOOLS,
            "tool_choice": "auto",
            "stream": False,
            "n": 1,
            "temperature": 0.1,
            "max_tokens": 4096,
        }).encode()

        req = urllib.request.Request(
            "https://api.githubcopilot.com/chat/completions",
            data=payload, method="POST", headers=api_headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            logger.error("Copilot chat API HTTP %s: %s", exc.code, body[:500])
            if exc.code in (401, 403):
                global _copilot_api_token_cache
                _copilot_api_token_cache = {}
            raise RuntimeError(f"Copilot API error {exc.code}: {body[:300]}")

        logger.debug("Copilot API response keys: %s  finish=%s",
                     list(data.keys()),
                     (data.get("choices") or [{}])[0].get("finish_reason"))

        choices = data.get("choices") or []
        if not choices:
            logger.error("Copilot API returned no choices: %s", data)
            raise RuntimeError(f"Copilot API returned no choices: {data}")

        choice       = choices[0]
        finish_reason = choice.get("finish_reason", "")
        msg          = choice.get("message", {})

        # No tool calls — return final content.
        if finish_reason != "tool_calls":
            return (msg.get("content") or "").strip() or "(no response)"

        # Copilot wants to call HA tools.
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return (msg.get("content") or "").strip() or "(no response)"

        # Add assistant message with tool_calls to conversation.
        messages.append(msg)

        # Execute each tool call and add results.
        for tc in tool_calls:
            fn   = tc.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            logger.info("HA tool call: %s(%s)", name, args)
            result = _call_ha_tool(name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": result,
            })

    return "(max tool iterations reached)"


async def handle_chat(request: aiohttp.web.Request) -> aiohttp.web.Response:
    try:
        body = await request.json()
    except Exception:
        return aiohttp.web.json_response({"error": "invalid JSON"}, status=400)
    prompt = str(body.get("prompt", "")).strip()
    if not prompt:
        return aiohttp.web.json_response({"error": "prompt required"}, status=400)
    # Optional multi-turn conversation tracking.
    conv_id = str(body.get("conversation_id", "")).strip() or None
    history = _conversation_histories.get(conv_id, []) if conv_id else []
    try:
        loop = asyncio.get_running_loop()
        output = await loop.run_in_executor(
            None, _run_copilot_chat, prompt, history)
        # Persist history for multi-turn context.
        if conv_id:
            updated = list(history) + [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": output},
            ]
            # Trim to keep last N turns (each turn = 2 messages).
            _conversation_histories[conv_id] = updated[-(_MAX_HISTORY_TURNS * 2):]
        return aiohttp.web.json_response({"output": output})
    except RuntimeError as exc:
        return aiohttp.web.json_response({"error": str(exc)})
    except Exception as exc:
        logger.exception("Error in /chat")
        return aiohttp.web.json_response({"error": str(exc)}, status=500)

# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

async def handle_index(request: aiohttp.web.Request) -> aiohttp.web.Response:
    idx = STATIC_DIR / "index.html"
    if idx.exists():
        return aiohttp.web.Response(body=idx.read_bytes(), content_type="text/html")
    return aiohttp.web.Response(text="UI not found", status=404)


async def handle_health(request: aiohttp.web.Request) -> aiohttp.web.Response:
    s = get_auth_status()
    return aiohttp.web.json_response({
        "status":            "ok",
        "uptime":            round(time.time() - start_time, 2),
        "authenticated":     s["authenticated"],
        "copilot_available": s.get("copilot_available", False),
        "timestamp":         time.time(),
    })


async def handle_auth_status(request: aiohttp.web.Request) -> aiohttp.web.Response:
    loop = asyncio.get_running_loop()
    status = await loop.run_in_executor(None, get_auth_status)
    return aiohttp.web.json_response(status)


async def handle_auth_start(request: aiohttp.web.Request) -> aiohttp.web.Response:
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, start_auth_login)
        return aiohttp.web.json_response(result)
    except Exception as exc:
        logger.error("handle_auth_start error: %s", exc)
        return aiohttp.web.json_response({"error": str(exc), "code": "", "url": "", "lines": []}, status=200)


async def handle_auth_poll(request: aiohttp.web.Request) -> aiohttp.web.Response:
    # poll_auth() just reads module-level state — fast, no I/O.
    return aiohttp.web.json_response(poll_auth())


# ---------------------------------------------------------------------------
# /chat/stream  — SSE streaming endpoint for the web UI
# ---------------------------------------------------------------------------

_http_session: aiohttp.ClientSession | None = None


async def _get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


async def handle_chat_stream(request: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
    """Stream Copilot Chat API response as Server-Sent Events."""
    try:
        body = await request.json()
    except Exception:
        return aiohttp.web.Response(text="invalid JSON", status=400)

    prompt  = str(body.get("prompt", "")).strip()
    conv_id = str(body.get("conversation_id", "")).strip() or None
    if not prompt:
        return aiohttp.web.Response(text="prompt required", status=400)

    history = list(_conversation_histories.get(conv_id, [])) if conv_id else []

    # Prepare SSE response before any slow work so the connection is held.
    resp = aiohttp.web.StreamResponse(headers={
        "Content-Type":       "text/event-stream",
        "Cache-Control":      "no-cache",
        "X-Accel-Buffering":  "no",
    })
    await resp.prepare(request)

    async def sse(data: dict | str) -> None:
        payload = data if isinstance(data, str) else json.dumps(data)
        await resp.write(f"data: {payload}\n\n".encode())

    # Get auth token (sync — runs in thread executor).
    loop = asyncio.get_running_loop()
    try:
        token, scheme = await loop.run_in_executor(None, _get_copilot_api_token)
    except RuntimeError as exc:
        await sse({"error": str(exc)})
        await sse("[DONE]")
        return resp

    messages: list[dict] = [
        {
            "role": "system",
            "content": (
                "You are GitHub Copilot, an AI assistant embedded in Home Assistant. "
                "Help the user with home automation, YAML configuration, scripts, "
                "automations, integrations, shell commands, and code questions. "
                "Format code examples with appropriate fenced code blocks. "
                "Be concise and practical."
            ),
        }
    ]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    session  = await _get_http_session()
    full_buf: list[str] = []

    try:
        async with session.post(
            "https://api.githubcopilot.com/chat/completions",
            json={"model": "gpt-4o", "messages": messages, "stream": True, "n": 1},
            headers={
                "Authorization":         f"{scheme} {token}",
                "Content-Type":          "application/json",
                "Accept":                "text/event-stream",
                "User-Agent":            "GitHubCopilotChat/0.12.0",
                "Editor-Version":        "vscode/1.85.1",
                "Editor-Plugin-Version": "copilot-chat/0.12.0",
                "Openai-Organization":   "github-copilot",
                "openai-intent":         "conversation-panel",
            },
            timeout=aiohttp.ClientTimeout(total=90, connect=15),
        ) as upstream:
            if upstream.status != 200:
                err_text = await upstream.text()
                if upstream.status in (401, 403):
                    global _copilot_api_token_cache
                    _copilot_api_token_cache = {}
                await sse({"error": f"Copilot API error {upstream.status}: {err_text[:300]}"})
                await sse("[DONE]")
                return resp

            buf = ""
            async for raw in upstream.content.iter_chunked(512):
                buf += raw.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.rstrip("\r")
                    if not line or line == ":":
                        continue
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        # Persist history.
                        if conv_id and full_buf:
                            full_text = "".join(full_buf)
                            updated   = list(history) + [
                                {"role": "user",      "content": prompt},
                                {"role": "assistant", "content": full_text},
                            ]
                            _conversation_histories[conv_id] = (
                                updated[-(_MAX_HISTORY_TURNS * 2):]
                            )
                        await sse("[DONE]")
                        return resp
                    try:
                        chunk  = json.loads(data_str)
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta  = choices[0].get("delta", {}).get("content") or ""
                        if delta:
                            full_buf.append(delta)
                        await sse(data_str)   # forward raw chunk verbatim
                    except (KeyError, IndexError, json.JSONDecodeError):
                        pass

    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.exception("Streaming chat error")
        await sse({"error": str(exc)})

    await sse("[DONE]")
    return resp


# ---------------------------------------------------------------------------
# App + entry point
# ---------------------------------------------------------------------------

def make_app() -> aiohttp.web.Application:
    app = aiohttp.web.Application()
    app.router.add_get("/",              handle_index)
    app.router.add_get("/index.html",    handle_index)
    app.router.add_get("/health",        handle_health)
    app.router.add_get("/auth/status",   handle_auth_status)
    app.router.add_get("/ws",            ws_terminal)
    app.router.add_post("/chat",         handle_chat)
    app.router.add_post("/chat/stream",  handle_chat_stream)
    app.router.add_post("/auth/start",   handle_auth_start)
    app.router.add_post("/auth/poll",    handle_auth_poll)
    return app


async def _main() -> None:
    register_discovery()
    loop = asyncio.get_running_loop()
    # Ensure copilot binary (runtime fallback for skipped build-time installs).
    await loop.run_in_executor(None, _ensure_copilot)
    app = make_app()
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, HOST, PORT)
    await site.start()
    copilot_ok = _check_copilot()
    logger.info("ha_mcp_bridge listening on %s:%s  copilot=%s",
                HOST, PORT, copilot_ok)
    if not copilot_ok:
        logger.warning("copilot binary not available ΓÇö PTY will show an error")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(_main())
