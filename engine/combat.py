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
from .enums import InterruptType, DamageType
from .dm_rulings import Interrupt


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
    # 持续型原始怪物道纹：效果持续期间每个[回始]重新支付异变5X（已裁定）。
    # 必中为次数型（下X次选择[目标]无法闪避），不参与回合计费；余数记在 entity._bizhong_left。
    SUSTAIN_MONSTER_DAOWEN = ("狂暴", "强化", "活力", "减速", "自愈", "飞行")
    YUANCHU_COST_RATE = 5  # 原初X代价：异变5X（已裁定）
    
    def __init__(self, state: GameState, dice: DiceEngine):
        self.state = state
        self.dice = dice
        self.combat_log: list[dict] = []  # 完整战斗日志
        # 卖身契代价替身 / 三相残韵盘本场消耗的残韵
        self.cost_proxy = None
        self._sanxiang_consumed = ""
        # 残韵改写：entity_id → {源道纹: 变化后道纹}，只改下一次发动结算，不改持有
        self._resonance_rewrites: dict[int, dict[str, str]] = {}
    
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
        if amount <= 0 or damage_type == "代价":
            return amount
        if target.has_status("加害"):
            amount += target.get_status_value("加害")
        if target.has_status("龙鳞"):
            amount = max(0, amount - target.get_status_value("龙鳞"))
        return amount

    def _gain_speed(self, entity: Entity, amount: int) -> int:
        if amount <= 0:
            return 0
        if entity.has_status("加速"):
            amount *= 2
        entity.current_speed += amount
        return amount

    def _note_dodge(self, entity: Entity) -> dict:
        extra = {}
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

    def _apply_hostile_damage(self, target: Entity, amount: int, damage_type: str = "普通") -> dict:
        """
        对target造成外部/敌对伤害的统一入口（供攻击与道纹伤害调用；自身【代价】不走此入口）。
        撤退（任意[朋友]/[员工]即将受到足以使当前命零的伤害时触发）：
        判定须扣除格挡后的实际伤害是否≥当前生命（格挡足够抵消则不触发撤退，也不触发死亡）；
        触发后本次伤害清零、目标保留当前生命与格挡、标记has_retreated退出本场战斗。
        负岳碑(终音法器)：若玩家已通过 declare_fuyuebei_toll(name) 预先声明保护该目标，
        且玩家当前生命>20，则改为玩家流血20，抵消本次伤害并取消本次撤退(目标不掉血也不撤退)。
        龙心谷专属（F2）：嫁祸/背负 重定向、逆鳞层数、伤痕血限衰减在此统一处理。
        """
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
                    return self._apply_hostile_damage(j_target, amount, damage_type)
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
                        return self._apply_hostile_damage(ent, amount, damage_type)

        if damage_type != "代价" and target.entity_type in ("朋友", "员工") and not target.has_retreated and target.is_alive:
            remaining_after_shield = max(0, amount - target.shield) if amount > 0 else 0
            if remaining_after_shield >= target.current_hp and target.current_hp > 0:
                player = self.state.player
                if (target.name in self.state.fuyuebei_declared and "负岳碑" in self.state.artifacts_owned
                        and player is not None and player.current_hp > 20):
                    self.state.fuyuebei_declared.remove(target.name)
                    self._pay_bleed_cost(player, 20)
                    return {
                        "raw_damage": amount, "shield_absorbed": 0, "actual_damage": 0,
                        "hp_before": target.current_hp, "hp_after": target.current_hp,
                        "blood_limit_before": target.blood_limit, "died": False,
                        "damage_type": damage_type, "retreated": False, "fuyuebei_toll_paid": 20,
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
                target.blood_limit = max(1, target.blood_limit - xv)
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
                    h = healer.heal(drain)
                    detail["jisheng_heal"] = {"healer": healer.name, **h}
                    cancer = self.check_cancer(healer)
                    if cancer:
                        detail["jisheng_cancer"] = cancer
        return detail

    # ---- F2 全量：罪孽/扭曲专属道纹的公共辅助 ----
    def _shards_of(self, entity: Entity) -> int:
        """实体当前的碎片池（玩家=state.shards；怪物/同伴=entity.shards）"""
        if entity is self.state.player:
            return self.state.shards
        return entity.shards

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

    def _pay_bleed_cost(self, payer: Entity, amount: int, dragon_heart_use: int = 0) -> dict:
        """
        支付"流血X"代价的统一入口（供道纹代价与鲜血契约等遗物自身的流血代价共用）。
        血誓戒：[回始]玩家首次主动支付流血代价时，获得等同于本次流血的格挡；
        若支付后生命≤30%[血限]，改为获得等量生命。仅玩家本人支付时生效(卖身契转嫁给他人不算)。
        dragon_heart_use：本次希望消耗"流血龙心"抵消的点数(龙心谷"炼心"产出)，抵消后剩余部分才真正支付。
        """
        actual, offset = self._offset_with_dragon_heart(payer, "流血", amount, dragon_heart_use)
        detail = payer.take_damage(actual, "代价")
        detail["dragon_heart_offset"] = offset
        if (payer is self.state.player and actual > 0 and not payer.blood_oath_used_this_round
                and any(r.name == "血誓戒" for r in self.state.relics)):
            payer.blood_oath_used_this_round = True
            if payer.blood_limit > 0 and payer.current_hp / payer.blood_limit <= 0.3:
                heal_detail = payer.heal(actual)
                detail["blood_oath"] = {"type": "life", "amount": heal_detail["actual_heal"]}
            else:
                payer.gain_shield(actual)
                detail["blood_oath"] = {"type": "shield", "amount": actual}
        self._bank_lianxin(payer, "流血", actual)
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
    ) -> dict:
        """
        解析一次攻击
        dodge: 目标是否选择闪避（由AI决策）
        blood_shadow: 目标是否选择用【血影】遗物(流血10取消本次判定)代替常规闪避
        """
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
        if (blood_shadow and not must_hit and self.state.side_has(target, "血影")
                and target.current_hp > 10):
            self._pay_bleed_cost(target, 10)
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
                extra = self._note_dodge(target)
                if extra:
                    result["dodge_extra"] = extra
                    result["speed_after_dodge"] = target.current_speed
                # 遗物：闪避触发（目标为轮回者时）
                if target is self.state.player:
                    result["relic_logs"] = self.process_relics("on_dodge")
                    if target.current_speed == 0:
                        result["relic_logs"] += self.process_relics("on_speed_zero")
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
        # 龙族血脉（真龙之心遗物）：对非怪物造成伤害翻倍（对怪物的秒杀效果在伤害结算后处理）
        if self.state.side_has(attacker, "龙族血脉") and target.entity_type != "怪物":
            damage *= 2

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
        # 法术：受到伤害前（玩家为目标时触发反应型法术，可能反杀攻击者或加盾）
        if self.state.player is not None and target is self.state.player and damage > 0:
            slogs = self.trigger_spells("受到伤害前", {"attacker": attacker, "target": target, "incoming": damage})
            if slogs:
                result["spell_logs"] = slogs
            if not attacker.is_alive:
                damage = 0  # 攻击者被法术反杀
        # 爆裂X（F2，裁定口径：受到伤害【前】反噬）：目标持[爆裂]时攻击者先失去等量生命，
        # 攻击者因此命零则本次伤害不落地（与 sim/balance_sim hit_monster 同口径：按结算总量反射一次）
        if target.has_status("爆裂") and attacker is not target and damage > 0:
            rd = self._raw_hp_loss(attacker, damage)
            result["baolie_reflect"] = rd
            if not attacker.is_alive:
                damage = 0
        # 裂变：受到伤害分X次结算（每次=原伤害÷X向下取整）
        if target.has_status("裂变") and damage > 0:
            xv = target.get_status_value("裂变") or 1
            if xv > 1:
                per = damage // xv
                ta = ts = 0; died = False
                for _ in range(xv):
                    dr = self._apply_hostile_damage(target, per, "普通" if not ignore_shield else "无视格挡")
                    ta += dr["actual_damage"]; ts += dr["shield_absorbed"]; died = died or dr["died"]
                damage_result = {"actual_damage": ta, "shield_absorbed": ts, "hp_after": target.current_hp, "died": died, "split": xv}
            else:
                damage_result = self._apply_hostile_damage(target, damage, "普通" if not ignore_shield else "无视格挡")
        else:
            damage_result = self._apply_hostile_damage(target, damage, "普通" if not ignore_shield else "无视格挡")
        # 龙族血脉：持有者对怪物造成伤害后，直接使其命零（死斗两边各一份）
        if (self.state.side_has(attacker, "龙族血脉")
                and target.entity_type == "怪物" and damage_result["actual_damage"] > 0 and target.is_alive):
            target.current_hp = 0
            target.is_alive = False
            damage_result = dict(damage_result)
            damage_result["died"] = True
            damage_result["hp_after"] = 0
            damage_result["dragon_bloodline_kill"] = True
        result["damage_dealt"] = damage_result["actual_damage"]
        result["shield_absorbed"] = damage_result["shield_absorbed"]
        result["hp_lost"] = damage_result["actual_damage"]
        result["target_died"] = damage_result["died"]
        result["target_hp_after"] = damage_result["hp_after"]
        # 洗劫X（F2）：造成伤害时夺取[目标]等量[碎片]（状态挂在攻击者身上，持续X）
        if damage_result.get("actual_damage", 0) > 0 and attacker.has_status("洗劫"):
            stolen = self._xijie_steal(attacker, target, damage_result["actual_damage"])
            result["xijie_stolen"] = stolen
        if damage_result["actual_damage"] > 0:
            attacker.damage_dealt_this_round += damage_result["actual_damage"]
        if "split" in damage_result:
            result["split"] = damage_result["split"]
        # 法术：失去生命后（玩家实损>0时触发）
        if (self.state.player is not None and target is self.state.player
                and damage_result["actual_damage"] > 0 and target.is_alive):
            slogs2 = self.trigger_spells("失去生命后", {"attacker": attacker, "target": target})
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
    
    def round_start(self) -> dict:
        """
        回始结算
        1. 拥有者获得等同当前法限的法力（加法，从不赋值到法限）
        2. 结算回始类效果
        3. 返回需要决策的信息
        """
        effects = []
        
        # 活血追踪归零 + 出手预算归零（回始重置本回合已用出手次数）+ 血誓戒每回合限一次归零 + 血族血脉判定归零
        for e in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            e.hp_lost_this_round = 0
            e.actions_used_this_round = 0
            e.blood_oath_used_this_round = False
            e.mana_inflicted_this_round = 0
            e.damage_dealt_this_round = 0
        # 回始：每个轮回者获得等同当前法限的法力。战始已清零；折速/鲜血契约在战始 +=；守夜灯在本段之后 +=。
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
        relic_logs = self.process_relics("round_start")
        effects.extend({"type": "relic", "log": l} for l in relic_logs)
        
        # 结算回始效果
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            # 自愈：回始获得血限10X%的回复（坏死禁疗）
            if entity.has_status("自愈") and not entity.has_status("坏死"):
                x = entity.get_status_value("自愈")
                heal_pct = 10 * x
                heal_amount = math.ceil(entity.blood_limit * heal_pct / 100)
                heal_result = entity.heal(heal_amount)
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
                    rd = entity.take_damage(dmg_n)
                    effects.append({"type": "shuaibai_tick", "entity": entity.name,
                                    "damage": rd["actual_damage"], "died": rd["died"]})
            pending = getattr(entity, "_dongcha_pending", 0)
            if pending and entity.entity_type == "轮回者" and entity.is_alive:
                entity.current_mana += pending
                effects.append({"type": "dongcha_mana", "entity": entity.name, "gained": pending})
                entity._dongcha_pending = 0
            
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
                    entity.blood_limit = max(1, entity.blood_limit - 2 * x)
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
            d = math.ceil(tgt.current_hp * 30 / 100)  # 引擎口径：当前生命30%（B3 待裁定项，保持现状）
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
    
    def round_end(self) -> dict:
        """
        回终结算
        1. 回终类效果结算
        2. 格挡清空
        3. 持续X剩余回合-1
        """
        effects = []
        
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            # 畸变结算
            if entity.has_status("畸变"):
                x = entity.get_status_value("畸变")
                blood_loss = entity.attack_count * entity.attack_power
                result = entity.take_damage(blood_loss, "代价")
                effects.append({
                    "type": "deform_damage",
                    "entity": entity.name,
                    "blood_loss": blood_loss,
                    "hp_after": result["hp_after"],
                    "died": result["died"]
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
                    heal_detail = entity.heal(entity.damage_dealt_this_round)
                    effects.append({"type": "blood_lineage_heal", "entity": entity.name,
                                     "amount": heal_detail["actual_heal"]})
                else:
                    bleed_detail = self._pay_bleed_cost(entity, 20)
                    effects.append({"type": "blood_lineage_bleed", "entity": entity.name,
                                     "amount": bleed_detail["actual_damage"]})

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
                effects.append({
                    "type": "status_expired",
                    "entity": entity.name,
                    "expired_effects": expired
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

        # 活血：有活血状态的实体，回终按本回合累计失血÷2回复
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            if entity.has_status("活血") and entity.hp_lost_this_round >= 2:
                heal_n = entity.hp_lost_this_round // 2
                h = entity.heal(heal_n)
                effects.append({"type": "huoxue_heal", "entity": entity.name,
                                "heal": heal_n, "actual": h["actual_heal"]})
            entity.hp_lost_this_round = 0

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
    DEBT_THRESHOLD = 10           # 还债：怪物负债（碎片为负）达到N触发（占位）
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
        # 死者之书强化：每只癌变怪物使局外【休整】额外产生8点恢复量（占位，可调）
        boost = 8
        self.state.death_book_wisdom.append(f"癌变·{monster.name}：休整恢复量+{boost}")
        return {
            "type": "proliferation",  # 保留旧 key 兼容；新 key 见下一行
            "type_alias": "cancer",
            "monster": monster.name,
            "absorbed_heal": absorbed,
            "rest_boost": boost,
            "note": (f"{monster.name}累计承受{absorbed}点恢复被癌变吸收进《死者之书》，"
                     f"局外【休整】恢复量+{boost}"),
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
            if target is None:
                return {"success": False, "error": "伤害模式需指定目标"}
            dmg = target.take_damage(self.SCULPTURE_DAMAGE)
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
    
    def apply_daowen_effect(self, name: str, calc: dict, caster: Entity, target: Entity, dragon_heart_use: int = 0) -> dict:
        """应用道纹效果（统一效果键处理；供api与法术共用）。dragon_heart_use仅对caster自身的数值型代价生效。"""
        result = {"daowen": name, "effects": []}
        x = calc.get("x", 0)
        if name in ("自食", "固执"):
            target = caster

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
            dmg = self._apply_hostile_damage(target, 0 if (mengbi_blocked or baolie_suppress) else base)
            result["effects"].append({"type": "damage", "target": target.name, **dmg})
            if dmg.get("actual_damage", 0) > 0:
                caster.damage_dealt_this_round += dmg["actual_damage"]
                self._xijie_steal(caster, target, dmg["actual_damage"])
        if "total_damage" in calc and "target_damage" not in calc:  # 血债等多段
            add = nilin_bonus
            nilin_bonus = 0
            chunk = self._jieli_boost(caster, calc["total_damage"] + add)
            if caster.has_status("坠落") and chunk > 0:
                chunk = math.ceil(chunk / 2)
            dmg = self._apply_hostile_damage(target, 0 if (mengbi_blocked or baolie_suppress) else chunk)
            if add:
                dmg["nilin_bonus"] = add
            result["effects"].append({"type": "damage", "target": target.name, **dmg})
            if dmg.get("actual_damage", 0) > 0:
                caster.damage_dealt_this_round += dmg["actual_damage"]
                self._xijie_steal(caster, target, dmg["actual_damage"])
        if "aoe_damage" in calc:
            a = 0 if (mengbi_blocked or baolie_suppress) else self._jieli_boost(caster, calc["aoe_damage"])
            if caster.has_status("坠落") and a > 0:
                a = math.ceil(a / 2)
            # 逆鳞加成仅作用于首个目标的首段伤害
            if nilin_bonus:
                a += nilin_bonus
                result["nilin_bonus"] = nilin_bonus
                nilin_bonus = 0
            if caster in self.state.get_all_player_side():
                aoe_targets = self.state.get_all_enemy_side()
            else:
                aoe_targets = self.state.get_all_player_side()
            skip = getattr(self, "_skip_aoe_names", set()) or set()
            aoe_targets = [e for e in aoe_targets if e.name not in skip]
            self._skip_aoe_names = set()
            for enemy in aoe_targets:
                # 对首个敌人附加剩余加成（若前未消耗）
                dmg_a = a
                if nilin_bonus and enemy is aoe_targets[0]:
                    dmg_a += nilin_bonus
                    nilin_bonus = 0
                dmg = enemy.take_damage(dmg_a)
                result["effects"].append({"type": "aoe_damage", "target": enemy.name, **dmg})
                if dmg.get("actual_damage", 0) > 0:
                    caster.damage_dealt_this_round += dmg["actual_damage"]
                    self._xijie_steal(caster, enemy, dmg["actual_damage"])
        if "hp_percent_loss" in calc and name != "赌命":  # 赌命已改为[回始]随机结算（F2），此处仅保留其他百分比道纹
            d = math.ceil(target.current_hp * calc["hp_percent_loss"] / 100) + (nilin_bonus if nilin_bonus else 0)
            if nilin_bonus:
                result["nilin_bonus"] = nilin_bonus
                nilin_bonus = 0
            dmg = self._apply_hostile_damage(target, 0 if baolie_suppress else d)
            result["effects"].append({"type": "pct_damage", "target": target.name, **dmg})
            if dmg.get("actual_damage", 0) > 0:
                caster.damage_dealt_this_round += dmg["actual_damage"]
                self._xijie_steal(caster, target, dmg["actual_damage"])

        # ---- 回复类 ----
        if "target_heal" in calc and not huaisi_block:
            result["effects"].append({"type": "heal", "target": target.name, **target.heal(calc["target_heal"])})
        # 自愈的 heal_percent 只在[回始]结算，发动当下不奶。
        if "heal_percent" in calc and name != "自愈" and not (target.has_status("坏死")):
            h = math.ceil(target.blood_limit * calc["heal_percent"] / 100)
            result["effects"].append({"type": "heal_pct", "target": target.name, **target.heal(h)})
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
            target.blood_limit -= calc["blood_limit_reduction"]
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
                target.blood_limit += calc["blood_limit_increase"]
                result["effects"].append({"type": "blood_limit_increase", "target": target.name, "increase": calc["blood_limit_increase"]})
        if "blood_limit_penalty" in calc and target.shards < (calc.get("shard_drain", 0) or 0):
            # 逼债：碎片不足则失血限
            target.blood_limit -= calc["blood_limit_penalty"]; target.current_hp = min(target.current_hp, target.blood_limit)
            result["effects"].append({"type": "bizhai_blood", "target": target.name, "lost": calc["blood_limit_penalty"]})

        # ---- 攻击面板修改 ----
        _panel_keys = ("attack_boost", "attack_reduction", "attack_fixed", "attack_count_fixed")
        panel_locked = target.has_status("定型") and any(k in calc for k in _panel_keys)
        if panel_locked:
            result["effects"].append({"type": "dingxing_block", "target": target.name})
        if (not panel_locked) and "attack_boost" in calc:
            target.attack_power += calc["attack_boost"]
            result["effects"].append({"type": "attack_boost", "target": target.name, "attack_power": target.attack_power})
        if (not panel_locked) and "attack_reduction" in calc:
            target.attack_power = max(0, target.attack_power - calc["attack_reduction"])
            result["effects"].append({"type": "attack_reduction", "target": target.name, "attack_power": target.attack_power})
        if (not panel_locked) and "attack_fixed" in calc:
            target.attack_power = calc["attack_fixed"]
            result["effects"].append({"type": "attack_fixed", "target": target.name, "attack_power": target.attack_power})
        if (not panel_locked) and "attack_count_fixed" in calc:
            target.attack_count = calc["attack_count_fixed"]
            result["effects"].append({"type": "attack_count_fixed", "target": target.name, "attack_count": target.attack_count})
        if name == "变形":  # 自身攻击力与攻击次数互换
            if caster.has_status("定型"):
                result["effects"].append({"type": "dingxing_block", "target": caster.name})
            else:
                caster.attack_power, caster.attack_count = caster.attack_count, caster.attack_power
                result["effects"].append({"type": "swap", "target": caster.name, "attack_power": caster.attack_power, "attack_count": caster.attack_count})

        # ---- 速度修改 ----
        if "speed_boost" in calc:
            gained = self._gain_speed(target, calc["speed_boost"])
            result["effects"].append({"type": "speed_boost", "target": target.name, "speed": target.current_speed, "gained": gained})
        if "speed_halved" in calc:
            target.current_speed = target.current_speed // 2
            result["effects"].append({"type": "speed_halved", "target": target.name, "speed": target.current_speed})
        if "speed_penalty" in calc:
            target.current_speed = max(0, target.current_speed - calc["speed_penalty"])
            result["effects"].append({"type": "speed_penalty", "target": target.name, "speed": target.current_speed})

        # ---- 碎片系（罪孽）----
        if "shard_steal" in calc:  # 赎金：夺碎片（原口径保留：全额扣减可负债[喂还债路径]，仅正值部分可夺得；玩家目标假碎片优先）
            avail = self._shards_of(target)
            gained = max(0, min(avail, calc["shard_steal"]))
            self._lose_shards_of(target, calc["shard_steal"])
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

        # ---- 代价（卖身契：玩家代价转由cost_proxy承担）----
        cost_target = caster
        if (caster is self.state.player and self.cost_proxy is not None and self.cost_proxy.is_alive):
            cost_target = self.cost_proxy
        # 共心环(终音法器)：本场选定类型后，[朋友]/[员工]也可使用该共享龙心抵消同类型代价；
        # 未持有共心环、或类型与选定的不同时，仍只有玩家自身可以使用自己的龙心。
        dh_use = dragon_heart_use
        if cost_target is not self.state.player:
            heart_type = "衰老" if "cost_blood_limit" in calc else ("疲惫" if "cost_speed" in calc else "流血")
            shared_ok = ("共心环" in self.state.artifacts_owned
                         and self.state.shared_dragon_heart_type == heart_type)
            if not shared_ok:
                dh_use = 0
        if "cost_hp" in calc:
            result["effects"].append({"type": "bleed_cost", "source": cost_target.name,
                                       **self._pay_bleed_cost(cost_target, calc["cost_hp"], dh_use)})
            if cost_target.current_hp <= 0: self.cost_proxy = None
        if "cost_blood_limit" in calc:
            # 不朽之躯：免疫衰老（死斗两边各一份）
            if self.state.side_has(cost_target, "不朽之躯"):
                result["effects"].append({"type": "aging_cost", "source": cost_target.name,
                                           "new_blood_limit": cost_target.blood_limit,
                                           "dragon_heart_offset": 0, "immune": True})
            else:
                actual, offset = self._offset_with_dragon_heart(cost_target, "衰老", calc["cost_blood_limit"], dh_use)
                cost_target.blood_limit -= actual; cost_target.current_hp = min(cost_target.current_hp, cost_target.blood_limit)
                self._bank_lianxin(cost_target, "衰老", actual)
                result["effects"].append({"type": "aging_cost", "source": cost_target.name,
                                           "new_blood_limit": cost_target.blood_limit, "dragon_heart_offset": offset})
        if "cost_speed" in calc:
            actual, offset = self._offset_with_dragon_heart(cost_target, "疲惫", calc["cost_speed"], dh_use)
            cost_target.current_speed -= actual
            self._bank_lianxin(cost_target, "疲惫", actual)
            result["effects"].append({"type": "fatigue_cost", "source": cost_target.name,
                                       "new_speed": cost_target.current_speed, "dragon_heart_offset": offset})
        if "mana_gain" in calc:
            caster.current_mana += calc["mana_gain"]
            result["effects"].append({"type": "mana_gain", "source": caster.name, "mana_gained": calc["mana_gain"]})

        # ---- 特殊 ----
        if "self_attack_count" in calc:  # 自残：目标自打X次
            for _ in range(calc["self_attack_count"]):
                result["effects"].append({"type": "self_attack", "target": target.name, **self._apply_hostile_damage(target, target.attack_power)})
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
                    name="缓慢", remaining_rounds=1, value=x, source=caster.name))
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
        elif "duration" in calc and calc.get("duration") is not None:
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
        # 赌命X：消耗X假碎片（付费在 _action_use_daowen 预检；怪物侧在 _apply_exclusive_for_monster）。
        # 状态经 duration 挂在施法者上，[回始]在 round_start 按存活角色随机结算。
        elif name == "赌命":
            result["effects"].append({"type": "duming_register", "caster": caster.name, "x": x})
        # 消灾X：付费在 _action_use_daowen 预检（怪物侧在 _apply_exclusive_for_monster）；此处登记重投次数。
        elif name == "消灾":
            self.dice.set_rerolls(self.dice.rerolls_pending + x)
            result["effects"].append({"type": "xiaozai_rerolls", "added": x, "total": self.dice.rerolls_pending})
        # 抵扣X：封印[目标]拥有的一件遗物，持续X（目标无遗物则无效果）
        elif name == "抵扣":
            sealed = self._seal_one_relic(target, x)
            result["effects"].append({"type": "dikou", "target": target.name,
                                      "sealed": sealed or None, "rounds": x})

        return result



    # 法术流程注册表：法术名 → {触发时点, [(道纹, 目标角色, x模式)]}
    # 目标角色: attacker(伤害来源)/self/target；x模式: kill(够杀)/heal(够奶)/shield(够挡)/auto(默认3)
    SPELL_FLOWS = {
        "先发制人": {"trigger": "受到伤害前", "steps": [("杀伐", "attacker", "kill")]},
        "临界泄压": {"trigger": "受到伤害前", "steps": [("锐利", "attacker", "auto")]},
        "后发制人": {"trigger": "受到伤害前", "steps": [("庇护", "self", "shield")]},
        "生生不息": {"trigger": "失去生命后", "steps": [("再生", "self", "heal")]},
        "以牙还牙": {"trigger": "失去生命后", "steps": [("再生", "self", "heal"), ("杀伐", "attacker", "kill")]},
        "借力打力": {"trigger": "受到伤害前", "steps": [("庇护", "self", "shield"), ("杀伐", "attacker", "kill")]},
        "不死不休": {"trigger": "失去生命后", "steps": [("血债", "attacker", "auto")]},
        "千刀万剐": {"trigger": "失去生命后", "steps": [("再生", "self", "heal"), ("血债", "attacker", "auto")]},
        "咎由自取": {"trigger": "目标发动道纹前", "steps": [("坠落", "target", "auto"), ("杀伐", "target", "kill"), ("血债", "target", "auto")]},
    }

    def _spell_x(self, xmode, player, tgt, ctx):
        mana = player.current_mana
        if xmode == "kill":
            return min(mana, max(1, math.ceil(tgt.current_hp / 2))) if (tgt and tgt.is_alive) else 0
        if xmode == "heal":
            return min(mana, max(1, math.ceil((player.blood_limit - player.current_hp) / 3)))
        if xmode == "shield":
            return min(mana, max(1, math.ceil(ctx.get("incoming", 0) / 4)))
        return 3

    def trigger_spells(self, trigger: str, ctx: dict = None) -> list:
        """触发玩家持有的反应型法术（受伤害前/失血后/目标发动道纹前）"""
        ctx = ctx or {}
        player = self.state.player
        logs = []
        if not player or not player.is_alive:
            return logs
        for spell in list(player.spells):
            flow = self.SPELL_FLOWS.get(spell.name)
            if not flow or flow["trigger"] != trigger:
                continue
            # 所需道纹须持有且可用
            if not all(d in player.dao_wen and player.dao_wen[d].can_use() for d in spell.required_daowen):
                continue
            step_logs = []
            for dw, role, xmode in flow["steps"]:
                tgt = {"attacker": ctx.get("attacker"), "self": player,
                       "target": ctx.get("target") or ctx.get("attacker")}.get(role, player)
                if role != "self" and (tgt is None or not tgt.is_alive):
                    continue
                if tgt is None:
                    tgt = player
                x = self._spell_x(xmode, player, tgt, ctx)
                if x < 1:
                    continue
                try:
                    calc = DaoWenEngine.resolve(dw, x, target=tgt, caster=player)
                except Exception:
                    continue
                # 消耗型道纹需扣法力（代价型由apply处理）
                if calc.get("cost_type") == "消耗":
                    cost = calc.get("cost", 0)
                    if cost > 0 and not player.spend_mana(cost):
                        continue
                self.apply_daowen_effect(dw, calc, player, tgt)
                step_logs.append(f"{spell.name}:{dw}{x}→{tgt.name}")
                if role != "self" and tgt is not player and not tgt.is_alive:
                    break  # 目标已倒，后续步骤跳过
            if step_logs:
                logs.extend(step_logs)
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

    def process_relics(self, trigger: str, ctx: dict = None) -> list:
        """遗物效果触发框架。trigger: battle_start/round_start/on_dodge/on_speed_zero"""
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
            if "回锋刀" in relics:
                d = 3 * max(0, player.speed_limit - player.current_speed)
                if d > 0:
                    enemies = self.state.get_all_enemy_side()
                    if enemies:
                        enemies[0].take_damage(d); logs.append(f"回锋刀：对{enemies[0].name}造{d}伤")
            if "守夜灯" in relics:  # 敌回始+法限50%法力
                g = player.mana_limit // 2
                if g > 0:
                    player.current_mana += g; logs.append(f"守夜灯：+{g}法力")
        if trigger == "battle_start":
            if "折速法印" in relics:
                x = min(5, max(1, player.speed_limit // 2))
                player.current_speed = max(0, player.current_speed - x); player.current_mana += 6 * x
                logs.append(f"折速法印：疲惫{x}，+{6*x}法力")
            if "鲜血契约" in relics:
                x = min(player.blood_limit // 5, 12)
                if x > 0:
                    self._pay_bleed_cost(player, x); player.current_mana += x
                    logs.append(f"鲜血契约：流血{x}，+{x}法力")
            # 三相残韵盘：消耗一种残韵，战终获另两种
            if "三相残韵盘" in relics:
                held = [t for t, c in self.state.resonance.items() if c > 0]
                if held:
                    consume = max(held, key=lambda t: self.state.resonance[t])
                    self.state.resonance[consume] -= 1; self._sanxiang_consumed = consume
                    logs.append(f"三相残韵盘：消耗{consume}残韵")
            # 卖身契：指定第一名朋友/员工为代价替身
            if "卖身契" in relics:
                proxies = [e for e in (self.state.friends + self.state.employees) if e.is_alive]
                if proxies:
                    self.cost_proxy = proxies[0]
                    logs.append(f"卖身契：本场代价由{self.cost_proxy.name}承担")
        if trigger == "battle_end" and "三相残韵盘" in relics and self._sanxiang_consumed:
            others = [t for t in ("转换", "反转", "曲解") if t != self._sanxiang_consumed]
            for t in others:
                self.state.resonance[t] = self.state.resonance.get(t, 0) + 1
            logs.append(f"三相残韵盘：战终获得{'、'.join(others)}残韵各1")
        # 钱袋：改在 api.py 的 _action_battle_end 里随标准击杀奖励一并结算(用battle_start_blood_limit快照)，
        # 不再用"on_monster_death"这个从未被调用过的触发点(此前是死代码)。
        return logs

    # ========== 怪物回合（引擎自主驱动） ==========
    # 成长/控场道纹激活优先级
    MONSTER_ACTIVATE_PRIORITY = ["活力", "强化", "狂暴", "必中", "蒙蔽", "坏死", "减速", "僵化", "自愈", "庇护", "飞行"]

    # 怪物已激活的道纹 / 已进化的怪物（均按战斗重置）
    _monster_activated: dict = {}
    _monster_evolved: set = set()  # 进化（原初X）：本场已进化的怪物 id 集合

    def reset_monster_activation(self):
        """战始重置怪物激活状态与战斗遗物状态"""
        self._monster_activated = {}
        self._monster_evolved = set()  # 进化（原初X）：每场战斗限一次
        self.cost_proxy = None
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

    def _monster_sustain_billing(self, m: Entity, activated: set) -> Optional[str]:
        """
        持续型原始道纹的回合计费（已裁定：改计费粒度）：
        已激活的持续型原始怪物道纹，效果持续期间每个[回始]重新支付异变5X；
        达阈值触发【崩解】直接命零，回合计费中断。返回崩解时正在计费的道纹名或None。
        调用时点：怪物回合内的道纹出手激活之前（即计费按上个回合已激活的集合结算，不重复收本场激活当回合）。
        """
        for g in list(activated):
            if g in self.SUSTAIN_MONSTER_DAOWEN and g in m.dao_wen:
                pay = m.add_mutation(self.YUANCHU_COST_RATE * m.dao_wen[g].x_value)
                if pay["collapsed"]:
                    return g
        return None

    def _monster_activate(self, m: Entity, activated: set):
        """
        怪物道纹出手：激活一个未激活的成长/控场道纹，返回道纹名或None。
        原始怪物道纹以【异变】为代价（道纹归属规则#1）：激活时支付异变5X（X=面板数值）；
        异变达阈值触发【崩解】直接命零，返回 "崩解:道纹名"，本次激活效果中断。
        干扰仪：若怪物本回合被【干扰】，则本回合无法发动自身道纹。
        龙心谷专属（F2）第二梯队：通用层之后，尝试激活本副本的专属道纹。
        """
        if m.has_status("干扰"):
            return None
        # 残韵改写优先兑现：只改本次结算，不付原道纹代价，不记入 activated
        pending = self._resonance_rewrites.get(id(m)) or {}
        for source in list(pending.keys()):
            if source in m.dao_wen:
                dest = self.consume_resonance_rewrite(m, source)
                if dest:
                    return self._resolve_rewritten_activation(m, source, dest)
        for g in self.MONSTER_ACTIVATE_PRIORITY:
            if g in m.dao_wen and g not in activated:
                if g in self.ORIGINAL_MONSTER_DAOWEN:
                    pay = m.add_mutation(self.YUANCHU_COST_RATE * m.dao_wen[g].x_value)
                    if pay["collapsed"]:
                        return "崩解:" + g
                activated.add(g)
                if g == "强化":
                    m.attack_power += m.dao_wen[g].x_value
                if g == "必中":
                    self.grant_bizhong(m, m.dao_wen[g].x_value)
                if g == "自愈":
                    xv = m.dao_wen[g].x_value
                    m.add_status(StatusEffect(name="自愈", remaining_rounds=-1, value=xv, source=m.name))
                return g
        # 第二梯队：副本专属（龙心谷 8 件等），按固定优先级
        region = self.state.current_region
        exclusive_pool = self.REGION_EXCLUSIVE_DAOWEN.get(region, set())
        # 与 sim/balance_sim EXCLUSIVE_PRIORITY 同序，确保确定性
        exclusive_order = ["变形","逆鳞","假钞","赌命","加害","洗劫","逼债","赎金","清算","龙鳞","爆裂","裂变","伤痕","退化","畸变","活血","嫁祸","背负","超频","定型","抵扣","消灾"]
        for g in exclusive_order:
            if g in exclusive_pool and g in m.dao_wen and g not in activated:
                # 赌命需假碎片余额等代价准入（简化：若有假碎片则过，无则跳过）
                if g == "赌命" and getattr(m, "fake_shards", 0) < m.dao_wen[g].x_value:
                    continue
                if g in self.ORIGINAL_MONSTER_DAOWEN:
                    pay = m.add_mutation(self.YUANCHU_COST_RATE * m.dao_wen[g].x_value)
                    if pay["collapsed"]:
                        return "崩解:" + g
                activated.add(g)
                # 立即结算专属效果（F2 的 4 件 + 其他专属的通用状态）
                try:
                    calc = DaoWenEngine.resolve(g, m.dao_wen[g].x_value, target=m, caster=m)
                    self._apply_exclusive_for_monster(g, calc, m)
                except Exception:
                    # 若 resolve 失败（如缺少目标），仍视为已激活但无效果
                    m.add_status(StatusEffect(name=g, remaining_rounds=calc.get("duration", -1) if 'calc' in locals() else -1, value=m.dao_wen[g].x_value, source=m.name))
                return g
        # 第三梯队：【原初X】借来的道纹（战终前视为持有，发动时照常支付自身代价；怪物不付法力）
        return self._monster_activate_borrowed(m, activated)

    # 原初借用里对自身生效的道纹；其余默认打向轮回者
    BORROWED_SELF_TARGET = {
        "庇护", "再生", "固执", "慈悲", "增殖", "透支", "贯穿", "超频",
        "变形", "自食", "滑翔", "飞行", "自愈", "狂暴", "必中",
    }

    def _resolve_rewritten_activation(self, m: Entity, source: str, dest: str) -> str:
        """残韵改写本次发动：按新道纹原版公式结算，不改持有，不付原道纹代价。"""
        x = m.dao_wen[source].x_value if source in m.dao_wen else 1
        target = m if dest in self.BORROWED_SELF_TARGET or dest == "自残" else self.state.player
        if target is None:
            target = m
        try:
            calc = DaoWenEngine.resolve(dest, x, target=target, caster=m)
            if dest == "冲击":
                player = self.state.player
                if player is not None and player.is_alive:
                    dmg = self._apply_hostile_damage(
                        player, calc.get("aoe_damage", x))
                    if dmg.get("actual_damage", 0) > 0:
                        m.damage_dealt_this_round += dmg["actual_damage"]
            else:
                self.apply_daowen_effect(dest, calc, m, target)
        except Exception:
            return f"改写中断:{source}→{dest}"
        return f"改写:{source}→{dest}"

    def _monster_activate_borrowed(self, m: Entity, activated: set) -> Optional[str]:
        """发动尚未激活的原初借用道纹，对轮回者打出真实效果。"""
        player = self.state.player
        if player is None or not player.is_alive:
            return None
        for name, inst in m.dao_wen.items():
            if name in activated:
                continue
            tags = getattr(inst.dao_wen, "tags", None) or []
            if "原初借用" not in tags:
                continue
            rewritten = self.consume_resonance_rewrite(m, name)
            if rewritten:
                return self._resolve_rewritten_activation(m, name, rewritten)
            activated.add(name)
            target = m if name in self.BORROWED_SELF_TARGET else player
            try:
                calc = DaoWenEngine.resolve(name, inst.x_value, target=target, caster=m)
                if name == "冲击":
                    # apply_daowen_effect 的 AOE 扫的是 state.enemies；怪物发动应对准轮回者一侧
                    dmg = self._apply_hostile_damage(player, calc.get("aoe_damage", inst.x_value))
                    if dmg.get("actual_damage", 0) > 0:
                        m.damage_dealt_this_round += dmg["actual_damage"]
                else:
                    self.apply_daowen_effect(name, calc, m, target)
            except Exception:
                return f"借用中断:{name}"
            return f"借用:{name}"
        return None

    def _apply_exclusive_for_monster(self, name: str, calc: dict, caster: Entity):
        """怪物侧专属道纹的即时结算（F2 + 全量 24 种），与玩家侧 apply_daowen_effect 同语义但简化为状态/计数"""
        x = calc.get("x", 0)
        # 通用持续状态已在上层通过 calc duration 处理，此处补计数类
        if name == "逆鳞":
            if not hasattr(caster, "_nilin"):
                caster._nilin = 0
            caster.add_status(StatusEffect(name="逆鳞", value=x, remaining_rounds=x, source=caster.name))
            # 逆鳞的流血代价已在 calculate 中声明，但怪物侧流血直接扣生命
            if calc.get("cost_hp"):
                caster.take_damage(calc["cost_hp"], "代价")
                caster._nilin = getattr(caster, "_nilin", 0) + calc["cost_hp"]
        elif name == "嫁祸":
            # 嫁祸：自身下X次受伤由目标承担，目标选玩家（无道德底线）或同场最弱同伴
            from .models import StatusEffect as _SE
            # 策略：优先玩家（与 sim 的 JIAHUO_POLICY=player_first 一致）
            target = self.state.player
            if target and target.is_alive:
                caster._jiahuo_left = x
                caster._jiahuo_target = target
                caster.add_status(_SE(name="嫁祸", value=x, remaining_rounds=x, source=caster.name))
        elif name == "背负":
            # 背负：目标下X次受伤由自身承担，目标选同场最濒危同伴
            candidates = [e for e in self.state.get_all_enemy_side() if e is not caster and e.is_alive]
            if candidates:
                tgt = min(candidates, key=lambda e: e.current_hp)
                caster._beifu_left = x
                caster._beifu_target = tgt
                tgt.add_status(StatusEffect(name="被背负", value=x, remaining_rounds=-1, source=caster.name))
        elif name == "伤痕":
            # 伤痕由玩家侧已在 apply_daowen_effect 中处理，此处怪物侧若对玩家使用则直接加状态
            # 怪物对玩家使用时，目标应为玩家
            player = self.state.player
            if player:
                player.add_status(StatusEffect(name="伤痕", value=x, remaining_rounds=-1, source=caster.name))
        # ---- F2 全量：罪孽/扭曲专属（怪物侧） ----
        # 假钞X：获得10X假碎片
        elif name == "假钞":
            caster.fake_shards = getattr(caster, "fake_shards", 0) + 10 * x
        # 赌命X：代价X假碎片（激活准入已校验余额）；状态挂怪物侧，[回始]随机结算
        elif name == "赌命":
            if getattr(caster, "fake_shards", 0) >= x:
                caster.fake_shards -= x
                caster.add_status(StatusEffect(name="赌命", value=x, remaining_rounds=x, source=caster.name))
        # 消灾X：消耗50X假碎片/5X碎片（与玩家侧付费口径一致），登记全局重投次数
        elif name == "消灾":
            fake_cost, real_cost = 50 * x, 5 * x
            paid = False
            if getattr(caster, "fake_shards", 0) >= fake_cost:
                caster.fake_shards -= fake_cost; paid = True
            elif caster.shards >= real_cost:
                caster.lose_shards(real_cost); paid = True
            if paid:
                self.dice.set_rerolls(self.dice.rerolls_pending + x)
        # 逼债X：[回始]目标失X碎片否则失2X血限——对玩家挂账
        elif name == "逼债":
            player = self.state.player
            if player and player.is_alive:
                player._bizhai.append({"x": x, "caster": caster})
                player.add_status(StatusEffect(name="逼债", value=x, remaining_rounds=-1, source=caster.name))
        # 清算X：[回始]目标失[你碎片]点格挡——对玩家挂账
        elif name == "清算":
            player = self.state.player
            if player and player.is_alive:
                player._qingsuan.append({"x": x, "caster": caster})
                player.add_status(StatusEffect(name="清算", value=x, remaining_rounds=x, source=caster.name))
        # 抵扣X：封印玩家一件遗物（持续X回合，[回终]递减）
        elif name == "抵扣":
            if self.state.player is not None and self.state.relics:
                self._seal_one_relic(self.state.player, x)
        # 洗劫X：状态挂怪物侧，造成伤害时夺取等量碎片
        elif name == "洗劫":
            caster.add_status(StatusEffect(name="洗劫", value=x, remaining_rounds=x, source=caster.name))
        # 爆裂X：状态挂怪物侧，受到伤害前反噬攻击者
        elif name == "爆裂":
            caster.add_status(StatusEffect(name="爆裂", value=x, remaining_rounds=x, source=caster.name))
        # 退化X：使玩家每次发动道纹时数值-X（持续∞）
        elif name == "退化":
            player = self.state.player
            if player:
                player.add_status(StatusEffect(name="退化", value=x, remaining_rounds=-1, source=caster.name))
        elif name == "加害":
            player = self.state.player
            if player and player.is_alive:
                player.add_status(StatusEffect(name="加害", value=x, remaining_rounds=-1, source=caster.name))
        elif name == "龙鳞":
            caster.add_status(StatusEffect(name="龙鳞", value=x, remaining_rounds=-1, source=caster.name))
        elif name == "定型":
            player = self.state.player
            if player and player.is_alive:
                player.add_status(StatusEffect(name="定型", value=x, remaining_rounds=x, source=caster.name))
        # 其他专属的通用状态已通过 calc duration 在外层添加，此处无需额外

    def _monster_attack_actions(self, m: Entity, activated: set) -> int:
        """怪物攻击出手数 = 1 + 活力X(若激活) + 狂暴1(若激活)；手雷减攻则-1"""
        n = 1
        if "活力" in activated:
            n += m.dao_wen["活力"].x_value
        if "狂暴" in activated:
            n += 1
        # 高爆手雷：本回合攻击次数-1（每枚手雷叠加）
        if m.has_status("手雷减攻"):
            n -= m.get_status_value("手雷减攻")
        return max(1, n)

    def _apply_control_to_player(self, name: str, m: Entity, player: Entity):
        """怪物激活控场道纹后对轮回者施加效果"""
        x = m.dao_wen[name].x_value
        if name == "蒙蔽":
            player.add_status(StatusEffect("蒙蔽", -1, x))
        elif name == "坏死":
            player.add_status(StatusEffect("坏死", -1, 0))
        elif name == "减速":
            player.current_speed = max(0, player.current_speed // 2)
        elif name == "僵化":
            player.attack_power = 1

    def run_monster_phase(self, dodge_policy: str = "auto") -> list:
        """
        运行所有存活怪物的回合：道纹出手(激活成长/控场道纹) + 攻击出手(一轮攻击×攻击出手数)。
        玩家闪避按policy：auto=单次伤害>当前格挡且有速度则闪避。
        第1回合(白板)不激活道纹。返回每只怪的出手结果。
        """
        player = self.state.player
        results = []
        if not player or not player.is_alive:
            return results
        # 敌方持有的爆裂：玩家回合刚结束 = 它们的[敌回终]
        results.extend(self._tick_baolie(self.state.get_all_enemy_side()))
        whiteboard = self.state.current_round <= 1
        for m in self.state.get_all_enemy_side():
            if not self.can_act(m):
                why = "缓慢" if m.has_status("缓慢") else "眩晕/束缚"
                results.append({"monster": m.name, "skipped": why})
                continue
            # 龙息：对方持有则本侧行动前受伤（死斗两边各一份）
            breath = self.apply_opposing_longxi(m)
            if breath:
                results.append({"monster": m.name, "dragon_breath": breath.get("dragon_breath"), **breath})
                if not m.is_alive:
                    continue
            act = self._monster_activated.setdefault(id(m), set())
            # 持续型原始道纹回合计费（道纹出手激活之前）
            if not whiteboard:
                cg = self._monster_sustain_billing(m, act)
                if cg is not None:
                    results.append({"monster": m.name, "collapsed": cg,
                                    "note": f"持续型道纹【{cg}】回合计费后异变达{m.mutation_count}层，触发【崩解】直接命零"})
                    continue
            # 道纹出手（白板第1回合不激活）
            if not whiteboard:
                an = self._monster_activate(m, act)
                if an and an.startswith("崩解:"):
                    results.append({"monster": m.name, "collapsed": an[3:],
                                    "note": f"支付异变后达{m.mutation_count}层，触发【崩解】直接命零，激活效果中断"})
                    continue
                if an:
                    # 道纹出手计入本回合出手数（供【凡庸】判定）
                    m.actions_used_this_round += 1
                    if an in ("蒙蔽", "坏死", "减速", "僵化"):
                        self._apply_control_to_player(an, m, player)
                    entry = {"monster": m.name, "daowen_activated": an}
                    if isinstance(an, str) and an.startswith("改写:"):
                        entry["resonance_rewrite"] = True
                    elif isinstance(an, str) and an.startswith("借用:"):
                        entry["borrowed"] = True
                    results.append(entry)
            # 攻击出手
            n = self._monster_attack_actions(m, act)
            for _ in range(n):
                if not player.is_alive:
                    break
                # 每个攻击出手计入本回合出手数（供【凡庸】判定）
                m.actions_used_this_round += 1
                # 物品索引：高爆手雷「本回合攻击次数-1」。不改 attack_count 面板，避免误触雕塑。
                hits = max(0, m.attack_count - m.get_status_value("手雷减攻"))
                for _h in range(hits):
                    if not player.is_alive or not m.is_alive:
                        break
                    # 必中只覆盖剩余次数；余数在 resolve_attack 里消耗
                    must = self.bizhong_remaining(m) > 0
                    atk_target = m if m.has_status("无神") else player
                    dodge = (dodge_policy == "auto" and not must
                             and atk_target.current_speed > 0 and m.attack_power > atk_target.shield
                             and atk_target is not m)
                    _r = self.resolve_attack(m, atk_target, is_must_hit=False, dodge=dodge)
                    # 标记"一轮攻击内的第几击"：一次攻击出手包含 attack_count 次攻击，
                    # 每次独立判定闪避(README:204)，但它们同属一个出手，不应各占一个出手号。
                    _r["hit_index"] = _h + 1
                    _r["hit_total"] = hits
                    _r["new_action"] = (_h == 0)
                    results.append(_r)
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
        detail = actor.take_damage(dmg)
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
