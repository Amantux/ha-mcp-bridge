"""HA MCP Bridge — PTY bridge for the GitHub Copilot CLI.

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
os.fork() + os.execvpe() — avoids DeprecationWarning in multi-threaded
asyncio ("This process is multi-threaded, use of fork() may lead to
deadlocks") and is safe with aiohttp's thread-pool executor.

Resize protocol (binary WebSocket frames):
  first byte 0x01 + big-endian uint16 rows + uint16 cols

Auth flow
---------
1. UI loads: polls /auth/status.
   - If not authed with gh: renders an inline device-code card.
   - User clicks "Sign in with GitHub" → POST /auth/start → shows code + URL.
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


def _copilot_env() -> dict:
    """Env for the copilot PTY — forward gh token when available."""
    env = {**os.environ,
           "GH_CONFIG_DIR": GH_CONFIG_DIR,
           "TERM":          "xterm-256color",
           "COLORTERM":     "truecolor"}
    token = _gh_token()
    if token:
        env["GH_TOKEN"]     = token
        env["GITHUB_TOKEN"] = token
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
         "--web"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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

def _check_copilot() -> bool:
    """Return True only if `copilot` binary is present AND executes cleanly."""
    path = subprocess.run(["which", "copilot"],
                          capture_output=True, text=True).stdout.strip()
    if not path:
        logger.debug("copilot: not found in PATH")
        return False
    # Binary is present — can it actually run?
    try:
        r = subprocess.run(["copilot", "--version"],
                           capture_output=True, timeout=10)
        if r.returncode == 0:
            ver = (r.stdout or r.stderr or b"").decode(errors="replace").strip().splitlines()
            logger.debug("copilot version: %s", ver[0] if ver else "?")
            return True
        logger.warning("copilot --version exited %s (binary may need gcompat)",
                       r.returncode)
        return False
    except FileNotFoundError:
        return False
    except Exception as exc:
        logger.warning("copilot --version failed: %s", exc)
        return False


def _install_copilot_binary(dl_arch: str) -> bool:
    """Download the copilot tarball and install to /usr/local/bin/copilot.

    Uses `tar` via subprocess (more reliable across Python versions than
    Python's tarfile module with mutated member names).
    Returns True on success.
    """
    url = (
        "https://github.com/github/copilot-cli/releases/latest/download/"
        f"copilot-linux-{dl_arch}.tar.gz"
    )
    import tempfile
    logger.info("Downloading copilot from %s", url)
    with tempfile.TemporaryDirectory() as tmp:
        tarball = os.path.join(tmp, "copilot.tar.gz")

        # Download
        dl = subprocess.run(
            ["curl", "-fsSL", "--retry", "3", "--retry-delay", "2",
             "-o", tarball, url],
            timeout=180,
        )
        if dl.returncode != 0:
            raise RuntimeError(f"curl download failed (exit {dl.returncode})")
        size = os.path.getsize(tarball)
        logger.info("Downloaded %d bytes", size)
        if size < 1_000_000:
            raise RuntimeError(f"Tarball suspiciously small: {size} bytes")

        # Verify it's a valid gzip before extracting
        chk = subprocess.run(["tar", "-tzf", tarball], capture_output=True)
        if chk.returncode != 0:
            raise RuntimeError("Downloaded file is not a valid gzip tarball")
        files_in_tar = chk.stdout.decode(errors="replace").strip().splitlines()
        logger.info("Tarball contents: %s", files_in_tar)

        # Extract directly to /usr/local/bin/
        # The tarball has a single top-level file named `copilot`.
        ex = subprocess.run(
            ["tar", "-xzf", tarball, "-C", "/usr/local/bin/"],
            capture_output=True,
        )
        if ex.returncode != 0:
            raise RuntimeError(
                f"tar extract failed: {ex.stderr.decode(errors='replace')}")

    # Ensure executable bit
    binary = "/usr/local/bin/copilot"
    if not os.path.isfile(binary):
        raise RuntimeError(f"Expected {binary} after extraction but not found")
    os.chmod(binary, 0o755)
    return True


def _ensure_copilot() -> None:
    """Install the copilot binary if missing or non-functional.

    Runs at startup as a runtime fallback for build-time install failures
    (e.g., armv7 arch, offline network, checksum failures).
    """
    if _check_copilot():
        logger.info("copilot binary OK")
        return
    import platform
    machine  = platform.machine().lower()
    arch_map = {"x86_64": "x64", "amd64": "x64",
                "aarch64": "arm64", "arm64": "arm64"}
    dl_arch  = arch_map.get(machine)
    if not dl_arch:
        logger.warning("No copilot release for arch '%s' — PTY will be unavailable",
                       machine)
        return
    try:
        _install_copilot_binary(dl_arch)
        if _check_copilot():
            logger.info("copilot installed successfully (runtime)")
        else:
            logger.error(
                "copilot binary installed but won't run. "
                "This usually means the glibc shim (gcompat) is missing. "
                "Check that the Dockerfile includes: "
                "apk add gcompat libc6-compat"
            )
    except Exception as exc:
        logger.error("copilot runtime install failed: %s", exc)


# ---------------------------------------------------------------------------
# WebSocket <-> PTY  (subprocess.Popen — no os.fork())
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

    # Spawn `copilot` with slave_fd as its controlling terminal.
    # start_new_session=True creates a new process group / session so the
    # slave_fd becomes the controlling TTY for the child.
    proc = subprocess.Popen(
        ["copilot"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        start_new_session=True,
        env=_copilot_env(),
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
# /chat — non-interactive call for HA conversation agent
# ---------------------------------------------------------------------------

def _run_copilot_chat(prompt: str) -> str:
    if not _check_copilot():
        raise RuntimeError(
            "GitHub Copilot CLI is not available. "
            "Please authenticate via the Copilot panel in the HA sidebar."
        )
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
            "Copilot CLI returned no output. Sign in via the sidebar panel "
            "using the /login command."
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
        logger.warning("copilot binary not available — PTY will show an error")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(_main())
