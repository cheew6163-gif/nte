"""任务注册表：各任务可分别提供自己的流程和识别器。"""
from dataclasses import dataclass
from typing import Callable
from .core import KaciTask
from .nte999 import Detector999, Nte999Loop
from .vision import Detector


@dataclass(frozen=True)
class TaskSpec:
    name: str
    task_factory: Callable | None
    detector_factory: Callable


TASKS = {
    "kaci": TaskSpec(KaciTask.name, KaciTask, lambda resources: Detector(resources / "assets")),
    # 999夜由 runtime 直接驱动固定时间流程，不再使用逐帧任务状态机。
    "nte999": TaskSpec(Nte999Loop.name, None,
                       lambda resources: Detector999(resources / "assets999")),
}
