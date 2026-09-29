import runpy
from pathlib import Path
import unittest

select = runpy.run_path(str(Path(__file__).resolve().parent / 'ssh-dispatch.py'))['select_display']

class Routing(unittest.TestCase):
    def test_direct_address_beats_old_forward(self):
        self.assertEqual(select('', '192.0.2.10', [('192.0.2.10:0.0','192.0.2.10'), ('localhost:10.0','192.0.2.10')]), '192.0.2.10:0.0')
    def test_forwarded_session_uses_ssh_origin(self):
        self.assertEqual(select('', '192.0.2.20', [('localhost:10.0','192.0.2.10'), ('localhost:11.0','192.0.2.20')]), 'localhost:11.0')
    def test_ambiguity_or_unknown_client_fails(self):
        for peer in ['192.0.2.10', '192.0.2.30', '']:
            with self.assertRaises(ValueError):
                select('', peer, [('localhost:10.0','192.0.2.10'), ('localhost:11.0','192.0.2.10')])
    def test_explicit_must_be_live(self):
        self.assertEqual(select('localhost:11.0', '', [('localhost:11.0','')]), 'localhost:11.0')
        with self.assertRaises(ValueError): select('dead:0', '', [('localhost:11.0','')])

if __name__ == '__main__': unittest.main()
