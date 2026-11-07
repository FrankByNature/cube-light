# Cube Light

Software for a home-built **5×5×5 RGB LED cube**: a Raspberry Pi web controller that runs animated patterns and playable 3D games, plus the RP2040 firmware that drives the LEDs.

```
 Phone / browser ──HTTP──▶ Raspberry Pi (cube_MP_server.py) ──I2C──▶ RP2040(s) ──▶ 125 WS2812 LEDs
 ESP32 controller ─BT─▶ game_controller_bridge.py ──HTTP──┘
```

## Features

- **Web controller** on port 5000 with live brightness and per-pattern tuning (speed, colours, modes) that applies while a pattern is running. Reloading the page, or opening it on a second phone, shows what the cube is actually doing.
- **Live preview** in the browser of the frame the cube is showing, which also makes it possible to develop without the cube (see below).
- **11 patterns:** rainbow, fireworks, wave, rain, flame, XMS, drips, lava, ocean, spiral, shimmer.
- **3 games**, with high scores saved between runs:
  - **Space Planes** — walls fly toward you; line your dot up with the hole before each one arrives.
  - **3D Snake** — moves along all three axes, wraps around the edges, with a timed purple bonus every fifth food. Win by filling all 125 cells.
  - **Meteors** — shoot down falling meteors from the bottom plane before they land.
- **Bluetooth game controller** (optional): an ESP32 handheld that drives the games through the same API as the web UI.
- **Two cube variants supported** (Mini and Squid). The server detects which one is connected at startup.

## Repository layout

| Path | What it is |
|---|---|
| `cube_MP_server.py` | Main app: Flask web server + cube runner process (patterns, games) |
| `cube_io.py` | I2C output layer: bus detection, the two cubes' wiring maps, simulated bus |
| `patterns.py` | All patterns and their live-tunable state |
| `effects.py` | Shared particle/spark bursts (fireworks, game explosions) |
| `templates/index.html` | The single-page web UI |
| `cube_MP_server.service` | systemd unit for running the server on boot |
| `cube_controller_rfcomm@.service` | systemd unit that creates `/dev/rfcomm0` for the controller on boot |
| `XYZ_test.py` | Hardware smoke test — lights the three axes |
| `game_controller_bridge.py` | Bluetooth (SPP) controller → REST bridge |
| `ESP32_controller/` | Firmware for the handheld controller |
| `Mini_cube_rp2040/` | Firmware for the Mini cube (one RP2040 at I2C `0x20`) |
| `Squid_cube_rp2040/` | Firmware for the Squid cube (five RP2040s at `0x21–0x25`, one per layer) |
| `gamma.py` | Generates the gamma lookup table pasted into the firmware |
| `tests/` | pytest suite; runs anywhere, no cube needed |
| `requirements.txt`, `requirements-dev.txt` | Python dependencies for the Pi and for development |

## Hardware

- **Host:** Raspberry Pi Zero 2 W (any Pi with I2C enabled should work), I2C bus 1 at the default **100 kHz**. Faster bus speeds were tried and produced corrupted frames on this wiring, so leave the default.
- **Cube:** 125 WS2812/NeoPixel LEDs driven by RP2040 boards acting as I2C slaves.
  - *Mini:* one RP2040 drives five strips of 25 LEDs.
  - *Squid:* five RP2040s, one per layer; address set by GPIO 27/28/29 straps to 3V3.
- **Controller (optional):** ESP32 with buttons and a small OLED, advertising as `CubeController` over Bluetooth Classic.

## Setup

### Raspberry Pi

Enable I2C (`sudo raspi-config` → Interface Options → I2C), then install the dependencies:

```bash
pip install -r requirements.txt
```

(`smbus2` is used if the Debian `python3-smbus` package isn't installed; either works.)

Run it directly:

```bash
python cube_MP_server.py
```

Then open `http://<pi-address>:5000`. `Ctrl+C` clears the cube and shuts down cleanly.

To run at boot, put the project in `/home/cube/share` (or edit the paths in the unit file) and install the service:

```bash
sudo cp cube_MP_server.service /etc/systemd/system/
sudo systemctl enable --now cube_MP_server
```

Logs go to the journal:

```bash
journalctl -u cube_MP_server -f
```

After changing any file — including `templates/index.html` — restart the service; templates and patterns are only loaded at startup:

```bash
sudo systemctl restart cube_MP_server
```

**Cube variant:** detected automatically by probing the I2C bus. To force one, set the environment variable `CUBE_HARDWARE=mini` or `CUBE_HARDWARE=squid` (for the service, add `Environment=CUBE_HARDWARE=mini` to the unit), or set `MINI_OVERRIDE` near the top of `cube_io.py`.

**Shut Down Pi button:** the web UI's shutdown button runs `sudo -n shutdown -h now`, which needs a passwordless sudo rule for the service user. Create it with `sudo visudo -f /etc/sudoers.d/cube-shutdown` containing:

```
cube ALL=(root) NOPASSWD: /sbin/shutdown
```

**Smoke test:** `python XYZ_test.py` lights one LED along each axis — handy for checking wiring and orientation.

### Security

The server has no login: anyone who can reach port 5000 can control the cube, which is fine on a home network and is why it should not be exposed to the internet. The shutdown endpoint additionally requires a custom `X-Cube-Confirm: shutdown` header, which the UI sends. That header is what stops a malicious web page open in a browser on your network from shutting the Pi down: browsers will not add custom headers to a cross-origin request without a CORS preflight, and this server does not answer preflights (it deliberately has no CORS support; the UI is same-origin and the controller bridge is not a browser).

### Developing without the cube

The server runs on any machine. If there is no I2C bus (or `CUBE_SIMULATE=1` is set) it uses a simulated bus and logs that it is simulated. Open the web UI and use **Show Preview** on the Control tab to see what the cube would be displaying.

```bash
pip install -r requirements.txt -r requirements-dev.txt
CUBE_SIMULATE=1 python cube_MP_server.py      # http://localhost:5000
pytest                                         # the test suite
```

### Cube firmware

Built with the Arduino IDE using the [Earle Philhower arduino-pico](https://github.com/earlephilhower/arduino-pico) core and the **Adafruit NeoPixel** library. Open the sketch for your variant and flash each RP2040.

Each board runs two cores: one receives frames over I2C into a back buffer, the other copies each completed frame into its own buffer and drives the LED strips from that. Gamma correction and temporal dithering happen on the board.

Brightness is capped on the board, and the two cubes use **different caps** (different LEDs and power budgets): the Mini at 52 (about 20% of full scale), the Squid at 78 (about 31%). To change a cap or the gamma curve, regenerate the table for that cube and paste it over `gamma16[256]` in its sketch:

```bash
python gamma.py mini        # or: squid, or --cap N
```

### Bluetooth controller (optional)

Flash `ESP32_controller/ESP32_controller.ino` to the ESP32, then on the Pi:

```bash
sudo rfcomm bind 0 <controller-MAC> 1        # creates /dev/rfcomm0
python game_controller_bridge.py
```

The bridge can also be started and stopped from the Games tab in the web UI. `python game_controller_bridge.py --test` shows decoded button presses without contacting the server.

`rfcomm bind` doesn't survive a reboot. To create `/dev/rfcomm0` at every boot, install the unit with the controller's MAC address as the instance name (pair and trust the controller first):

```bash
sudo cp cube_controller_rfcomm@.service /etc/systemd/system/
sudo systemctl enable --now cube_controller_rfcomm@<controller-MAC>
```

Binding doesn't connect, so the controller can be off at boot; the link comes up when the bridge opens the device. The device belongs to the `dialout` group, so if the bridge gets a permission error, run `sudo usermod -aG dialout cube` and reboot.

## How it works

The server runs as two processes. The **Flask process** serves the UI and turns each API call into a command on a queue; it also keeps a mirror of what it has asked for, which `GET /cube/state` reports so a freshly loaded page can sync. The **cube runner process** is the only one that touches the I2C bus. It runs patterns in a background thread, steps the active game each tick, and applies brightness. This way only one thing ever writes frames. A copy of every frame also goes into shared memory for the UI's preview. If the runner ever crashes the service exits so systemd restarts it.

Each frame goes out as 25 I2C block writes, one per vertical column of 5 LEDs (15 bytes of RGB). The firmware shows the new frame only after the last column arrives, so you never see a half-drawn frame.

### Adding a pattern

1. Write `pattern_name(leds, send_to_cube, is_running, hsv_to_rgb)` in `patterns.py`. Loop while `is_running()`, write to the `leds[x][y][z]` RGB array, call `send_to_cube(leds)`, and sleep.
2. Register it in the `PATTERNS` dict at the bottom of `patterns.py`.
3. Add a button in `templates/index.html` with `data-pattern="name"` calling `startPattern('name')`, and (optionally) a settings panel with `class="pattern-controls" data-pattern="name"`.

Live-tunable settings are a row in one of the `TUNABLES` / `COLOUR_TUNABLES` / `MODE_TUNABLES` tables in `cube_MP_server.py`, a key in the pattern's state dict, and a row in the `TUNABLES` table in `index.html`. See [`CLAUDE.md`](CLAUDE.md) for the full architecture notes.
