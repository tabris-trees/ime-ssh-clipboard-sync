#!/usr/bin/python3
"""Route an SSH IME request to this client's live private X11 session."""
import argparse
import importlib.util
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('resolver', ROOT / 'resolve-session.py')
resolver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resolver)
CONFIG = Path.home() / '.config/codex-ibus-x11'

def select_display(explicit, peer, sessions):
    if explicit:
        matches = {d for d, _ in sessions if d == explicit}
    elif peer:
        # A direct X server address is stronger evidence than a historical SSH
        # origin (one client may also have an old forwarded display).
        matches = {d for d, _ in sessions if d.rsplit(':', 1)[0] == peer}
        if not matches:
            matches = {d for d, origin in sessions if origin == peer}
    else:
        matches = set()
    if len(matches) != 1:
        raise ValueError('Cannot identify one live IBus display for this SSH client. '
                         'Launch ibus-x11-run codex-desktop from your X11 terminal; '
                         'if multiple sessions exist, set RemoteDisplay in sync-config.json '
                         'to the DISPLAY printed by that terminal. Candidates: ' + repr(sorted(matches)))
    return matches.pop()

def live_sessions():
    sessions = []
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            if proc.stat().st_uid != os.getuid() or (proc/'comm').read_text().strip() != 'ibus-daemon':
                continue
            env = dict(x.split('=', 1) for x in (proc/'environ').read_text().split('\0') if '=' in x)
            display = env.get('DISPLAY', '')
            if not display or env.get('XDG_CONFIG_HOME') != str(CONFIG):
                continue
            found = resolver.resolve(display, CONFIG/'ibus/bus')
            if not found or found['RESOLVED_IBUS_PID'] != proc.name:
                continue
            origin = env.get('SSH_CONNECTION', '').split()
            sessions.append((display, origin[0] if origin else ''))
        except (OSError, UnicodeError):
            continue
    return sessions

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['cn', 'en', 'status'])
    parser.add_argument('--display', default='')
    args = parser.parse_args()
    connection = os.environ.get('SSH_CONNECTION', '').split()
    try:
        display = select_display(args.display or os.environ.get('DISPLAY') or os.environ.get('CODEX_IBUS_DISPLAY'),
                                 connection[0] if connection else '', live_sessions())
    except ValueError as error:
        sys.exit(str(error))
    os.environ['DISPLAY'] = display
    os.execv(str(ROOT/'ibus-x11'), [str(ROOT/'ibus-x11'), args.mode])

if __name__ == '__main__':
    main()
