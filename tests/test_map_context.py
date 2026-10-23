"""Independent calibration and failure cases for the user-confirmed map target."""
from pathlib import Path
import json
import threading
import unittest
import cv2
import numpy as np
from yihuan.core import Observation
from yihuan.nte999 import Detector999, Scene999, Action999, Intent999, Phase
from yihuan.vision import guarded_action, read_image
from yihuan.windows import ActionChanged, GameWindow

ROOT = Path(__file__).resolve().parent.parent
# Manually mapped from the user's selected bonfire using the neighboring road/buildings.
# These coordinates are independent of detector output.
TARGET = (746, 424)


class MapContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.detector = Detector999(ROOT / "assets999")
        cls.frame = read_image(ROOT / "tests/frames999/map_reference.png")

    def assert_target(self, image, expected=TARGET):
        found = self.detector.detect(image)
        self.assertEqual(found.scene, Scene999.MAP)
        np.testing.assert_allclose(found.targets["bonfire"], expected, atol=1)
        self.assertEqual(found.flags["target_source"], "map_geometry")
        return found

    def test_independent_calibration_selects_confirmed_bonfire(self):
        found = self.assert_target(self.frame)
        self.assertTrue(found.flags["target_selected"])
        self.assertIn("teleport", found.targets)
        self.assertNotIn("arrow", found.flags)
        self.assertNotIn("map_label", self.detector.specs)

    def test_base_zoom_survives_4k_jpeg_brightness_bgra_and_grayscale(self):
        large = cv2.resize(self.frame, (3840,2160))
        _, data = cv2.imencode(".jpg", large, [cv2.IMWRITE_JPEG_QUALITY,80])
        variants = (large, cv2.imdecode(data,cv2.IMREAD_COLOR),
                    cv2.convertScaleAbs(large,alpha=.7,beta=4),
                    cv2.cvtColor(large,cv2.COLOR_BGR2BGRA),
                    cv2.cvtColor(large,cv2.COLOR_BGR2GRAY))
        for i, image in enumerate(variants):
            with self.subTest(format=i):
                self.assertIn("teleport", self.assert_target(image).targets)

    def test_map_pan_relocates_same_world_point(self):
        for dx,dy in ((45,-25),(-75,40)):
            with self.subTest(translation=(dx,dy)):
                frame = self.frame.copy()
                region = frame[130:790,100:1050].copy()
                frame[130:790,100:1050] = cv2.warpAffine(
                    region,np.float32([[1,0,dx],[0,1,dy]]),(950,660))
                self.assert_target(frame,(TARGET[0]+dx,TARGET[1]+dy))

    def test_player_arrow_text_and_target_icon_do_not_define_location(self):
        frame = self.frame.copy()
        cv2.circle(frame,TARGET,39,(60,60,60),-1)
        frame[462:503,785:905] = 0
        cv2.fillPoly(frame,[np.array([[610,470],[625,490],[600,490]])],(0,230,255))
        found = self.assert_target(frame)
        self.assertFalse(found.flags["target_selected"])
        self.assertNotIn("teleport",found.targets)

    def test_other_bonfire_and_wrong_selection_ring_cannot_authorize_teleport(self):
        frame = self.frame.copy()
        cv2.circle(frame,TARGET,39,(60,60,60),-1)
        cv2.circle(frame,(618,481),31,(255,255,255),3)
        found = self.assert_target(frame)
        self.assertTrue(found.flags["panel"])
        self.assertFalse(found.flags["target_selected"])
        self.assertNotIn("teleport",found.targets)

    def test_missing_or_severely_occluded_geometry_rejects_existing_panel(self):
        frame = self.frame.copy()
        frame[290:650,560:920] = 0
        found = self.detector.detect(frame)
        self.assertEqual(found.scene,Scene999.MAP)
        self.assertTrue(found.flags["panel"])
        self.assertNotIn("bonfire",found.targets)
        self.assertNotIn("teleport",found.targets)
        self.assertTrue(found.flags["map_failure_reason"])

    def test_local_occlusion_is_rejected_even_when_global_score_passes(self):
        frame=self.frame.copy()
        frame[290:390,760:860]=60
        found=self.detector.detect(frame)
        self.assertGreaterEqual(found.scores["map_context"],.8)
        self.assertNotIn("bonfire",found.targets)
        self.assertIn("局部地形",found.flags["map_failure_reason"])

    def test_repeated_geometry_rejects_ambiguous_candidates(self):
        frame = self.frame.copy()
        frame[290:650,160:520] = frame[290:650,560:920].copy()
        found = self.detector.detect(frame)
        self.assertNotIn("bonfire",found.targets)
        self.assertNotIn("teleport",found.targets)
        self.assertIn("多个",found.flags["map_failure_reason"])

    def test_real_nonbaseline_zoom_is_rejected(self):
        for name in ("map_other_zoom.png","map_target_confirmed.png"):
            with self.subTest(reference=name):
                found = self.detector.detect(read_image(ROOT/"tests/frames999"/name))
                self.assertEqual(found.scene,Scene999.MAP)
                self.assertNotIn("bonfire",found.targets)
                self.assertNotIn("teleport",found.targets)
                self.assertIn("基准缩放",found.flags["map_failure_reason"])

    def test_ring_without_panel_does_not_create_teleport_target(self):
        frame = self.frame.copy()
        frame[:,1080:] = 0
        found = self.assert_target(frame)
        self.assertNotIn("teleport",found.targets)

    def test_observations_are_serializable(self):
        found = self.assert_target(self.frame)
        json.dumps({"targets":found.targets,"scores":found.scores,"flags":found.flags})


class MapGuardTests(unittest.TestCase):
    def run_guard(self, frames, anchor=TARGET, action=Action999.CLICK_TELEPORT):
        class Detector:
            size = (1499,843)
            def detect(self,_): return next(frames)
        class Actor:
            window = GameWindow(1,"异环",-3840,0,3840,2160)
            def __init__(self): self.clicked=False
            def perform(self,intent,targets,before_press=None):
                if not before_press(): raise ActionChanged("changed")
                self.clicked=True
        actor=Actor()
        intent=Intent999(action,Scene999.MAP,Phase.MAP,
                         target="teleport" if action==Action999.CLICK_TELEPORT else "bonfire",
                         map_anchor=anchor,required_flags=("target_selected",) if action==Action999.CLICK_TELEPORT else ())
        result=guarded_action(intent,lambda: np.zeros((1,1,3),np.uint8),Detector(),actor,threading.Event())
        self.assertEqual(result,actor.clicked)
        return result

    @staticmethod
    def observation(target=TARGET,selected=True):
        targets={"teleport":(1295,755)}
        if target is not None: targets["bonfire"]=target
        return Observation(Scene999.MAP,{},targets,{"target_selected":selected})

    def test_unchanged_geometry_allows_guarded_click(self):
        self.assertTrue(self.run_guard(iter([self.observation(),self.observation()])))

    def test_changed_map_before_positioning_cancels_click(self):
        self.assertFalse(self.run_guard(iter([self.observation((791,399))])))

    def test_map_motion_during_positioning_cancels_fixed_teleport_button(self):
        self.assertFalse(self.run_guard(iter([self.observation(),self.observation((791,399))])))

    def test_missing_geometry_during_positioning_cancels_click(self):
        self.assertFalse(self.run_guard(iter([self.observation(),self.observation(None)])))

    def test_selection_loss_during_positioning_cancels_click(self):
        self.assertFalse(self.run_guard(iter([self.observation(),self.observation(selected=False)])))

    def test_opposite_small_movements_between_fresh_frames_cancel_teleport(self):
        # Each fresh point is within 4 physical pixels of the old anchor, but their
        # separation is greater than 4 pixels; a fixed teleport button is insufficient.
        self.assertFalse(self.run_guard(iter([self.observation((744.6,424)),
                                              self.observation((747.4,424))])))

    def test_changed_map_also_cancels_bonfire_click(self):
        self.assertFalse(self.run_guard(iter([self.observation(),self.observation((748,424))]),
                                        action=Action999.CLICK_BONFIRE))


if __name__ == "__main__": unittest.main()
