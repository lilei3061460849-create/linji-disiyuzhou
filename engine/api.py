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
from .events import EventPool, parse_events
from .dm_rulings import DMRulingsDB, DMRuling, Interrupt


class GameEngine:
    """
    游戏引擎主类
    AI通过此接口与游戏交互，所有数值计算必须经过本引擎
    """
    
    def __init__(self, db_path: str = "data/dm_rulings.db", save_dir: str = "data/saves"):
        self.state = GameState()
        self.dice = DiceEngine()
        self.combat = CombatEngine(self.state, self.dice)
        self.rulings_db = DMRulingsDB(db_path)
        self.save_dir = save_dir
        os.makedirs(save_dir, exist_ok=True)
        
        # 规则校验器（延迟导入避免循环）
        self._validator = None
        self._rule_sync = None
        
        # 中断队列（等待DM裁定）
        self._pending_interrupts: list[Interrupt] = []
        
        # 事件系统
        _readme = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md")
        self.event_pool = EventPool(parse_events(_readme) if os.path.exists(_readme) else {})
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
            elif action_type == "resolve_event":
                result = self._action_resolve_event(params)
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
        """选择副本（同时初始化遗物池、开局发现一件遗物）"""
        region = params.get("region", "")
        valid = ["罪孽都市", "扭曲都市", "龙心谷"]
        if region not in valid:
            return {"success": False, "error": f"只能从{valid}中选择"}
        self.state.current_region = region
        self.state.phase = "pre_battle"
        self._init_relic_pool()
        # 开局发现一件遗物
        import random as _r
        if self.state.relics_pool:
            r = self.state.relics_pool.pop(_r.randrange(len(self.state.relics_pool)))
            self.state.relics.append(r)
            starter = r.name
        else:
            starter = None
        return {
            "success": True, "action": "选择副本",
            "result": {"region": region, "starter_relic": starter},
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
        """休整：产生恢复量并实际回复目标（默认自己，可指定朋友/员工）"""
        tier = params.get("tier", 1)
        heal_map = {1: (8, 0), 2: (24, 10), 3: (48, 25)}
        if tier not in heal_map:
            self.state.energy += 1
            return {"success": False, "error": "休整档位无效"}
        heal, cost = heal_map[tier]
        if self.state.shards < cost:
            self.state.energy += 1
            return {"success": False, "error": f"碎片不足，需要{cost}，当前{self.state.shards}"}
        self.state.shards -= cost
        # 选择目标
        target = self.state.player
        tname = params.get("target", "")
        for e in self.state.friends + self.state.employees:
            if e.name == tname:
                target = e; break
        h = target.heal(heal) if target else {"actual_heal":0}
        return {
            "success": True, "action": "休整",
            "result": {"heal_amount": heal, "shard_cost": cost, "target": target.name if target else None,
                       "actual_heal": h.get("actual_heal", 0), "hp_after": target.current_hp if target else 0,
                       "shards_remaining": self.state.shards},
        }
    
    def _pre_battle_xiuxing(self, params: dict) -> dict:
        """修行：获得属性点并立即分配（to=speed/mana；血限只能开局获得）"""
        tier = params.get("tier", 1)
        tier_map = {1: (1, 0), 2: (2, 15), 3: (3, 35), 4: (4, 65), 5: (5, 100), 6: (6, 150)}
        if tier not in tier_map:
            self.state.energy += 1
            return {"success": False, "error": "修行档位无效"}
        points, cost = tier_map[tier]
        if self.state.shards < cost:
            self.state.energy += 1
            return {"success": False, "error": f"碎片不足，需要{cost}"}
        self.state.shards -= cost
        player = self.state.player
        alloc = params.get("to", "speed")  # speed / mana
        gained = {"speed": 0, "mana": 0}
        for _ in range(points):
            if alloc == "mana":
                player.mana_limit += 2; gained["mana"] += 2
            else:
                player.speed_limit += 1; gained["speed"] += 1
        player.current_speed = player.speed_limit
        player.current_mana = player.mana_limit
        return {
            "success": True, "action": "修行",
            "result": {"points_gained": points, "shard_cost": cost, "allocated": alloc, "gained": gained,
                       "speed_limit": player.speed_limit, "mana_limit": player.mana_limit,
                       "action_count": player.action_count},
        }
    
    # 可学法术注册表（名 → 所需道纹）
    SPELL_REGISTRY = {
        "先发制人": ["杀伐"], "临界泄压": ["锐利"], "生生不息": ["再生"],
        "后发制人": ["庇护"], "以牙还牙": ["杀伐", "再生"], "借力打力": ["杀伐", "庇护"],
        "不死不休": ["血债"], "千刀万剐": ["血债", "再生"], "咎由自取": ["坠落", "杀伐", "血债"],
    }
    # 遗物池定义（12件，效果应用在第3阶段）
    RELIC_DEFS = [
        ("血誓戒", "[回始]首次主动支付流血代价时，获得等同于本次流血的格挡；若支付后生命≤30%，改为获得等量生命"),
        ("买路财", "战斗中可失去等同于怪物20%[血限]的[碎片]安全撤退"),
        ("同魂笔", "对[目标]发动残韵时，可另选一[目标]使其一种道纹受同种残韵影响"),
        ("回锋刀", "每失去1点速度后对[目标]造成3伤害；[回始]对[目标]造成3×([速限]-当前速度)伤害"),
        ("折速法印", "[战始]可疲惫X获得6X法力"),
        ("三相残韵盘", "[战始]消耗一种残韵；[战终]获得另两种残韵各1"),
        ("鲜血契约", "[战始]可流血X使首回合法力+X(X≤20%[血限])"),
        ("避风铃", "每次闪避后获得3格挡；当前速度归零时获得15格挡"),
        ("守夜灯", "[敌回始]获得[法限]50%法力，[敌回终]清空，每回合一次"),
        ("钱袋", "每当敌方[目标][命零]，额外获得其[战始][血限]2%的[碎片]"),
        ("卖身契", "[战始]指定一名[朋友]/[员工]；本场你支付的代价改由其承担"),
        ("无所求", "每当在事件中选拒绝类选项，永久获得1属性点"),
    ]

    def _init_relic_pool(self):
        if not self.state.relics_pool:
            self.state.relics_pool = [Relic(name=n, effect=e) for n, e in self.RELIC_DEFS]

    def _pre_battle_xuexi(self, params: dict) -> dict:
        """学习：实际添加道纹或法术到玩家"""
        sub = params.get("sub", "daowen")  # daowen / spell
        tier = params.get("tier", 1)
        cost = {1: 0, 2: 10, 3: 25}.get(tier, 0)
        if self.state.shards < cost:
            self.state.energy += 1
            return {"success": False, "error": f"碎片不足，需要{cost}"}
        name = params.get("name", "")
        player = self.state.player
        if not player:
            self.state.energy += 1
            return {"success": False, "error": "没有玩家"}
        if sub in ("daowen", "转化道纹"):
            if name not in DaoWenEngine.list_all():
                self.state.energy += 1
                return {"success": False, "error": f"未知道纹: {name}"}
            if name not in player.dao_wen:
                player.dao_wen[name] = DaoWenInstance(
                    DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
            self.state.shards -= cost
            return {"success": True, "action": "学习",
                    "result": {"learned": "daowen", "name": name, "shard_cost": cost,
                               "player_daowen": list(player.dao_wen.keys())}}
        elif sub == "spell":
            if name not in self.SPELL_REGISTRY:
                self.state.energy += 1
                return {"success": False, "error": f"未知法术: {name}"}
            req = self.SPELL_REGISTRY[name]
            player.spells.append(Spell(name=name, required_daowen=req, trigger_condition="", effect_flow=""))
            self.state.shards -= cost
            return {"success": True, "action": "学习",
                    "result": {"learned": "spell", "name": name, "required_daowen": req, "shard_cost": cost}}
    
    def _pre_battle_gongming(self, params: dict) -> dict:
        """共鸣：发现/自选遗物并实际加入"""
        import random as _r
        self._init_relic_pool()
        sub = params.get("sub", "discover")
        if not self.state.relics_pool:
            self.state.energy += 1
            return {"success": False, "error": "遗物池为空"}
        if sub == "choose":
            # 自选(额外消耗1精力+15碎片)
            name = params.get("name", "")
            idx = next((i for i,r in enumerate(self.state.relics_pool) if r.name == name), -1)
            if idx < 0:
                self.state.energy += 1
                return {"success": False, "error": f"遗物池无此遗物: {name}"}
            if self.state.shards < 15:
                self.state.energy += 1
                return {"success": False, "error": "碎片不足15"}
            self.state.shards -= 15
            self.state.energy -= 1
            relic = self.state.relics_pool.pop(idx)
            self.state.relics.append(relic)
            return {"success": True, "action": "共鸣(自选)", "result": {"gained_relic": relic.name, "effect": relic.effect}}
        # discover：从池中随机抽一件（随机数流程简化为直接抽取）
        relic = self.state.relics_pool.pop(_r.randrange(len(self.state.relics_pool)))
        self.state.relics.append(relic)
        return {"success": True, "action": "共鸣(发现)", "result": {"gained_relic": relic.name, "effect": relic.effect,
                                                                      "pool_remaining": len(self.state.relics_pool)}}
    
    def _pre_battle_tansuo(self, params: dict) -> dict:
        """探索：从当前事件池(通用+本副本专属)随机抽取一个事件"""
        import random as _r
        region = self.state.current_region
        name = self.event_pool.trigger(region, _r)
        if name is None:
            self.state.energy += 1
            return {"success": False, "error": "当前事件池已空（所有事件均已触发）"}
        ev = self.event_pool.events[name]
        return {
            "success": True, "action": "探索",
            "result": {"event": name, "region": ev["region"], "desc": ev["desc"],
                       "options": [{"id": o["id"], "text": o["text"]} for o in ev["options"]]},
            "instruction": f"遭遇【{name}】，请选择选项后调用 resolve_event"
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
            return {"success": False, "error": f"道纹{name}不可用（冷却/封印）"}
        
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
            # 飞行：非飞行者无法选中飞行目标
            if target is not player and not self.combat.is_targetable(player, target):
                return {"success": False, "error": f"{target.name}处于飞行，无法被选中"}
        
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
        
        return {
            "success": True,
            "action": f"发动道纹【{name}X={x}】",
            "calculation": calc,
            "execution": execution,
            "state": self.combat._get_combat_state()
        }
    
    def _execute_daowen_effect(self, name: str, calc: dict, caster: Entity, target: Entity) -> dict:
        """发动道纹：法力检查后委托combat.apply_daowen_effect"""
        return self.combat.apply_daowen_effect(name, calc, caster, target)

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

    def _action_consume_item(self, params: dict) -> dict:
        """使用消耗品（召唤物/雕塑/普通，遵守现有消耗品规则，使用不消耗出手）"""
        item_name = params.get("name", "")
        item = None
        for c in self.state.consumables:
            if c.name == item_name and not c.is_depleted:
                item = c
                break
        if item is None:
            return {"success": False, "error": f"找不到可用消耗品: {item_name}"}

        # 召唤物：召唤记录面板的怪物作为临时朋友
        if item.kind == "summon":
            summon = self.combat.summon_tamed_friend(item)
            return {
                "success": summon.get("success", True),
                "action": f"使用召唤物【{item_name}】",
                "result": summon,
                "state": self.combat._get_combat_state(),
            }

        # 雕塑：消耗1耐久造成15伤害或获得20格挡
        if item.kind == "sculpture":
            mode = params.get("mode", "damage")  # damage / shield
            target_name = params.get("target", "")
            target = None
            if mode == "damage":
                for e in self.state.get_all_enemy_side():
                    if e.name == target_name:
                        target = e
                        break
            result = self.combat.use_sculpture(item, target=target, mode=mode)
            return {
                "success": result.get("success", True),
                "action": f"使用雕塑【{item_name}】({mode})",
                "result": result,
                "state": self.combat._get_combat_state(),
            }

        # 普通消耗品：扣减耐久，效果按其描述由DM/AI结算
        remaining = item.use()
        return {
            "success": True,
            "action": f"使用消耗品【{item_name}】",
            "result": {
                "effect": item.effect,
                "uses_remaining": remaining,
                "is_depleted": item.is_depleted,
                "note": "消耗品效果按其描述结算，使用不消耗出手",
            },
            "state": self.combat._get_combat_state(),
        }

    def _action_use_spell(self, params: dict) -> dict:
        """查看/装配法术（反应型法术学会后在触发时点由引擎自动结算，无需手动发动）"""
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}
        spell_name = params.get("spell_name", "")
        spell = next((s for s in player.spells if s.name == spell_name), None)
        if spell is None:
            return {"success": False, "error": f"未掌握法术: {spell_name}"}
        flow = self.combat.SPELL_FLOWS.get(spell_name)
        armed = all(d in player.dao_wen and player.dao_wen[d].can_use() for d in spell.required_daowen)
        return {
            "success": True, "action": f"装配法术【{spell_name}】",
            "result": {"required_daowen": spell.required_daowen, "rank": spell.rank,
                       "trigger": flow["trigger"] if flow else spell.trigger_condition,
                       "steps": flow["steps"] if flow else spell.effect_flow,
                       "armed": armed,
                       "note": "反应型法术在触发时点(受伤害前/失血后/目标发动道纹前)由引擎自动结算"},
        }
    
    # ==================== 事件结算 ====================

    def _action_resolve_event(self, params: dict) -> dict:
        """结算事件选项：自动应用常见代价/收益，特殊效果交DM"""
        from .events import resolve_option_effect
        name = params.get("event", "")
        option_id = params.get("option_id")
        ev = self.event_pool.events.get(name)
        if not ev:
            return {"success": False, "error": f"未知事件: {name}"}
        opt = next((o for o in ev["options"] if o["id"] == option_id), None)
        if opt is None:
            return {"success": False, "error": f"事件{name}无选项{option_id}"}
        res = resolve_option_effect(opt["text"], self)
        self.event_pool.resolve(name)
        return {
            "success": True, "action": f"事件【{name}】选项{option_id}",
            "result": {"option": opt["text"], "applied": res["applied"], "instructions": res["instructions"],
                       "shards": self.state.shards,
                       "player_hp": self.state.player.current_hp if self.state.player else None},
            "note": "已自动结算可解析的代价/收益；instructions中的特殊效果需DM裁定"
        }

    # ==================== 回合管理 ====================
    
    def _action_round_start(self, params: dict) -> dict:
        """回始"""
        result = self.combat.round_start()
        return {"success": True, "action": "回始", "result": result}
    
    def _action_round_end(self, params: dict) -> dict:
        """回终"""
        result = self.combat.round_end()

        # 提取多路径胜利结果（已由 combat.round_end 结算）
        alt_paths = [e for e in result.get("effects", [])
                     if isinstance(e, dict) and e.get("type") in
                     ("taming", "sculpture", "proliferation", "debt_bind")]

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
            "victory_paths": alt_paths,
            "monster_difficulties": difficulties,
            "note": "多路径胜利（降服/雕塑/增生/还债）已结算；消耗品可在后续回合使用"
        }
    
    def _action_battle_start(self, params: dict) -> dict:
        """战始（结算战始遗物）"""
        self.state.phase = "battle_start"
        self.state.current_battle += 1
        self.state.current_round = 0
        self.combat.reset_monster_activation()
        relic_logs = self.combat.process_relics("battle_start")
        return {
            "success": True, "action": "战始",
            "battle_number": self.state.current_battle,
            "relic_logs": relic_logs,
            "instruction": "请抽取怪物并结算战始效果"
        }
    
    def _action_battle_end(self, params: dict) -> dict:
        """战终"""
        # 碎片奖励计算（被降服/雕塑/增生/还债移出的怪物不视为击杀，不产碎片）
        shard_reward = 0
        removed = []
        for monster in self.state.enemies:
            if (monster.is_subdued or monster.is_sculptured
                    or monster.is_proliferated or monster.is_debt_bound):
                removed.append({"name": monster.name,
                                "way": ("降服" if monster.is_subdued else
                                        "雕塑" if monster.is_sculptured else
                                        "增生" if monster.is_proliferated else "还债")})
                continue
            if not monster.is_alive:
                reward = math.ceil(monster.blood_limit * 0.02) + len(monster.dao_wen) * 5
                shard_reward += reward
        
        self.state.shards += shard_reward
        
        # 清除局内增益
        if self.state.player:
            self.state.player.clear_shield()
            # 恢复速度到速限（闪避消耗的速度战终复原）
            self.state.player.current_speed = self.state.player.speed_limit
        
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
                "removed_via_alt_path": removed,
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
        
        try:
            result = self.dice.resolve_pool(pool_name, number)
            return {
                "success": True,
                "action": "随机数提交",
                "result": result
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
    
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
