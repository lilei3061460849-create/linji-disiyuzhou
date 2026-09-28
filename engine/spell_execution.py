"""法术统一执行模型（Phase 1 引入）。

本模块定义法术系统五个核心概念：

- SpellDefinition  法术静态定义（名称/触发器/生命周期/程序体/元数据）
- SpellBinding     角色与法术的拥有+激活关系（挂 Entity）
- SpellCastRequest 一次施法的决策输入（caster 选了哪些 X/target/dodge）
- SpellExecution   一次施法的运行时状态机
- StepResult       单步执行的事实结果

设计原则（见 报告.md §Phase 1-6 规划）：
1. Definition/Binding/Request/Execution/Result 严格分离，不混用字段；
2. 所有法术类型（单道纹/瞬发/反应/道纹前/全局/自动）最终都通过
   SpellExecution.step() 驱动 _execute_single_daowen_step 完成结算；
3. 正常游戏流程的中断（法力不足/目标失效/道纹不可用/速度不足）用
   StepResult(status=interrupted/skipped/...) 表达，ValueError 只用于
   真正的程序员契约违例（非法 AST / 缺失字段 / refs 不一致等）。

Phase 1 落地范围：只建立数据结构 + 抽出单步执行核心 + 把三个 resolver
里重复的单步结算逻辑接到统一核心；控制栈/LoopStep/IfStep 执行期求值/
Binding 统一字段 等留到 Phase 3-5。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 触发器与生命周期
# ---------------------------------------------------------------------------

class TriggerType(str, Enum):
    """法术启动时机。Phase 2 会扩充 IMMEDIATE。"""
    BEFORE_DAMAGE_TAKEN = "受到伤害前"
    AFTER_DAMAGE_TAKEN = "受到伤害后"
    BEFORE_LIFE_LOST = "失去生命前"
    AFTER_LIFE_LOST = "失去生命后"
    TARGET_BEFORE_DAOWEN = "目标发动道纹前"
    BATTLE_START = "战始"
    BATTLE_END = "战终"
    ROUND_START = "回始"
    ROUND_END = "回终"
    SELF_TURN_END = "自身回合结束"
    ENEMY_ROUND_START = "敌回始"
    ENEMY_ROUND_END = "敌回终"
    DODGE = "闪避时"
    IMMEDIATE = "瞬发"  # Phase 2：cast(flow=...) 瞬发法术


class Lifecycle(str, Enum):
    """法术绑定存在多久。"""
    INSTANT = "instant"      # 本次施法完即弃，不建立 Binding
    BATTLE = "battle"        # 本场战斗有效，战终清
    PERMANENT = "permanent"  # 进入存档，跨战斗保留


class ExecutionStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class StepStatus(str, Enum):
    COMPLETED = "completed"    # 正常完成，效果已落地
    DODGED = "dodged"         # 目标闪避，未造成效果但 dodge 速度已扣
    SKIPPED = "skipped"       # 该步有条件跳过（如坠落目标不在飞行/血债无前序伤害），非法术中断
    INTERRUPTED = "interrupted"  # 本步失败导致**整个法术**应当中断（如法力不足/道纹不可用/契约违例）
    FAILED = "failed"         # 系统级失败（非法内部状态），不应该在正常游戏中出现


class InterruptReason(str, Enum):
    NONE = ""
    MANA_INSUFFICIENT = "mana_insufficient"
    TARGET_INVALID = "target_invalid"         # target=None / 已死亡 / 离场
    TARGET_UNTARGETABLE = "target_untargetable"  # 飞行等无法选中
    DAOWEN_UNUSABLE = "daowen_unusable"       # 封印/冷却/唯一已用
    SPEED_INSUFFICIENT = "speed_insufficient"
    INVALID_X = "invalid_x"                   # X<1 或 X 非整数
    INVALID_INPUT = "invalid_input"           # 提交结构非法（契约错，本应抛异常但反应路径需容错时用）
    # Phase 2：瞬发每一步都是一次"发动道纹"，与 use_daowen 同口径的正常中断
    SHARDS_INSUFFICIENT = "shards_insufficient"          # 赌命/消灾碎片代价付不起
    CASTER_DEAD = "caster_dead"                          # 施法者被「目标发动道纹前」反应命零
    TRIGGER_CHOICES_INVALID = "trigger_choices_invalid"  # 该步的反应提交在执行时已不合法


# ---------------------------------------------------------------------------
# 1. SpellDefinition
# ---------------------------------------------------------------------------

@dataclass
class SpellDefinition:
    """法术静态定义。不持有任何运行时状态。

    在 Phase 1 里，body 使用现有 AST 类型：
      - spell_dsl.ActionStep(daowen, target)
      - spell_dsl.IfStep(condition, then_steps, else_steps)
      - (daowen, role) 旧内置元组（通过 _step_daowen/_step_role 兼容访问）
    Phase 3/4 再扩展为 LoopStep + 参数节点。
    """
    name: str
    required_daowen: list[str]
    trigger: TriggerType
    lifecycle: Lifecycle
    body: list              # ActionStep / IfStep / (daowen, role) 元组
    rank: int = 1
    automatic: bool = False
    loop: bool = False      # Phase 1 保留：标记是否循环（真正的 LoopStep 在 Phase 4）
    effect_flow_text: str = ""   # 原始文本，用于 describe/debug
    custom_conditions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "required_daowen": list(self.required_daowen),
            "trigger": self.trigger.value,
            "lifecycle": self.lifecycle.value,
            "rank": self.rank,
            "automatic": self.automatic,
            "loop": self.loop,
            "effect_flow": self.effect_flow_text,
            "custom_conditions": list(self.custom_conditions),
        }


# ---------------------------------------------------------------------------
# 2. SpellBinding
# ---------------------------------------------------------------------------

@dataclass
class SpellBinding:
    """角色对某法术的拥有+激活状态。

    Phase 1 只定义数据结构，暂不替换 Entity.spells / armed_spells 两个字段
    （那是 Phase 5 存档兼容工作）。CombatEngine 在运行期会把现有两种来源
    （自创 spells 列表 / 内置 armed_spells 列表）都包装成 SpellBinding 提供给
    执行器，以便逐步切断下游代码对两个平列字段的依赖。
    """
    definition: SpellDefinition
    armed: bool = True       # 是否激活（=可在触发点被触发）
    source: str = "custom"   # "custom" / "builtin"，仅用于日志/兼容


# ---------------------------------------------------------------------------
# 3. SpellCastRequest —— 一次施法的决策输入
# ---------------------------------------------------------------------------

@dataclass
class StepRequest:
    """单步输入：玩家/AI/自动决策给出的 X/target/dodge。"""
    x: int
    target_ref: Optional[str] = None    # role=any 时由决策方指定；role=self/attacker 等自动解析时可为 None
    dodge: bool = False
    dodge_relic_target_ref: Optional[str] = None  # 闪避消耗遗物（兼容现有字段）
    extra: dict[str, Any] = field(default_factory=dict)  # 预留 y 等多参数


@dataclass
class SpellCastRequest:
    """一次施法的决策输入。

    对于反应/全局/自动法术，prepare 返回候选后由调用方/自动决策器组装此对象。
    对于瞬发法术，玩家在 cast(flow=...) 时直接给出 steps 列表。

    Phase 1 里 cycles 字段仍保留——因为循环由执行器接管在 Phase 4，
    Phase 1-3 仍沿用"调用方预展开 cycles"结构，但每个 cycle 的每一步都通过
    统一的 StepResult 路径执行。
    """
    use: bool = True
    cycles: list[list[StepRequest]] = field(default_factory=list)
    # 条件分支冻结签名（从旧 _engine_branch_signature 迁移过来，Phase 3 会重构为 DecisionSnapshot）
    branch_signature: Optional[list[list]] = None
    branch_owner: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 5. StepResult —— 单步执行结果
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    """单步执行后对事实的描述。不负责决定法术是否继续（那是 Execution 的责任）。"""
    status: StepStatus
    daowen: str = ""
    x: int = 0
    target_ref: Optional[str] = None
    target_name: str = ""
    cost_paid: int = 0
    mana_gained: int = 0
    execution: Optional[dict] = None   # apply_daowen_effect 返回值（伤害/治疗/状态 等）
    reason: InterruptReason = InterruptReason.NONE
    detail: str = ""                   # 人类可读原因（如"目标已失效""法力不足"）

    def to_log(self) -> dict:
        """转换为现有日志 dict 格式，保持对外 API 兼容。"""
        if self.status == StepStatus.COMPLETED:
            return {"daowen": self.daowen, "x": self.x, "target": self.target_name,
                    "execution": self.execution}
        if self.status == StepStatus.DODGED:
            return {"daowen": self.daowen, "target": self.target_name, "dodged": True}
        if self.status == StepStatus.SKIPPED:
            return {"daowen": self.daowen, "skipped": self.detail or True}
        # INTERRUPTED / FAILED
        return {"daowen": self.daowen, "interrupted": self.reason.value,
                "detail": self.detail}


# ---------------------------------------------------------------------------
# 4. SpellExecution —— 运行时状态机
# ---------------------------------------------------------------------------

class SpellExecution:
    """一次具体施法的运行时状态。

    Phase 1 实现：给定已展开 flat_steps + cycles（Phase 1 仍由外部准备），
    逐 cycle 逐 step 调用 _execute_single_daowen_step，结果累积到 self.results，
    遇 INTERRUPTED/FAILED 立刻置 status 并停机；completed 则走完。

    Phase 3/4 会把控制流（IfStep/LoopStep）接管进来，届时外部不再需要提前
    flatten 或展开 cycles，Execution 自己推进 control_stack。
    """

    def __init__(self, engine, definition: SpellDefinition, caster,
                 attacker, refs: dict[str, Any], request: SpellCastRequest,
                 flat_steps: list, *, trigger_label: str = "",
                 target_resolver=None, skip_predicate=None,
                 on_step=None, before_step=None):
        self.engine = engine
        self.definition = definition
        self.caster = caster
        self.attacker = attacker
        self.refs = refs
        self.request = request
        self.flat_steps = flat_steps
        self.trigger_label = trigger_label
        self.target_resolver = target_resolver
        self.skip_predicate = skip_predicate
        self.on_step = on_step   # callback(StepResult) 在每步结束后调用，供路径特定状态更新
        # Phase 2：每步进入单步核心之前的前置环节（由调用方注入，Execution 本身不含规则）。
        #   before_step(step, entry, execution) -> (early: StepResult|None, extra: dict)
        # early 非 None 时本步不进核心，直接以 early 作为本步结果（interrupted 则停机）；
        # extra 记入 step_extras（如该步触发的「目标发动道纹前」反应日志）。
        # 瞬发法术用它让每一步与 use_daowen 走同一套"发动道纹"环节。
        self.before_step = before_step
        self.step_extras: list[dict] = []

        self.status: ExecutionStatus = ExecutionStatus.RUNNING
        self.interrupt_reason: InterruptReason = InterruptReason.NONE
        self.interrupt_detail: str = ""
        self.results: list[StepResult] = []
        self.cycle_index: int = 0
        self.step_index: int = 0

    # -- 快捷访问 --
    @property
    def is_running(self) -> bool:
        return self.status == ExecutionStatus.RUNNING

    def _stop(self, status: ExecutionStatus, reason: InterruptReason, detail: str = "") -> None:
        self.status = status
        self.interrupt_reason = reason
        self.interrupt_detail = detail

    # -- 主驱动 --
    def run_all(self) -> list[StepResult]:
        """Phase 1：按 cycles 顺序执行；遇中断即停。返回完整 StepResult 列表。"""
        for self.cycle_index, cycle in enumerate(self.request.cycles, 1):
            if not self.is_running:
                break
            if len(cycle) != len(self.flat_steps):
                self._stop(ExecutionStatus.FAILED, InterruptReason.INVALID_INPUT,
                           f"cycle长度{len(cycle)}与flat_steps长度{len(self.flat_steps)}不一致")
                break
            for self.step_index, (entry, step) in enumerate(zip(cycle, self.flat_steps)):
                if not self.is_running:
                    break
                extra: dict = {}
                if self.before_step is not None:
                    early, extra = self.before_step(step, entry, self)
                    extra = extra or {}
                    if early is not None:
                        self.results.append(early)
                        self.step_extras.append(extra)
                        if self.on_step is not None:
                            self.on_step(early)
                        if early.status in (StepStatus.INTERRUPTED, StepStatus.FAILED):
                            self._stop(
                                ExecutionStatus.INTERRUPTED if early.status == StepStatus.INTERRUPTED
                                else ExecutionStatus.FAILED,
                                early.reason, early.detail,
                            )
                            break
                        continue
                # entry 可能是 StepRequest 或 dict；_execute_single_daowen_step 都能处理
                result = self.engine._execute_single_daowen_step(
                    definition=self.definition,
                    step=step,
                    entry=entry,
                    caster=self.caster,
                    attacker=self.attacker,
                    refs=self.refs,
                    trigger_label=self.trigger_label,
                    target_resolver=self.target_resolver,
                    skip_predicate=self.skip_predicate,
                )
                self.results.append(result)
                self.step_extras.append(extra)
                if self.on_step is not None:
                    self.on_step(result)
                if result.status == StepStatus.INTERRUPTED or result.status == StepStatus.FAILED:
                    self._stop(
                        ExecutionStatus.INTERRUPTED if result.status == StepStatus.INTERRUPTED
                        else ExecutionStatus.FAILED,
                        result.reason, result.detail,
                    )
                    break
                # SKIPPED/DODGED/COMPLETED 继续下一步
        if self.status == ExecutionStatus.RUNNING:
            self.status = ExecutionStatus.COMPLETED
        return self.results

    def logs(self) -> list[dict]:
        """转换为旧 API 返回的 log 列表（对调用方透明）。"""
        out = []
        for r in self.results:
            entry = r.to_log()
            if self.definition.name:
                entry["spell"] = self.definition.name
            if self.cycle_index:
                entry["cycle"] = self.cycle_index
            out.append(entry)
        return out

    def step_summaries(self) -> list[dict]:
        """可 JSON 序列化的逐步结果摘要（瞬发 cast 的返回值使用）。"""
        out = []
        for idx, r in enumerate(self.results):
            extra = self.step_extras[idx] if idx < len(self.step_extras) else {}
            out.append({
                "step": idx + 1, "status": r.status.value, "reason": r.reason.value or None,
                "daowen": r.daowen, "x": r.x, "target": r.target_name or None,
                "cost_paid": r.cost_paid, "mana_gained": r.mana_gained,
                "detail": r.detail or None,
                "trigger_spell_logs": extra.get("trigger_spell_logs", []),
            })
        return out
