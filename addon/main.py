"""HA MCP Bridge add-on.

Identical structure to ha-basic-addon (the proven s6-overlay pattern).

Responsibilities
----------------
1. Register with Supervisor so HA fires async_step_hassio in the integration.
2. Expose /health and /status endpoints so the custom integration can poll.

The MCP server connection is configured and probed by the *integration*
(coordinator.py), not this add-on process.  This keeps the add-on simple
and guarantees the s6-overlay service lifecycle works correctly.

Endpoints
---------
GET /health     liveness probe; returns {"status":"ok","uptime":<float>}
GET /status     extended status including add-on version
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
_LOGGER = logging.getLogger("ha_mcp_bridge")

OPTIONS_PATH = Path("/data/options.json")
SUPERVISOR_API = "http://supervisor"

# Supervisor injects this token automatically — never configure it manually.
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

_start_time = time.time()


def _load_options() -> dict:
    if not OPTIONS_PATH.exists():
        return {}
    try:
        return json.loads(OPTIONS_PATH.read_text())
    except json.JSONDecodeError:
        _LOGGER.warning("Unable to decode options.json, using defaults.")
        return {}


_OPTIONS = _load_options()
_PORT = int(_OPTIONS.get("port", 8099))


def register_discovery() -> None:
    """POST to Supervisor /discovery so HA fires async_step_hassio.

    The "discovery" key in config.json is ONLY an allowlist.
    The add-on MUST make this call on startup — it does NOT fire automatically.
    Supervisor validates the service name then notifies HA core, which creates
    a config flow with context={"source": SOURCE_HASSIO}.
    """
    if not SUPERVISOR_TOKEN:
        _LOGGER.warning(
            "SUPERVISOR_TOKEN not set — skipping discovery. "
            "This is expected in local dev; under Supervisor the token is always present."
        )
        return

    payload = json.dumps({
        "service": "ha_mcp_bridge",
        "config": {"host": "127.0.0.1", "port": _PORT},
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
            _LOGGER.info("Discovery registered with Supervisor: uuid=%s", uuid)
    except Exception as exc:
        _LOGGER.error("Failed to register discovery: %s", exc)


class _Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        uptime = round(time.time() - _start_time, 2)
        if self.path in ("/", "/health"):
            self._send_json({"status": "ok", "uptime": uptime})
        elif self.path == "/status":
            self._send_json({
                "status": "ok",
                "uptime": uptime,
                "port": _PORT,
                "version": "0.1.2",
            })
        else:
            self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def log_message(self, fmt, *args) -> None:
        _LOGGER.debug(fmt, *args)


def run() -> None:
    # Register with Supervisor FIRST so HA starts the config flow
    # while the HTTP server is coming up.
    register_discovery()

    server = ThreadingHTTPServer(("0.0.0.0", _PORT), _Handler)
    _LOGGER.info("Listening on port %d", _PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
