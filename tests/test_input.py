import ctypes
import threading
import unittest
from yihuan.core import Action, Intent, Phase, Scene
from yihuan.windows import Actor, Cancelled, GameWindow, INPUT, KEYBDINPUT, MOUSEINPUT, WinApi


class FakeApi:
    def __init__(self, failure=None, cursor_offset=(0, 0), corrects=True):
        self.sent, self.held = [], set()
        self.failure, self.count, self.ctrl, self.focus = failure, 0, False, True
        self.cursor, self.requested = (0, 0), None
        self.cursor_offset, self.corrects = cursor_offset, corrects
        self.corrections = []

    def key_down(self, key):
        return self.ctrl

    def verify(self, window):
        if not self.focus:
            raise RuntimeError("lost focus")

    def absolute_position(self, x, y):
        self.requested = (x, y)
        return round(x * 65535 / 3839), round(y * 65535 / 2159)

    def cursor_position(self): return self.cursor

    def set_physical_position(self, x, y):
        self.corrections.append((x, y))
        if self.corrects: self.cursor = (x, y)

    def send(self, item):
        self.count += 1
        if self.count == self.failure:
            raise RuntimeError("injected input failure")
        kind = "mouse" if item.type == 0 else "key"
        flags = int(item.mi.dwFlags if item.type == 0 else item.ki.dwFlags)
        self.sent.append((kind, flags))
        if kind == "mouse":
            if flags == 0xC001:
                self.cursor = tuple(p + d for p, d in zip(self.requested, self.cursor_offset))
            if flags == 2: self.held.add("left")
            if flags == 4: self.held.discard("left")
        else:
            if flags == 8: self.held.add("esc")
            if flags == 10: self.held.discard("esc")


class StopOnWait:
    def __init__(self, at):
        self.at, self.count, self.stopped = at, 0, False
    def is_set(self): return self.stopped
    def wait(self, seconds):
        self.count += 1
        if self.count >= self.at: self.stopped = True
        return self.stopped


class InputTests(unittest.TestCase):
    window = GameWindow(1, "异环", 0, 0, 3840, 2160)
    go = Intent(Action.GO, Scene.ACTIVITY, Phase.READY)
    esc = Intent(Action.ESC_BATTLE, Scene.BATTLE, Phase.WATCH)

    def test_native_input_structure_sizes(self):
        self.assertEqual(ctypes.sizeof(INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
        self.assertEqual(ctypes.sizeof(MOUSEINPUT), 32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 24)

    def test_click_is_move_down_up(self):
        api = FakeApi()
        actor = Actor(api, self.window, threading.Event())
        actor.perform(self.go, {"go": (1319, 791)})
        self.assertEqual(api.sent, [("mouse", 0xC001), ("mouse", 2), ("mouse", 4)])
        self.assertFalse(api.held)

    def test_escape_uses_scan_code_and_is_released(self):
        api = FakeApi()
        actor = Actor(api, self.window, threading.Event())
        actor.perform(self.esc, {})
        self.assertEqual(api.sent, [("key", 8), ("key", 10)])
        self.assertFalse(api.held)

    def test_stop_before_mouse_down_prevents_click(self):
        api = FakeApi()
        actor = Actor(api, self.window, StopOnWait(1))
        with self.assertRaises(Cancelled): actor.perform(self.go, {"go": (1319, 791)})
        self.assertEqual(len(api.sent), 1)
        self.assertFalse(api.held)

    def test_stop_while_holding_mouse_or_escape_releases(self):
        for command, wait_at in [(self.go, 4), (self.esc, 1)]:
            with self.subTest(command=command.action):
                api = FakeApi()
                actor = Actor(api, self.window, StopOnWait(wait_at))
                actor.perform(command, {"go": (1319, 791)})
                actor.release()
                self.assertFalse(api.held)
                self.assertFalse(actor.held)

    def test_lost_focus_ctrl_and_stop_prevent_input(self):
        for reason in ("focus", "ctrl", "stop"):
            api, stop = FakeApi(), threading.Event()
            if reason == "focus": api.focus = False
            if reason == "ctrl": api.ctrl = True
            if reason == "stop": stop.set()
            actor = Actor(api, self.window, stop)
            with self.assertRaises((RuntimeError, Cancelled)): actor.perform(self.esc, {})
            self.assertEqual(api.sent, [])

    def test_down_and_release_errors_cleanup(self):
        for failure in (2, 3):
            with self.subTest(call=failure):
                api = FakeApi(failure)
                actor = Actor(api, self.window, threading.Event())
                with self.assertRaises(RuntimeError): actor.perform(self.go, {"go": (1319, 791)})
                actor.release()
                self.assertFalse(api.held)
                self.assertFalse(actor.held)

    def test_virtual_desktop_negative_origin_conversion(self):
        api = object.__new__(WinApi)
        metrics = {76: -1920, 77: 0, 78: 5760, 79: 2160}
        api.user = type("User", (), {"GetSystemMetrics": lambda self, i: metrics[i]})()
        self.assertEqual(api.absolute_position(-1920, 0), (0, 0))
        self.assertEqual(api.absolute_position(3839, 2159), (65535, 65535))

    def test_cursor_offset_is_corrected_before_click(self):
        api = FakeApi(cursor_offset=(-1000, -400))
        actor = Actor(api, self.window, threading.Event())
        with self.assertLogs("yihuan", level="WARNING"):
            actor.perform(self.go, {"go": (1319, 791.5)})
        self.assertEqual(api.corrections, [(3379, 2028)])
        self.assertEqual(actor.last_input["cursor_after_move"], [2379, 1628])
        self.assertEqual(actor.last_input["cursor_after_correction"], [3379, 2028])
        self.assertEqual(actor.last_input["move_method"], "SetPhysicalCursorPos")
        self.assertFalse(api.held)

    def test_failed_position_correction_never_presses_left(self):
        api = FakeApi(cursor_offset=(-1000, -400), corrects=False)
        actor = Actor(api, self.window, threading.Event())
        with self.assertLogs("yihuan", level="WARNING"):
            with self.assertRaisesRegex(RuntimeError, "鼠标未到按钮"):
                actor.perform(self.go, {"go": (1319, 791.5)})
        self.assertEqual(api.sent, [("mouse", 0xC001)])
        self.assertFalse(actor.held)
        self.assertEqual(actor.last_input["stage"], "鼠标位置不符，未按左键")

    def test_stop_during_position_correction_never_clicks(self):
        api = FakeApi(cursor_offset=(-1000, 0))
        actor = Actor(api, self.window, StopOnWait(4))
        with self.assertLogs("yihuan", level="WARNING"):
            with self.assertRaises(Cancelled): actor.perform(self.go, {"go": (1319, 791.5)})
        self.assertEqual(api.sent, [("mouse", 0xC001)])

    def test_button_coordinate_includes_negative_monitor_origin(self):
        window = GameWindow(1, "异环", -3840, -120, 3840, 2160)
        actor = Actor(FakeApi(), window, threading.Event())
        actor.perform(self.go, {"go": (1319, 791.5)})
        self.assertEqual(actor.last_input["screen_target"], [-461, 1908])

    def test_out_of_range_button_coordinate_is_rejected(self):
        api = FakeApi()
        actor = Actor(api, self.window, threading.Event())
        with self.assertRaisesRegex(RuntimeError, "坐标超出"):
            actor.perform(self.go, {"go": (3000, 791)})
        self.assertEqual(api.sent, [])

    def test_physical_cursor_api_read_write_and_failures(self):
        api = object.__new__(WinApi)
        class User:
            def GetPhysicalCursorPos(self, point):
                point._obj.x, point._obj.y = -461, 1908
                return True
            def SetPhysicalCursorPos(self, x, y): return (x, y) == (-461, 1908)
        api.user = User()
        self.assertEqual(api.cursor_position(), (-461, 1908))
        api.set_physical_position(-461, 1908)
        with self.assertRaisesRegex(RuntimeError, "未接受物理坐标"):
            api.set_physical_position(1, 2)
        api.user.GetPhysicalCursorPos = lambda point: False
        with self.assertRaisesRegex(RuntimeError, "无法回读"):
            api.cursor_position()


if __name__ == "__main__":
    unittest.main()
