import tempfile
import unittest
from pathlib import Path

from studio.gxt.model import Gxt
from test_app_generation import fixture_gxt


class GxtRoundtripTests(unittest.TestCase):
    def test_unchanged_file_preserves_unused_trailing_bytes(self):
        original = fixture_gxt() + b'\0\0unused padding\0'
        gxt = Gxt('', data=original)
        self.assertEqual(gxt.build(), original)

    def test_edit_save_then_noop_uses_new_saved_bytes(self):
        original = fixture_gxt() + b'\0padding'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'american.gxt'
            path.write_bytes(original)
            gxt = Gxt(str(path))
            gxt.t['MAIN']['cur'][123] = b'New text'
            changed = gxt.build()
            gxt.validar(changed)
            self.assertNotEqual(changed, original)
            gxt.save(path)
            self.assertEqual(gxt.build(), path.read_bytes())
            self.assertEqual(Path(str(path) + '.bak').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
