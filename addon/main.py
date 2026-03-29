"""HA MCP Bridge add-on entry point.

HTTP endpoints
--------------
GET  /            → serve chat UI (static/index.html)
GET  /health      → JSON health/uptime (polled by HA integration)
POST /chat        → proxy to GitHub Copilot chat completions API
POST /auth/device → start GitHub device-flow; returns {user_code, verification_uri, …}
GET  /auth/status → {authenticated: bool, username: str|null}
POST /auth/poll   → poll for token after user approves; returns {success: bool}
POST /auth/revoke → delete stored token

Supervisor discovery
--------------------
On startup the add-on POSTs to the Supervisor /discovery endpoint so that HA
creates a config flow (async_step_hassio).  The "discovery" key in config.json
is only an allowlist — it does NOT fire automatically.

Ingress
-------
When accessed via HA Supervisor ingress the Supervisor proxy strips the ingress
path prefix before forwarding, so the add-on always sees plain paths like
/health, /chat, etc.  The chat UI uses window.location to compute the correct
base URL for its API calls, so it works transparently under both direct access
and ingress.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# auth.py and copilot.py live in the same directory as main.py (/app).
sys.path.insert(0, str(Path(__file__).parent))
import auth
import copilot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ha_mcp_bridge")

OPTIONS_PATH = Path("/data/options.json")
STATIC_DIR = Path(__file__).parent / "static"
SUPERVISOR_API = "http://supervisor"
start_time = time.time()

# Supervisor injects this token into every add-on automatically.
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


def load_options() -> dict:
    if not OPTIONS_PATH.exists():
        return {}
    try:
        return json.loads(OPTIONS_PATH.read_text())
    except json.JSONDecodeError:
        logger.warning("Unable to decode options.json, falling back to defaults")
        return {}


OPTIONS = load_options()
HOST = str(OPTIONS.get("host", "0.0.0.0"))
PORT = int(OPTIONS.get("port", 8099))


def register_discovery() -> None:
    """POST to Supervisor /discovery so HA fires async_step_hassio.

    The "discovery" key in config.json only *permits* this call — it does NOT
    fire automatically.  The add-on must actively register every time it starts.

    Supervisor validates the service name against config.json "discovery" list,
    then forwards the payload to HA core which creates a config flow with
    context={"source": SOURCE_HASSIO} → calls async_step_hassio.
    """
    if not SUPERVISOR_TOKEN:
        logger.warning(
            "SUPERVISOR_TOKEN not set — skipping discovery registration. "
            "Expected in local dev; under Supervisor this token is always present."
        )
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
            logger.info("Discovery registered with Supervisor: uuid=%s", uuid)
    except Exception as exc:
        logger.error("Failed to register discovery with Supervisor: %s", exc)


class Handler(BaseHTTPRequestHandler):
    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            # Serve the Copilot chat UI.
            index = STATIC_DIR / "index.html"
            if index.exists():
                self._send_html(index.read_bytes())
            else:
                self._send_json({"error": "UI not found"}, HTTPStatus.NOT_FOUND)
            return

        if path == "/health":
            self._send_json({
                "status": "ok",
                "uptime": round(time.time() - start_time, 2),
                "authenticated": auth.is_authenticated(),
                "timestamp": time.time(),
            })
            return

        if path == "/auth/status":
            self._send_json({
                "authenticated": auth.is_authenticated(),
                "username": auth.get_username() if auth.is_authenticated() else None,
            })
            return

        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------

    def do_POST(self) -> None:
        path = self.path.split("?")[0]

        # ── /chat ──────────────────────────────────────────────────────
        if path == "/chat":
            body = self._read_body()
            messages = body.get("messages", [])
            if not messages:
                self._send_json({"error": "messages list is required"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                reply = copilot.chat(messages)
                self._send_json({"response": reply})
            except RuntimeError as exc:
                # Not-authenticated or API error — return as JSON so the UI
                # can display it gracefully rather than crashing.
                self._send_json({"error": str(exc)})
            except Exception as exc:
                logger.exception("Unexpected error in /chat")
                self._send_json({"error": f"Internal error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        # ── /auth/device ───────────────────────────────────────────────
        if path == "/auth/device":
            try:
                flow = auth.start_device_flow()
                self._send_json(flow)
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            return

        # ── /auth/poll ─────────────────────────────────────────────────
        if path == "/auth/poll":
            body = self._read_body()
            device_code = body.get("device_code", "")
            if not device_code:
                self._send_json({"error": "device_code required"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                result = auth.poll_device_token(device_code)
                if result:
                    auth.save_token(result)
                    logger.info("GitHub auth completed successfully")
                    self._send_json({"success": True})
                else:
                    # Still pending — tell the client to keep polling.
                    self._send_json({"success": False, "pending": True})
            except RuntimeError as exc:
                # Terminal error (expired / denied).
                self._send_json({"success": False, "error": str(exc)})
            except Exception as exc:
                self._send_json({"success": False, "error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            return

        # ── /auth/revoke ───────────────────────────────────────────────
        if path == "/auth/revoke":
            auth.revoke()
            self._send_json({"ok": True})
            return

        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def log_message(self, fmt, *args) -> None:
        logger.debug(fmt, *args)


def run() -> None:
    # Register with Supervisor FIRST so HA starts the config flow
    # while the HTTP server is coming up.
    register_discovery()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    logger.info(
        "ha_mcp_bridge listening on %s:%s  auth=%s",
        HOST, PORT, auth.is_authenticated(),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
