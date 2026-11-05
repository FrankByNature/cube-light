"""Hardware output layer shared by cube_MP_server.py and XYZ_test.py.

Everything that knows about the I2C bus and the two cubes' wiring lives here:

- opening the bus (or a simulated stand-in when there is no I2C),
- probing the bus to tell the Mini and Squid cubes apart,
- the two column-write routines with their different coordinate mappings.

Simulation
----------
If the `smbus` module is missing, `/dev/i2c-1` can't be opened, or the
environment variable CUBE_SIMULATE=1 is set, the bus is replaced by `SimBus`,
which accepts every write and answers no probe. The server then runs
normally on any machine (defaulting to the Squid mapping), which is how the
patterns and games can be developed and tested away from the Pi. The web UI's
Preview panel shows what the cube would be displaying.

Hardware selection
------------------
`MINI` is resolved once at import:

- CUBE_HARDWARE=mini / squid in the environment forces a variant,
- otherwise `MINI_OVERRIDE` below (True = Mini, False = Squid) if not None,
- otherwise the bus is probed: the Mini answers at 0x20, the Squid at
  0x21-0x25, and if nothing answers we fall back to Squid so the server
  still starts.

Under the 'spawn' start method this module is imported (and the bus opened)
once per process, which is what we want: each process needs its own handle.
"""
import logging
import multiprocessing
import os

import numpy as np

log = logging.getLogger(__name__)

# Set to True (Mini) or False (Squid) to skip bus probing; None = auto-detect.
# CUBE_HARDWARE in the environment takes precedence over this.
MINI_OVERRIDE = None

I2C_BUS = 1
MINI_ADDRESS = 0x20
SQUID_ADDRESSES = [0x21, 0x22, 0x23, 0x24, 0x25]

# Keep for anything that imported the old names from cube_MP_server
mini_adrs = MINI_ADDRESS
row_adrs = SQUID_ADDRESSES

try:
    import smbus
except ImportError:
    try:
        import smbus2 as smbus   # drop-in replacement, also pip-installable
    except ImportError:
        smbus = None


class SimBus:
    """Stand-in for SMBus when there is no I2C bus.

    Writes are accepted and discarded; probes fail so detection falls back to
    the Squid mapping. The last register written is kept so tests can check
    the frame-commit order."""

    def __init__(self):
        self.writes = 0
        self.last = None   # (address, register, payload) of the last write

    def write_quick(self, addr):
        raise OSError('simulated bus: nothing present')

    def write_i2c_block_data(self, addr, reg, data):
        self.writes += 1
        self.last = (addr, reg, list(data))


def _open_bus():
    """Return (bus, simulated)."""
    if os.environ.get('CUBE_SIMULATE') == '1':
        return SimBus(), True
    if smbus is None:
        return SimBus(), True
    try:
        return smbus.SMBus(I2C_BUS), False
    except (FileNotFoundError, PermissionError, OSError) as e:
        if multiprocessing.current_process().name == 'MainProcess':
            log.warning("Could not open I2C bus %d (%s) - running simulated", I2C_BUS, e)
        return SimBus(), True


i2cbus, SIMULATED = _open_bus()


def detect_hardware():
    """Probe the I2C bus to decide which cube is attached.

    The Mini cube answers at 0x20; the Squid cube answers at 0x21-0x25. These
    don't overlap, so a quick (no-data) write to each address cleanly tells the
    two apart. Returns True for Mini, False for Squid. Falls back to Squid if
    nothing answers (e.g. cube unplugged) so the server still starts."""
    def present(addr):
        try:
            i2cbus.write_quick(addr)   # SMBus quick command: just address + ACK
            return True
        except (OSError, IOError):
            return False

    if present(MINI_ADDRESS):
        return True
    if any(present(a) for a in SQUID_ADDRESSES):
        return False
    if not SIMULATED and multiprocessing.current_process().name == 'MainProcess':
        log.warning("No cube detected on I2C - defaulting to Squid")
    return False


def _resolve_hardware():
    forced = os.environ.get('CUBE_HARDWARE', '').strip().lower()
    if forced in ('mini', 'squid'):
        return forced == 'mini'
    if MINI_OVERRIDE is not None:
        return bool(MINI_OVERRIDE)
    return detect_hardware()


MINI = _resolve_hardware()


def hardware_name():
    return 'Mini' if MINI else 'Squid'


def _brightness(brightness_val):
    """Accept either a multiprocessing.Value or a plain float."""
    return brightness_val.value if hasattr(brightness_val, 'value') else brightness_val


def send_to_mini(led_data, brightness_val):
    """Mini cube: single board at 0x20, register 5*x+y, serpentine z ordering.

    Columns use serpentine ordering: when y is even the z-axis is reversed
    (pos = 4 - z), odd y is forward - this compensates for boustrophedon LED
    wiring. Reads led_data[x][pos][y]. The frame commits in firmware when
    register 24 (x = 4, y = 4) arrives, so the loop order matters."""
    try:
        b_val = _brightness(brightness_val)
        for x in range(5):
            for y in range(5):
                payload = []
                for z in range(5):
                    if y % 2 == 0:
                        pos = 4 - z
                    else:
                        pos = z
                    r = int(led_data[x][pos][y][0] * b_val)
                    g = int(led_data[x][pos][y][1] * b_val)
                    b = int(led_data[x][pos][y][2] * b_val)
                    r = max(0, min(255, r))
                    g = max(0, min(255, g))
                    b = max(0, min(255, b))
                    payload.extend([r, g, b])
                i2cbus.write_i2c_block_data(MINI_ADDRESS, (5 * x + y), payload)
    except Exception:
        # Suppress I2C errors to prevent spamming console during fast switching
        pass


def send_to_squid(led_data, brightness_val):
    """Squid cube: one board per layer at 0x21-0x25, register y, no serpentine.

    Reads led_data[x][y][4-z]. Each board commits its own frame when
    register 4 arrives."""
    try:
        b_val = _brightness(brightness_val)
        for x in range(5):
            for y in range(5):
                payload = []
                for z in range(5):
                    r = int(led_data[x][y][4 - z][0] * b_val)
                    g = int(led_data[x][y][4 - z][1] * b_val)
                    b = int(led_data[x][y][4 - z][2] * b_val)
                    r = max(0, min(255, r))
                    g = max(0, min(255, g))
                    b = max(0, min(255, b))
                    payload.extend([r, g, b])
                i2cbus.write_i2c_block_data(SQUID_ADDRESSES[x], y, payload)
    except Exception:
        # Suppress I2C errors to prevent spamming console during fast switching
        pass


def send_to_cube(led_data, brightness_val):
    """Dispatch to the active hardware output, selected by the MINI flag."""
    if MINI:
        send_to_mini(led_data, brightness_val)
    else:
        send_to_squid(led_data, brightness_val)


def frame_bytes(led_data, brightness_val):
    """The frame as the cube receives it: (5,5,5,3) uint8 in x, y, z order
    with brightness applied. Used for the web UI's preview."""
    b_val = _brightness(brightness_val)
    return np.clip(np.asarray(led_data) * b_val, 0, 255).astype(np.uint8)
