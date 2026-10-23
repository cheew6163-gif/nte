"""局部模板匹配；按参考画幅归一化，返回按钮实际匹配中心。"""
from pathlib import Path
import json
import cv2
import numpy as np
from .core import Observation, Scene
from .windows import ActionChanged
from .privacy import redact_frame


def read_image(path):
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError(f"无法读取图片：{path}")
    return image


def save_image(path, image, *, redact=False, scene=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    saved = redact_frame(image, scene) if redact else image
    ok, data = cv2.imencode(path.suffix or ".png", saved)
    if not ok:
        raise ValueError("截图编码失败")
    data.tofile(str(path))


class Detector:
    def __init__(self, assets):
        self.assets = Path(assets)
        spec = json.loads((self.assets / "templates.json").read_text(encoding="utf-8"))
        self.size = (spec["base_width"], spec["base_height"])
        self.specs = spec["templates"]
        self.templates = {}
        for name, item in self.specs.items():
            image = read_image(self.assets / item["file"])
            self.templates[name] = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
            if float(self.templates[name].std()) < 2:
                raise ValueError(f"模板内容过少：{name}")

    def detect(self, frame):
        height, width = frame.shape[:2]
        if abs(width / height - 16 / 9) > .02:
            raise ValueError(f"截图比例错误：{width}x{height}，需要16:9")
        small = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
        if small.ndim == 3:
            small = cv2.cvtColor(small, cv2.COLOR_BGRA2GRAY if small.shape[2] == 4 else cv2.COLOR_BGR2GRAY)
        if small.std() < 3 or small.mean() < 2:
            return Observation(Scene.UNKNOWN, {"capture_std": float(small.std())})
        scores, centers = {}, {}
        for name, template in self.templates.items():
            item = self.specs[name]
            x1, y1, x2, y2 = item["box"]
            margin = item["margin"]
            left, top = max(0, x1 - margin), max(0, y1 - margin)
            right, bottom = min(self.size[0], x2 + margin), min(self.size[1], y2 + margin)
            region = small[top:bottom, left:right]
            result = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
            _, score, _, position = cv2.minMaxLoc(result)
            scores[name] = float(score) if np.isfinite(score) else 0.0
            centers[name] = (left + position[0] + template.shape[1] / 2,
                             top + position[1] + template.shape[0] / 2)
        yes = lambda name: scores[name] >= self.specs[name]["threshold"]
        battle_marker = yes("escape")
        if "escape_key" in scores:
            # 图标背景会随镜头变化；用高分Esc文字和位置一致的图标共同确认。
            icon_box, key_box = self.specs["escape"]["box"], self.specs["escape_key"]["box"]
            expected_dx = (key_box[0] + key_box[2] - icon_box[0] - icon_box[2]) / 2
            expected_dy = (key_box[1] + key_box[3] - icon_box[1] - icon_box[3]) / 2
            icon, key = centers["escape"], centers["escape_key"]
            aligned = (abs(key[0] - icon[0] - expected_dx) <= 3
                       and abs(key[1] - icon[1] - expected_dy) <= 3)
            battle_marker = battle_marker or (yes("escape_key") and aligned
                and scores["escape"] >= self.specs["escape"].get("weak_threshold",
                                                           self.specs["escape"]["threshold"]))
        if yes("dialog_body") and yes("confirm"):
            scene = Scene.DIALOG
        elif yes("victory") and (yes("reward") or yes("leave")):
            scene = Scene.VICTORY
        elif yes("defeat") and (yes("reward") or yes("leave")):
            scene = Scene.DEFEAT
        elif yes("go") and yes("activity_title"):
            scene = Scene.ACTIVITY
        elif yes("match") and yes("vs"):
            scene = Scene.MATCH
        elif (battle_marker and not any(yes(n) for n in
              ("victory", "defeat", "reward", "leave", "dialog_body", "confirm"))):
            # 结算/弹窗的标题识别不全时，也不把它误当成可退出的战场。
            scene = Scene.BATTLE
        else:
            scene = Scene.UNKNOWN
        return Observation(scene, scores, {name: centers[name] for name in ("go", "match", "confirm")})


class StableScene:
    def __init__(self, frames=2):
        self.required = frames
        self.scene = None
        self.count = 0

    def push(self, observation):
        if observation.scene == self.scene:
            self.count += 1
        else:
            self.scene, self.count = observation.scene, 1
        return self.count >= self.required


def guarded_action(intent, capture, detector, actor, stop_event, on_validation=None):
    """定位前和按下前都取新图；页面或按钮变化时交还状态机重新识别。"""
    if stop_event.is_set():
        return False

    def validate():
        frame = capture()
        if frame is None:
            return Observation(Scene.UNKNOWN)
        observation = detector.detect(frame)
        if on_validation is not None:
            on_validation(frame, observation)
        return observation

    def point_matches(actual, expected):
        if actual is None or expected is None:
            return False
        width, height = detector.size
        return max(abs(actual[0] - expected[0]) * actor.window.width / width,
                   abs(actual[1] - expected[1]) * actor.window.height / height) <= 4

    def matches(observation):
        if (observation.scene != intent.expected
                or not all(observation.flags.get(name)
                           for name in getattr(intent, "required_flags", ()))):
            return False
        anchor = getattr(intent, "map_anchor", None)
        if anchor is not None:
            # 传送按钮固定不动时，独立地图目标的移动同样使旧动作失效。
            return point_matches(observation.targets.get("bonfire"), anchor)
        return True

    fresh = validate()
    if stop_event.is_set() or not matches(fresh):
        return False
    initial_target = fresh.targets.get(intent.target) if intent.target else None
    if intent.target and initial_target is None:
        return False

    def before_press():
        if stop_event.is_set():
            return False
        current = validate()
        if stop_event.is_set() or not matches(current):
            return False
        if getattr(intent, "map_anchor", None) is not None:
            if not point_matches(current.targets.get("bonfire"), fresh.targets.get("bonfire")):
                return False
        if intent.target:
            target = current.targets.get(intent.target)
            if target is None:
                return False
            # 模板中心采用参考图坐标；容许误差必须换算为实际游戏像素。
            width, height = detector.size
            scale_x = actor.window.width / width
            scale_y = actor.window.height / height
            if max(abs(target[0] - initial_target[0]) * scale_x,
                   abs(target[1] - initial_target[1]) * scale_y) > 4:
                return False
        return True

    try:
        actor.perform(intent, fresh.targets, before_press=before_press)
    except ActionChanged:
        return False
    return not stop_event.is_set()
