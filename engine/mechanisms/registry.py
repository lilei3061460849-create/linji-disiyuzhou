"""Mechanism：机制声明与注册表。

机制 = 【什么时候】when + 【对谁】target + 【满足什么条件】condition +
       【做什么】effect + priority。机制定义全局唯一。

机制自身状态（state）必须按实体存放：entity → mechanism state。
复用现有 Entity 结构（entity._mechanism_states[机制名]），
不做 JilinMechanism.states[entity_id] 这类全局状态系统。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from .triggers import Phase, Trigger, TriggerContext


@dataclass
class Mechanism:
    name: str
    when: Trigger
    effect: Callable[[TriggerContext, list], Any] = None  # type: ignore[assignment]
    # 相位修正机制返回 {"amount": 新值} 或 int（新值）；事件机制返回值仅作记录。
    target: Any = None                # TargetSelector；None = 不产生目标（纯数值修正）
    condition: Optional[Callable[[TriggerContext], bool]] = None
    priority: int = 100               # 数字小先执行。与 CombatHook.priority 同一套语义，顺序即规则。
    needs_state: bool = False         # 需要按实体的机制自身状态时置 True

    def __post_init__(self):
        if self.effect is None:
            raise ValueError(f"机制[{self.name}]缺少 effect")

    def state_of(self, entity) -> dict:
        """本机制在该实体上的自身状态（惰性创建，按实体存放，不进入全局表）。

        仅 needs_state=True 的机制应使用；状态生命周期与实体一致，
        序列化兼容期内不会写入存档（机制状态需随存档走时另行设计）。
        """
        if entity is None:
            raise ValueError(f"机制[{self.name}]需要实体状态，但实体为 None")
        bucket = getattr(entity, "_mechanism_states", None)
        if bucket is None:
            bucket = {}
            entity._mechanism_states = bucket
        if self.name not in bucket:
            bucket[self.name] = {}
        return bucket[self.name]


class MechanismRegistry:
    """全局机制定义登记处。定义全局唯一；状态走实体（见 Mechanism.state_of）。"""

    def __init__(self):
        self._mechanisms: dict[str, Mechanism] = {}

    def register(self, mechanism: Mechanism) -> Mechanism:
        existing = self._mechanisms.get(mechanism.name)
        if existing is not None and existing is not mechanism:
            raise ValueError(f"机制[{mechanism.name}]重复注册")
        self._mechanisms[mechanism.name] = mechanism
        return mechanism

    def get(self, name: str) -> Optional[Mechanism]:
        return self._mechanisms.get(name)

    def names(self) -> list[str]:
        return sorted(self._mechanisms)

    def all(self) -> list[Mechanism]:
        return [self._mechanisms[n] for n in sorted(self._mechanisms)]

    def event_mechanisms(self) -> list[Mechanism]:
        """全部事件型机制定义，按 priority 升序（供战斗引擎订阅事件总线）。"""
        return sorted(
            (m for m in self._mechanisms.values() if m.when.kind == "event"),
            key=lambda m: m.priority,
        )

    def unregister(self, name: str) -> Optional[Mechanism]:
        """从登记处移除一个机制定义（返回被移除的机制；不存在返回 None）。"""
        return self._mechanisms.pop(name, None)

    def phase_mechanisms(self, phase: str) -> list[Mechanism]:
        """某相位的全部机制，按 priority 升序（同 priority 保持注册顺序）。"""
        return sorted(
            (m for m in self._mechanisms.values() if m.when.matches_phase(phase)),
            key=lambda m: m.priority,
        )


# 全局机制定义登记处（机制定义属于全局词汇；机制状态永远走实体，不进这里）。
MECHANISMS = MechanismRegistry()


class MechanismHookAdapter:
    """【迁移过渡代码，不得新增依赖】

    已迁移 Mechanism 在既有 CombatHookManager 分发路径上的执行壳。
    同一份机制只在此处执行一次：机制定义在声明层，执行仍走 Hook 层
    这一条既有分发路径——不引入第三条路径，也不会与旧 Hook 重复触发。

    MVP 只桥接相位 INCOMING_ADJUST（【加害】）。接线其它相位前，必须先确认
    对应分发方法在引擎里只有唯一调用点。
    """

    def __init__(self, mechanism: Mechanism):
        if mechanism.when.kind != "phase":
            raise ValueError(
                f"机制[{mechanism.name}]不是相位机制，不能挂到 Hook 分发路径")
        self.mechanism = mechanism
        self.priority = mechanism.priority  # 沿用机制声明的 priority（顺序即规则）

    def _context(self, target, amount, damage_type, source, state) -> TriggerContext:
        return TriggerContext(
            combat=None, state=state, phase=self.mechanism.when.key,
            target=target, source=source,
            amount=amount, damage_type=damage_type,
        )

    def on_incoming_adjust(self, target, amount, damage_type, source, state):
        mechanism = self.mechanism
        ctx = self._context(target, amount, damage_type, source, state)
        if mechanism.condition is not None and not mechanism.condition(ctx):
            return amount
        targets = mechanism.target.select(ctx) if mechanism.target is not None else []
        result = mechanism.effect(ctx, targets)
        if isinstance(result, dict):
            return result.get("amount", amount)
        if isinstance(result, (int, float)):
            return int(result)
        return amount
