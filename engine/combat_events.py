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
    STATUS_APPLIED = "status_applied"
    STATUS_EXPIRED = "status_expired"
    ENTITY_RETREATED = "entity_retreated"
    ENTITY_DIED = "entity_died"
    ROUND_END = "round_end"
    BATTLE_END = "battle_end"


@dataclass
class CombatEvent:
    event_type: CombatEventType
    battle_no: int = 0
    round_no: int = 0
    actor_name: str = ""
    target_name: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "battle_no": self.battle_no,
            "round_no": self.round_no,
            "actor_name": self.actor_name,
            "target_name": self.target_name,
            "data": self.data,
            "timestamp": self.timestamp,
        }
