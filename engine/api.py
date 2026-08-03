"""
游戏引擎 API - AI的唯一交互入口
核心原则：
1. AI通过此API获取状态、做出决策、触发行动
2. 所有数值计算由引擎完成，AI禁止自行计算
3. 程序无法判定时返回Interrupt，等待DM裁定
4. DM裁定存入数据库，下次类似情况自动匹配
"""
from __future__ import annotations
import json
import os
import time
import uuid
from typing import Optional, Any

from .models import (
    Entity, GameState, DaoWen, DaoWenInstance, Spell, 
    Relic, Consumable, StatusEffect, LongJiXin
)
from .enums import GamePhase, InterruptType, EntityType
from .dice import DiceEngine, EventPool, RandomRequest
from .daowen import DaoWenEngine, ResonanceEngine
from .combat import CombatEngine
from .dm_rulings import DMRulingsDB, DMRuling, Interrupt
from .spells import (
    SPELL_LIBRARY, SpellDef, get_spell, list_spells,
    validate_building_blocks, create_custom_spell, MAX_SPELL_LOOPS,
)
from .content import (
    build_relic_pool, get_relic, MONSTER_POOLS, get_monster_pool,
    monster_count_for_battle, GENERAL_EVENTS, get_event, list_general_events,
    make_consumable, MonsterDef,
)


class GameEngine:
    """
    游戏引擎主类
    AI通过此接口与游戏交互，所有数值计算必须经过本引擎
    """
    
    def __init__(self, db_path: str = "data/dm_rulings.db", save_dir: str = "data/saves"):
        self.state = GameState()
        self.dice = DiceEngine()
        self.event_pool = EventPool()
        self.combat = CombatEngine(self.state, self.dice)
        self.rulings_db = DMRulingsDB(db_path)
        self.save_dir = save_dir
        os.makedirs(save_dir, exist_ok=True)
        
        # 初始化遗物池（12件通用遗物，事件遗物不加入）
        self.state.relics_pool = build_relic_pool()
        
        # 规则校验器（延迟导入避免循环）
        self._validator = None
        self._rule_sync = None
        
        # 中断队列（等待DM裁定）
        self._pending_interrupts: list[Interrupt] = []
        
        # 行动历史（可追溯）
        self._action_history: list[dict] = []
        
        # 上一次的行动结果
        self._last_result: Optional[dict] = None
    
    @property
    def validator(self):
        if self._validator is None:
            from .validator import RuleValidator
            self._validator = RuleValidator()
        return self._validator
    
    @property
    def rule_sync(self):
        return self._rule_sync
    
    def enable_rule_sync(self, rule_files: list[str], rules_dir: str = "."):
        """启用规则文件同步"""
        from .rule_sync import RuleSync
        self._rule_sync = RuleSync(rule_files=rule_files, rules_dir=rules_dir)
    
    # ==================== 状态查询 ====================
    
    def get_state(self) -> dict:
        """
        获取当前游戏完整状态
        AI每次决策前应调用此方法
        """
        return {
            "state": self.state.to_dict(),
            "pending_interrupts": [i.to_dict() for i in self._pending_interrupts],
            "last_result": self._last_result,
            "available_actions": self.get_available_actions(),
        }
    
    def get_available_actions(self) -> dict:
        """
        获取当前可用行动
        根据游戏阶段返回不同的行动列表
        """
        phase = self.state.phase
        
        if phase == "setup":
            return self._get_setup_actions()
        elif phase == "pre_battle":
            return self._get_pre_battle_actions()
        elif phase == "battle_start":
            return self._get_battle_start_actions()
        elif phase == "in_combat":
            return self._get_combat_actions()
        elif phase == "battle_end":
            return self._get_battle_end_actions()
        elif phase == "dead_duel":
            return self._get_dead_duel_actions()
        else:
            return {"actions": [], "note": "游戏已结束"}
    
    def _get_setup_actions(self) -> dict:
        return {
            "phase": "开局",
            "required_actions": [
                "分配25点初始属性点（1点=6血限=1速限=2法限）",
                "在【杀伐】【锐利】中选择一种作为初始道纹",
                "选择一个一阶副本（罪孽都市/扭曲都市/龙心谷）"
            ],
            "auto_actions": [
                "获得20碎片",
                "发现一件遗物（需要随机数）",
                "自选一种残韵（转换/反转/曲解）"
            ],
            "attribute_points_remaining": self.state.attribute_points if self.state.attribute_points > 0 else 25,
        }
    
    def _get_pre_battle_actions(self) -> dict:
        return {
            "phase": "局外阶段",
            "energy": self.state.energy,
            "actions": [
                {"id": "领悟", "cost": 1, "description": "选择获得1种残韵"},
                {"id": "休整", "cost": 1, "description": "产生8恢复量，可自由分配"},
                {"id": "休整_10碎片", "cost": 1, "description": "产生24恢复量（消耗10碎片）"},
                {"id": "休整_25碎片", "cost": 1, "description": "产生48恢复量（消耗25碎片）"},
                {"id": "修行", "cost": 1, "description": "获得1点属性点"},
                {"id": "修行_15碎片", "cost": 1, "description": "获得2点属性点（消耗15碎片）"},
                {"id": "学习", "cost": 1, "description": "选择学会一种法术/习得一种转化道纹"},
                {"id": "学习_10碎片", "cost": 1, "description": "选择学会两种法术（消耗10碎片）"},
                {"id": "共鸣", "cost": 1, "description": "发现一件遗物（需要随机数）"},
                {"id": "共鸣_自选", "cost": 1, "description": "自选一件遗物（消耗15碎片，需要精力再-1）"},
                {"id": "探索", "cost": 1, "description": "发现1个未遇到事件（需要随机数）"},
            ],
            "region_actions": self._get_region_actions(),
        }
    
    def _get_region_actions(self) -> list[dict]:
        region = self.state.current_region
        if region == "扭曲都市":
            return [{"id": "维修", "cost": 1, "description": "获得耐久分配点数"}]
        elif region == "罪孽都市":
            return [{"id": "雇佣", "cost": 1, "description": "diy一位微光者员工"}]
        elif region == "龙心谷":
            return [{"id": "炼心", "cost": 1, "description": "准备龙心，直到下次支付代价后获得"}]
        return []
    
    def _get_battle_start_actions(self) -> dict:
        return {
            "phase": "战始",
            "required": [
                "抽取怪物（需要随机数）",
                "选择战斗背景",
                "结算战始效果"
            ],
            "current_battle": self.state.current_battle,
        }
    
    def _get_combat_actions(self) -> dict:
        """获取战斗中可用行动"""
        player = self.state.player
        if not player or not player.is_alive:
            return {"actions": [], "note": "轮回者已死亡"}
        
        actions = []
        
        # 道纹行动
        for name, dw_instance in player.dao_wen.items():
            if dw_instance.can_use():
                max_x = player.current_mana  # 自由控X：1≤X≤当前可用法力
                actions.append({
                    "type": "daowen",
                    "id": name,
                    "max_x": max_x,
                    "description": f"发动【{name}X】(1≤X≤{max_x})",
                    "cost_type": dw_instance.dao_wen.cost_type,
                    "requires_target": "target" in dw_instance.dao_wen.effect_formula if hasattr(dw_instance.dao_wen, 'effect_formula') else True
                })
            else:
                actions.append({
                    "type": "daowen",
                    "id": name,
                    "available": False,
                    "reason": f"冷却剩余{dw_instance.cooldown_remaining}场" if dw_instance.cooldown_remaining > 0 else "被封印"
                })
        
        # 法术行动
        for spell in player.spells:
            can_cast = all(
                name in player.dao_wen and player.dao_wen[name].can_use()
                for name in spell.required_daowen
            )
            actions.append({
                "type": "spell",
                "id": spell.name,
                "available": can_cast,
                "required_daowen": spell.required_daowen,
                "trigger_condition": spell.trigger_condition,
            })
        
        # 残韵
        available_resonance = []
        for name, count in self.state.resonance.items():
            if count > 0:
                available_resonance.append({"type": name, "count": count})
        
        if available_resonance:
            actions.append({
                "type": "resonance",
                "available_resonances": available_resonance,
                "note": "残韵可在任意时刻插队使用"
            })
        
        # 消耗品
        for item in self.state.consumables:
            if not item.is_depleted:
                actions.append({
                    "type": "consumable",
                    "id": item.name,
                    "uses_remaining": item.current_uses,
                    "effect": item.effect,
                })
        
        # 急中生智
        actions.append({
            "type": "wit_of_desperation",
            "description": "消耗一次出手，声明急中生智",
            "available": True
        })
        
        # 逃跑
        actions.append({
            "type": "escape",
            "description": "尝试逃跑（触发逃跑与追击事件）",
            "available": True
        })
        
        # 普通攻击
        actions.append({
            "type": "attack",
            "description": f"发动一轮攻击（{player.attack_count}次，每次{player.attack_power}伤害）",
            "attack_count": player.attack_count,
            "attack_power": player.attack_power,
        })
        
        return {
            "phase": "战斗回合",
            "round": self.state.current_round,
            "energy": self.state.energy,
            "actions": actions,
        }
    
    def _get_battle_end_actions(self) -> dict:
        return {
            "phase": "战终",
            "shards_earned": 0,  # 将由计算填充
            "clear_bonuses": True,
            "restore_energy": True,
        }
    
    def _get_dead_duel_actions(self) -> dict:
        return {
            "phase": "最终死斗",
            "note": "无法逃跑，只能战斗到一方倒下",
        }
    
    # ==================== 核心行动接口 ====================
    
    def execute_action(self, action_type: str, params: dict = None) -> dict:
        """
        执行行动的统一入口
        AI通过此接口执行所有行动
        
        返回格式：
        {
            "success": bool,
            "action": str,
            "result": dict,      # 行动结果
            "state_update": dict, # 状态变化
            "interrupt": dict,   # 如果触发中断，此处不为空
            "next_actions": list  # 接下来可用的行动
        }
        """
        if params is None:
            params = {}
        
        # 检查是否有待处理的中断
        if self._pending_interrupts:
            return {
                "success": False,
                "error": "有待处理的中断等待DM裁定",
                "pending_interrupts": [i.to_dict() for i in self._pending_interrupts],
                "instruction": "请先通过 submit_ruling() 提交DM裁定"
            }
        
        try:
            if action_type == "setup_attributes":
                result = self._action_setup_attributes(params)
            elif action_type == "setup_choose_daowen":
                result = self._action_setup_choose_daowen(params)
            elif action_type == "setup_choose_region":
                result = self._action_setup_choose_region(params)
            elif action_type == "setup_choose_resonance":
                result = self._action_setup_choose_resonance(params)
            elif action_type == "pre_battle_action":
                result = self._action_pre_battle(params)
            elif action_type == "use_daowen":
                result = self._action_use_daowen(params)
            elif action_type == "use_spell":
                result = self._action_use_spell(params)
            elif action_type == "use_resonance":
                result = self._action_use_resonance(params)
            elif action_type == "attack":
                result = self._action_attack(params)
            elif action_type == "dodge_decision":
                result = self._action_dodge_decision(params)
            elif action_type == "consume_item":
                result = self._action_consume_item(params)
            elif action_type == "declare_wit":
                result = self._action_declare_wit(params)
            elif action_type == "declare_escape":
                result = self._action_declare_escape(params)
            elif action_type == "declare_evolution":
                result = self._action_declare_evolution(params)
            elif action_type == "round_start":
                result = self._action_round_start(params)
            elif action_type == "round_end":
                result = self._action_round_end(params)
            elif action_type == "battle_start":
                result = self._action_battle_start(params)
            elif action_type == "battle_end":
                result = self._action_battle_end(params)
            elif action_type == "random_number":
                result = self._action_submit_random(params)
            else:
                result = {"success": False, "error": f"未知行动类型: {action_type}"}
            
            # 记录行动历史
            self._action_history.append({
                "action": action_type,
                "params": params,
                "result": result,
                "timestamp": time.time(),
                "game_id": self.state.game_id,
                "round": self.state.current_round
            })
            
            # 自动校验（如果启用）
            validation_result = None
            if self._validator and result.get("success"):
                validation_result = self._validator.validate(
                    self.state,
                    {"action": action_type, "params": params},
                    result
                )
                if validation_result.get("violations"):
                    result["validation_violations"] = validation_result["violations"]
                if validation_result.get("warnings"):
                    result["validation_warnings"] = validation_result["warnings"]
                result["validation_passed"] = validation_result.get("valid", True)
            
            self._last_result = result
            return result
            
        except Exception as e:
            error_result = {
                "success": False,
                "error": str(e),
                "action": action_type,
                "instruction": "引擎计算出错，请检查参数"
            }
            self._last_result = error_result
            return error_result
    
    # ==================== 开局行动 ====================
    
    def _action_setup_attributes(self, params: dict) -> dict:
        """
        分配初始属性点
        1属性点 = 6血限 = 1速限 = 2法限
        """
        blood_points = params.get("blood_points", 0)
        speed_points = params.get("speed_points", 0)
        mana_points = params.get("mana_points", 0)
        
        total = blood_points + speed_points + mana_points
        
        if total != 25:
            return {
                "success": False,
                "error": f"属性点总和必须为25，当前为{total}",
                "instruction": "1属性点=6血限=1速限=2法限，请重新分配"
            }
        
        blood_limit = blood_points * 6
        speed_limit = speed_points
        mana_limit = mana_points * 2
        
        player = Entity(
            name=params.get("name", "轮回者"),
            entity_type=EntityType.REINCARNATOR.value,
            blood_limit=blood_limit,
            current_hp=blood_limit,
            mana_limit=mana_limit,
            current_mana=mana_limit,
            speed_limit=speed_limit,
            current_speed=speed_limit,
            attack_count=1,
            attack_power=1,
        )
        
        self.state.player = player
        self.state.attribute_points = 0
        self.state.allocated_blood = blood_limit
        self.state.shards = 20
        
        return {
            "success": True,
            "action": "分配属性点",
            "result": {
                "name": player.name,
                "blood_limit": blood_limit,
                "mana_limit": mana_limit,
                "speed_limit": speed_limit,
                "action_count": player.action_count,
                "shards": 20
            },
            "next_actions": ["setup_choose_daowen", "setup_choose_resonance", "setup_choose_region"],
            "note": "接下来需要：选择初始道纹、选择残韵、选择副本。遗物发现需要随机数。"
        }
    
    def _action_setup_choose_daowen(self, params: dict) -> dict:
        """选择初始道纹"""
        choice = params.get("daowen", "")
        valid = ["杀伐", "锐利"]
        
        if choice not in valid:
            return {"success": False, "error": f"只能从{valid}中选择"}
        
        dw = DaoWen(
            name=choice,
            formula=f"{choice}X的公式",
            cost_type="消耗",
            cost_formula="X",
            effect_formula="2X伤害" if choice == "杀伐" else "4X血限减少"
        )
        
        self.state.player.dao_wen[choice] = DaoWenInstance(dao_wen=dw)
        
        return {
            "success": True,
            "action": "选择初始道纹",
            "result": {"daowen": choice},
            "next_actions": ["setup_choose_resonance", "setup_choose_region"]
        }
    
    def _action_setup_choose_region(self, params: dict) -> dict:
        """选择副本"""
        region = params.get("region", "")
        valid = ["罪孽都市", "扭曲都市", "龙心谷"]
        
        if region not in valid:
            return {"success": False, "error": f"只能从{valid}中选择"}
        
        self.state.current_region = region
        self.state.phase = "pre_battle"
        
        return {
            "success": True,
            "action": "选择副本",
            "result": {"region": region},
            "next_actions": ["pre_battle_action"],
            "note": "开局完成，进入局外阶段。每个副本3点精力，耗尽后进入战斗。"
        }
    
    def _action_setup_choose_resonance(self, params: dict) -> dict:
        """选择初始残韵"""
        rtype = params.get("resonance_type", "")
        valid = ["转换", "反转", "曲解"]
        
        if rtype not in valid:
            return {"success": False, "error": f"只能从{valid}中选择"}
        
        self.state.resonance[rtype] = self.state.resonance.get(rtype, 0) + 1
        
        return {
            "success": True,
            "action": "选择残韵",
            "result": {"resonance_type": rtype, "count": self.state.resonance[rtype]},
            "next_actions": ["setup_choose_daowen", "setup_choose_region"]
        }
    
    # ==================== 局外行动 ====================
    
    def _action_pre_battle(self, params: dict) -> dict:
        """局外阶段行动"""
        action = params.get("sub_action", "")
        
        if self.state.energy <= 0:
            return {
                "success": False,
                "error": "精力已耗尽",
                "instruction": "精力耗尽，进入战斗阶段。请调用 battle_start。"
            }
        
        self.state.energy -= 1
        
        result_map = {
            "领悟": self._pre_battle_lingwu,
            "休整": self._pre_battle_xiuzheng,
            "修行": self._pre_battle_xiuxing,
            "学习": self._pre_battle_xuexi,
            "共鸣": self._pre_battle_gongming,
            "探索": self._pre_battle_tansuo,
            "维修": self._pre_battle_weixiu,
            "雇佣": self._pre_battle_guyong,
            "炼心": self._pre_battle_lianxin,
        }
        
        if action in result_map:
            return result_map[action](params)
        else:
            self.state.energy += 1  # 恢复精力
            return {"success": False, "error": f"未知局外行动: {action}"}
    
    def _pre_battle_lingwu(self, params: dict) -> dict:
        """领悟：选择获得1种残韵"""
        rtype = params.get("resonance_type", "")
        valid = ["转换", "反转", "曲解"]
        if rtype not in valid:
            self.state.energy += 1
            return {"success": False, "error": f"只能从{valid}中选择"}
        
        self.state.resonance[rtype] = self.state.resonance.get(rtype, 0) + 1
        
        return {
            "success": True,
            "action": "领悟",
            "result": {"gained_resonance": rtype, "total": self.state.resonance[rtype]},
            "energy_remaining": self.state.energy
        }
    
    def _pre_battle_xiuzheng(self, params: dict) -> dict:
        """休整：产生恢复量"""
        tier = params.get("tier", 1)  # 1=8, 2=24(10碎片), 3=48(25碎片)
        
        if tier == 1:
            heal = 8
            cost = 0
        elif tier == 2:
            heal = 24
            cost = 10
        elif tier == 3:
            heal = 48
            cost = 25
        else:
            self.state.energy += 1
            return {"success": False, "error": "休整档位无效"}
        
        if self.state.shards < cost:
            self.state.energy += 1
            return {"success": False, "error": f"碎片不足，需要{cost}，当前{self.state.shards}"}
        
        self.state.shards -= cost
        
        return {
            "success": True,
            "action": "休整",
            "result": {
                "heal_amount": heal,
                "shard_cost": cost,
                "instruction": f"获得{heal}点恢复量，可自由分配给自己或队友",
                "shards_remaining": self.state.shards
            },
            "note": "请告知引擎恢复量如何分配（通过后续的 heal_to_entity 行动）"
        }
    
    def _pre_battle_xiuxing(self, params: dict) -> dict:
        """修行：获得属性点"""
        tier = params.get("tier", 1)  # 1=1点, 2=2点(15碎片), ...
        
        tier_map = {1: (1, 0), 2: (2, 15), 3: (3, 35), 4: (4, 65), 5: (5, 100), 6: (6, 150)}
        
        if tier not in tier_map:
            self.state.energy += 1
            return {"success": False, "error": "修行档位无效"}
        
        points, cost = tier_map[tier]
        
        if self.state.shards < cost:
            self.state.energy += 1
            return {"success": False, "error": f"碎片不足，需要{cost}"}
        
        self.state.shards -= cost
        self.state.attribute_points += points
        
        return {
            "success": True,
            "action": "修行",
            "result": {
                "points_gained": points,
                "shard_cost": cost,
                "total_attribute_points": self.state.attribute_points,
                "note": "属性点可用于：1速限=2法限（血限只能在开局获得）"
            }
        }
    
    def _pre_battle_xuexi(self, params: dict) -> dict:
        """
        学习：选择学会一种/两种(10)/三种(25)法术
             / 自创一种法术
             / 习得一种/两种(10)转化道纹
        """
        player = self.state.player
        if not player:
            self.state.energy += 1
            return {"success": False, "error": "没有玩家"}

        sub = params.get("sub", "spell")  # spell / daowen / create_spell
        tier = params.get("tier", 1)

        # 档位费用：法术 1/2(10)/3(25) 种；转化道纹 1/2(10) 种
        if sub == "daowen":
            tier_map = {1: (1, 0), 2: (2, 10)}
        elif sub == "create_spell":
            tier_map = {1: (1, 0)}
        else:
            tier_map = {1: (1, 0), 2: (2, 10), 3: (3, 25)}

        if tier not in tier_map:
            self.state.energy += 1
            return {"success": False, "error": f"学习档位无效: {tier}"}

        count, cost = tier_map[tier]

        if self.state.shards < cost:
            self.state.energy += 1
            return {"success": False, "error": f"碎片不足，需要{cost}，当前{self.state.shards}"}

        # ---- 自创法术 ----
        if sub == "create_spell":
            created = create_custom_spell(
                name=params.get("name", ""),
                steps=params.get("steps", []),
                trigger_condition=params.get("trigger_condition", ""),
                owned_daowen=set(player.dao_wen.keys()),
                custom_conditions=params.get("custom_conditions"),
            )
            if not created["success"]:
                self.state.energy += 1
                return created

            definition: SpellDef = created["spell"]
            SPELL_LIBRARY[definition.name] = definition
            player.spells.append(definition.to_spell())
            self.state.shards -= cost
            return {
                "success": True,
                "action": "学习（自创法术）",
                "result": {
                    "created_spell": definition.to_dict(),
                    "rank": created["rank"],
                    "shard_cost": cost,
                },
                "note": "自创法术[战终]可以进行修订",
            }

        # ---- 习得转化道纹 ----
        if sub == "daowen":
            names = params.get("daowen_names") or ([params["daowen_name"]] if params.get("daowen_name") else [])
            if len(names) != count:
                self.state.energy += 1
                return {"success": False, "error": f"该档位须指定{count}种转化道纹，当前{len(names)}种"}

            transform_set = set(ResonanceEngine.MONSTER_TRANSFORM_DAOWEN)
            learned = []
            for n in names:
                if n not in transform_set:
                    self.state.energy += 1
                    return {
                        "success": False,
                        "error": f"【{n}】不是怪物转化道纹，局外【学习】只能习得转化道纹",
                    }
                if n in player.dao_wen:
                    self.state.energy += 1
                    return {"success": False, "error": f"已持有道纹【{n}】（道纹唯一，不重复存在）"}
                dw = DaoWenEngine.get_definition(n)
                player.dao_wen[n] = DaoWenInstance(dao_wen=dw)
                learned.append(n)

            self.state.shards -= cost
            return {
                "success": True,
                "action": "学习（转化道纹）",
                "result": {"learned_daowen": learned, "shard_cost": cost},
            }

        # ---- 学会已有法术 ----
        names = params.get("spell_names") or ([params["spell_name"]] if params.get("spell_name") else [])
        if len(names) != count:
            self.state.energy += 1
            return {
                "success": False,
                "error": f"该档位须指定{count}种法术，当前{len(names)}种",
                "available_spells": list_spells(),
            }

        known = {s.name for s in player.spells}
        to_learn = []
        for n in names:
            definition = get_spell(n)
            if not definition:
                self.state.energy += 1
                return {"success": False, "error": f"法术库中不存在【{n}】", "available_spells": list_spells()}
            if n in known:
                self.state.energy += 1
                return {"success": False, "error": f"已掌握法术【{n}】"}
            to_learn.append(definition)

        self.state.shards -= cost
        learned = []
        for definition in to_learn:
            player.spells.append(definition.to_spell())
            block = validate_building_blocks(definition, set(player.dao_wen.keys()))
            learned.append({
                "name": definition.name,
                "rank": definition.rank,
                "required_daowen": definition.required_daowen,
                "trigger_condition": definition.trigger_condition,
                "castable_now": block["valid"],
                "missing_daowen": block.get("missing_daowen", []),
            })

        return {
            "success": True,
            "action": "学习（法术）",
            "result": {"learned_spells": learned, "shard_cost": cost},
            "note": "法术已开启；缺少所需道纹时无法发动（积木规则）",
        }
    
    def _pre_battle_gongming(self, params: dict) -> dict:
        """共鸣：发现遗物"""
        sub = params.get("sub", "discover")  # discover / choose
        
        # 共鸣：精力再次-1（共计2点），发现/自选（15碎片）一件遗物
        if self.state.energy < 1:
            self.state.energy += 1
            return {"success": False, "error": "共鸣需要2点精力（本次行动1点+额外1点），精力不足"}
        
        owned = {r.name for r in self.state.relics}
        available = [r for r in self.state.relics_pool if r.name not in owned]
        if not available:
            self.state.energy += 1
            return {"success": False, "error": "遗物池已空"}
        
        if sub == "choose":
            if self.state.shards < 15:
                self.state.energy += 1
                return {"success": False, "error": f"自选遗物需15碎片，当前{self.state.shards}"}
            name = params.get("relic_name", "")
            relic = next((r for r in available if r.name == name), None)
            if not relic:
                self.state.energy += 1
                return {
                    "success": False,
                    "error": f"遗物池中没有【{name}】",
                    "available": [r.name for r in available],
                }
            self.state.energy -= 1
            self.state.shards -= 15
            self.state.relics.append(relic)
            self.state.relics_pool = [r for r in self.state.relics_pool if r.name != relic.name]
            return {
                "success": True,
                "action": "共鸣（自选）",
                "result": {
                    "gained_relic": {"name": relic.name, "effect": relic.effect},
                    "shard_cost": 15,
                    "energy_remaining": self.state.energy,
                },
            }
        
        # 发现：从随机抽取的3个未持有选项中选择1个
        self.state.energy -= 1
        n = min(3, len(available))
        self.dice.create_pool("relic_discover", available)
        return {
            "success": True,
            "action": "共鸣（发现）",
            "random_required": True,
            "pool_name": "relic_discover",
            "pool_range": f"1~{len(available)}",
            "energy_remaining": self.state.energy,
            "instruction": f"请玩家在 1~{len(available)} 中选择一个数字（发现机制）",
            "pool_options": [r.name for r in available],
        }
    
    def _pre_battle_tansuo(self, params: dict) -> dict:
        """探索：发现事件"""
        tier = params.get("tier", 1)  # 1=1个, 2=2个(30碎片)
        
        tier_map = {1: (1, 0), 2: (2, 30)}
        if tier not in tier_map:
            self.state.energy += 1
            return {"success": False, "error": f"探索档位无效: {tier}"}
        count, cost = tier_map[tier]
        
        if self.state.shards < cost:
            self.state.energy += 1
            return {"success": False, "error": f"碎片不足，需要{cost}，当前{self.state.shards}"}
        
        # 事件池：所有未遇到的通用事件（通用在前，专属在后）
        available = [e for e in GENERAL_EVENTS if not self.event_pool.is_encountered(e["name"])]
        if not available:
            self.state.energy += 1
            return {"success": False, "error": "事件池已空（所有事件均已遇到）"}
        
        self.state.shards -= cost
        self.dice.create_pool("event_discover", available)
        
        return {
            "success": True,
            "action": "探索",
            "random_required": True,
            "pool_name": "event_discover",
            "pool_range": f"1~{len(available)}",
            "discover_count": count,
            "shard_cost": cost,
            "instruction": (
                f"请玩家在 1~{len(available)} 中给出数字"
                + (f"（本档位可发现{count}个事件，需分别提交）" if count > 1 else "")
            ),
            "pool_options": [e["name"] for e in available],
        }
    
    def _pre_battle_weixiu(self, params: dict) -> dict:
        """维修（扭曲都市专属）"""
        tier = params.get("tier", 1)
        tier_map = {1: (1, 0), 2: (2, 5), 3: (3, 12)}
        
        if tier not in tier_map:
            self.state.energy += 1
            return {"success": False, "error": "维修档位无效"}
        
        points, cost = tier_map[tier]
        
        if self.state.shards < cost:
            self.state.energy += 1
            return {"success": False, "error": f"碎片不足，需要{cost}"}
        
        self.state.shards -= cost
        
        return {
            "success": True,
            "action": "维修",
            "result": {
                "durability_points": points,
                "shard_cost": cost,
                "instruction": f"获得{points}点耐久分配，可分配给消耗品"
            }
        }
    
    def _pre_battle_guyong(self, params: dict) -> dict:
        """雇佣（罪孽都市专属）"""
        return {
            "success": True,
            "action": "雇佣",
            "result": {
                "instruction": "请告知引擎员工的自定义配置（20点预算：1点=12血限，3点=1攻击次数=2攻击力）",
                "budget": 20,
                "note": "还需选择一种转化道纹"
            }
        }
    
    def _pre_battle_lianxin(self, params: dict) -> dict:
        """炼心（龙心谷专属）"""
        return {
            "success": True,
            "action": "炼心",
            "result": {
                "instruction": "已记录炼心状态，直到下次支付数值为X的代价后，获得对应类型的龙心"
            }
        }
    
    # ==================== 战斗行动 ====================
    
    def _action_use_daowen(self, params: dict) -> dict:
        """发动道纹"""
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}
        
        name = params.get("daowen_name", "")
        x = params.get("x", 1)
        target_name = params.get("target", "")
        
        if name not in player.dao_wen:
            return {"success": False, "error": f"未持有道纹: {name}"}
        
        dw_instance = player.dao_wen[name]
        
        if not dw_instance.can_use():
            if dw_instance.cooldown_remaining > 0:
                return {
                    "success": False,
                    "error": f"道纹【{name}】冷却中，剩余{dw_instance.cooldown_remaining}场",
                }
            return {"success": False, "error": f"道纹【{name}】被封印，不可用"}
        
        # 唯一：使用后本次轮回中无法再次使用
        if name in player.used_unique:
            return {"success": False, "error": f"道纹【{name}】代价为【唯一】，本次轮回已使用过"}
        
        if x < 1:
            return {"success": False, "error": "X必须≥1"}
        
        # 查找目标
        target = player  # 默认目标是自己
        if target_name:
            all_entities = self.state.get_all_player_side() + self.state.get_all_enemy_side()
            for e in all_entities:
                if e.name == target_name:
                    target = e
                    break
        
        # 调用道纹引擎计算
        try:
            calc = DaoWenEngine.resolve(name, x, target=target, caster=player)
        except Exception as e:
            return {"success": False, "error": f"道纹计算失败: {str(e)}"}
        
        # 检查法力是否足够（代价道纹不消耗法力）
        cost = calc.get("cost", calc.get("cost_mutation", 0))
        if calc.get("cost_type") == "消耗" and cost > 0:
            if not player.spend_mana(cost):
                return {"success": False, "error": f"法力不足，需要{cost}，当前{player.current_mana}"}
        
        # 执行效果
        execution = self._execute_daowen_effect(name, calc, player, target)
        
        # 冷却X：使用后记为【X(0)/Y】，[战终]后已完成战斗场数+1，达到Y时才能再次使用
        cost_applied = {}
        if calc.get("cost_type") == "冷却":
            dw_instance.cooldown_remaining = calc.get("cost", x)
            cost_applied["cooldown"] = dw_instance.cooldown_remaining
        
        # 唯一：使用后本次轮回中无法再次使用
        if calc.get("cost_type") == "唯一":
            player.used_unique.add(name)
            cost_applied["unique_locked"] = True
        
        return {
            "success": True,
            "action": f"发动道纹【{name}X={x}】",
            "calculation": calc,
            "execution": execution,
            "cost_applied": cost_applied,
            "state": self.combat._get_combat_state()
        }
    
    def _execute_daowen_effect(self, name: str, calc: dict, caster: Entity, target: Entity) -> dict:
        """执行道纹效果"""
        result = {"daowen": name, "effects": []}
        
        # 伤害类
        if "target_damage" in calc:
            dmg = target.take_damage(calc["target_damage"])
            result["effects"].append({"type": "damage", "target": target.name, **dmg})
        
        # 多段伤害（血债X：对[目标]造成2X次1点伤害）
        # 每一段独立结算，逐段扣减格挡
        if "hits" in calc and "damage_per_hit" in calc:
            hits = calc["hits"]
            per_hit = calc["damage_per_hit"]
            total_actual = 0
            total_absorbed = 0
            for i in range(hits):
                if not target.is_alive:
                    break
                d = target.take_damage(per_hit)
                total_actual += d["actual_damage"]
                total_absorbed += d["shield_absorbed"]
            result["effects"].append({
                "type": "multi_hit_damage",
                "target": target.name,
                "hits": hits,
                "damage_per_hit": per_hit,
                "actual_damage": total_actual,
                "shield_absorbed": total_absorbed,
                "hp_after": target.current_hp,
                "died": not target.is_alive,
            })
        
        # AOE伤害
        if "aoe_damage" in calc:
            for enemy in self.state.get_all_enemy_side():
                dmg = enemy.take_damage(calc["aoe_damage"])
                result["effects"].append({"type": "aoe_damage", "target": enemy.name, **dmg})
        
        # 回复类
        if "target_heal" in calc:
            heal = target.heal(calc["target_heal"])
            result["effects"].append({"type": "heal", "target": target.name, **heal})
        
        # 格挡类
        if "target_shield" in calc:
            target.gain_shield(calc["target_shield"])
            result["effects"].append({"type": "shield", "target": target.name,
                                       "amount": calc["target_shield"]})
        
        # 血限减少
        if "blood_limit_reduction" in calc:
            reduction = calc["blood_limit_reduction"]
            target.blood_limit -= reduction
            target.current_hp = min(target.current_hp, target.blood_limit)
            if target.current_hp <= 0:
                target.is_alive = False
            result["effects"].append({
                "type": "blood_limit_reduction",
                "target": target.name,
                "reduction": reduction,
                "new_blood_limit": target.blood_limit,
                "died": not target.is_alive
            })
        
        # 血限增加
        if "blood_limit_increase" in calc:
            increase = calc["blood_limit_increase"]
            target.blood_limit += increase
            result["effects"].append({"type": "blood_limit_increase", "target": target.name, "increase": increase})
        
        # 流血代价
        if "cost_hp" in calc:
            cost_result = caster.take_damage(calc["cost_hp"], "代价")
            result["effects"].append({"type": "bleed_cost", "source": caster.name, **cost_result})
        
        # 衰老代价
        if "cost_blood_limit" in calc:
            caster.blood_limit -= calc["cost_blood_limit"]
            caster.current_hp = min(caster.current_hp, caster.blood_limit)
            result["effects"].append({
                "type": "aging_cost",
                "source": caster.name,
                "blood_limit_lost": calc["cost_blood_limit"],
                "new_blood_limit": caster.blood_limit
            })
        
        # 疲惫代价（失去X点当前速度）
        if "cost_speed" in calc:
            caster.current_speed = max(0, caster.current_speed - calc["cost_speed"])
            result["effects"].append({
                "type": "fatigue_cost",
                "source": caster.name,
                "speed_lost": calc["cost_speed"],
                "new_speed": caster.current_speed
            })
        
        # 枯竭代价（失去X点法限）
        if "cost_mana_limit" in calc:
            caster.mana_limit = max(0, caster.mana_limit - calc["cost_mana_limit"])
            caster.current_mana = min(caster.current_mana, caster.mana_limit)
            result["effects"].append({
                "type": "exhaust_cost",
                "source": caster.name,
                "mana_limit_lost": calc["cost_mana_limit"],
                "new_mana_limit": caster.mana_limit
            })
        
        # 萎缩代价（失去X点速限）
        if "cost_speed_limit" in calc:
            caster.speed_limit = max(0, caster.speed_limit - calc["cost_speed_limit"])
            caster.current_speed = min(caster.current_speed, caster.speed_limit)
            result["effects"].append({
                "type": "shrink_cost",
                "source": caster.name,
                "speed_limit_lost": calc["cost_speed_limit"],
                "new_speed_limit": caster.speed_limit,
                "new_action_count": caster.action_count
            })
        
        # 异变代价（获得X层异变，达50层失去意志变为怪物）
        if "cost_mutation" in calc:
            caster.mutation_stacks += calc["cost_mutation"]
            entry = {
                "type": "mutation_cost",
                "source": caster.name,
                "mutation_gained": calc["cost_mutation"],
                "total_stacks": caster.mutation_stacks,
            }
            if caster.is_mutated:
                entry["became_monster"] = True
                entry["note"] = "异变达到50层，该角色失去意志，变为怪物"
                caster.entity_type = EntityType.MONSTER.value
            result["effects"].append(entry)
        
        # 碎片代价
        if "cost_shards" in calc:
            self.state.shards = max(0, self.state.shards - calc["cost_shards"])
            result["effects"].append({
                "type": "shard_cost",
                "shards_spent": calc["cost_shards"],
                "shards_remaining": self.state.shards
            })
        
        # 法力获得
        if "mana_gain" in calc:
            caster.current_mana += calc["mana_gain"]
            result["effects"].append({
                "type": "mana_gain",
                "source": caster.name,
                "mana_gained": calc["mana_gain"]
            })
        
        # 状态效果添加
        if "duration" in calc and calc.get("duration") is not None:
            duration = calc["duration"] if calc["duration"] != 0 else -1
            effect_target = target if target else caster
            effect_target.add_status(StatusEffect(
                name=name,
                remaining_rounds=duration,
                value=calc.get("x", 0),
                source=caster.name
            ))
            result["effects"].append({
                "type": "status_added",
                "target": effect_target.name,
                "status": name,
                "duration": duration,
                "value": calc.get("x", 0)
            })
        
        return result
    
    def _action_use_spell(self, params: dict) -> dict:
        """
        发动法术

        规则：
        - 积木规则：每一步严格按对应道纹的原版公式结算
        - 中断规则：法力耗尽或中间流程失效则流程中断
        - 循环规则：终点结算可重新触发自身启动条件（法力充足时自动循环）
        - 自由控X：每个变量由施法者在合法范围内自由指定
        """
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}

        spell_name = params.get("spell_name", "")
        variables = params.get("variables", {}) or {}
        target_name = params.get("target", "")

        # 单变量法术允许直接传 x
        if "x" in params and "X" not in variables:
            variables["X"] = params["x"]

        # 必须已通过局外【学习】掌握
        owned = next((s for s in player.spells if s.name == spell_name), None)
        if not owned:
            return {
                "success": False,
                "error": f"未掌握法术【{spell_name}】，法术须通过局外【学习】行动掌握",
                "known_spells": [s.name for s in player.spells],
            }

        definition = get_spell(spell_name)
        if not definition:
            return {"success": False, "error": f"法术库中不存在【{spell_name}】"}

        # 积木规则校验
        check = validate_building_blocks(definition, set(player.dao_wen.keys()))
        if not check["valid"]:
            return {"success": False, "error": check["error"]}

        # 变量校验
        for var in definition.variables():
            if var not in variables:
                return {
                    "success": False,
                    "error": f"缺少变量 {var}",
                    "required_variables": definition.variables(),
                }
            if int(variables[var]) < 1:
                return {"success": False, "error": f"变量 {var} 必须≥1（自由控X规则）"}

        # 目标
        target = player
        if target_name:
            all_entities = self.state.get_all_player_side() + self.state.get_all_enemy_side()
            target = next((e for e in all_entities if e.name == target_name), player)

        flow_log: list[dict] = []
        interrupted_reason = None
        damage_dealt_total = 0
        loops_run = 0

        while True:
            loops_run += 1
            damage_this_pass = 0

            # 「不死不休」流程起点：失去X点生命
            if definition.self_life_loss_var:
                loss = int(variables[definition.self_life_loss_var])
                if player.current_hp <= loss:
                    interrupted_reason = "生命不足以支付流程起点的生命损失，流程中断"
                    break
                res = player.take_damage(loss, "代价")
                flow_log.append({
                    "step": "流程起点：失去生命",
                    "amount": loss,
                    "hp_after": res["hp_after"],
                })

            for idx, step in enumerate(definition.steps, 1):
                # 条件门
                if step.condition == "target_flying" and not target.has_status("飞行"):
                    flow_log.append({"step": idx, "daowen": step.daowen, "skipped": "目标未处于飞行"})
                    continue
                if step.condition == "no_damage_dealt" and damage_this_pass > 0:
                    flow_log.append({"step": idx, "daowen": step.daowen, "skipped": "本次流程已造成伤害"})
                    continue

                x = int(variables[step.var]) * step.coefficient

                # 该步的目标：治疗/防护类指向自己，其余指向敌方目标
                step_target = player if step.daowen in ("再生", "庇护", "慈悲", "增殖") else target

                try:
                    calc = DaoWenEngine.resolve(step.daowen, x, target=step_target, caster=player)
                except Exception as e:
                    interrupted_reason = f"第{idx}步【{step.daowen}】计算失败：{e}"
                    break

                # 中断规则：法力不足则流程中断
                cost = calc.get("cost", 0)
                if calc.get("cost_type") == "消耗" and cost > 0:
                    if player.current_mana < cost:
                        interrupted_reason = (
                            f"第{idx}步【{step.daowen}{x}】需要{cost}法力，"
                            f"当前{player.current_mana}，流程中断"
                        )
                        break
                    player.spend_mana(cost)

                # 中断规则：流血代价不足以支付则流程中断
                if calc.get("cost_hp", 0) > 0 and player.current_hp <= calc["cost_hp"]:
                    interrupted_reason = (
                        f"第{idx}步【{step.daowen}{x}】需流血{calc['cost_hp']}，"
                        f"当前生命{player.current_hp}，流程中断"
                    )
                    break

                execution = self._execute_daowen_effect(step.daowen, calc, player, step_target)

                for eff in execution["effects"]:
                    if eff.get("type") in ("damage", "aoe_damage", "multi_hit_damage"):
                        damage_this_pass += eff.get("actual_damage", 0)

                flow_log.append({
                    "step": idx,
                    "daowen": f"{step.daowen}{x}",
                    "var": step.var,
                    "calculation": calc,
                    "execution": execution,
                })

                if not player.is_alive:
                    interrupted_reason = "施法者[命零]，流程中断"
                    break

            damage_dealt_total += damage_this_pass

            if interrupted_reason:
                break
            if not definition.loops:
                break
            if loops_run >= MAX_SPELL_LOOPS:
                interrupted_reason = f"达到循环安全上限（{MAX_SPELL_LOOPS}次）"
                break

        return {
            "success": True,
            "action": f"发动法术【{spell_name}】",
            "spell": definition.to_dict(),
            "variables": variables,
            "loops_run": loops_run,
            "total_damage_dealt": damage_dealt_total,
            "flow": flow_log,
            "interrupted": interrupted_reason,
            "state": self.combat._get_combat_state(),
        }

    def _action_consume_item(self, params: dict) -> dict:
        """
        使用消耗品

        规则：消耗品（X/Y）可使用X次，每次使用后X-1，X归零后消耗。
             使用时不消耗出手。
        """
        item_name = params.get("item_name") or params.get("item", "")
        target_name = params.get("target", "")

        item = next(
            (c for c in self.state.consumables if c.name == item_name and not c.is_depleted),
            None
        )
        if not item:
            return {
                "success": False,
                "error": f"没有可用的消耗品【{item_name}】",
                "available": [
                    {"name": c.name, "uses": f"{c.current_uses}/{c.max_uses}"}
                    for c in self.state.consumables if not c.is_depleted
                ],
            }

        target = self.state.player
        if target_name:
            all_entities = self.state.get_all_player_side() + self.state.get_all_enemy_side()
            target = next((e for e in all_entities if e.name == target_name), self.state.player)

        remaining = item.use()
        depleted = item.is_depleted
        if depleted:
            self.state.consumables.remove(item)

        return {
            "success": True,
            "action": f"使用消耗品【{item_name}】",
            "result": {
                "item": item_name,
                "effect": item.effect,
                "target": target.name if target else None,
                "uses_remaining": remaining,
                "depleted": depleted,
                "costs_action": False,
            },
            "note": "使用消耗品不消耗出手。效果为文本描述，具体结算请按其效果调用对应行动。",
        }

    def _action_use_resonance(self, params: dict) -> dict:
        """使用残韵"""
        source = params.get("source_daowen", "")
        rtype = params.get("resonance_type", "")
        
        # 检查玩家是否拥有该类型残韵
        if rtype not in self.state.resonance or self.state.resonance[rtype] <= 0:
            return {"success": False, "error": f"没有可用的{rtype}残韵（当前：{self.state.resonance}）"}
        
        # 检查源道纹是否存在于当前持有者身上
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}
        
        caster_has = source in player.dao_wen
        
        result = ResonanceEngine.apply_resonance(
            source, rtype, 
            caster_has_daowen=caster_has,
            target_has_daowen=True,
            resonance_stock=self.state.resonance  # 传入残韵库存用于校验
        )
        
        if not result["success"]:
            return {"success": False, "error": result["error"]}
        
        # 消耗残韵
        self.state.resonance[rtype] -= 1
        
        # 如果是轮回者拥有的道纹，永久变化
        if caster_has and result.get("permanent_change"):
            target_name = result["target"]
            old_dw = player.dao_wen[source]
            # 创建新道纹实例
            new_dw = DaoWen(
                name=target_name,
                formula=f"{target_name}X",
                cost_type=old_dw.dao_wen.cost_type,
                cost_formula=old_dw.dao_wen.cost_formula,
                effect_formula=old_dw.dao_wen.effect_formula
            )
            player.dao_wen[target_name] = DaoWenInstance(dao_wen=new_dw)
            del player.dao_wen[source]
        
        return {
            "success": True,
            "action": f"残韵【{rtype}】{source} → {result['target']}",
            "result": result,
            "resonance_remaining": self.state.resonance
        }
    
    def _action_attack(self, params: dict) -> dict:
        """普通攻击"""
        attacker_name = params.get("attacker", self.state.player.name if self.state.player else "")
        target_selections = params.get("target_selections", [])
        
        # 查找攻击者
        all_entities = self.state.get_all_player_side() + self.state.get_all_enemy_side()
        attacker = next((e for e in all_entities if e.name == attacker_name), None)
        
        if not attacker:
            return {"success": False, "error": f"找不到实体: {attacker_name}"}
        
        targets = self.state.get_all_enemy_side() if attacker in self.state.get_all_player_side() else self.state.get_all_player_side()
        
        if not targets:
            return {"success": False, "error": "没有可用目标"}
        
        result = self.combat.calculate_round_attack(attacker, targets, target_selections)
        
        return {
            "success": True,
            "action": f"{attacker.name}发动一轮攻击",
            "result": result,
            "note": "每次攻击的目标是否闪避，需要逐次决策"
        }
    
    def _action_dodge_decision(self, params: dict) -> dict:
        """闪避决策"""
        target_name = params.get("target", "")
        dodge = params.get("dodge", False)
        attacker_name = params.get("attacker", "")
        is_must_hit = params.get("is_must_hit", False)
        
        all_entities = self.state.get_all_player_side() + self.state.get_all_enemy_side()
        target = next((e for e in all_entities if e.name == target_name), None)
        attacker = next((e for e in all_entities if e.name == attacker_name), None)
        
        if not target or not attacker:
            return {"success": False, "error": "目标或攻击者不存在"}
        
        result = self.combat.resolve_attack(attacker, target, is_must_hit=is_must_hit, dodge=dodge)
        
        return {
            "success": True,
            "action": f"闪避决策：{'闪避' if dodge else '承受'}",
            "result": result,
            "state": self.combat._get_combat_state()
        }
    
    def _action_declare_wit(self, params: dict) -> dict:
        """声明急中生智"""
        player = self.state.player
        target_name = params.get("target", "")
        
        all_entities = self.state.get_all_player_side() + self.state.get_all_enemy_side()
        target = next((e for e in all_entities if e.name == target_name), None)
        
        if not target:
            return {"success": False, "error": "目标不存在"}
        
        interrupt = self.combat.initiate_wit(player, target)
        self._pending_interrupts.append(interrupt)
        
        return {
            "success": True,
            "action": "声明急中生智",
            "interrupt": interrupt.to_dict(),
            "instruction": "需要DM裁定急中生智方案"
        }
    
    def _action_declare_escape(self, params: dict) -> dict:
        """声明逃跑"""
        escaper = self.state.player
        pursuers = self.state.get_all_enemy_side()
        
        interrupt = self.combat.initiate_escape(escaper, pursuers)
        self._pending_interrupts.append(interrupt)
        
        return {
            "success": True,
            "action": "声明逃跑",
            "interrupt": interrupt.to_dict(),
            "instruction": "需要DM裁定逃跑方案"
        }
    
    def _action_declare_evolution(self, params: dict) -> dict:
        """怪物进化"""
        monster_name = params.get("monster", "")
        monster = next((e for e in self.state.enemies if e.name == monster_name), None)
        
        if not monster:
            return {"success": False, "error": f"找不到怪物: {monster_name}"}
        
        difficulty = self.combat.check_monster_difficulty(monster)
        if not difficulty:
            return {"success": False, "error": f"{monster_name}未陷入困境，不能进化"}
        
        interrupt = self.combat.initiate_evolution(monster, difficulty)
        self._pending_interrupts.append(interrupt)
        
        return {
            "success": True,
            "action": f"{monster_name}进化",
            "interrupt": interrupt.to_dict(),
            "instruction": "需要DM裁定进化特性"
        }
    
    # ==================== 回合管理 ====================
    
    def _action_round_start(self, params: dict) -> dict:
        """回始"""
        result = self.combat.round_start()
        return {"success": True, "action": "回始", "result": result}
    
    def _action_round_end(self, params: dict) -> dict:
        """回终"""
        result = self.combat.round_end()
        
        # 检查怪物困境
        difficulties = []
        for monster in self.state.enemies:
            if monster.is_alive:
                diff = self.combat.check_monster_difficulty(monster)
                if diff:
                    difficulties.append(diff)
        
        return {
            "success": True,
            "action": "回终",
            "result": result,
            "monster_difficulties": difficulties,
            "note": "如果怪物陷入困境，AI应选择进化或逃跑"
        }
    
    def _action_battle_start(self, params: dict) -> dict:
        """
        战始

        流程：抽取出怪（数量=战斗场数，一阶副本-2，最低1，允许重复抽选同一怪物种族）
             →选择一种战斗背景→得知怪物面板及道纹
        """
        self.state.phase = "battle_start"
        self.state.current_battle += 1
        self.state.current_round = 0
        self.state.enemies.clear()

        region = self.state.current_region
        pool = get_monster_pool(region)
        need = monster_count_for_battle(self.state.current_battle, region_tier=1)

        # 已由调用方指定怪物名单（例如先前已完成随机抽取）
        picked = params.get("monsters")
        if picked:
            missing = [n for n in picked if not any(m.name == n for m in pool)]
            if missing:
                return {
                    "success": False,
                    "error": f"怪物不在【{region}】怪物池中: {missing}",
                    "pool": [m.name for m in pool],
                }
            self._spawn_monsters([next(m for m in pool if m.name == n) for n in picked])
            return {
                "success": True,
                "action": "战始",
                "battle_number": self.state.current_battle,
                "monster_count": len(picked),
                "enemies": [e.to_dict() for e in self.state.enemies],
                "instruction": "请选择一种战斗背景，随后进入第1回合。注意：怪物白板开局，道纹须其主动发动后方可生效。",
            }

        if not pool:
            return {
                "success": True,
                "action": "战始",
                "battle_number": self.state.current_battle,
                "monster_count": need,
                "warning": f"副本【{region}】暂无怪物池数据，请手动提供 monsters 参数",
            }

        # 需要随机数抽怪：一次抽一只，允许重复
        self.dice.create_pool("monster_draw", [m.name for m in pool])
        return {
            "success": True,
            "action": "战始",
            "battle_number": self.state.current_battle,
            "monster_count": need,
            "random_required": True,
            "pool_name": "monster_draw",
            "pool_range": f"1~{len(pool)}",
            "instruction": (
                f"本场需抽取{need}只怪物（允许重复）。"
                f"请玩家在 1~{len(pool)} 中依次给出{need}个数字，"
                f"通过 random_number 行动逐个提交。"
            ),
            "pool_options": [m.panel() for m in pool],
        }

    def _spawn_monsters(self, defs: list) -> None:
        """将怪物定义实例化到战场（重名自动加序号后缀）"""
        counts: dict[str, int] = {}
        for d in defs:
            counts[d.name] = counts.get(d.name, 0) + 1
        seen: dict[str, int] = {}
        for d in defs:
            if counts[d.name] > 1:
                seen[d.name] = seen.get(d.name, 0) + 1
                suffix = f"·{seen[d.name]}"
            else:
                suffix = ""
            self.state.enemies.append(d.to_entity(suffix))
    
    def _action_battle_end(self, params: dict) -> dict:
        """战终"""
        # 碎片奖励计算
        shard_reward = 0
        for monster in self.state.enemies:
            if not monster.is_alive:
                reward = math.ceil(monster.blood_limit * 0.02) + len(monster.dao_wen) * 5
                shard_reward += reward
        
        self.state.shards += shard_reward
        
        # 清除局内增益
        cooldowns_ticked = []
        if self.state.player:
            self.state.player.clear_shield()
            # 恢复速度到速限（闪避消耗的速度战终复原）
            self.state.player.current_speed = self.state.player.speed_limit
            
            # 冷却递减：[战终]后已完成战斗场数+1
            for dw_name, dw_inst in self.state.player.dao_wen.items():
                if dw_inst.cooldown_remaining > 0:
                    dw_inst.cooldown_remaining -= 1
                    cooldowns_ticked.append({
                        "daowen": dw_name,
                        "remaining": dw_inst.cooldown_remaining,
                        "ready": dw_inst.cooldown_remaining == 0,
                    })
        
        # 临时朋友消失
        self.state.temp_friends.clear()
        
        # 恢复精力
        self.state.energy = 3
        
        # 清空敌人
        self.state.enemies.clear()
        
        self.state.phase = "pre_battle"
        
        return {
            "success": True,
            "action": "战终",
            "result": {
                "shard_reward": shard_reward,
                "total_shards": self.state.shards,
                "energy_restored": 3,
                "cleared_temp_friends": True,
                "cooldowns_ticked": cooldowns_ticked,
            }
        }
    
    # ==================== DM裁定接口 ====================
    
    def submit_ruling(
        self,
        interrupt_type: str,
        ruling_text: str,
        ruling_data: dict = None,
        tags: list[str] = None
    ) -> dict:
        """
        DM提交裁定
        1. 移除待处理中断
        2. 保存到数据库
        3. 应用裁定效果
        """
        if not self._pending_interrupts:
            return {"success": False, "error": "没有待处理的中断"}
        
        interrupt = self._pending_interrupts.pop(0)
        
        # 创建裁定记录
        ruling = DMRuling(
            interrupt_type=interrupt_type,
            context=interrupt.context,
            ruling_text=ruling_text,
            ruling_data=ruling_data or {},
            tags=tags or []
        )
        
        ruling_id = self.rulings_db.save_ruling(ruling)
        
        return {
            "success": True,
            "action": "DM裁定",
            "ruling_id": ruling_id,
            "interrupt_type": interrupt_type,
            "ruling_text": ruling_text,
            "ruling_data": ruling_data,
            "note": "裁定已保存，下次类似场景将自动匹配"
        }
    
    def check_precedent(self, interrupt_type: str, context: dict) -> dict:
        """
        查询是否有先例裁定
        AI在触发特殊事件前可先查询
        """
        similar = self.rulings_db.find_similar(interrupt_type, context)
        
        return {
            "found": len(similar) > 0,
            "count": len(similar),
            "rulings": [r.to_dict() for r in similar],
            "instruction": "如果有匹配的先例，可以直接应用；否则需要DM新裁定"
        }
    
    # ==================== 随机数接口 ====================
    
    def _action_submit_random(self, params: dict) -> dict:
        """提交随机数"""
        pool_name = params.get("pool_name", "")
        number = params.get("number", 0)
        
        # 出怪抽取：允许重复抽选同一怪物种族，池不缩小
        if pool_name == "monster_draw":
            return self._submit_monster_draw(number)
        
        try:
            result = self.dice.resolve_pool(pool_name, number)
        except Exception as e:
            return {"success": False, "error": str(e)}
        
        out = {"success": True, "action": "随机数提交", "result": result}
        
        # 共鸣发现遗物：从遗物池中取出
        if pool_name == "relic_discover":
            relic = result["selected"]
            if isinstance(relic, Relic):
                self.state.relics.append(relic)
                self.state.relics_pool = [r for r in self.state.relics_pool if r.name != relic.name]
                out["result"] = {
                    "gained_relic": {"name": relic.name, "effect": relic.effect},
                    "relics_owned": [r.name for r in self.state.relics],
                }
        
        # 探索发现事件
        elif pool_name == "event_discover":
            ev = result["selected"]
            name = ev["name"] if isinstance(ev, dict) else str(ev)
            self.event_pool.mark_encountered(name)
            out["result"] = {"event": ev, "instruction": "请选择一个事件选项"}
        
        return out

    def _submit_monster_draw(self, number: int) -> dict:
        """逐个提交出怪随机数，抽满后自动实例化到战场"""
        region = self.state.current_region
        pool = get_monster_pool(region)
        need = monster_count_for_battle(self.state.current_battle, region_tier=1)
        
        if not 1 <= number <= len(pool):
            return {"success": False, "error": f"数字 {number} 超出范围 1~{len(pool)}"}
        
        picked = getattr(self, "_monster_draw_buffer", [])
        picked.append(pool[number - 1])
        self._monster_draw_buffer = picked
        
        self.dice.resolve_pool("monster_draw", number, keep=True)
        
        if len(picked) < need:
            return {
                "success": True,
                "action": "出怪抽取",
                "result": {
                    "drawn": [m.name for m in picked],
                    "remaining_draws": need - len(picked),
                    "instruction": f"还需抽取{need - len(picked)}只，请继续在 1~{len(pool)} 中给出数字",
                },
            }
        
        self._spawn_monsters(picked)
        self._monster_draw_buffer = []
        self.dice.clear_pool("monster_draw")
        
        return {
            "success": True,
            "action": "出怪完成",
            "result": {
                "monster_count": len(self.state.enemies),
                "enemies": [e.to_dict() for e in self.state.enemies],
                "panels": [
                    f"{e.name}（{e.attack_count}×{e.attack_power}/{e.blood_limit}，"
                    f"{'，'.join(f'{k}{v.x_value}' for k, v in e.dao_wen.items())}）"
                    for e in self.state.enemies
                ],
            },
            "instruction": "请选择一种战斗背景。注意：怪物白板开局，道纹须其主动发动后方可生效。",
        }
    
    def request_random(self, pool_name: str, options: list[Any]) -> dict:
        """
        请求随机数
        AI调用此方法创建随机池，然后必须向玩家索取数字
        """
        result = self.dice.create_pool(pool_name, options)
        return result
    
    # ==================== 存档系统 ====================
    
    def save_game(self, slot: str = "auto") -> dict:
        """保存游戏"""
        save_data = {
            "state": self.state.to_dict(),
            "action_history": self._action_history,
            "dice_history": self.dice.get_history(),
            "rulings": [r.to_dict() for r in self.rulings_db.get_all_rulings()],
            "timestamp": time.time()
        }
        
        filepath = os.path.join(self.save_dir, f"save_{slot}.json")
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(save_data, f, ensure_ascii=False, indent=2)
        
        return {"success": True, "filepath": filepath}
    
    def load_game(self, slot: str = "auto") -> dict:
        """加载游戏"""
        filepath = os.path.join(self.save_dir, f"save_{slot}.json")
        
        if not os.path.exists(filepath):
            return {"success": False, "error": f"存档不存在: {filepath}"}
        
        with open(filepath, 'r', encoding='utf-8') as f:
            save_data = json.load(f)
        
        # 恢复状态
        state_data = save_data["state"]
        self.state = GameState()
        self.state.game_id = state_data.get("game_id", "")
        self.state.phase = state_data.get("phase", "setup")
        self.state.current_round = state_data.get("current_round", 0)
        self.state.current_battle = state_data.get("current_battle", 0)
        self.state.current_region = state_data.get("current_region", "")
        self.state.energy = state_data.get("energy", 3)
        self.state.shards = state_data.get("shards", 20)
        self.state.attribute_points = state_data.get("attribute_points", 0)
        
        self._action_history = save_data.get("action_history", [])
        
        return {"success": True, "filepath": filepath}
    
    def get_action_history(self) -> list[dict]:
        """获取行动历史"""
        return self._action_history
    
    def get_rulings_history(self) -> list[dict]:
        """获取所有DM裁定"""
        return [r.to_dict() for r in self.rulings_db.get_all_rulings()]


import math  # 修复 battle_end 中使用 math.ceil
