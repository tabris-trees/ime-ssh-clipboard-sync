"""Find a live IBus daemon for the exact X display, without trusting stale PID files."""
import os
from pathlib import Path
import shlex
import sys


def resolve(display, bus_dir, proc_root=Path('/proc')):
    for bus in sorted(Path(bus_dir).glob('*')):
        if not bus.is_file():
            continue
        fields = dict(line.split('=', 1) for line in bus.read_text().splitlines()
                      if '=' in line and not line.startswith('#'))
        pid = fields.get('IBUS_DAEMON_PID', '')
        address = fields.get('IBUS_ADDRESS', '')
        if not pid.isdigit() or not address:
            continue
        process = proc_root / pid
        try:
            if process.stat().st_uid != os.getuid():
                continue
            if (process / 'comm').read_text().strip() != 'ibus-daemon':
                continue
            env = dict(x.split('=', 1) for x in (process / 'environ').read_bytes().decode().split('\0') if '=' in x)
            if env.get('DISPLAY') != display:
                continue
            if env.get('XDG_CONFIG_HOME') != str(Path(bus_dir).parent.parent):
                continue
            return dict(IBUS_ADDRESS=address, RESOLVED_IBUS_PID=pid,
                        DBUS_SESSION_BUS_ADDRESS=env.get('DBUS_SESSION_BUS_ADDRESS', ''),
                        DBUS_SESSION_BUS_PID=env.get('DBUS_SESSION_BUS_PID', ''))
        except (OSError, UnicodeError):
            continue
    return None

if __name__ == '__main__':
    result = resolve(sys.argv[1], sys.argv[2])
    if result is None:
        sys.exit(1)
    for key, value in result.items():
        print('export ' + key + '=' + shlex.quote(value))
