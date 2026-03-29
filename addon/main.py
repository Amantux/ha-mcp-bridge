from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ha_mcp_bridge")

OPTIONS_PATH = Path("/data/options.json")
SUPERVISOR_API = "http://supervisor"
start_time = time.time()

# Supervisor injects this token into every add-on automatically.
# It is the credential for all Supervisor API calls.
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

    The "discovery" key in config.json only *permits* this call - it does NOT
    fire automatically. The add-on must actively register every time it starts.

    Supervisor validates the service name against config.json "discovery" list,
    then forwards the payload to HA core which creates a config flow with
    context={"source": SOURCE_HASSIO} and calls async_step_hassio.
    """
    if not SUPERVISOR_TOKEN:
        logger.warning(
            "SUPERVISOR_TOKEN not set - skipping discovery registration. "
            "Expected in local dev; under Supervisor this token is always present."
        )
        return

    payload = json.dumps({
        "service": "ha_mcp_bridge",
        "config": {
            "host": "127.0.0.1",
            "port": PORT,
        },
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
    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/", "/health"):
            self._send_json({
                "status": "ok",
                "uptime": round(time.time() - start_time, 2),
                "path": self.path,
                "timestamp": time.time(),
            })
            return
        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, fmt, *args) -> None:
        logger.debug(fmt, *args)


def run() -> None:
    # Register with Supervisor FIRST so HA starts the config flow
    # while the HTTP server is coming up.
    register_discovery()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    logger.info("Listening on %s:%s", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
