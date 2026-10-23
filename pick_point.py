"""取点工具：在截图上左键点击，打印 4K 像素坐标与 1499x843 参考坐标。

用法：
    python pick_point.py --image "屏幕截图.png"
    python pick_point.py --capture          # 直接抓取当前游戏客户区
    python pick_point.py --capture --admin  # 仅提示：需要时用管理员终端运行

退出：按 q 或 Esc。右键点击可撤销上一个点（仅界面显示，不影响输出）。
"""
from pathlib import Path
import argparse
import logging
import sys

import cv2
import numpy as np

from yihuan.vision import read_image

BASE = (1499, 843)


def grab_game():
    from yihuan.settings import load_config
    from yihuan.windows import Capture, WinApi, enable_dpi_awareness
    enable_dpi_awareness()
    config = load_config()
    api = WinApi()
    window = api.find_game(config)
    logging.getLogger("yihuan").info("窗口：%s %dx%d", window.title, window.width, window.height)
    capture = Capture(api, window, config, logging.getLogger("yihuan"))
    try:
        frame = capture.grab()
    finally:
        capture.close()
    if frame is None:
        raise RuntimeError("截图失败，请确认游戏在前台且分辨率为 3840x2160")
    return frame


def main():
    parser = argparse.ArgumentParser(description="坐标取点工具")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--image", type=Path, help="从图片取点")
    group.add_argument("--capture", action="store_true", help="抓取当前游戏客户区")
    parser.add_argument("--max-width", type=int, default=1600, help="预览窗口最大宽度")
    args = parser.parse_args()

    if args.image:
        image = read_image(args.image)
    elif args.capture:
        image = grab_game()
    else:
        parser.error("需要 --image 或 --capture")

    height, width = image.shape[:2]
    scale = min(1.0, args.max_width / width)
    shown = cv2.resize(image, (int(width * scale), int(height * scale)),
                       interpolation=cv2.INTER_AREA)
    view = shown.copy()
    window = "pick point  (left click = print, q = quit)"

    def redraw():
        cv2.imshow(window, view)

    def on_mouse(event, x, y, flags, _):
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        gx, gy = int(round(x / scale)), int(round(y / scale))
        bx, by = gx * BASE[0] / width, gy * BASE[1] / height
        print(f"4K=({gx},{gy})   base1499=({bx:.1f},{by:.1f})", flush=True)
        cv2.circle(view, (x, y), 6, (0, 0, 255), 2)
        cv2.putText(view, f"{gx},{gy}", (x + 8, y - 8), cv2.FONT_HERSHEY_SIMPLEX,
                    .6, (0, 0, 255), 2)
        redraw()

    cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(window, on_mouse)
    redraw()
    print(f"图像 {width}x{height}，预览缩放 {scale:.3f}")
    print("左键取点并打印坐标；按 q/Esc 退出。")
    while True:
        key = cv2.waitKey(20) & 0xFF
        if key in (27, ord("q")):
            break
    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    sys.exit(main())
