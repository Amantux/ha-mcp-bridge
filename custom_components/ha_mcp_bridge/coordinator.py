from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_HOST,
    CONF_MCP_TOKEN,
    CONF_MCP_URL,
    CONF_PORT,
    CONF_UPDATE_INTERVAL,
    DEFAULT_MCP_TOKEN,
    DEFAULT_MCP_URL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)
from .helpers import build_health_url

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = ClientTimeout(total=10)
_MCP_TIMEOUT = ClientTimeout(total=15)


class HaMcpBridgeDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, session: ClientSession, entry: ConfigEntry) -> None:
        self._session = session
        self._url = build_health_url(entry.data[CONF_HOST], int(entry.data[CONF_PORT]))
        # MCP settings may live in entry.data (set at setup) OR entry.options (updated later).
        self._mcp_url: str = str(
            entry.options.get(CONF_MCP_URL) or entry.data.get(CONF_MCP_URL, DEFAULT_MCP_URL)
        ).strip()
        self._mcp_token: str = str(
            entry.options.get(CONF_MCP_TOKEN) or entry.data.get(CONF_MCP_TOKEN, DEFAULT_MCP_TOKEN)
        ).strip()
        interval = int(entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            name="ha_mcp_bridge",
            update_interval=timedelta(seconds=interval),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            async with self._session.get(self._url, timeout=_TIMEOUT) as response:
                response.raise_for_status()
                data = await response.json()
        except (ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Error fetching add-on health: {err}") from err

        # Probe the MCP server if configured.
        data["mcp_available"] = None
        data["mcp_error"] = "not_configured"
        if self._mcp_url:
            mcp = await self._probe_mcp()
            data["mcp_available"] = mcp["available"]
            data["mcp_error"] = mcp.get("error")

        return data

    async def _probe_mcp(self) -> dict[str, Any]:
        """Probe the MCP server by sending a JSON-RPC ping to /mcp/message.

        We POST without a session_id so the server processes it (202) without
        needing an active SSE stream.  A 202 response confirms the MCP endpoint
        is live.  We avoid GET /mcp/sse because that is a long-running SSE
        stream that would never resolve.
        """
        # Derive the message endpoint from the SSE URL.
        msg_url = self._mcp_url.replace("/sse", "/message").rstrip("/")
        if msg_url == self._mcp_url:
            # URL doesn't end in /sse — just append /message path segment
            from urllib.parse import urlparse, urlunparse
            parts = urlparse(self._mcp_url)
            msg_url = urlunparse(parts._replace(path=parts.path.rstrip("/") + "/../message"))
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._mcp_token:
            headers["Authorization"] = f"Bearer {self._mcp_token}"
        ping_payload = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}
        try:
            async with self._session.post(
                msg_url, json=ping_payload, timeout=_MCP_TIMEOUT, headers=headers
            ) as resp:
                _LOGGER.debug("MCP probe POST %s → HTTP %d", msg_url, resp.status)
                if resp.status in (200, 202):
                    return {"available": True, "error": None}
                return {"available": False, "error": f"HTTP {resp.status}"}
        except asyncio.TimeoutError:
            return {"available": False, "error": "timeout"}
        except ClientError as exc:
            return {"available": False, "error": str(exc)}
