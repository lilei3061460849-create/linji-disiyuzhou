"""
战斗声明式钩子系统（Combat Declarative Hook System）
彻底解耦 combat.py 中的硬编码条件分支，将遗物、道纹与法器重构为可独立插拔的生命周期 Listener。
"""
from __future__ import annotations
import math
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable
from .combat_events import CombatEvent, CombatEventType
from .effect_context import make_context, normalize_context
from .mechanisms.registry import MECHANISMS, MechanismHookAdapter
from .mechanisms.triggers import Phase


@runtime_checkable
class CombatHook(Protocol):
    """战斗生命周期钩子协议。

    priority：**执行优先级，数字小的先执行**。

    警告：本项目里 Hook 的执行顺序**本身就是规则的一部分**，不是实现细节。
    例：`加害`(+X) 必须先于 `龙鳞`(-X，且 max(0,...) 下限截断) 结算——
    伤害8/加害2/龙鳞8 时，现顺序得 max(0, (8+2)-8) = 2；
    反过来龙鳞先把 8 削成 0，加害的 `amount > 0` 前置条件不再成立，结果是 0。
    因此下面这些 priority 数值是对「重构前字面注册顺序」的**如实固化**，
    不得在没有 DM 裁定的情况下调整。
    已迁移到声明层的机制（当前仅【加害】，priority=20）经 MechanismHookAdapter
    挂在本列表的原位置，priority 语义与数值完全不变。
    """

    priority: int = 100

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
    priority = 10

    def on_multiplier_adjust(self, target: Any, amount: int, damage_type: str, source: Optional[Any], state: Any) -> int:
        if (source is not None and hasattr(state, "side_has") and state.side_has(source, "龙族血脉")
                and getattr(target, "entity_type", "") != "怪物" and damage_type != "代价"):
            return amount * 2
        return amount


class LonglinHook:
    """龙鳞：每次受到伤害-X，最低为0（持续∞）

    必须后于【加害】（已迁移为声明式 Mechanism，见 engine/mechanisms/builtins.py；
    经 MechanismHookAdapter 执行，priority=20 不变）。
    """
    priority = 30

    def on_incoming_adjust(self, target: Any, amount: int, damage_type: str, source: Optional[Any], state: Any) -> int:
        if amount > 0 and damage_type != "代价" and hasattr(target, "has_status") and target.has_status("龙鳞"):
            val = target.get_status_value("龙鳞") or 0
            return max(0, amount - val)
        return amount


class BaolieHook:
    """爆裂：受到伤害前，攻击者失去等量生命，持续X（敌回终递减）

    架构说明：这里直接改 attacker.current_hp 并翻 is_alive，不走
    CombatEngine._raw_hp_loss（Hook 只拿得到 state，拿不到 combat）。
    死亡通知与来源上下文由 CombatEngine._apply_hostile_damage_inner 的
    reflected/suppressed 分支统一补齐。
    反噬失血本身**不再触发一次爆裂**，避免 A→B→A 无限反弹（现有规则，勿改）。

    失血统计（DM 裁定 2026-08-19）：【活血】只看角色本回合有没有实际掉过 HP，
    不区分掉血来源，因此爆裂反噬造成的 HP 损失必须计入 hp_lost_this_round。
    """
    priority = 40

    def on_before_damage(self, target: Any, amount: int, damage_type: str, attacker: Optional[Any], state: Any) -> Dict[str, Any]:
        if (target and hasattr(target, "has_status") and target.has_status("爆裂")
                and attacker is not None and attacker is not target and amount > 0 and damage_type != "代价"):
            prev_hp = attacker.current_hp
            attacker.current_hp = max(0, attacker.current_hp - amount)
            reflect_amt = prev_hp - attacker.current_hp
            # 与 Entity.take_damage / CombatEngine._raw_hp_loss 同口径：
            # 只要实际掉了 HP，就计入本回合失血（【活血】等效果据此结算）。
            attacker.hp_lost_this_round += reflect_amt
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
    """避风铃闪避句：每次闪避后获得3点格挡。归零+15走失速总线。

    ⚠️ 双实现登记（P1）：引擎真实分发路径是
    `CombatEngine._note_dodge()`（闪避 +3）与 `CombatEngine._trigger_bifengling_zero()`（归零 +15）。
    `CombatHookManager.apply_dodge()` **没有任何引擎调用点**，只有单元测试直接调用它。
    在把闪避真正迁到 Hook 总线之前，禁止在 combat.py 里接线 apply_dodge()，否则会 +3 两次。
    """
    priority = 50

    def on_dodge(self, entity: Any, state: Any) -> Dict[str, Any]:
        if not entity or not hasattr(state, "side_has") or not state.side_has(entity, "避风铃"):
            return {}
        entity.shield += 3
        return {"shield_gained": 3, "total_shield": entity.shield}


class ShouyedengHook:
    """守夜灯：[敌回始]获得等同于[法限]50%的法力，该法力[敌回终]清空

    ⚠️ 双实现登记（P1）：引擎真实分发路径是 `CombatEngine._grant_shouyedeng()`
    （额外校验 存活/轮回者/遗物封印，并调用 clamp_immortal_body）。
    `CombatHookManager.apply_round_start()` **没有任何引擎调用点**，只有单元测试直接调用它。
    两侧都以 `entity._shouyedeng_granted` 做每回合幂等，因此即便误接也不会重复授予法力，
    但校验条件不同，迁移前不得接线。
    """
    priority = 60

    def on_round_start(self, entity: Any, is_enemy_turn: bool, state: Any) -> Dict[str, Any]:
        if not entity or not hasattr(state, "side_has") or not state.side_has(entity, "守夜灯"):
            return {}
        if is_enemy_turn:
            if getattr(entity, "_shouyedeng_granted", 0):
                return {}
            mana_to_gain = math.ceil(entity.mana_limit * 0.5)
            entity.current_mana += mana_to_gain
            entity._shouyedeng_granted = mana_to_gain
            return {"mana_gained": mana_to_gain, "for_reaction": True}
        return {}


class DamageRedirectionHook:
    """嫁祸与背负：伤害重定向逻辑"""
    priority = 70
    # 由 CombatEngine 通过 apply_redirection() 显式分发，不参与通用遍历。
    explicit_dispatch = True

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
    priority = 80
    # 由 CombatEngine 通过 apply_mitigation() 显式分发，不参与通用遍历。
    explicit_dispatch = True

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
                        cost_share_target_ref=share_map.pop(target_ref, ""),
                        cost_context={"timing": "reaction", "source": "负岳碑", "source_type": "artifact", "tags": {"active_payment"}})
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
    priority = 90
    # 由 CombatEngine 通过 apply_after_damage_pipeline() 显式分发。
    # 通用的 apply_after_damage() 会跳过本 Hook —— 否则两条分发路径会让
    # 伤痕/切割/寄生/负岳索/龙族血脉斩杀各触发两次。
    explicit_dispatch = True

    def on_after_damage(self, target: Any, actual_damage: int, shield_absorbed: int, detail: Dict[str, Any], attacker: Optional[Any], combat: Any) -> Dict[str, Any]:
        if actual_damage <= 0 or not target:
            return {}

        res = {}
        damage_ctx = normalize_context(detail.get("ctx"))
        damage_event_id = damage_ctx.event_id if damage_ctx else None
        # 致死时挂在死亡上下文下的父事件。默认是本次伤害；
        # 若死因其实是血限被压（伤痕/切割），则改挂那次血限变化，形成
        # 伤害 → 血限下降 → 命零 的三层链。
        lethal_ctx = damage_ctx
        # 逆鳞层数
        if hasattr(target, "has_status") and target.has_status("逆鳞"):
            target._nilin = getattr(target, "_nilin", 0) + actual_damage
            detail["nilin_stack_added"] = actual_damage
            detail["nilin_total"] = target._nilin

        # 伤痕扣血限
        if hasattr(target, "has_status") and target.has_status("伤痕"):
            xv = target.get_status_value("伤痕") or 0
            delta = max(1, target.blood_limit - xv) - target.blood_limit
            # lethal=False：命零判定仍统一留到本方法末尾，保持原有触发次序。
            shanghen = combat._apply_blood_limit_change(
                target, delta, "伤痕", "debuff", ctx=damage_ctx,
                source_type="daowen", subtype="scar", actor=attacker,
                tags={"daowen", "after_damage", "blood_limit_loss"},
                lethal=False)
            detail["shanghen_ctx"] = shanghen["ctx"]
            if target.current_hp <= 0:
                # 保持原次序：此处先置 is_alive（切割的 is_alive 前置条件依赖它），
                # 真正的死亡通知统一留到本方法末尾的 _check_hp_zero_death。
                target.is_alive = False
                detail["died"] = True
                detail["hp_after"] = 0
                lethal_ctx = normalize_context(shanghen["ctx"]) or lethal_ctx
            detail["shanghen_blood_loss"] = xv

        # 切割：你使其他角色失去生命的同时扣除其等量血限
        if (attacker is not None and attacker is not target
                and hasattr(attacker, "has_status") and attacker.has_status("切割")
                and getattr(target, "is_alive", False)):
            qiege = combat._apply_blood_limit_change(
                target, -actual_damage, "切割", "debuff", ctx=damage_ctx,
                source_type="daowen", subtype="cut", actor=attacker, owner=attacker,
                tags={"daowen", "blood_limit_loss"},
                lethal=False)
            if target.current_hp <= 0:
                target.is_alive = False
                detail["died"] = True
                detail["hp_after"] = 0
                lethal_ctx = normalize_context(qiege["ctx"]) or lethal_ctx
            detail["qiege_blood_loss"] = actual_damage
            detail["qiege_ctx"] = qiege["ctx"]

        # 寄生吸血
        if hasattr(target, "has_status") and target.has_status("寄生"):
            xv = target.get_status_value("寄生") or 0
            drain = math.ceil(actual_damage * 20 * xv / 100)
            src_name = next((s.source for s in getattr(target, "status_effects", []) if s.name == "寄生" and not getattr(s, "is_expired", False)), "")
            healer = combat._find_named(src_name)
            if healer is not None and getattr(healer, "is_alive", False) and drain > 0 and not healer.has_status("坏死"):
                h = combat.state.apply_heal(healer, drain, ctx={
                    "timing": damage_ctx.timing if damage_ctx else "",
                    "source": "寄生", "source_type": "daowen", "actor": healer, "target": healer,
                    "owner": healer, "mechanic": "heal", "subtype": "parasite", "amount": drain,
                    "tags": {"daowen", "after_damage"}, "parent_event_id": damage_event_id,
                })
                detail["jisheng_heal"] = {"healer": healer.name, **h}
                cancer = combat.check_cancer(healer)
                if cancer:
                    detail["jisheng_cancer"] = cancer

        # 负岳索：[战始]选择一名朋友/员工；其首次受到伤害时，你[回复]等量生命。
        # 状态挂在被保护者身上，但回复对象是遗物持有者（玩家），不是受伤目标本人。
        if hasattr(target, "has_status") and target.has_status("负岳索"):
            target.status_effects = [s for s in target.status_effects if s.name != "负岳索"]
            healer = getattr(combat.state, "player", None)
            if healer is not None and getattr(healer, "is_alive", False):
                healed = combat.state.apply_heal(healer, actual_damage, ctx={
                    "timing": damage_ctx.timing if damage_ctx else "",
                    "source": "负岳索", "source_type": "relic", "actor": healer, "target": healer,
                    "owner": healer, "mechanic": "heal", "subtype": "fuyuesuo", "amount": actual_damage,
                    "tags": {"relic", "after_damage"}, "parent_event_id": damage_event_id,
                })
                detail["fuyuesuo_heal"] = {"healer": healer.name, **healed}

        # 龙族血脉攻击怪物直接命零
        if (attacker is not None and hasattr(combat.state, "side_has") and combat.state.side_has(attacker, "龙族血脉")
                and getattr(target, "entity_type", "") == "怪物" and getattr(target, "is_alive", False)):
            target.current_hp = 0
            target.is_alive = False
            detail.update({"died": True, "hp_after": 0, "dragon_bloodline_kill": True})
            lethal_ctx = make_context(
                timing=damage_ctx.timing if damage_ctx else "",
                source="龙族血脉", source_type="relic", actor=attacker, target=target,
                owner=attacker, mechanic="execute", subtype="dragon_bloodline_kill",
                amount=0, tags={"relic", "after_damage", "execute"},
                parent_event_id=damage_event_id,
            )
            detail["dragon_bloodline_kill_ctx"] = lethal_ctx.to_dict()

        if detail.get("died"):
            combat._check_hp_zero_death(target, ctx=lethal_ctx)

        return res


class CombatHookManager:
    """钩子管理器：集中注册与生命周期分发。

    两类分发：
      * 通用遍历（apply_multiplier_adjust / apply_incoming_adjust / apply_before_damage /
        apply_after_damage / apply_dodge / apply_round_start）——按 priority 升序执行。
      * 显式分发（apply_redirection / apply_mitigation / apply_after_damage_pipeline）——
        由 CombatEngine 在伤害管线的固定位置调用。被显式分发的 Hook 标记
        `explicit_dispatch = True`，通用遍历会跳过它们，杜绝“同一效果触发两次”。
    """

    def __init__(self):
        # 顺序即规则：这里的相对次序是重构前字面注册顺序的如实固化，
        # 现在由 priority 显式表达（见 CombatHook.priority）。
        self.redirection_hook = DamageRedirectionHook()
        self.mitigation_hook = LethalMitigationHook()
        self.after_damage_hook = AfterDamageEffectsHook()
        # 已迁移到声明层的机制（当前仅【加害】）经适配器挂到同一条 Hook 分发路径，
        # 执行顺序与迁移前完全一致（加害 priority=20，位于龙鳞之前）。
        mechanism_hooks: List[Any] = [
            MechanismHookAdapter(mechanism)
            for mechanism in MECHANISMS.phase_mechanisms(Phase.INCOMING_ADJUST)
        ]
        # 注意：必须复用上面这三个**同一实例**，不能再 new 一份，
        # 否则注册表与显式分发路径持有的是两个对象，状态与去重都会失真。
        self._hooks: List[Any] = self._sorted([
            DragonBloodlineMultiplierHook(),
            *mechanism_hooks,
            LonglinHook(),
            BaolieHook(),
            BifenglingHook(),
            ShouyedengHook(),
            self.redirection_hook,
            self.mitigation_hook,
            self.after_damage_hook,
        ])

    @staticmethod
    def _priority_of(hook: Any) -> int:
        return getattr(hook, "priority", 100)

    @classmethod
    def _sorted(cls, hooks: List[Any]) -> List[Any]:
        # 稳定排序：同 priority 保持注册先后，行为与重构前一致。
        return sorted(hooks, key=cls._priority_of)

    @classmethod
    def _is_explicit(cls, hook: Any) -> bool:
        return bool(getattr(hook, "explicit_dispatch", False))

    def hooks(self) -> List[Any]:
        """按实际执行顺序返回全部已注册 Hook（供审计/测试断言顺序）。"""
        return list(self._hooks)

    def register_hook(self, hook: Any) -> None:
        if hook not in self._hooks:
            self._hooks.append(hook)
            self._hooks = self._sorted(self._hooks)

    def apply_redirection(self, target: Any, damage_type: str, state: Any) -> Optional[Any]:
        return self.redirection_hook.find_redirection_target(target, damage_type, state)

    def apply_mitigation(self, target: Any, amount: int, damage_type: str, combat: Any) -> Optional[Dict[str, Any]]:
        return self.mitigation_hook.check_mitigation(target, amount, damage_type, combat)

    def apply_after_damage_pipeline(self, target: Any, actual_damage: int, shield_absorbed: int, detail: Dict[str, Any], attacker: Optional[Any], combat: Any) -> Dict[str, Any]:
        return self.after_damage_hook.on_after_damage(target, actual_damage, shield_absorbed, detail, attacker, combat)

    def apply_multiplier_adjust(self, target: Any, amount: int, damage_type: str, source: Optional[Any], state: Any) -> int:
        for hook in self._hooks:
            if self._is_explicit(hook):
                continue
            if hasattr(hook, "on_multiplier_adjust"):
                amount = hook.on_multiplier_adjust(target, amount, damage_type, source, state)
        return amount

    def apply_incoming_adjust(self, target: Any, amount: int, damage_type: str, source: Optional[Any], state: Any) -> int:
        for hook in self._hooks:
            if self._is_explicit(hook):
                continue
            if hasattr(hook, "on_incoming_adjust"):
                amount = hook.on_incoming_adjust(target, amount, damage_type, source, state)
        return amount

    def apply_before_damage(self, target: Any, amount: int, damage_type: str, attacker: Optional[Any], state: Any) -> Dict[str, Any]:
        result = {}
        for hook in self._hooks:
            if self._is_explicit(hook):
                continue
            if hasattr(hook, "on_before_damage"):
                res = hook.on_before_damage(target, amount, damage_type, attacker, state)
                if res:
                    result.update(res)
        return result

    def apply_after_damage(self, target: Any, actual_damage: int, shield_absorbed: int, detail: Dict[str, Any], attacker: Optional[Any], state: Any) -> Dict[str, Any]:
        """通用 after-damage 遍历。

        AfterDamageEffectsHook 标了 explicit_dispatch，会被跳过——
        它由 apply_after_damage_pipeline() 负责，绝不能在这里再跑一遍。
        """
        result = {}
        for hook in self._hooks:
            if self._is_explicit(hook):
                continue
            if hasattr(hook, "on_after_damage"):
                res = hook.on_after_damage(target, actual_damage, shield_absorbed, detail, attacker, state)
                if res:
                    result.update(res)
        return result

    def apply_dodge(self, entity: Any, state: Any) -> Dict[str, Any]:
        result = {}
        for hook in self._hooks:
            if self._is_explicit(hook):
                continue
            if hasattr(hook, "on_dodge"):
                res = hook.on_dodge(entity, state)
                if res:
                    result.update(res)
        return result

    def apply_round_start(self, entity: Any, is_enemy_turn: bool, state: Any) -> Dict[str, Any]:
        result = {}
        for hook in self._hooks:
            if self._is_explicit(hook):
                continue
            if hasattr(hook, "on_round_start"):
                res = hook.on_round_start(entity, is_enemy_turn, state)
                if res:
                    result.update(res)
        return result
