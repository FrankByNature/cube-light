"""Shared particle-burst ("firework") effects.

One implementation used everywhere a spherical burst of sparks is drawn:
the fireworks pattern (patterns.py), the snake's eat/death bursts and the
planes death explosion (cube_MP_server.py).

A Spark is a point particle with a velocity, an optional downward gravity
pull, and a life in [0, 1] that fades linearly each frame. Rendering scales
the spark's colour by its remaining life and draws additively into the cube.
"""
import random

import numpy as np


class Spark:
    def __init__(self, pos, vel, colour, fade=0.1, gravity=0.0, die_below=None):
        self.p = [float(pos[0]), float(pos[1]), float(pos[2])]
        self.v = [float(vel[0]), float(vel[1]), float(vel[2])]
        self.colour = [int(c) for c in colour]
        self.life = 1.0
        self.fade = fade            # life lost per frame
        self.gravity = gravity      # z-velocity lost per frame
        self.die_below = die_below  # kill the spark if it falls below this z

    def update(self):
        self.p[0] += self.v[0]
        self.p[1] += self.v[1]
        self.p[2] += self.v[2]
        self.v[2] -= self.gravity
        self.life -= self.fade
        if self.die_below is not None and self.p[2] < self.die_below:
            self.life = 0.0


def spawn_burst(pos, count, base_colour=None, colours=None, colour_jitter=0,
                speed_range=(0.15, 0.45), fade=0.1, gravity=0.0, die_below=None):
    """Spherical burst of `count` sparks centred on `pos` (cube coords).

    Colour comes from `colours` (a random pick per spark) or from
    `base_colour`, optionally jittered by +/- `colour_jitter` per channel.
    Returns the list of new Sparks (callers typically += it onto their own).
    """
    sparks = []
    for _ in range(count):
        theta = random.uniform(0, 2 * np.pi)
        phi = random.uniform(0, np.pi)
        speed = random.uniform(*speed_range)
        vel = (speed * np.sin(phi) * np.cos(theta),
               speed * np.sin(phi) * np.sin(theta),
               speed * np.cos(phi))

        if colours is not None:
            colour = list(random.choice(colours))
        elif colour_jitter:
            colour = [min(255, max(0, c + random.randint(-colour_jitter, colour_jitter)))
                      for c in base_colour]
        else:
            colour = list(base_colour)

        sparks.append(Spark(pos, vel, colour, fade=fade, gravity=gravity,
                            die_below=die_below))
    return sparks


def update_sparks(sparks):
    """Advance every spark one frame; return only the ones still alive."""
    alive = []
    for s in sparks:
        s.update()
        if s.life > 0:
            alive.append(s)
    return alive


def render_sparks(sparks, leds):
    """Additively draw sparks into `leds`, colour scaled by remaining life."""
    for s in sparks:
        ix, iy, iz = int(round(s.p[0])), int(round(s.p[1])), int(round(s.p[2]))
        if 0 <= ix < 5 and 0 <= iy < 5 and 0 <= iz < 5:
            fade = max(0.0, min(1.0, s.life))
            colour = np.array([int(c * fade) for c in s.colour])
            leds[ix, iy, iz] = np.clip(leds[ix, iy, iz] + colour, 0, 255)
