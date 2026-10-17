import smbus
import numpy as np
import time

# LED cube data structure
leds = np.zeros((5, 5, 5, 3), dtype=np.int_)
b_val = 1

# Hardware select:
#   True  -> always Mini cube (single board @ 0x20)
#   False -> always Squid cube (five boards @ 0x21-0x25)
#   None  -> auto-detect from the I2C bus at startup (probe both cubes)
MINI_OVERRIDE = None

i2cbus = smbus.SMBus(1)
mini_adrs = 0x20
row_adrs = [0x21, 0x22, 0x23, 0x24, 0x25]

def detect_hardware():
    """Probe the I2C bus to decide which cube is attached.

    The Mini cube answers at 0x20; the Squid cube answers at 0x21-0x25. These
    don't overlap, so a quick (no-data) write to each address cleanly tells the
    two apart. Returns True for Mini, False for Squid. Falls back to Squid if
    nothing answers (e.g. cube unplugged)."""
    def present(addr):
        try:
            i2cbus.write_quick(addr)   # SMBus quick command: just address + ACK
            return True
        except (OSError, IOError):
            return False

    if present(mini_adrs):
        return True
    if any(present(a) for a in row_adrs):
        return False
    print("No cube detected on I2C - defaulting to Squid")
    return False

# Resolve the active hardware. Set MINI_OVERRIDE to skip detection.
MINI = MINI_OVERRIDE if MINI_OVERRIDE is not None else detect_hardware()
print(f"Hardware: {'Mini' if MINI else 'Squid'} cube")

def send_to_mini(led_data, brightness_val):
    """Mini cube: single board at 0x20, register 5*x+y, serpentine z ordering."""
    try:
        b_val = brightness_val.value if hasattr(brightness_val, 'value') else brightness_val
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
                i2cbus.write_i2c_block_data(mini_adrs, (5*x + y), payload)
    except Exception:
        # Suppress I2C errors to prevent spamming console during fast switching
        pass

def send_to_squid(led_data, brightness_val):
    """Squid cube: one board per layer at 0x21-0x25, register y."""
    try:
        b_val = brightness_val.value if hasattr(brightness_val, 'value') else brightness_val
        for x in range(5):
            for y in range(5):
                payload = []
                for z in range(5):
                    r = int(led_data[x][y][4-z][0] * b_val)
                    g = int(led_data[x][y][4-z][1] * b_val)
                    b = int(led_data[x][y][4-z][2] * b_val)
                    r = max(0, min(255, r))
                    g = max(0, min(255, g))
                    b = max(0, min(255, b))
                    payload.extend([r, g, b])
                i2cbus.write_i2c_block_data(row_adrs[x], y, payload)
    except Exception:
        # Suppress I2C errors to prevent spamming console during fast switching
        pass

def send_to_cube(led_data, brightness_val):
    """Dispatch to the active hardware output, selected by the MINI flag."""
    if MINI:
        send_to_mini(led_data, brightness_val)
    else:
        send_to_squid(led_data, brightness_val)

for i in range(5):
    leds[i][0][0][0] = 255
    leds[0][i][0][1] = 255
    leds[0][0][i][2] = 255

send_to_cube(leds, b_val)