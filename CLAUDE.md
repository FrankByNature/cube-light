# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Software for a physical **5×5×5 RGB LED cube**, in two halves that talk over I2C:

1. **Host control software (Python)** — runs on a Raspberry Pi, generates animation frames and pushes them out as I2C block writes. This is most of the repo. On a machine with no I2C bus (or with `CUBE_SIMULATE=1` set) it runs against a **simulated bus** instead, so patterns and games can be developed and tested anywhere; the web UI's Preview panel shows what the cube would display.
2. **Cube firmware (Arduino/C++)** — runs on RP2040 microcontrollers inside the cube. It is the I2C slave that receives those frames and drives the WS2812/NeoPixel LEDs. Lives in `Mini_cube_rp2040/` and `Squid_cube_rp2040/`.

The cube is addressed as 25 vertical "columns" of 5 LEDs each; each column is one I2C block write. There is no build system on the firmware side. The Python side has a **pytest suite in `tests/`** that runs without hardware.

## Running

```bash
pip install -r requirements.txt -r requirements-dev.txt
python cube_MP_server.py          # Main app: web controller on http://<host>:5000
CUBE_SIMULATE=1 python cube_MP_server.py   # force the simulated bus (dev machine)
pytest                            # test suite (always simulated)
python XYZ_test.py                # Hardware smoke test (lights the three axes)
```

Runtime deps: `flask`, `numpy`, and `smbus` (Debian `python3-smbus`) or `smbus2`; the bridge needs `pyserial` + `requests`. `Ctrl+C` or SIGTERM triggers a clean shutdown that clears the cube and terminates the runner process. Everything logs through the `logging` module to stdout (the journal under systemd).

### Editing vs. deploying (important)

There are **two copies of this project**: the working copy on the Windows dev machine (where edits are made) and the Pi's copy at `/home/cube/share`, which the `cube_MP_server.service` systemd unit runs from. The Pi's copy is exposed as a network share (`\\Cube\cube_share`, usually mapped to `Z:`). **Nothing syncs them automatically.**

**Claude must only ever edit the local files on the dev machine — never write to the Pi's copy (`Z:` / the share), even to fix a problem that a stale file on the Pi is causing.** Deploying is the user's step, done by hand. If something looks stale on the Pi, say so and let the user copy it.

Two things that have bitten before, worth repeating when a change doesn't show up on the cube or the web page:

- **`templates/index.html` is easy to forget.** Copying only the `.py` files leaves the UI on an older version, so new pattern buttons and settings panels never appear even though the pattern and its routes are live on the Pi. Copy the template too (and `cube_io.py`, which the server now needs).
- **A restart is always needed**, even for a template-only change: the app runs with `debug=False`, so Flask/Jinja caches `index.html` on first render and never re-checks the file, and `patterns.py` is only imported at process start. `sudo systemctl restart cube_MP_server` on the Pi, then hard-refresh the browser.

### I2C bus speed

The Pi's bus runs at the default **100 kHz**, which bounds the frame rate at roughly 25 fps (25 block writes of 16 bytes per frame). **Do not suggest raising it:** 400 kHz was tried and produced corrupted frames on this wiring. All animations are frame-based rather than time-based, so they are tuned for this rate.

## Main application architecture (`cube_MP_server.py`)

The target hardware is a **Raspberry Pi Zero 2 W** (quad-core). The app uses **two OS processes** (multiprocessing, `spawn` start method), communicating exclusively through one `Queue`, shared `Value`s and one shared `Array` — never shared Python objects:

- **Flask process (main):** serves the single-page UI (`templates/index.html`, via `render_template`) and exposes the REST API. Routes validate input, push command dicts onto `command_queue` and return immediately. The Flask process also keeps a **mirror of what it has asked for** (`ui_state`: current pattern / game, and the tuning values, which it writes into its own copy of the `patterns.*` dicts) so that `GET /cube/state` can tell a freshly loaded page the brightness, running pattern or game, all live tuning values and the defaults. A **watchdog thread** joins the runner process; if the runner dies on its own the main process cleans up and exits non-zero so systemd restarts the whole service.
- **`cube_runner` process:** the **single owner of the LEDs and the I2C bus** — patterns *and* all games run here, so two writers can never interleave frames on the bus (mutual exclusion is structural, not cooperative). The loop blocks on `command_queue.get(timeout=...)`: `GAME_TICK` (0.02 s) while a game is active so the game keeps animating, `IDLE_TIMEOUT` (0.25 s) otherwise. The timeout is **not** command latency — `get()` wakes immediately when a command is queued; it only bounds how often the loop wakes when idle. When a command arrives the loop **drains everything already queued** and drops all but the last `brightness` refresh (the slider posts per step while dragged, and each refresh of a static cube is a full frame on the bus). Each command is handled inside a try/except so a bad command can't kill the runner.
  - **Patterns** run in a **daemon thread** so the command loop stays responsive (this is how a running pattern can be stopped or re-tuned). The thread target is `run_pattern`, which logs a traceback if the pattern raises and clears `pattern_running` (guarded by a generation counter so a dead thread never clears a newer pattern's flag). A `threading.Lock` (`leds_lock`) serializes all sends through `safe_send`, which also copies the brightness-applied frame into `frame_shared` for the UI preview. `pattern_running` is a plain per-process dict, not a multiprocessing `Value`.
  - **Games** are objects (`PlanesGame`, `SnakeGame`, `MeteorsGame`, registered in the `games` dict inside `cube_runner`) stepped inline by the loop: game commands carry a `'game'` key (`game_start` / `game_stop` / `game_move`), and each iteration calls `games[active_game].step(now)` inside a try/except (a crash logs, stops the game and clears the cube). Starting a game stops the pattern and any other game inside the runner (and vice versa for `pattern`), so routes just queue one command — no cross-queue choreography.

The games share the same command vocabulary, differing only in `direction` values — Planes uses `up/down/left/right`, Snake uses `px/nx/py/ny/pz/nz` (± per axis), Meteors uses `up/down/left/right` plus `fire` (also exposed as `/meteors/fire`). Planes writes `planes_score`/`planes_game_over` (UI polls `/planes/state`); the player dot is **white**. Snake writes `snake_score`/`snake_game_over`/`snake_won` (polled via `/snake/state`). The snake wraps around cube edges (modulo `N`), its body is a **green→yellow gradient** from head to tail (`snake_colour`), red food scores 1, and **every 5th red food spawns a timed purple bonus** worth 5 (`PURPLE_LIFETIME`, dims over its last seconds; when it expires the red food is re-placed if it had nowhere to go). It ends on self-collision — or **wins** by filling all 125 cells (`TOTAL_CELLS` check; a win skips the red-head pause and runs a fast full-snake explosion). Meteors writes `meteors_score`/`meteors_game_over` (polled via `/meteors/state`): the blue player dot moves in the bottom plane (z = 0) and fires green shots up; orange meteors charge up in the top plane like drips, then fall with a dim tail, and one reaching z = 0 ends the game. Spawn interval shrinks with elapsed play time (`SPAWN_START`/`SPAWN_DECAY`/`SPAWN_MIN`). When a game-over/win animation finishes, the game sets its own `active` flag False so the runner drops back to lazy polling.

Shared state created in `__main__` (only what crosses the process boundary): `brightness_value` (c_double, **0.0–1.0**, not 0–100), `frame_shared` (375 × c_uint8, the last frame sent, served by `GET /cube/frame`), `planes_score`/`planes_game_over`/`planes_highscore`, `snake_score`/`snake_game_over`/`snake_won`/`snake_highscore`, `meteors_score`/`meteors_game_over`/`meteors_highscore`. High scores persist per game in `highscores.json` (`load_highscores`/`save_highscore`, written atomically via a temp file + `os.replace` — add a key to `HIGHSCORE_GAMES` for a new game).

Firework/spark visuals are shared: **`effects.py`** (`Spark`, `spawn_burst`, `update_sparks`, `render_sparks`) is the single particle-burst implementation used by the fireworks pattern, the snake's eat/death bursts, and the planes death explosion. Bursts are non-blocking — callers keep a spark list, update/render it each frame.

### Security model

No authentication: anyone on the LAN can drive the cube. **There is deliberately no CORS** (the UI is same-origin and the bridge is not a browser), and `POST /system/shutdown` requires the header `X-Cube-Confirm: shutdown`. Together those stop a hostile web page in a browser on the LAN from shutting the Pi down: a custom header forces a CORS preflight, which this server never answers. Don't add `flask_cors` back. The shutdown runs `sudo -n shutdown -h now`, which needs a NOPASSWD sudoers rule on the Pi (documented in the README).

### The pattern contract

Every pattern is a function `pattern_x(leds, send_to_cube, is_running, hsv_to_rgb)` that loops `while is_running():`, mutates the `leds` numpy array `(5,5,5,3)`, calls `send_to_cube(leds)`, and sleeps. `is_running` is a closure over `pattern_running['value']` — checking it each iteration is what makes patterns stoppable. The `send_to_cube` passed in is a thread-safe wrapper that also applies live brightness.

Patterns live in **`patterns.py`** and are registered in the `PATTERNS` dict at the bottom. **To add a pattern:** write the function, add it to `PATTERNS`, then add a UI button in `templates/index.html` with `data-pattern="name"` calling `startPattern('name')`, and if it has settings a panel `<div class="pattern-controls" data-pattern="name">`. The JS shows/hides panels by that attribute, so no per-pattern JS is needed. `tests/test_patterns.py` runs every registered pattern automatically.

### Live tuning pattern (important)

Per-pattern parameters are kept in **module-level dicts in `patterns.py`** (`wave_state`, `rainbow_state`, `rain_state`, `drips_state`, `fireworks_state`, `xms_state`, `flame_state`, `lava_state`, `ocean_state`, `spiral_state`, `shimmer_state`; listed in `PATTERN_STATES` in the server). The flow is: UI slider → Flask route → `command_queue` → `cube_runner` mutates `patterns.<state>[...]` → the running pattern reads the dict each frame and updates live. The Flask process applies the same mutation to its own copy so `/cube/state` reflects it.

Tunables are **table-driven at both ends**. Three lists inside `create_flask_app` generate one POST route per row, and all of them emit the same generic `{'type': 'tune', 'state', 'key', 'value'}` command, which `cube_runner` handles in one line: `getattr(patterns, cmd['state'])[cmd['key']] = cmd['value']` — the value can be a number, a `[r, g, b]` list or a mode string, so no new `cube_runner` branch is ever needed.

- `TUNABLES` — scalars: `(url, json_key, state_attr, state_key, default, lo, hi)`, clamped to `[lo, hi]`.
- `COLOUR_TUNABLES` — colour pickers: `(url, state_attr, state_key)`, body `{red, green, blue}`, stored as `[r, g, b]` (channels clamped).
- `MODE_TUNABLES` — string choices: `(url, json_key, state_attr, state_key, allowed, default)`, falling back to `default` if the value isn't in `allowed`.

The wave colour/mode/axis routes are ordinary rows in these tables (they used to have their own routes and runner branches).

On the UI side, **`index.html` has one `TUNABLES` table** mapping each `(state, key)` to its URL, JSON key, control element and display scaling. Sliders and colour pickers carry `data-state`/`data-key` attributes and call the generic `sliderChanged(this)` / `colourChanged(this)`; mode buttons call `setMode(state, key, value)`. The same table drives the load-time sync from `/cube/state` and the per-panel "Reset to Defaults" buttons (defaults come from the server's `PATTERN_DEFAULTS` snapshot of `patterns.py`).

**To add a tunable:** add a row to the matching server table, a key in the relevant `patterns.py` state dict, a row in the JS `TUNABLES` table, and a control with `data-state`/`data-key`. Endpoint names are `tune_<state_attr>_<state_key>`, so they must stay unique across all three tables.

Patterns are numpy-vectorized where it's cheap to do so (`pattern_wave`, `pattern_XMS` compute the whole `(5,5,5)` grid at once rather than looping per cell). This is CPU headroom, not frame rate — output is I2C-bound.

### Adding a game

Write a class with the `PlanesGame`/`SnakeGame` shape: `start()`, `stop()`, `move(direction)`, `step(now)`, and an `active` flag (set it False yourself when a game-over animation finishes so the runner drops back to lazy polling). The constructor receives `(leds, send, brightness_value, <shared Values>)`; `step()` mutates `leds` and pushes each frame with `self.send(self.leds, self.brightness)`. Then: register an instance in the `games` dict inside `cube_runner`, create the game's shared `Value`s in `__main__` and thread them through the `cube_runner` and `create_flask_app` argument lists, add `/<name>/start|stop|move|state` routes that queue `{'type': 'game_*', 'game': '<name>'}` commands (calling `note_game`/`note_stopped` and adding the game-over Value to `game_over_values` so `/cube/state` tracks it), add a key to `HIGHSCORE_GAMES`, and add a UI panel + `showGamePanel()`/`applyCubeState()` entries in `templates/index.html`. No new process or queue is needed. Game logic is unit-testable with a fake clock — see `tests/test_games.py`.

## Hardware addressing & coordinate mapping (`cube_io.py`)

This is the subtle part. There are **two physical cube variants** with different wiring. The bus, the detection and both output functions live in **`cube_io.py`**, shared by the server and `XYZ_test.py`; the module-level **`MINI` flag** selects which one `send_to_cube` dispatches to.

`MINI` is resolved **once per process at import**: `CUBE_HARDWARE=mini|squid` in the environment wins, then `MINI_OVERRIDE` in `cube_io.py` (`True`/`False`, `None` = probe), otherwise `detect_hardware()` probes the bus: the Mini answers at `0x20`, the Squid at `0x21–0x25` (non-overlapping), so a quick no-data write to each address tells them apart. Under `spawn` this runs in both the main process and the `cube_runner` worker, since each needs its own I2C handle; the probe is cheap. The result is logged only from the main process. If nothing answers it falls back to Squid so the server still starts. If the `smbus` module is missing, the bus can't be opened, or `CUBE_SIMULATE=1` is set, `cube_io.SIMULATED` is True and a `SimBus` stand-in accepts every write (the firmware ignores the zero-byte probe writes, so probing is harmless).

- **`send_to_mini`** (`MINI = True`): single I2C address `0x20`. Each of the 25 columns is written with register `5*x + y` and a 15-byte payload (5 LEDs × RGB). Columns use **serpentine ordering**: when `y` is even the z-axis is reversed (`pos = 4 - z`), odd `y` is forward — this compensates for boustrophedon LED wiring. Reads `led_data[x][pos][y]`.
- **`send_to_squid`** (`MINI = False`): five layer addresses `0x21–0x25` (one per `x`), register `y`, no serpentine. Reads `led_data[x][y][4-z]`.

The two variants use **different coordinate mappings** (matching each cube's physical wiring) while patterns always write `leds[x][y][z]` — preserve each function's mapping when editing. `tests/test_cube_io.py` pins both mappings and the register order.

I2C write errors are deliberately swallowed (`except Exception: pass`) to avoid console spam during rapid pattern switching — failures are silent, so a dead cube won't raise.

### Wire protocol (host ↔ firmware)

Each I2C block write is **1 register byte + 15 data bytes** = 5 LEDs × 3 channels (R,G,B, 0–255); the firmware ignores any write that isn't exactly 16 bytes. The register byte selects which group of 5 LEDs is being updated. Channel values are **raw linear 0–255** — gamma correction and brightness shaping happen in firmware, not on the host. The host must write the columns in order; the firmware commits the frame only when it receives the **last** register of the frame, so a frame is presented atomically once all 25 columns arrive.

## Cube firmware (`Mini_cube_rp2040/`, `Squid_cube_rp2040/`)

Arduino sketches for the **RP2040** using the Earle Philhower arduino-pico core (note the dual-core `setup1`/`loop1`/`tight_loop_contents` idioms) and the **Adafruit_NeoPixel** library. Both sketches share the same design:

- **Dual core:** core 0 (`setup`/`loop`) is the I2C slave — `Wire.onReceive(rcv)` writes incoming bytes into `LEDs[writeBuffer]`, and on the last register of a frame the ISR itself flips `writeBuffer`/`readBuffer` and sets `newFrame`. Core 1 (`setup1`/`loop1`) copies the completed `LEDs[readBuffer]` into its **private `frameOut` buffer** when it sees `newFrame`, then continuously drives the five NeoPixel strips from that copy. The ISR is the only writer of the two buffer indices and core 1 the only user of `frameOut`, so the host can start the next frame immediately without any of it leaking into the frame being shown (the only shared window is the microsecond memcpy).
- **Input guards:** `rcv` ignores writes that aren't exactly 16 bytes (so the host's presence probe doesn't black a column) and registers outside the valid range (so a bus glitch can't index outside the arrays).
- **Gamma + temporal dithering:** incoming 8-bit values index a `const uint16_t gamma16[256]` table whose upper byte is the integer output and lower byte is a fractional remainder. `dither()` accumulates the fraction per-LED-per-channel and bumps the integer on overflow, giving extra perceptual bit depth at low brightness. **This `gamma16` table is the output of `gamma.py`** — the two cubes deliberately use **different brightness caps** (Mini 52 ≈ 20% of full scale, Squid 78 ≈ 31%; `CAPS` in `gamma.py`). Regenerate with `python gamma.py mini` / `python gamma.py squid` (or `--cap N`) and paste it over the matching sketch's table if you change a cap or the curve; keep the comment above the table and `CAPS` in step.
- The two sketches differ only in I2C addressing and how a frame is split across boards — see below.

### The two cube variants

- **Mini cube** (`Mini_cube_rp2040`): **one** RP2040 at fixed address `0x20` (`I2C_adrs = 32`) drives all 125 LEDs across 5 strips of 25. Register = `5*x + y` (0–24); frame commits when register `24` arrives.
- **Squid cube** (`Squid_cube_rp2040`): **five** RP2040 boards, one per cube layer, at addresses `0x21–0x25`. Each board reads three address-select GPIOs (27/28/29, strapped to 3V3 for a 1, with **internal pull-downs** so an unstrapped pin reads 0 rather than floating) to compute `I2C_adrs = 32 + v1 + 2*v2 + 4*v3`, drives 5 strips of 5, register = layer index (0–4); frame commits when register `4` arrives.

## Web UI (`templates/index.html`)

Single page, three tabs (Control / Patterns / Games), plain JS. Every request goes through `api()`/`post()`, which surface a dead server or an error response in the status line on the Control tab. On load it fetches `/cube/state` and `applyCubeState()` syncs the brightness sliders, the active pattern button and panel, every tuning control and any running game, then opens the matching tab. The brightness slider is debounced (one POST per 60 ms). The Control tab has a **Preview** panel: a canvas that polls `/cube/frame` ten times a second while open and draws the 125 LEDs isometrically (x = 0, the Planes player's slice, nearest the viewer; z up). Switching away from the Patterns tab stops the pattern and away from the Games tab stops the game, as before.

## Auxiliary / standalone files (not part of the server)

- `gamma.py` — prints the 256-entry gamma-2.2 lookup table for a cube (`mini` / `squid`, or `--cap N`) in paste-ready form (see the firmware section).
- `XYZ_test.py` — minimal I2C smoke test that lights one LED per axis on either cube, through `cube_io`.
- `tests/` — pytest suite: `conftest.py` forces simulated mode and builds a Flask test client whose "runner" is a plain queue; `test_server.py` covers routes, validation, state mirroring, shutdown header, high-score persistence; `test_games.py` drives the three games with a fake clock; `test_patterns.py` runs every registered pattern; `test_cube_io.py` pins the two wiring maps; `test_effects.py` the spark system.
- `game_controller_bridge.py` — Bluetooth game-controller bridge: reads button tokens from an ESP32 (Bluetooth Classic SPP via `/dev/rfcomm0`) and turns them into the **same REST calls the web UI makes** (`/planes/*`, `/snake/*`, `/meteors/*`). It is a pure client of the server — run it alongside `cube_MP_server.py`, by hand or from the Games tab's **Bluetooth Controller** button (`/bridge/start|stop|state`: the Flask process launches it with `subprocess`, logs to `controller_bridge.log` and stops it on shutdown; `bridge_link_status()` reads the log tail for the link state, matching the phrases "Opening ", "Connected (", "Serial error" and "not found" — keep those if rewording the bridge's log lines); `--test` mode decodes controller input without contacting the server. **The game route URLs are its API — don't rename them without updating the bridge.** Deps: `pyserial`, `requests`.
- `ESP32_controller/ESP32_controller.ino` — firmware for that handheld controller (advertises as "CubeController", sends one token per line). Select cycles the `GAMES` list. There is no dedicated Fire button: in Meteors the bridge maps Z- (`nz`) to `/meteors/fire`. A 96x16 SSD1306 OLED (I2C on GPIO 21/22) shows the selected game, score and high score: the bridge polls `/<game>/state` and sends `disp:<game>:<score>:<highscore>:<status>` lines back to the ESP32 whenever they change. Deps: Adafruit SSD1306 + GFX.
