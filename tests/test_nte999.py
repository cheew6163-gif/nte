import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from yihuan.core import TaskTimeout, Timings
from yihuan.map_label import MapLabelLocation
from yihuan.nte999 import (Action999, Detector999, Flow999, Nte999Loop, Scene999,
                           SHIFT_TOTAL, load_flow_999)
from yihuan.windows import Cancelled

ROOT = Path(__file__).resolve().parent.parent


class FakeClock:
    def __init__(self, start=100.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += max(0.0, float(seconds))


class FakeStop:
    def __init__(self, clock, stop_at=None):
        self.clock = clock
        self.stop_at = stop_at
        self.stopped = False

    def wait(self, seconds):
        self.clock.advance(seconds)
        if self.stop_at is not None and self.clock.now >= self.stop_at:
            self.stopped = True
        return self.stopped

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True


class FakeActor:
    def __init__(self, clock, stop):
        self.clock = clock
        self.stop = stop
        self.calls = []
        self.held = set()
        self.last_input = None
        self.check_calls = 0
        self.on_perform = None

    def check(self):
        self.check_calls += 1
        if self.stop.is_set():
            raise Cancelled()

    def perform(self, intent, targets, before_press=None):
        if before_press is not None and not before_press():
            raise RuntimeError("unexpected before_press")
        sent = self.clock.now
        point = targets.get(intent.target) if intent.target else None
        self.calls.append({"action": intent.action, "time": sent, "kind": intent.kind,
                           "key": intent.key, "key2": intent.key2,
                           "target": intent.target, "point": point})
        if self.on_perform is not None:
            self.on_perform(intent, sent)
        duration = intent.seconds if intent.kind == "tap" else (.08 if intent.kind == "click" else 0)
        self.clock.advance(duration)
        if intent.kind == "press":
            self.held.add(intent.key)
        elif intent.kind == "release":
            self.held.discard(intent.key)
        elif intent.kind == "swap":
            self.held.discard(intent.key)
            self.held.add(intent.key2)
        self.last_input = {"action": intent.action.value, "kind": intent.kind,
                           "sent_at": sent, "held_keys": sorted(self.held)}
        return self.last_input

    def release(self):
        self.held.clear()
        self.last_input = {"stage": "released", "held_keys": []}


class FakeCapture:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []

    def grab(self):
        self.calls.append(self.clock.now)
        return np.zeros((2160, 3840, 3), dtype=np.uint8)


class FakeLocator:
    def __init__(self, points=(), text_misses=0, text_found_no_target=False):
        self.points = list(points)
        self.text_misses = text_misses
        self.text_found_no_target = text_found_no_target
        self.calls = []

    def locate(self, frame):
        self.calls.append(1)
        if self.text_misses > 0:
            self.text_misses -= 1
            return MapLabelLocation(None, text_found=False,
                                    reason="未找到“残丝长巷”文字")
        if self.text_found_no_target or not self.points:
            return MapLabelLocation(None, text_found=True, text_score=.95,
                                    reason="已找到“残丝长巷”，但其左上方未找到指定篝火图标")
        point = self.points.pop(0)
        return MapLabelLocation(point, text_point=(point[0] + 99, point[1] + 56),
                                predicted=point, text_found=True,
                                text_score=.95, text_second=.3, target_area=90)


def make_flow(max_loops=1, **overrides):
    values = dict(
        w_hold=3.0, a_hold=1.8, s_hold=2.0, battle_hold=80.0,
        shift_interval=2.0, shift_hold=.08, refresh_gap=.8,
        menu_wait=.8, exit_wait=.5, map_wait=1.0, map_retry=5.0,
        map_poll=.25, panel_wait=.5, teleport_wait=5.0,
        f_repeat_count=3, f_repeat_interval=.5,
        refresh_point=(86.5, 184.0), teleport_point=(1295.0, 755.0),
        max_loops=max_loops)
    values.update(overrides)
    return Flow999(**values)


class Nte999FixedFlowTests(unittest.TestCase):
    def make(self, points=((740.0, 428.0),), max_loops=1, stop_at=None,
             text_misses=0, text_found_no_target=False, **overrides):
        clock = FakeClock()
        stop = FakeStop(clock, stop_at)
        actor = FakeActor(clock, stop)
        capture = FakeCapture(clock)
        locator = FakeLocator(points, text_misses, text_found_no_target)
        loop = Nte999Loop(make_flow(max_loops, **overrides), actor, capture,
                          locator, stop, clock=clock)
        return loop, actor, capture, locator, stop, clock

    @staticmethod
    def actions(actor):
        return [row["action"] for row in actor.calls]

    def test_first_round_is_fixed_and_only_map_phase_captures(self):
        loop, actor, capture, locator, stop, clock = self.make()
        loop.run()
        actions = self.actions(actor)

        self.assertEqual(actions[0], Action999.PRESS_F)
        self.assertEqual(actions[1], Action999.CLICK_REFRESH)
        self.assertEqual(actions[2], Action999.PRESS_ESC)
        self.assertEqual(actions[3:7], [Action999.PRESS_W, Action999.SWAP_WA,
                                        Action999.SWAP_AS, Action999.RELEASE_S])
        self.assertEqual(actions.count(Action999.TAP_SHIFT), SHIFT_TOTAL)
        self.assertEqual(actions.count(Action999.PRESS_F), 1)
        self.assertEqual(loop.completed, 1)
        self.assertEqual(loop.phase_text, "完成指定轮数（1轮）")

        refresh = next(row for row in actor.calls if row["action"] == Action999.CLICK_REFRESH)
        teleport = next(row for row in actor.calls if row["action"] == Action999.CLICK_TELEPORT)
        bonfire = next(row for row in actor.calls if row["action"] == Action999.CLICK_BONFIRE)
        self.assertEqual(refresh["point"], (86.5, 184.0))
        self.assertEqual(teleport["point"], (1295.0, 755.0))
        self.assertEqual(bonfire["point"], (740.0, 428.0))

        w = next(row for row in actor.calls if row["action"] == Action999.PRESS_W)
        a = next(row for row in actor.calls if row["action"] == Action999.SWAP_WA)
        s = next(row for row in actor.calls if row["action"] == Action999.SWAP_AS)
        release = next(row for row in actor.calls if row["action"] == Action999.RELEASE_S)
        self.assertAlmostEqual(a["time"] - w["time"], 3.0, places=2)
        self.assertAlmostEqual(s["time"] - a["time"], 1.8, places=2)
        self.assertAlmostEqual(release["time"] - s["time"], 2.0, places=2)
        self.assertEqual(actor.held, set())

        shifts = [row for row in actor.calls if row["action"] == Action999.TAP_SHIFT]
        self.assertAlmostEqual(shifts[0]["time"], release["time"], places=2)
        for previous, current in zip(shifts, shifts[1:]):
            self.assertAlmostEqual(current["time"] - previous["time"], 2.0, places=2)
        self.assertAlmostEqual(shifts[-1]["time"] - release["time"],
                               (SHIFT_TOTAL - 1) * 2.0, places=2)

        m = next(row for row in actor.calls if row["action"] == Action999.PRESS_M)
        self.assertGreater(m["time"], shifts[-1]["time"])
        between = [row for row in actor.calls if release["time"] < row["time"] < m["time"]]
        self.assertTrue(between)
        self.assertTrue(all(row["action"] == Action999.TAP_SHIFT for row in between))

        self.assertTrue(capture.calls)
        self.assertEqual(len(locator.calls), 1)
        self.assertTrue(all(m["time"] <= stamp <= bonfire["time"] for stamp in capture.calls))

    def test_second_round_waits_five_seconds_before_repeated_f(self):
        loop, actor, capture, locator, stop, clock = self.make(
            points=((700.0, 400.0), (730.0, 360.0)), max_loops=2)
        loop.run()
        self.assertEqual(loop.completed, 2)
        teleports = [row for row in actor.calls if row["action"] == Action999.CLICK_TELEPORT]
        self.assertEqual(len(teleports), 2)
        first = teleports[0]
        after = [row for row in actor.calls if row["time"] > first["time"]]
        f_rows = [row for row in after if row["action"] == Action999.PRESS_F]
        self.assertEqual(len(f_rows), 3)
        self.assertGreaterEqual(f_rows[0]["time"] - first["time"], 5.0)
        self.assertFalse(any(first["time"] < row["time"] < first["time"] + 5.0
                             and row["action"] == Action999.PRESS_F for row in actor.calls))
        self.assertGreaterEqual(f_rows[1]["time"] - f_rows[0]["time"], .5)
        self.assertLess(f_rows[1]["time"] - f_rows[0]["time"], .7)
        second = teleports[1]
        self.assertEqual([row for row in actor.calls
                          if row["time"] > second["time"] and row["action"] == Action999.PRESS_F], [])
        self.assertEqual(len(capture.calls), 2)
        self.assertEqual(len(locator.calls), 2)

    def test_map_text_retry_then_pause_without_clicking(self):
        loop, actor, capture, locator, stop, clock = self.make(
            points=(), text_misses=100, map_retry=1.0, map_poll=.25)
        with self.assertRaisesRegex(TaskTimeout, "残丝"):
            loop.run()
        self.assertGreaterEqual(len(capture.calls), 2)
        actions = self.actions(actor)
        self.assertNotIn(Action999.CLICK_BONFIRE, actions)
        self.assertNotIn(Action999.CLICK_TELEPORT, actions)
        self.assertEqual(actions[-1], Action999.PRESS_M)

    def test_text_found_without_bonfire_pauses_immediately(self):
        loop, actor, capture, locator, stop, clock = self.make(text_found_no_target=True)
        with self.assertRaisesRegex(TaskTimeout, "篝火"):
            loop.run()
        self.assertEqual(len(capture.calls), 1)
        self.assertNotIn(Action999.CLICK_BONFIRE, self.actions(actor))
        self.assertNotIn(Action999.CLICK_TELEPORT, self.actions(actor))

    def test_stop_during_five_second_wait_does_not_count_or_press_f(self):
        loop, actor, capture, locator, stop, clock = self.make()
        actor.on_perform = lambda intent, sent: (
            setattr(stop, "stop_at", sent + 2.0)
            if intent.action == Action999.CLICK_TELEPORT else None)
        with self.assertRaises(Cancelled):
            loop.run()
        self.assertEqual(loop.completed, 0)
        self.assertEqual(self.actions(actor).count(Action999.PRESS_F), 1)
        actor.release()
        self.assertEqual(actor.held, set())

    def test_stop_during_battle_does_not_open_map(self):
        loop, actor, capture, locator, stop, clock = self.make()
        actor.on_perform = lambda intent, sent: (
            setattr(stop, "stop_at", sent + 10.0)
            if intent.action == Action999.RELEASE_S else None)
        with self.assertRaises(Cancelled):
            loop.run()
        actions = self.actions(actor)
        self.assertNotIn(Action999.PRESS_M, actions)
        self.assertNotIn(Action999.CLICK_BONFIRE, actions)
        self.assertEqual(capture.calls, [])
        self.assertEqual(loop.completed, 0)
        actor.release()
        self.assertEqual(actor.held, set())

    def test_flow_loader_uses_private_points_and_keeps_config_timings(self):
        flow = load_flow_999(ROOT / "assets999", Timings(), max_loops=3)
        self.assertEqual(flow.refresh_point, (86.5, 184.0))
        self.assertEqual(flow.teleport_point, (1295.0, 755.0))
        self.assertEqual(flow.teleport_wait, 5.0)
        self.assertEqual(flow.f_repeat_count, 3)
        self.assertEqual(flow.w_hold, 3.0)
        self.assertEqual(flow.battle_hold, 80.0)
        self.assertEqual(flow.max_loops, 3)

    def test_invalid_fixed_point_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            data = json.loads((ROOT / "assets999/templates.json").read_text(encoding="utf-8"))
            data["fixed"]["refresh_point"] = [2000, 0]
            (Path(directory) / "templates.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_flow_999(Path(directory), Timings(), 1)


class DetectorAssetTests(unittest.TestCase):
    def test_assets_load_and_blank_is_unknown(self):
        detector = Detector999(ROOT / "assets999")
        frame = np.zeros((2160, 3840, 3), dtype=np.uint8)
        result = detector.detect(frame)
        self.assertEqual(result.scene, Scene999.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
