"""
战斗计算引擎
负责战斗中的数值对撞、回合推进、伤害结算
所有数值计算在此完成，AI禁止自行计算
"""
from __future__ import annotations
import math
from typing import Optional, Any
from .models import Entity, StatusEffect, GameState, DaoWenInstance, DaoWen, Spell, Consumable
from .daowen import DaoWenEngine, ResonanceEngine
from .dice import DiceEngine
from .enums import (ActionPhase, TriggerTiming, InterruptType, DamageType,
                    EffectScope, EffectPolarity, CostType)
from .dm_rulings import Interrupt
from .combat_events import CombatEvent, CombatEventType
from .combat_hooks import CombatHookManager


class CombatEngine:
    """战斗计算引擎"""
    
    # 副本专属道纹
    REGION_EXCLUSIVE_DAOWEN = {
        "扭曲都市": {"变形","定型","畸变","僵化","超频","坏死","爆裂","退化"},
        "罪孽都市": {"洗劫","逼债","抵扣","清算","赎金","假钞","赌命","消灾"},
        "龙心谷":   {"加害","龙鳞","逆鳞","活血","裂变","嫁祸","背负","伤痕"},
    }
    
    # 原始怪物道纹（道纹归属规则：各组起点）——【原初X】可借用范围
    ORIGINAL_MONSTER_DAOWEN = ("狂暴", "强化", "活力", "减速", "必中", "自愈", "飞行")
    # 原始怪物道纹只在首次发动时支付异变5X；效果持续期间不再重复计费。
    # 必中为次数型（下X次选择[目标]无法闪避），余数记在 entity._bizhong_left。
    YUANCHU_COST_RATE = 5
    
    def __init__(self, state: GameState, dice: DiceEngine):
        self.state = state
        self.dice = dice
        self.combat_log: list[dict] = []  # 完整战斗日志
        self.hook_manager = CombatHookManager()
        self.event_stream: list[CombatEvent] = []
        # 三相残韵盘本场消耗的残韵
        self._sanxiang_consumed = ""
        # 残韵改写：entity_id → {源道纹: 变化后道纹}，只改下一次发动结算，不改持有
        self._resonance_rewrites: dict[int, dict[str, str]] = {}
    
    def _battle_delta(self, entity: Entity, field_name: str, delta: int,
                      source: str, polarity: str) -> int:
        """战斗中未注明永久且不是代价/伤害的面板变化统一登记为局内效果。"""
        return self.state.apply_scoped_delta(
            entity, field_name, delta,
            scope=EffectScope.BATTLE.value,
            polarity=polarity,
            source=source,
        )

    # ========== 伤害计算 ==========
    
    def calculate_attack_damage(
        self, 
        attacker: Entity, 
        target: Entity,
        hit_index: int = 0,
        is_must_hit: bool = False
    ) -> dict:
        """
        计算单次攻击伤害
        规则：
        - 闪避：消耗1点当前速度完全闪避
        - 必中：无法闪避
        - 格挡：仅抵消外部伤害，不抵消代价
        - 伤害类型决定是否被格挡
        """
        result = {
            "attacker": attacker.name,
            "target": target.name,
            "hit_index": hit_index,
            "attack_power": attacker.attack_power,
            "is_must_hit": is_must_hit,
            "can_dodge": not is_must_hit and target.current_speed >= 1,
            "dodge_available": target.current_speed >= 1,
        }
        
        return result
    
    def _is_flying(self, entity: Entity) -> bool:
        return bool(getattr(entity, "is_flying", False)
                    or entity.has_status("飞行")
                    or entity.has_status("滑翔"))

    def _field_has_zhuiluo(self) -> bool:
        for e in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            if e.has_status("坠落"):
                return True
        return False

    def _tick_baolie(self, entities) -> list:
        """只递减爆裂。持续X按持有者的[敌回终]计数。"""
        logs = []
        for entity in entities:
            keep = tuple(s.name for s in entity.status_effects if s.name != "爆裂")
            expired = entity.tick_status_effects(skip_names=keep)
            if expired:
                logs.append({"type": "baolie_expired", "entity": entity.name})
        return logs

    def _incoming_adjust(self, target: Entity, amount: int, damage_type: str = "普通") -> int:
        if amount <= 0 or damage_type == "代价" or target is None:
            return amount
        return self.hook_manager.apply_incoming_adjust(target, amount, damage_type, None, self.state)

    def _gain_speed(self, entity: Entity, amount: int) -> int:
        if amount <= 0:
            return 0
        if entity.has_status("加速"):
            amount *= 2
        entity.current_speed += amount
        self.clamp_immortal_body(entity)
        return amount

    def clamp_immortal_body(self, entity: Entity) -> None:
        """不朽之躯：获得的[法力]/[速度]无法超过[法限]/[速限]。

        只限制“获得”的当前法力/当前速度不得超过各自上限；属性点（修行/无所求）
        提升的是上限本身，不受本限制。朋友/员工不继承（side_has 已排除）。
        """
        if entity is None or not self.state.side_has(entity, "不朽之躯"):
            return
        entity.current_mana = min(entity.current_mana, entity.mana_limit)
        entity.current_speed = min(entity.current_speed, entity.speed_limit)

    def _note_dodge(self, entity: Entity, relic_target_ref: Optional[str] = None) -> dict:
        """所有闪避共用触发入口；需要选目标的回锋刀必须由提交显式携带引用。"""
        extra = {}
        if self.state.side_has(entity, "避风铃"):
            entity.gain_shield(3)
            extra["avoid_wind_shield"] = 3
            if entity.current_speed == 0:
                entity.gain_shield(15)
                extra["avoid_wind_zero_shield"] = 15
        if self.state.side_has(entity, "回锋刀"):
            refs = self._combat_entity_refs()
            target = refs.get(relic_target_ref or "")
            if (target is None or not target.is_alive
                    or self.state.on_player_side(target) == self.state.on_player_side(entity)):
                raise ValueError("回锋刀触发必须显式提交合法敌方目标引用")
            detail = self._apply_hostile_damage(target, 3, source=entity)
            extra["counter_blade"] = {"target": target.name, **detail}
        if entity.has_status("急速"):
            entity._jisu_dodges = getattr(entity, "_jisu_dodges", 0) + 1
            if entity._jisu_dodges >= 2:
                entity._jisu_dodges -= 2
                extra["jisu_speed"] = self._gain_speed(entity, 1)
        if entity.has_status("洞察"):
            entity._dongcha_pending = getattr(entity, "_dongcha_pending", 0) + 10
            extra["dongcha_pending"] = entity._dongcha_pending
        return extra

    def _jieli_boost(self, dealer: Entity, amount: int) -> int:
        if amount <= 0 or not dealer.has_status("借力"):
            return amount
        return math.ceil(amount * (1 + 10 * dealer.get_status_value("借力") / 100))

    def _find_named(self, name: str) -> Optional[Entity]:
        for e in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            if e.name == name:
                return e
        return None

    def single_round_action_count(self, entity: Entity) -> int:
        if entity is None:
            return 0
        if entity.entity_type == "怪物":
            n = 2
            act = self._monster_activated.get(id(entity), set())
            if "活力" in act and "活力" in entity.dao_wen:
                n += entity.dao_wen["活力"].x_value
            else:
                n += entity.get_status_value("活力")
            if "狂暴" in act or entity.has_status("狂暴"):
                n += 1
            n -= entity.get_status_value("无力")
            return max(0, n)
        return DaoWenEngine.single_round_action_count(entity)

    def _apply_hostile_damage(self, target: Entity, amount: int,
                              damage_type: str = DamageType.NORMAL.value,
                              source: Optional[Entity] = None) -> dict:
        """
        对target造成外部/敌对伤害的统一入口（供攻击与道纹伤害调用；自身【代价】不走此入口）。
        撤退（任意[朋友]/[员工]即将受到足以使当前命零的伤害时触发）：
        判定须扣除格挡后的实际伤害是否≥当前生命（格挡足够抵消则不触发撤退，也不触发死亡）；
        触发后本次伤害清零、目标保留当前生命与格挡、标记has_retreated退出本场战斗。
        负岳碑(终音法器)：若玩家已通过 declare_fuyuebei_toll(name) 预先声明保护该目标，
        且玩家当前生命>20，则改为玩家流血20，抵消本次伤害并取消本次撤退(目标不掉血也不撤退)。
        龙心谷专属（F2）：嫁祸/背负 重定向、逆鳞层数、伤痕血限衰减在此统一处理。
        """
        amount = self.hook_manager.apply_multiplier_adjust(target, amount, damage_type, source, self.state)
        amount = self._incoming_adjust(target, amount, damage_type)
        # ---- F2：嫁祸/背负 伤害重定向（在撤退判定之前） ----
        if damage_type != "代价":
            # 嫁祸：自身下X次受伤由目标承担
            if hasattr(target, "_jiahuo_left") and getattr(target, "_jiahuo_left", 0) > 0:
                j_target = getattr(target, "_jiahuo_target", None)
                # 消耗一次
                target._jiahuo_left -= 1
                if target._jiahuo_left <= 0:
                    target.status_effects = [s for s in target.status_effects if s.name != "嫁祸"]
                    if hasattr(target, "_jiahuo_target"):
                        delattr(target, "_jiahuo_target")
                if j_target and j_target.is_alive:
                    return self._apply_hostile_damage(j_target, amount, damage_type, source)
                # 目标已死则不再重定向，继续按原目标结算
            # 背负：目标的伤害由背负者承担（遍历全场找背负者）
            for ent in self.state.get_all_player_side() + self.state.get_all_enemy_side():
                if ent is target:
                    continue
                if hasattr(ent, "_beifu_left") and getattr(ent, "_beifu_left", 0) > 0:
                    if getattr(ent, "_beifu_target", None) is target:
                        ent._beifu_left -= 1
                        if ent._beifu_left <= 0:
                            # 清理被背负标记
                            target.status_effects = [s for s in target.status_effects if s.name != "被背负"]
                            if hasattr(ent, "_beifu_target"):
                                delattr(ent, "_beifu_target")
                        return self._apply_hostile_damage(ent, amount, damage_type, source)

        if damage_type != "代价" and target.entity_type in ("朋友", "员工") and not target.has_retreated and target.is_alive:
            remaining_after_shield = max(0, amount - target.shield) if amount > 0 else 0
            if remaining_after_shield >= target.current_hp and target.current_hp > 0:
                player = self.state.player
                target_ref = next((ref for ref, entity in self._combat_entity_refs().items()
                                   if entity is target), "")
                if (target_ref in self.state.fuyuebei_declared and "负岳碑" in self.state.artifacts_owned
                        and player is not None and player.current_hp > 20):
                    self.state.fuyuebei_declared.remove(target_ref)
                    share_map = self.state.event_modifiers.get("fuyuebei_cost_share_refs", {})
                    payment = self.pay_numeric_cost(
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
        # 断尾求生（真龙之心遗物）：玩家即将命零时，若已预声明愿意牺牲的龙族遗物，移除该遗物抵消本次伤害
        if (damage_type != "代价" and target.is_alive
                and self.state.side_has(target, "断尾求生") and self.state.side_tail_declared(target)):
            remaining_after_shield = max(0, amount - target.shield) if amount > 0 else 0
            if remaining_after_shield >= target.current_hp and target.current_hp > 0:
                sacrificed = self.state.side_tail_declared(target)
                self.state.remove_side_relic(target, sacrificed)
                self.state.clear_side_tail_declared(target)
                return {
                    "raw_damage": amount, "shield_absorbed": 0, "actual_damage": 0,
                    "hp_before": target.current_hp, "hp_after": target.current_hp,
                    "blood_limit_before": target.blood_limit, "died": False,
                    "damage_type": damage_type, "tail_sacrificed": sacrificed,
                }
        detail = target.take_damage(amount, damage_type)
        # ---- F2：逆鳞层数、伤痕血限 ----
        actual = detail.get("actual_damage", 0)
        if actual > 0:
            if target.has_status("逆鳞"):
                target._nilin = getattr(target, "_nilin", 0) + actual
                detail["nilin_stack_added"] = actual
                detail["nilin_total"] = target._nilin
            if target.has_status("伤痕"):
                xv = target.get_status_value("伤痕")
                delta = max(1, target.blood_limit - xv) - target.blood_limit
                self._battle_delta(
                    target, "blood_limit", delta, "伤痕", EffectPolarity.DEBUFF.value)
                target.current_hp = min(target.current_hp, target.blood_limit)
                if target.current_hp <= 0:
                    target.is_alive = False
                    detail["died"] = True
                    detail["hp_after"] = 0
                detail["shanghen_blood_loss"] = xv
            if target.has_status("寄生"):
                xv = target.get_status_value("寄生")
                drain = math.ceil(actual * 20 * xv / 100)
                src_name = next((s.source for s in target.status_effects
                                 if s.name == "寄生" and not s.is_expired), "")
                healer = self._find_named(src_name)
                if healer is not None and healer.is_alive and drain > 0 and not healer.has_status("坏死"):
                    h = self.state.apply_heal(healer, drain)
                    detail["jisheng_heal"] = {"healer": healer.name, **h}
                    cancer = self.check_cancer(healer)
                    if cancer:
                        detail["jisheng_cancer"] = cancer
            if target.has_status("负岳索"):
                target.status_effects = [status for status in target.status_effects if status.name != "负岳索"]
                healed = self.state.apply_heal(target, actual)
                detail["fuyuesuo_heal"] = healed
            if (source is not None and self.state.side_has(source, "龙族血脉")
                    and target.entity_type == "怪物" and target.is_alive):
                target.current_hp = 0
                target.is_alive = False
                detail.update({"died": True, "hp_after": 0, "dragon_bloodline_kill": True})
            if detail.get("died"):
                self._on_entity_death(target)
        return detail

    def _on_entity_death(self, entity: Entity) -> None:
        """统一死亡触发；重复通知通过实体标记幂等。"""
        if getattr(entity, "_death_triggers_emitted", False):
            return
        entity._death_triggers_emitted = True
        if entity.entity_type == "怪物":
            if self.state.player and self.state.side_has(self.state.player, "焦黑发丝"):
                self._gain_speed(self.state.player, 2)
            # 乱葬岗·招魂：记录本场已命零怪物尸体
            if not getattr(self.state, "dead_monsters", None):
                self.state.dead_monsters = []
            if entity not in self.state.dead_monsters:
                self.state.dead_monsters.append(entity)
        # 乱葬岗·分裂：[命零]创造X个复制体（血限20%，无分裂道纹）；缄默时全场命零效果被禁
        if getattr(self.state, "_pending_split_clones", 0) > 0:
            silenced = any(e.has_status("缄默") for e in self.state.get_all_player_side()
                           + self.state.get_all_enemy_side())
            if not silenced and entity.entity_type != "怪物":
                clones = self.state._pending_split_clones
                base_hp = max(1, math.ceil(entity.blood_limit * 20 / 100))
                for i in range(clones):
                    clone = Entity(name=f"{entity.name}·裂{i + 1}", entity_type="临时朋友",
                                   blood_limit=base_hp, current_hp=base_hp,
                                   attack_count=max(0, entity.attack_count),
                                   attack_power=entity.attack_power)
                    for dw_name, dw_inst in entity.dao_wen.items():
                        if dw_name == "分裂":
                            continue  # 复制体无分裂道纹
                        clone.dao_wen[dw_name] = dw_inst
                    self.state.temp_friends.append(clone)
                self.state._pending_split_clones = 0
                self._split_clones_spawned = clones
            else:
                self.state._pending_split_clones = 0

    # ---- F2 全量：罪孽/扭曲专属道纹的公共辅助 ----
    def _shards_of(self, entity: Entity) -> int:
        """实体可失去/被夺取的碎片量；假碎片优先，负债不抵消仍可支付的假碎片。"""
        if entity is self.state.player:
            return self.state.fake_shards + max(0, self.state.shards)
        return entity.fake_shards + max(0, entity.shards)

    def _lose_shards_of(self, entity: Entity, amount: int) -> int:
        """实体失去碎片（假碎片优先，玩家走 state.lose_shards）。返回实际失去的真碎片数。"""
        if entity is self.state.player:
            return self.state.lose_shards(amount)
        return entity.lose_shards(amount)

    def _raw_hp_loss(self, entity: Entity, amount: int) -> dict:
        """直接生命损失（绕过格挡；爆裂反射/赌命用），计入失血追踪，含命零判定"""
        before = entity.current_hp
        entity.current_hp = max(0, entity.current_hp - max(0, amount))
        lost = before - entity.current_hp
        entity.hp_lost_this_round += lost
        died = False
        if entity.current_hp <= 0:
            entity.is_alive = False
            died = True
            self._on_entity_death(entity)
        return {"hp_before": before, "hp_after": entity.current_hp, "lost": lost, "died": died}

    def _seal_one_relic(self, target: Entity, rounds: int) -> str:
        """抵扣X：封印目标拥有的一件遗物，持续X回合。返回被封印的遗物名；目标无遗物返回\"\"。"""
        if target is self.state.player:
            holder = self.state
            owned = [r.name for r in self.state.relics]
        else:
            # 引擎中怪物/同伴无 relic 字段 → 视为不拥有遗物，封印无效果
            holder = target
            owned = []
        # 目标无遗物 → 无效果
        if not owned:
            return ""
        # 封印第一件未在封印中的遗物；若全部已封印则延长第一件的剩余回合
        for rname in owned:
            if holder.sealed_relics.get(rname, 0) <= 0:
                holder.sealed_relics[rname] = max(1, rounds)
                return rname
        first = owned[0]
        holder.sealed_relics[first] = max(holder.sealed_relics.get(first, 0), rounds)
        return first

    def _xijie_steal(self, caster: Entity, target: Entity, damage_amount: int) -> int:
        """洗劫X：造成伤害时夺取[目标]等量[碎片]（假碎片优先由目标侧扣减；夺取量=min(目标碎片,伤害)）"""
        if damage_amount <= 0 or target is caster or not caster.has_status("洗劫"):
            return 0
        avail = self._shards_of(target)
        if avail <= 0:
            return 0  # 若[目标]没有[碎片]则夺取无效
        steal = min(avail, damage_amount)
        self._lose_shards_of(target, steal)
        if caster is self.state.player:
            self.state.shards += steal
        else:
            caster.shards += steal
        return steal

    def bizhong_remaining(self, entity: Entity) -> int:
        """必中X剩余可选目标次数。"""
        return max(0, int(getattr(entity, "_bizhong_left", 0) or 0))

    def grant_bizhong(self, entity: Entity, x: int) -> int:
        """获得下X次选择[目标]时无法被闪避。次数叠加。"""
        if not isinstance(x, int) or isinstance(x, bool) or x < 1:
            return self.bizhong_remaining(entity)
        entity._bizhong_left = self.bizhong_remaining(entity) + x
        entity.status_effects = [s for s in entity.status_effects if s.name != "必中"]
        entity.add_status(StatusEffect(
            name="必中", value=entity._bizhong_left, remaining_rounds=-1, source=entity.name))
        return entity._bizhong_left

    def consume_bizhong(self, entity: Entity) -> bool:
        """消耗一次必中余数。还有余数则本次选择[目标]无法闪避。"""
        left = self.bizhong_remaining(entity)
        if left <= 0:
            return False
        entity._bizhong_left = left - 1
        if entity._bizhong_left <= 0:
            entity.status_effects = [s for s in entity.status_effects if s.name != "必中"]
        else:
            for s in entity.status_effects:
                if s.name == "必中":
                    s.value = entity._bizhong_left
        return True

    def _on_cost_paid(self, payer: Entity) -> Optional[dict]:
        """烙痕钉等“每付出一次代价”效果的统一触发点。"""
        if payer is not self.state.player:
            return None
        ref = self.state.event_modifiers.get("brand_nail_target_ref")
        target = self._combat_entity_refs().get(ref or "")
        if target is None or not target.is_alive:
            return None
        detail = self._apply_hostile_damage(target, 10, "必中", payer)
        return {"target": target.name, **detail}

    SHAREABLE_NUMERIC_COSTS = {"流血", "衰老", "枯竭", "萎缩", "疲惫", "异变"}

    def blood_pact_targets(self, payer: Optional[Entity] = None) -> dict[str, Entity]:
        """【血契】可共同承担代价的存活朋友/员工；不要求玩家侧员工已部署。"""
        payer = payer or self.state.player
        if payer is self.state.player:
            targets: dict[str, Entity] = {}
            for prefix, entities in (("friend", self.state.friends), ("employee", self.state.employees)):
                for index, entity in enumerate(entities):
                    if entity.is_alive:
                        targets[f"{prefix}:{index}"] = entity
            return targets
        if payer is not None and payer.entity_type == "轮回者" and self.state.on_enemy_side(payer):
            return {
                f"enemy:{index}": entity
                for index, entity in enumerate(self.state.enemies)
                if entity is not payer and entity.is_alive
                and entity.entity_type in ("朋友", "员工")
            }
        return {}

    def _has_active_blood_pact(self, payer: Entity) -> bool:
        if payer is self.state.player:
            return (any(relic.name == "血契" for relic in self.state.relics)
                    and self.state.sealed_relics.get("血契", 0) <= 0)
        if payer is not None and payer.entity_type == "轮回者" and self.state.on_enemy_side(payer):
            return any(relic.name == "血契" for relic in self.state.opponent_relics)
        return False

    def blood_shadow_cost_share_options(self, payer: Entity) -> list[dict]:
        """血影要求所有承担者支付后仍存活，返回可执行的血契分担引用。"""
        if not self._has_active_blood_pact(payer) or payer.current_hp <= 5:
            return []
        return [
            {"ref": ref, "name": ally.name}
            for ref, ally in self.blood_pact_targets(payer).items()
            if ally.current_hp > 5
        ]

    @staticmethod
    def _cost_capacity(entity: Entity, cost_type: str) -> Optional[int]:
        return {
            "流血": entity.current_hp,
            "衰老": entity.blood_limit,
            "枯竭": entity.mana_limit,
            "萎缩": entity.speed_limit,
            "疲惫": entity.current_speed,
            "异变": None,
        }.get(cost_type)

    def validate_numeric_cost(
        self,
        payer: Entity,
        cost_type: str,
        amount: int,
        cost_share_target_ref: str = "",
    ) -> tuple[Entity, int, Optional[Entity], int]:
        """纯校验并计算血契拆分；奇数由原支付者承担向上取整的一半。"""
        if cost_type not in self.SHAREABLE_NUMERIC_COSTS:
            raise ValueError(f"{cost_type}不是可共同承担的数值代价")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise ValueError("数值代价必须是非负整数")
        ally = None
        owner_amount = amount
        ally_amount = 0
        if cost_share_target_ref:
            if not self._has_active_blood_pact(payer):
                raise ValueError("只有持有且未被封印【血契】的轮回者才能提交cost_share_target_ref")
            ally = self.blood_pact_targets(payer).get(cost_share_target_ref)
            if ally is None:
                raise ValueError("cost_share_target_ref必须指向一名存活朋友/员工")
            owner_amount = math.ceil(amount / 2)
            ally_amount = amount // 2
        for entity, part in ((payer, owner_amount), (ally, ally_amount)):
            if entity is None or part <= 0:
                continue
            capacity = (None if cost_type == "衰老" and self.state.side_has(entity, "不朽之躯")
                        else self._cost_capacity(entity, cost_type))
            if capacity is not None and part > capacity:
                raise ValueError(
                    f"{entity.name}无法完整承担{cost_type}{part}（可支付{capacity}）")
        return payer, owner_amount, ally, ally_amount

    def _apply_numeric_cost_part(self, payer: Entity, cost_type: str, amount: int) -> dict:
        """支付一方的已拆分数值代价；不再进行血契递归。"""
        if amount <= 0:
            return {"payer": payer.name, "cost_type": cost_type, "paid": 0}
        if cost_type == "流血":
            detail = self._pay_bleed_cost(payer, amount)
            return {"payer": payer.name, "cost_type": cost_type,
                    "paid": detail.get("actual_damage", 0), "detail": detail}
        if cost_type == "衰老":
            if self.state.side_has(payer, "不朽之躯"):
                return {"payer": payer.name, "cost_type": cost_type, "paid": 0, "immune": True}
            payer.blood_limit = max(0, payer.blood_limit - amount)
            payer.current_hp = min(payer.current_hp, payer.blood_limit)
            if payer.current_hp <= 0:
                payer.is_alive = False
                self._on_entity_death(payer)
        elif cost_type == "枯竭":
            payer.mana_limit = max(0, payer.mana_limit - amount)
            payer.current_mana = min(payer.current_mana, payer.mana_limit)
        elif cost_type == "萎缩":
            payer.speed_limit = max(0, payer.speed_limit - amount)
            payer.current_speed = min(payer.current_speed, payer.speed_limit)
        elif cost_type == "疲惫":
            payer.current_speed = max(0, payer.current_speed - amount)
        elif cost_type == "异变":
            mutation = payer.add_mutation(amount)
            self._bank_lianxin(payer, cost_type, amount)
            nail = self._on_cost_paid(payer)
            return {"payer": payer.name, "cost_type": cost_type, "paid": amount,
                    "mutation": mutation, "brand_nail": nail}
        self._bank_lianxin(payer, cost_type, amount)
        nail = self._on_cost_paid(payer)
        return {"payer": payer.name, "cost_type": cost_type, "paid": amount,
                "brand_nail": nail}

    def pay_numeric_cost(
        self,
        payer: Entity,
        cost_type: str,
        amount: int,
        *,
        cost_share_target_ref: str = "",
        dragon_heart_use: int = 0,
    ) -> dict:
        """统一支付可分担的数值代价：龙心先抵消，血契再拆分剩余后果。"""
        if not isinstance(dragon_heart_use, int) or isinstance(dragon_heart_use, bool) or dragon_heart_use < 0:
            raise ValueError("dragon_heart_use必须是非负整数")
        if payer is not self.state.player:
            shared_ok = ("共心环" in self.state.artifacts_owned
                         and self.state.shared_dragon_heart_type == cost_type)
            if not shared_ok:
                dragon_heart_use = 0
        heart = None
        offset = 0
        if dragon_heart_use > 0 and amount > 0:
            heart = next((item for item in self.state.consumables
                          if item.kind == "dragon_heart"
                          and item.dragon_heart_type == cost_type
                          and item.name == f"{cost_type}龙心"
                          and not item.is_depleted), None)
            if heart is not None:
                offset = min(dragon_heart_use, heart.current_uses, amount)
        remaining = amount - offset
        _, owner_amount, ally, ally_amount = self.validate_numeric_cost(
            payer, cost_type, remaining, cost_share_target_ref)
        if heart is not None and offset > 0:
            heart.current_uses -= offset
        owner_detail = self._apply_numeric_cost_part(payer, cost_type, owner_amount)
        ally_detail = (self._apply_numeric_cost_part(ally, cost_type, ally_amount)
                       if ally is not None else None)
        return {
            "cost_type": cost_type,
            "requested": amount,
            "dragon_heart_offset": offset,
            "remaining": remaining,
            "owner": owner_detail,
            "shared_with": ally_detail,
            "cost_share_target_ref": cost_share_target_ref or None,
            "actual_paid": owner_detail.get("paid", 0) + (ally_detail or {}).get("paid", 0),
        }

    def _pay_bleed_cost(self, payer: Entity, amount: int, dragon_heart_use: int = 0) -> dict:
        """
        支付单个承担者的"流血X"代价；血契拆分由 pay_numeric_cost 在外层完成。
        血誓戒：[回始]玩家首次主动支付流血代价时，获得等同于本次流血的格挡；
        若支付后生命≤30%[血限]，改为获得等量生命。血契分担时只按玩家本人实际承担的部分触发。
        dragon_heart_use：本次希望消耗"流血龙心"抵消的点数(龙心谷"炼心"产出)，抵消后剩余部分才真正支付。
        """
        actual, offset = self._offset_with_dragon_heart(payer, "流血", amount, dragon_heart_use)
        detail = payer.take_damage(actual, "代价")
        detail["dragon_heart_offset"] = offset
        if (payer is self.state.player and actual > 0 and not payer.blood_oath_used_this_round
                and any(r.name == "血誓戒" for r in self.state.relics)):
            payer.blood_oath_used_this_round = True
            if payer.blood_limit > 0 and payer.current_hp / payer.blood_limit <= 0.3:
                heal_detail = self.state.apply_heal(payer, actual)
                detail["blood_oath"] = {"type": "life", "amount": heal_detail["actual_heal"]}
            else:
                payer.gain_shield(actual)
                detail["blood_oath"] = {"type": "shield", "amount": actual}
        self._bank_lianxin(payer, "流血", actual)
        if actual > 0:
            nail = self._on_cost_paid(payer)
            if nail:
                detail["brand_nail"] = nail
        return detail

    def _offset_with_dragon_heart(self, payer: Entity, cost_type: str, amount: int, dragon_heart_use: int) -> tuple:
        """
        用一枚匹配类型的【××龙心】抵消本次代价：最多抵消 min(请求量, 龙心当前耐久, 原始代价)。
        返回 (抵消后实际需支付的数值, 实际消耗的龙心点数)。
        """
        if dragon_heart_use <= 0 or amount <= 0:
            return amount, 0
        heart_name = f"{cost_type}龙心"
        heart = next((c for c in self.state.consumables
                      if c.kind == "dragon_heart" and c.dragon_heart_type == cost_type
                      and c.name == heart_name and not c.is_depleted), None)
        if heart is None:
            return amount, 0
        offset = min(dragon_heart_use, heart.current_uses, amount)
        if offset <= 0:
            return amount, 0
        heart.current_uses -= offset
        return amount - offset, offset

    def _bank_lianxin(self, payer: Entity, cost_type: str, actual_paid: int):
        """
        炼心待生效时，玩家下一次实际支付(抵消后仍>0)的数值型代价，转化为等值的【××龙心】消耗品。
        同名消耗品自动合并耐久与耐久上限（沿用既有消耗品合并规则）。
        """
        if not (payer is self.state.player and self.state.pending_lianxin and actual_paid > 0):
            return
        self.state.pending_lianxin = False
        heart_name = f"{cost_type}龙心"
        existing = next((c for c in self.state.consumables if c.name == heart_name and c.kind == "dragon_heart"), None)
        if existing:
            existing.current_uses += actual_paid
            existing.max_uses += actual_paid
        else:
            self.state.consumables.append(Consumable(
                name=heart_name, effect=f"消耗Y点耐久可抵消Y点{cost_type}代价",
                current_uses=actual_paid, max_uses=actual_paid,
                kind="dragon_heart", dragon_heart_type=cost_type))

    def resolve_attack(
        self,
        attacker: Entity,
        target: Entity,
        hit_index: int = 0,
        is_must_hit: bool = False,
        dodge: bool = False,
        blood_shadow: bool = False,
        spell_choices: Optional[dict] = None,
        entity_refs: Optional[dict[str, Entity]] = None,
        dodge_relic_target_ref: Optional[str] = None,
        cost_share_target_ref: str = "",
    ) -> dict:
        """
        解析一次攻击
        dodge: 目标是否选择闪避（由AI决策）
        blood_shadow: 目标是否选择用【血影】遗物(流血10取消本次判定)代替常规闪避
        """
        entity_refs = entity_refs or self._combat_entity_refs()
        spell_choices = spell_choices if spell_choices is not None else {"before": {}, "after": {}}
        self.validate_spell_reaction_submission(target, attacker, spell_choices, entity_refs)
        result = {
            "attacker": attacker.name,
            "target": target.name,
            "hit_index": hit_index,
            "dodge_attempted": dodge,
            "dodge_success": False,
            "damage_dealt": 0,
            "shield_absorbed": 0,
            "hp_lost": 0,
            "target_died": False,
        }
        # 飞行：非飞行者无法选中飞行目标
        if not self.is_targetable(attacker, target):
            result["cant_target"] = True
            result["note"] = "飞行目标无法被非飞行者选中"
            return result

        # 必中：显式必中，或消耗一次「下X次选择[目标]」余数
        must_hit = is_must_hit or self.consume_bizhong(attacker)

        # 血影（初拥之夜遗物，仅玩家自身持有）：非必中判定下，可流血10取消本次判定，是常规闪避外的另一选项
        if (blood_shadow and not must_hit and self.state.side_has(target, "血影")):
            self.pay_numeric_cost(
                target, "流血", 10,
                cost_share_target_ref=cost_share_target_ref)
            result["blood_shadow_success"] = True
            result["note"] = "血影：流血10，本次判定被取消"
            return result

        # 闪避判定
        if dodge:
            if must_hit:
                result["dodge_success"] = False
                result["dodge_fail_reason"] = "必中攻击无法闪避"
            elif target.current_speed >= 1:
                target.current_speed -= 1
                result["dodge_success"] = True
                result["speed_after_dodge"] = target.current_speed
                extra = self._note_dodge(target, dodge_relic_target_ref)
                if extra:
                    result["dodge_extra"] = extra
                    result["speed_after_dodge"] = target.current_speed
                # 闪避成功，本局速度-1（战终复原）
                return result
            else:
                result["dodge_success"] = False
                result["dodge_fail_reason"] = "速度不足"
        
        # 伤害结算
        damage = attacker.attack_power
        # 逆鳞（F2）：下次伤害+全部层数后清空
        if hasattr(attacker, "_nilin") and getattr(attacker, "_nilin", 0) > 0:
            bonus = attacker._nilin
            damage += bonus
            result["nilin_bonus"] = bonus
            attacker._nilin = 0
        damage = self._jieli_boost(attacker, damage)
        if attacker.has_status("坠落"):
            damage = math.ceil(damage / 2)
        # 检查蒙蔽状态
        if attacker.has_status("蒙蔽"):
            stacks = attacker.get_status_value("蒙蔽")
            if stacks > 0:
                damage = 0
                # 减少蒙蔽层数
                for s in attacker.status_effects:
                    if s.name == "蒙蔽" and s.value > 0:
                        s.value -= 1
                        if s.value <= 0:
                            attacker.status_effects.remove(s)
                        break
                result["damage_dealt"] = 0
                result["blocked_by"] = "蒙蔽"
                return result
        
        # 检查贯穿（无视格挡）
        ignore_shield = attacker.has_status("贯穿")
        # 震岳龙躯（真龙之心遗物）：激活期间，自身受到超出15点的伤害无效
        if self.state.side_body_shield(target) > 0:
            damage = min(damage, 15)
        # 反应法术由攻击prepare列出、resolve显式提交；计算层不再自动选择X或目标。
        if damage > 0:
            slogs = self._resolve_spell_reactions(
                ActionPhase.BEFORE_DAMAGE_TAKEN.value, target, attacker,
                spell_choices["before"], entity_refs,
            )
            if slogs:
                result["spell_logs"] = slogs
            if not attacker.is_alive:
                damage = 0
        # 爆裂X（F2，裁定口径：受到伤害【前】反噬）：目标持[爆裂]时攻击者先失去等量生命，
        # 攻击者因此命零则本次伤害不落地（与 sim/balance_sim hit_monster 同口径：按结算总量反射一次）
        if target.has_status("爆裂") and attacker is not target and damage > 0:
            rd = self._raw_hp_loss(attacker, damage)
            result["baolie_reflect"] = rd
            if not attacker.is_alive:
                damage = 0
        # 裂变：受到伤害分X次结算；依全局整数规则，每次除法向上取整。
        if target.has_status("裂变") and damage > 0:
            xv = target.get_status_value("裂变") or 1
            if xv > 1:
                per = math.ceil(damage / xv)
                ta = ts = 0; died = False
                for _ in range(xv):
                    dr = self._apply_hostile_damage(target, per, "普通" if not ignore_shield else "无视格挡", attacker)
                    ta += dr["actual_damage"]; ts += dr["shield_absorbed"]; died = died or dr["died"]
                damage_result = {"actual_damage": ta, "shield_absorbed": ts, "hp_after": target.current_hp, "died": died, "split": xv}
            else:
                damage_result = self._apply_hostile_damage(target, damage, "普通" if not ignore_shield else "无视格挡", attacker)
        else:
            damage_result = self._apply_hostile_damage(target, damage, "普通" if not ignore_shield else "无视格挡", attacker)
        result["damage_dealt"] = damage_result["actual_damage"]
        result["shield_absorbed"] = damage_result["shield_absorbed"]
        result["hp_lost"] = damage_result["actual_damage"]
        result["target_died"] = damage_result["died"]
        result["target_hp_after"] = damage_result["hp_after"]
        # 撤退：朋友/员工即将命零时自动撤退（伤害清零、保留生命、退出本场），透传给战报渲染
        if damage_result.get("retreated"):
            result["retreated"] = True
        # 洗劫X（F2）：造成伤害时夺取[目标]等量[碎片]（状态挂在攻击者身上，持续X）
        if damage_result.get("actual_damage", 0) > 0 and attacker.has_status("洗劫"):
            stolen = self._xijie_steal(attacker, target, damage_result["actual_damage"])
            result["xijie_stolen"] = stolen
        if damage_result["actual_damage"] > 0:
            attacker.damage_dealt_this_round += damage_result["actual_damage"]
        if "split" in damage_result:
            result["split"] = damage_result["split"]
        if damage_result["actual_damage"] > 0 and target.is_alive:
            slogs2 = self._resolve_spell_reactions(
                ActionPhase.AFTER_LIFE_LOST.value, target, attacker,
                spell_choices["after"], entity_refs,
            )
            if slogs2:
                result.setdefault("spell_logs", []).extend(slogs2)
        
        # 结算后效果
        # 兴奋：每次出手后速度+1（X 只管持续）
        if attacker.has_status("兴奋"):
            result["speed_boost_from_excitement"] = self._gain_speed(attacker, 1)

        return result
    
    def calculate_round_attack(
        self, 
        attacker: Entity, 
        targets: list[Entity],
        target_selections: list[int]  # 每次攻击选定的目标索引
    ) -> dict:
        """
        一轮攻击（仅限怪物与微光者）
        规则：连续发动N次攻击（N=自身攻击次数），每次独立选定目标
        """
        attack_count = attacker.attack_count
        
        if len(target_selections) < attack_count:
            return {
                "error": f"需要{attack_count}个目标选择，只提供了{len(target_selections)}个",
                "required": attack_count
            }
        
        results = []
        for i in range(attack_count):
            target_idx = target_selections[i]
            if target_idx < 0 or target_idx >= len(targets):
                results.append({"error": f"目标索引{target_idx}无效"})
                continue
            
            target = targets[target_idx]
            if not target.is_alive:
                results.append({"error": f"目标{target.name}已死亡"})
                continue
            
            # 计算本次攻击
            calc = self.calculate_attack_damage(attacker, target, i)
            results.append({
                "hit_index": i,
                "target": target.name,
                "damage": attacker.attack_power,
                "can_dodge": calc["can_dodge"],
                "instruction": f"第{i+1}次攻击 → {target.name}，是否闪避？(闪避消耗1点速度)"
            })
        
        return {
            "attacker": attacker.name,
            "attack_count": attack_count,
            "hits": results
        }
    
    # ========== 回合管理 ==========
    
    def round_start(self, relic_choices: Optional[dict] = None) -> dict:
        """
        回始结算
        1. 拥有者获得等同当前法限的法力（加法，从不赋值到法限）
        2. 结算回始类效果
        3. 返回需要决策的信息
        """
        effects = []
        player = self.state.player
        if player is not None:
            if self.state.current_round == 0:
                shield = self.state.event_modifiers.pop("next_battle_first_round_shield", 0)
                if shield:
                    player.gain_shield(shield)
                    effects.append({"type": "event_first_round_shield", "amount": shield})
            leather = self.state.event_modifiers.pop("leather_shield_next", 0)
            if leather and self.state.side_has(player, "皮衣"):
                player.gain_shield(leather)
                effects.append({"type": "leather_shield", "amount": leather})
        
        # 活血追踪归零 + 出手预算归零（回始重置本回合已用出手次数）+ 血誓戒每回合限一次归零 + 血族血脉判定归零
        for e in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            e.hp_lost_this_round = 0
            e.actions_used_this_round = 0
            e.blood_oath_used_this_round = False
            e.mana_inflicted_this_round = 0
            e.damage_dealt_this_round = 0
        # 回始：每个轮回者获得等同当前法限的法力。战始已清零；折速法印在战始+=；血契/守夜灯在本段之后+=。
        # 死斗里封存对手也是轮回者，必须同样获得法力，否则只能普攻1点。
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            if entity.entity_type != "轮回者" or not entity.is_alive:
                continue
            old_mana = entity.current_mana
            gained = entity.mana_limit
            entity.current_mana += gained
            effects.append({
                "type": "mana_refill",
                "entity": entity.name,
                "from": old_mana,
                "to": entity.current_mana,
                "gained": gained,
            })

        # 遗物：回始触发（回锋刀造伤、守夜灯加法力）。守夜灯加在获得法限之后。
        relic_logs = self.process_relics(TriggerTiming.ROUND_START, {"relic_choices": relic_choices or {}})
        effects.extend({"type": "relic", "log": l} for l in relic_logs)
        
        # 结算回始效果
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            # 自愈：回始获得血限10X%的回复（坏死禁疗）
            if entity.has_status("自愈") and not entity.has_status("坏死"):
                x = entity.get_status_value("自愈")
                heal_pct = 10 * x
                heal_amount = math.ceil(entity.blood_limit * heal_pct / 100)
                heal_result = self.state.apply_heal(entity, heal_amount)
                effects.append({
                    "type": "self_heal",
                    "entity": entity.name,
                    "heal": heal_amount,
                    "actual": heal_result["actual_heal"]
                })
            if entity.has_status("衰败") and entity.is_alive:
                xv = entity.get_status_value("衰败")
                dmg_n = math.ceil(entity.current_hp * 10 * xv / 100)
                if dmg_n > 0:
                    source_name = next((status.source for status in entity.status_effects if status.name == "衰败"), "")
                    rd = self._apply_hostile_damage(entity, dmg_n, source=self._find_named(source_name))
                    effects.append({"type": "shuaibai_tick", "entity": entity.name,
                                    "damage": rd["actual_damage"], "died": rd["died"]})
            pending = getattr(entity, "_dongcha_pending", 0)
            if pending and entity.entity_type == "轮回者" and entity.is_alive:
                entity.current_mana += pending
                self.clamp_immortal_body(entity)
                effects.append({"type": "dongcha_mana", "entity": entity.name, "gained": pending})
                entity._dongcha_pending = 0
            # 乱葬岗·勾魂：[回始]使目标失去2X点当前法力（持续∞）
            if entity.has_status("勾魂") and entity.entity_type == "轮回者" and entity.is_alive:
                drain = entity.get_status_value("勾魂")
                lost = min(entity.current_mana, drain)
                entity.current_mana -= lost
                effects.append({"type": "gouhun_mana", "entity": entity.name, "lost": lost})
            
            # 狂暴：回始发动一轮额外攻击（标记）
            if entity.has_status("狂暴"):
                effects.append({
                    "type": "extra_attack_ready",
                    "entity": entity.name,
                    "note": "该实体本回合有一次额外攻击机会"
                })
            
            # 畸变：回终结算，此处标记
            if entity.has_status("畸变"):
                x = entity.get_status_value("畸变")
                blood_loss = entity.attack_count * entity.attack_power
                effects.append({
                    "type": "deform_pending",
                    "entity": entity.name,
                    "blood_loss": blood_loss,
                    "note": "回终结算"
                })

        # ---- F2：罪孽专属道纹 [回始] 结算（逼债/清算/赌命） ----
        # 逼债X：目标失去X碎片，否则失去2X血限（二选一；与 sim/balance_sim exclusive_round_start 同口径）
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            for entry in list(getattr(entity, "_bizhai", [])):
                x = entry["x"]
                if self._shards_of(entity) >= x:
                    self._lose_shards_of(entity, x)
                    effects.append({"type": "bizhai", "entity": entity.name, "lost_shards": x})
                else:
                    delta = max(1, entity.blood_limit - 2 * x) - entity.blood_limit
                    self._battle_delta(
                        entity, "blood_limit", delta, "逼债", EffectPolarity.DEBUFF.value)
                    entity.current_hp = min(entity.current_hp, entity.blood_limit)
                    if entity.current_hp <= 0:
                        entity.is_alive = False
                    effects.append({"type": "bizhai_blood", "entity": entity.name,
                                    "lost_blood_limit": 2 * x, "blood_limit": entity.blood_limit})
        # 清算X：目标失去[你碎片]点格挡（你=施法者当前碎片，每回始读取）
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            for entry in list(getattr(entity, "_qingsuan", [])):
                caster = entry["caster"]
                drain = max(0, self._shards_of(caster))
                lost = min(entity.shield, drain)
                entity.shield -= lost
                effects.append({"type": "qingsuan", "entity": entity.name, "lost_shield": lost, "drain": drain})
        # 赌命X：按场上存活角色从轮回者方开始发放数字，投随机数，对应目标失去30%当前生命
        duming_holders = [e for e in self.state.get_all_player_side() + self.state.get_all_enemy_side()
                          if e.is_alive and e.has_status("赌命")]
        for holder in duming_holders:
            alive = [e for e in self.state.get_all_player_side() + self.state.get_all_enemy_side() if e.is_alive]
            if len(alive) < 1:
                continue
            roll = self.dice.auto_roll(f"赌命_r{self.state.current_round}", [e.name for e in alive],
                                       context=f"{holder.name}发动赌命")
            idx = int(roll["player_number"]) - 1
            tgt = alive[min(max(idx, 0), len(alive) - 1)]
            d = math.ceil(tgt.blood_limit * 30 / 100)  # 用户裁定口径：血限30%
            rd = self._raw_hp_loss(tgt, d)
            effects.append({"type": "duming", "caster": holder.name, "target": tgt.name,
                            "roll": idx + 1, "of": len(alive), "damage": rd["lost"], **rd})

        self.state.current_round += 1
        
        return {
            "round": self.state.current_round,
            "phase": "回始",
            "effects": effects,
            "state": self._get_combat_state()
        }
    
    def round_end(self, blood_lineage_cost_share_target_ref: str = "") -> dict:
        """
        回终结算
        1. 回终类效果结算
        2. 格挡清空
        3. 持续X剩余回合-1
        """
        effects = []
        
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            # 畸变结算：正文是失去[血限]，不是受到等量伤害；按回终时的当前攻击面板动态计算。
            if entity.has_status("畸变") and entity.is_alive:
                blood_loss = max(0, entity.attack_count * entity.attack_power)
                before_limit = entity.blood_limit
                delta = max(0, entity.blood_limit - blood_loss) - entity.blood_limit
                self._battle_delta(
                    entity, "blood_limit", delta, "畸变", EffectPolarity.DEBUFF.value)
                entity.current_hp = min(entity.current_hp, entity.blood_limit)
                if entity.blood_limit <= 0 or entity.current_hp <= 0:
                    entity.current_hp = 0
                    entity.is_alive = False
                effects.append({
                    "type": "deform_blood_limit_loss",
                    "entity": entity.name,
                    "blood_loss": before_limit - entity.blood_limit,
                    "blood_limit_after": entity.blood_limit,
                    "hp_after": entity.current_hp,
                    "died": not entity.is_alive,
                })

            # 特殊事件【凡庸】（README 第500行）：任一角色连续五回合未出手／
            # 五回合未能使敌对角色生命减少时触发；轮回者直接死亡，怪物命零死亡，
            # 轮回者获得消耗品【残骸】(1/1)。此前从未实装，导致长期僵持局面不会终结。
            if entity.is_alive:
                # 两个条件互相独立，任一连续满 5 回合即触发（README 用"/"表示或）
                if entity.actions_used_this_round <= 0:
                    entity.no_action_rounds += 1
                else:
                    entity.no_action_rounds = 0
                if entity.damage_dealt_this_round <= 0:
                    entity.no_damage_rounds += 1
                else:
                    entity.no_damage_rounds = 0
                if entity.no_action_rounds >= 5 or entity.no_damage_rounds >= 5:
                    _why = ("连续五回合未出手" if entity.no_action_rounds >= 5
                            else "连续五回合未能使敌对角色生命减少")
                    entity.current_hp = 0
                    entity.is_alive = False
                    self._on_entity_death(entity)
                    entity.no_action_rounds = 0
                    entity.no_damage_rounds = 0
                    if entity is self.state.player:
                        self.state.last_death_cause = "mediocrity"
                    effects.append({"type": "mediocrity", "entity": entity.name,
                                    "note": f"{_why}，触发【凡庸】：凭空全身炸裂，[命零]"})
                    if entity.entity_type == "怪物":
                        self.state.consumables.append(
                            Consumable(name="残骸", effect="局内使用恢复20生命并获得异变10",
                                       current_uses=1, max_uses=1))
                        effects.append({"type": "mediocrity_loot", "entity": entity.name,
                                        "note": "轮回者获得消耗品【残骸】(1/1)"})

            # 血族血脉：持有者这一侧各自结算（死斗两边各一份）
            if entity.entity_type == "轮回者" and self.state.side_has(entity, "血族血脉"):
                if entity.damage_dealt_this_round > 0:
                    heal_detail = self.state.apply_heal(entity, entity.damage_dealt_this_round)
                    effects.append({"type": "blood_lineage_heal", "entity": entity.name,
                                     "amount": heal_detail["actual_heal"]})
                else:
                    payment = self.pay_numeric_cost(
                        entity, "流血", 20,
                        cost_share_target_ref=(blood_lineage_cost_share_target_ref
                                               if entity is self.state.player else ""))
                    effects.append({"type": "blood_lineage_bleed", "entity": entity.name,
                                     "cost": payment, "amount": payment["actual_paid"]})

            # 赤族诅咒：[回终]固定流血20
            if entity.entity_type == "赤族" and entity.is_alive:
                bleed_detail = entity.take_damage(20, "代价")
                effects.append({"type": "chizu_curse_bleed", "entity": entity.name,
                                 "amount": bleed_detail["actual_damage"], "died": bleed_detail["died"]})

            # 格挡清空
            if entity.shield > 0:
                effects.append({
                    "type": "shield_clear",
                    "entity": entity.name,
                    "cleared": entity.shield
                })
                entity.clear_shield()
            
        # 法力清空（敌回终）
        # 规则：[法限]用于发动道纹与法术，法力[敌回终]清空。
        # 死斗双方都是轮回者，必须与回始同一套循环：每个存活轮回者各自清空。
        # 朋友/员工/怪物没有法限，不走这条。
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            if entity.entity_type != "轮回者" or not entity.is_alive:
                continue
            if entity.current_mana > 0:
                effects.append({
                    "type": "mana_clear",
                    "entity": entity.name,
                    "cleared": entity.current_mana
                })
                entity.current_mana = 0
        
        # 持续效果递减。爆裂按[敌回终]：己方身上在此拍；敌方身上改在怪物回合开始时减。
        player_side_ids = {id(e) for e in self.state.get_all_player_side()}
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            skip = ("爆裂",) if id(entity) not in player_side_ids else ()
            expired = entity.tick_status_effects(skip_names=skip)
            if expired:
                # 只有“持续期间直接改写面板”的效果到期即还原；畸变/伤痕/逼债等
                # 已经产生的累计局内后果保留到战终，再由battle作用域统一回滚。
                panel_modifier_sources = {"强化", "弱化", "僵化"}
                rolled_back = self.state.rollback_scoped_sources(
                    entity, set(expired) & panel_modifier_sources)
                effects.append({
                    "type": "status_expired",
                    "entity": entity.name,
                    "expired_effects": expired,
                    "rolled_back_deltas": rolled_back,
                })
                # 逆鳞（F2）：状态到期时清空计层
                if "逆鳞" in expired and hasattr(entity, "_nilin"):
                    entity._nilin = 0
                # 嫁祸到期时清理计数（若未因次数耗尽）
                if "嫁祸" in expired and hasattr(entity, "_jiahuo_left"):
                    entity._jiahuo_left = 0
                    if hasattr(entity, "_jiahuo_target"):
                        delattr(entity, "_jiahuo_target")
                if ("飞行" in expired or "滑翔" in expired) and not self._is_flying(entity):
                    entity.is_flying = False
                if "变形" in expired and hasattr(entity, "_bianxing_original"):
                    entity.attack_power, entity.attack_count = entity._bianxing_original
                    delattr(entity, "_bianxing_original")
                    effects.append({"type": "bianxing_restore", "entity": entity.name,
                                    "attack_power": entity.attack_power,
                                    "attack_count": entity.attack_count})
                # 干扰/手雷减攻到期自动由 tick 清理，无需额外
            # F2：逼债/清算状态消失即清账（∞/持续X到期后不再逐回始结算）
            if not entity.has_status("逼债") and getattr(entity, "_bizhai", None):
                entity._bizhai = []
            if not entity.has_status("清算") and getattr(entity, "_qingsuan", None):
                entity._qingsuan = []
            # F2：抵扣封印回合递减，归零解封
            for rname in list(getattr(entity, "sealed_relics", {}).keys()):
                entity.sealed_relics[rname] -= 1
                if entity.sealed_relics[rname] <= 0:
                    del entity.sealed_relics[rname]
        # 玩家侧抵扣封印回合递减（state.sealed_relics）
        for rname in list(self.state.sealed_relics.keys()):
            self.state.sealed_relics[rname] -= 1
            if self.state.sealed_relics[rname] <= 0:
                del self.state.sealed_relics[rname]

        # 震岳龙躯：两边各自递减
        if self.state.dragon_body_shield_rounds > 0:
            self.state.dragon_body_shield_rounds -= 1
            effects.append({"type": "dragon_body_tick", "side": "player",
                            "remaining": self.state.dragon_body_shield_rounds})
        if self.state.opponent_dragon_body_shield_rounds > 0:
            self.state.opponent_dragon_body_shield_rounds -= 1
            effects.append({"type": "dragon_body_tick", "side": "opponent",
                            "remaining": self.state.opponent_dragon_body_shield_rounds})

        # 皮衣记录本回合失血，下一回始获得等量格挡。
        if (self.state.player and self.state.side_has(self.state.player, "皮衣")
                and self.state.player.hp_lost_this_round > 0):
            self.state.event_modifiers["leather_shield_next"] = self.state.player.hp_lost_this_round

        # 活血：有活血状态的实体，回终按本回合累计失血÷2回复
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            if entity.has_status("活血") and entity.hp_lost_this_round >= 2:
                heal_n = entity.hp_lost_this_round // 2
                h = self.state.apply_heal(entity, heal_n)
                effects.append({"type": "huoxue_heal", "entity": entity.name,
                                "heal": heal_n, "actual": h["actual_heal"]})
            entity.hp_lost_this_round = 0

        # 手术·强制移植：本场第三回终仍保持原移植道纹时转为怪物。
        if self.state.current_round >= 3:
            for entity in list(self.state.friends + self.state.employees):
                transplanted = getattr(entity, "_transplanted_daowen", "")
                if transplanted and transplanted in entity.dao_wen:
                    if entity in self.state.friends: self.state.friends.remove(entity)
                    if entity in self.state.employees: self.state.employees.remove(entity)
                    entity.entity_type = "怪物"; entity.is_deployed = True
                    self.state.enemies.append(entity)
                    effects.append({"type": "transplant_monster", "entity": entity.name,
                                    "daowen": transplanted})

        # 多路径胜利结算（雕塑/癌变/还债）
        settled = self.settle_victory_paths()
        if settled:
            effects.extend(settled)

        return {
            "round": self.state.current_round,
            "phase": "回终",
            "effects": effects,
            "state": self._get_combat_state()
        }
    
    # ========== 困境检查 ==========
    
    def check_monster_difficulty(self, monster: Entity) -> Optional[dict]:
        """
        怪物困境检查
        规则：困境是指怪物的主要优势被针对、原有取胜路线被稳定限制
        核心道纹被变化或失效、连续无法有效攻击、对方建立稳定压制循环、
        急中生智成功破解主要优势时，均应立即进行困境检查
        
        返回None表示未陷入困境，返回dict表示需要DM裁定（进化/逃跑）
        """
        if not monster.is_alive:
            return None
        
        hp_ratio = monster.hp_ratio
        
        # 检查是否陷入困境
        difficulty_signals = []
        
        # 1. 生命低于30%
        if hp_ratio <= 0.3:
            difficulty_signals.append("生命低于30%")
        
        # 2. 被眩晕/束缚/无法行动
        if monster.has_status("眩晕") or monster.has_status("束缚"):
            difficulty_signals.append("被控制")
        
        # 3. 攻击力被弱化到极低
        if monster.attack_power <= 1:
            difficulty_signals.append("攻击力极低")
        
        # 4. 退化效果叠加
        if monster.has_status("退化"):
            difficulty_signals.append("道纹数值被退化")
        
        # 5. 定型阻止改变
        if monster.has_status("定型"):
            difficulty_signals.append("被定型无法改变属性")
        
        # 6. 坏死无法回复
        if monster.has_status("坏死"):
            difficulty_signals.append("无法获得回复")
        
        # 困境探针（裁定⑦ 2026-08-10）：≥1个劣势信号即判定困境
        # （原口径≥2，导致进化在模拟策略下结构性不可达：4574场埋点15314次检查，信号分布{0:14267,1:1047,≥2:0}）
        if len(difficulty_signals) >= 1:
            return {
                "monster": monster.name,
                "hp_ratio": round(hp_ratio, 2),
                "signals": difficulty_signals,
                "action_required": "进化或逃跑（二选一，本场战斗限一次）",
                "note": "困境检查以当前胜率为依据，而非当前生命"
            }
        
        return None
    
    # ========== 逃跑与追击 ==========
    
    def initiate_escape(self, escaper: Entity, pursuers: list[Entity]) -> Interrupt:
        """
        发起逃跑
        规则：
        1. 战斗无缝继续
        2. 逃跑方必须消耗自身出手发动急中生智
        3. 追击方破解急中生智后逃跑失败
        """
        return Interrupt(
            interrupt_type=InterruptType.ESCAPE_AND_PURSUIT,
            context={
                "escaper": escaper.name,
                "escaper_hp": escaper.current_hp,
                "escaper_hp_ratio": round(escaper.hp_ratio, 2),
                "pursuers": [p.name for p in pursuers],
                "current_round": self.state.current_round,
            },
            description=(
                f"{escaper.name}试图逃跑！\n"
                f"规则：{escaper.name}必须消耗出手发动【急中生智】企图拖延时间逃跑。\n"
                f"追击方在正常回合内破解急中生智，全部破解后逃跑失败继续战斗，否则逃脱成功。\n"
                f"请DM裁定{escaper.name}的急中生智方案是否合理。"
            ),
            options=[
                {"id": "escape_success", "label": "逃脱成功", "description": "急中生智合理，逃脱成功"},
                {"id": "escape_fail", "label": "逃脱失败", "description": "急中生智被破解，继续战斗"},
            ],
            state_snapshot=self.state.to_dict()
        )
    
    # ========== 进化（原初X，引擎直接结算，无需DM中断） ==========
    
    def execute_evolution(self, monster: Entity, daowen_name: str, x: int) -> dict:
        """
        特殊事件【进化】：怪物发动【原初X】（README·特殊事件）。
        原初X：代价：异变5X。选择一种**当前轮回者已持有**、且自身未持有的道纹，
        [战终]前视为持有该道纹（其数值固定为本次X），借用的道纹发动时照常支付其自身代价。

        设计意图：借用对象改为"轮回者的道纹"而非固定的7种原始怪物道纹，
        使玩家的构筑本身成为风险来源——越依赖某条公式化路线，被复制时反噬越重，
        从而抑制"无脑最优解"。同时保持怪物与轮回者的身份区隔
        （怪物仍不持有法力/速度/残韵/局外阶段，只是临时借用道纹）。
        前置（怪物准则#3）：须处于困境；逃跑与进化二选一，每场战斗限一次。
        """
        if not monster.is_alive:
            return {"success": False, "error": f"{monster.name}已命零"}
        if id(monster) in self._monster_evolved:
            return {"success": False, "error": f"{monster.name}本场已选择过逃跑/进化（每场战斗限一次）"}
        difficulty = self.check_monster_difficulty(monster)
        if not difficulty:
            return {"success": False, "error": f"{monster.name}未陷入困境，不能进化"}
        player = self.state.player
        player_daowen = list(player.dao_wen.keys()) if player else []
        if daowen_name not in player_daowen:
            return {"success": False,
                    "error": f"【{daowen_name}】不在轮回者当前持有的道纹中，"
                             f"原初X只能借用：{'、'.join(player_daowen) if player_daowen else '（轮回者无道纹）'}"}
        if daowen_name in monster.dao_wen:
            return {"success": False, "error": f"{monster.name}已持有【{daowen_name}】，原初X只能借用自身未持有的原始怪物道纹"}
        if not isinstance(x, int) or isinstance(x, bool) or x < 1:
            return {"success": False, "error": "X必须为≥1的整数"}
        
        # 支付代价：异变5X（代价从做出选择开始生效，优先于效果结算）
        cost = self.YUANCHU_COST_RATE * x
        pay = monster.add_mutation(cost)
        self._monster_evolved.add(id(monster))
        log = [f"{monster.name}发动【原初{x}】：异变+{cost}（当前{pay['mutation_total']}层）"]
        
        if pay["collapsed"]:
            log.append(f"异变达到{pay['mutation_total']}层，触发【崩解】：{monster.name}直接命零，进化效果中断")
            return {"success": True, "action": "进化·原初X", "collapsed": True,
                    "log": log, "mutation": pay,
                    "state": self._get_combat_state()}
        
        # 借用：战终前视为持有（enemies于[战终]清空，借用自动到期）
        borrowed = DaoWen(
            name=daowen_name,
            formula=f"{daowen_name}X",
            cost_type="代价",
            cost_formula="异变5X",
            effect_formula="",
            is_monster_original=True,
            tags=["原初借用"],
        )
        monster.dao_wen[daowen_name] = DaoWenInstance(dao_wen=borrowed, x_value=x)
        log.append(f"{monster.name}[战终]前视为持有【{daowen_name}{x}】，发动时照常支付其自身代价")
        return {"success": True, "action": "进化·原初X", "collapsed": False,
                "borrowed": {"name": daowen_name, "x": x},
                "difficulty_signals": difficulty.get("signals", []),
                "log": log, "mutation": pay,
                "state": self._get_combat_state()}
    
    def get_plight_evolution_options(self) -> list[dict]:
        """
        供AI决策（事实源计算）：当前存活、处于困境、且本场未选择过逃跑/进化的怪物，
        及其【原初X】可用参数。怪物准则#3：陷入困境时强制逃跑/进化二选一，每场限一次；
        AI扮演怪物方，自行决定是否调用 declare_evolution 及参数。
        """
        options = []
        for m in self.state.enemies:
            if not m.is_alive or id(m) in self._monster_evolved:
                continue
            difficulty = self.check_monster_difficulty(m)
            if not difficulty:
                continue
            # 异变预算：门票异变5X后若达到阈值则触发【崩解】直接命零、借用中断。
            # max_x_by_mutation = 不崩解的最大X；超出属于合法但纯亏的自杀式选择，不禁止。
            max_x = max(0, (Entity.MUTATION_COLLAPSE_THRESHOLD - 1 - m.mutation_count) // self.YUANCHU_COST_RATE)
            options.append({
                "monster": m.name,
                "difficulty_signals": difficulty.get("signals", []),
                "mutation_layers": m.mutation_count,
                "max_x_by_mutation": max_x,
                # 借用池 = 轮回者当前持有、且该怪物尚未持有的道纹
                "borrowable_daowen": [d for d in (self.state.player.dao_wen if self.state.player else {})
                                      if d not in m.dao_wen],
            })
        return options
    
    # ========== 多路径胜利系统 ==========
    # 所有阈值数值均为占位初值，需经测试调整（见 AI_EXPERIENCE.md）

    PROLIFERATION_THRESHOLD = 2.0  # 癌变：README「累计恢复量达血限×2」；超出血限的恢复按双倍计
    CANCER_THRESHOLD = PROLIFERATION_THRESHOLD  # 别名：增生旧名已统一为癌变，二者同阈值
    DEBT_THRESHOLD = 10           # 还债：怪物负债达到10碎片时触发（已裁定固定值）
    SCULPTURE_DAMAGE = 15         # 雕塑：每点耐久可造成的伤害
    SCULPTURE_SHIELD = 20         # 雕塑：每点耐久可获得的格挡

    def cancer_threshold_of(self, entity: Entity) -> int:
        """README：累计恢复量达到血限×2（过量按双倍已计入 total_healed）。"""
        if entity.blood_limit <= 0:
            return 0
        return math.ceil(entity.blood_limit * self.PROLIFERATION_THRESHOLD)

    def check_cancer(self, entity: Entity) -> Optional[dict]:
        """任一角色恢复量达阈值即癌变。怪物仍吸收进书；轮回者/同伴直接命零。"""
        if entity is None or not entity.is_alive or entity.is_proliferated:
            return None
        threshold = self.cancer_threshold_of(entity)
        if threshold <= 0 or entity.total_healed < threshold:
            return None
        if entity.entity_type == "怪物":
            return self._proliferate_monster(entity)
        return self._cancer_character(entity)

    def check_all_cancer(self) -> list[dict]:
        results = []
        for entity in list(self.state.get_all_player_side()) + list(self.state.get_all_enemy_side()):
            hit = self.check_cancer(entity)
            if hit:
                results.append(hit)
        return results

    def _can_be_sculptured(self, entity: Entity) -> bool:
        """雕塑对任何非轮回者生效。轮回者攻次/攻力归0不触发。"""
        return entity.entity_type != "轮回者"

    def settle_victory_paths(self) -> list[dict]:
        """
        回终多路径胜利结算（依次检查：雕塑 / 癌变 / 还债）
        雕塑：任何非轮回者（怪物/微光者/赤族等），不视为击杀，不提供碎片。
        还债：仅怪物。
        癌变对任一角色生效。
        """
        results = []
        for monster in list(self.state.enemies):
            if not monster.is_alive or monster.is_sculptured \
                    or monster.is_proliferated or monster.is_debt_bound:
                continue

            # 1. 雕塑：攻击次数或攻击力之一归0（任何非轮回者）
            if self._can_be_sculptured(monster) and (
                    monster.attack_count <= 0 or monster.attack_power <= 0):
                results.append(self._sculpture_monster(monster))
                continue

            # 2. 癌变：累计受到恢复量达阈值
            cancer = self.check_cancer(monster)
            if cancer:
                results.append(cancer)
                continue

            # 3. 还债：负债达阈值（仅怪物；shards为负）
            if monster.entity_type == "怪物" and monster.shards <= -self.DEBT_THRESHOLD:
                results.append(self._debt_bind_monster(monster))
                continue

        seen = {id(e) for e in self.state.enemies}
        for ally in list(self.state.get_all_player_side()):
            if id(ally) in seen:
                continue
            if (ally.is_alive and not ally.is_sculptured and not ally.is_proliferated
                    and not ally.is_debt_bound
                    and self._can_be_sculptured(ally)
                    and (ally.attack_count <= 0 or ally.attack_power <= 0)):
                results.append(self._sculpture_monster(ally))
                continue
            cancer = self.check_cancer(ally)
            if cancer:
                results.append(cancer)
        return results

    def _remove_from_combat(self, monster: Entity):
        """将怪物移出战斗（不视为击杀）"""
        monster.is_alive = False

    def _cancer_character(self, entity: Entity) -> dict:
        """轮回者/同伴癌变：累计恢复达血限×2 → 直接命零。不吸收进书、不加休整+8。"""
        entity.is_proliferated = True
        entity.is_cancer = True
        entity.current_hp = 0
        entity.is_alive = False
        if entity is self.state.player:
            self.state.last_death_cause = "cancer"
        return {
            "type": "cancer",
            "type_alias": "proliferation",
            "entity": entity.name,
            "entity_type": entity.entity_type,
            "absorbed_heal": entity.total_healed,
            "threshold": self.cancer_threshold_of(entity),
            "note": f"{entity.name}累计承受{entity.total_healed}点恢复，触发【癌变】：直接[命零]",
        }

    def _sculpture_monster(self, monster: Entity) -> dict:
        """雕塑：怪物/微光者攻击次数或攻击力归0→化为雕塑消耗品（耐久=血限5%）"""
        durability = max(1, math.ceil(monster.blood_limit * 0.05))
        reason = "攻击次数归0" if monster.attack_count <= 0 else "攻击力归0"
        monster.is_sculptured = True
        self._remove_from_combat(monster)
        consumable = Consumable(
            name=f"{monster.name}雕塑",
            effect=(f"每消耗1点耐久，对1个目标造成{self.SCULPTURE_DAMAGE}点伤害，"
                    f"或使自身获得{self.SCULPTURE_SHIELD}点格挡"),
            current_uses=durability,
            max_uses=durability,
            kind="sculpture",
        )
        self.state.consumables.append(consumable)
        return {
            "type": "sculpture",
            "monster": monster.name,
            "reason": reason,
            "consumable": consumable.name,
            "durability": durability,
            "note": (f"{monster.name}{reason}，化为雕塑【{consumable.name}】（{durability}/{durability}）"),
        }

    def _proliferate_monster(self, monster: Entity) -> dict:
        """癌变：累计受到恢复量达阈值→吸收进死者之书，强化休整（旧名 增生）"""
        monster.is_proliferated = True
        # 兼容：同时写入癌变别名，便于外部以新名读取
        monster.is_cancer = True  # type: ignore[attr-defined]
        self._remove_from_combat(monster)
        absorbed = monster.total_healed
        # 正文：每只被吸收的癌变怪物使局外【休整】永久额外产生8点恢复量，可叠加。
        boost = 8
        self.state.rest_heal_bonus += boost
        self.state.death_book_wisdom.append(f"癌变·{monster.name}：休整恢复量+{boost}")
        return {
            "type": "proliferation",  # 保留旧 key 兼容；新 key 见下一行
            "type_alias": "cancer",
            "monster": monster.name,
            "absorbed_heal": absorbed,
            "rest_boost": boost,
            "rest_heal_bonus_total": self.state.rest_heal_bonus,
            "note": (f"{monster.name}累计承受{absorbed}点恢复被癌变吸收进《死者之书》，"
                     f"局外【休整】恢复量永久+{boost}（累计+{self.state.rest_heal_bonus}）"),
        }

    def _debt_bind_monster(self, monster: Entity) -> dict:
        """还债：负债达阈值→视为员工；负债还清后离开（走独立的负债经济轨道，不受出战支援/工资/黑名单约束）"""
        monster.is_debt_bound = True
        # 转为员工（保留当前面板），其待还负债记录于 shards（负值）
        monster.entity_type = "员工"
        monster.is_deployed = True  # "视为其参战"：立即出战，不需要玩家消耗出手派遣
        self.state.employees.append(monster)
        self.state.enemies.remove(monster)
        return {
            "type": "debt_bind",
            "monster": monster.name,
            "debt": -monster.shards,
            "note": (f"{monster.name}负债达{-monster.shards}，触发还债，视为[员工]参战；"
                     f"还清负债（支付{-monster.shards}碎片）后该员工离队"),
        }

    def use_sculpture(self, consumable: Consumable, target: Entity = None,
                      mode: str = "damage") -> dict:
        """
        使用雕塑：消耗1点耐久，造成15伤害或获得20格挡
        mode: "damage"(对target造伤) / "shield"(自身格挡)
        """
        if consumable.kind != "sculpture":
            return {"success": False, "error": "非雕塑消耗品"}
        if consumable.is_depleted:
            return {"success": False, "error": "雕塑已耗尽"}
        if mode not in ("damage", "shield"):
            return {"success": False, "error": "雕塑mode必须是damage或shield"}
        if mode == "damage" and target is None:
            return {"success": False, "error": "伤害模式需指定目标"}
        if mode == "shield" and self.state.player is None:
            return {"success": False, "error": "没有玩家，无法获得格挡"}
        consumable.use()
        if mode == "shield":
            player = self.state.player
            player.gain_shield(self.SCULPTURE_SHIELD)
            return {
                "success": True,
                "type": "sculpture_shield",
                "shield": self.SCULPTURE_SHIELD,
                "remaining": consumable.current_uses,
                "note": f"雕塑赋能：获得{self.SCULPTURE_SHIELD}点格挡",
            }
        else:
            dmg = self._apply_hostile_damage(target, self.SCULPTURE_DAMAGE, source=self.state.player)
            return {
                "success": True,
                "type": "sculpture_damage",
                "target": target.name,
                "damage": self.SCULPTURE_DAMAGE,
                "target_hp_after": dmg["hp_after"],
                "target_died": dmg["died"],
                "remaining": consumable.current_uses,
                "note": f"雕塑赋能：对{target.name}造成{self.SCULPTURE_DAMAGE}点伤害",
            }

    # 兼容旧接口名（降服已删，改为指代多路径胜利结算）
    def settle_taming(self) -> list[dict]:
        return self.settle_victory_paths()

    def init_monster_shards(self, monster: Entity) -> int:
        """
        罪孽都市怪物[战始]自带碎片=其全部专属道纹数值之和×2
        其他副本怪物碎片默认0。返回初始化后的碎片数。
        """
        if self.state.current_region != "罪孽都市":
            return monster.shards
        exclusive = self.REGION_EXCLUSIVE_DAOWEN.get("罪孽都市", set())
        total = 0
        for name, inst in monster.dao_wen.items():
            if name in exclusive:
                total += getattr(inst, "x_value", 0) or 0
        monster.shards = total * 2
        return monster.shards

    def drain_monster_shards(self, monster: Entity, amount: int) -> int:
        """夺取/逼债使怪物失去碎片（可降至负值即负债），返回夺得的碎片数（≥0部分）"""
        gained = max(0, monster.shards)  # 仅正值部分可被夺得
        monster.shards -= amount
        return min(gained, amount)

    # 一阶副本集合
    TIER1_REGIONS = {"罪孽都市", "扭曲都市", "龙心谷"}

    @classmethod
    def monster_spawn_count(cls, battle_number: int, region: str) -> int:
        """出怪数量=战斗场数；一阶副本直接-3，最低1（实测定值，原-2通关率仅6%）"""
        if region in cls.TIER1_REGIONS:
            return max(1, battle_number - 3)
        return max(1, battle_number)

    # ========== 急中生智 ==========
    
    def initiate_wit(self, declarer: Entity, target: Entity) -> Interrupt:
        """
        急中生智声明
        规则：
        1. 无法以任何形式造成伤害，同种解法第二次失效
        2. 公式：知识+环境元素+道纹=干扰目标
        3. 严禁进行纯数值买卖
        4. 只允许利用现实存在的概念
        5. 严禁以任何形式中断、解除、削弱或篡改已生效的道纹、法术与遗物
        6. 必须写明期望达成的明确效果
        """
        return Interrupt(
            interrupt_type=InterruptType.WIT_OF_DESPERATION,
            context={
                "declarer": declarer.name,
                "target": target.name,
                "declarer_hp": declarer.current_hp,
                "target_hp": target.current_hp,
                "declarer_daowen": list(declarer.dao_wen.keys()),
                "target_daowen": list(target.dao_wen.keys()),
                "current_round": self.state.current_round,
            },
            description=(
                f"{declarer.name}声明【急中生智】！\n\n"
                f"规则：\n"
                f"1. 无法以任何形式造成伤害\n"
                f"2. 同种解法第二次失效\n"
                f"3. 公式：知识+环境元素+道纹=干扰目标\n"
                f"4. 严禁纯数值买卖\n"
                f"5. 只允许利用现实存在的概念\n"
                f"6. 必须写明期望达成的明确效果\n\n"
                f"请DM裁定方案是否合理。"
            ),
            options=[
                {"id": "wit_success", "label": "急中生智成功", "description": "方案合理，获得DM裁定的效果"},
                {"id": "wit_fail", "label": "急中生智失败", "description": "方案不合理，消耗出手但无效果"},
            ],
            state_snapshot=self.state.to_dict()
        )
    
    def initiate_negotiation(self, proposal: str) -> Interrupt:
        """
        员工叛变·急中生智谈判声明：给出合理的谈判方案破解叛乱，需要DM裁定方案是否成立。
        """
        return Interrupt(
            interrupt_type=InterruptType.STAFF_MUTINY,
            context={
                "employees": [e.name for e in self.state.employees],
                "employee_attack_total": sum(e.attack_count * e.attack_power for e in self.state.employees),
                "player_hp": self.state.player.current_hp if self.state.player else 0,
                "shards": self.state.shards,
                "proposal": proposal,
            },
            description=(
                f"轮回者尝试以谈判方案破解员工叛变：\n\n{proposal}\n\n"
                f"请DM裁定该方案是否合理、能否平息叛乱。"
            ),
            options=[
                {"id": "negotiation_success", "label": "谈判成功", "description": "叛乱平息，方案对应的代价/效果按DM裁定生效"},
                {"id": "negotiation_fail", "label": "谈判失败", "description": "叛乱未平息，需改用镇压或让利处理"},
            ],
            state_snapshot=self.state.to_dict()
        )

    # ========== 辅助方法 ==========
    
    def apply_daowen_effect(
        self, name: str, calc: dict, caster: Entity, target: Entity,
        dragon_heart_use: int = 0, *, cost_share_target_ref: str = "",
        aoe_targets_override: Optional[list[Entity]] = None,
    ) -> dict:
        """应用道纹效果；aoe_targets_override用于绑定两阶段决策的目标快照。"""
        result = {"daowen": name, "effects": []}
        x = calc.get("x", 0)
        if name in ("自食", "固执"):
            target = caster

        # ---- 乱葬岗·附煞（sha_qi）效果修正 ----
        # 施法者持有的道纹实例可能带煞气；对消耗/持续/伤害/回复做代数修正。
        inst = caster.dao_wen.get(name)
        sha = getattr(inst, "sha_qi", "") if inst is not None else ""
        if sha:
            if sha == "法煞" and calc.get("cost", 0) > 0:
                calc["cost"] = max(calc.get("x", 1), calc["cost"] - calc.get("x", 1))
            if sha == "魂煞" and calc.get("duration") not in (None, -1):
                calc["duration"] += calc.get("x", 1)
            if sha == "冥煞":
                for k in ("target_damage", "total_damage", "aoe_damage"):
                    if k in calc:
                        calc[k] = calc[k] * 2
            if sha == "血煞" and "target_heal" in calc:
                calc["target_heal"] *= 2
            if sha == "心煞":
                result["sha_qi_cooldown_boost"] = True

        # 【冷却X】代价：README「冷却X：使用后该道纹记为【X(0)/Y】，[战终]后已完成
        # 战斗场数+1，达到Y时才能再次使用」。此前从未写入 cooldown_remaining，
        # 导致 固执/束缚/畸变/迟滞 可在同一场里无限重复发动（束缚因此支配全局）。
        if calc.get("cost_type") == "冷却":
            inst = caster.dao_wen.get(name)
            if inst is not None:
                inst.cooldown_remaining = max(inst.cooldown_remaining,
                                              int(calc.get("cost", 0)))
                result["cooldown_set"] = inst.cooldown_remaining

        # 蒙蔽(施法者伤害类道纹归零) / 坏死(目标禁疗)
        mengbi_blocked = caster.has_status("蒙蔽") and ("target_damage" in calc or "aoe_damage" in calc)
        if mengbi_blocked:
            for s in caster.status_effects:
                if s.name == "蒙蔽" and s.value > 0:
                    s.value -= 1
                    if s.value <= 0: caster.status_effects.remove(s)
                    break
            result["mengbi_blocked"] = True
        huaisi_block = target.has_status("坏死") and "target_heal" in calc

        # ---- 逆鳞加成（F2）：施法者若有层数，下次伤害+层数后清空 ----
        nilin_bonus = 0
        if hasattr(caster, "_nilin") and getattr(caster, "_nilin", 0) > 0 and any(k in calc for k in ("target_damage", "total_damage", "aoe_damage", "hp_percent_loss")):
            nilin_bonus = caster._nilin
            caster._nilin = 0
            result["nilin_bonus"] = nilin_bonus
            # 状态层数虽清空，但 status 本身仍按 duration 存在（仅清空计数）

        # ---- 爆裂X（F2，裁定口径：受到伤害【前】反噬）----
        # 目标持[爆裂]时，攻击者先失去等量生命；攻击者因此命零则本次伤害不落地。
        # 直接生命损失（_raw_hp_loss）为叶子结算，不会再次触发反噬，无需递归防护。
        baolie_suppress = False
        if (target.has_status("爆裂") and caster is not target and not mengbi_blocked
                and any(k in calc for k in ("target_damage", "total_damage", "aoe_damage", "hp_percent_loss"))):
            incoming = 0
            if "target_damage" in calc:
                incoming = calc["target_damage"]
            elif "total_damage" in calc:
                incoming = calc["total_damage"]
            elif "aoe_damage" in calc:
                incoming = calc["aoe_damage"]
            if incoming > 0:
                rd = self._raw_hp_loss(caster, incoming)
                result["baolie_reflect"] = rd
                if not caster.is_alive:
                    baolie_suppress = True  # 攻击者先死，本次伤害不落地

        # ---- 伤害类 ----
        if "target_damage" in calc:
            base = calc["target_damage"] + (nilin_bonus if nilin_bonus else 0)
            base = self._jieli_boost(caster, base)
            if caster.has_status("坠落") and base > 0:
                base = math.ceil(base / 2)
            dmg = self._apply_hostile_damage(target, 0 if (mengbi_blocked or baolie_suppress) else base, source=caster)
            result["effects"].append({"type": "damage", "target": target.name, **dmg})
            if dmg.get("actual_damage", 0) > 0:
                caster.damage_dealt_this_round += dmg["actual_damage"]
                self._xijie_steal(caster, target, dmg["actual_damage"])
        if name == "血债" or ("hits" in calc and calc.get("damage_per_hit") == 1 and "target_damage" not in calc):
            hits = calc.get("hits", 1)
            total_act = 0
            total_abs = 0
            for _ in range(hits):
                if not target.is_alive:
                    break
                dmg_i = self._apply_hostile_damage(target, 0 if (mengbi_blocked or baolie_suppress) else 1, source=caster)
                total_act += dmg_i.get("actual_damage", 0)
                total_abs += dmg_i.get("shield_absorbed", 0)
            dmg = {"raw_damage": hits, "actual_damage": total_act, "shield_absorbed": total_abs, "hp_after": target.current_hp, "died": not target.is_alive}
            result["effects"].append({"type": "damage", "target": target.name, **dmg})
            if total_act > 0:
                caster.damage_dealt_this_round += total_act
                self._xijie_steal(caster, target, total_act)
        elif "total_damage" in calc and "target_damage" not in calc:  # 其他多段
            add = nilin_bonus
            nilin_bonus = 0
            chunk = self._jieli_boost(caster, calc["total_damage"] + add)
            if caster.has_status("坠落") and chunk > 0:
                chunk = math.ceil(chunk / 2)
            dmg = self._apply_hostile_damage(target, 0 if (mengbi_blocked or baolie_suppress) else chunk, source=caster)
            if add:
                dmg["nilin_bonus"] = add
            result["effects"].append({"type": "damage", "target": target.name, **dmg})
            if dmg.get("actual_damage", 0) > 0:
                caster.damage_dealt_this_round += dmg["actual_damage"]
                self._xijie_steal(caster, target, dmg["actual_damage"])
        # ---- 乱葬岗·附煞后置：锁煞/蚀煞（造成伤害后触发） ----
        if sha in ("锁煞", "蚀煞"):
            dealt = 0
            for ef in result.get("effects", []):
                if ef.get("type") == "damage":
                    dealt += ef.get("actual_damage", 0) or 0
            if dealt > 0 and target.is_alive:
                if sha == "锁煞" and target.entity_type == "轮回者":
                    drain = min(target.current_mana, dealt)
                    target.current_mana -= drain
                    result["sha_qi_lock_mana"] = drain
                elif sha == "蚀煞" and target.entity_type != "轮回者":
                    target.attack_power = max(0, target.attack_power - 1)
                    result["sha_qi_erode_atk"] = -1

        if "aoe_damage" in calc:
            a = 0 if (mengbi_blocked or baolie_suppress) else self._jieli_boost(caster, calc["aoe_damage"])
            if caster.has_status("坠落") and a > 0:
                a = math.ceil(a / 2)
            # 逆鳞加成仅作用于首个目标的首段伤害
            if nilin_bonus:
                a += nilin_bonus
                result["nilin_bonus"] = nilin_bonus
                nilin_bonus = 0
            if aoe_targets_override is not None:
                # 两阶段怪物决策必须只结算prepare时列出的目标，且已闪避目标已由调用方剔除。
                aoe_targets = [e for e in aoe_targets_override if e.is_alive]
            elif caster in self.state.get_all_player_side():
                aoe_targets = self.state.get_all_enemy_side()
            else:
                aoe_targets = self.state.get_all_player_side()
            for enemy in aoe_targets:
                # 对首个敌人附加剩余加成（若前未消耗）
                dmg_a = a
                if nilin_bonus and enemy is aoe_targets[0]:
                    dmg_a += nilin_bonus
                    nilin_bonus = 0
                dmg = self._apply_hostile_damage(enemy, dmg_a, source=caster)
                result["effects"].append({"type": "aoe_damage", "target": enemy.name, **dmg})
                if dmg.get("actual_damage", 0) > 0:
                    caster.damage_dealt_this_round += dmg["actual_damage"]
                    self._xijie_steal(caster, enemy, dmg["actual_damage"])
        if "hp_percent_loss" in calc and name != "赌命":  # 赌命已改为[回始]随机结算（F2），此处仅保留其他百分比道纹
            d = math.ceil(target.current_hp * calc["hp_percent_loss"] / 100) + (nilin_bonus if nilin_bonus else 0)
            if nilin_bonus:
                result["nilin_bonus"] = nilin_bonus
                nilin_bonus = 0
            dmg = self._apply_hostile_damage(target, 0 if baolie_suppress else d, source=caster)
            result["effects"].append({"type": "pct_damage", "target": target.name, **dmg})
            if dmg.get("actual_damage", 0) > 0:
                caster.damage_dealt_this_round += dmg["actual_damage"]
                self._xijie_steal(caster, target, dmg["actual_damage"])

        # ---- 回复类 ----
        if "target_heal" in calc and not huaisi_block:
            result["effects"].append({"type": "heal", "target": target.name, **self.state.apply_heal(target, calc["target_heal"])})
        # 自愈的 heal_percent 只在[回始]结算，发动当下不奶。
        if "heal_percent" in calc and name != "自愈" and not (target.has_status("坏死")):
            h = math.ceil(target.blood_limit * calc["heal_percent"] / 100)
            result["effects"].append({"type": "heal_pct", "target": target.name, **self.state.apply_heal(target, h)})
        if "target_heal" in calc or ("heal_percent" in calc and name != "自愈"):
            cancer = self.check_cancer(target)
            if cancer:
                result["effects"].append(cancer)

        # ---- 格挡/血限 ----
        if "target_shield" in calc:
            s = calc["target_shield"]; target.gain_shield(s)
            result["effects"].append({"type": "shield", "target": target.name, "amount": s})
        if "shield_drain" in calc:  # 清算：目标失格挡
            lost = min(target.shield, calc["shield_drain"]); target.shield -= lost
            result["effects"].append({"type": "shield_drain", "target": target.name, "lost": lost})
        if "blood_limit_reduction" in calc:
            _hp_before = target.current_hp
            self._battle_delta(
                target, "blood_limit", -calc["blood_limit_reduction"],
                name, EffectPolarity.DEBUFF.value)
            # README 第460行"[血限]及当前生命同时 -4X"：两者是各自独立的扣减。
            # 此前实现只做 current_hp=min(current_hp, blood_limit)（血限压顶），
            # 对残血目标等于毫无效果——锐利打 10/200 的怪一点血都掉不了。
            if "hp_reduction" in calc:
                target.current_hp -= calc["hp_reduction"]
            target.current_hp = max(0, min(target.current_hp, target.blood_limit))
            if target.current_hp <= 0: target.is_alive = False
            # 血限压迫导致的当前生命减少，同样属于"使敌对角色生命减少"，
            # 必须计入本回合伤害统计，否则纯锐利流派会被【凡庸】判定为无所作为而自爆。
            _hp_cut = _hp_before - target.current_hp
            if _hp_cut > 0 and target is not caster:
                caster.damage_dealt_this_round += _hp_cut
            result["effects"].append({"type": "blood_limit_reduction", "target": target.name,
                                      "new_blood_limit": target.blood_limit,
                                      "hp_reduced": _hp_cut})
        if "blood_limit_increase" in calc:
            # 不朽之躯（初拥之夜遗物）：血限无法增加，对该实体的增殖等血限增长一律归零
            if self.state.side_has(target, "不朽之躯"):
                result["effects"].append({"type": "blood_limit_increase", "target": target.name,
                                           "increase": 0, "blocked_by": "不朽之躯"})
            else:
                increase = self._battle_delta(
                    target, "blood_limit", calc["blood_limit_increase"],
                    name, EffectPolarity.BUFF.value)
                result["effects"].append({"type": "blood_limit_increase", "target": target.name,
                                          "increase": increase})
        if "blood_limit_penalty" in calc and target.shards < (calc.get("shard_drain", 0) or 0):
            # 逼债：碎片不足则失血限；不是代价，按局内减益登记。
            lost = -self._battle_delta(
                target, "blood_limit", -calc["blood_limit_penalty"],
                name, EffectPolarity.DEBUFF.value)
            target.current_hp = min(target.current_hp, target.blood_limit)
            result["effects"].append({"type": "bizhai_blood", "target": target.name, "lost": lost})

        # ---- 攻击面板修改 ----
        _panel_keys = ("attack_boost", "attack_reduction", "attack_fixed", "attack_count_fixed")
        panel_locked = target.has_status("定型") and any(k in calc for k in _panel_keys)
        if panel_locked:
            result["effects"].append({"type": "dingxing_block", "target": target.name})
        if (not panel_locked) and "attack_boost" in calc:
            self._battle_delta(
                target, "attack_power", calc["attack_boost"],
                name, EffectPolarity.BUFF.value)
            result["effects"].append({"type": "attack_boost", "target": target.name, "attack_power": target.attack_power})
        if (not panel_locked) and "attack_reduction" in calc:
            delta = max(0, target.attack_power - calc["attack_reduction"]) - target.attack_power
            self._battle_delta(
                target, "attack_power", delta, name, EffectPolarity.DEBUFF.value)
            result["effects"].append({"type": "attack_reduction", "target": target.name, "attack_power": target.attack_power})
        if (not panel_locked) and "attack_fixed" in calc:
            self._battle_delta(
                target, "attack_power", calc["attack_fixed"] - target.attack_power,
                name, EffectPolarity.NEUTRAL.value)
            result["effects"].append({"type": "attack_fixed", "target": target.name, "attack_power": target.attack_power})
        if (not panel_locked) and "attack_count_fixed" in calc:
            self._battle_delta(
                target, "attack_count", calc["attack_count_fixed"] - target.attack_count,
                name, EffectPolarity.NEUTRAL.value)
            result["effects"].append({"type": "attack_count_fixed", "target": target.name, "attack_count": target.attack_count})
        bianxing_blocked = False
        if name == "变形":  # 自身攻击力与攻击次数互换；持续结束后还原首次变形前面板
            if caster.has_status("定型"):
                bianxing_blocked = True
                result["effects"].append({"type": "dingxing_block", "target": caster.name})
            else:
                if not hasattr(caster, "_bianxing_original"):
                    caster._bianxing_original = (caster.attack_power, caster.attack_count)
                caster.attack_power, caster.attack_count = caster.attack_count, caster.attack_power
                result["effects"].append({"type": "swap", "target": caster.name,
                                          "attack_power": caster.attack_power,
                                          "attack_count": caster.attack_count})

        # ---- 速度修改 ----
        if "speed_boost" in calc:
            gained = self._gain_speed(target, calc["speed_boost"])
            result["effects"].append({"type": "speed_boost", "target": target.name, "speed": target.current_speed, "gained": gained})
        if "speed_halved" in calc:
            target.current_speed = math.ceil(target.current_speed / 2)
            result["effects"].append({"type": "speed_halved", "target": target.name, "speed": target.current_speed})
        if "speed_penalty" in calc and (name != "赎金" or self._shards_of(target) <= 0):
            target.current_speed = max(0, target.current_speed - calc["speed_penalty"])
            result["effects"].append({"type": "speed_penalty", "target": target.name,
                                      "lost": calc["speed_penalty"], "speed": target.current_speed})

        # ---- 碎片系（罪孽）----
        if "shard_steal" in calc and self._shards_of(target) > 0:
            # 赎金是“有碎片则夺取，若无碎片才失速”的二选一；不得把不足额偷取扩成负债。
            gained = min(self._shards_of(target), calc["shard_steal"])
            self._lose_shards_of(target, gained)
            if caster is self.state.player:
                self.state.shards += gained
            else:
                caster.shards += gained
            result["effects"].append({"type": "shard_steal", "target": target.name, "gained": gained})
        if "fake_shards" in calc:  # 假钞：获得10X假碎片（假碎片与真碎片分离存储）
            if caster is self.state.player:
                self.state.fake_shards += calc["fake_shards"]
            else:
                caster.fake_shards = getattr(caster, "fake_shards", 0) + calc["fake_shards"]
            result["effects"].append({"type": "fake_shards", "gained": calc["fake_shards"]})
        if "cost_shards" in calc and name != "消灾":  # 消灾的付费在专属分支统一处理
            spent = self._lose_shards_of(self.state.player, calc["cost_shards"]) if caster is self.state.player else 0
            result["effects"].append({"type": "cost_shards", "spent": spent})

        # ---- 数值代价：统一走代价总线；【血契】可按显式引用共同承担 ----
        cost_spec = None
        if "cost_hp" in calc:
            cost_spec = ("流血", calc["cost_hp"], "bleed_cost")
        elif "cost_blood_limit" in calc:
            cost_spec = ("衰老", calc["cost_blood_limit"], "aging_cost")
        elif "cost_speed" in calc:
            cost_spec = ("疲惫", calc["cost_speed"], "fatigue_cost")
        elif "cost_mutation" in calc and caster.entity_type != "怪物":
            # 怪物原始道纹在两阶段怪物流程中已先支付异变5X；此处只补轮回者/同伴的同类代价。
            cost_spec = ("异变", calc["cost_mutation"], "mutation_cost")
        if cost_spec is not None:
            cost_type, amount, effect_type = cost_spec
            payment = self.pay_numeric_cost(
                caster, cost_type, amount,
                cost_share_target_ref=cost_share_target_ref,
                dragon_heart_use=dragon_heart_use,
            )
            cost_effect = {"type": effect_type, **payment}
            if cost_type == "流血":
                # 保留既有公开字段，同时以owner/shared_with暴露血契拆分详情。
                cost_effect["actual_damage"] = payment["actual_paid"]
                cost_effect["hp_after"] = caster.current_hp
            result["effects"].append(cost_effect)
        elif cost_share_target_ref:
            raise ValueError("该道纹没有可由【血契】共同承担的数值代价")
        if "mana_gain" in calc:
            caster.current_mana += calc["mana_gain"]
            self.clamp_immortal_body(caster)
            result["effects"].append({"type": "mana_gain", "source": caster.name, "mana_gained": calc["mana_gain"]})

        # ---- 乱葬岗（二阶）专属道纹效果 ----
        if name == "瓦解" and calc.get("blood_limit_pct"):
            pct = calc["blood_limit_pct"]
            cut = math.ceil(target.blood_limit * pct / 100)
            self._battle_delta(target, "blood_limit", -cut, "瓦解", EffectPolarity.DEBUFF.value)
            target.current_hp = min(target.current_hp, target.blood_limit)
            result["effects"].append({"type": "wajie", "target": target.name,
                                      "blood_limit_pct": pct, "blood_limit_cut": cut,
                                      "blood_limit_after": target.blood_limit})
        if name == "镇尸" and calc.get("no_heal"):
            target.add_status(StatusEffect(name="镇尸", value=1,
                                           remaining_rounds=calc.get("duration", 1),
                                           source=caster.name))
            result["effects"].append({"type": "zhenshi", "target": target.name,
                                      "duration": calc.get("duration", 1)})
        if name == "勾魂" and calc.get("round_start_mana_drain"):
            target.add_status(StatusEffect(name="勾魂", value=calc["round_start_mana_drain"],
                                           remaining_rounds=-1, source=caster.name))
            result["effects"].append({"type": "gouhun", "target": target.name,
                                      "mana_drain": calc["round_start_mana_drain"]})
        if name == "冥气" and calc.get("speed_loss_speed_limit"):
            target.add_status(StatusEffect(name="冥气", value=calc["speed_loss_speed_limit"],
                                           remaining_rounds=calc.get("duration", 1),
                                           source=caster.name))
            result["effects"].append({"type": "mingqi", "target": target.name,
                                      "speed_loss_speed_limit": calc["speed_loss_speed_limit"],
                                      "duration": calc.get("duration", 1)})
        if name == "缄默" and calc.get("silence_death_triggers"):
            target.add_status(StatusEffect(name="缄默", value=1,
                                           remaining_rounds=calc.get("duration", 1),
                                           source=caster.name))
            result["effects"].append({"type": "qianmo", "target": target.name,
                                      "duration": calc.get("duration", 1)})
        if name == "尸爆" and calc.get("self_destruct"):
            # [命零]对全体敌方打出自身血限10X%伤害
            if caster.is_alive and caster.current_hp > 0:
                pct = calc["aoe_pct"]
                dmg = math.ceil(caster.blood_limit * pct / 100)
                for enemy in [e for e in self.state.get_all_enemy_side() if e.is_alive]:
                    rd = self._apply_hostile_damage(enemy, dmg, source=caster)
                    result["effects"].append({"type": "aoe_damage", "target": enemy.name, **rd})
                caster.is_alive = False
                result["self_destructed"] = True
        if name == "分裂" and calc.get("split_clones"):
            # [命零]时创造X个复制体（血限20%）
            self.state._pending_split_clones = calc["split_clones"]
            result["effects"].append({"type": "fenlie",
                                      "note": "本场[命零]时创造X个复制体（血限20%）",
                                      "clones": calc["split_clones"]})
        if name == "招魂" and calc.get("revive_temp_friend"):
            # 唤回1具已击灭的怪物尸体作临时朋友（生命20X）
            dead = [e for e in self.state.dead_monsters if e.entity_type == "怪物"]
            if dead:
                corpse = dead[-1]
                from .models import Entity
                revived = Entity(name=f"{corpse.name}（魂）", entity_type="临时朋友",
                                 blood_limit=calc["temp_hp"], current_hp=calc["temp_hp"],
                                 attack_count=corpse.attack_count, attack_power=corpse.attack_power)
                for dw_name, dw_inst in corpse.dao_wen.items():
                    revived.dao_wen[dw_name] = dw_inst
                self.state.temp_friends.append(revived)
                result["effects"].append({"type": "zhaohun", "name": revived.name,
                                          "hp": calc["temp_hp"], "corpse": corpse.name})
            else:
                result["effects"].append({"type": "zhaohun", "note": "没有可唤回的怪物尸体"})

        # ---- 特殊 ----
        if "self_attack_count" in calc:  # 自残：目标自打X次
            for _ in range(calc["self_attack_count"]):
                result["effects"].append({"type": "self_attack", "target": target.name, **self._apply_hostile_damage(target, target.attack_power, source=target)})
        if "targets_removed" in calc:  # 封印：仅移出怪物（README：X个[目标]怪物）
            removed = 0
            removed_names = []
            for e in list(self.state.enemies):
                if (e.is_alive and e.entity_type == "怪物"
                        and removed < calc["targets_removed"]):
                    e.is_alive = False
                    e.removed_without_kill = True
                    removed += 1
                    removed_names.append(e.name)
            result["effects"].append({
                "type": "seal", "removed": removed, "targets": removed_names,
            })

        # ---- 持续/触发状态（status_added）----
        if "guaranteed_hits" in calc:
            self.grant_bizhong(caster, int(calc["guaranteed_hits"]))
            result["effects"].append({"type": "bizhong", "target": caster.name,
                                      "count": calc["guaranteed_hits"],
                                      "remaining": self.bizhong_remaining(caster)})

        # 蒙蔽X：使[目标]下X次造成的伤害无效。次数型，不走 duration 挂状态。
        # 此前只算出 invalid_damage_hits，apply 不消费，轮回者 use_daowen 等于白扣 5X 法力。
        # 怪物侧走 _apply_control_to_player，探照灯走 add_status，所以旧测试都绿。
        if "invalid_damage_hits" in calc:
            hits = int(calc["invalid_damage_hits"])
            if hits > 0:
                target.add_status(StatusEffect(
                    name="蒙蔽", remaining_rounds=-1, value=hits, source=caster.name))
                result["effects"].append({
                    "type": "mengbi",
                    "target": target.name,
                    "count": hits,
                    "remaining": target.get_status_value("蒙蔽"),
                })

        if name == "缓慢":
            if calc.get("effective"):
                target.add_status(StatusEffect(
                    name="缓慢", remaining_rounds=x, value=x, source=caster.name))
                result["effects"].append({
                    "type": "manqian", "target": target.name, "effective": True,
                    "action_count": calc.get("target_action_count"),
                })
            else:
                result["effects"].append({
                    "type": "manqian", "target": target.name, "effective": False,
                    "action_count": calc.get("target_action_count"),
                })

        if name == "坠落":
            duration = x if calc.get("duration") in (None, 0) else calc["duration"]
            grounded = []
            for e in self.state.get_all_player_side() + self.state.get_all_enemy_side():
                if not e.is_alive:
                    continue
                if self._is_flying(e) or e.has_status("坠落"):
                    e.is_flying = False
                    e.status_effects = [s for s in e.status_effects if s.name not in ("飞行", "滑翔")]
                    e.add_status(StatusEffect(name="坠落", remaining_rounds=duration, value=x, source=caster.name))
                    grounded.append(e.name)
            result["effects"].append({"type": "zhuiluo", "targets": grounded, "duration": duration})
        elif ("duration" in calc and calc.get("duration") is not None
              and not (name == "变形" and bianxing_blocked)
              # 乱葬岗道纹已在上方乱葬岗段自行 add_status，跳过通用状态处理避免重复叠加
              and name not in ("勾魂", "冥气", "缄默", "镇尸", "瓦解", "缓慢")):
            duration = calc["duration"] if calc["duration"] != 0 else -1
            effect_target = target if target else caster
            # 自身作用型道纹(变形/超频/自食等)作用于施法者
            # 洗劫：状态应挂在施法者上——"造成伤害时夺取等量碎片"以施法者为触发主体（与 sim/balance_sim 口径一致）
            self_targeted = name in ("超频", "自食", "飞行", "滑翔", "狂暴", "自愈", "必中", "变形", "洗劫", "固执")
            et = caster if self_targeted else effect_target
            if name in ("飞行", "滑翔") and self._field_has_zhuiluo():
                et.is_flying = False
                et.add_status(StatusEffect(name="坠落", remaining_rounds=1, value=x, source=caster.name))
                result["effects"].append({"type": "zhuiluo_block_flight", "target": et.name})
            else:
                et.add_status(StatusEffect(name=name, remaining_rounds=duration, value=x, source=caster.name))
                result["effects"].append({"type": "status_added", "target": et.name, "status": name, "duration": duration, "value": x})

        # ---- 龙心谷专属 4 件（F2）：逆鳞/嫁祸/背负/伤痕 的 combat 侧实装 ----
        # 逆鳞X：目标每失去1HP积1层，下次伤害+全部层后清空，持续X（已通过 duration 加状态，此处初始化计数）
        if name == "逆鳞":
            if not hasattr(target, "_nilin"):
                target._nilin = 0
            result["effects"].append({"type": "nilin_setup", "target": target.name, "x": x})
        # 嫁祸X：自身下X次受伤由目标承担（无持续，仅计数）
        elif name == "嫁祸":
            caster._jiahuo_left = x
            caster._jiahuo_target = target
            caster.add_status(StatusEffect(name="嫁祸", value=x, remaining_rounds=x, source=caster.name))
            result["effects"].append({"type": "jiahuo", "caster": caster.name, "target": target.name, "count": x})
        # 背负X：目标下X次受伤由自身承担
        elif name == "背负":
            caster._beifu_left = x
            caster._beifu_target = target
            # 在目标侧加标记便于查询
            target.add_status(StatusEffect(name="被背负", value=x, remaining_rounds=-1, source=caster.name))
            result["effects"].append({"type": "beifu", "caster": caster.name, "target": target.name, "count": x})
        # 伤痕X：目标每次掉血后血限-X，永久（已通过 duration 加伤痕状态，此处仅补日志）
        elif name == "伤痕":
            result["effects"].append({"type": "shanghen", "target": target.name, "x": x})

        # ---- F2 全量：罪孽都市（逼债/清算/赌命/消灾/抵扣）的注册与即时结算 ----
        # 逼债X：[回始]使[目标]失去X碎片，否则失去2X血限（二选一）。此处仅挂账，[回始]在 round_start 结算。
        if name == "逼债":
            target._bizhai.append({"x": x, "caster": caster})
            target.add_status(StatusEffect(name="逼债", value=x, remaining_rounds=-1, source=caster.name))
            result["effects"].append({"type": "bizhai_register", "target": target.name, "x": x})
        # 清算X：[回始]使[目标]失去你[碎片]点格挡，持续X。此处仅挂账。
        elif name == "清算":
            target._qingsuan.append({"x": x, "caster": caster})
            result["effects"].append({"type": "qingsuan_register", "target": target.name, "x": x})
        # 赌命X：玩家侧在_action_use_daowen预检付费；怪物侧由两阶段决策结算器付费。
        # 状态经 duration 挂在施法者上，[回始]在 round_start 按存活角色随机结算。
        elif name == "赌命":
            result["effects"].append({"type": "duming_register", "caster": caster.name, "x": x})
        # 消灾X：玩家侧在_action_use_daowen预检付费；怪物侧由两阶段决策结算器付费；此处登记重投次数。
        elif name == "消灾":
            self.dice.set_rerolls(self.dice.rerolls_pending + x)
            result["effects"].append({"type": "xiaozai_rerolls", "added": x, "total": self.dice.rerolls_pending})
        # 抵扣X：封印[目标]拥有的一件遗物，持续X（目标无遗物则无效果）
        elif name == "抵扣":
            sealed = self._seal_one_relic(target, x)
            result["effects"].append({"type": "dikou", "target": target.name,
                                      "sealed": sealed or None, "rounds": x})

        return result



    # 法术流程注册表；计算层只描述步骤，不再替持有者选择X、目标或闪避。
    SPELL_FLOWS = {
        "先发制人": {"trigger": ActionPhase.BEFORE_DAMAGE_TAKEN.value, "steps": [("杀伐", "attacker")]},
        "临界泄压": {"trigger": ActionPhase.BEFORE_DAMAGE_TAKEN.value, "steps": [("锐利", "attacker")]},
        "后发制人": {"trigger": ActionPhase.BEFORE_DAMAGE_TAKEN.value, "steps": [("庇护", "self")]},
        "生生不息": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("再生", "self")]},
        "以牙还牙": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("再生", "self"), ("杀伐", "attacker")]},
        "借力打力": {"trigger": ActionPhase.BEFORE_DAMAGE_TAKEN.value, "steps": [("庇护", "self"), ("杀伐", "attacker")]},
        "不死不休": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("血债", "attacker")], "loop": True},
        "千刀万剐": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("再生", "self"), ("血债", "attacker")], "loop": True},
        "咎由自取": {"trigger": "目标发动道纹前", "steps": [("坠落", "target"), ("杀伐", "target"), ("血债", "target")]},
    }

    # 自创法术文本→执行：解析 trigger_condition / effect_flow 为 SPELL_FLOWS 同构结构。
    # 格式（与死者之书一致）：trigger如"受到伤害前"/"失去生命后"；flow如"发动杀伐X→发动再生X"。
    # 目标推断：攻击/削弱/控制类道纹打attacker，自保/增益/回复类打self，坠落打target（若飞行）。
    _SPELL_SELF_DAOWEN = {"庇护", "再生", "固执", "活血", "龙鳞", "自食", "透支", "假钞", "超频", "变形"}
    _SPELL_TRIGGER_MAP = {
        "受到伤害前": ActionPhase.BEFORE_DAMAGE_TAKEN.value,
        "失去生命后": ActionPhase.AFTER_LIFE_LOST.value,
        "失去生命后（循环）": ActionPhase.AFTER_LIFE_LOST.value,
        "目标发动道纹前": "目标发动道纹前",
    }

    def _parse_custom_spell(self, spell) -> Optional[dict]:
        """把自创法术的文本解析为 SPELL_FLOWS 同构结构；解析失败返回None。"""
        from engine.daowen import DaoWenEngine
        trigger = (spell.trigger_condition or "").strip()
        trigger = trigger.replace("（循环）", "（循环）")
        ph = self._SPELL_TRIGGER_MAP.get(trigger)
        if ph is None:
            return None
        flow = (spell.effect_flow or "").strip()
        # 提取所有"发动<道纹>X"步骤（跳过"付出代价/若...否则跳过"等条件语）
        import re
        steps = []
        for m in re.finditer(r"发动\s*([\u4e00-\u9fa5]{2,4})\s*X", flow):
            daowen = m.group(1)
            if daowen not in DaoWenEngine.list_all():
                return None  # 非已有道纹=违规
            if daowen in self._SPELL_SELF_DAOWEN:
                role = "self"
            elif daowen == "坠落":
                role = "target"
            else:
                role = "attacker"
            steps.append((daowen, role))
        if not steps:
            return None
        return {"trigger": ph, "steps": steps}

    def _eligible_spell_flows(self, holder: Entity, trigger: str) -> dict[str, dict]:
        flows = {}
        if holder is None or not holder.is_alive:
            return flows
        for spell in holder.spells:
            flow = self.SPELL_FLOWS.get(spell.name)
            if flow is None:
                # 自创法术：解析文本（可能被缓存到 spell 上）
                flow = getattr(spell, "_parsed_flow", None)
                if flow is None:
                    flow = self._parse_custom_spell(spell)
                    if flow is None:
                        continue  # 解析失败=违规或格式错，不触发
                    spell._parsed_flow = flow
            if (flow["trigger"] == trigger
                    and all(name in holder.dao_wen and holder.dao_wen[name].can_use()
                            for name in spell.required_daowen)):
                flows[spell.name] = flow
        return flows

    def prepare_spell_reactions(self, holder: Entity, attacker: Entity) -> dict:
        """列出一次受击前/失血后可能触发的法术，供攻击prepare嵌入。"""
        refs = self._combat_entity_refs()
        reverse = {id(entity): ref for ref, entity in refs.items()}
        result = {}
        for key, trigger in (("before", ActionPhase.BEFORE_DAMAGE_TAKEN.value),
                             ("after", ActionPhase.AFTER_LIFE_LOST.value)):
            result[key] = []
            for name, flow in self._eligible_spell_flows(holder, trigger).items():
                steps = []
                for daowen, role in flow["steps"]:
                    target = holder if role == "self" else attacker
                    steps.append({"daowen": daowen, "target_ref": reverse.get(id(target)),
                                  "x": "positive integer", "dodge": "boolean if hostile"})
                result[key].append({"spell_name": name, "steps": steps,
                                    "loop": bool(flow.get("loop"))})
        return result

    def validate_spell_reaction_submission(self, holder: Entity, attacker: Entity,
                                           submitted: Any, refs: dict[str, Entity]) -> None:
        if not isinstance(submitted, dict):
            raise ValueError("每次攻击必须显式提交spell_choices对象")
        reverse = {id(entity): ref for ref, entity in refs.items()}
        for key, trigger in (("before", ActionPhase.BEFORE_DAMAGE_TAKEN.value),
                             ("after", ActionPhase.AFTER_LIFE_LOST.value)):
            eligible = self._eligible_spell_flows(holder, trigger)
            choices = submitted.get(key)
            if not isinstance(choices, dict) or set(choices) != set(eligible):
                raise ValueError(f"spell_choices.{key}必须逐一覆盖{sorted(eligible)}")
            mana = holder.current_mana
            speed_budget = {ref: entity.current_speed for ref, entity in refs.items()}
            for spell_name, flow in eligible.items():
                decision = choices[spell_name]
                if not isinstance(decision, dict) or not isinstance(decision.get("use"), bool):
                    raise ValueError(f"法术{spell_name}必须显式提交use布尔值")
                if not decision["use"]:
                    continue
                cycles = decision.get("cycles")
                if not isinstance(cycles, list) or not cycles:
                    raise ValueError(f"法术{spell_name}发动时必须提交至少一个cycles")
                if not flow.get("loop") and len(cycles) != 1:
                    raise ValueError(f"法术{spell_name}不是循环法术，只能提交一个cycle")
                for cycle in cycles:
                    if not isinstance(cycle, list) or len(cycle) != len(flow["steps"]):
                        raise ValueError(f"法术{spell_name}每个cycle必须完整提交{len(flow['steps'])}步")
                    for entry, (daowen, role) in zip(cycle, flow["steps"]):
                        if not isinstance(entry, dict):
                            raise ValueError("法术步骤必须是对象")
                        x = entry.get("x")
                        expected_target = holder if role == "self" else attacker
                        expected_ref = reverse.get(id(expected_target))
                        if (not isinstance(x, int) or isinstance(x, bool) or x < 1
                                or entry.get("target_ref") != expected_ref):
                            raise ValueError(f"法术{spell_name}步骤必须提交合法x与target_ref")
                        calc = DaoWenEngine.resolve(daowen, x, target=expected_target, caster=holder)
                        if calc.get("cost_type") == "消耗":
                            mana -= calc.get("cost", 0)
                            if mana < 0:
                                raise ValueError(f"法术{spell_name}提交的法力不足")
                        hostile = self.state.on_player_side(holder) != self.state.on_player_side(expected_target)
                        if hostile:
                            if not isinstance(entry.get("dodge"), bool):
                                raise ValueError("敌对法术步骤必须显式提交dodge")
                            if entry["dodge"]:
                                speed_budget[expected_ref] -= 1
                                if speed_budget[expected_ref] < 0:
                                    raise ValueError("法术目标速度不足以闪避")

    def prepare_daowen_trigger_spells(self, actor: Entity) -> dict:
        refs = self._combat_entity_refs()
        result = {}
        for ref, holder in refs.items():
            if self.state.on_player_side(holder) == self.state.on_player_side(actor):
                continue
            flows = self._eligible_spell_flows(holder, "目标发动道纹前")
            if flows:
                result[ref] = [{"spell_name": name, "steps": [
                    {"daowen": daowen, "target_ref": next((r for r, e in refs.items() if e is actor), None),
                     "x": "positive integer", "dodge": "boolean"}
                    for daowen, _ in flow["steps"]]} for name, flow in flows.items()]
        return result

    def validate_daowen_trigger_spells(self, actor: Entity, submitted: Any,
                                       refs: dict[str, Entity]) -> None:
        expected = self.prepare_daowen_trigger_spells(actor)
        if not isinstance(submitted, dict) or set(submitted) != set(expected):
            raise ValueError(f"trigger_spell_choices必须覆盖{sorted(expected)}")
        actor_ref = next((ref for ref, entity in refs.items() if entity is actor), None)
        for holder_ref in expected:
            holder = refs[holder_ref]
            flows = self._eligible_spell_flows(holder, "目标发动道纹前")
            choices = submitted[holder_ref]
            if not isinstance(choices, dict) or set(choices) != set(flows):
                raise ValueError("目标发动道纹前的法术提交不完整")
            mana = holder.current_mana
            for spell_name, flow in flows.items():
                decision = choices[spell_name]
                if not isinstance(decision, dict) or not isinstance(decision.get("use"), bool):
                    raise ValueError(f"法术{spell_name}必须显式提交use")
                if not decision["use"]:
                    continue
                steps = decision.get("steps")
                if not isinstance(steps, list) or len(steps) != len(flow["steps"]):
                    raise ValueError(f"法术{spell_name}必须完整提交steps")
                for entry, (daowen, _) in zip(steps, flow["steps"]):
                    x = entry.get("x") if isinstance(entry, dict) else None
                    if (not isinstance(x, int) or isinstance(x, bool) or x < 1
                            or entry.get("target_ref") != actor_ref or not isinstance(entry.get("dodge"), bool)):
                        raise ValueError("法术步骤的x/target_ref/dodge非法")
                    calc = DaoWenEngine.resolve(daowen, x, target=actor, caster=holder)
                    if calc.get("cost_type") == "消耗":
                        mana -= calc.get("cost", 0)
                        if mana < 0:
                            raise ValueError(f"法术{spell_name}法力不足")

    def resolve_daowen_trigger_spells(self, actor: Entity, submitted: dict,
                                      refs: dict[str, Entity]) -> list[dict]:
        logs = []
        for holder_ref, choices in submitted.items():
            holder = refs[holder_ref]
            for spell_name, flow in self._eligible_spell_flows(holder, "目标发动道纹前").items():
                decision = choices[spell_name]
                if not decision["use"]:
                    continue
                previous_damage = 0
                for entry, (daowen, _) in zip(decision["steps"], flow["steps"]):
                    if daowen == "坠落" and not (actor.is_flying or actor.has_status("飞行") or actor.has_status("滑翔")):
                        continue
                    if daowen == "血债" and previous_damage > 0:
                        continue
                    calc = DaoWenEngine.resolve(daowen, entry["x"], target=actor, caster=holder)
                    if calc.get("cost_type") == "消耗" and not holder.spend_mana(calc.get("cost", 0)):
                        raise ValueError("法术结算法力不足")
                    if entry["dodge"]:
                        if actor.current_speed < 1:
                            raise ValueError("道纹行动者速度不足以闪避反应法术")
                        actor.current_speed -= 1
                        self._note_dodge(actor, entry.get("dodge_relic_target_ref"))
                        logs.append({"spell": spell_name, "daowen": daowen, "dodged": True})
                        previous_damage = 0
                        continue
                    execution = self.apply_daowen_effect(daowen, calc, holder, actor)
                    previous_damage = sum(effect.get("actual_damage", 0) for effect in execution.get("effects", []))
                    logs.append({"spell": spell_name, "daowen": daowen, "execution": execution})
        return logs

    def _resolve_spell_reactions(self, trigger: str, holder: Entity, attacker: Entity,
                                 submitted: dict, refs: dict[str, Entity]) -> list[dict]:
        flows = self._eligible_spell_flows(holder, trigger)
        logs = []
        for spell_name, flow in flows.items():
            decision = submitted[spell_name]
            if not decision["use"]:
                logs.append({"spell": spell_name, "used": False})
                continue
            for cycle_index, cycle in enumerate(decision["cycles"], 1):
                for entry, (daowen, role) in zip(cycle, flow["steps"]):
                    target = holder if role == "self" else attacker
                    if target is None or not target.is_alive:
                        logs.append({"spell": spell_name, "cycle": cycle_index,
                                     "daowen": daowen, "skipped": "目标已失效"})
                        continue
                    x = entry["x"]
                    calc = DaoWenEngine.resolve(daowen, x, target=target, caster=holder)
                    if calc.get("cost_type") == "消耗":
                        if not holder.spend_mana(calc.get("cost", 0)):
                            raise ValueError(f"法术{spell_name}结算时法力不足")
                    hostile = self.state.on_player_side(holder) != self.state.on_player_side(target)
                    if hostile and entry.get("dodge"):
                        target.current_speed -= 1
                        self._note_dodge(target, entry.get("dodge_relic_target_ref"))
                        logs.append({"spell": spell_name, "cycle": cycle_index,
                                     "daowen": daowen, "target": target.name, "dodged": True})
                        continue
                    execution = self.apply_daowen_effect(daowen, calc, holder, target)
                    logs.append({"spell": spell_name, "cycle": cycle_index, "daowen": daowen,
                                 "x": x, "target": target.name, "execution": execution})
        return logs

    # ========== 大流程：员工叛变 / 死之传承 ==========

    def check_employee_rebellion(self) -> dict:
        """
        员工叛变（[战终]检查）：所有[员工]攻击次数×攻击力相加，
        若 ≥ 轮回者当前生命 + 所有[朋友]攻击总值，则所有员工叛变夺取《死者之书》。
        """
        emps = [e for e in self.state.employees if e.is_alive]
        if not emps:
            return {"rebellion": False, "reason": "无员工"}
        emp_atk = sum(e.attack_count * e.attack_power for e in emps)
        friend_atk = sum(f.attack_count * f.attack_power for f in self.state.friends if f.is_alive)
        player_hp = self.state.player.current_hp if (self.state.player and self.state.player.is_alive) else 0
        threshold = player_hp + friend_atk
        if emp_atk >= threshold:
            return {"rebellion": True, "rebels": [e.name for e in emps],
                    "employee_attack_total": emp_atk, "threshold": threshold,
                    "options": ["镇压（与所有叛变员工开战）", "让利（本场每名员工工资+5碎片）", "急中生智（谈判）"]}
        return {"rebellion": False, "employee_attack_total": emp_atk, "threshold": threshold}

    def trigger_death_legacy(self, legacy: dict[str, str]) -> dict:
        """新增一页三段式遗言；每段必填且不得超过20字。"""
        required = ("trigger_point", "fork", "cost_budget")
        if not isinstance(legacy, dict):
            raise ValueError("遗言必须是包含 trigger_point/fork/cost_budget 的对象")
        if set(legacy) != set(required):
            raise ValueError("遗言字段必须且只能是 trigger_point/fork/cost_budget")

        normalized = {}
        for field_name in required:
            value = legacy[field_name]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"遗言字段 {field_name} 必须是非空字符串")
            value = value.strip()
            if len(value) > self.state.death_book_capacity:
                raise ValueError(
                    f"遗言字段 {field_name} 超过{self.state.death_book_capacity}字上限")
            normalized[field_name] = value

        self.state.death_book_legacies.append(normalized)
        return {
            "triggered": True,
            "legacy": normalized,
            "total_legacies": len(self.state.death_book_legacies),
        }

    def validate_battle_start_relic_choices(self, choices: dict) -> None:
        """校验所有可选[战始]遗物参数；缺项不得由计算层猜测或代选。"""
        if not isinstance(choices, dict):
            raise ValueError("relic_choices必须是对象")
        active = {r.name for r in self.state.relics if self.state.sealed_relics.get(r.name, 0) <= 0}
        player = self.state.player
        for name in ("折速法印", "三相残韵盘"):
            if name not in active:
                continue
            decision = choices.get(name)
            if not isinstance(decision, dict) or not isinstance(decision.get("use"), bool):
                raise ValueError(f"持有【{name}】时必须显式提交relic_choices.{name}.use布尔值")
            if not decision["use"]:
                continue
            if name == "折速法印":
                x = decision.get("x")
                if not isinstance(x, int) or isinstance(x, bool) or x < 1 or not player:
                    raise ValueError("折速法印x必须是正整数")
                self.validate_numeric_cost(
                    player, "疲惫", x, decision.get("cost_share_target_ref", ""))
            elif name == "三相残韵盘":
                resonance = decision.get("resonance_type", "")
                if resonance not in ("转换", "反转", "曲解") or self.state.resonance.get(resonance, 0) < 1:
                    raise ValueError("三相残韵盘必须显式选择一种当前持有的resonance_type")
        for name in ("猩红果实", "苍白之花"):
            if name in active:
                decision = choices.get(name)
                if not isinstance(decision, dict) or not isinstance(decision.get("use"), bool):
                    raise ValueError(f"持有【{name}】时必须显式提交use布尔值")
                if decision["use"] and name == "猩红果实":
                    self.validate_numeric_cost(
                        player, "流血", 10, decision.get("cost_share_target_ref", ""))
                if decision["use"] and name == "苍白之花":
                    self.validate_numeric_cost(
                        player, "疲惫", 5, decision.get("cost_share_target_ref", ""))
        refs = self._combat_entity_refs()
        if "负岳索" in active:
            decision = choices.get("负岳索")
            legal = {ref for ref, entity in refs.items()
                     if entity in self.state.friends + self.state.employees and entity.is_alive}
            if not isinstance(decision, dict) or decision.get("target_ref") not in legal:
                raise ValueError(f"负岳索必须显式选择朋友/员工target_ref，可选{sorted(legal)}")
        if "炉心坠" in active:
            decision = choices.get("炉心坠")
            hearts = {item.name for item in self.state.consumables if item.kind == "dragon_heart"}
            if not isinstance(decision, dict) or decision.get("heart_name") not in hearts:
                raise ValueError(f"炉心坠必须显式选择龙心heart_name，可选{sorted(hearts)}")
        if "烙痕钉" in active:
            decision = choices.get("烙痕钉")
            legal = {ref for ref, entity in refs.items() if self.state.on_enemy_side(entity)}
            if not isinstance(decision, dict) or decision.get("target_ref") not in legal:
                raise ValueError(f"烙痕钉必须显式选择敌方target_ref，可选{sorted(legal)}")

    def validate_round_start_relic_choices(self, choices: dict) -> None:
        if not isinstance(choices, dict):
            raise ValueError("relic_choices必须是对象")
        active = {r.name for r in self.state.relics if self.state.sealed_relics.get(r.name, 0) <= 0}
        player = self.state.player
        damage = 3 * max(0, player.speed_limit - player.current_speed) if player else 0
        if "回锋刀" in active and damage > 0:
            decision = choices.get("回锋刀")
            index = decision.get("enemy_index") if isinstance(decision, dict) else None
            if (not isinstance(index, int) or isinstance(index, bool) or index < 0
                    or index >= len(self.state.enemies) or not self.state.enemies[index].is_alive):
                legal = [i for i, enemy in enumerate(self.state.enemies) if enemy.is_alive]
                raise ValueError(f"回锋刀触发时必须显式提交合法relic_choices.回锋刀.enemy_index，可选{legal}")
        if "余火印" in active:
            decision = choices.get("余火印")
            if not isinstance(decision, dict) or not isinstance(decision.get("use"), bool):
                raise ValueError("余火印每个回始必须显式提交use布尔值")
            if decision["use"]:
                heart = next((item for item in self.state.consumables
                              if item.name == decision.get("heart_name") and item.kind == "dragon_heart"), None)
                x = decision.get("x")
                if (heart is None or not isinstance(x, int) or isinstance(x, bool)
                        or x < 1 or x > heart.current_uses):
                    raise ValueError("余火印必须提交合法heart_name与1~当前耐久的x")
        def _validate_blood_pact_decision(holder: Entity, key: str, allow_heart: bool):
            decision = choices.get(key)
            if not isinstance(decision, dict) or not isinstance(decision.get("use"), bool):
                raise ValueError(f"{key}每个回始必须显式提交use布尔值")
            if not decision["use"]:
                return
            x = decision.get("x")
            heart_use = decision.get("dragon_heart_use", 0)
            if not isinstance(x, int) or isinstance(x, bool) or x < 1:
                raise ValueError(f"{key}.x必须是正整数")
            if (not isinstance(heart_use, int) or isinstance(heart_use, bool)
                    or heart_use < 0 or (heart_use and not allow_heart)):
                raise ValueError(f"{key}.dragon_heart_use非法")
            heart = next((item for item in self.state.consumables
                          if item.kind == "dragon_heart" and item.dragon_heart_type == "流血"
                          and not item.is_depleted), None) if allow_heart else None
            offset = min(heart_use, heart.current_uses, 4 * x) if heart else 0
            self.validate_numeric_cost(
                holder, "流血", 4 * x - offset,
                decision.get("cost_share_target_ref", ""),
            )

        if "血契" in active:
            _validate_blood_pact_decision(player, "血契", True)
        opponent = next((entity for entity in self.state.enemies
                         if entity.entity_type == "轮回者" and entity.is_alive), None)
        if opponent is not None and self._has_active_blood_pact(opponent):
            _validate_blood_pact_decision(opponent, "对手血契", False)

    def process_relics(self, trigger: str | TriggerTiming, ctx: dict = None) -> list:
        """遗物效果触发框架。可选效果只读取AI显式提交的ctx，不设置启发式默认值。"""
        timing_map = {
            TriggerTiming.BATTLE_START: "battle_start", TriggerTiming.BATTLE_END: "battle_end",
            TriggerTiming.ROUND_START: "round_start", TriggerTiming.ROUND_END: "round_end",
        }
        trigger = timing_map.get(trigger, trigger)
        ctx = ctx or {}
        player = self.state.player
        logs = []
        if not player:
            return logs
        # 抵扣X（F2）：被封印的遗物在封印期间不触发任何效果
        relics = {r.name for r in self.state.relics if self.state.sealed_relics.get(r.name, 0) <= 0}

        if trigger == "on_dodge" and "避风铃" in relics:
            player.gain_shield(3); logs.append("避风铃：闪避+3格挡")
        if trigger == "on_speed_zero" and "避风铃" in relics and player.current_speed == 0:
            player.gain_shield(15); logs.append("避风铃：速度归零+15格挡")
        if trigger == "round_start":
            choices = ctx.get("relic_choices", {})
            self.validate_round_start_relic_choices(choices)
            if "回锋刀" in relics:
                d = 3 * max(0, player.speed_limit - player.current_speed)
                if d > 0:
                    enemy = self.state.enemies[choices["回锋刀"]["enemy_index"]]
                    self._apply_hostile_damage(enemy, d, source=player)
                    logs.append(f"回锋刀：对{enemy.name}造{d}伤")
            if "守夜灯" in relics:  # 敌回始+法限50%法力
                g = math.ceil(player.mana_limit / 2)
                if g > 0:
                    player.current_mana += g
                    self.clamp_immortal_body(player)
                    logs.append(f"守夜灯：+{g}法力")
            if "血契" in relics and choices["血契"]["use"]:
                decision = choices["血契"]
                x = decision["x"]
                payment = self.pay_numeric_cost(
                    player, "流血", 4 * x,
                    cost_share_target_ref=decision.get("cost_share_target_ref", ""),
                    dragon_heart_use=decision.get("dragon_heart_use", 0),
                )
                player.current_mana += x
                self.clamp_immortal_body(player)
                shared = payment.get("shared_with")
                shared_note = f"，与{shared['payer']}共同承担" if shared else ""
                logs.append(f"血契：流血{4*x}{shared_note}，+{x}法力")
            opponent = next((entity for entity in self.state.enemies
                             if entity.entity_type == "轮回者" and entity.is_alive), None)
            if (opponent is not None and self._has_active_blood_pact(opponent)
                    and choices["对手血契"]["use"]):
                decision = choices["对手血契"]
                x = decision["x"]
                payment = self.pay_numeric_cost(
                    opponent, "流血", 4 * x,
                    cost_share_target_ref=decision.get("cost_share_target_ref", ""))
                opponent.current_mana += x
                self.clamp_immortal_body(opponent)
                shared = payment.get("shared_with")
                shared_note = f"，与{shared['payer']}共同承担" if shared else ""
                logs.append(f"对手血契：流血{4*x}{shared_note}，+{x}法力")
            if "余火印" in relics and choices["余火印"]["use"]:
                x = choices["余火印"]["x"]
                heart = next(item for item in self.state.consumables
                             if item.name == choices["余火印"]["heart_name"] and item.kind == "dragon_heart")
                heart.current_uses -= x
                player.current_mana += 2 * x
                self.clamp_immortal_body(player)
                logs.append(f"余火印：消耗{heart.name}耐久{x}，+{2*x}法力")
        if trigger == "battle_start":
            choices = ctx.get("relic_choices", {})
            self.validate_battle_start_relic_choices(choices)
            if "折速法印" in relics and choices["折速法印"]["use"]:
                decision = choices["折速法印"]
                x = decision["x"]
                self.pay_numeric_cost(
                    player, "疲惫", x,
                    cost_share_target_ref=decision.get("cost_share_target_ref", ""))
                player.current_mana += 6 * x
                self.clamp_immortal_body(player)
                logs.append(f"折速法印：疲惫{x}，+{6*x}法力")
            if "三相残韵盘" in relics and choices["三相残韵盘"]["use"]:
                consume = choices["三相残韵盘"]["resonance_type"]
                self.state.resonance[consume] -= 1
                self._sanxiang_consumed = consume
                logs.append(f"三相残韵盘：消耗{consume}残韵")
            if "猩红果实" in relics and choices["猩红果实"]["use"]:
                decision = choices["猩红果实"]
                self.pay_numeric_cost(
                    player, "流血", 10,
                    cost_share_target_ref=decision.get("cost_share_target_ref", ""))
                self.state.event_modifiers["scarlet_fruit_active"] = True
                logs.append("猩红果实：流血10；战终血限+2")
            if "苍白之花" in relics and choices["苍白之花"]["use"]:
                decision = choices["苍白之花"]
                self.pay_numeric_cost(
                    player, "疲惫", 5,
                    cost_share_target_ref=decision.get("cost_share_target_ref", ""))
                self.state.event_modifiers["pale_flower_active"] = True
                logs.append("苍白之花：疲惫5；战终精力+1")
            if "缄默面具" in relics:
                x = self.state.event_modifiers.get("silent_mask_x", 0)
                player.current_mana += 20 * x
                self.clamp_immortal_body(player)
                logs.append(f"缄默面具：+{20*x}法力")
            if "帮派令" in relics:
                player.add_status(StatusEffect("洗劫", 3, 3, "帮派令"))
                logs.append("帮派令：获得洗劫3")
            refs = self._combat_entity_refs()
            if "负岳索" in relics:
                target = refs[choices["负岳索"]["target_ref"]]
                target.add_status(StatusEffect("负岳索", -1, 1, "负岳索"))
                logs.append(f"负岳索：保护{target.name}首次受伤")
            if "炉心坠" in relics:
                heart = next(item for item in self.state.consumables
                             if item.name == choices["炉心坠"]["heart_name"] and item.kind == "dragon_heart")
                heart.current_uses += 10; heart.max_uses += 10
                logs.append(f"炉心坠：{heart.name}耐久+10")
            if "烙痕钉" in relics:
                self.state.event_modifiers["brand_nail_target_ref"] = choices["烙痕钉"]["target_ref"]
                logs.append("烙痕钉：已锁定目标")
            for ally in self.state.friends + self.state.employees:
                if ally.is_alive and any(relic.name == "防弹插板" for relic in ally.relics):
                    ally.gain_shield(15)
                    logs.append(f"防弹插板：{ally.name}+15格挡")
        if trigger == "battle_end" and "三相残韵盘" in relics and self._sanxiang_consumed:
            others = [t for t in ("转换", "反转", "曲解") if t != self._sanxiang_consumed]
            for t in others:
                self.state.resonance[t] = self.state.resonance.get(t, 0) + 1
            logs.append(f"三相残韵盘：战终获得{'、'.join(others)}残韵各1")
        # 钱袋：改在 api.py 的 _action_battle_end 里随标准击杀奖励一并结算(用battle_start_blood_limit快照)，
        # 不再用"on_monster_death"这个从未被调用过的触发点(此前是死代码)。
        return logs

    # ========== 怪物回合（两阶段显式决策） ==========
    # 怪物已激活的道纹 / 已进化的怪物（均按战斗重置）
    _monster_activated: dict = {}
    _monster_evolved: set = set()  # 进化（原初X）：本场已进化的怪物 id 集合

    def reset_monster_activation(self):
        """战始重置怪物激活状态与战斗遗物状态"""
        self._monster_activated = {}
        self._monster_evolved = set()  # 进化（原初X）：每场战斗限一次
        self._sanxiang_consumed = ""
        self._resonance_rewrites = {}

    def queue_resonance_rewrite(self, entity: Entity, source: str, dest: str) -> None:
        """残韵作用于他人道纹：登记其下一次发动该源道纹时按 dest 结算。"""
        bucket = self._resonance_rewrites.setdefault(id(entity), {})
        bucket[source] = dest

    def consume_resonance_rewrite(self, entity: Entity, source: str) -> Optional[str]:
        bucket = self._resonance_rewrites.get(id(entity)) or {}
        dest = bucket.pop(source, None)
        if dest and not bucket:
            self._resonance_rewrites.pop(id(entity), None)
        return dest

    def _monster_attack_actions(self, m: Entity, activated: set) -> int:
        """怪物攻击出手数 = 1 + 活力X(若激活) + 狂暴1(若激活)。

        高爆手雷修改的是每轮攻击中的“攻击次数”，不再同时削减攻击出手数。
        """
        n = 1
        if "活力" in activated:
            n += m.dao_wen["活力"].x_value
        if "狂暴" in activated:
            n += 1
        return max(0, n)

    def _combat_entity_refs(self) -> dict[str, Entity]:
        """为两阶段决策提供本场稳定的显式目标引用，避免同名实体歧义。"""
        refs: dict[str, Entity] = {}
        if self.state.player and self.state.player.is_alive:
            refs["player:0"] = self.state.player
        for prefix, entities in (
            ("friend", self.state.friends),
            ("employee", self.state.employees),
            ("temp_friend", self.state.temp_friends),
            ("enemy", self.state.enemies),
        ):
            for i, entity in enumerate(entities):
                if not entity.is_alive or entity.has_retreated:
                    continue
                if prefix == "employee" and not entity.is_deployed:
                    continue
                refs[f"{prefix}:{i}"] = entity
        return refs

    def _daowen_requires_target(self, name: str) -> bool:
        import inspect
        DaoWenEngine.register_all()
        func = DaoWenEngine._registry.get(name)
        return bool(func and "target" in inspect.signature(func).parameters)

    def prepare_monster_phase(self) -> dict:
        """只枚举合法选择，不决定道纹、目标或闪避，也不改变战斗数值。"""
        refs = self._combat_entity_refs()
        player_refs = [
            {"ref": ref, "name": e.name}
            for ref, e in refs.items()
            if self.state.on_player_side(e)
        ]
        all_targets = [{"ref": ref, "name": e.name} for ref, e in refs.items()]
        # 白板回合（首回合怪物只普攻不出道纹）不适用于死斗：守擂主将是轮回者，
        # 与挑战者同样应首回合就能发动道纹，否则挑战者首回合秒杀裸奔主将（不对称）。
        whiteboard = self.state.current_round <= 1 and not self.state.in_final_duel
        actors = []
        skipped = []
        for index, monster in enumerate(self.state.enemies):
            if not monster.is_alive or monster.removed_without_kill:
                continue
            actor_ref = f"enemy:{index}"
            if not self.can_act(monster):
                skipped.append({"actor_ref": actor_ref, "monster": monster.name, "reason": "无法行动"})
                continue
            activated = self._monster_activated.get(id(monster), set())
            daowen_options = []
            if not whiteboard and not monster.has_status("干扰"):
                for name, inst in monster.dao_wen.items():
                    if name in activated or not inst.can_use() or name not in DaoWenEngine.list_all():
                        continue
                    if name == "赌命" and getattr(monster, "fake_shards", 0) < inst.x_value:
                        continue
                    if (name == "消灾" and monster.fake_shards < 50 * inst.x_value
                            and monster.shards < 5 * inst.x_value):
                        continue
                    rewritten_as = (self._resonance_rewrites.get(id(monster)) or {}).get(name)
                    effective_name = rewritten_as or name
                    requires_target = self._daowen_requires_target(effective_name)
                    legal_targets = ([target for target in all_targets
                                      if self.is_targetable(monster, refs[target["ref"]])]
                                     if requires_target else [])
                    if "龙威" in self.state.dragon_traits and self.state.player and self.state.player.is_alive:
                        legal_targets = [target for target in legal_targets
                                         if (not self.state.on_player_side(refs[target["ref"]])
                                             or target["ref"] == "player:0")]
                    if requires_target and not legal_targets:
                        continue
                    # 过滤当前付不起数值代价的候选（改写后代价可能超出怪物资源）
                    preview_target = (refs[legal_targets[0]["ref"]] if legal_targets else monster)
                    preview_calc = DaoWenEngine.resolve(
                        effective_name, inst.x_value, target=preview_target, caster=monster)
                    if not self._monster_can_pay_calc_cost(monster, preview_calc):
                        continue
                    daowen_options.append({
                        "name": name,
                        "resolves_as": effective_name,
                        "x": inst.x_value,
                        "requires_target": requires_target,
                        "target_options": legal_targets,
                        "dodge_submission": ("per_target" if effective_name == "冲击"
                                             else ("single_if_hostile" if requires_target else "none")),
                        "dodge_target_options": player_refs if effective_name == "冲击" else [],
                        "trigger_spell_options": self.prepare_daowen_trigger_spells(monster),
                    })
            attack_targets = [
                target for target in player_refs
                if self.is_targetable(monster, refs[target["ref"]])
            ]
            # 【龙威】是规则约束而非策略默认：敌方只能把持有者列为合法攻击目标。
            if "龙威" in self.state.dragon_traits and self.state.player and self.state.player.is_alive:
                attack_targets = [target for target in attack_targets if target["ref"] == "player:0"]
            for target_option in attack_targets:
                entity = refs[target_option["ref"]]
                blood_pact_options = self.blood_shadow_cost_share_options(entity)
                target_option["can_blood_shadow"] = (
                    self.state.side_has(entity, "血影")
                    and (entity.current_hp > 10 or bool(blood_pact_options)))
                target_option["blood_shadow_cost_share_target_options"] = blood_pact_options
                target_option["spell_options"] = self.prepare_spell_reactions(entity, monster)
                target_option["dodge_relic_target_options"] = [
                    candidate for candidate in all_targets
                    if self.state.on_player_side(refs[candidate["ref"]]) != self.state.on_player_side(entity)
                ] if self.state.side_has(entity, "回锋刀") else []
            actors.append({
                "actor_ref": actor_ref,
                "monster": monster.name,
                "daowen_required": bool(daowen_options),
                "daowen_options": daowen_options,
                "attack_target_options": attack_targets,
                "base_attack_actions": self._monster_attack_actions(monster, activated),
                "base_hits_per_attack": max(0, monster.attack_count - monster.get_status_value("手雷减攻")),
                "dodge_must_be_explicit": True,
            })
        return {"round": self.state.current_round, "actors": actors, "skipped": skipped}

    def _monster_can_pay_calc_cost(self, caster: Entity, calc: dict) -> bool:
        """怪物发动道纹前校验数值代价是否付得起（不实际支付）。

        残韵改写/状态变化可能让怪物付不起代价（如速度归零后发动
        【洞察】(疲惫3)）——付不起就不该被 prepare 列为合法项，
        也不该在 resolve 时硬报错。
        """
        if caster is None or not caster.entity_type == "怪物":
            return True
        for key, capacity in (
                ("cost_hp", getattr(caster, "current_hp", 0)),
                ("cost_blood_limit", getattr(caster, "blood_limit", 0)),
                ("cost_speed", getattr(caster, "current_speed", 0)),
        ):
            amount = calc.get(key, 0)
            if amount and capacity < amount:
                return False
        return True

    def _resolve_monster_daowen_choice(
        self, monster: Entity, choice: dict, refs: dict[str, Entity], activated: set,
        prepared_option: dict,
    ) -> dict:
        name = choice.get("name", "")
        inst = monster.dao_wen.get(name)
        if inst is None or name in activated or not inst.can_use():
            raise ValueError(f"{monster.name}不能发动道纹【{name}】")
        if name not in DaoWenEngine.list_all():
            raise ValueError(f"未知道纹【{name}】")
        rewritten_as = (self._resonance_rewrites.get(id(monster)) or {}).get(name)
        effective_name = rewritten_as or name
        requires_target = self._daowen_requires_target(effective_name)
        target_ref = choice.get("target_ref", "")
        if requires_target:
            target = refs.get(target_ref)
            if target is None:
                raise ValueError(f"道纹【{effective_name}】必须提交合法target_ref")
            if target is not monster and not self.is_targetable(monster, target):
                raise ValueError(f"目标{target.name}当前不可被{monster.name}选中")
        else:
            if target_ref:
                raise ValueError(f"道纹【{effective_name}】不接受target_ref")
            target = monster

        # 先完成完整闪避提交的静态校验，再支付任何代价或改变激活状态。
        calc = DaoWenEngine.resolve(effective_name, inst.x_value, target=target, caster=monster)
        # 动态代价校验：残韵改写/状态变化后怪物可能付不起代价（如速度归零后
        # 【洞察】(疲惫3)）——本次视为无法发动并跳过，不硬报错、不占出手。
        if not self._monster_can_pay_calc_cost(monster, calc):
            return {"monster": monster.name, "daowen_skipped": name,
                    "resolves_as": effective_name, "reason": "无法支付代价"}
        hostile = self.state.on_player_side(target) != self.state.on_player_side(monster)
        dodge = choice.get("dodge")
        blood_shadow = choice.get("blood_shadow", False)
        aoe_dodge_choices: list[tuple[Entity, bool, dict]] = []
        must_hit_preview = self.bizhong_remaining(monster) > 0
        if effective_name == "冲击":
            submitted_dodges = choice.get("dodge_targets")
            if not isinstance(submitted_dodges, list):
                raise ValueError("道纹【冲击】必须为全部敌对目标显式提交dodge_targets")
            expected_ref_list = [
                target.get("ref") for target in prepared_option.get("dodge_target_options", [])
            ]
            if (any(not isinstance(ref, str) for ref in expected_ref_list)
                    or len(expected_ref_list) != len(set(expected_ref_list))):
                raise ValueError("prepare返回的冲击闪避目标快照无效")
            expected_refs = set(expected_ref_list)
            received: dict[str, dict] = {}
            for entry in submitted_dodges:
                if (not isinstance(entry, dict) or not isinstance(entry.get("dodge"), bool)
                        or not isinstance(entry.get("blood_shadow"), bool)
                        or not isinstance(entry.get("target_ref"), str)
                        or entry["dodge"] and entry["blood_shadow"]):
                    raise ValueError("dodge_targets每项必须包含target_ref与布尔值dodge/blood_shadow")
                ref = entry["target_ref"]
                if ref in received:
                    raise ValueError(f"重复提交闪避目标: {ref}")
                received[ref] = entry
            if set(received) != expected_refs:
                raise ValueError(f"冲击必须覆盖全部敌对目标；需要{sorted(expected_refs)}")
            for ref in expected_ref_list:
                entity = refs.get(ref)
                if entity is None or not self.state.on_player_side(entity):
                    raise ValueError("prepare中的冲击目标已失效，请重新prepare_monster_phase")
                entry = received[ref]
                want_dodge = entry["dodge"]
                if want_dodge and not must_hit_preview and entity.current_speed < 1:
                    raise ValueError(f"{entity.name}速度不足，不能选择闪避")
                if entry["blood_shadow"] and (must_hit_preview or not self.state.side_has(entity, "血影")
                                                or entity.current_hp <= 10):
                    raise ValueError(f"{entity.name}不能使用血影")
                if want_dodge and not must_hit_preview and self.state.side_has(entity, "回锋刀"):
                    allowed = {target["ref"] for target in prepared_option["target_options"]
                               if not self.state.on_player_side(refs[target["ref"]])}
                    if entry.get("dodge_relic_target_ref") not in allowed:
                        raise ValueError("回锋刀触发必须显式提交合法目标")
                aoe_dodge_choices.append((entity, want_dodge, entry))
            if dodge not in (None, False):
                raise ValueError("冲击使用dodge_targets，不接受dodge=true")
        elif requires_target and hostile:
            if not isinstance(dodge, bool) or not isinstance(blood_shadow, bool):
                raise ValueError(f"道纹【{effective_name}】必须显式提交布尔值dodge/blood_shadow")
            if dodge and blood_shadow:
                raise ValueError("不能同时闪避并使用血影")
            if dodge and not must_hit_preview and target.current_speed < 1:
                raise ValueError(f"{target.name}速度不足，不能选择闪避")
            if blood_shadow and (must_hit_preview or not self.state.side_has(target, "血影")
                                 or target.current_hp <= 10):
                raise ValueError(f"{target.name}不能使用血影")
            if dodge and not must_hit_preview and self.state.side_has(target, "回锋刀"):
                allowed = {entry["ref"] for entry in prepared_option["target_options"]
                           if self.state.on_player_side(refs[entry["ref"]]) != self.state.on_player_side(target)}
                if choice.get("dodge_relic_target_ref") not in allowed:
                    raise ValueError("回锋刀触发必须显式提交合法目标")
            if choice.get("dodge_targets") not in (None, []):
                raise ValueError(f"道纹【{effective_name}】不接受dodge_targets")
        else:
            if (dodge not in (None, False) or blood_shadow not in (None, False)
                    or choice.get("dodge_targets") not in (None, [])):
                raise ValueError(f"道纹【{effective_name}】当前结算不接受闪避提交")

        trigger_choices = choice.get("trigger_spell_choices", {})
        self.validate_daowen_trigger_spells(monster, trigger_choices, refs)
        trigger_logs = self.resolve_daowen_trigger_spells(monster, trigger_choices, refs)
        if not monster.is_alive:
            return {"monster": monster.name, "daowen_activated": name,
                    "interrupted_by_spell": True, "trigger_spell_logs": trigger_logs}

        # 残韵改写只替换本次结算：不支付源道纹代价，也不将源道纹记为已激活。
        if rewritten_as:
            consumed = self.consume_resonance_rewrite(monster, name)
            if consumed != rewritten_as:
                raise ValueError("残韵改写已变化，请重新prepare_monster_phase")
        # 原始怪物道纹发动时支付异变5X；选择导致崩解仍是合法结算，效果中断。
        elif name in self.ORIGINAL_MONSTER_DAOWEN:
            paid = monster.add_mutation(self.YUANCHU_COST_RATE * inst.x_value)
            if paid["collapsed"]:
                return {"monster": monster.name, "collapsed": name,
                        "note": "支付异变后触发【崩解】，道纹效果中断"}
        elif name == "赌命":
            if monster.fake_shards < inst.x_value:
                raise ValueError(f"{monster.name}假碎片不足，不能发动【赌命】")
            monster.fake_shards -= inst.x_value
        elif name == "消灾":
            fake_cost, real_cost = 50 * inst.x_value, 5 * inst.x_value
            if monster.fake_shards >= fake_cost:
                monster.fake_shards -= fake_cost
            elif monster.shards >= real_cost:
                monster.shards -= real_cost
            else:
                raise ValueError(f"{monster.name}碎片不足，不能发动【消灾】")

        if not rewritten_as:
            activated.add(name)
        monster.actions_used_this_round += 1

        aoe_targets_override = None
        if effective_name == "冲击":
            if must_hit_preview:
                self.consume_bizhong(monster)
                aoe_targets_override = [entity for entity, _, _ in aoe_dodge_choices]
            else:
                aoe_targets_override = []
                for entity, want_dodge, entry in aoe_dodge_choices:
                    if entry["blood_shadow"]:
                        self.pay_numeric_cost(
                            entity, "流血", 10,
                            cost_share_target_ref=entry.get("cost_share_target_ref", ""))
                    elif want_dodge:
                        entity.current_speed -= 1
                        self._note_dodge(entity, entry.get("dodge_relic_target_ref"))
                    else:
                        aoe_targets_override.append(entity)
        elif requires_target and hostile:
            if must_hit_preview:
                self.consume_bizhong(monster)
            elif blood_shadow:
                self.pay_numeric_cost(
                    target, "流血", 10,
                    cost_share_target_ref=choice.get("cost_share_target_ref", ""))
                return {"monster": monster.name, "daowen_activated": name,
                        "resolves_as": effective_name, "target": target.name, "blood_shadow": True,
                        "trigger_spell_logs": trigger_logs}
            elif dodge:
                target.current_speed -= 1
                self._note_dodge(target, choice.get("dodge_relic_target_ref"))
                return {"monster": monster.name, "daowen_activated": name,
                        "resolves_as": effective_name, "target": target.name, "dodged": True,
                        "trigger_spell_logs": trigger_logs}

        execution = self.apply_daowen_effect(
            effective_name, calc, monster, target,
            aoe_targets_override=aoe_targets_override,
        )
        return {"monster": monster.name, "daowen_activated": name, "x": inst.x_value,
                "resolves_as": effective_name, "resonance_rewrite": bool(rewritten_as),
                "target": target.name, "execution": execution,
                "trigger_spell_logs": trigger_logs}

    def resolve_monster_phase(self, choices: list[dict], prepared: dict) -> list[dict]:
        """严格按传入的prepare快照验证并结算；任何非法输入由API事务整体回滚。"""
        if not isinstance(choices, list):
            raise ValueError("choices必须是列表")
        if not isinstance(prepared, dict) or not isinstance(prepared.get("actors"), list):
            raise ValueError("prepared必须是prepare_monster_phase返回的合法快照")
        expected = {actor["actor_ref"]: actor for actor in prepared["actors"]}
        submitted: dict[str, dict] = {}
        for choice in choices:
            if not isinstance(choice, dict):
                raise ValueError("每个怪物选择必须是对象")
            ref = choice.get("actor_ref", "")
            if ref in submitted:
                raise ValueError(f"重复提交怪物选择: {ref}")
            submitted[ref] = choice
        # 死斗交替（对称）：守擂侧每步只结算1个actor，其余本步不动
        # （逐出手交替与挑战者侧一致，修复守擂方机制性必胜）。
        if not self.state.in_final_duel and set(submitted) != set(expected):
            raise ValueError(f"必须为全部可行动怪物各提交一次选择；需要{sorted(expected)}，收到{sorted(submitted)}")

        refs = self._combat_entity_refs()
        results: list[dict] = []
        results.extend(self._tick_baolie(self.state.get_all_enemy_side()))
        results.extend(prepared["skipped"])
        for actor_ref in submitted:  # 死斗部分提交：只结算本步提交的actor
            monster = refs.get(actor_ref)
            if monster is None or not monster.is_alive:
                continue
            choice = submitted[actor_ref]
            breath = self.apply_opposing_longxi(monster)
            if breath:
                results.append({"monster": monster.name, **breath})
                if not monster.is_alive:
                    continue
            activated = self._monster_activated.setdefault(id(monster), set())
            # 攻击出手数以“道纹结算前”的已激活集合为准：狂暴/活力是[回始]持续效果，
            # 本回合刚发动时从下回合起生效，prepare列出的 base_attack_actions 也是按
            # 结算前状态给出的——两处必须一致，否则按 prepare 提交必然失败。
            activated_before = set(activated)

            dao_choice = choice.get("daowen")
            options = {o["name"] for o in expected[actor_ref]["daowen_options"]}
            if options and not isinstance(dao_choice, dict):
                raise ValueError(f"{monster.name}必须从合法选项中提交一个daowen对象")
            if not options and dao_choice is not None:
                raise ValueError(f"{monster.name}本次没有合法道纹选项，daowen必须为null")
            if isinstance(dao_choice, dict):
                if dao_choice.get("name") not in options:
                    raise ValueError(f"{monster.name}提交的道纹不在prepare合法选项中")
                prepared_option = next(
                    option for option in expected[actor_ref]["daowen_options"]
                    if option["name"] == dao_choice["name"]
                )
                if (prepared_option["requires_target"]
                        and dao_choice.get("target_ref") not in {
                            target["ref"] for target in prepared_option["target_options"]
                        }):
                    raise ValueError(f"{monster.name}提交的道纹目标不在prepare合法选项中")
                dao_result = self._resolve_monster_daowen_choice(
                    monster, dao_choice, refs, activated, prepared_option,
                )
                results.append(dao_result)
                if not monster.is_alive:
                    continue

            attack_actions = choice.get("attack_actions")
            expected_actions = self._monster_attack_actions(monster, activated_before)
            if not isinstance(attack_actions, list) or len(attack_actions) != expected_actions:
                raise ValueError(f"{monster.name}必须提交{expected_actions}个attack_actions")
            hits_per_action = max(0, monster.attack_count - monster.get_status_value("手雷减攻"))
            for action_index, attack_action in enumerate(attack_actions):
                if not isinstance(attack_action, dict) or not isinstance(attack_action.get("hits"), list):
                    raise ValueError("每个attack_action必须包含hits列表")
                hits = attack_action["hits"]
                if len(hits) != hits_per_action:
                    raise ValueError(f"{monster.name}每个攻击出手必须提交{hits_per_action}次命中选择")
                monster.actions_used_this_round += 1
                legal_attack_options = {
                    target["ref"]: target for target in expected[actor_ref]["attack_target_options"]
                }
                for hit_index, hit in enumerate(hits):
                    if (not isinstance(hit, dict) or not isinstance(hit.get("dodge"), bool)
                            or not isinstance(hit.get("blood_shadow"), bool)):
                        raise ValueError("每次攻击必须显式提交target_ref、dodge与blood_shadow")
                    if hit["dodge"] and hit["blood_shadow"]:
                        raise ValueError("同一次判定不能同时闪避并使用血影")
                    if hit.get("target_ref") not in legal_attack_options:
                        raise ValueError("怪物攻击目标不在prepare合法选项中")
                    target = refs.get(hit.get("target_ref", ""))
                    if target is None or not self.state.on_player_side(target):
                        raise ValueError("怪物攻击target_ref必须是prepare列出的己方目标")
                    if not self.is_targetable(monster, target):
                        raise ValueError(f"{target.name}当前不可被{monster.name}选中")
                    if not target.is_alive:
                        results.append({"attacker": monster.name, "target": target.name,
                                        "skipped": "预选目标已命零", "hit_index": hit_index + 1})
                        continue
                    must_hit = self.bizhong_remaining(monster) > 0
                    if hit["dodge"] and not must_hit and target.current_speed < 1:
                        raise ValueError(f"{target.name}速度不足，不能选择闪避")
                    option = legal_attack_options[hit["target_ref"]]
                    if hit["blood_shadow"] and not option.get("can_blood_shadow"):
                        raise ValueError(f"{target.name}不能使用血影")
                    if hit["dodge"] and not must_hit and self.state.side_has(target, "回锋刀"):
                        allowed = {entry["ref"] for entry in option["dodge_relic_target_options"]}
                        if hit.get("dodge_relic_target_ref") not in allowed:
                            raise ValueError("回锋刀触发必须显式提交合法目标")
                    self.validate_spell_reaction_submission(
                        target, monster, hit.get("spell_choices"), refs,
                    )
                    attack_target = monster if monster.has_status("无神") else target
                    resolved = self.resolve_attack(
                        monster, attack_target, dodge=hit["dodge"], blood_shadow=hit["blood_shadow"],
                        spell_choices=hit.get("spell_choices"), entity_refs=refs,
                        dodge_relic_target_ref=hit.get("dodge_relic_target_ref"),
                        cost_share_target_ref=hit.get("cost_share_target_ref", ""),
                    )
                    resolved.update({"hit_index": hit_index + 1, "hit_total": hits_per_action,
                                     "attack_action_index": action_index + 1,
                                     "new_action": (hit_index == 0)})
                    results.append(resolved)
                    if not monster.is_alive:
                        break
                if not monster.is_alive:
                    break
        return results

    def buyaicai_escape_cost(self, monster: Entity) -> dict:
        """买路财：失去等同于怪物20%[血限]的[碎片]可安全撤退；碎片不足可用2生命=1碎片补"""
        if not monster:
            return {"can_escape": False, "reason": "无目标"}
        cost = math.ceil(monster.blood_limit * 0.2)
        short = max(0, cost - self.state.shards)
        life_cost = short * 2  # 1碎片=2生命
        return {"can_escape": True, "shard_cost": cost, "shortfall_shards": short,
                "extra_life_cost": life_cost}

    def apply_opposing_longxi(self, actor: Entity) -> Optional[dict]:
        """若对方持有龙息，actor 行动前受 10×当前回合必中伤害。"""
        if actor is None or not actor.is_alive:
            return None
        foe_has = False
        if self.state.on_player_side(actor):
            foe_has = "龙息" in self.state.opponent_dragon_traits
        elif self.state.on_enemy_side(actor):
            foe_has = "龙息" in self.state.dragon_traits
        if not foe_has:
            return None
        dmg = 10 * max(1, self.state.current_round)
        source = (self.state.player if self.state.on_enemy_side(actor)
                  else next((e for e in self.state.enemies if e.entity_type == "轮回者"), None))
        detail = self._apply_hostile_damage(actor, dmg, "必中", source)
        detail["dragon_breath"] = dmg
        return detail

    def can_act(self, entity: Entity) -> bool:
        """是否可出手（眩晕/束缚/缓慢下不可）"""
        return (entity.is_alive
                and not entity.has_status("眩晕")
                and not entity.has_status("束缚")
                and not entity.has_status("缓慢"))

    def is_targetable(self, attacker: Entity, target: Entity) -> bool:
        """目标是否可被选中。滑翔视同飞行；坠落压住全场飞行。"""
        if self._field_has_zhuiluo() or target.has_status("坠落"):
            return True
        if self._is_flying(target):
            return self._is_flying(attacker)
        return True

    def _get_combat_state(self) -> dict:
        """获取当前战斗状态摘要"""
        return {
            "round": self.state.current_round,
            "player_side": [e.to_dict() for e in self.state.get_all_player_side()],
            "enemy_side": [e.to_dict() for e in self.state.get_all_enemy_side()],
        }
    
    def get_damage_preview(
        self, 
        attacker: Entity, 
        target: Entity, 
        daowen_name: str = None,
        x: int = 0
    ) -> dict:
        """
        伤害预览（不实际执行，只计算结果供AI参考）
        AI在决策前可以调用此方法预览道纹效果
        """
        if daowen_name:
            try:
                calc = DaoWenEngine.resolve(daowen_name, x, target=target, caster=attacker)
                return {
                    "type": "daowen_preview",
                    "daowen": daowen_name,
                    "x": x,
                    "calculation": calc,
                    "attacker": attacker.name,
                    "target": target.name
                }
            except Exception as e:
                return {"error": str(e)}
        else:
            # 普通攻击预览
            return {
                "type": "attack_preview",
                "attacker": attacker.name,
                "attack_power": attacker.attack_power,
                "attack_count": attacker.attack_count,
                "total_damage": attacker.attack_power * attacker.attack_count,
                "target": target.name,
                "target_hp": target.current_hp,
                "target_shield": target.shield,
                "can_kill": (target.current_hp - attacker.attack_power) <= 0
            }
