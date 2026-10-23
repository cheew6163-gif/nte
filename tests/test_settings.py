from pathlib import Path
import json
import tempfile
import unittest
from yihuan.settings import load_config

ROOT = Path(__file__).resolve().parent.parent


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        c = load_config(ROOT / "config.json")
        self.assertEqual(c["timings"]["battle_watch"], 14)
        self.assertEqual(c["max_loops"], 0)
        self.assertEqual(c["screen_width"], 3840)

    def test_invalid_settings_refused(self):
        for key, value in [("poll_interval", 0), ("stable_frames", 1), ("max_loops", -1),
                           ("window_title_regex", ""), ("capture_backend", "bad"), ("screen_width", 1920)]:
            with self.subTest(key=key), tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
                c = load_config(ROOT / "config.json")
                c[key] = value
                path = Path(directory) / "bad.json"
                path.write_text(json.dumps(c), encoding="utf-8")
                with self.assertRaises(ValueError): load_config(path)
