import numpy as np

import effects


def test_burst_spawns_requested_count_with_chosen_colours():
    sparks = effects.spawn_burst((2, 2, 2), count=12, colours=[[255, 0, 0], [0, 255, 0]])
    assert len(sparks) == 12
    assert all(s.colour in ([255, 0, 0], [0, 255, 0]) for s in sparks)
    assert all(s.life == 1.0 for s in sparks)


def test_colour_jitter_stays_in_range():
    sparks = effects.spawn_burst((0, 0, 0), count=50, base_colour=[250, 5, 128], colour_jitter=30)
    for s in sparks:
        assert all(0 <= c <= 255 for c in s.colour)


def test_sparks_fade_out_and_are_dropped():
    sparks = effects.spawn_burst((2, 2, 2), count=5, base_colour=[255, 255, 255], fade=0.5)
    sparks = effects.update_sparks(sparks)
    assert len(sparks) == 5          # life 0.5
    sparks = effects.update_sparks(sparks)
    assert sparks == []              # life 0 -> gone


def test_render_is_additive_and_clipped():
    leds = np.zeros((5, 5, 5, 3), dtype=np.int_)
    leds[2, 2, 2] = [200, 200, 200]
    sparks = [effects.Spark((2, 2, 2), (0, 0, 0), [100, 0, 0])]
    effects.render_sparks(sparks, leds)
    assert list(leds[2, 2, 2]) == [255, 200, 200]


def test_sparks_outside_cube_are_not_drawn():
    leds = np.zeros((5, 5, 5, 3), dtype=np.int_)
    sparks = [effects.Spark((7, 2, 2), (0, 0, 0), [255, 255, 255])]
    effects.render_sparks(sparks, leds)
    assert leds.max() == 0


def test_gravity_and_die_below():
    s = effects.Spark((2, 2, 1), (0, 0, 0), [255, 255, 255], fade=0.0, gravity=1.0, die_below=0)
    s.update()           # z = 1, then vz = -1
    assert s.life > 0
    s.update()           # z = 0, vz = -2
    s.update()           # z = -2 -> below the floor
    assert s.life == 0.0
