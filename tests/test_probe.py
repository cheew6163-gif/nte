"""单步入口的隔离测试；所有API、截图和输入均为替身。"""
import itertools
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from yihuan.core import Observation, Scene
from yihuan.probe import (ProbeOptions, ProbeReport, _require_input_desktop,
                          _screen_target, desktop_action, probe_game, run_input_check)
from yihuan.windows import Cancelled, GameWindow
from yihuan.nte999 import Scene999


ACTIVITY = Observation(Scene.ACTIVITY, {"go": .99}, {"go": (1319, 791.5), "match": (1312, 758)})
MATCH = Observation(Scene.MATCH, {"match": .99}, {"go": (1319, 791.5), "match": (1312, 758)})
VICTORY = Observation(Scene.VICTORY, {}, {"go": (1319, 791.5), "match": (1312, 758)})


class FakeReport:
    def __init__(self):
        self.data = {"errors": [], "screenshots": {}, "click_response_confirmed": False}
    def image(self, name, frame):
        if frame is not None:
            self.data["screenshots"][name] = name + ".png"


class FakeApi:
    window = GameWindow(12, "异环", 0, 0, 3840, 2160)
    def __init__(self, interactive=True):
        self.interactive, self.activations = interactive, 0
    def key_down(self, key): return False
    def input_diagnostics(self, window=None, target=None):
        return {"desktop": {"same_input_desktop": self.interactive,
                            "thread_desktop": {"receives_input": self.interactive}}}
    def find_game(self, config): return self.window
    def activate(self, window, stop):
        self.activations += 1
        return window


class FakeDetector:
    size = (1499, 843)
    def detect(self, frame): return frame


class FakeCapture:
    def __init__(self, frames):
        self.frames, self.index, self.closed = frames, 0, False
    def grab(self):
        result = self.frames[min(self.index, len(self.frames) - 1)]
        self.index += 1
        return result
    def close(self): self.closed = True


class FakeActor:
    instances = []
    def __init__(self, api, window, stop):
        self.moves, self.clicks, self.released, self.held = [], [], False, set()
        self.last_input = None
        self.instances.append(self)
    def position(self, x, y, **kwargs):
        self.moves.append((x, y))
        self.last_input = {"screen_target": [x, y], "stage": "position verified"}
        return x, y
    def click_at(self, x, y, before_press=None, **kwargs):
        self.position(x, y)
        if before_press is not None and not before_press():
            raise RuntimeError("last frame callback rejected")
        self.clicks.append((x, y))
        self.last_input["mouse_down_sent"] = True
        self.last_input["stage"] = "released"
    def release(self): self.released = True


class ProbeTests(unittest.TestCase):
    config = {"screen_width": 3840, "screen_height": 2160, "stable_frames": 2}

    def setUp(self):
        FakeActor.instances = []

    def game(self, frames, click=False, target="go", api=None, stop=None):
        capture, report = FakeCapture(frames), FakeReport()
        options = ProbeOptions("game", click=click, target=target, timeout=2, start_delay=0)
        counter = itertools.count(step=.1)
        with patch("yihuan.probe.time.monotonic", side_effect=lambda: next(counter)):
            probe_game(api or FakeApi(), options, report, stop or threading.Event(),
                       config=self.config, detector=FakeDetector(),
                       capture_factory=lambda *args: capture, actor_factory=FakeActor)
        return report, capture

    def test_bonfire_step_clicks_once_and_verifies_the_same_selected_target(self):
        before=Observation(Scene999.MAP,{}, {"bonfire":(746,424)}, {"target_selected":False})
        after=Observation(Scene999.MAP,{}, {"bonfire":(746,424),"teleport":(1295,755)},
                          {"target_selected":True})
        report, _ = self.game([before,before,before,after,after],click=True,target="bonfire")
        actor=FakeActor.instances[-1]
        self.assertEqual(len(actor.clicks),1)
        self.assertEqual(report.data["status"],"bonfire_selection_verified")
        self.assertTrue(report.data["click_response_confirmed"])

    def test_bonfire_wrong_selected_circle_does_not_pass(self):
        before=Observation(Scene999.MAP,{}, {"bonfire":(746,424)}, {"target_selected":False})
        after=Observation(Scene999.MAP,{}, {"bonfire":(746,424)}, {"target_selected":False})
        report, _ = self.game([before,before,before,after],click=True,target="bonfire")
        actor=FakeActor.instances[-1]
        self.assertEqual(len(actor.clicks),1)
        self.assertFalse(report.data["click_response_confirmed"])
        self.assertEqual(report.data["status"],"click_sent_response_unconfirmed")

    def test_already_selected_bonfire_does_not_claim_a_new_click_response(self):
        selected=Observation(Scene999.MAP,{}, {"bonfire":(746,424),"teleport":(1295,755)},
                             {"target_selected":True})
        report, _ = self.game([selected],click=True,target="bonfire")
        self.assertEqual(report.data["status"],"bonfire_selection_verified")
        self.assertTrue(report.data["initial_target_selected"])
        self.assertFalse(report.data["selection_state_changed"])
        self.assertFalse(report.data["click_response_confirmed"])

    def test_bonfire_map_motion_before_press_cancels_step(self):
        before=Observation(Scene999.MAP,{}, {"bonfire":(746,424)})
        moved=Observation(Scene999.MAP,{}, {"bonfire":(791,399)})
        with self.assertRaisesRegex(RuntimeError,"位置已变化"):
            self.game([before,before,moved],click=True,target="bonfire")
        self.assertFalse(FakeActor.instances[-1].clicks)

    def test_default_game_only_positions(self):
        report, capture = self.game([ACTIVITY])
        actor = FakeActor.instances[0]
        self.assertEqual(actor.moves, [(3379, 2028)])
        self.assertEqual(actor.clicks, [])
        self.assertTrue(actor.released)
        self.assertTrue(capture.closed)
        self.assertEqual(report.data["status"], "game_move_verified")

    def test_one_go_click_observes_match_without_clicking_match(self):
        report, capture = self.game([ACTIVITY, ACTIVITY, ACTIVITY, MATCH, MATCH], click=True)
        self.assertEqual(FakeActor.instances[0].clicks, [(3379, 2028)])
        self.assertEqual(report.data["status"], "go_response_match_verified")
        self.assertTrue(report.data["click_response_confirmed"])
        self.assertIn("before_press", report.data["screenshots"])
        self.assertIn("after", report.data["screenshots"])

    def test_match_target_requires_match_scene(self):
        with self.assertRaisesRegex(RuntimeError, "未稳定识别匹配页"):
            self.game([ACTIVITY], click=True, target="match")
        self.assertFalse(FakeActor.instances[0].clicks)
        self.assertTrue(FakeActor.instances[0].released)

    def test_latest_scene_change_prevents_press(self):
        with self.assertRaisesRegex(RuntimeError, "按下前页面变为"):
            self.game([ACTIVITY, ACTIVITY, VICTORY], click=True)
        self.assertFalse(FakeActor.instances[0].clicks)
        self.assertTrue(FakeActor.instances[0].released)

    def test_no_response_never_retries(self):
        report, _ = self.game([ACTIVITY], click=True)
        self.assertEqual(len(FakeActor.instances[0].clicks), 1)
        self.assertEqual(report.data["status"], "click_sent_response_unconfirmed")
        self.assertFalse(report.data["click_response_confirmed"])

    def test_wrong_desktop_rejects_before_activation(self):
        api = FakeApi(interactive=False)
        with self.assertRaisesRegex(RuntimeError, "用户输入桌面"):
            self.game([ACTIVITY], click=True, api=api)
        self.assertEqual(api.activations, 0)
        self.assertFalse(FakeActor.instances)

    def test_stopped_before_recognition_has_no_input(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(Cancelled): self.game([ACTIVITY], click=True, stop=stop)
        self.assertFalse(FakeActor.instances)

    def test_desktop_click_needs_native_receiver_event(self):
        report, received = FakeReport(), threading.Event()
        received.set()
        with patch("yihuan.probe._desktop_frame", return_value=None), patch("yihuan.probe._wait"):
            desktop_action(FakeApi(), FakeApi.window, (500, 400), ProbeOptions("desktop", click=True),
                           report, threading.Event(), received, actor_factory=FakeActor)
        self.assertEqual(report.data["status"], "desktop_click_received")
        self.assertTrue(report.data["click_response_confirmed"])
        self.assertEqual(FakeActor.instances[0].clicks, [(500, 400)])

    def test_desktop_move_does_not_claim_received_click(self):
        report = FakeReport()
        with patch("yihuan.probe._desktop_frame", return_value=None), patch("yihuan.probe._wait"):
            desktop_action(FakeApi(), FakeApi.window, (500, 400), ProbeOptions("desktop"),
                           report, threading.Event(), threading.Event(), actor_factory=FakeActor)
        self.assertEqual(report.data["status"], "desktop_move_verified")
        self.assertFalse(report.data["click_response_confirmed"])
        self.assertFalse(FakeActor.instances[0].clicks)

    def test_desktop_sent_click_without_receiver_is_failure(self):
        report = FakeReport()
        counter = itertools.count(step=.1)
        with patch("yihuan.probe._desktop_frame", return_value=None), \
                patch("yihuan.probe.time.monotonic", side_effect=lambda: next(counter)):
            with self.assertRaisesRegex(RuntimeError, "靶窗未收到点击事件"):
                desktop_action(FakeApi(), FakeApi.window, (500, 400),
                               ProbeOptions("desktop", click=True, timeout=1), report,
                               threading.Event(), threading.Event(), actor_factory=FakeActor)
        self.assertFalse(report.data["click_response_confirmed"])
        self.assertTrue(FakeActor.instances[0].released)

    def test_unknown_desktop_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "用户输入桌面"):
            _require_input_desktop({})

    def test_reference_rounding_cannot_leave_client_area(self):
        with self.assertRaisesRegex(RuntimeError, "超出游戏客户区"):
            _screen_target(FakeApi.window, (1498.99, 842.99), (1499, 843))

    def test_mutex_failure_is_saved_in_report_without_any_input(self):
        with tempfile.TemporaryDirectory() as temp:
            options=ProbeOptions("game",click=True,output_dir=Path(temp))
            with patch("yihuan.probe.WinApi",return_value=FakeApi()), \
                 patch("yihuan.probe.single_instance",side_effect=RuntimeError("assistant already running")), \
                 patch("builtins.print"):
                code=run_input_check(options)
            saved=next(Path(temp).glob("input_check_*/report.json")).read_text(encoding="utf-8")
        self.assertEqual(code,1)
        self.assertIn("assistant already running",saved)
        self.assertFalse(FakeActor.instances)

    def test_report_preserves_failed_status_and_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            report = ProbeReport(ProbeOptions("inspect", output_dir=Path(temp)))
            report.data["status"] = "failed"
            report.data["environment"] = {"desktop": "isolated", "error": {"code": 5}}
            saved = report.finish()
            self.assertIn('"code": 5', saved.read_text(encoding="utf-8"))
            self.assertIn('"status": "failed"', saved.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
