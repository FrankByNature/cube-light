#!/usr/bin/env python3
"""
Bluetooth game-controller bridge for the LED cube.

An ESP32 (Bluetooth Classic / SPP) sends short newline-terminated button
tokens; this script turns them into the very same HTTP calls the web UI makes,
so a physical controller can drive the Snake / Space Planes / Meteors games with **no
changes to cube_MP_server.py**. It is simply another *client* of the cube
server's existing REST API - run it alongside cube_MP_server.py (on the Pi, or
any machine that can reach the server and the controller).

--------------------------------------------------------------------------
Controller protocol  (ESP32 -> this script), one token per line:
    px nx py ny pz nz     6 direction buttons (axis +/-)
    start                 Start button: (re)start the selected game
    game:planes           selection changed to Space Planes
    game:snake            selection changed to 3D Snake
    game:meteors          selection changed to Meteors

Display protocol  (this script -> ESP32), sent whenever it changes:
    disp:<game>:<score>:<highscore>:<status>
        status is ready | play | over | won; '-' marks an unknown value
        (server unreachable, or --test mode). The bridge polls the selected
        game's /<game>/state every POLL_INTERVAL seconds to build it, and the
        ESP32 shows it on its OLED.
--------------------------------------------------------------------------

Direction mapping:
    Snake  uses all six -> px/nx/py/ny/pz/nz are sent straight through.
    Planes and Meteors are 2D, so the X/Y buttons act as a d-pad for both:
        px -> right   nx -> left   py -> up   ny -> down     (pz ignored)
    Planes also ignores nz.
    and Z- (nz) shoots - there is no dedicated Fire button.

Game selection:
    The Select button on the ESP32 cycles Planes -> Snake -> Meteors and sends a
    `game:<name>` token. Switching selection stops any running game, exactly
    like the web UI's game tabs. Press Start to launch the selected game.
    (To start the next game over, just press Start again - it resets.)

--------------------------------------------------------------------------
Bluetooth setup (one time, on the Pi):
    1. Pair with the ESP32 (it advertises as "CubeController"):
         bluetoothctl
         > scan on            # note the ESP32's MAC address
         > pair  AA:BB:CC:DD:EE:FF
         > trust AA:BB:CC:DD:EE:FF
         > quit
    2. Bind an RFCOMM serial device to it:
         sudo rfcomm bind 0 AA:BB:CC:DD:EE:FF 1     # creates /dev/rfcomm0
    3. Run this bridge (opening the device triggers the connection):
         python game_controller_bridge.py

Test the controller WITHOUT the cube server running:
         python game_controller_bridge.py --test
    -> connects to the controller and prints every decoded button press
       (and the action it *would* send), but never contacts the server.
       Use this to verify the Bluetooth link and the button wiring on its own.

Dependencies (Pi):  pip install pyserial requests
--------------------------------------------------------------------------
"""

import argparse
import sys
import time

try:
    import serial  # pyserial
except ImportError:
    serial = None

try:
    import requests
except ImportError:
    requests = None


# ----- token -> action mapping --------------------------------------------

DIRECTIONS = {'px', 'nx', 'py', 'ny', 'pz', 'nz'}

# Planes only understands these 4 (front-plane) directions; the X/Y buttons
# are used as a d-pad, same as Meteors. pz/nz are unused in Planes.
PLANES_FROM_AXIS = {
    'px': 'right',
    'nx': 'left',
    'py': 'up',
    'ny': 'down',
}

# Meteors moves in the bottom (x/y) plane; same axis->pad mapping as the web
# UI uses for Snake's arrow keys. nz is the fire button; pz is unused.
METEORS_FROM_AXIS = {
    'px': 'right',
    'nx': 'left',
    'py': 'up',
    'ny': 'down',
}

VALID_GAMES = ('planes', 'snake', 'meteors')

# How often the selected game's /state is polled for the controller's OLED,
# and how long one poll may block the input loop.
POLL_INTERVAL = 0.5
POLL_TIMEOUT = 0.5


class Bridge:
    """Translates controller tokens into cube-server REST calls."""

    def __init__(self, server, test=False, timeout=2.0):
        self.server = server.rstrip('/')
        self.test = test
        self.timeout = timeout
        self.selected = 'planes'   # mirrors the web UI's default game
        self.running = False
        self.last_display = None   # last status line sent to the controller's OLED

    # -- HTTP helper -------------------------------------------------------
    def _post(self, path, json=None):
        """POST to the cube server - or, in test mode, just describe the call."""
        if self.test:
            print(f"    [test] would POST {path} {json if json else ''}".rstrip())
            return
        if requests is None:
            print("    !! 'requests' not installed - cannot reach server "
                  "(pip install requests)", file=sys.stderr)
            return
        try:
            requests.post(self.server + path, json=json, timeout=self.timeout)
        except requests.RequestException as e:
            print(f"    !! POST {path} failed: {e}", file=sys.stderr)

    def _get(self, path):
        """GET JSON from the cube server; None in test mode or on any failure."""
        if self.test or requests is None:
            return None
        try:
            r = requests.get(self.server + path, timeout=POLL_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            return None

    # -- controller display ------------------------------------------------
    def display_line(self):
        """Status line for the controller's OLED:
        disp:<game>:<score>:<highscore>:<ready|play|over|won>  ('-' = unknown)."""
        state = self._get(f'/{self.selected}/state')
        if state is None:
            return f"disp:{self.selected}:-:-:ready"
        if not self.running:
            status = 'ready'
        elif state.get('won'):
            status = 'won'
        elif state.get('game_over'):
            status = 'over'
        else:
            status = 'play'
        return (f"disp:{self.selected}:{state.get('score', '-')}:"
                f"{state.get('highscore', '-')}:{status}")

    def update_display(self, ser):
        """Send the status line to the controller, but only when it changed."""
        line = self.display_line()
        if line != self.last_display:
            print(f"> {line}")
            ser.write((line + '\n').encode('utf-8'))
            self.last_display = line

    # -- token handling ----------------------------------------------------
    def handle(self, token):
        token = token.strip()
        if not token:
            return

        if token.startswith('game:'):
            name = token.split(':', 1)[1].strip().lower()
            if name in VALID_GAMES:
                self.select_game(name)
            else:
                print(f"  ? unknown game selection: {token!r}")
            return

        if token == 'start':
            self.start_selected()
            return

        if token in DIRECTIONS:
            self.move(token)
            return

        print(f"  ? unrecognised token: {token!r}")

    def select_game(self, name):
        # Switching selection stops any running game, like the web UI's tabs.
        if self.running and name != self.selected:
            self._post(f'/{self.selected}/stop')
            self.running = False
        self.selected = name
        print(f"  selected game -> {name}")

    def start_selected(self):
        print(f"  START {self.selected}")
        self._post(f'/{self.selected}/start')
        self.running = True

    def move(self, axis):
        if self.selected == 'snake':
            print(f"  move snake {axis}")
            self._post('/snake/move', {'direction': axis})
        elif self.selected == 'meteors':
            if axis == 'nz':
                print("  FIRE")
                self._post('/meteors/fire')
                return
            direction = METEORS_FROM_AXIS.get(axis)
            if direction is None:
                print(f"  (meteors) ignoring {axis}")
                return
            print(f"  move meteors {direction}")
            self._post('/meteors/move', {'direction': direction})
        else:  # planes
            direction = PLANES_FROM_AXIS.get(axis)
            if direction is None:
                print(f"  (planes) ignoring {axis}")
                return
            print(f"  move planes {direction}")
            self._post('/planes/move', {'direction': direction})


def reader_loop(port, baud, bridge):
    """Open the serial port, feed decoded lines to the bridge, reconnect on drop."""
    if serial is None:
        print("ERROR: pyserial not installed.  pip install pyserial", file=sys.stderr)
        return

    mode = "TEST MODE - server not contacted" if bridge.test \
        else f"forwarding to {bridge.server}"

    while True:
        try:
            print(f"Opening {port} ...")
            with serial.Serial(port, baud, timeout=0.1) as ser:
                print(f"Connected ({mode}).")
                print(f"Selected game: {bridge.selected}.  Waiting for input "
                      "(Ctrl+C to quit) ...")
                bridge.last_display = None   # force a display refresh on (re)connect
                next_poll = 0.0
                buf = b''
                while True:
                    # Keep the controller's OLED current (score, high score).
                    if time.monotonic() >= next_poll:
                        bridge.update_display(ser)
                        next_poll = time.monotonic() + POLL_INTERVAL

                    # readline() returns a partial line on timeout, so buffer
                    # until the newline arrives.
                    buf += ser.readline()
                    if not buf.endswith(b'\n'):
                        continue
                    token = buf.decode('utf-8', errors='replace').strip()
                    buf = b''
                    if not token:
                        continue
                    print(f"< {token!r}")
                    bridge.handle(token)
                    next_poll = 0.0   # selection/start changes show up right away
        except serial.SerialException as e:
            print(f"Serial error: {e}.  Retrying in 3s ...", file=sys.stderr)
            time.sleep(3)
        except FileNotFoundError:
            print(f"{port} not found - did you 'rfcomm bind'?  Retrying in 3s ...",
                  file=sys.stderr)
            time.sleep(3)


def main():
    ap = argparse.ArgumentParser(
        description="Bluetooth controller bridge for the LED cube games.")
    ap.add_argument('--port', default='/dev/rfcomm0',
                    help='serial device for the ESP32 (default: /dev/rfcomm0)')
    ap.add_argument('--baud', type=int, default=115200,
                    help='baud rate (ignored by RFCOMM, default: 115200)')
    ap.add_argument('--server', default='http://localhost:5000',
                    help='cube server base URL (default: http://localhost:5000)')
    ap.add_argument('--test', action='store_true',
                    help='monitor mode: print decoded inputs but never contact '
                         'the server (test the controller + Bluetooth on their own)')
    args = ap.parse_args()

    bridge = Bridge(server=args.server, test=args.test)
    try:
        reader_loop(args.port, args.baud, bridge)
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == '__main__':
    main()
