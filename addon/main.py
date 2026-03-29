"""HA MCP Bridge — PTY bridge for gh copilot.

Architecture
------------
aiohttp serves everything on port 8099:
  GET  /          xterm.js UI
  GET  /ws        WebSocket <-> PTY running `gh copilot suggest -t shell`
  GET  /health    JSON health (polled by HA integration)
  GET  /auth/status
  POST /auth/start    starts `gh auth login --web`, returns {code, url}
  POST /auth/poll     returns buffered lines + done/authenticated

WebSocket <-> PTY bridge
------------------------
os.fork() + os.execvpe() gives gh a real TTY (slave fd) so it renders
its interactive selection menus with colors. The asyncio event loop
reads master_fd via loop.add_reader() (non-blocking) and forwards raw
bytes to the xterm.js WebSocket. Input from xterm.js is written to the
master fd. Terminal resize messages are sent as binary frames:
    b'\x01' + struct.pack('HH', rows, cols)
which the handler applies via TIOCSWINSZ.

GH_CONFIG_DIR=/data/gh persists the OAuth token across restarts.
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
GH_EXT_SRC     = Path("/app/gh-extensions/extensions")
start_time     = time.time()

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
ANSI = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


# ---------------------------------------------------------------------------
# Options / discovery
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
# gh helpers
# ---------------------------------------------------------------------------

def _gh_env() -> dict:
    env = {**os.environ, "GH_CONFIG_DIR": GH_CONFIG_DIR, "NO_COLOR": "0",
           "TERM": "xterm-256color"}
    env.pop("GH_TOKEN", None)
    return env


def _install_extensions() -> None:
    if not GH_EXT_SRC.exists():
        return
    import shutil
    dest = Path(GH_CONFIG_DIR) / "extensions"
    dest.mkdir(parents=True, exist_ok=True)
    for item in GH_EXT_SRC.iterdir():
        target = dest / item.name
        if not target.exists():
            try:
                shutil.copytree(str(item), str(target))
                logger.info("Installed gh extension: %s", item.name)
            except Exception as exc:
                logger.warning("Failed to copy %s: %s", item.name, exc)


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


# ---------------------------------------------------------------------------
# WebSocket <-> PTY handler
# ---------------------------------------------------------------------------

async def ws_terminal(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    """Spawn `gh copilot suggest -t shell` in a PTY; bridge to xterm.js."""
    ws = aiohttp.web.WebSocketResponse()
    await ws.prepare(request)

    # Create a PTY pair — slave is the TTY the child process gets.
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

    # fork() so the child gets a fresh process with the slave as its TTY.
    pid = os.fork()
    if pid == 0:                          # ── child ──
        os.close(master_fd)
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        os.execvpe("gh",
                   ["gh", "copilot", "suggest", "-t", "shell"],
                   _gh_env())
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
                # Resize protocol: first byte 0x01, then struct HH (rows, cols)
                if len(data) >= 5 and data[0] == 0x01:
                    rows, cols = struct.unpack("HH", data[1:5])
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
# HTTP routes
# ---------------------------------------------------------------------------

async def handle_index(request: aiohttp.web.Request) -> aiohttp.web.Response:
    idx = STATIC_DIR / "index.html"
    if idx.exists():
        return aiohttp.web.Response(body=idx.read_bytes(),
                                    content_type="text/html")
    return aiohttp.web.Response(text="UI not found", status=404)


async def handle_health(request: aiohttp.web.Request) -> aiohttp.web.Response:
    s = get_auth_status()
    return aiohttp.web.json_response({
        "status": "ok",
        "uptime": round(time.time() - start_time, 2),
        "authenticated": s["authenticated"],
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
        "lines": lines,
        "done": done,
        "authenticated": s.get("authenticated", False),
        "username": s.get("username", ""),
    })


# ---------------------------------------------------------------------------
# /chat — non-interactive, used by the HA conversation agent
# ---------------------------------------------------------------------------

def _run_copilot_chat(prompt: str) -> str:
    """Run gh copilot suggest non-interactively; strip ANSI; return text."""
    result = subprocess.run(
        ["gh", "copilot", "suggest", "-t", "shell", prompt],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env={**_gh_env(), "NO_COLOR": "1"},
        timeout=60,
    )
    combined = result.stdout + (("\n" + result.stderr) if result.stderr else "")
    clean = ANSI.sub("", combined).strip()
    if not clean:
        if result.returncode != 0:
            raise RuntimeError(
                f"gh copilot exited {result.returncode}. "
                "Sign in via the Copilot panel in the HA sidebar first."
            )
        raise RuntimeError("gh copilot returned no output.")
    return clean


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
# App factory + entry point
# ---------------------------------------------------------------------------

def make_app() -> aiohttp.web.Application:
    app = aiohttp.web.Application()
    app.router.add_get("/",            handle_index)
    app.router.add_get("/index.html",  handle_index)
    app.router.add_get("/health",      handle_health)
    app.router.add_get("/auth/status", handle_auth_status)
    app.router.add_get("/ws",          ws_terminal)
    app.router.add_post("/chat",        handle_chat)
    app.router.add_post("/auth/start",  handle_auth_start)
    app.router.add_post("/auth/poll",   handle_auth_poll)
    return app


async def _main() -> None:
    _install_extensions()
    register_discovery()
    app = make_app()
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, HOST, PORT)
    await site.start()
    logger.info("ha_mcp_bridge listening on %s:%s", HOST, PORT)
    # Run forever
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(_main())
