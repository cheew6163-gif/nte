"""999夜刷纽扣：地图重定位 → 传送 → 刷新怪物 → 固定走位 → 80 秒战斗。

与咔呲任务共用 runtime/窗口/截图设施，但使用独立的识别器与资源目录
(assets999)，不改动咔呲的模板和阈值。
"""
from dataclasses import dataclass, field
from enum import Enum
import json
import logging
import math
from pathlib import Path

import cv2
import numpy as np

from .core import Observation, TaskTimeout, Timings
from .vision import read_image


BASE_W, BASE_H = 1499, 843
MAP_AREA_RIGHT = 1050          # 右侧传送面板从约 x=1085 开始
SHIFT_TOTAL = 40               # 80 秒 / 2 秒


class Scene999(str, Enum):
    UNKNOWN = "未识别"
    MAP = "地图"
    CAMP = "篝火菜单"
    FIELD = "人物界面"


class Phase(str, Enum):
    READY = "等待人物界面"
    MAP = "地图定位"
    TELEPORT = "等待传送到达"
    MENU = "等待篝火菜单"
    EXIT = "刷新后关闭菜单"
    EXIT_FIELD = "等待返回人物界面"
    W = "前进"
    A = "左移"
    S = "后退走位至图7"
    BATTLE = "战斗中"
    MAP_AFTER = "战斗结束，准备开地图"
    DONE = "完成指定轮数"


class Action999(str, Enum):
    OPEN_MAP = "按M打开地图"
    CLICK_BONFIRE = "点击目标篝火"
    CLICK_TELEPORT = "点击传送"
    PRESS_F = "按F打开篝火菜单"
    CLICK_REFRESH = "点击刷新怪物"
    PRESS_ESC = "按Esc返回人物界面"
    PRESS_W = "按住W前进"
    SWAP_WA = "松开W并按住A"
    SWAP_AS = "松开A并按住S走位"
    RELEASE_S = "走位结束，松开S"
    TAP_SHIFT = "轻点左Shift"
    PRESS_M = "战斗后按M开地图"


@dataclass(frozen=True)
class Intent999:
    action: Action999
    expected: Scene999
    phase: Phase
    kind: str = "click"          # click / press / release / tap / swap
    target: str | None = None
    key: str | None = None
    key2: str | None = None
    seconds: float = 0.06
    keep_stable: bool = False    # 长按动作不重置画面稳定计数
    point: tuple[float, float] | None = None
    required_flags: tuple[str, ...] = ()


def _merge_clusters(points, gap):
    groups = []
    for x, y, area in points:
        for group in groups:
            if abs(group["sx"] / group["n"] - x) < gap and abs(group["sy"] / group["n"] - y) < gap:
                group["sx"] += x
                group["sy"] += y
                group["area"] += area
                group["n"] += 1
                break
        else:
            groups.append({"sx": x, "sy": y, "area": area, "n": 1})
    return [(g["sx"] / g["n"], g["sy"] / g["n"], g["area"]) for g in groups]


def _components(mask, min_area):
    mask = (mask.astype(np.uint8)) * 255
    count, _, stats, centers = cv2.connectedComponentsWithStats(mask, 8)
    return [(float(centers[i][0]), float(centers[i][1]), int(stats[i][4]))
            for i in range(1, count) if stats[i][4] >= min_area]


class Detector999:
    """地图、篝火菜单、人物界面和 F 提示的识别器。"""

    def __init__(self, assets):
        self.assets = Path(assets)
        spec = json.loads((self.assets / "templates.json").read_text(encoding="utf-8"))
        self.size = (spec["base_width"], spec["base_height"])
        self.specs = spec["templates"]
        self.templates = {}
        for name, item in self.specs.items():
            # UID含账号数字，不参与999夜的界面判断，也不加载或匹配此模板。
            if name == "hud":
                continue
            image = read_image(self.assets / item["file"])
            if image.ndim == 3:
                image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            self.templates[name] = image

    def _match(self, gray, name):
        item = self.specs[name]
        x1, y1, x2, y2 = item["box"]
        margin = item["margin"]
        left, top = max(0, x1 - margin), max(0, y1 - margin)
        right, bottom = min(self.size[0], x2 + margin), min(self.size[1], y2 + margin)
        result = cv2.matchTemplate(gray[top:bottom, left:right], self.templates[name],
                                   cv2.TM_CCOEFF_NORMED)
        _, score, _, position = cv2.minMaxLoc(result)
        h, w = self.templates[name].shape[:2]
        center = (left + position[0] + w / 2, top + position[1] + h / 2)
        return (float(score) if np.isfinite(score) else 0.0), center

    def _map_icons(self, image, panel):
        """HSV 分割篝火与箭头；对变暗/失焦画面同样有效。"""
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        hue, sat, val = cv2.split(hsv)
        limit = MAP_AREA_RIGHT if panel else self.size[0]
        pink = (((hue >= 150) & (hue <= 179)) | (hue <= 6)) & (sat > 55) & (val > 25)
        pink[:, limit:] = False
        icons = [item for item in _merge_clusters(_components(pink, 8), 24)
                 if 40 <= item[2] <= 1500]
        gold = (hue >= 15) & (hue <= 35) & (sat > 60) & (val > 50)
        gold[:, limit:] = False
        arrows = [item for item in _merge_clusters(_components(gold, 15), 28)
                  if 35 <= item[2] <= 2500]
        return icons, arrows

    def detect(self, frame):
        height, width = frame.shape[:2]
        if abs(width / height - 16 / 9) > .02:
            raise ValueError(f"截图比例错误：{width}x{height}，需要16:9")
        small = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
        if small.ndim == 3:
            small = cv2.cvtColor(small, cv2.COLOR_BGRA2GRAY if small.shape[2] == 4
                                 else cv2.COLOR_BGR2GRAY)
        if small.std() < 3 or small.mean() < 2:
            return Observation(Scene999.UNKNOWN, {"capture_std": float(small.std())})

        scores, centers = {}, {}
        for name in self.templates:
            scores[name], centers[name] = self._match(small, name)
        yes = lambda name: scores[name] >= self.specs[name]["threshold"]

        map_open = yes("map_title")
        # 菜单身份与刷新按钮分别判断；按钮悬停不应把整个菜单判成未知。
        camp = yes("camp_mark")
        refresh_names = [name for name in ("refresh", "refresh_hover", "refresh_label")
                         if name in scores and yes(name)]
        refresh_name = None
        if "refresh_label" in refresh_names:
            refresh_name = "refresh_label"
        elif refresh_names:
            refresh_name = max(refresh_names, key=lambda name: scores[name])
        panel = yes("teleport") or yes("panel_close")
        if map_open:
            scene = Scene999.MAP
        elif camp:
            scene = Scene999.CAMP
        else:
            # 非黑画面未命中地图/篝火菜单时作为人物界面处理。
            # 此分类不证明已到达篝火；按F仍须最新两次截图均命中f_prompt。
            scene = Scene999.FIELD

        targets = {}
        flags = {"panel": panel, "bonfires": 0, "arrow": None,
                 "f_prompt": yes("f_prompt"), "refresh_variant": refresh_name}
        color = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
        if color.ndim == 3 and color.shape[2] == 4:
            color = cv2.cvtColor(color, cv2.COLOR_BGRA2BGR)
        if scene == Scene999.MAP:
            icons, arrows = self._map_icons(color, panel)
            center = (BASE_W / 2, BASE_H / 2)   # 地图始终以玩家为中心
            player = None
            if arrows:
                candidate = min(arrows, key=lambda a: math.hypot(a[0] - center[0],
                                                                 a[1] - center[1]))
                if math.hypot(candidate[0] - center[0], candidate[1] - center[1]) < 60:
                    player = candidate
            if player is not None:
                icons = [i for i in icons
                         if math.hypot(i[0] - player[0], i[1] - player[1]) > 12]
            else:
                player = center
            flags["arrow"] = [round(player[0], 1), round(player[1], 1)]
            flags["bonfires"] = len(icons)
            if icons:
                target = min(icons, key=lambda i: math.hypot(i[0] - player[0],
                                                             i[1] - player[1]))
                if math.hypot(target[0] - player[0], target[1] - player[1]) <= 260:
                    targets["bonfire"] = (target[0], target[1])
            if panel and yes("teleport"):
                targets["teleport"] = centers["teleport"]
            if yes("panel_close"):
                targets["close"] = centers["panel_close"]
        elif scene == Scene999.CAMP and refresh_name is not None:
            dx, dy = self.specs[refresh_name].get("click_offset", [0, 0])
            targets["refresh"] = (centers[refresh_name][0] + dx, centers[refresh_name][1] + dy)

        return Observation(scene, scores, targets, flags)


class Nte999Task:
    name = "999夜 · 刷纽扣"

    def __init__(self, timings=None, max_loops=0, now=0.0, logger=None):
        self.timings = timings or Timings()
        self.max_loops = max_loops
        self.log = logger or logging.getLogger("yihuan")
        self.phase = Phase.READY
        self.entered = now
        self.completed = 0
        self.last_scene = None
        self.unknown_since = None
        self.click_pos = None
        self.click_at = 0.0
        self.click_attempts = 0
        self.round_pending = False
        self.f_attempts = self.esc_attempts = 0
        self.last_f_at = self.last_esc_at = 0.0
        self.refresh_at = 0.0
        self.w_start = self.a_start = self.s_start = 0.0
        self.battle_start = 0.0
        self.battle_finished_at = 0.0
        self.shift_count = 0
        self.next_shift = 0.0
        self.no_target_since = None
        self.last_progress = 0.0

    @property
    def done(self):
        return self.phase == Phase.DONE

    @property
    def blind(self):
        """只有松开走位S之后的战斗不截图；走位阶段仍识图。"""
        return self.phase == Phase.BATTLE

    @property
    def phase_text(self):
        if self.phase == Phase.BATTLE:
            return f"战斗（Shift {self.shift_count}/{SHIFT_TOTAL}）"
        if self.phase == Phase.W:
            return "按住W前进"
        if self.phase == Phase.A:
            return "按住A左移"
        if self.phase == Phase.S:
            return f"按住S走位至图7（约{self.timings.s_hold:g}秒，随后松开）"
        return self.phase.value

    def _phase(self, phase, now):
        self.phase, self.entered = phase, now
        self.log.info("第%d轮｜%s", self.completed + 1, self.phase_text)

    def _intent(self, action, expected, **kwargs):
        return Intent999(action, expected, self.phase, **kwargs)

    def check_timeout(self, now, observation=None):
        limits = {
            Phase.READY: self.timings.map_timeout,
            Phase.MAP: self.timings.map_timeout,
            Phase.TELEPORT: self.timings.teleport_timeout,
            Phase.MENU: self.timings.menu_timeout,
            Phase.EXIT: self.timings.refresh_gap + 3.0,
            Phase.EXIT_FIELD: 10.0,
            Phase.W: self.timings.w_hold + self.timings.move_unknown,
            Phase.A: self.timings.a_hold + self.timings.move_unknown,
            Phase.S: self.timings.s_hold + self.timings.move_unknown,
            Phase.MAP_AFTER: 10.0,
        }
        if self.phase in limits and now - self.entered >= limits[self.phase]:
            if self.phase == Phase.MENU and self.last_scene == Scene999.CAMP:
                raise TaskTimeout("篝火菜单已出现，但刷新怪物按钮未通过识别或动作前校验，已暂停")
            raise TaskTimeout(f"{self.phase_text}超时，已暂停；请查看日志和诊断截图")
        scene = observation.scene if observation is not None else self.last_scene
        if scene == Scene999.UNKNOWN and self.phase in (Phase.W, Phase.A, Phase.S):
            if self.unknown_since is None:
                self.unknown_since = now
            elif now - self.unknown_since >= self.timings.move_unknown:
                raise TaskTimeout("长按期间连续无法识别人物界面，已暂停并释放按键")
        else:
            self.unknown_since = None

    def observe(self, observation, now):
        if self.done:
            return None
        self.last_scene = observation.scene
        self.check_timeout(now, observation)
        scene, phase = observation.scene, self.phase
        if phase == Phase.READY:
            if scene == Scene999.MAP:
                self._phase(Phase.MAP, now)
            elif scene == Scene999.FIELD:
                return self._intent(Action999.OPEN_MAP, Scene999.FIELD, kind="tap", key="m")
            elif scene == Scene999.CAMP:
                raise TaskTimeout("启动时停在篝火菜单；请先按 Esc 返回人物界面再启动")
            return None

        if phase == Phase.MAP:
            if scene != Scene999.MAP:
                return None
            target = observation.targets.get("bonfire")
            if observation.flags.get("panel"):
                # 面板淡入时允许继续等；没有高分传送按钮时绝不发布点击。
                if "teleport" not in observation.targets:
                    return None
                if (target is not None and self.click_pos is not None
                        and math.dist(self.click_pos, target) > 24):
                    raise TaskTimeout("当前选中的篝火与目标位置不一致，已暂停")
                return self._intent(Action999.CLICK_TELEPORT, Scene999.MAP, target="teleport")
            if target is None:
                if self.no_target_since is None:
                    self.no_target_since = now
                waited = now - self.no_target_since
                if waited >= 3.0:
                    raise TaskTimeout(
                        f"地图上未找到篝火（已等待{waited:.1f}秒，"
                        f"识别到{observation.flags.get('bonfires', 0)}个图标）；"
                        "请确认地图缩放与基准一致，已保存诊断截图")
                return None
            self.no_target_since = None
            if self.click_pos is not None and now - self.click_at < 1.2:
                return None
            if self.click_attempts >= 2:
                raise TaskTimeout("点击篝火后传送面板未出现，已暂停")
            # 只描述动作；真正发出后在commit中消耗重试次数。
            return self._intent(Action999.CLICK_BONFIRE, Scene999.MAP,
                                target="bonfire", point=tuple(target))

        if phase == Phase.TELEPORT:
            if scene == Scene999.CAMP:
                self.f_attempts = 0
                self._phase(Phase.MENU, now)
                return None
            if scene == Scene999.MAP:
                if now - self.entered > 5:
                    raise TaskTimeout("点击传送后仍停在地图，已暂停")
                return None
            if scene == Scene999.FIELD and observation.flags.get("f_prompt"):
                if self.round_pending:
                    self.completed += 1
                    self.round_pending = False
                    self.log.info("第%d轮完成，已确认传送回目标篝火", self.completed)
                    if self.max_loops and self.completed >= self.max_loops:
                        self._phase(Phase.DONE, now)
                        return None
                return self._intent(Action999.PRESS_F, Scene999.FIELD, kind="tap", key="f",
                                    required_flags=("f_prompt",))
            # 不再以UID或宽松FIELD分类启动15秒到达倒计时。
            # F提示未出现时继续等待，由teleport_timeout限制整个传送阶段。
            return None

        if phase == Phase.MENU:
            if scene == Scene999.CAMP and "refresh" in observation.targets:
                return self._intent(Action999.CLICK_REFRESH, Scene999.CAMP, target="refresh")
            if (scene == Scene999.FIELD and observation.flags.get("f_prompt")
                    and now - self.last_f_at >= 2.0 and self.f_attempts < 3):
                return self._intent(Action999.PRESS_F, Scene999.FIELD, kind="tap", key="f",
                                    required_flags=("f_prompt",))
            return None

        if phase == Phase.EXIT:
            if scene == Scene999.FIELD:
                self.esc_attempts = 0
                self._phase(Phase.EXIT_FIELD, now)
            elif scene == Scene999.CAMP and now - self.refresh_at >= self.timings.refresh_gap:
                return self._intent(Action999.PRESS_ESC, Scene999.CAMP, kind="tap", key="esc")
            return None

        if phase == Phase.EXIT_FIELD:
            if scene == Scene999.FIELD:
                return self._intent(Action999.PRESS_W, Scene999.FIELD, kind="press",
                                    key="w", keep_stable=True)
            if (scene == Scene999.CAMP and now - self.last_esc_at >= 1.5
                    and self.esc_attempts < 3):
                return self._intent(Action999.PRESS_ESC, Scene999.CAMP, kind="tap", key="esc")
            return None

        if phase in (Phase.W, Phase.A, Phase.S):
            if scene in (Scene999.MAP, Scene999.CAMP):
                raise TaskTimeout(f"{self.phase_text}期间识别到{scene.value}，已暂停并释放按键")
            if scene != Scene999.FIELD:
                return None
            if phase == Phase.W and now - self.w_start >= self.timings.w_hold:
                return self._intent(Action999.SWAP_WA, Scene999.FIELD, kind="swap",
                                    key="w", key2="a", keep_stable=True)
            if phase == Phase.A and now - self.a_start >= self.timings.a_hold:
                return self._intent(Action999.SWAP_AS, Scene999.FIELD, kind="swap",
                                    key="a", key2="s", keep_stable=True)
            if phase == Phase.S and now - self.s_start >= self.timings.s_hold:
                return self._intent(Action999.RELEASE_S, Scene999.FIELD, kind="release",
                                    key="s", keep_stable=True)
            return None

        if self.blind:
            intents = self.tick(now)
            return intents[0] if intents else None

        if phase == Phase.MAP_AFTER:
            if scene == Scene999.MAP:
                self.round_pending = True
                self.click_pos = None
                self.click_attempts = 0
                self.no_target_since = None
                self._phase(Phase.MAP, now)
            elif scene == Scene999.FIELD and now - self.battle_finished_at >= 0.8:
                return self._intent(Action999.PRESS_M, Scene999.FIELD, kind="tap", key="m")
            return None
        return None

    def commit(self, intent, now, sent_at=None, actual_target=None):
        if intent.phase != self.phase:
            raise RuntimeError("过期动作被拒绝")
        sent_at = now if sent_at is None else sent_at
        action = intent.action
        if action in (Action999.OPEN_MAP, Action999.PRESS_M):
            self.click_pos = None
            self.click_attempts = 0
            self.no_target_since = None
            if action == Action999.PRESS_M:
                self.round_pending = True
            self._phase(Phase.MAP, now)
        elif action == Action999.CLICK_BONFIRE:
            self.click_pos = tuple(actual_target) if actual_target is not None else intent.point
            self.click_at = now
            self.click_attempts += 1
        elif action == Action999.CLICK_TELEPORT:
            self.f_attempts = 0
            self._phase(Phase.TELEPORT, now)
        elif action == Action999.PRESS_F:
            if self.phase != Phase.MENU:
                self.f_attempts = 0
                self._phase(Phase.MENU, now)
            self.f_attempts += 1
            self.last_f_at = sent_at
        elif action == Action999.CLICK_REFRESH:
            self.refresh_at = now
            self.esc_attempts = 0
            self._phase(Phase.EXIT, now)
        elif action == Action999.PRESS_ESC:
            if self.phase != Phase.EXIT_FIELD:
                self.esc_attempts = 0
                self._phase(Phase.EXIT_FIELD, now)
            self.esc_attempts += 1
            self.last_esc_at = sent_at
        elif action == Action999.PRESS_W:
            self.w_start = sent_at
            self._phase(Phase.W, sent_at)
        elif action == Action999.SWAP_WA:
            self.a_start = sent_at
            self._phase(Phase.A, sent_at)
        elif action == Action999.SWAP_AS:
            self.s_start = sent_at
            self._phase(Phase.S, sent_at)
        elif action == Action999.RELEASE_S:
            # 图6到图7的S只用于走位。实际松开成功后才开始80秒战斗。
            self.battle_start = sent_at
            self.shift_count = 0
            self.next_shift = sent_at
            self.unknown_since = None
            self.last_progress = sent_at
            self.log.info("S走位完成并已松开：用时%.2f秒；现在开始%.1f秒Shift战斗",
                          sent_at - self.s_start, self.timings.battle_hold)
            self._phase(Phase.BATTLE, sent_at)
        elif action == Action999.TAP_SHIFT:
            if self.phase != Phase.BATTLE:
                raise RuntimeError("走位尚未完成或S尚未松开，禁止战斗Shift")
            self.shift_count += 1
            # 以实际按下时间为准；卡顿后不追赶连点。
            self.next_shift = sent_at + self.timings.shift_interval
            if now > self.battle_start + self.timings.battle_hold:
                raise TaskTimeout("Shift执行超过战斗时间上限，已暂停")

    def tick(self, now):
        """S走位并松开后，每两秒Shift；80秒和40次完成后结束。"""
        if not self.blind:
            return []
        deadline = self.battle_start + self.timings.battle_hold
        if now >= deadline:
            if self.shift_count != SHIFT_TOTAL:
                raise TaskTimeout(f"战斗时间已到，但Shift只完成{self.shift_count}/{SHIFT_TOTAL}次；"
                                  "为避免追赶连点已暂停")
            self.battle_finished_at = now
            self._phase(Phase.MAP_AFTER, now)
            return []
        if now - self.last_progress >= 5:
            self.last_progress = now
            self.log.info("战斗计时%.1f秒，Shift %d/%d，剩余%.1f秒；不按S",
                          now - self.battle_start, self.shift_count, SHIFT_TOTAL, deadline - now)
        if self.shift_count < SHIFT_TOTAL and now >= self.next_shift:
            minimum_remaining = ((SHIFT_TOTAL - self.shift_count - 1) * self.timings.shift_interval
                                 + self.timings.shift_hold)
            if now + minimum_remaining > deadline:
                raise TaskTimeout("任务延迟过大，无法在原时间窗口内按间隔完成40次Shift，已暂停")
            return [self._intent(Action999.TAP_SHIFT, Scene999.FIELD, kind="tap",
                                 key="lshift", seconds=self.timings.shift_hold, keep_stable=True)]
        return []
