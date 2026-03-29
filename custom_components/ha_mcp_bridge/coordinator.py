"""Coordinator for ha_mcp_bridge."""
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


class HaMcpBridgeCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the add-on /health endpoint and optionally probes the MCP server."""

    def __init__(self, hass: HomeAssistant, session: ClientSession, entry: ConfigEntry) -> None:
        self._session = session
        self._health_url = build_health_url(
            str(entry.data[CONF_HOST]), int(entry.data[CONF_PORT])
        )
        self._mcp_url: str = str(entry.data.get(CONF_MCP_URL, DEFAULT_MCP_URL)).strip()
        self._mcp_token: str = str(entry.data.get(CONF_MCP_TOKEN, DEFAULT_MCP_TOKEN)).strip()
        interval = int(entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        # 1. Check the add-on is alive.
        try:
            async with self._session.get(self._health_url, timeout=_TIMEOUT) as resp:
                resp.raise_for_status()
                health = await resp.json()
        except (ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Cannot reach add-on /health: {err}") from err

        data: dict[str, Any] = {
            "status": health.get("status", "ok"),
            "uptime": health.get("uptime"),
            "mcp_available": None,
            "mcp_error": "not_configured",
        }

        # 2. Probe the MCP server if one was configured during setup.
        if self._mcp_url:
            mcp_result = await self._probe_mcp()
            data["mcp_available"] = mcp_result["available"]
            data["mcp_error"] = mcp_result.get("error")

        return data

    async def _probe_mcp(self) -> dict[str, Any]:
        """Try to reach the configured MCP server URL.

        Any HTTP response (even 4xx) means the server is reachable.
        Connection-level errors mean it is not.
        """
        headers: dict[str, str] = {}
        if self._mcp_token:
            headers["Authorization"] = f"Bearer {self._mcp_token}"

        try:
            async with self._session.get(
                self._mcp_url, timeout=_MCP_TIMEOUT, headers=headers
            ) as resp:
                # Any HTTP status means the server responded (it's up).
                _LOGGER.debug("MCP probe %s → HTTP %d", self._mcp_url, resp.status)
                return {"available": True, "error": None}
        except asyncio.TimeoutError:
            return {"available": False, "error": "timeout"}
        except ClientError as exc:
            return {"available": False, "error": str(exc)}
