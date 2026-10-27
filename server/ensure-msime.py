#!/usr/bin/python3
"""Maintain the private session's candidate panel, engines and switch shortcut."""
import fcntl
import hashlib
import os
from pathlib import Path
import subprocess
import time
import gi
gi.require_version('IBus', '1.0')
from gi.repository import IBus, GLib, Gio

HOME = Path.home()
ROOT = Path(__file__).resolve().parent
PREFIX = Path(os.environ.get('MSIME_ROOT', str(HOME / 'software/msime')))
OPTIONS = Path(os.environ.get(
    'MSIME_IBUS_OPTIONS',
    str(HOME / '.config/codex-ibus-x11/msime-client/runtime-options.json'),
))
STATE_ROOT = Path(os.environ.get(
    'CODEX_IBUS_STATE_ROOT',
    str(HOME / '.local/state/codex-ibus-x11'),
))
PANEL_WRAPPER = Path(os.environ.get(
    'CODEX_IBUS_PANEL_WRAPPER',
    str(HOME / 'tools/ibus-ui-gtk3-wrapper.sh'),
))

def main():
    if not OPTIONS.is_file():
        return
    IBus.init()
    bus = IBus.Bus.new()
    if not bus.is_connected():
        raise RuntimeError('Private IBus is not connected')
    key = hashlib.sha256(os.environ['DISPLAY'].encode()).hexdigest()[:16]
    state = STATE_ROOT/'sessions'/key
    state.mkdir(parents=True, exist_ok=True)
    with (state/'integration.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not bus.name_has_owner('org.freedesktop.IBus.Panel'):
            with (state/'panel.log').open('ab') as log:
                subprocess.Popen([str(PANEL_WRAPPER)],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        names = {engine.get_name() for engine in bus.list_active_engines()}
        if 'msime-linux' not in names:
            env = dict(os.environ, MSIME_IBUS_OPTIONS=str(OPTIONS))
            with (state/'msime.log').open('ab') as log:
                subprocess.Popen([str(PREFIX/'bin/msime-linux-ibus-launcher')], env=env,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            for _ in range(50):
                if 'msime-linux' in {e.get_name() for e in bus.list_active_engines()}:
                    break
                time.sleep(.1)
            else:
                raise RuntimeError('MSIME registration failed: '+str(state/'msime.log'))
        config = bus.get_config()
        engines = ['libpinyin', 'xkb:us::eng', 'msime-linux']
        # memconf serves the daemon; GSettings is also read by the GTK panel.
        for section, name, value in [('general', 'preload-engines', engines),
                                     ('general', 'engines-order', engines),
                                     ('general/hotkey', 'triggers', [])]:
            variant = GLib.Variant('as', value)
            if config and config.get_value(section, name) != variant:
                config.set_value(section, name, variant)
        for schema, name, value in [('org.freedesktop.ibus.general', 'preload-engines', engines),
                                   ('org.freedesktop.ibus.general', 'engines-order', engines),
                                   ('org.freedesktop.ibus.general.hotkey', 'triggers', [])]:
            settings = Gio.Settings.new(schema)
            if settings.get_strv(name) != value:
                settings.set_strv(name, value)
        Gio.Settings.sync()
        # X11 grab gives a deterministic three-engine cycle, independent of
        # GTK's MRU switcher and the older daemon's shortcut handling.
        with (state/'engine-hotkey.log').open('ab') as log:
            subprocess.Popen(['/usr/bin/python3', str(ROOT/'engine-hotkey.py')],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


if __name__ == '__main__':
    main()
