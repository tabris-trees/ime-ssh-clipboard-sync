import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parent / 'ibus-x11'
FUNCTIONS = SCRIPT.read_text().split('case "${1:-status}" in')[0]

class Startup(unittest.TestCase):
    def run_case(self, stub):
        with tempfile.TemporaryDirectory() as tmp:
            code = FUNCTIONS + '\nstate_dir="$TEST_STATE"\nchoose_chinese_engine() { echo msime-linux; }\nsleep() { :; }\n' + stub
            return subprocess.run(['/bin/bash','-c',code],env=dict(os.environ,DISPLAY='test:1',TEST_STATE=tmp),capture_output=True,text=True,timeout=5)
    def test_waits_for_late_engine(self):
        r=self.run_case('''calls=0
set_engine() { calls=$((calls+1)); [[ $calls -ge 3 ]]; }
ibus() { echo unexpected; return 1; }
initialize_engine
[[ $calls == 3 ]]
''')
        self.assertEqual(r.returncode,0,r.stderr)
    def test_falls_back_without_changing_preference(self):
        r=self.run_case('''echo msime-linux > "$state_dir/chinese-engine"
set_engine() { return 1; }
ibus() { [[ $2 == libpinyin ]]; }
initialize_engine
[[ $(cat "$state_dir/chinese-engine") == msime-linux ]]
''')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn("using 'libpinyin'",r.stderr)
    def test_all_engines_failed_is_reported(self):
        r=self.run_case('''set_engine() { return 1; }
ibus() { return 1; }
initialize_engine
''')
        self.assertNotEqual(r.returncode,0)
        self.assertIn('could not activate any',r.stderr)

if __name__ == '__main__': unittest.main()
