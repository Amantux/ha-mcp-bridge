"""GitHub Copilot conversation agent for Home Assistant.

Why this works
--------------
Home Assistant's conversation component lets integrations register a custom
"conversation agent" — an object that implements async_process().  When the
user sends a message via the HA conversation UI (voice assistant, chat widget,
or the developer tools Conversation panel), HA forwards it to every registered
agent in priority order.

This agent:
1. Maintains per-conversation history (capped at MAX_TURNS exchanges) so
   follow-up questions work naturally.
2. POSTs the history to the add-on's /chat endpoint (running at the same
   host:port as the health endpoint — just a different path).
3. The add-on calls the GitHub Copilot chat completions API and returns the
   reply as JSON.

Registration
------------
async_setup_entry in __init__.py calls conversation.async_set_agent() with
an instance of this class.  async_unload_entry calls async_unset_agent() so
HA removes it cleanly when the integration is unloaded.

The 'conversation' dependency in manifest.json ensures HA sets up the
conversation component before this integration loads.
"""
from __future__ import annotations

import asyncio
import logging

from aiohttp import ClientError, ClientTimeout
from homeassistant.components import conversation
from homeassistant.components.conversation import AbstractConversationAgent, ConversationInput, ConversationResult
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client, intent

from .const import CONF_HOST, CONF_PORT
from .helpers import build_health_url

_LOGGER = logging.getLogger(__name__)

# Copilot can be slow on the first token; give it a generous timeout.
_CHAT_TIMEOUT = ClientTimeout(total=60)

# Number of full turns (user + assistant pairs) kept per conversation.
MAX_TURNS = 10


class HaMcpBridgeConversationAgent(AbstractConversationAgent):
    """Proxies HA conversation messages to GitHub Copilot via the bridge add-on."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        # history[conversation_id] = list of {role, content} dicts
        self._history: dict[str, list[dict]] = {}

        # Derive the /chat URL from the same host:port as the health endpoint.
        health_url = build_health_url(
            str(entry.data[CONF_HOST]), int(entry.data[CONF_PORT])
        )
        self._chat_url = health_url.replace("/health", "/chat")

    # ------------------------------------------------------------------
    # AbstractConversationAgent contract
    # ------------------------------------------------------------------

    @property
    def attribution(self) -> dict[str, str]:
        return {
            "name": "GitHub Copilot via HA MCP Bridge",
            "url": "https://github.com/Amantux/ha-mcp-bridge",
        }

    @property
    def supported_languages(self) -> str:
        # "*" means this agent handles all languages.
        return conversation.MATCH_ALL

    async def async_process(self, user_input: ConversationInput) -> ConversationResult:
        """Receive a user message, call Copilot, return the reply.

        HA calls this method on the event loop; the aiohttp call is fully
        async so it does not block.
        """
        response = intent.IntentResponse(language=user_input.language)
        conv_id = user_input.conversation_id or "default"

        # Accumulate history so follow-up questions have context.
        history = self._history.setdefault(conv_id, [])
        history.append({"role": "user", "content": user_input.text})

        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            payload = {"prompt": user_input.text}
            async with session.post(
                self._chat_url, json=payload, timeout=_CHAT_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            if "error" in data:
                reply = f"Copilot error: {data['error']}"
            else:
                reply = data.get("response") or "(no response)"

            history.append({"role": "assistant", "content": reply})
            response.async_set_speech(reply)

        except asyncio.TimeoutError:
            _LOGGER.warning("Copilot /chat timed out for conversation %s", conv_id)
            response.async_set_speech(
                "GitHub Copilot didn't respond in time. Please try again."
            )
        except ClientError as exc:
            _LOGGER.warning("Cannot reach HA MCP Bridge at %s: %s", self._chat_url, exc)
            response.async_set_speech(
                "Could not reach the HA MCP Bridge add-on. "
                "Make sure it is running and you are signed in to GitHub."
            )

        return ConversationResult(
            response=response,
            conversation_id=conv_id,
        )

