"""HA MCP Bridge — PTY bridge for the GitHub Copilot CLI.

Architecture
------------
aiohttp on port 8099:
  GET  /              xterm.js UI
  GET  /ws            WebSocket <-> PTY running `copilot`
  GET  /health        JSON health (polled by HA integration)
  GET  /auth/status   {authenticated, username} via gh auth status
  POST /auth/start    start `gh auth login --web`; return {code, url}
  POST /auth/poll     return buffered lines + done/authenticated
  POST /chat          non-interactive for HA conversation agent

WebSocket <-> PTY
-----------------
os.fork() + os.execvpe() gives `copilot` a real PTY so it renders its
full TUI (animated banner, slash commands, model selector, etc.).
loop.add_reader() pipes master_fd -> WebSocket bytes without blocking.
Resize protocol: first byte 0x01 + struct.pack('>HH', rows, cols).

Auth
----
gh auth login stores an OAuth token in GH_CONFIG_DIR=/data/gh.
`copilot` has its own auth via `/login` slash command inside the TUI.
GH_TOKEN is forwarded to `copilot` from the gh-stored token so the
user only has to authenticate once when possible.
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
import urllib.request
from pathlib import Path

import aiohttp
import aiohttp.web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ha_mcp_bridge")

OPTIONS_PATH   = Path("/data/options.json")
STATIC_DIR     = Path(__file__).parent / "static"
SUPERVISOR_API = "http://supervisor"
GH_CONFIG_DIR  = "/data/gh"
start_time     = time.time()

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
ANSI = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


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
        logger.warning("SUPERVISOR_TOKEN not set — skipping discovery.")
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
# gh / copilot helpers
# ---------------------------------------------------------------------------

def _gh_env() -> dict:
    """Environment for gh subprocess calls."""
    env = {**os.environ, "GH_CONFIG_DIR": GH_CONFIG_DIR, "NO_COLOR": "1"}
    env.pop("GH_TOKEN", None)
    return env


def _copilot_env() -> dict:
    """Environment for the copilot PTY — pass stored gh token so copilot
    can authenticate automatically without requiring /login."""
    env = {**os.environ, "GH_CONFIG_DIR": GH_CONFIG_DIR,
           "TERM": "xterm-256color", "COLORTERM": "truecolor"}
    # Try to forward the gh OAuth token so copilot picks it up.
    token = _gh_token()
    if token:
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
    return env


def _gh_token() -> str | None:
    """Return the OAuth token stored by `gh auth`."""
    try:
        r = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True, text=True, env=_gh_env(), timeout=5,
        )
        t = r.stdout.strip()
        return t if t else None
    except Exception:
        return None


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
                user = line.split(" as ")[-1].strip().lstrip("@").split(" ")[0]
        return {"authenticated": authed, "username": user}
    except Exception as exc:
        return {"authenticated": False, "username": "", "error": str(exc)}


_auth_proc:  subprocess.Popen | None = None
_auth_lines: list[str] = []
_auth_lock   = threading.Lock()


def _auth_reader(proc: subprocess.Popen) -> None:
    for raw in proc.stdout:
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
        ["gh", "auth", "login", "--hostname", "github.com",
         "--git-protocol", "https", "--web"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=_gh_env(), text=True,
    )
    with _auth_lock:
        _auth_proc = proc
    threading.Thread(target=_auth_reader, args=(proc,), daemon=True).start()

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


def _check_copilot() -> bool:
    """Return True if the `copilot` binary is available and executable."""
    try:
        r = subprocess.run(["copilot", "--version"],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except FileNotFoundError:
        return False
    except Exception:
        return False


def _ensure_copilot() -> None:
    """Install the copilot binary at runtime if missing (build-time install
    skipped on unsupported arches like armv7, or if network was unavailable)."""
    if _check_copilot():
        return
    import platform
    machine = platform.machine().lower()
    # Only supported for x86_64 and aarch64
    if machine not in ("x86_64", "amd64", "aarch64", "arm64"):
        logger.warning("copilot binary not available for arch %s", machine)
        return
    logger.info("copilot binary missing — attempting runtime install…")
    try:
        r = subprocess.run(
            ["bash", "-c",
             "curl -fsSL https://gh.io/copilot-install | PREFIX=/usr/local bash"],
            timeout=120,
        )
        if r.returncode == 0 and _check_copilot():
            logger.info("copilot installed successfully")
        else:
            logger.warning("copilot install returned %s", r.returncode)
    except Exception as exc:
        logger.warning("copilot runtime install failed: %s", exc)


# ---------------------------------------------------------------------------
# WebSocket <-> PTY  (spawns `copilot`)
# ---------------------------------------------------------------------------

async def ws_terminal(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    """Bridge xterm.js to a real PTY running the GitHub Copilot CLI."""
    ws = aiohttp.web.WebSocketResponse()
    await ws.prepare(request)

    if not _check_copilot():
        await ws.send_str(
            "\r\n\x1b[31m[ha-mcp-bridge] ERROR: 'copilot' binary not found.\x1b[0m\r\n"
            "Architecture may be unsupported (armv7) or install failed at build time.\r\n"
            "Check add-on logs for details.\r\n"
        )
        await ws.close()
        return ws

    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

    pid = os.fork()
    if pid == 0:                      # ── child ──
        os.close(master_fd)
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        os.execvpe("copilot", ["copilot"], _copilot_env())
        os._exit(1)

    # ── parent ──
    os.close(slave_fd)
    loop = asyncio.get_running_loop()
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
                await ws.close()
                return
            try:
                await ws.send_bytes(chunk)
            except Exception:
                return

    reader_task = asyncio.create_task(pty_to_ws())

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                data = msg.data
                # Resize: 0x01 + big-endian uint16 rows + uint16 cols
                if len(data) >= 5 and data[0] == 0x01:
                    rows, cols = struct.unpack(">HH", data[1:5])
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                                struct.pack("HHHH", rows, cols, 0, 0))
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
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        except OSError:
            pass

    return ws


# ---------------------------------------------------------------------------
# /chat — non-interactive for HA conversation agent
# ---------------------------------------------------------------------------

def _run_copilot_chat(prompt: str) -> str:
    """Send a single prompt to `copilot` non-interactively via stdin."""
    env = {**_copilot_env(), "NO_COLOR": "1"}
    result = subprocess.run(
        ["copilot", "--no-tty"],
        input=prompt + "\n/exit\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
    )
    combined = result.stdout + (("\n" + result.stderr) if result.stderr else "")
    clean = ANSI.sub("", combined).strip()
    if not clean and result.returncode != 0:
        raise RuntimeError(
            "copilot CLI failed. Make sure you have signed in via the Copilot "
            "panel in the HA sidebar (/login command inside the terminal)."
        )
    return clean or "(no response)"


async def handle_chat(request: aiohttp.web.Request) -> aiohttp.web.Response:
    try:
        body = await request.json()
    except Exception:
        return aiohttp.web.json_response({"error": "invalid JSON"}, status=400)
    prompt = str(body.get("prompt", "")).strip()
    if not prompt:
        return aiohttp.web.json_response({"error": "prompt required"}, status=400)
    try:
        loop = asyncio.get_running_loop()
        output = await loop.run_in_executor(None, _run_copilot_chat, prompt)
        return aiohttp.web.json_response({"output": output})
    except RuntimeError as exc:
        return aiohttp.web.json_response({"error": str(exc)})
    except Exception as exc:
        logger.exception("Error in /chat")
        return aiohttp.web.json_response({"error": str(exc)}, status=500)


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------

async def handle_index(request: aiohttp.web.Request) -> aiohttp.web.Response:
    idx = STATIC_DIR / "index.html"
    if idx.exists():
        return aiohttp.web.Response(body=idx.read_bytes(), content_type="text/html")
    return aiohttp.web.Response(text="UI not found", status=404)


async def handle_health(request: aiohttp.web.Request) -> aiohttp.web.Response:
    s = get_auth_status()
    return aiohttp.web.json_response({
        "status": "ok",
        "uptime": round(time.time() - start_time, 2),
        "authenticated": s["authenticated"],
        "copilot_available": _check_copilot(),
        "timestamp": time.time(),
    })


async def handle_auth_status(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.json_response(get_auth_status())


async def handle_auth_start(request: aiohttp.web.Request) -> aiohttp.web.Response:
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, start_auth_login)
    return aiohttp.web.json_response(result)


async def handle_auth_poll(request: aiohttp.web.Request) -> aiohttp.web.Response:
    with _auth_lock:
        lines = list(_auth_lines)
        done = _auth_proc is None or _auth_proc.poll() is not None
    s = get_auth_status() if done else {}
    return aiohttp.web.json_response({
        "lines": lines, "done": done,
        "authenticated": s.get("authenticated", False),
        "username": s.get("username", ""),
    })


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
    # Ensure copilot binary exists (runtime fallback for arches where the
    # build-time install was skipped, e.g., armv7 or offline builds).
    await loop.run_in_executor(None, _ensure_copilot)
    app = make_app()
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, HOST, PORT)
    await site.start()
    copilot_ok = _check_copilot()
    logger.info("ha_mcp_bridge listening on %s:%s  copilot=%s", HOST, PORT, copilot_ok)
    if not copilot_ok:
        logger.warning("'copilot' binary not available — PTY terminal will show an error")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(_main())
