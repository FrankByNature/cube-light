"""System health log, for working out what happened when the Pi drops off
the network.

While the server runs, its main process samples the Pi every INTERVAL seconds
and appends one line to `health.log` next to this file: load, free memory,
CPU temperature, the firmware's under-voltage/throttling flags, the Wi-Fi link,
whether the router still answers a ping, Bluetooth, and the server, runner and
controller-bridge processes. Each line is fsync'd, so the last sample survives
the Pi hanging or losing power straight afterwards (the journal, by default,
only reaches the SD card every few minutes). The file sits in the shared
project folder, so it can be read from another machine once the Pi is back.

Reading it after an outage:

- START marks every server start and STOP every clean shutdown, so a START with
  no STOP before it means the previous run ended in a crash, hang or power cut.
- Lines carry on through the outage with `gw=fail` or `wlan=down`: the Pi was
  running and only its network was gone.
- Lines simply stop: the whole Pi hung or lost power, at about the last line.
- `throttled=` anything other than 0x0: the firmware has seen under-voltage or
  throttling since boot (bit 0x1 / 0x10000 = under-voltage now / since boot).

Changes in the link, router, Bluetooth, power flags and processes are also
logged to the journal as warnings. Anything that can't be read (no /proc on a
dev machine, no vcgencmd...) is simply left out of the line.

Set CUBE_HEALTH_LOG=0 to turn it off.
"""
import logging
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time

log = logging.getLogger('health')

HEALTH_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'health.log')
INTERVAL = 30            # seconds between samples
MAX_BYTES = 2_000_000    # rotate to health.log.1 past this (about ten days)

# Fields whose changes are worth a warning in the journal
WATCHED = ('wlan', 'gw', 'bt', 'throttled', 'runner', 'bridge')


def _read(root, path):
    try:
        with open(os.path.join(root, path), encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return None


def _run(cmd, timeout=3):
    """stdout of a command, or None if it is missing, fails or times out.
    Looks in the sbin directories too, which aren't on every user's PATH."""
    exe = shutil.which(cmd[0])
    if exe is None:
        for d in ('/usr/sbin', '/sbin', '/usr/bin'):
            if os.path.exists(os.path.join(d, cmd[0])):
                exe = os.path.join(d, cmd[0])
                break
    if exe is None:
        return None
    try:
        return subprocess.run([exe] + list(cmd[1:]), capture_output=True, text=True,
                              timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return None


# ----- individual readings -------------------------------------------------

def uptime_s(root='/'):
    text = _read(root, 'proc/uptime')
    return float(text.split()[0]) if text else None


def load1(root='/'):
    text = _read(root, 'proc/loadavg')
    return float(text.split()[0]) if text else None


def memory_mb(root='/'):
    """(available MB, swap used MB) or (None, None)."""
    text = _read(root, 'proc/meminfo')
    if not text:
        return None, None
    kb = {}
    for line in text.splitlines():
        m = re.match(r'(\w+):\s+(\d+)', line)
        if m:
            kb[m.group(1)] = int(m.group(2))
    avail = kb.get('MemAvailable')
    swap = (kb['SwapTotal'] - kb['SwapFree']) if 'SwapTotal' in kb and 'SwapFree' in kb else None
    return (avail // 1024 if avail is not None else None,
            swap // 1024 if swap is not None else None)


def cpu_temp_c(root='/'):
    text = _read(root, 'sys/class/thermal/thermal_zone0/temp')
    try:
        return int(text) / 1000 if text else None
    except ValueError:
        return None


def wlan_state(root='/', iface='wlan0'):
    """'up' / 'down' / 'dormant'... from the kernel, or None if no such interface."""
    text = _read(root, f'sys/class/net/{iface}/operstate')
    return text.strip() if text else None


def wifi_signal(root='/', iface='wlan0'):
    """(link quality, signal dBm) from /proc/net/wireless, or (None, None)."""
    text = _read(root, 'proc/net/wireless')
    if not text:
        return None, None
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(':')
        if name.strip() == iface:
            parts = rest.split()
            try:
                return int(float(parts[1])), int(float(parts[2]))
            except (IndexError, ValueError):
                return None, None
    return None, None


def default_gateway(root='/'):
    """IP of the default route's gateway, from /proc/net/route, or None."""
    text = _read(root, 'proc/net/route')
    if not text:
        return None
    best = None
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 7 or f[1] != '00000000':
            continue
        try:
            gw, flags, metric = int(f[2], 16), int(f[3], 16), int(f[6])
        except ValueError:
            continue
        if flags & 0x2 and (best is None or metric < best[0]):   # RTF_GATEWAY
            best = (metric, gw)
    return socket.inet_ntoa(struct.pack('<L', best[1])) if best else None


def ping_ms(host, run=_run):
    """Round trip in ms, False if there was no reply, None if ping couldn't run."""
    out = run(['ping', '-c', '1', '-W', '2', host], timeout=4)
    if out is None:
        return None
    m = re.search(r'time[=<]([\d.]+)\s*ms', out)
    return float(m.group(1)) if m else False


def throttled(run=_run):
    """The firmware's throttle flags, e.g. '0x0' or '0x50005', or None."""
    out = run(['vcgencmd', 'get_throttled'])
    m = re.search(r'throttled=(0x[0-9a-fA-F]+)', out or '')
    return m.group(1) if m else None


def wifi_power_save(run=_run, iface='wlan0'):
    """'on' / 'off', or None. Power saving is a known cause of a Pi dropping
    off Wi-Fi after a spell of inactivity."""
    out = run(['iw', 'dev', iface, 'get', 'power_save'])
    m = re.search(r'Power save:\s*(\w+)', out or '')
    return m.group(1) if m else None


def bluetooth_state(root='/'):
    if os.path.exists(os.path.join(root, 'sys/class/bluetooth/hci0')):
        return 'up'
    return 'none' if os.path.exists(os.path.join(root, 'sys/class')) else None


def process_rss_mb(pid, root='/'):
    """Resident memory of a process in MB, or None if it isn't there."""
    if pid is None:
        return None
    text = _read(root, f'proc/{pid}/status')
    m = re.search(r'VmRSS:\s+(\d+)', text or '')
    return int(m.group(1)) // 1024 if m else None


# ----- one sample ----------------------------------------------------------

def snapshot(root='/', run=_run, extra=None):
    """All readings as an ordered dict; values that couldn't be read are None."""
    up = uptime_s(root)
    load = load1(root)
    avail, swap = memory_mb(root)
    temp = cpu_temp_c(root)
    quality, signal = wifi_signal(root)
    gateway = default_gateway(root)
    gw = ping_ms(gateway, run) if gateway else None
    snap = {
        'up': f'{up / 3600:.1f}h' if up is not None else None,
        'load': f'{load:.2f}' if load is not None else None,
        'mem_free': f'{avail}MB' if avail is not None else None,
        'swap': f'{swap}MB' if swap is not None else None,
        'temp': f'{temp:.0f}C' if temp is not None else None,
        'throttled': throttled(run),
        'wlan': wlan_state(root),
        'signal': f'{signal}dBm' if signal is not None else None,
        'quality': quality,
        'gw': (f'{gw:.0f}ms' if isinstance(gw, float) else 'fail' if gw is False else None),
        'bt': bluetooth_state(root),
    }
    if extra is not None:
        try:
            snap.update(extra())
        except Exception as e:   # a broken callback must not stop the samples
            snap['extra_error'] = type(e).__name__
    return snap


def format_line(snap, tag=None, when=None):
    """'<local time> [TAG] key=value ...', leaving out values that are None."""
    words = [time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(when))]
    if tag:
        words.append(tag)
    words += [f'{k}={v}' for k, v in snap.items() if v is not None]
    return ' '.join(words)


def status(snap):
    """The watched fields, with the router reduced to ok/fail so a changing
    round-trip time doesn't count as a change."""
    s = {k: snap.get(k) for k in WATCHED}
    if s['gw'] not in (None, 'fail'):
        s['gw'] = 'ok'
    return s


# ----- the logger thread ---------------------------------------------------

class HealthLogger(threading.Thread):
    def __init__(self, path=HEALTH_LOG, interval=INTERVAL, extra=None,
                 max_bytes=MAX_BYTES, root='/', run=_run):
        super().__init__(daemon=True, name='health-log')
        self.path = path
        self.interval = interval
        self.extra = extra
        self.max_bytes = max_bytes
        self.root = root
        self.runner = run
        self._stop_event = threading.Event()
        self._last = None
        self._write_failed = False
        self._sample_failed = False

    def write(self, line):
        """Append one line and force it to disk."""
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > self.max_bytes:
                os.replace(self.path, self.path + '.1')
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            if not self._write_failed:
                log.warning("Could not write %s: %s", self.path, e)
                self._write_failed = True

    def start_line(self):
        boot = (_read(self.root, 'proc/sys/kernel/random/boot_id') or '').strip()[:8]
        kernel = (_read(self.root, 'proc/sys/kernel/osrelease') or '').strip()
        return format_line({'boot': boot or None, 'kernel': kernel or None,
                            'wifi_power_save': wifi_power_save(self.runner)}, tag='START')

    def sample(self):
        try:
            snap = snapshot(self.root, self.runner, self.extra)
        except Exception:
            if not self._sample_failed:
                log.exception("Health sample failed")
                self._sample_failed = True
            return
        self.write(format_line(snap))
        now = status(snap)
        if self._last is not None:
            for key, value in now.items():
                if value != self._last[key]:
                    log.warning("Health: %s changed %s -> %s", key, self._last[key], value)
        self._last = now

    def run(self):
        line = self.start_line()
        self.write(line)
        log.info("Health log: %s every %ds (%s)", self.path, self.interval, line[26:])
        self.sample()
        while not self._stop_event.wait(self.interval):
            self.sample()

    def stop(self):
        """Stop sampling and mark a clean shutdown in the log."""
        self._stop_event.set()
        self.write(format_line({}, tag='STOP'))
