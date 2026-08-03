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
    
    # 副本专属道纹（不×3）
    REGION_EXCLUSIVE_DAOWEN = {
        "扭曲都市": {"变形","定型","畸变","僵化","超频","坏死","爆裂","退化"},
        "罪孽都市": {"洗劫","逼债","抵扣","清算","赎金","假钞","赌命","消灾"},
        "龙心谷":   {"加害","龙鳞","逆鳞","活血","裂变","嫁祸","背负","伤痕"},
    }
    
    # 怪物原始道纹+转化道纹（这些也不×3，因为是怪物自己的）
    MONSTER_OWN_DAOWEN = {
        "狂暴","强化","活力","减速","必中","自愈","飞行",
        "愤怒","自残","无神","借力","弱化","自食","兴奋","无力",
        "迟滞","急速","加速","眩晕","洞察","蒙蔽","滋养","衰败",
        "寄生","滑翔","坠落",
    }
    
    def __init__(self, state: GameState, dice: DiceEngine):
        self.state = state
        self.dice = dice
        self.combat_log: list[dict] = []  # 完整战斗日志
        # 降服追踪：本回合各怪物对轮回者造成的伤害（回始归零）
        self._round_monster_damage: dict[str, int] = {}
    
    # ========== 伤害计算 ==========
    
    def is_monster_triple(self, dao_wen_name: str, entity: Entity) -> int:
        """
        怪物非专属道纹效果×3规则
        规则：怪物使用非专属道纹效果×3，副本专属道纹按原效果结算
        返回：效果倍率（1或3）
        """
        if entity.entity_type != "怪物":
            return 1
        
        # 怪物自己的道纹（原始+转化）不×3
        if dao_wen_name in self.MONSTER_OWN_DAOWEN:
            return 1
        
        # 副本专属道纹不×3
        region = self.state.current_region
        if region in self.REGION_EXCLUSIVE_DAOWEN:
            if dao_wen_name in self.REGION_EXCLUSIVE_DAOWEN[region]:
                return 1
        
        # 其他道纹（核心道纹等）×3
        return 3
    
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

        # 必中（含必中状态）
        must_hit = is_must_hit or attacker.has_status("必中")
        # 闪避判定
        if dodge:
            if must_hit:
                result["dodge_success"] = False
                result["dodge_fail_reason"] = "必中攻击无法闪避"
            elif target.current_speed >= 1:
                target.current_speed -= 1
                result["dodge_success"] = True
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
        # 加害：攻击者造成的伤害+X
        if attacker.has_status("加害"):
            damage += attacker.get_status_value("加害")
        # 龙鳞：目标每次受到伤害-X（最低0）
        if target.has_status("龙鳞"):
            damage = max(0, damage - target.get_status_value("龙鳞"))

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
        # 法术：受到伤害前（玩家为目标时触发反应型法术，可能反杀攻击者或加盾）
        if self.state.player is not None and target is self.state.player and damage > 0:
            slogs = self.trigger_spells("受到伤害前", {"attacker": attacker, "target": target, "incoming": damage})
            if slogs:
                result["spell_logs"] = slogs
            if not attacker.is_alive:
                damage = 0  # 攻击者被法术反杀
        # 裂变：受到伤害分X次结算（每次=原伤害÷X向下取整）
        if target.has_status("裂变") and damage > 0:
            xv = target.get_status_value("裂变") or 1
            if xv > 1:
                per = damage // xv
                ta = ts = 0; died = False
                for _ in range(xv):
                    dr = target.take_damage(per, "普通" if not ignore_shield else "无视格挡")
                    ta += dr["actual_damage"]; ts += dr["shield_absorbed"]; died = died or dr["died"]
                damage_result = {"actual_damage": ta, "shield_absorbed": ts, "hp_after": target.current_hp, "died": died, "split": xv}
            else:
                damage_result = target.take_damage(damage, "普通" if not ignore_shield else "无视格挡")
        else:
            damage_result = target.take_damage(damage, "普通" if not ignore_shield else "无视格挡")
        result["damage_dealt"] = damage_result["actual_damage"]
        result["shield_absorbed"] = damage_result["shield_absorbed"]
        result["hp_lost"] = damage_result["actual_damage"]
        result["target_died"] = damage_result["died"]
        result["target_hp_after"] = damage_result["hp_after"]
        if "split" in damage_result:
            result["split"] = damage_result["split"]
        # 法术：失去生命后（玩家实损>0时触发）
        if (self.state.player is not None and target is self.state.player
                and damage_result["actual_damage"] > 0 and target.is_alive):
            slogs2 = self.trigger_spells("失去生命后", {"attacker": attacker, "target": target})
            if slogs2:
                result.setdefault("spell_logs", []).extend(slogs2)
        
        # 结算后效果
        # 兴奋：每次出手后速度+1
        if attacker.has_status("兴奋"):
            speed_gain = attacker.get_status_value("兴奋")
            attacker.current_speed += speed_gain
            result["speed_boost_from_excitement"] = speed_gain

        # 降服追踪：怪物对轮回者造成的伤害计入本回合累计
        if (attacker.entity_type in ("怪物",) and
                self.state.player is not None and target is self.state.player):
            self.record_monster_damage(attacker, result.get("hp_lost", 0))

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
        1. 法力补满至法限
        2. 结算回始类效果
        3. 返回需要决策的信息
        """
        effects = []
        
        # 降服追踪：本回合各怪物伤害记录归零
        self._round_monster_damage = {}
        # 活血追踪归零
        for e in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            e.hp_lost_this_round = 0
        # 遗物：回始触发
        relic_logs = self.process_relics("round_start")
        effects.extend({"type": "relic", "log": l} for l in relic_logs)
        
        # 轮回者法力补满
        if self.state.player and self.state.player.is_alive:
            old_mana = self.state.player.current_mana
            self.state.player.current_mana = self.state.player.mana_limit
            effects.append({
                "type": "mana_refill",
                "entity": self.state.player.name,
                "from": old_mana,
                "to": self.state.player.mana_limit
            })
        
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
        
        # 持续效果递减
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            expired = entity.tick_status_effects()
            if expired:
                effects.append({
                    "type": "status_expired",
                    "entity": entity.name,
                    "expired_effects": expired
                })

        # 活血：有活血状态的实体，回终按本回合累计失血÷2回复
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            if entity.has_status("活血") and entity.hp_lost_this_round >= 2:
                heal_n = entity.hp_lost_this_round // 2
                h = entity.heal(heal_n)
                effects.append({"type": "huoxue_heal", "entity": entity.name,
                                "heal": heal_n, "actual": h["actual_heal"]})
            entity.hp_lost_this_round = 0

        # 降服结算：连续3回合未能对轮回者造成伤害的怪物被降服
        tamed = self.settle_taming()
        if tamed:
            effects.extend(tamed)

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
    
    # ========== 多路径胜利系统 ==========
    # 所有阈值数值均为占位初值，需经测试调整（见 AI_EXPERIENCE.md）

    TAMING_REQUIRED_TURNS = 3     # 降服：连续N回合未造成伤害
    PROLIFERATION_THRESHOLD = 1.0  # 增生：累计受到恢复量达到血限的N倍（占位）
    DEBT_THRESHOLD = 10           # 还债：怪物负债（碎片为负）达到N触发（占位）
    SCULPTURE_DAMAGE = 15         # 雕塑：每点耐久可造成的伤害
    SCULPTURE_SHIELD = 20         # 雕塑：每点耐久可获得的格挡

    def record_monster_damage(self, monster: Entity, damage_to_player: int) -> None:
        """记录怪物本回合对轮回者造成的伤害（用于降服计数）"""
        if not monster.is_alive or monster.is_subdued:
            return
        self._round_monster_damage[monster.name] = (
            self._round_monster_damage.get(monster.name, 0) + max(0, damage_to_player)
        )

    def settle_victory_paths(self) -> list[dict]:
        """
        回终多路径胜利结算（依次检查：降服 / 雕塑 / 增生 / 还债）
        所有路径都不视为击杀，不提供碎片收益
        """
        results = []
        for monster in list(self.state.enemies):
            if not monster.is_alive or monster.is_subdued or monster.is_sculptured \
                    or monster.is_proliferated or monster.is_debt_bound:
                continue

            # 1. 降服：连续N回合未造成伤害
            damage = self._round_monster_damage.get(monster.name, 0)
            if damage > 0:
                monster.no_damage_streak = 0
            else:
                monster.no_damage_streak += 1
            if monster.no_damage_streak >= self.TAMING_REQUIRED_TURNS:
                results.append(self._subdue_monster(monster))
                continue

            # 2. 雕塑：攻击次数或攻击力之一归0
            if monster.attack_count <= 0 or monster.attack_power <= 0:
                results.append(self._sculpture_monster(monster))
                continue

            # 3. 增生：累计受到恢复量达阈值
            threshold = math.ceil(monster.blood_limit * self.PROLIFERATION_THRESHOLD)
            if monster.blood_limit > 0 and monster.total_healed >= threshold:
                results.append(self._proliferate_monster(monster))
                continue

            # 4. 还债：负债达阈值（怪物shards为负）
            if monster.shards <= -self.DEBT_THRESHOLD:
                results.append(self._debt_bind_monster(monster))
                continue

        # 清空本回合伤害记录
        self._round_monster_damage = {}
        return results

    def _remove_from_combat(self, monster: Entity):
        """将怪物移出战斗（不视为击杀）"""
        monster.is_alive = False

    def _subdue_monster(self, monster: Entity) -> dict:
        """降服：记录面板、移出战斗、生成召唤物消耗品"""
        panel = self._snapshot_monster_panel(monster)
        monster.is_subdued = True
        self._remove_from_combat(monster)
        consumable = Consumable(
            name=f"{monster.name}召唤物",
            effect=(f"使用后召唤{monster.name}（{panel['attack_count']}×"
                    f"{panel['attack_power']}/{panel['blood_limit']}）作为临时朋友作战，战终离去"),
            current_uses=1,
            max_uses=1,
            kind="summon",
            panel=panel,
        )
        self.state.consumables.append(consumable)
        return {
            "type": "taming",
            "monster": monster.name,
            "panel": panel,
            "consumable": consumable.name,
            "note": (f"{monster.name}连续{self.TAMING_REQUIRED_TURNS}回合未能对轮回者造成伤害，"
                     f"已被降服，化为消耗品【{consumable.name}】"),
        }

    def _sculpture_monster(self, monster: Entity) -> dict:
        """雕塑：攻击次数或攻击力归0→化为雕塑消耗品（耐久=血限5%）"""
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
        """增生：累计受到恢复量达阈值→吸收进死者之书，强化休整"""
        monster.is_proliferated = True
        self._remove_from_combat(monster)
        absorbed = monster.total_healed
        # 死者之书强化：每只增生怪物使局外【休整】额外产生8点恢复量（占位，可调）
        boost = 8
        self.state.death_book_wisdom.append(f"增生·{monster.name}：休整恢复量+{boost}")
        return {
            "type": "proliferation",
            "monster": monster.name,
            "absorbed_heal": absorbed,
            "rest_boost": boost,
            "note": (f"{monster.name}累计承受{absorbed}点恢复被增生吸收进《死者之书》，"
                     f"局外【休整】恢复量+{boost}"),
        }

    def _debt_bind_monster(self, monster: Entity) -> dict:
        """还债：负债达阈值→视为员工；负债还清后离开"""
        monster.is_debt_bound = True
        # 转为员工（保留当前面板），其待还负债记录于 shards（负值）
        monster.entity_type = "员工"
        self.state.employees.append(monster)
        self.state.enemies.remove(monster)
        return {
            "type": "debt_bind",
            "monster": monster.name,
            "debt": -monster.shards,
            "note": (f"{monster.name}负债达{-monster.shards}，触发还债，视为[员工]参战；"
                     f"还清负债（支付{-monster.shards}碎片）后该员工离队"),
        }

    def _snapshot_monster_panel(self, monster: Entity) -> dict:
        """记录怪物当前面板快照（用于降服召唤物）"""
        return {
            "name": monster.name,
            "entity_type": monster.entity_type,
            "attack_count": monster.attack_count,
            "attack_power": monster.attack_power,
            "blood_limit": monster.blood_limit,
            "current_hp": monster.current_hp,
            "is_flying": monster.is_flying,
            "dao_wen": {
                k: {"name": v.dao_wen.name, "x_value": v.x_value}
                for k, v in monster.dao_wen.items()
            },
        }

    def summon_tamed_friend(self, consumable: Consumable) -> dict:
        """使用降服召唤物：召唤临时朋友（战终离去）"""
        if consumable.kind != "summon" or not consumable.panel:
            return {"success": False, "error": "非召唤物或无面板记录"}
        if consumable.is_depleted:
            return {"success": False, "error": "消耗品已耗尽"}
        panel = consumable.panel
        friend = Entity(
            name=panel["name"],
            entity_type="临时朋友",
            blood_limit=panel["blood_limit"],
            current_hp=panel["current_hp"],
            attack_count=panel["attack_count"],
            attack_power=panel["attack_power"],
            is_flying=panel.get("is_flying", False),
        )
        for k, info in panel.get("dao_wen", {}).items():
            friend.dao_wen[k] = DaoWenInstance(
                dao_wen=DaoWen(
                    name=info["name"], formula="", cost_type="",
                    cost_formula="", effect_formula=""
                ),
                x_value=info.get("x_value", 0),
            )
        self.state.temp_friends.append(friend)
        consumable.use()
        return {
            "success": True,
            "type": "summon_tamed_friend",
            "friend": friend.name,
            "panel": panel,
            "consumable_remaining": consumable.current_uses,
            "note": f"{friend.name}作为临时朋友加入战斗，战终离去",
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

    # 兼容旧接口名
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
    
    def apply_daowen_effect(self, name: str, calc: dict, caster: Entity, target: Entity) -> dict:
        """应用道纹效果（统一效果键处理；供api与法术共用）"""
        result = {"daowen": name, "effects": []}
        multiplier = self.is_monster_triple(name, caster)
        if multiplier > 1:
            result["monster_triple"] = True
            result["multiplier"] = multiplier
        x = calc.get("x", 0)

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

        # ---- 伤害类 ----
        if "target_damage" in calc:
            dmg = target.take_damage(0 if mengbi_blocked else calc["target_damage"] * multiplier)
            result["effects"].append({"type": "damage", "target": target.name, **dmg})
        if "total_damage" in calc and "target_damage" not in calc:  # 血债等多段
            dmg = target.take_damage(0 if mengbi_blocked else calc["total_damage"] * multiplier)
            result["effects"].append({"type": "damage", "target": target.name, **dmg})
        if "aoe_damage" in calc:
            a = 0 if mengbi_blocked else calc["aoe_damage"] * multiplier
            for enemy in self.state.get_all_enemy_side():
                result["effects"].append({"type": "aoe_damage", "target": enemy.name, **enemy.take_damage(a)})
        if "hp_percent_loss" in calc:  # 赌命：当前生命百分比
            d = math.ceil(target.current_hp * calc["hp_percent_loss"] / 100)
            result["effects"].append({"type": "pct_damage", "target": target.name, **target.take_damage(d)})

        # ---- 回复类 ----
        if "target_heal" in calc and not huaisi_block:
            result["effects"].append({"type": "heal", "target": target.name, **target.heal(calc["target_heal"] * multiplier)})
        if "heal_percent" in calc and not (target.has_status("坏死")):
            h = math.ceil(target.blood_limit * calc["heal_percent"] / 100)
            result["effects"].append({"type": "heal_pct", "target": target.name, **target.heal(h)})

        # ---- 格挡/血限 ----
        if "target_shield" in calc:
            s = calc["target_shield"] * multiplier; target.gain_shield(s)
            result["effects"].append({"type": "shield", "target": target.name, "amount": s})
        if "shield_drain" in calc:  # 清算：目标失格挡
            lost = min(target.shield, calc["shield_drain"]); target.shield -= lost
            result["effects"].append({"type": "shield_drain", "target": target.name, "lost": lost})
        if "blood_limit_reduction" in calc:
            target.blood_limit -= calc["blood_limit_reduction"]; target.current_hp = min(target.current_hp, target.blood_limit)
            if target.current_hp <= 0: target.is_alive = False
            result["effects"].append({"type": "blood_limit_reduction", "target": target.name, "new_blood_limit": target.blood_limit})
        if "blood_limit_increase" in calc:
            target.blood_limit += calc["blood_limit_increase"]
            result["effects"].append({"type": "blood_limit_increase", "target": target.name, "increase": calc["blood_limit_increase"]})
        if "blood_limit_penalty" in calc and target.shards < (calc.get("shard_drain", 0) or 0):
            # 逼债：碎片不足则失血限
            target.blood_limit -= calc["blood_limit_penalty"]; target.current_hp = min(target.current_hp, target.blood_limit)
            result["effects"].append({"type": "bizhai_blood", "target": target.name, "lost": calc["blood_limit_penalty"]})

        # ---- 攻击面板修改 ----
        if "attack_boost" in calc:
            target.attack_power += calc["attack_boost"]
            result["effects"].append({"type": "attack_boost", "target": target.name, "attack_power": target.attack_power})
        if "attack_reduction" in calc:
            target.attack_power = max(0, target.attack_power - calc["attack_reduction"])
            result["effects"].append({"type": "attack_reduction", "target": target.name, "attack_power": target.attack_power})
        if "attack_fixed" in calc:
            target.attack_power = calc["attack_fixed"]
            result["effects"].append({"type": "attack_fixed", "target": target.name, "attack_power": target.attack_power})
        if "attack_count_fixed" in calc:
            target.attack_count = calc["attack_count_fixed"]
            result["effects"].append({"type": "attack_count_fixed", "target": target.name, "attack_count": target.attack_count})
        if name == "变形":  # 自身攻击力与攻击次数互换
            caster.attack_power, caster.attack_count = caster.attack_count, caster.attack_power
            result["effects"].append({"type": "swap", "target": caster.name, "attack_power": caster.attack_power, "attack_count": caster.attack_count})

        # ---- 速度修改 ----
        if "speed_boost" in calc:
            target.current_speed += calc["speed_boost"]
            result["effects"].append({"type": "speed_boost", "target": target.name, "speed": target.current_speed})
        if "speed_halved" in calc:
            target.current_speed = target.current_speed // 2
            result["effects"].append({"type": "speed_halved", "target": target.name, "speed": target.current_speed})
        if "speed_penalty" in calc:
            target.current_speed = max(0, target.current_speed - calc["speed_penalty"])
            result["effects"].append({"type": "speed_penalty", "target": target.name, "speed": target.current_speed})

        # ---- 碎片系（罪孽）----
        if "shard_drain" in calc:  # 逼债：目标失碎片（可负债）
            target.shards -= calc["shard_drain"]
            result["effects"].append({"type": "shard_drain", "target": target.name, "shards": target.shards})
        if "shard_steal" in calc:  # 赎金/洗劫：夺碎片
            steal = calc["shard_steal"]; gained = max(0, min(target.shards, steal))
            target.shards -= steal
            if caster is self.state.player: self.state.shards += gained
            result["effects"].append({"type": "shard_steal", "target": target.name, "gained": gained})
        if "fake_shards" in calc:
            self.state.shards += calc["fake_shards"]
            result["effects"].append({"type": "fake_shards", "gained": calc["fake_shards"]})
        if "cost_shards" in calc:
            self.state.shards -= calc["cost_shards"]
            result["effects"].append({"type": "cost_shards", "spent": calc["cost_shards"]})

        # ---- 代价 ----
        if "cost_hp" in calc:
            result["effects"].append({"type": "bleed_cost", "source": caster.name, **caster.take_damage(calc["cost_hp"], "代价")})
        if "cost_blood_limit" in calc:
            caster.blood_limit -= calc["cost_blood_limit"]; caster.current_hp = min(caster.current_hp, caster.blood_limit)
            result["effects"].append({"type": "aging_cost", "source": caster.name, "new_blood_limit": caster.blood_limit})
        if "cost_speed" in calc:
            caster.current_speed -= calc["cost_speed"]
            result["effects"].append({"type": "fatigue_cost", "source": caster.name, "new_speed": caster.current_speed})
        if "mana_gain" in calc:
            caster.current_mana += calc["mana_gain"]
            result["effects"].append({"type": "mana_gain", "source": caster.name, "mana_gained": calc["mana_gain"]})

        # ---- 特殊 ----
        if "self_attack_count" in calc:  # 自残：目标自打X次
            for _ in range(calc["self_attack_count"]):
                result["effects"].append({"type": "self_attack", "target": target.name, **target.take_damage(target.attack_power)})
        if "targets_removed" in calc:  # 封印：移出X怪
            removed = 0
            for e in list(self.state.enemies):
                if e.is_alive and removed < calc["targets_removed"]:
                    e.is_alive = False; e.is_subdued = True; removed += 1
            result["effects"].append({"type": "seal", "removed": removed})

        # ---- 持续/触发状态（status_added）----
        if "duration" in calc and calc.get("duration") is not None:
            duration = calc["duration"] if calc["duration"] != 0 else -1
            effect_target = target if target else caster
            # 自身作用型道纹(变形/超频/自食等)作用于施法者
            self_targeted = name in ("超频", "自食", "飞行", "滑翔", "狂暴", "自愈", "必中", "变形")
            et = caster if self_targeted else effect_target
            et.add_status(StatusEffect(name=name, remaining_rounds=duration, value=x, source=caster.name))
            result["effects"].append({"type": "status_added", "target": et.name, "status": name, "duration": duration, "value": x})
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

    def trigger_death_legacy(self, wisdom: str) -> dict:
        """
        死之传承（轮回者[命零]时触发）：在《死者之书》新增一条遗言（≤20字）。
        """
        wisdom = wisdom[:self.state.death_book_capacity] if wisdom else ""
        self.state.death_book_wisdom.append(wisdom)
        return {"triggered": True, "wisdom": wisdom, "total_wisdom": len(self.state.death_book_wisdom)}

    def process_relics(self, trigger: str, ctx: dict = None) -> list:
        """遗物效果触发框架。trigger: battle_start/round_start/on_dodge/on_speed_zero/on_monster_death"""
        ctx = ctx or {}
        player = self.state.player
        logs = []
        if not player:
            return logs
        relics = {r.name for r in self.state.relics}

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
                    player.take_damage(x, "代价"); player.current_mana += x
                    logs.append(f"鲜血契约：流血{x}，+{x}法力")
        if trigger == "on_monster_death" and "钱袋" in relics:
            m = ctx.get("monster")
            if m:
                gain = max(1, math.ceil(m.blood_limit * 0.02))
                self.state.shards += gain; logs.append(f"钱袋：+{gain}碎片")
        return logs

    # ========== 怪物回合（引擎自主驱动） ==========
    # 怪物已激活的道纹（按战斗重置）
    _monster_activated: dict = {}

    # 成长/控场道纹激活优先级
    MONSTER_ACTIVATE_PRIORITY = ["活力", "强化", "狂暴", "必中", "蒙蔽", "坏死", "减速", "僵化", "自愈", "庇护", "飞行"]

    def reset_monster_activation(self):
        """战始重置怪物激活状态"""
        self._monster_activated = {}

    def _monster_activate(self, m: Entity, activated: set):
        """怪物道纹出手：激活一个未激活的成长/控场道纹，返回道纹名或None"""
        for g in self.MONSTER_ACTIVATE_PRIORITY:
            if g in m.dao_wen and g not in activated:
                activated.add(g)
                if g == "强化":
                    m.attack_power += m.dao_wen[g].x_value
                return g
        return None

    def _monster_attack_actions(self, m: Entity, activated: set) -> int:
        """怪物攻击出手数 = 1 + 活力X(若激活) + 狂暴1(若激活)"""
        n = 1
        if "活力" in activated:
            n += m.dao_wen["活力"].x_value
        if "狂暴" in activated:
            n += 1
        return n

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
        whiteboard = self.state.current_round <= 1
        for m in self.state.get_all_enemy_side():
            if not self.can_act(m):
                results.append({"monster": m.name, "skipped": "眩晕/束缚"})
                continue
            act = self._monster_activated.setdefault(id(m), set())
            # 道纹出手（白板第1回合不激活）
            if not whiteboard:
                an = self._monster_activate(m, act)
                if an in ("蒙蔽", "坏死", "减速", "僵化"):
                    self._apply_control_to_player(an, m, player)
                    results.append({"monster": m.name, "daowen_activated": an})
            # 攻击出手
            n = self._monster_attack_actions(m, act)
            must = m.has_status("必中") or "必中" in act
            for _ in range(n):
                if not player.is_alive:
                    break
                for _h in range(m.attack_count):
                    if not player.is_alive or not m.is_alive:
                        break
                    dodge = (dodge_policy == "auto" and not must
                             and player.current_speed > 0 and m.attack_power > player.shield)
                    results.append(self.resolve_attack(m, player, is_must_hit=must, dodge=dodge))
        return results

    def can_act(self, entity: Entity) -> bool:
        """是否可出手（眩晕/束缚下不可）"""
        return entity.is_alive and not entity.has_status("眩晕") and not entity.has_status("束缚")

    def is_targetable(self, attacker: Entity, target: Entity) -> bool:
        """目标是否可被选中（飞行状态下，非飞行攻击者无法选中）"""
        if getattr(target, "is_flying", False) or target.has_status("飞行"):
            return getattr(attacker, "is_flying", False) or attacker.has_status("飞行")
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
