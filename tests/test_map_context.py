"""Offline checks for the “残丝长巷” label based bonfire localization."""
from pathlib import Path
import json
import unittest
import cv2
import numpy as np
from yihuan.nte999 import Detector999, Scene999
from yihuan.vision import read_image

ROOT = Path(__file__).resolve().parent.parent
REFERENCE = (746, 424)
CONFIRMED_ZOOM = (749, 211)
ACTUAL_DIAGNOSTIC = (728.5, 358.5)


class MapLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.detector = Detector999(ROOT / "assets999")
        cls.reference = read_image(ROOT / "tests/frames999/map_reference.png")
        cls.confirmed = read_image(ROOT / "tests/frames999/map_target_confirmed.png")
        cls.actual = read_image(ROOT / "diagnostics/20261023_130141.png")

    def assert_target(self, image, expected, tolerance=12):
        found = self.detector.detect(image)
        self.assertEqual(found.scene, Scene999.MAP)
        self.assertEqual(found.flags["target_source"], "map_label")
        self.assertIn("bonfire", found.targets)
        np.testing.assert_allclose(found.targets["bonfire"], expected, atol=tolerance)
        return found

    def test_reference_frame_selects_confirmed_bonfire_from_label(self):
        found = self.assert_target(self.reference, REFERENCE)
        self.assertGreaterEqual(found.scores["map_label"], .8)
        self.assertGreater(found.scores["map_label"] - found.scores["map_label_second"], .12)
        self.assertEqual(found.flags["text_source"], "full")
        self.assertNotIn("map_context", found.scores)
        self.assertNotIn("selected_ring", found.scores)

    def test_player_marker_overlap_uses_right_part_fallback(self):
        frame = self.reference.copy()
        cv2.fillPoly(frame, [np.array([[785, 455], [814, 470], [785, 505]], np.int32)],
                     (80, 220, 250))
        found = self.assert_target(frame, REFERENCE)
        self.assertEqual(found.flags["text_source"], "fallback")
        self.assertGreaterEqual(found.scores["map_label_fallback"], .8)
        self.assertIn("bonfire", found.targets)

    def test_confirmed_and_actual_frames_with_different_map_views(self):
        for name, image, expected in (
                ("confirmed", self.confirmed, CONFIRMED_ZOOM),
                ("actual", self.actual, ACTUAL_DIAGNOSTIC)):
            with self.subTest(frame=name):
                self.assert_target(image, expected, tolerance=10)

    def test_map_pan_relocates_label_and_target_together(self):
        small = cv2.resize(self.actual, (1499, 843), interpolation=cv2.INTER_AREA)
        for dx, dy in ((45, -25), (-75, 40)):
            with self.subTest(translation=(dx, dy)):
                frame = small.copy()
                area = frame[0:843, 0:1080].copy()
                frame[0:843, 0:1080] = cv2.warpAffine(
                    area, np.float32([[1, 0, dx], [0, 1, dy]]), (1080, 843))
                self.assert_target(frame, (ACTUAL_DIAGNOSTIC[0] + dx,
                                           ACTUAL_DIAGNOSTIC[1] + dy), tolerance=10)

    def test_missing_label_is_unknown_with_reason(self):
        frame = cv2.resize(self.actual, (1499, 843), interpolation=cv2.INTER_AREA)
        frame[400:440, 770:900] = 0
        found = self.detector.detect(frame)
        self.assertEqual(found.scene, Scene999.UNKNOWN)
        self.assertNotIn("bonfire", found.targets)
        self.assertIn("残丝", found.flags["map_failure_reason"])

    def test_duplicate_label_is_rejected_as_ambiguous(self):
        frame = cv2.resize(self.actual, (1499, 843), interpolation=cv2.INTER_AREA)
        patch = frame[387:447, 762:902].copy()
        frame[600:660, 300:440] = patch
        found = self.detector.detect(frame)
        self.assertEqual(found.scene, Scene999.UNKNOWN)
        self.assertNotIn("bonfire", found.targets)
        self.assertIn("多个", found.flags["map_failure_reason"])

    def test_label_without_bonfire_is_map_but_not_clickable(self):
        frame = cv2.resize(self.actual, (1499, 843), interpolation=cv2.INTER_AREA)
        frame[330:385, 700:760] = 40
        found = self.detector.detect(frame)
        self.assertEqual(found.scene, Scene999.MAP)
        self.assertTrue(found.flags["text_found"])
        self.assertNotIn("bonfire", found.targets)
        self.assertIn("篝火", found.flags["map_failure_reason"])

    def test_other_pink_marker_outside_local_window_is_not_used(self):
        frame = cv2.resize(self.actual, (1499, 843), interpolation=cv2.INTER_AREA)
        # 这是“残丝长巷”左侧的另一个粉色标记；它不在基准相对窗口/半径内。
        cv2.circle(frame, (602, 414), 10, (80, 80, 230), -1)
        found = self.assert_target(frame, ACTUAL_DIAGNOSTIC, tolerance=10)
        self.assertNotEqual(found.targets["bonfire"], (602.0, 414.0))

    def test_jpeg_brightness_and_bgra_variants(self):
        small = cv2.resize(self.actual, (1499, 843), interpolation=cv2.INTER_AREA)
        variants = (
            cv2.imdecode(cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 75])[1],
                         cv2.IMREAD_COLOR),
            cv2.convertScaleAbs(small, alpha=.75, beta=4),
            cv2.convertScaleAbs(small, alpha=1.15, beta=10),
            cv2.cvtColor(small, cv2.COLOR_BGR2BGRA),
        )
        for index, frame in enumerate(variants):
            with self.subTest(variant=index):
                self.assert_target(frame, ACTUAL_DIAGNOSTIC, tolerance=10)

    def test_blank_frame_is_unknown(self):
        found = self.detector.detect(np.zeros((2160, 3840, 3), dtype=np.uint8))
        self.assertEqual(found.scene, Scene999.UNKNOWN)

    def test_observations_are_serializable(self):
        found = self.assert_target(self.actual, ACTUAL_DIAGNOSTIC)
        json.dumps({"targets": found.targets, "scores": found.scores, "flags": found.flags})


if __name__ == "__main__":
    unittest.main()
