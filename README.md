# Cube Light

Software for a home-built **5×5×5 RGB LED cube**: a Raspberry Pi web controller that runs animated patterns and playable 3D games, plus the RP2040 firmware that drives the LEDs.

```
 Phone / browser ──HTTP──▶ Raspberry Pi (cube_MP_server.py) ──I2C──▶ RP2040(s) ──▶ 125 WS2812 LEDs
 ESP32 controller ─BT─▶ game_controller_bridge.py ──HTTP──┘
```

## Features

- **Web controller** on port 5000 with live brightness and per-pattern tuning (speed, colours, modes) that applies while a pattern is running.
- **11 patterns:** rainbow, fireworks, wave, rain, flame, XMS, drips, lava, ocean, spiral, shimmer.
- **3 games**, with high scores saved between runs:
  - **Space Planes** — walls fly toward you; line your dot up with the hole before each one arrives.
  - **3D Snake** — moves along all three axes, wraps around the edges, and you win by filling all 125 cells.
  - **Meteors** — shoot down falling meteors from the bottom plane before they land.
- **Bluetooth game controller** (optional): an ESP32 handheld that drives the games through the same API as the web UI.
- **Two cube variants supported** (Mini and Squid). The server detects which one is connected at startup.

## Repository layout

| Path | What it is |
|---|---|
| `cube_MP_server.py` | Main app: Flask web server + cube runner process (patterns, games, I2C output) |
| `patterns.py` | All patterns and their live-tunable state |
| `effects.py` | Shared particle/spark bursts (fireworks, game explosions) |
| `templates/index.html` | The single-page web UI |
| `cube_MP_server.service` | systemd unit for running the server on boot |
| `XYZ_test.py` | Hardware smoke test — lights the three axes |
| `game_controller_bridge.py` | Bluetooth (SPP) controller → REST bridge |
| `ESP32_controller/` | Firmware for the handheld controller |
| `Mini_cube_rp2040/` | Firmware for the Mini cube (one RP2040 at I2C `0x20`) |
| `Squid_cube_rp2040/` | Firmware for the Squid cube (five RP2040s at `0x21–0x25`, one per layer) |
| `gamma.py` | Generates the gamma lookup table pasted into the firmware |

## Hardware

- **Host:** Raspberry Pi Zero 2 W (any Pi with I2C enabled should work), I2C bus 1.
- **Cube:** 125 WS2812/NeoPixel LEDs driven by RP2040 boards acting as I2C slaves.
  - *Mini:* one RP2040 drives five strips of 25 LEDs.
  - *Squid:* five RP2040s, one per layer; address set by GPIO 27/28/29 straps.
- **Controller (optional):** ESP32 with buttons, advertising as `CubeController` over Bluetooth Classic.

## Setup

### Raspberry Pi

Enable I2C (`sudo raspi-config` → Interface Options → I2C), then install the dependencies:

```bash
pip install flask flask_cors numpy smbus
```

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

After changing any file — including `templates/index.html` — restart the service; templates and patterns are only loaded at startup:

```bash
sudo systemctl restart cube_MP_server
```

**Cube variant:** detected automatically by probing the I2C bus. To force one, set `MINI_OVERRIDE = True` (Mini) or `False` (Squid) near the top of `cube_MP_server.py`.

**Smoke test:** `python XYZ_test.py` lights one LED along each axis — handy for checking wiring and orientation.

### Cube firmware

Built with the Arduino IDE using the [Earle Philhower arduino-pico](https://github.com/earlephilhower/arduino-pico) core and the **Adafruit NeoPixel** library. Open the sketch for your variant and flash each RP2040.

Each board runs two cores: one receives frames over I2C into a back buffer, the other drives the LED strips from the front buffer. Gamma correction and temporal dithering happen on the board. Brightness is capped at about 25% for current and eye comfort. To change the curve or cap, edit `Bcap` in `gamma.py`, run it, and paste the output into the sketch's `gamma16[256]` table.

### Bluetooth controller (optional)

Flash `ESP32_controller/ESP32_controller.ino` to the ESP32, then on the Pi:

```bash
pip install pyserial requests
sudo rfcomm bind 0 <controller-MAC> 1        # creates /dev/rfcomm0
python game_controller_bridge.py
```

`python game_controller_bridge.py --test` shows decoded button presses without contacting the server.

## How it works

The server runs as two processes. The **Flask process** serves the UI and turns each API call into a command on a queue. The **cube runner process** is the only one that touches the I2C bus. It runs patterns in a background thread, steps the active game each tick, and applies brightness. This way only one thing ever writes frames.

Each frame goes out as 25 I2C block writes, one per vertical column of 5 LEDs (15 bytes of RGB). The firmware shows the new frame only after the last column arrives, so you never see a half-drawn frame.

### Adding a pattern

1. Write `pattern_name(leds, send_to_cube, is_running, hsv_to_rgb)` in `patterns.py`. Loop while `is_running()`, write to the `leds[x][y][z]` RGB array, call `send_to_cube(leds)`, and sleep.
2. Register it in the `PATTERNS` dict at the bottom of `patterns.py`.
3. Add a button in `templates/index.html` that calls `startPattern('name', this)`.

Live-tunable settings are a row in one of the `TUNABLES` / `COLOUR_TUNABLES` / `MODE_TUNABLES` tables in `cube_MP_server.py` plus a key in the pattern's state dict. See [`CLAUDE.md`](CLAUDE.md) for the full architecture notes.
