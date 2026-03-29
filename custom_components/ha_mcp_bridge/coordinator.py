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

from .const import CONF_HOST, CONF_PORT, CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL, DOMAIN
from .helpers import build_status_url

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = ClientTimeout(total=10)


class HaMcpBridgeCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the add-on /status endpoint."""

    def __init__(self, hass: HomeAssistant, session: ClientSession, entry: ConfigEntry) -> None:
        self._session = session
        self._url = build_status_url(entry.data[CONF_HOST], int(entry.data[CONF_PORT]))
        interval = int(entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            async with self._session.get(self._url, timeout=_TIMEOUT) as resp:
                resp.raise_for_status()
                return await resp.json()
        except (ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Error fetching bridge status: {err}") from err
