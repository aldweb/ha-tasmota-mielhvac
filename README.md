# Tasmota MiElHVAC Integration

> ⚠️ **Work in Progress** - This integration is currently under development

Home Assistant custom integration for **Mitsubishi Electric heat pumps** via Tasmota's **MiElHVAC driver** running on ESP32/ESP8266.

## Hardware Requirements

- ESP32 or ESP8266 board
- CN105 connector cable (to connect ESP to heat pump)
- Tasmota firmware with MiElHVAC driver
- Mitsubishi Electric heat pump with CN105 port

## Features

- ✅ Multi-device support
- ✅ Full climate control (temperature, modes, fan speed)
- ✅ Vertical swing control
- ✅ Horizontal swing monitoring
- ✅ Config Flow (UI configuration)
- ✅ Device registry integration

## Installation

1. Copy the `mielhvac_tasmota` folder to your `custom_components` directory
2. Restart Home Assistant
3. Go to **Configuration → Integrations → Add Integration**
4. Search for "Tasmota MiElHVAC"

## Configuration

Your Tasmota device must be configured with the **MiElHVAC driver** and connected via **CN105 port** to the heat pump. 

MQTT topics used:
- `tele/{device}/LWT` - Device availability
- `tele/{device}/SENSOR` - Current temperature (MiElHVAC.Temperature)
- `tele/{device}/HVACSETTINGS` - Heat pump state and settings
- `cmnd/{device}/HVAC*` - Command topics for control

**Note:** Default sensor model is `MiElHVAC` - this should match your Tasmota configuration.

## Status

🚧 Currently in development - slightly tested with Mitsubishi Electric heat pumps via CN105 connection and Tasmota MiElHVAC driver, it might be broken any time.

Feedback and contributions welcome!

## License

MIT
