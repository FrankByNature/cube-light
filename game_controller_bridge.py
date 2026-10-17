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
    fire                  Fire button (Meteors only; ignored by the other games)
    start                 Start button: (re)start the selected game
    game:planes           selection changed to Space Planes
    game:snake            selection changed to 3D Snake
    game:meteors          selection changed to Meteors
--------------------------------------------------------------------------

Direction mapping:
    Snake  uses all six -> px/nx/py/ny/pz/nz are sent straight through.
    Planes is 2D (front plane only), so only 4 axis buttons are used:
        py -> right   ny -> left   pz -> up   nz -> down     (px/nx ignored)
    Meteors is 2D too (bottom plane), mapped like the web UI's keys for Snake:
        px -> right   nx -> left   py -> up   ny -> down     (pz/nz ignored)
    and the Fire button shoots.

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

# Planes only understands these 4 (front-plane) directions, so the 3D axis
# buttons are mapped onto them. px/nx (depth) have no meaning in Planes.
PLANES_FROM_AXIS = {
    'py': 'right',
    'ny': 'left',
    'pz': 'up',
    'nz': 'down',
}

# Meteors moves in the bottom (x/y) plane; same axis->pad mapping as the web
# UI uses for Snake's arrow keys. pz/nz have no meaning in Meteors.
METEORS_FROM_AXIS = {
    'px': 'right',
    'nx': 'left',
    'py': 'up',
    'ny': 'down',
}

VALID_GAMES = ('planes', 'snake', 'meteors')


class Bridge:
    """Translates controller tokens into cube-server REST calls."""

    def __init__(self, server, test=False, timeout=2.0):
        self.server = server.rstrip('/')
        self.test = test
        self.timeout = timeout
        self.selected = 'planes'   # mirrors the web UI's default game
        self.running = False

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

        if token == 'fire':
            self.fire()
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

    def fire(self):
        if self.selected != 'meteors':
            print(f"  ({self.selected}) ignoring fire")
            return
        print("  FIRE")
        self._post('/meteors/fire')


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
            with serial.Serial(port, baud, timeout=1) as ser:
                print(f"Connected ({mode}).")
                print(f"Selected game: {bridge.selected}.  Waiting for input "
                      "(Ctrl+C to quit) ...")
                while True:
                    line = ser.readline()
                    if not line:
                        continue  # read timeout - just keep waiting
                    token = line.decode('utf-8', errors='replace').strip()
                    if not token:
                        continue
                    print(f"< {token!r}")
                    bridge.handle(token)
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
