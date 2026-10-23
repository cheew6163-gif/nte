"""与截图和Windows无关的任务状态机；输入发出后等待画面确认。"""
from dataclasses import dataclass, field
from enum import Enum
import logging


BATTLE_CONFIRM_AFTER = 23.0  # 从开始匹配输入完成起算；不阻止提前结算。


class Scene(str, Enum):
    UNKNOWN = "未识别"
    ACTIVITY = "活动页"
    MATCH = "匹配页"
    BATTLE = "战场"
    VICTORY = "胜利结算"
    DEFEAT = "失败结算"
    DIALOG = "离开确认框"


def is_unknown(scene):
    """兼容任务自定义场景枚举：按“未识别”值判断。"""
    return getattr(scene, "value", scene) == Scene.UNKNOWN.value


class Phase(str, Enum):
    READY = "等待活动页"
    MATCH = "等待前往生效 / 匹配页"
    LOAD = "等待战场"
    WATCH = "战场观察"
    BATTLE_DIALOG = "等待退场确认框"
    SETTLEMENT = "等待结算（至少1秒）"
    RESULT_DIALOG = "等待结算离开确认框"
    RETURN = "等待返回活动页（预计12秒）"
    DONE = "完成指定轮数"


class Action(str, Enum):
    GO = "点击前往"
    MATCH = "点击开始匹配"
    ESC_BATTLE = "战场按Esc"
    CONFIRM_BATTLE = "确认退出战场"
    ESC_RESULT = "结算按Esc"
    CONFIRM_RESULT = "确认离开结算"


@dataclass(frozen=True)
class Observation:
    scene: Scene
    scores: dict[str, float] = field(default_factory=dict)
    targets: dict[str, tuple[float, float]] = field(default_factory=dict)
    flags: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Intent:
    action: Action
    expected: Scene
    phase: Phase

    @property
    def target(self):
        return {Action.GO: "go", Action.MATCH: "match",
                Action.CONFIRM_BATTLE: "confirm", Action.CONFIRM_RESULT: "confirm"}.get(self.action)


@dataclass(frozen=True)
class Timings:
    battle_watch: float = 5.0  # 无配置时的默认值；正式运行由config.json覆盖。
    settlement_min: float = 1.0
    activity_timeout: float = 20.0
    match_timeout: float = 15.0
    load_timeout: float = 90.0
    dialog_timeout: float = 8.0
    settlement_timeout: float = 20.0
    return_timeout: float = 45.0
    unknown_timeout: float = 15.0
    # 999夜任务专用；咔呲任务不读取这些字段。
    w_hold: float = 3.0
    a_hold: float = 1.8
    s_hold: float = 2.0  # 图6到图7的后退走位；松开后才开始战斗。
    battle_hold: float = 80.0
    shift_interval: float = 2.0
    shift_hold: float = 0.08
    refresh_gap: float = 0.8
    map_timeout: float = 15.0
    menu_timeout: float = 15.0
    teleport_timeout: float = 90.0
    move_unknown: float = 3.0


class TaskTimeout(RuntimeError):
    pass


class KaciTask:
    name = "咔呲 · 自动结算循环"

    def __init__(self, timings=None, max_loops=0, now=0.0, logger=None):
        self.timings = timings or Timings()
        self.max_loops = max_loops
        self.log = logger or logging.getLogger("yihuan")
        self.phase = Phase.READY
        self.entered = now
        self.completed = 0
        self.round_started = False
        self.battle_since = None
        self.watch_due_logged = False
        self.result_not_before = 0.0
        self.unknown_since = None
        self.last_scene = Scene.UNKNOWN
        self.go_attempts = 0
        self.go_sent_at = None

    @property
    def done(self):
        return self.phase == Phase.DONE

    @property
    def phase_text(self):
        if self.phase == Phase.LOAD:
            return f"等待战场（匹配后{BATTLE_CONFIRM_AFTER:g}秒起确认）"
        if self.phase == Phase.WATCH:
            return f"战场观察{self.timings.battle_watch:g}秒"
        return self.phase.value

    def _phase(self, phase, now):
        self.phase, self.entered = phase, now
        self.log.info("第%d轮｜%s", self.completed + 1, self.phase_text)

    def check_timeout(self, now):
        limits = {
            Phase.READY: self.timings.activity_timeout, Phase.MATCH: self.timings.match_timeout,
            Phase.LOAD: self.timings.load_timeout, Phase.WATCH: self.timings.battle_watch + 15,
            Phase.BATTLE_DIALOG: self.timings.dialog_timeout,
            Phase.SETTLEMENT: self.timings.settlement_timeout,
            Phase.RESULT_DIALOG: self.timings.dialog_timeout, Phase.RETURN: self.timings.return_timeout,
        }
        if self.phase in limits and now - self.entered >= limits[self.phase]:
            if self.phase == Phase.MATCH and self.last_scene == Scene.ACTIVITY:
                raise TaskTimeout(f"前往点击后仍停在活动页（已尝试{self.go_attempts}次），已暂停；"
                                  "请查看日志中的鼠标位置和诊断文件")
            raise TaskTimeout(f"{self.phase_text}超时，已暂停；请查看日志和诊断截图")
        # 加载/返回期间黑场和无HUD是正常过渡，使用各自的阶段超时。
        if (self.phase not in (Phase.LOAD, Phase.RETURN) and self.unknown_since is not None
                and now - self.unknown_since >= self.timings.unknown_timeout):
            raise TaskTimeout("持续无法识别当前界面，已暂停；请查看诊断截图")

    def observe(self, observation, now):
        if self.done:
            return None
        self.last_scene = observation.scene
        self.check_timeout(now)
        scene = observation.scene
        if (self.phase == Phase.WATCH and self.battle_since is not None
                and now - self.battle_since >= self.timings.battle_watch
                and not self.watch_due_logged):
            self.watch_due_logged = True
            self.log.info("观察已满%.1f秒（实际%.2f秒）；当前识别=%s；退出图标=%s，Esc提示=%s；"
                          "战场画面通过动作前校验后才按Esc，提前结算仍优先",
                          self.timings.battle_watch, now - self.battle_since, scene.value,
                          observation.scores.get("escape"), observation.scores.get("escape_key"))
        if scene == Scene.UNKNOWN:
            if self.unknown_since is None:
                self.unknown_since = now
            return None
        self.unknown_since = None

        # 可能提前自动返回。只有已经点过匹配，才将返回活动页视为一轮结束。
        if scene == Scene.ACTIVITY and self.round_started:
            self.completed += 1
            self.round_started = False
            self.battle_since = None
            self.watch_due_logged = False
            self.result_not_before = 0.0
            self.log.info("第%d轮完成，已确认返回活动页", self.completed)
            self._phase(Phase.DONE if self.max_loops and self.completed >= self.max_loops else Phase.READY, now)
            return None

        # 对方可在载入或退场弹窗出现前先退出；胜负结算优先于战场退出。
        if (scene in (Scene.VICTORY, Scene.DEFEAT) and self.round_started
                and self.phase not in (Phase.RETURN, Phase.RESULT_DIALOG)
                and now >= self.result_not_before):
            return Intent(Action.ESC_RESULT, scene, self.phase)

        if self.phase == Phase.READY and scene == Scene.ACTIVITY:
            return Intent(Action.GO, scene, self.phase)
        # 只对仍清晰可见的“前往”补点一次。匹配、确认和Esc均不盲目重发。
        # 重试不延长15秒的总等待上限；动作前仍需最新截图再次校验。
        if (self.phase == Phase.MATCH and scene == Scene.ACTIVITY and self.go_attempts < 2
                and self.go_sent_at is not None and now - self.go_sent_at >= 3.0):
            return Intent(Action.GO, scene, self.phase)
        if self.phase == Phase.MATCH and scene == Scene.MATCH:
            self.log.info("已确认前往生效：匹配页已出现")
            return Intent(Action.MATCH, scene, self.phase)
        if (self.phase == Phase.LOAD and scene == Scene.BATTLE and now - self.entered >= BATTLE_CONFIRM_AFTER):
            self.battle_since = now
            self.watch_due_logged = False
            self.log.info("确认战场：匹配后%.2f秒；现在开始观察%.1f秒",
                          now - self.entered, self.timings.battle_watch)
            self._phase(Phase.WATCH, now)
        elif (self.phase == Phase.WATCH and scene == Scene.BATTLE
              and now - self.battle_since >= self.timings.battle_watch):
            return Intent(Action.ESC_BATTLE, scene, self.phase)
        elif self.phase == Phase.BATTLE_DIALOG and scene == Scene.DIALOG:
            return Intent(Action.CONFIRM_BATTLE, scene, self.phase)
        elif self.phase == Phase.RESULT_DIALOG and scene == Scene.DIALOG:
            return Intent(Action.CONFIRM_RESULT, scene, self.phase)
        return None

    def commit(self, intent, now):
        if intent.phase != self.phase:
            raise RuntimeError("过期动作被拒绝")
        next_phase = {
            Action.GO: Phase.MATCH, Action.MATCH: Phase.LOAD,
            Action.ESC_BATTLE: Phase.BATTLE_DIALOG, Action.CONFIRM_BATTLE: Phase.SETTLEMENT,
            Action.ESC_RESULT: Phase.RESULT_DIALOG, Action.CONFIRM_RESULT: Phase.RETURN,
        }[intent.action]
        if intent.action == Action.GO:
            if intent.phase == Phase.READY:
                self.go_attempts = 0
            self.go_attempts += 1
            self.go_sent_at = now
            self.log.info("前往输入已发送（第%d次），等待实际匹配页确认", self.go_attempts)
            if intent.phase == Phase.MATCH:
                return  # 重试保留第一次输入后的总超时起点。
        if intent.action == Action.MATCH:
            self.round_started = True
        if intent.action == Action.CONFIRM_BATTLE:
            self.result_not_before = now + self.timings.settlement_min
        self._phase(next_phase, now)


