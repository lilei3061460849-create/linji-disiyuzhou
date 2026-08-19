"""
战斗事件总线数据模型（Event-Driven Combat Architecture）
用于解耦巨石战斗引擎与战报生成器，提供单一事实源不可变事件流。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Dict, List
import time


class CombatEventType(str, Enum):
    BATTLE_START = "battle_start"
    ROUND_START = "round_start"
    ACTION_DECLARED = "action_declared"
    DODGE_DECLARED = "dodge_declared"
    BEFORE_DAMAGE = "before_damage"
    DAMAGE_APPLIED = "damage_applied"
    HEAL_APPLIED = "heal_applied"
    # 血限变化是任务书要求的因果链中间跳（伤害 → 血限下降 → 依赖血限的效果），
    # 必须作为一等事件存在，否则这条链在事件流里是断的。
    BLOOD_LIMIT_CHANGED = "blood_limit_changed"
    STATUS_APPLIED = "status_applied"
    STATUS_EXPIRED = "status_expired"
    ENTITY_RETREATED = "entity_retreated"
    ENTITY_DIED = "entity_died"
    ROUND_END = "round_end"
    BATTLE_END = "battle_end"


@dataclass
class CombatEvent:
    """“发生了什么”。

    职责边界（不要与 EffectContext 互相替代）：
      * CombatEvent  —— 客观事实：什么时点、谁对谁、发生了哪一类变化、数值多少。
      * EffectContext —— 因果来源：为什么发生、来自哪个效果、父事件是谁。
    因此 CombatEvent 只“携带” EffectContext 的快照（self.ctx），不复制它的语义。
    """
    event_type: CombatEventType
    battle_no: int = 0
    round_no: int = 0
    actor_name: str = ""
    target_name: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    # EffectContext.to_dict() 的浅层快照；None 表示该事件没有可追溯来源。
    ctx: Optional[Dict[str, Any]] = None
    timestamp: float = field(default_factory=time.time)

    @property
    def event_id(self) -> Optional[str]:
        return (self.ctx or {}).get("event_id")

    @property
    def parent_event_id(self) -> Optional[str]:
        return (self.ctx or {}).get("parent_event_id")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "battle_no": self.battle_no,
            "round_no": self.round_no,
            "actor_name": self.actor_name,
            "target_name": self.target_name,
            "data": self.data,
            "ctx": self.ctx,
            "timestamp": self.timestamp,
        }
