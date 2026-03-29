"""HA MCP Bridge add-on.

Responsibilities
----------------
1. Register with Supervisor so HA fires async_step_hassio in the integration.
2. Connect to Home Assistant's built-in MCP Server via the Supervisor Core-API
   proxy (no user token required — Supervisor injects SUPERVISOR_TOKEN).
3. Expose a local HTTP API so the custom integration can poll it.

Endpoints
---------
GET /health          basic liveness check
GET /status          add-on status + MCP connection result (JSON)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_LOGGER = logging.getLogger("ha_mcp_bridge")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OPTIONS_PATH = Path("/data/options.json")
SUPERVISOR_API = "http://supervisor"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# HA Core API is reachable via the Supervisor proxy at /core/api/...
# No long-lived token needed — SUPERVISOR_TOKEN is sufficient.
CORE_API = f"{SUPERVISOR_API}/core/api"

# HA MCP Server SSE endpoint (available when homeassistant/mcp_server is enabled).
# We issue a lightweight OPTIONS / HEAD to check reachability rather than opening
# the full SSE stream (which would block forever).
MCP_ENDPOINT = f"{CORE_API}/mcp_server/sse"

_start_time = time.time()
_STATUS: dict = {"status": "starting", "mcp_available": False, "mcp_tools": [], "uptime": 0}


def _supervisor_request(method: str, path: str, body: dict | None = None) -> dict:
    """Make a synchronous request to the Supervisor / Core API."""
    url = f"{SUPERVISOR_API}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Supervisor discovery registration
# ---------------------------------------------------------------------------

def register_discovery(port: int) -> None:
    """POST to Supervisor /discovery.

    The "discovery" key in config.json is only an allowlist.
    The add-on MUST make this call on startup or HA never fires async_step_hassio.
    """
    if not SUPERVISOR_TOKEN:
        _LOGGER.warning("No SUPERVISOR_TOKEN — skipping discovery (not under Supervisor).")
        return

    try:
        result = _supervisor_request("POST", "/discovery", {
            "service": "ha_mcp_bridge",
            "config": {"host": "127.0.0.1", "port": port},
        })
        uuid = result.get("data", {}).get("uuid", "unknown")
        _LOGGER.info("Supervisor discovery registered: uuid=%s", uuid)
    except Exception as exc:
        _LOGGER.error("Failed to register Supervisor discovery: %s", exc)


# ---------------------------------------------------------------------------
# MCP Server connectivity check
# ---------------------------------------------------------------------------

def probe_mcp_server() -> dict:
    """Check whether HA's built-in MCP Server integration is reachable.

    We call POST /api/mcp_server/sse with an initialize JSON-RPC message.
    If HA's mcp_server component is enabled it responds with a valid JSON-RPC
    result; if not, we get a 404 or connection error.

    Returns a dict:
        {"available": bool, "tools": list[str], "error": str | None}
    """
    if not SUPERVISOR_TOKEN:
        return {"available": False, "tools": [], "error": "no_supervisor_token"}

    # MCP initialize request (JSON-RPC 2.0)
    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "ha-mcp-bridge", "version": "0.1.0"},
        },
    }

    try:
        result = _supervisor_request("POST", "/core/api/mcp_server/sse", init_payload)
        # A successful initialize returns {"jsonrpc":"2.0","id":1,"result":{...}}
        if "result" in result:
            _LOGGER.info("HA MCP Server reachable. Capabilities: %s", result["result"].get("capabilities"))
            # Now list tools
            tools_payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            tools_result = _supervisor_request("POST", "/core/api/mcp_server/sse", tools_payload)
            tools = [t["name"] for t in tools_result.get("result", {}).get("tools", [])]
            return {"available": True, "tools": tools, "error": None}
        return {"available": False, "tools": [], "error": result.get("error", {}).get("message", "unexpected_response")}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"available": False, "tools": [], "error": "mcp_server_not_enabled"}
        return {"available": False, "tools": [], "error": f"http_{exc.code}"}
    except Exception as exc:
        return {"available": False, "tools": [], "error": str(exc)}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def _json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        _STATUS["uptime"] = round(time.time() - _start_time, 2)
        if self.path in ("/", "/health"):
            self._json({"status": "ok", "uptime": _STATUS["uptime"]})
        elif self.path == "/status":
            self._json(_STATUS)
        else:
            self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def log_message(self, fmt, *args) -> None:
        _LOGGER.debug(fmt, *args)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    options = {}
    if OPTIONS_PATH.exists():
        try:
            options = json.loads(OPTIONS_PATH.read_text())
        except json.JSONDecodeError:
            _LOGGER.warning("Could not parse options.json, using defaults.")

    port = int(options.get("port", 8099))

    # 1. Register discovery so HA fires async_step_hassio
    register_discovery(port)

    # 2. Probe the MCP server and cache result in _STATUS
    _LOGGER.info("Probing HA MCP Server at %s …", MCP_ENDPOINT)
    mcp = probe_mcp_server()
    _STATUS.update({
        "status": "ok",
        "mcp_available": mcp["available"],
        "mcp_tools": mcp["tools"],
        "mcp_error": mcp.get("error"),
    })
    _LOGGER.info(
        "MCP Server probe: available=%s tools=%d error=%s",
        mcp["available"], len(mcp["tools"]), mcp.get("error"),
    )

    # 3. Serve HTTP
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    _LOGGER.info("Listening on port %d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
