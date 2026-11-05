"""Hardware smoke test: lights one LED along each axis.

    red   along x  (leds[i][0][0])
    green along y  (leds[0][i][0])
    blue  along z  (leds[0][0][i])

Handy for checking wiring and orientation on either cube. Uses the same
output layer as the server (cube_io.py), so what you see here is exactly the
mapping the patterns and games will get. Set CUBE_HARDWARE=mini or squid to
force a variant, otherwise the bus is probed.
"""
import logging

import numpy as np

import cube_io

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

leds = np.zeros((5, 5, 5, 3), dtype=np.int_)
brightness = 1.0

print(f"Hardware: {cube_io.hardware_name()} cube"
      f"{' (simulated - no I2C bus found)' if cube_io.SIMULATED else ''}")

for i in range(5):
    leds[i][0][0][0] = 255   # red along x
    leds[0][i][0][1] = 255   # green along y
    leds[0][0][i][2] = 255   # blue along z

cube_io.send_to_cube(leds, brightness)
