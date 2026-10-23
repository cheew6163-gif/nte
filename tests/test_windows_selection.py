import ctypes
import os
import threading
import unittest
from unittest.mock import Mock
from yihuan.windows import GameWindow, WinApi, thread_dpi_awareness


class SelectionTests(unittest.TestCase):
    config = {"window_title_regex": r"异环|Neverness|\bNTE\b", "screen_width": 3840, "screen_height": 2160}

    def make(self, rows, foreground=0):
        api = object.__new__(WinApi)
        api.window_handles = lambda: list(rows)
        api.title = lambda hwnd: rows[hwnd]["title"]
        api.process_id = lambda hwnd: rows[hwnd]["pid"]
        api.class_name = lambda hwnd: rows[hwnd].get("class", "GameWindow")
        queried = []
        def geometry(hwnd):
            queried.append(hwnd)
            row = rows[hwnd]
            return GameWindow(hwnd, row["title"], row.get("left", 0), row.get("top", 0),
                              row.get("width", 3840), row.get("height", 2160))
        api.geometry = geometry
        class User:
            def IsWindowVisible(self, hwnd): return True
            def IsIconic(self, hwnd): return rows[hwnd].get("minimized", False)
            def GetForegroundWindow(self): return foreground
        api.user = User()
        return api, queried

    def test_minimized_assistant_is_never_selected_as_game(self):
        rows = {
            10: {"title": "异环 · 视觉助手", "pid": os.getpid(), "width": 760, "height": 480, "minimized": True},
            20: {"title": "异环", "pid": os.getpid()+1, "left": 0, "top": 0},
        }
        api, queried = self.make(rows, foreground=10)
        self.assertEqual(api.find_game(self.config).hwnd, 20)
        self.assertNotIn(10, queried)
        self.assertEqual(api.candidate_log[0]["excluded"], "assistant")

    def test_only_assistant_is_not_reported_as_valid_game(self):
        api, _ = self.make({10: {"title": "异环 · 视觉助手", "pid": os.getpid(), "minimized": True}})
        with self.assertRaisesRegex(RuntimeError, "排除助手自身"):
            api.find_game(self.config)

    def test_foreign_assistant_title_is_also_excluded(self):
        api, queried = self.make({10: {"title": "异环 · 视觉助手 0.1", "pid": os.getpid()+1, "minimized": True}})
        with self.assertRaises(RuntimeError): api.find_game(self.config)
        self.assertEqual(queried, [])

    def test_actual_game_can_be_selected_before_resolution_validation(self):
        # 150%下即使暂时报告2560x1440，也不能回退成“选择助手自己”。
        api, _ = self.make({20: {"title": "异环", "pid": os.getpid()+1, "width": 2560, "height": 1440}})
        self.assertEqual(api.find_game(self.config).hwnd, 20)

    def test_browser_and_console_titles_do_not_become_games(self):
        api, queried = self.make({
            1: {"title": "异环攻略 - Browser", "pid": os.getpid()+1, "class": "Chrome_WidgetWin_1", "minimized": True},
            2: {"title": "异环脚本 cmd", "pid": os.getpid()+2, "class": "ConsoleWindowClass", "minimized": True},
            3: {"title": "异环", "pid": os.getpid()+3},
        })
        self.assertEqual(api.find_game(self.config).hwnd, 3)
        self.assertEqual(queried, [3])

    def test_g5_on_assistant_foreground_is_not_game_foreground(self):
        api, _ = self.make({10: {"title": "异环 · 视觉助手", "pid": os.getpid()}}, foreground=10)
        self.assertFalse(api.foreground_matches("异环"))

    def test_activate_refuses_self_even_if_passed_wrong_window(self):
        api, _ = self.make({10: {"title": "异环 · 视觉助手", "pid": os.getpid()}})
        with self.assertRaisesRegex(RuntimeError, "拒绝"):
            api.activate(GameWindow(10, "异环 · 视觉助手", 0, 0, 760, 480), threading.Event())

    def test_external_4k_at_negative_origin_is_selected(self):
        api, _ = self.make({20: {"title": "异环", "pid": os.getpid()+1, "left": -3840, "top": -200}})
        game = api.find_game(self.config)
        self.assertEqual((game.left, game.top, game.width, game.height), (-3840, -200, 3840, 2160))

    def test_other_foreign_window_resolution_does_not_hide_exact_4k_game(self):
        api, _ = self.make({
            20: {"title": "异环", "pid": os.getpid()+1},
            30: {"title": "异环登录", "pid": os.getpid()+2, "width": 1280, "height": 720},
        })
        self.assertEqual(api.find_game(self.config).hwnd, 20)

    def test_thread_dpi_requests_per_monitor_v2(self):
        user = Mock()
        user.SetThreadDpiAwarenessContext.return_value = 1
        status = thread_dpi_awareness(user)
        self.assertTrue(status["success"])
        self.assertEqual(status["context"], "PerMonitorV2")
        expected = ctypes.c_void_p(-4).value
        self.assertEqual(user.SetThreadDpiAwarenessContext.call_args.args[0].value, expected)

    def test_thread_dpi_fallback_is_recorded(self):
        user = Mock()
        user.SetThreadDpiAwarenessContext.side_effect = [0, 1]
        self.assertEqual(thread_dpi_awareness(user), {"success": True, "context": "PerMonitor"})

    def test_thread_dpi_failure_is_not_silently_successful(self):
        user = Mock()
        user.SetThreadDpiAwarenessContext.return_value = 0
        self.assertFalse(thread_dpi_awareness(user)["success"])

    def test_geometry_converts_both_client_corners_to_screen(self):
        api = object.__new__(WinApi)
        api.title = lambda hwnd: "异环"
        class User:
            def GetClientRect(self, hwnd, output):
                output._obj.left, output._obj.top = 0, 0
                output._obj.right, output._obj.bottom = 2560, 1440
                return True
            def ClientToScreen(self, hwnd, output):
                point = output._obj
                point.x, point.y = round(point.x * 1.5) - 3840, round(point.y * 1.5)
                return True
        api.user = User()
        game = api.geometry(20)
        self.assertEqual((game.left, game.top, game.width, game.height), (-3840, 0, 3840, 2160))

    def test_diagnostics_distinguish_1600_laptop_and_4k_primary(self):
        api, _ = self.make({20: {"title": "异环", "pid": os.getpid()+1}}, foreground=20)
        laptop = {"device": "laptop", "left": -2560, "top": 240, "width": 2560, "height": 1600, "primary": False}
        external = {"device": "external", "left": 0, "top": 0, "width": 3840, "height": 2160, "primary": True}
        api.dpi_status = {"success": True, "context": "PerMonitorV2"}
        api.candidate_log = []
        api.monitors = lambda: [laptop, external]
        api.monitor_info = lambda handle: external
        api.user.MonitorFromWindow = Mock(return_value=2)
        api.user.GetDpiForWindow = Mock(return_value=144)
        result = api.diagnostics(api.geometry(20))
        self.assertEqual(result["target_monitor"], external)
        self.assertEqual(result["window_dpi"], 144)
        self.assertEqual(result["target"]["width"], 3840)
        self.assertEqual(result["foreground"]["hwnd"], 20)


if __name__ == "__main__":
    unittest.main()
