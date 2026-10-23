"""Windows 截图、前台窗口和输入适配。模块导入本身不发送输入。"""
import ctypes as C
from ctypes import wintypes as W
from dataclasses import asdict, dataclass
import logging
import math
from .native_diagnostics import clear_error, last_error, error_details, WinApiError
import os
import re
import threading
import time
import numpy as np


DWORD, LONG, WORD, ULONG_PTR = C.c_uint32, C.c_int32, C.c_uint16, C.c_size_t


class MOUSEINPUT(C.Structure):
    _fields_ = [("dx", LONG), ("dy", LONG), ("mouseData", DWORD), ("dwFlags", DWORD),
                ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(C.Structure):
    _fields_ = [("wVk", WORD), ("wScan", WORD), ("dwFlags", DWORD),
                ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(C.Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class INPUT_UNION(C.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(C.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", DWORD), ("value", INPUT_UNION)]


class Cancelled(Exception):
    pass


class ActionChanged(Exception):
    """定位期间画面变化：取消当前动作，交回状态机重新识别。"""
    pass


def thread_dpi_awareness(user):
    """每个工作线程单独请求物理像素坐标，不能只依赖主线程的进程设置。"""
    try:
        function = user.SetThreadDpiAwarenessContext
        function.argtypes, function.restype = [C.c_void_p], C.c_void_p
        for context in (-4, -3):  # PerMonitorV2，旧版Win10回退PerMonitor。
            if function(C.c_void_p(context)):
                return {"success": True, "context": "PerMonitorV2" if context == -4 else "PerMonitor"}
        return {"success": False, "error": getattr(C, "get_last_error", lambda: 0)()}
    except AttributeError:
        return {"success": False, "error": "SetThreadDpiAwarenessContext unavailable"}


def enable_dpi_awareness():
    if os.name != "nt":
        return
    user = C.WinDLL("user32", use_last_error=True)
    try:
        function = user.SetProcessDpiAwarenessContext
        function.argtypes, function.restype = [C.c_void_p], W.BOOL
        function(C.c_void_p(-4))  # PerMonitorV2；必须在创建Tk窗口前调用。
    except AttributeError:
        user.SetProcessDPIAware()
    return thread_dpi_awareness(user)


def single_instance():
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [C.c_void_p, W.BOOL, W.LPCWSTR]
    kernel.CreateMutexW.restype = W.HANDLE
    handle = kernel.CreateMutexW(None, False, "Local\\YiHuanVisionAssistant_01")
    if not handle:
        raise C.WinError(C.get_last_error())
    if C.get_last_error() == 183:
        kernel.CloseHandle.argtypes = [W.HANDLE]
        kernel.CloseHandle(handle)
        raise RuntimeError("视觉助手已经运行，请切换到已打开的窗口")
    return handle  # 由进程保留到结束，防止多个实例同时发出操作。


@dataclass(frozen=True)
class GameWindow:
    hwnd: int
    title: str
    left: int
    top: int
    width: int
    height: int


class MONITORINFOEXW(C.Structure):
    _fields_ = [("cbSize", DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT),
                ("dwFlags", DWORD), ("szDevice", W.WCHAR * 32)]


class WinApi:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("实际截图和输入只支持Windows；离线图片检查可在其他系统运行")
        self.user = C.WinDLL("user32", use_last_error=True)
        signatures = {
            "GetForegroundWindow": (W.HWND, []),
            "GetWindowTextW": (C.c_int, [W.HWND, W.LPWSTR, C.c_int]),
            "GetClientRect": (W.BOOL, [W.HWND, C.POINTER(W.RECT)]),
            "ClientToScreen": (W.BOOL, [W.HWND, C.POINTER(W.POINT)]),
            "IsWindowVisible": (W.BOOL, [W.HWND]),
            "IsIconic": (W.BOOL, [W.HWND]),
            "ShowWindow": (W.BOOL, [W.HWND, C.c_int]),
            "SetForegroundWindow": (W.BOOL, [W.HWND]),
            "GetAsyncKeyState": (C.c_int16, [C.c_int]),
            "GetSystemMetrics": (C.c_int, [C.c_int]),
            "SendInput": (W.UINT, [W.UINT, C.POINTER(INPUT), C.c_int]),
            "GetPhysicalCursorPos": (W.BOOL, [C.POINTER(W.POINT)]),
            "GetCursorPos": (W.BOOL, [C.POINTER(W.POINT)]),
            "GetClipCursor": (W.BOOL, [C.POINTER(W.RECT)]),
            "SetPhysicalCursorPos": (W.BOOL, [C.c_int, C.c_int]),
            "GetWindowThreadProcessId": (DWORD, [W.HWND, C.POINTER(DWORD)]),
            "GetClassNameW": (C.c_int, [W.HWND, W.LPWSTR, C.c_int]),
            "MonitorFromWindow": (W.HANDLE, [W.HWND, DWORD]),
            "GetMonitorInfoW": (W.BOOL, [W.HANDLE, C.POINTER(MONITORINFOEXW)]),
        }
        for name, (result, args) in signatures.items():
            fn = getattr(self.user, name)
            fn.restype, fn.argtypes = result, args
        self.dpi_status = thread_dpi_awareness(self.user)
        self.candidate_log = []
        self.api_calls = []
        self.last_api_call = None
        if not self.dpi_status["success"]:
            raise RuntimeError(f"无法启用工作线程的物理像素坐标：{self.dpi_status}")

    def title(self, hwnd):
        buffer = C.create_unicode_buffer(1024)
        self.user.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def key_down(self, key):
        return bool(self.user.GetAsyncKeyState(key) & 0x8000)

    def process_id(self, hwnd):
        process = DWORD()
        self.user.GetWindowThreadProcessId(hwnd, C.byref(process))
        return int(process.value)

    def class_name(self, hwnd):
        buffer = C.create_unicode_buffer(256)
        self.user.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value

    def is_assistant(self, hwnd, title):
        return self.process_id(hwnd) == os.getpid() or "视觉助手" in title

    def geometry(self, hwnd):
        rect = W.RECT()
        clear_error()
        ok = self.user.GetClientRect(hwnd, C.byref(rect))
        code = last_error()
        if not ok:
            raise WinApiError("无法读取游戏窗口位置", error_details("GetClientRect", {"hwnd": int(hwnd)}, code))
        origin, corner = W.POINT(rect.left, rect.top), W.POINT(rect.right, rect.bottom)
        for label, point in (("origin", origin), ("corner", corner)):
            args = {"hwnd": int(hwnd), "point": [int(point.x), int(point.y)], "corner": label}
            clear_error()
            ok = self.user.ClientToScreen(hwnd, C.byref(point))
            code = last_error()
            if not ok:
                raise WinApiError("无法换算游戏客户区坐标", error_details("ClientToScreen", args, code))
        # 原点和尺寸均由同一屏幕坐标系中的两角得到，避免混用客户区与屏幕单位。
        return GameWindow(hwnd, self.title(hwnd), origin.x, origin.y, corner.x - origin.x, corner.y - origin.y)

    def foreground_matches(self, pattern):
        hwnd = self.user.GetForegroundWindow()
        if not hwnd:
            return False
        title = self.title(hwnd)
        return bool(not self.is_assistant(hwnd, title) and re.search(pattern, title, re.I))

    def window_handles(self):
        handles = []
        callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)

        @callback_type
        def visit(hwnd, _):
            handles.append(hwnd)
            return True

        self.user.EnumWindows.argtypes = [callback_type, W.LPARAM]
        self.user.EnumWindows.restype = W.BOOL
        self.user.EnumWindows(visit, 0)
        return handles

    def find_game(self, config):
        candidates = []
        self.candidate_log = []
        for hwnd in self.window_handles():
            title = self.title(hwnd)
            if not self.user.IsWindowVisible(hwnd) or not re.search(config["window_title_regex"], title, re.I):
                continue
            row = {"hwnd": hwnd, "pid": self.process_id(hwnd), "title": title}
            self.candidate_log.append(row)
            # 助手的标题同样含“异环”，必须在最小化/尺寸筛选之前排除自身。
            if self.is_assistant(hwnd, title):
                row["excluded"] = "assistant"
                continue
            if self.class_name(hwnd) in ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "Chrome_WidgetWin_1"):
                row["excluded"] = "console_or_browser"
                continue
            try:
                item = self.geometry(hwnd)
                row.update(asdict(item))
                row["minimized"] = bool(self.user.IsIconic(hwnd))
                # 先选实际游戏，再恢复并核验物理尺寸。未选中时不能用尺寸判断掩盖标题错误。
                if row["minimized"] or (item.width >= 640 and item.height >= 360):
                    candidates.append(item)
            except RuntimeError as exc:
                row["error"] = str(exc)
        if not candidates:
            raise RuntimeError("未找到游戏窗口；已排除助手自身。请查看启动诊断中的窗口标题，必要时修改window_title_regex")
        foreground = self.user.GetForegroundWindow()
        active = [w for w in candidates if w.hwnd == foreground]
        if active:
            return active[0]
        exact = [w for w in candidates if (w.width, w.height) == (config["screen_width"], config["screen_height"])]
        if len(exact) == 1:
            return exact[0]
        if len(candidates) != 1:
            raise RuntimeError("找到多个游戏窗口，请切回目标游戏后按G5启动")
        return candidates[0]

    def activate(self, window, stop_event):
        if self.is_assistant(window.hwnd, window.title):
            raise RuntimeError("拒绝将视觉助手自身作为游戏窗口")
        if self.user.IsIconic(window.hwnd):
            self.user.ShowWindow(window.hwnd, 9)
        clear_error()
        activated = self.user.SetForegroundWindow(window.hwnd)
        code = last_error()
        self.last_activation = error_details("SetForegroundWindow", {"hwnd": int(window.hwnd)}, code, int(bool(activated)))
        deadline = time.monotonic() + 3.0
        last = None
        while time.monotonic() < deadline:
            if stop_event.is_set():
                raise Cancelled()
            if self.user.GetForegroundWindow() == window.hwnd:
                # 独占全屏游戏从最小化恢复后，窗口原点可能改变，重新取得实际坐标。
                last = self.geometry(window.hwnd)
                if (last.width, last.height) == (3840, 2160):
                    return last
            stop_event.wait(.05)
        if last is not None and self.user.GetForegroundWindow() == window.hwnd:
            return last
        raise RuntimeError("游戏未能切到前台；请手动切回游戏，在活动页按G5启动")

    def monitor_info(self, handle):
        info = MONITORINFOEXW()
        info.cbSize = C.sizeof(info)
        if not self.user.GetMonitorInfoW(handle, C.byref(info)):
            raise RuntimeError("无法读取显示器信息")
        r = info.rcMonitor
        return {"device": info.szDevice, "left": r.left, "top": r.top,
                "width": r.right - r.left, "height": r.bottom - r.top, "primary": bool(info.dwFlags & 1)}

    def monitors(self):
        result = []
        callback_type = C.WINFUNCTYPE(W.BOOL, W.HANDLE, W.HDC, C.POINTER(W.RECT), W.LPARAM)
        @callback_type
        def visit(handle, dc, rect, data):
            try:
                result.append(self.monitor_info(handle))
            except RuntimeError as exc:
                result.append({"error": str(exc)})
            return True
        function = self.user.EnumDisplayMonitors
        function.argtypes = [W.HDC, C.POINTER(W.RECT), callback_type, W.LPARAM]
        function.restype = W.BOOL
        function(None, None, visit, 0)
        return result

    def diagnostics(self, window=None):
        result = {"thread_dpi": self.dpi_status, "monitors": self.monitors(),
                  "window_candidates": self.candidate_log, "activation": getattr(self, "last_activation", None)}
        foreground = self.user.GetForegroundWindow()
        if foreground:
            result["foreground"] = {"hwnd": foreground, "title": self.title(foreground),
                                    "pid": self.process_id(foreground), "class": self.class_name(foreground)}
        if window is not None:
            result["target"] = asdict(window)
            result["target_pid"] = self.process_id(window.hwnd)
            result["target_monitor"] = self.monitor_info(self.user.MonitorFromWindow(window.hwnd, 2))
            try:
                function = self.user.GetDpiForWindow
                function.argtypes, function.restype = [W.HWND], W.UINT
                result["window_dpi"] = function(window.hwnd)
            except AttributeError:
                result["window_dpi"] = None
        try:
            result["input_environment"] = self.input_diagnostics(window)
        except Exception as exc:
            result["input_environment"] = {"error": str(exc)}
        return result

    def verify(self, window):
        if self.user.GetForegroundWindow() != window.hwnd:
            raise RuntimeError("游戏已离开前台，操作已暂停")
        current = self.geometry(window.hwnd)
        if (current.left, current.top, current.width, current.height) != (window.left, window.top, window.width, window.height):
            raise RuntimeError("游戏窗口的位置或分辨率发生变化，操作已暂停")

    def _record_call(self, name, parameters, result, code):
        record = error_details(name, parameters, code, result)
        if not hasattr(self, "api_calls"):
            self.api_calls = []
        self.api_calls.append(record)
        self.api_calls[:] = self.api_calls[-64:]
        self.last_api_call = record
        return record

    def send(self, item):
        if item.type == 0:
            parameters = {"type": 0, "dx": int(item.mi.dx), "dy": int(item.mi.dy),
                          "mouseData": int(item.mi.mouseData), "dwFlags": int(item.mi.dwFlags),
                          "time": int(item.mi.time), "dwExtraInfo": int(item.mi.dwExtraInfo)}
        else:
            parameters = {"type": int(item.type), "wVk": int(item.ki.wVk), "wScan": int(item.ki.wScan),
                          "dwFlags": int(item.ki.dwFlags), "time": int(item.ki.time),
                          "dwExtraInfo": int(item.ki.dwExtraInfo)}
        parameters.update(input_count=1, cbSize=C.sizeof(INPUT))
        clear_error()
        accepted = self.user.SendInput(1, C.byref(item), C.sizeof(INPUT))
        code = last_error()  # 先保存，不能被回读光标或日志 API 覆盖。
        record = self._record_call("SendInput", parameters, int(accepted), code)
        if accepted != 1:
            raise WinApiError("Windows未接受模拟输入（UIPI/桌面/权限原因需要结合诊断，错误码不能单独证明）", record)
        return record

    def current_dpi(self):
        fn = self.user.GetThreadDpiAwarenessContext
        fn.argtypes, fn.restype = [], C.c_void_p
        context = fn()
        aware = self.user.GetAwarenessFromDpiAwarenessContext
        aware.argtypes, aware.restype = [C.c_void_p], C.c_int
        equal = self.user.AreDpiAwarenessContextsEqual
        equal.argtypes, equal.restype = [C.c_void_p, C.c_void_p], W.BOOL
        return {"context": int(context or 0), "awareness": aware(context),
                "per_monitor_v2": bool(equal(context, C.c_void_p(-4)))}

    def virtual_desktop(self):
        metrics_values = [self.user.GetSystemMetrics(i) for i in (76, 77, 78, 79)]
        ml, mt, mw, mh = metrics_values
        rows = self.monitors() if hasattr(self.user, "EnumDisplayMonitors") else []
        rows = [r for r in rows if r.get("width", 0) > 0 and r.get("height", 0) > 0]
        if rows:
            left, top = min(r["left"] for r in rows), min(r["top"] for r in rows)
            right = max(r["left"] + r["width"] for r in rows)
            bottom = max(r["top"] + r["height"] for r in rows)
            source = "EnumDisplayMonitors/GetMonitorInfoW"
        else:
            left, top, right, bottom, source = ml, mt, ml + mw, mt + mh, "GetSystemMetrics_fallback"
        if right - left <= 1 or bottom - top <= 1:
            raise RuntimeError("虚拟桌面范围无效，已取消定位")
        return {"left": left, "top": top, "right": right, "bottom": bottom,
                "width": right-left, "height": bottom-top, "source": source,
                "metrics": {"left": ml, "top": mt, "width": mw, "height": mh}, "monitors": rows}

    def absolute_position(self, x, y):
        if not all(isinstance(p, (int, float)) and math.isfinite(p) and int(p) == p for p in (x, y)):
            raise RuntimeError("物理屏幕目标必须是有限整数坐标，已取消定位")
        desktop = self.virtual_desktop()
        self.last_virtual_desktop = desktop
        if not (desktop["left"] <= x < desktop["right"] and desktop["top"] <= y < desktop["bottom"]):
            raise RuntimeError(f"目标({x},{y})超出虚拟桌面范围，已取消定位")
        if desktop["monitors"] and not any(r["left"] <= x < r["left"]+r["width"]
                and r["top"] <= y < r["top"]+r["height"] for r in desktop["monitors"]):
            raise RuntimeError("目标位于多屏布局的空白区域，已取消定位")
        return (round((x-desktop["left"])*65535/(desktop["width"]-1)),
                round((y-desktop["top"])*65535/(desktop["height"]-1)))

    def cursor_position(self):
        point = W.POINT()
        clear_error()
        ok = self.user.GetPhysicalCursorPos(C.byref(point))
        code = last_error()
        record = self._record_call("GetPhysicalCursorPos", {}, int(ok), code)
        if not ok:
            raise WinApiError("无法回读鼠标物理位置，已取消点击", record)
        record["position"] = [int(point.x), int(point.y)]
        return int(point.x), int(point.y)

    def logical_cursor_position(self):
        point = W.POINT()
        clear_error()
        ok = self.user.GetCursorPos(C.byref(point))
        code = last_error()
        if not ok:
            raise WinApiError("无法回读线程坐标系下的鼠标位置", error_details("GetCursorPos", {}, code))
        return int(point.x), int(point.y)

    def clip_rectangle(self):
        rect = W.RECT()
        clear_error()
        ok = self.user.GetClipCursor(C.byref(rect))
        code = last_error()
        if not ok:
            raise WinApiError("无法读取鼠标限制范围", error_details("GetClipCursor", {}, code))
        return {"left": int(rect.left), "top": int(rect.top), "right": int(rect.right), "bottom": int(rect.bottom)}

    def set_physical_position(self, x, y):
        clear_error()
        ok = self.user.SetPhysicalCursorPos(x, y)
        code = last_error()
        record = self._record_call("SetPhysicalCursorPos", {"x": x, "y": y}, int(ok), code)
        if not ok:
            raise WinApiError("Windows未接受物理坐标定位，已取消点击", record)
        return record

    def input_diagnostics(self, window=None, target=None):
        from .native_diagnostics import input_diagnostics
        return input_diagnostics(self, window, target)


class Capture:
    def __init__(self, api, window, config, logger):
        self.api, self.window, self.log = api, window, logger
        self.camera = None
        self.cached, self.cached_at = None, 0.0
        self.sct = None
        backend = config["capture_backend"]
        # auto仅对主显示器使用DXGI，防止把相同分辨率的另一个输出当成游戏。
        primary = (window.left, window.top) == (0, 0)
        if backend == "dxcam" or (backend == "auto" and primary):
            try:
                import dxcam
                self.camera = dxcam.create(device_idx=config["dxcam_device_idx"],
                                           output_idx=config["dxcam_output_idx"], output_color="BGRA")
                if (self.camera.width, self.camera.height) != (window.width, window.height):
                    raise RuntimeError("DXGI输出尺寸与游戏窗口不符")
                self.log.info("截图方式：DXGI / DXcam")
            except Exception as exc:
                if self.camera:
                    self.camera.release()
                    self.camera = None
                if backend == "dxcam":
                    raise RuntimeError(f"DXGI截图初始化失败：{exc}") from exc
                self.log.warning("DXGI未启用，改用MSS：%s", exc)
        if self.camera is None:
            import mss
            self.sct = mss.mss()
            self.log.info("截图方式：MSS")

    def grab(self):
        self.api.verify(self.window)
        if self.camera is not None:
            frame = self.camera.grab(new_frame_only=True)
            if frame is not None:
                self.cached, self.cached_at = frame.copy(), time.monotonic()
            # 仅复用最近350ms的静止画面；输出失效时不持续使用过期截图。
            if self.cached is not None and time.monotonic() - self.cached_at <= .35:
                return self.cached.copy()
            return None
        w = self.window
        return np.array(self.sct.grab({"left": w.left, "top": w.top, "width": w.width, "height": w.height}))

    def close(self):
        if self.camera is not None:
            self.camera.release()
        if self.sct is not None:
            self.sct.close()


class Actor:
    # 扫描码：与键盘布局无关，游戏更容易接收。
    SCAN_CODES = {"esc": 0x01, "w": 0x11, "a": 0x1E, "s": 0x1F,
                  "f": 0x21, "m": 0x32, "lshift": 0x2A}

    def __init__(self, api, window, stop_event):
        self.api, self.window, self.stop = api, window, stop_event
        self.held = set()
        self.keys = set()
        self.lock = threading.RLock()
        self.log = logging.getLogger("yihuan")
        self.last_input = None

    def check(self):
        if self.stop.is_set() or self.api.key_down(0xA2):
            raise Cancelled()
        self.api.verify(self.window)

    def _mouse(self, flags, x=0, y=0):
        item = INPUT(type=0)
        item.mi = MOUSEINPUT(x, y, 0, flags, 0, 0)
        return self.api.send(item)

    def _esc(self, up=False):
        return self._key("esc", up)

    def _key(self, name, up=False):
        if name not in self.SCAN_CODES:
            raise ValueError(f"未定义的按键：{name}")
        item = INPUT(type=1)
        item.ki = KEYBDINPUT(0, self.SCAN_CODES[name], 0x0008 | (0x0002 if up else 0), 0, 0)
        try:
            record = self.api.send(item)
        except BaseException as exc:
            if self.last_input is not None:
                details = getattr(exc, "details", None)
                if details is not None:
                    self.last_input.setdefault("input_calls", []).append(details)
                    self.last_input.setdefault("windows_error", details)
                self.last_input.setdefault("key_errors", []).append(
                    {"key": name, "up": up, "error": str(exc), "windows_error": details})
                self.last_input["stage"] = f"{name}{'松开' if up else '按下'}失败"
            raise
        if self.last_input is not None:
            if record is not None:
                self.last_input.setdefault("input_calls", []).append(record)
            if not up or self.last_input.get("kind") == "release":
                stamp = record.get("monotonic") if isinstance(record, dict) else None
                self.last_input.setdefault("sent_at", time.monotonic() if stamp is None else stamp)
        return record

    def key_down(self, name):
        """发送前保守登记；失败时仍由统一清理尝试释放。"""
        if name not in self.SCAN_CODES:
            raise ValueError(f"未定义的按键：{name}")
        if name in self.keys:
            return
        self.check()
        self.keys.add(name)
        self._key(name, False)

    def key_up(self, name):
        self._key(name, True)
        # 发送成功才删除；失败后release仍能重试。
        self.keys.discard(name)

    def tap_key(self, name, seconds):
        original_error = None
        try:
            self.key_down(name)
            if self.stop.wait(seconds):
                raise Cancelled()
        except BaseException as exc:
            original_error = exc
            raise
        finally:
            if name in self.keys:
                try:
                    self.key_up(name)
                except Exception as exc:
                    if self.last_input is not None:
                        self.last_input["release_error"] = str(exc)
                        self.last_input["release_windows_error"] = getattr(exc, "details", None)
                    if original_error is None:
                        raise

    def _up(self, name):
        if name == "left":
            self._mouse(0x0004)
        else:
            self._esc(True)
        self.held.discard(name)

    def release(self):
        errors = []
        with self.lock:
            for _ in range(2):
                for name in tuple(self.held):
                    try:
                        self._up(name)
                    except Exception as exc:
                        errors.append(str(exc))
                for name in tuple(self.keys):
                    try:
                        self._key(name, True)
                    except Exception as exc:
                        errors.append(str(exc))
                    else:
                        self.keys.discard(name)
        if self.last_input is not None:
            self.last_input["held_keys"] = sorted(self.keys)
            self.last_input["held_buttons"] = sorted(self.held)
            self.last_input["cleanup_complete"] = not (self.held or self.keys)
        if self.held or self.keys:
            raise RuntimeError("模拟按键释放失败：" + "; ".join(errors))

    def _snapshot(self, label, target):
        try:
            fn = getattr(self.api, "input_diagnostics", None)
            context = fn(self.window, target) if fn else {}
        except Exception as exc:
            context = {"diagnostic_error": str(exc)}
        self.last_input[label] = context
        return context

    def _context_check(self, context):
        desktop = context.get("desktop", {})
        if (desktop.get("same_input_desktop") is False
                or desktop.get("thread_desktop", {}).get("receives_input") is False):
            raise RuntimeError("当前工作线程不在Windows输入桌面，已取消定位；请在用户桌面运行单步检查")
        if context.get("uipi_risk") is True:
            raise RuntimeError("助手完整性级别低于游戏（UIPI限制），已取消输入；请关闭助手，并在用户桌面以与游戏相同权限运行")
        if context.get("target_inside_clip") is False:
            raise RuntimeError("目标在ClipCursor鼠标限制范围外，已取消点击；限制范围见输入诊断")

    def _near(self, actual, target):
        return max(abs(actual[0]-target[0]), abs(actual[1]-target[1])) <= 4

    def _samples(self, method, target):
        started = time.monotonic()
        previous = 0
        samples = []
        self.last_input.setdefault("position_samples", []).append({"method": method, "samples": samples})
        for delay in (0.0, .02, .06, .15):
            if delay and self.stop.wait(delay-previous):
                raise Cancelled()
            previous = delay
            self.check()
            actual = self.api.cursor_position()
            samples.append({"elapsed_ms": round((time.monotonic()-started)*1000, 3),
                            "physical": list(actual), "at_target": self._near(actual, target)})
            if delay == 0 and not self._near(actual, target):
                self.log.warning("鼠标即时定位偏差：目标=%s，实际=%s，方式=%s", target, actual, method)
        if any(row["at_target"] for row in samples) and not samples[-1]["at_target"]:
            self.last_input["cursor_returned_after_arrival"] = True
            self.log.warning("光标曾到达目标后又离开；请对照游戏重置、鼠标限制或用户移动的诊断")
        return actual

    def position(self, gx, gy, reference=None, action="单步定位"):
        """有限定位与物理回读；绝不按下鼠标。"""
        with self.lock:
            self.check()
            self.last_input = {"action": action, "stage": "准备定位", "screen_target": [gx, gy],
                               "reference": list(reference) if reference is not None else None,
                               "input_calls": [], "mouse_down_sent": False}
            target = (gx, gy)
            try:
                w = self.window
                if not (w.left <= gx < w.left+w.width and w.top <= gy < w.top+w.height):
                    raise RuntimeError("屏幕目标超出游戏客户区，已取消点击")
                x, y = self.api.absolute_position(gx, gy)
                self.last_input["absolute_target"] = [x, y]
                self.last_input["virtual_desktop"] = getattr(self.api, "last_virtual_desktop", None)
                context = self._snapshot("environment_before", target)
                self._context_check(context)
                self.last_input["cursor_before"] = list(self.api.cursor_position())
                self.last_input.update(move_method="SendInput", stage="发送SendInput定位")
                self.check()
                result = self._mouse(0x8000 | 0x4000 | 0x0001, x, y)
                if result is not None: self.last_input["input_calls"].append(result)
                self.last_input["stage"] = "已发送移动，等待位置回读"
                actual = self._samples("SendInput", target)
                self.last_input["cursor_after_move"] = list(actual)
                if not self._near(actual, target):
                    self.log.warning("鼠标定位偏差：目标=%s，实际=%s；尝试物理坐标定位一次", target, actual)
                    self._context_check(self._snapshot("environment_before_correction", target))
                    self.check()
                    self.last_input.update(move_method="SetPhysicalCursorPos", stage="调用SetPhysicalCursorPos纠正")
                    result = self.api.set_physical_position(gx, gy)
                    if result is not None: self.last_input["input_calls"].append(result)
                    actual = self._samples("SetPhysicalCursorPos", target)
                    self.last_input["cursor_after_correction"] = list(actual)
                self._context_check(self._snapshot("environment_after", target))
                self.check()
                actual = self.api.cursor_position()
                self.last_input["cursor_final"] = list(actual)
                if not self._near(actual, target):
                    self.last_input["stage"] = "鼠标位置不符，未按左键"
                    raise RuntimeError(f"鼠标未到按钮：目标({gx},{gy})，实际{actual}；已取消点击")
                self.last_input["stage"] = "位置已核对，尚未按左键"
                self.log.info("鼠标位置核对：目标=%s，实际=%s，方式=%s", target, actual, self.last_input["move_method"])
                return actual
            except Cancelled:
                self.last_input["stage"] = "收到停止，未按左键"
                raise
            except Exception as exc:
                self.last_input["error"] = str(exc)
                self.last_input["windows_error"] = getattr(exc, "details", None)
                if isinstance(exc, WinApiError):
                    self.last_input["stage"] = "定位API失败，未按左键：" + exc.details["api"]
                    self.last_input["input_calls"].append(exc.details)
                self._snapshot("environment_failure", target)
                self.log.error("定位失败，未按左键：%s；详情=%s", exc, self.last_input)
                raise

    def _press(self, name, before_press=None, target=None):
        if before_press is not None and not before_press():
            self.last_input["stage"] = "最后截图校验变化，取消按下"
            raise ActionChanged("定位期间画面或目标发生变化")
        self.check()
        if target is not None:
            actual = self.api.cursor_position()
            self.last_input["cursor_before_down"] = list(actual)
            if not self._near(actual, target):
                self.last_input["stage"] = "按下前光标已离开目标，未按左键"
                raise RuntimeError(f"按下前鼠标位置改变：目标{target}，实际{actual}；已取消点击")
            fn = getattr(self.api, "clip_rectangle", None)
            if fn:
                clip = fn()
                self.last_input["clip_before_down"] = clip
                if not (clip["left"] <= target[0] < clip["right"] and clip["top"] <= target[1] < clip["bottom"]):
                    raise RuntimeError("按下前鼠标限制发生变化，已取消点击")
        # 上述截图、回读可能耗时；紧接按下前再次检查停止/前台。
        self.check()
        self.held.add(name)
        original_error = None
        try:
            result = self._mouse(0x0002) if name == "left" else self._esc()
            if name == "left" and result is not None:
                self.last_input.setdefault("input_calls", []).append(result)
            self.last_input["mouse_down_sent"] = name == "left"
            self.last_input["stage"] = "左键已按下" if name == "left" else "Esc已按下"
            self.stop.wait(.10 if name == "left" else .04)
        except BaseException as exc:
            original_error = exc
            self.last_input["windows_error"] = getattr(exc, "details", None)
            raise
        finally:
            try:
                self._up(name)
            except Exception as exc:
                self.last_input["release_error"] = str(exc)
                if original_error is None:
                    raise
        self.last_input["stage"] = "左键已松开；页面响应待确认" if name == "left" else "Esc已松开；页面响应待确认"

    def click_at(self, gx, gy, before_press=None, reference=None, action="单步点击"):
        with self.lock:
            self.position(gx, gy, reference, action)
            self._press("left", before_press, (gx, gy))
            return self.last_input

    def _perform_key(self, intent, before_press=None):
        self.last_input = {"action": intent.action.value, "stage": "准备按键：" + intent.kind,
                           "kind": intent.kind, "key": intent.key,
                           "key2": getattr(intent, "key2", None), "input_calls": [],
                           "held_keys": sorted(self.keys)}
        try:
            if intent.kind != "release" and before_press is not None and not before_press():
                raise ActionChanged("按键前画面发生变化")
            self.check()
            if intent.kind == "press":
                self.key_down(intent.key)
                state = "已按住"
            elif intent.kind == "release":
                self.key_up(intent.key)
                state = "已松开"
            elif intent.kind == "swap":
                self.key_up(intent.key)
                self.key_down(intent.key2)
                state = "已交换方向键"
            elif intent.kind == "tap":
                self.tap_key(intent.key, intent.seconds or .06)
                state = "已轻点并松开"
            else:
                raise ValueError(f"未定义的按键动作：{intent.kind}")
            self.last_input["stage"] = state
        except BaseException as exc:
            self.last_input["error"] = str(exc)
            if isinstance(exc, (Cancelled, ActionChanged)):
                self.last_input["stage"] = "停止或画面变化，取消按键"
            else:
                self.last_input["stage"] = "按键失败：" + intent.kind
            if getattr(exc, "details", None) is not None:
                self.last_input.setdefault("windows_error", exc.details)
            raise
        finally:
            self.last_input["held_keys"] = sorted(self.keys)

    def perform(self, intent, targets, before_press=None):
        with self.lock:
            if getattr(intent, "kind", None) in ("press", "tap", "swap", "release"):
                self._perform_key(intent, before_press)
                return
            self.check()
            if intent.target:
                px, py = targets[intent.target]
                if not (0 <= px < 1499 and 0 <= py < 843):
                    raise RuntimeError("识别按钮坐标超出游戏区域，已取消点击")
                gx = self.window.left + round(px*self.window.width/1499)
                gy = self.window.top + round(py*self.window.height/843)
                # 浮点中心靠近最右/下边缘时，round可能产生客户区外坐标。
                if not (self.window.left <= gx < self.window.left+self.window.width
                        and self.window.top <= gy < self.window.top+self.window.height):
                    raise RuntimeError("识别坐标取整后超出游戏区域，已取消点击")
                self.click_at(gx, gy, before_press, (px, py), intent.action.value)
            else:
                self.last_input = {"action": intent.action.value, "stage": "准备输入", "input_calls": []}
                self._press("esc", before_press)
