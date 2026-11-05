"""The Flask routes: what each one validates and what it queues for the
runner. The runner itself is a queue here, so these are fast and need no
second process."""
import json
import os

import pytest

import cube_MP_server as server
import patterns


def test_state_endpoint_reports_defaults(app):
    r = app['client'].get('/cube/state')
    assert r.status_code == 200
    d = r.get_json()
    assert d['brightness'] == 100
    assert d['pattern'] is None and d['game'] is None
    assert d['simulated'] is True
    assert set(d['patterns']) == set(patterns.PATTERNS)
    assert set(d['tuning']) == set(server.PATTERN_STATES)
    assert d['defaults']['wave_state']['wave_speed'] == 3


def test_frame_endpoint_shape(app):
    d = app['client'].get('/cube/frame').get_json()
    assert len(d['frame']) == server.FRAME_BYTES
    assert d['simulated'] is True


def test_pattern_start_validates_name(app):
    c = app['client']
    assert c.post('/pattern/start', json={'pattern': 'nope'}).status_code == 400
    assert c.post('/pattern/start', json={}).status_code == 400
    assert c.post('/pattern/start').status_code == 400     # no JSON body at all
    assert app['drain']() == []

    r = c.post('/pattern/start', json={'pattern': 'wave'})
    assert r.status_code == 200
    assert app['drain']() == [{'type': 'pattern', 'pattern': 'wave'}]
    assert c.get('/cube/state').get_json()['pattern'] == 'wave'

    c.post('/pattern/stop')
    assert c.get('/cube/state').get_json()['pattern'] is None


def test_brightness_is_clamped_and_shared(app):
    c = app['client']
    assert c.post('/cube/brightness', json={'brightness': 250}).get_json()['brightness'] == 100
    assert c.post('/cube/brightness', json={'brightness': -5}).get_json()['brightness'] == 0
    c.post('/cube/brightness', json={'brightness': 37})
    assert app['brightness'].value == pytest.approx(0.37)
    assert c.get('/cube/state').get_json()['brightness'] == 37
    assert all(cmd['type'] == 'brightness' for cmd in app['drain']())


def test_fill_clamps_channels(app):
    c = app['client']
    r = c.post('/cube/fill', json={'red': 999, 'green': -1, 'blue': 'abc'})
    assert r.get_json()['colour'] == {'r': 255, 'g': 0, 'b': 0}
    assert app['drain']() == [{'type': 'fill', 'r': 255, 'g': 0, 'b': 0}]


def test_scalar_tunable_clamps_casts_and_mirrors(app):
    c = app['client']
    r = c.post('/wave/speed', json={'speed': 99})
    assert r.get_json()['speed'] == 10
    assert app['drain']() == [{'type': 'tune', 'state': 'wave_state', 'key': 'wave_speed', 'value': 10}]
    assert c.get('/cube/state').get_json()['tuning']['wave_state']['wave_speed'] == 10

    r = c.post('/xms/trigger', json={'value': 0.25})
    assert r.get_json()['value'] == 0.25            # float tunable stays float
    r = c.post('/lava/count', json={'value': 'junk'})
    assert r.get_json()['value'] == 3               # falls back to the default


def test_wave_colour_route_is_clamped_like_the_others(app):
    """The old wave_color route stored whatever it was given; a non-numeric
    value would then crash the wave thread."""
    c = app['client']
    r = c.post('/wave/color', json={'red': 'abc', 'green': 300, 'blue': 12})
    assert r.status_code == 200
    assert r.get_json()['color'] == [0, 255, 12]
    assert app['drain']() == [{'type': 'tune', 'state': 'wave_state', 'key': 'color', 'value': [0, 255, 12]}]


def test_wave_mode_and_axis_fall_back_to_defaults(app):
    c = app['client']
    assert c.post('/wave/axis', json={'axis': 'q'}).get_json()['axis'] == 'z'
    assert c.post('/wave/axis', json={'axis': 'x'}).get_json()['axis'] == 'x'
    assert c.post('/wave/mode', json={'mode': 'sparkly'}).get_json()['mode'] == 'rainbow'
    assert c.post('/wave/mode', json={'mode': 'solid'}).get_json()['mode'] == 'solid'
    assert c.post('/lava/mode', json={'mode': 'multi'}).get_json()['mode'] == 'multi'
    cmds = app['drain']()
    assert all(cmd['type'] == 'tune' for cmd in cmds)


def test_every_tunable_url_is_served(app):
    """Each entry in the three tables must produce a working route, and the
    state dict it targets must exist in patterns.py."""
    c = app['client']
    rules = {r.rule for r in c.application.url_map.iter_rules()}
    for url in ('/wave/color', '/wave/mode', '/wave/axis', '/lava/color', '/spiral/mode',
                '/shimmer/color2', '/fireworks/size', '/ocean/height'):
        assert url in rules
    for cmd in [{'type': 'tune', 'state': s, 'key': k}
                for s in server.PATTERN_STATES for k in getattr(patterns, s)]:
        assert isinstance(getattr(patterns, cmd['state']), dict)


def test_game_routes_queue_commands_and_track_state(app):
    c = app['client']
    c.post('/snake/start')
    c.post('/snake/move', json={'direction': 'px'})
    c.post('/meteors/fire')
    assert app['drain']() == [
        {'type': 'game_start', 'game': 'snake'},
        {'type': 'game_move', 'game': 'snake', 'direction': 'px'},
        {'type': 'game_move', 'game': 'meteors', 'direction': 'fire'},
    ]
    assert c.get('/cube/state').get_json()['game'] == 'snake'

    # The game ending on its own is reflected without a stop request
    app['shared']['snake_game_over'].value = True
    assert c.get('/cube/state').get_json()['game'] is None
    app['shared']['snake_game_over'].value = False

    c.post('/planes/stop')                       # stopping a different game
    assert c.get('/cube/state').get_json()['game'] == 'snake'
    c.post('/snake/stop')
    assert c.get('/cube/state').get_json()['game'] is None


def test_state_routes_expose_scores(app):
    app['shared']['meteors_score'].value = 12
    app['shared']['meteors_highscore'].value = 30
    d = app['client'].get('/meteors/state').get_json()
    assert d == {'score': 12, 'game_over': False, 'highscore': 30}


def test_shutdown_requires_confirmation_header(app, monkeypatch):
    c = app['client']
    launched = []
    monkeypatch.setattr(server.subprocess, 'Popen', lambda *a, **k: launched.append(a))
    assert c.post('/system/shutdown').status_code == 403
    assert launched == []
    r = c.post('/system/shutdown', headers={server.SHUTDOWN_HEADER: server.SHUTDOWN_HEADER_VALUE})
    assert r.status_code == 200
    assert launched and launched[0][0][:2] == ['sudo', '-n']
    assert app['drain']() == [{'type': 'stop'}]


def test_no_cors_headers(app):
    r = app['client'].get('/cube/state', headers={'Origin': 'http://evil.example'})
    assert 'Access-Control-Allow-Origin' not in r.headers


def test_highscores_round_trip_and_atomic_write(no_highscore_file):
    assert server.load_highscores() == {'planes': 0, 'snake': 0, 'meteors': 0}
    server.save_highscore('planes', 5)
    server.save_highscore('meteors', 9)
    assert server.load_highscores() == {'planes': 5, 'snake': 0, 'meteors': 9}
    assert json.load(open(no_highscore_file)) == {'planes': 5, 'snake': 0, 'meteors': 9}
    # No temp files left behind
    assert sorted(os.listdir(os.path.dirname(no_highscore_file))) == ['highscores.json']


def test_corrupt_highscore_file_reads_as_zero(no_highscore_file):
    no_highscore_file.write_text('{"planes": 3, "snake"')     # truncated
    assert server.load_highscores() == {'planes': 0, 'snake': 0, 'meteors': 0}
    no_highscore_file.write_text('[1, 2, 3]')                  # wrong shape
    assert server.load_highscores() == {'planes': 0, 'snake': 0, 'meteors': 0}
