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


class DamageRedirectionHook:
    """嫁祸与背负：伤害重定向逻辑"""
    def find_redirection_target(self, target: Any, damage_type: str, state: Any) -> Optional[Any]:
        if damage_type == "代价" or not target:
            return None
        # 嫁祸：自身下X次受伤由目标承担
        if hasattr(target, "_jiahuo_left") and getattr(target, "_jiahuo_left", 0) > 0:
            j_target = getattr(target, "_jiahuo_target", None)
            target._jiahuo_left -= 1
            if target._jiahuo_left <= 0:
                target.status_effects = [s for s in target.status_effects if s.name != "嫁祸"]
                if hasattr(target, "_jiahuo_target"):
                    delattr(target, "_jiahuo_target")
            if j_target and getattr(j_target, "is_alive", False):
                return j_target

        # 背负：目标的伤害由背负者承担
        all_entities = (state.get_all_player_side() + state.get_all_enemy_side()) if hasattr(state, "get_all_player_side") else []
        for ent in all_entities:
            if ent is target:
                continue
            if hasattr(ent, "_beifu_left") and getattr(ent, "_beifu_left", 0) > 0:
                if getattr(ent, "_beifu_target", None) is target:
                    ent._beifu_left -= 1
                    if ent._beifu_left <= 0:
                        target.status_effects = [s for s in target.status_effects if s.name != "被背负"]
                        if hasattr(ent, "_beifu_target"):
                            delattr(ent, "_beifu_target")
                    return ent
        return None


class LethalMitigationHook:
    """撤退、负岳碑与断尾求生：濒死保护与伤害吸收"""
    def check_mitigation(self, target: Any, amount: int, damage_type: str, combat: Any) -> Optional[Dict[str, Any]]:
        if damage_type == "代价" or not target or not getattr(target, "is_alive", False):
            return None

        # 1. 朋友/员工撤退与负岳碑
        if getattr(target, "entity_type", "") in ("朋友", "员工") and not getattr(target, "has_retreated", False):
            remaining_after_shield = max(0, amount - getattr(target, "shield", 0)) if amount > 0 else 0
            if remaining_after_shield >= target.current_hp and target.current_hp > 0:
                player = combat.state.player
                target_ref = next((ref for ref, entity in combat._combat_entity_refs().items() if entity is target), "")
                if (target_ref in combat.state.fuyuebei_declared and "负岳碑" in combat.state.artifacts_owned
                        and player is not None and player.current_hp > 20):
                    combat.state.fuyuebei_declared.remove(target_ref)
                    share_map = combat.state.event_modifiers.get("fuyuebei_cost_share_refs", {})
                    payment = combat.pay_numeric_cost(
                        player, "流血", 20,
                        cost_share_target_ref=share_map.pop(target_ref, ""))
                    return {
                        "raw_damage": amount, "shield_absorbed": 0, "actual_damage": 0,
                        "hp_before": target.current_hp, "hp_after": target.current_hp,
                        "blood_limit_before": target.blood_limit, "died": False,
                        "damage_type": damage_type, "retreated": False,
                        "fuyuebei_toll_paid": 20, "fuyuebei_cost": payment,
                    }
                target.has_retreated = True
                return {
                    "raw_damage": amount, "shield_absorbed": 0, "actual_damage": 0,
                    "hp_before": target.current_hp, "hp_after": target.current_hp,
                    "blood_limit_before": target.blood_limit, "died": False,
                    "damage_type": damage_type, "retreated": True,
                }

        # 2. 断尾求生
        if (getattr(target, "is_alive", False) and combat.state.side_has(target, "断尾求生")
                and combat.state.side_tail_declared(target)):
            remaining_after_shield = max(0, amount - getattr(target, "shield", 0)) if amount > 0 else 0
            if remaining_after_shield >= target.current_hp and target.current_hp > 0:
                sacrificed = combat.state.side_tail_declared(target)
                combat.state.remove_side_relic(target, sacrificed)
                combat.state.clear_side_tail_declared(target)
                return {
                    "raw_damage": amount, "shield_absorbed": 0, "actual_damage": 0,
                    "hp_before": target.current_hp, "hp_after": target.current_hp,
                    "blood_limit_before": target.blood_limit, "died": False,
                    "damage_type": damage_type, "tail_sacrificed": sacrificed,
                }
        return None


class AfterDamageEffectsHook:
    """逆鳞、伤痕、寄生、负岳索与龙族血脉斩杀：伤害落地后综合处理"""
    def on_after_damage(self, target: Any, actual_damage: int, shield_absorbed: int, detail: Dict[str, Any], attacker: Optional[Any], combat: Any) -> Dict[str, Any]:
        if actual_damage <= 0 or not target:
            return {}

        res = {}
        # 逆鳞层数
        if hasattr(target, "has_status") and target.has_status("逆鳞"):
            target._nilin = getattr(target, "_nilin", 0) + actual_damage
            detail["nilin_stack_added"] = actual_damage
            detail["nilin_total"] = target._nilin

        # 伤痕扣血限
        if hasattr(target, "has_status") and target.has_status("伤痕"):
            xv = target.get_status_value("伤痕") or 0
            delta = max(1, target.blood_limit - xv) - target.blood_limit
            combat._battle_delta(target, "blood_limit", delta, "伤痕", "debuff")
            target.current_hp = min(target.current_hp, target.blood_limit)
            if target.current_hp <= 0:
                target.is_alive = False
                detail["died"] = True
                detail["hp_after"] = 0
            detail["shanghen_blood_loss"] = xv

        # 切割：你使其他角色失去生命的同时扣除其等量血限
        if (attacker is not None and attacker is not target
                and hasattr(attacker, "has_status") and attacker.has_status("切割")
                and getattr(target, "is_alive", False)):
            combat._battle_delta(target, "blood_limit", -actual_damage, "切割", "debuff")
            target.current_hp = min(target.current_hp, target.blood_limit)
            if target.current_hp <= 0:
                target.is_alive = False
                detail["died"] = True
                detail["hp_after"] = 0
            detail["qiege_blood_loss"] = actual_damage

        # 寄生吸血
        if hasattr(target, "has_status") and target.has_status("寄生"):
            xv = target.get_status_value("寄生") or 0
            drain = math.ceil(actual_damage * 20 * xv / 100)
            src_name = next((s.source for s in getattr(target, "status_effects", []) if s.name == "寄生" and not getattr(s, "is_expired", False)), "")
            healer = combat._find_named(src_name)
            if healer is not None and getattr(healer, "is_alive", False) and drain > 0 and not healer.has_status("坏死"):
                h = combat.state.apply_heal(healer, drain)
                detail["jisheng_heal"] = {"healer": healer.name, **h}
                cancer = combat.check_cancer(healer)
                if cancer:
                    detail["jisheng_cancer"] = cancer

        # 负岳索首击自愈
        if hasattr(target, "has_status") and target.has_status("负岳索"):
            target.status_effects = [s for s in target.status_effects if s.name != "负岳索"]
            healed = combat.state.apply_heal(target, actual_damage)
            detail["fuyuesuo_heal"] = healed

        # 龙族血脉攻击怪物直接命零
        if (attacker is not None and hasattr(combat.state, "side_has") and combat.state.side_has(attacker, "龙族血脉")
                and getattr(target, "entity_type", "") == "怪物" and getattr(target, "is_alive", False)):
            target.current_hp = 0
            target.is_alive = False
            detail.update({"died": True, "hp_after": 0, "dragon_bloodline_kill": True})

        if detail.get("died"):
            combat._on_entity_death(target)

        return res


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
            DamageRedirectionHook(),
            LethalMitigationHook(),
            AfterDamageEffectsHook(),
        ]
        self.redirection_hook = DamageRedirectionHook()
        self.mitigation_hook = LethalMitigationHook()
        self.after_damage_hook = AfterDamageEffectsHook()

    def register_hook(self, hook: Any) -> None:
        if hook not in self._hooks:
            self._hooks.append(hook)

    def apply_redirection(self, target: Any, damage_type: str, state: Any) -> Optional[Any]:
        return self.redirection_hook.find_redirection_target(target, damage_type, state)

    def apply_mitigation(self, target: Any, amount: int, damage_type: str, combat: Any) -> Optional[Dict[str, Any]]:
        return self.mitigation_hook.check_mitigation(target, amount, damage_type, combat)

    def apply_after_damage_pipeline(self, target: Any, actual_damage: int, shield_absorbed: int, detail: Dict[str, Any], attacker: Optional[Any], combat: Any) -> Dict[str, Any]:
        return self.after_damage_hook.on_after_damage(target, actual_damage, shield_absorbed, detail, attacker, combat)

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
