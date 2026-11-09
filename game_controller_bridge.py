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
       It retries with growing gaps while the controller is off, and exits
       once it has been unreachable for 1 minute (--give-up MINUTES, 0 =
       never), so a forgotten bridge doesn't keep the radio busy all night.

Test the controller WITHOUT the cube server running:
         python game_controller_bridge.py --test
    -> connects to the controller and prints every decoded button press
       (and the action it *would* send), but never contacts the server.
       Use this to verify the Bluetooth link and the button wiring on its own.

Dependencies (Pi):  pip install pyserial requests
--------------------------------------------------------------------------
"""

import argparse
import logging
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

# The server reads the tail of this script's log to show the link state in
# the web UI (bridge_link_status in cube_MP_server.py), matching on the
# phrases "Giving up", "not found", "Serial error", "link up", "< " (a button
# arrived), "Connected (" and "Opening " - keep those messages if you reword
# anything here.
log = logging.getLogger('bridge')


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

# Reconnecting. Every attempt to open /dev/rfcomm0 makes the Pi's radio try to
# reach the controller for about 5 s, and the Pi Zero 2 W uses one chip for
# both Bluetooth and Wi-Fi. A bridge left retrying every few seconds for hours
# with the controller switched off preceded the Pi dropping off the network,
# so the bridge backs off between attempts and gives up (exits) once the
# controller has been unreachable for GIVE_UP_AFTER seconds (--give-up).
GIVE_UP_AFTER = 60
RETRY_MIN = 3              # seconds before the first retry...
RETRY_MAX = 30             # ...doubling up to this
# Opening the rfcomm device succeeds at once even when the controller is off;
# the failure only shows about 5 s later as a read error. So the link counts
# as up once a session has lasted LINK_PROOF seconds or delivered a button.
LINK_PROOF = 10
# A write to a half-dead link can otherwise block forever (the bridge's log
# froze on exactly that), so writes give up and count as a serial error.
WRITE_TIMEOUT = 2


class Bridge:
    """Translates controller tokens into cube-server REST calls."""

    def __init__(self, server, test=False, timeout=1.0):
        # `timeout` bounds how long one POST may stall the input loop when the
        # server is down; keep it short so buttons stay responsive.
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
            log.info("[test] would POST %s %s", path, json if json else '')
            return
        if requests is None:
            log.error("'requests' not installed - cannot reach server (pip install requests)")
            return
        try:
            requests.post(self.server + path, json=json, timeout=self.timeout)
        except requests.RequestException as e:
            log.warning("POST %s failed: %s", path, e)

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
            log.info("> %s", line)
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
                log.info("? unknown game selection: %r", token)
            return

        if token == 'start':
            self.start_selected()
            return

        if token in DIRECTIONS:
            self.move(token)
            return

        log.info("? unrecognised token: %r", token)

    def select_game(self, name):
        # Switching selection stops any running game, like the web UI's tabs.
        if self.running and name != self.selected:
            self._post(f'/{self.selected}/stop')
            self.running = False
        self.selected = name
        log.info("selected game -> %s", name)

    def start_selected(self):
        log.info("START %s", self.selected)
        self._post(f'/{self.selected}/start')
        self.running = True

    def move(self, axis):
        if self.selected == 'snake':
            log.info("move snake %s", axis)
            self._post('/snake/move', {'direction': axis})
        elif self.selected == 'meteors':
            if axis == 'nz':
                log.info("FIRE")
                self._post('/meteors/fire')
                return
            direction = METEORS_FROM_AXIS.get(axis)
            if direction is None:
                log.info("(meteors) ignoring %s", axis)
                return
            log.info("move meteors %s", direction)
            self._post('/meteors/move', {'direction': direction})
        else:  # planes
            direction = PLANES_FROM_AXIS.get(axis)
            if direction is None:
                log.info("(planes) ignoring %s", axis)
                return
            log.info("move planes %s", direction)
            self._post('/planes/move', {'direction': direction})


def reader_loop(port, baud, bridge, give_up=GIVE_UP_AFTER,
                clock=time.monotonic, sleep=time.sleep, open_port=None):
    """Open the serial port, feed decoded lines to the bridge, reconnect on
    drop. Returns once the controller has been unreachable for `give_up`
    seconds (0 = retry forever). `clock`, `sleep` and `open_port` are there
    for the tests."""
    if serial is None:
        log.error("pyserial not installed.  pip install pyserial")
        return
    if open_port is None:
        def open_port():
            return serial.Serial(port, baud, timeout=0.1, write_timeout=WRITE_TIMEOUT)

    mode = "TEST MODE - server not contacted" if bridge.test \
        else f"forwarding to {bridge.server}"

    last_seen = clock()   # the give-up clock starts when the bridge does
    delay = RETRY_MIN
    while True:
        try:
            log.info("Opening %s ...", port)
            with open_port() as ser:
                # The device is open; whether the controller is really there
                # only shows later (see LINK_PROOF).
                log.info("Connected (%s).", mode)
                log.info("Selected game: %s.  Waiting for input (Ctrl+C to quit) ...",
                         bridge.selected)
                bridge.last_display = None   # force a display refresh on (re)connect
                opened = clock()
                link_up = False
                next_poll = 0.0
                buf = b''
                while True:
                    if not link_up and clock() - opened >= LINK_PROOF:
                        link_up = True
                        log.info("Controller link up.")
                    if link_up:
                        last_seen = clock()
                        delay = RETRY_MIN

                    # Keep the controller's OLED current (score, high score).
                    if clock() >= next_poll:
                        bridge.update_display(ser)
                        next_poll = clock() + POLL_INTERVAL

                    # readline() returns a partial line on timeout, so buffer
                    # until the newline arrives.
                    buf += ser.readline()
                    if not buf.endswith(b'\n'):
                        continue
                    token = buf.decode('utf-8', errors='replace').strip()
                    buf = b''
                    if not token:
                        continue
                    if not link_up:
                        link_up = True
                        log.info("Controller link up.")
                    last_seen = clock()
                    log.info("< %r", token)
                    bridge.handle(token)
                    next_poll = 0.0   # selection/start changes show up right away
        except serial.SerialException as e:   # includes a write timeout
            log.warning("Serial error: %s.", e)
        except FileNotFoundError:
            log.warning("%s not found - did you 'rfcomm bind'?", port)

        unreachable = clock() - last_seen
        if give_up and unreachable >= give_up:
            log.warning("Giving up: controller unreachable for %d min. Bridge stopped - "
                        "start it again once the controller is on.", round(unreachable / 60))
            return
        log.info("Retrying in %ds ...", delay)
        sleep(delay)
        delay = min(delay * 2, RETRY_MAX)


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
    ap.add_argument('--give-up', type=float, default=GIVE_UP_AFTER / 60, metavar='MINUTES',
                    help='exit once the controller has been unreachable this long '
                         '(default: %(default)g; 0 = keep retrying forever)')
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format='%(asctime)s %(levelname)s %(message)s')
    bridge = Bridge(server=args.server, test=args.test)
    try:
        reader_loop(args.port, args.baud, bridge, give_up=args.give_up * 60)
    except KeyboardInterrupt:
        log.info("Bye.")


if __name__ == '__main__':
    main()
