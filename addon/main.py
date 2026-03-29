"""HA MCP Bridge add-on — terminal pipe to gh copilot.

How it works
------------
All GitHub auth and Copilot API access is delegated to the official `gh` CLI.

  Auth:  POST /auth/start   -> runs `gh auth login --web --skip-ssh-key` and
                               captures the one-time device code + URL from
                               gh's stdout so the web UI can display them.
         GET  /auth/status  -> `gh auth status`

  Chat:  POST /chat         -> runs `gh copilot suggest -t shell "<prompt>"`
                               as a subprocess; stdout/stderr piped back as
                               JSON.  `gh` handles the Copilot token exchange
                               internally using its own stored OAuth token.

  GET /              -> terminal web UI (static/index.html)
  GET /health        -> JSON health (polled by HA integration)

GH_CONFIG_DIR is set to /data/gh so authentication persists across add-on
restarts.  On first start the entrypoint copies the bundled gh-copilot
extension binaries into /data/gh/extensions so the user does not need
network access at runtime.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ha_mcp_bridge")

OPTIONS_PATH = Path("/data/options.json")
STATIC_DIR = Path(__file__).parent / "static"
SUPERVISOR_API = "http://supervisor"
GH_CONFIG_DIR = "/data/gh"
GH_EXT_SRC = Path("/app/gh-extensions/extensions")
start_time = time.time()

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gh_env() -> dict:
    """Return environment for gh subprocess calls."""
    env = {**os.environ, "GH_CONFIG_DIR": GH_CONFIG_DIR, "NO_COLOR": "1"}
    env.pop("GH_TOKEN", None)  # let gh use its own stored token
    return env


def _install_extensions() -> None:
    """Copy bundled extension binaries into /data/gh/extensions if needed."""
    if not GH_EXT_SRC.exists():
        return
    dest = Path(GH_CONFIG_DIR) / "extensions"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        import shutil
        for item in GH_EXT_SRC.iterdir():
            target = dest / item.name
            if not target.exists():
                shutil.copytree(str(item), str(target))
                logger.info("Installed gh extension: %s", item.name)
    except Exception as exc:
        logger.warning("Failed to copy gh extensions: %s", exc)


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
        logger.warning("SUPERVISOR_TOKEN not set — skipping discovery registration.")
        return

    payload = json.dumps({
        "service": "ha_mcp_bridge",
        "config": {"host": "127.0.0.1", "port": PORT},
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{SUPERVISOR_API}/discovery",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            uuid = body.get("data", {}).get("uuid", "unknown")
            logger.info("Discovery registered: uuid=%s", uuid)
    except Exception as exc:
        logger.error("Discovery registration failed: %s", exc)


# ---------------------------------------------------------------------------
# Auth state (shared between handler threads)
# ---------------------------------------------------------------------------

_auth_proc: subprocess.Popen | None = None
_auth_output_lines: list[str] = []
_auth_lock = threading.Lock()


def _auth_reader(proc: subprocess.Popen) -> None:
    """Background thread: read gh auth login output and stash lines."""
    for raw in proc.stdout:
        line = raw.rstrip()
        logger.info("[gh auth] %s", line)
        with _auth_lock:
            _auth_output_lines.append(line)
    proc.wait()


def start_auth_login() -> dict:
    """Spawn `gh auth login --web --skip-ssh-key` and capture device code."""
    global _auth_proc, _auth_output_lines

    # Kill any previous flow
    with _auth_lock:
        if _auth_proc and _auth_proc.poll() is None:
            _auth_proc.kill()
        _auth_output_lines = []

    proc = subprocess.Popen(
        [
            "gh", "auth", "login",
            "--hostname", "github.com",
            "--git-protocol", "https",
            "--web",
            "--skip-ssh-key",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=_gh_env(),
        text=True,
    )
    with _auth_lock:
        _auth_proc = proc

    t = threading.Thread(target=_auth_reader, args=(proc,), daemon=True)
    t.start()

    # Give gh a moment to print the device code
    time.sleep(3)
    with _auth_lock:
        lines = list(_auth_output_lines)

    # Parse code + URL from gh output lines like:
    #   ! First copy your one-time code: XXXX-XXXX
    #   - Open this URL to continue in your web browser: https://github.com/login/device
    code, url = "", "https://github.com/login/device"
    for line in lines:
        if "one-time code" in line or "copy your" in line:
            parts = line.split(":")
            if len(parts) >= 2:
                code = parts[-1].strip()
        if "github.com/login/device" in line or "Open this URL" in line:
            for part in line.split():
                if part.startswith("https://"):
                    url = part

    return {"code": code, "url": url, "lines": lines}


def get_auth_status() -> dict:
    result = subprocess.run(
        ["gh", "auth", "status", "--hostname", "github.com"],
        capture_output=True, text=True, env=_gh_env(), timeout=10,
    )
    authed = result.returncode == 0
    user = ""
    for line in (result.stdout + result.stderr).splitlines():
        if "Logged in to" in line or "as " in line:
            parts = line.split(" as ")
            if len(parts) > 1:
                user = parts[-1].strip().lstrip("@")
    return {"authenticated": authed, "username": user, "detail": result.stderr.strip()}


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

def run_copilot_chat(prompt: str) -> str:
    """Run `gh copilot suggest -t shell <prompt>` and return the response text.

    `gh copilot suggest` is interactive (shows a selection menu at the end).
    We pipe it stdin=DEVNULL so it cannot block waiting for terminal input;
    when it tries to write the interactive menu to a non-TTY stdout it either
    writes the plain text or exits. We collect all stdout/stderr and strip
    ANSI codes before returning.
    """
    import re

    result = subprocess.run(
        ["gh", "copilot", "suggest", "-t", "shell", prompt],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=_gh_env(),
        timeout=60,
    )

    combined = result.stdout + ("\n" + result.stderr if result.stderr else "")

    # Strip ANSI escape codes
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    clean = ansi_escape.sub("", combined).strip()

    if not clean:
        if result.returncode != 0:
            raise RuntimeError(
                f"gh copilot exited with code {result.returncode}. "
                "Make sure you are signed in (use the Sign in button above)."
            )
        raise RuntimeError("gh copilot returned no output.")

    return clean


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            idx = STATIC_DIR / "index.html"
            self._send_html(idx.read_bytes() if idx.exists() else b"<h1>UI not found</h1>")
            return

        if path == "/health":
            status = get_auth_status()
            self._send_json({
                "status": "ok",
                "uptime": round(time.time() - start_time, 2),
                "authenticated": status["authenticated"],
                "timestamp": time.time(),
            })
            return

        if path == "/auth/status":
            self._send_json(get_auth_status())
            return

        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = self.path.split("?")[0]

        if path == "/chat":
            body = self._read_body()
            prompt = str(body.get("prompt", "")).strip()
            if not prompt:
                self._send_json({"error": "prompt is required"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                reply = run_copilot_chat(prompt)
                self._send_json({"output": reply})
            except RuntimeError as exc:
                self._send_json({"error": str(exc)})
            except Exception as exc:
                logger.exception("Error in /chat")
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if path == "/auth/start":
            try:
                result = start_auth_login()
                self._send_json(result)
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            return

        if path == "/auth/poll":
            # Return buffered output lines from the running gh auth login process.
            with _auth_lock:
                lines = list(_auth_output_lines)
                done = _auth_proc is None or _auth_proc.poll() is not None
            status = get_auth_status() if done else {}
            self._send_json({
                "lines": lines,
                "done": done,
                "authenticated": status.get("authenticated", False),
                "username": status.get("username", ""),
            })
            return

        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def log_message(self, fmt, *args) -> None:
        logger.debug(fmt, *args)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    _install_extensions()
    register_discovery()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    logger.info("ha_mcp_bridge listening on %s:%s", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
