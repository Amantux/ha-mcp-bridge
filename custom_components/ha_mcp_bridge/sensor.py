"""Sensor platform for ha_mcp_bridge."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
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
        configuration_url=f"http://127.0.0.1:{entry.data.get('port', 8099)}/status",
    )


class _Base(CoordinatorEntity[HaMcpBridgeCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: HaMcpBridgeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = _device(entry)


class BridgeStatusSensor(_Base):
    """Overall add-on status (ok / error string)."""

    _attr_name = "Bridge Status"
    _attr_icon = "mdi:bridge"

    def __init__(self, coordinator: HaMcpBridgeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("status")


class McpToolCountSensor(_Base):
    """Number of MCP tools exposed by HA's built-in MCP Server."""

    _attr_name = "MCP Tool Count"
    _attr_icon = "mdi:wrench-clock"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "tools"

    def __init__(self, coordinator: HaMcpBridgeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_mcp_tool_count"

    @property
    def native_value(self) -> int | None:
        tools = (self.coordinator.data or {}).get("mcp_tools")
        return len(tools) if tools is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "mcp_available": data.get("mcp_available"),
            "tools": data.get("mcp_tools", []),
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
        McpToolCountSensor(coordinator, entry),
    ], update_before_add=True)
