"""
Climate Platform for Tasmota MiElHVAC Integration.

Creates climate entities for Mitsubishi Electric HVAC devices controlled by
Tasmota's MiElHVAC driver. Entities are automatically linked to existing
Tasmota devices via MAC address.

Compatibility:
  - Legacy driver (pre-PR#24486): HVACSETTINGS uses "Temp", SENSOR uses "Power"
  - New driver (post-PR#24490):   HVACSETTINGS uses "SetTemperature",
                                  SENSOR uses "PowerState"
  - New driver (post-PR#24517):   SENSOR also contains "RemoteTemperature"
  - New driver (post-PR#24660):   Capabilities, Purifier, NightMode, EconoCool,
                                  AirDirection, ENERGY{} sub-object
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
    """Climate entity for Mitsubishi Electric HVAC via Tasmota MiElHVAC driver.

    Supports both the legacy driver payload format and the new format introduced
    by PRs #24486–#24660.
    """

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
        # New in PR#24517 / #24496 (renamed from HVACRemoteTemp)
        self._topic_cmd_remote_temp = f"cmnd/{self._base_topic}/HVACSetRemoteTemp"

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

        # Temperature configuration (may be overridden by capabilities, PR#24660)
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
        self._last_on_mode = HVACMode.AUTO  # Remember last non-OFF mode for turn_on

        # New in PR#24517: remote temperature sent by HA to device
        self._remote_temperature: float | None = None

        # New in PR#24660: run-state features (None = not supported / not yet known)
        self._purifier: str | None = None
        self._night_mode: str | None = None
        self._econo_cool: str | None = None
        self._air_direction: str | None = None

        # New in PR#24660: capability flags from driver
        self._capabilities: dict[str, Any] = {}

        # New in PR#24660: energy data
        self._power_usage: float | None = None   # Watts
        self._energy_total: float | None = None  # kWh

        # Outdoor temperature (reported in SENSOR.MiElHVAC)
        self._outdoor_temperature: float | None = None

        # Supported features
        self._attr_hvac_modes = list(HVAC_MODE_MAP.values())
        self._attr_fan_modes = FAN_MODES
        self._attr_swing_modes = SWING_V_MODES
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.SWING_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
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
                restored_mode = HVACMode(last_state.state)
                self._attr_hvac_mode = restored_mode
                if restored_mode != HVACMode.OFF:
                    self._last_on_mode = restored_mode

            if temp := last_state.attributes.get(ATTR_TEMPERATURE):
                self._attr_target_temperature = float(temp)

            if fan_mode := last_state.attributes.get("fan_mode"):
                self._attr_fan_mode = fan_mode

            if swing_mode := last_state.attributes.get("swing_mode"):
                self._attr_swing_mode = swing_mode

            if swing_h := last_state.attributes.get("swing_horizontal"):
                self._swing_h_mode = swing_h

            # Restore new fields if available
            if purifier := last_state.attributes.get("purifier"):
                self._purifier = purifier
            if night_mode := last_state.attributes.get("night_mode"):
                self._night_mode = night_mode
            if econo_cool := last_state.attributes.get("econo_cool"):
                self._econo_cool = econo_cool

        await self._subscribe_topics()

    async def _subscribe_topics(self) -> None:
        """Subscribe to MQTT topics."""

        @callback
        def availability_received(msg: ReceiveMessage) -> None:
            """Handle LWT availability messages."""
            self._available = msg.payload == "Online"
            self.async_write_ha_state()

        @callback
        def sensor_received(msg: ReceiveMessage) -> None:
            """Handle SENSOR messages (current temp, outdoor temp, energy, remote temp).

            Supports both legacy and new driver payload formats:
            - Legacy: {"MiElHVAC": {"Temperature": 22.0, "Power": "ON", ...}}
            - New:    {"MiElHVAC": {"RoomTemperature": 22.0, "PowerState": "ON",
                                     "OutdoorTemperature": 15.0,
                                     "RemoteTemperature": 21.5, ...},
                       "ENERGY": {"Power": 850, "Total": 123.4}}
            """
            try:
                data = json.loads(msg.payload)
                hvac_data = data.get(self._model, {})
                if not hvac_data:
                    return

                updated = False

                # --- Current (room) temperature ---
                # New driver (post-#24490): "RoomTemperature" as float
                # Legacy driver: "Temperature" as float
                room_temp = hvac_data.get("RoomTemperature") or hvac_data.get("Temperature")
                if room_temp is not None:
                    try:
                        self._attr_current_temperature = float(room_temp)
                        updated = True
                    except (ValueError, TypeError):
                        pass

                # --- Outdoor temperature (new, PR#24660) ---
                if outdoor := hvac_data.get("OutdoorTemperature"):
                    try:
                        self._outdoor_temperature = float(outdoor)
                        updated = True
                    except (ValueError, TypeError):
                        pass

                # --- Remote temperature (new, PR#24517) ---
                if remote := hvac_data.get("RemoteTemperature"):
                    try:
                        self._remote_temperature = float(remote)
                        updated = True
                    except (ValueError, TypeError):
                        pass

                # --- Capabilities (new, PR#24660) ---
                cap_fields = [
                    "ModeHeatSupported", "ModeDrySupported", "ModeFanSupported",
                    "VaneVSupported", "SwingSupported", "FanAutoSupported",
                    "OutdoorTemperatureSupported", "AirDirectionSupported",
                    "PurifierSupported", "NightModeSupported", "EconoCoolSupported",
                    "SetTemperatureCoolMinMax", "SetTemperatureHeatMinMax",
                    "SetTemperatureAutoMinMax", "CapabilitiesHex", "OptionsHex",
                ]
                for cap_field in cap_fields:
                    if cap_field in hvac_data:
                        self._capabilities[cap_field] = hvac_data[cap_field]
                        updated = True

                # --- Run-state features (new, PR#24660) ---
                if "Purifier" in hvac_data:
                    self._purifier = hvac_data["Purifier"]
                    updated = True
                if "NightMode" in hvac_data:
                    self._night_mode = hvac_data["NightMode"]
                    updated = True
                if "EconoCool" in hvac_data:
                    self._econo_cool = hvac_data["EconoCool"]
                    updated = True
                if "AirDirection" in hvac_data:
                    self._air_direction = hvac_data["AirDirection"]
                    updated = True

                # --- Energy: from ENERGY{} sub-object (new, PR#24660) ---
                # Tasmota publishes standard {"ENERGY": {"Power": W, "Total": kWh}}
                if energy_data := data.get("ENERGY"):
                    if (power := energy_data.get("Power")) is not None:
                        try:
                            self._power_usage = float(power)
                            updated = True
                        except (ValueError, TypeError):
                            pass
                    if (total := energy_data.get("Total")) is not None:
                        try:
                            self._energy_total = float(total)
                            updated = True
                        except (ValueError, TypeError):
                            pass
                # Also accept from inside MiElHVAC (intermediate driver versions)
                elif (power := hvac_data.get("Power")) is not None:
                    # Distinguish energy Power (numeric) vs legacy PowerState (string)
                    if isinstance(power, (int, float)):
                        try:
                            self._power_usage = float(power)
                            updated = True
                        except (ValueError, TypeError):
                            pass
                if (energy := hvac_data.get("Energy")) is not None:
                    if isinstance(energy, (int, float)):
                        try:
                            self._energy_total = float(energy)
                            updated = True
                        except (ValueError, TypeError):
                            pass

                if updated:
                    self.async_write_ha_state()

            except (json.JSONDecodeError, ValueError, KeyError):
                pass

        @callback
        def state_received(msg: ReceiveMessage) -> None:
            """Handle HVACSETTINGS state updates.

            Supports both legacy and new driver payload formats:
            - Legacy: {"Temp": 22.0, "HAMode": "cool", ...}
            - New:    {"SetTemperature": 22.0, "HAMode": "cool", ...}
              (PR#24490 renamed "Temp" → "SetTemperature")
            """
            try:
                data = json.loads(msg.payload)
                updated = False

                # --- Target temperature ---
                # New driver (post-#24490): "SetTemperature" as float
                # Legacy driver: "Temp" as float
                set_temp = data.get("SetTemperature") or data.get("Temp")
                if set_temp is not None:
                    try:
                        self._attr_target_temperature = float(set_temp)
                        updated = True
                    except (ValueError, TypeError):
                        pass

                # --- HVAC mode ---
                if "HAMode" in data:
                    ha_mode = data["HAMode"]
                    new_mode = HVAC_MODE_MAP.get(ha_mode, HVACMode.OFF)
                    if new_mode != HVACMode.OFF:
                        self._last_on_mode = new_mode
                    self._attr_hvac_mode = new_mode
                    self._attr_hvac_action = ACTION_MAP.get(ha_mode, HVACAction.OFF)
                    updated = True

                # --- Fan speed ---
                if "FanSpeed" in data:
                    self._attr_fan_mode = data["FanSpeed"]
                    updated = True

                # --- Vertical swing ---
                if "SwingV" in data:
                    self._attr_swing_mode = data["SwingV"]
                    updated = True

                # --- Horizontal swing ---
                if "SwingH" in data:
                    self._swing_h_mode = data["SwingH"]
                    updated = True

                # --- Run-state features (new, PR#24660) ---
                if "Purifier" in data:
                    self._purifier = data["Purifier"]
                    updated = True
                if "NightMode" in data:
                    self._night_mode = data["NightMode"]
                    updated = True
                if "EconoCool" in data:
                    self._econo_cool = data["EconoCool"]
                    updated = True
                if "AirDirection" in data:
                    self._air_direction = data["AirDirection"]
                    updated = True

                if updated:
                    self.async_write_ha_state()

            except (json.JSONDecodeError, ValueError, KeyError):
                pass

        @callback
        def info_received(msg: ReceiveMessage) -> None:
            """Handle STATUS1 messages to extract MAC address (fallback)."""
            try:
                data = json.loads(msg.payload)
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
                pass

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
                    "msg_callback": sensor_received,
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
        attrs: dict[str, Any] = {
            "swing_horizontal": self._swing_h_mode,
        }

        # Outdoor temperature
        if self._outdoor_temperature is not None:
            attrs["outdoor_temperature"] = self._outdoor_temperature

        # Remote temperature (set by HA to device, PR#24517)
        if self._remote_temperature is not None:
            attrs["remote_temperature"] = self._remote_temperature

        # Energy data (PR#24660)
        if self._power_usage is not None:
            attrs["power_usage_w"] = self._power_usage
        if self._energy_total is not None:
            attrs["energy_total_kwh"] = self._energy_total

        # Run-state features (PR#24660) — only expose if driver reported them
        if self._purifier is not None:
            attrs["purifier"] = self._purifier
        if self._night_mode is not None:
            attrs["night_mode"] = self._night_mode
        if self._econo_cool is not None:
            attrs["econo_cool"] = self._econo_cool
        if self._air_direction is not None:
            attrs["air_direction"] = self._air_direction

        # Capability flags (PR#24660) — only expose if received from driver
        if self._capabilities:
            attrs["capabilities"] = self._capabilities

        return attrs

    # -------------------------------------------------------------------------
    # Standard climate controls
    # -------------------------------------------------------------------------

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
            if hvac_mode != HVACMode.OFF:
                self._last_on_mode = hvac_mode
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

    async def async_turn_on(self) -> None:
        """Turn the HVAC device on (restore last mode or default to auto)."""
        await self.async_set_hvac_mode(self._last_on_mode)

    async def async_turn_off(self) -> None:
        """Turn the HVAC device off."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    # -------------------------------------------------------------------------
    # Extended controls (new driver features, PR#24517 / #24660)
    # -------------------------------------------------------------------------

    async def async_set_remote_temperature(self, temperature: float) -> None:
        """Send a remote (external) temperature to the HVAC unit.

        This uses the new command name from PR#24496 (HVACSetRemoteTemp).
        The integration sends the value and the driver uses it instead of the
        built-in room sensor.
        """
        await mqtt.async_publish(
            self.hass,
            self._topic_cmd_remote_temp,
            str(round(temperature, 1)),
            qos=1,
            retain=False,
        )
        self._remote_temperature = temperature
        self.async_write_ha_state()

    async def async_set_purifier(self, state: bool) -> None:
        """Set purifier on/off (requires cap_run_state, PR#24660)."""
        await mqtt.async_publish(
            self.hass,
            f"cmnd/{self._base_topic}/HVACSetPurify",
            "on" if state else "off",
            qos=1,
            retain=False,
        )
        self._purifier = "on" if state else "off"
        self.async_write_ha_state()

    async def async_set_night_mode(self, state: bool) -> None:
        """Set night mode on/off (requires cap_run_state, PR#24660)."""
        await mqtt.async_publish(
            self.hass,
            f"cmnd/{self._base_topic}/HVACSetNightMode",
            "on" if state else "off",
            qos=1,
            retain=False,
        )
        self._night_mode = "on" if state else "off"
        self.async_write_ha_state()

    async def async_set_econo_cool(self, state: bool) -> None:
        """Set EconoCool on/off — COOL mode only (requires cap_run_state, PR#24660)."""
        await mqtt.async_publish(
            self.hass,
            f"cmnd/{self._base_topic}/HVACSetEconoCool",
            "on" if state else "off",
            qos=1,
            retain=False,
        )
        self._econo_cool = "on" if state else "off"
        self.async_write_ha_state()

    async def async_set_air_direction(self, direction: str) -> None:
        """Set i-See air direction (requires cap_run_state + i-See, PR#24660)."""
        await mqtt.async_publish(
            self.hass,
            f"cmnd/{self._base_topic}/HVACSetAirDirection",
            direction,
            qos=1,
            retain=False,
        )
        self._air_direction = direction
        self.async_write_ha_state()

    async def async_set_horizontal_swing(self, swing_mode: str) -> None:
        """Set horizontal swing mode."""
        from .const import SWING_H_MODES
        if swing_mode in SWING_H_MODES:
            await mqtt.async_publish(
                self.hass,
                f"cmnd/{self._base_topic}/HVACSetSwingH",
                swing_mode,
                qos=1,
                retain=False,
            )
            self._swing_h_mode = swing_mode
            self.async_write_ha_state()
