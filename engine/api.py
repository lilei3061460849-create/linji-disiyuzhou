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
import re
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
from .gamedata import REGION_EXCLUSIVE_DAOWEN
from .dm_rulings import DMRulingsDB, DMRuling, Interrupt


# 扭曲都市废墟设施工具库（README正文8件：名→(耐久, 效果文本逐字)）
TWISTED_TOOL_LIBRARY = {
    "反怪物电击枪": (3, "对一个[目标]造成25点伤害；若[目标]处于【飞行】，额外造成15点伤害并施加【坠落1】"),
    "备用血泵": (3, "使自身获得20点［回复］；若自身当前生命≤30%，额外获得30点格挡。"),
    "强光探照灯": (2, "使一个[目标]陷入【蒙蔽2】"),
    "高压水枪": (2, "清除全场所有敌方[目标]身上的所有“持续X”效果"),
    "储能电池": (3, "[回始]本回合额外获得12点法力。"),
    "急救箱": (2, "使自身获得[回复25]，并清除自身身上一种“持续X”的负面减益。"),
    "干扰仪": (2, "使全场所有敌方[目标]本回合无法发动自身道纹"),
    "高爆手雷": (2, "对一个[目标]造成15点伤害，并使其本回合攻击次数-1"),
}


class GameEngine:
    """
    游戏引擎主类
    AI通过此接口与游戏交互，所有数值计算必须经过本引擎
    """
    
    def __init__(self, db_path: str = "data/dm_rulings.db", save_dir: str = "data/saves",
                 rng_seed: Optional[int] = None, sealed_candidate_path: str = "data/sealed_candidate.json"):
        """
        rng_seed: 引擎自身随机源的种子。默认为None（真实随机，用于实际游戏）；
        测试/回归场景可传入固定整数，使全程随机结果可复现。
        sealed_candidate_path: "最终的冠冕"封存候选人的持久化文件路径。必须在不同GameEngine
        实例/不同轮回者playthrough之间共享同一路径，候选人数据才能被下一位到达者读取。
        """
        self.state = GameState()
        self.dice = DiceEngine(seed=rng_seed)
        self.combat = CombatEngine(self.state, self.dice)
        self.rulings_db = DMRulingsDB(db_path)
        self.save_dir = save_dir
        self.sealed_candidate_path = sealed_candidate_path
        os.makedirs(save_dir, exist_ok=True)
        
        # 规则校验器（延迟导入避免循环）
        self._validator = None
        self._rule_sync = None
        
        # 中断队列（等待DM裁定）
        self._pending_interrupts: list[Interrupt] = []
        # 体外心脏：记录[战始]翻倍前的血限基准，[战终]用于还原
        self._artifact_base_blood_limit = 0
        
        # 事件系统
        _readme = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md")
        self.event_pool = EventPool(parse_events(_readme) if os.path.exists(_readme) else {})
        # 怪物池（出怪系统）
        from .monsters import parse_monster_pool
        self.monster_pool = parse_monster_pool(_readme) if os.path.exists(_readme) else {}
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
        
        # 进化（怪物方决策）：仅困境怪物可发动，逃跑/进化二选一，每场限一次
        plight_options = self.combat.get_plight_evolution_options()
        actions.append({
            "type": "evolution",
            "action_id": "declare_evolution",
            "available": len(plight_options) > 0,
            "description": "【进化】发动原初X（代价：异变5X）：借用一种未持有的原始怪物道纹至战终，参数一次性给出 monster/daowen/x",
            "plight_monsters": plight_options,
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
            elif action_type == "retreat_via_toll":
                result = self._action_retreat_via_toll(params)
            elif action_type == "lianxin_in_battle":
                result = self._action_lianxin_in_battle(params)
            elif action_type == "declare_evolution":
                result = self._action_declare_evolution(params)
            elif action_type == "deploy_employee":
                result = self._action_deploy_employee(params)
            elif action_type == "dismiss_employee":
                result = self._action_dismiss_employee(params)
            elif action_type == "pay_employee_wage":
                result = self._action_pay_employee_wage(params)
            elif action_type == "choose_hired_daowen":
                result = self._action_choose_hired_daowen(params)
            elif action_type == "suppress_rebellion":
                result = self._action_suppress_rebellion(params)
            elif action_type == "resolve_rebellion_battle":
                result = self._action_resolve_rebellion_battle(params)
            elif action_type == "appease_rebellion":
                result = self._action_appease_rebellion(params)
            elif action_type == "negotiate_rebellion":
                result = self._action_negotiate_rebellion(params)
            elif action_type == "resolve_final_duel":
                result = self._action_resolve_final_duel(params)
            elif action_type == "choose_terminal_artifact":
                result = self._action_choose_terminal_artifact(params)
            elif action_type == "choose_first_embrace":
                result = self._action_choose_first_embrace(params)
            elif action_type == "use_black_card":
                result = self._action_use_black_card(params)
            elif action_type == "use_crime_vault":
                result = self._action_use_crime_vault(params)
            elif action_type == "fire_godfather_revolver":
                result = self._action_fire_godfather_revolver(params)
            elif action_type == "select_shared_dragon_heart":
                result = self._action_select_shared_dragon_heart(params)
            elif action_type == "declare_fuyuebei_toll":
                result = self._action_declare_fuyuebei_toll(params)
            elif action_type == "pay_for_dragon_nature":
                result = self._action_pay_for_dragon_nature(params)
            elif action_type == "unlock_dragon_trait":
                result = self._action_unlock_dragon_trait(params)
            elif action_type == "activate_dragon_body":
                result = self._action_activate_dragon_body(params)
            elif action_type == "devour_monster":
                result = self._action_devour_monster(params)
            elif action_type == "declare_tail_sacrifice":
                result = self._action_declare_tail_sacrifice(params)
            elif action_type == "use_dragon_wings":
                result = self._action_use_dragon_wings(params)
            elif action_type == "use_blood_wings":
                result = self._action_use_blood_wings(params)
            elif action_type == "enslave_as_chizu":
                result = self._action_enslave_as_chizu(params)
            elif action_type == "use_truth_eye":
                result = self._action_use_truth_eye(params)
            elif action_type == "blood_feast":
                result = self._action_blood_feast(params)
            elif action_type == "monster_phase":
                result = self._action_monster_phase(params)
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
        # 开局发现一件遗物（引擎自动生成随机数并结算，见 DiceEngine.auto_roll）
        if self.state.relics_pool:
            names = [r.name for r in self.state.relics_pool]
            roll = self.dice.auto_roll("setup_starter_relic", names, context="开局发现一件遗物")
            idx = roll["record"]["selected_index"]
            r = self.state.relics_pool.pop(idx)
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
        # 战斗中发动过的炼心：下一次局外行动额外多消耗1点精力(一次性结算，用完即清零)
        if self.state.pending_energy_penalty > 0:
            self.state.energy -= self.state.pending_energy_penalty
            self.state.pending_energy_penalty = 0
        
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
            "忘忧": self._pre_battle_wangyou,
            "献祭": self._pre_battle_sacrifice,
        }
        
        # 副本专属行动门禁（README：维修=扭曲都市、雇佣=罪孽都市、炼心=龙心谷专属）。
        # 缺少该校验会让任意副本都能用他人专属行动，统计与平衡数据将失真。
        REGION_EXCLUSIVE = {"维修": "扭曲都市", "雇佣": "罪孽都市", "炼心": "龙心谷"}
        need_region = REGION_EXCLUSIVE.get(action)
        if need_region and self.state.current_region != need_region:
            self.state.energy += 1
            return {"success": False,
                    "error": f"【{action}】是{need_region}专属行动，当前副本为"
                             f"{self.state.current_region or '未选择'}"}

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
        if "不朽之躯" in self.state.first_embrace_traits:
            self.state.energy += 1
            return {"success": False, "error": "不朽之躯：属性无法突破上限，无法修行"}
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
    # 三副本终音法器（死斗胜利后按current_region发放，见resolve_final_duel/choose_terminal_artifact）
    TERMINAL_ARTIFACTS = {
        "扭曲都市": [
            ("体外心脏", "[战始]，自身[血限]与当前生命临时翻倍"),
            ("羔羊之泪", "[战始]，场上所有角色与怪物立刻失去50%当前生命"),
            ("红头绳", "局外【行动】永久新增\"献祭\"（衰老3，换取精力+2）"),
            ("猩红尖牙", "获得该法器，并立刻强制触发特殊事件\"初拥之夜\""),
        ],
        "罪孽都市": [
            ("黑金名片", "[战始]，可使场上所有敌方[目标][血限]减半后，付出等量[碎片]（允许负债，负债≤50）"),
            ("罪业金库", "[回始]，可消耗X点[碎片]，获得2X点格挡（X≤2%当前碎片）"),
            ("教父左轮", "（耐久6/6，永不消耗，[战终]耐久回满）。对[目标]打出30%自身[血限]×使用次数的【必中】伤害"),
        ],
        "龙心谷": [
            ("共心环", "[战始]，选择自身拥有的一枚【××龙心】；本场战斗中，自身、[朋友]与[员工]均可消耗该龙心耐久，抵消同类型代价"),
            ("负岳碑", "当任意[朋友]或[员工]即将触发【撤退】时，可以流血20，抵消本次伤害并取消本次【撤退】"),
            ("真龙之心", "每消耗12X龙性，获得X种不同龙族遗物（6X衰老=2X枯竭=X萎缩=12X龙性）"),
        ],
    }

    # 初拥之夜9选1（1~8限选1次，9号可重复触发权）
    FIRST_EMBRACE_OPTIONS = {
        1: ("血族血脉", "[回终]若本回合造成伤害则获得等量[回复]，否则流血20"),
        2: ("不朽之躯", "当前[血限]减半；免疫衰老；[血限]无法增加；属性无法突破上限"),
        3: ("鲜血之翼", "代价：流血5X，发动【飞行X】回合"),
        4: ("血族尖牙", "代价：衰老20，使生命低于自身的一个[目标]转化为听命于你的赤族（诅咒：[回终]赤族流血20）"),
        5: ("真理眼", "代价：冷却2，使一个[目标]必须言明真理，否则无法开口"),
        6: ("寒冰法力", "对[目标]每累计施加10法力，使其本回合出手次数-1"),
        7: ("血影", "当自身被选为非必中判定的[目标]时，可流血10，取消本次判定"),
        8: ("血食", "可使一名听命于你的赤族[命零]，自身获得等同于该赤族当前生命的[回复]"),
        9: ("封存血脉", "保留触发权，随时再次触发初拥之夜"),
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
        ("忘忧香", "局外行动你可以选择\"忘忧\"（失忆1/2/3，获得30/55/80[碎片]）"),
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
            # 副本专属道纹门禁（README§三-4 + 用户裁定）：
            # 专属道纹不可直接学习，只能先通过残韵从**当前副本**的怪物身上转化获得；
            # 已持有该副本任一专属道纹后，才可学习该副本的其他专属道纹。
            # 其他副本的专属道纹一律不可获得。
            owner = None
            for _rg, _pool in REGION_EXCLUSIVE_DAOWEN.items():
                if name in _pool:
                    owner = _rg
                    break
            if owner is not None:
                if owner != self.state.current_region:
                    self.state.energy += 1
                    return {"success": False,
                            "error": f"【{name}】是{owner}的专属道纹，当前副本为"
                                     f"{self.state.current_region or '未选择'}，无法习得"}
                own_pool = REGION_EXCLUSIVE_DAOWEN[owner]
                if not (own_pool & set(player.dao_wen)):
                    self.state.energy += 1
                    return {"success": False,
                            "error": f"【{name}】是{owner}专属道纹：须先通过残韵从本副本怪物身上"
                                     f"转化获得一种专属道纹后，才能学习其他专属道纹"}
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
        # discover：引擎自动生成随机数并结算（DiceEngine.auto_roll）
        names = [r.name for r in self.state.relics_pool]
        roll = self.dice.auto_roll("resonance_relic_pool", names, context="共鸣(发现)")
        idx = roll["record"]["selected_index"]
        relic = self.state.relics_pool.pop(idx)
        self.state.relics.append(relic)
        return {"success": True, "action": "共鸣(发现)", "result": {"gained_relic": relic.name, "effect": relic.effect,
                                                                      "pool_remaining": len(self.state.relics_pool)}}
    

    def _pre_battle_tansuo(self, params: dict) -> dict:
        """探索：从当前事件池(通用+本副本专属)中，由引擎自动生成随机数抽取一个事件"""
        region = self.state.current_region
        pool = self.event_pool.build_pool(region)
        if not pool:
            self.state.energy += 1
            return {"success": False, "error": "当前事件池已空（所有事件均已触发）"}
        roll = self.dice.auto_roll("event_pool", pool, context=f"探索（{region}）")
        name = roll["selected"]
        self.event_pool.current = name
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
        """
        雇佣（罪孽都市专属）：diy一位微光者员工。
        20点基础预算：1点分配值=12血限；3点分配值=1攻击次数+2攻击力（捆绑购买，不可拆分）。
        新员工默认 is_deployed=False（待命不占场），需在战斗中调用 deploy_employee 消耗1出手派遣。
        受出战支援/黑名单机制全局约束（is_blacklisted 时禁止本动作）。
        """
        if self.state.is_blacklisted:
            self.state.energy += 1
            return {"success": False, "error": "已被列入失信黑名单，本次轮回无法再执行雇佣"}
        name = params.get("name", "")
        if not name:
            self.state.energy += 1
            return {"success": False, "error": "必须指定员工名称(name)"}
        if any(e.name == name for e in self.state.employees):
            self.state.energy += 1
            return {"success": False, "error": f"已存在同名员工: {name}，请更换名称"}
        blood_alloc = params.get("blood_alloc", 0)
        atk_bundles = params.get("atk_bundles", 0)
        if (not isinstance(blood_alloc, int) or not isinstance(atk_bundles, int)
                or blood_alloc < 0 or atk_bundles < 1 or blood_alloc + 3 * atk_bundles != 20):
            self.state.energy += 1
            return {"success": False,
                    "error": "预算分配非法：blood_alloc + 3×atk_bundles 必须恰好等于20，且 atk_bundles 至少为1"
                             "（1点分配值=12血限；每3点分配值捆绑购买1攻击次数+2攻击力，不可拆分；"
                             "禁止雇佣攻击次数为0的纯沙包盟友，出手预算=攻击次数/3也会因此永远为0而无法行动）"}
        blood_limit = blood_alloc * 12
        attack_count = atk_bundles
        attack_power = atk_bundles * 2
        emp = Entity(name=name, entity_type="员工", blood_limit=blood_limit, current_hp=blood_limit,
                     attack_count=attack_count, attack_power=attack_power, is_deployed=False)
        self.state.employees.append(emp)

        # diy后"发现并选择一种转化道纹"：从19个转化道纹中随机抽取3个未持有的(全部19个都未持有)，
        # 引擎自动生成随机数抽取(DiceEngine.auto_roll)，玩家从3个候选里选1个，调用 choose_hired_daowen 完成
        remaining = list(DaoWenEngine.TRANSFORMED_DAOWEN)
        discovered = []
        for i in range(3):
            roll = self.dice.auto_roll(f"hire_daowen_discovery_{name}_{i}", remaining,
                                        context=f"雇佣{name}-发现转化道纹")
            discovered.append(roll["selected"])
            remaining.remove(roll["selected"])
        self.state.pending_daowen_choices[name] = discovered

        return {
            "success": True, "action": "雇佣",
            "result": {"name": name, "blood_limit": blood_limit, "attack_count": attack_count,
                       "attack_power": attack_power, "is_deployed": False,
                       "discovered_daowen_choices": discovered,
                       "note": "已加入员工名单，默认待命不占场；战斗中需调用 deploy_employee 消耗1出手派遣出战后才会参战与计入战终工资结算。"
                                "请从 discovered_daowen_choices 中调用 choose_hired_daowen(name, daowen) 选择1种转化道纹"},
        }

    def _action_choose_hired_daowen(self, params: dict) -> dict:
        """雇佣的diy后置步骤：从发现的3个转化道纹候选中选择1种，赋予该员工"""
        name = params.get("name", "")
        daowen_name = params.get("daowen", "")
        choices = self.state.pending_daowen_choices.get(name)
        if choices is None:
            return {"success": False, "error": f"{name}没有待选择的转化道纹（未雇佣该员工/已选择过）"}
        if daowen_name not in choices:
            return {"success": False, "error": f"{daowen_name}不在候选范围内: {choices}"}
        emp = next((e for e in self.state.employees if e.name == name), None)
        if emp is None:
            return {"success": False, "error": f"找不到员工: {name}"}
        emp.dao_wen[daowen_name] = DaoWenInstance(
            DaoWen(name=daowen_name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
        del self.state.pending_daowen_choices[name]
        return {"success": True, "action": "选择转化道纹",
                "result": {"employee": name, "chosen": daowen_name,
                           "employee_daowen": list(emp.dao_wen.keys())}}

    # ==================== 员工叛变（三选一处理分支） ====================

    def _pending_rebellion_error(self, force: bool) -> Optional[dict]:
        """未强制触发时，必须存在[战终]检查命中的待处理叛变才能选择处理分支"""
        if force:
            return None
        if not self.state.rebellion_active:
            return {"success": False, "error": "当前没有待处理的员工叛变"}
        return None

    def _action_suppress_rebellion(self, params: dict) -> dict:
        """
        镇压：与所有叛变[员工]开启战斗。
        直接复用现有战斗体系——把当前state.employees的完整面板(含道纹)原样搬进state.enemies，
        当作本场"出怪"结果；随后按普通战斗流程(round_start/attack/use_daowen/monster_phase/
        round_end)进行，[员工]自身的攻击/道纹沿用其原有面板，不做任何强化或削弱。
        战斗结束后须调用 resolve_rebellion_battle(outcome=victory/defeat) 结算后果。
        """
        force = params.get("force", False)
        err = self._pending_rebellion_error(force)
        if err:
            return err
        if not self.state.employees:
            return {"success": False, "error": "没有员工可镇压"}
        rebels = list(self.state.employees)
        for e in rebels:
            e.is_deployed = True
            e.has_retreated = False
        self.state.employees = []
        self.state.enemies = rebels
        self.state.current_round = 0
        self.combat.reset_monster_activation()
        self.state.rebellion_in_progress = True
        self.state.rebellion_active = False
        return {
            "success": True, "action": "镇压叛变",
            "result": {
                "rebels": [e.name for e in rebels],
                "panels": [{"name": e.name, "attack_count": e.attack_count, "attack_power": e.attack_power,
                            "blood_limit": e.blood_limit, "current_hp": e.current_hp,
                            "dao_wen": {k: v.x_value for k, v in e.dao_wen.items()}} for e in rebels],
            },
            "instruction": "叛变员工已作为本场敌方(state.enemies)，按普通战斗流程推进；"
                           "战斗分出胜负后调用 resolve_rebellion_battle(outcome=victory/defeat) 结算",
        }

    def _action_resolve_rebellion_battle(self, params: dict) -> dict:
        """
        镇压战斗结算：
        胜利=肃清叛徒并保留财产(不额外掉碎片，也不产生击杀碎片奖励，叛徒清空)；
        失败或撤退=失去全部碎片，本场仍存活的叛徒携财逃跑(永久离开，不回到员工名单)。
        """
        if not self.state.rebellion_in_progress:
            return {"success": False, "error": "当前没有进行中的员工叛变战斗"}
        outcome = params.get("outcome", "")
        if outcome not in ("victory", "defeat"):
            return {"success": False, "error": "outcome必须是 victory 或 defeat（战斗失败与主动撤退统一按defeat结算）"}
        escaped = [e.name for e in self.state.enemies if e.is_alive]
        if outcome == "defeat":
            self.state.shards = 0
        self.state.enemies = []
        self.state.rebellion_in_progress = False
        return {
            "success": True, "action": "镇压结算",
            "result": {"outcome": outcome, "shards": self.state.shards,
                       "escaped_with_loot": escaped if outcome == "defeat" else []},
        }

    def _action_appease_rebellion(self, params: dict) -> dict:
        """让利：本次轮回所有[员工]每场工资+5，叛变平息"""
        force = params.get("force", False)
        err = self._pending_rebellion_error(force)
        if err:
            return err
        self.state.wage_bonus += 5
        self.state.rebellion_active = False
        return {"success": True, "action": "让利", "result": {"wage_bonus": self.state.wage_bonus}}

    def _action_negotiate_rebellion(self, params: dict) -> dict:
        """急中生智：给出合理的谈判方案破解叛乱，需要DM裁定方案是否成立"""
        force = params.get("force", False)
        err = self._pending_rebellion_error(force)
        if err:
            return err
        proposal = params.get("proposal", "")
        if not proposal:
            return {"success": False, "error": "必须给出谈判方案(proposal)，禁止空谈判"}
        interrupt = self.combat.initiate_negotiation(proposal)
        self._pending_interrupts.append(interrupt)
        return {
            "success": True, "action": "员工叛变·急中生智谈判",
            "interrupt": interrupt.to_dict(),
            "instruction": "需要DM裁定谈判方案是否合理；裁定后请调用 appease_rebellion(force=True) 平息叛乱"
                           "或改用 suppress_rebellion(force=True) 镇压",
        }

    def _pre_battle_lianxin(self, params: dict) -> dict:
        """炼心（龙心谷专属，局外版：已消耗1精力，见_action_pre_battle统一扣减）"""
        self.state.pending_lianxin = True
        return {
            "success": True,
            "action": "炼心",
            "result": {
                "pending_lianxin": True,
                "instruction": "直到你下一次实际支付数值为X的代价后，获得对应类型的【××龙心】(耐久X)"
            }
        }

    def _action_lianxin_in_battle(self, params: dict) -> dict:
        """炼心（战斗中版）：不消耗出手，改为下一次局外行动额外多消耗1点精力"""
        self.state.pending_lianxin = True
        self.state.pending_energy_penalty += 1
        return {
            "success": True,
            "action": "炼心(战斗中)",
            "result": {
                "pending_lianxin": True,
                "pending_energy_penalty": self.state.pending_energy_penalty,
                "instruction": "不消耗本回合出手；下一次局外行动将额外多消耗1点精力",
            }
        }

    def _pre_battle_wangyou(self, params: dict) -> dict:
        """忘忧（需持有遗物"忘忧香"）：失忆1/2/3(永久失去自身指定的X种道纹)，获得30/55/80碎片"""
        if not any(r.name == "忘忧香" for r in self.state.relics):
            self.state.energy += 1
            return {"success": False, "error": "没有忘忧香，无法执行忘忧"}
        tier = params.get("tier", 1)
        reward_map = {1: 30, 2: 55, 3: 80}
        if tier not in reward_map:
            self.state.energy += 1
            return {"success": False, "error": "忘忧档位必须是1/2/3"}
        player = self.state.player
        daowen_names = params.get("daowen_names", [])
        if not player or len(daowen_names) != tier or any(d not in player.dao_wen for d in daowen_names):
            self.state.energy += 1
            return {"success": False, "error": f"必须指定{tier}种自身已持有的道纹(daowen_names)永久失去"}
        for d in daowen_names:
            del player.dao_wen[d]
        reward = reward_map[tier]
        self.state.shards += reward
        return {"success": True, "action": "忘忧",
                "result": {"lost_daowen": daowen_names, "shards_gained": reward, "shards": self.state.shards}}

    def _pre_battle_sacrifice(self, params: dict) -> dict:
        """献祭（需持有终音法器"红头绳"）：衰老3，换取精力+2"""
        if "红头绳" not in self.state.artifacts_owned:
            self.state.energy += 1
            return {"success": False, "error": "没有红头绳，无法执行献祭"}
        player = self.state.player
        if not player:
            self.state.energy += 1
            return {"success": False, "error": "没有玩家"}
        player.blood_limit = max(1, player.blood_limit - 3)
        player.current_hp = min(player.current_hp, player.blood_limit)
        self.state.energy += 2
        return {"success": True, "action": "献祭",
                "result": {"blood_limit": player.blood_limit, "energy": self.state.energy}}

    # ==================== 员工经济系统（出战支援 / 工资 / 黑名单 / 解雇） ====================
    # 全局对所有[员工]生效；"还债"转化来的员工(is_debt_bound=True)独立走负债偿还轨道，不受此处约束。

    WAGE_CAP = 12  # 工资上限[碎片]

    def _blacklist_departure(self, reason: str):
        """记一次员工离队(拒付工资/解雇/死亡)，累计达3触发失信黑名单"""
        self.state.blacklist_level += 1
        if self.state.blacklist_level >= 3:
            self.state.is_blacklisted = True

    # ==================== 出手预算校验 ====================
    # 覆盖：attack/use_daowen/deploy_employee/declare_wit/declare_escape 消耗1出手；
    # consume_item(明确不消耗出手)、use_resonance(可任意时刻插队)不受此约束。

    def _consume_action_or_error(self, entity: "Entity") -> Optional[dict]:
        """校验entity本回合出手是否用尽；未用尽则消耗1次并返回None，用尽则返回错误dict。
        怪物走独立的[战始]固定攻击+道纹规则(run_monster_phase)，不受此速限/攻击次数推导的出手预算约束。"""
        if entity.entity_type == "怪物":
            return None
        if entity.actions_used_this_round >= entity.action_count:
            return {"success": False,
                    "error": f"{entity.name}本回合出手已用完({entity.actions_used_this_round}/{entity.action_count})"}
        entity.actions_used_this_round += 1
        return None

    def _apply_dragon_claw_growth(self, entity: "Entity") -> None:
        """龙族利爪（真龙之心遗物）：自身每完成一次行动后，攻击次数+1，攻击力+2。
        必须在该次行动本身已经用到(旧的)攻击次数之后才调用——尤其是【攻击】，
        它的一轮攻击命中次数=攻击次数，若在calculate_round_attack读取攻击次数之前就先增长，
        会导致本次攻击莫名要求多一个目标选择，这是过去出现过的真实bug。"""
        if entity is self.state.player and "龙族利爪" in self.state.dragon_traits:
            entity.attack_count += 1
            entity.attack_power += 2

    # ==================== 最终死斗·交替出手校验 ====================

    def _check_duel_turn_or_error(self, actor: "Entity") -> Optional[dict]:
        """死斗中(state.in_final_duel)，出手方必须与state.duel_turn一致，否则拒绝；非死斗时直接放行"""
        if not self.state.in_final_duel:
            return None
        actor_side = "player_side" if actor in self.state.get_all_player_side() else "opponent_side"
        if actor_side != self.state.duel_turn:
            return {"success": False,
                    "error": f"死斗须严格交替出手：当前轮到{self.state.duel_turn}，{actor.name}({actor_side})不能行动"}
        return None

    def _advance_duel_turn(self):
        """死斗中一次出手成功结算后，轮次交给对方"""
        if self.state.in_final_duel:
            self.state.duel_turn = "opponent_side" if self.state.duel_turn == "player_side" else "player_side"

    def _action_deploy_employee(self, params: dict) -> dict:
        """派遣[员工]出战：消耗玩家1出手（现已强制校验回合出手预算）。仅[员工]需要此步骤，[朋友]开局即直接参战。"""
        name = params.get("name", "")
        emp = next((e for e in self.state.employees if e.name == name and e.is_alive), None)
        if emp is None:
            return {"success": False, "error": f"找不到存活的员工: {name}"}
        if emp.is_debt_bound:
            return {"success": False, "error": f"{name}属于还债转化员工，已自动参战，无需派遣"}
        if emp.has_retreated:
            return {"success": False, "error": f"{name}本场已【撤退】，无法再次加入本场战斗"}
        if emp.is_deployed:
            return {"success": False, "error": f"{name}已在场，无需重复派遣"}
        if not self.state.player:
            return {"success": False, "error": "没有玩家"}
        budget_error = self._consume_action_or_error(self.state.player)
        if budget_error:
            return budget_error
        self._apply_dragon_claw_growth(self.state.player)
        emp.is_deployed = True

        # current_round 由 round_start() 递增，代表"当前正在进行的回合序号"(1-indexed)；
        # 若在round_start之前部署(current_round仍为0)，视为从第1回合起参战。
        emp.deployed_at_round = max(1, self.state.current_round)
        return {
            "success": True, "action": "派遣员工",
            "result": {"employee": name, "deployed_at_round": emp.deployed_at_round},
            "note": "本次派遣已消耗玩家1出手",
            "state": self.combat._get_combat_state(),
        }

    def _action_dismiss_employee(self, params: dict) -> dict:
        """解雇[员工]：自由行动，无代价，随时可用；直接移除，计入黑名单，不结算工资、不触发死亡结算"""
        name = params.get("name", "")
        emp = next((e for e in self.state.employees if e.name == name), None)
        if emp is None:
            return {"success": False, "error": f"找不到员工: {name}"}
        self.state.employees.remove(emp)
        self.state.pending_wage_decisions.pop(name, None)
        self._blacklist_departure("解雇")
        return {
            "success": True, "action": "解雇员工",
            "result": {"employee": name, "blacklist_level": self.state.blacklist_level,
                       "is_blacklisted": self.state.is_blacklisted},
        }

    def _compute_pending_wages(self):
        """战终首次结算：为每个"存活+已部署+非还债"的员工计算应付工资，写入 pending_wage_decisions。
        已经出现过的key(无论是否已决策)不会被重新计算，避免同一员工在同一场战斗内被反复计费。
        wage_bonus(员工叛变·让利)在封顶后叠加，不影响12碎片的封顶本身。"""
        for e in self.state.employees:
            if e.is_alive and e.is_deployed and not e.is_debt_bound and e.name not in self.state.pending_wage_decisions:
                # current_round 为1-indexed的"当前回合序号"，与 deployed_at_round 同口径，故+1为闭区间计数
                rounds_participated = max(0, self.state.current_round - e.deployed_at_round + 1)
                wage = min(self.WAGE_CAP, 2 * (1 + rounds_participated)) + self.state.wage_bonus
                self.state.pending_wage_decisions[e.name] = wage

    def _action_pay_employee_wage(self, params: dict) -> dict:
        """对战终待决的某个员工工资做出 pay/refuse 决策。拒付=战终触发，强制离队+计入黑名单。
        决策后不会立即从 pending_wage_decisions 中删除该key，而是标记为None(已决策)，
        防止同一场战斗多次调用 battle_end 时被重新计费；真正清空发生在 battle_end 成功结算之后。"""
        name = params.get("name", "")
        decision = params.get("decision", "")
        wage = self.state.pending_wage_decisions.get(name)
        if wage is None:
            return {"success": False, "error": f"{name}当前没有待决的工资结算（未部署/未存活/已决策过）"}
        if decision == "pay":
            if self.state.shards < wage:
                return {"success": False, "error": f"碎片不足，需要{wage}，当前{self.state.shards}，无法支付，请改为提交 refuse"}
            self.state.shards -= wage
            self.state.pending_wage_decisions[name] = None
            return {"success": True, "action": "支付工资",
                    "result": {"employee": name, "wage_paid": wage, "shards": self.state.shards}}
        elif decision == "refuse":
            self.state.pending_wage_decisions[name] = None
            emp = next((e for e in self.state.employees if e.name == name), None)
            if emp is not None:
                self.state.employees.remove(emp)
            self._blacklist_departure("拒付工资")
            return {"success": True, "action": "拒付工资",
                    "result": {"employee": name, "wage_refused": wage, "departed": True,
                               "blacklist_level": self.state.blacklist_level,
                               "is_blacklisted": self.state.is_blacklisted}}
        else:
            return {"success": False, "error": "decision必须是 pay 或 refuse"}

    # ==================== 战斗行动 ====================

    def _action_use_daowen(self, params: dict) -> dict:
        """
        发动道纹。
        params.actor 留空时=玩家自行发动道纹(法力制，行为与此前完全一致)。
        params.actor 指定为已部署[朋友]/[员工]时=听从轮回者指令代其发动：
        1.[朋友]/[员工]与怪物/微光者同属"不持有法力"的一方，发动道纹不支付法力，只消耗其出手(与怪物规则一致)；
          附带【代价】的道纹仍照常由该实体自身支付代价。
        2.必须指定一个不是其自身的目标(听从指令的道纹/攻击均需面向"其他非自身目标")。
        """
        actor_name = params.get("actor", "")
        is_command = bool(actor_name)
        if is_command:
            ally_pool = self.state.friends + [e for e in self.state.employees if e.is_deployed]
            actor = next((e for e in ally_pool if e.name == actor_name and e.is_alive and not e.has_retreated), None)
            if actor is None:
                return {"success": False, "error": f"找不到已参战的[朋友]/[员工]: {actor_name}"}
        else:
            actor = self.state.player
            if not actor:
                return {"success": False, "error": "没有玩家"}

        name = params.get("daowen_name", "")
        x = params.get("x", 1)
        target_name = params.get("target", "")

        if name not in actor.dao_wen:
            return {"success": False, "error": f"{actor.name}未持有道纹: {name}"}

        dw_instance = actor.dao_wen[name]

        if not dw_instance.can_use():
            return {"success": False, "error": f"道纹{name}不可用（冷却/封印）"}

        if x < 1:
            return {"success": False, "error": "X必须≥1"}

        if is_command and (not target_name or target_name == actor.name):
            return {"success": False, "error": "听从指令发动道纹必须指定一个非自身的目标"}

        # 查找目标
        target = actor  # 默认目标是自己（仅玩家自行发动时保留此默认，听从指令必须显式指定见上）
        if target_name:
            all_entities = self.state.get_all_player_side() + self.state.get_all_enemy_side()
            for e in all_entities:
                if e.name == target_name:
                    target = e
                    break
            else:
                return {"success": False, "error": f"找不到目标: {target_name}"}
            # 飞行：非飞行者无法选中飞行目标
            if target is not actor and not self.combat.is_targetable(actor, target):
                return {"success": False, "error": f"{target.name}处于飞行，无法被选中"}

        duel_error = self._check_duel_turn_or_error(actor)
        if duel_error:
            return duel_error

        # 调用道纹引擎计算
        try:
            calc = DaoWenEngine.resolve(name, x, target=target, caster=actor)
        except Exception as e:
            return {"success": False, "error": f"道纹计算失败: {str(e)}"}

        # 检查法力是否足够（代价道纹不消耗法力）
        # [朋友]/[员工]不持有法力（与怪物规则一致），发动道纹不支付法力，只消耗出手；仅玩家自身发动时走法力制
        cost = calc.get("cost", calc.get("cost_mutation", 0))
        if not is_command and calc.get("cost_type") == "消耗" and cost > 0:
            if not actor.spend_mana(cost):
                return {"success": False, "error": f"法力不足，需要{cost}，当前{actor.current_mana}"}
            # 寒冰法力（初拥之夜遗物）：持有者每消耗法力发动道纹，无论目标是谁(含自己)都累计"施加法力"，
            # 每满10点使该目标本回合出手次数-1(以叠加"无力"状态实现)
            if "寒冰法力" in self.state.first_embrace_traits and actor is self.state.player:
                before_tier = target.mana_inflicted_this_round // 10
                target.mana_inflicted_this_round += cost
                after_tier = target.mana_inflicted_this_round // 10
                new_stacks = after_tier - before_tier
                if new_stacks > 0:
                    target.add_status(StatusEffect(name="无力", value=new_stacks, remaining_rounds=1, source="寒冰法力"))

        budget_error = self._consume_action_or_error(actor)
        if budget_error:
            return budget_error
        self._apply_dragon_claw_growth(actor)

        # 执行效果
        dragon_heart_use = params.get("dragon_heart_use", 0)
        execution = self._execute_daowen_effect(name, calc, actor, target, dragon_heart_use)
        self._advance_duel_turn()


        return {
            "success": True,
            "action": f"发动道纹【{name}X={x}】" + (f"（{actor.name}听从指令发动）" if is_command else ""),
            "calculation": calc,
            "execution": execution,
            "state": self.combat._get_combat_state()
        }
    
    def _execute_daowen_effect(self, name: str, calc: dict, caster: Entity, target: Entity, dragon_heart_use: int = 0) -> dict:
        """发动道纹：法力检查后委托combat.apply_daowen_effect"""
        return self.combat.apply_daowen_effect(name, calc, caster, target, dragon_heart_use)

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
        
        second = params.get("second_target", "")
        second_source_daowen = params.get("second_source_daowen", "")
        second_log = None
        if second and any(r.name == "同魂笔" for r in self.state.relics):
            if not second_source_daowen:
                second_log = "同魂笔：必须指定second_source_daowen(第二个目标身上要受影响的道纹)，未生效"
            else:
                all_entities = self.state.get_all_player_side() + self.state.get_all_enemy_side()
                second_entity = next((e for e in all_entities if e.name == second), None)
                if second_entity is None:
                    second_log = f"同魂笔：找不到目标{second}，未生效"
                elif second_source_daowen not in second_entity.dao_wen:
                    second_log = f"同魂笔：{second}未持有{second_source_daowen}，未生效"
                else:
                    r2 = ResonanceEngine.apply_resonance(second_source_daowen, rtype,
                                                          caster_has_daowen=(second_source_daowen in player.dao_wen),
                                                          target_has_daowen=True)
                    if r2.get("success") and r2.get("caster_gets_daowen"):
                        new_name = r2["target"]
                        # 残韵作用于非轮回者拥有的道纹时：不改变其拥有的道纹，施法者永久获得变化后的道纹
                        if new_name not in player.dao_wen:
                            player.dao_wen[new_name] = DaoWenInstance(DaoWen(
                                name=new_name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
                            second_log = f"同魂笔：{second}的{second_source_daowen}受{rtype}影响，施法者永久获得{new_name}"
                        else:
                            second_log = f"同魂笔：施法者已持有{new_name}，不重复获得"
                    else:
                        second_log = f"同魂笔：{r2.get('error', '未知原因')}，未生效"
        return {
            "success": True,
            "action": f"残韵【{rtype}】{source} → {result['target']}",
            "result": result,
            "second_target_log": second_log,
            "resonance_remaining": self.state.resonance
        }
    
    def _action_attack(self, params: dict) -> dict:
        """普通攻击（消耗攻击者1出手；死斗中须遵守交替出手）"""
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

        duel_error = self._check_duel_turn_or_error(attacker)
        if duel_error:
            return duel_error

        budget_error = self._consume_action_or_error(attacker)
        if budget_error:
            return budget_error

        result = self.combat.calculate_round_attack(attacker, targets, target_selections)
        # 龙族利爪的增长必须放在calculate_round_attack读取(旧的)攻击次数之后，
        # 否则本次攻击会莫名要求多一个目标选择(过去出现过的真实bug)。
        self._apply_dragon_claw_growth(attacker)
        self._advance_duel_turn()

        return {
            "success": True,
            "action": f"{attacker.name}发动一轮攻击",
            "result": result,
            "note": "每次攻击的目标是否闪避，需要逐次决策"
        }
    
    def _action_dodge_decision(self, params: dict) -> dict:
        """闪避决策（blood_shadow=True时改用【血影】流血10取消判定，代替常规闪避）"""
        target_name = params.get("target", "")
        dodge = params.get("dodge", False)
        blood_shadow = params.get("blood_shadow", False)
        attacker_name = params.get("attacker", "")
        is_must_hit = params.get("is_must_hit", False)
        
        all_entities = self.state.get_all_player_side() + self.state.get_all_enemy_side()
        target = next((e for e in all_entities if e.name == target_name), None)
        attacker = next((e for e in all_entities if e.name == attacker_name), None)
        
        if not target or not attacker:
            return {"success": False, "error": "目标或攻击者不存在"}
        
        result = self.combat.resolve_attack(attacker, target, is_must_hit=is_must_hit, dodge=dodge, blood_shadow=blood_shadow)
        
        return {
            "success": True,
            "action": f"闪避决策：{'血影' if blood_shadow else ('闪避' if dodge else '承受')}",
            "result": result,
            "state": self.combat._get_combat_state()
        }
    
    def _action_declare_wit(self, params: dict) -> dict:
        """声明急中生智（消耗玩家1出手）"""
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}
        target_name = params.get("target", "")
        
        all_entities = self.state.get_all_player_side() + self.state.get_all_enemy_side()
        target = next((e for e in all_entities if e.name == target_name), None)
        
        if not target:
            return {"success": False, "error": "目标不存在"}

        budget_error = self._consume_action_or_error(player)
        if budget_error:
            return budget_error
        self._apply_dragon_claw_growth(player)

        interrupt = self.combat.initiate_wit(player, target)
        self._pending_interrupts.append(interrupt)
        
        return {
            "success": True,
            "action": "声明急中生智",
            "interrupt": interrupt.to_dict(),
            "instruction": "需要DM裁定急中生智方案"
        }
    
    def _action_declare_escape(self, params: dict) -> dict:
        """声明逃跑（消耗玩家1出手；死斗中禁止逃跑）"""
        escaper = self.state.player
        if not escaper:
            return {"success": False, "error": "没有玩家"}
        if self.state.in_final_duel:
            return {"success": False, "error": "最终死斗无法逃跑"}
        pursuers = self.state.get_all_enemy_side()

        budget_error = self._consume_action_or_error(escaper)
        if budget_error:
            return budget_error
        self._apply_dragon_claw_growth(escaper)

        interrupt = self.combat.initiate_escape(escaper, pursuers)
        self._pending_interrupts.append(interrupt)
        
        return {
            "success": True,
            "action": "声明逃跑",
            "interrupt": interrupt.to_dict(),
            "instruction": "需要DM裁定逃跑方案"
        }

    def _action_retreat_via_toll(self, params: dict) -> dict:
        """
        买路财：战斗中可失去等同于怪物20%[血限]的[碎片]安全撤退，无需【逃跑与追击】的DM裁定；
        碎片不足时按1[碎片]=2生命的比例用生命补足差额。要求持有遗物"买路财"。
        撤退后本场战斗直接结束(不产生击杀碎片奖励，因为怪物并未被击败)。
        """
        if not any(r.name == "买路财" for r in self.state.relics):
            return {"success": False, "error": "没有买路财，无法使用此撤退方式"}
        if self.state.in_final_duel:
            return {"success": False, "error": "最终死斗无法逃跑"}
        target_name = params.get("target", "")
        monster = next((e for e in self.state.enemies if e.name == target_name and e.is_alive), None)
        if monster is None:
            return {"success": False, "error": f"找不到存活的怪物: {target_name}"}

        cost = math.ceil(monster.blood_limit * 0.2)
        shard_pay = min(self.state.shards, cost)
        shortfall = cost - shard_pay
        life_cost = shortfall * 2
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}
        if life_cost > 0 and player.current_hp <= life_cost:
            return {"success": False,
                    "error": f"碎片不足({self.state.shards}/{cost})，且生命不足以补足差额(还需{life_cost}点生命)"}

        self.state.shards -= shard_pay
        if life_cost > 0:
            player.take_damage(life_cost, "代价")

        # 安全撤退：直接结束本场战斗，不结算击杀碎片奖励
        self.state.enemies.clear()
        self.state.temp_friends.clear()
        for emp in self.state.employees:
            if emp.is_alive and not emp.is_debt_bound:
                emp.is_deployed = False
                emp.deployed_at_round = 0
        for ally in self.state.friends + self.state.employees:
            ally.has_retreated = False
        self.state.energy = 3
        self.state.phase = "pre_battle"

        return {
            "success": True, "action": "买路财·安全撤退",
            "result": {"shard_paid": shard_pay, "life_paid": life_cost, "shards": self.state.shards,
                       "note": "本场战斗结束，未击败任何怪物，不产生击杀碎片奖励"},
        }

    def _action_declare_evolution(self, params: dict) -> dict:
        """怪物进化：发动【原初X】借用原始怪物道纹（引擎直接结算，无需DM中断）"""
        monster_name = params.get("monster", "")
        monster = next((e for e in self.state.enemies if e.name == monster_name), None)
        
        if not monster:
            return {"success": False, "error": f"找不到怪物: {monster_name}"}
        
        daowen_name = params.get("daowen", "")
        try:
            x = int(params.get("x", 1))
        except (TypeError, ValueError):
            return {"success": False, "error": "X必须为整数"}
        
        return self.combat.execute_evolution(monster, daowen_name, x)

    def _action_consume_item(self, params: dict) -> dict:
        """使用消耗品（雕塑/普通，遵守现有消耗品规则，使用不消耗出手）"""
        item_name = params.get("name", "")
        item = None
        for c in self.state.consumables:
            if c.name == item_name and not c.is_depleted:
                item = c
                break
        if item is None:
            return {"success": False, "error": f"找不到可用消耗品: {item_name}"}

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

        # 普通消耗品：扣减耐久；异变类效果走统一入口 add_mutation
        # （裁定⑧= A4全量：任何角色的任何异变来源同一入口，达50层即【崩解】命零；
        #  其余效果维持既有约定——按描述由DM/AI结算，使用不消耗出手）
        remaining = item.use()
        mutation_info = None
        mut_match = re.search(r"获得异变(\d+)", item.effect or "")
        if mut_match and self.state.player:
            layers = int(mut_match.group(1))
            mut = self.state.player.add_mutation(layers)
            mutation_info = {
                "mutation_added": mut["mutation_added"],
                "mutation_total": mut["mutation_total"],
                "collapsed": mut["collapsed"],
            }
            if mut["collapsed"]:
                mutation_info["note"] = (
                    f"异变达{mut['mutation_total']}层触发【崩解】，"
                    f"{self.state.player.name}直接命零，尸体变为怪物")
        return {
            "success": True,
            "action": f"使用消耗品【{item_name}】",
            "result": {
                "effect": item.effect,
                "uses_remaining": remaining,
                "is_depleted": item.is_depleted,
                "mutation": mutation_info,
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
        res = resolve_option_effect(opt["text"], self, event_name=name)
        reject_kw = ("拒绝", "无事发生", "观棋", "无视", "离开", "目送", "绕桥", "让炉", "避开", "捂住", "转身")
        if any(k in opt["text"] for k in reject_kw) and any(r.name == "无所求" for r in self.state.relics):
            self.state.player.speed_limit += 1
            self.state.player.current_speed = self.state.player.speed_limit
            res["applied"].append("无所求：+1速限")
        self.event_pool.resolve(name)
        # 裁定⑬：扭曲都市完成事件后附赠【发现】——从随机抽取的未持有工具库选项中选定1件
        bonus = None
        if self.state.current_region == "扭曲都市":
            owned = {c.name for c in self.state.consumables}
            pool = [n for n in TWISTED_TOOL_LIBRARY if n not in owned]
            if pool:
                import random as _r2
                candidates = _r2.sample(pool, min(3, len(pool)))
                want = params.get("bonus_pick")
                chosen = want if want in candidates else candidates[0]
                dur, txt = TWISTED_TOOL_LIBRARY[chosen]
                self.state.consumables.append(Consumable(
                    name=chosen, effect=txt, current_uses=dur, max_uses=dur))
                bonus = {"候选": candidates, "获得": chosen, "耐久": dur,
                         "来源": f"事件【{name}】完成后附赠【发现】",
                         "fallback": want is not None and want not in candidates}
        result_payload = {"option": opt["text"], "applied": res["applied"], "instructions": res["instructions"],
                          "shards": self.state.shards,
                          "player_hp": self.state.player.current_hp if self.state.player else None}
        if bonus:
            result_payload["附赠发现"] = bonus
        return {
            "success": True, "action": f"事件【{name}】选项{option_id}",
            "result": result_payload,
            "note": "已自动结算可解析的代价/收益；instructions中的特殊效果需DM裁定"
        }

    # ==================== 回合管理 ====================
    
    def _action_monster_phase(self, params: dict) -> dict:
        """怪物回合：引擎自主运行所有怪物的道纹出手+攻击出手"""
        dodge_policy = params.get("dodge_policy", "auto")
        results = self.combat.run_monster_phase(dodge_policy)
        # 怪物出手后若玩家死亡
        player_dead = (self.state.player is None) or (not self.state.player.is_alive)
        # README《六、战斗推演格式》要求"按出手次数依次列出"，禁止概括或合并结算。
        # run_monster_phase 已逐次产出每一击的明细，此处必须原样上抛，
        # 否则调用方只能拿到一个汇总计数，无法书写合规战报。
        return {
            "success": True, "action": "怪物回合",
            "result": {"attacks": len(results), "player_dead": player_dead,
                       "player_hp": self.state.player.current_hp if self.state.player else 0,
                       "details": results},
        }

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
                     ("sculpture", "proliferation", "debt_bind")]

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
            "note": "多路径胜利（雕塑/增生/还债）已结算；消耗品可在后续回合使用"
        }
    
    def _action_battle_start(self, params: dict) -> dict:
        """战始：抽取出怪(数量=战斗场数-3,最低1,允许重复抽选同一怪物种族)→结算战始遗物。
        战斗背景：文档"战斗背景：（名称与影响）"仅为战斗推演格式里的占位提示，
        正文未定义任何具体名称与机制效果，故本引擎不做机制化处理，留给叙事层自由发挥。"""
        from .monsters import compute_draw_count, make_monster_entity
        self.state.phase = "battle_start"
        self.state.current_battle += 1
        self.state.current_round = 0
        self.combat.reset_monster_activation()
        self.state.shared_dragon_heart_type = ""  # 共心环：每场需重新选定
        self.state.godfather_revolver_uses = 0
        gun = next((c for c in self.state.consumables if c.name == "教父左轮" and c.kind == "artifact_weapon"), None)
        if gun is not None:
            gun.current_uses = gun.max_uses  # 教父左轮：[战终]耐久回满(此处按下一场[战始]起效实现，效果等价)

        region = self.state.current_region
        pool = self.monster_pool.get(region, [])
        self.state.enemies.clear()
        drawn_names = []
        draw_count = 0
        if pool:
            draw_count = compute_draw_count(self.state.current_battle, is_tier_one=True)
            for i in range(draw_count):
                roll = self.dice.auto_roll(f"monster_draw_{self.state.current_battle}_{i}", pool,
                                            context=f"出怪(第{self.state.current_battle}场,第{i + 1}只)")
                monster_def = roll["selected"]
                self.state.enemies.append(make_monster_entity(monster_def))
                drawn_names.append(monster_def["name"])

        # 事件登记的"下一场额外出现的怪物"（如龙心谷"追求者·拿走口粮"）
        forced = list(self.state.forced_monsters_next_battle)
        self.state.forced_monsters_next_battle = []
        for fm in forced:
            self.state.enemies.append(make_monster_entity(fm))
            drawn_names.append(fm["name"] + "(额外出现)")

        relic_logs = self.combat.process_relics("battle_start")

        artifact_logs = []
        if "体外心脏" in self.state.artifacts_owned and self.state.player:
            player = self.state.player
            self._artifact_base_blood_limit = player.blood_limit
            player.blood_limit *= 2
            player.current_hp *= 2
            artifact_logs.append(f"体外心脏：血限与当前生命临时翻倍({self._artifact_base_blood_limit}→{player.blood_limit})")
        if "羔羊之泪" in self.state.artifacts_owned:
            for e in self.state.get_all_player_side() + self.state.get_all_enemy_side():
                loss = math.ceil(e.current_hp * 0.5)
                if e.entity_type in ("朋友", "员工"):
                    self.combat._apply_hostile_damage(e, loss)
                else:
                    e.take_damage(loss)
            artifact_logs.append("羔羊之泪：场上所有角色与怪物立刻失去50%当前生命")

        return {
            "success": True, "action": "战始",
            "battle_number": self.state.current_battle,
            "region": region,
            "draw_count": draw_count,
            "enemies": drawn_names,
            "relic_logs": relic_logs,
            "artifact_logs": artifact_logs,
            "instruction": "怪物已抽取完毕；请补充选择本场战斗背景(纯叙事，不影响数值)并结算其余[战始]效果",
        }
    
    # ==================== 最终的冠冕 / 第8场最终死斗 ====================
    # 完成第7场后自动触发：无封存候选→完整封存当前角色(含团队)，玩家以新轮回者重新开始；
    # 已有候选→双方(各自带队伍)进入死斗。整套流程无需玩家选择，[战终]内自动判定与转场。
    # 二阶及以上副本未实现：胜者同样被完整封存(而不是尝试接入不存在的下一阶内容)，
    # 成为下一位挑战者的候选人，效果上形成擂台循环。

    def _serialize_entity_full(self, e: Entity) -> dict:
        """完整序列化单个实体(用于封存候选人，需能无损还原道纹X值/法术/状态/面板等)"""
        return {
            "name": e.name, "entity_type": e.entity_type,
            "blood_limit": e.blood_limit, "current_hp": e.current_hp,
            "mana_limit": e.mana_limit, "current_mana": e.current_mana,
            "speed_limit": e.speed_limit, "current_speed": e.current_speed,
            "attack_count": e.attack_count, "attack_power": e.attack_power,
            "shield": e.shield, "is_flying": e.is_flying, "is_alive": e.is_alive,
            "shards": e.shards, "is_debt_bound": e.is_debt_bound,
            "dao_wen": {k: v.x_value for k, v in e.dao_wen.items()},
            "spells": [s.to_dict() for s in e.spells],
            "status_effects": [{"name": s.name, "value": s.value,
                                 "remaining_rounds": s.remaining_rounds, "source": s.source}
                                for s in e.status_effects],
        }

    def _deserialize_entity_full(self, d: dict) -> Entity:
        """按_serialize_entity_full的格式还原实体"""
        e = Entity(name=d["name"], entity_type=d["entity_type"],
                   blood_limit=d["blood_limit"], current_hp=d["current_hp"],
                   mana_limit=d["mana_limit"], current_mana=d["current_mana"],
                   speed_limit=d["speed_limit"], current_speed=d["current_speed"],
                   attack_count=d["attack_count"], attack_power=d["attack_power"],
                   shield=d.get("shield", 0), is_flying=d.get("is_flying", False),
                   is_alive=d.get("is_alive", True), shards=d.get("shards", 0),
                   is_debt_bound=d.get("is_debt_bound", False))
        for name, x in d.get("dao_wen", {}).items():
            e.dao_wen[name] = DaoWenInstance(
                DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""), x_value=x)
        for sp in d.get("spells", []):
            e.spells.append(Spell(name=sp["name"], required_daowen=sp["required_daowen"],
                                   trigger_condition=sp["trigger_condition"], effect_flow=sp["effect_flow"],
                                   rank=sp.get("rank", 1)))
        for st in d.get("status_effects", []):
            e.status_effects.append(StatusEffect(name=st["name"], value=st.get("value", 0),
                                                  remaining_rounds=st.get("remaining_rounds", -1),
                                                  source=st.get("source", "")))
        return e

    def _serialize_full_character(self) -> dict:
        """完整封存：玩家+队友(朋友/员工)+遗物+残韵+碎片+死者之书等级+属性点+终音法器/初拥之夜/真龙之心记录。
        已知限制：作为死斗对手载入时，目前只有player/friends/employees的战斗面板会真正参与战斗结算；
        artifacts/first_embrace_traits/dragon_traits等"元状态"只做记录延续，不会让对手在死斗中
        实际触发这些遗物的被动效果(这些trait判定目前统一挂在GameEngine.state上，只对"当前操作的这一方"
        生效，尚未做成可同时对双方独立生效的形式)。"""
        s = self.state
        return {
            "player": self._serialize_entity_full(s.player) if s.player else None,
            "friends": [self._serialize_entity_full(f) for f in s.friends],
            "employees": [self._serialize_entity_full(e) for e in s.employees],
            "relics": [r.to_dict() for r in s.relics],
            "resonance": dict(s.resonance),
            "shards": s.shards,
            "death_book_wisdom": list(s.death_book_wisdom),
            "attribute_points": s.attribute_points,
            "current_region": s.current_region,
            "artifacts_owned": list(s.artifacts_owned),
            "first_embrace_traits": list(s.first_embrace_traits),
            "pending_first_embrace": s.pending_first_embrace,
            "chizu_names": list(s.chizu_names),
            "dragon_nature": s.dragon_nature,
            "dragon_traits": list(s.dragon_traits),
            "has_sacrifice_action": s.has_sacrifice_action,
        }

    def _restore_side_from_snapshot(self, snapshot: dict) -> list[Entity]:
        """把封存快照的player+friends+employees还原为一个整体阵营(供死斗对手使用)。
        与挑战者一侧(玩家/朋友/员工)存在同名实体时强制改名，避免按名字查找目标时
        (全引擎的目标解析都是按name匹配)误伤/误判到己方同名单位——这不是死斗专属规则，
        是让"两个都叫轮回者的角色对战"这一必然场景下引擎仍能正确工作的必要前提。"""
        existing_names = {e.name for e in self.state.get_all_player_side()}
        side = []

        def _uniquify(entity: Entity):
            if entity.name in existing_names:
                entity.name = f"{entity.name}（对手）"
            existing_names.add(entity.name)
            side.append(entity)

        if snapshot.get("player"):
            opponent_player = self._deserialize_entity_full(snapshot["player"])
            opponent_player.entity_type = "轮回者"
            _uniquify(opponent_player)
        for f in snapshot.get("friends", []):
            _uniquify(self._deserialize_entity_full(f))
        for e in snapshot.get("employees", []):
            _uniquify(self._deserialize_entity_full(e))
        return side

    def _duel_priority_key(self, e: Entity) -> tuple:
        """先手顺序：速限→法限→血限→当前生命，数值越大越先手"""
        return (e.speed_limit, e.mana_limit, e.blood_limit, e.current_hp)

    def _trigger_final_crown(self) -> dict:
        """完成第7场后自动触发【最终的冠冕】"""
        if not os.path.exists(self.sealed_candidate_path):
            sealed_name = self.state.player.name if self.state.player else "轮回者"
            snapshot = self._serialize_full_character()
            os.makedirs(os.path.dirname(self.sealed_candidate_path) or ".", exist_ok=True)
            with open(self.sealed_candidate_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            self.state = GameState()
            self.combat.state = self.state
            return {
                "outcome": "sealed",
                "sealed_name": sealed_name,
                "instruction": f"无封存候选，{sealed_name}已连同队伍完整封存；"
                               "请调用 setup_attributes 开始新的轮回者",
            }
        with open(self.sealed_candidate_path, encoding="utf-8") as f:
            candidate_snapshot = json.load(f)
        os.remove(self.sealed_candidate_path)  # 候选人已被取用，槽位清空

        challenger_player = self.state.player
        opponent_side = self._restore_side_from_snapshot(candidate_snapshot)
        opponent_leader = next((e for e in opponent_side if e.entity_type == "轮回者"), None)

        self.state.enemies = opponent_side
        self.state.current_round = 0
        self.combat.reset_monster_activation()
        self.state.in_final_duel = True

        challenger_key = self._duel_priority_key(challenger_player)
        opponent_key = self._duel_priority_key(opponent_leader) if opponent_leader else (0, 0, 0, 0)
        first_mover = "player_side" if challenger_key >= opponent_key else "opponent_side"
        self.state.duel_turn = first_mover

        return {
            "outcome": "duel_start",
            "opponent_name": opponent_leader.name if opponent_leader else "未知对手",
            "opponent_side": [e.name for e in opponent_side],
            "first_mover": first_mover,
            "instruction": "第8场最终死斗开始：双方交替出手，残韵可任意时刻插队，无法逃跑；"
                           "请调用 resolve_final_duel(outcome=victory/defeat) 结算胜负",
        }

    def _action_resolve_final_duel(self, params: dict) -> dict:
        """
        死斗结算：
        胜利=先领取本次所属副本的终音法器(见choose_terminal_artifact)，再(连同队伍)被完整封存，
        成为下一位挑战者的候选人(二阶以上副本未实现，以封存代替"进入下一阶副本")；
        失败=当前挑战者战败，触发死之传承，本次轮回结束，需重新开始新的轮回者。
        """
        if not self.state.in_final_duel:
            return {"success": False, "error": "当前没有进行中的最终死斗"}
        outcome = params.get("outcome", "")
        if outcome not in ("victory", "defeat"):
            return {"success": False, "error": "outcome必须是 victory 或 defeat"}
        wisdom = params.get("death_book_wisdom", "")

        if outcome == "victory":
            self.state.in_final_duel = False
            region = self.state.current_region
            options = self.TERMINAL_ARTIFACTS.get(region, [])
            if not options:
                seal = self._finalize_victory_seal()
                return {"success": True, "action": "死斗结算",
                        "result": {"outcome": "victory", "seal": seal,
                                   "instruction": f"{region}没有已定义的终音法器，已直接完整封存"}}
            self.state.pending_terminal_region = region
            return {
                "success": True, "action": "死斗结算",
                "result": {
                    "outcome": "victory", "pending_terminal_choice": region,
                    "options": [{"id": i + 1, "name": n, "effect": e} for i, (n, e) in enumerate(options)],
                    "instruction": "请调用 choose_terminal_artifact(choice=序号) 领取终音法器后才会完整封存",
                }}
        else:
            legacy = wisdom[:20] if wisdom else ""
            self.state = GameState()
            self.combat.state = self.state
            return {"success": True, "action": "死斗结算",
                    "result": {"outcome": "defeat", "death_book_wisdom": legacy,
                               "instruction": "败者失去轮回者身份，触发死之传承；请调用 setup_attributes 开始新的轮回者"}}

    def _finalize_victory_seal(self) -> dict:
        """完整封存当前(胜利的)角色，写入候选人槽位，重置引擎状态等待新轮回者"""
        sealed_name = self.state.player.name if self.state.player else "轮回者"
        snapshot = self._serialize_full_character()
        os.makedirs(os.path.dirname(self.sealed_candidate_path) or ".", exist_ok=True)
        with open(self.sealed_candidate_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        self.state = GameState()
        self.combat.state = self.state
        return {"sealed_name": sealed_name,
                "instruction": f"{sealed_name}获胜并连同队伍完整封存，成为下一位挑战者的候选人；"
                               "请调用 setup_attributes 开始新的轮回者"}

    def _action_choose_terminal_artifact(self, params: dict) -> dict:
        """领取死斗胜利后的终音法器；若选择"猩红尖牙"则先触发初拥之夜，之后才真正完整封存"""
        region = self.state.pending_terminal_region
        if not region:
            return {"success": False, "error": "当前没有待领取的终音法器"}
        options = self.TERMINAL_ARTIFACTS.get(region, [])
        choice = params.get("choice", 0)
        if not isinstance(choice, int) or not (1 <= choice <= len(options)):
            return {"success": False, "error": f"choice必须是1~{len(options)}之间的整数"}
        name, effect = options[choice - 1]
        self.state.artifacts_owned.append(name)
        self.state.pending_terminal_region = ""

        if name == "红头绳":
            self.state.has_sacrifice_action = True
        if name == "教父左轮":
            self.state.consumables.append(Consumable(
                name="教父左轮", effect=effect, current_uses=6, max_uses=6, kind="artifact_weapon"))
        if name == "猩红尖牙":
            self.state.pending_first_embrace = True
            self.state.seal_pending_after_embrace = True
            return {"success": True, "action": "领取终音法器",
                    "result": {"artifact": name, "effect": effect, "first_embrace_pending": True,
                               "instruction": "已强制触发【初拥之夜】，请调用 choose_first_embrace(choice=1~9) 选择"}}

        seal = self._finalize_victory_seal()
        return {"success": True, "action": "领取终音法器",
                "result": {"artifact": name, "effect": effect, "seal": seal}}

    def _action_choose_first_embrace(self, params: dict) -> dict:
        """
        初拥之夜：9选1。1~8每项限选1次(除9外)，选择后[回复]30%[血限]。
        猩红尖牙触发的这一次选择完成后，若仍处于死斗胜利流程中，会紧接着完整封存角色。
        """
        if not self.state.pending_first_embrace:
            return {"success": False, "error": "当前没有待处理的初拥之夜"}
        choice = params.get("choice", 0)
        if choice not in self.FIRST_EMBRACE_OPTIONS:
            return {"success": False, "error": "choice必须是1~9之间的整数"}
        name, effect = self.FIRST_EMBRACE_OPTIONS[choice]
        if choice != 9 and name in self.state.first_embrace_traits:
            return {"success": False, "error": f"{name}已经选过，1~8每项限选1次"}

        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}

        if choice != 9:
            # 以【遗物】形式授予：遗物是唯一事实源，自动继承遗物通用规则
            self.state.grant_relic(name, effect, tag="血族")
            self.state.pending_first_embrace = False
            if name == "不朽之躯":
                player.blood_limit = math.ceil(player.blood_limit / 2)
                player.current_hp = min(player.current_hp, player.blood_limit)
        else:
            pass  # 封存血脉：不清空pending_first_embrace，保留触发权

        heal_amount = math.ceil(player.blood_limit * 0.3)
        heal_detail = player.heal(heal_amount)

        result = {"success": True, "action": "初拥之夜",
                  "result": {"choice": choice, "trait": name, "effect": effect,
                             "healed": heal_detail["actual_heal"]}}

        if self.state.seal_pending_after_embrace:
            self.state.seal_pending_after_embrace = False
            result["result"]["seal"] = self._finalize_victory_seal()
        return result

    # ==================== 初拥之夜：可主动发动的具体能力 ====================

    def _action_use_blood_wings(self, params: dict) -> dict:
        """鲜血之翼：代价流血5X，发动【飞行X】回合"""
        if "鲜血之翼" not in self.state.first_embrace_traits:
            return {"success": False, "error": "没有鲜血之翼"}
        player = self.state.player
        x = params.get("x", 0)
        if not player or not isinstance(x, int) or x < 1:
            return {"success": False, "error": "X必须是正整数"}
        self.combat._pay_bleed_cost(player, 5 * x)
        player.add_status(StatusEffect(name="飞行", value=x, remaining_rounds=x, source="鲜血之翼"))
        return {"success": True, "action": "鲜血之翼",
                "result": {"bled": 5 * x, "flying_rounds": x, "hp": player.current_hp}}

    def _action_enslave_as_chizu(self, params: dict) -> dict:
        """血族尖牙：代价衰老20，使生命低于自身的一个[目标]转化为听命于你的赤族"""
        if "血族尖牙" not in self.state.first_embrace_traits:
            return {"success": False, "error": "没有血族尖牙"}
        player = self.state.player
        target_name = params.get("target", "")
        target = next((e for e in self.state.enemies if e.name == target_name and e.is_alive), None)
        if not player or target is None:
            return {"success": False, "error": f"找不到存活的敌方目标: {target_name}"}
        if target.current_hp >= player.current_hp:
            return {"success": False, "error": "目标当前生命必须低于自身才能被转化"}
        player.blood_limit = max(1, player.blood_limit - 20)
        player.current_hp = min(player.current_hp, player.blood_limit)
        target.entity_type = "赤族"
        target.is_chizu_of = player.name
        target.is_deployed = True
        self.state.enemies.remove(target)
        self.state.friends.append(target)
        self.state.chizu_names.append(target.name)
        return {"success": True, "action": "血族尖牙",
                "result": {"enslaved": target.name, "chizu_names": list(self.state.chizu_names)}}

    def _action_use_truth_eye(self, params: dict) -> dict:
        """真理眼：代价冷却2(按战斗场数计)，使一个[目标]必须言明真理，否则无法开口；真伪由DM裁定"""
        if "真理眼" not in self.state.first_embrace_traits:
            return {"success": False, "error": "没有真理眼"}
        if self.state.truth_eye_cooldown > 0:
            return {"success": False, "error": f"真理眼冷却中，还需{self.state.truth_eye_cooldown}场战斗"}
        target_name = params.get("target", "")
        statement = params.get("statement", "")
        if not target_name or not statement:
            return {"success": False, "error": "必须指定target与statement(要求对方回答/陈述的内容)"}
        self.state.truth_eye_cooldown = 2
        interrupt = Interrupt(
            interrupt_type=InterruptType.CUSTOM,
            context={"ability": "真理眼", "target": target_name, "statement": statement},
            description=f"对{target_name}发动【真理眼】，要求其就以下内容言明真理，否则无法开口：\n{statement}\n"
                        f"请DM裁定对方的回应是否属实。",
            options=[{"id": "truth", "label": "属实", "description": "按DM裁定的真实内容生效"},
                     {"id": "silence", "label": "无法开口", "description": "对方拒绝或无法说出真话，只能沉默"}],
            state_snapshot=self.state.to_dict(),
        )
        self._pending_interrupts.append(interrupt)
        return {"success": True, "action": "真理眼",
                "result": {"interrupt": interrupt.to_dict(), "cooldown_battles": 2}}

    def _action_blood_feast(self, params: dict) -> dict:
        """血食：可使一名听命于你的赤族[命零]，自身获得等同于该赤族当前生命的[回复]"""
        if "血食" not in self.state.first_embrace_traits:
            return {"success": False, "error": "没有血食"}
        player = self.state.player
        name = params.get("chizu", "")
        chizu = next((e for e in self.state.friends if e.name == name and e.entity_type == "赤族" and e.is_alive), None)
        if not player or chizu is None:
            return {"success": False, "error": f"找不到存活的赤族: {name}"}
        amount = chizu.current_hp
        chizu.current_hp = 0
        chizu.is_alive = False
        heal_detail = player.heal(amount)
        return {"success": True, "action": "血食",
                "result": {"sacrificed": name, "healed": heal_detail["actual_heal"], "player_hp": player.current_hp}}

    # ==================== 终音法器：可主动发动的具体效果 ====================

    def _action_use_black_card(self, params: dict) -> dict:
        """黑金名片(罪孽都市终音)：[战始]可使所有敌方[目标][血限]减半，付出等量[碎片](允许负债，负债≤50)"""
        if "黑金名片" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有黑金名片"}
        enemies = self.state.get_all_enemy_side()
        if not enemies:
            return {"success": False, "error": "没有可生效的敌方目标"}
        total_cost = sum(math.ceil(e.blood_limit / 2) for e in enemies)
        if self.state.shards - total_cost < -50:
            return {"success": False, "error": f"负债不能超过50(需要付出{total_cost}，当前{self.state.shards})"}
        halved = []
        for e in enemies:
            half = math.ceil(e.blood_limit / 2)
            e.blood_limit -= half
            e.current_hp = min(e.current_hp, e.blood_limit)
            halved.append({"name": e.name, "new_blood_limit": e.blood_limit})
        self.state.shards -= total_cost
        return {"success": True, "action": "黑金名片",
                "result": {"cost": total_cost, "shards": self.state.shards, "halved": halved}}

    def _action_use_crime_vault(self, params: dict) -> dict:
        """罪业金库(罪孽都市终音)：[回始]可消耗X点[碎片](X≤2%当前碎片)，获得2X点格挡"""
        if "罪业金库" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有罪业金库"}
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}
        x = params.get("x", 0)
        cap = math.floor(self.state.shards * 0.02)
        if not isinstance(x, int) or x < 1 or x > cap:
            return {"success": False, "error": f"X必须是1~{cap}(当前碎片{self.state.shards}的2%)之间的整数"}
        self.state.shards -= x
        player.gain_shield(2 * x)
        return {"success": True, "action": "罪业金库",
                "result": {"spent": x, "shield_gained": 2 * x, "shards": self.state.shards, "shield": player.shield}}

    def _action_fire_godfather_revolver(self, params: dict) -> dict:
        """教父左轮(罪孽都市终音)：耐久6/6永不消耗(仅按场次回满)，对[目标]打出30%自身[血限]×本场使用次数的必中伤害"""
        if "教父左轮" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有教父左轮"}
        gun = next((c for c in self.state.consumables if c.name == "教父左轮" and c.kind == "artifact_weapon"), None)
        if gun is None or gun.current_uses <= 0:
            return {"success": False, "error": "教父左轮本场弹药已耗尽，需等待下一场[战终]回满"}
        player = self.state.player
        target_name = params.get("target", "")
        all_entities = self.state.get_all_player_side() + self.state.get_all_enemy_side()
        target = next((e for e in all_entities if e.name == target_name), None)
        if not player or target is None:
            return {"success": False, "error": f"找不到目标: {target_name}"}
        self.state.godfather_revolver_uses += 1
        gun.current_uses -= 1
        damage = math.ceil(player.blood_limit * 0.3) * self.state.godfather_revolver_uses
        dmg = self.combat._apply_hostile_damage(target, damage)
        return {"success": True, "action": "教父左轮",
                "result": {"target": target.name, "damage": damage,
                           "uses_this_battle": self.state.godfather_revolver_uses,
                           "ammo_remaining": gun.current_uses, **dmg}}

    def _action_select_shared_dragon_heart(self, params: dict) -> dict:
        """共心环(龙心谷终音)：[战始]选定自身拥有的一枚【××龙心】类型，本场自身/朋友/员工均可用它抵消同类型代价"""
        if "共心环" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有共心环"}
        heart_type = params.get("dragon_heart_type", "")
        heart = next((c for c in self.state.consumables
                      if c.kind == "dragon_heart" and c.dragon_heart_type == heart_type), None)
        if heart is None:
            return {"success": False, "error": f"自身没有持有{heart_type}龙心"}
        self.state.shared_dragon_heart_type = heart_type
        return {"success": True, "action": "共心环",
                "result": {"shared_dragon_heart_type": heart_type}}

    def _action_declare_fuyuebei_toll(self, params: dict) -> dict:
        """负岳碑(龙心谷终音)：预先声明"下次该[朋友]/[员工]即将撤退时，改为玩家流血20取消撤退与本次伤害" """
        if "负岳碑" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有负岳碑"}
        name = params.get("name", "")
        ally = next((e for e in self.state.friends + self.state.employees if e.name == name), None)
        if ally is None:
            return {"success": False, "error": f"找不到[朋友]/[员工]: {name}"}
        if name not in self.state.fuyuebei_declared:
            self.state.fuyuebei_declared.append(name)
        return {"success": True, "action": "负岳碑·预声明保护",
                "result": {"protected": name, "declared": list(self.state.fuyuebei_declared)}}

    # ==================== 真龙之心：龙性资源与8种龙族遗物 ====================

    DRAGON_NATURE_RATE = {"衰老": 2, "枯竭": 6, "萎缩": 12}  # 1点该类型代价 = N点龙性
    DRAGON_TRAITS = ["龙族血脉", "龙威", "龙族利爪", "龙息", "震岳龙躯", "吞骸龙胃", "断尾求生", "烬翼"]
    # 龙族项目以【遗物】形式授予，此表提供其效果文本（与 README/物品索引.md 一致）
    DRAGON_TRAIT_EFFECTS = {
        "龙族血脉": "对怪物造成伤害后，直接使其［命零］；对非怪物造成伤害翻倍",
        "龙威": "所有敌方必须优先选择自身为[目标]",
        "龙族利爪": "初始获得3点攻击次数与1点攻击力；自身每完成一次行动后，攻击次数+1，攻击力+2",
        "龙息": "所有敌方[目标]行动前，受到10×当前回合数的必中伤害",
        "震岳龙躯": "消耗6X点龙性，自身受到超出15点的所有伤害无效。持续X",
        "吞骸龙胃": "任意怪物［命零］后，可将其吞噬：自身获得[回复12]，并选择一枚【××龙心】，使其当前耐久+6",
        "断尾求生": "自身即将［命零］时，可销毁一件本场获得的其他龙族遗物，抵消本次使自身［命零］的伤害",
        "烬翼": "［回始］，可消耗3X点龙性，获得【飞行X】",
    }
    # 龙威("所有敌方必须优先选择自身为目标")：run_monster_phase目前本就总是让怪物攻击玩家本人
    # (从未实现"怪物选中朋友/员工"的目标分配逻辑)，故该遗物在当前引擎下恒定已满足、无需额外代码。

    def _action_pay_for_dragon_nature(self, params: dict) -> dict:
        """真龙之心：支付衰老/枯竭/萎缩代价换取龙性（6X衰老=2X枯竭=X萎缩=12X龙性）"""
        if "真龙之心" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有真龙之心"}
        cost_type = params.get("cost_type", "")
        x = params.get("x", 0)
        if cost_type not in self.DRAGON_NATURE_RATE or not isinstance(x, int) or x < 1:
            return {"success": False, "error": "cost_type必须是衰老/枯竭/萎缩之一，x必须是正整数"}
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}
        if cost_type == "衰老":
            player.blood_limit = max(1, player.blood_limit - x)
            player.current_hp = min(player.current_hp, player.blood_limit)
        elif cost_type == "枯竭":
            player.mana_limit = max(0, player.mana_limit - x)
            player.current_mana = min(player.current_mana, player.mana_limit)
        else:
            player.speed_limit = max(0, player.speed_limit - x)
            player.current_speed = min(player.current_speed, player.speed_limit)
        gained = x * self.DRAGON_NATURE_RATE[cost_type]
        self.state.dragon_nature += gained
        return {"success": True, "action": "真龙之心·换取龙性",
                "result": {"paid": f"{cost_type}{x}", "dragon_nature_gained": gained,
                           "dragon_nature": self.state.dragon_nature}}

    def _action_unlock_dragon_trait(self, params: dict) -> dict:
        """真龙之心：每消耗12龙性，获得1件未持有的龙族遗物"""
        if "真龙之心" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有真龙之心"}
        trait = params.get("trait", "")
        if trait not in self.DRAGON_TRAITS:
            return {"success": False, "error": f"trait必须是{self.DRAGON_TRAITS}之一"}
        if trait in self.state.dragon_traits:
            return {"success": False, "error": f"已持有{trait}，不能重复获得"}
        if self.state.dragon_nature < 12:
            return {"success": False, "error": f"龙性不足，需要12，当前{self.state.dragon_nature}"}
        self.state.dragon_nature -= 12
        self.state.grant_relic(trait, self.DRAGON_TRAIT_EFFECTS.get(trait, ""), tag="龙族")
        if trait == "龙族利爪":
            self.state.player.attack_count = 3
            self.state.player.attack_power = 1
        return {"success": True, "action": "真龙之心·获得龙族遗物",
                "result": {"trait": trait, "dragon_nature_remaining": self.state.dragon_nature,
                           "dragon_traits": list(self.state.dragon_traits)}}

    def _action_activate_dragon_body(self, params: dict) -> dict:
        """震岳龙躯：消耗6X点龙性，自身受到超出15点的所有伤害无效，持续X回合"""
        if "震岳龙躯" not in self.state.dragon_traits:
            return {"success": False, "error": "没有震岳龙躯"}
        x = params.get("x", 0)
        if not isinstance(x, int) or x < 1:
            return {"success": False, "error": "x必须是正整数"}
        cost = 6 * x
        if self.state.dragon_nature < cost:
            return {"success": False, "error": f"龙性不足，需要{cost}，当前{self.state.dragon_nature}"}
        self.state.dragon_nature -= cost
        self.state.dragon_body_shield_rounds = x
        return {"success": True, "action": "震岳龙躯",
                "result": {"rounds": x, "dragon_nature": self.state.dragon_nature}}

    def _action_devour_monster(self, params: dict) -> dict:
        """吞骸龙胃：任意怪物[命零]后可将其吞噬，自身获得回复12，并选择一枚龙心使其耐久+6"""
        if "吞骸龙胃" not in self.state.dragon_traits:
            return {"success": False, "error": "没有吞骸龙胃"}
        monster_name = params.get("monster", "")
        monster = next((e for e in self.state.enemies if e.name == monster_name), None)
        if monster is None or monster.is_alive:
            return {"success": False, "error": f"找不到已命零的怪物: {monster_name}"}
        player = self.state.player
        heal_detail = player.heal(12)
        heart_name = params.get("dragon_heart", "")
        heart = next((c for c in self.state.consumables if c.name == heart_name and c.kind == "dragon_heart"), None)
        if heart is not None:
            heart.current_uses += 6
            heart.max_uses += 6
        return {"success": True, "action": "吞骸龙胃",
                "result": {"healed": heal_detail["actual_heal"],
                           "dragon_heart_boosted": heart_name if heart is not None else None}}

    def _action_declare_tail_sacrifice(self, params: dict) -> dict:
        """断尾求生：预先声明本次即将命零时，愿意移除哪一种本场获得的其他龙族遗物来抵消伤害"""
        if "断尾求生" not in self.state.dragon_traits:
            return {"success": False, "error": "没有断尾求生"}
        trait = params.get("trait", "")
        others = [t for t in self.state.dragon_traits if t != "断尾求生"]
        if trait not in others:
            return {"success": False, "error": f"trait必须是本场已获得的其他龙族遗物之一: {others}"}
        self.state.dragon_tail_sacrifice_declared = trait
        return {"success": True, "action": "断尾求生·预声明",
                "result": {"declared_sacrifice": trait}}

    def _action_use_dragon_wings(self, params: dict) -> dict:
        """烬翼：[回始]可消耗3X点龙性，获得飞行X"""
        if "烬翼" not in self.state.dragon_traits:
            return {"success": False, "error": "没有烬翼"}
        x = params.get("x", 0)
        if not isinstance(x, int) or x < 1:
            return {"success": False, "error": "x必须是正整数"}
        cost = 3 * x
        if self.state.dragon_nature < cost:
            return {"success": False, "error": f"龙性不足，需要{cost}，当前{self.state.dragon_nature}"}
        self.state.dragon_nature -= cost
        self.state.player.add_status(StatusEffect(name="飞行", value=x, remaining_rounds=x, source="烬翼"))
        return {"success": True, "action": "烬翼",
                "result": {"flying_rounds": x, "dragon_nature": self.state.dragon_nature}}

    def _action_battle_end(self, params: dict) -> dict:
        """战终"""
        # 员工经济系统·工资结算门槛：先按"存活+已部署+非还债"员工计算工资写入待决列表；
        # 任何一名待决(值不为None，代表尚未pay/refuse)即阻塞后续战终结算。
        self._compute_pending_wages()
        still_pending = {k: v for k, v in self.state.pending_wage_decisions.items() if v is not None}
        if still_pending:
            return {
                "success": False,
                "error": "存在未决的员工工资结算，请先为以下员工逐个调用 pay_employee_wage(name, decision=pay/refuse)",
                "pending_wage_decisions": still_pending,
            }
        self.state.pending_wage_decisions = {}
        # 员工经济系统·死亡离队：计入黑名单并从名单移除，避免重复计数
        for emp in [e for e in self.state.employees if not e.is_alive and not e.is_debt_bound]:
            self.state.employees.remove(emp)
            self._blacklist_departure("死亡离队")

        relic_end = self.combat.process_relics("battle_end")
        # 碎片奖励计算（被雕塑/增生/还债/封印移出的怪物不视为击杀，不产碎片）
        # 奖励公式用的是[战始][血限]快照(battle_start_blood_limit)，不是当前血限(增殖等会改变当前血限)
        has_money_bag = any(r.name == "钱袋" for r in self.state.relics)
        shard_reward = 0
        removed = []
        for monster in self.state.enemies:
            if (monster.is_sculptured or monster.removed_without_kill
                    or monster.is_proliferated or monster.is_debt_bound):
                removed.append({"name": monster.name,
                                "way": ("雕塑" if monster.is_sculptured else
                                        "封印" if monster.removed_without_kill else
                                        "增生" if monster.is_proliferated else "还债")})
                continue
            if not monster.is_alive:
                reward = math.ceil(monster.battle_start_blood_limit * 0.02) + len(monster.dao_wen) * 5
                if has_money_bag:
                    reward += math.ceil(monster.battle_start_blood_limit * 0.02)  # 钱袋：额外+[战始][血限]2%
                shard_reward += reward
        
        self.state.shards += shard_reward
        
        # 清除局内增益
        if self.state.player:
            self.state.player.clear_shield()
            # 恢复速度到速限（闪避消耗的速度战终复原）
            self.state.player.current_speed = self.state.player.speed_limit
            # 体外心脏：临时翻倍的血限[战终]还原为基准值，当前生命同步封顶
            if "体外心脏" in self.state.artifacts_owned and self._artifact_base_blood_limit > 0:
                self.state.player.blood_limit = self._artifact_base_blood_limit
                self.state.player.current_hp = min(self.state.player.current_hp, self.state.player.blood_limit)
                self._artifact_base_blood_limit = 0

        # 真理眼冷却：每场[战终]-1
        if self.state.truth_eye_cooldown > 0:
            self.state.truth_eye_cooldown -= 1

        # 道纹【冷却X】：README「[战终]后已完成战斗场数+1，达到Y时才能再次使用」。
        # 对所有持有道纹的角色统一递减，归零即恢复可用。
        for _ent in ([self.state.player] if self.state.player else []) \
                + self.state.friends + self.state.employees:
            for _inst in _ent.dao_wen.values():
                if _inst.cooldown_remaining > 0:
                    _inst.cooldown_remaining -= 1
        
        # 临时朋友消失
        self.state.temp_friends.clear()

        # 出战支援：每场战斗单独部署，战终后存活员工回到"待命"状态，下一场需重新派遣
        for emp in self.state.employees:
            if emp.is_alive and not emp.is_debt_bound:
                emp.is_deployed = False
                emp.deployed_at_round = 0

        # 撤退：仅"无法再次加入本场战斗"，战终后重置，可正常参加下一场
        for ally in self.state.friends + self.state.employees + self.state.temp_friends:
            ally.has_retreated = False

        # 恢复精力
        self.state.energy = 3
        
        # 清空敌人
        self.state.enemies.clear()
        
        self.state.phase = "pre_battle"

        rebellion_check = self.combat.check_employee_rebellion()
        if rebellion_check.get("rebellion"):
            self.state.rebellion_active = True

        # 捕获战终结果数值，因【最终的冠冕】可能在下面把self.state整个替换为新轮回者的空白状态
        battle_end_result = {
            "shard_reward": shard_reward,
            "total_shards": self.state.shards,
            "energy_restored": 3,
            "cleared_temp_friends": True,
            "removed_via_alt_path": removed,
            "relic_end_logs": relic_end,
            "employee_rebellion": rebellion_check,
            "player_dead": (not self.state.player.is_alive) if self.state.player else False,
        }

        # 最终的冠冕：完成第7场后自动触发，不消耗精力、不经玩家选择
        if self.state.current_battle == 7:
            battle_end_result["final_crown"] = self._trigger_final_crown()

        return {
            "success": True,
            "action": "战终",
            "result": battle_end_result,
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
