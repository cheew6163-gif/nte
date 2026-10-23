import queue
import unittest
from unittest.mock import patch
from yihuan.runtime import Hotkeys


class Steps:
    def __init__(self, states):
        self.states, self.index = states, 0
    def is_set(self): return self.index >= len(self.states)
    def wait(self, seconds): self.index += 1
    @property
    def current(self): return self.states[min(self.index, len(self.states) - 1)]


class FakeRunner:
    def __init__(self, busy=False): self.busy, self.stops = busy, 0
    def stop(self): self.stops += 1; self.busy = False


class HotkeyTests(unittest.TestCase):
    def simulate(self, states, runner):
        steps, events = Steps(states), queue.Queue()
        class Api:
            def key_down(self, key): return steps.current.get(key, False)
            def foreground_matches(self, pattern): return steps.current.get("front", True)
        hotkeys = Hotkeys(runner, events, steps, "异环")
        with patch("yihuan.runtime.WinApi", Api), \
             patch("yihuan.runtime.time.monotonic", side_effect=lambda: 10 + steps.index * .01):
            hotkeys.run()
        messages = []
        while not events.empty(): messages.append(events.get_nowait())
        return messages

    def test_start_edges_only_when_game_foreground(self):
        messages = self.simulate([
            {6: True}, {6: True}, {}, {6: True, "front": False}, {}, {6: True}, {6: True}, {}, {0x77: True}
        ], FakeRunner())
        self.assertEqual([kind for kind, _ in messages].count("start_request"), 2)

    def test_ctrl_stops_and_prevents_immediate_restart(self):
        runner = FakeRunner(True)
        messages = self.simulate([{}, {0xA2: True}, {}, {6: True}, {}, {0x77: True}], runner)
        self.assertEqual(runner.stops, 1)
        self.assertNotIn("start_request", [kind for kind, _ in messages])


if __name__ == "__main__":
    unittest.main()
