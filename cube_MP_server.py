from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import smbus
import multiprocessing
import numpy as np
import ctypes
import time
import signal
import sys
import threading
import random
import json
import os
import subprocess
from queue import Empty

# Persistent high scores, stored next to this script so it loads on startup
HIGHSCORES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'highscores.json')

def load_highscores():
    """Read persisted high scores; return zeroed defaults if missing or corrupt."""
    try:
        with open(HIGHSCORES_FILE, 'r') as f:
            data = json.load(f)
        return {'planes': int(data.get('planes', 0)), 'snake': int(data.get('snake', 0)),
                'meteors': int(data.get('meteors', 0))}
    except (FileNotFoundError, ValueError, OSError):
        return {'planes': 0, 'snake': 0, 'meteors': 0}

def save_highscore(game, value):
    """Persist one game's high score, preserving the other games' stored values."""
    scores = load_highscores()
    scores[game] = int(value)
    try:
        with open(HIGHSCORES_FILE, 'w') as f:
            json.dump(scores, f)
    except OSError:
        pass

# Set spawn context
if __name__ == '__main__':
    multiprocessing.set_start_method('spawn', force=True)

import patterns
import effects

# LED cube data structure
leds = np.zeros((5, 5, 5, 3), dtype=np.int_)

# Hardware select:
#   True  -> always Mini cube (single board @ 0x20)
#   False -> always Squid cube (five boards @ 0x21-0x25)
#   None  -> auto-detect from the I2C bus at startup (probe both cubes)
MINI_OVERRIDE = None

# I2C Setup
i2cbus = smbus.SMBus(1)
mini_adrs = 0x20
row_adrs = [0x21, 0x22, 0x23, 0x24, 0x25]

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

    if present(mini_adrs):
        return True
    if any(present(a) for a in row_adrs):
        return False
    if multiprocessing.current_process().name == 'MainProcess':
        print("No cube detected on I2C - defaulting to Squid")
    return False

# Resolve the active hardware once at import. Under the 'spawn' start method
# this runs in every process (main + each worker), since each re-imports this
# module and needs its own I2C handle. The result is printed only from the main
# process (see __main__) to avoid the same line appearing once per worker.
MINI = MINI_OVERRIDE if MINI_OVERRIDE is not None else detect_hardware()

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

def hsv_to_rgb(h, s, v):
    h = h % 360
    h = h / 60.0
    i = int(h)
    f = h - i
    p = v * (1 - s)
    q = v * (1 - s * f)
    t = v * (1 - s * (1 - f))

    if i == 0: r, g, b = v, t, p
    elif i == 1: r, g, b = q, v, p
    elif i == 2: r, g, b = p, v, t
    elif i == 3: r, g, b = p, q, v
    elif i == 4: r, g, b = t, p, v
    else: r, g, b = v, p, q

    return int(r * 255), int(g * 255), int(b * 255)


# ---------------------------------------------------------------------------
# Games
#
# Each game is a class with the same shape: start() / stop() / move(direction)
# / step(now), plus an `active` flag. cube_runner drives the active game by
# calling step() once per tick; step() mutates `leds` and pushes a frame with
# self.send(). When a game-over animation finishes, the game sets its own
# `active` False so the runner drops back to lazy command polling.
# ---------------------------------------------------------------------------

class PlanesGame:
    """Space Planes: one plane at a time spawns at the back and flies forward;
    the player dot moves around the front plane and must line up with the hole
    before the plane arrives.

    The player lives in the front plane (x = 0) and moves in y (left/right)
    and z (up/down). Planes are y-z cross sections that travel along x from
    the back of the cube (x = 4) towards the front (x = 0)."""

    FADE_DURATION = 1.0  # Time in seconds for a plane to fade in at the back

    # Planes glide smoothly: x is a float and a plane between two slices is
    # drawn across both, brightness split by how close it is to each.
    COLLIDE_RANGE = 0.5  # a plane hits the dot while |x| < this (nearest slice is 0)

    # The tenth plane is a scrolling rainbow
    RAINBOW_SPREAD = 40.0   # hue degrees between neighbouring diagonals
    RAINBOW_SCROLL = 240.0  # hue degrees per second (scrolls backwards)

    # Planes come in groups of 10: nine with a 2x2 hole, then one with a
    # single-pixel hole, then a short pause before the next (faster) group.
    GROUP_SIZE = 10
    GROUP_PAUSE = 1.5    # extra seconds of empty cube between groups

    def __init__(self, leds, send, brightness_value, score_value, game_over_value, highscore):
        self.leds = leds
        self.send = send
        self.brightness = brightness_value
        self.score_value = score_value
        self.game_over_value = game_over_value
        self.highscore = highscore
        self.active = False
        self.reset()

    def reset(self):
        self.player_y = 2       # left/right within the front plane
        self.player_z = 2       # up/down within the front plane
        self.player_color = [255, 255, 255]
        self.planes = []
        self.score = 0
        self.speed = 1.0
        self.game_over = False
        self.sparks = []
        self.flash_frames = 0   # white impact flash at the start of the death burst
        self.spawned = 0        # planes spawned so far (drives group position)
        self.last_hole = None   # (hole_pos, hole_size) of the previous plane
        now = time.time()
        self.last_spawn_time = now
        self.last_update_time = now

    @staticmethod
    def _hole_centre(pos, size):
        return (pos[0] + (size - 1) / 2.0, pos[1] + (size - 1) / 2.0)

    def _max_hole_distance(self, group):
        """How far (Chebyshev, between hole centres) the next hole may be from
        the last one: 1 cell in the first group, +0.5 each group after, up to
        the whole cube."""
        return min(4.0, 1.0 + 0.5 * group)

    def _pick_hole(self, hole_size, group):
        """Random hole position near the previous hole, never the same plane
        twice in a row."""
        positions = [(y, z) for y in range(6 - hole_size) for z in range(6 - hole_size)]
        if self.last_hole is None:
            return random.choice(positions)
        last_pos, last_size = self.last_hole
        ly, lz = self._hole_centre(last_pos, last_size)
        max_dist = self._max_hole_distance(group)
        candidates = []
        for pos in positions:
            if pos == last_pos and hole_size == last_size:
                continue
            cy, cz = self._hole_centre(pos, hole_size)
            if max(abs(cy - ly), abs(cz - lz)) <= max_dist:
                candidates.append(pos)
        return random.choice(candidates)

    @staticmethod
    def _create_plane_mask(hole_size, hole_pos):
        """(5, 5) bool mask of a y-z cross section with a hole_size x hole_size hole."""
        hole_y, hole_z = hole_pos
        mask = np.ones((5, 5), dtype=bool)
        mask[hole_y:hole_y + hole_size, hole_z:hole_z + hole_size] = False
        return mask

    def _rainbow_colours(self, age):
        """(5, 5, 3) rainbow across the plane's diagonals, scrolling backwards."""
        colours = np.empty((5, 5, 3))
        base = -self.RAINBOW_SCROLL * age
        for y in range(5):
            for z in range(5):
                colours[y, z] = hsv_to_rgb(base + (y + z) * self.RAINBOW_SPREAD, 1.0, 1.0)
        return colours

    @staticmethod
    def _speed_for_score(score):
        """Speed steps up every 10 planes survived: 130% -> 160% -> 190%
        -> 220% -> 250% (40), then caps. Beyond 40 the spacing tightens
        instead (see _plane_gap_for_score)."""
        return min(2.5, 1.3 + 0.3 * (score // 10))

    @staticmethod
    def _plane_gap_for_score(score):
        """X-slice gap required between consecutive planes: a new plane can
        start fading in once the previous one is this far forward from the
        back. 3 slices to start, then 2 from 40 and 1 from 50 once the speed
        has capped, so planes pack closer together."""
        if score < 40:
            return 3
        return max(1, 3 - (score - 30) // 10)

    @staticmethod
    def _velocity(speed):
        """Plane speed in slices per second."""
        return speed / 0.6

    def _dot_in_hole(self, obj):
        """True if the player dot is lined up with this plane's hole."""
        size = obj['hole_size']
        return (obj['hole'][0] <= self.player_y <= obj['hole'][0] + size - 1 and
                obj['hole'][1] <= self.player_z <= obj['hole'][1] + size - 1)

    def start(self):
        self.reset()
        self.active = True
        self.score_value.value = 0
        self.game_over_value.value = False
        print("Game started")

    def stop(self):
        self.active = False
        print("Game stopped")

    def move(self, direction):
        if not self.active or self.game_over:
            return
        # Movement is confined to the front plane (y = left/right, z = up/down)
        if direction == 'up' and self.player_z < 4:
            self.player_z += 1
        elif direction == 'down' and self.player_z > 0:
            self.player_z -= 1
        elif direction == 'left' and self.player_y > 0:
            self.player_y -= 1
        elif direction == 'right' and self.player_y < 4:
            self.player_y += 1

    def _trigger_explosion(self):
        """End the game with a firework burst where the player dot stands."""
        self.game_over = True
        self.game_over_value.value = True
        burst_colour = [random.randint(150, 255), random.randint(150, 255), random.randint(150, 255)]
        self.sparks = effects.spawn_burst(
            (0, self.player_y, self.player_z), count=18,
            base_colour=burst_colour, speed_range=(0.06, 0.20), fade=0.035)
        self.flash_frames = 2
        if self.score > self.highscore.value:
            self.highscore.value = self.score
            save_highscore('planes', self.score)
        print(f"Game Over! Score: {self.score}")

    def step(self, now):
        if not self.active:
            return

        if self.game_over:
            # Death animation: brief white flash, then the spark burst fades out
            self.leds[:] = 0
            if self.flash_frames > 0:
                self.leds[0, self.player_y, self.player_z] = [255, 255, 255]
                self.flash_frames -= 1
            self.sparks = effects.update_sparks(self.sparks)
            effects.render_sparks(self.sparks, self.leds)
            self.send(self.leds, self.brightness)
            if not self.sparks and self.flash_frames <= 0:
                self.active = False   # animation finished; the runner drops the game
            return

        # Difficulty is driven entirely by planes survived (score).
        self.speed = self._speed_for_score(self.score)

        # Spawn planes. A new plane may start fading in at the back once the
        # previous one has moved `gap` slices forward, so several can be in
        # flight at once. Each group of 10 waits for an empty cube plus a
        # short pause before it starts.
        spawn_interval = max(0.3, 0.6 / self.speed)
        gap = self._plane_gap_for_score(self.score)
        group, index = divmod(self.spawned, self.GROUP_SIZE)
        if index == 0 and self.spawned > 0:
            ready_to_spawn = (not self.planes and
                              now - self.last_spawn_time > spawn_interval + self.GROUP_PAUSE)
        elif self.planes:
            newest = self.planes[-1]
            ready_to_spawn = newest['moving'] and newest['x'] <= 4 - gap
        else:
            ready_to_spawn = now - self.last_spawn_time > spawn_interval
        if ready_to_spawn:
            final = index == self.GROUP_SIZE - 1
            hole_size = 1 if final else 2
            hole_pos = self._pick_hole(hole_size, group)
            # The first nine of each group step from red through the
            # spectrum; the tenth is a scrolling rainbow.
            hue = index / self.GROUP_SIZE * 360.0
            colours = np.empty((5, 5, 3))
            colours[:, :] = hsv_to_rgb(hue, 1.0, 1.0)

            self.planes.append({
                'mask': self._create_plane_mask(hole_size, hole_pos),
                'hole': hole_pos,
                'hole_size': hole_size,
                'spawn_time': now,
                'colours': colours,
                'rainbow': final,
                'moving': False,   # Starts moving forward after fade-in
                'scored': False,
                'x': 4.0           # Back of the cube
            })
            self.spawned += 1
            self.last_hole = (hole_pos, hole_size)
            self.last_spawn_time = now

        # Glide the planes forward, in proportion to the time since last frame
        dt = now - self.last_update_time
        self.last_update_time = now
        dx = self._velocity(self.speed) * dt
        survived = []
        for obj in self.planes:
            if not obj['moving']:
                # Begin moving once the fade-in has completed
                if now - obj['spawn_time'] >= self.FADE_DURATION:
                    obj['moving'] = True
                survived.append(obj)
                continue

            obj['x'] -= dx

            # Continuous collision: while the plane is nearest the player's
            # slice (x = 0) the dot must be lined up with the hole. Checked
            # every frame so moving onto the plane is also fatal.
            if abs(obj['x']) < self.COLLIDE_RANGE and not self._dot_in_hole(obj):
                self._trigger_explosion()
                return

            if not obj['scored'] and obj['x'] <= -self.COLLIDE_RANGE:
                # Passed through the player's slice with the dot in the hole
                # the whole time - a clean dodge, score it.
                obj['scored'] = True
                self.score += 1
                self.score_value.value = self.score

            if obj['x'] > -1.0:
                survived.append(obj)
            else:
                self.last_spawn_time = now  # fully gone; start the gap before the next plane
        self.planes = survived

        # Render: each plane is spread over the two slices either side of its
        # x, weighted by distance, so it glides rather than jumps.
        frame = np.zeros((5, 5, 5, 3))
        for obj in self.planes:
            if obj['rainbow']:
                obj['colours'] = self._rainbow_colours(now - obj['spawn_time'])
            plane = obj['colours'] * obj['mask'][:, :, None]
            if obj['moving']:
                lower = int(np.floor(obj['x']))
                frac = obj['x'] - lower
                slices = ((lower, 1.0 - frac), (lower + 1, frac))
            else:
                # Fading in at the back (x = 4)
                slices = ((4, min(1.0, (now - obj['spawn_time']) / self.FADE_DURATION)),)
            for sx, weight in slices:
                if 0 <= sx < 5 and weight > 0:
                    np.maximum(frame[sx], plane * weight, out=frame[sx])
        self.leds[:] = frame.astype(self.leds.dtype)

        # Draw the player dot in the front plane, last so it stays visible
        self.leds[0, self.player_y, self.player_z] = self.player_color

        self.send(self.leds, self.brightness)


class SnakeGame:
    """3D snake that wraps around the cube edges. Ends on self-collision - or
    is WON by filling every cell of the cube with the snake."""

    N = 5
    TOTAL_CELLS = N ** 3

    # Body is a gradient: green at the head fading to yellow at the tail
    COLOUR_HEAD = [0, 255, 0]
    COLOUR_TAIL = [255, 255, 0]
    COLOUR_FOOD = [255, 0, 0]
    COLOUR_PURPLE = [160, 0, 255]

    # Warm firework colours (reds / oranges / yellows) for the death explosion
    WARM_SPARK_COLOURS = [[255, 80, 0], [255, 160, 0], [255, 255, 0], [255, 40, 40]]
    # Eat-burst colours: red food explodes red, purple food explodes purple
    RED_SPARK_COLOURS = [[255, 0, 0], [255, 60, 0], [255, 30, 30], [200, 0, 0]]
    PURPLE_SPARK_COLOURS = [[160, 0, 255], [200, 80, 255], [120, 0, 200], [180, 40, 255]]

    # Bonus purple food: appears after every 5 red foods, lasts 10s (dims the
    # final 4s) and is worth 5 points
    PURPLE_LIFETIME = 10.0
    PURPLE_DIM_START = 6.0   # begin fading when 4 seconds remain

    DEATH_HOLD = 1.0              # red-head pause before the explosion
    DEATH_EXPLODE_INTERVAL = 0.12
    WIN_EXPLODE_INTERVAL = 0.03   # a win explodes up to 125 segments, so go fast

    # Maps a UI command to a direction unit vector
    DIRECTIONS = {
        # x and y axes swapped so the pad's right/left moves right/left
        # and up/down moves away/toward (z is unchanged)
        'px': [0, 1, 0], 'nx': [0, -1, 0],
        'py': [1, 0, 0], 'ny': [-1, 0, 0],
        'pz': [0, 0, 1], 'nz': [0, 0, -1],
    }

    def __init__(self, leds, send, brightness_value, score_value, game_over_value,
                 won_value, highscore):
        self.leds = leds
        self.send = send
        self.brightness = brightness_value
        self.score_value = score_value
        self.game_over_value = game_over_value
        self.won_value = won_value
        self.highscore = highscore
        self.active = False
        self.reset()

    def reset(self):
        self.snake = [[2, 2, 2]]         # list of [x, y, z], head first
        self.direction = [0, 0, -1]
        self.next_direction = [0, 0, -1]
        self.length = 2                  # head + 1 body segment
        self.score = 0
        self.speed = 0.75                # fixed at 75% (no UI selector)
        self.game_over = False
        self.won = False
        self.sparks = []                 # firework burst when food is eaten
        self.explode_index = 0           # next snake segment to blow up on game over
        self.explode_time = 0.0
        self.explode_interval = self.DEATH_EXPLODE_INTERVAL
        self.death_time = 0.0            # when the snake collided (head flashes red first)
        self.red_eaten = 0               # red foods consumed (every 5th spawns purple)
        self.food = None
        self.purple_food = None          # bonus food cell, or None when absent
        self.purple_spawn_time = 0.0     # when the current purple food appeared
        self.last_move_time = time.time()
        self.food = self._spawn_food()

    def snake_colour(self, i):
        """Gradient from green at the head (i == 0) to yellow at the tail."""
        n = len(self.snake)
        t = i / (n - 1) if n > 1 else 0.0
        return [int(round(self.COLOUR_HEAD[c] + (self.COLOUR_TAIL[c] - self.COLOUR_HEAD[c]) * t))
                for c in range(3)]

    def _free_cells(self):
        """Cells not occupied by the snake or either food."""
        occupied = len(self.snake)
        if self.food is not None:
            occupied += 1
        if self.purple_food is not None:
            occupied += 1
        return self.TOTAL_CELLS - occupied

    def _spawn_food(self):
        """Pick a random cell not occupied by the snake or the other food.
        Callers must check _free_cells() first so a free cell exists."""
        while True:
            cell = [random.randint(0, self.N - 1), random.randint(0, self.N - 1),
                    random.randint(0, self.N - 1)]
            if cell in self.snake:
                continue
            if cell == self.food:
                continue
            if self.purple_food is not None and cell == self.purple_food:
                continue
            return cell

    def start(self):
        self.reset()
        self.active = True
        self.score_value.value = 0
        self.game_over_value.value = False
        self.won_value.value = False
        print("Snake started")

    def stop(self):
        self.active = False
        print("Snake stopped")

    def move(self, direction):
        if not self.active or self.game_over:
            return
        new_dir = self.DIRECTIONS.get(direction)
        if new_dir:
            cur = self.direction
            # Ignore a 180 turn back into the neck
            if not (new_dir[0] == -cur[0] and new_dir[1] == -cur[1] and new_dir[2] == -cur[2]):
                self.next_direction = new_dir

    def _end_game(self, now, won):
        self.game_over = True
        self.won = won
        self.game_over_value.value = True
        self.won_value.value = won
        self.explode_index = 0
        self.explode_time = now
        self.death_time = now
        self.explode_interval = self.DEATH_EXPLODE_INTERVAL
        if won:
            # No red-head pause on a win, and the full-cube snake explodes fast
            self.death_time = now - self.DEATH_HOLD
            self.explode_interval = self.WIN_EXPLODE_INTERVAL
        if self.score > self.highscore.value:
            self.highscore.value = self.score
            save_highscore('snake', self.score)
        if won:
            print(f"Snake WINS - cube filled! Score: {self.score}")
        else:
            print(f"Snake Game Over! Score: {self.score}")

    def step(self, now):
        if not self.active:
            return
        if not self.game_over:
            self._step_play(now)
        elif now - self.death_time < self.DEATH_HOLD:
            self._render_death_hold()
        else:
            self._step_explosion(now)

    def _step_play(self, now):
        move_interval = max(0.08, 0.4 / self.speed)

        if now - self.last_move_time > move_interval:
            self.last_move_time = now
            self.direction = self.next_direction

            head = self.snake[0]
            new_head = [
                (head[0] + self.direction[0]) % self.N,
                (head[1] + self.direction[1]) % self.N,
                (head[2] + self.direction[2]) % self.N,
            ]

            ate_red = (new_head == self.food)
            ate_purple = (self.purple_food is not None and new_head == self.purple_food)
            will_grow = ate_red or ate_purple
            # The tail tip vacates on a normal move, so it isn't a collision unless growing
            occupied = self.snake if will_grow else self.snake[:-1]

            if new_head in occupied:
                self._end_game(now, won=False)
            else:
                self.snake.insert(0, new_head)
                if will_grow:
                    self.length += 1
                    # Firework burst where the food was eaten - red for red
                    # food, purple (and slightly bigger) for the bonus food
                    if ate_purple:
                        self.sparks += effects.spawn_burst(
                            new_head, count=random.randint(12, 15),
                            colours=self.PURPLE_SPARK_COLOURS,
                            speed_range=(0.25, 0.65), fade=0.1)
                    else:
                        self.sparks += effects.spawn_burst(
                            new_head, count=random.randint(5, 8),
                            colours=self.RED_SPARK_COLOURS,
                            speed_range=(0.15, 0.45), fade=0.12)
                    if ate_purple:
                        self.score += 5
                        self.purple_food = None
                    else:
                        self.score += 1
                        self.red_eaten += 1
                        # Only respawn food while a free cell exists (near a
                        # full cube there may be nowhere left to put it)
                        self.food = None
                        if self._free_cells() > 0:
                            self.food = self._spawn_food()
                        # Every 5th red food drops a timed bonus purple food
                        if (self.red_eaten % 5 == 0 and self.purple_food is None
                                and self._free_cells() > 0):
                            self.purple_food = self._spawn_food()
                            self.purple_spawn_time = now
                    self.score_value.value = self.score
                while len(self.snake) > self.length:
                    self.snake.pop()
                # Win: the snake fills every cell of the cube
                if len(self.snake) >= self.TOTAL_CELLS:
                    self._end_game(now, won=True)

        if self.game_over:
            return

        # Advance the eat-burst effect
        self.sparks = effects.update_sparks(self.sparks)

        # Expire the bonus purple food once its lifetime is up
        purple_fade = 1.0
        if self.purple_food is not None:
            purple_age = now - self.purple_spawn_time
            if purple_age >= self.PURPLE_LIFETIME:
                self.purple_food = None
            elif purple_age > self.PURPLE_DIM_START:
                # Linear fade to black over the last seconds
                purple_fade = ((self.PURPLE_LIFETIME - purple_age)
                               / (self.PURPLE_LIFETIME - self.PURPLE_DIM_START))

        # Render
        self.leds[:] = 0
        if self.food:
            fx, fy, fz = self.food
            self.leds[fx, fy, fz] = self.COLOUR_FOOD
        if self.purple_food is not None:
            px, py, pz = self.purple_food
            self.leds[px, py, pz] = [int(c * purple_fade) for c in self.COLOUR_PURPLE]
        for i, seg in enumerate(self.snake):
            self.leds[seg[0], seg[1], seg[2]] = self.snake_colour(i)
        effects.render_sparks(self.sparks, self.leds)

        self.send(self.leds, self.brightness)

    def _render_death_hold(self):
        # Hold for a moment with the head lit red before things blow up
        self.leds[:] = 0
        for i, seg in enumerate(self.snake):
            if i == 0:
                self.leds[seg[0], seg[1], seg[2]] = [255, 0, 0]
            else:
                self.leds[seg[0], seg[1], seg[2]] = self.snake_colour(i)
        self.send(self.leds, self.brightness)

    def _step_explosion(self, now):
        # Blow up each segment in turn, head -> tail, with a firework burst
        if (self.explode_index < len(self.snake)
                and now - self.explode_time > self.explode_interval):
            self.sparks += effects.spawn_burst(
                self.snake[self.explode_index], count=random.randint(10, 15),
                colours=self.WARM_SPARK_COLOURS,
                speed_range=(0.15, 0.45), fade=0.12)
            self.explode_index += 1
            self.explode_time = now

        self.sparks = effects.update_sparks(self.sparks)

        self.leds[:] = 0
        # Draw the segments that haven't exploded yet (current index through tail)
        for i in range(self.explode_index, len(self.snake)):
            seg = self.snake[i]
            self.leds[seg[0], seg[1], seg[2]] = self.snake_colour(i)
        effects.render_sparks(self.sparks, self.leds)

        self.send(self.leds, self.brightness)

        if self.explode_index >= len(self.snake) and not self.sparks:
            self.active = False   # animation finished; the runner drops the game


class MeteorsGame:
    """Meteor defence: meteors glow up in the top plane (z = 4), then fall
    straight down. The player is a blue dot in the bottom plane (z = 0) that
    fires green shots straight up; a shot that meets a meteor destroys it in a
    small firework. Any meteor that reaches the bottom plane ends the game.
    Meteors spawn faster the longer the game runs."""

    N = 5

    COLOUR_PLAYER = [0, 0, 255]
    COLOUR_SHOT = [0, 255, 0]

    CHARGE_TIME = 2.0      # seconds for a new meteor to brighten to full (like drips)
    FALL_INTERVAL = 0.5    # seconds per cell once falling
    SHOT_INTERVAL = 0.05   # seconds per cell for a shot - much faster than a meteor
    FIRE_COOLDOWN = 0.2    # minimum time between shots (stops key-repeat spam)

    # Spawn interval shrinks exponentially with elapsed time, down to a floor:
    # 2.5s at the start, ~1.85s after 30s, ~1.4s after 60s, floor after ~150s
    SPAWN_START = 2.5
    SPAWN_DECAY = 0.99     # per second of play
    SPAWN_MIN = 0.55

    # The first few meteors land in the centre 3x3 so new players can find them
    EASY_METEORS = 5

    # Tail cells above a falling meteor, as a fraction of its head brightness
    TAIL = (0.3, 0.1)

    HIT_SPARK_COLOURS = [[255, 140, 0], [255, 200, 40], [255, 80, 0], [255, 255, 120]]
    IMPACT_SPARK_COLOURS = [[255, 80, 0], [255, 160, 0], [255, 40, 0], [255, 220, 60]]
    GROUND_FLASH_TIME = 0.6   # the bottom plane glows red after an impact

    # Pad / keyboard directions -> (dx, dy) in the bottom plane. Matches the
    # snake pad: left/right moves along y, up/down moves along x.
    DIRECTIONS = {
        'up': (1, 0), 'down': (-1, 0),
        'left': (0, -1), 'right': (0, 1),
    }

    def __init__(self, leds, send, brightness_value, score_value, game_over_value, highscore):
        self.leds = leds
        self.send = send
        self.brightness = brightness_value
        self.score_value = score_value
        self.game_over_value = game_over_value
        self.highscore = highscore
        self.active = False
        self.reset()

    def reset(self):
        now = time.time()
        self.player_x = 2
        self.player_y = 2
        self.meteors = []
        self.shots = []
        self.sparks = []
        self.meteors_spawned = 0
        self.score = 0
        self.game_over = False
        self.impact_time = 0.0
        self.start_time = now
        self.next_spawn_time = now + 1.0   # brief pause before the first meteor
        self.last_fire_time = 0.0

    def start(self):
        self.reset()
        self.active = True
        self.score_value.value = 0
        self.game_over_value.value = False
        print("Meteors started")

    def stop(self):
        self.active = False
        print("Meteors stopped")

    def move(self, direction):
        if not self.active or self.game_over:
            return
        if direction == 'fire':
            self._fire(time.time())
            return
        delta = self.DIRECTIONS.get(direction)
        if delta:
            self.player_x = max(0, min(self.N - 1, self.player_x + delta[0]))
            self.player_y = max(0, min(self.N - 1, self.player_y + delta[1]))

    def _fire(self, now):
        if now - self.last_fire_time < self.FIRE_COOLDOWN:
            return
        self.last_fire_time = now
        # The shot appears just above the player and climbs from there
        self.shots.append({'x': self.player_x, 'y': self.player_y, 'z': 1,
                           'next_move': now + self.SHOT_INTERVAL})

    def _spawn_interval(self, now):
        elapsed = now - self.start_time
        return max(self.SPAWN_MIN, self.SPAWN_START * self.SPAWN_DECAY ** elapsed)

    def _spawn_meteor(self, now):
        """New meteor in a random top-plane column that isn't already charging.
        The first EASY_METEORS are kept to the centre 3x3."""
        busy = {(m['x'], m['y']) for m in self.meteors if m['z'] == self.N - 1}
        cells = range(1, self.N - 1) if self.meteors_spawned < self.EASY_METEORS else range(self.N)
        free = [(x, y) for x in cells for y in cells if (x, y) not in busy]
        if not free:
            return
        x, y = random.choice(free)
        self.meteors_spawned += 1
        self.meteors.append({
            'x': x, 'y': y, 'z': self.N - 1,
            'spawn_time': now,
            'falling': False,
            'next_fall': now + self.CHARGE_TIME + self.FALL_INTERVAL,
            # Orange with a little per-meteor variation in the green channel
            'colour': [255, random.randint(70, 130), random.randint(0, 12)],
        })

    def _end_game(self, meteor, now):
        self.game_over = True
        self.game_over_value.value = True
        self.impact_time = now
        self.sparks = effects.spawn_burst(
            (meteor['x'], meteor['y'], 0), count=20,
            colours=self.IMPACT_SPARK_COLOURS,
            speed_range=(0.1, 0.35), fade=0.04)
        self.meteors = []
        self.shots = []
        if self.score > self.highscore.value:
            self.highscore.value = self.score
            save_highscore('meteors', self.score)
        print(f"Meteors Game Over! Score: {self.score}")

    def step(self, now):
        if not self.active:
            return
        if self.game_over:
            self._step_game_over(now)
        else:
            self._step_play(now)

    def _step_play(self, now):
        # Spawn, slightly randomised around the current interval
        if now >= self.next_spawn_time:
            self._spawn_meteor(now)
            self.next_spawn_time = now + self._spawn_interval(now) * random.uniform(0.7, 1.3)

        # Shots climb quickly and vanish off the top
        for shot in self.shots:
            while now >= shot['next_move']:
                shot['z'] += 1
                shot['next_move'] += self.SHOT_INTERVAL
        self.shots = [s for s in self.shots if s['z'] < self.N]

        # Meteors finish charging, then fall one cell per FALL_INTERVAL
        for m in self.meteors:
            if not m['falling'] and now - m['spawn_time'] >= self.CHARGE_TIME:
                m['falling'] = True
            if m['falling'] and now >= m['next_fall']:
                m['z'] -= 1
                m['next_fall'] += self.FALL_INTERVAL

        # Collisions. Shots only rise and meteors only fall, so a shot at or
        # above a meteor in the same column has met it (even if they crossed
        # between frames). Each shot takes out the lowest meteor it reached.
        for shot in list(self.shots):
            reached = [m for m in self.meteors
                       if m['x'] == shot['x'] and m['y'] == shot['y'] and m['z'] <= shot['z']]
            if reached:
                target = min(reached, key=lambda m: m['z'])
                self.meteors.remove(target)
                self.shots.remove(shot)
                self.sparks += effects.spawn_burst(
                    (target['x'], target['y'], target['z']), count=random.randint(8, 12),
                    colours=self.HIT_SPARK_COLOURS,
                    speed_range=(0.15, 0.4), fade=0.1)
                self.score += 1
                self.score_value.value = self.score

        # A meteor that reaches the bottom plane ends the game
        for m in self.meteors:
            if m['z'] <= 0:
                self._end_game(m, now)
                self._step_game_over(now)
                return

        self.sparks = effects.update_sparks(self.sparks)

        # Render
        self.leds[:] = 0
        # Tails first so a meteor head is never overwritten by another's tail
        for m in self.meteors:
            if m['falling']:
                for i, frac in enumerate(self.TAIL, start=1):
                    tz = m['z'] + i
                    if tz < self.N:
                        # Tails burn redder and dimmer than the head
                        self.leds[m['x'], m['y'], tz] = [
                            int(m['colour'][0] * frac),
                            int(m['colour'][1] * 0.4 * frac), 0]
        for m in self.meteors:
            charge = min(1.0, (now - m['spawn_time']) / self.CHARGE_TIME)
            self.leds[m['x'], m['y'], m['z']] = self._flame_colour(m, charge)
        for shot in self.shots:
            self.leds[shot['x'], shot['y'], shot['z']] = self.COLOUR_SHOT
        self.leds[self.player_x, self.player_y, 0] = self.COLOUR_PLAYER
        effects.render_sparks(self.sparks, self.leds)

        self.send(self.leds, self.brightness)

    @staticmethod
    def _flame_colour(meteor, scale):
        """Meteor head colour with a per-frame flicker so it reads as flame."""
        flicker = scale * random.uniform(0.75, 1.0)
        r, g, b = meteor['colour']
        g = max(0, min(255, g + random.randint(-20, 20)))
        return [int(r * flicker), int(g * flicker), int(b * flicker)]

    def _step_game_over(self, now):
        # The bottom plane glows red and fades while the impact burst plays out
        self.leds[:] = 0
        glow = max(0.0, 1.0 - (now - self.impact_time) / self.GROUND_FLASH_TIME)
        if glow > 0:
            self.leds[:, :, 0] = [int(255 * glow), int(30 * glow), 0]
        self.sparks = effects.update_sparks(self.sparks)
        effects.render_sparks(self.sparks, self.leds)
        self.send(self.leds, self.brightness)
        if not self.sparks and glow <= 0:
            self.active = False   # animation finished; the runner drops the game


# ---------------------------------------------------------------------------
# The cube runner process: single owner of the LEDs and the I2C bus.
# ---------------------------------------------------------------------------

GAME_TICK = 0.02     # command-poll timeout while a game is active (~50 fps)
IDLE_TIMEOUT = 0.25  # lazy poll otherwise; a queued command still wakes get() instantly

def cube_runner(command_queue, brightness_value,
                planes_score, planes_game_over, planes_highscore,
                snake_score, snake_game_over, snake_won, snake_highscore,
                meteors_score, meteors_game_over, meteors_highscore):
    """Runs patterns (in a daemon thread) and games (stepped inline from the
    command loop). Everything that writes the cube lives in this one process,
    so a pattern and a game can never interleave frames on the I2C bus."""
    print("Cube runner started")

    leds_lock = threading.Lock()
    current_pattern_thread = None

    # Plain dict, not a multiprocessing.Value: the flag is only shared between
    # this loop and the pattern thread, which live in the same process.
    pattern_running = {'value': False}

    def check_running():
        return pattern_running['value']

    def safe_send(data, brightness):
        with leds_lock:
            send_to_cube(data, brightness)

    games = {
        'planes': PlanesGame(leds, safe_send, brightness_value,
                             planes_score, planes_game_over, planes_highscore),
        'snake': SnakeGame(leds, safe_send, brightness_value,
                           snake_score, snake_game_over, snake_won, snake_highscore),
        'meteors': MeteorsGame(leds, safe_send, brightness_value,
                               meteors_score, meteors_game_over, meteors_highscore),
    }
    active_game = None

    def clear_cube():
        with leds_lock:
            leds[:] = 0
            send_to_cube(leds, brightness_value.value)

    def stop_pattern_thread():
        nonlocal current_pattern_thread
        pattern_running['value'] = False
        if current_pattern_thread and current_pattern_thread.is_alive():
            current_pattern_thread.join(timeout=1.0)
        current_pattern_thread = None

    def stop_active_game(clear=True):
        nonlocal active_game
        if active_game is not None:
            games[active_game].stop()
            active_game = None
        if clear:
            clear_cube()

    while True:
        # Block until a command arrives. While a game is active, wake at the
        # game tick rate so it keeps animating; otherwise poll lazily (the
        # timeout is not a command latency - get() returns immediately when
        # something is queued).
        try:
            cmd = command_queue.get(timeout=GAME_TICK if active_game else IDLE_TIMEOUT)
        except Empty:
            cmd = None

        if cmd is not None:
            ctype = cmd['type']

            if ctype == 'stop':
                # Stop everything: pattern and game alike
                stop_pattern_thread()
                stop_active_game(clear=cmd.get('clear', True))

            elif ctype == 'pattern':
                stop_pattern_thread()
                stop_active_game(clear=False)

                pattern_name = cmd['pattern']
                if pattern_name in patterns.PATTERNS:
                    print(f"Starting pattern: {pattern_name}")
                    pattern_running['value'] = True
                    current_pattern_thread = threading.Thread(
                        target=patterns.PATTERNS[pattern_name],
                        args=(
                            leds,
                            lambda data: safe_send(data, brightness_value),
                            check_running,
                            hsv_to_rgb
                        ),
                        daemon=True  # Ensure thread dies if main process dies
                    )
                    current_pattern_thread.start()

            elif ctype == 'fill':
                stop_pattern_thread()
                stop_active_game(clear=False)
                with leds_lock:
                    leds[:, :, :] = [cmd['r'], cmd['g'], cmd['b']]
                    send_to_cube(leds, brightness_value.value)

            elif ctype == 'brightness':
                # Patterns and games re-send every frame with live brightness;
                # only a static display needs an explicit refresh.
                if not pattern_running['value'] and active_game is None:
                    with leds_lock:
                        send_to_cube(leds, brightness_value.value)

            elif ctype == 'wave_color':
                patterns.wave_state['color'] = [cmd['r'], cmd['g'], cmd['b']]

            elif ctype == 'wave_mode':
                patterns.wave_state['color_mode'] = cmd['mode']

            elif ctype == 'wave_axis':
                patterns.wave_state['axis'] = cmd['axis']

            elif ctype == 'tune':
                # Generic live-tuning: set patterns.<state>[<key>] = <value>.
                # Routes are generated from the TUNABLES table (see create_flask_app).
                getattr(patterns, cmd['state'])[cmd['key']] = cmd['value']

            elif ctype == 'game_start':
                stop_pattern_thread()
                stop_active_game(clear=False)
                name = cmd['game']
                if name in games:
                    games[name].start()
                    active_game = name

            elif ctype == 'game_stop':
                if active_game == cmd['game']:
                    stop_active_game(clear=cmd.get('clear', True))
                elif (active_game is None and not pattern_running['value']
                        and cmd.get('clear', True)):
                    # The game already ended its own death animation; just make
                    # sure the cube is dark.
                    clear_cube()

            elif ctype == 'game_move':
                if active_game == cmd['game']:
                    games[active_game].move(cmd['direction'])

        if active_game is not None:
            game = games[active_game]
            game.step(time.time())
            if not game.active:
                # The game finished its own game-over animation
                active_game = None


def create_flask_app(command_queue, brightness_value,
                     planes_score, planes_game_over, planes_highscore,
                     snake_score, snake_game_over, snake_won, snake_highscore,
                     meteors_score, meteors_game_over, meteors_highscore,
                     initial_brightness=100):
    app = Flask(__name__)
    CORS(app)

    @app.route('/')
    def index():
        return render_template('index.html', initial_brightness=initial_brightness)

    @app.route('/cube/brightness', methods=['POST'])
    def set_brightness():
        data = request.json or {}
        try:
            brightness = int(data.get('brightness', 100))
        except (TypeError, ValueError):
            brightness = 100
        brightness = max(0, min(100, brightness))   # clamp to 0-100%
        brightness_value.value = brightness / 100.0
        command_queue.put({'type': 'brightness'})
        return jsonify({'status': 'success', 'brightness': brightness})

    def _clamp_channel(v):
        try:
            return max(0, min(255, int(v)))
        except (TypeError, ValueError):
            return 0

    @app.route('/cube/fill', methods=['POST'])
    def fill_cube():
        data = request.json or {}
        red = _clamp_channel(data.get('red', 0))
        green = _clamp_channel(data.get('green', 0))
        blue = _clamp_channel(data.get('blue', 0))

        command_queue.put({
            'type': 'fill',
            'r': red,
            'g': green,
            'b': blue
        })

        return jsonify({'status': 'success', 'colour': {'r': red, 'g': green, 'b': blue}})

    @app.route('/cube/off', methods=['POST'])
    def turn_off():
        command_queue.put({'type': 'stop'})
        return jsonify({'status': 'off'})

    @app.route('/system/shutdown', methods=['POST'])
    def shutdown_pi():
        # Clear the cube before the Pi goes down; SIGTERM handler in __main__
        # does the same on `systemctl stop`, but shutdown is triggered by us here.
        command_queue.put({'type': 'stop'})
        try:
            subprocess.Popen(['sudo', 'shutdown', '-h', 'now'])
        except OSError as e:
            return jsonify({'status': 'error', 'message': str(e)}), 500
        return jsonify({'status': 'shutting down'})

    @app.route('/pattern/start', methods=['POST'])
    def start_pattern_route():
        data = request.json
        pattern_name = data.get('pattern')

        # cube_runner stops any running game itself before starting the pattern
        command_queue.put({
            'type': 'pattern',
            'pattern': pattern_name
        })

        return jsonify({'status': 'success', 'pattern': pattern_name})

    @app.route('/pattern/stop', methods=['POST'])
    def stop_pattern_route():
        command_queue.put({'type': 'stop'})
        return jsonify({'status': 'stopped'})

    @app.route('/wave/color', methods=['POST'])
    def set_wave_color():
        data = request.json
        red = data.get('red', 0)
        green = data.get('green', 0)
        blue = data.get('blue', 0)

        command_queue.put({
            'type': 'wave_color',
            'r': red,
            'g': green,
            'b': blue
        })

        return jsonify({'status': 'success', 'color': {'r': red, 'g': green, 'b': blue}})

    @app.route('/wave/mode', methods=['POST'])
    def set_wave_mode():
        data = request.json
        mode = data.get('mode', 'solid')

        command_queue.put({
            'type': 'wave_mode',
            'mode': mode
        })

        return jsonify({'status': 'success', 'mode': mode})

    @app.route('/wave/axis', methods=['POST'])
    def set_wave_axis():
        data = request.json
        axis = data.get('axis', 'z')
        if axis not in ('x', 'z'):
            axis = 'z'

        command_queue.put({
            'type': 'wave_axis',
            'axis': axis
        })

        return jsonify({'status': 'success', 'axis': axis})

    # ------------------------------------------------------------------
    # Live pattern-tuning routes, generated from one table.
    # Each entry: (url, json_key, patterns_state_attr, state_key, default, lo, hi)
    # The route reads <json_key> from the POST body, clamps it to [lo, hi],
    # and sends {'type':'tune', ...} which cube_runner applies as
    # patterns.<attr>[<key>] = value. To add a tunable, add a row here, a
    # matching key in the patterns.py state dict, and a UI slider.
    # ------------------------------------------------------------------
    TUNABLES = [
        ('/wave/rainbow/speed',  'speed',              'wave_state',      'rainbow_speed',        5,   1,   20),
        ('/wave/rainbow/spread', 'spread',             'wave_state',      'rainbow_spread',       60,  10,  120),
        ('/wave/speed',          'speed',              'wave_state',      'wave_speed',           3,   1,   10),
        ('/rain/intensity',      'intensity',          'rain_state',      'intensity',            5,   1,   10),
        ('/drips/spawn',         'spawn_chance',       'drips_state',     'spawn_chance',         5,   1,   10),
        ('/fireworks/launch',    'launch_probability', 'fireworks_state', 'launch_probability',   4,   1,   10),
        ('/fireworks/max',       'max_fireworks',      'fireworks_state', 'max_fireworks',        1,   1,   5),
        ('/fireworks/size',      'explosion_size',     'fireworks_state', 'explosion_size',       5,   1,   10),
        ('/rainbow/speed',       'speed',              'rainbow_state',   'speed',                5,   1,   20),
        ('/rainbow/spread',      'spread',             'rainbow_state',   'spread',               60,  10,  120),
        ('/xms/refractory',      'value',              'xms_state',       'refractory_period',    6,   1,   20),
        ('/xms/threshold',       'value',              'xms_state',       'excitation_threshold', 5,   1,   20),
        ('/xms/trigger',         'value',              'xms_state',       'trigger_probability',  0.003, 0.0, 1.0),
        ('/flame/base_temp',     'value',              'flame_state',     'base_temp',            0.5, 0.0, 2.0),
        ('/flame/var_temp',      'value',              'flame_state',     'var_temp',             0.5, 0.0, 2.0),
        ('/lava/speed',          'value',              'lava_state',      'speed',                5,   1,   20),
        ('/lava/size',           'value',              'lava_state',      'size',                 5,   1,   10),
        ('/lava/count',          'value',              'lava_state',      'count',                3,   1,   6),
        ('/ocean/speed',         'value',              'ocean_state',     'speed',                5,   1,   10),
        ('/ocean/height',        'value',              'ocean_state',     'height',               5,   1,   10),
        ('/spiral/speed',        'value',              'spiral_state',    'speed',                5,   1,   10),
        ('/spiral/radius',       'value',              'spiral_state',    'radius',               2.0, 0.5, 2.0),
        ('/spiral/fall',         'value',              'spiral_state',    'fall_speed',           5,   1,   10),
        ('/spiral/color_speed',  'value',              'spiral_state',    'color_speed',          5,   1,   10),
        ('/shimmer/intensity',   'value',              'shimmer_state',   'intensity',            5,   1,   10),
    ]

    def make_tunable_route(json_key, state_attr, state_key, default, lo, hi):
        cast = float if isinstance(default, float) else int
        def handler():
            data = request.json or {}
            try:
                value = cast(data.get(json_key, default))
            except (TypeError, ValueError):
                value = default
            value = max(lo, min(hi, value))   # clamp to the slider's range
            command_queue.put({'type': 'tune', 'state': state_attr,
                               'key': state_key, 'value': value})
            return jsonify({'status': 'success', json_key: value})
        return handler

    for url, json_key, state_attr, state_key, default, lo, hi in TUNABLES:
        app.add_url_rule(
            url,
            endpoint='tune_' + state_attr + '_' + state_key,
            view_func=make_tunable_route(json_key, state_attr, state_key, default, lo, hi),
            methods=['POST'],
        )

    # Non-scalar tunables, generated the same way. Both still emit the generic
    # 'tune' command - cube_runner just assigns whatever value it carries, so a
    # [r, g, b] list or a mode string needs no extra branch there.
    # Colour: (url, patterns_state_attr, state_key); body is {red, green, blue}.
    COLOUR_TUNABLES = [
        ('/lava/color',   'lava_state',   'color'),
        ('/spiral/color', 'spiral_state', 'color'),
        ('/shimmer/color',  'shimmer_state', 'color'),
        ('/shimmer/color2', 'shimmer_state', 'color2'),
    ]

    # Mode: (url, json_key, state_attr, state_key, allowed values, default)
    MODE_TUNABLES = [
        ('/lava/mode',   'mode', 'lava_state',   'color_mode', ('solid', 'multi', 'rainbow'), 'solid'),
        ('/spiral/mode', 'mode', 'spiral_state', 'color_mode', ('solid', 'rainbow'),          'rainbow'),
    ]

    def make_colour_route(state_attr, state_key):
        def handler():
            data = request.json or {}
            colour = [_clamp_channel(data.get('red', 0)),
                      _clamp_channel(data.get('green', 0)),
                      _clamp_channel(data.get('blue', 0))]
            command_queue.put({'type': 'tune', 'state': state_attr,
                               'key': state_key, 'value': colour})
            return jsonify({'status': 'success', 'color': colour})
        return handler

    def make_mode_route(json_key, state_attr, state_key, allowed, default):
        def handler():
            data = request.json or {}
            mode = data.get(json_key, default)
            if mode not in allowed:
                mode = default
            command_queue.put({'type': 'tune', 'state': state_attr,
                               'key': state_key, 'value': mode})
            return jsonify({'status': 'success', json_key: mode})
        return handler

    for url, state_attr, state_key in COLOUR_TUNABLES:
        app.add_url_rule(
            url,
            endpoint='tune_' + state_attr + '_' + state_key,
            view_func=make_colour_route(state_attr, state_key),
            methods=['POST'],
        )

    for url, json_key, state_attr, state_key, allowed, default in MODE_TUNABLES:
        app.add_url_rule(
            url,
            endpoint='tune_' + state_attr + '_' + state_key,
            view_func=make_mode_route(json_key, state_attr, state_key, allowed, default),
            methods=['POST'],
        )

    # Game routes. cube_runner enforces mutual exclusion (starting one game or
    # a pattern stops whatever else is running), so routes just queue commands.
    @app.route('/planes/start', methods=['POST'])
    def start_planes():
        command_queue.put({'type': 'game_start', 'game': 'planes'})
        return jsonify({'status': 'started'})

    @app.route('/planes/stop', methods=['POST'])
    def stop_planes():
        command_queue.put({'type': 'game_stop', 'game': 'planes'})
        return jsonify({'status': 'stopped'})

    @app.route('/planes/move', methods=['POST'])
    def move_planes():
        data = request.json or {}
        direction = data.get('direction')
        command_queue.put({'type': 'game_move', 'game': 'planes', 'direction': direction})
        return jsonify({'status': 'moved', 'direction': direction})

    @app.route('/planes/state', methods=['GET'])
    def get_planes_state():
        return jsonify({'score': planes_score.value, 'game_over': planes_game_over.value,
                        'highscore': planes_highscore.value})

    # Snake game routes
    @app.route('/snake/start', methods=['POST'])
    def start_snake():
        command_queue.put({'type': 'game_start', 'game': 'snake'})
        return jsonify({'status': 'started'})

    @app.route('/snake/stop', methods=['POST'])
    def stop_snake():
        command_queue.put({'type': 'game_stop', 'game': 'snake'})
        return jsonify({'status': 'stopped'})

    @app.route('/snake/move', methods=['POST'])
    def move_snake():
        data = request.json or {}
        direction = data.get('direction')
        command_queue.put({'type': 'game_move', 'game': 'snake', 'direction': direction})
        return jsonify({'status': 'moved', 'direction': direction})

    @app.route('/snake/state', methods=['GET'])
    def get_snake_state():
        return jsonify({'score': snake_score.value, 'game_over': snake_game_over.value,
                        'won': snake_won.value, 'highscore': snake_highscore.value})

    # Meteors game routes. Firing is a move with direction 'fire'; /meteors/fire
    # is a shortcut for clients that want a dedicated button endpoint.
    @app.route('/meteors/start', methods=['POST'])
    def start_meteors():
        command_queue.put({'type': 'game_start', 'game': 'meteors'})
        return jsonify({'status': 'started'})

    @app.route('/meteors/stop', methods=['POST'])
    def stop_meteors():
        command_queue.put({'type': 'game_stop', 'game': 'meteors'})
        return jsonify({'status': 'stopped'})

    @app.route('/meteors/move', methods=['POST'])
    def move_meteors():
        data = request.json or {}
        direction = data.get('direction')
        command_queue.put({'type': 'game_move', 'game': 'meteors', 'direction': direction})
        return jsonify({'status': 'moved', 'direction': direction})

    @app.route('/meteors/fire', methods=['POST'])
    def fire_meteors():
        command_queue.put({'type': 'game_move', 'game': 'meteors', 'direction': 'fire'})
        return jsonify({'status': 'fired'})

    @app.route('/meteors/state', methods=['GET'])
    def get_meteors_state():
        return jsonify({'score': meteors_score.value, 'game_over': meteors_game_over.value,
                        'highscore': meteors_highscore.value})

    return app

if __name__ == '__main__':
    INITIAL_BRIGHTNESS = 100

    # Create shared objects (only what actually crosses the process boundary:
    # Flask writes brightness and reads scores; cube_runner does the reverse)
    ctx = multiprocessing.get_context('spawn')
    command_queue = ctx.Queue()
    brightness_value = ctx.Value(ctypes.c_double, INITIAL_BRIGHTNESS / 100.0)
    planes_score = ctx.Value(ctypes.c_int, 0)
    planes_game_over = ctx.Value(ctypes.c_bool, False)
    snake_score = ctx.Value(ctypes.c_int, 0)
    snake_game_over = ctx.Value(ctypes.c_bool, False)
    snake_won = ctx.Value(ctypes.c_bool, False)
    meteors_score = ctx.Value(ctypes.c_int, 0)
    meteors_game_over = ctx.Value(ctypes.c_bool, False)

    # Load persisted high scores from disk so they survive restarts
    saved_highscores = load_highscores()
    planes_highscore = ctx.Value(ctypes.c_int, saved_highscores['planes'])
    snake_highscore = ctx.Value(ctypes.c_int, saved_highscores['snake'])
    meteors_highscore = ctx.Value(ctypes.c_int, saved_highscores['meteors'])
    print(f"Loaded high scores - Planes: {saved_highscores['planes']}, "
          f"Snake: {saved_highscores['snake']}, Meteors: {saved_highscores['meteors']}")

    print(f"Hardware: {'Mini' if MINI else 'Squid'} cube")
    print("Starting LED Cube Controller...")
    print("Main process: Flask web server on port 5000")
    print("Child process: cube runner (patterns + games)")

    cube_process = ctx.Process(target=cube_runner, args=(
        command_queue, brightness_value,
        planes_score, planes_game_over, planes_highscore,
        snake_score, snake_game_over, snake_won, snake_highscore,
        meteors_score, meteors_game_over, meteors_highscore))
    cube_process.start()

    app = create_flask_app(command_queue, brightness_value,
                           planes_score, planes_game_over, planes_highscore,
                           snake_score, snake_game_over, snake_won, snake_highscore,
                           meteors_score, meteors_game_over, meteors_highscore,
                           INITIAL_BRIGHTNESS)

    def cleanup():
        print("\nShutting down...")
        cube_process.terminate()
        cube_process.join(timeout=2)
        try:
            leds[:] = 0
            send_to_cube(leds, 1.0)
        except Exception:
            pass
        print("Server stopped")

    def signal_handler(sig, frame):
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        cleanup()
    except Exception as e:
        print(f"Error: {e}")
