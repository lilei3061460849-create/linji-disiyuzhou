"""
游戏引擎 API - AI的唯一交互入口
核心原则：
1. AI通过此API获取状态、做出决策、触发行动
2. 所有数值计算由引擎完成，AI禁止自行计算
3. 程序无法判定时返回Interrupt，等待DM裁定
4. DM裁定存入数据库，下次类似情况自动匹配
5. 未实装的机制一律如实标记 unavailable，绝不允许返回看似成功的空壳结果
"""
from __future__ import annotations
import json
import math
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
from .gamedata import (
    MONSTER_POOLS, RELIC_POOL, SPELL_LIBRARY, VALID_SPELL_TRIGGERS,
    REGION_BATTLE_COUNT, REGION_TIERS, monster_spawn_count,
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

        # 规则校验器（延迟导入避免循环）
        self._validator = None
        self._rule_sync = None

        # 从 gamedata 装载遗物池（衍生物，事实源为 README.md）
        self.state.relics_pool = [
            Relic(
                name=r["name"],
                effect=r["effect"],
                tags=["implemented"] if r.get("implemented") else ["unimplemented"],
            )
            for r in RELIC_POOL
        ]

        # 中断队列（等待DM裁定）
        self._pending_interrupts: list[Interrupt] = []

        # 待解决的随机请求（随机数规则：引擎给范围，玩家给数字）
        # {"purpose": "spawn_monster"|"discover_relic", "pool_name": str, "meta": {...}}
        self._pending_random: Optional[dict] = None

        # 死之传承/异变化是否已触发
        self._death_handled = False

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
            "pending_random": self._pending_random,
            "last_result": self._last_result,
            "available_actions": self.get_available_actions(),
        }

    def get_available_actions(self) -> dict:
        """
        获取当前可用行动
        根据游戏阶段返回不同的行动列表
        原则：未实装的机制如实标记 unavailable，不伪装成可用
        """
        phase = self.state.phase

        if phase == "setup":
            return self._get_setup_actions()
        elif phase == "pre_battle":
            return self._get_pre_battle_actions()
        elif phase == "battle_start":
            return self._get_battle_start_actions()
        elif phase in ("in_combat", "dead_duel"):
            return self._get_combat_actions()
        elif phase == "battle_end":
            return self._get_battle_end_actions()
        else:
            return {"actions": [], "note": "游戏已结束"}

    def _get_setup_actions(self) -> dict:
        return {
            "phase": "开局",
            "required_actions": [
                "setup_attributes：分配25点初始属性点（1点=6血限=1速限=2法限）",
                "setup_choose_daowen：在【杀伐】【锐利】中选择一种作为初始道纹",
                "setup_choose_resonance：自选一种残韵（转换/反转/曲解）",
                "setup_choose_region：选择一个一阶副本（罪孽都市/扭曲都市/龙心谷）",
            ],
            "auto_actions": [
                "获得20碎片（setup_attributes 时自动发放）",
                "discover_relic_setup：发现一件遗物（需要随机数 1~遗物池数量）",
            ],
            "attribute_points_remaining": 25 if not self.state.player else 0,
        }

    def _get_pre_battle_actions(self) -> dict:
        return {
            "phase": "局外阶段",
            "energy": self.state.energy,
            "actions": [
                {"id": "领悟", "cost_energy": 1, "available": True, "description": "选择获得1种残韵"},
                {"id": "休整", "cost_energy": 1, "available": True, "description": "产生8/24(10碎片)/48(25碎片)恢复量，可自由分配给自己或队友"},
                {"id": "修行", "cost_energy": 1, "available": True, "description": "获得1/2(15)/3(35)/4(65)/5(100)/6(150)点属性点"},
                {"id": "学习", "cost_energy": 1, "available": True, "description": "学会1/2(10)/3(25)种法术，或习得1/2(10)种转化道纹（须为已持有道纹的相邻变化）"},
                {"id": "共鸣", "cost_energy": 2, "available": bool(self.state.relics_pool),
                 "description": "精力再次-1，发现/自选(15碎片)一件遗物"},
                {"id": "探索", "cost_energy": 1, "available": False,
                 "unavailable_reason": "事件系统未实装：事件池、事件选择与结算尚未实现，不能假装探索成功"},
                {"id": "spend_attribute_points", "cost_energy": 0,
                 "available": self.state.attribute_points > 0,
                 "description": "消耗属性点：1点=1速限 或 1点=2法限（血限无法通过修行提升）"},
            ],
            "region_actions": self._get_region_actions(),
            "note": "精力耗尽后调用 battle_start 进入战斗",
        }

    def _get_region_actions(self) -> list[dict]:
        """副本专属行动——未实装的如实说明"""
        region = self.state.current_region
        if region == "扭曲都市":
            return [{"id": "维修", "available": False,
                     "unavailable_reason": "维修作用于消耗品；消耗品获取依赖事件系统，暂未实装"}]
        elif region == "罪孽都市":
            return [{"id": "雇佣", "available": False,
                     "unavailable_reason": "雇佣/员工工资体系暂未实装"}]
        elif region == "龙心谷":
            return [{"id": "炼心", "available": False,
                     "unavailable_reason": "龙心（代价抵消消耗品）体系暂未实装"}]
        return []

    def _get_battle_start_actions(self) -> dict:
        return {
            "phase": "战始",
            "required": [
                "抽取怪物（random_number 行动逐只抽选）",
                "选择战斗背景",
                "结算战始效果",
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
                    "max_x_by_mana": max_x,
                    "description": f"发动【{name}X】（代价类型决定X上限）",
                    "requires_target": name not in CombatEngine.UNTARGETED_DAOWEN,
                })
            else:
                actions.append({
                    "type": "daowen",
                    "id": name,
                    "available": False,
                    "reason": dw_instance.reason_unusable(),
                })

        # 法术行动（已通过的法术，按其触发时点在对应时机发动）
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

        # 残韵（可任意时刻插队）
        available_resonance = [
            {"type": name, "count": count}
            for name, count in self.state.resonance.items() if count > 0
        ]
        if available_resonance:
            actions.append({
                "type": "resonance",
                "available_resonances": available_resonance,
                "note": "残韵可在任意时刻插队使用",
            })

        # 代价类特殊行动
        actions.append({
            "type": "wit_of_desperation",
            "description": "消耗一次出手，声明急中生智（需要DM裁定）",
            "available": True,
        })
        if self.state.phase != "dead_duel":
            actions.append({
                "type": "escape",
                "description": "尝试逃跑（触发逃跑与追击事件，需要DM裁定）",
                "available": True,
            })
        if any(r.name == "买路财" for r in self.state.relics):
            actions.append({
                "type": "retreat",
                "description": "失去等同于怪物20%血限的碎片，安全撤退（买路财）",
                "available": True,
            })

        # 出手预算
        actions.append({
            "type": "budget",
            "actions_used": self.state.actions_used,
            "actions_total": self._player_action_budget(),
            "note": "每次主动行动（道纹/攻击）消耗1次出手；反应型法术与闪避不消耗",
        })

        # 敌方回合行动入口
        if self.state.enemies:
            actions.append({
                "type": "monster_turn",
                "description": "执行一只怪物的完整回合（出手次数=回合数÷3向上取整）",
                "monsters": [m.name for m in self.state.enemies if m.is_alive],
            })

        return {
            "phase": self.state.phase,
            "round": self.state.current_round,
            "actions": actions,
        }

    def _get_battle_end_actions(self) -> dict:
        return {
            "phase": "战终",
            "note": "调用 battle_end 完成结算：碎片奖励、增益减益清除、冷却推进、员工叛变检查",
        }

    # ==================== 核心行动接口 ====================

    def execute_action(self, action_type: str, params: dict = None) -> dict:
        """
        执行行动的统一入口
        AI通过此接口执行所有行动
        """
        if params is None:
            params = {}

        # 检查是否有待处理的中断
        if self._pending_interrupts:
            return {
                "success": False,
                "error": "有待处理的中断等待DM裁定",
                "pending_interrupts": [i.to_dict() for i in self._pending_interrupts],
                "instruction": "请先通过 submit_ruling() 提交DM裁定",
            }

        # 检查是否有待解决的随机请求（随机数规则）
        if self._pending_random and action_type != "random_number":
            return {
                "success": False,
                "error": "有待解决的随机数请求，必须先提交玩家数字",
                "pending_random": self._pending_random,
                "instruction": "请让玩家在范围内给出数字后提交 random_number",
            }

        try:
            handler = {
                "setup_attributes": self._action_setup_attributes,
                "setup_choose_daowen": self._action_setup_choose_daowen,
                "setup_choose_resonance": self._action_setup_choose_resonance,
                "setup_choose_region": self._action_setup_choose_region,
                "discover_relic_setup": self._action_discover_relic,
                "pre_battle_action": self._action_pre_battle,
                "spend_attribute_points": self._action_spend_attribute_points,
                "use_daowen": self._action_use_daowen,
                "use_spell": self._action_use_spell,
                "use_resonance": self._action_use_resonance,
                "attack": self._action_attack,
                "dodge_decision": self._action_dodge_decision,
                "monster_turn": self._action_monster_turn,
                "retreat": self._action_retreat,
                "declare_wit": self._action_declare_wit,
                "declare_escape": self._action_declare_escape,
                "declare_evolution": self._action_declare_evolution,
                "round_start": self._action_round_start,
                "round_end": self._action_round_end,
                "battle_start": self._action_battle_start,
                "battle_end": self._action_battle_end,
                "random_number": self._action_submit_random,
            }.get(action_type)

            if handler is None:
                result = {"success": False, "error": f"未知或未实装的行动类型: {action_type}"}
            else:
                result = handler(params)

            # 行动后果检查（轮回者死亡/异变化）
            if action_type in ("use_daowen", "use_spell", "attack", "dodge_decision",
                               "monster_turn", "round_end", "retreat"):
                death_note = self._check_player_death()
                if death_note and isinstance(result, dict):
                    result["death_note"] = death_note

            # 记录行动历史
            self._action_history.append({
                "action": action_type,
                "params": params,
                "result": result,
                "timestamp": time.time(),
                "game_id": self.state.game_id,
                "round": self.state.current_round,
            })

            # 自动校验（如果启用）
            if self._validator and isinstance(result, dict) and result.get("success"):
                validation_result = self._validator.validate(
                    self.state,
                    {"action": action_type, "params": params},
                    result,
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
                "instruction": "引擎计算出错，请检查参数",
            }
            self._last_result = error_result
            return error_result

    # ==================== 内部工具 ====================

    def _build_daowen_def(self, name: str) -> DaoWen:
        """根据注册表构建道纹定义（元信息，数值一律以 DaoWenEngine.resolve 为准）"""
        return DaoWen(
            name=name,
            formula=f"{name}X",
            cost_type="见计算结果",
            cost_formula="",
            effect_formula="",
        )

    def _find_entity(self, name: str) -> Optional[Entity]:
        all_entities = []
        if self.state.player:
            all_entities.append(self.state.player)
        all_entities += self.state.friends + self.state.employees + self.state.temp_friends + self.state.enemies
        return next((e for e in all_entities if e.name == name), None)

    def _caster_state_dict(self, caster: Entity) -> dict:
        """自由控X规则的X上限检查所需的状态字典"""
        return {
            "current_hp": caster.current_hp,
            "blood_limit": caster.blood_limit,
            "mana_limit": caster.mana_limit,
            "speed_limit": caster.speed_limit,
            "current_speed": caster.current_speed,
            "daowen_count": len(caster.dao_wen),
        }

    def _has_relic(self, name: str) -> bool:
        if not any(r.name == name for r in self.state.relics):
            return False
        sealed = self.state.relic_flags.get("抵扣_封印", {})
        return sealed.get(name, 0) <= 0

    def _player_action_budget(self) -> int:
        """轮回者本回合出手预算 = 速限/3向上取整 + 活力 - 无力（最低0）"""
        player = self.state.player
        if not player:
            return 0
        budget = player.action_count
        budget += player.get_status_value("活力")
        budget -= player.get_status_value("无力")
        return max(0, budget)

    def _apply_daowen_cost(self, calc: dict, caster: Entity, dw_instance: Optional[DaoWenInstance]) -> list[dict]:
        """
        真实支付道纹代价（代价必须真实生效）
        法力消耗在调用方已处理；此处处理所有非"消耗"代价：
        流血/衰老/枯竭/萎缩/疲惫/失忆/异变/冷却/唯一
        """
        applied = []
        cost_type = calc.get("cost_type", "消耗")

        if cost_type == "流血" and "cost_hp" in calc:
            before = caster.current_hp
            res = caster.take_damage(calc["cost_hp"], "代价")
            applied.append({"type": "流血", "amount": calc["cost_hp"],
                            "hp": f"{before}→{res['hp_after']}"})
            # 血誓戒：回始首次主动支付流血代价时获得等量格挡；支付后生命≤30%改为获得等量生命
            if caster is self.state.player and self._has_relic("血誓戒") \
                    and not self.state.relic_flags.get("血誓戒_本回合已触发"):
                self.state.relic_flags["血誓戒_本回合已触发"] = True
                if caster.current_hp <= math.ceil(caster.blood_limit * 0.3):
                    heal = caster.heal(calc["cost_hp"])
                    applied.append({"type": "血誓戒", "effect": f"生命≤30%，获得{heal['actual_heal']}点回复"})
                else:
                    caster.gain_shield(calc["cost_hp"])
                    applied.append({"type": "血誓戒", "effect": f"获得{calc['cost_hp']}点格挡"})

        elif cost_type == "衰老" and "cost_blood_limit" in calc:
            caster.blood_limit -= calc["cost_blood_limit"]
            caster.current_hp = min(caster.current_hp, caster.blood_limit)
            applied.append({"type": "衰老", "amount": calc["cost_blood_limit"],
                            "new_blood_limit": caster.blood_limit})

        elif cost_type == "枯竭" and "cost_mana_limit" in calc:
            caster.mana_limit -= calc["cost_mana_limit"]
            caster.current_mana = min(caster.current_mana, caster.mana_limit)
            applied.append({"type": "枯竭", "amount": calc["cost_mana_limit"],
                            "new_mana_limit": caster.mana_limit})

        elif cost_type == "萎缩" and "cost_speed_limit" in calc:
            caster.speed_limit -= calc["cost_speed_limit"]
            caster.current_speed = min(caster.current_speed, caster.speed_limit)
            applied.append({"type": "萎缩", "amount": calc["cost_speed_limit"],
                            "new_speed_limit": caster.speed_limit})

        elif cost_type == "疲惫" and "cost_speed" in calc:
            caster.current_speed = max(0, caster.current_speed - calc["cost_speed"])
            applied.append({"type": "疲惫", "amount": calc["cost_speed"],
                            "new_speed": caster.current_speed})

        elif cost_type == "异变" and "cost_mutation" in calc:
            caster.mutation += calc["cost_mutation"]
            applied.append({"type": "异变", "amount": calc["cost_mutation"],
                            "mutation_total": caster.mutation,
                            "warning": "异变达到50层时变为怪物" if caster.mutation >= 50 else ""})

        elif cost_type == "冷却" and dw_instance is not None:
            # 冷却X：使用后该道纹记为 X(0)/Y；战终完成场数+1，达到Y才能再次使用
            cooldown = calc.get("cost", 0)
            dw_instance.cooldown_remaining = cooldown
            applied.append({"type": "冷却", "cooldown_battles": cooldown})

        elif cost_type == "唯一" and dw_instance is not None:
            dw_instance.unique_used = True
            applied.append({"type": "唯一", "note": "本次轮回中无法再次使用"})

        return applied

    def _check_player_death(self) -> Optional[str]:
        """检查轮回者死亡或异变化（≥50层变为怪物）"""
        player = self.state.player
        if not player or self._death_handled:
            return None

        if player.mutation >= 50:
            player.is_alive = False
            self._death_handled = True
            self._raise_death_interrupt("异变达到50层，失去意志，变为怪物")
            return "异变达到50层：轮回者变为怪物，本次轮回终结"

        if not player.is_alive or player.current_hp <= 0:
            player.is_alive = False
            self._death_handled = True
            self._raise_death_interrupt("轮回者[命零]且无回复手段")
            return "轮回者[命零]：触发死之传承，等待DM确认遗言"

        return None

    def _raise_death_interrupt(self, reason: str):
        interrupt = Interrupt(
            interrupt_type=InterruptType.DEATH_INHERITANCE,
            context={"reason": reason, "battle": self.state.current_battle,
                     "round": self.state.current_round, "region": self.state.current_region},
            description=(
                f"死之传承触发（{reason}）。\n"
                f"该轮回者可在《死者之书》中新增一条遗言（最多20字）。\n"
                f"请通过 submit_ruling(interrupt_type='死之传承', ruling_text='遗言内容') 提交；\n"
                f"若无遗言，提交空裁定后本次轮回终结。"
            ),
            state_snapshot=self.state.to_dict(),
        )
        self._pending_interrupts.append(interrupt)

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
                "instruction": "1属性点=6血限=1速限=2法限，请重新分配",
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
        self._death_handled = False

        return {
            "success": True,
            "action": "分配属性点",
            "result": {
                "name": player.name,
                "blood_limit": blood_limit,
                "mana_limit": mana_limit,
                "speed_limit": speed_limit,
                "action_count": player.action_count,
                "shards": 20,
            },
            "next_actions": ["setup_choose_daowen", "setup_choose_resonance", "discover_relic_setup", "setup_choose_region"],
        }

    def _action_setup_choose_daowen(self, params: dict) -> dict:
        """选择初始道纹"""
        if not self.state.player:
            return {"success": False, "error": "请先分配属性点"}

        choice = params.get("daowen", "")
        valid = ["杀伐", "锐利"]

        if choice not in valid:
            return {"success": False, "error": f"只能从{valid}中选择"}

        self.state.player.dao_wen[choice] = DaoWenInstance(dao_wen=self._build_daowen_def(choice))

        return {
            "success": True,
            "action": "选择初始道纹",
            "result": {"daowen": choice},
            "next_actions": ["setup_choose_resonance", "discover_relic_setup", "setup_choose_region"],
        }

    def _action_setup_choose_region(self, params: dict) -> dict:
        """选择副本"""
        region = params.get("region", "")
        valid = list(MONSTER_POOLS.keys())

        if region not in valid:
            return {"success": False, "error": f"只能从{valid}中选择"}

        self.state.current_region = region
        self.state.current_battle = 0
        self.state.phase = "pre_battle"
        self.state.energy = 3

        return {
            "success": True,
            "action": "选择副本",
            "result": {"region": region, "tier": REGION_TIERS.get(region, 1)},
            "next_actions": ["pre_battle_action"],
            "note": "进入局外阶段。3点精力，耗尽后进入战斗。",
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
        }

    # ==================== 遗物发现 ====================

    def _action_discover_relic(self, params: dict) -> dict:
        """
        发现遗物（开局 / 共鸣-发现）
        随机数规则：从当前遗物池中抽取3个未持有选项... 规则书为"发现"：
        从随机抽取的3个选项中选择1个。本实现如实按规则：抽3选1，两次随机：
        先抽3个候选（需要玩家数字），再由AI从候选中自选1个。
        池中小于3件时全部作为候选。
        """
        purpose = params.get("purpose", "setup")  # setup / gongming

        # 二阶段：从候选中选定（发现机制：抽3选1）
        chosen = params.get("chosen", "")
        if chosen:
            return self._resolve_discover_relic(chosen)

        pool = [r.name for r in self.state.relics_pool]
        if not pool:
            return {"success": False, "error": "遗物池已空"}

        if self._pending_random:
            return {"success": False, "error": "已有待解决的随机请求"}

        candidates = pool if len(pool) <= 3 else None
        if candidates is None:
            # 随机抽3个候选：玩家数字决定起始偏移，顺序取3
            self.dice.create_pool("relic_candidates", pool)
            self._pending_random = {
                "purpose": "discover_relic_candidates",
                "pool_name": "relic_candidates",
                "meta": {"pool": pool, "purpose": purpose},
            }
            return {
                "success": True,
                "action": "发现遗物（抽候选）",
                "random_required": True,
                "range": f"1~{len(pool)}",
                "instruction": f"请玩家在 1~{len(pool)} 中给出数字，用于从遗物池随机抽取3个候选",
            }

        # 直接3选1（池≤3）
        return {
            "success": True,
            "action": "发现遗物（自选）",
            "candidates": candidates,
            "instruction": "请从候选中选择1件，再次调用 discover_relic_setup 并传入 chosen=<遗物名>",
            "choose_by_param": "chosen",
        }

    def _resolve_discover_relic(self, chosen_name: str) -> dict:
        """将选定遗物加入持有"""
        pool = self.state.relics_pool
        relic = next((r for r in pool if r.name == chosen_name), None)
        if relic is None:
            return {"success": False, "error": f"遗物[{chosen_name}]不在池中：{[r.name for r in pool]}"}

        pool.remove(relic)
        self.state.relics.append(relic)
        implemented = "implemented" in relic.tags
        return {
            "success": True,
            "action": "获得遗物",
            "result": {
                "relic": relic.name,
                "effect": relic.effect,
                "implemented": implemented,
                "note": None if implemented else "该遗物效果暂未实装，引擎不会假装其生效",
            },
        }

    # ==================== 局外行动 ====================

    def _action_pre_battle(self, params: dict) -> dict:
        """局外阶段行动"""
        if self.state.phase != "pre_battle":
            return {"success": False, "error": f"当前阶段({self.state.phase})不能执行局外行动"}

        action = params.get("sub_action", "")

        unavailable = {
            "探索": "事件系统未实装，不能假装探索成功",
            "维修": "维修作用于消耗品；消耗品获取依赖事件系统，暂未实装",
            "雇佣": "员工雇佣体系暂未实装",
            "炼心": "龙心代价抵消体系暂未实装",
        }
        if action in unavailable:
            return {"success": False, "error": unavailable[action], "unavailable": True}

        if self.state.energy <= 0:
            return {
                "success": False,
                "error": "精力已耗尽",
                "instruction": "精力耗尽，进入战斗阶段。请调用 battle_start。",
            }

        result_map = {
            "领悟": self._pre_battle_lingwu,
            "休整": self._pre_battle_xiuzheng,
            "修行": self._pre_battle_xiuxing,
            "学习": self._pre_battle_xuexi,
            "共鸣": self._pre_battle_gongming,
        }

        if action not in result_map:
            return {"success": False, "error": f"未知局外行动: {action}"}

        self.state.energy -= 1
        return result_map[action](params)

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
            "energy_remaining": self.state.energy,
        }

    def _pre_battle_xiuzheng(self, params: dict) -> dict:
        """休整：产生恢复量，可自由分配给自己或队友（分配真实生效）"""
        tier = params.get("tier", 1)  # 1=8, 2=24(10碎片), 3=48(25碎片)

        tier_map = {1: (8, 0), 2: (24, 10), 3: (48, 25)}
        if tier not in tier_map:
            self.state.energy += 1
            return {"success": False, "error": "休整档位无效"}

        heal, cost = tier_map[tier]

        if self.state.shards < cost:
            self.state.energy += 1
            return {"success": False, "error": f"碎片不足，需要{cost}，当前{self.state.shards}"}

        self.state.shards -= cost

        # 真实分配恢复量：allocations = {实体名: 数值}
        allocations = params.get("allocations") or {self.state.player.name: heal}
        total_alloc = sum(allocations.values())
        if total_alloc > heal:
            self.state.energy += 1
            self.state.shards += cost
            return {"success": False, "error": f"分配总量{total_alloc}超过恢复量{heal}"}

        applied = []
        for name, amount in allocations.items():
            if amount <= 0:
                continue
            target = self._find_entity(name)
            if target is None or target not in (
                    [self.state.player] + self.state.friends + self.state.employees + self.state.temp_friends):
                self.state.energy += 1
                self.state.shards += cost
                return {"success": False, "error": f"恢复量只能分配给自己或队友，未找到[{name}]"}
            res = target.heal(amount)
            applied.append({"target": name, "heal": amount, "actual": res["actual_heal"],
                            "hp": f"{res['hp_before']}→{res['hp_after']}"})

        return {
            "success": True,
            "action": "休整",
            "result": {
                "heal_pool": heal,
                "shard_cost": cost,
                "allocated": applied,
                "unused": heal - total_alloc,
                "shards_remaining": self.state.shards,
            },
        }

    def _pre_battle_xiuxing(self, params: dict) -> dict:
        """修行：获得属性点"""
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
        self.state.attribute_points += points

        return {
            "success": True,
            "action": "修行",
            "result": {
                "points_gained": points,
                "shard_cost": cost,
                "total_attribute_points": self.state.attribute_points,
                "note": "用 spend_attribute_points 分配：1点=1速限 或 1点=2法限（血限无法修行提升）",
            },
        }

    def _action_spend_attribute_points(self, params: dict) -> dict:
        """分配属性点（局外修行成果，真实生效）"""
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}

        to = params.get("to", "")  # "速限" / "法限"
        points = params.get("points", 0)

        if points <= 0 or points > self.state.attribute_points:
            return {"success": False, "error": f"属性点不足或无效：持有{self.state.attribute_points}，请求{points}"}

        if to == "速限":
            player.speed_limit += points
            player.current_speed += points
        elif to == "法限":
            player.mana_limit += points * 2
        else:
            return {"success": False, "error": "只能分配到 速限 或 法限（血限无法修行提升）"}

        self.state.attribute_points -= points

        return {
            "success": True,
            "action": "分配属性点",
            "result": {
                "to": to,
                "points": points,
                "speed_limit": player.speed_limit,
                "mana_limit": player.mana_limit,
                "action_count": player.action_count,
                "remaining_points": self.state.attribute_points,
            },
        }

    def _pre_battle_xuexi(self, params: dict) -> dict:
        """
        学习（真实生效，不再只返回instruction）：
        - learn_type="spell"：学会法术（须持有其全部所需道纹）
        - learn_type="transform_daowen"：习得转化道纹（须为已持有道纹沿闭环的相邻变化）
        tier: 1=一种, 2=两种(10碎片), 3=三种(25碎片)
        """
        learn_type = params.get("learn_type", "spell")
        names = params.get("names", [])
        tier = params.get("tier", 1)

        if learn_type == "create_spell":
            self.state.energy += 1
            return {
                "success": False,
                "unavailable": True,
                "error": "自创法术涉及残韵式创造，按规则须抛出中断由DM裁定，暂未实装自动流程",
            }

        tier_cost = {1: 0, 2: 10, 3: 25}
        if tier not in tier_cost:
            self.state.energy += 1
            return {"success": False, "error": "学习档位无效（1/2/3）"}
        if len(names) != tier:
            self.state.energy += 1
            return {"success": False, "error": f"档位{tier}需提供{tier}个名称，实际{len(names)}个"}

        cost = tier_cost[tier]
        if self.state.shards < cost:
            self.state.energy += 1
            return {"success": False, "error": f"碎片不足，需要{cost}"}

        player = self.state.player
        learned = []

        if learn_type == "spell":
            for name in names:
                if name not in SPELL_LIBRARY:
                    self.state.energy += 1
                    return {"success": False, "error": f"法术[{name}]不存在。可学：{sorted(SPELL_LIBRARY.keys())}"}
                if any(s.name == name for s in player.spells):
                    self.state.energy += 1
                    return {"success": False, "error": f"法术[{name}]已学会"}
                spec = SPELL_LIBRARY[name]
                missing = [d for d in spec["required_daowen"] if d not in player.dao_wen]
                if missing:
                    self.state.energy += 1
                    return {
                        "success": False,
                        "error": f"未持有法术[{name}]所需道纹：{missing}（法术必须完全由已有道纹组成）",
                    }

            self.state.shards -= cost
            for name in names:
                spec = SPELL_LIBRARY[name]
                player.spells.append(Spell(
                    name=name,
                    required_daowen=list(spec["required_daowen"]),
                    trigger_condition=spec["trigger"],
                    effect_flow="→".join(s["daowen"] for s in spec["steps"]),
                    rank=len(spec["required_daowen"]),
                ))
                learned.append(name)

        elif learn_type == "transform_daowen":
            for name in names:
                if name in player.dao_wen:
                    self.state.energy += 1
                    return {"success": False, "error": f"已持有道纹【{name}】（角色道纹唯一）"}
                if name not in DaoWenEngine.list_all():
                    self.state.energy += 1
                    return {"success": False, "error": f"道纹【{name}】不存在"}
                # 转化道纹：须以自身已持有道纹为起点，沿闭环相邻路径获得
                from_source = None
                from_resonance = None
                for owned in player.dao_wen.keys():
                    for edge in ResonanceEngine.get_available_resonance(owned):
                        if edge["target_daowen"] == name:
                            from_source = owned
                            from_resonance = edge["resonance_type"]
                            break
                    if from_source:
                        break
                if from_source is None:
                    self.state.energy += 1
                    return {
                        "success": False,
                        "error": f"【{name}】不是任何已持有道纹的相邻转化路径（当前持有：{list(player.dao_wen.keys())}）",
                    }
                learned.append({"name": name, "from": from_source, "via": from_resonance})

            self.state.shards -= cost
            for item in learned:
                player.dao_wen[item["name"]] = DaoWenInstance(dao_wen=self._build_daowen_def(item["name"]))

        else:
            self.state.energy += 1
            return {"success": False, "error": "learn_type 仅支持 spell / transform_daowen"}

        return {
            "success": True,
            "action": "学习",
            "result": {
                "learn_type": learn_type,
                "learned": learned,
                "shard_cost": cost,
                "shards_remaining": self.state.shards,
            },
        }

    def _pre_battle_gongming(self, params: dict) -> dict:
        """共鸣：精力再次-1，发现/自选(15碎片)一件遗物"""
        sub = params.get("sub", "discover")  # discover / choose

        # 共鸣本身已在 _action_pre_battle 扣1点精力，这里扣"再次-1"
        if self.state.energy <= 0:
            return {
                "success": False,
                "error": "共鸣需要精力再次-1，当前精力不足",
                "note": "已扣除的1点精力不退还（行动已声明）",
            }

        if not self.state.relics_pool:
            return {"success": False, "error": "遗物池已空"}

        if sub == "discover":
            self.state.energy -= 1
            return self._action_discover_relic({"purpose": "gongming"})

        elif sub == "choose":
            cost = 15
            if self.state.shards < cost:
                return {"success": False, "error": f"碎片不足，自选遗物需要{cost}"}
            chosen = params.get("chosen", "")
            self.state.energy -= 1
            self.state.shards -= cost
            result = self._resolve_discover_relic(chosen)
            result["shard_cost"] = cost
            return result

        return {"success": False, "error": "sub 仅支持 discover / choose"}

    # ==================== 战斗：出怪 ====================

    def _action_battle_start(self, params: dict) -> dict:
        """
        战始：
        1. 抽取出怪（数量=战斗场数，一阶副本直接-2，最低1，允许重复种族）
        2. 随机数规则：每只怪由玩家数字从怪物池抽选
        3. 选择战斗背景
        4. 怪物白板开局（无初始状态）
        """
        if self.state.phase not in ("pre_battle",):
            return {"success": False, "error": f"当前阶段({self.state.phase})不能进入战始"}
        if self.state.energy > 0:
            return {"success": False, "error": f"精力未耗尽({self.state.energy})，不能进入战斗"}

        self.state.phase = "battle_start"
        self.state.current_battle += 1
        self.state.current_round = 0
        self.state.relic_flags.clear()
        self.state.battle_background = params.get("battle_background", "未选择")

        battle_no = self.state.current_battle
        region = self.state.current_region

        if battle_no > REGION_BATTLE_COUNT:
            # 第8场应为最终死斗，由冠冕流程处理
            return {
                "success": False,
                "error": f"{region}仅{REGION_BATTLE_COUNT}场常规战斗，第8场为最终死斗（由最终的冠冕触发）",
            }

        count = monster_spawn_count(battle_no, region)
        pool = [m["name"] for m in MONSTER_POOLS[region]]

        self._pending_random = {
            "purpose": "spawn_monster",
            "pool_name": f"spawn_battle_{battle_no}",
            "meta": {
                "region": region,
                "battle_no": battle_no,
                "count": count,
                "pool": pool,
                "draws": [],
                "battle_background": self.state.battle_background,
            },
        }

        return {
            "success": True,
            "action": "战始",
            "battle_number": battle_no,
            "battle_background": self.state.battle_background,
            "spawn_count": count,
            "random_required": True,
            "range": f"1~{len(pool)}",
            "instruction": (
                f"第{battle_no}场出怪{count}只。怪物池共{len(pool)}种，允许重复种族。"
                f"请玩家逐只给出 1~{len(pool)} 的数字（random_number）"
            ),
        }

    def _finish_spawn(self, meta: dict) -> dict:
        """抽怪完成后，实体化怪物并结算战始"""
        region = meta["region"]
        monsters = []
        for draw_idx in meta["draws"]:
            data = MONSTER_POOLS[region][draw_idx - 1]
            entity = Entity(
                name=data["name"],
                entity_type="怪物",
                blood_limit=data["blood_limit"],
                current_hp=data["blood_limit"],
                attack_count=data["attack_count"],
                attack_power=data["attack_power"],
                speed_limit=0, current_speed=0,
                spawn_blood_limit=data["blood_limit"],
            )
            # 同名多只时编号（1、2...）保证实体名唯一
            if any(m.name == entity.name for m in monsters):
                n = 2
                while any(m.name == f"{data['name']}{n}" for m in monsters):
                    n += 1
                entity.name = f"{data['name']}{n}"
            for dw_name in data["daowen"]:
                entity.dao_wen[dw_name] = DaoWenInstance(dao_wen=self._build_daowen_def(dw_name))
            # 罪孽都市怪物战始自带碎片 = 专属道纹数值之和×2
            if region == "罪孽都市":
                from .gamedata import REGION_EXCLUSIVE_DAOWEN as _RGE
                exclusive_sum = sum(v for k, v in data["daowen"].items() if k in _RGE["罪孽都市"])
                entity.shards = exclusive_sum * 2
            monsters.append(entity)

        self.state.enemies = monsters
        self.state.phase = "in_combat"

        # 罪孽都市专属：怪物自带碎片可被夺取，未夺取则死亡/结束时消散
        return {
            "success": True,
            "action": "出怪完成",
            "battle_number": meta["battle_no"],
            "battle_background": meta["battle_background"],
            "enemies": [
                {
                    "name": m.name,
                    "panel": f"{m.attack_count}×{m.attack_power}/{m.blood_limit}",
                    "daowen": {k: v for k, v in ((n, i.dao_wen.name) for n, i in m.dao_wen.items())},
                    "carry_shards": m.shards,
                    "note": "白板开局：任何道纹须在其出手轮主动发动后方可生效",
                }
                for m in monsters
            ],
            "wormhole": "怪物出手次数=当前回合数÷3向上取整；[战始]效果现已结算",
        }

    # ==================== 战斗：道纹 ====================

    def _action_use_daowen(self, params: dict) -> dict:
        """
        发动道纹（真实结算）：
        - 校验持有/可用/出手预算/X上限/实装白名单
        - 消耗/代价真实支付
        - 目标为敌方单体时可声明闪避（target_dodge）
        """
        result = self._use_daowen_core(
            caster=self.state.player,
            name=params.get("daowen_name", ""),
            x=params.get("x", 1),
            target_name=params.get("target", ""),
            target_dodge=params.get("target_dodge", False),
            consume_action=True,
            forget_names=params.get("forget_names"),
            extra=params.get("extra") or params,
        )
        return result

    def _use_daowen_core(
        self,
        caster: Entity,
        name: str,
        x: int,
        target_name: str,
        target_dodge: bool = False,
        consume_action: bool = True,
        forget_names: Optional[list] = None,
        skip_mana_cost: bool = False,
        extra: dict = None,
    ) -> dict:
        """use_daowen / use_spell / monster_turn 共用的真实结算核心"""
        from .gamedata import IMPLEMENTED_DAOWEN, UNIMPLEMENTED_DAOWEN
        extra = extra or {}
        player = self.state.player
        is_player_side = caster in ([player] + self.state.friends + self.state.employees + self.state.temp_friends)

        if name not in caster.dao_wen:
            return {"success": False, "error": f"{caster.name}未持有道纹: {name}"}

        if name not in IMPLEMENTED_DAOWEN:
            reason = "机制交互纵深过大，待DM裁定语义后补装" if name in UNIMPLEMENTED_DAOWEN else "机制未实装"
            return {
                "success": False,
                "unavailable": True,
                "error": f"道纹【{name}】{reason}，引擎拒绝假装生效",
            }

        dw_instance = caster.dao_wen[name]

        if not dw_instance.can_use():
            return {"success": False, "error": f"道纹【{name}】不可用（{dw_instance.reason_unusable()}）"}

        if x < 1:
            return {"success": False, "error": "X必须≥1"}

        # 无法行动：束缚/眩晕施加于施法者时禁止主动行动
        if caster.has_status("束缚") or caster.has_status("眩晕"):
            return {"success": False, "error": f"{caster.name}处于无法行动状态（束缚/眩晕）"}

        # 退化：每次发动道纹时，该次道纹的数值-X（最低为0）
        degrade_note = None
        if caster.has_status("退化"):
            degrade = caster.get_status_value("退化")
            new_x = max(0, x - degrade)
            if new_x != x:
                degrade_note = f"退化使道纹数值 {x}→{new_x}"
                x = new_x
                if x <= 0:
                    return {"success": False, "error": "退化使道纹数值降为0，发动失败"}

        # 出手预算（仅限轮回者侧主动行动）
        if consume_action and caster is player:
            budget = self._player_action_budget()
            if self.state.actions_used >= budget:
                return {
                    "success": False,
                    "error": f"出手次数已用完（{self.state.actions_used}/{budget}），请结束行动进入敌方回合",
                }

        # 查找目标
        target = caster
        if target_name:
            found = self._find_entity(target_name)
            if found is None:
                return {"success": False, "error": f"目标不存在: {target_name}"}
            target = found

        # 无神：选择目标时强制改为自身
        if caster.has_status("无神") and target is not caster:
            target = caster

        # 敌对单体判定 → 目标可闪避（飞行规则：非飞行角色无法选飞行者为目标）
        targeted = name not in CombatEngine.UNTARGETED_DAOWEN and target is not caster
        target_is_enemy_of_caster = (target in self.state.enemies) if is_player_side else (target not in self.state.enemies)

        if CombatEngine.is_flying(target) and not CombatEngine.is_flying(caster) \
                and target_is_enemy_of_caster and targeted:
            return {"success": False, "error": f"{target.name}处于飞行状态，无法被非飞行角色选为目标"}

        dodge_resolved = None
        if targeted and target_is_enemy_of_caster:
            must_hit = caster.has_status("必中")
            if target_dodge:
                if must_hit:
                    dodge_resolved = {"dodge_attempted": True, "success": False, "reason": "必中判定无法闪避"}
                elif target.current_speed >= 1:
                    target.current_speed -= 1
                    dodge_resolved = {"dodge_attempted": True, "success": True,
                                      "speed_after": target.current_speed}
                else:
                    dodge_resolved = {"dodge_attempted": True, "success": False, "reason": "速度不足"}

        # 闪避成功 → 判定与结算完全失效，且不消耗（残韵未生效不消耗原则同样适用于此）
        if dodge_resolved and dodge_resolved["success"]:
            if consume_action and caster is player:
                self.state.actions_used += 1
            return {
                "success": True,
                "action": f"{caster.name}发动道纹【{name}X={x}】被闪避",
                "dodge": dodge_resolved,
                "cost_refunded": True,
                "note": "闪避成功：该道纹判定与结算完全失效，消耗与代价未发生",
            }

        # 失忆代价需要指定失去的道纹
        calc = DaoWenEngine.resolve(
            name, x,
            target=target, caster=caster,
            _state=self._caster_state_dict(caster),
        )

        # 法力消耗（怪物发动面板道纹不支付法力，只消耗出手；愤怒：目标法力消耗减半）
        cost = calc.get("cost", 0)
        if calc.get("cost_type") == "消耗" and cost > 0 and not skip_mana_cost and caster.entity_type != "怪物":
            if caster.has_status("愤怒"):
                cost = math.ceil(cost / 2)
            if not caster.spend_mana(cost):
                return {"success": False, "error": f"法力不足，需要{cost}，当前{caster.current_mana}"}

        # 失忆X：永久失去X种道纹（参数指定）
        if calc.get("cost_type") == "失忆":
            x_cost = calc.get("cost", x)
            if not forget_names or len(forget_names) != x_cost:
                return {"success": False, "error": f"失忆{x_cost}需通过 forget_names 指定失去的{x_cost}种道纹"}
            for fn in forget_names:
                if fn not in caster.dao_wen:
                    return {"success": False, "error": f"未持有道纹【{fn}】，无法失忆"}
            for fn in forget_names:
                del caster.dao_wen[fn]

        # 支付代价（流血/衰老/疲惫/异变/冷却/唯一等）
        cost_applied = self._apply_daowen_cost(calc, caster, dw_instance)

        # 执行效果
        execution = self._execute_daowen_effect(name, calc, caster, target, extra=extra)

        if consume_action and caster is player:
            self.state.actions_used += 1

        return {
            "success": True,
            "action": f"{caster.name}发动道纹【{name}X={x}】",
            "calculation": calc,
            "cost_applied": cost_applied,
            "dodge": dodge_resolved,
            "degrade_note": degrade_note,
            "execution": execution,
            "budget": {"used": self.state.actions_used,
                       "total": self._player_action_budget()} if caster is player else None,
        }

    def _execute_daowen_effect(self, name: str, calc: dict, caster: Entity, target: Entity, extra: dict = None) -> dict:
        """执行道纹效果（怪物非专属道纹效果×3；特殊机制道纹在此真实分发）"""
        result = {"daowen": name, "effects": []}
        extra = extra or {}

        def add_status(entity: Entity, status_name: str, duration: int, value: int, meta: dict = None):
            entity.add_status(StatusEffect(
                name=status_name,
                remaining_rounds=duration if duration != 0 else -1,
                value=value,
                source=caster.name,
                meta=meta or {},
            ))
            result["effects"].append({
                "type": "status_added", "target": entity.name, "status": status_name,
                "duration": duration, "value": value,
            })

        multiplier = self.combat.is_monster_triple(name, caster)
        if multiplier > 1:
            result["monster_triple"] = True
            result["multiplier"] = multiplier

        # ============ 特殊机制道纹（完全接管，不走通用分支）============

        if name == "超频":
            boost = calc["speed_boost"] * multiplier
            caster.current_speed += boost
            result["effects"].append({"type": "speed_boost", "target": caster.name, "amount": boost})
            return result

        if name == "减速":
            target.current_speed = max(0, math.ceil(target.current_speed / 2))
            add_status(target, "减速", calc["duration"], calc["x"] * multiplier)
            result["effects"].append({"type": "speed_halved", "target": target.name,
                                      "speed_after": target.current_speed})
            return result

        if name == "自残":
            hits = calc["self_attack_count"]
            hits_log = []
            for i in range(hits):
                if not target.is_alive:
                    break
                hits_log.append(self.combat.resolve_attack(target, target, hit_index=i))
            result["effects"].append({"type": "self_attacks", "target": target.name, "hits": hits_log})
            return result

        if name == "自食":
            reduced = min(calc["attack_reduction"], caster.attack_power)
            caster.attack_power -= reduced
            heal = caster.heal(calc["heal"] * multiplier)
            result["effects"].append({"type": "attack_to_heal", "target": caster.name,
                                      "attack_reduced": reduced, **heal})
            return result

        if name == "变形":
            if target.has_status("定型"):
                result["effects"].append({"type": "blocked", "note": "目标被定型，无法变形"})
                return result
            orig_count, orig_power = target.attack_count, target.attack_power
            target.attack_count, target.attack_power = orig_power, orig_count
            add_status(target, "变形", calc["duration"], 0,
                       meta={"orig_attack_count": orig_count, "orig_attack_power": orig_power})
            result["effects"].append({
                "type": "swap_attack", "target": target.name,
                "from": f"{orig_count}×{orig_power}", "to": f"{target.attack_count}×{target.attack_power}",
                "duration": calc["duration"],
            })
            return result

        if name == "封印":
            count = calc["targets_removed"]
            sealed_names = []
            for _ in range(count):
                alive = [m for m in self.state.enemies if m.is_alive]
                if not alive:
                    break
                chosen = None
                want = (extra.get("seal_targets") or [])
                for nm in want:
                    chosen = next((m for m in alive if m.name == nm), None)
                    if chosen:
                        break
                if chosen is None:
                    chosen = alive[0]
                chosen.is_alive = False   # 移出本场战斗（不提供碎片收益）
                sealed_names.append(chosen.name)
            result["effects"].append({"type": "seal", "sealed": sealed_names,
                                      "note": "被移出的怪物不提供任何碎片收益"})
            return result

        if name == "赎金":
            amount = calc["shard_steal"] * multiplier
            stolen = 0
            if target is self.state.player:
                available = self.state.shards + self.state.fake_shards
                stolen = min(amount, available)
                self.state.lose_shards(stolen, in_battle=True)
                self.state.shards -= 0
            else:
                stolen = min(amount, target.shards)
                target.shards -= stolen
            if stolen > 0:
                if caster is self.state.player:
                    self.state.shards += stolen
                else:
                    caster.shards += stolen
                result["effects"].append({"type": "shard_steal", "amount": stolen})
            else:
                # 目标没有碎片，改为失去X点当前速度
                x = calc["x"]
                target.current_speed = max(0, target.current_speed - x)
                result["effects"].append({"type": "speed_penalty", "target": target.name, "amount": x})
            return result

        if name == "假钞":
            gained = calc["fake_shards"] * multiplier
            self.state.fake_shards += gained
            result["effects"].append({"type": "fake_shards", "amount": gained,
                                      "fake_shards_now": self.state.fake_shards})
            return result

        if name == "坠落":
            grounded = []
            for e in self.combat._all_entities():
                if CombatEngine.is_flying(e):
                    e.is_flying = False
                    e.status_effects = [s for s in e.status_effects if s.name not in ("飞行", "滑翔")]
                    grounded.append(e.name)
                add_status(e, "坠落", calc["duration"], calc["x"])
            result["effects"].append({"type": "ground_all", "grounded": grounded,
                                      "note": "所有飞行角色无法飞行且造成伤害减半"})
            return result

        if name == "滑翔":
            add_status(caster, "滑翔", calc["duration"], calc["x"])
            result["effects"].append({"type": "gain_flying", "target": caster.name})
            return result

        if name == "缓慢":
            # 缓慢X：本回合若目标单轮出手次数≤X，则其无法出手
            if calc.get("effective"):
                add_status(target, "缓慢", 1, calc["x"])
                result["effects"].append({"type": "slow_apply", "target": target.name,
                                          "note": "本回合无法出手"})
            else:
                result["effects"].append({"type": "slow_failed",
                                          "note": calc.get("summary", "未生效")})
            return result

        if name == "蒙蔽":
            add_status(target, "蒙蔽", -1, calc["invalid_damage_hits"] * multiplier)
            return result

        if name == "抵扣":
            # 选择目标拥有的一件遗物封印（封印其效果钩子）
            victim_relics = []
            if target is self.state.player:
                victim_relics = [r.name for r in self.state.relics]
            else:
                victim_relics = extra.get("target_relics", [])
            choice = extra.get("relic_name") or (victim_relics[0] if victim_relics else None)
            if choice:
                self.state.relic_flags.setdefault("抵扣_封印", {})
                self.state.relic_flags["抵扣_封印"][choice] = calc["duration"]
                result["effects"].append({"type": "relic_sealed", "relic": choice,
                                          "duration": calc["duration"]})
            else:
                result["effects"].append({"type": "relic_seal_failed", "note": "目标没有可封印的遗物"})
            return result

        if name == "嫁祸":
            add_status(caster, "嫁祸", -1, calc["redirect_count"],
                       meta={"redirect_to": target.name})
            return result

        if name == "背负":
            add_status(caster, "背负", -1, calc["absorb_count"],
                       meta={"protected": target.name})
            return result

        if name == "清算":
            drain = calc.get("shield_drain", 0)
            add_status(target, "清算", calc["duration"], drain)
            return result

        if name == "逆鳞":
            # 层数从0开始积累
            add_status(target, "逆鳞", calc["duration"], 0)
            return result

        # ============ 通用分支 ============

        # 多次独立伤害（血债X：2X次1点伤害；每次独立判定闪避/龙鳞/反伤/承伤）
        if "hits" in calc and "damage_per_hit" in calc:
            hits = calc["hits"] * multiplier
            per = calc["damage_per_hit"]
            hit_log = []
            for i in range(hits):
                if not target.is_alive:
                    hit_log.append({"hit": i, "skipped": "目标已死亡"})
                    break
                modifier_result = {"attack_based": False}
                dmg_value = self.combat.apply_outgoing_damage_modifiers(caster, target, per, modifier_result)
                bearer = self.combat.find_damage_bearer(target, modifier_result)
                dmg = bearer.take_damage(dmg_value)
                merged = {**modifier_result, **dmg}
                if dmg["actual_damage"] > 0:
                    self.combat.on_damage_taken(caster, bearer, dmg["actual_damage"], merged)
                hit_log.append({"hit": i, "actual": dmg["actual_damage"], **merged})
            result["effects"].append({"type": "multi_hit_damage", "target": target.name,
                                      "hits": hits, "per_hit": per, "log": hit_log})
            return result

        # 伤害类（含借 力/加害/龙鳞/裂变/贯穿/承伤链/受伤钩子）
        if "target_damage" in calc:
            modifier_result = {"attack_based": False}
            dmg_value = calc["target_damage"] * multiplier
            dmg_value = self.combat.apply_outgoing_damage_modifiers(caster, target, dmg_value, modifier_result)
            if target.has_status("裂变"):
                x_split = max(1, target.get_status_value("裂变"))
                per = dmg_value // x_split
                dmg_value = per * x_split
                modifier_result["liebian_split"] = {"times": x_split, "per_hit": per}
            bearer = self.combat.find_damage_bearer(target, modifier_result)
            ignore_shield = caster.has_status("贯穿")
            dmg = bearer.take_damage(dmg_value, "普通" if not ignore_shield else "无视格挡")
            merged = {**modifier_result, **dmg}
            if dmg["actual_damage"] > 0:
                self.combat.on_damage_taken(caster, bearer, dmg["actual_damage"], merged)
            result["effects"].append({"type": "damage", "target": target.name, **merged})

        # AOE伤害（冲击：作用于施法者的敌方阵营全体，不可闪避）
        if "aoe_damage" in calc:
            actual_aoe = calc["aoe_damage"] * multiplier
            player_side = [e for e in ([self.state.player] + self.state.friends
                                       + self.state.employees + self.state.temp_friends) if e]
            if caster in player_side:
                targets = self.state.get_all_enemy_side()
            else:
                targets = [e for e in player_side if e.is_alive]
            for enemy in targets:
                modifier_result = {"attack_based": False}
                dmg_value = self.combat.apply_outgoing_damage_modifiers(caster, enemy, actual_aoe, modifier_result)
                bearer = self.combat.find_damage_bearer(enemy, modifier_result)
                dmg = bearer.take_damage(dmg_value)
                merged = {**modifier_result, **dmg}
                if dmg["actual_damage"] > 0:
                    self.combat.on_damage_taken(caster, bearer, dmg["actual_damage"], merged)
                result["effects"].append({"type": "aoe_damage", "target": enemy.name, **merged})

        # 回复类
        if "target_heal" in calc:
            actual_heal = calc["target_heal"] * multiplier
            heal = target.heal(actual_heal)
            result["effects"].append({"type": "heal", "target": target.name, **heal})

        if "heal" in calc and "target_heal" not in calc:
            heal = target.heal(calc["heal"] * multiplier)
            result["effects"].append({"type": "heal", "target": target.name, **heal})

        # 格挡类
        if "target_shield" in calc:
            actual_shield = calc["target_shield"] * multiplier
            target.gain_shield(actual_shield)
            result["effects"].append({"type": "shield", "target": target.name, "amount": actual_shield,
                                      "base": calc["target_shield"], "multiplier": multiplier})

        # 血限减少（副作用：当前生命同步削减）
        if "blood_limit_reduction" in calc:
            reduction = calc["blood_limit_reduction"]
            target.blood_limit -= reduction
            hp_lost = min(reduction, target.current_hp)
            target.current_hp = min(target.current_hp, target.blood_limit)
            if hp_lost > 0:
                target.hp_lost_this_round += hp_lost
                self.combat.on_damage_taken(caster, target, hp_lost, {"actual_damage": hp_lost, "blood_limit_source": True})
            if target.current_hp <= 0:
                target.is_alive = False
            result["effects"].append({
                "type": "blood_limit_reduction",
                "target": target.name,
                "reduction": reduction,
                "new_blood_limit": target.blood_limit,
                "died": not target.is_alive,
            })

        # 血限增加
        if "blood_limit_increase" in calc:
            increase = calc["blood_limit_increase"]
            target.blood_limit += increase
            result["effects"].append({"type": "blood_limit_increase", "target": target.name, "increase": increase})

        # 直接伤害字段（衰败等百分比伤害）
        if "damage" in calc:
            dmg = target.take_damage(calc["damage"])
            if dmg["actual_damage"] > 0:
                self.combat.on_damage_taken(caster, target, dmg["actual_damage"], dmg)
            result["effects"].append({"type": "damage", "target": target.name, **dmg})

        # 法力获得
        if "mana_gain" in calc:
            caster.current_mana += calc["mana_gain"]
            result["effects"].append({"type": "mana_gain", "source": caster.name,
                                      "mana_gained": calc["mana_gain"]})

        # 状态效果添加（持续X / 持续∞，含怪物×3数值）
        if "duration" in calc and calc.get("duration") is not None:
            duration = calc["duration"] if calc["duration"] != 0 else -1
            effect_target = target if target else caster
            value = calc.get("x", 0)
            if multiplier > 1:
                value = value * multiplier
            add_status(effect_target, name, duration, value)
            # 飞行道纹生效时同步实体飞行标记
            if name == "飞行":
                effect_target.is_flying = True

        return result

    # ==================== 战斗：法术 ====================

    def _action_use_spell(self, params: dict) -> dict:
        """
        发动法术（真实结算，遵守积木/循环/中断三大法则）
        params:
          spell_name: 法术名（须已在局外学习）
          trigger_timing: 声明的触发时点，须与法术触发条件一致
          target: 敌方目标名（spell流程中 enemy 步使用）
          x / y / z: 各步骤的自由控X数值
          max_cycles: 循环类法术的最大循环数保险（默认50，循环按中断法则自然终止）
        """
        player = self.state.player
        if not player or not player.is_alive:
            return {"success": False, "error": "轮回者不可用"}

        spell_name = params.get("spell_name", "")
        spec = SPELL_LIBRARY.get(spell_name)
        if spec is None:
            return {"success": False, "error": f"法术[{spell_name}]不存在。可学：{sorted(SPELL_LIBRARY.keys())}"}
        if not any(s.name == spell_name for s in player.spells):
            return {"success": False, "error": f"未学会法术[{spell_name}]（须局外学习后方可开启）"}

        declared_trigger = params.get("trigger_timing", spec["trigger"])
        if declared_trigger != spec["trigger"]:
            return {
                "success": False,
                "error": f"法术[{spell_name}]触发条件为[{spec['trigger']}]，不能在[{declared_trigger}]发动",
            }
        if spec["trigger"] not in VALID_SPELL_TRIGGERS:
            return {"success": False, "error": f"非法触发时点：{spec['trigger']}"}

        target = None
        if params.get("target"):
            target = self._find_entity(params["target"])
            if target is None:
                return {"success": False, "error": f"目标不存在: {params['target']}"}

        steps = spec["steps"]
        is_loop = spec.get("loop", False)
        max_cycles = params.get("max_cycles", 50)

        all_step_results = []
        cycles_run = 0
        interrupted = False
        interrupt_reason = ""

        while True:
            cycles_run += 1
            previous_step_damage = None

            for step in steps:
                # 条件步骤
                cond = step.get("condition")
                if cond == "target_flying":
                    flying = bool(target and (target.is_flying or target.has_status("飞行") or target.has_status("滑翔")))
                    if not flying:
                        all_step_results.append({"step": step["daowen"], "skipped": "条件不满足（目标未飞行）"})
                        continue
                elif cond == "previous_step_no_damage":
                    if previous_step_damage is None or previous_step_damage > 0:
                        all_step_results.append({"step": step["daowen"], "skipped": "条件不满足（上一步已造成伤害）"})
                        continue

                # X 取值
                x_param = step.get("x_param", "x")
                if x_param == "3x":
                    x = params.get("x", 1) * 3
                else:
                    x = params.get(x_param, 0)
                    if x < 1:
                        interrupted = True
                        interrupt_reason = f"步骤[{step['daowen']}]未提供有效的{x_param}（X≥1）"
                        break

                step_target = player if step.get("target") == "self" else target
                if step_target is None:
                    interrupted = True
                    interrupt_reason = f"步骤[{step['daowen']}]缺少目标"
                    break
                if step_target is not player and not step_target.is_alive:
                    interrupted = True
                    interrupt_reason = f"目标已死亡"
                    break

                daowen_name = step["daowen"]
                calc_key = daowen_name

                # 积木法则：法术不得凭空创造新机制，逐步调用道纹原版公式
                if daowen_name not in player.dao_wen:
                    interrupted = True
                    interrupt_reason = f"未持有道纹【{daowen_name}】，法术流程中断"
                    break

                try:
                    calc = DaoWenEngine.resolve(
                        daowen_name, x,
                        target=step_target, caster=player,
                        _state=self._caster_state_dict(player),
                    )
                except ValueError as e:
                    interrupted = True
                    interrupt_reason = f"道纹【{daowen_name}】计算失败：{e}，中断"
                    break

                # 中断法则：法力耗尽则中断
                cost = calc.get("cost", 0)
                if calc.get("cost_type") == "消耗" and cost > 0:
                    if player.current_mana < cost:
                        interrupted = True
                        interrupt_reason = f"法力耗尽（需{cost}，余{player.current_mana}），法术流程中断"
                        break
                    player.spend_mana(cost)

                # 代价支付（流血等）；支付后死亡则由死亡检查接管
                cost_applied = self._apply_daowen_cost(calc, player, player.dao_wen.get(daowen_name))
                if not player.is_alive:
                    interrupted = True
                    interrupt_reason = "支付代价后[命零]，法术流程中断"
                    break

                execution = self._execute_daowen_effect(daowen_name, calc, player, step_target)

                step_damage = 0
                if execution.get("effects"):
                    for e in execution["effects"]:
                        step_damage += e.get("actual_damage", 0) or 0
                previous_step_damage = step_damage

                all_step_results.append({
                    "step": daowen_name,
                    "x": x,
                    "calculation": calc.get("summary"),
                    "cost_applied": cost_applied,
                    "execution": execution.get("effects", []),
                })

            if interrupted or not is_loop:
                break
            if cycles_run >= max_cycles:
                interrupt_reason = f"达到最大循环数{max_cycles}，强制中断（防止死循环，实际应更早因资源耗尽中断）"
                break

            # 循环法则的延续条件：下一轮必须仍能支付代价/消耗
            first_step = steps[0]
            first_x = params.get(first_step.get("x_param", "x"), 0)
            if first_step.get("x_param") == "3x":
                first_x = params.get("x", 1) * 3
            try:
                preview_calc = DaoWenEngine.resolve(
                    first_step["daowen"], first_x,
                    target=player if first_step.get("target") == "self" else target,
                    caster=player, _state=self._caster_state_dict(player),
                )
                ct = preview_calc.get("cost_type")
                can_continue = True
                if ct == "消耗" and player.current_mana < preview_calc.get("cost", 0):
                    can_continue = False
                elif ct == "流血" and player.current_hp <= preview_calc.get("cost_hp", 0):
                    can_continue = False
                if not can_continue:
                    interrupted = True
                    interrupt_reason = f"资源不足以支撑下一循环（{ct}），按中断法则终止"
                    break
            except Exception:
                break

        return {
            "success": True,
            "action": f"发动法术【{spell_name}】",
            "trigger_timing": declared_trigger,
            "cycles": cycles_run,
            "steps_executed": all_step_results,
            "interrupted": interrupted,
            "interrupt_reason": interrupt_reason or None,
            "player_mana_after": player.current_mana,
            "note": "积木法则：逐步骤调用持有道纹原版公式；中断法则：法力耗尽或流程失效即中断",
        }

    # ==================== 战斗：残韵 ====================

    def _action_use_resonance(self, params: dict) -> dict:
        """
        使用残韵（转换/反转/曲解），作用于一条正在发动或可变化的道纹路径：
        - 作用于轮回者自己拥有的道纹：该道纹永久变为变化后的道纹（施法者不获得）
        - 作用于非轮回者拥有的道纹（如残韵改写怪物面板道纹）：仅改变本次结算，
          结算完成后施法者永久获得变化后的道纹（未实装的战斗内插结算另行中断，由DM处理）
        """
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}

        source = params.get("source_daowen", "")
        rtype = params.get("resonance_type", "")

        if self.state.resonance.get(rtype, 0) <= 0:
            return {"success": False, "error": f"没有可用的{rtype}残韵（当前：{self.state.resonance}）"}

        target_name = params.get("on_monster", "")  # 作用于怪物面板道纹时提供怪物名
        monster = None
        if target_name:
            monster = next((m for m in self.state.enemies if m.name == target_name), None)
            if monster is None:
                return {"success": False, "error": f"怪物不存在: {target_name}"}
            if source not in monster.dao_wen:
                return {"success": False, "error": f"{target_name}的面板上没有道纹【{source}】"}
            caster_has = False
        else:
            if source not in player.dao_wen:
                return {"success": False, "error": f"玩家未持有道纹【{source}】（作用于怪物道纹请提供 on_monster）"}
            caster_has = True

        result = ResonanceEngine.apply_resonance(
            source, rtype,
            caster_has_daowen=caster_has,
            target_has_daowen=True,
            resonance_stock=self.state.resonance,
        )
        if not result["success"]:
            return {"success": False, "error": result["error"]}

        new_name = result["target"]

        from .gamedata import IMPLEMENTED_DAOWEN
        if new_name not in IMPLEMENTED_DAOWEN:
            return {
                "success": False,
                "unavailable": True,
                "error": f"残韵变化结果【{new_name}】机制未实装，引擎拒绝生成无法生效的道纹",
            }

        # 消耗残韵（残韵未生效则不消耗，此处路径与实装均已确认）
        self.state.resonance[rtype] -= 1

        if caster_has:
            # 永久变化：轮回者拥有的对应道纹永久变为变化后的道纹
            if new_name in player.dao_wen:
                return {"success": False, "error": f"已持有【{new_name}】（道纹唯一）"}
            del player.dao_wen[source]
            player.dao_wen[new_name] = DaoWenInstance(dao_wen=self._build_daowen_def(new_name))
            effect_note = f"玩家持有的【{source}】永久变为【{new_name}】"
        else:
            # 仅改变面板（怪物）道纹：面板替换，施法者永久获得变化后的道纹
            if new_name not in monster.dao_wen:
                from .models import DaoWenInstance as _DI
                del monster.dao_wen[source]
                monster.dao_wen[new_name] = _DI(dao_wen=self._build_daowen_def(new_name))
            if new_name in player.dao_wen:
                effect_note = f"(已持有【{new_name}】，按道纹唯一不再重复获得)"
            else:
                player.dao_wen[new_name] = DaoWenInstance(dao_wen=self._build_daowen_def(new_name))
                effect_note = f"施法者永久获得变化后的道纹【{new_name}】"

        return {
            "success": True,
            "action": f"残韵【{rtype}】{source} → {new_name}",
            "result": result,
            "effect": effect_note,
            "resonance_remaining": self.state.resonance,
        }

    # ==================== 战斗：攻击与闪避 ====================

    def _action_attack(self, params: dict) -> dict:
        """普通攻击（真实结算，目标可闪避）"""
        player = self.state.player
        if not player or not player.is_alive:
            return {"success": False, "error": "轮回者不可用"}

        if player.has_status("束缚") or player.has_status("眩晕"):
            return {"success": False, "error": "处于无法行动状态（束缚/眩晕）"}

        budget = self._player_action_budget()
        if self.state.actions_used >= budget:
            return {"success": False, "error": f"出手次数已用完（{self.state.actions_used}/{budget}）"}

        target_name = params.get("target", "")
        target = self._find_entity(target_name)
        if target is None or target not in self.state.enemies or not target.is_alive:
            return {"success": False, "error": f"目标无效: {target_name}"}

        if CombatEngine.is_flying(target) and not CombatEngine.is_flying(player):
            return {"success": False, "error": f"{target.name}处于飞行状态，无法被非飞行角色选为目标"}

        is_must_hit = player.has_status("必中")
        dodge = params.get("target_dodge", False) and target.current_speed >= 1
        result = self.combat.resolve_attack(player, target, is_must_hit=is_must_hit, dodge=dodge)
        self.state.actions_used += 1

        return {
            "success": True,
            "action": f"{player.name}攻击{target.name}",
            "result": result,
            "budget": {"used": self.state.actions_used, "total": budget},
        }

    def _action_dodge_decision(self, params: dict) -> dict:
        """闪避决策（逐次结算一次攻击）"""
        target_name = params.get("target", self.state.player.name if self.state.player else "")
        dodge = params.get("dodge", False)
        attacker_name = params.get("attacker", "")
        is_must_hit = params.get("is_must_hit", False)

        target = self._find_entity(target_name)
        attacker = self._find_entity(attacker_name)

        if not target or not attacker:
            return {"success": False, "error": "目标或攻击者不存在"}

        result = self.combat.resolve_attack(attacker, target, is_must_hit=is_must_hit, dodge=dodge)

        # 避风铃：每次闪避后获得3点格挡；当前速度归零时获得15点格挡
        if result.get("dodge_success") and target is self.state.player and self._has_relic("避风铃"):
            target.gain_shield(3)
            result["relic_避风铃"] = "+3格挡"
            if target.current_speed <= 0 and not self.state.relic_flags.get("避风铃_已触发15"):
                self.state.relic_flags["避风铃_已触发15"] = True
                target.gain_shield(15)
                result["relic_避风铃"] = "+3格挡，速度归零再+15格挡"

        return {
            "success": True,
            "action": f"闪避决策：{'闪避' if dodge else '承受'}",
            "result": result,
        }

    # ==================== 战斗：怪物回合 ====================

    def _action_monster_turn(self, params: dict) -> dict:
        """
        一只怪物的完整出手轮（AI扮演怪物做出最优决策，引擎只结算）
        params:
          monster: 怪物名
          acts: 行动列表，数量=当前回合数÷3向上取整：
            - {"type": "attack_round", "target": "<目标名>", "dodges": [true/false, ...]}
              一轮攻击：连续攻击次数次，每次独立闪避判定
            - {"type": "use_daowen", "daowen": "<名>", "x": N, "target": "<名>", "target_dodge": true/false}
              发动面板道纹（不支付法力，只消耗出手；代价道纹照常支付代价）
        """
        monster_name = params.get("monster", "")
        monster = next((m for m in self.state.enemies if m.name == monster_name and m.is_alive), None)
        if not monster:
            return {"success": False, "error": f"怪物不存在或已死亡: {monster_name}"}

        # 行动禁止判定：束缚（无法行动）/ 眩晕（无法出手，受到伤害后解除）
        if monster.has_status("束缚"):
            return {"success": True, "action": f"{monster.name}的出手轮",
                    "skipped": "束缚：无法行动", "turn_log": []}
        if monster.has_status("眩晕"):
            return {"success": True, "action": f"{monster.name}的出手轮",
                    "skipped": "眩晕：无法出手", "turn_log": []}

        acts = params.get("acts", [])
        base_allowed = CombatEngine.monster_act_count(self.state.current_round)
        huoli = monster.get_status_value("活力")
        wuli = monster.get_status_value("无力")
        allowed = max(0, base_allowed + huoli - wuli)

        # 缓慢X：本回合若目标单轮出手次数≤X，则其无法出手
        if monster.has_status("缓慢"):
            threshold = monster.get_status_value("缓慢")
            if allowed <= threshold:
                return {"success": True, "action": f"{monster.name}的出手轮",
                        "skipped": f"缓慢：出手次数{allowed}≤{threshold}，本回合无法出手",
                        "turn_log": []}

        has_kuangbao = monster.has_status("狂暴")
        max_acts = allowed + (1 if has_kuangbao else 0)

        if not acts:
            if max_acts <= 0:
                return {"success": True, "action": f"{monster.name}的出手轮",
                        "skipped": "无出手次数", "turn_log": []}
            return {"success": False, "error": "未提供行动（禁止在数值未耗尽时坐以待毙）"}
        if len(acts) > max_acts:
            return {
                "success": False,
                "error": f"行动数超限：本回合最多{allowed}次出手"
                         + ("+1次狂暴额外攻击" if has_kuangbao else "")
                         + f"，提供了{len(acts)}个",
            }
        if has_kuangbao and len(acts) > allowed:
            # 狂暴提供的额外行为仅限一轮攻击
            extras = acts[allowed:]
            if any(a.get("type") != "attack_round" for a in extras):
                return {"success": False, "error": "狂暴的额外出手只能是【一轮攻击】"}

        turn_log = []
        for act in acts:
            act_type = act.get("type")

            if act_type == "attack_round":
                target_name = act.get("target", self.state.player.name if self.state.player else "")
                target = self._find_entity(target_name)
                if target is None or not target.is_alive:
                    turn_log.append({"type": "attack_round", "error": f"目标无效: {target_name}"})
                    continue
                if CombatEngine.is_flying(target) and not CombatEngine.is_flying(monster):
                    turn_log.append({"type": "attack_round", "error": f"{target.name}飞行中，无法选为目标"})
                    continue
                # 迟滞：攻击次数固定为1
                hit_total = 1 if monster.has_status("迟滞") else monster.attack_count
                dodges = act.get("dodges", [])
                is_must_hit = monster.has_status("必中")
                hits = []
                for hit_idx in range(hit_total):
                    if not target.is_alive:
                        break
                    dodge_choice = dodges[hit_idx] if hit_idx < len(dodges) else False
                    hit_result = self.combat.resolve_attack(
                        monster, target, hit_index=hit_idx,
                        is_must_hit=is_must_hit, dodge=dodge_choice,
                    )
                    # 避风铃（轮回者闪避）
                    if hit_result.get("dodge_success") and target is self.state.player \
                            and self._has_relic("避风铃"):
                        target.gain_shield(3)
                        hit_result["relic_避风铃"] = "+3格挡"
                        if target.current_speed <= 0 and not self.state.relic_flags.get("避风铃_已触发15"):
                            self.state.relic_flags["避风铃_已触发15"] = True
                            target.gain_shield(15)
                            hit_result["relic_避风铃"] = "+3格挡，速度归零再+15格挡"
                    hits.append(hit_result)
                turn_log.append({
                    "type": "attack_round",
                    "target": target.name,
                    "hits": hits,
                    "hits_landed": sum(1 for h in hits if h.get("damage_dealt", 0) > 0),
                })

            elif act_type == "use_daowen":
                r = self._use_daowen_core(
                    caster=monster,
                    name=act.get("daowen", ""),
                    x=act.get("x", 1),
                    target_name=act.get("target", monster.name),
                    target_dodge=act.get("target_dodge", False),
                    consume_action=False,
                    skip_mana_cost=True,   # 怪物发动自身面板道纹不支付法力，只消耗出手
                    extra=act.get("extra") or act,
                )
                turn_log.append({"type": "use_daowen", **r})

            else:
                turn_log.append({"type": act_type, "error": "未知怪物行动类型（attack_round/use_daowen）"})

            if self.state.player and not self.state.player.is_alive:
                break

        return {
            "success": True,
            "action": f"{monster.name}的出手轮",
            "round": self.state.current_round,
            "acts_used": len(acts),
            "acts_allowed": max_acts,
            "turn_log": turn_log,
            "player_hp": self.state.player.current_hp if self.state.player else None,
        }

    # ==================== 战斗：撤退（买路财） ====================

    def _action_retreat(self, params: dict) -> dict:
        """
        买路财：战斗中可以失去等同于怪物20%[血限]的[碎片]安全撤退；
        碎片不足时，可以其他代价补足（1[碎片]=2生命=1[血限]）
        """
        if not self._has_relic("买路财"):
            return {"success": False, "error": "未持有遗物【买路财】"}
        if self.state.phase not in ("in_combat",):
            return {"success": False, "error": "仅战斗中可撤退"}

        alive = [m for m in self.state.enemies if m.is_alive]
        cost = sum(math.ceil(m.blood_limit * 0.2) for m in alive)

        pay_shards = min(self.state.shards, max(0, cost))
        remainder = cost - pay_shards

        hp_pay = params.get("hp_pay", 0)
        blood_pay = params.get("blood_limit_pay", 0)
        cover = math.ceil(hp_pay / 2) + blood_pay
        if remainder > 0 and cover < remainder:
            return {
                "success": False,
                "error": f"撤退需{cost}碎片等价物：碎片{pay_shards}可抵，剩余{remainder}需 hp_pay(2:1)+blood_limit_pay(1:1) 补足，当前提供{cover}",
                "required": {"shards_part": pay_shards, "remainder": remainder},
            }

        player = self.state.player
        self.state.shards -= pay_shards
        if hp_pay:
            player.take_damage(hp_pay, "代价")
        if blood_pay:
            player.blood_limit -= blood_pay
            player.current_hp = min(player.current_hp, player.blood_limit)

        if not player.is_alive:
            return {"success": False, "error": "补足撤退代价后[命零]，撤退失败"}

        return self._settle_battle_end(escaped=True, retreat_detail={
            "cost_shards_equivalent": cost,
            "shards_paid": pay_shards,
            "hp_paid": hp_pay,
            "blood_limit_paid": blood_pay,
        })

    # ==================== 回合与战终 ====================

    def _action_round_start(self, params: dict) -> dict:
        """回始"""
        result = self.combat.round_start()
        return {"success": True, "action": "回始", "result": result}

    def _action_round_end(self, params: dict) -> dict:
        """回终"""
        result = self.combat.round_end()

        # 抵扣封印的遗物计时推进
        sealed = self.state.relic_flags.get("抵扣_封印", {})
        for relic_name in list(sealed.keys()):
            sealed[relic_name] -= 1
            if sealed[relic_name] <= 0:
                del sealed[relic_name]

        # 检查怪物困境
        difficulties = []
        for monster in self.state.enemies:
            if monster.is_alive:
                diff = self.combat.check_monster_difficulty(monster)
                if diff:
                    difficulties.append(diff)

        # 战斗胜负检查
        battle_finished = False
        if self.state.player and not self.state.player.is_alive:
            battle_finished = True
        if not self.state.get_all_enemy_side():
            battle_finished = True

        return {
            "success": True,
            "action": "回终",
            "result": result,
            "monster_difficulties": difficulties,
            "battle_finished": battle_finished,
            "next": "battle_end" if battle_finished else "round_start",
        }

    def _settle_battle_end(self, escaped: bool = False, retreat_detail: dict = None) -> dict:
        """战终结算的公共实现"""
        # 碎片奖励：怪物[战始][血限]×2%＋死亡时拥有的道纹数×5（仅[命零]击杀有奖励）
        shard_reward = 0
        reward_detail = []
        for monster in self.state.enemies:
            if not monster.is_alive and not escaped:
                base = math.ceil((monster.spawn_blood_limit or monster.blood_limit) * 0.02)
                bonus = len(monster.dao_wen) * 5
                moneybag = 0
                if self._has_relic("钱袋"):
                    moneybag = math.ceil((monster.spawn_blood_limit or monster.blood_limit) * 0.02)
                total = base + bonus + moneybag
                shard_reward += total
                reward_detail.append({
                    "monster": monster.name,
                    "base_2pct_spawn_blood": base,
                    "daowen_bonus": bonus,
                    "moneybag_bonus": moneybag,
                    "total": total,
                })

        self.state.shards += shard_reward

        # 清除局内增益与减益（格挡/法力/持续效果），代价造成的属性损失保留
        cleared = []
        player_side = ([self.state.player] if self.state.player else []) \
            + self.state.friends + self.state.employees + self.state.temp_friends
        for e in player_side:
            if e is None:
                continue
            if e.shield:
                e.clear_shield()
            if e.status_effects:
                e.status_effects.clear()
            if e.current_speed != e.speed_limit:
                e.current_speed = e.speed_limit  # 闪避消耗的速度战终复原
            cleared.append(e.name)
        for m in self.state.enemies:
            m.shield = 0
            m.status_effects.clear()

        # 临时朋友消失
        self.state.temp_friends.clear()

        # 冷却推进：战终后已完成战斗场数+1
        for e in player_side:
            if e is None:
                continue
            for dw in e.dao_wen.values():
                if dw.cooldown_remaining > 0:
                    dw.cooldown_remaining -= 1

        # 恢复精力
        self.state.energy = 3

        # 假碎片清空（战终清除局内资源）
        self.state.fake_shards = 0

        result_body = {
            "escaped": escaped,
            "retreat": retreat_detail,
            "shard_reward": shard_reward,
            "reward_detail": reward_detail,
            "total_shards": self.state.shards,
            "cleared_entities": cleared,
            "energy_restored": 3,
        }

        # 清空敌人
        self.state.enemies.clear()

        # 员工叛变检查
        mutiny = self._check_staff_mutiny()
        if mutiny:
            result_body["staff_mutiny"] = mutiny

        # 最终的冠冕：完成第7场后触发
        crown = None
        if self.state.current_battle >= REGION_BATTLE_COUNT and self.state.player and self.state.player.is_alive:
            crown = self._trigger_crown()
            result_body["crown"] = crown
            self.state.phase = "dead_duel" if crown.get("duel") else "game_over"
        else:
            self.state.phase = "pre_battle"

        return {
            "success": True,
            "action": "战终" + ("（撤退）" if escaped else ""),
            "result": result_body,
            "phase": self.state.phase,
        }

    def _action_battle_end(self, params: dict) -> dict:
        """战终"""
        if self.state.phase not in ("in_combat", "dead_duel"):
            return {"success": False, "error": f"当前阶段({self.state.phase})不能执行战终"}
        escaped = params.get("escaped", False)
        return self._settle_battle_end(escaped=escaped)

    def _check_staff_mutiny(self) -> Optional[dict]:
        """员工叛变检查（战终）"""
        employees = [e for e in self.state.employees if e.is_alive]
        if not employees:
            return None

        player = self.state.player
        emp_total = sum(e.attack_count * e.attack_power for e in employees)
        friend_total = sum(f.attack_count * f.attack_power for f in self.state.friends if f.is_alive)
        threshold = (player.current_hp if player else 0) + friend_total

        if emp_total >= threshold:
            interrupt = Interrupt(
                interrupt_type=InterruptType.STAFF_MUTINY,
                context={
                    "employees_attack_total": emp_total,
                    "player_hp": player.current_hp if player else 0,
                    "friends_attack_total": friend_total,
                },
                description=(
                    f"员工攻击总值{emp_total} ≥ 轮回者当前生命{player.current_hp if player else 0}+朋友攻击总值{friend_total}，"
                    f"所有员工共同叛变夺取《死者之书》！\n"
                    f"选项：镇压（与叛变员工开战）/ 让利（每场工资+5）/ 急中生智（谈判方案，DM裁定）"
                ),
                options=[
                    {"id": "suppress", "label": "镇压", "description": "与所有叛变员工开启战斗"},
                    {"id": "concede", "label": "让利", "description": "本次轮回所有员工每场工资+5，叛变平息"},
                    {"id": "wit", "label": "急中生智", "description": "给出谈判方案破解叛乱（DM裁定）"},
                ],
                state_snapshot=self.state.to_dict(),
            )
            self._pending_interrupts.append(interrupt)
            return {"triggered": True, "employees_attack_total": emp_total, "threshold": threshold}
        return None

    def _trigger_crown(self) -> dict:
        """最终的冠冕：封存候选 或 开启死斗"""
        if self.state.sealed_candidate is None:
            # 完整封存本次轮回
            snapshot = {
                "player": self.state.player.to_dict() if self.state.player else None,
                "player_daowen": list(self.state.player.dao_wen.keys()) if self.state.player else [],
                "player_spells": [s.name for s in self.state.player.spells] if self.state.player else [],
                "friends": [f.to_dict() for f in self.state.friends],
                "shards": self.state.shards,
                "relics": [r.name for r in self.state.relics],
                "resonance": dict(self.state.resonance),
                "attribute_points": self.state.attribute_points,
                "saved_at_battle": self.state.current_battle,
            }
            self.state.sealed_candidate = snapshot
            return {
                "duel": False,
                "sealed": True,
                "note": "本次轮回已完整封存为冠冕候选。下一名轮回者完成第7场后将与其死斗。",
            }

        # 已有封存候选 → 第8场·最终死斗
        candidate = self._materialize_candidate(self.state.sealed_candidate)
        self.state.enemies = [candidate]
        self.state.phase = "dead_duel"
        return {
            "duel": True,
            "note": "最终死斗开启：无法逃跑，只有胜者能进入下一阶副本",
            "candidate": candidate.to_dict(),
            "order_rule": "先手顺序：[速限]→[法限]→[血限]→当前生命",
        }

    def _materialize_candidate(self, snapshot: dict) -> Entity:
        """将封存的冠冕候选实体化为死斗对手"""
        p = snapshot.get("player") or {}
        entity = Entity(
            name=p.get("name", "封存候选"),
            entity_type=EntityType.REINCARNATOR.value,
            blood_limit=p.get("blood_limit", 60),
            current_hp=p.get("current_hp", p.get("blood_limit", 60)),
            mana_limit=p.get("mana_limit", 10),
            current_mana=p.get("mana_limit", 10),
            speed_limit=p.get("speed_limit", 5),
            current_speed=p.get("speed_limit", 5),
            attack_count=p.get("attack_count", 1),
            attack_power=p.get("attack_power", 1),
        )
        for dw_name in snapshot.get("player_daowen", []):
            entity.dao_wen[dw_name] = DaoWenInstance(dao_wen=self._build_daowen_def(dw_name))
        return entity

    # ==================== 中断声明 ====================

    def _action_declare_wit(self, params: dict) -> dict:
        """声明急中生智"""
        player = self.state.player
        target_name = params.get("target", "")
        target = self._find_entity(target_name)

        if not target:
            return {"success": False, "error": "目标不存在"}

        interrupt = self.combat.initiate_wit(player, target)
        self._pending_interrupts.append(interrupt)

        return {
            "success": True,
            "action": "声明急中生智",
            "interrupt": interrupt.to_dict(),
            "instruction": "需要DM裁定急中生智方案",
        }

    def _action_declare_escape(self, params: dict) -> dict:
        """声明逃跑"""
        if self.state.phase == "dead_duel":
            return {"success": False, "error": "死斗无法逃跑"}
        escaper = self.state.player
        pursuers = self.state.get_all_enemy_side()

        interrupt = self.combat.initiate_escape(escaper, pursuers)
        self._pending_interrupts.append(interrupt)

        return {
            "success": True,
            "action": "声明逃跑",
            "interrupt": interrupt.to_dict(),
            "instruction": "需要DM裁定逃跑方案",
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
            "instruction": "需要DM裁定进化特性",
        }

    # ==================== DM裁定接口 ====================

    def submit_ruling(
        self,
        interrupt_type: str,
        ruling_text: str,
        ruling_data: dict = None,
        tags: list[str] = None,
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

        ruling = DMRuling(
            interrupt_type=interrupt_type,
            context=interrupt.context,
            ruling_text=ruling_text,
            ruling_data=ruling_data or {},
            tags=tags or [],
        )

        ruling_id = self.rulings_db.save_ruling(ruling)

        applied = None
        data = ruling_data or {}

        # 死之传承：遗言入死者之书，本次轮回终结
        if interrupt_type == "死之传承":
            wisdom = (ruling_text or "").strip()[:20]
            if wisdom:
                self.state.death_book_wisdom.append(wisdom)
            self.state.phase = "game_over"
            applied = {"wisdom_saved": wisdom or None, "run_ended": True}

        # 员工叛变
        elif interrupt_type == "员工叛变":
            choice = data.get("choice", "")
            if choice == "concede":
                self.state.employee_wage_bonus += 5
                applied = {"choice": "让利", "wage_bonus": self.state.employee_wage_bonus}
            elif choice == "suppress":
                # 与叛变员工开战
                self.state.enemies = [e for e in self.state.employees if e.is_alive]
                self.state.employees = []
                self.state.phase = "in_combat"
                applied = {"choice": "镇压", "battle_resumed": [e.name for e in self.state.enemies]}
            else:
                applied = {"choice": choice or "急中生智", "note": "由DM裁定结果，引擎不臆造数值效果"}

        return {
            "success": True,
            "action": "DM裁定",
            "ruling_id": ruling_id,
            "interrupt_type": interrupt_type,
            "ruling_text": ruling_text,
            "ruling_data": ruling_data,
            "applied": applied,
            "note": "裁定已保存，下次类似场景将自动匹配",
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
            "instruction": "如果有匹配的先例，可以直接应用；否则需要DM新裁定",
        }

    # ==================== 随机数接口 ====================

    def _action_submit_random(self, params: dict) -> dict:
        """
        提交玩家随机数（随机数规则的唯一入口）
        若存在待解决的随机请求（出怪/遗物候选），按用途路由结算
        """
        pool_name = params.get("pool_name", "")
        number = params.get("number", 0)

        pending = self._pending_random
        if pending is not None:
            purpose = pending["purpose"]
            meta = pending["meta"]

            if purpose == "spawn_monster":
                pool = meta["pool"]
                if not 1 <= number <= len(pool):
                    return {"success": False, "error": f"数字{number}超出范围 1~{len(pool)}"}
                meta["draws"].append(number)
                remaining = meta["count"] - len(meta["draws"])
                # 池允许重复抽选同一怪物种族，不消耗选项
                if remaining > 0:
                    return {
                        "success": True,
                        "action": f"抽取第{len(meta['draws'])}只怪",
                        "drawn": pool[number - 1],
                        "draws_so_far": [pool[i - 1] for i in meta["draws"]],
                        "random_required": True,
                        "range": f"1~{len(pool)}",
                        "instruction": f"还需{remaining}只，请继续给出数字",
                    }
                # 抽满，实体化
                self._pending_random = None
                return self._finish_spawn(meta)

            if purpose == "discover_relic_candidates":
                pool = meta["pool"]
                if not 1 <= number <= len(pool):
                    return {"success": False, "error": f"数字{number}超出范围 1~{len(pool)}"}
                # 以玩家数字为起点顺序取3个候选
                rotated = pool[number - 1:] + pool[:number - 1]
                candidates = rotated[:3]
                self._pending_random = None
                self.dice.clear_pool(pending["pool_name"])
                return {
                    "success": True,
                    "action": "发现遗物（候选已抽出）",
                    "candidates": candidates,
                    "instruction": f"发现机制：从{candidates}中选择1件，调用 discover_relic_setup 并传入 chosen=<遗物名>",
                }

        # 通用池结算
        try:
            result = self.dice.resolve_pool(pool_name, number)
            return {"success": True, "action": "随机数提交", "result": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def request_random(self, pool_name: str, options: list[Any]) -> dict:
        """
        请求随机数
        AI调用此方法创建随机池，然后必须向玩家索取数字
        """
        return self.dice.create_pool(pool_name, options)

    # ==================== 共鸣候选选择（discover_relic 二阶段） ====================

    def _action_choose_relic_candidate(self, params: dict) -> dict:
        chosen = params.get("chosen", "")
        return self._resolve_discover_relic(chosen)

    # ==================== 存档系统 ====================

    def save_game(self, slot: str = "auto") -> dict:
        """保存游戏"""
        save_data = {
            "state": self.state.to_dict(),
            "action_history": self._action_history,
            "dice_history": self.dice.get_history(),
            "rulings": [r.to_dict() for r in self.rulings_db.get_all_rulings()],
            "timestamp": time.time(),
        }

        filepath = os.path.join(self.save_dir, f"save_{slot}.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(save_data, f, ensure_ascii=False, indent=2)

        return {"success": True, "filepath": filepath}

    def load_game(self, slot: str = "auto") -> dict:
        """加载游戏"""
        filepath = os.path.join(self.save_dir, f"save_{slot}.json")

        if not os.path.exists(filepath):
            return {"success": False, "error": f"存档不存在: {filepath}"}

        with open(filepath, "r", encoding="utf-8") as f:
            save_data = json.load(f)

        state_data = save_data["state"]
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
