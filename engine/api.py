"""
游戏引擎 API - AI的唯一交互入口
核心原则：
1. AI通过此API获取状态、做出决策、触发行动
2. 所有数值计算由引擎完成，AI禁止自行计算
3. 程序无法判定时返回Interrupt，等待DM裁定
4. DM裁定存入数据库，下次类似情况自动匹配
"""
from __future__ import annotations
import base64
import copy
import dataclasses
import json
import pickle
import os
import re
import time
import uuid
from typing import Optional, Any

from .models import (
    Entity, GameState, DaoWen, DaoWenInstance, Spell,
    Relic, Consumable, StatusEffect, LongJiXin
)
from .enums import GamePhase, CombatSubphase, ActionPhase, TriggerTiming, InterruptType, EntityType
from .dice import DiceEngine
from .daowen import DaoWenEngine, ResonanceEngine
from .combat import CombatEngine
from .events import EventPool, parse_events
from .dungeons import DEFAULT_INDEX
from .gamedata import (REGION_EXCLUSIVE_DAOWEN, ORIGINAL_MONSTER_DAOWEN,
                       MONSTER_TRANSFORM_DAOWEN)
from .dm_rulings import DMRulingsDB, DMRuling, Interrupt
from .death_book import DeathBookStore, draft_legacy, validate_legacy


# 扭曲都市废墟设施工具库（README正文8件：名→(耐久, 效果文本逐字)）
TWISTED_TOOL_LIBRARY = {
    "反怪物电击枪": (3, "对一个[目标]造成25点伤害；若[目标]处于【飞行】，额外造成15点伤害并施加【坠落1】"),
    "备用血泵": (3, "使自身获得20点［回复］；若自身当前生命≤30%，额外获得30点格挡。"),
    "强光探照灯": (2, "使一个[目标]陷入【蒙蔽2】"),
    "高压水枪": (2, "清除全场所有敌方[目标]身上的所有“持续X”效果"),
    "储能电池": (3, "立即获得12点法力。"),
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
                 rng_seed: Optional[int] = None, sealed_candidate_path: str = "data/sealed_candidate.json",
                 death_book_path: str = "死者之书.md"):
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
        self.death_book_path = death_book_path
        self.death_book = DeathBookStore(death_book_path)
        self.state.death_book_legacies = self.death_book.load()
        os.makedirs(save_dir, exist_ok=True)

        # 规则校验器（延迟导入避免循环）
        self._validator = None
        self._rule_sync = None

        # 中断队列（等待DM裁定）
        self._pending_interrupts: list[Interrupt] = []
        # 体外心脏：记录[战始]翻倍前的血限基准，[战终]用于还原
        self._artifact_base_blood_limit = 0

        # 事件系统
        self.event_pool = EventPool(parse_events(DEFAULT_INDEX) if DEFAULT_INDEX.exists() else {})
        # 怪物池（出怪系统）：从全副本索引加载，不再解析 README。
        from .monsters import parse_monster_pool
        self.monster_pool = parse_monster_pool(DEFAULT_INDEX) if DEFAULT_INDEX.exists() else {}
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

    def enable_rule_sync(self, rule_files: list[str] | None = None, rules_dir: str = "."):
        """启用规则同步；默认跟踪全部正文事实源。"""
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
        if self.state.pending_relic_choices:
            return {
                "phase": "遗物发现待选",
                "actions": [{"type": "choose_discovered_relic",
                             "relic_name": name} for name in self.state.pending_relic_choices],
                "source": self.state.pending_relic_source,
            }
        if self.state.pending_item_choices:
            return {
                "phase": "消耗品发现待选",
                "actions": [{"type": "choose_discovered_item",
                             "item_name": name} for name in self.state.pending_item_choices],
                "source": self.state.pending_item_source,
            }
        if self.state.pending_attack:
            pending = self.state.pending_attack
            return {
                "phase": "攻击待提交",
                "action_type": "resolve_attack",
                "params_schema": {"token": pending["token"], "hits": pending["options"]["hits_schema"]},
                **pending["options"],
            }
        if self.state.pending_monster_phase:
            pending = self.state.pending_monster_phase
            return {
                "phase": "怪物阶段待提交",
                "action_type": "resolve_monster_phase",
                "params_schema": {"token": pending["token"], "choices": "为全部actors各提交一次"},
                **pending["options"],
            }

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
        actions = []
        if self.state.player is None:
            actions.append({"action_type": "setup_attributes", "params_schema": {
                "name": "string", "blood_points": "integer", "speed_points": "integer",
                "mana_points": "integer", "constraint": "sum=25"}})
        else:
            if not self.state.player.dao_wen:
                actions.append({"action_type": "setup_choose_daowen",
                                "params_schema": {"daowen": ["杀伐", "锐利"]}})
            if not self.state.resonance:
                actions.append({"action_type": "setup_choose_resonance",
                                "params_schema": {"resonance_type": ["转换", "反转", "曲解"]}})
            if not self.state.current_region:
                actions.append({"action_type": "setup_choose_region",
                                "params_schema": {"region": ["罪孽都市", "扭曲都市", "龙心谷"]}})
        return {"phase": GamePhase.SETUP.value, "actions": actions}

    def _get_pre_battle_actions(self) -> dict:
        tiers = {
            "领悟": [1, 2, 3], "休整": [1, 2, 3], "修行": [1, 2, 3, 4, 5, 6],
            "学习": [1, 2], "共鸣": [1, 2], "探索": [1],
        }
        actions = [{"action_type": "pre_battle_action",
                    "params_schema": {"sub_action": name, "tier": values,
                                      "additional": "按action返回的正文选项显式提交"}}
                   for name, values in tiers.items()]
        for region_action in self._get_region_actions():
            actions.append({"action_type": "pre_battle_action",
                            "params_schema": {"sub_action": region_action["id"],
                                              "additional": region_action["params_schema"]}})
        if any(entity.name == "医生" and entity.is_alive for entity in self.state.employees):
            actions.append({"action_type": "upgrade_doctor",
                            "params_schema": {"mode": ["attack_count", "attack_power"]},
                            "cost": "5碎片，不消耗精力"})
        if any(relic.name == "忘忧香" for relic in self.state.relics):
            actions.append({"action_type": "pre_battle_action",
                            "params_schema": {"sub_action": "忘忧", "tier": [1, 2, 3],
                                              "daowen_names": "完整失忆选择"}})
        if self.state.has_sacrifice_action:
            actions.append({"action_type": "pre_battle_action",
                            "params_schema": {"sub_action": "献祭", "tier": [1, 2, 3]}})
        if "真龙之心" in self.state.artifacts_owned:
            actions.extend([
                {"action_type": "pay_for_dragon_nature",
                 "params_schema": {"cost_type": ["衰老", "枯竭", "萎缩"], "x": "integer"}},
                {"action_type": "unlock_dragon_trait",
                 "params_schema": {"trait": [name for name in self.DRAGON_TRAITS
                                                if name not in self.state.dragon_traits]}},
            ])
        for employee in self.state.employees:
            if employee.is_debt_bound and employee.shards < 0:
                actions.append({"action_type": "repay_debt_employee",
                                "params_schema": {"name": employee.name, "amount": -employee.shards}})
        for employee_name, choices in self.state.pending_daowen_choices.items():
            actions.append({"action_type": "choose_hired_daowen",
                            "params_schema": {"name": employee_name, "daowen": choices}})
        if self.state.pending_terminal_region:
            actions.append({"action_type": "choose_terminal_artifact",
                            "params_schema": {"choice": "listed terminal artifact index"}})
        if self.state.pending_first_embrace:
            actions.append({"action_type": "choose_first_embrace",
                            "params_schema": {"choice": "1..9"}})
        if self.state.energy <= 0:
            actions.append({"action_type": "battle_start",
                            "params_schema": {"relic_choices": "逐件显式提交可选战始遗物"}})
        return {"phase": GamePhase.PRE_BATTLE.value, "energy": self.state.energy, "actions": actions}

    def _get_region_actions(self) -> list[dict]:
        if self.state.current_region == "扭曲都市":
            return [{"id": "维修", "params_schema": {"tier": [1, 2, 3], "allocations": "耐久分配"}}]
        if self.state.current_region == "罪孽都市":
            return [{"id": "雇佣", "params_schema": {"name": "string", "blood_alloc": "integer",
                                                       "atk_bundles": "integer"}}]
        if self.state.current_region == "龙心谷":
            return [{"id": "炼心", "params_schema": {}}]
        return []

    def _get_battle_start_actions(self) -> dict:
        actions = [{"action_type": "round_start",
                    "params_schema": {"relic_choices": "按当前回始遗物逐件显式提交"}}]
        if "黑金名片" in self.state.artifacts_owned:
            actions.insert(0, {"action_type": "use_black_card", "params_schema": {}})
        if "共心环" in self.state.artifacts_owned:
            actions.insert(0, {"action_type": "select_shared_dragon_heart",
                               "params_schema": {"dragon_heart_type": "owned cost type"}})
        if "罪业金库" in self.state.artifacts_owned:
            actions.insert(0, {"action_type": "use_crime_vault", "params_schema": {"x": "integer"}})
        if "烬翼" in self.state.dragon_traits:
            actions.insert(0, {"action_type": "use_dragon_wings", "params_schema": {"x": "integer"}})
        if self.state.in_final_duel:
            actions.insert(0, {"action_type": "activate_duel_relic",
                               "params_schema": {"side": ["player_side", "opponent_side"],
                                                 "relic": "持有者的可选战始遗物参数"}})
        return {"phase": CombatSubphase.AWAIT_ROUND_START.value,
                "current_battle": self.state.current_battle, "actions": actions}

    def _get_combat_actions(self) -> dict:
        """返回可直接交给 execute_action 的战斗 action schema。"""
        subphase = self.state.combat_subphase
        if subphase == CombatSubphase.AWAIT_ROUND_START.value:
            return self._get_battle_start_actions()
        if subphase == CombatSubphase.MONSTER_ACTIONS.value:
            return {"phase": subphase, "actions": [
                {"action_type": "prepare_monster_phase", "params_schema": {}}
            ]}
        if subphase == CombatSubphase.AWAIT_ROUND_END.value:
            return self._get_battle_end_actions()

        player = self.state.player
        if not player or not player.is_alive:
            return {"phase": subphase, "actions": [], "note": "轮回者已死亡"}
        refs = self.combat._combat_entity_refs()
        target_options = [{"ref": ref, "name": entity.name} for ref, entity in refs.items()]
        actions: list[dict] = []
        for name, instance in player.dao_wen.items():
            if not instance.can_use():
                actions.append({"action_type": "use_daowen", "available": False,
                                "params_schema": {"daowen_name": name},
                                "reason": "冷却中或被封印"})
                continue
            max_x = self._max_legal_daowen_x(player, name)
            actions.append({
                "action_type": "use_daowen", "available": max_x >= 1,
                "params_schema": {
                    "daowen_name": name,
                    "x": {"type": "integer", "minimum": 1, "maximum": max_x},
                    "target_ref": target_options,
                    "dodge": "敌对单目标时必填布尔值",
                    "blood_shadow": "目标持有血影时必填布尔值",
                    "spell_choices": "若触发法术，按法术选项完整提交",
                },
            })
        for actor_ref, actor in refs.items():
            if actor is player or not self.state.on_player_side(actor):
                continue
            for name, instance in actor.dao_wen.items():
                if not instance.can_use():
                    continue
                fixed_x = instance.x_value if instance.x_value > 0 else 1
                actions.append({"action_type": "use_daowen", "available": True,
                                "params_schema": {"actor_ref": actor_ref, "daowen_name": name,
                                                  "x": fixed_x, "target_ref": target_options,
                                                  "dodge": "boolean", "blood_shadow": "boolean",
                                                  "trigger_spell_choices": "complete object"}})
        for spell in player.spells:
            actions.append({"action_type": "use_spell", "params_schema": {"spell_name": spell.name},
                            "available": all(n in player.dao_wen and player.dao_wen[n].can_use()
                                             for n in spell.required_daowen),
                            "note": "法术实际结算在对应判定的 spell_choices 中显式提交"})
        if any(value > 0 for value in self.state.resonance.values()):
            actions.append({"action_type": "use_resonance", "params_schema": {
                "resonance_type": [n for n, v in self.state.resonance.items() if v > 0],
                "source_daowen": "道纹名", "target_ref": target_options,
            }})
        for item in self.state.consumables:
            if not item.is_depleted:
                actions.append({"action_type": "consume_item", "params_schema": {
                    "name": item.name, "target_ref": target_options,
                }, "uses_remaining": item.current_uses})
        plight_options = self.combat.get_plight_evolution_options()
        actions.append({"action_type": "declare_evolution", "available": bool(plight_options),
                        "params_schema": {"monster": "string", "daowen": "string", "x": "integer"},
                        "plight_monsters": plight_options})
        actor_options = [{"ref": ref, "name": entity.name} for ref, entity in refs.items()
                         if self.state.on_player_side(entity) and entity.is_alive]
        for employee_index, employee in enumerate(self.state.employees):
            if employee.is_alive and not employee.is_deployed and not employee.is_debt_bound:
                actions.append({"action_type": "deploy_employee",
                                "params_schema": {"employee_ref": f"employee:{employee_index}",
                                                  "name": employee.name}})
        artifact_actions = {
            "教父左轮": ("fire_godfather_revolver", {"target_ref": target_options}),
            "负岳碑": ("declare_fuyuebei_toll", {"target_ref": target_options}),
        }
        for artifact in self.state.artifacts_owned:
            if artifact in artifact_actions:
                action_type, schema = artifact_actions[artifact]
                actions.append({"action_type": action_type, "params_schema": schema})
        trait_actions = {
            "震岳龙躯": ("activate_dragon_body", {"x": "integer"}),
            "吞骸龙胃": ("devour_monster", {"monster_ref": "enemy:index", "dragon_heart": "name"}),
            "断尾求生": ("declare_tail_sacrifice", {"trait": "owned dragon relic"}),
            "鲜血之翼": ("use_blood_wings", {"x": "integer"}),
            "血族尖牙": ("enslave_as_chizu", {"target_ref": target_options}),
            "真理眼": ("use_truth_eye", {"target_ref": target_options, "statement": "string"}),
            "血食": ("blood_feast", {"target_ref": target_options}),
        }
        for trait in self.state.dragon_traits + self.state.first_embrace_traits:
            if trait in trait_actions:
                action_type, schema = trait_actions[trait]
                actions.append({"action_type": action_type, "params_schema": schema})
        actions.extend([
            {"action_type": "prepare_attack", "params_schema": {"actor_ref": actor_options}},
            {"action_type": "declare_wit", "params_schema": {"target_ref": target_options}},
            {"action_type": "declare_escape", "params_schema": {}},
            {"action_type": "prepare_monster_phase", "params_schema": {},
             "note": "结束己方行动阶段并进入怪物行动"},
        ])
        return {"phase": subphase, "round": self.state.current_round, "actions": actions}

    def _max_legal_daowen_x(self, actor: Entity, name: str) -> int:
        """按真实代价口径枚举当前可提交的最大X，不把当前法力直接冒充上限。"""
        upper = max(1, actor.current_mana, actor.current_hp, actor.blood_limit,
                    actor.current_speed, self.state.shards, self.state.fake_shards, 50)
        legal = 0
        for x in range(1, upper + 1):
            try:
                calc = DaoWenEngine.resolve(name, x, target=actor, caster=actor)
            except Exception:
                continue
            ctype = calc.get("cost_type")
            if ctype == "消耗" and calc.get("cost", 0) > actor.current_mana:
                continue
            if ctype == "流血" and calc.get("cost", 0) >= actor.current_hp:
                continue
            if ctype == "疲惫" and calc.get("cost", 0) > actor.current_speed:
                continue
            if name == "赌命" and self.state.fake_shards < calc.get("fake_cost", x):
                continue
            legal = x
        return legal

    def _get_battle_end_actions(self) -> dict:
        actions = [{"action_type": "round_end", "params_schema": {}}]
        if not any(enemy.is_alive for enemy in self.state.enemies):
            actions.append({"action_type": "battle_end", "params_schema": {}})
        return {"phase": CombatSubphase.AWAIT_ROUND_END.value, "actions": actions}

    def _get_dead_duel_actions(self) -> dict:
        return {"phase": "最终死斗", "actions": self._get_combat_actions().get("actions", []),
                "note": "无法逃跑；必须遵守duel_turn和战斗子阶段"}

    # ==================== 核心行动接口 ====================

    _COMBAT_ONLY_ACTIONS = {
        "prepare_attack", "resolve_attack", "attack", "declare_wit", "declare_escape",
        "retreat_via_toll", "deploy_employee", "lianxin_in_battle", "declare_evolution",
        "prepare_monster_phase", "resolve_monster_phase", "monster_phase",
        "round_start", "round_end", "resolve_rebellion_battle",
        "activate_duel_relic", "resolve_final_duel", "use_black_card", "use_crime_vault",
        "fire_godfather_revolver", "select_shared_dragon_heart", "declare_fuyuebei_toll",
        "activate_dragon_body", "devour_monster", "declare_tail_sacrifice",
        "use_dragon_wings", "use_blood_wings", "enslave_as_chizu", "blood_feast",
    }

    def _restore_state_in_place(self, snapshot: GameState) -> None:
        """事务失败时原位恢复，既回滚数值，也保持调用方已持有的实体引用有效。"""
        def restore_object(current, saved):
            current_keys = set(current.__dict__)
            saved_keys = set(saved.__dict__)
            for key in current_keys - saved_keys:
                delattr(current, key)
            for key in saved_keys:
                saved_value = getattr(saved, key)
                if not hasattr(current, key):
                    setattr(current, key, copy.deepcopy(saved_value))
                    continue
                current_value = getattr(current, key)
                if (dataclasses.is_dataclass(current_value)
                        and dataclasses.is_dataclass(saved_value)
                        and type(current_value) is type(saved_value)):
                    restore_object(current_value, saved_value)
                elif isinstance(current_value, list) and isinstance(saved_value, list):
                    if (len(current_value) == len(saved_value)
                            and all(dataclasses.is_dataclass(a) and dataclasses.is_dataclass(b)
                                    and type(a) is type(b) for a, b in zip(current_value, saved_value))):
                        for a, b in zip(current_value, saved_value):
                            restore_object(a, b)
                    else:
                        current_value[:] = copy.deepcopy(saved_value)
                elif isinstance(current_value, dict) and isinstance(saved_value, dict):
                    if (set(current_value) == set(saved_value)
                            and all(dataclasses.is_dataclass(current_value[k])
                                    and dataclasses.is_dataclass(saved_value[k])
                                    and type(current_value[k]) is type(saved_value[k])
                                    for k in current_value)):
                        for k in current_value:
                            restore_object(current_value[k], saved_value[k])
                    else:
                        current_value.clear()
                        current_value.update(copy.deepcopy(saved_value))
                else:
                    setattr(current, key, copy.deepcopy(saved_value))

        restore_object(self.state, snapshot)

    def _snapshot_combat_runtime(self) -> dict:
        """保存不在GameState内、但会被行动修改的战斗运行态，供失败事务回滚。"""
        groups = {
            "player": [self.state.player] if self.state.player else [],
            "friend": self.state.friends,
            "employee": self.state.employees,
            "temp_friend": self.state.temp_friends,
            "enemy": self.state.enemies,
        }
        refs = {(kind, i): entity for kind, entities in groups.items() for i, entity in enumerate(entities)}
        by_id = {id(entity): ref for ref, entity in refs.items()}
        return {
            "activated": {by_id[key]: set(value) for key, value in self.combat._monster_activated.items()
                          if key in by_id},
            "rewrites": {by_id[key]: dict(value) for key, value in self.combat._resonance_rewrites.items()
                         if key in by_id},
            "cost_proxy": by_id.get(id(self.combat.cost_proxy)) if self.combat.cost_proxy else None,
            "sanxiang": self.combat._sanxiang_consumed,
        }

    def _restore_combat_runtime(self, snapshot: dict) -> None:
        groups = {
            "player": [self.state.player] if self.state.player else [],
            "friend": self.state.friends,
            "employee": self.state.employees,
            "temp_friend": self.state.temp_friends,
            "enemy": self.state.enemies,
        }
        refs = {(kind, i): entity for kind, entities in groups.items() for i, entity in enumerate(entities)}
        self.combat._monster_activated = {
            id(refs[ref]): set(value) for ref, value in snapshot["activated"].items() if ref in refs
        }
        self.combat._resonance_rewrites = {
            id(refs[ref]): dict(value) for ref, value in snapshot["rewrites"].items() if ref in refs
        }
        self.combat.cost_proxy = refs.get(snapshot["cost_proxy"])
        self.combat._sanxiang_consumed = snapshot["sanxiang"]

    def _phase_error(self, action_type: str, params: dict) -> Optional[dict]:
        """执行入口的阶段门禁；【消灾】是唯一允许在局外发动的道纹。"""
        phase = self.state.phase
        if action_type.startswith("setup_") and phase != "setup":
            return {"success": False, "error": f"【{action_type}】只能在开局阶段执行"}
        if action_type in ("pre_battle_action", "resolve_event", "upgrade_doctor") and phase != "pre_battle":
            return {"success": False, "error": "局外行动/事件只能在局外阶段执行"}
        if action_type == "battle_start" and phase != "pre_battle":
            return {"success": False, "error": "只有局外阶段可以进入战始"}
        if action_type == "battle_start" and self.state.energy > 0:
            return {"success": False, "error": f"尚有{self.state.energy}点精力，耗尽后才能进入战斗"}
        if action_type == "use_daowen":
            name = params.get("daowen_name", "")
            if phase == "pre_battle" and name == "消灾":
                return None
            if phase != "in_combat":
                return {"success": False, "error": "道纹只能在战斗中发动；局外唯一例外是【消灾】"}
        if action_type in self._COMBAT_ONLY_ACTIONS and phase != "in_combat":
            return {"success": False, "error": f"【{action_type}】只能在战斗中执行"}
        if action_type == "battle_end" and phase != "in_combat":
            return {"success": False, "error": "只有进行中的战斗可以结算战终"}
        if phase == "in_combat":
            subphase = self.state.combat_subphase
            required = {
                "round_start": CombatSubphase.AWAIT_ROUND_START.value,
                "prepare_monster_phase": CombatSubphase.PLAYER_ACTIONS.value,
                "resolve_monster_phase": CombatSubphase.MONSTER_ACTIONS.value,
                "round_end": CombatSubphase.AWAIT_ROUND_END.value,
                "prepare_attack": CombatSubphase.PLAYER_ACTIONS.value,
                "resolve_attack": CombatSubphase.PLAYER_ACTIONS.value,
            }.get(action_type)
            player_actions = {
                "use_daowen", "use_spell", "use_resonance", "consume_item",
                "declare_wit", "declare_escape", "retreat_via_toll", "deploy_employee",
                "lianxin_in_battle", "declare_evolution", "activate_duel_relic",
                "use_black_card", "use_crime_vault", "fire_godfather_revolver",
                "select_shared_dragon_heart", "declare_fuyuebei_toll", "activate_dragon_body",
                "devour_monster", "declare_tail_sacrifice", "use_dragon_wings",
                "use_blood_wings", "enslave_as_chizu", "blood_feast",
            }
            if action_type in player_actions:
                required = CombatSubphase.PLAYER_ACTIONS.value
            if action_type == "consume_item" and params.get("name") == "活性土壤":
                required = CombatSubphase.AWAIT_ROUND_START.value
            if action_type in ("use_black_card", "select_shared_dragon_heart",
                               "use_crime_vault", "use_dragon_wings"):
                required = CombatSubphase.AWAIT_ROUND_START.value
            if action_type == "activate_duel_relic" and self.state.in_final_duel:
                required = CombatSubphase.AWAIT_ROUND_START.value
            if action_type == "declare_escape" and self.state.in_final_duel:
                required = None
            if required and subphase != required:
                return {"success": False,
                        "error": f"【{action_type}】要求战斗子阶段{required}，当前为{subphase}"}
        return None

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

        if self.state.pending_relic_choices and action_type != "choose_discovered_relic":
            return {
                "success": False,
                "error": "必须先从发现候选中选择1件遗物",
                "choices": list(self.state.pending_relic_choices),
            }
        if self.state.pending_item_choices and action_type != "choose_discovered_item":
            return {
                "success": False,
                "error": "必须先从发现候选中选择1件消耗品",
                "choices": list(self.state.pending_item_choices),
            }
        if self.state.pending_attack and action_type != "resolve_attack":
            return {
                "success": False,
                "error": "已有待提交的攻击决策，请先调用resolve_attack",
                "token": self.state.pending_attack.get("token"),
            }
        if self.state.pending_monster_phase and action_type != "resolve_monster_phase":
            return {
                "success": False,
                "error": "已有待提交的怪物阶段决策，请先调用resolve_monster_phase",
                "token": self.state.pending_monster_phase.get("token"),
            }
        # 检查是否有待处理的中断
        if self._pending_interrupts:
            player_dead = (self.state.player is None) or (not self.state.player.is_alive)
            return {
                "success": False,
                "error": "有待处理的中断等待DM裁定",
                "pending_interrupts": [i.to_dict() for i in self._pending_interrupts],
                "result": {"player_dead": player_dead},
                "instruction": "请先通过 submit_ruling() 提交DM裁定"
            }

        queued = self._queue_death_inheritance_if_needed(action_type)
        if queued:
            return {
                "success": False,
                "error": "轮回者已命零，死之传承等待审核",
                "interrupt": queued.to_dict(),
                "pending_interrupts": [i.to_dict() for i in self._pending_interrupts],
                "result": {"player_dead": True},
                "instruction": "请先通过 submit_ruling() 审核遗言（approve/edit/reject）",
            }

        phase_error = self._phase_error(action_type, params)
        if phase_error:
            return phase_error

        # 所有行动按“校验失败不改变游戏状态”的原子契约执行。
        # 随机数请求必须在各处理器完成静态校验后才发起，避免失败消耗随机源。
        state_before = copy.deepcopy(self.state)
        combat_runtime_before = self._snapshot_combat_runtime()
        event_pool_before = (set(self.event_pool.triggered), self.event_pool.current)
        dice_before = copy.deepcopy(self.dice)
        interrupts_before = copy.deepcopy(self._pending_interrupts)
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
            elif action_type == "upgrade_doctor":
                result = self._action_upgrade_doctor(params)
            elif action_type == "use_daowen":
                result = self._action_use_daowen(params)
            elif action_type == "use_spell":
                result = self._action_use_spell(params)
            elif action_type == "use_resonance":
                result = self._action_use_resonance(params)
            elif action_type == "prepare_attack":
                result = self._action_prepare_attack(params)
            elif action_type == "resolve_attack":
                result = self._action_resolve_attack(params)
            elif action_type == "attack":
                result = {"success": False, "error": "旧attack已移除；请使用prepare_attack/resolve_attack"}
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
            elif action_type == "activate_duel_relic":
                result = self._action_activate_duel_relic(params)
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
            elif action_type == "prepare_monster_phase":
                result = self._action_prepare_monster_phase(params)
            elif action_type == "resolve_monster_phase":
                result = self._action_resolve_monster_phase(params)
            elif action_type == "monster_phase":
                result = self._action_monster_phase(params)
            elif action_type == "choose_discovered_relic":
                result = self._action_choose_discovered_relic(params)
            elif action_type == "choose_discovered_item":
                result = self._action_choose_discovered_item(params)
            elif action_type == "repay_debt_employee":
                result = self._action_repay_debt_employee(params)
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
            else:
                result = {"success": False, "error": f"未知行动类型: {action_type}"}

            if not result.get("success", False):
                self._restore_state_in_place(state_before)
                self.combat.state = self.state
                self._restore_combat_runtime(combat_runtime_before)
                self.event_pool.triggered, self.event_pool.current = event_pool_before
                self.dice = dice_before
                self.combat.dice = self.dice
                self._pending_interrupts = interrupts_before

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

            queued = self._queue_death_inheritance_if_needed(action_type)
            if queued:
                result["interrupt"] = queued.to_dict()
                result["instruction"] = (
                    (result.get("instruction") or "")
                    + " 轮回者命零，死之传承等待审核；请调用 submit_ruling(approve/edit/reject)。"
                ).strip()

            self._last_result = result
            return result

        except Exception as e:
            self._restore_state_in_place(state_before)
            self.combat.state = self.state
            self._restore_combat_runtime(combat_runtime_before)
            self.event_pool.triggered, self.event_pool.current = event_pool_before
            self.dice = dice_before
            self.combat.dice = self.dice
            self._pending_interrupts = interrupts_before
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
        if self.state.player is not None:
            return {"success": False, "error": "初始属性已经分配，不能重复开局"}
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
            attack_count=0,
            attack_power=0,
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
                "attack_count": player.attack_count,
                "attack_power": player.attack_power,
                "action_count": player.action_count,
                "shards": 20
            },
            "next_actions": ["setup_choose_daowen", "setup_choose_resonance", "setup_choose_region"],
            "note": "接下来需要：选择初始道纹、选择残韵、选择副本。遗物发现需要随机数。"
        }

    def _action_setup_choose_daowen(self, params: dict) -> dict:
        """选择初始道纹"""
        if self.state.player is None:
            return {"success": False, "error": "请先分配初始属性"}
        if self.state.player.dao_wen:
            return {"success": False, "error": "初始道纹已经选择，不能重复选择"}
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
        """选择副本，并生成开局“随机3件、显式选1件”的遗物发现。"""
        if self.state.player is None:
            return {"success": False, "error": "请先分配初始属性"}
        if not self.state.player.dao_wen or sum(self.state.resonance.values()) != 1:
            return {"success": False, "error": "选择副本前必须先完成初始道纹与1种初始残韵选择"}
        region = params.get("region", "")
        valid = ["罪孽都市", "扭曲都市", "龙心谷"]
        if region not in valid:
            return {"success": False, "error": f"只能从{valid}中选择"}
        self.state.current_region = region
        self.state.phase = "pre_battle"
        self._init_relic_pool()
        discovery = self._offer_relic_discovery("开局发现")
        if not discovery.get("success"):
            return discovery
        return {
            "success": True,
            "action": "选择副本",
            "result": {"region": region, "relic_choices": discovery["choices"]},
            "next_actions": ["choose_discovered_relic"],
            "note": "开局发现已随机列出3件遗物；必须显式选择1件后进入局外行动。",
        }

    def _action_setup_choose_resonance(self, params: dict) -> dict:
        """选择初始残韵"""
        if self.state.player is None:
            return {"success": False, "error": "请先分配初始属性"}
        if sum(self.state.resonance.values()) > 0:
            return {"success": False, "error": "初始残韵已经选择，不能重复选择"}
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

    def _action_upgrade_doctor(self, params: dict) -> dict:
        """医生事件的后续服务：每支付5碎片，显式选择+1攻击次数或+2攻击力。"""
        doctor = next((entity for entity in self.state.employees
                       if entity.name == "医生" and entity.is_alive), None)
        if doctor is None:
            return {"success": False, "error": "当前没有存活医生"}
        mode = params.get("mode")
        if mode not in ("attack_count", "attack_power"):
            return {"success": False, "error": "mode必须是attack_count或attack_power"}
        if self.state.shards < 5:
            return {"success": False, "error": "医生升级需要5碎片"}
        self.state.shards -= 5
        if mode == "attack_count":
            doctor.attack_count += 1
        else:
            doctor.attack_power += 2
        return {"success": True, "action": "医生升级",
                "result": {"mode": mode, "paid": 5,
                           "attack_count": doctor.attack_count,
                           "attack_power": doctor.attack_power}}

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
        h = self.state.apply_heal(target, heal) if target else {"actual_heal":0}
        cancer = self.combat.check_cancer(target) if target else None
        payload = {"heal_amount": heal, "shard_cost": cost, "target": target.name if target else None,
                   "actual_heal": h.get("actual_heal", 0), "hp_after": target.current_hp if target else 0,
                   "shards_remaining": self.state.shards}
        if cancer:
            payload["cancer"] = cancer
        return {"success": True, "action": "休整", "result": payload}

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

    # 遗物池定义（13件；【发现】只列候选，效果在对应触发时点应用）
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
        """每局仅初始化一次，池耗尽后不得重新生成重复遗物。"""
        if self.state.relic_pool_initialized:
            return
        self.state.relics_pool = [Relic(name=n, effect=e) for n, e in self.RELIC_DEFS]
        self.state.relic_pool_initialized = True

    def _offer_relic_discovery(self, source: str) -> dict:
        """从当前池不重复随机列出至多3件；只列候选，不替AI作选择。"""
        self._init_relic_pool()
        if self.state.pending_relic_choices:
            return {"success": False, "error": "已有待选择的遗物发现"}
        if not self.state.relics_pool:
            return {"success": False, "error": "遗物池已耗尽"}
        remaining = [r.name for r in self.state.relics_pool]
        choices: list[str] = []
        for slot in range(min(3, len(remaining))):
            roll = self.dice.auto_roll(
                f"relic_discovery_{source}_{slot + 1}",
                remaining,
                context=f"{source}：随机列出遗物候选{slot + 1}",
            )
            selected = remaining.pop(roll["record"]["selected_index"])
            choices.append(selected)
        self.state.pending_relic_choices = choices
        self.state.pending_relic_source = source
        return {"success": True, "choices": choices, "source": source}

    def _action_choose_discovered_relic(self, params: dict) -> dict:
        choice = params.get("relic_name", "")
        choices = self.state.pending_relic_choices
        if not choices:
            return {"success": False, "error": "当前没有待选择的遗物发现"}
        if choice not in choices:
            return {"success": False, "error": "只能选择本次发现列出的遗物", "choices": list(choices)}
        relic = next((r for r in self.state.relics_pool if r.name == choice), None)
        if relic is None:
            return {"success": False, "error": f"遗物池中不存在【{choice}】"}
        self.state.relics_pool.remove(relic)
        self.state.relics.append(relic)
        source = self.state.pending_relic_source
        self.state.pending_relic_choices = []
        self.state.pending_relic_source = ""
        return {
            "success": True,
            "action": "选择发现遗物",
            "result": {"relic": choice, "source": source},
        }

    def _offer_item_discovery(self, pool: list[str], source: str) -> dict:
        if self.state.pending_item_choices:
            return {"success": False, "error": "已有待选择的消耗品发现"}
        remaining = list(dict.fromkeys(pool))
        if not remaining:
            return {"success": False, "error": "没有可发现的消耗品"}
        choices = []
        for slot in range(min(3, len(remaining))):
            roll = self.dice.auto_roll(
                f"item_discovery_{source}_{slot + 1}", remaining,
                context=f"{source}：随机列出消耗品候选{slot + 1}",
            )
            choices.append(remaining.pop(roll["record"]["selected_index"]))
        self.state.pending_item_choices = choices
        self.state.pending_item_source = source
        return {"success": True, "choices": choices, "source": source}

    def _action_choose_discovered_item(self, params: dict) -> dict:
        choice = params.get("item_name", "")
        if not self.state.pending_item_choices:
            return {"success": False, "error": "当前没有待选择的消耗品发现"}
        if choice not in self.state.pending_item_choices:
            return {"success": False, "error": "只能选择本次发现列出的消耗品",
                    "choices": list(self.state.pending_item_choices)}
        if choice not in TWISTED_TOOL_LIBRARY:
            return {"success": False, "error": f"未知工具: {choice}"}
        durability, effect = TWISTED_TOOL_LIBRARY[choice]
        self.state.consumables.append(Consumable(
            name=choice, effect=effect, current_uses=durability, max_uses=durability,
        ))
        source = self.state.pending_item_source
        self.state.pending_item_choices = []
        self.state.pending_item_source = ""
        return {"success": True, "action": "选择发现消耗品",
                "result": {"item": choice, "durability": durability, "source": source}}

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
            # 怪物转化道纹门禁（README 第211/247-252行）：
            # 转化道纹须"以自身已持有的一种道纹为起点"经残韵变化获得；
            # 而其起点全部是原始怪物道纹（狂暴/强化/活力/减速/必中/自愈/飞行），
            # 人类无法承受并获得原始怪物道纹，故轮回者不可能凭空学会转化道纹。
            # 例：蒙蔽源自"必中--反转-->蒙蔽"，没有必中就学不到蒙蔽。
            if name in MONSTER_TRANSFORM_DAOWEN:
                if name not in player.dao_wen:
                    self.state.energy += 1
                    return {"success": False,
                            "error": f"【{name}】是怪物转化道纹，须以自身已持有的道纹为起点"
                                     f"经残韵变化获得，无法通过局外【学习】直接习得"}
            if name in ORIGINAL_MONSTER_DAOWEN:
                self.state.energy += 1
                return {"success": False,
                        "error": f"【{name}】是原始怪物道纹，人类无法承受并获得"}
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
        """共鸣：发现时随机列3件后显式选1；付费自选则直接加入。"""
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
        discovery = self._offer_relic_discovery("共鸣发现")
        if not discovery.get("success"):
            return discovery
        return {
            "success": True,
            "action": "共鸣(发现)",
            "result": {"relic_choices": discovery["choices"]},
            "next_actions": ["choose_discovered_relic"],
        }


    def _pre_battle_tansuo(self, params: dict) -> dict:
        """探索：从当前事件池(通用+本副本专属)中，由引擎自动生成随机数抽取一个事件"""
        if self.event_pool.current is not None:
            return {"success": False,
                    "error": f"事件【{self.event_pool.current}】尚未结算，不能再次探索"}
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
        if (not isinstance(blood_alloc, int) or isinstance(blood_alloc, bool)
                or not isinstance(atk_bundles, int) or isinstance(atk_bundles, bool)
                or blood_alloc < 0 or atk_bundles < 0 or blood_alloc + 3 * atk_bundles != 20):
            return {"success": False,
                    "error": "预算分配非法：blood_alloc + 3×atk_bundles 必须恰好等于20，且均为非负整数"
                             "（允许攻击次数为0；1点分配值=12血限；每3点分配值捆绑购买1攻击次数+2攻击力）"}
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
        self.state.phase = GamePhase.IN_COMBAT.value
        self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_START.value
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
        self.state.phase = "pre_battle"
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
        怪物走prepare/resolve两阶段规则，不受此速限/攻击次数推导的出手预算约束。"""
        if entity.entity_type == "怪物":
            return None
        if not self.combat.can_act(entity):
            return {"success": False,
                    "error": f"{entity.name}无法出手（眩晕/束缚/缓慢）"}
        breath = self.combat.apply_opposing_longxi(entity)
        if breath and not entity.is_alive:
            return {"success": False, "error": f"{entity.name}被龙息命零",
                    "dragon_breath": breath}
        if entity.actions_used_this_round >= entity.action_count:
            return {"success": False,
                    "error": f"{entity.name}本回合出手已用完({entity.actions_used_this_round}/{entity.action_count})"}
        entity.actions_used_this_round += 1
        if entity.has_status("兴奋"):
            self.combat._gain_speed(entity, 1)
        return None

    def _apply_dragon_claw_growth(self, entity: "Entity") -> None:
        """龙族利爪（真龙之心遗物）：自身每完成一次行动后，攻击次数+1，攻击力+2。
        必须在该次行动本身已经用到(旧的)攻击次数之后才调用——尤其是【攻击】，
        它的一轮攻击命中次数=攻击次数，若在calculate_round_attack读取攻击次数之前就先增长，
        会导致本次攻击莫名要求多一个目标选择，这是过去出现过的真实bug。"""
        if entity is not None and self.state.side_has(entity, "龙族利爪"):
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

    def _duel_side_can_act(self, side: str) -> bool:
        """该侧是否还有未用完的出手预算（存活且未撤退；get_all_* 已排除死者/撤退者）。"""
        entities = (self.state.get_all_player_side() if side == "player_side"
                    else self.state.get_all_enemy_side())
        return any(e.actions_used_this_round < e.action_count and self.combat.can_act(e)
                   for e in entities)

    def _advance_duel_turn(self):
        """死斗中一次出手成功结算后：对方还有余手则换边；否则本侧连动，余手不作废。"""
        if not self.state.in_final_duel:
            return
        other = "opponent_side" if self.state.duel_turn == "player_side" else "player_side"
        if self._duel_side_can_act(other):
            self.state.duel_turn = other

    def _hostile_to(self, actor: Entity, target: Entity) -> bool:
        if target is None or target is actor:
            return False
        actor_on_player = actor in self.state.get_all_player_side()
        target_on_player = target in self.state.get_all_player_side()
        return actor_on_player != target_on_player

    def _resolve_daowen_dodge(self, name: str, actor: Entity, target: Entity,
                              dodge: bool, dodge_targets: list,
                              blood_shadow: bool = False,
                              dodge_relic_target_ref: str = "") -> tuple[dict, Optional[list[Entity]]]:
        """结算显式闪避，并直接返回本次AOE目标，避免跨行动保存临时跳过状态。"""
        log = {"must_hit": False, "dodged_names": [], "fully_dodged": False}
        hostile_possible = name == "冲击" or (target is not None and self._hostile_to(actor, target))
        if hostile_possible and self.combat.bizhong_remaining(actor) > 0:
            log["must_hit"] = self.combat.consume_bizhong(actor)
            if log["must_hit"] and name != "冲击":
                return log, None
        if blood_shadow:
            if (name == "冲击" or target is None or not self._hostile_to(actor, target)
                    or not self.state.side_has(target, "血影") or target.current_hp <= 10):
                raise ValueError("当前判定不能使用血影")
            self.combat._pay_bleed_cost(target, 10)
            log["blood_shadow"] = True
            log["fully_dodged"] = True
            return log, None
        if name == "冲击":
            refs = self.combat._combat_entity_refs()
            expected = {ref: entity for ref, entity in refs.items()
                        if self.state.on_player_side(entity) != self.state.on_player_side(actor)}
            if not isinstance(dodge_targets, list):
                raise ValueError("冲击必须显式提交dodge_targets")
            received = {}
            for entry in dodge_targets:
                if (not isinstance(entry, dict) or entry.get("target_ref") not in expected
                        or not isinstance(entry.get("dodge"), bool)
                        or not isinstance(entry.get("blood_shadow"), bool)
                        or entry["dodge"] and entry["blood_shadow"]):
                    raise ValueError("冲击每个目标必须提交合法target_ref/dodge/blood_shadow")
                if entry["target_ref"] in received:
                    raise ValueError("冲击dodge_targets不能重复")
                received[entry["target_ref"]] = entry
            if set(received) != set(expected):
                raise ValueError("冲击dodge_targets必须覆盖全部敌对目标")
            aoe_targets = []
            for ref, ent in expected.items():
                entry = received[ref]
                if log["must_hit"]:
                    aoe_targets.append(ent); continue
                if entry["blood_shadow"]:
                    if not self.state.side_has(ent, "血影") or ent.current_hp <= 10:
                        raise ValueError(f"{ent.name}不能使用血影")
                    self.combat._pay_bleed_cost(ent, 10)
                    log["dodged_names"].append({"name": ent.name, "blood_shadow": True})
                elif entry["dodge"]:
                    if ent.current_speed < 1:
                        raise ValueError(f"{ent.name}速度不足")
                    ent.current_speed -= 1
                    extra = self.combat._note_dodge(ent, entry.get("dodge_relic_target_ref"))
                    log["dodged_names"].append({"name": ent.name, "speed_after": ent.current_speed, **extra})
                else:
                    aoe_targets.append(ent)
            log["fully_dodged"] = bool(expected) and not aoe_targets
            return log, aoe_targets
        if not dodge or target is None or not self._hostile_to(actor, target):
            return log, None
        if target.current_speed < 1:
            log["dodge_fail_reason"] = "速度不足"
            return log, None
        target.current_speed -= 1
        extra = self.combat._note_dodge(target, dodge_relic_target_ref)
        entry = {"name": target.name, "speed_after": target.current_speed}
        if extra:
            entry.update(extra)
        log["dodged_names"].append(entry)
        log["fully_dodged"] = True
        return log, None

    def _action_deploy_employee(self, params: dict) -> dict:
        """派遣[员工]出战：消耗玩家1出手（现已强制校验回合出手预算）。仅[员工]需要此步骤，[朋友]开局即直接参战。"""
        employee_ref = params.get("employee_ref", "")
        emp = None
        if isinstance(employee_ref, str) and employee_ref.startswith("employee:"):
            try:
                emp = self.state.employees[int(employee_ref.split(":", 1)[1])]
            except (ValueError, IndexError):
                emp = None
        if emp is None and params.get("name"):
            matches = [entity for entity in self.state.employees
                       if entity.name == params["name"] and entity.is_alive]
            emp = matches[0] if len(matches) == 1 else None
        if emp is None or not emp.is_alive:
            return {"success": False, "error": "employee_ref不是存活员工"}
        name = emp.name
        if emp.is_debt_bound:
            return {"success": False, "error": f"{name}属于还债转化员工，已自动参战，无需派遣"}
        if emp.has_retreated:
            return {"success": False, "error": f"{name}本场已【撤退】，无法再次加入本场战斗"}
        if emp.is_deployed:
            return {"success": False, "error": f"{name}已在场，无需重复派遣"}
        if not self.state.player:
            return {"success": False, "error": "没有玩家"}
        duel_error = self._check_duel_turn_or_error(self.state.player)
        if duel_error:
            return duel_error
        budget_error = self._consume_action_or_error(self.state.player)
        if budget_error:
            return budget_error
        self._apply_dragon_claw_growth(self.state.player)
        emp.is_deployed = True

        # current_round 由 round_start() 递增，代表"当前正在进行的回合序号"(1-indexed)；
        # 若在round_start之前部署(current_round仍为0)，视为从第1回合起参战。
        emp.deployed_at_round = max(1, self.state.current_round)
        self._advance_duel_turn()
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

    def _action_repay_debt_employee(self, params: dict) -> dict:
        """独立还债轨道：一次付清该员工负债后离队；不发工资、不计黑名单。"""
        name = params.get("name", "")
        employee = next((e for e in self.state.employees
                         if e.name == name and e.is_debt_bound), None)
        if employee is None:
            return {"success": False, "error": f"找不到还债员工: {name}"}
        debt = max(0, -employee.shards)
        if debt <= 0:
            return {"success": False, "error": f"{name}当前没有未清负债"}
        if self.state.shards < debt:
            return {"success": False,
                    "error": f"必须一次付清{debt}碎片，当前只有{self.state.shards}"}
        self.state.shards -= debt
        employee.shards = 0
        self.state.employees.remove(employee)
        self.state.pending_wage_decisions.pop(name, None)
        return {
            "success": True,
            "action": "付清还债员工负债",
            "result": {"employee": name, "paid": debt, "departed": True,
                       "blacklist_unchanged": self.state.blacklist_level,
                       "shards": self.state.shards},
        }

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
        actor_ref = params.get("actor_ref", "")
        actor_name = params.get("actor", "")  # 旧存档/外部调用仅在名称唯一时兼容
        is_command = False
        refs = self.combat._combat_entity_refs()
        if actor_ref:
            actor = refs.get(actor_ref)
        elif actor_name:
            matches = [entity for entity in refs.values() if entity.name == actor_name]
            actor = matches[0] if len(matches) == 1 else None
        else:
            actor = self.state.player
        if actor is None or not actor.is_alive or actor.has_retreated:
            return {"success": False, "error": "actor_ref不是当前存活行动者"}
        if actor is self.state.player:
            pass
        elif (self.state.in_final_duel and actor.entity_type == "轮回者"
              and actor in self.state.get_all_enemy_side()):
            pass
        elif actor in self.state.friends or actor in [e for e in self.state.employees if e.is_deployed]:
            is_command = True
        else:
            return {"success": False, "error": f"{actor.name}不能作为use_daowen的行动者"}

        name = params.get("daowen_name", "")
        x = params.get("x", 1)
        target_name = params.get("target", "")
        target_ref = params.get("target_ref", "")

        if name not in actor.dao_wen:
            return {"success": False, "error": f"{actor.name}未持有道纹: {name}"}

        dw_instance = actor.dao_wen[name]

        if not dw_instance.can_use():
            return {"success": False, "error": f"道纹{name}不可用（冷却/封印）"}

        if not isinstance(x, int) or isinstance(x, bool) or x < 1:
            return {"success": False, "error": "X必须≥1且为整数"}
        if is_command and dw_instance.x_value > 0 and x != dw_instance.x_value:
            return {"success": False, "error": f"{actor.name}的【{name}】固定为X={dw_instance.x_value}"}

        # 计算函数声明了target即正文含[目标]：缺少显式目标时判定失效，禁止静默改为自身。
        import inspect
        DaoWenEngine.register_all()
        requires_target = "target" in inspect.signature(DaoWenEngine._registry[name]).parameters
        if requires_target and not (target_ref or target_name):
            return {"success": False, "error": f"【{name}】需要显式指定目标target_ref；缺少[目标]时失效"}

        if is_command and not (target_ref or target_name):
            return {"success": False, "error": "听从指令发动道纹必须指定一个非自身的目标"}

        # 使用稳定引用定位目标；旧名称仅在唯一匹配时兼容。
        refs = self.combat._combat_entity_refs()
        target = actor
        if target_ref:
            target = refs.get(target_ref)
            if target is None:
                return {"success": False, "error": "target_ref不是当前合法实体"}
        elif target_name:
            matches = [entity for entity in refs.values() if entity.name == target_name]
            if len(matches) != 1:
                return {"success": False, "error": f"目标名称{target_name}不唯一或不存在，请改用target_ref"}
            target = matches[0]
        if is_command and target is actor:
            return {"success": False, "error": "听从指令发动道纹必须指定一个非自身目标"}
        if target is not actor and not self.combat.is_targetable(actor, target):
            return {"success": False, "error": f"{target.name}处于飞行，无法被选中"}

        duel_error = self._check_duel_turn_or_error(actor)
        if duel_error:
            return duel_error
        if self.state.phase == "in_combat":
            if not self.combat.can_act(actor):
                return {"success": False, "error": f"{actor.name}当前无法出手"}
            if actor.entity_type != "怪物" and actor.actions_used_this_round >= actor.action_count:
                return {"success": False,
                        "error": f"{actor.name}本回合出手已用完({actor.actions_used_this_round}/{actor.action_count})"}

        if actor.has_status("无神"):
            target = actor

        # 调用道纹引擎计算
        try:
            resolve_kw = {"target": target, "caster": actor}
            if name == "缓慢":
                resolve_kw["target_action_count"] = self.combat.single_round_action_count(target)
            calc = DaoWenEngine.resolve(name, x, **resolve_kw)
        except Exception as e:
            return {"success": False, "error": f"道纹计算失败: {str(e)}"}
        if (self.state.side_has(actor, "缄默面具")
                and calc.get("cost_type") not in (None, "", "消耗")):
            return {"success": False, "error": "缄默面具：无法发动附带代价的道纹"}
        trigger_choices = params.get("trigger_spell_choices", {})
        try:
            self.combat.validate_daowen_trigger_spells(actor, trigger_choices, refs)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        trigger_logs = self.combat.resolve_daowen_trigger_spells(actor, trigger_choices, refs)
        if not actor.is_alive:
            return {"success": True, "action": f"{actor.name}发动道纹前被反应法术命零",
                    "result": {"trigger_spell_logs": trigger_logs, "daowen_resolved": False}}

        # 检查法力是否足够（代价道纹不消耗法力）
        # [朋友]/[员工]不持有法力（与怪物规则一致），发动道纹不支付法力，只消耗出手；仅玩家自身发动时走法力制
        cost = calc.get("cost", calc.get("cost_mutation", 0))
        if not is_command and calc.get("cost_type") == "消耗" and cost > 0:
            if not actor.spend_mana(cost):
                return {"success": False, "error": f"法力不足，需要{cost}，当前{actor.current_mana}"}
            # 寒冰法力：持有者每消耗法力发动道纹，无论目标是谁(含自己)都累计"施加法力"
            if self.state.side_has(actor, "寒冰法力"):
                before_tier = target.mana_inflicted_this_round // 10
                target.mana_inflicted_this_round += cost
                after_tier = target.mana_inflicted_this_round // 10
                new_stacks = after_tier - before_tier
                if new_stacks > 0:
                    target.add_status(StatusEffect(name="无力", value=new_stacks, remaining_rounds=1, source="寒冰法力"))

        # F2：赌命X/消灾X 的碎片类代价预检与支付（代价类型非"消耗"，不走法力制）
        # 赌命X：消耗X假碎片
        if name == "赌命":
            fake_need = calc.get("fake_cost", x)
            if actor is self.state.player:
                have = self.state.fake_shards
            else:
                have = getattr(actor, "fake_shards", 0)
            if have < fake_need:
                return {"success": False, "error": f"假碎片不足：赌命X需{fake_need}假碎片，当前{have}"}
            if actor is self.state.player:
                self.state.fake_shards -= fake_need
            else:
                actor.fake_shards -= fake_need
        # 消灾X：消耗50X假碎片/5X碎片（局外发动消耗×2）；优先假碎片
        # 战斗全程 phase="in_combat"；局外发动时两类碎片代价均×2。
        elif name == "消灾":
            in_combat = self.state.phase == "in_combat"
            mult = 1 if in_combat else 2
            fake_need = calc.get("fake_cost", 50 * x) * mult
            real_need = calc.get("real_cost", 5 * x) * mult
            if actor is self.state.player:
                have_fake, have_real = self.state.fake_shards, self.state.shards
            else:
                have_fake, have_real = getattr(actor, "fake_shards", 0), actor.shards
            if have_fake >= fake_need:
                if actor is self.state.player:
                    self.state.fake_shards -= fake_need
                else:
                    actor.fake_shards -= fake_need
            elif have_real >= real_need:
                if actor is self.state.player:
                    self.state.lose_shards(real_need)
                else:
                    actor.lose_shards(real_need)
            else:
                return {"success": False,
                        "error": f"碎片不足：消灾X需{fake_need}假碎片或{real_need}碎片（局外×{mult}），"
                                 f"当前假{have_fake}/真{have_real}"}

        if self.state.phase == "in_combat":
            budget_error = self._consume_action_or_error(actor)
            if budget_error:
                return budget_error
            self._apply_dragon_claw_growth(actor)

        dodge_value = params.get("dodge", False)
        blood_shadow_value = params.get("blood_shadow", False)
        if not isinstance(dodge_value, bool) or not isinstance(blood_shadow_value, bool):
            return {"success": False, "error": "dodge与blood_shadow必须是布尔值"}
        if dodge_value and blood_shadow_value:
            return {"success": False, "error": "不能同时闪避并使用血影"}
        dodge = dodge_value
        dodge_targets = list(params.get("dodge_targets") or [])
        dodge_log, aoe_targets_override = self._resolve_daowen_dodge(
            name, actor, target, dodge, dodge_targets,
            blood_shadow=bool(params.get("blood_shadow", False)),
            dodge_relic_target_ref=params.get("dodge_relic_target_ref", ""),
        )
        if dodge_log.get("fully_dodged"):
            self._advance_duel_turn()
            return {
                "success": True,
                "action": f"发动道纹【{name}X={x}】" + (f"（{actor.name}听从指令发动）" if is_command else ""),
                "calculation": calc,
                "execution": {"daowen": name, "effects": [], "dodged": dodge_log},
                "dodge": dodge_log,
                "trigger_spell_logs": trigger_logs,
                "state": self.combat._get_combat_state(),
            }

        dragon_heart_use = params.get("dragon_heart_use", 0)
        execution = self._execute_daowen_effect(
            name, calc, actor, target, dragon_heart_use,
            aoe_targets_override=aoe_targets_override,
        )
        if dodge_log.get("dodged_names"):
            execution = dict(execution)
            execution["dodged"] = dodge_log
        self._advance_duel_turn()

        return {
            "success": True,
            "action": f"发动道纹【{name}X={x}】" + (f"（{actor.name}听从指令发动）" if is_command else ""),
            "calculation": calc,
            "execution": execution,
            "dodge": dodge_log or None,
            "trigger_spell_logs": trigger_logs,
            "state": self.combat._get_combat_state()
        }

    def _execute_daowen_effect(
        self, name: str, calc: dict, caster: Entity, target: Entity,
        dragon_heart_use: int = 0, *, aoe_targets_override: Optional[list[Entity]] = None,
    ) -> dict:
        """发动道纹：法力检查后委托combat.apply_daowen_effect。"""
        return self.combat.apply_daowen_effect(
            name, calc, caster, target, dragon_heart_use,
            aoe_targets_override=aoe_targets_override,
        )

    def _find_resonance_holder(self, source: str, target_ref: str):
        """按稳定引用定位残韵作用的道纹持有者；未指定时只接受唯一持有者。"""
        player = self.state.player
        if source in player.dao_wen:
            return player, None
        refs = self.combat._combat_entity_refs()
        if target_ref:
            target = refs.get(target_ref)
            if target is None:
                return None, f"找不到target_ref: {target_ref}"
            if source not in target.dao_wen:
                return None, f"{target.name}未持有道纹: {source}"
            return target, None
        holders = [entity for entity in refs.values() if entity is not player and source in entity.dao_wen]
        if len(holders) == 1:
            return holders[0], None
        if not holders:
            return None, f"场上无人持有道纹: {source}"
        return None, f"多名角色持有{source}，请指定target_ref"

    def _grant_transformed_daowen(self, player: Entity, dest: str) -> bool:
        """残韵获得变化后道纹。规则4：X不从原道纹拷贝；规则6：同名不重复。"""
        if dest in player.dao_wen:
            return False
        player.dao_wen[dest] = DaoWenInstance(DaoWen(
            name=dest, formula=f"{dest}X", cost_type="消耗",
            cost_formula="X", effect_formula=""))
        return True

    def _action_use_resonance(self, params: dict) -> dict:
        """使用残韵"""
        source = params.get("source_daowen", "")
        rtype = params.get("resonance_type", "")

        # 检查玩家是否拥有该类型残韵
        if rtype not in self.state.resonance or self.state.resonance[rtype] <= 0:
            return {"success": False, "error": f"没有可用的{rtype}残韵（当前：{self.state.resonance}）"}

        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}

        holder, holder_err = self._find_resonance_holder(source, params.get("target_ref", "") or "")
        if holder_err:
            return {"success": False, "error": holder_err}

        caster_has = holder is player

        result = ResonanceEngine.apply_resonance(
            source, rtype,
            caster_has_daowen=caster_has,
            target_has_daowen=True,
            resonance_stock=self.state.resonance  # 传入残韵库存用于校验
        )

        if not result["success"]:
            return {"success": False, "error": result["error"]}

        # 路径与持有者均已确认，此时才消耗（规则1：未生效不消耗）
        self.state.resonance[rtype] -= 1

        dest = result["target"]
        granted = False
        # 如果是轮回者拥有的道纹，永久变化（规则3）
        if caster_has and result.get("permanent_change"):
            old_dw = player.dao_wen[source]
            new_dw = DaoWen(
                name=dest,
                formula=f"{dest}X",
                cost_type=old_dw.dao_wen.cost_type,
                cost_formula=old_dw.dao_wen.cost_formula,
                effect_formula=old_dw.dao_wen.effect_formula
            )
            player.dao_wen[dest] = DaoWenInstance(dao_wen=new_dw)
            if dest != source:
                del player.dao_wen[source]
        else:
            # 规则2：不改持有，只改下一次发动结算；施法者获得变化后道纹
            self.combat.queue_resonance_rewrite(holder, source, dest)
            granted = self._grant_transformed_daowen(player, dest)

        second = params.get("second_target_ref", "")
        second_source_daowen = params.get("second_source_daowen", "")
        second_log = None
        if second and any(r.name == "同魂笔" for r in self.state.relics):
            if not second_source_daowen:
                second_log = "同魂笔：必须指定second_source_daowen(第二个目标身上要受影响的道纹)，未生效"
            else:
                second_entity = self.combat._combat_entity_refs().get(second)
                if second_entity is None:
                    second_log = f"同魂笔：找不到目标引用{second}，未生效"
                elif second_source_daowen not in second_entity.dao_wen:
                    second_log = f"同魂笔：{second}未持有{second_source_daowen}，未生效"
                else:
                    r2 = ResonanceEngine.apply_resonance(second_source_daowen, rtype,
                                                          caster_has_daowen=(second_source_daowen in player.dao_wen),
                                                          target_has_daowen=True)
                    if r2.get("success") and r2.get("caster_gets_daowen"):
                        new_name = r2["target"]
                        # 残韵作用于非轮回者拥有的道纹时：不改变其拥有的道纹，施法者永久获得变化后的道纹
                        self.combat.queue_resonance_rewrite(second_entity, second_source_daowen, new_name)
                        if self._grant_transformed_daowen(player, new_name):
                            second_log = f"同魂笔：{second_entity.name}的{second_source_daowen}受{rtype}影响，施法者永久获得{new_name}"
                        else:
                            second_log = f"同魂笔：施法者已持有{new_name}，不重复获得"
                    else:
                        second_log = f"同魂笔：{r2.get('error', '未知原因')}，未生效"
        return {
            "success": True,
            "action": f"残韵【{rtype}】{source} → {result['target']}",
            "result": result,
            "holder": holder.name,
            "holder_is_player": caster_has,
            "granted_daowen": dest if granted else None,
            "second_target_log": second_log,
            "resonance_remaining": self.state.resonance
        }

    def _action_prepare_attack(self, params: dict) -> dict:
        """第一阶段：绑定一次行动的逐击目标、闪避、血影和法术反应选项。"""
        if self.state.pending_attack:
            return {"success": False, "error": "已有待提交攻击"}
        refs = self.combat._combat_entity_refs()
        actor_ref = params.get("actor_ref") or "player:0"
        attacker = refs.get(actor_ref)
        if attacker is None or not attacker.is_alive:
            return {"success": False, "error": "actor_ref不是当前存活行动者"}
        if attacker.entity_type == "怪物" and not self.state.in_final_duel:
            return {"success": False, "error": "普通怪物必须通过怪物阶段行动"}
        duel_error = self._check_duel_turn_or_error(attacker)
        if duel_error:
            return duel_error
        if not self.combat.can_act(attacker):
            return {"success": False, "error": f"{attacker.name}当前无法行动"}
        if attacker.actions_used_this_round >= attacker.action_count:
            return {"success": False, "error": f"{attacker.name}本回合出手已用完"}

        if attacker.has_status("无神"):
            target_refs = [actor_ref]
        else:
            target_refs = [ref for ref, entity in refs.items()
                           if self.state.on_player_side(entity) != self.state.on_player_side(attacker)
                           and self.combat.is_targetable(attacker, entity)]
        if (self.state.on_enemy_side(attacker) and "龙威" in self.state.dragon_traits
                and "player:0" in refs):
            target_refs = ["player:0"]
        if (self.state.on_player_side(attacker) and "龙威" in self.state.opponent_dragon_traits):
            opponent_ref = next((ref for ref, entity in refs.items()
                                 if entity.entity_type == "轮回者" and self.state.on_enemy_side(entity)), None)
            if opponent_ref:
                target_refs = [opponent_ref]
        if not target_refs and attacker.attack_count > 0:
            return {"success": False, "error": "没有合法攻击目标"}

        target_options = []
        for ref in target_refs:
            target = refs[ref]
            target_options.append({
                "ref": ref, "name": target.name,
                "can_dodge": target.current_speed > 0,
                "can_blood_shadow": self.state.side_has(target, "血影") and target.current_hp > 10,
                "spell_options": self.combat.prepare_spell_reactions(target, attacker),
                "dodge_relic_target_options": [
                    {"ref": r, "name": e.name} for r, e in refs.items()
                    if self.state.on_player_side(e) != self.state.on_player_side(target) and e.is_alive
                ] if self.state.side_has(target, "回锋刀") else [],
            })
        options = {
            "actor_ref": actor_ref,
            "actor": attacker.name,
            "hit_count": max(0, attacker.attack_count),
            "target_options": target_options,
            "hits_schema": [{
                "target_ref": [o["ref"] for o in target_options],
                "dodge": "boolean", "blood_shadow": "boolean",
                "dodge_relic_target_ref": "持有回锋刀且闪避时必填",
                "spell_choices": "按目标spell_options完整提交",
            } for _ in range(max(0, attacker.attack_count))],
        }
        token = uuid.uuid4().hex
        self.state.pending_attack = {"token": token, "round": self.state.current_round, "options": options}
        return {"success": True, "action": "准备攻击", "result": {"token": token, **options}}

    def _action_resolve_attack(self, params: dict) -> dict:
        """第二阶段：只接受prepare快照中的逐击完整选择并原子结算。"""
        pending = self.state.pending_attack
        if not pending:
            return {"success": False, "error": "请先调用prepare_attack"}
        if params.get("token") != pending.get("token") or pending.get("round") != self.state.current_round:
            return {"success": False, "error": "攻击token无效或已过期"}
        hits = params.get("hits")
        options = pending["options"]
        if not isinstance(hits, list) or len(hits) != options["hit_count"]:
            return {"success": False, "error": f"必须提交{options['hit_count']}个hits"}
        refs = self.combat._combat_entity_refs()
        attacker = refs.get(options["actor_ref"])
        if attacker is None or not attacker.is_alive:
            return {"success": False, "error": "攻击者已失效"}
        legal = {entry["ref"]: entry for entry in options["target_options"]}
        speed_spend: dict[str, int] = {}
        blood_spend: dict[str, int] = {}
        for index, hit in enumerate(hits):
            if not isinstance(hit, dict) or hit.get("target_ref") not in legal:
                return {"success": False, "error": f"第{index + 1}击目标不在prepare快照中"}
            if not isinstance(hit.get("dodge"), bool) or not isinstance(hit.get("blood_shadow"), bool):
                return {"success": False, "error": "每击必须显式提交布尔值dodge与blood_shadow"}
            if hit["dodge"] and hit["blood_shadow"]:
                return {"success": False, "error": "同一次判定不能同时闪避并使用血影"}
            ref = hit["target_ref"]
            target = refs.get(ref)
            if target is None:
                return {"success": False, "error": "prepare攻击目标已失效"}
            if hit["dodge"] and self.combat.bizhong_remaining(attacker) <= index:
                speed_spend[ref] = speed_spend.get(ref, 0) + 1
                if speed_spend[ref] > target.current_speed:
                    return {"success": False, "error": f"{target.name}速度不足以完成全部闪避"}
                if self.state.side_has(target, "回锋刀"):
                    rr = hit.get("dodge_relic_target_ref")
                    allowed = {x["ref"] for x in legal[ref]["dodge_relic_target_options"]}
                    if rr not in allowed:
                        return {"success": False, "error": "回锋刀触发必须显式提交合法dodge_relic_target_ref"}
            if hit["blood_shadow"]:
                if not legal[ref]["can_blood_shadow"]:
                    return {"success": False, "error": f"{target.name}不能使用血影"}
                blood_spend[ref] = blood_spend.get(ref, 0) + 10
                if blood_spend[ref] >= target.current_hp:
                    return {"success": False, "error": f"{target.name}生命不足以支付全部血影"}
            self.combat.validate_spell_reaction_submission(
                target, attacker, hit.get("spell_choices"), refs,
            )
        duel_error = self._check_duel_turn_or_error(attacker)
        if duel_error:
            return duel_error
        budget_error = self._consume_action_or_error(attacker)
        if budget_error:
            return budget_error
        results = []
        for index, hit in enumerate(hits):
            target = refs[hit["target_ref"]]
            if not target.is_alive:
                results.append({"hit_index": index + 1, "skipped": "目标已命零"})
                continue
            result = self.combat.resolve_attack(
                attacker, target, dodge=hit["dodge"], blood_shadow=hit["blood_shadow"],
                spell_choices=hit.get("spell_choices") or {}, entity_refs=refs,
                dodge_relic_target_ref=hit.get("dodge_relic_target_ref"),
            )
            result["hit_index"] = index + 1
            results.append(result)
        self._apply_dragon_claw_growth(attacker)
        self.state.pending_attack = {}
        self._advance_duel_turn()
        if self.state.in_final_duel and not self._duel_side_can_act("player_side") and not self._duel_side_can_act("opponent_side"):
            self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
        return {"success": True, "action": f"{attacker.name}结算一轮攻击",
                "result": {"attacker": attacker.name, "hits": results}}

    def _action_declare_wit(self, params: dict) -> dict:
        """声明急中生智（消耗玩家1出手）"""
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}
        target = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
        if not target:
            return {"success": False, "error": "target_ref不是当前合法目标"}

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
        # 同名重复抽 + 封印尸体仍留在 state.enemies：必须跳过已命零/已移出的，
        # 否则会命中第一具尸体并报「已命零」，活着的同名困境怪永远进化不了。
        monster = next((e for e in self.state.enemies
                        if e.name == monster_name and e.is_alive and not e.removed_without_kill), None)

        if not monster:
            return {"success": False, "error": f"找不到存活的怪物: {monster_name}"}

        daowen_name = params.get("daowen", "")
        try:
            x = int(params.get("x", 1))
        except (TypeError, ValueError):
            return {"success": False, "error": "X必须为整数"}

        return self.combat.execute_evolution(monster, daowen_name, x)

    def _action_consume_item(self, params: dict) -> dict:
        """使用消耗品（雕塑/普通/扭曲工具库8件，遵守现有消耗品规则，使用不消耗出手）"""
        item_name = params.get("name", "")
        item = None
        for c in self.state.consumables:
            if c.name == item_name and not c.is_depleted:
                item = c
                break
        if item is None:
            return {"success": False, "error": f"找不到可用消耗品: {item_name}"}

        # 扭曲工具库 8 件：引擎侧真实结算（F3）
        if item.name in TWISTED_TOOL_LIBRARY:
            return self._consume_twisted_tool(item, params)

        # 雕塑：消耗1耐久造成15伤害或获得20格挡
        if item.kind == "sculpture":
            mode = params.get("mode", "damage")  # damage / shield
            target = None
            if mode == "damage":
                target = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
                if target not in self.state.get_all_enemy_side():
                    target = None
            result = self.combat.use_sculpture(item, target=target, mode=mode)
            return {
                "success": result.get("success", True),
                "action": f"使用雕塑【{item_name}】({mode})",
                "result": result,
                "state": self.combat._get_combat_state(),
            }

        # 正文具名消耗品：全部在扣耐久前完成参数校验；未实现项不得“成功但只扣耐久”。
        if item.name in {"绝息淤泥", "活性土壤", "假钞贴", "穿甲弹", "洗劫面具", "赤泉囊", "龙血瓶"}:
            return self._consume_named_event_item(item, params)

        # 普通消耗品：扣减耐久；异变类效果走统一入口 add_mutation
        # （裁定⑧= A4全量：任何角色的任何异变来源同一入口，达50层即【崩解】命零；
        #  其余效果仅限已经有机械解析器的回复/异变文本；未知效果在扣耐久前拒绝。）
        effect = item.effect or ""
        heal_match = (re.search(r"恢复(\d+)生命", effect)
                      or re.search(r"\[回复\]\s*(\d+)", effect)
                      or re.search(r"回复(\d+)", effect))
        mut_match = re.search(r"获得异变(\d+)", effect)
        if heal_match is None and mut_match is None:
            return {"success": False, "error": f"消耗品【{item.name}】没有已注册的效果处理器"}
        remaining = item.use()
        mutation_info = None
        heal_info = None
        cancer_info = None
        if self.state.player:
            if heal_match:
                amount = int(heal_match.group(1))
                heal_info = self.state.apply_heal(self.state.player, amount)
                cancer = self.combat.check_cancer(self.state.player)
                if cancer:
                    cancer_info = cancer
                    if not self.state.player.is_alive and not self.state.last_death_cause:
                        self.state.last_death_cause = "cancer"
        mut_match = re.search(r"获得异变(\d+)", effect)
        if mut_match and self.state.player:
            layers = int(mut_match.group(1))
            mut = self.state.player.add_mutation(layers)
            mutation_info = {
                "mutation_added": mut["mutation_added"],
                "mutation_total": mut["mutation_total"],
                "collapsed": mut["collapsed"],
            }
            if mut["collapsed"]:
                self.state.last_death_cause = "collapse"
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
                "heal": heal_info,
                "cancer": cancer_info,
                "mutation": mutation_info,
                "note": "消耗品效果按其描述结算，使用不消耗出手",
            },
            "state": self.combat._get_combat_state(),
        }

    def _consume_named_event_item(self, item: Consumable, params: dict) -> dict:
        refs = self.combat._combat_entity_refs()
        player = self.state.player
        if player is None:
            return {"success": False, "error": "没有玩家"}
        name = item.name
        result: dict[str, Any] = {"item": name}
        if name == "绝息淤泥":
            if self.state.phase != GamePhase.IN_COMBAT.value:
                return {"success": False, "error": "绝息淤泥只能在战斗中使用"}
            item.use()
            self.state.event_modifiers["escape_at_battle_end"] = True
            result["battle_end_escape"] = True
        elif name == "活性土壤":
            if self.state.combat_subphase != CombatSubphase.AWAIT_ROUND_START.value:
                return {"success": False, "error": "活性土壤只能在战始、回始前使用"}
            x = params.get("x")
            panel = params.get("friend")
            if not params.get("dm_approved"):
                interrupt = Interrupt(
                    interrupt_type=InterruptType.UNSEEN_SCENE,
                    description="玩家设计朋友面板，等待DM确认",
                    context={"x": x, "friend": panel}, options=[],
                    state_snapshot=self.state.to_dict(),
                )
                self._pending_interrupts.append(interrupt)
                return {"success": True, "action": "活性土壤等待DM确认",
                        "interrupt": interrupt.to_dict(), "uses_remaining": item.current_uses}
            if (not isinstance(x, int) or isinstance(x, bool) or x < 1 or player.current_mana < x
                    or not isinstance(panel, dict)):
                return {"success": False, "error": "活性土壤需要合法x、足够法力和DM确认的friend面板"}
            ac, ap, hp = panel.get("attack_count"), panel.get("attack_power"), panel.get("blood_limit")
            budget = (ac * ac + 2 * ap + math.ceil(hp / 6)) if all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (ac, ap, hp)) else -1
            if budget > x or budget < 0 or not isinstance(panel.get("name"), str) or not panel["name"]:
                return {"success": False, "error": f"朋友面板预算必须≤X={x}"}
            player.current_mana -= x
            friend = Entity(panel["name"], "朋友", blood_limit=hp, current_hp=hp,
                            attack_count=ac, attack_power=ap)
            self.state.friends.append(friend)
            item.use()
            result.update({"friend": friend.name, "budget": budget, "mana_paid": x})
        elif name == "假钞贴":
            item.use(); self.state.fake_shards += 20
            result.update({"fake_shards_gained": 20, "fake_shards": self.state.fake_shards})
        elif name == "穿甲弹":
            target = refs.get(params.get("target_ref", ""))
            if target is None or self.state.on_player_side(target) == self.state.on_player_side(player):
                return {"success": False, "error": "穿甲弹必须显式指定合法敌方target_ref"}
            item.use()
            detail = self.combat._apply_hostile_damage(target, 15, "无视格挡", player)
            result.update({"target": target.name, "damage": detail})
        elif name == "洗劫面具":
            item.use(); self.combat.grant_bizhong(player, 2)
            result.update({"guaranteed_hits": 2, "remaining": self.combat.bizhong_remaining(player)})
        elif name == "赤泉囊":
            if self.state.phase != GamePhase.PRE_BATTLE.value:
                return {"success": False, "error": "赤泉囊只能在局外使用"}
            allocations = params.get("heal_allocations")
            outside_refs = {"player:0": player}
            outside_refs.update({f"friend:{i}": e for i, e in enumerate(self.state.friends) if e.is_alive})
            outside_refs.update({f"employee:{i}": e for i, e in enumerate(self.state.employees) if e.is_alive})
            if (not isinstance(allocations, list) or sum(entry.get("amount", -1) for entry in allocations if isinstance(entry, dict)) != 8
                    or any(not isinstance(entry, dict) or entry.get("target_ref") not in outside_refs
                           or not isinstance(entry.get("amount"), int) or isinstance(entry.get("amount"), bool)
                           or entry["amount"] < 0 for entry in allocations)):
                return {"success": False, "error": "赤泉囊必须用heal_allocations把8点恢复量完整分配给合法目标"}
            item.use()
            heals = []
            for entry in allocations:
                heals.append({"target": outside_refs[entry["target_ref"]].name,
                              **self.state.apply_heal(outside_refs[entry["target_ref"]], entry["amount"])})
            self.state.event_modifiers["red_spring_battle_losses"] = 2
            result.update({"heals": heals, "future_battle_start_losses": 2})
        elif name == "龙血瓶":
            amount = params.get("amount")
            target = refs.get(params.get("target_ref", ""))
            if (not isinstance(amount, int) or isinstance(amount, bool) or amount < 1
                    or amount > item.current_uses or target is None or not self.state.on_player_side(target)):
                return {"success": False, "error": "龙血瓶需要合法amount和玩家侧target_ref"}
            item.current_uses -= amount
            result.update({"target": target.name, "extracted": amount,
                           "heal": self.state.apply_heal(target, amount)})
        result.update({"uses_remaining": item.current_uses, "is_depleted": item.is_depleted})
        return {"success": True, "action": f"使用消耗品【{name}】", "result": result}

    def _consume_twisted_tool(self, item: Consumable, params: dict) -> dict:
        """扭曲工具库 8 件的引擎侧真实结算（F3）"""
        name = item.name
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}
        def selected_enemy():
            target = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
            if target in self.state.get_all_enemy_side():
                return target
            legacy = params.get("target", "")
            matches = [entity for entity in self.state.get_all_enemy_side() if entity.name == legacy]
            return matches[0] if len(matches) == 1 else None
        # 急救箱的“清除一种”必须由AI显式选择，先校验后扣耐久。
        aid_status = None
        if name == "急救箱":
            negative = [s for s in player.status_effects
                        if s.remaining_rounds > 0 or s.name in ("坏死", "退化", "伤痕", "畸变", "蒙蔽")]
            selected = params.get("remove_status")
            if negative:
                if not isinstance(selected, str) or selected not in [s.name for s in negative]:
                    return {"success": False,
                            "error": f"急救箱必须显式提交remove_status，可选{[s.name for s in negative]}"}
                aid_status = next(s for s in negative if s.name == selected)
            elif selected not in (None, ""):
                return {"success": False, "error": "当前没有可由急救箱清除的负面状态"}

        # 全部参数合法后统一扣耐久。
        remaining = item.use()
        result: dict[str, Any] = {"tool": name, "uses_remaining": remaining, "is_depleted": item.is_depleted}
        # 1. 反怪物电击枪：对目标 25 伤害，飞行目标 +15 并施坠落
        if name == "反怪物电击枪":
            target = selected_enemy()
            if target is None:
                item.current_uses += 1
                return {"success": False, "error": "找不到敌方target_ref"}
            # 只有当前飞行/滑翔状态算飞行；仅“持有”飞行道纹不算已经飞行。
            flying = target.is_flying or target.has_status("飞行") or target.has_status("滑翔")
            dmg = 25 + (15 if flying else 0)
            detail = self.combat._apply_hostile_damage(target, dmg, source=player)
            if flying:
                target.add_status(StatusEffect(name="坠落", value=1, remaining_rounds=1, source="反怪物电击枪"))
            result.update({"target": target.name, "damage": dmg, "flying_bonus": 15 if flying else 0, "hp_after": target.current_hp, "detail": detail})
        # 2. 备用血泵：回复20（走 heal，计入癌变/战终回吐），≤30% 额外30格挡
        elif name == "备用血泵":
            heal_detail = self.state.apply_heal(player, 20)
            healed = heal_detail["actual_heal"]
            shield_gained = 0
            if player.current_hp <= player.blood_limit * 0.3:
                player.shield += 30
                shield_gained = 30
            cancer = self.combat.check_cancer(player)
            result.update({"healed": healed, "hp_after": player.current_hp,
                           "shield_gained": shield_gained, "heal": heal_detail,
                           "cancer": cancer})
        # 3. 强光探照灯：目标蒙蔽2
        elif name == "强光探照灯":
            target = selected_enemy()
            if target is None:
                item.current_uses += 1
                return {"success": False, "error": "找不到敌方target_ref"}
            target.add_status(StatusEffect(name="蒙蔽", value=2, remaining_rounds=-1, source="强光探照灯"))
            result.update({"target": target.name, "蒙蔽": 2})
        # 4. 高压水枪：清除全场敌方所有持续X效果
        elif name == "高压水枪":
            cleared = []
            for m in self.state.get_all_enemy_side():
                before = len(m.status_effects)
                m.status_effects = [s for s in m.status_effects if s.remaining_rounds <= 0]
                if len(m.status_effects) != before:
                    cleared.append(m.name)
            result.update({"cleared_enemies": cleared})
        # 5. 储能电池：使用后立即获得12法力
        elif name == "储能电池":
            player.current_mana += 12
            result.update({"mana_gained": 12, "mana_after": player.current_mana})
        # 6. 急救箱：回复25（走 heal）并清一种负面持续
        elif name == "急救箱":
            heal_detail = self.state.apply_heal(player, 25)
            healed = heal_detail["actual_heal"]
            removed = None
            if aid_status is not None:
                player.status_effects.remove(aid_status)
                removed = aid_status.name
            cancer = self.combat.check_cancer(player)
            result.update({"healed": healed, "removed_status": removed,
                           "heal": heal_detail, "cancer": cancer})
        # 7. 干扰仪：全场敌方本回合无法发动道纹（加干扰1状态）
        elif name == "干扰仪":
            jammed = []
            for m in self.state.get_all_enemy_side():
                m.add_status(StatusEffect(name="干扰", value=1, remaining_rounds=1, source="干扰仪"))
                jammed.append(m.name)
            result.update({"jammed": jammed})
        # 8. 高爆手雷：目标15伤害 + 本回合攻击次数-1
        elif name == "高爆手雷":
            target = selected_enemy()
            if target is None:
                item.current_uses += 1
                return {"success": False, "error": "找不到敌方target_ref"}
            detail = self.combat._apply_hostile_damage(target, 15, source=player)
            # 攻击次数-1：用状态标记，本回合内 _monster_attack_actions 会读取
            target.add_status(StatusEffect(name="手雷减攻", value=1, remaining_rounds=1, source="高爆手雷"))
            result.update({"target": target.name, "damage": 15, "detail": detail, "nade_minus": 1})
        else:
            result.update({"note": "未知工具"})
        return {"success": True, "action": f"使用工具【{name}】", "result": result, "state": self.combat._get_combat_state()}

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
        if self.event_pool.current is None:
            return {"success": False, "error": "当前没有待结算事件；请先通过探索触发事件"}
        if name != self.event_pool.current:
            return {"success": False,
                    "error": f"只能结算当前事件【{self.event_pool.current}】，不能结算【{name}】"}
        ev = self.event_pool.events.get(name)
        if not ev:
            return {"success": False, "error": f"未知事件: {name}"}
        opt = next((o for o in ev["options"] if o["id"] == option_id), None)
        if opt is None:
            return {"success": False, "error": f"事件{name}无选项{option_id}"}
        res = resolve_option_effect(opt["text"], self, event_name=name, params=params)
        if res.get("interrupt_required"):
            interrupt = Interrupt(
                interrupt_type=InterruptType.UNSEEN_SCENE,
                context=res["interrupt_required"],
                description=f"事件【{name}】包含创造性效果，等待DM裁定",
                options=[], state_snapshot=self.state.to_dict(),
            )
            self._pending_interrupts.append(interrupt)
            return {"success": True, "action": f"事件【{name}】等待裁定",
                    "completed": False, "interrupt": interrupt.to_dict()}
        if res.get("error"):
            return {"success": False, "error": res["error"],
                    "pages": res.get("pages"), "instruction": res.get("instruction", "")}
        # 「拒绝改造」等带代价的选项也含「拒绝」，不能当拒绝类。
        # 真拒绝：正文写「无事发生」，或选项以「拒绝：/拒绝:」起头。
        reject_kw = ("无事发生", "观棋", "无视", "离开", "目送", "绕桥", "让炉", "避开", "捂住", "转身")
        text = opt["text"]
        is_reject = (any(k in text for k in reject_kw)
                     or text.startswith("拒绝：") or text.startswith("拒绝:"))
        if is_reject and any(r.name == "无所求" for r in self.state.relics):
            self.state.player.speed_limit += 1
            self.state.player.current_speed = self.state.player.speed_limit
            res["applied"].append("无所求：+1速限")
        self.event_pool.resolve(name)
        # 扭曲都市完成事件后附赠【发现】：正式随机只走DiceEngine，并等待显式选1。
        bonus = None
        if self.state.current_region == "扭曲都市":
            owned = {c.name for c in self.state.consumables}
            pool = [n for n in TWISTED_TOOL_LIBRARY if n not in owned]
            if pool:
                discovery = self._offer_item_discovery(pool, f"事件【{name}】完成后附赠发现")
                if not discovery.get("success"):
                    return discovery
                bonus = {"候选": discovery["choices"], "等待选择": True,
                         "来源": discovery["source"]}
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

    def _action_prepare_monster_phase(self, params: dict) -> dict:
        """第一阶段：只返回合法选项，绝不替AI选择道纹、目标或闪避。"""
        if self.state.pending_monster_phase:
            return {"success": False, "error": "已有待提交的怪物阶段决策"}
        options = self.combat.prepare_monster_phase()
        token = uuid.uuid4().hex
        self.state.pending_monster_phase = {"token": token, "round": self.state.current_round,
                                            "options": options}
        self.state.combat_subphase = CombatSubphase.MONSTER_ACTIONS.value
        return {"success": True, "action": "准备怪物阶段",
                "result": {"token": token, **options},
                "instruction": "请为每个actors条目提交完整选择后调用resolve_monster_phase"}

    def _action_resolve_monster_phase(self, params: dict) -> dict:
        """第二阶段：验证完整选择与一次性token，通过后统一结算。"""
        pending = self.state.pending_monster_phase
        if not pending:
            return {"success": False, "error": "请先调用prepare_monster_phase"}
        if params.get("token") != pending.get("token"):
            return {"success": False, "error": "怪物阶段token无效或已过期"}
        if pending.get("round") != self.state.current_round:
            return {"success": False, "error": "回合已变化，请重新prepare_monster_phase"}
        try:
            results = self.combat.resolve_monster_phase(
                params.get("choices"), prepared=pending["options"],
            )
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
        self.state.pending_monster_phase = {}
        self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
        player_dead = (self.state.player is None) or (not self.state.player.is_alive)
        return {
            "success": True,
            "action": "结算怪物阶段",
            "result": {"entries": len(results), "player_dead": player_dead,
                       "player_hp": self.state.player.current_hp if self.state.player else 0,
                       "details": results},
        }

    def _action_monster_phase(self, params: dict) -> dict:
        """拒绝旧版固定策略入口，防止绕过两阶段AI显式决策。"""
        return {"success": False,
                "error": "monster_phase已停用；请依次调用prepare_monster_phase与resolve_monster_phase"}

    def _action_round_start(self, params: dict) -> dict:
        """回始；完全平局死斗在每个新回合交换该回合首手方。"""
        relic_choices = params.get("relic_choices", {})
        self.combat.validate_round_start_relic_choices(relic_choices)
        result = self.combat.round_start(relic_choices)
        self.state.combat_subphase = CombatSubphase.PLAYER_ACTIONS.value
        if self.state.in_final_duel and self.state.duel_tie_alternating:
            if self.state.duel_rounds_started > 0:
                self.state.duel_round_first = (
                    "opponent_side" if self.state.duel_round_first == "player_side" else "player_side"
                )
            self.state.duel_rounds_started += 1
            self.state.duel_turn = self.state.duel_round_first
            result["duel_round_first"] = self.state.duel_round_first
            result["duel_tie_alternating"] = True
        return {"success": True, "action": "回始", "result": result}

    def _action_round_end(self, params: dict) -> dict:
        """回终"""
        result = self.combat.round_end()
        self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_START.value

        # 提取多路径胜利结果（已由 combat.round_end 结算）
        alt_paths = [e for e in result.get("effects", [])
                     if isinstance(e, dict) and e.get("type") in
                     ("sculpture", "proliferation", "cancer", "debt_bind")]

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
            "note": "多路径胜利（雕塑/癌变/还债）已结算；消耗品可在后续回合使用"
        }

    def _apply_terminal_artifacts_on_battle_start(self) -> list[str]:
        """终音被动：两边各一份。体外心脏翻自己；羔羊之泪每边持有就打一轮全场50%。"""
        logs = []
        player = self.state.player
        opp = next((e for e in self.state.enemies
                    if e.entity_type == "轮回者" and e.is_alive), None)

        if "体外心脏" in self.state.artifacts_owned and player:
            self._artifact_base_blood_limit = player.blood_limit
            player.blood_limit *= 2
            player.current_hp *= 2
            logs.append(
                f"体外心脏：血限与当前生命临时翻倍"
                f"({self._artifact_base_blood_limit}→{player.blood_limit})")
        if "体外心脏" in self.state.opponent_artifacts_owned and opp:
            self._opponent_artifact_base_blood_limit = opp.blood_limit
            opp.blood_limit *= 2
            opp.current_hp *= 2
            logs.append(
                f"对手体外心脏：血限与当前生命临时翻倍"
                f"({self._opponent_artifact_base_blood_limit}→{opp.blood_limit})")

        def _lamb_tear():
            for e in self.state.get_all_player_side() + self.state.get_all_enemy_side():
                loss = math.ceil(e.current_hp * 0.5)
                self.combat._apply_hostile_damage(e, loss)

        if "羔羊之泪" in self.state.artifacts_owned:
            _lamb_tear()
            logs.append("羔羊之泪：场上所有角色与怪物立刻失去50%当前生命")
        if "羔羊之泪" in self.state.opponent_artifacts_owned:
            _lamb_tear()
            logs.append("对手羔羊之泪：场上所有角色与怪物立刻失去50%当前生命")
        return logs

    def _action_battle_start(self, params: dict) -> dict:
        """战始：抽取出怪(数量=战斗场数-3,最低1,允许重复抽选同一怪物种族)→结算战始遗物。
        战斗背景：文档"战斗背景：（名称与影响）"仅为战斗推演格式里的占位提示，
        正文未定义任何具体名称与机制效果，故本引擎不做机制化处理，留给叙事层自由发挥。"""
        from .monsters import compute_draw_count, make_monster_entity
        relic_choices = params.get("relic_choices", {})
        # 先完成全部静态校验，再抽怪；非法遗物参数不得消耗正式随机源。
        self.combat.validate_battle_start_relic_choices(relic_choices)
        self.state.phase = GamePhase.IN_COMBAT.value
        self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_START.value
        self.state.current_battle += 1
        # 记录[战始]生命，作为[战终]清除"回复"类增益后的生命下限
        if self.state.player:
            self.state.player.battle_start_hp = self.state.player.current_hp
            self.state.player.healed_this_battle = 0
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
                m = make_monster_entity(monster_def)
                self.combat.init_monster_shards(m)  # 罪孽都市：[战始]自带碎片=专属道纹数值之和×2（洗劫/赎金/逼债的碎片来源）
                self.state.enemies.append(m)
                drawn_names.append(monster_def["name"])

        # 事件登记的下一场修正全部在战始一次性消费。
        modifiers = self.state.event_modifiers
        reveal_full_information = bool(modifiers.pop("next_battle_full_information", False))
        if modifiers.pop("bounty_extra_monster", False) and pool:
            roll = self.dice.auto_roll(
                f"bounty_monster_{self.state.current_battle}", pool,
                context="通缉悬赏榜额外帮派怪物",
            )
            bonus = make_monster_entity(roll["selected"])
            self.combat.init_monster_shards(bonus)
            self.state.enemies.append(bonus)
            drawn_names.append(bonus.name + "(悬赏额外)")
        next_fake = modifiers.pop("next_battle_fake_shards", 0)
        if next_fake:
            self.state.fake_shards += next_fake
        if modifiers.get("loan_active"):
            self.state.shards -= 10
            if self.state.shards < 0 and self.state.player:
                interest = math.ceil(abs(self.state.shards) / 10) * 5
                self.state.player.blood_limit = max(0, self.state.player.blood_limit - interest)
                self.state.player.current_hp = min(self.state.player.current_hp, self.state.player.blood_limit)
        red_spring = modifiers.get("red_spring_battle_losses", 0)
        if red_spring > 0 and self.state.player:
            self.combat._raw_hp_loss(self.state.player, 4)
            modifiers["red_spring_battle_losses"] = red_spring - 1

        # 事件登记的"下一场额外出现的怪物"（如龙心谷"追求者·拿走口粮"）
        forced = list(self.state.forced_monsters_next_battle)
        self.state.forced_monsters_next_battle = []
        for fm in forced:
            m = make_monster_entity(fm)
            self.combat.init_monster_shards(m)
            self.state.enemies.append(m)
            drawn_names.append(fm["name"] + "(额外出现)")
        arena_percent = modifiers.pop("arena_health_percent", 0)
        if arena_percent:
            for monster in self.state.enemies:
                gain = math.ceil(monster.blood_limit * arena_percent / 100)
                monster.blood_limit += gain
                monster.current_hp += gain

        # 战始先清零当前法力，再结算战始遗物。回始再获得等同法限的法力。
        # 折速/鲜血契约因此叠在 0 上，首回合 = 遗物加成 + 法限，不会被赋值冲掉。
        if self.state.player and self.state.player.is_alive:
            self.state.player.current_mana = 0
        relic_logs = self.combat.process_relics(TriggerTiming.BATTLE_START, {"relic_choices": relic_choices})

        artifact_logs = self._apply_terminal_artifacts_on_battle_start()

        return {
            "success": True, "action": "战始",
            "battle_number": self.state.current_battle,
            "region": region,
            "draw_count": draw_count,
            "enemies": drawn_names,
            "full_information": ([enemy.to_dict() for enemy in self.state.enemies]
                                 if reveal_full_information else None),
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
            "relics": [r.to_dict() for r in e.relics],
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
        for relic in d.get("relics", []):
            e.relics.append(Relic(name=relic["name"], effect=relic.get("effect", ""),
                                  tags=list(relic.get("tags") or [])))
        for st in d.get("status_effects", []):
            e.status_effects.append(StatusEffect(name=st["name"], value=st.get("value", 0),
                                                  remaining_rounds=st.get("remaining_rounds", -1),
                                                  source=st.get("source", "")))
        return e

    def _serialize_full_character(self) -> dict:
        """完整封存：玩家+队友(朋友/员工)+遗物+残韵+碎片+死者之书等级+属性点+终音法器/初拥之夜/真龙之心记录。
        死斗载入时写入 opponent_relics / opponent_artifacts_owned，被动按持有者一侧各自结算。"""
        s = self.state
        return {
            "player": self._serialize_entity_full(s.player) if s.player else None,
            "friends": [self._serialize_entity_full(f) for f in s.friends],
            "employees": [self._serialize_entity_full(e) for e in s.employees],
            "relics": [r.to_dict() for r in s.relics],
            "resonance": dict(s.resonance),
            "shards": s.shards,
            "death_book_wisdom": list(s.death_book_wisdom),
            "death_book_legacies": [dict(entry) for entry in s.death_book_legacies],
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
            self.state.death_book_legacies = self.death_book.load()
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
        self.state.opponent_relics = [
            Relic(name=r.get("name", ""), effect=r.get("effect", ""), tags=r.get("tags") or [])
            for r in (candidate_snapshot.get("relics") or [])
            if r.get("name")
        ]
        self.state.opponent_artifacts_owned = list(candidate_snapshot.get("artifacts_owned") or [])
        # 旧快照可能只记下了 dragon_traits / first_embrace_traits 名字，遗物缺 tag
        dragon_names = list(candidate_snapshot.get("dragon_traits") or [])
        embrace_names = list(candidate_snapshot.get("first_embrace_traits") or [])
        existing = {r.name for r in self.state.opponent_relics}
        for r in self.state.opponent_relics:
            if r.name in dragon_names and "龙族" not in r.tags:
                r.tags.append("龙族")
            if r.name in embrace_names and "血族" not in r.tags:
                r.tags.append("血族")
        for name in dragon_names:
            if name not in existing:
                self.state.opponent_relics.append(Relic(name=name, effect="", tags=["龙族"]))
                existing.add(name)
        for name in embrace_names:
            if name not in existing:
                self.state.opponent_relics.append(Relic(name=name, effect="", tags=["血族"]))
                existing.add(name)
        # 与战始相同：死斗开场先清零双方轮回者法力，回始再获得等同法限。
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            if entity.entity_type == "轮回者" and entity.is_alive:
                entity.current_mana = 0
                entity.battle_start_hp = entity.current_hp
                entity.healed_this_battle = 0
        artifact_logs = self._apply_terminal_artifacts_on_battle_start()

        challenger_key = self._duel_priority_key(challenger_player)
        opponent_key = self._duel_priority_key(opponent_leader) if opponent_leader else (0, 0, 0, 0)
        complete_tie = challenger_key == opponent_key
        if complete_tie:
            roll = self.dice.auto_roll(
                "final_duel_complete_tie_first",
                ["player_side", "opponent_side"],
                context="最终死斗四项先手属性完全平局：随机首手",
            )
            first_mover = roll["selected"]
        else:
            first_mover = "player_side" if challenger_key > opponent_key else "opponent_side"
        self.state.duel_turn = first_mover
        self.state.duel_tie_alternating = complete_tie
        self.state.duel_round_first = first_mover
        self.state.duel_rounds_started = 0
        self.state.phase = GamePhase.IN_COMBAT.value
        self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_START.value

        optional = []
        for r in self.state.opponent_relics:
            if r.name in ("折速法印", "鲜血契约", "三相残韵盘", "卖身契"):
                optional.append({"side": "opponent_side", "name": r.name, "effect": r.effect})
        for r in self.state.relics:
            if r.name in ("折速法印", "鲜血契约", "三相残韵盘", "卖身契"):
                optional.append({"side": "player_side", "name": r.name, "effect": r.effect})

        return {
            "outcome": "duel_start",
            "opponent_name": opponent_leader.name if opponent_leader else "未知对手",
            "opponent_side": [e.name for e in opponent_side],
            "first_mover": first_mover,
            "complete_tie_alternating": complete_tie,
            "optional_relics": optional,
            "artifact_logs": artifact_logs,
            "instruction": "第8场最终死斗开始：双方交替出手，残韵可任意时刻插队，无法逃跑。"
                           "可选遗物由持有者自己决定是否发动（activate_duel_relic）；"
                           "请调用 resolve_final_duel(outcome=victory/defeat) 结算胜负",
        }

    def _action_activate_duel_relic(self, params: dict) -> dict:
        """死斗开场：持有者自己决定是否发动可选战始遗物。side=player_side/opponent_side。"""
        if not self.state.in_final_duel:
            return {"success": False, "error": "当前没有进行中的最终死斗"}
        side = params.get("side", "")
        name = params.get("relic", "")
        use = params.get("use", True)
        if side not in ("player_side", "opponent_side"):
            return {"success": False, "error": "side必须是 player_side 或 opponent_side"}
        pool = self.state.relics if side == "player_side" else self.state.opponent_relics
        holder = self.state.player if side == "player_side" else next(
            (e for e in self.state.enemies if e.entity_type == "轮回者" and e.is_alive), None)
        if holder is None:
            return {"success": False, "error": "找不到该侧轮回者"}
        if not any(r.name == name for r in pool):
            return {"success": False, "error": f"{side}未持有遗物: {name}"}
        if not use:
            return {"success": True, "action": f"{holder.name}放弃发动【{name}】",
                    "result": {"relic": name, "used": False}}
        if name == "折速法印":
            try:
                x = int(params.get("x", 0))
            except (TypeError, ValueError):
                return {"success": False, "error": "X必须为整数"}
            if x < 1 or x > holder.current_speed:
                return {"success": False, "error": f"折速X须在1~当前速度{holder.current_speed}之间"}
            holder.current_speed -= x
            holder.current_mana += 6 * x
            return {"success": True, "action": f"{holder.name}发动【折速法印】",
                    "result": {"relic": name, "used": True, "x": x,
                               "speed": holder.current_speed, "mana": holder.current_mana}}
        if name == "鲜血契约":
            try:
                x = int(params.get("x", 0))
            except (TypeError, ValueError):
                return {"success": False, "error": "X必须为整数"}
            cap = holder.blood_limit // 5
            if x < 1 or x > cap or x > holder.current_hp:
                return {"success": False, "error": f"鲜血契约X须在1~{cap}且不超过当前生命"}
            self.combat._pay_bleed_cost(holder, x)
            holder.current_mana += x
            return {"success": True, "action": f"{holder.name}发动【鲜血契约】",
                    "result": {"relic": name, "used": True, "x": x,
                               "hp": holder.current_hp, "mana": holder.current_mana}}
        return {"success": False, "error": f"【{name}】不是死斗开场可选遗物，或尚未接线"}

    def _action_resolve_final_duel(self, params: dict) -> dict:
        """
        死斗结算：
        胜利=先领取本次所属副本的终音法器(见choose_terminal_artifact)，再(连同队伍)被完整封存，
        成为下一位挑战者的候选人(二阶以上副本未实现，以封存代替"进入下一阶副本")；
        失败=当前挑战者战败，提交三段式death_book_entry并触发死之传承，本次轮回结束。
        """
        if not self.state.in_final_duel:
            return {"success": False, "error": "当前没有进行中的最终死斗"}
        outcome = params.get("outcome", "")
        if outcome not in ("victory", "defeat"):
            return {"success": False, "error": "outcome必须是 victory 或 defeat"}
        legacy = params.get("death_book_entry")

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
            player = self.state.player
            if player is not None:
                player.current_hp = 0
                player.is_alive = False
            self.state.last_death_cause = "duel"
            if isinstance(legacy, dict):
                try:
                    self.state.pending_death_draft = validate_legacy(
                        legacy, self.state.death_book_capacity)
                except ValueError:
                    self.state.pending_death_draft = {}
            return {"success": True, "action": "死斗结算",
                    "result": {"outcome": "defeat",
                               "instruction": "败者失去轮回者身份，已触发死之传承，等待审核后写入死者之书"}}

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
        heal_detail = self.state.apply_heal(player, heal_amount)

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
        target = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
        if not player or target is None or target not in self.state.enemies:
            return {"success": False, "error": "target_ref不是存活敌方目标"}
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
        target = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
        statement = params.get("statement", "")
        if target is None or not statement:
            return {"success": False, "error": "必须指定合法target_ref与statement"}
        self.state.truth_eye_cooldown = 2
        interrupt = Interrupt(
            interrupt_type=InterruptType.CUSTOM,
            context={"ability": "真理眼", "target": target.name, "statement": statement},
            description=f"对{target.name}发动【真理眼】，要求其就以下内容言明真理，否则无法开口：\n{statement}\n"
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
        chizu = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
        if (not player or chizu is None or chizu not in self.state.friends
                or chizu.entity_type != "赤族" or not chizu.is_alive):
            return {"success": False, "error": "target_ref不是存活赤族"}
        amount = chizu.current_hp
        chizu.current_hp = 0
        chizu.is_alive = False
        heal_detail = self.state.apply_heal(player, amount)
        return {"success": True, "action": "血食",
                "result": {"sacrificed": chizu.name, "healed": heal_detail["actual_heal"], "player_hp": player.current_hp}}

    # ==================== 终音法器：可主动发动的具体效果 ====================

    def _action_use_black_card(self, params: dict) -> dict:
        """黑金名片(罪孽都市终音)：[战始]可使所有敌方[目标][血限]减半，付出等量[碎片](允许负债，负债≤50)"""
        if "黑金名片" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有黑金名片"}
        if self.state.event_modifiers.get("black_card_used_battle") == self.state.current_battle:
            return {"success": False, "error": "黑金名片本场战始已经发动"}
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
        self.state.event_modifiers["black_card_used_battle"] = self.state.current_battle
        self.combat._on_cost_paid(self.state.player)
        return {"success": True, "action": "黑金名片",
                "result": {"cost": total_cost, "shards": self.state.shards, "halved": halved}}

    def _action_use_crime_vault(self, params: dict) -> dict:
        """罪业金库(罪孽都市终音)：[回始]可消耗X点[碎片](X≤2%当前碎片)，获得2X点格挡"""
        if "罪业金库" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有罪业金库"}
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}
        target_round = self.state.current_round + 1
        if self.state.event_modifiers.get("crime_vault_used_round") == target_round:
            return {"success": False, "error": "罪业金库本回始已经发动"}
        x = params.get("x", 0)
        cap = math.floor(self.state.shards * 0.02)
        if not isinstance(x, int) or x < 1 or x > cap:
            return {"success": False, "error": f"X必须是1~{cap}(当前碎片{self.state.shards}的2%)之间的整数"}
        self.state.shards -= x
        player.gain_shield(2 * x)
        self.state.event_modifiers["crime_vault_used_round"] = target_round
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
        target = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
        if not player or target is None:
            return {"success": False, "error": "target_ref不是当前合法目标"}
        self.state.godfather_revolver_uses += 1
        gun.current_uses -= 1
        damage = math.ceil(player.blood_limit * 0.3) * self.state.godfather_revolver_uses
        dmg = self.combat._apply_hostile_damage(target, damage, "必中", player)
        return {"success": True, "action": "教父左轮",
                "result": {"target": target.name, "damage": damage,
                           "uses_this_battle": self.state.godfather_revolver_uses,
                           "ammo_remaining": gun.current_uses, **dmg}}

    def _action_select_shared_dragon_heart(self, params: dict) -> dict:
        """共心环(龙心谷终音)：[战始]选定自身拥有的一枚【××龙心】类型，本场自身/朋友/员工均可用它抵消同类型代价"""
        if "共心环" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有共心环"}
        if self.state.shared_dragon_heart_type:
            return {"success": False, "error": "共心环本场战始已经选择龙心"}
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
        ally = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
        if ally is None or ally not in self.state.friends + self.state.employees:
            return {"success": False, "error": "target_ref不是朋友或员工"}
        target_ref = params["target_ref"]
        if target_ref not in self.state.fuyuebei_declared:
            self.state.fuyuebei_declared.append(target_ref)
        return {"success": True, "action": "负岳碑·预声明保护",
                "result": {"protected": ally.name, "declared": list(self.state.fuyuebei_declared)}}

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
    # 龙威通过prepare_monster_phase收窄合法目标列表实现；resolve只接受prepare列出的目标。

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
        if self.state.phase == GamePhase.IN_COMBAT.value:
            self.combat._on_cost_paid(player)
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
        monster_ref = params.get("monster_ref", "")
        monster = None
        if isinstance(monster_ref, str) and monster_ref.startswith("enemy:"):
            try:
                index = int(monster_ref.split(":", 1)[1])
                monster = self.state.enemies[index]
            except (ValueError, IndexError):
                monster = None
        if monster is None or monster.is_alive:
            return {"success": False, "error": "monster_ref不是已命零怪物"}
        player = self.state.player
        heal_detail = self.state.apply_heal(player, 12)
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
        target_round = self.state.current_round + 1
        if self.state.event_modifiers.get("dragon_wings_used_round") == target_round:
            return {"success": False, "error": "烬翼本回始已经发动"}
        x = params.get("x", 0)
        if not isinstance(x, int) or x < 1:
            return {"success": False, "error": "x必须是正整数"}
        cost = 3 * x
        if self.state.dragon_nature < cost:
            return {"success": False, "error": f"龙性不足，需要{cost}，当前{self.state.dragon_nature}"}
        self.state.dragon_nature -= cost
        self.state.event_modifiers["dragon_wings_used_round"] = target_round
        self.state.player.add_status(StatusEffect(name="飞行", value=x, remaining_rounds=x, source="烬翼"))
        return {"success": True, "action": "烬翼",
                "result": {"flying_rounds": x, "dragon_nature": self.state.dragon_nature}}

    def _action_battle_end(self, params: dict) -> dict:
        """战终；仍有未移出的存活敌人时不得跳过战斗直接结算。"""
        living = [e.name for e in self.state.enemies
                  if e.is_alive and not e.removed_without_kill and not e.is_sculptured
                  and not e.is_proliferated and not e.is_debt_bound]
        escaping = self.state.event_modifiers.pop("escape_at_battle_end", False)
        if living and not escaping:
            return {"success": False, "error": f"仍有存活敌人，不能结算战终: {living}"}
        if escaping:
            for enemy in self.state.enemies:
                if enemy.is_alive:
                    enemy.removed_without_kill = True
                    enemy.is_alive = False
        # 员工经济系统·工资结算门槛：先按"存活+已部署+非还债"员工计算工资写入待决列表；
        # 任何一名待决(值不为None，代表尚未pay/refuse)即阻塞后续战终结算。
        self._compute_pending_wages()
        still_pending = {k: v for k, v in self.state.pending_wage_decisions.items() if v is not None}
        if still_pending:
            return {
                "success": True,
                "action": "战终工资待决",
                "completed": False,
                "instruction": "请先为以下员工逐个调用 pay_employee_wage(name, decision=pay/refuse)，再重新调用battle_end",
                "pending_wage_decisions": still_pending,
            }
        self.state.pending_wage_decisions = {}
        # 员工经济系统·死亡离队：计入黑名单并从名单移除，避免重复计数
        for emp in [e for e in self.state.employees if not e.is_alive and not e.is_debt_bound]:
            self.state.employees.remove(emp)
            self._blacklist_departure("死亡离队")

        relic_end = self.combat.process_relics(TriggerTiming.BATTLE_END)
        # 碎片奖励计算（被雕塑/癌变/还债/封印移出的怪物不视为击杀，不产碎片）
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
                                        "癌变" if monster.is_proliferated else "还债")})
                continue
            if not monster.is_alive:
                reward = math.ceil(monster.battle_start_blood_limit * 0.02) + len(monster.dao_wen) * 5
                if has_money_bag:
                    reward += math.ceil(monster.battle_start_blood_limit * 0.02)  # 钱袋：额外+[战始][血限]2%
                shard_reward += reward

        modifiers = self.state.event_modifiers
        if modifiers.pop("arena_double_loot", False):
            shard_reward *= 2
        shard_reward += modifiers.pop("bounty_reward", 0)
        if modifiers.pop("arena_bet_three_rounds", False) and self.state.current_round <= 3:
            shard_reward += 45
        self.state.shards += shard_reward
        if modifiers.pop("scarlet_fruit_active", False) and self.state.player:
            self.state.player.blood_limit += 2
        pale_flower_bonus = 1 if modifiers.pop("pale_flower_active", False) else 0
        modifiers.pop("brand_nail_target_ref", None)

        # [战终]对所有角色统一清除局内回复、格挡与状态；不得只清轮回者。
        all_characters = (([self.state.player] if self.state.player else [])
                          + self.state.friends + self.state.employees
                          + self.state.temp_friends + self.state.enemies)
        _COST_STATUS = {"流血", "衰老", "异变", "崩解"}
        for entity in all_characters:
            entity.clear_shield()
            if entity.healed_this_battle > 0:
                entity.current_hp = (max(1, entity.current_hp - entity.healed_this_battle)
                                     if entity.is_alive else 0)
            entity.healed_this_battle = 0
            entity.status_effects = [s for s in entity.status_effects if s.name in _COST_STATUS]
            entity._bizhai = []
            entity._qingsuan = []
            entity.current_speed = entity.speed_limit
            if hasattr(entity, "_bianxing_original"):
                entity.attack_power, entity.attack_count = entity._bianxing_original
                delattr(entity, "_bianxing_original")
        self.state.sealed_relics = {}

        # 体外心脏：临时翻倍的血限[战终]还原为基准值，当前生命同步封顶。
        if (self.state.player and "体外心脏" in self.state.artifacts_owned
                and self._artifact_base_blood_limit > 0):
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

        # 恢复精力；苍白之花的战终奖励叠加在基础3点之后。
        self.state.energy = 3 + pale_flower_bonus

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

    # ==================== 死之传承 / 死者之书 ====================

    def _reload_death_book(self):
        self.state.death_book_legacies = self.death_book.load()

    def _reset_after_death(self):
        """命零审核结束后结束本轮回，从文件装回已落盘遗言。"""
        self.state = GameState()
        self.combat.state = self.state
        self._reload_death_book()

    def _infer_death_cause(self, action_type: str = "") -> str:
        if self.state.last_death_cause:
            return self.state.last_death_cause
        player = self.state.player
        if player is not None and getattr(player, "is_proliferated", False):
            return "cancer"
        if player is not None and player.mutation_count >= Entity.MUTATION_COLLAPSE_THRESHOLD:
            return "collapse"
        if self.state.in_final_duel or action_type == "resolve_final_duel":
            return "duel"
        if action_type == "round_end":
            return "mediocrity"
        if action_type == "consume_item":
            return "collapse"
        return "attack"

    def _queue_death_inheritance_if_needed(self, action_type: str = "") -> Optional[Interrupt]:
        """轮回者命零后只抛一次死之传承中断，草稿写入 context 待审核。"""
        if self.state.death_inheritance_queued:
            return None
        player = self.state.player
        if player is None or player.is_alive:
            return None
        last_action = self._action_history[-1] if self._action_history else None
        draft = self.state.pending_death_draft or draft_legacy(
            self.state, self._infer_death_cause(action_type), last_action,
            self.state.death_book_capacity)
        self.state.pending_death_draft = draft
        self.state.death_inheritance_queued = True
        interrupt = Interrupt(
            interrupt_type=InterruptType.DEATH_INHERITANCE,
            context={
                "draft": dict(draft),
                "cause": self._infer_death_cause(action_type),
                "player": player.name,
                "battle": self.state.current_battle,
                "region": self.state.current_region,
            },
            description=(
                f"{player.name}已[命零]，触发【死之传承】。\n"
                f"草稿：触发点「{draft['trigger_point']}」／"
                f"岔路「{draft['fork']}」／代价预算「{draft['cost_budget']}」\n"
                "请审核：通过、修改后写入、或驳回（驳回不写入死者之书）。"
            ),
            options=[
                {"id": "approve", "label": "通过", "description": "按草稿写入《死者之书》"},
                {"id": "edit", "label": "修改后写入", "description": "提交修改后的三段式再写入"},
                {"id": "reject", "label": "驳回", "description": "不写入《死者之书》，本轮回结束"},
            ],
            state_snapshot=self.state.to_dict(),
        )
        self._pending_interrupts.append(interrupt)
        return interrupt

    def _prepare_death_ruling(self, interrupt: Interrupt, ruling_data: dict) -> dict:
        action = (ruling_data or {}).get("action") or (ruling_data or {}).get("option") or ""
        action = str(action).strip().lower()
        aliases = {"通过": "approve", "修改后写入": "edit", "驳回": "reject",
                   "approve": "approve", "edit": "edit", "reject": "reject"}
        action = aliases.get(action, action)
        if action not in {"approve", "edit", "reject"}:
            raise ValueError("死之传承裁定必须是 approve / edit / reject")
        if action == "reject":
            return {"action": "reject"}
        if action == "approve":
            source = (ruling_data or {})
            if all(source.get(field) for field in ("trigger_point", "fork", "cost_budget")):
                legacy = validate_legacy(source, self.state.death_book_capacity)
            else:
                legacy = validate_legacy(
                    interrupt.context.get("draft") or self.state.pending_death_draft,
                    self.state.death_book_capacity)
            return {"action": "approve", "legacy": legacy}
        legacy = validate_legacy({
            "trigger_point": (ruling_data or {}).get("trigger_point"),
            "fork": (ruling_data or {}).get("fork"),
            "cost_budget": (ruling_data or {}).get("cost_budget"),
            **({"title": ruling_data["title"]} if (ruling_data or {}).get("title") else {}),
        }, self.state.death_book_capacity)
        return {"action": "edit", "legacy": legacy}

    def _commit_death_ruling(self, prepared: dict) -> dict:
        if prepared["action"] == "reject":
            self._reset_after_death()
            return {"written": False, "rejected": True,
                    "instruction": "遗言已驳回，未写入死者之书；请调用 setup_attributes 开始新的轮回者"}
        written = self.death_book.append(prepared["legacy"])
        self._reset_after_death()
        return {
            "written": True,
            "legacy": {k: written[k] for k in ("trigger_point", "fork", "cost_budget")},
            "total_legacies": len(self.state.death_book_legacies),
            "path": str(self.death_book.path),
            "instruction": "遗言已写入死者之书；请调用 setup_attributes 开始新的轮回者",
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

        interrupt = self._pending_interrupts[0]
        is_death = (
            interrupt.interrupt_type == InterruptType.DEATH_INHERITANCE
            or interrupt_type == InterruptType.DEATH_INHERITANCE.value
        )
        prepared = None
        if is_death:
            try:
                prepared = self._prepare_death_ruling(interrupt, ruling_data or {})
            except ValueError as exc:
                return {"success": False, "error": str(exc),
                        "instruction": "非法遗言未写入；中断仍在，请改提交合法三段式"}

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
        result = {
            "success": True,
            "action": "DM裁定",
            "ruling_id": ruling_id,
            "interrupt_type": interrupt_type,
            "ruling_text": ruling_text,
            "ruling_data": ruling_data,
            "note": "裁定已保存，下次类似场景将自动匹配"
        }
        if prepared is not None:
            result["death_book"] = self._commit_death_ruling(prepared)
        return result

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

    # ==================== 存档系统 ====================

    SAVE_FORMAT_VERSION = 2

    def save_game(self, slot: str = "auto") -> dict:
        """保存可完整往返的版本化快照；只允许load_game读取本引擎生成的本地文件。"""
        snapshot = {
            "state": self.state,
            "dice": self.dice,
            "combat_runtime": self._snapshot_combat_runtime(),
            "event_triggered": set(self.event_pool.triggered),
            "event_current": self.event_pool.current,
            "pending_interrupts": self._pending_interrupts,
            "action_history": self._action_history,
            "last_result": self._last_result,
        }
        encoded = base64.b64encode(pickle.dumps(snapshot, protocol=pickle.HIGHEST_PROTOCOL)).decode("ascii")
        save_data = {"format": "linji-save", "version": self.SAVE_FORMAT_VERSION,
                     "payload": encoded, "timestamp": time.time()}
        filepath = os.path.join(self.save_dir, f"save_{slot}.json")
        os.makedirs(self.save_dir, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as handle:
            json.dump(save_data, handle, ensure_ascii=False, indent=2)
        return {"success": True, "filepath": filepath, "version": self.SAVE_FORMAT_VERSION}

    def load_game(self, slot: str = "auto") -> dict:
        """原子恢复完整状态、随机源、事件池、待决项和战斗运行态。"""
        filepath = os.path.join(self.save_dir, f"save_{slot}.json")
        if not os.path.exists(filepath):
            return {"success": False, "error": f"存档不存在: {filepath}"}
        try:
            with open(filepath, "r", encoding="utf-8") as handle:
                save_data = json.load(handle)
            if save_data.get("format") != "linji-save" or save_data.get("version") != self.SAVE_FORMAT_VERSION:
                return {"success": False, "error": "不支持的存档格式或版本"}
            restored = pickle.loads(base64.b64decode(save_data["payload"].encode("ascii")))
            if not isinstance(restored.get("state"), GameState) or not isinstance(restored.get("dice"), DiceEngine):
                return {"success": False, "error": "存档内容类型无效"}
        except Exception as exc:
            return {"success": False, "error": f"存档损坏: {exc}"}

        self.state = restored["state"]
        self.dice = restored["dice"]
        self.combat.state = self.state
        self.combat.dice = self.dice
        self.event_pool.triggered = set(restored["event_triggered"])
        self.event_pool.current = restored["event_current"]
        self._pending_interrupts = restored["pending_interrupts"]
        self._action_history = restored["action_history"]
        self._last_result = restored["last_result"]
        self._restore_combat_runtime(restored["combat_runtime"])
        return {"success": True, "filepath": filepath, "version": self.SAVE_FORMAT_VERSION,
                "state": self.state.to_dict()}

    def get_action_history(self) -> list[dict]:
        """获取行动历史"""
        return self._action_history

    def get_rulings_history(self) -> list[dict]:
        """获取所有DM裁定"""
        return [r.to_dict() for r in self.rulings_db.get_all_rulings()]


import math  # 修复 battle_end 中使用 math.ceil
