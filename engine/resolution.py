"""一次效果结算的生命周期上下文（ResolutionContext）。

## 它是什么

一次效果结算，在引擎里表现为一串嵌套调用：

    顶层 action
      └─ Effect（如 杀伐 命中）
           └─ State Mutation（扣血 / 加盾 / 改血限 …）
                └─ Trigger（失去生命 / 伤害落地 / 实体死亡 …）
                     └─ Effect（血债、再生、焦黑发丝 …）
                          └─ …（可继续嵌套）

`ResolutionContext` 就是**这条链的记账本**：谁在什么时候进入结算、嵌套多深、
本次结算一共产生了多少个效果、出问题时的终止原因是什么。

## 它不是什么（刻意划定的边界）

* 不是万能 Context：**不含** GameEngine、AI、UI、数据库、RuleRepository、
  配置、网络。只有结算本身需要的计数与（可选的）链记录。
* 不参与规则判定：任何机制/道纹/法术**不得**读它来决定数值或顺序。
  它只服务于两件事——**保险丝**与**可追踪性**（trace）。
* 不是新的事件总线：触发分发仍在 `CombatHookManager` / `TriggerBus`，
  事件事实仍在 `state.combat_events`。这里只记「结算到第几层」。

## 为什么需要它

1. **保险丝只有一处**：原来只有 `_apply_hostile_damage` 有深度计数，
   其余汇点（回复/代价/状态/命零/触发法术/怪物阶段）靠语义性再入保护收敛，
   没有**广度型**保护——深度不涨、数量爆炸的形态无人拦。
2. **终止原因不可追踪**：「为什么这个怪没死」目前只能靠加 print。
3. **阈值必须由实测决定**，不能拍脑袋。实测（`sim/effect_chain_audit.py depth`，
   27 局）：
     * 最大嵌套深度 **5**（TriggerBus.dispatch），≈ 阈值的 1/13；
     * 单次顶层 action 内效果汇点调用总数峰值 **45**，≈ 预算的 1/44。
   阈值取到这么宽，是为了**保证它永远不会改变任何现有战斗结果**——
   只在真的出现失控时截断成一次可诊断的异常。

## 成本

默认路径只做几个整数自增/自减（`enter`/`leave`），不建对象、不分配列表；
仅当 `tracing=True` 时才记录 `ResolutionFrame`。性能回归见报告 §5。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Optional


class ResolutionDepthError(RecursionError):
    """嵌套深度超过保险丝——正常规则不可能达到，出现即为失控（如 A→B→A 循环）。"""


class ResolutionBudgetError(RuntimeError):
    """一次行动内的效果总数超过预算——深度不涨但数量爆炸的失控形态。"""


@dataclass
class ResolutionFrame:
    """链上的一帧（只在 tracing 打开时创建）。"""

    seq: int
    depth: int
    kind: str
    label: str
    parent: Optional[int]
    #: 诊断备注（如 `hp 10→4`），由 `ResolutionContext.note()` 写入。
    note: str = ""


#: 效果种类：**观测用标签**，不是行为开关。汇点按这个粒度记账。
KIND_EFFECT = "effect"
KIND_DAMAGE = "damage"
KIND_HEAL = "heal"
KIND_COST = "cost"
KIND_STATUS = "status"
KIND_SHIELD = "shield"
KIND_MUTATION = "mutation"
KIND_DEATH = "death"
KIND_REVIVE = "revive"
KIND_SPLIT = "split"
KIND_EVOLVE = "evolve"
KIND_BLOOD_LIMIT = "blood_limit"
KIND_TRIGGER = "trigger"
KIND_PHASE = "phase"
KIND_OTHER = "other"


class ResolutionContext:
    """一个 CombatEngine 一份：记录效果结算的嵌套与预算。

    线程/引擎隔离：本对象不跨引擎共享，也不含全局状态。
    """

    #: 嵌套深度上限。既有 `CombatEngine.MAX_EFFECT_CHAIN_DEPTH` 与之同义
    #: （实测峰值 5，留 12 倍余量）。
    MAX_DEPTH = 64
    #: 单次顶层行动内的效果总数上限（实测峰值 45，留 40 倍余量）。
    MAX_EFFECTS = 2000
    #: trace 保留的**已结束**帧上限（环形丢弃）。上限存在的意义是：
    #: 长局/长时间挂着 trace 也不会把内存吃光（需求：不得大量驻留对象）。
    MAX_HISTORY = 500

    def __init__(self, *, tracing: bool = False):
        self._depth = 0
        self._effects = 0
        self._seq = 0
        self._chain: list[ResolutionFrame] = []
        self._tracing = tracing
        self._trip_reason: str = ""
        self._action: str = ""
        #: 本次结算的临时数据（只在 tracing 打开时使用；效果自身的小账本）。
        self.scratch: dict[str, Any] = {}
        #: 已处理过的触发标签（供诊断「这个 trigger 为什么没生效」）。
        self.seen_triggers: list[str] = []
        #: 已结束的帧（**只在 tracing 打开时**累积；环形，最多 MAX_HISTORY 条）。
        self.history: list[ResolutionFrame] = []

    # ------------------------------------------------------------ 生命周期

    def begin_action(self, action: str = "", params: Any = None) -> None:
        """一次**顶层行动**开始：清空计数与链。

        注意：预演内部也会走 `_execute_action_core`，但预演是沙盒执行，它的
        context 状态由 `engine/sandbox.py` 保存/恢复（见 snapshot/restore）。
        """
        self._depth = 0
        self._effects = 0
        self._seq = 0
        self._chain.clear()
        self._trip_reason = ""
        self.seen_triggers.clear()
        self.scratch.clear()
        self._action = action

    def end_action(self) -> None:
        """一次顶层行动结束：只保留终止原因（链本身留给 trace 快照）。"""
        self._action = ""

    def clear_history(self) -> None:
        """丢弃已结束的帧（trace 数据的唯一保留点是 `history`）。"""
        self.history.clear()

    @property
    def action(self) -> str:
        return self._action

    # ------------------------------------------------------------ 进出结算

    def enter(self, kind: str, label: str = "") -> Optional[ResolutionFrame]:
        """进入一次效果结算；返回 token（未开 trace 时为 None）交给 `leave`。"""
        depth = self._depth + 1
        if depth > self.MAX_DEPTH:
            self._trip_reason = f"深度 {depth} 超过 {self.MAX_DEPTH}"
            raise ResolutionDepthError(
                f"效果链嵌套超过 {self.MAX_DEPTH} 层（当前 {kind}"
                f"{'：' + label if label else ''}），疑似循环触发；"
                f"链：{self.describe_chain()}")
        effects = self._effects + 1
        if effects > self.MAX_EFFECTS:
            self._trip_reason = f"效果数 {effects} 超过 {self.MAX_EFFECTS}"
            raise ResolutionBudgetError(
                f"单次行动内效果数超过 {self.MAX_EFFECTS}"
                f"（当前 {kind}{'：' + label if label else ''}），疑似失控扩散；"
                f"链：{self.describe_chain()}")
        self._depth = depth
        self._effects = effects
        if not self._tracing:
            return None
        self._seq += 1
        frame = ResolutionFrame(
            seq=self._seq, depth=depth, kind=kind, label=label,
            parent=self._chain[-1].seq if self._chain else None,
        )
        self._chain.append(frame)
        return frame

    def leave(self, token: Optional[ResolutionFrame] = None) -> None:
        """离开一次效果结算（必须与 `enter` 成对，用 try/finally 保证）。

        trace 打开时，把这一帧**带备注地**归档到 `history`：帧一旦结束就不再
        参与判定，只作为「事情是怎么发生的」的证据。归档是环形写入，
        上限 `MAX_HISTORY`，因此长时间开着 trace 也不会无限增长。
        """
        if self._depth > 0:
            self._depth -= 1
        if token is not None:
            if self._chain and self._chain[-1] is token:
                self._chain.pop()
            self.history.append(token)
            if len(self.history) > self.MAX_HISTORY:
                del self.history[:len(self.history) - self.MAX_HISTORY]

    # ------------------------------------------------------------ 观测

    @property
    def depth(self) -> int:
        """当前嵌套深度（未进入任何结算时为 0）。"""
        return self._depth

    @property
    def effect_count(self) -> int:
        """本次行动累计的效果次数。"""
        return self._effects

    @property
    def tracing(self) -> bool:
        return self._tracing

    def set_tracing(self, enabled: bool) -> None:
        self._tracing = bool(enabled)
        if not enabled:
            self._chain.clear()

    @property
    def trip_reason(self) -> str:
        """保险丝触发原因（未触发时为空串）。"""
        return self._trip_reason

    def chain(self) -> list[ResolutionFrame]:
        """当前结算链快照（未开 trace 时为空）。"""
        return list(self._chain)

    def describe_chain(self) -> str:
        """人类可读的链描述（错误信息与 trace 用）。"""
        if not self._chain:
            return "(未开启 trace)" if not self._tracing else "(空)"
        return " → ".join(f"{f.kind}{':' + f.label if f.label else ''}"
                          for f in self._chain[-8:])

    def annotate(self, key: str, value: Any) -> None:
        """记一条结算期临时数据（**只在 trace 打开时生效**，默认零开销）。"""
        if self._tracing:
            self.scratch[key] = value

    def note(self, text: str) -> None:
        """给**当前最内层帧**追加一条诊断备注（如 `hp 10→4`）。

        只在 tracing 打开时生效；效果代码可以放心地把它写在结算路径上
        ——关闭时它只是一次布尔判断。
        """
        if self._tracing and self._chain:
            frame = self._chain[-1]
            frame.note = f"{frame.note} {text}".strip() if frame.note else text

    def describe_trace(self, limit: int = 60) -> str:
        """把已结束的帧渲染成一条因果链（trace 关闭时返回提示串）。

        每行形如：

            [3] damage 赌鬼 3   hp 10→7        ← 状态变化
            [4] death  命零 赌鬼
        """
        if not self._tracing:
            return "(trace 未开启：game.combat.resolution.set_tracing(True))"
        if not self.history:
            return "(无结算记录)"
        lines = []
        # 帧是结束（leave）时才归档的，所以按 seq 排序才是「发生顺序」。
        for frame in sorted(self.history[-limit:], key=lambda f: f.seq):
            line = f"[{frame.seq}] {'  ' * (frame.depth - 1)}{frame.kind}"
            if frame.label:
                line += f" {frame.label}"
            if frame.note:
                line += f"   {frame.note}"
            lines.append(line)
        return "\n".join(lines)

    def note_trigger(self, label: str) -> None:
        """记一次触发处理（trace/诊断用；默认也要记账，成本是一个 append）。"""
        if self._tracing and len(self.seen_triggers) < 512:
            self.seen_triggers.append(label)

    # ------------------------------------------------------------ 沙盒

    def snapshot(self) -> tuple:
        """给沙盒用的状态快照（进入预演前调用）。

        只保存标量与链长度：链帧本身是诊断数据，预演不需要把它带出来。
        """
        return (self._depth, self._effects, self._seq, len(self._chain),
                self._trip_reason, self._action, len(self.history))

    def restore(self, token: tuple) -> None:
        """按快照还原（退出预演时调用）——原地恢复，身份不变。"""
        (self._depth, self._effects, self._seq, chain_len,
         self._trip_reason, self._action, history_len) = token
        del self._chain[chain_len:]
        del self.history[history_len:]


# ------------------------------------------------------------------ 统一入口写法

def resolution_of(target: Any) -> Optional["ResolutionContext"]:
    """从「引擎」或「战斗管理器」解析出结算上下文；拿不到就返回 None（跳过记账）。

    为什么要这个函数：结算入口分布在不同层——`GameEngine`（events/api）、
    `CombatEngine`（combat/机制分片），有些工具函数只拿到其中之一。
    统一在这里解析，避免每个入口各写一遍 `getattr(getattr(x, "combat", None), ...)`。
    纯 `GameState` / 单测对象上没有引擎时返回 None，记账自动降级为「无操作」。
    """
    if target is None:
        return None
    ctx = getattr(target, "resolution", None)
    if isinstance(ctx, ResolutionContext):
        return ctx
    combat = getattr(target, "combat", None)
    ctx = getattr(combat, "resolution", None)
    return ctx if isinstance(ctx, ResolutionContext) else None


def note_delta(target: Any, field: str, before: Any, after: Any) -> None:
    """把一次状态变化记到当前帧上（`hp 10→4`）；无变化或无 trace 时是空操作。"""
    if before == after:
        return
    ctx = resolution_of(target)
    if ctx is not None:
        ctx.note(f"{field} {before}→{after}")


@contextmanager
def resolution_frame(target: Any, kind: str, *parts: Any):
    """开一个结算帧：`with resolution_frame(self, KIND_EFFECT, "杀伐", target.name):`。

    这是**唯一**的开帧写法（入口与汇点都用它），保证进出成对、异常也收尾。
    trace 关闭时 label 根本不构造（parts 只在开启时才 join）。
    """
    ctx = resolution_of(target)
    if ctx is None:
        yield None
        return
    token = ctx.enter(kind, " ".join(str(p) for p in parts) if ctx.tracing else "")
    try:
        yield token
    finally:
        ctx.leave(token)
