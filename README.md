# metroboard-hass

Control a [Design Rules Metroboard](https://www.designrules.co/) from
[Home Assistant](https://www.home-assistant.io/) over your local network.

The bridge runs on the board next to the stock app and talks MQTT. Home
Assistant discovers it and adds it as a device, with no YAML needed. The board
keeps working as a normal Metroboard: live train data and updates from Design
Rules still come from their servers.

Write-up: [Turning my Metroboard into a smart light](https://alex.wohlbruck.com/blog/metroboard-home-assistant)

> [!WARNING]
> Use this at your own risk. It changes the software on your Metroboard in a way
> Design Rules doesn't support, and it could void your warranty or leave your
> board not working. I'm not responsible for any damage to your board, problems
> with your network or Home Assistant, or anything Design Rules does about a
> modified board. The code is provided as is, with no warranty, under the MIT
> license.

## Tested on

| | |
|---|---|
| Board | Metroboard Chicago (ESP32-C3, 4 MB flash) |
| MicroPython | 1.25.0 |
| Metroboard app | v0.8.0 (`base_app`) |

Other cities should work the same way, but I've only tried Chicago. The
installer stops if it can't find the spot in the app where the hook goes.

The bridge is what's running on my board. The installer has been tested against
copies of my board's filesystem, but not yet run end to end on a live board, so
make the backup in step 2 before you try it.

## Home Assistant entities

You get one device, **Metroboard (&lt;city&gt;)**:

| Entity | Type | Values |
|---|---|---|
| Display | light | on/off, brightness, effect `Line Colors` / `White` |
| Brightness | select | `LOW` / `MED` / `HIGH` |
| Color Mode | select | `COLOR` / `WHITE` |
| Transit Mode | select | `SOLID` / `PULSED` |
| Update Frequency | select | `FASTEST` / `10` / `30` |
| Night Mode | switch | on/off |
| Avg Response, Wi-Fi Signal, Firmware, IP Address | sensor | diagnostic |

Turning the light off blanks the display and pauses train updates. The board has
three brightness levels, so the slider snaps to 85, 170 or 255. Color is the
light's effect, because the board can only show the real line colors or all
white. Changes are saved to the board's config, so they survive a reboot and
stay in sync with the buttons on the back.

## Requirements

- An MQTT broker, like the
  [Mosquitto add-on](https://github.com/home-assistant/addons/blob/master/mosquitto/DOCS.md).
  It has to be the same broker Home Assistant's MQTT integration is connected
  to, and the board has to be able to reach it from your Wi-Fi.
- A USB-C data cable
- Python 3 and:

```bash
pip install esptool littlefs-python "mpy-cross==1.25.0.post2"
```

`mpy-cross` has to be the 1.25 version so the compiled files match the board's
MicroPython.

## Install

1. Plug the board into your computer over USB-C.

2. Back up the whole flash first. If anything goes wrong, this gets you back to
   exactly where you started, and you'll need it again after some vendor
   updates.

   ```bash
   python3 tools/mb_flash.py backup
   ```

   The file has your Wi-Fi credentials and the board's secrets in it, so keep it
   private.

3. Copy the example config and set your broker's IP address. Fill in `user` and
   `password` if your broker needs them.

   ```bash
   cp mb_hass.example.json mb_hass.json
   ```

4. Install:

   ```bash
   python3 tools/mb_flash.py install
   ```

The board resets when it's done. Unplug it from your computer and put it back
on its power adapter. It shows up in Home Assistant under
**Settings → Devices & services → MQTT** about 20 seconds after it boots. You
don't need to use "Add MQTT device".

To change the config later, edit `mb_hass.json` and run `install` again.

If it doesn't show up, check that `broker` is the broker Home Assistant's MQTT
integration uses and that devices on your Wi-Fi can reach it. To see the
bridge's logs, set `"debug": true`, run `install` again, leave the board plugged
into your computer, and open its serial port:

```bash
python3 -m serial.tools.miniterm --dtr 0 --rts 0 /dev/cu.usbmodem* 115200
```

## What install does

The installer reads the board's filesystem partition through the bootloader
with `esptool`, edits it with `littlefs-python`, and writes it back. That works
whatever the running app is doing.

- Compiles `mb_hass.py` and `lib/mqtt_as.py` to `.mpy` and writes them, with
  `mb_hass.json`, to the root of the filesystem
- Adds a hook to `/base_app/app_main.py`, right after the app sets up its
  buttons:

  ```python
  import mb_hass
  asyncio.create_task(mb_hass.run(ctx))
  ```

- Sets `/boot_select.txt` to `base_app`
- Skips the pending vendor update in `updated_app`, if there is one, so the board
  doesn't switch away from the hooked app

The board ships with two copies of the app. `base_app` is plain Python, and
`updated_app` holds the latest update from Design Rules, compiled. The hook can
only go in the plain Python copy, so the board runs `base_app` after install.
On my board that meant going from app v1.0.0 back to v0.8.0. Train data is
unaffected, since it comes from Design Rules' servers either way.

## How it works

The stock app is a cloud client running on asyncio. It polls Design Rules'
realtime API every 5, 10 or 30 seconds and lights the LEDs the response says
have trains at them. It doesn't run any local server.

`mb_hass.run(ctx)` is started as one more task in the app's event loop. `ctx` is
the app's live state, so the bridge changes settings on the same objects the
buttons use.

- **MQTT client.** Peter Hinch's
  [`mqtt_as`](https://github.com/peterhinch/micropython-mqtt), which does all
  of its socket work asynchronously, so a slow or missing broker can't stall the
  display. It reconnects on its own, and on every connect the bridge
  resubscribes and republishes discovery, availability and state.
- **Wi-Fi.** The app owns the Wi-Fi connection. The bridge subclasses `mqtt_as`
  so it waits for the app's connection instead of managing its own.
- **Precompiled.** Both modules ship as `.mpy`. Importing the 36 KB `mqtt_as.py`
  source makes the board compile it next to the running app, and it runs out of
  memory.
- **Isolated.** Everything is wrapped so a failure in the bridge can't take down
  the display.

## MQTT topics

Base topic: `metroboard/<device-id>`

| Topic | |
|---|---|
| `…/availability` | `online` / `offline` (retained, last will) |
| `…/state` | JSON with every setting and the diagnostics (retained) |
| `…/light/state` | e.g. `{"state": "ON", "brightness": 255, "effect": "Line Colors"}` (retained) |
| `…/set/light` | JSON with any of `state`, `brightness`, `effect` |
| `…/set/<entity>` | plain value for `brightness`, `color_mode`, `transit_mode`, `update_freq`, `night_mode` |

```bash
mosquitto_pub -h <broker> -t metroboard/<device-id>/set/light \
  -m '{"state": "ON", "brightness": 170, "effect": "White"}'
mosquitto_pub -h <broker> -t metroboard/<device-id>/set/brightness -m LOW
```

## Config

`mb_hass.json`:

| Key | Default | |
|---|---|---|
| `broker` | | Your MQTT broker's IP address |
| `port` | `1883` | |
| `user`, `password` | `null` | Set these if your broker needs them |
| `discovery_prefix` | `homeassistant` | Home Assistant's MQTT discovery prefix |
| `keepalive` | `60` | Seconds |
| `state_interval_s` | `30` | How often state and diagnostics are republished |
| `debug` | `false` | Turns on the firmware's logger so bridge logs print on the USB serial console |

## Vendor updates

Updates from Design Rules only touch the app folders, so the bridge files at the
root survive them. The board only skips the one update version the installer
told it to. When Design Rules releases a newer one, the board downloads it,
switches to it, and Home Assistant control stops. Once that update has run for
a while, it replaces `base_app` with a compiled copy, and there's no
`app_main.py` left to hook.

To get control back, run install with the backup you made in step 2:

```bash
python3 tools/mb_flash.py install --app-from metroboard-<timestamp>.bin
```

That copies the readable `base_app` from your backup, adds the hook, and tells
the board to skip the newer version.

## Uninstall

```bash
python3 tools/mb_flash.py uninstall
```

This removes the hook and the bridge files, and puts `boot_select.txt` and the
update setting back the way they were. If you need to go further, write the
backup from step 2 back to the board:

```bash
python3 -m esptool write-flash 0 metroboard-<timestamp>.bin
```

## Design Rules' rules

The firmware includes a README from Design Rules that welcomes local changes, as
long as you don't:

1. Poll their realtime API more often than once every 5 seconds
2. Change the hourly heartbeat
3. Change anything sent in error logs, heartbeats or update reports
4. Publicly share the API key or URLs in the firmware

Boards that break the first three are blacklisted automatically. This bridge
doesn't touch any of it. It only adds local control, and the fastest update
option it offers is their 5 second minimum. This repo doesn't contain any of
Design Rules' code, and the tool never copies the firmware off your board except
into your own backup.

## License

MIT. `lib/mqtt_as.py` is © Peter Hinch, also MIT, included unchanged.
