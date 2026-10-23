"""999夜刷纽扣：固定时间驱动刷新/走位/战斗，仅战斗后地图需要识别。

启动时人物已在目的地篝火旁。首次直接按 F，之后按固定等待点击“刷新怪物”、
Esc、W/A/S 走位，S 实际松开后开始 80 秒 Shift 战斗。战斗结束才按 M 开地图，
以“残丝长巷”文字为基准定位用户确认的篝火，点击固定“传送”位置，等待 5 秒后
按 F 进入下一轮。除地图阶段外不截图、不调用场景识别器。
"""
from collections import deque
from dataclasses import dataclass
from enum import Enum
import copy
import json
import logging
import math
from pathlib import Path
import time

import cv2
import numpy as np

from .core import Observation, TaskTimeout, Timings
from .map_label import MapLabelLocator
from .vision import save_image


BASE_W, BASE_H = 1499, 843
SHIFT_TOTAL = 40


class Scene999(str, Enum):
    UNKNOWN = "未识别"
    MAP = "地图"
    CAMP = "篝火菜单"
    FIELD = "人物界面"


class Action999(str, Enum):
    PRESS_F = "按F打开篝火菜单"
    CLICK_REFRESH = "点击刷新怪物"
    PRESS_ESC = "按Esc返回人物界面"
    PRESS_W = "按住W前进"
    SWAP_WA = "松开W并按住A"
    SWAP_AS = "松开A并按住S走位"
    RELEASE_S = "走位结束，松开S"
    TAP_SHIFT = "轻点左Shift"
    PRESS_M = "战斗后按M开地图"
    CLICK_BONFIRE = "点击目标篝火"
    CLICK_TELEPORT = "点击传送"


@dataclass(frozen=True)
class Intent999:
    action: Action999
    expected: Scene999
    phase: str
    kind: str = "click"          # click / press / release / tap / swap
    target: str | None = None
    key: str | None = None
    key2: str | None = None
    seconds: float = 0.06
    keep_stable: bool = False
    point: tuple[float, float] | None = None
    required_flags: tuple[str, ...] = ()
    map_anchor: tuple[float, float] | None = None


class Detector999:
    """离线只读识别：只按文字基准定位地图篝火，不做固定阶段识别。"""

    def __init__(self, assets):
        self.assets = Path(assets)
        spec = json.loads((self.assets / "templates.json").read_text(encoding="utf-8"))
        self.size = (spec["base_width"], spec["base_height"])
        self.locator = MapLabelLocator(self.assets, spec["map_label"], self.size)

    def detect(self, frame):
        height, width = frame.shape[:2]
        if abs(width / height - 16 / 9) > .02:
            raise ValueError(f"截图比例错误：{width}x{height}，需要16:9")
        small = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
        if small.ndim == 3:
            gray = cv2.cvtColor(small, cv2.COLOR_BGRA2GRAY if small.shape[2] == 4
                                else cv2.COLOR_BGR2GRAY)
        else:
            gray = small
        if gray.std() < 3 or gray.mean() < 2:
            return Observation(Scene999.UNKNOWN, {"capture_std": float(gray.std())})
        location = self.locator.locate(frame)
        scores = {"map_label": location.text_score,
                  "map_label_second": location.text_second,
                  "map_label_fallback": location.fallback_score,
                  "map_label_target_area": float(location.target_area)}
        flags = {"target_source": "map_label" if location.point is not None else None,
                 "map_failure_reason": location.reason or None,
                 "text_source": location.text_source,
                 "text_found": location.text_found}
        targets = {"bonfire": location.point} if location.point is not None else {}
        return Observation(Scene999.MAP if location.text_found else Scene999.UNKNOWN,
                           scores, targets, flags)


@dataclass(frozen=True)
class Flow999:
    w_hold: float
    a_hold: float
    s_hold: float
    battle_hold: float
    shift_interval: float
    shift_hold: float
    refresh_gap: float
    menu_wait: float
    exit_wait: float
    map_wait: float
    map_retry: float
    map_poll: float
    panel_wait: float
    teleport_wait: float
    f_repeat_count: int
    f_repeat_interval: float
    refresh_point: tuple[float, float]
    teleport_point: tuple[float, float]
    max_loops: int


def _seconds(value, name):
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"999夜固定等待{name}无效")
    return float(value)


def _point(value, name):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"999夜固定坐标{name}需要两个数")
    x, y = value
    if (not isinstance(x, (int, float)) or not isinstance(y, (int, float))
            or not math.isfinite(x) or not math.isfinite(y)
            or not 0 <= x < BASE_W or not 0 <= y < BASE_H):
        raise ValueError(f"999夜固定坐标{name}超出1499×843参考画幅")
    return (float(x), float(y))


def load_flow_999(assets, timings: Timings, max_loops: int):
    """读取任务私有固定坐标/等待；W/A/S/战斗/Shift 仍用 config.json 的值。"""
    spec = json.loads((Path(assets) / "templates.json").read_text(encoding="utf-8"))
    fixed = spec["fixed"]
    count = fixed["f_repeat_count"]
    if type(count) is not int or not 1 <= count <= 5:
        raise ValueError("999夜F重复次数需要1至5的整数")
    flow = Flow999(
        w_hold=float(timings.w_hold),
        a_hold=float(timings.a_hold),
        s_hold=float(timings.s_hold),
        battle_hold=float(timings.battle_hold),
        shift_interval=float(timings.shift_interval),
        shift_hold=float(timings.shift_hold),
        refresh_gap=float(timings.refresh_gap),
        menu_wait=_seconds(fixed["menu_wait"], "menu_wait"),
        exit_wait=_seconds(fixed["exit_wait"], "exit_wait"),
        map_wait=_seconds(fixed["map_wait"], "map_wait"),
        map_retry=_seconds(fixed["map_retry"], "map_retry"),
        map_poll=_seconds(fixed["map_poll"], "map_poll"),
        panel_wait=_seconds(fixed["panel_wait"], "panel_wait"),
        teleport_wait=_seconds(fixed["teleport_wait"], "teleport_wait"),
        f_repeat_count=count,
        f_repeat_interval=_seconds(fixed["f_repeat_interval"], "f_repeat_interval"),
        refresh_point=_point(fixed["refresh_point"], "refresh_point"),
        teleport_point=_point(fixed["teleport_point"], "teleport_point"),
        max_loops=int(max_loops),
    )
    if flow.map_poll <= 0 or flow.map_retry <= 0:
        raise ValueError("999夜地图重试间隔和上限必须大于0")
    if flow.f_repeat_count > 1 and flow.f_repeat_interval <= 0:
        raise ValueError("999夜F重复间隔必须大于0")
    if flow.shift_hold >= flow.shift_interval:
        raise ValueError("Shift按住时间必须短于两次按下的间隔")
    if flow.battle_hold < (SHIFT_TOTAL - 1) * flow.shift_interval + flow.shift_hold:
        raise ValueError("战斗时间不足以按当前间隔完成40次Shift")
    return flow


class Nte999Loop:
    """999夜固定流程执行器；由 runtime 注入真实 Actor/Capture。"""
    name = "999夜 · 刷纽扣"

    def __init__(self, flow, actor, capture, locator, stop_event, logger=None,
                 status=None, debug_dir=None, clock=None):
        self.flow = flow
        self.actor = actor
        self.capture = capture
        self.locator = locator
        self.stop_event = stop_event
        self.log = logger or logging.getLogger("yihuan")
        self.status = status
        self.debug_dir = Path(debug_dir) if debug_dir is not None else None
        self.clock = clock or time.monotonic
        self.phase_text = "准备"
        self.completed = 0
        self.shift_count = 0
        self.last_frame = None
        self.last_observation = None
        self.last_location = None
        self.last_input = None
        self.recent_actions = deque(maxlen=12)
        self._debug_seq = 0

    def _set_phase(self, text):
        self.phase_text = text
        self.log.info("第%d轮｜%s", self.completed + 1, text)
        if self.status is not None:
            self.status(f"第{self.completed + 1}轮 · {text}")

    def _perform(self, intent, targets=None):
        self.actor.perform(intent, targets or {})
        record = getattr(self.actor, "last_input", None) or {}
        sent_at = record.get("sent_at")
        if sent_at is None:
            sent_at = self.clock()
        self.last_input = copy.deepcopy(record)
        self.recent_actions.append({"action": intent.action.value,
                                    "phase": intent.phase,
                                    "input": self.last_input})
        return float(sent_at)

    def _tap(self, key, action, phase, seconds=0.06):
        return self._perform(Intent999(action, Scene999.FIELD, phase, kind="tap",
                                       key=key, seconds=seconds))

    def _press(self, key, action, phase):
        return self._perform(Intent999(action, Scene999.FIELD, phase, kind="press", key=key))

    def _swap(self, key, key2, action, phase):
        return self._perform(Intent999(action, Scene999.FIELD, phase, kind="swap",
                                       key=key, key2=key2))

    def _release(self, key, action, phase):
        return self._perform(Intent999(action, Scene999.FIELD, phase, kind="release", key=key))

    def _click(self, action, target, point, phase, expected=Scene999.MAP):
        return self._perform(Intent999(action, expected, phase, kind="click",
                                       target=target, point=point), {target: point})

    def _wait(self, seconds):
        deadline = self.clock() + max(0.0, float(seconds))
        while True:
            self.actor.check()
            remaining = deadline - self.clock()
            if remaining <= 0:
                return
            self.stop_event.wait(min(.05, remaining))

    def _wait_until(self, deadline):
        while True:
            self.actor.check()
            remaining = deadline - self.clock()
            if remaining <= 0:
                return
            self.stop_event.wait(min(.05, remaining))

    def _first_menu(self):
        self._set_phase("首次按F打开篝火菜单")
        self._tap("f", Action999.PRESS_F, self.phase_text)
        self._wait(self.flow.menu_wait)

    def _refresh(self):
        self._set_phase("点击刷新怪物")
        self._click(Action999.CLICK_REFRESH, "refresh", self.flow.refresh_point,
                    self.phase_text, expected=Scene999.CAMP)
        self._wait(self.flow.refresh_gap)
        self._set_phase("刷新后按Esc返回")
        self._tap("esc", Action999.PRESS_ESC, self.phase_text)
        self._wait(self.flow.exit_wait)

    def _walk(self):
        self._set_phase(f"按住W前进{self.flow.w_hold:g}秒")
        started = self._press("w", Action999.PRESS_W, self.phase_text)
        self._wait_until(started + self.flow.w_hold)
        self._set_phase(f"松开W并按住A {self.flow.a_hold:g}秒")
        started = self._swap("w", "a", Action999.SWAP_WA, self.phase_text)
        self._wait_until(started + self.flow.a_hold)
        self._set_phase(f"松开A并按住S走位{self.flow.s_hold:g}秒")
        s_started = self._swap("a", "s", Action999.SWAP_AS, self.phase_text)
        self._wait_until(s_started + self.flow.s_hold)
        self._set_phase("走位结束，松开S")
        released = self._release("s", Action999.RELEASE_S, self.phase_text)
        self.log.info("S走位完成并已松开：用时%.2f秒；现在开始%.1f秒Shift战斗",
                      released - s_started, self.flow.battle_hold)
        return released

    def _battle(self, start):
        self.shift_count = 0
        self._set_phase(f"战斗（Shift 0/{SHIFT_TOTAL}）")
        deadline = start + self.flow.battle_hold
        next_shift = start
        for index in range(SHIFT_TOTAL):
            self._wait_until(next_shift)
            now = self.clock()
            if now >= deadline:
                raise TaskTimeout(f"战斗时间已到，但Shift只完成{index}/{SHIFT_TOTAL}次；"
                                  "为避免追赶连点已暂停")
            minimum_remaining = ((SHIFT_TOTAL - index - 1) * self.flow.shift_interval
                                 + self.flow.shift_hold)
            if now + minimum_remaining > deadline:
                raise TaskTimeout("任务延迟过大，无法在原时间窗口内按间隔完成40次Shift，已暂停")
            sent = self._tap("lshift", Action999.TAP_SHIFT, self.phase_text,
                             self.flow.shift_hold)
            self.shift_count = index + 1
            next_shift = sent + self.flow.shift_interval
            self.phase_text = f"战斗（Shift {self.shift_count}/{SHIFT_TOTAL}）"
            if self.shift_count % 10 == 0:
                self.log.info("战斗计时%.1f秒，Shift %d/%d；战斗期间不按S",
                              self.clock() - start, self.shift_count, SHIFT_TOTAL)
                if self.status is not None:
                    self.status(f"第{self.completed + 1}轮 · {self.phase_text}")
        self._wait_until(deadline)
        self._set_phase(f"战斗结束（Shift {self.shift_count}/{SHIFT_TOTAL}）")

    def _save_map_debug(self, frame, location, failed):
        if self.debug_dir is None or frame is None:
            return
        try:
            result = np.array(frame, copy=True)
            height, width = result.shape[:2]
            sx, sy = width / BASE_W, height / BASE_H
            if location is not None and location.text_box is not None:
                x, y, w, h = location.text_box
                cv2.rectangle(result, (round(x * sx), round(y * sy)),
                              (round((x + w) * sx), round((y + h) * sy)),
                              (80, 220, 80), 2)
            if location is not None and location.predicted is not None:
                px, py = round(location.predicted[0] * sx), round(location.predicted[1] * sy)
                cv2.drawMarker(result, (px, py), (0, 220, 255), cv2.MARKER_CROSS, 22, 2)
            if location is not None and location.point is not None:
                px, py = round(location.point[0] * sx), round(location.point[1] * sy)
                cv2.circle(result, (px, py), 26, (60, 60, 235), 3)
                cv2.drawMarker(result, (px, py), (60, 60, 235), cv2.MARKER_CROSS, 34, 2)
            if failed:
                label = "MAP LABEL FAILED: text/target not found"
            elif location is not None and location.point is not None:
                label = ("CLICK (%.1f, %.1f) text %.3f %s"
                         % (location.point[0], location.point[1],
                            location.text_score, location.text_source))
            else:
                label = "MAP DEBUG"
            cv2.rectangle(result, (0, 0), (width, 54), (18, 22, 28), -1)
            cv2.putText(result, label[:120], (14, 36), cv2.FONT_HERSHEY_SIMPLEX,
                        .8, (240, 245, 245), 2, cv2.LINE_AA)
            self._debug_seq += 1
            stamp = time.strftime("%Y%m%d_%H%M%S")
            path = self.debug_dir / f"map_{stamp}_{self._debug_seq:03d}.png"
            save_image(path, result, redact=True, scene="地图")
            self.log.info("地图定位调试图：%s", path)
        except Exception:
            self.log.exception("地图定位调试图保存失败")

    def _locate_bonfire(self):
        self._set_phase("地图定位：残丝文字基准")
        deadline = self.clock() + self.flow.map_retry
        last_reason = ""
        while True:
            self.actor.check()
            frame = self.capture.grab()
            if frame is not None:
                self.last_frame = frame
                location = self.locator.locate(frame)
                self.last_location = location
                self.last_observation = Observation(
                    Scene999.MAP if location.text_found else Scene999.UNKNOWN,
                    {"map_label": location.text_score,
                     "map_label_second": location.text_second,
                     "map_label_target_area": float(location.target_area)},
                    {"bonfire": location.point} if location.point is not None else {},
                    {"target_source": "map_label" if location.point is not None else None,
                     "map_failure_reason": location.reason or None,
                     "text_found": location.text_found})
                if location.point is not None:
                    self._save_map_debug(frame, location, failed=False)
                    self.log.info("地图定位：残丝文字%.3f（%s，次候选%.3f，右半%.3f），"
                                  "预测%s，图标面积%d，点击(%.1f,%.1f)",
                                  location.text_score, location.text_source,
                                  location.text_second, location.fallback_score,
                                  location.predicted, location.target_area,
                                  location.point[0], location.point[1])
                    return location.point
                if location.text_found:
                    self._save_map_debug(frame, location, failed=True)
                    raise TaskTimeout(f"{location.reason}；地图定位阶段已暂停")
                last_reason = location.reason
            remaining = deadline - self.clock()
            if remaining <= 0:
                if self.last_frame is not None:
                    self._save_map_debug(self.last_frame, self.last_location, failed=True)
                raise TaskTimeout(
                    f"{last_reason or '地图中未找到“残丝长巷”文字'}"
                    f"（已等待{self.flow.map_retry:.1f}秒）；已暂停并保存诊断")
            self._wait(min(self.flow.map_poll, remaining))

    def _map_and_teleport(self):
        self._set_phase("战斗后按M打开地图")
        self._tap("m", Action999.PRESS_M, self.phase_text)
        self._wait(self.flow.map_wait)
        point = self._locate_bonfire()
        self._set_phase("点击目标篝火")
        self._click(Action999.CLICK_BONFIRE, "bonfire", point, self.phase_text)
        self._wait(self.flow.panel_wait)
        self._set_phase("点击固定传送按钮")
        self._click(Action999.CLICK_TELEPORT, "teleport", self.flow.teleport_point,
                    self.phase_text)

    def _press_f_after_teleport(self):
        self._set_phase(f"传送后{self.flow.teleport_wait:g}秒已满，按F")
        for index in range(self.flow.f_repeat_count):
            self._tap("f", Action999.PRESS_F, self.phase_text)
            if index + 1 < self.flow.f_repeat_count:
                self._wait(self.flow.f_repeat_interval)

    def run(self):
        first = True
        while True:
            if first:
                self._first_menu()
                first = False
            self._refresh()
            battle_start = self._walk()
            self._battle(battle_start)
            self._map_and_teleport()
            self._set_phase(f"传送完成，等待{self.flow.teleport_wait:g}秒")
            self._wait(self.flow.teleport_wait)
            self.completed += 1
            self.log.info("第%d轮完成：已点击传送并等待%.1f秒；未做视觉确认",
                          self.completed, self.flow.teleport_wait)
            if self.flow.max_loops and self.completed >= self.flow.max_loops:
                self._set_phase(f"完成指定轮数（{self.completed}轮）")
                return
            self._press_f_after_teleport()
            self._wait(self.flow.menu_wait)
