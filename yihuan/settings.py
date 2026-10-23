from dataclasses import fields
from pathlib import Path
import json
import math
import re
import sys
from .core import Timings


RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
DATA_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else RESOURCE_DIR


def load_config(path=None):
    target = Path(path) if path else DATA_DIR / "config.json"
    if not target.exists():
        target = RESOURCE_DIR / "config.json"
    config = json.loads(target.read_text(encoding="utf-8-sig"))
    re.compile(config["window_title_regex"])
    if not config["window_title_regex"].strip():
        raise ValueError("游戏窗口标题规则不能为空")
    if (config["screen_width"], config["screen_height"]) != (3840, 2160):
        raise ValueError("首版仅支持3840×2160")
    for key, lo, hi in [("poll_interval", .05, 1), ("start_delay", 0, 10)]:
        value = config[key]
        if not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f"配置{key}超出范围")
    for key, lo, hi in [("stable_frames", 2, 5), ("max_loops", 0, 1000000),
                        ("dxcam_device_idx", 0, 20), ("dxcam_output_idx", 0, 20)]:
        if type(config[key]) is not int or not lo <= config[key] <= hi:
            raise ValueError(f"配置{key}需要{lo}至{hi}之间的整数")
    if config["capture_backend"] not in ("auto", "dxcam", "mss"):
        raise ValueError("capture_backend需要auto/dxcam/mss")
    # 兼容上一版的误命名；此时长现在明确用于图6到图7的S走位。
    timings = config["timings"]
    if "prepare_hold" in timings:
        if "s_hold" in timings and timings["s_hold"] != timings["prepare_hold"]:
            raise ValueError("s_hold与旧prepare_hold冲突，请只保留s_hold")
        timings["s_hold"] = timings.pop("prepare_hold")
    allowed = {f.name for f in fields(Timings)}
    if set(config["timings"]) != allowed:
        raise ValueError("timings字段缺失或不正确")
    for name, value in config["timings"].items():
        if not isinstance(value, (int, float)) or not math.isfinite(value) or not .01 <= value <= 3600:
            raise ValueError(f"时间参数{name}无效")
    if config["timings"]["settlement_timeout"] <= config["timings"]["settlement_min"]:
        raise ValueError("结算超时必须大于结算最短间隔")
    if timings["shift_hold"] >= timings["shift_interval"]:
        raise ValueError("Shift按住时间必须短于两次按下的间隔")
    if timings["battle_hold"] < 39 * timings["shift_interval"] + timings["shift_hold"]:
        raise ValueError("战斗时间不足以按当前间隔完成40次Shift")
    return config
