from pathlib import Path
import copy
import json
import queue
import tempfile
import unittest
import os
from unittest.mock import patch
import numpy as np
from yihuan.core import Action, Observation
from yihuan.runtime import Runner
from yihuan.settings import load_config
from yihuan.tasks import TaskSpec
from yihuan.nte999 import Scene999
from yihuan.vision import read_image
from yihuan.windows import ActionChanged, GameWindow, WinApi

ROOT = Path(__file__).resolve().parent.parent


class World:
    def __init__(self, victory=False, stop_at=None, fail=False, dropped_go=0):
        self.now, self.since = 0.0, 0.0
        self.state = "activity"
        self.victory, self.stop_at, self.fail = victory, stop_at, fail
        self.stopped, self.closed, self.released = False, False, False
        self.actions = []
        self.captures = 0
        self.input_error_details = None
        self.last_validation_frame = None
        self.dropped_go = dropped_go
        self.frames = {name: read_image(ROOT / f"tests/frames/{name}.jpg")
                       for name in ("activity", "match", "battle", "victory", "defeat", "dialog")}
        self.blank = np.zeros((843, 1499, 3), dtype=np.uint8)

    def frame(self):
        elapsed = self.now - self.since
        if self.state == "opening":
            return self.frames["match" if elapsed >= 2 else "activity"]
        if self.state == "loading":
            if elapsed < 32: return self.blank
            if self.victory and elapsed >= 34: return self.frames["victory"]
            return self.frames["battle"]
        if self.state == "return":
            return self.frames["activity"] if elapsed >= 12 else self.blank
        return self.frames[self.state]

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.now += seconds
        if self.stop_at is not None and self.now >= self.stop_at:
            self.stopped = True
        return self.stopped


class RuntimeTests(unittest.TestCase):
    def simulate(self, world):
        config = load_config(ROOT / "config.json")
        config["max_loops"] = 1
        events = queue.Queue()
        runner = Runner(events)
        runner.stop_event = world
        window = GameWindow(1, "异环", 0, 0, 3840, 2160)

        class Api:
            dpi_status = {"success": True, "context": "PerMonitorV2"}
            def find_game(self, _): return window
            def activate(self, win, stop): return window
            def process_id(self, hwnd): return os.getpid() + 1
            def diagnostics(self, window=None):
                if getattr(world, "diagnostic_error", False):
                    raise RuntimeError("diagnostic failure")
                return {"thread_dpi": self.dpi_status, "monitors": []}

        class Capture:
            def __init__(self, *args): pass
            def grab(self):
                world.captures += 1
                frame = world.frame().copy()
                # 每张图带不同标记，用来判断错误截图是否为真正的最后校验帧。
                frame[0, 0] = world.captures % 255
                world.last_validation_frame = frame
                return frame
            def close(self): world.closed = True

        class Actor:
            def __init__(self, *args): self.window = window
            def perform(self, intent, targets, before_press=None):
                if before_press is not None and not before_press():
                    raise ActionChanged("动作前画面已变化")
                world.actions.append((intent.action, world.now))
                if intent.action == Action.GO and world.dropped_go:
                    world.dropped_go -= 1
                    return
                if world.fail and intent.action == Action.ESC_BATTLE:
                    error = RuntimeError("injected input error")
                    error.details = world.input_error_details
                    raise error
                world.state = {
                    Action.GO: "opening", Action.MATCH: "loading",
                    Action.ESC_BATTLE: "dialog", Action.CONFIRM_BATTLE: "defeat",
                    Action.ESC_RESULT: "dialog", Action.CONFIRM_RESULT: "return",
                }[intent.action]
                world.since = world.now
            def release(self): world.released = True

        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            with patch("yihuan.runtime.load_config", return_value=config), \
                 patch("yihuan.runtime.WinApi", Api), patch("yihuan.runtime.Capture", Capture), \
                 patch("yihuan.runtime.Actor", Actor), patch("yihuan.runtime.time.monotonic", side_effect=lambda: world.now), \
                 patch("yihuan.runtime.DATA_DIR", Path(directory)):
                runner._run("kaci")
            startup_files = list((Path(directory) / "diagnostics").glob("startup_*.json"))
            self.assertEqual(len(startup_files), 1)
            world.startup_report = json.loads(startup_files[0].read_text(encoding="utf-8"))
            diagnostics = list((Path(directory) / "diagnostics").glob("[0-9]*.json"))
            reports = [json.loads(p.read_text(encoding="utf-8")) for p in diagnostics]
            for report in reports:
                if report.get("before_input_image"):
                    saved = read_image(Path(directory) / "diagnostics" / report["before_input_image"])
                    report["saved_validation_pixel"] = saved[0, 0].tolist()
        messages = []
        while not events.empty(): messages.append(events.get_nowait())
        self.assertTrue(world.closed)
        self.assertTrue(world.released)
        return messages, reports

    def test_reference_frames_through_full_victory_runner(self):
        world = World(victory=True)
        messages, reports = self.simulate(world)
        self.assertEqual([a for a, _ in world.actions],
                         [Action.GO, Action.MATCH, Action.ESC_RESULT, Action.CONFIRM_RESULT])
        self.assertEqual(messages[-1], ("done", "已完成1轮"))
        self.assertEqual(reports, [])

    def test_reference_frames_through_full_active_exit_runner(self):
        world = World()
        messages, reports = self.simulate(world)
        self.assertEqual([a for a, _ in world.actions], [Action.GO, Action.MATCH, Action.ESC_BATTLE,
                         Action.CONFIRM_BATTLE, Action.ESC_RESULT, Action.CONFIRM_RESULT])
        self.assertGreaterEqual(world.actions[4][1] - world.actions[3][1], 1.0)
        self.assertEqual(messages[-1], ("done", "已完成1轮"))
        self.assertEqual(reports, [])

    def test_stop_during_32_second_load_has_no_escape_and_cleans_up(self):
        world = World(stop_at=15)
        messages, reports = self.simulate(world)
        self.assertEqual([a for a, _ in world.actions], [Action.GO, Action.MATCH])
        self.assertEqual(messages[-1], ("done", "已停止"))
        self.assertEqual(reports, [])

    def test_runtime_error_releases_and_saves_diagnostic(self):
        world = World(fail=True)
        messages, reports = self.simulate(world)
        self.assertTrue(messages[-1][1].startswith("已暂停："))
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["scene"], "战场")

    def test_error_saves_actual_final_validation_image_and_native_details(self):
        world = World(fail=True)
        world.input_error_details = {"operation": "SendInput", "error_code": 5, "error_text": "Access denied"}
        _, reports = self.simulate(world)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["windows_error"], world.input_error_details)
        self.assertEqual(reports[0]["saved_validation_pixel"], world.last_validation_frame[0, 0].tolist())

    def test_startup_diagnostic_failure_still_saves_original_error(self):
        world = World(fail=True)
        world.input_error_details = {"api": "SendInput", "last_error": 5}
        world.diagnostic_error = True
        self.simulate(world)
        self.assertIn("diagnostic failure", world.startup_report["diagnostic_error"])
        self.assertIn("injected input error", world.startup_report["error"])
        self.assertEqual(world.startup_report["windows_error"], world.input_error_details)

    def simulate_nte999(self, fail=False):
        config = load_config(ROOT / "config.json")
        config["max_loops"] = 1
        events = queue.Queue()
        runner = Runner(events)
        world = World()
        runner.stop_event = world
        window = GameWindow(1, "异环", 0, 0, 3840, 2160)

        class Api:
            dpi_status = {"success": True, "context": "PerMonitorV2"}
            def find_game(self, _): return window
            def activate(self, win, stop): return window
            def process_id(self, hwnd): return os.getpid() + 1
            def diagnostics(self, window=None):
                return {"thread_dpi": self.dpi_status, "monitors": []}

        class Capture:
            def __init__(self, *args): pass
            def close(self): world.closed = True

        class Actor:
            def __init__(self, *args): self.last_input = {"stage": "fake"}
            def perform(self, *args, **kwargs): pass
            def check(self): pass
            def release(self): world.released = True

        class Detector:
            class Locator: pass
            locator = Locator()

        class Loop:
            def __init__(self, flow, actor, capture, locator, stop, log,
                         status=None, debug_dir=None):
                self.flow, self.actor, self.capture = flow, actor, capture
                self.locator, self.status = locator, status
                self.completed = 1
                self.phase_text = "地图定位"
                self.last_frame = np.zeros((843, 1499, 3), dtype=np.uint8)
                self.last_observation = Observation(
                    Scene999.MAP, {"map_label": .91}, {"bonfire": (700.0, 400.0)},
                    {"target_source": "map_label", "map_failure_reason": None,
                     "text_found": True})
                self.recent_actions = [{"action": "点击目标篝火", "phase": "地图定位",
                                        "input": {"sent_at": 1.0}}]
            def run(self):
                if self.status:
                    self.status("第1轮 · 地图定位")
                if fail:
                    raise RuntimeError("injected 999 failure")

        spec = TaskSpec("999夜 · 刷纽扣", None, lambda resources: Detector())
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            with patch("yihuan.runtime.load_config", return_value=config), \
                 patch("yihuan.runtime.WinApi", Api), \
                 patch("yihuan.runtime.Capture", Capture), \
                 patch("yihuan.runtime.Actor", Actor), \
                 patch("yihuan.runtime.TASKS", {"nte999": spec}), \
                 patch("yihuan.runtime.Nte999Loop", Loop), \
                 patch("yihuan.runtime.DATA_DIR", Path(directory)):
                runner._run("nte999")
            reports = [json.loads(p.read_text(encoding="utf-8"))
                       for p in (Path(directory) / "diagnostics").glob("[0-9]*.json")]
        messages = []
        while not events.empty():
            messages.append(events.get_nowait())
        self.assertTrue(world.closed)
        self.assertTrue(world.released)
        return messages, reports

    def test_nte999_runtime_uses_fixed_loop_and_reports_done(self):
        messages, reports = self.simulate_nte999()
        self.assertEqual(messages[-1], ("done", "已完成1轮"))
        self.assertIn(("status", "999夜 · 刷纽扣 · 第1轮 · 地图定位"), messages)
        self.assertEqual(reports, [])

    def test_nte999_runtime_error_saves_map_evidence(self):
        messages, reports = self.simulate_nte999(fail=True)
        self.assertTrue(messages[-1][1].startswith("已暂停："))
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["phase"], "地图定位")
        self.assertEqual(reports[0]["scene"], "地图")
        self.assertEqual(reports[0]["flags"]["target_source"], "map_label")
        self.assertEqual(len(reports[0]["recent_actions"]), 1)

    def test_window_restore_refreshes_geometry(self):
        api = object.__new__(WinApi)
        old = GameWindow(1, "异环", -32000, -32000, 3840, 2160)
        restored = GameWindow(1, "异环", 0, 0, 3840, 2160)
        class User:
            def IsIconic(self, hwnd): return True
            def ShowWindow(self, hwnd, flag): pass
            def SetForegroundWindow(self, hwnd): return True
            def GetForegroundWindow(self): return 1
        api.user = User()
        api.geometry = lambda hwnd: restored
        api.process_id = lambda hwnd: os.getpid() + 1
        self.assertEqual(api.activate(old, World()), restored)

    def test_unreleased_inputs_block_restart(self):
        runner = Runner(queue.Queue())
        class Unreleased:
            def release(self): raise RuntimeError("release failed")
        runner.unreleased_actor = Unreleased()
        with self.assertLogs("yihuan", level="ERROR"):
            self.assertFalse(runner.start("kaci"))
        self.assertIsNone(runner.worker)
        self.assertEqual(runner.events.get_nowait()[0], "done")

    def test_ignored_first_go_is_retried_and_then_completes(self):
        world = World(victory=True, dropped_go=1)
        messages, reports = self.simulate(world)
        self.assertEqual([a for a, _ in world.actions],
                         [Action.GO, Action.GO, Action.MATCH, Action.ESC_RESULT, Action.CONFIRM_RESULT])
        self.assertGreaterEqual(world.actions[1][1] - world.actions[0][1], 3)
        self.assertEqual(messages[-1], ("done", "已完成1轮"))
        self.assertEqual(reports, [])

    def test_ignored_go_pauses_with_trace_and_no_matching_click(self):
        world = World(dropped_go=100)
        messages, reports = self.simulate(world)
        self.assertEqual([a for a, _ in world.actions], [Action.GO, Action.GO])
        self.assertIn("前往点击后仍停在活动页", messages[-1][1])
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["scene"], "活动页")
        self.assertEqual(len(reports[0]["recent_actions"]), 2)
        self.assertIn("go", reports[0]["targets"])
        self.assertTrue(reports[0]["before_input_image"].endswith("_before_input.png"))


if __name__ == "__main__":
    unittest.main()
