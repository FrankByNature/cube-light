"""The controller bridge's reconnect loop (backoff, give-up timeout, write
timeout) with a fake clock and a fake /dev/rfcomm0, plus how the server turns
the bridge's log into the web UI's status line."""
import logging
import types

import pytest

import cube_MP_server as server
import game_controller_bridge as gcb


class FakeSerialError(Exception):
    pass


class StopTest(Exception):
    pass


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class Port:
    """One session on /dev/rfcomm0. Each readline() takes 0.1 s, like the real
    read timeout. `lines` are (seconds after opening, bytes) arrivals; the
    session raises a serial error `fail_after` seconds after opening, which is
    what a switched-off controller does (about 5 s, the Bluetooth page timeout)."""

    def __init__(self, clock, fail_after=5.1, lines=(), write_error=False):
        self.clock = clock
        self.fail_after = fail_after
        self.lines = list(lines)
        self.write_error = write_error
        self.written = []

    def __enter__(self):
        self.opened = self.clock.t
        return self

    def __exit__(self, *exc):
        return False

    def readline(self):
        self.clock.t += 0.1
        elapsed = self.clock.t - self.opened
        if elapsed >= self.fail_after:
            raise FakeSerialError('device reports readiness to read but returned no data')
        if self.lines and elapsed >= self.lines[0][0]:
            return self.lines.pop(0)[1]
        return b''

    def write(self, data):
        if self.write_error:
            raise FakeSerialError('Write timeout')
        self.written.append(data)


@pytest.fixture
def fake_serial(monkeypatch):
    ns = types.SimpleNamespace(SerialException=FakeSerialError, Serial=None)
    monkeypatch.setattr(gcb, 'serial', ns)
    return ns


def sessions(clock, *specs, then=None, limit=200):
    """open_port() returning the given sessions in turn, then `then(clock)`
    forever (default: controller off), with a cap so a broken loop can't hang."""
    queue = list(specs)
    count = {'n': 0}

    def open_port():
        count['n'] += 1
        if count['n'] > limit:
            raise StopTest
        if queue:
            spec = queue.pop(0)
            if isinstance(spec, Exception):
                raise spec
            return spec
        return then(clock) if then else Port(clock)
    open_port.count = count
    return open_port


def run(clock, open_port, give_up=300):
    gcb.reader_loop('/dev/rfcomm0', 115200, gcb.Bridge('http://x', test=True),
                    give_up=give_up, clock=clock, sleep=clock.sleep, open_port=open_port)


def test_controller_off_backs_off_and_gives_up(fake_serial, caplog):
    clock = Clock()
    open_port = sessions(clock)
    with caplog.at_level(logging.INFO, logger='bridge'):
        run(clock, open_port)   # returns instead of retrying forever
    assert clock.sleeps[:5] == [3, 6, 12, 24, 30]
    assert max(clock.sleeps) == gcb.RETRY_MAX
    elapsed = clock.t - 1000
    assert 300 <= elapsed < 300 + gcb.RETRY_MAX + 6
    # The old fixed 3 s retry made ~37 attempts in five minutes
    assert open_port.count['n'] <= 12
    assert any('Giving up: controller unreachable for 5 min' in r.getMessage()
               for r in caplog.records)
    assert not any('link up' in r.getMessage() for r in caplog.records)


def test_default_gives_up_after_about_a_minute(fake_serial, caplog):
    clock = Clock()
    with caplog.at_level(logging.INFO, logger='bridge'):
        gcb.reader_loop('/dev/rfcomm0', 115200, gcb.Bridge('http://x', test=True),
                        clock=clock, sleep=clock.sleep, open_port=sessions(clock))
    assert gcb.GIVE_UP_AFTER == 60
    assert 60 <= clock.t - 1000 < 60 + gcb.RETRY_MAX + 6
    assert any('Giving up: controller unreachable for 1 min' in r.getMessage()
               for r in caplog.records)


def test_missing_rfcomm_device_also_gives_up(fake_serial):
    clock = Clock()
    run(clock, sessions(clock, then=lambda c: (_ for _ in ()).throw(FileNotFoundError())))
    assert 300 <= clock.t - 1000 < 300 + gcb.RETRY_MAX + 1


def test_a_long_idle_session_counts_as_reachable(fake_serial, caplog):
    """Connected and idle (no buttons) for longer than the timeout, then the
    controller is switched off: the bridge gives up 5 minutes after that, not
    straight away."""
    clock = Clock()
    with caplog.at_level(logging.INFO, logger='bridge'):
        run(clock, sessions(clock, Port(clock, fail_after=400)))
    assert 'Controller link up.' in [r.getMessage() for r in caplog.records]
    assert clock.sleeps[0] == gcb.RETRY_MIN
    assert clock.t - 1000 >= 400 + 300


def test_a_button_proves_the_link_and_resets_the_backoff(fake_serial):
    clock = Clock()
    off = [Port(clock) for _ in range(4)]                     # ~70 s of failures
    button = Port(clock, fail_after=3, lines=[(1, b'start\n')])
    run(clock, sessions(clock, *off, button))
    # sleeps: 3, 6, 12, 24 for the failures, then back to 3 after the button
    assert clock.sleeps[:5] == [3, 6, 12, 24, 3]
    button_time = 1000 + 5.1 * 4 + 3 + 6 + 12 + 24 + 1
    assert clock.t - button_time >= 300


def test_give_up_zero_retries_forever(fake_serial):
    clock = Clock()
    with pytest.raises(StopTest):
        run(clock, sessions(clock, limit=60), give_up=0)
    assert clock.t - 1000 > 300


def test_a_write_timeout_is_a_serial_error_not_a_hang(fake_serial):
    clock = Clock()
    stuck = Port(clock, fail_after=1000, write_error=True)
    run(clock, sessions(clock, stuck))
    assert clock.t - 1000 < 400   # got past the stuck write and gave up


def test_the_real_port_has_a_write_timeout(fake_serial):
    opened = []

    def fake_open(*args, **kwargs):
        opened.append(kwargs)
        raise FileNotFoundError

    fake_serial.Serial = fake_open
    clock = Clock()
    gcb.reader_loop('/dev/rfcomm0', 115200, gcb.Bridge('http://x', test=True),
                    give_up=10, clock=clock, sleep=clock.sleep)
    assert opened[0]['write_timeout'] == gcb.WRITE_TIMEOUT


# ----- the server's view of the bridge log ----------------------------------

FAILED_ATTEMPT = """\
2026-11-08 01:50:55,627 INFO Opening /dev/rfcomm0 ...
2026-11-08 01:50:55,628 INFO Connected (forwarding to http://localhost:5000).
2026-11-08 01:50:55,629 INFO Selected game: meteors.  Waiting for input (Ctrl+C to quit) ...
2026-11-08 01:50:55,645 INFO > disp:meteors:58:58:ready
"""


@pytest.fixture
def bridge_log(tmp_path, monkeypatch):
    path = tmp_path / 'controller_bridge.log'
    monkeypatch.setattr(server, 'BRIDGE_LOG', str(path))
    return path


@pytest.mark.parametrize('tail, status', [
    (FAILED_ATTEMPT, 'Connecting...'),
    (FAILED_ATTEMPT + "2026-11-08 01:51:00,752 WARNING Serial error: no data.\n"
                      "2026-11-08 01:51:00,753 INFO Retrying in 30s ...\n",
     'Waiting for controller...'),
    (FAILED_ATTEMPT + "2026-11-08 01:51:05,645 INFO Controller link up.\n",
     'Controller connected'),
    ("2026-11-08 01:51:05,645 INFO < 'start'\n"
     "2026-11-08 01:51:05,646 INFO START meteors\n", 'Controller connected'),
    ("2026-11-08 01:51:05,645 WARNING /dev/rfcomm0 not found - did you 'rfcomm bind'?\n",
     'No /dev/rfcomm0 - run rfcomm bind'),
    ("2026-11-08 01:56:00,000 WARNING Giving up: controller unreachable for 5 min. "
     "Bridge stopped - start it again once the controller is on.\n", server.BRIDGE_GAVE_UP),
])
def test_link_status(bridge_log, tail, status):
    bridge_log.write_text(tail)
    assert server.bridge_link_status() == status


def test_stopped_bridge_shows_why_only_if_it_gave_up(bridge_log, monkeypatch):
    monkeypatch.setattr(server, 'bridge_running', lambda: False)
    bridge_log.write_text("x WARNING Giving up: controller unreachable for 5 min.\n")
    assert server.bridge_state() == {'running': False, 'link': server.BRIDGE_GAVE_UP}
    bridge_log.write_text(FAILED_ATTEMPT)   # stopped by hand
    assert server.bridge_state() == {'running': False, 'link': ''}
