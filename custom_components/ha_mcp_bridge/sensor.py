"""Sensor platform for ha_mcp_bridge."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HaMcpBridgeCoordinator


def _device(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="HA MCP Bridge",
        manufacturer="Community",
        model="HA MCP Bridge Add-on",
    )


class _Base(CoordinatorEntity[HaMcpBridgeCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: HaMcpBridgeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = _device(entry)


class BridgeStatusSensor(_Base):
    """Add-on health status (ok / unavailable)."""

    _attr_name = "Bridge Status"
    _attr_icon = "mdi:bridge"

    def __init__(self, coordinator: HaMcpBridgeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("status")


class McpAvailableSensor(_Base):
    """Whether the configured MCP server is reachable."""

    _attr_name = "MCP Server Available"
    _attr_icon = "mdi:server-network"

    def __init__(self, coordinator: HaMcpBridgeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_mcp_available"

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data or {}
        available = data.get("mcp_available")
        if available is None:
            return "not_configured"
        return "connected" if available else "unreachable"

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "mcp_url": self._entry.data.get("mcp_url", ""),
            "mcp_error": data.get("mcp_error"),
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HaMcpBridgeCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        BridgeStatusSensor(coordinator, entry),
        McpAvailableSensor(coordinator, entry),
    ], update_before_add=True)
