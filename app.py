"""正常启动界面；提供只读识图和有界的单步原生输入检查。"""
from pathlib import Path
import argparse
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser(description="异环视觉助手")
    entry = parser.add_mutually_exclusive_group()
    entry.add_argument("--check-image", type=Path, help="只识别图片，不发送鼠标键盘输入")
    entry.add_argument("--input-check", choices=("inspect", "desktop", "game"),
                       help="只读环境报告 / 桌面靶窗 / 游戏单步检查，不运行循环")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--move-only", action="store_true", help="只移动并核对，默认行为")
    action.add_argument("--click", action="store_true", help="明确允许单次左键点击")
    parser.add_argument("--target", choices=("go", "match"), default="go", help="游戏单步目标，默认前往")
    parser.add_argument("--timeout", type=float, default=15, help="单步检查上限1–120秒")
    parser.add_argument("--start-delay", type=float, default=2, help="单步检查前等待0–10秒")
    parser.add_argument("--report-dir", type=Path, help="单步诊断输出目录，默认diagnostics")
    parser.add_argument("--task", choices=("kaci", "nte999"), default="kaci",
                        help="--check-image 使用的任务，默认 kaci")
    args = parser.parse_args()
    if not args.input_check and (args.move_only or args.click or args.report_dir):
        parser.error("--move-only、--click、--report-dir需要--input-check")
    if args.input_check == "inspect" and args.click:
        parser.error("inspect只读，不支持--click")
    if args.check_image:
        from yihuan.settings import RESOURCE_DIR
        from yihuan.tasks import TASKS
        from yihuan.vision import read_image
        detector = TASKS[args.task].detector_factory(RESOURCE_DIR)
        observation = detector.detect(read_image(args.check_image))
        print(json.dumps({"task": args.task, "scene": observation.scene.value,
                          "scores": observation.scores, "targets": observation.targets,
                          "flags": observation.flags}, ensure_ascii=False, indent=2))
        return
    if os.name != "nt":
        raise SystemExit("实际运行需要Windows。可用--check-image进行离线图片诊断。")
    from yihuan.windows import enable_dpi_awareness, single_instance
    enable_dpi_awareness()
    if args.input_check:
        from yihuan.probe import ProbeOptions, run_input_check
        raise SystemExit(run_input_check(ProbeOptions(args.input_check, args.click, args.target,
                                                    args.timeout, args.start_delay, args.report_dir)))
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    try:
        instance_handle = single_instance()
        from yihuan.ui import App
        app = App(root)
        root.mainloop()
    except Exception as exc:
        root.withdraw()
        messagebox.showerror("异环视觉助手", str(exc), parent=root)
        root.destroy()
        raise


if __name__ == "__main__":
    main()
