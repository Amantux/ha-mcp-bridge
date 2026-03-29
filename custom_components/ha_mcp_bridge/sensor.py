from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HaMcpBridgeDataUpdateCoordinator


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="HA MCP Bridge",
        manufacturer="Community",
        model="HA MCP Bridge Add-on",
        configuration_url=f"http://127.0.0.1:{entry.data.get('port', 8099)}/health",
    )


class HaMcpBridgeStatusSensor(CoordinatorEntity[HaMcpBridgeDataUpdateCoordinator], SensorEntity):
    """Reports the plain-text status returned by the add-on health endpoint."""

    _attr_icon = "mdi:heart-pulse"
    _attr_has_entity_name = True
    _attr_name = "Status"

    def __init__(
        self,
        coordinator: HaMcpBridgeDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("status")

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        data = self.coordinator.data or {}
        return {
            "path": data.get("path"),
            "timestamp": data.get("timestamp"),
        }


class HaMcpBridgeUptimeSensor(CoordinatorEntity[HaMcpBridgeDataUpdateCoordinator], SensorEntity):
    """Reports how long the add-on process has been running (seconds)."""

    _attr_icon = "mdi:timer-outline"
    _attr_has_entity_name = True
    _attr_name = "Uptime"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        coordinator: HaMcpBridgeDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_uptime"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> float | None:
        value = (self.coordinator.data or {}).get("uptime")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None


class HaMcpBridgeMcpSensor(CoordinatorEntity[HaMcpBridgeDataUpdateCoordinator], SensorEntity):
    """Reports whether the configured MCP server is reachable."""

    _attr_icon = "mdi:server-network"
    _attr_has_entity_name = True
    _attr_name = "MCP Server"

    def __init__(
        self,
        coordinator: HaMcpBridgeDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_mcp"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data or {}
        available = data.get("mcp_available")
        if available is None:
            return "not_configured"
        return "connected" if available else "unreachable"

    @property
    def extra_state_attributes(self) -> dict[str, object]:
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
    coordinator: HaMcpBridgeDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            HaMcpBridgeStatusSensor(coordinator, entry),
            HaMcpBridgeUptimeSensor(coordinator, entry),
            HaMcpBridgeMcpSensor(coordinator, entry),
        ],
        update_before_add=True,
    )
