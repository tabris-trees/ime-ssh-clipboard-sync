#!/usr/bin/python3
"""Cycle the private IBus engines with Ctrl+Shift+S on this X display."""
import fcntl
import hashlib
import os
from pathlib import Path
import select
import sys
import time
from Xlib import X, XK, display
import gi
gi.require_version('IBus', '1.0')
from gi.repository import IBus, GLib

key = hashlib.sha256(os.environ['DISPLAY'].encode()).hexdigest()[:16]
state_root = Path(os.environ.get(
    'CODEX_IBUS_STATE_ROOT',
    str(Path.home() / '.local/state/codex-ibus-x11'),
))
state = state_root/'sessions'/key
lock = (state/'engine-hotkey.lock').open('a')
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(0)
IBus.init()
bus = IBus.Bus.new()
xd = display.Display()
root = xd.screen().root
code = xd.keysym_to_keycode(XK.string_to_keysym('s'))
errors = []
for extra in [0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask]:
    root.grab_key(code, X.ControlMask | X.ShiftMask | extra, False,
                  X.GrabModeAsync, X.GrabModeAsync, onerror=lambda e,r: errors.append(str(e)))
xd.sync()
if errors:
    raise RuntimeError('Ctrl+Shift+S is already grabbed: '+str(errors))
(state/'engine-hotkey.pid').write_text(str(os.getpid()))
print('Ctrl+Shift+S ready for '+os.environ['DISPLAY'], flush=True)
engines = ['libpinyin', 'xkb:us::eng', 'msime-linux']
last = 0.0
while bus.is_connected():
    while GLib.MainContext.default().pending():
        GLib.MainContext.default().iteration(False)
    select.select([xd.fileno()], [], [], .3)
    while xd.pending_events():
        event = xd.next_event()
        if event.type != X.KeyPress or time.monotonic()-last < .35:
            continue
        last = time.monotonic()
        current = bus.get_global_engine()
        name = current.get_name() if current else ''
        target = engines[(engines.index(name)+1)%len(engines)] if name in engines else engines[0]
        if bus.set_global_engine(target):
            if target in ['libpinyin','msime-linux']:
                tmp = state/('chinese-engine.tmp.'+str(os.getpid()))
                tmp.write_text(target+'\n'); tmp.replace(state/'chinese-engine')
            print(name+' -> '+target, flush=True)
xd.close()
