import logging
from logging.handlers import RotatingFileHandler
import queue
import threading
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from .tasks import TASKS
from .runtime import Runner, Hotkeys
from .settings import DATA_DIR, load_config


class QueueLog(logging.Handler):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def emit(self, record):
        self.events.put(("log", self.format(record)))


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.shutdown = threading.Event()
        self.runner = Runner(self.events)
        self.closing = False
        root.title("异环 · 视觉助手 0.3")
        root.geometry("760x480")
        root.minsize(650, 400)
        root.configure(bg="#f4f6f8")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f4f6f8")
        style.configure("TLabel", background="#f4f6f8", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(16, 7))
        style.configure("Start.TButton", background="#217f69", foreground="white")
        style.configure("Stop.TButton", background="#a63f49", foreground="white")

        panel = ttk.Frame(root, padding=20)
        panel.pack(fill="both", expand=True)
        top = ttk.Frame(panel)
        top.pack(fill="x")
        ttk.Label(top, text="脚本").pack(side="left", padx=(0, 10))
        self.task_ids = list(TASKS)
        self.selection = ttk.Combobox(top, values=[TASKS[k].name for k in self.task_ids],
                                      state="readonly", width=28, font=("Microsoft YaHei UI", 10))
        self.selection.current(0)
        self.selection.pack(side="left")
        self.start_button = ttk.Button(top, text="开始", command=self.start, style="Start.TButton")
        self.start_button.pack(side="left", padx=(18, 8))
        self.stop_button = ttk.Button(top, text="停止", command=self.stop, style="Stop.TButton", state="disabled")
        self.stop_button.pack(side="left")

        self.status = tk.StringVar(value="就绪 · 咔呲停在活动页；999夜停在目标篝火旁")
        ttk.Label(panel, textvariable=self.status, font=("Microsoft YaHei UI", 12),
                  wraplength=690).pack(fill="x", pady=(20, 12))
        self.log_text = ScrolledText(panel, state="disabled", height=14, wrap="word",
                                    font=("Consolas", 10), bg="white", relief="flat", padx=10, pady=10)
        self.log_text.pack(fill="both", expand=True)
        ttk.Label(panel, text="G5 启动（游戏前台）  ·  左Ctrl 停止  ·  4K全屏  ·  默认无限循环\n"
                             "999夜：W 3秒→A 1.8秒→S走位2秒后松开→Shift战斗80秒，每2秒一次",
                  foreground="#5d6774").pack(anchor="w", pady=(12, 0))

        directory = DATA_DIR / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("yihuan")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
        file_log = RotatingFileHandler(directory / "assistant.log", maxBytes=2000000,
                                      backupCount=3, encoding="utf-8")
        file_log.setFormatter(formatter)
        queue_log = QueueLog(self.events)
        queue_log.setFormatter(formatter)
        logger.addHandler(file_log)
        logger.addHandler(queue_log)
        config = load_config()
        self.hotkeys = Hotkeys(self.runner, self.events, self.shutdown, config["window_title_regex"])
        self.hotkeys.start()
        root.protocol("WM_DELETE_WINDOW", self.close)
        logger.info("就绪。请关闭旧Lua循环；开始后助手会最小化并切回游戏")
        root.after(30, self.poll)

    def start(self):
        if self.closing or self.runner.busy:
            return
        task_id = self.task_ids[self.selection.current()]
        self.root.iconify()
        self.status.set("正在切回游戏…")
        self.selection.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        if not self.runner.start(task_id):
            self.root.deiconify()
            self.selection.configure(state="readonly")
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")

    def stop(self):
        self.runner.stop()
        self.status.set("正在停止并释放模拟按键…")

    def append_log(self, message):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 2000:
            self.log_text.delete("1.0", "201.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def poll(self):
        try:
            for _ in range(150):
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.append_log(value)
                elif kind == "start_request":
                    self.start()
                elif kind == "status":
                    self.status.set(value)
                elif kind == "done":
                    self.status.set(value)
                    self.selection.configure(state="readonly")
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    if not self.closing:
                        self.root.deiconify()
        except queue.Empty:
            pass
        if self.closing and not self.runner.busy:
            self.root.destroy()
            return
        self.root.after(30, self.poll)

    def close(self):
        self.closing = True
        self.shutdown.set()
        self.runner.stop()
        self.status.set("正在停止并关闭…")
