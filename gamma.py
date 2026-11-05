#!/usr/bin/env python3
"""Generate the gamma16[256] lookup table pasted into the cube firmware.

Each entry is a 16-bit value: the upper byte is the integer LED output for a
linear 0-255 input after gamma 2.2 and a brightness cap, the lower byte is
the fractional remainder the firmware uses for temporal dithering.

The two cubes deliberately run at different brightness caps (different LEDs
and power budgets), so pick the table by cube name:

    python gamma.py mini      # cap 52  (~20% of full scale)
    python gamma.py squid     # cap 78  (~31% of full scale)
    python gamma.py --cap 64  # any other cap

Paste the output over the gamma16[256] initialiser in the matching sketch
(Mini_cube_rp2040/ or Squid_cube_rp2040/) and reflash.
"""
import argparse

# Brightness cap (integer output at input 255) per cube. Keep these in step
# with the comments above gamma16[] in each sketch.
CAPS = {'mini': 52, 'squid': 78}
GAMMA = 2.2


def gamma_table(cap, gamma=GAMMA):
    """Return the 256 16-bit entries for the given cap."""
    table = []
    for i in range(256):
        scaled = (i / 255.0) ** gamma * cap
        integer = int(scaled)
        fraction = int((scaled - integer) * 256)
        table.append((integer << 8) | fraction)
    return table


def format_table(table, per_row=16):
    rows = []
    for start in range(0, len(table), per_row):
        rows.append(', '.join(f'0x{v:04X}' for v in table[start:start + per_row]))
    return ',\n'.join(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('cube', nargs='?', choices=sorted(CAPS),
                    help='which cube to generate for (sets the cap)')
    ap.add_argument('--cap', type=int,
                    help='brightness cap 1-255 (overrides the cube default)')
    args = ap.parse_args()

    if args.cap is None and args.cube is None:
        ap.error('give a cube name (mini / squid) or --cap N')
    cap = args.cap if args.cap is not None else CAPS[args.cube]
    if not 1 <= cap <= 255:
        ap.error('--cap must be between 1 and 255')

    label = args.cube or 'custom'
    print(f'// gamma {GAMMA}, brightness cap {cap} (~{cap / 255:.0%} of full scale) - {label}')
    print('const uint16_t gamma16[256] = {')
    print(format_table(gamma_table(cap)))
    print('};')


if __name__ == '__main__':
    main()
