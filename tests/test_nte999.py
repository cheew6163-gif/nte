import unittest
from pathlib import Path

import numpy as np

from yihuan.core import Observation, TaskTimeout, Timings
from yihuan.nte999 import Action999, Detector999, Nte999Task, Phase, Scene999

ROOT = Path(__file__).resolve().parent.parent


def obs(scene, targets=None, flags=None):
    return Observation(scene, {}, targets or {}, flags or {})


class Nte999FlowTests(unittest.TestCase):
    def setUp(self):
        self.task = Nte999Task(Timings(), max_loops=1, now=0.0)
        self.intents = []

    def push(self, scene, now, targets=None, flags=None):
        intent = self.task.observe(obs(scene, targets, flags), now)
        if intent is not None:
            self.intents.append((now, intent.action))
            self.task.commit(intent, now)
        return intent

    def test_full_round_with_one_loop(self):
        task = self.task
        bonfire = {"bonfire": (700.0, 400.0)}
        panel = {"bonfire": (700.0, 400.0), "teleport": (1295.0, 755.0)}

        self.assertEqual(self.push(Scene999.FIELD, 0.0).action, Action999.OPEN_MAP)
        self.assertEqual(task.phase, Phase.MAP)
        self.assertIsNone(self.push(Scene999.FIELD, 0.5))       # 地图动画尚未结束
        self.assertEqual(self.push(Scene999.MAP, 1.0, bonfire, {"panel": False}).action,
                         Action999.CLICK_BONFIRE)
        self.assertEqual(self.push(Scene999.MAP, 2.0, panel, {"panel": True}).action,
                         Action999.CLICK_TELEPORT)
        self.assertEqual(task.phase, Phase.TELEPORT)

        # 传送中黑场不按 F
        self.assertIsNone(self.push(Scene999.UNKNOWN, 3.0))
        self.assertIsNone(self.push(Scene999.FIELD, 5.0, flags={"f_prompt": False}))
        self.assertEqual(self.push(Scene999.FIELD, 6.0, flags={"f_prompt": True}).action,
                         Action999.PRESS_F)
        self.assertEqual(task.phase, Phase.MENU)

        self.assertEqual(self.push(Scene999.CAMP, 7.0, {"refresh": (120.0, 240.0)}).action,
                         Action999.CLICK_REFRESH)
        self.assertEqual(task.phase, Phase.EXIT)
        self.assertIsNone(self.push(Scene999.CAMP, 7.5))        # 0.8 秒内不按 Esc
        self.assertEqual(self.push(Scene999.CAMP, 7.9).action, Action999.PRESS_ESC)
        self.assertEqual(task.phase, Phase.EXIT_FIELD)
        self.assertEqual(self.push(Scene999.FIELD, 8.5).action, Action999.PRESS_W)
        self.assertEqual(task.phase, Phase.W)

        self.assertIsNone(self.push(Scene999.FIELD, 11.0))
        self.assertEqual(self.push(Scene999.FIELD, 11.6).action, Action999.SWAP_WA)
        self.assertEqual(task.phase, Phase.A)
        self.assertIsNone(self.push(Scene999.FIELD, 13.0))
        self.assertEqual(self.push(Scene999.FIELD, 13.5).action, Action999.SWAP_AS)
        self.assertEqual(task.phase, Phase.S)
        self.assertIsNone(self.push(Scene999.FIELD, 15.2))
        self.assertEqual(self.push(Scene999.FIELD, 15.6).action, Action999.RELEASE_S)
        self.assertEqual(task.phase, Phase.BATTLE)
        self.assertEqual(task.shift_count, 0)
        self.assertEqual(self.push(Scene999.FIELD, 15.6).action, Action999.TAP_SHIFT)
        self.assertEqual(task.shift_count, 1)

        now = 15.6
        while self.task.phase == Phase.BATTLE and now < 120:
            now += 2.0
            intent = self.push(Scene999.FIELD, now)
        self.assertEqual(task.shift_count, 40)
        self.assertEqual(task.phase, Phase.MAP_AFTER)
        expected_release = task.battle_start + task.timings.battle_hold
        self.assertGreaterEqual(now, expected_release)
        self.assertLess(now - expected_release, 0.25)

        self.assertIsNone(self.push(Scene999.FIELD, now + 0.5))
        self.assertEqual(self.push(Scene999.FIELD, now + 1.0).action, Action999.PRESS_M)
        self.assertEqual(task.phase, Phase.MAP)
        self.assertTrue(task.round_pending)

        self.assertEqual(self.push(Scene999.MAP, now + 2.0, panel, {"panel": True}).action,
                         Action999.CLICK_TELEPORT)
        self.assertEqual(self.push(Scene999.FIELD, now + 8.0, flags={"f_prompt": True}), None)
        self.assertTrue(task.done)
        self.assertEqual(task.completed, 1)

    def test_no_f_before_prompt(self):
        self.push(Scene999.FIELD, 0.0)                      # 开地图
        self.push(Scene999.MAP, 1.0, {"bonfire": (700, 400)}, {"panel": False})
        self.push(Scene999.MAP, 2.0, {"bonfire": (700, 400), "teleport": (1295, 755)},
                  {"panel": True})
        for t in (3.0, 5.0, 10.0):
            self.assertIsNone(self.push(Scene999.FIELD, t, flags={"f_prompt": False}))
        self.assertNotIn(Action999.PRESS_F, [a for _, a in self.intents])

    def test_map_target_moves_each_round(self):
        self.push(Scene999.FIELD, 0.0)
        first = self.push(Scene999.MAP, 1.0, {"bonfire": (700, 400)}, {"panel": False})
        self.assertEqual(first.action, Action999.CLICK_BONFIRE)
        self.assertEqual(first.target, "bonfire")

        # 新的一轮重新定位到另一个坐标，仍使用新目标而不是旧坐标
        self.push(Scene999.MAP, 2.0, {"bonfire": (710, 405), "teleport": (1295, 755)},
                  {"panel": True})
        self.push(Scene999.FIELD, 3.0, flags={"f_prompt": True})
        self.push(Scene999.CAMP, 4.0, {"refresh": (120, 240)})
        self.push(Scene999.CAMP, 4.9)
        self.push(Scene999.FIELD, 5.5)
        for t in (8.6, 10.4, 12.5):
            self.push(Scene999.FIELD, t)
        self.push(Scene999.FIELD, 13.0)
        now = 13.0
        while self.task.phase == Phase.BATTLE and now < 100:
            now += 2.0
            self.push(Scene999.FIELD, now)
        self.push(Scene999.FIELD, now + 1.0)
        moved = self.push(Scene999.MAP, now + 2.0, {"bonfire": (900, 500)}, {"panel": False})
        self.assertEqual(moved.action, Action999.CLICK_BONFIRE)
        self.assertEqual(self.task.click_pos, (900, 500))

    def test_stays_in_menu_when_refresh_missing(self):
        self.push(Scene999.FIELD, 0.0)
        self.push(Scene999.MAP, 1.0, {"bonfire": (700, 400)}, {"panel": False})
        self.push(Scene999.MAP, 2.0, {"bonfire": (700, 400), "teleport": (1295, 755)},
                  {"panel": True})
        self.push(Scene999.FIELD, 3.0, flags={"f_prompt": True})
        self.assertIsNone(self.push(Scene999.CAMP, 4.0, {}))
        self.assertEqual(self.task.phase, Phase.MENU)

    def test_map_waits_before_reporting_missing(self):
        self.push(Scene999.FIELD, 0.0)
        self.assertIsNone(self.push(Scene999.MAP, 1.0, {}, {"panel": False}))
        self.assertIsNone(self.push(Scene999.MAP, 3.5, {}, {"panel": False}))
        with self.assertRaises(TaskTimeout):
            self.push(Scene999.MAP, 4.2, {}, {"panel": False})

    def test_panel_without_icon_still_teleports(self):
        self.push(Scene999.FIELD, 0.0)
        intent = self.push(Scene999.MAP, 1.0, {"teleport": (1295, 755)}, {"panel": True})
        self.assertEqual(intent.action, Action999.CLICK_TELEPORT)

    def test_blind_battle_tick(self):
        task = Nte999Task(Timings(), max_loops=0, now=0.0)
        task.phase, task.s_start = Phase.S, 0.0
        self.assertFalse(task.blind)
        self.assertIsNone(task.observe(obs(Scene999.FIELD), 1.9))
        release = task.observe(obs(Scene999.FIELD), 2.0)
        self.assertEqual(release.action, Action999.RELEASE_S)
        self.assertEqual(release.key, "s")
        task.commit(release, 2.0)
        self.assertTrue(task.blind)
        self.assertEqual(task.battle_start, 2.0)
        first = task.tick(2.0)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].action, Action999.TAP_SHIFT)
        task.commit(first[0], 2.0)
        self.assertEqual(task.phase, Phase.BATTLE)
        now = 2.0
        for _ in range(39):
            now += 2.0
            intents = task.tick(now)
            self.assertEqual(len(intents), 1)
            task.commit(intents[0], now)
        self.assertEqual(task.shift_count, 40)
        self.assertEqual(task.tick(81.9), [])
        self.assertEqual(task.tick(82.0), [])  # S已在走位结束时松开；战斗结束只切换状态。
        self.assertEqual(task.phase, Phase.MAP_AFTER)
        self.assertEqual(task.battle_finished_at, 82.0)
        self.assertFalse(task.blind)


class DetectorAssetTests(unittest.TestCase):
    def test_assets_load_and_blank_is_unknown(self):
        detector = Detector999(ROOT / "assets999")
        frame = np.zeros((2160, 3840, 3), dtype=np.uint8)
        result = detector.detect(frame)
        self.assertEqual(result.scene, Scene999.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
