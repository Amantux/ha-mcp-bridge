"""Config flow for ha_mcp_bridge.

Discovery path (Supervisor — zero user typing):
    async_step_hassio         validate add-on /health; abort if not ready
        ↓
    async_step_hassio_confirm  _set_confirm_only() → "New device found" badge
        ↓ (user clicks Submit)
    async_step_mcp_setup      user configures MCP server (optional external URL+token)
        ↓
    async_create_entry

Manual path:
    async_step_user  →  validate  →  async_step_mcp_setup  →  async_create_entry
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
    CONF_MCP_TOKEN,
    CONF_MCP_URL,
    CONF_PORT,
    CONF_UPDATE_INTERVAL,
    DEFAULT_HOST,
    DEFAULT_MCP_TOKEN,
    DEFAULT_MCP_URL,
    DEFAULT_PORT,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)
from .helpers import build_health_url

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = ClientTimeout(total=10)


class HaMcpBridgeOptionsFlow(config_entries.OptionsFlow):
    """Lets users change the poll interval and MCP config after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        opts = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_UPDATE_INTERVAL,
                    default=int(opts.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)),
                ): vol.All(int, vol.Range(min=10, max=3600)),
                vol.Optional(
                    CONF_MCP_URL,
                    default=str(opts.get(CONF_MCP_URL, DEFAULT_MCP_URL)),
                ): str,
                vol.Optional(
                    CONF_MCP_TOKEN,
                    default=str(opts.get(CONF_MCP_TOKEN, DEFAULT_MCP_TOKEN)),
                ): str,
            }),
        )


class HaMcpBridgeFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for HA MCP Bridge.

    Two-step Supervisor discovery + optional MCP server configuration.
    """

    VERSION = 1

    # Accumulated data passed forward between steps.
    _hassio_discovery: HassioServiceInfo | None = None
    _entry_data: dict[str, Any] = {}
    _entry_title: str = "HA MCP Bridge"

    @staticmethod
    def async_get_options_flow(entry: config_entries.ConfigEntry) -> HaMcpBridgeOptionsFlow:
        return HaMcpBridgeOptionsFlow()

    # ------------------------------------------------------------------
    # Supervisor discovery — two-step (HA quality-scale pattern)
    # https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/discovery/
    # ------------------------------------------------------------------

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> FlowResult:
        """Step 1: Supervisor fires this when the add-on calls POST /discovery.

        Validate connection here — abort before UI if the add-on isn't ready.
        Pass `updates` so a port change on restart silently updates the entry.
        """
        port = int(discovery_info.config.get(CONF_PORT, DEFAULT_PORT))

        await self.async_set_unique_id(discovery_info.uuid)
        self._abort_if_unique_id_configured(updates={CONF_PORT: port})

        try:
            await self._async_validate_addon(
                self.hass, {CONF_HOST: DEFAULT_HOST, CONF_PORT: port}
            )
        except (ClientError, asyncio.TimeoutError) as exc:
            _LOGGER.debug("Add-on not reachable during discovery: %s", exc)
            return self.async_abort(reason="cannot_connect")

        self._hassio_discovery = discovery_info
        self._entry_data = {CONF_HOST: DEFAULT_HOST, CONF_PORT: port}
        self._entry_title = discovery_info.name
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: 'New device found' confirmation card.

        _set_confirm_only() is what surfaces the notification badge in
        Settings → Devices & Services. Without it the flow runs silently.
        """
        assert self._hassio_discovery is not None

        if user_input is not None:
            # Confirmation received — proceed to MCP setup.
            return await self.async_step_mcp_setup()

        self._set_confirm_only()
        return self.async_show_form(
            step_id="hassio_confirm",
            description_placeholders={"addon": self._hassio_discovery.name},
        )

    # ------------------------------------------------------------------
    # MCP server configuration (both paths converge here)
    # ------------------------------------------------------------------

    async def async_step_mcp_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3: Optional MCP server connection.

        Leave mcp_url blank to skip MCP server connection (add-on runs as a
        health-only service).  Provide a URL + optional token to enable the
        integration to probe and expose the MCP server's availability as a
        sensor.

        Examples:
          - HA built-in MCP:   http://homeassistant.local:8123/api/mcp_server/sse
          - External server:   https://my-llm-proxy.example.com/mcp/sse
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            mcp_url = user_input.get(CONF_MCP_URL, "").strip().rstrip("/")
            mcp_token = user_input.get(CONF_MCP_TOKEN, "").strip()

            if mcp_url:
                # Validate that the URL is reachable before creating the entry.
                try:
                    await self._async_probe_mcp(self.hass, mcp_url, mcp_token)
                except (ClientError, asyncio.TimeoutError) as exc:
                    _LOGGER.warning("Cannot reach MCP server %s: %s", mcp_url, exc)
                    errors["base"] = "cannot_connect_mcp"

            if not errors:
                data = dict(self._entry_data)
                data[CONF_MCP_URL] = mcp_url
                data[CONF_MCP_TOKEN] = mcp_token
                return self.async_create_entry(title=self._entry_title, data=data)

        return self.async_show_form(
            step_id="mcp_setup",
            data_schema=vol.Schema({
                vol.Optional(CONF_MCP_URL, default=DEFAULT_MCP_URL): str,
                vol.Optional(CONF_MCP_TOKEN, default=DEFAULT_MCP_TOKEN): str,
            }),
            errors=errors,
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
                await self._async_validate_addon(self.hass, data)
            except (ClientError, asyncio.TimeoutError) as exc:
                _LOGGER.warning("Cannot connect to add-on: %s", exc)
                errors["base"] = "cannot_connect"
            else:
                self._entry_data = data
                self._entry_title = "HA MCP Bridge"
                return await self.async_step_mcp_setup()

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

    async def _async_validate_addon(
        self, hass: HomeAssistant, data: dict[str, Any]
    ) -> None:
        """GET /health on the add-on; raises on failure."""
        session = aiohttp_client.async_get_clientsession(hass)
        url = build_health_url(str(data[CONF_HOST]), int(data[CONF_PORT]))
        async with session.get(url, timeout=_TIMEOUT) as resp:
            resp.raise_for_status()

    async def _async_probe_mcp(
        self, hass: HomeAssistant, mcp_url: str, mcp_token: str
    ) -> None:
        """Try to reach the MCP server URL; raises ClientError/TimeoutError on failure."""
        session = aiohttp_client.async_get_clientsession(hass)
        headers = {}
        if mcp_token:
            headers["Authorization"] = f"Bearer {mcp_token}"
        # A GET to an SSE endpoint may return 200, 405, or similar — any HTTP
        # response (even an error code) means the server is reachable.
        # Connection-level errors (DNS, refused, timeout) raise exceptions.
        async with session.get(mcp_url, timeout=_TIMEOUT, headers=headers) as _:
            pass
