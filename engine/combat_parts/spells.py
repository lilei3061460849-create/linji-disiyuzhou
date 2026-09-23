"""CombatEngine 分片：内置法术流程表与装配、DSL 步骤展开、反应法术提交校验、全局时点法术、
自动反应（闪避/失去生命后/失去生命前）、法术反应解析

方法体自 engine/combat.py 原样搬来（逐字节），行为与对外契约不变；
CombatEngine 仍是唯一入口（门面类继承本 Mixin）。
"""
from __future__ import annotations
import math
import weakref
from typing import Optional, Any
from ..models import Entity, StatusEffect, GameState, DaoWenInstance, DaoWen, Spell, Consumable
from ..daowen import DaoWenEngine, ResonanceEngine
from ..dice import DiceEngine
from ..enums import (ActionPhase, TriggerTiming, InterruptType, DamageType,
                     EffectScope, EffectPolarity, CostType)
from ..dm_rulings import Interrupt
from ..combat_events import CombatEvent, CombatEventType, register_combat_event_observer
from ..combat_hooks import CombatHookManager
from ..effect_context import EffectContext, make_context, normalize_context
from ..mechanisms import MECHANISMS, Phase, TriggerBus, TriggerContext
from ..personality import remove_personality
from ..models import MONSTER_MANA_RELIC


class SpellReactionMixin:
    SPELL_FLOWS = {
        "先发制人": {"trigger": ActionPhase.BEFORE_DAMAGE_TAKEN.value, "steps": [("杀伐", "attacker")]},
        "后发制人": {"trigger": ActionPhase.BEFORE_DAMAGE_TAKEN.value, "steps": [("庇护", "self")]},
        "生生不息": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("再生", "self")]},
        "以牙还牙": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("再生", "self"), ("杀伐", "attacker")]},
        "借力打力": {"trigger": ActionPhase.BEFORE_DAMAGE_TAKEN.value, "steps": [("庇护", "self"), ("杀伐", "attacker")]},
        "不死不休": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("血债", "attacker")], "loop": True},
        "千刀万剐": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("再生", "self"), ("血债", "attacker")], "loop": True},
        "咎由自取": {"trigger": "目标发动道纹前", "steps": [("坠落", "target"), ("杀伐", "target"), ("血债", "target")]},
        "镇魔印": {"trigger": TriggerTiming.SELF_TURN_END.value,
                   "steps": [("封印", "any")],
                   "effect_flow": "自身回合结束后→发动封印X于任意目标",
                   "automatic": True},
        # 血炼周天（2026-09-16 补流程，清单 D1）：
        # 失去生命后→发动再生→发动透支→失去生命后（循环）。
        # 【透支】的流血会再次触发「失去生命后」，由此自驱动循环。
        "血炼周天": {"trigger": ActionPhase.AFTER_LIFE_LOST.value,
                     "steps": [("再生", "self"), ("透支", "self")],
                     "effect_flow": "失去生命后→发动再生→发动透支→失去生命后（循环）",
                     "loop": True},
    }

    # 内置法术的所需道纹（唯一事实源；api.GameEngine.SPELL_REGISTRY 是本表的别名）。
    # 键与 SPELL_FLOWS 一一对应——凡有流程的内置法术都必须在此登记所需道纹，
    # 否则「持道纹即可施法」判定无法知道该法术需要什么。
    BUILTIN_SPELL_DAOWEN = {
        "先发制人": ["杀伐"],
        "后发制人": ["庇护"],
        "生生不息": ["再生"],
        "以牙还牙": ["杀伐", "再生"],
        "借力打力": ["杀伐", "庇护"],
        "不死不休": ["血债"],
        "千刀万剐": ["血债", "再生"],
        "咎由自取": ["坠落", "杀伐", "血债"],
        "镇魔印": ["封印"],
        "血炼周天": ["再生", "透支"],
    }

    # ------------------------------------------------------------------
    # 「法术不再需要学习」：内置法术对任何持有全部所需道纹的角色直接开放。
    # 自创法术仍然挂在实体自己的 spells 列表上（定义要随存档走），
    # 内置法术不再写入该列表，改为按道纹实时推导。
    # ------------------------------------------------------------------

    def _builtin_spell_flows(self, holder: Entity) -> dict[str, dict]:
        """当前**已装配**的内置法术：所需道纹全部持有且可发动，并经 use_spell 装配。

        「可用」与「已装配」是两件事：持道纹=可以装配（不再需要学习），
        装配=表达"我打算用它"，只有装配后才会在触发时点自动结算。
        这样免去了学习门槛，但保留了意图门槛——否则一个同时持有【再生】
        【血债】的角色每次挨打都要对四种反应法术逐个表态，战斗无法推进。
        """
        flows: dict[str, dict] = {}
        if holder is None or not holder.is_alive:
            return flows
        armed = set(getattr(holder, "armed_spells", None) or ())
        for name, required in self.BUILTIN_SPELL_DAOWEN.items():
            flow = self.SPELL_FLOWS.get(name)
            if flow is None or name not in armed:
                continue
            if all(d in holder.dao_wen and holder.dao_wen[d].can_use()
                   for d in required):
                flows[name] = flow
        return flows

    def buildable_spells(self, holder: Entity) -> list[str]:
        """当前凭持有道纹**可以装配**但尚未装配的内置法术名。"""
        if holder is None or not holder.is_alive:
            return []
        armed = set(getattr(holder, "armed_spells", None) or ())
        out = []
        for name, required in self.BUILTIN_SPELL_DAOWEN.items():
            if name in armed or name not in self.SPELL_FLOWS:
                continue
            if all(d in holder.dao_wen and holder.dao_wen[d].can_use()
                   for d in required):
                out.append(name)
        return sorted(out)

    def spell_definition(self, holder: Entity, name: str):
        """取一个法术的定义：自创法术读实体 spells，内置法术按道纹即时合成。

        返回 Spell（内置法术为临时合成对象，不写回 holder.spells）；
        取不到返回 None。合成对象每次新建，禁止拿它做身份比较。
        """
        if holder is None or not name:
            return None
        from ..models import Spell
        spell = next((sp for sp in holder.spells if sp.name == name), None)
        if spell is not None:
            return spell
        required = self.BUILTIN_SPELL_DAOWEN.get(name)
        flow = self.SPELL_FLOWS.get(name)
        if required is None or flow is None:
            return None
        return Spell(
            name=name,
            required_daowen=list(required),
            trigger_condition=flow.get("effect_flow", ""),
            effect_flow=flow.get("effect_flow", ""),
            rank=len(required),
            automatic=bool(flow.get("automatic")),
        )

    # 自创法术文本→执行：解析 trigger_condition / effect_flow 为 SPELL_FLOWS 同构结构。
    # 2026-08-29 重写：接入 engine.spell_dsl（触发时机词汇表扩展、显式目标声明、
    # 条件分支、真循环）。学习环节（engine/api.py._pre_battle_xuexi）已经用同一
    # 个解析器做过强校验，这里理论上不会再遇到解析失败；仍保留 try/except 兜底，
    # 解析失败时返回 None（不触发），而不是让战斗结算抛出未处理异常。
    #
    # 全部 12 种触发时机现已全部接线：
    #   受到伤害前 / 失去生命后 / 目标发动道纹前 —— 复用既有反应型法术决策窗口
    #     （prepare/validate/resolve_spell_reactions，见 resolve_attack）。
    #   战始 / 战终 / 回始 / 回终 / 敌回始 / 敌回终 —— 全局时点法术
    #     （prepare/validate/resolve_global_trigger_spells，挂在对应 action 的
    #     可选 spell_choices 参数上，battle_start/battle_end/round_start/
    #     round_end 四个 action 里；敌回始/敌回终在普通战斗映射为怪物阶段
    #     开始前/结束后，在死斗里映射为对方视角的 round_start/round_end）。
    #   受到伤害后 / 失去生命前 —— 伤害结算管线内部新增的决策窗口
    #     （_apply_hostile_damage_inner 落地后 / take_damage 扣减前）。
    _WIRED_TRIGGERS = (
        ActionPhase.BEFORE_DAMAGE_TAKEN.value,
        ActionPhase.AFTER_LIFE_LOST.value,
        "目标发动道纹前",
        TriggerTiming.BATTLE_START.value,
        TriggerTiming.BATTLE_END.value,
        TriggerTiming.ROUND_START.value,
        TriggerTiming.ROUND_END.value,
        TriggerTiming.SELF_TURN_END.value,
        TriggerTiming.ENEMY_ROUND_START.value,
        TriggerTiming.ENEMY_ROUND_END.value,
        ActionPhase.AFTER_DAMAGE_TAKEN.value,
        ActionPhase.BEFORE_LIFE_LOST.value,
    )

    def _parse_custom_spell(self, spell) -> Optional[dict]:
        """把自创法术的文本解析为 SPELL_FLOWS 同构结构；解析失败返回None。"""
        from ..spell_dsl import parse_spell_definition, SpellDslError
        try:
            parsed = parse_spell_definition(
                spell.trigger_condition or "", spell.effect_flow or "",
                set(DaoWenEngine.list_all()))
        except SpellDslError:
            return None
        return {"trigger": parsed.trigger, "steps": parsed.steps, "loop": parsed.loop,
                "dsl": True}

    def _eligible_spell_flows(self, holder: Entity, trigger: str) -> dict[str, dict]:
        """某角色在某触发时机可发动的全部法术。

        内置法术不再需要【学习】：所需道纹全部持有且可发动即进入候选
        （见 _builtin_spell_flows）。自创法术仍读实体 spells 列表，
        同名时自创定义覆盖内置定义（自创是本体的改写，不是重复）。
        """
        flows = {}
        if holder is None or not holder.is_alive:
            return flows
        for name, flow in self._builtin_spell_flows(holder).items():
            if flow.get("trigger") == trigger:
                flows[name] = flow
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

    # ---- DSL 步骤展开辅助：ActionStep/IfStep(spell_dsl) 与旧 (daowen, role) 元组同构处理 ----

    @staticmethod
    def _step_role(step) -> str:
        """统一取出一个步骤声明的目标身份：self/attacker/target/caster/any。"""
        from ..spell_dsl import ActionStep
        if isinstance(step, ActionStep):
            return step.target
        # 旧内置 SPELL_FLOWS 用 (daowen, role) 元组，role 取值 self/attacker/target。
        return step[1]

    @staticmethod
    def _step_daowen(step) -> str:
        from ..spell_dsl import ActionStep
        if isinstance(step, ActionStep):
            return step.daowen
        return step[0]

    def _resolve_step_subject(self, role: str, holder: Entity, attacker: Entity,
                              caster: Optional[Entity] = None) -> Optional[Entity]:
        """把 self/attacker/target/caster 映射为具体实体（"any" 由调用方另行处理，
        因为它需要在结算时由发动方从候选目标中显式挑选，不能静态确定）。"""
        if role in ("self", "target"):
            return holder
        if role == "attacker":
            return attacker
        if role == "caster":
            return caster if caster is not None else holder
        return None

    def _condition_resolver(self, holder: Entity, attacker: Entity):
        """构造 spell_dsl.evaluate_condition 需要的 resolver 闭包。"""
        subjects = {"self": holder, "attacker": attacker, "target": holder, "caster": holder}

        def _resolve(subject: str, field):
            entity = subjects.get(subject)
            if entity is None:
                raise ValueError(f"条件里的主语{subject}在当前场景下不存在")
            if isinstance(field, tuple) and field[0] == "status":
                return entity.has_status(field[1])
            if isinstance(field, tuple) and field[0] == "daowen_stacks":
                inst = entity.dao_wen.get(field[1])
                return inst.x_value if inst else 0
            mapping = {
                "hp": entity.current_hp, "blood_limit": entity.blood_limit,
                "mana": entity.current_mana, "mana_limit": entity.mana_limit,
                "speed": entity.current_speed, "speed_limit": entity.speed_limit,
                "shield": entity.shield,
            }
            return mapping[field]
        return _resolve

    def _flatten_flow_steps(self, steps, holder: Entity, attacker: Entity) -> list:
        """把条件分支(IfStep)在当前局面下求值展开成一串确定性的 ActionStep 列表。

        条件分支在“列出可以怎么发动”阶段就求值展开（而不是留到执行阶段才判断），
        这样 prepare/validate/resolve 三阶段看到的步骤数量与内容完全一致，
        不会出现"校验时以为有N步，结算时条件变了变成M步"的不一致。
        对应现实语义：条件是在法术即将触发的那一刻判定的，触发之后局面
        （生命/法力/状态）在同一次 resolve 内部不会因为分支选择前置判断而变化。
        """
        from ..spell_dsl import IfStep, evaluate_condition
        resolver = self._condition_resolver(holder, attacker)
        flat = []
        for step in steps:
            if isinstance(step, IfStep):
                branch = step.then_steps if evaluate_condition(step.condition, resolver) else step.else_steps
                flat.extend(branch)
            else:
                flat.append(step)
        return flat

    @staticmethod
    def _flow_step_variants(steps) -> list[list]:
        """列出一个流程所有可能的条件展开结果，不读取当前战斗状态。

        反应法术的提交可能要在同一攻击阶段的前一个命中已经结算后才真正执行。
        如果此时再按当前法力求值，条件分支会从“短分支”漂移为“长分支”，
        于是同一份提交在静态校验时合法、在后续命中校验时却被拒绝。这个辅助
        只枚举 AST 中已经声明的分支，供一次提交冻结的分支签名复用；它不是
        重新设计条件语义，也不会在没有提交签名时替代正常的当前状态求值。
        """
        from ..spell_dsl import IfStep
        variants = [[]]
        for step in steps:
            if isinstance(step, IfStep):
                branches = []
                for branch in (step.then_steps, step.else_steps):
                    branches.extend(SpellReactionMixin._flow_step_variants(branch))
                variants = [prefix + suffix for prefix in variants for suffix in branches]
            else:
                variants = [prefix + [step] for prefix in variants]
        return variants

    def _flow_steps_signature(self, steps, holder: Entity, attacker: Entity,
                              refs: dict[str, Entity]) -> tuple:
        """把一次已展开的流程编码为不依赖实体对象的冻结签名。"""
        reverse = {id(entity): ref for ref, entity in refs.items()}
        signature = []
        for step in steps:
            role = self._step_role(step)
            target = self._resolve_step_subject(role, holder, attacker)
            signature.append((self._step_daowen(step), role, reverse.get(id(target))))
        return tuple(signature)

    def _steps_for_spell_decision(self, flow: dict, holder: Entity, attacker: Entity,
                                  refs: dict[str, Entity], decision: dict) -> list:
        """取得一次提交应使用的固定流程展开结果。

        第一次（静态）校验按触发瞬间状态求值，并把结果签名写入这份提交；
        同一次攻击后续命中/resolve_attack 的重复校验则只复用该签名，不再按
        已经被前一个命中改变的法力或生命重新选择分支。这样既保留了“触发时
        判断条件”的语义，也让 prepare → validate → resolve 使用同一份步骤。
        """
        current = self._flatten_flow_steps(flow["steps"], holder, attacker)
        frozen = decision.get("_engine_branch_signature")
        if frozen is None:
            # 仅在调用方尚未完成第一次校验时返回当前展开；调用方在通过结构
            # 校验后写入签名。
            return current
        if decision.get("_engine_branch_owner") != self._branch_owner_token:
            raise ValueError("法术提交包含未经引擎冻结的条件分支")
        expected = tuple(tuple(item) for item in frozen)
        for variant in self._flow_step_variants(flow["steps"]):
            if self._flow_steps_signature(variant, holder, attacker, refs) == expected:
                return variant
        raise ValueError("法术提交的条件分支不是该法术已冻结的有效展开")

    def _freeze_spell_decision_branch(self, flow: dict, holder: Entity, attacker: Entity,
                                     refs: dict[str, Entity], decision: dict,
                                     flat_steps: list) -> None:
        """在首次通过校验后冻结条件展开；保留同一提交的 prepare 语义。"""
        if "_engine_branch_signature" in decision:
            return
        decision["_engine_branch_owner"] = self._branch_owner_token
        decision["_engine_branch_signature"] = [
            list(item) for item in self._flow_steps_signature(flat_steps, holder, attacker, refs)
        ]

    @property
    def _branch_owner_token(self) -> str:
        """分支冻结的归属标记：字符串，随存档可序列化。

        修复（2026-09-15）：此前这里写入的是 CombatEngine 活引用，冻结字段会随
        调用方 params 进入 action_history，使 save_game 的 pickle 直接抛
        "Can't pickle local object 'all_.<locals>.cond'"（MECHANISMS 里的闭包）。
        改存归属字符串后语义不变（同一 process 内同一实例仍能通过校验），
        存档/读档不再因一次反应法术而整体失败。
        """
        token = getattr(self, "_branch_owner_token_value", None)
        if token is None:
            token = f"combat-{id(self):x}"
            self._branch_owner_token_value = token
        return token

    # 反应型法术四个挂接点：受到伤害前/失去生命后是历史已有的两个key
    # （"before"/"after"，字段名保留兼容旧调用点）；受到伤害后/失去生命前是
    # 本轮新增的两个挂接点，key为"damage_after"/"life_before"。四者共用
    # 同一套prepare/validate/resolve_spell_reactions流水线，只是触发时机
    # 不同——完全复用既有机制，不新建平行逻辑。
    _REACTION_SPELL_SLOTS = (
        ("before", ActionPhase.BEFORE_DAMAGE_TAKEN.value),
        ("after", ActionPhase.AFTER_LIFE_LOST.value),
        ("damage_after", ActionPhase.AFTER_DAMAGE_TAKEN.value),
        ("life_before", ActionPhase.BEFORE_LIFE_LOST.value),
    )

    def prepare_spell_reactions(self, holder: Entity, attacker: Entity) -> dict:
        """列出一次受击前/受击后/失血前/失血后可能触发的法术，供攻击prepare嵌入。

        条件分支（若...则...否则...）在此处已按当前局面求值展开；
        目标声明为"任意目标"(any)的步骤，在这里列出全部合法候选供发动方
        在提交时二选一，合法性判定复用 is_targetable（与"发动道纹"一致）。
        """
        refs = self._combat_entity_refs()
        reverse = {id(entity): ref for ref, entity in refs.items()}
        result = {}
        for key, trigger in self._REACTION_SPELL_SLOTS:
            result[key] = []
            for name, flow in self._eligible_spell_flows(holder, trigger).items():
                flat_steps = self._flatten_flow_steps(flow["steps"], holder, attacker)
                steps = []
                for step in flat_steps:
                    daowen = self._step_daowen(step)
                    role = self._step_role(step)
                    if role == "any":
                        candidates = [ref for ref, entity in refs.items()
                                     if self.is_targetable(holder, entity)]
                        steps.append({"daowen": daowen, "target_ref": None,
                                      "target_options": candidates,
                                      "x": "positive integer", "dodge": "boolean if hostile"})
                    else:
                        target = self._resolve_step_subject(role, holder, attacker)
                        steps.append({"daowen": daowen, "target_ref": reverse.get(id(target)),
                                      "x": "positive integer", "dodge": "boolean if hostile"})
                result[key].append({"spell_name": name, "steps": steps,
                                    "loop": bool(flow.get("loop"))})
        return result

    # 循环法术的工程安全阀：不是游戏规则上限（游戏规则=法力耗尽/流程中断才停），
    # 只是防止异常输入（例如误传几十万个cycle）在校验/结算阶段耗尽内存或卡死。
    # 任何真实法力池在此上限内必然早已耗尽（个位数到三位数消耗的道纹绝不可能
    # 循环这么多次），因此正常游戏流程永远不会触达这个值。
    MAX_SPELL_LOOP_CYCLES = 10_000

    # 具名死因：这些 mechanic 的 ctx 虽不是 mechanic="death"，但同样是调用方
    # **显式**给出的死因，必须原样留在 _death_ctx 里，而不是被兜底成 "hp_zero"。
    # （【癌变】即因此被吞掉：预演/事件里只能看到 hp_zero，看不出是癌变。）
    NAMED_DEATH_MECHANICS = {
        "cancer": "cancer",
        "proliferation": "cancer",
    }

    def _resolve_entry_target(self, step, entry, holder: Entity, attacker: Entity,
                              refs: dict[str, Entity], reverse: dict[int, str]):
        """按步骤声明的目标身份，从提交里取出/校验实际目标实体，返回(entity, ref)。

        role == "any" 时目标由提交方在 entry["target_ref"] 里显式指定，
        合法性复用 is_targetable（与"发动道纹"选择目标同一套规则）。
        """
        role = self._step_role(step)
        if role == "any":
            target_ref = entry.get("target_ref")
            target = refs.get(target_ref)
            if target is None:
                raise ValueError("法术步骤的任意目标target_ref不是当前合法实体")
            if not self.is_targetable(holder, target):
                raise ValueError(f"{target.name}处于飞行，无法被选中为法术目标")
            return target, target_ref
        target = self._resolve_step_subject(role, holder, attacker)
        return target, reverse.get(id(target))

    def validate_spell_reaction_submission(self, holder: Entity, attacker: Entity,
                                           submitted: Any, refs: dict[str, Entity],
                                           extra_mana: int = 0) -> None:
        """校验受击方反应法术提交。

        extra_mana：静态校验阶段可预付的额外法力预算（当前恒为 0；
        守夜灯改为[回始]授予后已无需预付）。
        """
        if not isinstance(submitted, dict):
            raise ValueError("每次攻击必须显式提交spell_choices对象")
        reverse = {id(entity): ref for ref, entity in refs.items()}
        for key, trigger in self._REACTION_SPELL_SLOTS:
            eligible = self._eligible_spell_flows(holder, trigger)
            choices = submitted.get(key)
            # 受到伤害后/失去生命前是本轮新增挂接点：为兼容大量既有调用点
            # 只提交{"before":..., "after":...}两个历史key，当该新挂接点
            # 确实没有候选法术时，缺省key按"无候选"处理，不强制要求提交，
            # 与全局法术"存在候选才必须显式提交"的契约一致；一旦真的存在
            # 候选，仍然必须显式覆盖，不允许静默跳过。
            if choices is None and not eligible and key in ("damage_after", "life_before"):
                continue
            if not isinstance(choices, dict) or set(choices) != set(eligible):
                raise ValueError(f"spell_choices.{key}必须逐一覆盖{sorted(eligible)}")
            mana = holder.current_mana + max(0, extra_mana)
            speed_budget = {ref: entity.current_speed for ref, entity in refs.items()}
            for spell_name, flow in eligible.items():
                decision = choices[spell_name]
                if not isinstance(decision, dict) or not isinstance(decision.get("use"), bool):
                    raise ValueError(f"法术{spell_name}必须显式提交use布尔值")
                if not decision["use"]:
                    continue
                flat_steps = self._steps_for_spell_decision(
                    flow, holder, attacker, refs, decision,
                )
                cycles = decision.get("cycles")
                if not isinstance(cycles, list) or not cycles:
                    raise ValueError(f"法术{spell_name}发动时必须提交至少一个cycles")
                if not flow.get("loop") and len(cycles) != 1:
                    raise ValueError(f"法术{spell_name}不是循环法术，只能提交一个cycle")
                if len(cycles) > self.MAX_SPELL_LOOP_CYCLES:
                    raise ValueError(f"法术{spell_name}提交的循环次数超过工程安全上限")
                for cycle in cycles:
                    if not isinstance(cycle, list) or len(cycle) != len(flat_steps):
                        raise ValueError(f"法术{spell_name}每个cycle必须完整提交{len(flat_steps)}步")
                    for entry, step in zip(cycle, flat_steps):
                        if not isinstance(entry, dict):
                            raise ValueError("法术步骤必须是对象")
                        x = entry.get("x")
                        daowen = self._step_daowen(step)
                        expected_target, expected_ref = self._resolve_entry_target(
                            step, entry, holder, attacker, refs, reverse)
                        if (not isinstance(x, int) or isinstance(x, bool) or x < 1
                                or entry.get("target_ref") != expected_ref):
                            raise ValueError(f"法术{spell_name}步骤必须提交合法x与target_ref")
                        calc = DaoWenEngine.resolve(daowen, x, target=expected_target, caster=holder)
                        if calc.get("cost_type") == "消耗":
                            mana -= calc.get("cost", 0)
                            if mana < 0:
                                raise ValueError(f"法术{spell_name}提交的法力不足")
                        # 2026-09-12：校验必须与结算(_apply_daowen_result)口径一致地
                        # 计入产法力道纹的收益。此前只记消耗不记产出，导致【透支】等
                        # "流血换法力"道纹在循环法术里被当成纯支出：一个法力净零的
                        # 自持循环(透支X→再生X)反而要求预付 cost*循环次数 的法力，
                        # 等于把"靠循环自己造法力"这一设计意图判死。产出在步骤结算后
                        # 到账，故按步序累加，后续步骤即可支用前面步骤产出的法力。
                        if "mana_gain" in calc:
                            mana += calc["mana_gain"]
                        hostile = self.state.on_player_side(holder) != self.state.on_player_side(expected_target)
                        if hostile:
                            if not isinstance(entry.get("dodge"), bool):
                                raise ValueError("敌对法术步骤必须显式提交dodge")
                            if entry["dodge"]:
                                speed_budget[expected_ref] -= 1
                                if speed_budget[expected_ref] < 0:
                                    raise ValueError("法术目标速度不足以闪避")
                # 这一份 decision 会在同一怪物阶段的后续命中中再次校验；
                # 条件分支必须锁定在第一次（触发瞬间）看到的状态。
                self._freeze_spell_decision_branch(
                    flow, holder, attacker, refs, decision, flat_steps,
                )

    def _trigger_spell_subject(self, role: str, holder: Entity, actor: Entity) -> Optional[str]:
        """「目标发动道纹前」触发语境下的身份映射：

        本触发点人话语义是"当[actor]即将发动道纹时，[holder]的反应法术触发"。
        这里 actor 相当于其它触发点里的"attacker"（对 holder 而言的外部行动方），
        因此 attacker/target 两种写法都指向 actor（沿用【咎由自取】原本"target"
        写法的含义），self/caster 指向 holder 自己。
        """
        if role in ("attacker", "target"):
            return "actor"
        if role in ("self", "caster"):
            return "holder"
        if role == "any":
            return "any"
        return None

    def prepare_daowen_trigger_spells(self, actor: Entity) -> dict:
        refs = self._combat_entity_refs()
        reverse = {id(entity): ref for ref, entity in refs.items()}
        actor_ref = reverse.get(id(actor))
        result = {}
        for ref, holder in refs.items():
            if self.state.on_player_side(holder) == self.state.on_player_side(actor):
                continue
            flows = self._eligible_spell_flows(holder, "目标发动道纹前")
            if not flows:
                continue
            entries = []
            for name, flow in flows.items():
                flat_steps = self._flatten_flow_steps(flow["steps"], holder, actor)
                steps = []
                for step in flat_steps:
                    daowen = self._step_daowen(step)
                    subject = self._trigger_spell_subject(self._step_role(step), holder, actor)
                    if subject == "any":
                        candidates = [r for r, e in refs.items() if self.is_targetable(holder, e)]
                        steps.append({"daowen": daowen, "target_ref": None,
                                      "target_options": candidates,
                                      "x": "positive integer", "dodge": "boolean"})
                    else:
                        target_ref = actor_ref if subject == "actor" else reverse.get(id(holder))
                        steps.append({"daowen": daowen, "target_ref": target_ref,
                                      "x": "positive integer", "dodge": "boolean"})
                entries.append({"spell_name": name, "steps": steps, "loop": bool(flow.get("loop"))})
            result[ref] = entries
        return result

    def validate_daowen_trigger_spells(self, actor: Entity, submitted: Any,
                                       refs: dict[str, Entity],
                                       extra_mana: int = 0) -> None:
        """校验「目标发动道纹前」反应法术（如咎由自取）。

        extra_mana：静态校验阶段可预付的额外法力预算（当前恒为 0）。
        """
        expected = self.prepare_daowen_trigger_spells(actor)
        if not isinstance(submitted, dict) or set(submitted) != set(expected):
            raise ValueError(f"trigger_spell_choices必须覆盖{sorted(expected)}")
        actor_ref = next((ref for ref, entity in refs.items() if entity is actor), None)
        reverse = {id(entity): ref for ref, entity in refs.items()}
        for holder_ref in expected:
            holder = refs[holder_ref]
            flows = self._eligible_spell_flows(holder, "目标发动道纹前")
            choices = submitted[holder_ref]
            if not isinstance(choices, dict) or set(choices) != set(flows):
                raise ValueError("目标发动道纹前的法术提交不完整")
            mana = holder.current_mana + max(0, extra_mana)
            for spell_name, flow in flows.items():
                decision = choices[spell_name]
                if not isinstance(decision, dict) or not isinstance(decision.get("use"), bool):
                    raise ValueError(f"法术{spell_name}必须显式提交use")
                if not decision["use"]:
                    continue
                flat_steps = self._flatten_flow_steps(flow["steps"], holder, actor)
                steps = decision.get("steps")
                if not isinstance(steps, list) or len(steps) != len(flat_steps):
                    raise ValueError(f"法术{spell_name}必须完整提交steps")
                for entry, step in zip(steps, flat_steps):
                    daowen = self._step_daowen(step)
                    subject = self._trigger_spell_subject(self._step_role(step), holder, actor)
                    if subject == "any":
                        target_ref = entry.get("target_ref") if isinstance(entry, dict) else None
                        target = refs.get(target_ref)
                        if target is None or not self.is_targetable(holder, target):
                            raise ValueError(f"法术{spell_name}的任意目标target_ref非法")
                    else:
                        expected_ref = actor_ref if subject == "actor" else reverse.get(id(holder))
                        target = actor if subject == "actor" else holder
                        x_check_ref = entry.get("target_ref") if isinstance(entry, dict) else None
                        if x_check_ref != expected_ref:
                            raise ValueError(f"法术{spell_name}步骤的target_ref非法")
                    x = entry.get("x") if isinstance(entry, dict) else None
                    if (not isinstance(x, int) or isinstance(x, bool) or x < 1
                            or not isinstance(entry.get("dodge"), bool)):
                        raise ValueError("法术步骤的x/target_ref/dodge非法")
                    calc = DaoWenEngine.resolve(daowen, x, target=target, caster=holder)
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
                flat_steps = self._flatten_flow_steps(flow["steps"], holder, actor)
                previous_damage = 0
                for entry, step in zip(decision["steps"], flat_steps):
                    daowen = self._step_daowen(step)
                    subject = self._trigger_spell_subject(self._step_role(step), holder, actor)
                    if subject == "any":
                        target = refs.get(entry.get("target_ref"))
                    else:
                        target = actor if subject == "actor" else holder
                    if daowen == "坠落" and not (target.is_flying or target.has_status("飞行") or target.has_status("滑翔")):
                        continue
                    if daowen == "血债" and previous_damage > 0:
                        continue
                    calc = DaoWenEngine.resolve(daowen, entry["x"], target=target, caster=holder)
                    if calc.get("cost_type") == "消耗":
                        cost = calc.get("cost", 0)
                        if not holder.spend_mana(cost):
                            raise ValueError("法术结算法力不足")
                        self.note_mana_inflicted(holder, target, cost)
                    if entry["dodge"]:
                        if target.current_speed < 1:
                            raise ValueError("道纹行动者速度不足以闪避反应法术")
                        self._spend_dodge_speed(target, entry.get("dodge_relic_target_ref"))
                        logs.append({"spell": spell_name, "daowen": daowen, "dodged": True})
                        previous_damage = 0
                        continue
                    execution = self.apply_daowen_effect(daowen, calc, holder, target)
                    previous_damage = sum(effect.get("actual_damage", 0) for effect in execution.get("effects", []))
                    logs.append({"spell": spell_name, "daowen": daowen, "execution": execution})
        return logs

    # ---- 全局时点法术（战始/战终/回始/回终/敌回始/敌回终）----
    # 这六个时点没有"攻击者/目标"这个天然对手身份（不像受到伤害前/失去生命后
    # 那样由一次攻击自带触发对象），因此法术效果步骤在 DSL 层已经被限定为
    # 只能声明 self/caster/any（见 spell_dsl._check_global_trigger_targets）。
    # 结算流程与既有反应法术（prepare/validate/resolve_spell_reactions）同构，
    # 只是没有 attacker 参数、且遍历对象是"当前场上全部可能持有法术的实体"
    # （玩家/朋友/员工/临时朋友/死斗对手），而不是单一受击者。

    def _global_trigger_holders(self, refs: dict[str, Entity]) -> dict[str, Entity]:
        """当前场上可能持有【战始/战终/回始/回终/敌回始/敌回终】法术的持有者。

        内置法术按道纹推导，因此不能只看 entity.spells 是否为空——
        持有【封印】的角色即使 spells 为空也持有【镇魔印】（自身回合结束）。
        怪物通常既不持 spells 也不持有内置法术所需道纹，扫描开销仍可忽略；
        死斗对手若通过完整封存快照持有自创法术，同样会被正确扫描到
        （不局限于玩家侧）。
        """
        return {ref: entity for ref, entity in refs.items()
                if entity.is_alive
                and (entity.spells or self._builtin_spell_flows(entity))}

    def _resolve_global_entry_target(self, step, entry, holder: Entity,
                                     refs: dict[str, Entity], reverse: dict[int, str]):
        """全局时点专用的目标解析：role 只能是 self/caster/any（DSL 层已保证）。"""
        role = self._step_role(step)
        if role == "any":
            target_ref = entry.get("target_ref") if isinstance(entry, dict) else None
            target = refs.get(target_ref)
            if target is None:
                raise ValueError("法术步骤的任意目标target_ref不是当前合法实体")
            if not self.is_targetable(holder, target):
                raise ValueError(f"{target.name}处于飞行，无法被选中为法术目标")
            return target, target_ref
        # self/caster 在全局时点里都指向法术持有者自己。
        return holder, reverse.get(id(holder))

    def prepare_global_trigger_spells(self, trigger: str) -> dict:
        """列出当前时点全部持有者的可发动全局法术，供 battle_start/battle_end/
        round_start/round_end 的 params_schema 嵌入 spell_choices 供决策方提交。
        """
        refs = self._combat_entity_refs()
        reverse = {id(entity): ref for ref, entity in refs.items()}
        result: dict[str, list[dict]] = {}
        for holder_ref, holder in self._global_trigger_holders(refs).items():
            flows = self._eligible_spell_flows(holder, trigger)
            if not flows:
                continue
            entries = []
            for name, flow in flows.items():
                flat_steps = self._flatten_flow_steps(flow["steps"], holder, holder)
                steps = []
                spell = self.spell_definition(holder, name)
                for step in flat_steps:
                    daowen = self._step_daowen(step)
                    role = self._step_role(step)
                    if role == "any":
                        candidates = [ref for ref, entity in refs.items()
                                     if self.is_targetable(holder, entity)]
                        steps.append({"daowen": daowen, "target_ref": None,
                                      "target_options": candidates,
                                      "x": "positive integer", "dodge": "boolean if hostile"})
                    else:
                        steps.append({"daowen": daowen, "target_ref": reverse.get(id(holder)),
                                      "x": "positive integer", "dodge": "boolean if hostile"})
                entries.append({"spell_name": name, "steps": steps, "loop": bool(flow.get("loop")),
                                # “自身回合结束”本身就是无主动选择的触发时点；
                                # automatic 字段保留给未来其它自动法术。
                                "automatic": bool(getattr(spell, "automatic", False)
                                                   or flow.get("trigger") == "自身回合结束")})
            result[holder_ref] = entries
        return result

    def is_automatic_spell(self, holder_ref: str, spell_name: str) -> bool:
        """查询一个全局法术是否由引擎自动提交。"""
        refs = self._combat_entity_refs()
        holder = refs.get(holder_ref)
        if holder is None:
            return False
        spell = self.spell_definition(holder, spell_name)
        if spell is None:
            return False
        if getattr(spell, "automatic", False):
            return True
        flow = self.SPELL_FLOWS.get(spell.name)
        if flow is None:
            flow = self._parse_custom_spell(spell)
        return bool(flow and flow.get("trigger") == "自身回合结束")

    def has_manual_global_trigger_spells(self, trigger: str) -> bool:
        """当前时点是否还存在需要外部显式提交的全局法术。"""
        expected = self.prepare_global_trigger_spells(trigger)
        return any(not entry.get("automatic", False)
                   for entries in expected.values() for entry in entries)

    def automatic_global_trigger_choices(self, trigger: str) -> dict:
        """为“自身回合结束”自动法术构造真实引擎提交。

        自动法术仍必须先被学习并挂在持有者的 ``spells`` 上；这里只负责
        在真实触发点为其提交流程参数。当前以 X=1 选择第一只合法敌对怪物，
        没有可选目标时提交 use=false。结算仍走
        validate/resolve_global_trigger_spells，因而异变支付、目标合法性和
        暂离队列都不是旁路注入。
        """
        refs = self._combat_entity_refs()
        expected = self.prepare_global_trigger_spells(trigger)
        submitted: dict[str, dict] = {}
        for holder_ref, entries in expected.items():
            holder = refs[holder_ref]
            submitted[holder_ref] = {}
            for entry in entries:
                name = entry["spell_name"]
                if not entry.get("automatic", False):
                    submitted[holder_ref][name] = {"use": False}
                    continue
                flow = self._eligible_spell_flows(holder, trigger)[name]
                flat_steps = self._flatten_flow_steps(flow["steps"], holder, holder)
                cycle = []
                valid = True
                for step in flat_steps:
                    role = self._step_role(step)
                    if role == "any":
                        # 【封印】的 DSL 目标是 any，但它的道纹结算还会
                        # 检查“必须是怪物”；这里提前选择当前敌方怪物，
                        # 不把轮回者/队友放进自动目标。
                        candidates = [
                            ref for ref, entity in refs.items()
                            if entity.is_alive
                            and entity.entity_type == "怪物"
                            and self.state.on_player_side(entity) != self.state.on_player_side(holder)
                            and self.is_targetable(holder, entity)
                        ]
                        target_ref = candidates[0] if candidates else None
                        if target_ref is None:
                            valid = False
                            break
                    else:
                        target_ref = next((ref for ref, entity in refs.items()
                                           if entity is holder), None)
                    cycle.append({"x": 1, "target_ref": target_ref, "dodge": False})
                submitted[holder_ref][name] = (
                    {"use": True, "cycles": [cycle]}
                    if valid and cycle else {"use": False}
                )
        return submitted

    def validate_global_trigger_spells(self, trigger: str, submitted: Any,
                                       refs: dict[str, Entity]) -> None:
        """校验全局时点法术提交；submitted 结构与 spell_choices 的单个时机同构：
        {holder_ref: {spell_name: {use, cycles: [[{x, target_ref, dodge}, ...], ...]}}}
        """
        expected = self.prepare_global_trigger_spells(trigger)
        if not isinstance(submitted, dict) or set(submitted) != set(expected):
            raise ValueError(f"【{trigger}】的spell_choices必须逐一覆盖{sorted(expected)}")
        reverse = {id(entity): ref for ref, entity in refs.items()}
        for holder_ref in expected:
            holder = refs[holder_ref]
            flows = self._eligible_spell_flows(holder, trigger)
            choices = submitted[holder_ref]
            if not isinstance(choices, dict) or set(choices) != set(flows):
                raise ValueError(f"【{trigger}】{holder.name}的法术提交必须逐一覆盖{sorted(flows)}")
            mana = holder.current_mana
            speed_budget = {ref: entity.current_speed for ref, entity in refs.items()}
            for spell_name, flow in flows.items():
                decision = choices[spell_name]
                if not isinstance(decision, dict) or not isinstance(decision.get("use"), bool):
                    raise ValueError(f"法术{spell_name}必须显式提交use布尔值")
                if not decision["use"]:
                    continue
                flat_steps = self._flatten_flow_steps(flow["steps"], holder, holder)
                cycles = decision.get("cycles")
                if not isinstance(cycles, list) or not cycles:
                    raise ValueError(f"法术{spell_name}发动时必须提交至少一个cycles")
                if not flow.get("loop") and len(cycles) != 1:
                    raise ValueError(f"法术{spell_name}不是循环法术，只能提交一个cycle")
                if len(cycles) > self.MAX_SPELL_LOOP_CYCLES:
                    raise ValueError(f"法术{spell_name}提交的循环次数超过工程安全上限")
                for cycle in cycles:
                    if not isinstance(cycle, list) or len(cycle) != len(flat_steps):
                        raise ValueError(f"法术{spell_name}每个cycle必须完整提交{len(flat_steps)}步")
                    for entry, step in zip(cycle, flat_steps):
                        if not isinstance(entry, dict):
                            raise ValueError("法术步骤必须是对象")
                        x = entry.get("x")
                        daowen = self._step_daowen(step)
                        expected_target, expected_ref = self._resolve_global_entry_target(
                            step, entry, holder, refs, reverse)
                        if (not isinstance(x, int) or isinstance(x, bool) or x < 1
                                or entry.get("target_ref") != expected_ref):
                            raise ValueError(f"法术{spell_name}步骤必须提交合法x与target_ref")
                        calc = DaoWenEngine.resolve(daowen, x, target=expected_target, caster=holder)
                        if calc.get("cost_type") == "消耗":
                            mana -= calc.get("cost", 0)
                            if mana < 0:
                                raise ValueError(f"法术{spell_name}提交的法力不足")
                        # 2026-09-12：校验必须与结算(_apply_daowen_result)口径一致地
                        # 计入产法力道纹的收益。此前只记消耗不记产出，导致【透支】等
                        # "流血换法力"道纹在循环法术里被当成纯支出：一个法力净零的
                        # 自持循环(透支X→再生X)反而要求预付 cost*循环次数 的法力，
                        # 等于把"靠循环自己造法力"这一设计意图判死。产出在步骤结算后
                        # 到账，故按步序累加，后续步骤即可支用前面步骤产出的法力。
                        if "mana_gain" in calc:
                            mana += calc["mana_gain"]
                        hostile = self.state.on_player_side(holder) != self.state.on_player_side(expected_target)
                        if hostile:
                            if not isinstance(entry.get("dodge"), bool):
                                raise ValueError("敌对法术步骤必须显式提交dodge")
                            if entry["dodge"]:
                                speed_budget[expected_ref] -= 1
                                if speed_budget[expected_ref] < 0:
                                    raise ValueError("法术目标速度不足以闪避")

    def resolve_global_trigger_spells(self, trigger: str, submitted: dict,
                                      refs: dict[str, Entity]) -> list[dict]:
        """结算全局时点法术；调用前必须先通过 validate_global_trigger_spells。"""
        reverse = {id(entity): ref for ref, entity in refs.items()}
        logs = []
        for holder_ref, choices in submitted.items():
            holder = refs.get(holder_ref)
            if holder is None or not holder.is_alive:
                continue
            flows = self._eligible_spell_flows(holder, trigger)
            for spell_name, flow in flows.items():
                decision = choices.get(spell_name)
                if not decision or not decision.get("use"):
                    logs.append({"spell": spell_name, "holder": holder.name, "used": False})
                    continue
                flat_steps = self._flatten_flow_steps(flow["steps"], holder, holder)
                for cycle_index, cycle in enumerate(decision["cycles"], 1):
                    for entry, step in zip(cycle, flat_steps):
                        daowen = self._step_daowen(step)
                        target, _ = self._resolve_global_entry_target(step, entry, holder, refs, reverse)
                        if target is None or not target.is_alive:
                            logs.append({"spell": spell_name, "holder": holder.name, "cycle": cycle_index,
                                        "daowen": daowen, "skipped": "目标已失效"})
                            continue
                        x = entry["x"]
                        calc = DaoWenEngine.resolve(daowen, x, target=target, caster=holder)
                        if calc.get("cost_type") == "消耗":
                            cost = calc.get("cost", 0)
                            if not holder.spend_mana(cost):
                                raise ValueError(f"法术{spell_name}结算时法力不足")
                            self.note_mana_inflicted(holder, target, cost)
                        hostile = self.state.on_player_side(holder) != self.state.on_player_side(target)
                        if hostile and entry.get("dodge"):
                            self._spend_dodge_speed(target, entry.get("dodge_relic_target_ref"))
                            logs.append({"spell": spell_name, "holder": holder.name, "cycle": cycle_index,
                                        "daowen": daowen, "target": target.name, "dodged": True})
                            continue
                        execution = self.apply_daowen_effect(daowen, calc, holder, target)
                        logs.append({"spell": spell_name, "holder": holder.name, "cycle": cycle_index,
                                    "daowen": daowen, "x": x, "target": target.name, "execution": execution})
        return logs

    def _max_auto_life_lost_x(self, daowen: str, target: Entity, caster: Entity,
                              budget: int) -> Optional[int]:
        """自动装配「失去生命后」反应法术时，为单步挑选一个可支付的 X。

        代价型道纹（杀死/再生/庇护等）消耗法力，X 越大效果越强；这里从预算上限
        向下取最大可支付 X（至少 1），使触发真正生效而非空转。非法力代价
        （血债=流血等）先付血，与法力无关，直接取 1（最小代价、真实触发）。
        """
        upper = max(1, budget)
        # 遍历上限：防御性封顶，避免极端预算下做无谓的 O(budget) 扫描。
        upper = min(upper, 10_000)
        for x in range(upper, 0, -1):
            calc = DaoWenEngine.resolve(daowen, x, target=target, caster=caster)
            if calc.get("cost_type") != "消耗":
                return 1
            if calc.get("cost", 0) <= budget:
                return x
        return None

    def _dodge_budget_reset(self) -> None:
        """换回合则清空自动反应路径的闪避计数（每回合最多 2 次，与 choose_dodge 同口径）。"""
        rnd = getattr(self.state, "current_round", None)
        if getattr(self, "_dodge_round", None) != rnd:
            self._dodge_round = rnd
            self._dodge_counts = {}

    def _auto_reaction_dodge_decision(self, daowen: str, calc: dict,
                                      holder: Entity, target: Entity) -> bool:
        """自动反应法术路径：被选定方是否消耗 1 点速度闪避本次道纹。

        DM 裁定（2026-08-31）：法术说到底只是自定义了触发条件的道纹，
        **道纹要遵守的规则，法术一样要遵守**。规则正文「凡带 [目标] 道纹，
        目标被选定时均可消耗 1 点当前速度进行闪避」、规则正文「禁止跳过闪避判定」。

        原先 `_auto_after_life_lost_decision` 把 dodge 写死 False，导致**道纹伤害**
        （区别于基础攻击的显式反应窗口）触发的反应法术不给目标任何声明机会——
        例如【先发制人】的杀伐反打变成无法闪避的必杀。本方法把它换成统一闪避策略。
        """
        if holder is None or target is None or not target.is_alive:
            return False
        # 非敌对步骤（自身增益 / 队友）不走闪避
        if self.state.on_player_side(holder) == self.state.on_player_side(target):
            return False
        # 无[目标]伤害的道纹（自身增益、纯控制等）不可闪避
        dmg = calc.get("target_damage") or calc.get("total_damage") or 0
        if not dmg:
            return False
        # 必中：无法闪避。此处**只读不消耗**——自动路径不替施法方花掉必中余数
        # （显式攻击路径的 consume_bizhong 才负责消耗），避免改变既有必中结算。
        if self.bizhong_remaining(holder) > 0:
            return False
        # 速度不足以支付闪避
        if target.current_speed < 1:
            return False
        # 飞行：非飞行者无法选中飞行目标（与显式路径同口径）
        if not self.is_targetable(holder, target):
            return False
        self._dodge_budget_reset()
        used = self._dodge_counts.get(id(target), 0)
        try:
            from engine.ai_tactics import choose_dodge
            want = bool(choose_dodge(None, int(dmg), budget_used=used, entity=target))
        except Exception:
            return False
        if want:
            self._dodge_counts[id(target)] = used + 1
        return want

    def _auto_after_life_lost_decision(self, name: str, flow: dict, holder: Entity,
                                       attacker: Optional[Entity], refs: dict[str, Entity],
                                       reverse: dict[int, str], budget: Optional[int] = None) -> dict:
        """为一次非攻击失血自动生成「失去生命后」的单法术提交决策。

        没有 AI 决策窗口，因此按“可支付且效果方向合理”自动装配：
          - self/target 步骤命中持有者自身；attacker 步骤命中失血来源（有对位实体才结算），
            无对位实体则该步跳过（不会因此使整个法术不触发）。
          - step 目标为 any（任意目标）时无法静态定目标 → 本法术放弃自动触发。
          - 任一法力步骤付不起（X=1 都超出预算）→ 本法术放弃自动触发。
          - budget 为“本次共用法力预算”（跨同一次失血的多个反应法术共享），传入后
            依剩余预算选 X，避免多个法术各自吃满预算导致逐个结算时法力不足而崩溃；
            _cost 反馈该法术实际消耗的法力，供上层扣减共享预算。
        """
        flow_target = attacker if attacker is not None else holder
        flat_steps = self._flatten_flow_steps(flow["steps"], holder, flow_target)
        budget = holder.current_mana if budget is None else budget
        cycle = []
        consumed = 0
        for step in flat_steps:
            daowen = self._step_daowen(step)
            role = self._step_role(step)
            if role == "any":
                return {"use": False}
            target = self._resolve_step_subject(role, holder, attacker)
            if role == "attacker":
                # 没有可对位的失血来源（如自伤/代价/legacy），跳过该反击步，不判失败。
                if target is None or not target.is_alive or target is holder:
                    continue
            elif target is None or not target.is_alive:
                continue
            x = self._max_auto_life_lost_x(daowen, target, holder, budget)
            if x is None:
                return {"use": False}
            calc = DaoWenEngine.resolve(daowen, x, target=target, caster=holder)
            if calc.get("cost_type") == "消耗":
                cost = calc.get("cost", 0)
                if cost > budget:
                    return {"use": False}
                budget -= cost
                consumed += cost
            cycle.append({"x": x, "target_ref": reverse.get(id(target)),
                          "dodge": self._auto_reaction_dodge_decision(
                              daowen, calc, holder, target)})
        if not cycle:
            return {"use": False}
        return {"use": True, "cycles": [cycle], "_cost": consumed}

    def _fire_auto_reaction(self, holder: Entity, trigger: str,
                            loss_ctx: Optional[EffectContext | dict]) -> list[dict]:
        """非攻击路径 → 自动触发任意反应型时点（受到伤害前/后、失去生命前/后）。

        攻击路径（resolve_attack）由显式反应窗口按 AI 提交结算，不经过本方法；
        这里处理其余一切导致"伤害/失血"的通道（道纹伤害/流血代价/血限压迫/
        爆裂反射/赌命/直接失血/未来新增效果……），对持有者而言"触发时机一到就
        触发"——不关心具体成因，完全满足"只检测事件、不逐个开窗"的需求。
        """
        if holder is None or not holder.is_alive:
            return []
        # 攻击失血由 resolve_attack 的反应窗口结算，本 hook 不重复触发；
        # 反应法术自身的结算会置 _resolving_life_lost_reactions>0，避免连锁死循环。
        if self._attack_after_window_target is holder or self._resolving_life_lost_reactions > 0:
            return []
        eligible = self._eligible_spell_flows(holder, trigger)
        if not eligible:
            return []
        refs = self._combat_entity_refs()
        reverse = {id(entity): ref for ref, entity in refs.items()}
        parent = normalize_context(loss_ctx) if loss_ctx is not None else None
        attacker: Optional[Entity] = None
        if parent is not None and parent.actor is not None and parent.actor is not holder:
            if any(parent.actor is e for e in refs.values()):
                attacker = parent.actor
        after: dict[str, dict] = {}
        # 共享法力预算：同一次失血可能同时有多个反应法术可触发（如“受到伤害前”同时
        # 装备先发制人/后发制人/借力打力），各自吃满预算会逐个结算时法力不足而崩溃。
        # 这里按“挨个触发、扣减剩余预算”的顺序装配，保证每个都被正确结算而不是半途报错。
        shared_budget = holder.current_mana
        for name, flow in eligible.items():
            dec = self._auto_after_life_lost_decision(
                name, flow, holder, attacker, refs, reverse, shared_budget)
            after[name] = dec
            if dec.get("use"):
                shared_budget = max(0, shared_budget - dec.get("_cost", 0))
        return self._resolve_spell_reactions(trigger, holder, attacker, after, refs)

    def _fire_after_life_lost(self, holder: Entity,
                              loss_ctx: Optional[EffectContext | dict]) -> list[dict]:
        """非攻击失血 → 触发「失去生命后」(AFTER_LIFE_LOST) 反应法术。"""
        return self._fire_auto_reaction(holder, ActionPhase.AFTER_LIFE_LOST.value, loss_ctx)

    def _fire_before_life_lost(self, holder: Entity,
                               loss_ctx: Optional[EffectContext | dict]) -> list[dict]:
        """生命即将下降 → 触发「失去生命前」(BEFORE_LIFE_LOST) 反应法术。

        与 AFTER_LIFE_LOST 语义区分：本窗口在生命真正扣减之前触发，攻击路径由
        resolve_attack 的显式窗口结算，此处只服务道纹伤害/流血代价/直接失血/
        血限压迫等非攻击生命下降。无符合条件法术时返回空列表，是纯 no-op。
        """
        return self._fire_auto_reaction(holder, ActionPhase.BEFORE_LIFE_LOST.value, loss_ctx)

    def _resolve_spell_reactions(self, trigger: str, holder: Entity, attacker: Entity,
                                 submitted: dict, refs: dict[str, Entity]) -> list[dict]:
        self._resolving_life_lost_reactions += 1
        try:
            reverse = {id(entity): ref for ref, entity in refs.items()}
            flows = self._eligible_spell_flows(holder, trigger)
            logs = []
            for spell_name, flow in flows.items():
                decision = submitted[spell_name]
                if not decision["use"]:
                    logs.append({"spell": spell_name, "used": False})
                    continue
                flat_steps = self._steps_for_spell_decision(
                    flow, holder, attacker, refs, decision,
                )
                for cycle_index, cycle in enumerate(decision["cycles"], 1):
                    for entry, step in zip(cycle, flat_steps):
                        daowen = self._step_daowen(step)
                        target, _ = self._resolve_entry_target(step, entry, holder, attacker, refs, reverse)
                        if target is None or not target.is_alive:
                            logs.append({"spell": spell_name, "cycle": cycle_index,
                                         "daowen": daowen, "skipped": "目标已失效"})
                            continue
                        x = entry["x"]
                        calc = DaoWenEngine.resolve(daowen, x, target=target, caster=holder)
                        if calc.get("cost_type") == "消耗":
                            cost = calc.get("cost", 0)
                            if not holder.spend_mana(cost):
                                raise ValueError(f"法术{spell_name}结算时法力不足")
                            self.note_mana_inflicted(holder, target, cost)
                        hostile = self.state.on_player_side(holder) != self.state.on_player_side(target)
                        if hostile and entry.get("dodge"):
                            self._spend_dodge_speed(target, entry.get("dodge_relic_target_ref"))
                            logs.append({"spell": spell_name, "cycle": cycle_index,
                                         "daowen": daowen, "target": target.name, "dodged": True})
                            continue
                        execution = self.apply_daowen_effect(daowen, calc, holder, target)
                        logs.append({"spell": spell_name, "cycle": cycle_index, "daowen": daowen,
                                     "x": x, "target": target.name, "execution": execution})
            return logs
        finally:
            self._resolving_life_lost_reactions -= 1
