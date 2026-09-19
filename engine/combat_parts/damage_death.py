"""
伤害与死亡结算：伤害上下文、生命损失记录、承露、失去生命后拦截、血限变化、敌对伤害、死亡与分裂克隆

从 engine/combat.py 原样抽出（方法体一字未改），由 CombatEngine 以 Mixin 方式继承：
对外仍是 `engine.combat.CombatEngine`，调用方与 API 契约均不受影响。
本模块的方法只能通过 CombatEngine 实例使用（依赖 self.state / self._emit 等引擎成员）。
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
# 常量定义在 models（授予点在 Entity.__post_init__），此处只读取以判定效果。
from ..models import MONSTER_MANA_RELIC


class DamageDeathMixin:
    """伤害与死亡结算：伤害上下文、生命损失记录、承露、失去生命后拦截、血限变化、敌对伤害、死亡与分裂克隆（CombatEngine 的组成部分）"""

    @staticmethod
    def _damage_context_subtype(damage_type: str) -> str:
        return {
            DamageType.NORMAL.value: "normal",
            DamageType.IGNORE_SHIELD.value: "ignore_shield",
            DamageType.IGNORE_DODGE.value: "ignore_dodge",
            DamageType.MUST_HIT.value: "must_hit",
            DamageType.REFLECT.value: "reflect",
            DamageType.COST.value: "cost",
            "无视格挡": "ignore_shield",
            "无视闪避": "ignore_dodge",
            "必中": "must_hit",
            "普通": "normal",
            "代价": "cost",
        }.get(damage_type, damage_type or "normal")

    def _damage_context(
        self, target: Entity, amount: int, damage_type: str,
        source: Optional[Entity], ctx: Optional[EffectContext | dict],
    ) -> tuple[EffectContext, bool]:
        normalized = normalize_context(ctx)
        if normalized is None:
            # 兼容旧调用：生成可追踪上下文并显式标记 legacy，避免后续迁移时静默漏源。
            return make_context(
                timing=self._current_context_timing(),
                source=getattr(source, "name", "legacy_damage"),
                source_type="legacy",
                actor=source, target=target,
                mechanic="damage", subtype=self._damage_context_subtype(damage_type),
                amount=amount, tags={"legacy_context"},
            ), True
        if normalized.mechanic == "damage":
            return normalized, False
        return make_context(
            timing=normalized.timing, source=normalized.source,
            source_type=normalized.source_type, actor=normalized.actor or source,
            target=normalized.target or target, owner=normalized.owner,
            mechanic="damage", subtype=normalized.subtype or self._damage_context_subtype(damage_type),
            amount=normalized.amount if normalized.amount is not None else amount,
            tags=normalized.tags, event_id=normalized.event_id,
            parent_event_id=normalized.parent_event_id,
        ), False

    def _attach_damage_context(self, detail: dict, ctx: EffectContext, legacy_warning: bool) -> dict:
        detail["ctx"] = ctx.to_dict()
        if legacy_warning:
            detail["context_warning"] = "伤害缺少EffectContext；已按legacy来源兼容记录"
        return detail

    def _write_hp_loss_record(self, entity: Entity, amount: int,
                              parent_ctx: Optional[EffectContext | dict],
                              subtype: str, reaction_logs: Optional[list]) -> dict:
        """构造一条“实际失去生命”事件并登记到 entity._hp_loss_events。

        仅负责记账，不触发任何反应。触发统一由调用方决定：
          - 既有 ctx-rich 入口（_apply_hostile_damage / _raw_hp_loss / 流血代价……
            已置 _hp_loss_recording>0 抑制兜底）：在 _record_hp_loss_event 里触发；
          - 其余一切降血（血限压顶/衰老/道纹直接减血/直写 current_hp）经
            Entity.__setattr__ 兜底钩子 _on_entity_hp_fallen 触发，再把结果写回本账。

        parent_ctx 既可能是 EffectContext 也可能是 legacy dict（兜底钩子从
        _hp_loss_ctx 取到），统一经 normalize_context 归一，避免 .timing 直接崩溃。
        """
        parent_ctx = normalize_context(parent_ctx) if parent_ctx is not None else None
        ctx = make_context(
            timing=parent_ctx.timing if parent_ctx else self._current_context_timing(),
            source=parent_ctx.source if parent_ctx else "legacy_hp_loss",
            source_type=parent_ctx.source_type if parent_ctx else "legacy",
            actor=parent_ctx.actor if parent_ctx else None,
            target=entity,
            owner=parent_ctx.owner if parent_ctx else None,
            mechanic="hp_loss",
            subtype=subtype,
            amount=amount,
            tags=(set(parent_ctx.tags) if parent_ctx else {"legacy_context"}),
            parent_event_id=parent_ctx.event_id if parent_ctx else None,
        )
        record = ctx.to_dict()
        events = getattr(entity, "_hp_loss_events", None)
        if events is None:
            entity._hp_loss_events = []
            events = entity._hp_loss_events
        events.append(record)
        if reaction_logs:
            record["reaction_logs"] = reaction_logs
        return record

    def _record_hp_loss_event(
        self, entity: Entity, amount: int,
        parent_ctx: Optional[EffectContext] = None,
        *, subtype: str = "damage",
    ) -> Optional[dict]:
        """记录“实际失去生命”事件；不改变既有 hp_lost_this_round 数值来源。

        「失去生命后」反应法术：无论因何失血（攻击/道纹/代价/血限压迫/爆裂反噬……）
        一律触发。攻击路径的失血由 resolve_attack 的反应窗口结算，此处跳过以免双发；
        反应法术自身的结算会把 _resolving_life_lost_reactions 置 >0，避免连锁死循环。
        """
        if entity is None or amount <= 0:
            return None
        # 先触发（沿用既有语义），再把结果写回同一笔失血账。
        if (self._attack_after_window_target is not entity
                and self._resolving_life_lost_reactions == 0):
            logs = self._fire_after_life_lost(entity, parent_ctx)
        else:
            logs = None
        record = self._write_hp_loss_record(entity, amount, parent_ctx, subtype, logs)
        toll = self._settle_chenglu(entity, amount, parent_ctx)
        if toll:
            record["chenglu"] = toll
        return record

    def _settle_chenglu(self, entity: Entity, amount: int,
                           parent_ctx: Optional[EffectContext | dict] = None) -> Optional[dict]:
        """承露盏（遗物）：每累计失去10点生命，获得1点法力。本场累计，战始归零。

        挂在 _record_hp_loss_event 这条**唯一失血总账**上，所以不区分来源：
        透支的流血、血影的流血、挨打、爆裂反噬……一律计入。这正是它与卖血流
        （血炼周天 = 再生⇄透支）配套的地方——透支每轮流血 4X 本来是纯支出，
        现在每满 10 点返还 1 法力。

        余数滚存（chenglu_paid 记已兑换过的总额），所以"失去 7 + 失去 5"
        照样在第 10 点上结一次账，不会因为分笔挨打而永远凑不满。
        不朽之躯的钳制照常生效：返还的法力仍然过 clamp，不能超过[法限]。
        """
        if entity is None or amount <= 0:
            return None
        if not self._relic_active(entity, "承露盏"):
            return None
        entity.hp_lost_this_battle = getattr(entity, "hp_lost_this_battle", 0) + amount
        paid = getattr(entity, "chenglu_paid", 0)
        gain = (entity.hp_lost_this_battle // 10) - (paid // 10)
        if gain <= 0:
            return None
        entity.chenglu_paid = (entity.hp_lost_this_battle // 10) * 10
        before = entity.current_mana
        entity.current_mana += gain
        self.clamp_immortal_body(entity)
        actual = entity.current_mana - before
        return {"relic": "承露盏", "hp_lost_total": entity.hp_lost_this_battle,
                "mana_gained": actual, "capped": actual < gain}

    # ---- 「失去生命后」统一拦截：绑定与兜底触发 (2026-08-30) ----
    def _hp_record_entities(self) -> list[Entity]:
        ents: list[Entity] = []
        if self.state.player is not None:
            ents.append(self.state.player)
        ents.extend(self.state.enemies)
        ents.extend(self.state.friends)
        ents.extend(self.state.employees)
        ents.extend(self.state.temp_friends)
        return ents

    def _bind_existing_hp_hooks(self) -> None:
        for entity in self._hp_record_entities():
            self._bind_hp_hook(entity)

    def _bind_hp_hook(self, entity: Optional[Entity]) -> None:
        if entity is None:
            return
        try:
            entity._hp_engine_ref = weakref.ref(self)
        except TypeError:
            pass  # 兜底：非可弱引用对象不绑定（正常 CombatEngine 均可弱引用）

    def _engine_owns(self, entity: Entity) -> bool:
        if entity is None:
            return False
        if entity is self.state.player:
            return True
        for lst in (self.state.enemies, self.state.friends,
                    self.state.employees, self.state.temp_friends):
            for e in lst:
                if e is entity:
                    return True
        return False

    def _on_entity_hp_fallen(self, entity: Entity, old: int, new: int) -> None:
        """Entity.__setattr__ 兜底钩子：任何未被既有入口接管的生命下降都走这里。

        被接管（_hp_loss_recording>0）或攻击失血（_attack_after_window_target）或
        反应自身失血（_resolving_life_lost_reactions>0）时提前返回，避免双发。
        未来新增道纹/遗物只要让 current_hp 变小，无需再手工接线。
        """
        if entity is None or not entity.is_alive:
            return
        if self._hp_loss_recording > 0:
            return
        if self._attack_after_window_target is entity:
            return
        if not self._engine_owns(entity):
            return  # 深拷贝快照/外部实体不越界触发
        ctx = self._hp_loss_ctx
        self._hp_loss_ctx = None  # 用完即清，避免残留上下文污染后续未接线的降血
        # 只检测“生命下降就触发”，不关心具体成因。触发后把结果登记到同一笔失血账，
        # 使血限压顶/衰老/道纹直减等“由兜底钩子接管”的降血也可见、可追踪，
        # 与 ctx-rich 路径（_record_hp_loss_event）保持一致。
        logs = self._fire_after_life_lost(entity, ctx)
        if logs:
            self._write_hp_loss_record(entity, old - new, ctx, "fallback_hp_loss", logs)

    # ========== 统一死亡判定 / 统一血限变化 ==========

    def _check_hp_zero_death(
        self, entity: Optional[Entity],
        ctx: Optional[EffectContext | dict] = None,
    ) -> bool:
        """统一“生命归零 → 命零”判定。

        任何使生命可能归零的状态变化（伤害 / 代价 / 血限压迫 / 崩解 / 特殊事件）
        都必须用这一个入口收口，禁止再写 `entity.is_alive = False`。
        返回本次调用是否判定了死亡（已死者返回 False，保持幂等）。

        注意：本方法只负责“判定 + 通知”，不做任何濒死保护——
        濒死/保护（撤退、负岳碑、断尾求生）在伤害管线更早的 mitigation 阶段完成，
        走到这里说明保护已经没有拦住。
        """
        if entity is None or entity.current_hp > 0:
            return False
        # 永久离场（雕塑/癌变/还债/救赎/逃跑）不是命零，绝不在此处宣布死亡；【封印】暂离不走本管线。
        if getattr(entity, "is_departed", False):
            return False
        # 已经走过统一死亡管线（含 Entity.take_damage 先翻了 is_alive 的情况）就不重复触发。
        if getattr(entity, "_death_triggers_emitted", False):
            return False
        self._hp_loss_recording += 1  # 命零置血=死亡收尾，不触发「失去生命后」
        try:
            entity.current_hp = 0
            entity.is_alive = False
        finally:
            self._hp_loss_recording -= 1
        self._on_entity_death(entity, ctx=ctx)
        return True

    def _record_blood_limit_event(
        self, entity: Entity, delta: int,
        parent_ctx: Optional[EffectContext] = None,
        *, source: str = "", source_type: str = "", subtype: str = "",
        actor: Optional[Entity] = None, owner: Optional[Entity] = None,
        tags: Optional[set[str]] = None,
    ) -> Optional[dict]:
        """记录一次血限变化的来源上下文，并发出 BLOOD_LIMIT_CHANGED 事件。

        血限变化是因果链的中间跳（伤害 → 血限下降 → 依赖血限的效果），
        没有它，“血限为什么掉”只能靠 Hook 自己猜。
        """
        if entity is None or delta == 0:
            return None
        if tags is None:
            base = set(parent_ctx.tags) if parent_ctx else {"legacy_context"}
            tags = base | {"blood_limit_loss" if delta < 0 else "blood_limit_gain"}
        ctx = make_context(
            timing=parent_ctx.timing if parent_ctx else self._current_context_timing(),
            source=source or (parent_ctx.source if parent_ctx else "legacy_blood_limit"),
            source_type=source_type or (parent_ctx.source_type if parent_ctx else "legacy"),
            actor=actor if actor is not None else (parent_ctx.actor if parent_ctx else None),
            target=entity,
            owner=owner if owner is not None else (parent_ctx.owner if parent_ctx else None),
            mechanic="blood_limit_change",
            subtype=subtype or ("cut" if delta < 0 else "gain"),
            amount=delta,
            tags=tags,
            parent_event_id=parent_ctx.event_id if parent_ctx else None,
        )
        record = ctx.to_dict()
        events = getattr(entity, "_blood_limit_events", None)
        if events is None:
            entity._blood_limit_events = []
            events = entity._blood_limit_events
        events.append(record)
        self._emit(
            CombatEventType.BLOOD_LIMIT_CHANGED,
            actor=ctx.actor, target=entity, ctx=record,
            delta=delta, blood_limit_after=entity.blood_limit,
        )
        return record

    def _heal_blocked(self, entity: Entity) -> bool:
        """目标是否被禁疗：坏死 / 镇尸 均为「无法获得[回复]」的效果。

        两个道纹各自独立（不做合并），只在同一消费点上共同判定。
        """
        return entity is not None and (entity.has_status("坏死") or entity.has_status("镇尸"))

    def _death_triggers_silenced(self, dying: Optional[Entity] = None) -> bool:
        """【缄默】的消费点：场上所有由[命零]触发的效果无法触发（持续X回合）。

        判定口径＝**全场**，不看阵营：只要场上还有一名存活实体带着【缄默】状态，
        本次[命零]触发效果一律封禁。正在命零的这名**自身**也计入——它此刻
        `is_alive` 已翻假、不在 `get_all_player_side()/get_all_enemy_side()` 里，
        若不算它，「缄默持有者自毁（尸爆）」就会在它自己死亡的那一刻失效。

        被封禁的效果：焦黑发丝（怪物命零→玩家速度+2）、招魂尸体入账、
        尸爆的[命零]AoE 与自毁、吞骸龙胃的吞噬窗口（DM 裁定 2026-09-18：窗口由[命零]
        开启，属"由[命零]触发的效果"）。自 2026-09-18 起 ENTITY_DIED 机制在
        `TriggerBus.dispatch` 默认封禁，新机制不必自己挂 `death_not_silenced()`。

        **明确不封禁的（都不是"由[命零]触发的效果"，勿当漏接线顺手改掉）**：
        - 死亡本身：命零就是死亡，不是它触发的效果。濒死保护（撤退/负岳碑/断尾求生）
          在伤害管线更早的 mitigation 阶段完成，走不到本判定。
          唯一例外是尸爆的自毁式命零——自毁本身就是那条[命零]效果的组成部分。
        - [战终]击杀奖励与碎片：那是战斗结算，不是命零触发效果。
        - 死之传承：轮回者命零后重置进入 setup，是轮回流程本身。封了会死锁——
          人已经死了，轮回却推不下去。
        - 性格特征清除（remove_personality）：实例生命周期收尾，不是游戏效果。
        """
        if dying is not None and dying.has_status("缄默"):
            return True
        for entity in (*self.state.get_all_player_side(), *self.state.get_all_enemy_side()):
            if entity.has_status("缄默"):
                return True
        return False

    def _apply_blood_limit_change(
        self, entity: Entity, delta: int, source: str, polarity: str,
        *, ctx: Optional[EffectContext | dict] = None,
        source_type: str = "", subtype: str = "",
        actor: Optional[Entity] = None, owner: Optional[Entity] = None,
        tags: Optional[set[str]] = None,
        clamp_hp: bool = True, lethal: bool = True,
    ) -> dict:
        """血限变化的统一入口：登记账本 → 记录来源 → 生命封顶 → 统一命零判定。

        数值行为与原来散落的 `_battle_delta + current_hp=min(...) + 手写命零` 完全一致，
        只是把“来源上下文”和“命零判定”固定下来，防止再出现
        “血限压到 0 生命却仍然 is_alive=True”的非法状态。
        """
        parent = normalize_context(ctx)
        applied = self._battle_delta(entity, "blood_limit", delta, source, polarity)
        bl_ctx = self._record_blood_limit_event(
            entity, applied, parent, source=source, source_type=source_type,
            subtype=subtype, actor=actor, owner=owner, tags=tags)
        if clamp_hp:
            # 血限压降导致的当前生命下降同样是失血；统一交给
            # Entity.__setattr__ 的「失去生命后」钩子触发，不再手工接线。
            # 「失去生命前」：血限压迫即将把当前生命压下来。
            if entity.current_hp > entity.blood_limit and self._attack_after_window_target is not entity:
                self._fire_before_life_lost(entity, bl_ctx or parent)
            self._hp_loss_ctx = bl_ctx or parent
            entity.current_hp = min(entity.current_hp, entity.blood_limit)
        died = self._check_hp_zero_death(entity, ctx=bl_ctx or parent) if lethal else False
        return {"applied": applied, "ctx": bl_ctx, "died": died}

    def _apply_hostile_damage(self, target: Entity, amount: int,
                              damage_type: str = DamageType.NORMAL.value,
                              source: Optional[Entity] = None,
                              ctx: Optional[EffectContext | dict] = None) -> dict:
        """
        对target造成外部/敌对伤害的统一入口（通过 HookManager 全生命周期调度）。
        ctx 为兼容层来源上下文，不改变既有伤害结算顺序和返回核心字段。
        """
        damage_ctx, legacy_ctx = self._damage_context(target, amount, damage_type, source, ctx)
        if self._effect_chain_depth >= self.MAX_EFFECT_CHAIN_DEPTH:
            # 保险丝：正常规则下不可能走到这里（重定向每跳都会递减计数，本身收敛）。
            raise RecursionError(
                f"效果链深度超过{self.MAX_EFFECT_CHAIN_DEPTH}层，疑似 A→B→A 循环触发："
                f"{damage_ctx.source}→{getattr(target, 'name', '?')}")
        self._effect_chain_depth += 1
        self._hp_loss_recording += 1  # 伤害失血由 _record_hp_loss_event 接管，抑制兜底钩子
        try:
            return self._apply_hostile_damage_inner(
                target, amount, damage_type, source, damage_ctx, legacy_ctx)
        finally:
            self._hp_loss_recording -= 1
            self._effect_chain_depth -= 1

    def _apply_hostile_damage_inner(
        self, target: Entity, amount: int, damage_type: str,
        source: Optional[Entity], damage_ctx: EffectContext, legacy_ctx: bool,
    ) -> dict:
        amount = self.hook_manager.apply_multiplier_adjust(target, amount, damage_type, source, self.state)
        amount = self._incoming_adjust(target, amount, damage_type)

        # 1. 伤害重定向 (嫁祸 / 背负)
        redirected_target = self.hook_manager.apply_redirection(target, damage_type, self.state)
        if redirected_target is not None:
            redirected_ctx = make_context(
                timing=damage_ctx.timing, source=damage_ctx.source,
                source_type=damage_ctx.source_type, actor=damage_ctx.actor,
                target=redirected_target, owner=damage_ctx.owner,
                mechanic="damage", subtype=damage_ctx.subtype, amount=amount,
                tags=set(damage_ctx.tags) | {"redirected"},
                parent_event_id=damage_ctx.event_id,
            )
            return self._apply_hostile_damage(redirected_target, amount, damage_type, source, ctx=redirected_ctx)

        # 2. 受到伤害前反噬 (爆裂 Hook)
        before_res = self.hook_manager.apply_before_damage(target, amount, damage_type, source, self.state)
        if before_res.get("reflected"):
            # 爆裂在 Hook 内直接扣了攻击者的生命并计入其本回合失血；此处补记来源上下文。
            reflect_ctx = self._record_hp_loss_event(
                source, before_res["reflected"], damage_ctx, subtype="baolie_reflect")
            if reflect_ctx:
                before_res["reflect_ctx"] = reflect_ctx
        if before_res.get("suppressed"):
            if source is not None and not source.is_alive:
                self._on_entity_death(source, ctx=before_res.get("reflect_ctx") or make_context(
                    timing=damage_ctx.timing, source="爆裂", source_type="daowen",
                    actor=target, target=source, owner=target, mechanic="death",
                    subtype="baolie_reflect", tags={"daowen", "reflect"},
                    parent_event_id=damage_ctx.event_id))
            return self._attach_damage_context({
                "raw_damage": amount, "shield_absorbed": 0, "actual_damage": 0,
                "hp_before": target.current_hp, "hp_after": target.current_hp,
                "blood_limit_before": target.blood_limit, "died": False,
                "damage_type": damage_type, "baolie_suppress": True,
            }, damage_ctx, legacy_ctx)

        # 3. 濒死伤害拦截与保护 (撤退 / 负岳碑 / 断尾求生)
        mitigation = self.hook_manager.apply_mitigation(target, amount, damage_type, self)
        if mitigation is not None:
            return self._attach_damage_context(mitigation, damage_ctx, legacy_ctx)

        # 4. 基础扣血。贯穿：你造成的伤害（任意通道）无视格挡；代价仍按代价结算。
        apply_type = damage_type
        if (source is not None and damage_type != "代价"
                and hasattr(source, "has_status") and source.has_status("贯穿")):
            apply_type = "无视格挡"
        # ---- 「受到伤害前 / 失去生命前」自动反应窗口（非攻击伤害） ----
        # 攻击路径（resolve_attack）由显式反应窗口结算，此处跳过以免双发；只有
        # 非攻击伤害（道纹/反噬等）才在这里自动触发，且无需逐个效果开窗。
        reaction_logs: list = []
        if (self._attack_after_window_target is not target
                and amount > 0 and target.is_alive):
            before = self._fire_auto_reaction(
                target, ActionPhase.BEFORE_DAMAGE_TAKEN.value, damage_ctx)
            if before:
                reaction_logs.extend(before)
            if source is not None and not source.is_alive:
                amount = 0
            # 失去生命前：受到伤害前反应已结算、伤害数值已确定，但生命尚未扣减。
            if amount > 0 and target.is_alive:
                life_before = self._fire_auto_reaction(
                    target, ActionPhase.BEFORE_LIFE_LOST.value, damage_ctx)
                if life_before:
                    reaction_logs.extend(life_before)
                if source is not None and not source.is_alive:
                    amount = 0
        detail = target.take_damage(amount, apply_type)
        self._attach_damage_context(detail, damage_ctx, legacy_ctx)
        actual = detail.get("actual_damage", 0)
        # 「受到伤害后」自动反应窗口：这一击已完整落地（即使被格挡全部吸收也算
        # "受到了伤害"，与"失去生命后"要求 actual_damage>0 严格区分）。
        if (self._attack_after_window_target is not target
                and amount > 0 and target.is_alive):
            damage_after = self._fire_auto_reaction(
                target, ActionPhase.AFTER_DAMAGE_TAKEN.value, damage_ctx)
            if damage_after:
                reaction_logs.extend(damage_after)
        if reaction_logs:
            detail["reaction_logs"] = reaction_logs
        hp_loss_ctx = self._record_hp_loss_event(target, actual, damage_ctx, subtype="damage")
        if hp_loss_ctx:
            detail["hp_loss_ctx"] = hp_loss_ctx
        self._emit(
            CombatEventType.DAMAGE_APPLIED, actor=source, target=target, ctx=detail["ctx"],
            raw_damage=amount, actual_damage=actual,
            shield_absorbed=detail.get("shield_absorbed", 0),
            hp_after=detail.get("hp_after"), damage_type=damage_type,
        )

        # 5. 落地后效果 (逆鳞 / 伤痕 / 寄生 / 负岳索 / 龙族血脉斩杀)
        self.hook_manager.apply_after_damage_pipeline(target, actual, detail.get("shield_absorbed", 0), detail, source, self)
        if target.entity_type == "怪物" and target.is_alive:
            redemption = self.check_redemption(target)
            if redemption:
                detail["redemption"] = redemption

        return detail

    def _on_entity_death(self, entity: Entity, ctx: Optional[EffectContext | dict] = None) -> None:
        """统一死亡触发；重复通知通过实体标记幂等。"""
        if getattr(entity, "_death_triggers_emitted", False):
            return
        parent = normalize_context(ctx)
        # 【缄默】消费点：封禁判定必须在死亡上下文构造之前完成，才能进 tags 供机制条件读取。
        silenced = self._death_triggers_silenced(entity)
        # 死因优先级：离场原因 > 调用方显式给出的死亡上下文 subtype > 兜底 hp_zero。
        # （【崩解】【凡庸】【尸爆】等特殊死因靠这一步才能留在 _death_ctx 里。）
        subtype = getattr(entity, "departure_reason", "")
        if not subtype and parent is not None and parent.subtype:
            if parent.mechanic == "death":
                subtype = parent.subtype
            elif parent.mechanic in self.NAMED_DEATH_MECHANICS:
                subtype = self.NAMED_DEATH_MECHANICS[parent.mechanic]
        death_ctx = make_context(
            timing=parent.timing if parent else self._current_context_timing(),
            source=parent.source if parent else "legacy_death",
            source_type=parent.source_type if parent else "legacy",
            actor=parent.actor if parent else None,
            target=entity,
            owner=parent.owner if parent else None,
            mechanic="death",
            subtype=subtype or "hp_zero",
            amount=0,
            tags=(set(parent.tags) if parent else {"legacy_context"})
                 | ({"silenced_death"} if silenced else set()),
            parent_event_id=parent.event_id if parent else None,
        )
        entity._death_ctx = death_ctx.to_dict()
        entity._death_triggers_emitted = True
        # 性格特征生命周期（2026-08-26）：命零即随实例清除（幂等）。
        # 挂在统一死亡管线里，AI/事件系统/查询接口此后都读不到该角色性格；
        # 不写模板、不跨实例继承、不留永久人格历史。
        remove_personality(self.state, entity)
        self._emit(
            CombatEventType.ENTITY_DIED, actor=death_ctx.actor, target=entity,
            ctx=entity._death_ctx, entity_type=entity.entity_type,
            cause=death_ctx.subtype, silenced=silenced,
        )
        if entity.entity_type == "怪物" and not silenced:
            # 乱葬岗·招魂：记录本场已命零怪物尸体。
            # 【缄默】生效期不入账——唤尸是[命零]触发的效果，封禁期内无尸可唤。
            if not getattr(self.state, "dead_monsters", None):
                self.state.dead_monsters = []
            if entity not in self.state.dead_monsters:
                self.state.dead_monsters.append(entity)
        # 2026-09-17：【分裂】改为即时创生（见 _spawn_fenlie_clones），
        # 原「[命零]时按本体 20% 血限创造复制体」的分支已随重做删除。

    def _spawn_fenlie_clones(self, caster, count: int, clone_hp: int) -> list:
        """【分裂】即时创造复制体：count 个 clone_hp 血限/生命的自身复制体。

        复制体继承本体除【分裂】外的全部道纹（避免无限套娃），阵营与本体一致
        （本体是怪物→进 enemies；否则→进 temp_friends）。返回新建的复制体列表。
        """
        clones = []
        for i in range(max(0, count)):
            clone = Entity(name=f"{caster.name}·裂{i + 1}",
                           entity_type=caster.entity_type,
                           blood_limit=clone_hp, current_hp=clone_hp,
                           attack_count=caster.attack_count,
                           attack_power=caster.attack_power)
            for dw_name, dw_inst in caster.dao_wen.items():
                if dw_name == "分裂":
                    continue      # 复制体无分裂道纹，防止无限分裂
                clone.dao_wen[dw_name] = dw_inst
            self._bind_hp_hook(clone)
            if caster.entity_type == "怪物":
                self.state.enemies.append(clone)
            else:
                self.state.temp_friends.append(clone)
            clones.append(clone)
        return clones
