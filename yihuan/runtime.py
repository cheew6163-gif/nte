from pathlib import Path
from collections import deque
import copy
import json
import logging
import threading
import time
from .core import BATTLE_CONFIRM_AFTER, Observation, Scene, Timings, is_unknown
from .tasks import TASKS
from .vision import StableScene, guarded_action, save_image
from .windows import Actor, Capture, Cancelled, WinApi
from .settings import RESOURCE_DIR, DATA_DIR, load_config
from .nte999 import Nte999Loop, load_flow_999
from .privacy import redact_report

class Runner:
    def __init__(self, events):
        self.events = events
        self.log = logging.getLogger("yihuan")
        self.stop_event = threading.Event()
        self.worker = None
        self.lock = threading.Lock()
        self.unreleased_actor = None

    @property
    def busy(self):
        return bool(self.worker and self.worker.is_alive())

    def start(self, task_id):
        with self.lock:
            if self.busy:
                return False
            if self.unreleased_actor:
                try:
                    self.unreleased_actor.release()
                    self.unreleased_actor = None
                except Exception:
                    self.log.exception("上次模拟按键尚未成功释放，拒绝重新启动")
                    self.events.put(("done", "按键仍未释放，暂不能启动；请查看日志"))
                    return False
            self.stop_event = threading.Event()
            self.worker = threading.Thread(target=self._run, args=(task_id,), name="vision-task", daemon=True)
            self.worker.start()
            return True

    def stop(self):
        self.stop_event.set()

    def _run(self, task_id):
        capture, actor, last_frame, last_observation = None, None, None, None
        last_action_frame = None
        recent_actions = deque(maxlen=12)
        api, window = None, None
        startup_path = DATA_DIR / "diagnostics" / ("startup_" + time.strftime("%Y%m%d_%H%M%S") + ".json")
        def save_startup(error=None):
            report = {}
            if error:
                report["error"] = str(error)
                report["windows_error"] = getattr(error, "details", None)
            try:
                if api:
                    report.update(api.diagnostics(window))
            except Exception as exc:
                report["diagnostic_error"] = str(exc)
            try:
                startup_path.parent.mkdir(parents=True, exist_ok=True)
                startup_path.write_text(json.dumps(redact_report(report), ensure_ascii=False, indent=2), encoding="utf-8")
                self.log.info("窗口/显示器启动诊断：%s", startup_path)
            except Exception:
                self.log.exception("启动诊断保存失败")
        result, task = "已停止", None
        try:
            config = load_config()
            if task_id == "nte999":
                self.log.info("999夜固定流程：首次F → 刷新 → Esc → W/A/S → 松S → 80秒Shift战斗 → "
                              "M → 残丝文字基准定位 → 固定传送 → 等待5秒 → F；仅地图阶段截图")
                self.log.info("999夜参数：W %.1f秒、A %.1f秒、S %.1f秒后松开、战斗%.1f秒、"
                              "Shift每%.1f秒按住%.2f秒共40次；配置目录=%s",
                              config["timings"]["w_hold"], config["timings"]["a_hold"],
                              config["timings"]["s_hold"], config["timings"]["battle_hold"],
                              config["timings"]["shift_interval"], config["timings"]["shift_hold"], DATA_DIR)
            else:
                self.log.info("生效计时：匹配后至少%.1f秒确认战场，确认后观察%.1f秒；"
                              "配置目录=%s；代码=%s", BATTLE_CONFIRM_AFTER,
                              config["timings"]["battle_watch"], DATA_DIR, Path(__file__).with_name("core.py"))
            self.log.info("启动%s；G5启动 / 左Ctrl停止", TASKS[task_id].name)
            api = WinApi()
            self.log.info("工作线程DPI：%s", api.dpi_status)
            window = api.find_game(config)
            self.log.info("选中游戏：标题=%s hwnd=%s pid=%s 客户区=%d×%d 原点=(%d,%d)",
                          window.title, window.hwnd, api.process_id(window.hwnd),
                          window.width, window.height, window.left, window.top)
            window = api.activate(window, self.stop_event)
            save_startup()
            if (window.width, window.height) != (config["screen_width"], config["screen_height"]):
                raise RuntimeError(f"实际游戏客户区为{window.width}×{window.height}，需要3840×2160；"
                                   "已记录窗口标题、显示器与DPI，请查看startup诊断")
            self.log.info("游戏窗口：%s，截图区域%d×%d", window.title, window.width, window.height)
            if self.stop_event.wait(config["start_delay"]):
                raise Cancelled()
            capture = Capture(api, window, config, self.log)
            actor = Actor(api, window, self.stop_event)
            detector = TASKS[task_id].detector_factory(RESOURCE_DIR)
            if task_id == "nte999":
                flow = load_flow_999(RESOURCE_DIR / "assets999",
                                     Timings(**config["timings"]), config["max_loops"])
                self.log.info("999夜固定点：刷新(%.1f,%.1f)、传送(%.1f,%.1f)；等待 菜单%.1f/"
                              "Esc后%.1f/地图%.1f/面板%.1f/传送后%.1f；F重复%d次每%.1f秒；"
                              "max_loops=%d",
                              flow.refresh_point[0], flow.refresh_point[1],
                              flow.teleport_point[0], flow.teleport_point[1],
                              flow.menu_wait, flow.exit_wait, flow.map_wait,
                              flow.panel_wait, flow.teleport_wait,
                              flow.f_repeat_count, flow.f_repeat_interval, flow.max_loops)
                task = Nte999Loop(
                    flow, actor, capture, detector.locator, self.stop_event, self.log,
                    status=lambda text: self.events.put(
                        ("status", f"{TASKS[task_id].name} · {text}")),
                    debug_dir=DATA_DIR / "diagnostics")
                task.run()
                result = f"已完成{task.completed}轮"
                last_frame, last_observation = task.last_frame, task.last_observation
            else:
                stable = StableScene(config["stable_frames"])
                task = TASKS[task_id].task_factory(Timings(**config["timings"]),
                                                   config["max_loops"], time.monotonic(), self.log)
            def record_action(intent):
                recent_actions.append({"action": intent.action.value, "phase": intent.phase.value,
                                       "scene": intent.expected.value,
                                       "input": copy.deepcopy(getattr(actor, "last_input", None))})

            def commit_action(intent, sent_at):
                task.commit(intent, time.monotonic())

            previous_scene, previous_phase = None, None
            last_debug = 0.0
            # 999夜已在上面的固定流程中执行；下面的逐帧循环只用于咔呲。
            while (task_id != "nte999" and not self.stop_event.is_set()
                   and not task.done):
                task.check_timeout(time.monotonic())
                if getattr(task, "blind", False):
                    # 不截图；每50ms仍检查停止、游戏前台与窗口，并真实执行输入。
                    actor.check()
                    performed = False
                    for intent in task.tick(time.monotonic()) or ():
                        actor.check()
                        sent_at = time.monotonic()
                        actor.perform(intent, {})
                        record_action(intent)
                        self.log.info("输入已发送：%s；战斗期间不截图", intent.action.value)
                        commit_action(intent, sent_at)
                        performed = True
                    if task.phase != previous_phase or performed:
                        self.events.put(("status",
                                         f"{TASKS[task_id].name} · 第{task.completed + 1}轮 · "
                                         f"{task.phase_text}"))
                        previous_phase = task.phase
                    if not getattr(task, "blind", False):
                        stable = StableScene(config["stable_frames"])
                        previous_scene = None
                    self.stop_event.wait(.05)
                    continue
                frame = capture.grab()
                if self.stop_event.is_set():
                    break
                if frame is None:
                    observation = Observation(Scene.UNKNOWN)
                else:
                    last_frame = frame
                    observation = detector.detect(frame)
                last_observation = observation
                now = time.monotonic()
                ready = stable.push(observation)
                if ready and observation.scene != previous_scene:
                    self.log.info("识别到：%s", observation.scene.value)
                    previous_scene = observation.scene
                if is_unknown(observation.scene) and now - last_debug >= 5:
                    self.log.info("等待可识别界面，匹配得分：%s", json.dumps(observation.scores, ensure_ascii=False))
                    last_debug = now
                if is_unknown(observation.scene) or ready:
                    intent = task.observe(observation, now)
                else:
                    intent = None
                if intent:
                    # 校验时没有新截图，视为识别不明，绝不使用很久以前的图执行操作。
                    def fresh_capture():
                        nonlocal last_observation
                        value = capture.grab()
                        if value is None:
                            last_observation = Observation(Scene.UNKNOWN)
                        return value
                    def remember_validation(value, validated):
                        nonlocal last_action_frame, last_frame, last_observation
                        last_action_frame = value.copy()
                        last_frame, last_observation = last_action_frame, validated
                    sent_at = time.monotonic()
                    if guarded_action(intent, fresh_capture, detector, actor, self.stop_event,
                                      on_validation=remember_validation):
                        record_action(intent)
                        self.log.info("输入已发送：%s；界面变化由后续截图确认", intent.action.value)
                        commit_action(intent, sent_at)
                        if not getattr(intent, "keep_stable", False):
                            stable = StableScene(config["stable_frames"])
                    else:
                        self.log.info("取消%s：停止=%s，最新校验画面=%s，得分=%s，标记=%s；重新识别",
                                      intent.action.value, self.stop_event.is_set(),
                                      last_observation.scene.value,
                                      json.dumps(last_observation.scores, ensure_ascii=False),
                                      json.dumps(last_observation.flags, ensure_ascii=False))
                        stable = StableScene(config["stable_frames"])
                if task.phase != previous_phase:
                    status = (f"已完成{task.completed}轮" if task.done
                              else f"{TASKS[task_id].name} · 第{task.completed + 1}轮 · {task.phase_text}")
                    self.events.put(("status", status))
                    previous_phase = task.phase
                # 松S后立即进入战斗分支，首个Shift不额外等待识图轮询间隔。
                if not getattr(task, "blind", False):
                    self.stop_event.wait(config["poll_interval"])
            if task_id != "nte999" and task.done:
                result = f"已完成{task.completed}轮"
        except Cancelled:
            result = "已停止"
        except Exception as exc:
            result = "已暂停：" + str(exc)
            self.log.exception("任务暂停：%s", exc)
            save_startup(exc)
            if task_id == "nte999" and task is not None:
                last_frame = getattr(task, "last_frame", last_frame)
                last_observation = getattr(task, "last_observation", last_observation)
                recent_actions.clear()
                recent_actions.extend(getattr(task, "recent_actions", ()))
            if last_frame is not None:
                stamp = time.strftime("%Y%m%d_%H%M%S")
                base = DATA_DIR / "diagnostics" / stamp
                try:
                    save_image(base.with_suffix(".png"), last_frame, redact=True,
                               scene=last_observation.scene if last_observation else None)
                    report = {"error": str(exc), "phase": task.phase_text if task else "启动",
                              "windows_error": getattr(exc, "details", None),
                              "scene": last_observation.scene.value if last_observation else "无截图",
                              "scores": last_observation.scores if last_observation else {},
                              "targets": last_observation.targets if last_observation else {},
                              "flags": last_observation.flags if last_observation else {},
                              "last_input": getattr(actor, "last_input", None),
                              "recent_actions": list(recent_actions)}
                    if last_action_frame is not None:
                        before_path = base.with_name(base.name + "_before_input.png")
                        save_image(before_path, last_action_frame, redact=True)
                        report["before_input_image"] = before_path.name
                    base.with_suffix(".json").write_text(json.dumps(redact_report(report), ensure_ascii=False, indent=2), encoding="utf-8")
                    self.log.info("已保存最近一次游戏画面：%s", base.with_suffix(".png"))
                except Exception:
                    self.log.exception("诊断截图保存失败")
        finally:
            self.stop_event.set()
            if actor:
                try:
                    actor.release()
                except Exception:
                    self.log.exception("释放模拟按键失败")
                    self.unreleased_actor = actor
                    result = "按键释放失败，请查看日志"
            if capture:
                try:
                    capture.close()
                except Exception:
                    self.log.exception("截图资源关闭失败")
            self.log.info("%s", result)
            self.events.put(("done", result))


class Hotkeys(threading.Thread):
    def __init__(self, runner, events, shutdown, title_regex):
        super().__init__(name="global-hotkeys", daemon=True)
        self.runner, self.events, self.shutdown = runner, events, shutdown
        self.pattern = title_regex

    def run(self):
        try:
            api = WinApi()
            was_start, was_ctrl = True, False  # 启动时按着G5，不当作新的启动动作。
            last_stop = 0.0
            while not self.shutdown.is_set():
                ctrl = api.key_down(0xA2)
                start = api.key_down(0x06) or api.key_down(0x77)  # G5/XBUTTON2；F8可作G HUB映射替代。
                if ctrl and self.runner.busy:
                    self.runner.stop()
                    last_stop = time.monotonic()
                    if not was_ctrl:
                        self.events.put(("log", "左Ctrl：停止请求已收到"))
                if (start and not was_start and not ctrl and not self.runner.busy
                        and time.monotonic() - last_stop > 1.0 and api.foreground_matches(self.pattern)):
                    self.events.put(("start_request", None))
                was_start, was_ctrl = start, ctrl
                self.shutdown.wait(.01)
        except Exception:
            logging.getLogger("yihuan").exception("全局按键监听失败")
            self.events.put(("log", "全局按键监听异常，请使用界面停止按钮并查看日志"))
