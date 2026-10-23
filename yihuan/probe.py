"""有界的原生输入检查；不会启动任务状态机或无限循环。"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
import ctypes as C
from ctypes import wintypes as W
import sys
import platform
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path
import queue
import threading
import time

from .core import Scene
from .settings import DATA_DIR, RESOURCE_DIR, load_config
from .vision import Detector, StableScene, save_image
from .windows import Actor, Cancelled, Capture, WinApi, single_instance
from .privacy import redact_report
from .nte999 import Detector999, Scene999


@dataclass(frozen=True)
class ProbeOptions:
    mode: str
    click: bool = False
    target: str = "go"
    timeout: float = 15.0
    start_delay: float = 2.0
    output_dir: Path | None = None


class ProbeReport:
    def __init__(self, options):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        self.directory = Path(options.output_dir or DATA_DIR / "diagnostics") / ("input_check_" + stamp)
        self.directory.mkdir(parents=True, exist_ok=True)
        packages = {}
        for name in ("numpy", "opencv-python", "mss", "Pillow", "dxcam"):
            try:
                packages[name] = version(name)
            except PackageNotFoundError:
                packages[name] = None
        self.data = {"runtime": {"python": sys.version, "executable": sys.executable,
                                  "windows": platform.platform(), "packages": packages}, "started_utc": datetime.now(timezone.utc).isoformat(),
                     "mode": options.mode, "click_requested": options.click,
                     "target": options.target, "status": "running", "screenshots": {},
                     "errors": [], "click_sent": False, "click_response_confirmed": False}

    def image(self, name, frame):
        if frame is not None:
            path = self.directory / (name + ".png")
            save_image(path, frame, redact=True)
            self.data["screenshots"][name] = path.name

    def finish(self):
        self.data["finished_utc"] = datetime.now(timezone.utc).isoformat()
        path = self.directory / "report.json"
        path.write_text(json.dumps(redact_report(self.data), ensure_ascii=False, indent=2), encoding="utf-8")
        return path


def _check_stop(api, stop):
    if stop.is_set() or api.key_down(0xA2):
        stop.set()
        raise Cancelled()


def _wait(api, stop, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        _check_stop(api, stop)
        stop.wait(min(.02, max(0, deadline - time.monotonic())))
    _check_stop(api, stop)


def _snapshot(api, window=None, target=None):
    return api.input_diagnostics(window=window, target=target)


def _record_error(report, stage, exc):
    report.data["errors"].append({"stage": stage, "error": str(exc),
                                  "details": getattr(exc, "details", None)})


def _final_snapshot(api, report, window=None, target=None):
    try:
        report.data["after_input"] = _snapshot(api, window, target)
    except Exception as exc:
        _record_error(report, "after_input_diagnostics", exc)


def _require_input_desktop(snapshot):
    """不能在隔离桌面试输入；未知状态也应先检查诊断。"""
    desktop = snapshot.get("desktop", {})
    if (desktop.get("same_input_desktop") is not True
            or desktop.get("thread_desktop", {}).get("receives_input") is not True):
        raise RuntimeError("当前线程未确认位于用户输入桌面，已禁止移动/点击；请查看环境报告并在交互桌面运行input_check.bat")


def _desktop_frame(window):
    if window is None:
        raise ValueError("截图必须限定到目标窗口，不保存完整桌面")
    import mss
    import numpy as np
    with mss.mss() as screen:
        area = {"left": window.left, "top": window.top, "width": window.width,
                "height": window.height}
        return np.array(screen.grab(area))


def _image_safely(report, name, capture):
    try:
        report.image(name, capture())
    except Exception as exc:
        _record_error(report, "screenshot:" + name, exc)


def _observation(observation):
    return {"scene": observation.scene.value, "scores": observation.scores,
            "targets": observation.targets, "flags": observation.flags}


def _screen_target(window, target, reference_size):
    px, py = target
    width, height = reference_size
    if not (0 <= px < width and 0 <= py < height):
        raise RuntimeError("识别目标超出参考画幅")
    gx = window.left + round(px * window.width / width)
    gy = window.top + round(py * window.height / height)
    if not (window.left <= gx < window.left + window.width
            and window.top <= gy < window.top + window.height):
        raise RuntimeError("取整后的目标超出游戏客户区")
    return gx, gy


def probe_game(api, options, report, stop, *, config=None, detector=None,
               capture_factory=Capture, actor_factory=Actor):
    """最多执行一次所选按钮；篝火单步只核验目标定位后位置未变化。"""
    config = config if config is not None else load_config()
    bonfire_check = options.target == "bonfire"
    if detector is None:
        detector = Detector999(RESOURCE_DIR / "assets999") if bonfire_check else Detector(RESOURCE_DIR / "assets")
    capture = actor = None
    window = None
    last_frame = None
    expected = Scene999.MAP if bonfire_check else (Scene.ACTIVITY if options.target == "go" else Scene.MATCH)
    deadline = time.monotonic() + options.timeout
    try:
        _check_stop(api, stop)
        report.data["environment"] = _snapshot(api)
        _require_input_desktop(report.data["environment"])
        window = api.find_game(config)
        report.data["selected_window"] = asdict(window)
        report.data["before_activation"] = _snapshot(api, window)
        window = api.activate(window, stop)
        if (window.width, window.height) != (config["screen_width"], config["screen_height"]):
            raise RuntimeError(f"游戏客户区为{window.width}×{window.height}，需要3840×2160")
        _wait(api, stop, options.start_delay)
        capture = capture_factory(api, window, config, logging.getLogger("yihuan"))
        actor = actor_factory(api, window, stop)
        stable = StableScene(config["stable_frames"])
        selected = None
        while time.monotonic() < deadline:
            _check_stop(api, stop)
            frame = capture.grab()
            if frame is not None:
                last_frame = frame
                found = detector.detect(frame)
                report.data["last_observation"] = _observation(found)
                if stable.push(found) and found.scene == expected and options.target in found.targets:
                    selected = found
                    break
            stop.wait(.1)
        if selected is None:
            raise RuntimeError(f"限时内未稳定识别{expected.value}，未发送鼠标输入")
        report.image("before", last_frame)
        if bonfire_check:
            report.data["initial_target_present"] = True
        reference = selected.targets[options.target]
        target = _screen_target(window, reference, detector.size)
        report.data["screen_target"] = list(target)
        report.data["before_input"] = _snapshot(api, window, target)
        _require_input_desktop(report.data["before_input"])

        def before_press():
            nonlocal last_frame
            _check_stop(api, stop)
            frame = capture.grab()
            if frame is None:
                raise RuntimeError("按下前无可用新画面，取消单步点击")
            last_frame = frame
            fresh = detector.detect(frame)
            report.data["before_press_observation"] = _observation(fresh)
            report.image("before_press", frame)
            if fresh.scene != expected:
                raise RuntimeError(f"按下前页面变为{fresh.scene.value}，取消单步点击")
            if options.target not in fresh.targets:
                raise RuntimeError("按下前目标已消失，取消单步点击")
            fresh_target = _screen_target(window, fresh.targets[options.target], detector.size)
            if max(abs(a - b) for a, b in zip(target, fresh_target)) > 4:
                raise RuntimeError("按下前按钮位置已变化，取消单步点击")
            _check_stop(api, stop)
            return True

        if not options.click:
            actor.position(*target, reference=reference, action="单步定位：" + options.target)
            report.data["status"] = "game_move_verified"
            report.data["click_sent"] = False
            return
        actor.click_at(*target, before_press=before_press, reference=reference,
                       action="单步点击：" + options.target)
        report.data["click_sent"] = True
        response_stable = StableScene(config["stable_frames"])
        report.data["response_observations"] = []
        while time.monotonic() < deadline:
            _check_stop(api, stop)
            frame = capture.grab()
            if frame is not None:
                last_frame = frame
                found = detector.detect(frame)
                report.data["response_observations"].append(_observation(found))
                if len(report.data["response_observations"]) > 30:
                    report.data["response_observations"].pop(0)
                ready = response_stable.push(found)
                if bonfire_check:
                    point = found.targets.get("bonfire")
                    accepted = found.scene == Scene999.MAP and point is not None
                    if accepted:
                        latest = _screen_target(window, point, detector.size)
                        accepted = max(abs(a-b) for a,b in zip(target,latest)) <= 4
                else:
                    accepted = (found.scene == Scene.MATCH if options.target == "go" else
                                found.scene in (Scene.BATTLE, Scene.VICTORY, Scene.DEFEAT))
                if ready and accepted:
                    report.data["click_response_confirmed"] = True
                    report.data["status"] = ("bonfire_point_verified" if bonfire_check else
                                             "go_response_match_verified" if options.target == "go"
                                             else "match_response_verified")
                    report.data["response_scene"] = found.scene.value
                    return
            stop.wait(.1)
        report.data["status"] = "click_sent_response_unconfirmed"
        report.data["errors"].append({"stage": "response", "error": "限时内未识别到预期响应；不补发、不执行下一按钮"})
    finally:
        if actor:
            report.data["last_input"] = actor.last_input
            report.data["click_sent"] = bool(report.data.get("click_sent") or
                                             (actor.last_input or {}).get("mouse_down_sent"))
            try:
                actor.release()
                report.data["held_after_cleanup"] = sorted(actor.held)
            except Exception as exc:
                _record_error(report, "release", exc)
                report.data["status"] = "release_failed"
        if capture:
            _image_safely(report, "after", capture.grab)
            if last_frame is not None and "after" not in report.data["screenshots"]:
                _image_safely(report, "last_valid", lambda: last_frame)
            try:
                capture.close()
            except Exception as exc:
                _record_error(report, "capture_close", exc)
                report.data["status"] = "cleanup_failed"
        _final_snapshot(api, report, window, report.data.get("screen_target"))


def desktop_action(api, window, target, options, report, stop, click_event,
                   *, actor_factory=Actor):
    """Tk主线程接收事件，工作线程执行一次原生移动/点击。"""
    actor = actor_factory(api, window, stop)
    try:
        report.data["before_input"] = _snapshot(api, window, target)
        _require_input_desktop(report.data["before_input"])
        _image_safely(report, "before", lambda: _desktop_frame(window))
        if options.click:
            actor.click_at(*target, action="桌面单步点击")
            report.data["click_sent"] = True
            deadline = time.monotonic() + min(options.timeout, 3.0)
            while time.monotonic() < deadline and not click_event.is_set():
                _check_stop(api, stop)
                stop.wait(.01)
            if not click_event.is_set():
                raise RuntimeError("光标已核对且左键已发送，但靶窗未收到点击事件")
            report.data["status"] = "desktop_click_received"
            report.data["click_response_confirmed"] = True
        else:
            actor.position(*target, action="桌面单步定位")
            report.data["click_sent"] = False
            report.data["status"] = "desktop_move_verified"
        _wait(api, stop, .1)
    finally:
        report.data["last_input"] = actor.last_input
        report.data["click_sent"] = bool(report.data.get("click_sent") or
                                         (actor.last_input or {}).get("mouse_down_sent"))
        try:
            actor.release()
        except Exception as exc:
            _record_error(report, "release", exc)
            report.data["status"] = "release_failed"
        report.data["held_after_cleanup"] = sorted(actor.held)
        _image_safely(report, "after", lambda: _desktop_frame(window))
        _final_snapshot(api, report, window, target)


def probe_desktop(options, report):
    import ctypes as C
    from ctypes import wintypes as W
    import tkinter as tk
    api = WinApi()
    report.data["environment"] = _snapshot(api)
    _require_input_desktop(report.data["environment"])
    root = tk.Tk()
    root.title("异环输入检查 · 独立桌面靶窗")
    root.geometry("760x460+120+120")
    root.attributes("-topmost", True)
    canvas = tk.Canvas(root, background="#152239", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    label = canvas.create_text(380, 55, text="原生鼠标单步检查 · 左 Ctrl 停止", fill="white", font=("Microsoft YaHei UI", 18))
    canvas.create_text(380, 95, text="只向此窗口发送一次输入；结束后自动关闭。", fill="white")
    canvas.create_oval(330, 220, 430, 320, fill="#58be8d", outline="white", width=3)
    canvas.create_text(380, 270, text="点击靶心", fill="#122333")
    stop, clicked, completed = threading.Event(), threading.Event(), queue.Queue()
    events = []
    target = [None, None]

    def on_click(event):
        row = {"x_root": event.x_root, "y_root": event.y_root,
               "x": event.x, "y": event.y, "event_time": event.time}
        events.append(row)
        if target[0] is not None and max(abs(event.x_root - target[0]), abs(event.y_root - target[1])) <= 4:
            clicked.set()
            canvas.itemconfigure(label, text="已收到实际左键事件")

    canvas.bind("<ButtonPress-1>", on_click)
    root.protocol("WM_DELETE_WINDOW", stop.set)
    root.update_idletasks()
    root.deiconify()
    root.lift()
    root.focus_force()
    root.update()
    get_root = api.user.GetAncestor
    get_root.argtypes, get_root.restype = [W.HWND, W.UINT], W.HWND
    hwnd = get_root(root.winfo_id(), 2) or root.winfo_id()
    target[:] = [canvas.winfo_rootx() + 380, canvas.winfo_rooty() + 270]
    report.data["screen_target"] = target[:]
    report.data["desktop_events"] = events

    def worker():
        try:
            worker_api = WinApi()
            _wait(worker_api, stop, options.start_delay)
            window = worker_api.geometry(hwnd)
            desktop_action(worker_api, window, tuple(target), options, report, stop, clicked)
        except Cancelled:
            report.data["status"] = "cancelled"
        except Exception as exc:
            report.data["status"] = "failed"
            _record_error(report, "desktop", exc)
        finally:
            completed.put(True)

    thread = threading.Thread(target=worker, name="single-input-check", daemon=True)
    overall_deadline = time.monotonic() + options.timeout + options.start_delay + 4
    def poll():
        if not completed.empty():
            root.destroy()
            return
        if time.monotonic() >= overall_deadline:
            stop.set()
        root.after(20, poll)
    thread.start()
    root.after(20, poll)
    root.mainloop()
    thread.join(timeout=1)


def run_input_check(options):
    if options.mode not in ("inspect", "desktop", "game"):
        raise ValueError("未知输入检查模式")
    if options.target not in ("go", "match", "bonfire"):
        raise ValueError("未知游戏检查目标")
    if not 1 <= options.timeout <= 120 or not 0 <= options.start_delay <= 10:
        raise ValueError("timeout应为1–120秒，start-delay应为0–10秒")
    if options.mode == "inspect" and options.click:
        raise ValueError("inspect只读，不支持--click")
    report = ProbeReport(options)
    instance_handle = None
    try:
        api = WinApi()
        report.data["environment"] = _snapshot(api)
        if options.mode != "inspect":
            _require_input_desktop(report.data["environment"])
            instance_handle = single_instance()  # 不与已有助手循环同时操作。
        if options.mode == "desktop":
            probe_desktop(options, report)
        elif options.mode == "inspect":
            # 环境检查只保存结构化报告，不抓取可能含其他应用个人信息的完整桌面。
            report.data["status"] = "inspection_complete"
        else:
            probe_game(api, options, report, threading.Event())
    except Cancelled:
        report.data["status"] = "cancelled"
    except Exception as exc:
        report.data["status"] = "failed"
        _record_error(report, options.mode, exc)
    finally:
        if instance_handle:
            kernel = C.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [W.HANDLE], W.BOOL
            kernel.CloseHandle(instance_handle)
    path = report.finish()
    print(json.dumps(redact_report({"status": report.data["status"], "report": str(path),
                      "click_response_confirmed": report.data["click_response_confirmed"],
                      "errors": report.data["errors"]}), ensure_ascii=False, indent=2))
    successful = {"inspection_complete", "desktop_move_verified", "desktop_click_received",
                  "game_move_verified", "go_response_match_verified", "match_response_verified",
                  "bonfire_point_verified"}
    return 0 if report.data["status"] in successful else 1
