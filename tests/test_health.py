"""The health logger, against a fake /proc + /sys tree and fake commands."""
import logging
import os
import time

import pytest

import health


def put(root, path, text):
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


ROUTE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t00000000\t0201A8C0\t0003\t0\t0\t900\t00000000\t0\t0\t0\n"
    "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
    "wlan0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0\n"
)

WIRELESS = (
    "Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n"
    " face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n"
    " wlan0: 0000   58.  -52.  -256        0      0      0      0     33        0\n"
)


@pytest.fixture
def pi(tmp_path):
    """A fake Pi filesystem."""
    put(tmp_path, 'proc/uptime', '7200.5 1000.0\n')
    put(tmp_path, 'proc/loadavg', '0.42 0.30 0.20 1/100 1234\n')
    put(tmp_path, 'proc/meminfo',
        'MemTotal:  436000 kB\nMemAvailable:  204800 kB\nSwapTotal: 102400 kB\nSwapFree: 81920 kB\n')
    put(tmp_path, 'proc/net/route', ROUTE)
    put(tmp_path, 'proc/net/wireless', WIRELESS)
    put(tmp_path, 'proc/sys/kernel/random/boot_id', 'abcdef12-3456\n')
    put(tmp_path, 'proc/sys/kernel/osrelease', '6.6.51+rpt-rpi-v8\n')
    put(tmp_path, 'proc/4242/status', 'Name:\tpython3\nVmRSS:\t   51200 kB\n')
    put(tmp_path, 'sys/class/thermal/thermal_zone0/temp', '48312\n')
    put(tmp_path, 'sys/class/net/wlan0/operstate', 'up\n')
    (tmp_path / 'sys/class/bluetooth/hci0').mkdir(parents=True)
    return tmp_path


def fake_run(ping='64 bytes from 192.168.1.1: icmp_seq=1 ttl=64 time=4.71 ms',
             throttled='throttled=0x0', power_save='Power save: on'):
    outputs = {'ping': ping, 'vcgencmd': throttled, 'iw': power_save}
    calls = []

    def run(cmd, timeout=3):
        calls.append(cmd)
        return outputs.get(cmd[0])
    run.calls = calls
    return run


def test_snapshot_reads_everything(pi):
    run = fake_run()
    snap = health.snapshot(str(pi), run)
    assert snap == {
        'up': '2.0h', 'load': '0.42', 'mem_free': '200MB', 'swap': '20MB',
        'temp': '48C', 'throttled': '0x0', 'wlan': 'up', 'signal': '-52dBm',
        'quality': 58, 'gw': '5ms', 'bt': 'up',
    }
    # The router pinged is the default route with the lowest metric
    assert ['ping', '-c', '1', '-W', '2', '192.168.1.1'] in run.calls


def test_router_not_answering_is_fail(pi):
    snap = health.snapshot(str(pi), fake_run(ping='1 packets transmitted, 0 received'))
    assert snap['gw'] == 'fail'


def test_missing_tools_are_left_out(pi):
    snap = health.snapshot(str(pi), lambda cmd, timeout=3: None)
    assert snap['gw'] is None and snap['throttled'] is None
    assert 'gw=' not in health.format_line(snap)


def test_no_proc_at_all_does_not_crash(tmp_path):
    """A dev machine (Windows) has none of these files."""
    snap = health.snapshot(str(tmp_path), lambda cmd, timeout=3: None)
    assert all(v is None for v in snap.values())
    assert health.format_line(snap).count('=') == 0


def test_process_memory(pi):
    assert health.process_rss_mb(4242, str(pi)) == 50
    assert health.process_rss_mb(9999, str(pi)) is None
    assert health.process_rss_mb(None, str(pi)) is None


def test_extra_fields_and_a_broken_callback(pi):
    snap = health.snapshot(str(pi), fake_run(), extra=lambda: {'bridge': 'on'})
    assert snap['bridge'] == 'on'

    def broken():
        raise RuntimeError('boom')
    assert health.snapshot(str(pi), fake_run(), extra=broken)['extra_error'] == 'RuntimeError'


def test_format_line():
    when = time.mktime((2026, 11, 8, 1, 51, 3, 0, 0, -1))
    assert health.format_line({'a': 1, 'b': None, 'c': 'x'}, when=when) == '2026-11-08 01:51:03 a=1 c=x'
    assert health.format_line({}, tag='STOP', when=when) == '2026-11-08 01:51:03 STOP'


def test_logger_writes_start_samples_and_stop(pi, tmp_path_factory):
    path = str(tmp_path_factory.mktemp('out') / 'health.log')
    logger = health.HealthLogger(path=path, interval=0.05, root=str(pi), run=fake_run())
    logger.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        if os.path.exists(path) and len(open(path).read().splitlines()) >= 3:
            break
        time.sleep(0.02)
    logger.stop()
    logger.join(timeout=2)
    lines = open(path).read().splitlines()
    assert ' START boot=abcdef12 kernel=6.6.51+rpt-rpi-v8 wifi_power_save=on' in lines[0]
    assert 'gw=5ms' in lines[1] and 'wlan=up' in lines[1]
    assert lines[-1].endswith(' STOP')


def test_logger_rotates(tmp_path):
    path = str(tmp_path / 'health.log')
    logger = health.HealthLogger(path=path, max_bytes=100, root=str(tmp_path),
                                 run=lambda cmd, timeout=3: None)
    for i in range(10):
        logger.write(f'line {i} ' + 'x' * 30)
    assert os.path.exists(path + '.1')
    assert os.path.getsize(path) <= 100 + 40


def test_changes_are_warned_in_the_journal(pi, caplog, tmp_path_factory):
    path = str(tmp_path_factory.mktemp('out') / 'health.log')
    logger = health.HealthLogger(path=path, root=str(pi), run=fake_run())
    with caplog.at_level(logging.WARNING, logger='health'):
        logger.sample()
        logger.runner = fake_run(ping='0 received', throttled='throttled=0x50005')
        logger.sample()
        logger.sample()   # no further change, no further warnings
    messages = [r.getMessage() for r in caplog.records]
    assert 'Health: gw changed ok -> fail' in messages
    assert 'Health: throttled changed 0x0 -> 0x50005' in messages
    assert len(messages) == 2
