"""Shared fixtures. The whole suite runs without a cube or an I2C bus: it
forces cube_io into simulated mode before anything imports it."""
import os
import sys

# Simulated bus, and the Squid mapping, regardless of what the machine has
os.environ['CUBE_SIMULATE'] = '1'
os.environ.setdefault('CUBE_HARDWARE', 'squid')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ctypes
import multiprocessing
import queue

import numpy as np
import pytest

import cube_MP_server as server


class FakeValue:
    """Stands in for a multiprocessing.Value inside one process."""
    def __init__(self, value):
        self.value = value


class Sender:
    """A send_to_cube replacement that records frames."""
    def __init__(self):
        self.frames = []

    def __call__(self, leds, brightness=1.0):
        self.frames.append(np.array(leds, copy=True))

    @property
    def last(self):
        return self.frames[-1]


@pytest.fixture
def leds():
    return np.zeros((5, 5, 5, 3), dtype=np.int_)


@pytest.fixture
def sender():
    return Sender()


@pytest.fixture
def no_highscore_file(tmp_path, monkeypatch):
    """Point the high-score file at a temp dir so tests never touch the real one."""
    path = tmp_path / 'highscores.json'
    monkeypatch.setattr(server, 'HIGHSCORES_FILE', str(path))
    return path


@pytest.fixture
def app(no_highscore_file):
    """A Flask test client plus the queue its routes write to. The 'runner'
    is just the queue here: tests assert on the commands produced."""
    cmd_queue = queue.Queue()
    ctx = multiprocessing.get_context('spawn')
    brightness = ctx.Value(ctypes.c_double, 1.0)
    frame = ctx.Array(ctypes.c_uint8, server.FRAME_BYTES)
    shared = {}
    for name in ('planes', 'snake', 'meteors'):
        shared[name + '_score'] = ctx.Value(ctypes.c_int, 0)
        shared[name + '_game_over'] = ctx.Value(ctypes.c_bool, False)
        shared[name + '_highscore'] = ctx.Value(ctypes.c_int, 0)
    shared['snake_won'] = ctx.Value(ctypes.c_bool, False)

    flask_app = server.create_flask_app(
        cmd_queue, brightness, frame,
        shared['planes_score'], shared['planes_game_over'], shared['planes_highscore'],
        shared['snake_score'], shared['snake_game_over'], shared['snake_won'], shared['snake_highscore'],
        shared['meteors_score'], shared['meteors_game_over'], shared['meteors_highscore'])
    flask_app.config['TESTING'] = True
    client = flask_app.test_client()

    def drain():
        items = []
        while True:
            try:
                items.append(cmd_queue.get_nowait())
            except queue.Empty:
                return items

    return {'client': client, 'queue': cmd_queue, 'drain': drain,
            'brightness': brightness, 'frame': frame, 'shared': shared}
