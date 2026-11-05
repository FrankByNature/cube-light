"""Game logic, driven directly with a fake clock and a recording sender."""
import numpy as np
import pytest

import cube_MP_server as server
from conftest import FakeValue


@pytest.fixture(autouse=True)
def _highscores_in_tmp(no_highscore_file):
    yield


# ---------------------------------------------------------------- Snake ----

def make_snake(leds, sender):
    return server.SnakeGame(leds, sender, FakeValue(1.0), FakeValue(0), FakeValue(False),
                            FakeValue(False), FakeValue(0))


def test_snake_wraps_around_edges(leds, sender):
    g = make_snake(leds, sender)
    g.start()
    g.snake = [[2, 2, 0], [2, 2, 1]]
    g.direction = g.next_direction = [0, 0, -1]
    g.food = [0, 0, 0]
    g.last_move_time = 0
    g.step(10.0)
    assert g.snake[0] == [2, 2, 4]


def test_snake_cannot_reverse_into_itself(leds, sender):
    g = make_snake(leds, sender)
    g.start()
    g.direction = [1, 0, 0]
    g.move('ny')            # ny is -x in the swapped mapping: a 180 turn
    assert g.next_direction == [0, 0, -1] or g.next_direction != [-1, 0, 0]
    g.move('pz')
    assert g.next_direction == [0, 0, 1]


def test_snake_eats_and_grows_and_scores(leds, sender):
    g = make_snake(leds, sender)
    g.start()
    g.snake = [[2, 2, 2], [2, 2, 3]]
    g.length = 2
    g.direction = g.next_direction = [0, 0, -1]
    g.food = [2, 2, 1]
    g.last_move_time = 0
    g.step(10.0)
    assert g.score == 1 and g.score_value.value == 1
    assert len(g.snake) == 3
    assert g.food != [2, 2, 1] and g.food not in g.snake


def test_snake_dies_on_self_collision(leds, sender):
    g = make_snake(leds, sender)
    g.start()
    # A loop the head is about to run into
    g.snake = [[2, 2, 2], [2, 3, 2], [2, 3, 1], [2, 2, 1], [2, 1, 1]]
    g.length = 5
    g.direction = g.next_direction = [0, 0, -1]   # head moves to [2, 2, 1]
    g.food = [0, 0, 0]
    g.last_move_time = 0
    g.step(10.0)
    assert g.game_over and not g.won
    assert g.game_over_value.value is True


def test_snake_win_fills_cube(leds, sender):
    g = make_snake(leds, sender)
    g.start()
    cells = [[x, y, z] for x in range(5) for y in range(5) for z in range(5)]
    g.snake = cells[1:]                  # 124 cells, head at cells[1]
    g.length = 124
    # Arrange the head to step onto the last free cell
    free = cells[0]
    g.snake[0] = [free[0], free[1], (free[2] + 1) % 5]
    g.direction = g.next_direction = [0, 0, -1]
    g.food = free
    g.purple_food = None
    g.last_move_time = 0
    g.step(10.0)
    assert g.won and g.game_over
    assert g.won_value.value is True


def test_snake_respawns_red_food_when_purple_expires(leds, sender):
    """Near a full cube the purple can hold the last free cell; when it
    expires the red food must come back or the game can never be won."""
    g = make_snake(leds, sender)
    g.start()
    cells = [[x, y, z] for x in range(5) for y in range(5) for z in range(5)]
    g.snake = cells[2:]                  # 123 cells
    g.length = 123
    g.food = None
    g.purple_food = cells[0]
    g.purple_spawn_time = 0.0
    g.direction = g.next_direction = [0, 0, -1]
    g.last_move_time = 100.0             # don't move this tick
    g.step(100.0 + g.PURPLE_LIFETIME + 0.1)
    assert g.purple_food is None
    assert g.food is not None and g.food not in g.snake


def test_snake_gradient_runs_head_to_tail(leds, sender):
    g = make_snake(leds, sender)
    g.snake = [[0, 0, 0], [0, 0, 1], [0, 0, 2]]
    assert g.snake_colour(0) == g.COLOUR_HEAD
    assert g.snake_colour(2) == g.COLOUR_TAIL


def test_snake_saves_highscore(leds, sender, no_highscore_file):
    g = make_snake(leds, sender)
    g.start()
    g.score = 7
    g._end_game(0.0, won=False)
    assert g.highscore.value == 7
    assert server.load_highscores()['snake'] == 7


# --------------------------------------------------------------- Planes ----

def make_planes(leds, sender):
    return server.PlanesGame(leds, sender, FakeValue(1.0), FakeValue(0), FakeValue(False), FakeValue(0))


def test_planes_player_stays_in_front_plane(leds, sender):
    g = make_planes(leds, sender)
    g.start()
    for _ in range(10):
        g.move('up')
        g.move('right')
    assert (g.player_y, g.player_z) == (4, 4)
    for _ in range(10):
        g.move('down')
        g.move('left')
    assert (g.player_y, g.player_z) == (0, 0)


def test_planes_hole_picker_always_has_a_candidate(leds, sender):
    g = make_planes(leds, sender)
    for group in range(6):
        for size_last, size_next in ((2, 2), (2, 1), (1, 2), (1, 1)):
            for pos in [(y, z) for y in range(6 - size_last) for z in range(6 - size_last)]:
                g.last_hole = (pos, size_last)
                hole = g._pick_hole(size_next, group)
                assert 0 <= hole[0] <= 5 - size_next and 0 <= hole[1] <= 5 - size_next


def test_planes_scores_when_dot_is_in_hole(leds, sender):
    g = make_planes(leds, sender)
    g.start()
    g.player_y, g.player_z = 1, 1
    g.planes = [{'mask': g._create_plane_mask(2, (1, 1)), 'hole': (1, 1), 'hole_size': 2,
                 'spawn_time': 0.0, 'colours': np.full((5, 5, 3), 255.0),
                 'moving': True, 'scored': False, 'x': 0.4}]
    g.spawned = 1
    g.last_update_time = 10.0
    # Velocity is 1.3 / 0.6 ~ 2.2 slices/s, so two 0.3 s steps carry the plane
    # from x = 0.4 through the player's slice to about x = -0.9
    g.step(10.3)
    assert not g.game_over
    g.step(10.6)
    assert g.score == 1 and not g.game_over


def test_planes_dies_when_dot_misses_hole(leds, sender):
    g = make_planes(leds, sender)
    g.start()
    g.player_y, g.player_z = 4, 4
    g.planes = [{'mask': g._create_plane_mask(2, (0, 0)), 'hole': (0, 0), 'hole_size': 2,
                 'spawn_time': 0.0, 'colours': np.full((5, 5, 3), 255.0),
                 'moving': True, 'scored': False, 'x': 0.6}]
    g.spawned = 1
    g.last_update_time = 10.0
    g.step(10.1)
    assert g.game_over and g.game_over_value.value is True
    # death animation runs, then the game deactivates itself
    for _ in range(200):
        g.step(10.2)
        if not g.active:
            break
    assert not g.active


def test_planes_difficulty_curve():
    assert server.PlanesGame._speed_for_score(0) == pytest.approx(1.3)
    assert server.PlanesGame._speed_for_score(40) == pytest.approx(2.5)
    assert server.PlanesGame._speed_for_score(400) == pytest.approx(2.5)
    assert server.PlanesGame._plane_gap_for_score(0) == 3
    assert server.PlanesGame._plane_gap_for_score(45) == 2
    assert server.PlanesGame._plane_gap_for_score(55) == 1
    assert server.PlanesGame._plane_gap_for_score(500) == 1


# -------------------------------------------------------------- Meteors ----

def make_meteors(leds, sender):
    return server.MeteorsGame(leds, sender, FakeValue(1.0), FakeValue(0), FakeValue(False), FakeValue(0))


def test_meteors_player_is_clamped_to_bottom_plane(leds, sender):
    g = make_meteors(leds, sender)
    g.start()
    for _ in range(10):
        g.move('up'); g.move('right')
    assert (g.player_x, g.player_y) == (4, 4)


def test_meteors_shot_destroys_meteor_and_scores(leds, sender):
    g = make_meteors(leds, sender)
    g.start()
    g.next_spawn_time = 1e9          # no random spawns
    g.player_x, g.player_y = 2, 2
    g.meteors = [{'x': 2, 'y': 2, 'z': 3, 'spawn_time': 0.0, 'falling': True,
                  'next_fall': 1e9, 'colour': [255, 100, 0]}]
    g.last_fire_time = -10
    g._fire(0.0)                     # move('fire') uses the wall clock; drive it directly
    assert len(g.shots) == 1 and g.shots[0]['z'] == 1
    # Shot climbs one cell per SHOT_INTERVAL (0.05 s): by t = 0.1 it is at z = 3
    g.step(0.1)
    assert g.score == 1 and g.meteors == [] and g.shots == []


def test_meteors_fire_cooldown(leds, sender):
    g = make_meteors(leds, sender)
    g.start()
    g._fire(1.0)
    g._fire(1.05)
    assert len(g.shots) == 1
    g._fire(1.0 + g.FIRE_COOLDOWN + 0.01)
    assert len(g.shots) == 2


def test_meteor_landing_ends_game(leds, sender):
    g = make_meteors(leds, sender)
    g.start()
    g.next_spawn_time = 1e9
    g.meteors = [{'x': 1, 'y': 1, 'z': 1, 'spawn_time': 0.0, 'falling': True,
                  'next_fall': 0.0, 'colour': [255, 100, 0]}]
    g.step(1.0)
    assert g.game_over and g.game_over_value.value is True
    assert sender.last[:, :, 0].max() > 0      # bottom plane glows red


def test_meteors_spawn_interval_shrinks_to_floor(leds, sender):
    g = make_meteors(leds, sender)
    g.start_time = 0.0
    assert g._spawn_interval(0.0) == pytest.approx(g.SPAWN_START)
    assert g._spawn_interval(60.0) < g.SPAWN_START
    assert g._spawn_interval(10000.0) == pytest.approx(g.SPAWN_MIN)
