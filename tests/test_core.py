import unittest
from yihuan.core import Action, KaciTask, Observation, Phase, Scene, TaskTimeout


class TaskTests(unittest.TestCase):
    def make(self, loops=1):
        return KaciTask(max_loops=loops, now=0)

    def act(self, task, scene, now, expected):
        command = task.observe(Observation(scene), now)
        self.assertIsNotNone(command)
        self.assertEqual(command.action, expected)
        task.commit(command, now)
        return command

    def enter_match(self, task):
        self.act(task, Scene.ACTIVITY, 0, Action.GO)
        self.act(task, Scene.MATCH, 2, Action.MATCH)

    def enter_battle(self, task, now=34):
        self.enter_match(task)
        self.assertIsNone(task.observe(Observation(Scene.BATTLE), now))
        self.assertEqual(task.phase, Phase.WATCH)

    def test_active_exit_full_cycle(self):
        t = self.make()
        self.enter_battle(t)
        self.assertIsNone(t.observe(Observation(Scene.BATTLE), 38.99))
        self.act(t, Scene.BATTLE, 39, Action.ESC_BATTLE)
        self.act(t, Scene.DIALOG, 39.3, Action.CONFIRM_BATTLE)
        self.assertIsNone(t.observe(Observation(Scene.DEFEAT), 40.29))
        self.act(t, Scene.DEFEAT, 40.31, Action.ESC_RESULT)
        self.act(t, Scene.DIALOG, 40.6, Action.CONFIRM_RESULT)
        t.observe(Observation(Scene.ACTIVITY), 52.6)
        self.assertTrue(t.done)
        self.assertEqual(t.completed, 1)
        self.assertIsNone(t.observe(Observation(Scene.ACTIVITY), 60))

    def test_victory_during_five_second_watch(self):
        t = self.make()
        self.enter_battle(t)
        self.act(t, Scene.VICTORY, 35, Action.ESC_RESULT)
        self.act(t, Scene.DIALOG, 35.3, Action.CONFIRM_RESULT)
        t.observe(Observation(Scene.ACTIVITY), 47.3)
        self.assertTrue(t.done)

    def test_victory_before_battle_is_seen(self):
        t = self.make()
        self.enter_match(t)
        self.act(t, Scene.VICTORY, 25, Action.ESC_RESULT)

    def test_five_seconds_begin_at_actual_battle_not_match_timer(self):
        t = self.make()
        self.enter_battle(t, now=50)
        self.assertIsNone(t.observe(Observation(Scene.BATTLE), 54.99))
        self.assertEqual(t.observe(Observation(Scene.BATTLE), 55).action, Action.ESC_BATTLE)

    def test_victory_instead_of_expected_battle_dialog(self):
        t = self.make()
        self.enter_battle(t)
        self.act(t, Scene.BATTLE, 39, Action.ESC_BATTLE)
        self.act(t, Scene.VICTORY, 39.2, Action.ESC_RESULT)
        self.act(t, Scene.DIALOG, 39.5, Action.CONFIRM_RESULT)

    def test_result_returning_directly_to_activity_skips_second_escape(self):
        t = self.make()
        self.enter_battle(t)
        self.act(t, Scene.BATTLE, 39, Action.ESC_BATTLE)
        self.act(t, Scene.DIALOG, 39.3, Action.CONFIRM_BATTLE)
        self.assertIsNone(t.observe(Observation(Scene.ACTIVITY), 40))
        self.assertTrue(t.done)

    def test_two_loops_and_no_extra_go_at_limit(self):
        t = self.make(2)
        self.enter_match(t)
        self.act(t, Scene.VICTORY, 25, Action.ESC_RESULT)
        self.act(t, Scene.DIALOG, 25.3, Action.CONFIRM_RESULT)
        t.observe(Observation(Scene.ACTIVITY), 37.3)
        self.assertEqual(t.phase, Phase.READY)
        self.act(t, Scene.ACTIVITY, 37.6, Action.GO)
        self.act(t, Scene.MATCH, 39.6, Action.MATCH)
        self.act(t, Scene.DEFEAT, 60, Action.ESC_RESULT)
        self.act(t, Scene.DIALOG, 60.3, Action.CONFIRM_RESULT)
        t.observe(Observation(Scene.ACTIVITY), 72.3)
        self.assertTrue(t.done)
        self.assertEqual(t.completed, 2)

    def test_loading_unknown_for_more_than_15_seconds_is_expected(self):
        t = self.make()
        self.enter_match(t)
        for now in (3, 10, 20, 32, 40):
            self.assertIsNone(t.observe(Observation(Scene.UNKNOWN), now))
        self.assertEqual(t.phase, Phase.LOAD)
        t.observe(Observation(Scene.BATTLE), 45)
        self.assertEqual(t.phase, Phase.WATCH)

    def test_loading_timeout_and_unknown_timeout(self):
        t = self.make()
        self.enter_match(t)
        with self.assertRaises(TaskTimeout):
            t.observe(Observation(Scene.UNKNOWN), 92)
        t = self.make()
        t.observe(Observation(Scene.UNKNOWN), 0)
        with self.assertRaises(TaskTimeout):
            t.observe(Observation(Scene.UNKNOWN), 15)

    def test_return_unknown_uses_return_timeout(self):
        t = self.make()
        self.enter_match(t)
        self.act(t, Scene.VICTORY, 25, Action.ESC_RESULT)
        self.act(t, Scene.DIALOG, 25.3, Action.CONFIRM_RESULT)
        t.observe(Observation(Scene.UNKNOWN), 26)
        self.assertIsNone(t.observe(Observation(Scene.UNKNOWN), 45))
        with self.assertRaises(TaskTimeout):
            t.observe(Observation(Scene.UNKNOWN), 71)

    def test_dialog_must_appear_before_confirmation(self):
        t = self.make()
        self.enter_battle(t)
        self.act(t, Scene.BATTLE, 39, Action.ESC_BATTLE)
        self.assertIsNone(t.observe(Observation(Scene.BATTLE), 40))
        with self.assertRaises(TaskTimeout):
            t.observe(Observation(Scene.BATTLE), 47)

    def test_no_esc_spam_while_waiting_for_result_dialog(self):
        t = self.make()
        self.enter_match(t)
        self.act(t, Scene.VICTORY, 25, Action.ESC_RESULT)
        for now in (25.2, 25.4, 26, 27):
            self.assertIsNone(t.observe(Observation(Scene.VICTORY), now))

    def test_start_on_wrong_page_does_not_press_anything(self):
        t = self.make()
        for scene in (Scene.BATTLE, Scene.DIALOG, Scene.MATCH, Scene.VICTORY):
            self.assertIsNone(t.observe(Observation(scene), 1))
        self.assertEqual(t.phase, Phase.READY)

    def test_failed_action_does_not_advance_and_stale_commit_is_rejected(self):
        t = self.make()
        command = t.observe(Observation(Scene.ACTIVITY), 0)
        self.assertEqual(t.phase, Phase.READY)
        t.commit(command, .1)
        with self.assertRaises(RuntimeError):
            t.commit(command, .2)

    def test_unlimited_mode_continues(self):
        t = self.make(0)
        self.enter_match(t)
        self.act(t, Scene.VICTORY, 25, Action.ESC_RESULT)
        self.act(t, Scene.DIALOG, 25.3, Action.CONFIRM_RESULT)
        t.observe(Observation(Scene.ACTIVITY), 37.3)
        self.assertFalse(t.done)
        self.assertEqual(t.completed, 1)

    def test_go_retries_once_only_while_activity_still_visible(self):
        t = self.make()
        self.act(t, Scene.ACTIVITY, 0, Action.GO)
        self.assertIsNone(t.observe(Observation(Scene.ACTIVITY), 2.99))
        self.assertIsNone(t.observe(Observation(Scene.UNKNOWN), 3.0))
        self.act(t, Scene.ACTIVITY, 3.1, Action.GO)
        self.assertEqual(t.entered, 0)
        self.assertEqual(t.go_attempts, 2)
        self.assertFalse(t.round_started)
        for now in (3.4, 6.2, 14.9):
            self.assertIsNone(t.observe(Observation(Scene.ACTIVITY), now))
        with self.assertRaisesRegex(TaskTimeout, "前往点击后仍停在活动页.*2次"):
            t.check_timeout(15)

    def test_matching_page_acknowledges_go_instead_of_retrying(self):
        t = self.make()
        self.act(t, Scene.ACTIVITY, 0, Action.GO)
        self.act(t, Scene.MATCH, 3.1, Action.MATCH)
        self.assertEqual(t.go_attempts, 1)
        self.assertEqual(t.phase, Phase.LOAD)

    def test_go_attempt_budget_resets_for_next_round(self):
        t = self.make(2)
        self.act(t, Scene.ACTIVITY, 0, Action.GO)
        self.act(t, Scene.ACTIVITY, 3.1, Action.GO)
        self.act(t, Scene.MATCH, 5, Action.MATCH)
        self.act(t, Scene.VICTORY, 25, Action.ESC_RESULT)
        self.act(t, Scene.DIALOG, 25.3, Action.CONFIRM_RESULT)
        t.observe(Observation(Scene.ACTIVITY), 37.3)
        self.act(t, Scene.ACTIVITY, 37.6, Action.GO)
        self.assertEqual(t.go_attempts, 1)


if __name__ == "__main__":
    unittest.main()
