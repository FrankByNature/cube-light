import numpy as np
import time
import random
import math

import effects

# Shared state for wave pattern
wave_state = {
    'color_mode': 'rainbow',  # 'solid' or 'rainbow'
    'color': [0, 255, 255],  # Default cyan
    'rainbow_speed': 5,  # How fast colors change (1-20)
    'rainbow_spread': 60,  # How spread out colors are (10-120)
    'wave_speed': 3,  # How fast the wave moves (1-10)
    'axis': 'z'  # Axis the wave travels along: 'z' (default) or 'x'
}

# Shared state for rainbow pattern
rainbow_state = {
    'speed': 5,  # How fast colors change (1-20)
    'spread': 60  # How spread out colors are (10-120)
}

# Shared state for rain pattern
rain_state = {
    'intensity': 5  # How heavy the rain is (1-10)
}

# Shared state for drips pattern
drips_state = {
    'spawn_chance': 5  # How often drips spawn (1-10)
}

# Shared state for fireworks pattern
fireworks_state = {
    'launch_probability': 4,  # How often fireworks launch (1-10)
    'max_fireworks': 1,  # Maximum number of simultaneous fireworks (1-5)
    'explosion_size': 5  # Size of the explosion burst (1-10, 5 = default)
}

# Shared state for XMS simulation pattern
xms_state = {
    'refractory_period': 6,       # How long a cell stays refractory (default 6)
    'excitation_threshold': 5,    # Neighbour excitation needed to trigger (default 5)
    'trigger_probability': 0.003  # Spontaneous firing probability (default 0.003)
}

# Shared state for flame pattern
flame_state = {
    'base_temp': 0.5,   # Base heat at the bottom (default 0.5)
    'var_temp': 0.5,    # Temperature variation from noise (default 0.5)
}

# Shared state for lava lamp pattern
lava_state = {
    'speed': 5,              # How fast the blobs drift (1-20)
    'size': 5,               # Blob radius (1-10)
    'count': 3,              # Number of blobs (1-6)
    'color_mode': 'solid',   # 'solid', 'multi' (a hue per blob) or 'rainbow'
    'color': [255, 40, 0],   # Solid-mode colour (default lava orange)
}

# Shared state for ocean pattern
ocean_state = {
    'speed': 5,    # How fast the swell travels (1-10)
    'height': 5,   # Wave amplitude - also how much foam the crests get (1-10)
}

# Shared state for spiral pattern
spiral_state = {
    'radius': 2.0,             # Radius of the dot's circle in the top plane (0.5-2.0)
    'speed': 5,                # How fast the dot goes round (1-10)
    'fall_speed': 5,           # How fast the dropped points sink (1-10)
    'color_speed': 5,          # How fast the rainbow cycles (1-10)
    'color_mode': 'rainbow',   # 'solid' or 'rainbow'
    'color': [0, 128, 255],    # Solid-mode colour
}

# Shared state for shimmer pattern
shimmer_state = {
    'color': [0, 80, 200],       # Main colour the whole cube sits at
    'color2': [255, 255, 255],   # Colour individual cells shimmer towards
    'intensity': 5,              # How busy and how strong the shimmer is (1-10)
}

#============================================================================================
#============================================================================================
#Rainbow_pattern

def pattern_rainbow(leds, send_to_cube, is_running, hsv_to_rgb):
    """Rainbow wave through the cube"""
    hue = 0
    while is_running():
        for z in range(5):
            h = (hue + z * rainbow_state['spread']) % 360
            r, g, b = hsv_to_rgb(h, 1.0, 1.0)
            
            for x in range(5):
                for y in range(5):
                    leds[x][y][z] = [r, g, b]
        
        send_to_cube(leds)
        hue = (hue + rainbow_state['speed']) % 360
        time.sleep(0.02)

#============================================================================================
#============================================================================================
#Wave_pattern

def _hsv_rainbow_rgb(hue):
    """Vectorized HSV->RGB for full saturation and value (s = v = 1).

    `hue` is a numpy array of degrees; returns a (..., 3) float array in [0, 1].
    Because saturation/value are fixed at 1, every channel is just a piecewise
    function of hue, so the whole grid is computed with np.select (no per-cell
    calls to the scalar hsv_to_rgb)."""
    h = (hue % 360) / 60.0
    i = np.floor(h).astype(int) % 6
    f = h - np.floor(h)
    z = np.zeros_like(h, dtype=float)
    o = np.ones_like(h, dtype=float)
    conds = [i == 0, i == 1, i == 2, i == 3, i == 4, i == 5]
    r = np.select(conds, [o, 1 - f, z, z, f, o])
    g = np.select(conds, [f, o, o, 1 - f, z, z])
    b = np.select(conds, [z, z, f, o, o, 1 - f])
    return np.stack([r, g, b], axis=-1)

def pattern_wave(leds, send_to_cube, is_running, hsv_to_rgb):
    """Wave pattern moving through cube with color control (vectorized).

    Same maths as before, but computed for all 125 cells at once with numpy:
    a radial sine surface sets a per-cell intensity, and the colour comes from
    either a moving rainbow or a fixed solid colour scaled by that intensity."""
    # Static coordinate grids, built once. indexing='ij' makes axis 0 = x, etc.
    X, Y, Z = np.meshgrid(np.arange(5), np.arange(5), np.arange(5), indexing='ij')

    offset = 0.0
    while is_running():
        # The wave travels along one axis; the radial distance is measured in the
        # plane perpendicular to it. 'z' (default) ripples up/down; 'x' rotates the
        # whole thing 90° so it ripples along the x-axis.
        if wave_state['axis'] == 'x':
            dist = np.sqrt((Y - 2) ** 2 + (Z - 2) ** 2)   # plane perpendicular to x
            travel = X
        else:
            dist = np.sqrt((X - 2) ** 2 + (Y - 2) ** 2)   # plane perpendicular to z
            travel = Z

        height = np.sin(dist - offset) * 2 + 2.5
        delta = np.abs(travel - height)
        # Cells within 1.5 of the surface light up; intensity fades with distance
        intensity = np.where(delta < 1.5, 1 - delta / 1.5, 0.0)   # (5,5,5)

        if wave_state['color_mode'] == 'rainbow':
            hue = (dist * wave_state['rainbow_spread']
                   - offset * wave_state['rainbow_speed'] * 10) % 360
            # s=1 RGB scales linearly with value, so scale a full-value colour
            # by the per-cell intensity instead of recomputing HSV per cell.
            base = _hsv_rainbow_rgb(hue) * 255.0          # (5,5,5,3)
            frame = base * intensity[..., None]
        else:
            colour = np.array(wave_state['color'], dtype=float)   # (3,)
            frame = colour * intensity[..., None]

        leds[:] = frame.astype(int)

        send_to_cube(leds)
        offset += 0.07 * wave_state['wave_speed']  # Use wave_speed to control animation speed
        time.sleep(0.02)

#============================================================================================
#============================================================================================
#Rain_pattern

def pattern_rain(leds, send_to_cube, is_running, hsv_to_rgb):
    """Rain drops falling through the cube"""
    drops = []
    
    while is_running():
        leds[:] = 0

        # Intensity controls how likely new drops spawn (0.05 to 0.95)
        drop_probability = 0.05 + (rain_state['intensity'] / 10.0) * 0.9
        
        if random.random() < drop_probability:
            drops.append({'x': random.randint(0, 4), 'y': random.randint(0, 4), 'z': 4, 'rg': random.randint(0, 150)})
        
        new_drops = []
        for drop in drops:
            if drop['z'] >= 0:
                leds[drop['x']][drop['y']][drop['z']] = [50 + drop['rg'], 50 + drop['rg'], 255]
            drop['z'] -= 1
            if drop['z'] >= 0:
                new_drops.append(drop)
        drops = new_drops
        
        send_to_cube(leds)
        time.sleep(0.02)

#============================================================================================
#============================================================================================
#Flame_pattern

cool = 0.03

def get_flame_colour(intensity):
    """Smooth colour gradient for flame"""
    brightness = 255
    intensity = max(0, min(1, intensity))
    
    if intensity < 0.2:
        r = intensity * 0.5
        return [int(r*brightness), 0, 0]
    elif intensity < 0.4:
        r = 0.1 + (intensity - 0.2) * 4.5
        return [int(r*brightness), 0, 0]
    elif intensity < 0.6:
        g = (intensity - 0.4) * 5
        return [brightness, int(g*brightness), 0]
    elif intensity < 0.8:
        g = (intensity - 0.4) * 2.5
        return [brightness, int(g*brightness), 0]
    else:
        b = (intensity - 0.8) * 5
        return [brightness, brightness, int(b*brightness)]

def perlin_noise(x, z, t):
    return (np.sin(x * 0.5 + t) + np.sin(z * 0.7 + t * 1.3)) / 2

def pattern_flame(leds, send_to_cube, is_running, hsv_to_rgb):
    flame_map = np.zeros((5, 5, 5))

    t = 0
    while is_running():
        t += 0.15
        
        base_temp = flame_state['base_temp']
        var_temp = flame_state['var_temp']
        
        for x in range(5):
            for z in range(5):
                noise = perlin_noise(x, z, t)
                base_heat = base_temp + noise * var_temp
                flame_map[x][0][z] = max(0.5, base_heat + random.uniform(-0.1, 0.1))
        
        for y in range(1, 5):
            for x in range(5):
                for z in range(5):
                    heat = flame_map[x][y-1][z] * 0.6
                    
                    if x > 0: 
                        heat += flame_map[x-1][y-1][z] * 0.1
                    if x < 4: 
                        heat += flame_map[x+1][y-1][z] * 0.1
                    if z > 0: 
                        heat += flame_map[x][y-1][z-1] * 0.1
                    if z < 4: 
                        heat += flame_map[x][y-1][z+1] * 0.1
                    
                    cooling = cool * y
                    turbulence = random.uniform(-0.15, 0.05)
                    
                    flame_map[x][y][z] = max(0, heat - cooling + turbulence)

        for x in range(5):
            for y in range(5):
                for z in range(5):
                    leds[x][y][z] = get_flame_colour(flame_map[x][z][y])

        send_to_cube(leds)
        time.sleep(0.02)

#============================================================================================
#============================================================================================
#Firework_pattern

class Firework:
    def __init__(self):
        self.x = random.randint(1, 3)
        self.y = random.randint(1, 3)
        self.z = 0.0
        self.speed = random.uniform(0.4, 0.6)
        self.explosion_height = random.uniform(3.0, 4.5)
        self.exploded = False
        self.particles = []
        self.alive = True
        self.colour = random.choice([
            [255, 50, 50],
            [50, 255, 50],
            [50, 50, 255],
            [255, 255, 50],
            [255, 50, 255],
            [50, 255, 255],
            [255, 150, 50],
            [255, 255, 255],
        ])
    
    def update(self):
        if not self.exploded:
            self.z += self.speed
            if self.z >= self.explosion_height:
                self.explode()
        else:
            self.particles = effects.update_sparks(self.particles)
            if not self.particles:
                self.alive = False

    def explode(self):
        self.exploded = True
        # Size slider (1-10, default 5): below 5 shrinks the burst radius via
        # spark speed (particle count stays default); above 5 the radius
        # stays default and particle count increases instead.
        size = fireworks_state['explosion_size']
        speed_scale = min(1.0, size / 5.0)
        count_scale = max(1.0, size / 5.0)
        self.particles = effects.spawn_burst(
            (self.x, self.y, self.z),
            count=max(5, int(random.randint(15, 25) * count_scale)),
            base_colour=self.colour,
            colour_jitter=30,
            speed_range=(0.2 * speed_scale, 0.5 * speed_scale),
            fade=0.03,       # ~0.3 s of sparks at this pattern's 100 fps loop
            gravity=0.08,
            die_below=0,     # sparks die when they fall out of the cube floor
        )

    def render(self, cube):
        if not self.exploded:
            z = int(self.z)
            if 0 <= z < 5:
                cube[self.x, self.y, z] = np.clip(
                    cube[self.x, self.y, z] + np.array(self.colour), 0, 255
                )
            if z > 0 and z - 1 < 5:
                trail_colour = [c // 4 for c in self.colour]
                cube[self.x, self.y, z-1] = np.clip(
                    cube[self.x, self.y, z-1] + np.array(trail_colour), 0, 255
                )
        else:
            effects.render_sparks(self.particles, cube)

def pattern_fireworks(leds, send_to_cube, is_running, hsv_to_rgb):
    fireworks = []

    while is_running():
        # Launch probability scales from 0.05 to 0.95 based on slider (1-10)
        launch_prob = 0.05 + (fireworks_state['launch_probability'] / 10.0) * 0.9
        
        if random.random() < launch_prob and len(fireworks) < fireworks_state['max_fireworks']:
            fireworks.append(Firework())
        
        # Clear cube
        leds[:] = 0

        # Update and render all fireworks
        for fw in fireworks:
            fw.update()
            fw.render(leds)
            
        # Remove dead fireworks
        fireworks = [fw for fw in fireworks if fw.alive]

        send_to_cube(leds)
        time.sleep(0.01)

#============================================================================================
#============================================================================================
#XM_sim_pattern

# Parameters (static)
EXCITED_DURATION = 2

# Colours
show_refractory = True
COLOUR_RESTING = (0, 0, 0)
COLOUR_EXCITED = (255, 150, 0)

# States
RESTING = 0; EXCITED = 1; REFRACTORY = 2

# Neighbour offsets and their excitation weights, precomputed once. The weight
# falls off with Euclidean distance: face neighbours (1 axis) count fully, edge
# neighbours (2 axes) ~1/sqrt(2), corner neighbours (3 axes) ~1/sqrt(3).
XMS_NEIGHBOURS = []
for _dx in (-1, 0, 1):
    for _dy in (-1, 0, 1):
        for _dz in (-1, 0, 1):
            if _dx == _dy == _dz == 0:
                continue
            _manhattan = abs(_dx) + abs(_dy) + abs(_dz)
            _weight = 1.0 if _manhattan == 1 else (0.707 if _manhattan == 2 else 0.578)
            XMS_NEIGHBOURS.append((_dx, _dy, _dz, _weight))

def xms_step(state, refractory_counter, excited_counter):
    """Advance the excitable-medium automaton one step (whole grid at once).

    `state`, `refractory_counter`, `excited_counter` are (5,5,5) int arrays.
    Returns the new versions. Transitions read the *old* state everywhere, so
    the update is order-independent just like the original cell-by-cell loop."""
    # Read params once per step so they're consistent within a frame
    refractory_period = xms_state['refractory_period']
    excitation_threshold = xms_state['excitation_threshold']
    trigger_probability = xms_state['trigger_probability']

    # Weighted count of EXCITED neighbours for every cell, via zero-padded shifts
    excited = (state == EXCITED).astype(float)
    padded = np.zeros((7, 7, 7))
    padded[1:6, 1:6, 1:6] = excited
    level = np.zeros((5, 5, 5))
    for dx, dy, dz, w in XMS_NEIGHBOURS:
        level += w * padded[1 + dx:6 + dx, 1 + dy:6 + dy, 1 + dz:6 + dz]

    new_state = state.copy()
    new_refractory = refractory_counter.copy()
    new_excited = excited_counter.copy()

    # EXCITED: stay excited until EXCITED_DURATION, then drop to REFRACTORY
    exc = state == EXCITED
    new_excited[exc] += 1
    to_refractory = exc & (new_excited >= EXCITED_DURATION)
    new_state[to_refractory] = REFRACTORY
    new_excited[to_refractory] = 0
    new_refractory[to_refractory] = refractory_period

    # REFRACTORY: count down, returning to RESTING at zero
    ref = state == REFRACTORY
    new_refractory[ref] -= 1
    to_resting = ref & (new_refractory <= 0)
    new_state[to_resting] = RESTING

    # RESTING: fire spontaneously, or from enough excited neighbours
    rest = state == RESTING
    spont = rest & (np.random.random((5, 5, 5)) < trigger_probability)
    exc_prob = np.minimum(level / excitation_threshold, 1.0)
    neigh = rest & ~spont & (np.random.random((5, 5, 5)) < exc_prob)
    fire = spont | neigh
    new_state[fire] = EXCITED
    new_excited[fire] = 0

    return new_state, new_refractory, new_excited

def render_xms(state, refractory_counter, show_refractory):
    """Render the automaton state to a (5,5,5,3) int array."""
    out = np.zeros((5, 5, 5, 3), dtype=np.int_)
    out[state == EXCITED] = COLOUR_EXCITED
    if show_refractory:
        ref = state == REFRACTORY
        rp = xms_state['refractory_period']
        intensity_r = np.minimum(refractory_counter / rp, 1.0)
        intensity_b = np.maximum(0.0, (refractory_counter - rp / 3.0) / rp)
        out[..., 0] = np.where(ref, (COLOUR_EXCITED[0] * intensity_r).astype(int), out[..., 0])
        out[..., 1] = np.where(ref, (COLOUR_EXCITED[1] * intensity_b).astype(int), out[..., 1])
    return out

def pattern_XMS(leds, send_to_cube, is_running, hsv_to_rgb):
    state = np.zeros((5, 5, 5), dtype=np.int_)              # all RESTING
    refractory_counter = np.zeros((5, 5, 5), dtype=np.int_)
    excited_counter = np.zeros((5, 5, 5), dtype=np.int_)

    while is_running():
        state, refractory_counter, excited_counter = xms_step(
            state, refractory_counter, excited_counter)
        leds[:] = render_xms(state, refractory_counter, show_refractory)
        send_to_cube(leds)
        time.sleep(0.05)

#============================================================================================
#============================================================================================
#Drips_pattern

class Drip:
    def __init__(self, x, y):
        self.x = x
        self.y = y
        self.z = 4  # Start at top layer
        self.brightness = 0
        self.max_brightness = 255
        self.brighten_rate = 10
        self.fall_speed = 1
        self.state = 'brightening'  # 'brightening' or 'falling'
        self.colour = [random.randint(50, 255), 
                      random.randint(50, 255), 
                      random.randint(50, 255)]
    
    def update(self):
        if self.state == 'brightening':
            self.brightness += self.brighten_rate
            if self.brightness >= self.max_brightness:
                self.brightness = self.max_brightness
                self.state = 'falling'
        elif self.state == 'falling':
            self.z -= self.fall_speed
        
        return self.z >= 0  # Return True if still active

def update_animation(leds, drips, spawn_chance_value):
    """Update the animation state"""
    # Clear the cube
    leds[:] = 0
    
    # Spawn chance scales from 0.05 to 0.95 based on slider (1-10)
    spawn_prob = 0.05 + (spawn_chance_value / 10.0) * 0.9
    
    # Randomly spawn new drips at the top
    if random.random() < spawn_prob:
        x = random.randint(0, 4)
        y = random.randint(0, 4)
        # Check if position is not already occupied
        if not any(d.x == x and d.y == y and d.z == 4 for d in drips):
            drips.append(Drip(x, y))
    
    # Update and draw all drips
    active_drips = []
    for drip in drips:
        if drip.update():
            # Calculate colour with brightness
            brightness_factor = drip.brightness / drip.max_brightness
            colour = [int(c * brightness_factor) for c in drip.colour]
            leds[drip.x, drip.y, drip.z] = colour
            
            # Add tail (1 pixel above, 10% brightness) only when falling
            if drip.state == 'falling' and drip.z < 4:
                tail_brightness = brightness_factor * 0.1
                tail_colour = [int(c * tail_brightness) for c in drip.colour]
                leds[drip.x, drip.y, drip.z + 1] = tail_colour
            
            active_drips.append(drip)
    
    return active_drips

def pattern_drips(leds, send_to_cube, is_running, hsv_to_rgb):
    leds[:] = 0
    drips = []

    while is_running():
        drips = update_animation(leds, drips, drips_state['spawn_chance'])
        send_to_cube(leds)
        time.sleep(0.05)

#============================================================================================
#============================================================================================
#Lava_lamp_pattern

LAVA_MARGIN = 0.6   # how far outside the cube a blob centre may drift

class LavaBlob:
    """One drifting metaball.

    The blob keeps a unit heading that wanders a little each frame (so the
    motion is a smooth random walk rather than a straight line) and bounces off
    the walls of a slightly oversized box, which lets a blob half-leave the
    cube instead of always sitting fully inside it."""

    def __init__(self):
        self.pos = np.array([random.uniform(0.0, 4.0) for _ in range(3)])
        heading = np.random.normal(size=3)
        self.vel = heading / (np.linalg.norm(heading) or 1.0)
        self.hue = random.uniform(0, 360)

    def update(self, speed):
        # Nudge the heading, then renormalise: speed comes from the slider only
        self.vel += np.random.normal(scale=0.15, size=3)
        norm = np.linalg.norm(self.vel)
        if norm > 0:
            self.vel /= norm
        self.pos += self.vel * (0.0025 * speed)

        for i in range(3):
            if self.pos[i] < -LAVA_MARGIN:
                self.pos[i] = -LAVA_MARGIN
                self.vel[i] = abs(self.vel[i])
            elif self.pos[i] > 4 + LAVA_MARGIN:
                self.pos[i] = 4 + LAVA_MARGIN
                self.vel[i] = -abs(self.vel[i])

def pattern_lava(leds, send_to_cube, is_running, hsv_to_rgb):
    """Lava lamp: soft glowing blobs that drift around and merge as they meet.

    Each blob contributes a gaussian falloff around its centre; the
    contributions are simply summed, so overlapping blobs brighten and read as
    one larger blob (the usual metaball trick)."""
    X, Y, Z = np.meshgrid(np.arange(5), np.arange(5), np.arange(5), indexing='ij')
    blobs = []
    hue_offset = 0.0

    while is_running():
        # Track the blob-count slider live
        count = int(lava_state['count'])
        while len(blobs) < count:
            blobs.append(LavaBlob())
        del blobs[count:]

        speed = lava_state['speed']
        mode = lava_state['color_mode']
        # Size slider 1-10 -> blob radius 0.8 to 2.6 cells
        radius = 0.8 + (lava_state['size'] - 1) / 9.0 * 1.8
        spread = 2 * (radius / 1.6) ** 2

        frame = np.zeros((5, 5, 5, 3))
        for i, blob in enumerate(blobs):
            blob.update(speed)

            if mode == 'solid':
                colour = np.array(lava_state['color'], dtype=float)
            elif mode == 'rainbow':
                # Blobs share one slowly cycling rainbow, spaced evenly round it
                hue = (hue_offset + i * 360.0 / count) % 360
                colour = np.array(hsv_to_rgb(hue, 1.0, 1.0), dtype=float)
            else:  # 'multi' - every blob keeps the hue it was born with
                colour = np.array(hsv_to_rgb(blob.hue, 1.0, 1.0), dtype=float)

            d2 = ((X - blob.pos[0]) ** 2 + (Y - blob.pos[1]) ** 2
                  + (Z - blob.pos[2]) ** 2)
            frame += np.exp(-d2 / spread)[..., None] * colour

        leds[:] = np.clip(frame, 0, 255).astype(int)
        send_to_cube(leds)
        hue_offset = (hue_offset + 0.3) % 360
        time.sleep(0.02)

#============================================================================================
#============================================================================================
#Ocean_pattern

OCEAN_DEEP = np.array([0, 20, 90], dtype=float)     # water body, well below the surface
OCEAN_SURF = np.array([0, 110, 255], dtype=float)   # bright blue at the surface itself
OCEAN_FOAM = np.array([255, 255, 255], dtype=float) # white highlights on the crests

def pattern_ocean(leds, send_to_cube, is_running, hsv_to_rgb):
    """Rolling blue swell with white foam on the crests (vectorized).

    Three travelling sine waves at different angles and speeds are summed into
    a height map over the (x, y) plane. Cells light up according to how close
    they are to that surface, with a dimmer blue glow for the water beneath it,
    and columns whose crest is riding high get white foam mixed in."""
    X, Y = np.meshgrid(np.arange(5), np.arange(5), indexing='ij')
    X = X.astype(float)
    Y = Y.astype(float)
    Zc = np.arange(5).astype(float)

    t = 0.0
    while is_running():
        # Height slider 1-10 -> amplitude 0.42 to 1.5 cells
        amp = 0.3 + ocean_state['height'] / 10.0 * 1.2

        height = (2.4
                  + amp * np.sin(0.9 * X - t)
                  + amp * 0.6 * np.sin(0.7 * Y - t * 1.3)
                  + amp * 0.4 * np.sin(0.5 * (X + Y) + t * 0.7))   # (5,5)

        # Signed distance from each cell to the surface above/below it
        depth = Zc[None, None, :] - height[:, :, None]             # (5,5,5)

        surface = np.clip(1 - np.abs(depth) / 1.2, 0, 1)           # the lit wave band
        body = np.where(depth < 0, np.clip((depth + 3.0) / 3.0, 0, 1), 0.0)

        # Foam rides the tallest wave in the frame: columns within `band` of the
        # current highest crest whiten, in proportion to how close they are to
        # it. Measuring against the frame's own maximum (rather than a fixed
        # level) means there is always a white cap somewhere, and scaling the
        # band with the amplitude keeps a calm sea from foaming all over.
        band = max(0.25, amp)
        crest = np.clip((height - (height.max() - band)) / band, 0, 1)   # (5,5)
        crest_colour = OCEAN_SURF + (OCEAN_FOAM - OCEAN_SURF) * crest[:, :, None, None]

        frame = body[..., None] * OCEAN_DEEP + surface[..., None] * crest_colour
        leds[:] = np.clip(frame, 0, 255).astype(int)

        send_to_cube(leds)
        t += 0.008 * ocean_state['speed']
        time.sleep(0.02)

#============================================================================================
#============================================================================================
#Spiral_pattern

SPIRAL_EMIT_GAP = 0.05   # vertical spacing between the points the dot drops

def pattern_spiral(leds, send_to_cube, is_running, hsv_to_rgb):
    """A dot circles the top plane and drops light behind it.

    Each dropped point keeps the (x, y) it was emitted at and sinks straight
    down, so the falling trail traces out a helix. Points are emitted once per
    SPIRAL_EMIT_GAP of fall rather than once per frame, which keeps the ribbon
    evenly spaced (and the trail list bounded) at any speed. Rotation and fall
    are separate sliders, so their ratio is what sets the pitch of the spiral -
    slow rotation against a fast fall stretches it towards a straight line,
    the reverse winds it into tight coils."""
    trail = []          # dicts of {'x', 'y', 'z', 'colour'}
    since_emit = 0.0
    angle = 0.0
    hue = 0.0

    while is_running():
        speed = spiral_state['speed']
        radius = spiral_state['radius']

        # With both speed sliders at their default this is ~1.25 turns over
        # the full height of the cube - enough to read as a spiral without the
        # trail wrapping into a solid tube
        angle = (angle + 0.016 * speed) % (2 * math.pi)
        x = 2 + radius * math.cos(angle)
        y = 2 + radius * math.sin(angle)

        if spiral_state['color_mode'] == 'rainbow':
            # The hue runs on its own clock rather than tracking the angle, so
            # the colour can cycle faster or slower than the dot goes round
            hue = (hue + 0.9 * spiral_state['color_speed']) % 360
            colour = list(hsv_to_rgb(hue, 1.0, 1.0))
        else:
            colour = list(spiral_state['color'])

        # Sink everything already dropped, discarding what has fallen out
        fall = 0.01 * spiral_state['fall_speed']
        alive = []
        for p in trail:
            p['z'] -= fall
            if p['z'] > -1.0:
                alive.append(p)
        trail = alive

        # Drop a new point once the trail has moved a full gap down
        since_emit += fall
        if since_emit >= SPIRAL_EMIT_GAP:
            since_emit -= SPIRAL_EMIT_GAP
            trail.append({'x': x, 'y': y, 'z': 4.0, 'colour': colour})

        leds[:] = 0
        best = np.zeros((5, 5, 5))   # brightest contribution each cell has seen
        for p in trail:
            # Dim towards the floor so the light looks like it's dissipating
            fade = 0.35 + 0.65 * max(0.0, min(1.0, (p['z'] + 1.0) / 5.0))
            _spiral_draw(leds, best, p['x'], p['y'], p['z'], p['colour'], fade)

        # The dot itself, drawn last and at full brightness
        _spiral_draw(leds, best, x, y, 4.0, colour, 1.0)

        send_to_cube(leds)
        time.sleep(0.02)

def _spiral_draw(leds, best, x, y, z, colour, fade):
    """Place one trail point, split across the two z cells it sits between so
    the sinking looks smooth rather than stepping a whole cell.

    Points are packed far closer together than one per cell (SPIRAL_EMIT_GAP is
    a twentieth of a cell), so adding them up would drive every channel past
    255 and wash the chosen colour out - a solid blue trail would clip its way
    to cyan. Each cell therefore keeps only its brightest contribution, which
    holds the colour exactly at any fall speed."""
    cx = max(0, min(4, int(round(x))))
    cy = max(0, min(4, int(round(y))))
    z0 = math.floor(z)
    frac = z - z0
    for zi, weight in ((z0, 1.0 - frac), (z0 + 1, frac)):
        if 0 <= zi < 5 and weight > 0:
            strength = weight * fade
            if strength > best[cx, cy, zi]:
                best[cx, cy, zi] = strength
                leds[cx, cy, zi] = [int(c * strength) for c in colour]

#============================================================================================
#============================================================================================
#Shimmer_pattern

SHIMMER_DECAY = 0.88   # per-frame decay of a cell's shimmer level
SHIMMER_JITTER = 0.12  # +/- variation of the main colour, per cell per channel

def pattern_shimmer(leds, send_to_cube, is_running, hsv_to_rgb):
    """The cube sits at the main colour while individual cells glint towards
    the second one.

    Every cell holds a shimmer level in [0, 1] that decays a little each frame.
    Cells are randomly sparked back up, and the level is just the blend factor
    between the two colours, so 0 is the plain main colour and 1 is the full
    shimmer colour. The intensity slider drives both how often cells spark and
    how far towards the second colour they get, so low settings are an
    occasional faint glint and high ones are a busy sparkle.

    Each cell also gets its own small fixed offset from the main colour, drawn
    once when the pattern starts, so the resting cube has a little natural
    variation instead of being one flat wash. The offset is a multiplier rather
    than a fixed amount, so it keeps working whatever main colour is set."""
    level = np.zeros((5, 5, 5))
    jitter = np.random.uniform(1 - SHIMMER_JITTER, 1 + SHIMMER_JITTER, (5, 5, 5, 3))

    while is_running():
        intensity = shimmer_state['intensity']
        # ~0.6 cells per frame sparking at intensity 1, ~4 at intensity 10
        spark_chance = 0.002 + intensity / 10.0 * 0.030
        # How far towards the second colour a spark reaches
        peak = 0.35 + intensity / 10.0 * 0.65

        level *= SHIMMER_DECAY
        sparked = np.random.random((5, 5, 5)) < spark_chance
        if sparked.any():
            # Vary the strength per spark, and never dim a cell that is already
            # brighter than the new spark would make it
            strength = np.random.uniform(0.5, 1.0, size=sparked.sum()) * peak
            level[sparked] = np.maximum(level[sparked], strength)

        # Jitter varies the resting colour per cell; a fully shimmering cell
        # still lands on the exact shimmer colour
        main = np.array(shimmer_state['color'], dtype=float) * jitter
        shimmer = np.array(shimmer_state['color2'], dtype=float)
        frame = main + (shimmer - main) * level[..., None]
        leds[:] = np.clip(frame, 0, 255).astype(int)

        send_to_cube(leds)
        time.sleep(0.03)


# Dictionary of all available patterns
PATTERNS = {
    'rainbow': pattern_rainbow,
    'fireworks': pattern_fireworks,
    'wave': pattern_wave,
    'rain': pattern_rain,
    'flame': pattern_flame,
    'XMS': pattern_XMS,
    'drips': pattern_drips,
    'lava': pattern_lava,
    'ocean': pattern_ocean,
    'spiral': pattern_spiral,
    'shimmer': pattern_shimmer
}