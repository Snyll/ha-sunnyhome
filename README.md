# SunnyHome for Home Assistant

Connects Home Assistant to [SunnyHome](https://sunnyhome.cloud), the monitoring and automation
service for solar homes.

- **Live inverter data** every 10 s from a SolaX, Growatt, Solis or Sofar inverter read by
  [SolaX Modbus](https://github.com/wills106/homeassistant-solax-modbus), a GoodWe inverter read by
  the built-in GoodWe integration, or a Deye inverter read by
  [Solarman](https://github.com/davidrapan/ha-solarman).
- **Your devices on the SunnyHome dashboard**, and sensors for automation rules: only the switches,
  lights and sensors you tick.
- **Binary sensors and people** (door, window, motion and similar sensors, and whether a person is
  home or away) that you tick, read only.
- **Heat pumps and boilers** (thermostats and water heaters): their temperature and mode are read;
  the target temperature and mode can be set for the ones you tick for control.
- **Control only if you allow it**, with a safety stop: a device SunnyHome switched on is switched
  off again by Home Assistant if SunnyHome stops renewing it for 15 minutes.
- **Outbound only.** Home Assistant connects out to SunnyHome over HTTPS; nothing is opened to the
  internet and no Home Assistant credentials are shared. Inverter settings are read, never changed.

Requires Home Assistant 2025.1 or newer and a SunnyHome account.

## Install with HACS

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Snyll&repository=ha-sunnyhome&category=integration)

Or by hand in HACS:

1. HACS → ⋮ → **Custom repositories**, add `https://github.com/Snyll/ha-sunnyhome` with type
   **Integration**.
2. Search for **SunnyHome**, open it and choose **Download**.
3. Restart Home Assistant.

HACS then offers each new version as an update.

## Install by hand

Copy `custom_components/sunnyhome` from the [latest release](https://github.com/Snyll/ha-sunnyhome/releases/latest)
into the `custom_components` folder of your Home Assistant configuration directory and restart
Home Assistant.

## Connect

1. In SunnyHome choose **Connect Home Assistant** and note the code (`XXXX-XXXX`, valid 10 minutes).
2. In Home Assistant open **Settings → Devices & services → Add integration → SunnyHome** and enter
   the code.
3. Under **Configure** tick the devices and sensors to share, and the ones SunnyHome may switch.

## Support

Report problems in [Issues](https://github.com/Snyll/ha-sunnyhome/issues) or write to
martin@sunnyhome.cloud.

## Licence

MIT, see [LICENSE](LICENSE).
