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
import urllib.request
from pathlib import Path

import aiohttp
import aiohttp.web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ha_mcp_bridge")

OPTIONS_PATH     = Path("/data/options.json")
STATIC_DIR       = Path(__file__).parent / "static"
SUPERVISOR_API   = "http://supervisor"
GH_CONFIG_DIR    = "/data/gh"
start_time       = time.time()

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
ANSI             = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

_COPILOT_PATH: str | None = None  # cached path to copilot binary

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


_auth_proc:  subprocess.Popen | None = None
_auth_lines: list[str]               = []
_auth_lock                           = threading.Lock()


def _auth_reader(proc: subprocess.Popen) -> None:
    for raw in proc.stdout:  # type: ignore[union-attr]
        line = ANSI.sub("", raw).rstrip()
        logger.info("[gh auth] %s", line)
        with _auth_lock:
            _auth_lines.append(line)
    proc.wait()


def start_auth_login() -> dict:
    global _auth_proc, _auth_lines
    with _auth_lock:
        if _auth_proc and _auth_proc.poll() is None:
            _auth_proc.kill()
        _auth_lines = []

    proc = subprocess.Popen(
        ["gh", "auth", "login",
         "--hostname",     "github.com",
         "--git-protocol", "https",
         "--scopes",       "copilot",
         "--web"],
         "--scopes", "copilot",  # request Copilot API access
        env=_gh_env(), text=True,
    )
    with _auth_lock:
        _auth_proc = proc
    threading.Thread(target=_auth_reader, args=(proc,), daemon=True).start()

    # Give the process a moment to emit the device code.
    time.sleep(3)
    with _auth_lock:
        lines = list(_auth_lines)

    code, url = "", "https://github.com/login/device"
    for line in lines:
        low = line.lower()
        if "one-time code" in low or "copy your" in low:
            code = line.split(":")[-1].strip()
        if "github.com/login/device" in line:
            for part in line.split():
                if part.startswith("https://"):
                    url = part
    return {"code": code, "url": url, "lines": lines}


def poll_auth() -> dict:
    with _auth_lock:
        lines = list(_auth_lines)
        done  = _auth_proc is None or _auth_proc.poll() is not None
    s = get_auth_status() if done else {}
    return {
        "lines":         lines,
        "done":          done,
        "authenticated": s.get("authenticated", False),
        "username":      s.get("username", ""),
    }


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


def _get_copilot_api_token() -> str:
    """Exchange the GitHub OAuth token for a short-lived Copilot API token.
    Result is cached until 60 s before expiry."""
    global _copilot_api_token_cache
    now = time.time()
    cached = _copilot_api_token_cache
    if cached.get("token") and cached.get("expires_at", 0) > now + 60:
        return cached["token"]

    gh_token = _gh_token()
    if not gh_token:
        raise RuntimeError(
            "Not authenticated with GitHub. "
            "Sign in via the Copilot panel in the HA sidebar."
        )

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
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        logger.error("Copilot token exchange HTTP %s: %s", exc.code, body[:300])
        raise RuntimeError(
            f"GitHub returned HTTP {exc.code} during Copilot token exchange. "
            "Ensure your account has Copilot access and re-authenticate with "
            "the --scopes copilot flag (sign out and back in via the sidebar)."
        )

    token = data.get("token")
    if not token:
        raise RuntimeError("Copilot token exchange returned no token: " + str(data))

    # expires_at: "2026-01-01T00:30:00Z"
    from datetime import datetime, timezone
    expires_str = data.get("expires_at", "")
    try:
        expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        expires_at = now + 1740  # 29 min fallback

    _copilot_api_token_cache = {"token": token, "expires_at": expires_at}
    logger.info("Copilot API token obtained (expires in %.0f s)", expires_at - now)
    return token


def _run_copilot_chat(prompt: str) -> str:
    """Call GitHub Copilot Chat API for the HA conversation agent.
    Uses the OpenAI-compatible endpoint at api.githubcopilot.com."""
    token = _get_copilot_api_token()

    payload = json.dumps({
        "model": "gpt-4o",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are GitHub Copilot, an AI assistant embedded in Home Assistant. "
                    "Help the user with shell commands, code questions, and technical tasks. "
                    "Be concise and practical."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "n": 1,
        "temperature": 0,
    }).encode()

    req = urllib.request.Request(
        "https://api.githubcopilot.com/chat/completions",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "ha-mcp-bridge/1.0",
            "Editor-Version": "ha-mcp-bridge/1.0",
            "Editor-Plugin-Version": "ha-mcp-bridge/1.0",
            "Copilot-Integration-Id": "ha-mcp-bridge",
            "openai-intent": "conversation-panel",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        return content.strip() or "(no response)"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        logger.error("Copilot chat API HTTP %s: %s", exc.code, body[:500])
        raise RuntimeError(f"Copilot API error {exc.code}: {body[:300]}")
    except Exception as exc:
        logger.exception("Copilot chat API call failed")
        raise RuntimeError(str(exc))


async def handle_chat(request: aiohttp.web.Request) -> aiohttp.web.Response:
    try:
        body = await request.json()
    except Exception:
        return aiohttp.web.json_response({"error": "invalid JSON"}, status=400)
    prompt = str(body.get("prompt", "")).strip()
    if not prompt:
        return aiohttp.web.json_response({"error": "prompt required"}, status=400)
    try:
        output = await asyncio.get_running_loop().run_in_executor(
            None, _run_copilot_chat, prompt)
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
    result = await loop.run_in_executor(None, start_auth_login)
    return aiohttp.web.json_response(result)


async def handle_auth_poll(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.json_response(poll_auth())


# ---------------------------------------------------------------------------
# App + entry point
# ---------------------------------------------------------------------------

def make_app() -> aiohttp.web.Application:
    app = aiohttp.web.Application()
    app.router.add_get("/",            handle_index)
    app.router.add_get("/index.html",  handle_index)
    app.router.add_get("/health",      handle_health)
    app.router.add_get("/auth/status", handle_auth_status)
    app.router.add_get("/ws",          ws_terminal)
    app.router.add_post("/chat",       handle_chat)
    app.router.add_post("/auth/start", handle_auth_start)
    app.router.add_post("/auth/poll",  handle_auth_poll)
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
