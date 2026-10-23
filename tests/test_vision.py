from pathlib import Path
import threading
import unittest
import cv2
import numpy as np
from yihuan.core import Action, Intent, Phase, Scene
from yihuan.vision import Detector, StableScene, guarded_action, read_image
from yihuan.windows import ActionChanged, GameWindow


ROOT = Path(__file__).resolve().parent.parent
SCENES = {name.lower(): scene for name, scene in Scene.__members__.items() if scene != Scene.UNKNOWN}


class GuardActor:
    window = GameWindow(1, "异环", 0, 0, 3840, 2160)

    def __init__(self):
        self.actions = []

    def perform(self, command, points, before_press=None):
        if before_press is not None and not before_press():
            raise ActionChanged("动作前画面已变化")
        self.actions.append((command.action, points))


class VisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.detector = Detector(ROOT / "assets")

    def test_six_reference_states(self):
        for name, scene in SCENES.items():
            with self.subTest(name=name):
                self.assertEqual(self.detector.detect(read_image(ROOT / f"tests/frames/{name}.jpg")).scene, scene)

    def test_4k_scaling_compression_and_brightness(self):
        for name, scene in SCENES.items():
            base = read_image(ROOT / f"tests/frames/{name}.jpg")
            large = cv2.resize(base, (3840, 2160), interpolation=cv2.INTER_CUBIC)
            ok, encoded = cv2.imencode(".jpg", large, [cv2.IMWRITE_JPEG_QUALITY, 65])
            self.assertTrue(ok)
            recompressed = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
            darkened = np.clip(recompressed.astype(np.float32) * .80 + 8, 0, 255).astype(np.uint8)
            for label, image in [("4k", large), ("jpeg", recompressed), ("brightness", darkened)]:
                with self.subTest(scene=name, transform=label):
                    self.assertEqual(self.detector.detect(image).scene, scene)

    def test_shifted_button_returns_shifted_center(self):
        base = read_image(ROOT / "tests/frames/activity.jpg")
        original = self.detector.detect(base).targets["go"]
        moved = cv2.warpAffine(base, np.float32([[1, 0, 6], [0, 1, -5]]), (1499, 843))
        observation = self.detector.detect(moved)
        self.assertEqual(observation.scene, Scene.ACTIVITY)
        self.assertAlmostEqual(observation.targets["go"][0], original[0] + 6)
        self.assertAlmostEqual(observation.targets["go"][1], original[1] - 5)

    def test_black_blank_noise_are_unknown(self):
        for image in (np.zeros((843, 1499, 3), np.uint8), np.full((843, 1499, 3), 255, np.uint8),
                      np.random.default_rng(42).integers(0, 256, (843, 1499, 3), dtype=np.uint8)):
            self.assertEqual(self.detector.detect(image).scene, Scene.UNKNOWN)

    def test_partial_result_is_not_mistaken_for_battle(self):
        image = read_image(ROOT / "tests/frames/defeat.jpg")
        image[90:185, 670:850] = 0
        self.assertEqual(self.detector.detect(image).scene, Scene.UNKNOWN)

    def test_confirm_text_alone_is_not_a_leave_dialog(self):
        image = read_image(ROOT / "tests/frames/dialog.jpg")
        image[398:448, 635:873] = 225
        self.assertEqual(self.detector.detect(image).scene, Scene.UNKNOWN)

    def test_dialog_takes_priority_over_result_background(self):
        image = read_image(ROOT / "tests/frames/defeat.jpg")
        dialog = read_image(ROOT / "tests/frames/dialog.jpg")
        image[245:596] = dialog[245:596]
        self.assertEqual(self.detector.detect(image).scene, Scene.DIALOG)

    def test_wrong_aspect_ratio_rejected(self):
        with self.assertRaises(ValueError):
            self.detector.detect(np.zeros((600, 800, 3), np.uint8))

    def test_stability_requires_consecutive_matching_states(self):
        gate = StableScene(2)
        def push(name):
            return gate.push(self.detector.detect(read_image(ROOT / f"tests/frames/{name}.jpg")))
        self.assertFalse(push("battle"))
        self.assertFalse(push("victory"))
        self.assertTrue(push("victory"))

    def test_race_victory_before_escape_drops_battle_action(self):
        event = threading.Event()
        actor = type("Actor", (), {"perform": lambda self, *args: (_ for _ in ()).throw(AssertionError("must not press"))})()
        command = Intent(Action.ESC_BATTLE, Scene.BATTLE, Phase.WATCH)
        self.assertFalse(guarded_action(command, lambda: read_image(ROOT / "tests/frames/victory.jpg"),
                                       self.detector, actor, event))

    def test_stopped_guard_does_not_capture_or_press(self):
        event = threading.Event()
        event.set()
        def forbidden():
            raise AssertionError("capture after stop")
        self.assertFalse(guarded_action(Intent(Action.GO, Scene.ACTIVITY, Phase.READY), forbidden,
                                       self.detector, None, event))

    def test_stop_arriving_during_capture_prevents_action(self):
        event = threading.Event()
        def capture():
            event.set()
            return read_image(ROOT / "tests/frames/activity.jpg")
        self.assertFalse(guarded_action(Intent(Action.GO, Scene.ACTIVITY, Phase.READY), capture,
                                       self.detector, None, event))

    def test_guard_executes_confirm_only_on_valid_dialog(self):
        actor = GuardActor()
        command = Intent(Action.CONFIRM_BATTLE, Scene.DIALOG, Phase.BATTLE_DIALOG)
        self.assertTrue(guarded_action(command, lambda: read_image(ROOT / "tests/frames/dialog.jpg"),
                                      self.detector, actor, threading.Event()))
        self.assertEqual(actor.actions[0][0], Action.CONFIRM_BATTLE)
        self.assertTrue(880 < actor.actions[0][1]["confirm"][0] < 935)

    def test_page_changes_during_positioning_cancel_click_and_record_both_checks(self):
        actor, validations = GuardActor(), []
        frames = [read_image(ROOT / f"tests/frames/{name}.jpg") for name in ("activity", "match")]
        captures = iter(frames)
        command = Intent(Action.GO, Scene.ACTIVITY, Phase.READY)
        self.assertFalse(guarded_action(command, lambda: next(captures), self.detector, actor,
                                       threading.Event(), on_validation=lambda image, obs: validations.append((image, obs))))
        self.assertEqual(actor.actions, [])
        self.assertEqual([obs.scene for _, obs in validations], [Scene.ACTIVITY, Scene.MATCH])
        self.assertIs(validations[-1][0], frames[-1])

    def test_button_motion_threshold_uses_actual_game_pixels(self):
        base = read_image(ROOT / "tests/frames/activity.jpg")
        command = Intent(Action.GO, Scene.ACTIVITY, Phase.READY)
        # 参考图1像素约等于4K游戏2.56像素；参考图2像素已超出4实际像素。
        for shift, allowed in ((1, True), (2, False)):
            with self.subTest(reference_shift=shift):
                moved = cv2.warpAffine(base, np.float32([[1, 0, shift], [0, 1, 0]]), (1499, 843))
                captures, actor = iter((base, moved)), GuardActor()
                self.assertEqual(guarded_action(command, lambda: next(captures), self.detector,
                                                actor, threading.Event()), allowed)
                self.assertEqual(len(actor.actions), int(allowed))

    def test_result_appearing_during_final_escape_check_prevents_escape(self):
        captures = iter(read_image(ROOT / f"tests/frames/{name}.jpg") for name in ("battle", "victory"))
        actor = GuardActor()
        command = Intent(Action.ESC_BATTLE, Scene.BATTLE, Phase.WATCH)
        self.assertFalse(guarded_action(command, lambda: next(captures), self.detector,
                                       actor, threading.Event()))
        self.assertEqual(actor.actions, [])

    def test_stop_during_final_validation_prevents_press(self):
        event, actor, calls = threading.Event(), GuardActor(), []
        frame = read_image(ROOT / "tests/frames/activity.jpg")
        def capture():
            calls.append(1)
            if len(calls) == 2:
                event.set()
            return frame
        command = Intent(Action.GO, Scene.ACTIVITY, Phase.READY)
        self.assertFalse(guarded_action(command, capture, self.detector, actor, event))
        self.assertEqual(len(calls), 2)
        self.assertEqual(actor.actions, [])

    def test_input_failure_is_not_treated_as_page_change(self):
        class FailingActor(GuardActor):
            def perform(self, *args, **kwargs):
                raise RuntimeError("native input failed")
        command = Intent(Action.GO, Scene.ACTIVITY, Phase.READY)
        with self.assertRaisesRegex(RuntimeError, "native input failed"):
            guarded_action(command, lambda: read_image(ROOT / "tests/frames/activity.jpg"),
                           self.detector, FailingActor(), threading.Event())


if __name__ == "__main__":
    unittest.main()
