"""
战斗计算引擎
负责战斗中的数值对撞、回合推进、伤害结算
所有数值计算在此完成，AI禁止自行计算
"""
from __future__ import annotations
import math
from typing import Optional, Any
from .models import Entity, StatusEffect, GameState, DaoWenInstance, DaoWen, Spell
from .daowen import DaoWenEngine, ResonanceEngine
from .dice import DiceEngine
from .enums import InterruptType, DamageType
from .dm_rulings import Interrupt
from .gamedata import REGION_EXCLUSIVE_DAOWEN


class CombatEngine:
    """战斗计算引擎"""
    
    # 副本专属道纹归属表（校验怪物池道纹合法性用）——以 gamedata 为准
    REGION_EXCLUSIVE_DAOWEN = REGION_EXCLUSIVE_DAOWEN
    
    # AOE/非单体判定的道纹（其余作用于敌方单体的道纹均带[目标]，可被闪避）
    UNTARGETED_DAOWEN = {"冲击", "坠落"}
    
    def __init__(self, state: GameState, dice: DiceEngine):
        self.state = state
        self.dice = dice
        self.combat_log: list[dict] = []  # 完整战斗日志
    
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
    
    @staticmethod
    def is_flying(entity: Entity) -> bool:
        """飞行状态判定：实体持有 飞行/滑翔 状态时视为飞行"""
        return entity.is_flying or entity.has_status("飞行") or entity.has_status("滑翔")

    def on_successful_dodge(self, entity: Entity) -> list[str]:
        """
        成功闪避后的通用钩子：
        - 急速：每闪避两次速度+1
        - 加速：获得的速度翻倍
        - 洞察：闪避后下回合法力+10（仅对持有法力者生效）
        """
        notes = []
        entity.dodge_streak += 1
        if entity.has_status("急速") and entity.dodge_streak % 2 == 0:
            gain = 1
            if entity.has_status("加速"):
                gain *= 2
            entity.current_speed += gain
            notes.append(f"急速：{entity.name}速度+{gain}")
        if entity.has_status("洞察"):
            self.state.relic_flags["洞察_下回合法力"] = (
                self.state.relic_flags.get("洞察_下回合法力", 0) + 10)
            notes.append(f"洞察：{entity.name}下回合法力+10")
        return notes

    def apply_outgoing_damage_modifiers(
        self,
        attacker: Entity,
        target: Entity,
        damage: int,
        result: dict,
    ) -> int:
        """攻击方与目标方对伤害的公共修正矩阵（攻击与道纹伤害共用）"""
        # 攻击方僵化：攻击力固定为1（仅对攻击判定生效）
        if result.get("attack_based") and attacker.has_status("僵化"):
            damage = min(damage, 1)
        # 攻击方强化：攻击力+X
        if result.get("attack_based") and attacker.has_status("强化"):
            damage += attacker.get_status_value("强化")
        # 攻击方弱化：攻击力-X（最低为0）
        if result.get("attack_based") and attacker.has_status("弱化"):
            damage = max(0, damage - attacker.get_status_value("弱化"))
        # 攻击方借力：造成伤害+10X%
        if attacker.has_status("借力"):
            pct = attacker.get_status_value("借力")
            damage = math.ceil(damage * (100 + pct) / 100)
        # 攻击方坠落（被击落）：造成伤害减半
        if attacker.has_status("坠落"):
            damage = math.ceil(damage / 2)
        # 攻击方逆鳞：下次造成伤害时+全部逆鳞层数，随后清除
        if attacker.has_status("逆鳞"):
            stacks = attacker.get_status_value("逆鳞")
            for s in list(attacker.status_effects):
                if s.name == "逆鳞":
                    attacker.status_effects.remove(s)
            if stacks > 0:
                damage += stacks
                result["nilin_bonus"] = stacks
        # 目标侧加害：每次受到伤害+X
        if target.has_status("加害"):
            damage += target.get_status_value("加害")
        # 目标侧龙鳞：每次受到伤害-X，最低为0
        if target.has_status("龙鳞"):
            damage = max(0, damage - target.get_status_value("龙鳞"))
        return damage

    def on_damage_taken(self, attacker: Optional[Entity], target: Entity, hp_lost: int, result: dict):
        """
        目标实际失去生命后的钩子：
        - 逆鳞（目标侧）：每失去1点生命获得1层逆鳞
        - 爆裂（目标侧）：攻击者失去等量生命
        - 伤痕（目标侧）：每次失去生命后[血限]-X
        - 寄生（目标侧）：受到伤害的20X%转化为施加者[回复]
        - 洗劫（攻击方）：造成伤害时夺取等量碎片
        - 眩晕（目标侧）：受到伤害后解除
        """
        if hp_lost <= 0:
            return

        # 逆鳞
        if target.has_status("逆鳞"):
            stacks = target.get_status_value("逆鳞")
            for s in target.status_effects:
                if s.name == "逆鳞":
                    s.value = stacks + hp_lost
            result["nilin_stacks"] = stacks + hp_lost

        # 爆裂：攻击者失去等量生命（反射为代价，不可格挡）
        if target.has_status("爆裂") and attacker is not None and attacker.is_alive:
            attacker.take_damage(hp_lost, "代价")
            result["baolie_reflect"] = hp_lost

        # 伤痕：每次失去生命后[血限]-X
        if target.has_status("伤痕"):
            x = target.get_status_value("伤痕")
            target.blood_limit -= x
            if target.current_hp > target.blood_limit:
                target.current_hp = target.blood_limit
            result["shanghen_blood_limit_loss"] = x
            if target.blood_limit <= 0 or target.current_hp <= 0:
                target.is_alive = False
                result["target_died"] = True

        # 寄生：受到伤害的20X%转化为施加者回复
        if target.has_status("寄生"):
            for s in target.status_effects:
                if s.name == "寄生":
                    host = next(
                        (e for e in ([self.state.player] if self.state.player else [])
                         + self.state.friends + self.state.employees
                         + self.state.temp_friends + self.state.enemies
                         if e and e.name == s.source),
                        None,
                    )
                    pct = 20 * s.value
                    if host and pct > 0:
                        heal_amount = math.ceil(hp_lost * pct / 100)
                        host.heal(heal_amount)
                        result.setdefault("jisheng_drain", []).append(
                            {"host": host.name, "heal": heal_amount})

        # 洗劫：造成伤害时夺取目标等量碎片（战斗中优先夺取假碎片）
        if attacker is not None and attacker.has_status("洗劫"):
            if target is self.state.player:
                available = self.state.shards + self.state.fake_shards
                stolen = min(hp_lost, available)
                self.state.lose_shards(stolen, in_battle=True)
            else:
                stolen = min(hp_lost, target.shards)
                target.shards -= stolen
            if attacker is self.state.player:
                self.state.shards += stolen
            else:
                attacker.shards += stolen
            result["xijie_stolen"] = stolen

        # 眩晕：受到伤害后解除
        if target.has_status("眩晕"):
            target.status_effects = [s for s in target.status_effects if s.name != "眩晕"]
            result["xuanyun_broken"] = True

    def _all_entities(self) -> list:
        return ([e for e in [self.state.player] if e]
                + self.state.friends + self.state.employees
                + self.state.temp_friends + self.state.enemies)

    def find_damage_bearer(self, original_target: Entity, result: dict) -> Entity:
        """
        承伤链判定（嫁祸/背负）：
        - 嫁祸X：施术者自身下X次受到的伤害由所选目标承担
        - 背负X：所选目标下X次受到的伤害由施术者承担
        每承担一次伤害消耗1层。
        """
        # 目标自身持有嫁祸：其伤害改由所选目标承担
        for s in list(original_target.status_effects):
            if s.name == "嫁祸" and s.value > 0:
                proxy = next(
                    (e for e in self._all_entities()
                     if e.name == s.meta.get("redirect_to") and e.is_alive),
                    None,
                )
                if proxy is not None:
                    s.value -= 1
                    if s.value <= 0:
                        original_target.status_effects.remove(s)
                    result["jiahuo_redirect"] = proxy.name
                    return proxy
        # 他人为目标背负：由背负者承担
        for e in self._all_entities():
            if e is original_target or not e.is_alive:
                continue
            for s in list(e.status_effects):
                if s.name == "背负" and s.value > 0 and s.meta.get("protected") == original_target.name:
                    s.value -= 1
                    if s.value <= 0:
                        e.status_effects.remove(s)
                    result["beifu_absorb"] = e.name
                    return e
        return original_target

    def resolve_attack(
        self,
        attacker: Entity,
        target: Entity,
        hit_index: int = 0,
        is_must_hit: bool = False,
        dodge: bool = False
    ) -> dict:
        """
        解析一次攻击
        dodge: 目标是否选择闪避（由AI决策）
        """
        result = {
            "attacker": attacker.name,
            "target": target.name,
            "hit_index": hit_index,
            "attack_based": True,
            "dodge_attempted": dodge,
            "dodge_success": False,
            "damage_dealt": 0,
            "shield_absorbed": 0,
            "hp_lost": 0,
            "target_died": False,
        }

        # 无神：选择目标时强制改为自身
        if attacker.has_status("无神"):
            target = attacker
            result["wushen_retarget"] = True

        # 闪避判定
        if dodge:
            if is_must_hit:
                result["dodge_success"] = False
                result["dodge_fail_reason"] = "必中攻击无法闪避"
            elif target.current_speed >= 1:
                target.current_speed -= 1
                result["dodge_success"] = True
                result["speed_after_dodge"] = target.current_speed
                result["dodge_hooks"] = self.on_successful_dodge(target)
                # 闪避成功，本局速度-1（战终复原）
                return result
            else:
                result["dodge_success"] = False
                result["dodge_fail_reason"] = "速度不足"

        # 必中X：消耗层数，下X次攻击附带必中
        if is_must_hit:
            for s in attacker.status_effects:
                if s.name == "必中" and s.value > 0:
                    s.value -= 1
                    if s.value <= 0:
                        attacker.status_effects.remove(s)
                    break

        # 基础伤害
        damage = attacker.attack_power

        # 检查蒙蔽状态（下X次造成的伤害无效）
        if attacker.has_status("蒙蔽"):
            stacks = attacker.get_status_value("蒙蔽")
            if stacks > 0:
                for s in attacker.status_effects:
                    if s.name == "蒙蔽" and s.value > 0:
                        s.value -= 1
                        if s.value <= 0:
                            attacker.status_effects.remove(s)
                        break
                result["damage_dealt"] = 0
                result["blocked_by"] = "蒙蔽"
                return result

        # 伤害修正矩阵
        damage = self.apply_outgoing_damage_modifiers(attacker, target, damage, result)

        # 裂变：受到的伤害改为分X次结算（每次向下取整，总伤=单次×次数）
        if target.has_status("裂变"):
            x = max(1, target.get_status_value("裂变"))
            per = damage // x
            damage = per * x
            result["liebian_split"] = {"times": x, "per_hit": per}

        # 贯穿（无视格挡）
        ignore_shield = attacker.has_status("贯穿")
        
        # 承伤链（嫁祸/背负）：命中后才结算承担
        bearer = self.find_damage_bearer(target, result)
        
        damage_result = bearer.take_damage(damage, "普通" if not ignore_shield else "无视格挡")
        if bearer is not target:
            damage_result["bearer"] = bearer.name
            target = bearer  # 后续钩子按实际承伤者结算

        # 固执（单次失去生命最高为1）
        if target.get_status_value("固执") > 0 and damage_result["actual_damage"] > 1:
            restore = damage_result["actual_damage"] - 1
            target.current_hp += restore
            target.hp_lost_this_round -= restore
            damage_result["actual_damage"] = 1
            damage_result["hp_after"] = target.current_hp
            damage_result["died"] = False
            target.is_alive = True
            damage_result["capped_by_guzhi"] = True

        result["damage_dealt"] = damage_result["actual_damage"]
        result["shield_absorbed"] = damage_result["shield_absorbed"]
        result["hp_lost"] = damage_result["actual_damage"]
        result["target_died"] = damage_result["died"]
        result["target_hp_after"] = damage_result["hp_after"]

        # 受伤后钩子（逆鳞/爆裂/伤痕/寄生/洗劫/眩晕解除）
        if damage_result["actual_damage"] > 0:
            self.on_damage_taken(attacker, target, damage_result["actual_damage"], result)

        # 兴奋：每次出手后速度+1（加速翻倍）
        if attacker.has_status("兴奋"):
            speed_gain = attacker.get_status_value("兴奋")
            if attacker.has_status("加速"):
                speed_gain *= 2
            attacker.current_speed += speed_gain
            result["speed_boost_from_excitement"] = speed_gain

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
    
    @staticmethod
    def monster_act_count(round_num: int) -> int:
        """怪物出手次数 = 当前回合数÷3，向上取整（第1~3回合均为1次）"""
        return max(1, math.ceil(round_num / 3))
    
    def round_start(self) -> dict:
        """
        回始结算
        1. 法力补满至法限
        2. 结算回始类效果
        3. 返回需要决策的信息
        """
        effects = []
        
        # 回合记账重置
        self.state.actions_used = 0
        self.state.relic_flags.pop("血誓戒_本回合已触发", None)
        for e in ([self.state.player] if self.state.player else []) \
                + self.state.friends + self.state.employees + self.state.temp_friends + self.state.enemies:
            e.hp_lost_this_round = 0
        
        # 轮回者法力补满（README 213行"[回始]自动补满等量法力"：补=补足，
        # 不削平超过[法限]的战始增益法力（缄默面具/折速法印/鲜血契约等）——
        # 超额法力最迟于[敌回终]随全部法力清空，不会跨轮滚存）
        if self.state.player and self.state.player.is_alive:
            old_mana = self.state.player.current_mana
            if old_mana < self.state.player.mana_limit:
                self.state.player.current_mana = self.state.player.mana_limit
                effects.append({
                    "type": "mana_refill",
                    "entity": self.state.player.name,
                    "from": old_mana,
                    "to": self.state.player.mana_limit
                })
            else:
                effects.append({
                    "type": "mana_refill_kept_overflow",
                    "entity": self.state.player.name,
                    "at": old_mana,
                    "note": f"当前法力{old_mana}≥[法限]{self.state.player.mana_limit}：补满为补足不削减，超额部分[敌回终]随全部法力清空"
                })
            # 洞察：上轮闪避后本回合法力+10
            insight = self.state.relic_flags.pop("洞察_下回合法力", 0)
            self.state.player.current_mana += insight
            if insight:
                effects.append({"type": "洞察法力", "entity": self.state.player.name, "amount": insight})
        
        # 结算回始效果
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            # 自愈：回始获得血限10X%的回复
            if entity.has_status("自愈"):
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
            
            # 狂暴：回始发动一轮额外攻击（标记）
            if entity.has_status("狂暴"):
                effects.append({
                    "type": "extra_attack_ready",
                    "entity": entity.name,
                    "note": "该实体本回合有一次额外攻击机会"
                })
            
            # 衰败：每回合回始造成当前生命10X%的伤害（持续∞）
            if entity.has_status("衰败"):
                pct = 10 * entity.get_status_value("衰败")
                dmg_amount = math.ceil(entity.current_hp * pct / 100)
                if dmg_amount > 0:
                    res = entity.take_damage(dmg_amount, "普通")
                    effects.append({
                        "type": "衰败伤害",
                        "entity": entity.name,
                        "damage": dmg_amount,
                        "hp_after": res["hp_after"],
                        "died": res["died"],
                    })
            
            # 清算：回始使目标失去（施法者碎片）点格挡（施法者碎片数值已存于状态值）
            if entity.has_status("清算"):
                drain = entity.get_status_value("清算")
                lost = min(entity.shield, drain)
                entity.shield -= lost
                if lost > 0:
                    effects.append({"type": "清算格挡流失", "entity": entity.name, "shield_lost": lost})
        
        # 逼债：回始使轮回者失去X点碎片，否则失去2X点血限（持续∞）
        player = self.state.player
        if player and player.is_alive and player.has_status("逼债"):
            x = player.get_status_value("逼债")
            if self.state.shards >= x:
                self.state.shards -= x
                effects.append({"type": "逼债碎片", "amount": x, "shards_after": self.state.shards})
            else:
                player.blood_limit -= 2 * x
                player.current_hp = min(player.current_hp, player.blood_limit)
                effects.append({"type": "逼债血限", "amount": 2 * x,
                                "blood_limit_after": player.blood_limit})
        
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
            # 畸变结算：回终使目标失去（攻击力×攻击次数）的[血限]
            if entity.has_status("畸变"):
                blood_loss = entity.attack_count * entity.attack_power
                entity.blood_limit -= blood_loss
                if entity.current_hp > entity.blood_limit:
                    entity.current_hp = entity.blood_limit
                if entity.blood_limit <= 0 or entity.current_hp <= 0:
                    entity.is_alive = False
                effects.append({
                    "type": "deform_blood_loss",
                    "entity": entity.name,
                    "blood_loss": blood_loss,
                    "blood_limit_after": entity.blood_limit,
                    "died": not entity.is_alive,
                })
            
            # 活血：每个完整回合内每累计失去2点生命，回终获得[回复1]
            if entity.has_status("活血") and entity.is_alive:
                heal_amount = entity.hp_lost_this_round // 2
                if heal_amount > 0:
                    res = entity.heal(heal_amount)
                    effects.append({
                        "type": "活血回复",
                        "entity": entity.name,
                        "heal": res["actual_heal"],
                    })
            
            # 格挡清空
            if entity.shield > 0:
                effects.append({
                    "type": "shield_clear",
                    "entity": entity.name,
                    "cleared": entity.shield
                })
                entity.clear_shield()
            
        # 法力清空（敌回终）
        # 规则：[法限]用于发动道纹与法术，法力[敌回终]清空
        if self.state.player and self.state.player.is_alive:
            if self.state.player.current_mana > 0:
                effects.append({
                    "type": "mana_clear",
                    "entity": self.state.player.name,
                    "cleared": self.state.player.current_mana
                })
                self.state.player.current_mana = 0
        
        # 持续效果递减（变形到期回滚：攻击力与攻击次数换回原值）
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            expired = entity.tick_status_effects()
            for s in expired:
                if s.name == "变形" and "orig_attack_count" in s.meta:
                    entity.attack_count = s.meta["orig_attack_count"]
                    entity.attack_power = s.meta["orig_attack_power"]
            if expired:
                effects.append({
                    "type": "status_expired",
                    "entity": entity.name,
                    "expired_effects": [s.name for s in expired]
                })
        
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
        
        if len(difficulty_signals) >= 2:
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
    
    # ========== 进化 ==========
    
    def initiate_evolution(self, monster: Entity, difficulty: dict) -> Interrupt:
        """
        怪物进化
        规则非常严格，必须由DM裁定
        """
        return Interrupt(
            interrupt_type=InterruptType.EVOLUTION,
            context={
                "monster": monster.name,
                "monster_type": monster.entity_type,
                "hp_ratio": round(monster.hp_ratio, 2),
                "current_daowen": list(monster.dao_wen.keys()),
                "difficulty_signals": difficulty.get("signals", []),
                "current_round": self.state.current_round,
            },
            description=(
                f"{monster.name}陷入困境，选择进化！\n\n"
                f"进化规则（必须严格遵守）：\n"
                f"1. 只能根据当前战场困境与自身物种解剖学/生物特征量身打造自定义特性\n"
                f"2. 严禁敷衍堆叠纯数值（禁止'获得50格挡'、'造成30伤害'等）\n"
                f"3. 必须保证活过下一回合\n"
                f"4. 命名不超过4个字\n"
                f"5. 必须改变规则与机制维度\n"
                f"6. 只能利用已有道纹、速度、法力、生命等资源作为代价，形成新规则且有明确负面效果\n"
                f"7. 进化特性不受残韵干扰\n\n"
                f"请DM设计进化特性。"
            ),
            state_snapshot=self.state.to_dict()
        )
    
    # ========== 急中生智 ==========
    
    def initiate_wit(self, declarer: Entity, target: Entity) -> Interrupt:
        """
        急中生智声明
        规则：
        1. 无法以任何形式造成伤害，同种解法第二次失效
        2. 公式：知识+环境元素+道纹=干扰目标
        3. 严禁进行纯数值买卖
        4. 只允许利用现实存在的概念
        5. 严禁以任何形式中断、解除、削弱或篡改已生效的道纹、法术与特性
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
    
    # ========== 辅助方法 ==========
    
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
