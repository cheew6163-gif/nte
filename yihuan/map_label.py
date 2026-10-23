"""按“残丝长巷”文字基准定位用户已确认的篝火图标。

地图阶段是 999 夜唯一需要截图的阶段。这里不使用道路/建筑大范围模板，
也不要求白圈或传送按钮得分：先用局部文字模板找到地名，再在文字左上方
的固定相对窗口内找粉色篝火图标，点击图标中心。地图平移时文字和图标一起
移动，相对关系不变；不同缩放由相对窗口和“离基准点最近”的选点规则覆盖。
"""
from dataclasses import dataclass
from pathlib import Path
import cv2
import numpy as np
from .vision import read_image


BASE_W, BASE_H = 1499, 843


@dataclass(frozen=True)
class MapLabelLocation:
    point: tuple[float, float] | None
    text_point: tuple[float, float] | None = None
    text_box: tuple[int, int, int, int] | None = None
    text_score: float = 0.0
    text_second: float = 0.0
    target_area: int = 0
    predicted: tuple[float, float] | None = None
    text_found: bool = False
    reason: str = ""


class MapLabelLocator:
    """Find the calibrated bonfire from the map label, without scene classifiers."""

    def __init__(self, assets, spec, size=(BASE_W, BASE_H)):
        self.assets = Path(assets)
        self.size = (int(size[0]), int(size[1]))
        self.spec = spec
        self.template = read_image(self.assets / spec["text_file"])
        if self.template.ndim == 3:
            self.template = cv2.cvtColor(self.template, cv2.COLOR_BGR2GRAY)
        if self.template.shape[0] >= self.size[1] or self.template.shape[1] >= self.size[0]:
            raise ValueError("地图文字模板尺寸无效")
        if float(self.template.std()) < 2:
            raise ValueError("地图文字模板内容过少")
        self.search_box = tuple(int(v) for v in spec["search_box"])
        self.text_threshold = float(spec["text_threshold"])
        self.unique_radius = int(spec["unique_radius"])
        self.unique_margin = float(spec["unique_margin"])
        self.target_offset = tuple(float(v) for v in spec["target_offset"])
        self.target_window = tuple(int(v) for v in spec["target_window"])
        self.target_radius = float(spec["target_radius"])
        self.pink_low = np.array(spec["pink_low"], dtype=np.uint8)
        self.pink_high = np.array(spec["pink_high"], dtype=np.uint8)
        self.min_pink_area = int(spec["min_pink_area"])
        self.cluster_kernel = int(spec["cluster_kernel"])
        self.ambiguity_margin = float(spec["ambiguity_margin"])

    def _normalize(self, frame):
        height, width = frame.shape[:2]
        if abs(width / height - 16 / 9) > .02:
            raise ValueError(f"截图比例错误：{width}x{height}，需要16:9")
        small = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
        if small.ndim == 2:
            gray = small
            color = None
        elif small.shape[2] == 4:
            color = small[:, :, :3]
            gray = cv2.cvtColor(color, cv2.COLOR_BGR2GRAY)
        else:
            color = small
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        return color, gray

    def _text(self, gray):
        left, top, right, bottom = self.search_box
        right, bottom = min(right, self.size[0]), min(bottom, self.size[1])
        template_h, template_w = self.template.shape[:2]
        if right - left < template_w or bottom - top < template_h:
            return None, 0.0, 0.0, "地图文字搜索区域小于模板"
        scores = cv2.matchTemplate(gray[top:bottom, left:right], self.template,
                                   cv2.TM_CCOEFF_NORMED)
        scores = np.nan_to_num(scores, nan=-1.0, posinf=-1.0, neginf=-1.0)
        _, best, _, position = cv2.minMaxLoc(scores)
        x, y = position
        yy, xx = np.ogrid[:scores.shape[0], :scores.shape[1]]
        other = scores.copy()
        other[(xx - x) ** 2 + (yy - y) ** 2 <= self.unique_radius ** 2] = -1.0
        second = float(other.max())
        box = (left + x, top + y, template_w, template_h)
        center = (left + x + template_w / 2, top + y + template_h / 2)
        return center, float(best), second, box

    def _bonfires(self, color, text_center):
        """Return pink bonfire clusters in the calibrated up-left window."""
        left, top, right, bottom = self.target_window
        x1 = max(0, int(text_center[0] + left))
        x2 = min(self.size[0], int(text_center[0] + right))
        y1 = max(0, int(text_center[1] + top))
        y2 = min(self.size[1], int(text_center[1] + bottom))
        if x2 <= x1 or y2 <= y1:
            return []
        hsv = cv2.cvtColor(color[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, self.pink_low, self.pink_high)
        merged = cv2.dilate(mask, np.ones((self.cluster_kernel, self.cluster_kernel), np.uint8))
        count, labels, _, _ = cv2.connectedComponentsWithStats(merged)
        found = []
        for index in range(1, count):
            pixels = (labels == index) & (mask > 0)
            area = int(np.count_nonzero(pixels))
            if area < self.min_pink_area:
                continue
            ys, xs = np.nonzero(pixels)
            center = (x1 + (float(xs.min()) + float(xs.max())) / 2,
                      y1 + (float(ys.min()) + float(ys.max())) / 2)
            found.append((center, area))
        return found

    def locate(self, frame):
        color, gray = self._normalize(frame)
        if gray.std() < 3 or gray.mean() < 2:
            return MapLabelLocation(None, reason="画面为空或过暗")
        center, best, second, box_or_reason = self._text(gray)
        if center is None:
            return MapLabelLocation(None, reason=box_or_reason)
        box = box_or_reason
        if best < self.text_threshold:
            return MapLabelLocation(
                None, text_box=box, text_score=best, text_second=second,
                reason="未找到“残丝长巷”文字；地图可能未展开、被遮挡或缩放/位置超出范围")
        if best - second < self.unique_margin:
            return MapLabelLocation(
                None, text_point=center, text_box=box, text_score=best, text_second=second,
                reason="“残丝长巷”文字存在多个相近候选，不能唯一定位")
        predicted = (center[0] + self.target_offset[0], center[1] + self.target_offset[1])
        if color is None:
            return MapLabelLocation(
                None, text_point=center, text_box=box, text_score=best, text_second=second,
                predicted=predicted, text_found=True,
                reason="当前截图不是彩色，无法按粉色图标定位篝火")
        candidates = []
        for point, area in self._bonfires(color, center):
            distance = float(np.hypot(point[0] - predicted[0], point[1] - predicted[1]))
            if distance <= self.target_radius:
                candidates.append((distance, area, point))
        if not candidates:
            return MapLabelLocation(
                None, text_point=center, text_box=box, text_score=best, text_second=second,
                predicted=predicted, text_found=True,
                reason="已找到“残丝长巷”，但其左上方未找到指定篝火图标")
        candidates.sort(key=lambda row: (row[0], -row[1]))
        if len(candidates) > 1 and candidates[1][0] - candidates[0][0] < self.ambiguity_margin:
            return MapLabelLocation(
                None, text_point=center, text_box=box, text_score=best, text_second=second,
                target_area=candidates[0][1], predicted=predicted, text_found=True,
                reason="“残丝长巷”上方存在多个相近篝火候选，不能唯一确认")
        point = candidates[0][2]
        return MapLabelLocation(
            (float(point[0]), float(point[1])), text_point=center, text_box=box,
            text_score=best, text_second=second, target_area=candidates[0][1],
            predicted=predicted, text_found=True)
