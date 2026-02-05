"""
Climate Platform for Tasmota MiElHVAC Integration.

Creates climate entities for Mitsubishi Electric HVAC devices controlled by
Tasmota's MiElHVAC driver. Entities are automatically linked to existing
Tasmota devices via MAC address.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.mqtt import subscription
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    ACTION_MAP,
    DEFAULT_MODEL,
    DOMAIN,
    FAN_MODES,
    HVAC_MODE_MAP,
    HVAC_MODE_REVERSE_MAP,
    MAX_TEMP,
    MIN_TEMP,
    PRECISION,
    SWING_V_MODES,
    TEMP_STEP,
)

_LOGGER = logging.getLogger(__name__)

SIGNAL_HVAC_DISCOVERED = f"{DOMAIN}_hvac_discovered"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tasmota MiElHVAC climate entities."""

    created_entities: dict[str, MiElHVACTasmota] = {}

    @callback
    def async_discover_hvac(
        device_id: str, mac: str | None = None, device_name: str | None = None
    ) -> None:
        """Handle discovery of a new HVAC device or metadata update."""
        entity = created_entities.get(device_id)

        if entity:
            # Update existing entity with new metadata
            if mac and entity._mac_address != mac:
                entity._set_mac_address(mac)
            if device_name and entity._device_name != device_name:
                entity._set_device_name(device_name)
            return

        # Create new entity
        _LOGGER.info(
            "Creating climate entity for %s%s",
            device_id,
            f" ({device_name})" if device_name else "",
        )

        entity = MiElHVACTasmota(hass, device_id, mac, device_name)
        created_entities[device_id] = entity
        async_add_entities([entity])

    # Listen for discovery signals
    config_entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_HVAC_DISCOVERED, async_discover_hvac)
    )


class MiElHVACTasmota(ClimateEntity, RestoreEntity):
    """Climate entity for Mitsubishi Electric HVAC via Tasmota MiElHVAC driver."""

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        mac: str | None = None,
        device_name: str | None = None,
    ) -> None:
        """Initialize the climate entity."""
        self.hass = hass
        self._device_id = device_id
        self._base_topic = device_id
        self._model = DEFAULT_MODEL
        self._mac_address = mac
        self._device_name = device_name

        # MQTT topics
        self._topic_avail = f"tele/{self._base_topic}/LWT"
        self._topic_sensor = f"tele/{self._base_topic}/SENSOR"
        self._topic_state = f"tele/{self._base_topic}/HVACSETTINGS"
        self._topic_status1 = f"stat/{self._base_topic}/STATUS1"
        self._topic_cmd_mode = f"cmnd/{self._base_topic}/HVACSetHAMode"
        self._topic_cmd_temp = f"cmnd/{self._base_topic}/HVACSetTemp"
        self._topic_cmd_swing_v = f"cmnd/{self._base_topic}/HVACSetSwingV"
        self._topic_cmd_swing_h = f"cmnd/{self._base_topic}/HVACSetSwingH"
        self._topic_cmd_fan = f"cmnd/{self._base_topic}/HVACSetFanSpeed"

        # Entity attributes
        self._attr_unique_id = f"{self._device_id}_mielhvac_climate"
        self._attr_name = "HVAC"
        self._attr_has_entity_name = True

        # Device info (linked to Tasmota device via MAC)
        self._attr_device_info = (
            {"connections": {("mac", mac.replace(":", "").upper())}}
            if mac
            else None
        )

        # Temperature configuration
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_min_temp = MIN_TEMP
        self._attr_max_temp = MAX_TEMP
        self._attr_target_temperature_step = TEMP_STEP
        self._attr_precision = PRECISION

        # Current state
        self._attr_current_temperature = None
        self._attr_target_temperature = None
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_hvac_action = HVACAction.OFF
        self._attr_fan_mode = "auto"
        self._attr_swing_mode = "auto"
        self._swing_h_mode = "auto"
        self._available = False

        # Supported features
        self._attr_hvac_modes = list(HVAC_MODE_MAP.values())
        self._attr_fan_modes = FAN_MODES
        self._attr_swing_modes = SWING_V_MODES
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.SWING_MODE
        )

        # MQTT subscription state
        self._sub_state = None

        # Request device info if MAC not provided (fallback)
        if not self._mac_address:
            hass.async_create_task(self._request_device_info())

    def _set_mac_address(self, mac: str) -> None:
        """Update MAC address and device info."""
        if self._mac_address == mac:
            return

        self._mac_address = mac
        self._attr_device_info = {
            "connections": {("mac", mac.replace(":", "").upper())}
        }

        _LOGGER.info("Updated MAC address for %s: %s", self._device_id, mac)
        self.async_write_ha_state()

    def _set_device_name(self, device_name: str) -> None:
        """Update device name."""
        if self._device_name == device_name:
            return

        self._device_name = device_name
        _LOGGER.info("Updated device name for %s: %s", self._device_id, device_name)
        self.async_write_ha_state()

    async def _request_device_info(self) -> None:
        """Request device info via Status command (fallback for MAC retrieval)."""
        try:
            await mqtt.async_publish(
                self.hass,
                f"cmnd/{self._base_topic}/Status",
                "1",
                qos=1,
                retain=False,
            )
        except Exception as err:
            _LOGGER.warning(
                "Failed to request device info for %s: %s", self._device_id, err
            )

    async def async_added_to_hass(self) -> None:
        """Subscribe to MQTT topics and restore previous state."""
        # Restore previous state
        if last_state := await self.async_get_last_state():
            if last_state.state in HVAC_MODE_MAP.values():
                self._attr_hvac_mode = HVACMode(last_state.state)

            if temp := last_state.attributes.get(ATTR_TEMPERATURE):
                self._attr_target_temperature = float(temp)

            if fan_mode := last_state.attributes.get("fan_mode"):
                self._attr_fan_mode = fan_mode

            if swing_mode := last_state.attributes.get("swing_mode"):
                self._attr_swing_mode = swing_mode

            if swing_h := last_state.attributes.get("swing_horizontal"):
                self._swing_h_mode = swing_h

        await self._subscribe_topics()

    async def _subscribe_topics(self) -> None:
        """Subscribe to MQTT topics."""

        @callback
        def availability_received(msg: ReceiveMessage) -> None:
            """Handle LWT availability messages."""
            self._available = msg.payload == "Online"
            self.async_write_ha_state()

        @callback
        def current_temp_received(msg: ReceiveMessage) -> None:
            """Handle current temperature from SENSOR messages."""
            try:
                data = json.loads(msg.payload)
                if temp := data.get(self._model, {}).get("Temperature"):
                    self._attr_current_temperature = float(temp)
                    self.async_write_ha_state()
            except (json.JSONDecodeError, ValueError, KeyError):
                pass  # Silently ignore malformed temperature data

        @callback
        def state_received(msg: ReceiveMessage) -> None:
            """Handle HVAC state updates from HVACSETTINGS messages."""
            try:
                data = json.loads(msg.payload)
                updated = False

                if "Temp" in data:
                    self._attr_target_temperature = float(data["Temp"])
                    updated = True

                if "HAMode" in data:
                    ha_mode = data["HAMode"]
                    self._attr_hvac_mode = HVAC_MODE_MAP.get(ha_mode, HVACMode.OFF)
                    self._attr_hvac_action = ACTION_MAP.get(ha_mode, HVACAction.OFF)
                    updated = True

                if "FanSpeed" in data:
                    self._attr_fan_mode = data["FanSpeed"]
                    updated = True

                if "SwingV" in data:
                    self._attr_swing_mode = data["SwingV"]
                    updated = True

                if "SwingH" in data:
                    self._swing_h_mode = data["SwingH"]
                    updated = True

                if updated:
                    self.async_write_ha_state()
            except (json.JSONDecodeError, ValueError, KeyError):
                pass  # Silently ignore malformed state data

        @callback
        def info_received(msg: ReceiveMessage) -> None:
            """Handle STATUS1 messages to extract MAC address (fallback)."""
            try:
                data = json.loads(msg.payload)

                # Try to extract MAC from StatusNET
                mac = data.get("StatusNET", {}).get("Mac") or data.get("Mac")

                if mac and not self._mac_address:
                    self._mac_address = mac
                    self._attr_device_info = {
                        "connections": {("mac", mac.replace(":", "").upper())}
                    }

                    _LOGGER.info(
                        "Retrieved MAC address for %s via Status command: %s",
                        self._device_id,
                        mac,
                    )

                    self.async_write_ha_state()
            except (json.JSONDecodeError, ValueError, KeyError):
                pass  # Silently ignore malformed status data

        # Prepare subscriptions
        self._sub_state = subscription.async_prepare_subscribe_topics(
            self.hass,
            self._sub_state,
            {
                "availability": {
                    "topic": self._topic_avail,
                    "msg_callback": availability_received,
                    "qos": 1,
                },
                "sensor": {
                    "topic": self._topic_sensor,
                    "msg_callback": current_temp_received,
                    "qos": 1,
                },
                "state": {
                    "topic": self._topic_state,
                    "msg_callback": state_received,
                    "qos": 1,
                },
                "info": {
                    "topic": self._topic_status1,
                    "msg_callback": info_received,
                    "qos": 1,
                },
            },
        )

        await subscription.async_subscribe_topics(self.hass, self._sub_state)

    async def async_will_remove_from_hass(self) -> None:
        """Clean up MQTT subscriptions."""
        # Unsubscribe from MQTT
        self._sub_state = subscription.async_unsubscribe_topics(
            self.hass, self._sub_state
        )

    @property
    def available(self) -> bool:
        """Return entity availability based on LWT."""
        return self._available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        return {"swing_horizontal": self._swing_h_mode}

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature."""
        if temperature := kwargs.get(ATTR_TEMPERATURE):
            await mqtt.async_publish(
                self.hass,
                self._topic_cmd_temp,
                str(int(temperature)),
                qos=1,
                retain=False,
            )
            self._attr_target_temperature = temperature
            self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC operating mode."""
        if tasmota_mode := HVAC_MODE_REVERSE_MAP.get(hvac_mode):
            await mqtt.async_publish(
                self.hass,
                self._topic_cmd_mode,
                tasmota_mode,
                qos=1,
                retain=False,
            )
            self._attr_hvac_mode = hvac_mode
            self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set fan speed mode."""
        if fan_mode in self._attr_fan_modes:
            await mqtt.async_publish(
                self.hass,
                self._topic_cmd_fan,
                fan_mode,
                qos=1,
                retain=False,
            )
            self._attr_fan_mode = fan_mode
            self.async_write_ha_state()

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Set vertical swing mode."""
        if swing_mode in self._attr_swing_modes:
            await mqtt.async_publish(
                self.hass,
                self._topic_cmd_swing_v,
                swing_mode,
                qos=1,
                retain=False,
            )
            self._attr_swing_mode = swing_mode
            self.async_write_ha_state()
