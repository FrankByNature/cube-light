"""Every pattern runs for a handful of frames without error, writes only
valid colour values, and stops when is_running() turns False."""
import time

import numpy as np
import pytest

import patterns
from cube_MP_server import hsv_to_rgb


def run_pattern(func, frames, leds, sender):
    """Run `func` until it has sent `frames` frames, then stop it."""
    counter = {'n': 0}

    def send(data):
        sender(data)
        counter['n'] += 1

    def is_running():
        return counter['n'] < frames

    # Patterns sleep between frames; shorten the wait
    real_sleep = time.sleep
    time.sleep = lambda s: real_sleep(0)
    try:
        func(leds, send, is_running, hsv_to_rgb)
    finally:
        time.sleep = real_sleep
    return counter['n']


@pytest.mark.parametrize('name', sorted(patterns.PATTERNS))
def test_pattern_runs_and_stops(name, leds, sender):
    n = run_pattern(patterns.PATTERNS[name], 15, leds, sender)
    assert n == 15, "pattern should stop as soon as is_running() is False"
    assert len(sender.frames) == 15
    for frame in sender.frames:
        assert frame.shape == (5, 5, 5, 3)
        assert frame.min() >= 0 and frame.max() <= 255


@pytest.mark.parametrize('name', sorted(patterns.PATTERNS))
def test_pattern_eventually_lights_something(name, leds, sender):
    # 60 frames at the pattern's own cadence; even the sparsest (drips,
    # fireworks at low probability) light a cell within that many
    run_pattern(patterns.PATTERNS[name], 60, leds, sender)
    assert any(f.max() > 0 for f in sender.frames), f"{name} never lit a cell"


def test_wave_reads_live_tuning(leds, sender):
    patterns.wave_state['color_mode'] = 'solid'
    patterns.wave_state['color'] = [0, 0, 200]
    try:
        run_pattern(patterns.pattern_wave, 3, leds, sender)
        frame = sender.last
        assert frame[..., 0].max() == 0 and frame[..., 1].max() == 0
        assert frame[..., 2].max() > 0
    finally:
        patterns.wave_state['color_mode'] = 'rainbow'
        patterns.wave_state['color'] = [0, 255, 255]


def test_lava_tracks_blob_count_slider(leds, sender):
    patterns.lava_state['count'] = 1
    try:
        run_pattern(patterns.pattern_lava, 2, leds, sender)
    finally:
        patterns.lava_state['count'] = 3
    assert sender.last.max() > 0


def test_hsv_to_rgb_primaries():
    assert hsv_to_rgb(0, 1, 1) == (255, 0, 0)
    assert hsv_to_rgb(120, 1, 1) == (0, 255, 0)
    assert hsv_to_rgb(240, 1, 1) == (0, 0, 255)
    assert hsv_to_rgb(360, 1, 1) == (255, 0, 0)     # wraps
    assert hsv_to_rgb(60, 1, 0) == (0, 0, 0)        # black at zero value


def test_vectorised_rainbow_matches_scalar():
    hues = np.arange(0, 360, 7.5)
    vec = (patterns._hsv_rainbow_rgb(hues) * 255).astype(int)
    for h, rgb in zip(hues, vec):
        assert tuple(rgb) == hsv_to_rgb(h, 1.0, 1.0)
