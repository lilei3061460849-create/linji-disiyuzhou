"""
战斗声明式钩子系统（Combat Declarative Hook System）
彻底解耦 combat.py 中的硬编码条件分支，将遗物、道纹与法器重构为可独立插拔的生命周期 Listener。
"""
from __future__ import annotations
import math
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable
from .combat_events import CombatEvent, CombatEventType


@runtime_checkable
class CombatHook(Protocol):
    """战斗生命周期钩子协议"""

    def on_multiplier_adjust(self, target: Any, amount: int, damage_type: str, source: Optional[Any], state: Any) -> int:
        return amount

    def on_incoming_adjust(self, target: Any, amount: int, damage_type: str, source: Optional[Any], state: Any) -> int:
        return amount

    def on_before_damage(self, target: Any, amount: int, damage_type: str, attacker: Optional[Any], state: Any) -> Dict[str, Any]:
        return {}

    def on_after_damage(self, target: Any, actual_damage: int, shield_absorbed: int, detail: Dict[str, Any], attacker: Optional[Any], state: Any) -> Dict[str, Any]:
        return {}

    def on_dodge(self, entity: Any, state: Any) -> Dict[str, Any]:
        return {}

    def on_round_start(self, entity: Any, is_enemy_turn: bool, state: Any) -> Dict[str, Any]:
        return {}


class DragonBloodlineMultiplierHook:
    """龙族血脉：对非怪物造成伤害翻倍"""
    def on_multiplier_adjust(self, target: Any, amount: int, damage_type: str, source: Optional[Any], state: Any) -> int:
        if (source is not None and hasattr(state, "side_has") and state.side_has(source, "龙族血脉")
                and getattr(target, "entity_type", "") != "怪物" and damage_type != "代价"):
            return amount * 2
        return amount


class JiahaiHook:
    """加害：每次受到伤害+X（持续∞）"""
    def on_incoming_adjust(self, target: Any, amount: int, damage_type: str, source: Optional[Any], state: Any) -> int:
        if amount > 0 and damage_type != "代价" and hasattr(target, "has_status") and target.has_status("加害"):
            return amount + (target.get_status_value("加害") or 0)
        return amount


class LonglinHook:
    """龙鳞：每次受到伤害-X，最低为0（持续∞）"""
    def on_incoming_adjust(self, target: Any, amount: int, damage_type: str, source: Optional[Any], state: Any) -> int:
        if amount > 0 and damage_type != "代价" and hasattr(target, "has_status") and target.has_status("龙鳞"):
            val = target.get_status_value("龙鳞") or 0
            return max(0, amount - val)
        return amount


class BaolieHook:
    """爆裂：受到伤害前，攻击者失去等量生命，持续X（敌回终递减）"""
    def on_before_damage(self, target: Any, amount: int, damage_type: str, attacker: Optional[Any], state: Any) -> Dict[str, Any]:
        if (target and hasattr(target, "has_status") and target.has_status("爆裂")
                and attacker is not None and attacker is not target and amount > 0 and damage_type != "代价"):
            prev_hp = attacker.current_hp
            attacker.current_hp = max(0, attacker.current_hp - amount)
            reflect_amt = prev_hp - attacker.current_hp
            suppressed = (attacker.current_hp <= 0)
            if suppressed:
                attacker.is_alive = False
            return {
                "reflected": reflect_amt,
                "attacker_hp_after": attacker.current_hp,
                "suppressed": suppressed,
            }
        return {}


class BifenglingHook:
    """避风铃：每次闪避后获得3点格挡，当前速度归零时获得15点格挡"""
    def on_dodge(self, entity: Any, state: Any) -> Dict[str, Any]:
        if not entity or not hasattr(state, "side_has") or not state.side_has(entity, "避风铃"):
            return {}
        gained = 3
        entity.shield += 3
        zero_gained = 0
        if getattr(entity, "current_speed", 1) == 0:
            entity.shield += 15
            zero_gained = 15
        return {"shield_gained": gained, "zero_speed_shield": zero_gained, "total_shield": entity.shield}


class ShouyedengHook:
    """守夜灯：[敌回始]获得等同于[法限]50%的法力，该法力[敌回终]清空"""
    def on_round_start(self, entity: Any, is_enemy_turn: bool, state: Any) -> Dict[str, Any]:
        if not entity or not hasattr(state, "side_has") or not state.side_has(entity, "守夜灯"):
            return {}
        if is_enemy_turn:
            mana_to_gain = math.ceil(entity.mana_limit * 0.5)
            entity.current_mana += mana_to_gain
            return {"mana_gained": mana_to_gain, "for_reaction": True}
        return {}


class CombatHookManager:
    """钩子管理器：集中注册与生命周期分发"""

    def __init__(self):
        self._hooks: List[Any] = [
            DragonBloodlineMultiplierHook(),
            JiahaiHook(),
            LonglinHook(),
            BaolieHook(),
            BifenglingHook(),
            ShouyedengHook(),
        ]

    def register_hook(self, hook: Any) -> None:
        if hook not in self._hooks:
            self._hooks.append(hook)

    def apply_multiplier_adjust(self, target: Any, amount: int, damage_type: str, source: Optional[Any], state: Any) -> int:
        for hook in self._hooks:
            if hasattr(hook, "on_multiplier_adjust"):
                amount = hook.on_multiplier_adjust(target, amount, damage_type, source, state)
        return amount

    def apply_incoming_adjust(self, target: Any, amount: int, damage_type: str, source: Optional[Any], state: Any) -> int:
        for hook in self._hooks:
            if hasattr(hook, "on_incoming_adjust"):
                amount = hook.on_incoming_adjust(target, amount, damage_type, source, state)
        return amount

    def apply_before_damage(self, target: Any, amount: int, damage_type: str, attacker: Optional[Any], state: Any) -> Dict[str, Any]:
        result = {}
        for hook in self._hooks:
            if hasattr(hook, "on_before_damage"):
                res = hook.on_before_damage(target, amount, damage_type, attacker, state)
                if res:
                    result.update(res)
        return result

    def apply_after_damage(self, target: Any, actual_damage: int, shield_absorbed: int, detail: Dict[str, Any], attacker: Optional[Any], state: Any) -> Dict[str, Any]:
        result = {}
        for hook in self._hooks:
            if hasattr(hook, "on_after_damage"):
                res = hook.on_after_damage(target, actual_damage, shield_absorbed, detail, attacker, state)
                if res:
                    result.update(res)
        return result

    def apply_dodge(self, entity: Any, state: Any) -> Dict[str, Any]:
        result = {}
        for hook in self._hooks:
            if hasattr(hook, "on_dodge"):
                res = hook.on_dodge(entity, state)
                if res:
                    result.update(res)
        return result

    def apply_round_start(self, entity: Any, is_enemy_turn: bool, state: Any) -> Dict[str, Any]:
        result = {}
        for hook in self._hooks:
            if hasattr(hook, "on_round_start"):
                res = hook.on_round_start(entity, is_enemy_turn, state)
                if res:
                    result.update(res)
        return result
