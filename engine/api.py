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
from .events import (
    EVENT_POOL_UNIVERSAL, EVENT_POOL_REGION, EVENT_FRIENDS,
    EVENT_RELICS, CONSUMABLES, TOOL_LIBRARY,
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
                {"id": "学习", "cost_energy": 1, "available": True, "description": "学会1/2(10)/3(25)种法术，或自创一种法术（完全由已拥有道纹按三大法则组装），或习得1/2(10)种转化道纹（须为已持有道纹的相邻变化）"},
                {"id": "revise_custom_spell", "cost_energy": 0,
                 "available": bool(self.state.player and any(s.spec for s in self.state.player.spells)),
                 "description": "修订自创法术（[战终]窗口；须通过创建同款校验，以当前持有道纹为准）"},
                {"id": "共鸣", "cost_energy": 2, "available": bool(self.state.relics_pool),
                 "description": "精力再次-1，发现/自选(15碎片)一件遗物"},
                {"id": "探索", "cost_energy": 1,
                 "available": bool(self._current_event_pool()),
                 "unavailable_reason": None if self._current_event_pool() else "当前事件池已空（全部已遇到或条件不满足）",
                 "description": "发现 1 个 / 2个（30碎片）未遇到事件；事件必须作出选择（含拒绝类选项；拒绝可触发【无所求】）"},
                {"id": "忘忧", "cost_energy": 1, "available": self._has_relic("忘忧香"),
                 "unavailable_reason": None if self._has_relic("忘忧香") else "需持有遗物【忘忧香】",
                 "description": "遗物【忘忧香】自有行动：失忆1/2/3，获得30/55/80碎片（forget_names指定失去的道纹）"},
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
            return [{"id": "维修", "available": bool(self.state.consumables),
                     "unavailable_reason": None if self.state.consumables else "没有可维修的消耗品（探索事件可附赠工具库发现）",
                     "description": "获得1/2(5碎片)/3(12)点【耐久分配】，分配给自己拥有的消耗品，不超过其耐久上限"}]
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

        # 事件必须作出选择后才能继续其他行动（事件不能被搁置）
        if self.state.pending_event and action_type not in ("choose_event_option", "random_number"):
            return {
                "success": False,
                "error": "有等待选择的事件，必须先通过 choose_event_option 作出选择",
                "pending_event": self.state.pending_event,
            }

        try:
            handler = {
                "setup_attributes": self._action_setup_attributes,
                "setup_choose_daowen": self._action_setup_choose_daowen,
                "setup_choose_resonance": self._action_setup_choose_resonance,
                "setup_choose_region": self._action_setup_choose_region,
                "discover_relic_setup": self._action_discover_relic,
                "pre_battle_action": self._action_pre_battle,
                "choose_event_option": self._action_choose_event_option,
                "use_consumable": self._action_use_consumable,
                "spend_attribute_points": self._action_spend_attribute_points,
                "use_daowen": self._action_use_daowen,
                "use_spell": self._action_use_spell,
                "use_resonance": self._action_use_resonance,
                "attack": self._action_attack,
                "dodge_decision": self._action_dodge_decision,
                "monster_turn": self._action_monster_turn,
                "friend_turn": self._action_friend_turn,
                "retreat": self._action_retreat,
                "declare_wit": self._action_declare_wit,
                "declare_escape": self._action_declare_escape,
                "declare_evolution": self._action_declare_evolution,
                "round_start": self._action_round_start,
                "round_end": self._action_round_end,
                "battle_start": self._action_battle_start,
                "battle_end": self._action_battle_end,
                "random_number": self._action_submit_random,
                "revise_custom_spell": self._action_revise_custom_spell,
            }.get(action_type)

            if handler is None:
                result = {"success": False, "error": f"未知或未实装的行动类型: {action_type}"}
            else:
                result = handler(params)

            # 行动后果检查（轮回者死亡/异变化）
            if action_type in ("use_daowen", "use_spell", "attack", "dodge_decision",
                               "monster_turn", "friend_turn", "round_end", "retreat",
                               "choose_event_option", "use_consumable"):
                death_note = self._check_player_death()
                if death_note and isinstance(result, dict):
                    result["death_note"] = death_note
                # 焦黑发丝等"怪物死亡计数"类遗物钩子
                if isinstance(result, dict):
                    self._sync_monster_death_hooks(result)

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

        # 卖身契：[战始]指定的[朋友]/[员工]代替承担本场轮回者支付的【代价】，其[命零]后失效
        proxy = None
        if caster is self.state.player:
            pname = self.state.relic_flags.get("卖身契_friend")
            if pname:
                proxy = next((f for f in self.state.friends + self.state.employees
                              if f.name == pname and f.is_alive), None)
                if proxy is None:
                    self.state.relic_flags.pop("卖身契_friend", None)
                    applied.append({"type": "卖身契", "note": "指定对象已[命零]，效果失效，代价仍由自身承担"})
        if proxy is not None and cost_type in ("疲惫", "枯竭", "萎缩", "冷却", "唯一", "失忆"):
            applied.append({"type": "卖身契",
                            "note": f"{proxy.name}的面板无该代价维度（{cost_type}），无法转承，仍由自身支付（如实记录，待DM裁定）"})
            proxy = None

        if cost_type == "流血" and "cost_hp" in calc:
            payer = proxy or caster
            before = payer.current_hp
            res = payer.take_damage(calc["cost_hp"], "代价")
            applied.append({"type": "流血", "amount": calc["cost_hp"],
                            "hp": f"{before}→{res['hp_after']}",
                            **({"paid_by": f"卖身契→{payer.name}"} if proxy else {})})
            # 血誓戒：回始首次主动支付流血代价时获得等量格挡；支付后生命≤30%改为获得等量生命
            # （卖身契转承时轮回者并未"支付"，不触发）
            if proxy is None and caster is self.state.player and self._has_relic("血誓戒") \
                    and not self.state.relic_flags.get("血誓戒_本回合已触发"):
                self.state.relic_flags["血誓戒_本回合已触发"] = True
                if caster.current_hp <= math.ceil(caster.blood_limit * 0.3):
                    heal = caster.heal(calc["cost_hp"])
                    applied.append({"type": "血誓戒", "effect": f"生命≤30%，获得{heal['actual_heal']}点回复"})
                else:
                    caster.gain_shield(calc["cost_hp"])
                    applied.append({"type": "血誓戒", "effect": f"获得{calc['cost_hp']}点格挡"})

        elif cost_type == "衰老" and "cost_blood_limit" in calc:
            payer = proxy or caster
            payer.blood_limit -= calc["cost_blood_limit"]
            payer.current_hp = min(payer.current_hp, payer.blood_limit)
            applied.append({"type": "衰老", "amount": calc["cost_blood_limit"],
                            "new_blood_limit": payer.blood_limit,
                            **({"paid_by": f"卖身契→{payer.name}"} if proxy else {})})

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
            _spd_before = caster.current_speed
            caster.current_speed = max(0, caster.current_speed - calc["cost_speed"])
            applied.append({"type": "疲惫", "amount": calc["cost_speed"],
                            "new_speed": caster.current_speed})
            if caster is self.state.player:
                note = self._huifengdao_on_speed_loss(_spd_before - caster.current_speed, source=None)
                if note:
                    applied.append({"type": "回锋刀", "note": note})

        elif cost_type == "异变" and "cost_mutation" in calc:
            payer = proxy or caster
            payer.mutation += calc["cost_mutation"]
            applied.append({"type": "异变", "amount": calc["cost_mutation"],
                            "mutation_total": payer.mutation,
                            **({"paid_by": f"卖身契→{payer.name}",
                                "warning": "异变达到50层时变为怪物" if payer.mutation >= 50 else ""}
                               if proxy else
                               {"warning": "异变达到50层时变为怪物" if payer.mutation >= 50 else ""})})
            # DM裁定（2026-07-31）：朋友/员工/临时朋友异变≥50一律异变为怪物——立即倒戈加入敌方
            if payer is not self.state.player and payer.entity_type != "怪物" \
                    and payer.is_alive and payer.mutation >= 50:
                applied.append(self._convert_friend_to_monster(payer))

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
            "忘忧": self._pre_battle_wangyou,
            "探索": self._pre_battle_tansuo,
            "维修": self._pre_battle_weixiu,
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

    # ==================== 事件系统（探索） ====================

    def _event_condition_met(self, cond: str) -> bool:
        """README 233行：已触发或条件不满足的事件不进入当前池"""
        if not cond:
            return True
        if cond == "has_glimmer_friend":
            return any(e.is_alive for e in
                       self.state.friends + self.state.employees + self.state.temp_friends)
        if cond == "daowen_ge_5":
            return self.state.player is not None and len(self.state.player.dao_wen) >= 5
        return True

    def _current_event_pool(self) -> list[dict]:
        """通用事件排列在前，当前区域专属事件排列在后（README 233行）"""
        pool = [e for e in EVENT_POOL_UNIVERSAL
                if not self.event_pool.is_encountered(e["id"]) and self._event_condition_met(e.get("condition"))]
        for e in EVENT_POOL_REGION.get(self.state.current_region, []):
            if not self.event_pool.is_encountered(e["id"]) and self._event_condition_met(e.get("condition")):
                pool.append(e)
        return pool

    def _event_def(self, eid: str) -> Optional[dict]:
        for e in EVENT_POOL_UNIVERSAL:
            if e["id"] == eid:
                return e
        for evs in EVENT_POOL_REGION.values():
            for e in evs:
                if e["id"] == eid:
                    return e
        return None

    def _pre_battle_tansuo(self, params: dict) -> dict:
        """探索（发现 1 个 / 2个（30碎片）未遇到事件）。发现用随机数=玩家数字。"""
        count = int(params.get("count", 1) or 1)
        if count not in (1, 2):
            self.state.energy += 1
            return {"success": False, "error": "探索只能发现 1 个 或 2个（30碎片）未遇到事件"}
        if self.state.pending_event:
            self.state.energy += 1
            return {"success": False, "error": "有未处理完的事件，请先 choose_event_option 处理"}
        if count == 2:
            if self.state.shards < 30:
                self.state.energy += 1
                return {"success": False, "error": "发现2个未遇到事件需要30[碎片]"}
        pool = self._current_event_pool()
        if not pool:
            self.state.energy += 1
            return {"success": False, "error": "当前事件池已空（全部事件均已遇到或条件不满足）"}
        if count == 2:
            self.state.shards -= 30
        self._pending_random = {
            "purpose": "explore",
            "pool_name": "event_pool",
            "meta": {"count": count, "pool": [e["id"] for e in pool], "draws": []},
        }
        return {
            "success": True,
            "action": f"探索（发现{count}个未遇到事件）",
            "random_required": True,
            "range": f"1~{len(pool)}",
            "instruction": f"当前事件池{len(pool)}个（通用在前，专属在后）：{[e['id'] for e in pool]}。"
                           f"请逐次给出 1~{len(pool)} 的数字（random_number），共{count}次",
            "energy_remaining": self.state.energy,
        }

    def _resolve_explore(self, meta: dict) -> dict:
        """抽满发现数后：登记事件队列（扭曲都市每完成一个事件附赠一次工具库发现）"""
        ids, draws = meta["pool"], meta["draws"]
        queue, seen = [], set()
        for d in draws:
            eid = ids[d - 1]
            if eid in seen:
                continue  # 重复数字=重复发现同一事件，如实只记一次（不虚增事件）
            seen.add(eid)
            self.event_pool.mark_encountered(eid)
            queue.append(eid)
            if self.state.current_region == "扭曲都市":
                queue.append("__tool_discovery__")
        self.state.pending_event = {"queue": queue, "current": None}
        return {
            "success": True,
            "action": "探索·发现",
            "events_found": [e for e in queue if e != "__tool_discovery__"],
            "tool_discovery_pending": "__tool_discovery__" in queue,
            "instruction": "事件已发现。请逐个调用 choose_event_option 查看并作出选择（不选择不能继续其他行动）",
        }

    def _action_choose_event_option(self, params: dict) -> dict:
        """
        对探索发现的事件作出选择。
        查询模式：不带 option_index/label 调用 → 返回当前事件的场景与全部选项。
        选择模式：option_index（0起）或 label + 收益所需参数。
        """
        pe = self.state.pending_event
        if not pe or not pe.get("queue"):
            return {"success": False, "error": "当前没有等待选择的事件"}
        eid = pe["queue"][0]
        pe["current"] = eid

        # 扭曲都市附赠：工具库发现（伪节点，遇之即发起随机）
        if eid == "__tool_discovery__":
            pe["queue"].pop(0)
            if not pe["queue"]:
                self.state.pending_event = None
            self._pending_random = {
                "purpose": "discover_tool",
                "pool_name": "tool_library",
                "meta": {"pool": list(TOOL_LIBRARY)},
            }
            return {
                "success": True,
                "action": "扭曲都市：探索完成事件，附赠【发现】一件废墟设施工具库消耗品",
                "random_required": True,
                "range": f"1~{len(TOOL_LIBRARY)}",
                "instruction": f"工具库共{len(TOOL_LIBRARY)}件：{list(TOOL_LIBRARY)}，请给出数字",
            }

        ev = self._event_def(eid)
        ops = ev["options"]
        option_index = params.get("option_index")
        label = params.get("label", "")
        if option_index is None and label:
            option_index = next((i for i, o in enumerate(ops) if o["label"] == label), None)

        if option_index is None:
            return {
                "success": True,
                "query": True,
                "event": eid,
                "desc": ev["desc"],
                "options": [{"index": i, "label": o["label"], "text": o.get("text", ""),
                             **({"needs_dm": o["needs_dm"]} if o.get("needs_dm") else {}),
                             **({"unavailable": o["unavailable"]} if o.get("unavailable") else {}),
                             **({"refuse": True} if o.get("refuse") else {})}
                            for i, o in enumerate(ops)],
                "instruction": "请用 option_index 或 label 作出选择（每个事件必须选择一个选项，不能跳过）",
            }

        if not 0 <= option_index < len(ops):
            return {"success": False, "error": f"选项序号{option_index}超出范围 0~{len(ops) - 1}"}
        opt = ops[option_index]

        if opt.get("needs_dm"):
            return {"success": False, "unavailable": True,
                    "error": f"【{opt['label']}】需要DM创造性裁定：{opt['needs_dm']}。程序拒绝假装成功，请选择其他选项"}
        if opt.get("unavailable"):
            return {"success": False, "unavailable": True,
                    "error": f"【{opt['label']}】当前不可用：{opt['unavailable']}。请选择其他选项"}

        # 支付代价（校验不过则不结算、事件不完成，可重新选择）
        cost_r = self._apply_event_cost(opt, params)
        if not cost_r["success"]:
            return cost_r

        notes = list(cost_r["notes"])
        refuse_note = None
        if opt.get("refuse"):
            # 无所求：每当你在事件中选择"拒绝"类选项，永久获得1点属性点
            if self._has_relic("无所求"):
                self.state.attribute_points += 1
                refuse_note = "【无所求】拒绝类选项：永久+1属性点"

        effect_r = self._apply_event_effect(ev, opt, params)
        if not effect_r["success"]:
            # 代价已支付但收益结算失败：如实回报（代价不返还——选择已作出）
            return {"success": False,
                    "error": effect_r["error"],
                    "warning": "代价已支付、收益结算失败（代价不返还）。请补足收益所需参数后重新选择同一选项",
                    "cost_notes": notes}

        notes += effect_r["notes"]

        # 事件完成：出队（赌局/发现等随机链在返回中继续）
        pe = self.state.pending_event
        if pe and pe["queue"] and pe["queue"][0] == eid:
            pe["queue"].pop(0)
        if pe is not None and not pe["queue"]:
            self.state.pending_event = None

        result = {
            "success": True,
            "action": f"事件【{eid}】选择：{opt['label']}",
            "notes": notes,
            "refuse_note": refuse_note,
            "shards": self.state.shards,
            "player_hp": self.state.player.current_hp if self.state.player else None,
            "remaining_events": (self.state.pending_event or {}).get("queue", []),
        }
        if effect_r.get("follow"):
            result["follow"] = effect_r["follow"]
        return result

    def _apply_event_cost(self, opt: dict, params: dict) -> dict:
        """事件代价（原文具体且合理）。失败=校验不过不结算。"""
        cost = opt.get("cost") or {}
        notes = []
        p = self.state.player
        if p is None:
            return {"success": False, "error": "没有玩家"}
        if "shards" in cost:
            n = cost["shards"]
            if self.state.shards < n:
                return {"success": False, "error": f"碎片不足（需{n}，现有{self.state.shards}）"}
            self.state.shards -= n
            notes.append(f"-{n}[碎片]（余{self.state.shards}）")
        if "hp" in cost:
            n = cost["hp"]
            before = p.current_hp
            p.take_damage(n, "代价")
            notes.append(f"流血{n}（生命{before}→{p.current_hp}）")
        if "aging" in cost:
            n = cost["aging"]
            p.blood_limit -= n
            p.current_hp = min(p.current_hp, p.blood_limit)
            notes.append(f"衰老{n}（血限→{p.blood_limit}）")
        if "exhaustion" in cost:
            n = cost["exhaustion"]
            if p.mana_limit < n:
                return {"success": False, "error": f"法限不足支付枯竭{n}（当前法限{p.mana_limit}）"}
            p.mana_limit -= n
            p.current_mana = min(p.current_mana, p.mana_limit)
            notes.append(f"枯竭{n}（法限→{p.mana_limit}）")
        if "fatigue" in cost:
            n = cost["fatigue"]
            p.speed_limit -= n
            p.current_speed = min(p.current_speed, max(0, p.speed_limit))
            notes.append(f"疲惫{n}（速限→{p.speed_limit}）")
        if "energy" in cost:
            n = cost["energy"]
            if self.state.energy < n:
                return {"success": False, "error": f"精力不足（需{n}，现有{self.state.energy}）"}
            self.state.energy -= n
            notes.append(f"-{n}点精力（余{self.state.energy}）")
        if "amnesia" in cost:
            n = cost["amnesia"]
            if n == "X":
                n = int(params.get("x", 0) or 0)
                if n < 1:
                    return {"success": False, "error": "失忆X需提供参数 x≥1"}
            forget = params.get("forget_names") or []
            if len(forget) != n or len(set(forget)) != n:
                return {"success": False, "error": f"失忆{n}需通过 forget_names 指定失去的{n}种不同道纹"}
            for fn in forget:
                if fn not in p.dao_wen:
                    return {"success": False, "error": f"未持有道纹【{fn}】，无法失忆"}
            for fn in forget:
                del p.dao_wen[fn]
            notes.append(f"失忆{n}（失去道纹：{forget}）")
        if cost.get("relic_destroy"):
            rn = params.get("relic_name", "")
            names = [r.name for r in self.state.relics] + [r.name for r in self.state.relics_pool]
            if rn not in [r.name for r in self.state.relics]:
                return {"success": False,
                        "error": f"销毁一件当前遗物需提供持有的遗物名 relic_name（当前持有：{[r.name for r in self.state.relics]}）"}
            self.state.relics = [r for r in self.state.relics if r.name != rn]
            notes.append(f"销毁遗物【{rn}】")
        return {"success": True, "notes": notes}

    def _gain_consumable(self, name: str) -> dict:
        """获得消耗品（耐久归零后彻底消耗销毁；同名合并耐久）"""
        spec = CONSUMABLES.get(name)
        if spec is None:
            return {"error": f"未知消耗品: {name}"}
        new_c = Consumable(name=name, effect=spec["effect"],
                           current_uses=spec["durability"], max_uses=spec["durability"])
        for c in self.state.consumables:
            if c.merge(new_c):
                return {"name": name, "durability": f"{c.current_uses}/{c.max_uses}", "merged": True}
        self.state.consumables.append(new_c)
        return {"name": name, "durability": f"{new_c.current_uses}/{new_c.max_uses}", "merged": False}

    def _make_event_relic(self, name: str, x_meta: int = None) -> Relic:
        """事件遗物：加入持有但不加入遗物池（README 288行）"""
        spec = EVENT_RELICS[name]
        tags = ["event_relic"] + (["implemented"] if spec["implemented"] else [])
        relic = Relic(name=name, effect=spec["effect"], tags=tags)
        self.state.relics.append(relic)
        if x_meta is not None:
            self.state.event_relic_meta[name] = x_meta
        return relic

    def _apply_event_effect(self, ev: dict, opt: dict, params: dict) -> dict:
        """事件收益（真实结算，随机收益走随机数链）"""
        eff = opt.get("effect") or {}
        notes = []
        follow = {}
        p = self.state.player

        if "shards" in eff:
            n = eff["shards"]
            self.state.shards += n
            notes.append(f"+{n}[碎片]（余{self.state.shards}）")
        if "blood_limit" in eff:
            p.blood_limit += eff["blood_limit"]
            notes.append(f"血限+{eff['blood_limit']}（→{p.blood_limit}）")
        if "attr" in eff:
            for k, v in eff["attr"].items():
                if k == "速限":
                    p.speed_limit += v
                    p.current_speed += v
                    notes.append(f"速限+{v}（→{p.speed_limit}）")
                elif k == "法限":
                    p.mana_limit += v
                    notes.append(f"法限+{v}（→{p.mana_limit}）")
                elif k == "血限":
                    p.blood_limit += v
                    notes.append(f"血限+{v}（→{p.blood_limit}）")
        if "resonance" in eff:
            rtype = eff["resonance"]
            if rtype == "choose":
                rtype = params.get("resonance_type", "")
                if rtype not in ("转换", "反转", "曲解"):
                    return {"success": False, "error": "自选残韵需提供 resonance_type（转换/反转/曲解）"}
            self.state.resonance[rtype] = self.state.resonance.get(rtype, 0) + 1
            notes.append(f"获得残韵【{rtype}】×{self.state.resonance[rtype]}")
        if "learn_spells" in eff:
            n = eff["learn_spells"]
            names = params.get("spell_names") or []
            if len(names) != n:
                return {"success": False,
                        "error": f"学会{n}种法术需提供 spell_names（{n}个，从法术库选择，须持有所需道纹）"}
            for nm in names:
                if nm not in SPELL_LIBRARY:
                    return {"success": False, "error": f"法术【{nm}】不在法术库: {sorted(SPELL_LIBRARY)}"}
                if any(s.name == nm for s in p.spells):
                    return {"success": False, "error": f"法术【{nm}】已学会"}
                spec = SPELL_LIBRARY[nm]
                missing = [d for d in spec["required_daowen"] if d not in p.dao_wen]
                if missing:
                    return {"success": False, "error": f"法术【{nm}】所需道纹未持有：{missing}"}
            for nm in names:
                spec = SPELL_LIBRARY[nm]
                p.spells.append(Spell(name=nm, required_daowen=list(spec["required_daowen"]),
                                      trigger_condition=spec["trigger"],
                                      effect_flow="→".join(s["daowen"] for s in spec["steps"]),
                                      rank=len(spec["required_daowen"])))
            notes.append(f"学会法术：{names}")
        if eff.get("relic_random"):
            if not self.state.relics_pool:
                notes.append("遗物池已空，本次遗物收益落空（如实记录）")
            else:
                r = self._action_discover_relic({"purpose": "event"})
                follow["relic_discovery"] = r
                notes.append("触发【发现】遗物机制（抽3选1，见 follow.relic_discovery）")
        if eff.get("relic_choose"):
            rn = params.get("relic_name", "")
            if not any(r.name == rn for r in self.state.relics_pool):
                return {"success": False,
                        "error": f"自选遗物需提供遗物池中存在的 relic_name（池中：{[r.name for r in self.state.relics_pool]}）"}
            r = self._resolve_discover_relic(rn)
            if not r.get("success"):
                return {"success": False, "error": r.get("error")}
            notes.append(f"自选获得遗物【{rn}】")
        if "event_relic" in eff:
            relic = self._make_event_relic(eff["event_relic"])
            impl = EVENT_RELICS[eff["event_relic"]]["implemented"]
            notes.append(f"获得事件遗物【{relic.name}】（不入遗物池"
                         + ("；效果未实装，如实登记不假装生效" if not impl else "") + "）")
        if "event_relic_x" in eff:
            x = int(params.get("x", 0) or 0)
            if x < 1:
                return {"success": False, "error": "献出声音需失忆X≥1（参数 x）"}
            relic = self._make_event_relic(eff["event_relic_x"], x_meta=x)
            notes.append(f"获得事件遗物【{relic.name}】（X={x}，每场[战始]获得{20 * x}点法力）")
        if "consumable" in eff:
            gain = self._gain_consumable(eff["consumable"])
            notes.append(f"获得消耗品【{eff['consumable']}】（耐久{gain.get('durability')}）")
        if "friend" in eff:
            tpl = EVENT_FRIENDS[eff["friend"]]
            fname = eff["friend"]
            if any(f.name == fname and f.is_alive for f in self.state.friends):
                return {"success": False, "error": f"【{fname}】已在队伍中"}
            friend = Entity(
                name=fname, entity_type="朋友",
                blood_limit=tpl["blood_limit"], current_hp=tpl["blood_limit"],
                attack_count=tpl["attack_count"], attack_power=tpl["attack_power"],
                mutation=tpl.get("mutation", 0))
            for dw_name, dw_x in tpl.get("daowen", {}).items():
                friend.dao_wen[dw_name] = DaoWenInstance(dao_wen=self._build_daowen_def(dw_name))
            self.state.friends.append(friend)
            notes.append(f"【{fname}】（{tpl['attack_count']}×{tpl['attack_power']}/{tpl['blood_limit']}，"
                         f"{list(tpl.get('daowen', {}))}）作为[朋友]加入，开局即在战场")
        if "heal_pool" in eff:
            pool = eff["heal_pool"]
            alloc = params.get("alloc") or {p.name: pool}
            total = sum(alloc.values())
            if total > pool:
                return {"success": False, "error": f"恢复量分配{total}超过{pool}"}
            team = [p] + self.state.friends + self.state.employees + self.state.temp_friends
            for name, amount in alloc.items():
                ent = next((e for e in team if e.name == name), None)
                if ent is None:
                    return {"success": False, "error": f"恢复量只能分配给自己或队友，未找到[{name}]"}
                heal = ent.heal(amount)
                notes.append(f"{name}获得回复{heal['actual_heal']}（生命→{heal['hp_after']}）")
        if "energy_delta" in eff:
            self.state.energy = max(0, self.state.energy + eff["energy_delta"])
            notes.append(f"精力{eff['energy_delta']}（余{self.state.energy}）【假设：裂隙温泉“下次行动精力-1”按立即扣1结算，待DM裁定】")
        if "next_battle" in eff:
            self.state.next_battle_mods.update(eff["next_battle"])
            notes.append(f"下一场战斗修饰已登记：{eff['next_battle']}")
        if "debt" in eff:
            self.state.debt_battle_start_cost = eff["debt"]["battle_start_cost"]
            notes.append(f"高利贷债务生效：每场[战始]失去{self.state.debt_battle_start_cost}[碎片]；负债时每负债10[碎片]强扣5点[血限]利息")
        if "gamble" in eff:
            kind = eff["gamble"]["kind"]
            x = int(params.get("x", 0) or 0)
            if x < 1:
                return {"success": False, "error": "押注需提供参数 x≥1"}
            if kind == "hp":
                before = p.current_hp
                p.take_damage(x, "代价")
                notes.append(f"流血{x}（生命{before}→{p.current_hp}）")
            self._pending_random = {
                "purpose": "event_gamble", "pool_name": "gamble",
                "meta": {"pool": ["win", "lose"], "kind": kind, "x": x},
            }
            follow["gamble"] = {"kind": kind, "x": x, "range": "1~2",
                                "instruction": "赌局50/50：请给出数字1~2（1=赢，2=输）【假设待DM裁定：输赢映射按1赢2输】"}
            notes.append(f"赌局开始（{'流血押注' if kind == 'hp' else f'押注{x}[碎片]'}），等待随机数")
        if eff.get("wrong_lastword"):
            text = params.get("text", "") or "（未写明内容的错误遗言）"
            self.state.last_words.append({"text": text, "wrong": True})
            notes.append(f"在《死者之书》中留下一条错误遗言：「{text}」")
        if eff.get("remove_lastword"):
            if self.state.last_words:
                removed = self.state.last_words.pop(0)
                notes.append(f"清除《死者之书》遗言：「{removed['text']}」")
            elif self.state.death_book_wisdom:
                removed = self.state.death_book_wisdom.pop(0)
                notes.append(f"清除《死者之书》遗言：「{removed}」")
            else:
                notes.append("《死者之书》中没有遗言可清除（如实记录）")
        if eff.get("no_more_memory"):
            self.state.no_more_memory = True
            notes.append("本次轮回无法再获得前世记忆")
        if eff.get("read_memory"):
            memories = ([w["text"] for w in self.state.last_words]
                        + list(self.state.death_book_wisdom))
            follow["memory"] = memories or "《死者之书》空空如也，没有可赎回的前世记忆"
            notes.append("赎回一段前世记忆")
        if eff.get("letter"):
            text = params.get("text", "")
            if not text or len(text) > 40:
                return {"success": False, "error": "写信需提供 text（最多40字）"}
            self.state.letter_to_next = text
            notes.append(f"信已寄出：「{text}」")
        if eff.get("implant_daowen"):
            fname = params.get("friend_name", "")
            friend = next((f for f in self.state.friends + self.state.employees
                           + self.state.temp_friends if f.name == fname and f.is_alive), None)
            if friend is None:
                names = [f.name for f in self.state.friends + self.state.employees + self.state.temp_friends if f.is_alive]
                return {"success": False, "error": f"强制移植需提供存活的微光者队友 friend_name（当前：{names}）"}
            pool19 = ["愤怒", "自残", "无神", "借力", "弱化", "自食", "兴奋", "无力", "迟滞",
                      "急速", "加速", "眩晕", "洞察", "蒙蔽", "滋养", "衰败", "寄生", "滑翔", "坠落"]
            self._pending_random = {
                "purpose": "event_implant", "pool_name": "implant",
                "meta": {"pool": pool19, "friend_name": fname},
            }
            follow["implant"] = {"range": f"1~{len(pool19)}",
                                 "instruction": f"随机植入一种怪物转化道纹（人类无法承受原始道纹，池=19种转化道纹）：{pool19}"}
        if eff.get("lose_friend_for_shards"):
            fname = params.get("friend_name", "")
            for lst in (self.state.friends, self.state.employees, self.state.temp_friends):
                friend = next((f for f in lst if f.name == fname and f.is_alive), None)
                if friend is not None:
                    gain = math.ceil(friend.blood_limit * 0.5)
                    lst.remove(friend)
                    self.state.shards += gain
                    notes.append(f"失去微光者队友【{fname}】，获得其血限50%的[碎片]+{gain}（余{self.state.shards}）")
                    break
            else:
                names = [f.name for f in self.state.friends + self.state.employees + self.state.temp_friends if f.is_alive]
                return {"success": False, "error": f"抽取灵魂需提供存活队友 friend_name（当前：{names}）"}
        if eff.get("shield_friend"):
            fname = params.get("friend_name", "")
            friend = next((f for f in self.state.friends if f.name == fname and f.is_alive), None)
            if friend is None:
                names = [f.name for f in self.state.friends if f.is_alive]
                return {"success": False, "error": f"防弹插板需指定一名[朋友] friend_name（当前朋友：{names}）"}
            friend.blood_limit += 10
            self.state.shielded_friends[fname] = True
            notes.append(f"【{fname}】获得【防弹插板】：血限+10（→{friend.blood_limit}），且每场[战始]获得15格挡")
        if "monster_next_battle" in eff:
            self.state.next_battle_mods["extra_monster_named"] = eff["monster_next_battle"]
            notes.append(f"下一场战斗中【{eff['monster_next_battle']}】将作为怪物额外出现")
        if eff.get("info_next_battle"):
            self.state.next_battle_mods["info"] = True
            notes.append("获得下一场战斗怪物的完整情报（战始面板公开时长提前，模拟中面板本就公开，如实只登记）")
        return {"success": True, "notes": notes, "follow": follow}

    def _resolve_event_gamble(self, pending: dict, number: int) -> dict:
        """赌局结算：1=赢，2=输（映射为假设，已标注）"""
        if number not in (1, 2):
            return {"success": False, "error": "赌局随机数只需 1（赢）或 2（输）"}
        meta = pending["meta"]
        self._pending_random = None
        win = number == 1
        x, kind = meta["x"], meta["kind"]
        p = self.state.player
        if kind == "shards":
            if win:
                self.state.shards += 2 * x
                note = f"赢：获得双倍[碎片]+{2 * x}"
            else:
                self.state.shards -= 2 * x
                if self.state.shards < -50:
                    self.state.shards = -50
                note = f"输：扣除双倍[碎片]-{2 * x}（允许负债，负债≤50）"
        else:
            if win:
                self.state.shards += 2 * x
                note = f"赢：获得2X[碎片]+{2 * x}"
            else:
                note = "输：无事发生"
        return {"success": True, "action": "赌局结算", "win": win, "note": note,
                "shards": self.state.shards, "player_hp": p.current_hp if p else None}

    def _pre_battle_weixiu(self, params: dict) -> dict:
        """维修（扭曲都市专属行动）：1点/2点（5碎片）/3点（12）【耐久分配】"""
        if self.state.current_region != "扭曲都市":
            self.state.energy += 1
            return {"success": False, "error": "【维修】是扭曲都市专属行动"}
        tier = int(params.get("tier", 0) or 0)
        tier_map = {1: (1, 0), 2: (2, 5), 3: (3, 12)}
        if tier not in tier_map:
            self.state.energy += 1
            return {"success": False, "error": "维修档位无效（1=1点 / 2=2点(5碎片) / 3=3点(12碎片)）"}
        points, shard_cost = tier_map[tier]
        if self.state.shards < shard_cost:
            self.state.energy += 1
            return {"success": False, "error": f"碎片不足，需要{shard_cost}"}
        alloc = params.get("alloc") or {}
        if sum(alloc.values()) != points:
            self.state.energy += 1
            return {"success": False, "error": f"档位{tier}获得{points}点耐久分配，alloc合计须等于{points}（实际{sum(alloc.values())}）"}
        for name, pts in alloc.items():
            c = next((c for c in self.state.consumables if c.name == name), None)
            if c is None:
                self.state.energy += 1
                return {"success": False, "error": f"未持有消耗品【{name}】，无法维修"}
            if c.current_uses + pts > c.max_uses:
                self.state.energy += 1
                return {"success": False,
                        "error": f"【{name}】耐久分配{c.current_uses}+{pts}超过其耐久上限{c.max_uses}"}
        self.state.shards -= shard_cost
        for name, pts in alloc.items():
            c = next(c for c in self.state.consumables if c.name == name)
            c.current_uses += pts
        return {"success": True, "action": f"维修（{points}点耐久分配）",
                "alloc": alloc, "shard_cost": shard_cost,
                "consumables": [c.to_dict() for c in self.state.consumables]}

    def _action_use_consumable(self, params: dict) -> dict:
        """
        使用消耗品（耐久真实扣减，归零后彻底消耗销毁）。
        usage 决定时点：battle（战斗中，耗1次出手——出手消耗口径为假设，待DM裁定）
        / anytime（战斗中任意时刻，不耗出手）/ pre_battle（局外）。
        passive（龙血瓶）与 round_start_auto（储能电池）不走本行动。
        """
        name = params.get("name", "")
        c = next((c for c in self.state.consumables if c.name == name), None)
        if c is None:
            return {"success": False, "error": f"未持有消耗品【{name}】"}
        spec = CONSUMABLES.get(name, {})
        if spec.get("needs_dm"):
            return {"success": False, "unavailable": True,
                    "error": f"【{name}】的使用涉及玩家设计与DM确认（{spec['effect']}），程序拒绝假装成功"}
        usage = spec.get("usage", "battle")
        if usage in ("passive", "round_start_auto", "battle_start"):
            return {"success": False, "error": f"【{name}】为{usage}型，不由主动使用触发（龙血瓶用 withdraw/休整提取）"}
        if usage == "pre_battle" and self.state.phase != "pre_battle":
            return {"success": False, "error": f"【{name}】只能在局外使用"}
        if usage in ("battle", "anytime") and self.state.phase not in ("in_combat", "dead_duel"):
            return {"success": False, "error": f"【{name}】只能在战斗中使用"}

        player = self.state.player
        target_name = params.get("target", "")
        target = self._find_entity(target_name) if target_name else None
        notes = []

        # 战斗中使用（battle 型）消耗1次出手
        if usage == "battle":
            budget = self._player_action_budget()
            if self.state.actions_used >= budget:
                return {"success": False, "error": f"出手次数已用完（{self.state.actions_used}/{budget}）"}

        # ---------- 效果结算 ----------
        if name == "绝息淤泥":
            notes.append("屏蔽自身灵魂位置：本次[战终]立刻逃脱（战斗立即以撤退结算，战利品按撤退口径归零）")
            remaining = c.use()
            if remaining <= 0:
                self.state.consumables.remove(c)   # 耐久归零后彻底消耗销毁
            return self._settle_battle_end(escaped=True, retreat_detail={"via": "绝息淤泥", "note": notes[0]})

        if name == "假钞贴":
            self.state.fake_shards += 20
            notes.append(f"+20[假碎片]（现有假碎片{self.state.fake_shards}，战斗中失去碎片时优先失去）")

        elif name == "穿甲弹":
            if target is None or not target.is_alive:
                return {"success": False, "error": "穿甲弹需要一个存活[目标] target"}
            before = target.current_hp
            # 忽略【格挡】与【闪避】：直接扣生命
            target.current_hp = max(0, target.current_hp - 15)
            if target.current_hp == 0:
                target.is_alive = False
            notes.append(f"对【{target.name}】打出15点忽略格挡与闪避的伤害（生命{before}→{target.current_hp}）")

        elif name == "洗劫面具":
            player.add_status(StatusEffect(name="必中", remaining_rounds=-1, value=2, source="洗劫面具"))
            notes.append("自身下2次攻击附带【必中】（必中层数→"
                         f"{player.get_status_value('必中')}）")

        elif name == "赤泉囊":
            pool = 8
            alloc = params.get("alloc") or {player.name: pool}
            if sum(alloc.values()) > pool:
                return {"success": False, "error": f"恢复量分配超过{pool}"}
            team = [player] + self.state.friends + self.state.employees + self.state.temp_friends
            for ename, amount in alloc.items():
                ent = next((e for e in team if e.name == ename), None)
                if ent is None:
                    return {"success": False, "error": f"恢复量只能分配给自己或队友，未找到[{ename}]"}
                heal = ent.heal(amount)
                notes.append(f"{ename}获得回复{heal['actual_heal']}")
            self.state.relic_flags["赤泉囊_debuff"] = 2
            notes.append("副作用登记：自身下两场战斗[战始]失去4点生命")

        elif name == "反怪物电击枪":
            if target is None or not target.is_alive:
                return {"success": False, "error": "电击枪需要一个存活[目标] target"}
            dmg = 25
            extra_note = ""
            if CombatEngine.is_flying(target):
                dmg += 15
                target.add_status(StatusEffect(name="坠落", remaining_rounds=1, value=1, source="电击枪"))
                extra_note = "；目标处于【飞行】：额外15伤害并施加【坠落1】"
            res = target.take_damage(dmg, "普通")
            notes.append(f"对【{target.name}】造成{dmg}点伤害{extra_note}（生命→{res['hp_after']}）")

        elif name == "备用血泵":
            heal = player.heal(20)
            notes.append(f"自身获得20点[回复]（实际{heal['actual_heal']}）")
            if player.current_hp <= math.ceil(player.blood_limit * 0.3):
                player.gain_shield(30)
                notes.append("生命≤30%：额外获得30点格挡")

        elif name == "强光探照灯":
            if target is None or not target.is_alive:
                return {"success": False, "error": "探照灯需要一个存活[目标] target"}
            target.add_status(StatusEffect(name="蒙蔽", remaining_rounds=-1, value=2, source="探照灯"))
            notes.append(f"【{target.name}】陷入【蒙蔽2】（下2次造成的伤害无效）")

        elif name == "高压水枪":
            cleared = []
            for m in self.state.enemies:
                if not m.is_alive:
                    continue
                before_cnt = len(m.status_effects)
                m.status_effects = [s for s in m.status_effects if s.remaining_rounds == -1]
                if len(m.status_effects) < before_cnt:
                    cleared.append(f"{m.name}清除{before_cnt - len(m.status_effects)}个持续X效果")
            notes.append("清除全场敌方所有“持续X”效果：" + ("；".join(cleared) if cleared else "无可清除者"))

        elif name == "急救箱":
            heal = player.heal(25)
            notes.append(f"自身获得[回复25]（实际{heal['actual_heal']}）")
            negatives = [s for s in player.status_effects if s.remaining_rounds != -1]
            if negatives:
                rm = negatives[0]
                player.status_effects.remove(rm)
                notes.append(f"清除自身持续负面减益【{rm.name}】")
            else:
                notes.append("自身没有可清除的持续X负面减益")

        elif name == "干扰仪":
            self.state.relic_flags["干扰仪_回合"] = self.state.current_round
            notes.append(f"全场所有敌方[目标]本回合（第{self.state.current_round}回合）无法发动自身道纹")

        else:
            return {"success": False, "unavailable": True,
                    "error": f"消耗品【{name}】的效果钩子未实装，程序拒绝假装生效"}

        remaining = c.use()
        destroyed = remaining <= 0
        if destroyed:
            self.state.consumables.remove(c)
            notes.append(f"【{name}】耐久归零，彻底消耗销毁")
        else:
            notes.append(f"【{name}】耐久→{remaining}/{c.max_uses}")

        if usage == "battle":
            self.state.actions_used += 1

        return {"success": True, "action": f"使用消耗品【{name}】", "notes": notes,
                "budget": {"used": self.state.actions_used, "total": self._player_action_budget()}
                if usage == "battle" else None}

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

    def _pre_battle_wangyou(self, params: dict) -> dict:
        """
        忘忧（遗物【忘忧香】自有局外行动，真实生效）：
        README原文：局外行动你可以选择"忘忧"（失忆1/2/3，获得30/55/80［碎片］）
        - tier=1/2/3 → 失忆1/2/3 → 获得30/55/80碎片
        - 失忆X：永久失去X种道纹，由 forget_names 指定（与道纹失忆代价同规则）
        - 精力消耗：与其他局外行动一致消耗1点（规则未单列，待DM裁定确认）
        """
        if not self._has_relic("忘忧香"):
            self.state.energy += 1
            return {"success": False, "error": "未持有遗物【忘忧香】，不能执行【忘忧】"}

        player = self.state.player
        if player is None:
            self.state.energy += 1
            return {"success": False, "error": "没有玩家"}

        tier = params.get("tier", 1)
        shard_map = {1: 30, 2: 55, 3: 80}
        if tier not in shard_map:
            self.state.energy += 1
            return {"success": False, "error": "忘忧档位无效（1/2/3 = 失忆1/2/3 → 30/55/80碎片）"}

        forget_names = params.get("forget_names") or []
        if len(forget_names) != tier or len(set(forget_names)) != tier:
            self.state.energy += 1
            return {"success": False,
                    "error": f"失忆{tier}需通过 forget_names 指定失去的{tier}种不同道纹"}
        for fn in forget_names:
            if fn not in player.dao_wen:
                self.state.energy += 1
                return {"success": False, "error": f"未持有道纹【{fn}】，无法失忆"}

        for fn in forget_names:
            del player.dao_wen[fn]
        gained = shard_map[tier]
        self.state.shards += gained

        return {
            "success": True,
            "action": f"忘忧（失忆{tier}→{gained}碎片）",
            "result": {
                "forgot_daowen": forget_names,
                "shards_gained": gained,
                "shards_total": self.state.shards,
                "daowen_remaining": list(player.dao_wen.keys()),
            },
            "energy_remaining": self.state.energy,
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
            return self._create_custom_spell(params, use_energy_refund=True)

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
            return {"success": False, "error": "learn_type 仅支持 spell / transform_daowen / create_spell"}

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

    # ==================== 自创法术 ====================

    def _spell_spec(self, spell_name: str) -> dict:
        """查法术spec：自创法术优先（存于Spell.spec），其次法术库"""
        player = self.state.player
        if player:
            for s in player.spells:
                if s.name == spell_name and s.spec:
                    return s.spec
        return SPELL_LIBRARY.get(spell_name)

    @staticmethod
    def _validate_custom_spell_blueprint(blueprint: dict, player) -> tuple[list, dict]:
        """
        校验自创法术图纸（创建与修订共用）：
        - 完全由校验时已拥有的道纹按三大法则组装（README学习条目+法术通则）
        - 触发时点合法；步骤条件/目标/参数在引擎可机械判定白名单内（否则如实拒绝，不假装）
        返回 (errors, normalized_spec)
        """
        from .gamedata import IMPLEMENTED_DAOWEN

        errors = []
        name = blueprint.get("name", "")
        trigger = blueprint.get("trigger", "")
        steps = blueprint.get("steps") or []
        loop = bool(blueprint.get("loop", False))

        if not name or len(name) > 20:
            errors.append("法术名必填且≤20字")
        if trigger not in VALID_SPELL_TRIGGERS:
            errors.append(f"触发时点[{trigger}]非法，合法：{sorted(VALID_SPELL_TRIGGERS)}")
        if not isinstance(steps, list) or not steps:
            errors.append("积木步骤不能为空（法术不得凭空创造效果，必须由已拥有道纹组成）")

        norm_steps = []
        for i, st in enumerate(steps or []):
            dw = st.get("daowen", "")
            if player is None or dw not in player.dao_wen:
                errors.append(f"步骤{i+1}【{dw}】：必须完全由已拥有道纹组成（当前持有：{list(player.dao_wen.keys()) if player else []}）")
                continue
            if dw not in IMPLEMENTED_DAOWEN:
                errors.append(f"步骤{i+1}【{dw}】引擎未实装，无法保证按原版公式结算，拒绝组装")
                continue
            x_param = st.get("x_param", "x")
            if not isinstance(x_param, str) or not x_param:
                errors.append(f"步骤{i+1}：x_param无效（发动时按此键取X值，如 'x' / 'y' / '3x'）")
                continue
            target = st.get("target", "enemy")
            if target not in ("self", "enemy", "target"):
                errors.append(f"步骤{i+1}【{dw}】：target仅支持 self/enemy/target")
                continue
            cond = st.get("condition")
            if cond is not None and cond not in ("target_flying", "previous_step_no_damage"):
                errors.append(f"步骤{i+1}【{dw}】：条件[{cond}]引擎无法机械判定，拒绝组装"
                              f"（可判定：target_flying / previous_step_no_damage）")
                continue
            norm_steps.append({"daowen": dw, "x_param": x_param, "target": target,
                               **({"condition": cond} if cond else {})})

        if errors:
            return errors, None

        required = []
        for st in norm_steps:
            if st["daowen"] not in required:
                required.append(st["daowen"])
        spec = {
            "name": name,
            "required_daowen": required,
            "trigger": trigger,
            "costs_action": False,   # 与法术库一致：反应型法术在触发时点插队，不耗出手（如有争议待DM裁定）
            "steps": norm_steps,
            "loop": loop,
            "custom": True,
        }
        return [], spec

    def _create_custom_spell(self, params: dict, use_energy_refund: bool = False) -> dict:
        """自创一种法术（学习行动的真免费项，真实生效）"""
        player = self.state.player

        def fail(msg):
            if use_energy_refund:
                self.state.energy += 1
            return {"success": False, "error": msg}

        if player is None:
            return fail("没有玩家")

        blueprint = {
            "name": params.get("name") or (params.get("names") or [""])[0],
            "trigger": params.get("trigger", ""),
            "steps": params.get("steps"),
            "loop": params.get("loop", False),
        }
        name = blueprint["name"]

        if name in SPELL_LIBRARY:
            return fail(f"[{name}]与法术库法术重名（全宇宙唯一），请直接学习或另起名字")
        if any(s.name == name for s in player.spells):
            return fail(f"法术[{name}]已存在；如需调整请在战终后调用 revise_custom_spell 修订")

        errors, spec = self._validate_custom_spell_blueprint(blueprint, player)
        if errors:
            return fail("自创图纸不合规：" + "；".join(errors))

        player.spells.append(Spell(
            name=name,
            required_daowen=list(spec["required_daowen"]),
            trigger_condition=spec["trigger"],
            effect_flow="→".join(s["daowen"] for s in spec["steps"]) + ("（循环）" if spec["loop"] else ""),
            rank=len(spec["required_daowen"]),
            spec=spec,
        ))
        return {
            "success": True,
            "action": f"自创法术【{name}】",
            "result": {
                "spec": spec,
                "note": "完全由创建时已拥有道纹按积木/循环/中断三大法则组装；[战终]可修订（revise_custom_spell）",
                "spells": [s.name for s in player.spells],
            },
        }

    def _action_revise_custom_spell(self, params: dict) -> dict:
        """
        修订自创法术（README：[战终]可以进行修订）
        实现窗口：战终结算后的局外阶段（phase=pre_battle）。
        不消耗精力（规则未写成本，待DM裁定确认）；修订须通过创建同款校验（以修订时持有道纹为准）。
        """
        if self.state.phase != "pre_battle":
            return {"success": False,
                    "error": f"修订窗口为[战终]后的局外阶段，当前阶段({self.state.phase})不能修订"}
        player = self.state.player
        if player is None:
            return {"success": False, "error": "没有玩家"}
        name = params.get("name", "")
        spell = next((s for s in player.spells if s.name == name), None)
        if spell is None:
            return {"success": False, "error": f"未持有法术[{name}]"}
        if not spell.spec:
            return {"success": False, "error": f"法术[{name}]是法术库法术，不可修订（仅自创法术可修订）"}

        blueprint = {
            "name": name,
            "trigger": params.get("trigger", spell.spec["trigger"]),
            "steps": params.get("steps", spell.spec["steps"]),
            "loop": params.get("loop", spell.spec.get("loop", False)),
        }
        errors, spec = self._validate_custom_spell_blueprint(blueprint, player)
        if errors:
            return {"success": False, "error": "修订图纸不合规（修订以当前持有道纹为准）：" + "；".join(errors)}

        before = spell.spec
        spell.spec = spec
        spell.required_daowen = list(spec["required_daowen"])
        spell.trigger_condition = spec["trigger"]
        spell.effect_flow = "→".join(s["daowen"] for s in spec["steps"]) + ("（循环）" if spec["loop"] else "")
        spell.rank = len(spec["required_daowen"])
        return {
            "success": True,
            "action": f"修订自创法术【{name}】",
            "result": {"before": before, "after": spec},
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

        # ---- 遗物[战始]可选项的前置校验（不合规如实拒绝，不带病开战）----
        p0 = self.state.player
        zhesu_x = int(params.get("zhesu_x", 0) or 0)
        xianxue_x = int(params.get("xianxue_x", 0) or 0)
        maishenqi_friend = params.get("maishenqi_friend", "")
        if zhesu_x:
            if not self._has_relic("折速法印"):
                return {"success": False, "error": "未持有遗物【折速法印】，[战始]声明疲惫X被拒绝"}
            if zhesu_x < 0 or zhesu_x > (p0.current_speed if p0 else 0):
                return {"success": False,
                        "error": f"折速法印疲惫X={zhesu_x}超过当前速度({p0.current_speed if p0 else 0})：疲惫不能透支"}
        if xianxue_x:
            if not self._has_relic("鲜血契约"):
                return {"success": False, "error": "未持有遗物【鲜血契约】，[战始]声明流血X被拒绝"}
            cap0 = (p0.blood_limit // 5) if p0 else 0   # X≤自身20%[血限]
            if xianxue_x < 1 or xianxue_x > cap0:
                return {"success": False,
                        "error": f"鲜血契约流血X={xianxue_x}超出上限（X≤20%[血限]={cap0}）"}
        if maishenqi_friend:
            if not self._has_relic("卖身契"):
                return {"success": False, "error": "未持有遗物【卖身契】，[战始]指定被拒绝"}
            f0 = next((f for f in self.state.friends + self.state.employees
                       if f.name == maishenqi_friend and f.is_alive), None)
            if f0 is None:
                return {"success": False,
                        "error": f"卖身契指定的[朋友]/[员工]【{maishenqi_friend}】不存在或已死亡"}

        self.state.phase = "battle_start"
        self.state.current_battle += 1
        self.state.current_round = 0
        # 跨场持久键必须保留（赤泉囊副作用等），其余单场记账清空
        _persist = {k: v for k, v in self.state.relic_flags.items() if k.startswith("赤泉囊_debuff")}
        self.state.relic_flags.clear()
        self.state.relic_flags.update(_persist)
        self.state.pending_resonance.clear()   # 上一场未命中的残韵声明失效（未生效不消耗）
        self.state.battle_background = params.get("battle_background", "未选择")
        # 事件遗物的战始可选项（由玩家在战始声明，[战始]结算）
        self.state.relic_flags["_battle_start_choices"] = {
            "scarlet_fruit": bool(params.get("use_scarlet_fruit")),
            "pale_flower": bool(params.get("use_pale_flower")),
            "fuyue_friend": params.get("fuyue_friend", ""),
            "maishenqi_friend": params.get("maishenqi_friend", ""),
            "zhesu_x": zhesu_x,
            "xianxue_x": xianxue_x,
        }

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

    def _convert_friend_to_monster(self, friend: Entity) -> dict:
        """
        朋友异变≥50 → 一律异变为怪物（DM裁定2026-07-31，与轮回者异变50对等）：
        立即脱离我方阵营、倒戈加入敌方（沿用其面板与道纹，白板规则同怪物：
        其面板道纹自倒戈后的出手轮起主动发动方可生效）；
        卖身契/负岳索指向该对象的，效果失效如实记录。
        """
        for lst in (self.state.friends, self.state.employees, self.state.temp_friends):
            if friend in lst:
                lst.remove(friend)
                break
        orig_name = friend.name
        extra = ""
        for key in ("卖身契_friend", "负岳索_friend"):
            if self.state.relic_flags.get(key) == orig_name:
                self.state.relic_flags.pop(key, None)
                extra += f"；{key.split('_')[0]}指向对象已怪化，效果失效"
        friend.entity_type = "怪物"
        friend.spawn_blood_limit = friend.blood_limit   # [战终]奖励按怪物口径：战始血限2%+道纹数×5
        # 与场上怪物重名时编号，保证实体名唯一
        if any(m.name == orig_name for m in self.state.enemies):
            n = 2
            while any(m.name == f"{orig_name}{n}" for m in self.state.enemies):
                n += 1
            friend.name = f"{orig_name}{n}"
        self.state.enemies.append(friend)
        return {"type": "异变怪化",
                "note": (f"{orig_name}异变达到{friend.mutation}层，立刻异变为怪物倒戈加入敌方"
                         f"（DM裁定：朋友一律异变为怪物）；本场[战终]如其未[命零]则就此为敌，不再回归{extra}")}

    def _huifengdao_on_speed_loss(self, points: int, source: Optional["Entity"] = None) -> Optional[str]:
        """
        回锋刀：每失去1点速度后，对[目标]造成3点伤害。
        [目标]口径：优先令其失去速度的攻击者（闪避时的攻击方/施加减速者），
        无明确来源（自付疲惫等）时按存活怪物列表首位顺延（假设，待DM裁定）。
        """
        if points <= 0:
            return None
        p = self.state.player
        if not p or not self._has_relic("回锋刀"):
            return None
        if self.state.phase != "in_combat":
            return None
        assumption = ""
        target = None
        if source is not None and source.is_alive and source in self.state.enemies:
            target = source
        else:
            target = next((m for m in self.state.enemies if m.is_alive), None)
            if target is not None:
                assumption = "；[目标]无明确攻击者，按存活怪物首位顺延（假设待DM裁定）"
        if target is None:
            return None
        dmg = 3 * points
        res = target.take_damage(dmg, "回锋刀")
        sink: dict = {}
        self._sync_monster_death_hooks(sink)
        extra = ("；" + "；".join(sink["relic_notes"])) if sink.get("relic_notes") else ""
        died = "→[命零]" if res.get("died") else ""
        absorbed = (f"（格挡吸收{res['shield_absorbed']}，净伤{res['actual_damage']}）"
                    if res.get("shield_absorbed") else "")
        return (f"【回锋刀】失去{points}点速度→对{target.name}造成{dmg}点伤害{absorbed}"
                f"（{res['hp_before']}→{res['hp_after']}{died}）{assumption}{extra}")

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

        # ---- 事件给下一场战斗的修饰（探索事件登记，战始真实应用并消耗）----
        mods = self.state.next_battle_mods or {}
        mod_notes = []
        if mods.get("enemy_blood_pct"):
            pct = mods["enemy_blood_pct"]
            for m in monsters:
                add = math.ceil(m.blood_limit * pct / 100)
                m.blood_limit += add
                m.current_hp += add
            mod_notes.append(f"地下角斗场：敌方全体血限+{pct}%")
        if mods.get("extra_monster"):
            # 通缉悬赏榜：额外加入1种帮派怪物（需玩家数字抽选——此处如实请求）
            # 简化：随机数为前序出怪数字流已耗；按发现机制用敌池下一只顺延（如实登记，待DM裁定抽签方式）
            pool = MONSTER_POOLS[region]
            candidates = [d for d in pool]
            chosen = None
            # 选一只不与已出同名的（允许重复种族时向后顺延）
            for i in range(len(candidates)):
                d = candidates[(meta["draws"][-1] + i) % len(candidates)]
                if not any(m.name == d["name"] for m in monsters):
                    chosen = d
                    break
            chosen = chosen or candidates[meta["draws"][-1] % len(candidates)]
            ent = Entity(name=chosen["name"], entity_type="怪物",
                         blood_limit=chosen["blood_limit"], current_hp=chosen["blood_limit"],
                         attack_count=chosen["attack_count"], attack_power=chosen["attack_power"],
                         speed_limit=0, current_speed=0, spawn_blood_limit=chosen["blood_limit"])
            for dw_name in chosen["daowen"]:
                ent.dao_wen[dw_name] = DaoWenInstance(dao_wen=self._build_daowen_def(dw_name))
            monsters.append(ent)
            mod_notes.append(f"通缉悬赏榜：额外加入1头帮派怪物【{ent.name}】；[战终]结算额外+30[碎片]悬赏金")
        if mods.get("extra_monster_named"):
            panels = {
                "追求者": {"attack_count": 8, "attack_power": 2, "blood_limit": 96,
                           "daowen": {"逆鳞": 2, "活血": 3, "固执": 3}},
            }
            nm = mods["extra_monster_named"]
            d = panels.get(nm)
            if d:
                ent = Entity(name=nm, entity_type="怪物",
                             blood_limit=d["blood_limit"], current_hp=d["blood_limit"],
                             attack_count=d["attack_count"], attack_power=d["attack_power"],
                             speed_limit=0, current_speed=0, spawn_blood_limit=d["blood_limit"])
                for dw_name in d["daowen"]:
                    ent.dao_wen[dw_name] = DaoWenInstance(dao_wen=self._build_daowen_def(dw_name))
                if not any(m.name == nm for m in monsters):
                    monsters.append(ent)
                    mod_notes.append(f"“追求者”依约作为怪物额外出现（{ent.attack_count}×{ent.attack_power}/{ent.blood_limit}）")
        self.state.relic_flags["_next_battle_mods_active"] = dict(mods)
        self.state.next_battle_mods = {}

        # ---- 高利贷钱庄：[战始]失去碎片；负债利息 ----
        if self.state.debt_battle_start_cost > 0:
            lost = self.state.debt_battle_start_cost
            self.state.shards -= lost
            mod_notes.append(f"高利贷：[战始]失去{lost}[碎片]（余{self.state.shards}）")
            if self.state.shards < 0:
                interest = (-self.state.shards // 10) * 5 if self.state.shards % 10 == 0 else ((-self.state.shards) // 10 + 1) * 5
                # 每负债10[碎片]强扣5点[血限]（不足10按一笔计——假设，待DM裁定）
                if self.state.player:
                    self.state.player.blood_limit -= interest
                    self.state.player.current_hp = min(self.state.player.current_hp, self.state.player.blood_limit)
                mod_notes.append(f"负债{self.state.shards}：利息强扣{interest}点[血限]")

        # ---- 赤泉囊副作用：下两场战斗[战始]失去4点生命 ----
        if self.state.relic_flags.get("赤泉囊_debuff", 0) > 0:
            self.state.relic_flags["赤泉囊_debuff"] -= 1
            if self.state.player:
                before = self.state.player.current_hp
                self.state.player.take_damage(4, "代价")
                mod_notes.append(f"赤泉囊副作用：[战始]失去4点生命（{before}→{self.state.player.current_hp}）")

        # ---- 事件遗物的[战始]效果 ----
        if self.state.player:
            p = self.state.player
            if self._has_relic("帮派令"):
                p.add_status(StatusEffect(name="洗劫", remaining_rounds=3, value=3, source="帮派令"))
                mod_notes.append("【帮派令】[战始]获得【洗劫3】（造成伤害时夺取目标等量[碎片]，持续3回合）")
            if self._has_relic("缄默面具"):
                xm = int(self.state.event_relic_meta.get("缄默面具", 0) or 0)
                if xm > 0:
                    p.current_mana += 20 * xm
                    mod_notes.append(f"【缄默面具】[战始]获得{20 * xm}点法力（无法再使用附带“代价”的道纹）")
            if self._has_relic("三相残韵盘"):
                # [战始]可以消耗自身拥有的一种残韵；[战终]获得另外两种残韵各1个
                for rt in ("转换", "反转", "曲解"):
                    if self.state.resonance.get(rt, 0) > 0:
                        self.state.resonance[rt] -= 1
                        others = [t for t in ("转换", "反转", "曲解") if t != rt]
                        self.state.relic_flags["三相残韵盘_消耗"] = rt
                        self.state.relic_flags["三相残韵盘_待返"] = others
                        mod_notes.append(f"【三相残韵盘】[战始]消耗残韵【{rt}】×1；[战终]将获得【{others[0]}】【{others[1]}】各1个【假设：自动消耗存量字典序第一种，待DM裁定】")
                        break
            for f in self.state.friends:
                if self.state.shielded_friends.get(f.name) and f.is_alive:
                    f.gain_shield(15)
                    mod_notes.append(f"【防弹插板】{f.name}[战始]获得15格挡")
            # 事件遗物的[战始]可选项（玩家在 battle_start 声明）
            choices = self.state.relic_flags.get("_battle_start_choices", {})
            if choices.get("scarlet_fruit") and self._has_relic("猩红果实"):
                before = p.current_hp
                p.take_damage(10, "代价")
                self.state.relic_flags["猩红果实_used"] = True
                mod_notes.append(f"【猩红果实】[战始]流血10（生命{before}→{p.current_hp}）；[战终][血限]+2")
            if choices.get("pale_flower") and self._has_relic("苍白之花"):
                p.current_speed = max(0, p.current_speed - 5)
                self.state.relic_flags["苍白之花_used"] = True
                mod_notes.append(f"【苍白之花】[战始]疲惫5（当前速度→{p.current_speed}）；[战终]精力+1")
            if choices.get("fuyue_friend") and self._has_relic("负岳索"):
                fname = choices["fuyue_friend"]
                f = next((f for f in self.state.friends + self.state.employees if f.name == fname and f.is_alive), None)
                if f is not None:
                    self.state.relic_flags["负岳索_friend"] = fname
                    self.state.relic_flags["负岳索_hp"] = f.current_hp
                    mod_notes.append(f"【负岳索】[战始]指定【{fname}】：其首次受到伤害时，自身[回复]等量生命（按回合净损失结算，待DM裁定粒度）")
            if choices.get("maishenqi_friend") and self._has_relic("卖身契"):
                fname = choices["maishenqi_friend"]
                f = next((f for f in self.state.friends + self.state.employees
                          if f.name == fname and f.is_alive), None)
                if f is not None:
                    self.state.relic_flags["卖身契_friend"] = fname
                    mod_notes.append(
                        f"【卖身契】[战始]指定【{fname}】：本场轮回者支付的【代价】改由其承担"
                        f"（流血/衰老/异变可转承；速度/法力类与失忆/冷却/唯一其面板无法承载，仍由自身支付"
                        f"——口径待DM裁定）；其[命零]后本效果失效")
            # 折速法印：[战始]可以疲惫X，获得6X点法力
            zhesu_x = int(choices.get("zhesu_x", 0) or 0)
            if zhesu_x and self._has_relic("折速法印"):
                p.current_speed = max(0, p.current_speed - zhesu_x)
                p.current_mana += 6 * zhesu_x
                mod_notes.append(f"【折速法印】[战始]疲惫{zhesu_x}（当前速度→{p.current_speed}）"
                                 f"→获得{6 * zhesu_x}点法力（溢出可超[法限]，同缄默面具口径）")
            # 鲜血契约：[战始]可以流血X，使首回合法力+X（X≤自身20%[血限]，已在battle_start前置校验）
            xianxue_x = int(choices.get("xianxue_x", 0) or 0)
            if xianxue_x and self._has_relic("鲜血契约"):
                before = p.current_hp
                p.take_damage(xianxue_x, "代价")
                p.current_mana += xianxue_x
                mod_notes.append(f"【鲜血契约】[战始]流血{xianxue_x}（生命{before}→{p.current_hp}）"
                                 f"→首回合法力+{xianxue_x}")

        # 钱袋接种（战终用到）：记录每场战始；战始结算清单入单场记账（供战报如实引用）
        self.state.relic_flags["_battle_start_notes"] = mod_notes
        self.state.enemies = monsters
        self.state.phase = "in_combat"
        # 回锋刀：战始阶段的疲惫（苍白之花/折速法印）同样计入"失去速度"，反击怪物首位
        if self.state.player and self._has_relic("回锋刀"):
            war_start_tired = (5 if self.state.relic_flags.get("苍白之花_used") else 0) \
                + int(choices.get("zhesu_x", 0) or 0 if self._has_relic("折速法印") else 0)
            note = self._huifengdao_on_speed_loss(war_start_tired, source=None)
            if note:
                mod_notes.append(note)

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
            "battle_start_effects": mod_notes,
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

        # ---- 残韵实时插队命中（README 残韵作用规则2/5）----
        # 残留声明命中（actor,daowen）：本次发动的消耗/代价/目标要求/效果/后续流程
        # 全部改为新道纹的原版公式；怪物面板拥有的道纹不被改变。
        # 命中即消耗残韵（改写即生效，参见 use_resonance 的待DM裁定口径），
        # 命中即由施法者永久获得变化后的道纹（规则2）。
        resonance_note = None
        hit_idx = next((i for i, p in enumerate(self.state.pending_resonance)
                        if p["target_actor"] == caster.name and p["source_daowen"] == name), None)
        resonance_override = hit_idx is not None
        if resonance_override:
            hit = self.state.pending_resonance.pop(hit_idx)
            rtype = hit["resonance_type"]
            old_name = name
            name = hit["new_daowen"]
            x = max(1, hit.get("x", x))
            skip_mana_cost = False   # 新公式全按原版：消耗/代价照常结算
            # 同魂笔一次声明耗1个残韵：两处命中共用。副条命中时若主条已命中扣过，则不重复扣。
            paired_already_charged = False
            if hit.get("via") == "同魂笔" and hit.get("paired_with"):
                main_actor, main_dw = hit["paired_with"]
                paired_already_charged = not any(
                    p["target_actor"] == main_actor and p["source_daowen"] == main_dw
                    for p in self.state.pending_resonance)
            if not paired_already_charged:
                self.state.resonance[rtype] = self.state.resonance.get(rtype, 0) - 1
                resonance_note = (
                    f"残韵【{rtype}】插队：{caster.name}的【{old_name}】本次结算改写为【{name}{x}】"
                    + (f"（经{hit.get('via')}）" if hit.get("via") not in (None, "残韵") else "")
                )
            else:
                resonance_note = (
                    f"残韵【{rtype}】（同魂笔延伸，同源声明已消耗）：{caster.name}的【{old_name}】"
                    f"本次结算改写为【{name}{x}】"
                )
            # 规则2：结算完成后施法者永久获得变化后的道纹（获得口径=命中时，待DM裁定）
            if player and name not in player.dao_wen and name in IMPLEMENTED_DAOWEN:
                player.dao_wen[name] = DaoWenInstance(dao_wen=self._build_daowen_def(name))
                resonance_note += f"；施法者永久获得变化后的道纹【{name}】"

        if resonance_override:
            pass  # 本次以残韵改写结算：允许结算非持有道纹，面板不变
        elif name not in caster.dao_wen:
            return {"success": False, "error": f"{caster.name}未持有道纹: {name}"}

        if name not in IMPLEMENTED_DAOWEN:
            reason = "机制交互纵深过大，待DM裁定语义后补装" if name in UNIMPLEMENTED_DAOWEN else "机制未实装"
            return {
                "success": False,
                "unavailable": True,
                "error": f"道纹【{name}】{reason}，引擎拒绝假装生效",
            }

        dw_instance = caster.dao_wen.get(name)

        if dw_instance is not None and not dw_instance.can_use():
            return {"success": False, "error": f"道纹【{name}】不可用（{dw_instance.reason_unusable()}）"}

        # 干扰仪（消耗品）：敌方全体本回合无法发动自身道纹
        if not is_player_side and self.state.relic_flags.get("干扰仪_回合") == self.state.current_round:
            return {"success": False, "error": f"干扰仪生效中（第{self.state.current_round}回合）：{caster.name}本回合无法发动自身道纹"}

        # 缄默面具（事件遗物）：持有者无法再使用任何附带"代价"的道纹
        if is_player_side and self._has_relic("缄默面具"):
            calc_preview = DaoWenEngine.resolve(name, x, target=caster, caster=caster,
                                                _state=self._caster_state_dict(caster))
            if calc_preview.get("cost_type") not in (None, "消耗"):
                return {"success": False,
                        "error": f"【缄默面具】：无法再使用附带“代价”的道纹（【{name}】代价={calc_preview.get('cost_type')}）"}

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
            # 缓慢X：本回合若出手次数≤X则无法出手（对轮回者侧同样执行）
            if caster.has_status("缓慢") and budget <= caster.get_status_value("缓慢"):
                return {"success": False,
                        "error": f"缓慢：本回合出手{budget}≤{caster.get_status_value('缓慢')}，无法出手"}

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
                    if target is self.state.player:
                        note = self._huifengdao_on_speed_loss(1, source=caster)
                        if note:
                            dodge_resolved["relic_回锋刀"] = note
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
            **({} if name != "缓慢" else {"target_action_count": (
                max(0, self.combat.monster_act_count(self.state.current_round)
                    + target.get_status_value("活力") - target.get_status_value("无力"))
                if target in self.state.enemies
                else self._player_action_budget() if target is self.state.player
                else max(1, math.ceil((target.attack_count or 1) / 3)))}),
        )

        # 法力消耗（怪物发动面板道纹不支付法力，只消耗出手——假设待DM裁定；
        # 但被残韵改写的新公式按原版消耗结算（README残韵规则5），怪物无法力资源
        # 则消耗无法满足，后续流程中断=发动失败；愤怒：目标法力消耗减半）
        cost = calc.get("cost", 0)
        if calc.get("cost_type") == "消耗" and cost > 0 and not skip_mana_cost \
                and (caster.entity_type != "怪物" or resonance_override):
            if caster.has_status("愤怒"):
                cost = math.ceil(cost / 2)
            if not caster.spend_mana(cost):
                return {"success": False,
                        "error": (f"法力不足，需要{cost}，当前{caster.current_mana}"
                                  + ("（残韵改写的新公式消耗无法满足，后续流程中断）" if resonance_override else "")),
                        "resonance": resonance_note}

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
            "resonance": resonance_note,
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
            _spd_before = target.current_speed
            target.current_speed = max(0, math.ceil(target.current_speed / 2))
            add_status(target, "减速", calc["duration"], calc["x"] * multiplier)
            result["effects"].append({"type": "speed_halved", "target": target.name,
                                      "speed_after": target.current_speed})
            if target is self.state.player and _spd_before > target.current_speed:
                note = self._huifengdao_on_speed_loss(_spd_before - target.current_speed, source=caster)
                if note:
                    result["effects"].append({"type": "relic_回锋刀", "note": note})
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
                _spd_before = target.current_speed
                target.current_speed = max(0, target.current_speed - x)
                result["effects"].append({"type": "speed_penalty", "target": target.name, "amount": x})
                if target is self.state.player and _spd_before > target.current_speed:
                    note = self._huifengdao_on_speed_loss(_spd_before - target.current_speed, source=caster)
                    if note:
                        result["effects"].append({"type": "relic_回锋刀", "note": note})
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
            # 缓慢X：本回合若目标单轮出手次数≤X则无法出手（怪物非专属×3作用于阈值）
            threshold = calc["x"] * multiplier
            acts = calc.get("target_action_count", 0)
            if acts <= threshold:
                add_status(target, "缓慢", 1, threshold)
                result["effects"].append({"type": "slow_apply", "target": target.name,
                                          "note": f"本回合无法出手（{acts}≤{threshold}）"})
            else:
                result["effects"].append({"type": "slow_failed",
                                          "note": f"未生效（目标出手{acts}＞阈值{threshold}）"})
            return result

        if name == "必中":
            # 必中X：自身下X次攻击附带必中（持续至层数耗尽，攻击时逐层消耗）
            charges = calc["guaranteed_hits"] * multiplier
            add_status(caster, "必中", 0, charges)
            result["effects"].append({
                "type": "status_added", "target": caster.name, "status": "必中",
                "duration": -1, "value": charges,
                "note": f"下{charges}次攻击附带必中（层尽即止，不回终自动消失）",
            })
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
        spec = self._spell_spec(spell_name)
        if spec is None:
            owned = [s.name for s in player.spells]
            return {"success": False,
                    "error": f"法术[{spell_name}]不存在。已学会：{owned}；法术库：{sorted(SPELL_LIBRARY.keys())}"}
        if not any(s.name == spell_name for s in player.spells):
            return {"success": False, "error": f"未学会法术[{spell_name}]（须局外学习后方可开启）"}

        # 施法时点重新校验：所需道纹必须仍全部持有，否则法术失效
        # （道纹可能被失忆/曲解替换等移除——规则：法术必须完全由已有道纹组成）
        missing = [d for d in spec["required_daowen"] if d not in player.dao_wen]
        if missing:
            return {"success": False,
                    "error": f"法术[{spell_name}]失效：所需道纹{missing}已丢失（法术必须完全由已有道纹组成）"}

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
        使用残韵（转换/反转/曲解），README「残韵作用」269-274行：
        1. 残韵未生效则不消耗（路径不存在/校验失败/声明后未命中——一律不扣）
        2. 作用于非轮回者拥有的道纹（战斗内对怪物发动中的道纹插队）：
           仅改变本次发动的道纹结算，不改变怪物拥有的道纹；
           结算完成后，施法者永久获得变化后的道纹。
        3. 作用于轮回者拥有的道纹：该轮回者的对应道纹永久变为变化后的道纹，
           施法者不会因此获得该道纹。
        4. 战内插队：登记 pending_resonance，目标真实发动该道纹时被 _use_daowen_core
           命中——消耗/代价/目标要求/效果全部按新道纹原版公式结算（规则5）。
           新公式消耗无法满足（怪物无法力）时后续流程中断（发动失败）。
        5. 同魂笔：发动残韵时可选另一个[目标]，使其拥有的一种道纹受同种残韵影响
           （一次发动耗1个残韵，两处各自命中生效——消耗口径待DM裁定，如实标注）。
        params:
          resonance_type: 转换/反转/曲解
          source_daowen: 源道纹名
          on_monster: 怪物名（战内插队；不提供=作用于玩家自身道纹，永久变化）
          x: 命中后新道纹本次结算的X值（规则4自由控X，默认1）
          same_resonance_extra: {"target": 另一怪物名, "daowen": 其道纹名, "x": N}（同魂笔）
        """
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}

        source = params.get("source_daowen", "")
        rtype = params.get("resonance_type", "")
        x = int(params.get("x", 1) or 1)

        from .gamedata import IMPLEMENTED_DAOWEN

        # ---------- 全校验通过前绝不消耗（残韵未生效不消耗） ----------
        if self.state.resonance.get(rtype, 0) <= 0:
            return {"success": False, "error": f"没有可用的{rtype}残韵（当前：{self.state.resonance}）"}

        target_name = params.get("on_monster", "")
        monster = None
        if target_name:
            monster = next((m for m in self.state.enemies if m.name == target_name and m.is_alive), None)
            if monster is None:
                return {"success": False, "error": f"战场中不存在存活怪物: {target_name}"}
            if source not in monster.dao_wen:
                return {"success": False, "error": f"{target_name}的面板上没有道纹【{source}】"}
            # 同一（怪物,道纹）尚未生效的声明不重复登记
            if any(p["target_actor"] == target_name and p["source_daowen"] == source
                   for p in self.state.pending_resonance):
                return {"success": False,
                        "error": f"对【{target_name}】的【{source}】已声明过插队，不可重复（残韵未生效不浪费）"}
        else:
            if source not in player.dao_wen:
                return {"success": False, "error": f"玩家未持有道纹【{source}】（作用于怪物道纹请提供 on_monster）"}

        new_name = ResonanceEngine.find_transformation(source, rtype)
        if new_name is None:
            return {"success": False,
                    "error": f"道纹【{source}】不存在【{rtype}】变化路径，残韵未生效不消耗"}
        if new_name not in IMPLEMENTED_DAOWEN:
            return {"success": False, "unavailable": True,
                    "error": f"残韵变化结果【{new_name}】机制未实装，残韵未生效不消耗"}

        # 同魂笔：可选另一目标
        extra = params.get("same_resonance_extra")
        extra_pending = None
        if extra:
            if not self._has_relic("同魂笔"):
                return {"success": False, "error": "same_resonance_extra 需要遗物【同魂笔】"}
            ex_target_name = extra.get("target", "")
            ex_daowen = extra.get("daowen", "")
            ex_monster = next((m for m in self.state.enemies if m.name == ex_target_name and m.is_alive), None)
            if ex_monster is None:
                return {"success": False, "error": f"同魂笔另一目标不存在: {ex_target_name}"}
            if ex_daowen not in ex_monster.dao_wen:
                return {"success": False, "error": f"{ex_target_name}的面板上没有道纹【{ex_daowen}】"}
            ex_new = ResonanceEngine.find_transformation(ex_daowen, rtype)
            if ex_new is None:
                return {"success": False,
                        "error": f"同魂笔：【{ex_daowen}】不存在【{rtype}】变化路径，残韵未生效不消耗"}
            if ex_new not in IMPLEMENTED_DAOWEN:
                return {"success": False, "unavailable": True,
                        "error": f"同魂笔变化结果【{ex_new}】机制未实装，残韵未生效不消耗"}
            extra_pending = {"target_actor": ex_target_name, "source_daowen": ex_daowen,
                             "resonance_type": rtype, "new_daowen": ex_new,
                             "x": int(extra.get("x", 1) or 1), "via": "同魂笔"}

        # ---------- 校验全部通过 ----------
        if monster is not None:
            # 战内插队：登记 pending，命中时才消耗（残韵未生效不消耗）
            entry = {"target_actor": target_name, "source_daowen": source,
                     "resonance_type": rtype, "new_daowen": new_name, "x": max(1, x), "via": "残韵"}
            self.state.pending_resonance.append(entry)
            if extra_pending is not None:
                extra_pending["paired_with"] = (target_name, source)
                self.state.pending_resonance.append(extra_pending)
            return {
                "success": True,
                "action": f"残韵【{rtype}】插队声明：{target_name}的【{source}】将在其发动时被改写为【{new_name}{max(1, x)}】",
                "note": "残韵尚未消耗：目标真实发动被命中时才消耗；若本场其始终不发动则自动失效（未生效不消耗）。"
                        "命中后按新道纹原版公式结算（规则5），怪物无法力时消耗类新公式将中断（发动失败），"
                        "残韵改写即视为生效、施法者按规则2永久获得新道纹（命中时获得口径为假设，待DM裁定）。"
                        + (f" 同魂笔：{extra_pending['target_actor']}的【{extra_pending['source_daowen']}】"
                           f"同受【{rtype}】影响→【{extra_pending['new_daowen']}】" if extra_pending else ""),
                "pending_resonance": self.state.pending_resonance,
            }

        # 作用于玩家自身道纹：永久变化（规则3），施法者不获得
        if new_name in player.dao_wen:
            return {"success": False, "error": f"已持有【{new_name}】（道纹唯一），残韵未生效不消耗"}
        del player.dao_wen[source]
        player.dao_wen[new_name] = DaoWenInstance(dao_wen=self._build_daowen_def(new_name))
        # 此处才消耗（残韵生效）
        self.state.resonance[rtype] -= 1
        return {
            "success": True,
            "action": f"残韵【{rtype}】{source} → {new_name}",
            "effect": f"玩家持有的【{source}】永久变为【{new_name}】（施法者不获得，规则3）",
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
        if player.has_status("缓慢") and budget <= player.get_status_value("缓慢"):
            return {"success": False,
                    "error": f"缓慢：本回合出手{budget}≤{player.get_status_value('缓慢')}，无法出手"}

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
        # 回锋刀：闪避失去1速度→反击3点伤害
        if result.get("dodge_success") and target is self.state.player:
            note = self._huifengdao_on_speed_loss(1, source=attacker)
            if note:
                result["relic_回锋刀"] = note

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

        # 守夜灯：[敌回始]获得等同于[法限]50%的法力，每回合一次，该法力[敌回终]清空
        shouye_note = None
        player = self.state.player
        if player and player.is_alive and self._has_relic("守夜灯") \
                and self.state.relic_flags.get("守夜灯_round") != self.state.current_round:
            self.state.relic_flags["守夜灯_round"] = self.state.current_round
            grant = player.mana_limit // 2
            self.state.relic_flags["守夜灯_granted"] = grant
            if grant > 0:
                player.current_mana += grant
                shouye_note = (f"【守夜灯】[敌回始]获得{grant}点法力（法力→{player.current_mana}；"
                               f"随全部法力[敌回终]清空，README 213行）")

        # 行动禁止判定：束缚（无法行动）/ 眩晕（无法出手，受到伤害后解除）
        if monster.has_status("束缚"):
            return {"success": True, "action": f"{monster.name}的出手轮",
                    "skipped": "束缚：无法行动", "turn_log": [],
                    "relic_notes": ([shouye_note] if shouye_note else [])}
        if monster.has_status("眩晕"):
            return {"success": True, "action": f"{monster.name}的出手轮",
                    "skipped": "眩晕：无法出手", "turn_log": [],
                    "relic_notes": ([shouye_note] if shouye_note else [])}

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
                        "turn_log": [],
                        "relic_notes": ([shouye_note] if shouye_note else [])}

        has_kuangbao = monster.has_status("狂暴")
        max_acts = allowed + (1 if has_kuangbao else 0)

        if not acts:
            if max_acts <= 0:
                return {"success": True, "action": f"{monster.name}的出手轮",
                        "skipped": "无出手次数", "turn_log": [],
                        "relic_notes": ([shouye_note] if shouye_note else [])}
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
                    # 回锋刀：轮回者闪避失去1速度→反击3点伤害
                    if hit_result.get("dodge_success") and target is self.state.player:
                        note = self._huifengdao_on_speed_loss(1, source=monster)
                        if note:
                            hit_result["relic_回锋刀"] = note
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
            "relic_notes": ([shouye_note] if shouye_note else []),
        }

    def _action_friend_turn(self, params: dict) -> dict:
        """
        一名[朋友]/[员工]的完整出手轮（真实结算，公开行动接口）。
        README 254行：微光者出手次数=当攻击次数/3（向上取整，模拟口径与怪物一致，
        待DM裁定），每次出手可能是发动一轮攻击或发动一次自身道纹效果。
        acts: [{"type": "attack_round", "target": "<怪名>"},
               {"type": "use_daowen", "daowen": "<名>", "x": N, "target": "<名>"}]
        """
        fname = params.get("friend", "")
        friend = next((f for f in self.state.friends + self.state.employees + self.state.temp_friends
                       if f.name == fname and f.is_alive), None)
        if friend is None:
            return {"success": False, "error": f"[朋友]/[员工]不存在或已死亡: {fname}"}
        if self.state.phase not in ("in_combat",):
            return {"success": False, "error": f"当前阶段({self.state.phase})不能行动"}
        budget = max(1, math.ceil(friend.attack_count / 3))
        acts = params.get("acts", [])
        if len(acts) > budget:
            return {"success": False, "error": f"微光者出手次数超限：预算{budget}，提供{len(acts)}次"}

        turn_log = []
        for act in acts:
            act_type = act.get("type")
            if act_type == "attack_round":
                target_name = act.get("target", "")
                target = next((m for m in self.state.enemies if m.name == target_name and m.is_alive), None)
                if target is None:
                    turn_log.append({"type": "attack_round", "error": f"目标无效: {target_name}"})
                    continue
                if CombatEngine.is_flying(target) and not CombatEngine.is_flying(friend):
                    turn_log.append({"type": "attack_round", "error": f"{target.name}飞行中，无法选为目标"})
                    continue
                hits = []
                for hit_idx in range(max(1, friend.attack_count)):
                    if not target.is_alive:
                        break
                    hr = self.combat.resolve_attack(friend, target, hit_index=hit_idx,
                                                    is_must_hit=friend.has_status("必中"),
                                                    dodge=False)
                    hits.append(hr)
                turn_log.append({"type": "attack_round", "target": target.name, "hits": hits,
                                 "hits_landed": sum(1 for h in hits if h.get("damage_dealt", 0) > 0)})
            elif act_type == "use_daowen":
                r = self._use_daowen_core(
                    caster=friend,
                    name=act.get("daowen", ""),
                    x=act.get("x", 1),
                    target_name=act.get("target", friend.name),
                    target_dodge=act.get("target_dodge", False),
                    consume_action=False,
                    skip_mana_cost=True,   # 微光者面板道纹按怪物同例（待DM裁定）
                    extra=act.get("extra") or act,
                )
                turn_log.append({"type": "use_daowen", **r})
            else:
                turn_log.append({"type": act_type, "error": "未知行动类型（attack_round/use_daowen）"})

            if not self.state.get_all_enemy_side():
                break

        return {"success": True,
                "action": f"{friend.name}（微光者）的出手轮",
                "budget": budget, "acts_used": len(acts), "turn_log": turn_log}

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
        extra_notes = []
        player = self.state.player
        # 皮衣（事件遗物）：上回合失去生命时，本回合获得等量格挡（回合净损失口径，粒度待DM裁定）
        if player and self._has_relic("皮衣"):
            prev = self.state.relic_flags.get("皮衣_hp_prev")
            if prev is not None and player.current_hp < prev:
                lost = prev - player.current_hp
                player.gain_shield(lost)
                extra_notes.append(f"【皮衣】上回合失去{lost}生命→获得{lost}格挡")
            self.state.relic_flags["皮衣_hp_prev"] = player.current_hp
        # 负岳索（事件遗物）：指定朋友首次受到伤害时，自身回复等量生命（回合净损失口径）
        fname = self.state.relic_flags.get("负岳索_friend")
        if fname and player:
            f = next((f for f in self.state.friends + self.state.employees if f.name == fname), None)
            if f is None or not f.is_alive:
                self.state.relic_flags.pop("负岳索_friend", None)
            else:
                prev_hp = self.state.relic_flags.get("负岳索_hp", f.current_hp)
                if f.current_hp < prev_hp:
                    lost = prev_hp - f.current_hp
                    heal = player.heal(lost)
                    extra_notes.append(f"【负岳索】{fname}受到伤害{lost}→自身[回复]{heal['actual_heal']}（首次触发后失效）")
                    self.state.relic_flags.pop("负岳索_friend", None)
                    self.state.relic_flags.pop("负岳索_hp", None)
                else:
                    self.state.relic_flags["负岳索_hp"] = f.current_hp
        # 回锋刀[回始]：对[目标]造成3×（你的[速限]-你的当前速度）的伤害
        if player and player.is_alive and self._has_relic("回锋刀") \
                and self.state.phase == "in_combat":
            gap = player.speed_limit - player.current_speed
            if gap > 0:
                target = next((m for m in self.state.enemies if m.is_alive), None)
                if target is not None:
                    dmg = 3 * gap
                    res = target.take_damage(dmg, "回锋刀")
                    sink: dict = {}
                    self._sync_monster_death_hooks(sink)
                    died = "→[命零]" if res.get("died") else ""
                    absorbed = (f"（格挡吸收{res['shield_absorbed']}，净伤{res['actual_damage']}）"
                                if res.get("shield_absorbed") else "")
                    extra_notes.append(
                        f"【回锋刀】[回始]对{target.name}造成3×{gap}={dmg}点伤害{absorbed}"
                        f"（{res['hp_before']}→{res['hp_after']}{died}；[目标]按存活怪物首位顺延，假设待DM裁定）"
                        + ("；" + "；".join(sink["relic_notes"]) if sink.get("relic_notes") else ""))
        result = self.combat.round_start()
        # 储能电池（[回始]自动）：本回合额外获得12点法力
        batt = next((c for c in self.state.consumables if c.name == "储能电池" and c.current_uses > 0), None)
        if batt and player:
            batt.use()
            player.current_mana += 12
            extra_notes.append(f"【储能电池】[回始]本回合额外+12法力（耐久→{batt.current_uses}/{batt.max_uses}）")
            if batt.is_depleted:
                self.state.consumables.remove(batt)
                extra_notes.append("【储能电池】耐久归零，彻底消耗销毁")
        # 皮衣试穿（事件修饰）：下一场战斗第一[回始]获得30点格挡
        mods = self.state.relic_flags.get("_next_battle_mods_active", {})
        if self.state.current_round == 1 and mods.get("first_round_shield") and player:
            player.gain_shield(mods["first_round_shield"])
            extra_notes.append(f"皮衣试穿：第一[回始]获得{mods['first_round_shield']}点格挡")
        if extra_notes:
            result = dict(result)
            result["relic_notes"] = extra_notes
        return {"success": True, "action": "回始", "result": result}

    def _sync_monster_death_hooks(self, result: dict) -> None:
        """焦黑发丝（事件遗物）：每当场上有一个怪物死亡时，你的速度+2"""
        if not (self.state.player and self._has_relic("焦黑发丝")):
            return
        dead = sum(1 for m in self.state.enemies if not m.is_alive)
        prev = self.state.relic_flags.get("_monster_dead_count", 0)
        if dead > prev:
            diff = dead - prev
            self.state.relic_flags["_monster_dead_count"] = dead
            self.state.player.current_speed += 2 * diff
            notes = result.setdefault("relic_notes", [])
            notes.append(f"【焦黑发丝】{diff}个怪物死亡→速度+{2 * diff}（→{self.state.player.current_speed}）")

    def _action_round_end(self, params: dict) -> dict:
        """回终"""
        result = self.combat.round_end()
        # 守夜灯授予法力随全体法力在[敌回终]清空——由 combat.round_end 的
        # 全局法力清空（README 213行）统一结算，不再单列

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
        battle_end_notes = []
        # 战终兜底：战斗可能在敌方阶段内结束（未走[回终]全局清空），
        # 残留法力按"法力[敌回终]清空"（README 213行）与局内增益清除规则清零，不跨场滚存
        p0 = self.state.player
        self.state.relic_flags.pop("守夜灯_round", None)
        self.state.relic_flags.pop("守夜灯_granted", None)
        if p0 and p0.current_mana > 0:
            battle_end_notes.append(f"战终：残留法力{p0.current_mana}点按[敌回终]清空规则清零")
            p0.current_mana = 0

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

        # ---- 事件修饰的战终结算 ----
        mods = self.state.relic_flags.get("_next_battle_mods_active", {})
        battle_mod_notes = list(battle_end_notes)
        if not escaped:
            if mods.get("bounty_double") and shard_reward > 0:
                shard_reward *= 2
                battle_mod_notes.append("地下角斗场：双倍[碎片]战利品")
            if mods.get("battle_end_shards"):
                shard_reward += mods["battle_end_shards"]
                battle_mod_notes.append(f"通缉悬赏榜：+{mods['battle_end_shards']}[碎片]悬赏金")
            if mods.get("win_in_3_rounds_bonus") and self.state.current_round <= 3 \
                    and not [m for m in self.state.enemies if m.is_alive]:
                self.state.shards += mods["win_in_3_rounds_bonus"]
                battle_mod_notes.append(f"盘外博彩：3回合内结束战斗，+{mods['win_in_3_rounds_bonus']}[碎片]")

        self.state.shards += shard_reward

        # 三相残韵盘：[战终]获得另外两种残韵各1个
        pan_back = self.state.relic_flags.pop("三相残韵盘_待返", None)
        if pan_back:
            for rt in pan_back:
                self.state.resonance[rt] = self.state.resonance.get(rt, 0) + 1
            self.state.relic_flags.pop("三相残韵盘_消耗", None)
            battle_mod_notes.append(f"【三相残韵盘】[战终]获得残韵：{pan_back[0]}×1、{pan_back[1]}×1")

        # 猩红果实/苍白之花：战始支付过的，[战终]结算收益
        if self.state.relic_flags.pop("猩红果实_used", False) and self.state.player:
            self.state.player.blood_limit += 2
            battle_mod_notes.append("【猩红果实】[战终][血限]+2")
        pale_bonus = self.state.relic_flags.pop("苍白之花_used", False)

        # 手术植入倒计时：每场战终-1场，到0仍保持原样则该微光者变为怪物
        for fname in list(self.state.implant_flags.keys()):
            self.state.implant_flags[fname] -= 1
            remaining = self.state.implant_flags[fname]
            if remaining <= 0:
                del self.state.implant_flags[fname]
                for lst in (self.state.friends, self.state.employees):
                    f = next((f for f in lst if f.name == fname), None)
                    if f is not None:
                        lst.remove(f)
                        battle_mod_notes.append(f"【{fname}】被植入的怪物道纹保持原样已达3场——其彻底变为怪物，脱离队伍（不再作为[朋友]结算）")
                        break
            else:
                battle_mod_notes.append(f"手术植入倒计时：【{fname}】剩余{remaining}场")

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

        # 恢复精力（苍白之花：战始疲惫5的，[战终]精力+1）
        self.state.energy = 3 + (1 if pale_bonus else 0)

        # 假碎片清空（战终清除局内资源）
        self.state.fake_shards = 0

        result_body = {
            "escaped": escaped,
            "retreat": retreat_detail,
            "shard_reward": shard_reward,
            "reward_detail": reward_detail,
            "battle_mod_notes": battle_mod_notes,
            "total_shards": self.state.shards,
            "cleared_entities": cleared,
            "energy_restored": self.state.energy,
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

            if purpose == "explore":
                pool = meta["pool"]
                if not 1 <= number <= len(pool):
                    return {"success": False, "error": f"数字{number}超出范围 1~{len(pool)}"}
                meta["draws"].append(number)
                remaining = meta["count"] - len(meta["draws"])
                if remaining > 0:
                    return {
                        "success": True,
                        "action": f"探索：第{len(meta['draws'])}个发现 = 【{pool[number - 1]}】",
                        "random_required": True, "range": f"1~{len(pool)}",
                        "instruction": f"还需发现{remaining}个，请继续给出数字（重复数字视为重复发现同一事件）",
                    }
                self._pending_random = None
                return self._resolve_explore(meta)

            if purpose == "discover_tool":
                pool = meta["pool"]
                if not 1 <= number <= len(pool):
                    return {"success": False, "error": f"数字{number}超出范围 1~{len(pool)}"}
                self._pending_random = None
                name = pool[number - 1]
                gain = self._gain_consumable(name)
                return {"success": True, "action": "扭曲都市废墟设施工具库·发现",
                        "gained": gain,
                        "note": "遵守消耗品规则：耐久归零后彻底消耗销毁，无法再局外【维修】"}

            if purpose == "event_gamble":
                return self._resolve_event_gamble(pending, number)

            if purpose == "event_implant":
                pool = meta["pool"]
                if not 1 <= number <= len(pool):
                    return {"success": False, "error": f"数字{number}超出范围 1~{len(pool)}"}
                self._pending_random = None
                dw = pool[number - 1]
                friend_name = meta["friend_name"]
                friend = next((f for f in self.state.friends + self.state.employees
                               + self.state.temp_friends if f.name == friend_name and f.is_alive), None)
                if friend is None:
                    return {"success": False, "error": f"微光者队友不存在: {friend_name}"}
                friend.dao_wen[dw] = DaoWenInstance(dao_wen=self._build_daowen_def(dw))
                self.state.implant_flags[friend_name] = 3
                return {"success": True, "action": f"手术·强制移植：{friend_name}被植入【{dw}】",
                        "note": "该道纹每场战斗三回合后若保持原样，则该微光者仍变为怪物（记入倒计时3场）"}

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
