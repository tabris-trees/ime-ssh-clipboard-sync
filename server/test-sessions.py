"""Regression checks: no live X server or clipboard content is touched."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
IBUS = str(ROOT / 'ibus-x11')
RUN = str(HOME / 'software/bin/ibus-x11-run')
CLIP = str(ROOT / 'clipboard-x11.py')
BRIDGE = str(HOME / 'tools/codex-clipboard-bridge/remote-receive.sh')

class Sessions(unittest.TestCase):
    def test_run_uses_callers_display_and_separate_bus(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Poison the former global environment: it must never be sourced.
            # Stub only daemon discovery; exercise the real run/load/save flow.
            root = Path(tmp)
            tool = root / 'ibus-x11'
            tool.write_text(Path(IBUS).read_text().replace('tool_root="$HOME/tools/codex-ibus-x11"', 'tool_root="' + tmp + '"'))
            tool.chmod(0o700)
            (root / 'resolve-session.py').write_text("import hashlib,os,sys; key=hashlib.sha256(sys.argv[1].encode()).hexdigest()[:16]; print('export RESOLVED_IBUS_PID='+os.environ['TEST_DAEMON_PID']); print('export IBUS_ADDRESS=unix:test-'+key); print('export DBUS_SESSION_BUS_ADDRESS=test:'+key); print('export DBUS_SESSION_BUS_PID='+os.environ['TEST_DAEMON_PID'])")
            Path(tmp, 'env').write_text("export DISPLAY='old.invalid:0'\n")
            for display in ['localhost:10.0', 'localhost:11.0']:
                key = hashlib.sha256(display.encode()).hexdigest()[:16]
                state = Path(tmp, 'sessions', key)
                state.mkdir(parents=True)
                (state / 'ibus.pid').write_text(str(os.getpid()))
                (state / 'env').write_text("export DISPLAY='wrong.invalid:0'\nexport DBUS_SESSION_BUS_ADDRESS='test:" + key + "'\n")
                env = dict(os.environ, DISPLAY=display, CODEX_IBUS_STATE_ROOT=tmp, TEST_DAEMON_PID=str(os.getpid()))
                result = subprocess.run([str(tool), 'run', '/usr/bin/python3', '-c', 'import os,json; print(json.dumps([os.environ["DISPLAY"],os.environ["DBUS_SESSION_BUS_ADDRESS"]]))'], env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), [display, 'test:' + key])

    def test_resolver_rejects_other_display_and_stale_process(self):
        import runpy
        resolve = runpy.run_path(str(ROOT / 'resolve-session.py'))['resolve']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            buses = root / 'config/ibus/bus'
            buses.mkdir(parents=True)
            proc = root / 'proc'
            for pid, display in [('101', 'localhost:10.0'), ('102', 'localhost:11.0')]:
                d = proc / pid
                d.mkdir(parents=True)
                (d / 'comm').write_text('ibus-daemon\n')
                (d / 'environ').write_bytes(('DISPLAY='+display+'\0XDG_CONFIG_HOME='+str(root/'config')+'\0DBUS_SESSION_BUS_ADDRESS=bus-'+pid+'\0').encode())
                (buses / pid).write_text('IBUS_DAEMON_PID='+pid+'\nIBUS_ADDRESS=ibus-'+pid+'\n')
            self.assertEqual(resolve('localhost:10.0', buses, proc)['IBUS_ADDRESS'], 'ibus-101')
            self.assertEqual(resolve('localhost:11.0', buses, proc)['IBUS_ADDRESS'], 'ibus-102')
            (proc / '101/comm').write_text('unrelated-process\n')
            self.assertIsNone(resolve('localhost:10.0', buses, proc))
            self.assertIsNone(resolve('missing:0', buses, proc))

    def test_missing_display_never_uses_saved_ip(self):
        env = dict(os.environ)
        for name in ['DISPLAY', 'CODEX_IBUS_DISPLAY', 'REMOTE_DISPLAY']:
            env.pop(name, None)
        for command in [[RUN, '/bin/true'], ['/usr/bin/python3', CLIP, 'status'], [BRIDGE, 'verify-image']]:
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('DISPLAY', result.stdout + result.stderr)

    def test_clipboard_owners_and_cache_are_isolated(self):
        values = []
        code = 'import runpy,json; d=runpy.run_path(' + repr(CLIP) + '); print(json.dumps([str(d[k]) for k in ["STATE_DIR","CACHE_DIR","PID_FILE","LOCK_FILE"]]))'
        for display in ['localhost:10.0', 'localhost:11.0']:
            result = subprocess.run(['/usr/bin/python3', '-c', code], env=dict(os.environ, DISPLAY=display), capture_output=True, text=True, check=True)
            values.append(json.loads(result.stdout))
        self.assertTrue(all(a != b for a, b in zip(*values)))

if __name__ == '__main__':
    unittest.main()
