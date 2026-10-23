"""任务注册表：各任务可分别提供自己的流程和识别器。"""
from dataclasses import dataclass
from typing import Callable
from .core import KaciTask
from .nte999 import Detector999, Nte999Task
from .vision import Detector


@dataclass(frozen=True)
class TaskSpec:
    name: str
    task_factory: Callable
    detector_factory: Callable


TASKS = {
    "kaci": TaskSpec(KaciTask.name, KaciTask, lambda resources: Detector(resources / "assets")),
    "nte999": TaskSpec(Nte999Task.name, Nte999Task,
                       lambda resources: Detector999(resources / "assets999")),
}
