"""GitHub Copilot conversation agent — platform entity for Home Assistant.

Why ConversationEntity instead of AbstractConversationAgent
------------------------------------------------------------
AbstractConversationAgent + async_set_agent() is the legacy registration path.
ConversationEntity is the modern platform-based approach (HA 2023.8+) and is
what appears in Settings -> Voice Assistants as a selectable AI agent.

How it is wired in
------------------
  __init__.py  PLATFORMS = ["sensor", "conversation"]
  HA calls conversation.async_setup_entry() on load, which calls
  async_add_entities([HaMcpBridgeCopilotEntity(entry)]).
  The entity appears under Settings -> Voice Assistants -> Add assistant.

How each message is handled
----------------------------
  1. User sends a message in HA conversation (voice, text, dev-tools).
  2. HA calls async_process(user_input).
  3. We POST {"prompt": text} to the add-on /chat endpoint.
  4. The add-on runs  gh copilot suggest -t shell <prompt>  as a subprocess.
  5. stdout is stripped of ANSI codes and returned as JSON {"output": "..."}.
  6. We wrap the text in IntentResponse and return a ConversationResult.
"""
from __future__ import annotations

import asyncio
import logging

from aiohttp import ClientError, ClientTimeout
from homeassistant.components.conversation import ConversationEntity, ConversationInput, ConversationResult
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client, intent
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_HOST, CONF_PORT, DOMAIN
from .helpers import build_health_url
from .sensor import _device_info

_LOGGER = logging.getLogger(__name__)

# gh copilot suggest can take a while — generous timeout.
_CHAT_TIMEOUT = ClientTimeout(total=90)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register the Copilot conversation entity for this config entry."""
    async_add_entities([HaMcpBridgeCopilotEntity(entry)])


class HaMcpBridgeCopilotEntity(ConversationEntity):
    """GitHub Copilot conversation agent backed by the HA MCP Bridge add-on.

    Appears in Settings -> Voice Assistants as "GitHub Copilot".
    """

    _attr_has_entity_name = True
    _attr_name = "GitHub Copilot"
    _attr_icon = "mdi:github"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_copilot"
        self._attr_device_info = _device_info(entry)

        # /chat lives at the same host:port as /health.
        health_url = build_health_url(
            str(entry.data[CONF_HOST]), int(entry.data[CONF_PORT])
        )
        self._chat_url = health_url.replace("/health", "/chat")

    @property
    def supported_languages(self) -> str:
        # "*" = accept all languages (MATCH_ALL).
        return "*"

    async def async_process(
        self, user_input: ConversationInput
    ) -> ConversationResult:
        """Forward the user message to the add-on and return Copilot's reply."""
        response = intent.IntentResponse(language=user_input.language)
        session = aiohttp_client.async_get_clientsession(self.hass)

        try:
            async with session.post(
                self._chat_url,
                json={"prompt": user_input.text},
                timeout=_CHAT_TIMEOUT,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            if "error" in data:
                reply = f"Copilot: {data['error']}"
            else:
                reply = data.get("output") or "(no response)"

        except asyncio.TimeoutError:
            _LOGGER.warning("Copilot /chat timed out")
            reply = "GitHub Copilot didn't respond in time. Please try again."
        except ClientError as exc:
            _LOGGER.warning("Cannot reach HA MCP Bridge at %s: %s", self._chat_url, exc)
            reply = (
                "Could not reach the HA MCP Bridge add-on. "
                "Make sure it is running and you have signed in to GitHub "
                "via the Copilot panel in the sidebar."
            )

        response.async_set_speech(reply)
        return ConversationResult(
            response=response,
            conversation_id=user_input.conversation_id,
        )
