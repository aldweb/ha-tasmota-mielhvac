"""Constants for the Tasmota MiElHVAC integration."""
from homeassistant.components.climate import HVACAction, HVACMode

# Integration domain
DOMAIN = "tasmota_mielhvac"

# Default model identifier in MQTT messages
DEFAULT_MODEL = "MiElHVAC"

# Temperature range and precision
MIN_TEMP = 10
MAX_TEMP = 31
TEMP_STEP = 1.0
PRECISION = 0.5

# HVAC mode mapping: Tasmota → Home Assistant
HVAC_MODE_MAP: dict[str, HVACMode] = {
    "off": HVACMode.OFF,
    "auto": HVACMode.AUTO,
    "cool": HVACMode.COOL,
    "dry": HVACMode.DRY,
    "heat": HVACMode.HEAT,
    "fan_only": HVACMode.FAN_ONLY,
}

# Reverse mapping: Home Assistant → Tasmota
HVAC_MODE_REVERSE_MAP: dict[HVACMode, str] = {
    v: k for k, v in HVAC_MODE_MAP.items()
}

# HVAC action mapping: Tasmota mode → Home Assistant action
ACTION_MAP: dict[str, HVACAction] = {
    "off": HVACAction.OFF,
    "heat": HVACAction.HEATING,
    "cool": HVACAction.COOLING,
    "dry": HVACAction.DRYING,
    "fan_only": HVACAction.FAN,
    "auto": HVACAction.IDLE,
}

# Available fan speed modes
FAN_MODES: list[str] = ["auto", "quiet", "1", "2", "3", "4"]

# Available swing modes (vertical)
SWING_V_MODES: list[str] = [
    "auto",
    "up",
    "up_middle",
    "center",
    "down_middle",
    "down",
    "swing",
]

# Available swing modes (horizontal)
SWING_H_MODES: list[str] = [
    "auto",
    "left",
    "left_middle",
    "center",
    "right_middle",
    "right",
    "swing",
]
