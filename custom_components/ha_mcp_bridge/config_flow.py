"""Config flow for ha_mcp_bridge.

Discovery path (Supervisor — zero user typing):
    async_step_hassio         validate add-on; abort if not ready
        ↓
    async_step_hassio_confirm  _set_confirm_only() → "New device found" badge
        ↓ (user clicks Submit)
    async_create_entry

Manual path:
    async_step_user  →  validate  →  async_create_entry
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from aiohttp import ClientError, ClientTimeout
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .const import (
    CONF_HOST,
    CONF_PORT,
    CONF_UPDATE_INTERVAL,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)
from .helpers import build_health_url

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = ClientTimeout(total=10)


class HaMcpBridgeOptionsFlow(config_entries.OptionsFlow):
    """Lets users change the poll interval after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_UPDATE_INTERVAL,
                    default=int(self.config_entry.options.get(
                        CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
                    )),
                ): vol.All(int, vol.Range(min=10, max=3600)),
            }),
        )


class HaMcpBridgeFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for HA MCP Bridge."""

    VERSION = 1

    # Stored in async_step_hassio; read in async_step_hassio_confirm.
    _hassio_discovery: HassioServiceInfo | None = None

    @staticmethod
    def async_get_options_flow(entry: config_entries.ConfigEntry) -> HaMcpBridgeOptionsFlow:
        return HaMcpBridgeOptionsFlow()

    # ------------------------------------------------------------------
    # Supervisor discovery — two-step (Mealie / HA quality-scale pattern)
    # https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/discovery/
    # ------------------------------------------------------------------

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> FlowResult:
        """Step 1: Supervisor fires this when the add-on calls POST /discovery.

        Key points:
        - SOURCE_HASSIO maps to async_step_hassio (not async_step_discovery).
        - Validate the connection NOW so we abort before showing any UI if the
          add-on isn't ready.
        - Pass `updates` to _abort_if_unique_id_configured so a port change on
          restart silently updates the existing entry instead of aborting.
        """
        port = int(discovery_info.config.get(CONF_PORT, DEFAULT_PORT))

        await self.async_set_unique_id(discovery_info.uuid)
        self._abort_if_unique_id_configured(updates={CONF_PORT: port})

        try:
            await self._async_validate(self.hass, {CONF_HOST: DEFAULT_HOST, CONF_PORT: port})
        except (ClientError, asyncio.TimeoutError) as exc:
            _LOGGER.debug("Add-on not reachable during discovery: %s", exc)
            return self.async_abort(reason="cannot_connect")

        self._hassio_discovery = discovery_info
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: Shows the 'New device found' confirmation card.

        _set_confirm_only() is the call that surfaces the notification badge in
        Settings → Devices & Services. Without it the flow runs silently.
        Connection was already validated in step 1 — just create the entry.
        """
        assert self._hassio_discovery is not None

        if user_input is not None:
            port = int(self._hassio_discovery.config.get(CONF_PORT, DEFAULT_PORT))
            return self.async_create_entry(
                title=self._hassio_discovery.name,
                data={CONF_HOST: DEFAULT_HOST, CONF_PORT: port},
            )

        self._set_confirm_only()
        return self.async_show_form(
            step_id="hassio_confirm",
            description_placeholders={"addon": self._hassio_discovery.name},
        )

    # ------------------------------------------------------------------
    # Manual setup
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manual setup via Settings → Devices & Services → Add integration."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                CONF_HOST: user_input[CONF_HOST].strip().rstrip("/"),
                CONF_PORT: int(user_input[CONF_PORT]),
            }
            try:
                await self._async_validate(self.hass, data)
            except (ClientError, asyncio.TimeoutError) as exc:
                _LOGGER.warning("Cannot connect to add-on: %s", exc)
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title="HA MCP Bridge", data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
            }),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _async_validate(self, hass: HomeAssistant, data: dict[str, Any]) -> None:
        """Hit /health; raises ClientError or TimeoutError on failure."""
        session = aiohttp_client.async_get_clientsession(hass)
        url = build_health_url(str(data[CONF_HOST]), int(data[CONF_PORT]))
        async with session.get(url, timeout=_TIMEOUT) as resp:
            resp.raise_for_status()
