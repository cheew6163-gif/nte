"""Locate the calibrated bonfire from static map geometry at the verified zoom."""
from dataclasses import dataclass
from pathlib import Path
import cv2
import numpy as np
from .vision import read_image


@dataclass(frozen=True)
class MapLocation:
    point: tuple[float, float] | None
    score: float = 0.0
    second: float = 0.0
    parts: float = 0.0
    reason: str = ""


class MapContextMatcher:
    def __init__(self, assets, spec):
        self.spec = spec
        self.template = read_image(Path(assets) / spec["file"])
        self.mask = read_image(Path(assets) / spec["mask"])
        if self.template.ndim == 3:
            self.template = cv2.cvtColor(self.template, cv2.COLOR_BGR2GRAY)
        if self.mask.ndim == 3:
            self.mask = cv2.cvtColor(self.mask, cv2.COLOR_BGR2GRAY)
        if self.template.shape != self.mask.shape or not np.any(self.mask):
            raise ValueError("地图上下文模板与遮罩不匹配")

    @staticmethod
    def _correlation(a, b, mask):
        a, b = a[mask].astype(np.float32), b[mask].astype(np.float32)
        if a.size < 100:
            return 0.0
        a -= a.mean()
        b -= b.mean()
        denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
        return float(np.dot(a, b) / denominator) if denominator > 1e-6 else 0.0

    def locate(self, gray, right_limit):
        left, top, right, bottom = self.spec["search_box"]
        right, bottom = min(right, right_limit, gray.shape[1]), min(bottom, gray.shape[0])
        h, w = self.template.shape
        if right-left < w or bottom-top < h:
            return MapLocation(None, reason="稳定地图地形未完整显示；请恢复基准缩放并移动地图")
        scores = cv2.matchTemplate(gray[top:bottom, left:right], self.template,
                                   cv2.TM_CCOEFF_NORMED, mask=self.mask)
        scores = np.nan_to_num(scores, nan=-1.0, posinf=-1.0, neginf=-1.0)
        _, best, _, position = cv2.minMaxLoc(scores)
        x, y = position
        yy, xx = np.ogrid[:scores.shape[0], :scores.shape[1]]
        other = scores.copy()
        other[(xx-x)**2 + (yy-y)**2 <= self.spec["unique_radius"]**2] = -1
        second = float(other.max())
        if best < self.spec["threshold"]:
            return MapLocation(None, float(best), second,
                               reason="地图道路/建筑未匹配；请恢复基准缩放，移开遮挡后重试")
        if best-second < self.spec["unique_margin"]:
            return MapLocation(None, float(best), second,
                               reason="地图存在多个相近地形候选，不能唯一确认目标")
        patch = gray[top+y:top+y+h, left+x:left+x+w]
        parts = []
        for y1, y2 in ((0, h//2), (h//2, h)):
            for x1, x2 in ((0, w//2), (w//2, w)):
                part = np.s_[y1:y2, x1:x2]
                parts.append(self._correlation(self.template[part], patch[part], self.mask[part] > 0))
        minimum = min(parts)
        if minimum < self.spec["part_threshold"]:
            return MapLocation(None, float(best), second, minimum,
                               "地图局部地形被遮挡或不一致，不能确认目标")
        px, py = self.spec["point_in_template"]
        return MapLocation((float(left+x+px), float(top+y+py)), float(best), second, minimum)
