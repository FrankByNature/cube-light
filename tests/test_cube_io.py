"""The I2C output layer, exercised against the simulated bus."""
import numpy as np

import cube_io


def test_simulated_bus_is_in_use():
    assert cube_io.SIMULATED is True
    assert isinstance(cube_io.i2cbus, cube_io.SimBus)


def test_squid_writes_25_columns_and_commits_each_board_last():
    """Each board's register 4 must be the last write to that board, since
    that is what makes the firmware swap frames."""
    bus = cube_io.i2cbus
    writes = []
    bus.write_i2c_block_data = lambda addr, reg, data: writes.append((addr, reg, list(data)))
    try:
        leds = np.zeros((5, 5, 5, 3), dtype=np.int_)
        leds[1, 2, 3] = [10, 20, 30]
        cube_io.send_to_squid(leds, 1.0)
    finally:
        del bus.write_i2c_block_data   # restore the class method

    assert len(writes) == 25
    for x, addr in enumerate(cube_io.SQUID_ADDRESSES):
        regs = [reg for a, reg, _ in writes if a == addr]
        assert regs == [0, 1, 2, 3, 4]
    # Squid mapping: led_data[x][y][4-z] -> board x, register y, slot z
    addr, reg, data = next(w for w in writes if w[0] == cube_io.SQUID_ADDRESSES[1] and w[1] == 2)
    slot = 4 - 3
    assert data[slot * 3: slot * 3 + 3] == [10, 20, 30]
    assert all(len(d) == 15 for _, _, d in writes)


def test_mini_serpentine_mapping_and_commit_order():
    bus = cube_io.i2cbus
    writes = []
    bus.write_i2c_block_data = lambda addr, reg, data: writes.append((addr, reg, list(data)))
    try:
        leds = np.zeros((5, 5, 5, 3), dtype=np.int_)
        leds[2, 1, 4] = [255, 0, 0]   # x=2, pos=1, y=4
        cube_io.send_to_mini(leds, 1.0)
    finally:
        del bus.write_i2c_block_data

    assert [reg for _, reg, _ in writes] == list(range(25))   # register 24 last
    assert all(a == cube_io.MINI_ADDRESS for a, _, _ in writes)
    # Column (x=2, y=4): y even -> pos = 4 - z, so pos 1 is slot z = 3
    _, _, data = next(w for w in writes if w[1] == 5 * 2 + 4)
    assert data[3 * 3: 3 * 3 + 3] == [255, 0, 0]


def test_brightness_is_applied_and_clamped():
    bus = cube_io.i2cbus
    writes = []
    bus.write_i2c_block_data = lambda addr, reg, data: writes.append(list(data))
    try:
        leds = np.full((5, 5, 5, 3), 300, dtype=np.int_)   # over range on purpose
        cube_io.send_to_squid(leds, 0.5)
    finally:
        del bus.write_i2c_block_data
    assert all(v == 150 for data in writes for v in data)

    frame = cube_io.frame_bytes(leds, 0.5)
    assert frame.dtype == np.uint8 and frame.shape == (5, 5, 5, 3)
    assert frame.max() == 150


def test_frame_bytes_accepts_value_like_brightness():
    class V:
        value = 0.25
    leds = np.full((5, 5, 5, 3), 200, dtype=np.int_)
    assert cube_io.frame_bytes(leds, V()).max() == 50


def test_i2c_errors_are_swallowed():
    bus = cube_io.i2cbus

    def boom(addr, reg, data):
        raise OSError('bus error')
    bus.write_i2c_block_data = boom
    try:
        cube_io.send_to_cube(np.zeros((5, 5, 5, 3), dtype=np.int_), 1.0)   # must not raise
    finally:
        del bus.write_i2c_block_data
