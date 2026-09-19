"""CombatEngine 分片：碎片/必中/血契、数值代价校验与分摊（均摊/波次/塌缩）、pay_numeric_cost、
流血代价、龙心抵消、连心银行；含类级常量 SHAREABLE_NUMERIC_COSTS

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
from ..resolution import KIND_COST, note_delta, resolution_frame
from ..models import MONSTER_MANA_RELIC


class CostPaymentMixin:
    def bizhong_remaining(self, entity: Entity) -> int:
        """必中X剩余可选目标次数。"""
        return max(0, int(getattr(entity, "_bizhong_left", 0) or 0))

    def grant_bizhong(self, entity: Entity, x: int) -> int:
        """获得下X次选择[目标]时无法被闪避。次数叠加。"""
        if not isinstance(x, int) or isinstance(x, bool) or x < 1:
            return self.bizhong_remaining(entity)
        entity._bizhong_left = self.bizhong_remaining(entity) + x
        entity.status_effects = [s for s in entity.status_effects if s.name != "必中"]
        entity.add_status(StatusEffect(
            name="必中", value=entity._bizhong_left, remaining_rounds=-1, source=entity.name))
        return entity._bizhong_left

    def consume_bizhong(self, entity: Entity) -> bool:
        """消耗一次必中余数。还有余数则本次选择[目标]无法闪避。"""
        left = self.bizhong_remaining(entity)
        if left <= 0:
            return False
        entity._bizhong_left = left - 1
        if entity._bizhong_left <= 0:
            entity.status_effects = [s for s in entity.status_effects if s.name != "必中"]
        else:
            for s in entity.status_effects:
                if s.name == "必中":
                    s.value = entity._bizhong_left
        return True

    def _on_cost_paid(self, payer: Entity, cost_ctx: Optional[EffectContext] = None) -> Optional[dict]:
        """烙痕钉等“每付出一次代价”效果的统一触发点。"""
        if payer is not self.state.player:
            return None
        ref = self.state.event_modifiers.get("brand_nail_target_ref")
        target = self._combat_entity_refs().get(ref or "")
        if target is None or not target.is_alive:
            return None
        detail = self._apply_hostile_damage(
            target, 10, "必中", payer,
            ctx={"timing": (cost_ctx.timing if cost_ctx else self._current_context_timing()),
                 "source": "烙痕钉", "source_type": "relic", "actor": payer,
                 "target": target, "owner": payer, "mechanic": "damage", "subtype": "relic",
                 "amount": 10, "tags": {"relic", "must_hit"},
                 "parent_event_id": cost_ctx.event_id if cost_ctx else None})
        return {"target": target.name, **detail}

    SHAREABLE_NUMERIC_COSTS = {"流血", "衰老", "枯竭", "萎缩", "疲惫", "异变"}

    def blood_pact_targets(self, payer: Optional[Entity] = None) -> dict[str, Entity]:
        """【血契】可共同承担代价的存活朋友/员工；不要求玩家侧员工已部署。"""
        payer = payer or self.state.player
        if payer is self.state.player:
            targets: dict[str, Entity] = {}
            for prefix, entities in (("friend", self.state.friends), ("employee", self.state.employees)):
                for index, entity in enumerate(entities):
                    if entity.is_alive:
                        targets[f"{prefix}:{index}"] = entity
            return targets
        if payer is not None and payer.entity_type == "轮回者" and self.state.on_enemy_side(payer):
            return {
                f"enemy:{index}": entity
                for index, entity in enumerate(self.state.enemies)
                if entity is not payer and entity.is_alive
                and entity.entity_type in ("朋友", "员工")
            }
        return {}

    def _has_active_blood_pact(self, payer: Entity) -> bool:
        if payer is self.state.player:
            return (any(relic.name == "血契" for relic in self.state.relics)
                    and self.state.sealed_relics.get("血契", 0) <= 0)
        if payer is not None and payer.entity_type == "轮回者" and self.state.on_enemy_side(payer):
            return any(relic.name == "血契" for relic in self.state.opponent_relics)
        return False

    def blood_shadow_cost_share_options(self, payer: Entity) -> list[dict]:
        """血影要求所有承担者支付后仍存活，返回可执行的血契分担引用。"""
        if not self._has_active_blood_pact(payer) or payer.current_hp <= 5:
            return []
        return [
            {"ref": ref, "name": ally.name}
            for ref, ally in self.blood_pact_targets(payer).items()
            if ally.current_hp > 5
        ]

    @staticmethod
    def _cost_capacity(entity: Entity, cost_type: str) -> Optional[int]:
        return {
            "流血": entity.current_hp,
            "衰老": entity.blood_limit,
            "枯竭": entity.mana_limit,
            "萎缩": entity.speed_limit,
            "疲惫": entity.current_speed,
            "异变": None,
        }.get(cost_type)

    def validate_numeric_cost(
        self,
        payer: Entity,
        cost_type: str,
        amount: int,
        cost_share_target_ref: str = "",
    ) -> tuple[Entity, int, Optional[Entity], int]:
        """纯校验并计算血契拆分；无法整除的余数按随机数分配（平分规则，2026-08-21）。"""
        if cost_type not in self.SHAREABLE_NUMERIC_COSTS:
            raise ValueError(f"{cost_type}不是可共同承担的数值代价")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise ValueError("数值代价必须是非负整数")
        ally = None
        owner_amount = amount
        ally_amount = 0
        if cost_share_target_ref:
            if not self._has_active_blood_pact(payer):
                raise ValueError("只有持有且未被封印【血契】的轮回者才能提交cost_share_target_ref")
            ally = self.blood_pact_targets(payer).get(cost_share_target_ref)
            if ally is None:
                raise ValueError("cost_share_target_ref必须指向一名存活朋友/员工")
            # 血契：数值型代价可与一名存活的朋友或员工平分；无法整除时余数按随机数分配。
            owner_amount, ally_amount = self._divide_flat(amount, 2)
        for entity, part in ((payer, owner_amount), (ally, ally_amount)):
            if entity is None or part <= 0:
                continue
            capacity = (None if cost_type == "衰老" and self.state.side_has(entity, "不朽之躯")
                        else self._cost_capacity(entity, cost_type))
            if capacity is not None and part > capacity:
                raise ValueError(
                    f"{entity.name}无法完整承担{cost_type}{part}（可支付{capacity}）")
        return payer, owner_amount, ally, ally_amount

    def _apply_numeric_cost_part(
        self, payer: Entity, cost_type: str, amount: int,
        cost_context: Optional[EffectContext] = None,
    ) -> dict:
        """支付一方的已拆分数值代价；不再进行血契递归。"""
        if amount <= 0:
            return {"payer": payer.name, "cost_type": cost_type, "paid": 0}
        if cost_type == "流血":
            detail = self._pay_bleed_cost(payer, amount, cost_context=cost_context)
            return {"payer": payer.name, "cost_type": cost_type,
                    "paid": detail.get("actual_damage", 0), "detail": detail}
        if cost_type == "衰老":
            if self.state.side_has(payer, "不朽之躯"):
                return {"payer": payer.name, "cost_type": cost_type, "paid": 0, "immune": True}
            # 衰老是代价，不进局内可回滚账本；但血限变化的来源仍要可追溯。
            payer.blood_limit = max(0, payer.blood_limit - amount)
            self._record_blood_limit_event(
                payer, -amount, cost_context, source=(cost_context.source if cost_context else "衰老"),
                source_type=(cost_context.source_type if cost_context else "cost"),
                subtype="aging", tags=(set(cost_context.tags) if cost_context else set()) | {"cost", "blood_limit_loss"})
            # 血限压迫导致的当前生命下降同样是失血；统一交给
            # Entity.__setattr__ 的「失去生命后」钩子触发，不再手工接线。
            # 「失去生命前」：血限压迫即将把当前生命压下来。
            if payer.current_hp > payer.blood_limit and self._attack_after_window_target is not payer:
                self._fire_before_life_lost(payer, cost_context)
            self._hp_loss_ctx = cost_context
            payer.current_hp = min(payer.current_hp, payer.blood_limit)
            self._check_hp_zero_death(payer, ctx=cost_context)
        elif cost_type == "枯竭":
            payer.mana_limit = max(0, payer.mana_limit - amount)
            payer.current_mana = min(payer.current_mana, payer.mana_limit)
        elif cost_type == "萎缩":
            payer.speed_limit = max(0, payer.speed_limit - amount)
            overflow = max(0, payer.current_speed - payer.speed_limit)
            if overflow:
                self._lose_current_speed(payer, overflow, ctx=cost_context)
            else:
                payer.current_speed = min(payer.current_speed, payer.speed_limit)
        elif cost_type == "疲惫":
            self._lose_current_speed(payer, amount, ctx=cost_context)
        elif cost_type == "异变":
            mutation = payer.add_mutation(amount)
            if mutation.get("collapsed"):
                self._on_entity_death(payer, ctx=self._collapse_context(payer, cost_context))
            self._bank_lianxin(payer, cost_type, amount)
            nail = self._on_cost_paid(payer, cost_context)
            return {"payer": payer.name, "cost_type": cost_type, "paid": amount,
                    "mutation": mutation, "brand_nail": nail}
        self._bank_lianxin(payer, cost_type, amount)
        nail = self._on_cost_paid(payer, cost_context)
        return {"payer": payer.name, "cost_type": cost_type, "paid": amount,
                "brand_nail": nail}

    def _collapse_context(
        self, entity: Entity, parent: Optional[EffectContext | dict] = None,
    ) -> EffectContext:
        """【崩解】（异变达阈值直接命零）的统一死亡上下文。

        Entity.add_mutation 出于模型层职责只翻 is_alive，不知道战斗上下文；
        所有调用点都必须用本上下文把死亡交回 _on_entity_death，否则崩解死者
        不会触发任何[命零]效果（焦黑发丝/招魂尸体/分裂）。
        """
        norm = normalize_context(parent)
        return make_context(
            timing=norm.timing if norm else self._current_context_timing(),
            source="崩解", source_type="system",
            actor=norm.actor if norm else None, target=entity,
            owner=norm.owner if norm else None,
            mechanic="death", subtype="collapse", amount=0,
            tags=(set(norm.tags) if norm else set()) | {"mutation", "collapse"},
            parent_event_id=norm.event_id if norm else None,
        )

    def _divide_flat(self, total: int, count: int) -> list[int]:
        """平分规则（2026-08-21）：总数值在 count 个目标间平均分配；
        无法整除时余数按随机数分配（引擎随机源，可复现）。"""
        if count <= 0:
            return []
        neg = total < 0
        total = abs(total)
        base, rem = divmod(total, count)
        pieces = [base] * count
        if rem:
            pool = list(range(count))
            for _ in range(rem):
                idx = pool.pop(self.dice.randrange(len(pool)))
                pieces[idx] += 1
        return [-p for p in pieces] if neg else pieces

    def _wave_targets(self, caster: Entity) -> list[Entity]:
        """拥有施法者建立的波及效果的存活角色（不含施法者自身）。"""
        out: list[Entity] = []
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            if entity is caster or not entity.is_alive:
                continue
            if any(s.name == "波及" and s.source == caster.name and not s.is_expired
                   for s in entity.status_effects):
                out.append(entity)
        return out

    def _toggle_wave_mark(self, entity: Entity, caster: Entity) -> bool:
        """建立/解除波及效果：已带施法者波及标记则解除，否则建立。返回是否建立。"""
        for s in list(entity.status_effects):
            if s.name == "波及" and s.source == caster.name:
                entity.status_effects.remove(s)
                return False
        entity.add_status(StatusEffect(name="波及", value=1, remaining_rounds=-1,
                                       source=caster.name))
        return True

    def pay_numeric_cost(
        self,
        payer: Entity,
        cost_type: str,
        amount: int,
        *,
        cost_share_target_ref: str = "",
        dragon_heart_use: int = 0,
        ctx: Optional[EffectContext | dict] = None,
        cost_context: Optional[EffectContext | dict] = None,
    ) -> dict:
        """统一支付可分担的数值代价：龙心先抵消，血契再拆分剩余后果。

        ctx 为统一 EffectContext 兼容层；cost_context 是阶段性旧名，保留兼容。
        未迁移调用点仍可不传 ctx，但若存在需要上下文的监听，会在返回明细中给出
        context_warning，避免开发/测试时静默漏掉来源。
        """

        # 结算生命周期记账（engine/resolution.py）：代价也是「效果」，同样受限。
        _cost_field = {"生命": "hp", "法力": "mana", "精力": "energy"}.get(cost_type, cost_type)
        _before = {f: getattr(payer, f, None) for f in ("current_hp", "mana", "energy")}
        with resolution_frame(self, KIND_COST, getattr(payer, "name", "?"),
                              cost_type, amount):
            try:
                return self._pay_numeric_cost_inner(
                    payer, cost_type, amount,
                    cost_share_target_ref=cost_share_target_ref,
                    dragon_heart_use=dragon_heart_use, ctx=ctx,
                    cost_context=cost_context)
            finally:
                for attr, field in (("current_hp", "hp"), ("mana", "mana"),
                                    ("energy", "energy")):
                    note_delta(self, field if attr != "current_hp" else _cost_field,
                               _before[attr], getattr(payer, attr, None))

    def _pay_numeric_cost_inner(
        self,
        payer: Entity,
        cost_type: str,
        amount: int,
        *,
        cost_share_target_ref: str = "",
        dragon_heart_use: int = 0,
        ctx: Optional[EffectContext | dict] = None,
        cost_context: Optional[EffectContext | dict] = None,
    ) -> dict:
        """代价支付的实现体（对外契约见 pay_numeric_cost）。"""
        if not isinstance(dragon_heart_use, int) or isinstance(dragon_heart_use, bool) or dragon_heart_use < 0:
            raise ValueError("dragon_heart_use必须是非负整数")
        if payer is not self.state.player:
            shared_ok = ("共心环" in self.state.artifacts_owned
                         and self.state.shared_dragon_heart_type == cost_type)
            if not shared_ok:
                dragon_heart_use = 0
        heart = None
        offset = 0
        if dragon_heart_use > 0 and amount > 0:
            heart = next((item for item in self.state.consumables
                          if item.kind == "dragon_heart"
                          and item.dragon_heart_type == cost_type
                          and item.name == f"{cost_type}龙心"
                          and not item.is_depleted), None)
            if heart is not None:
                offset = min(dragon_heart_use, heart.current_uses, amount)
        remaining = amount - offset
        _, owner_amount, ally, ally_amount = self.validate_numeric_cost(
            payer, cost_type, remaining, cost_share_target_ref)
        if heart is not None and offset > 0:
            heart.current_uses -= offset
        normalized_ctx = normalize_context(ctx if ctx is not None else cost_context)
        if normalized_ctx is not None and normalized_ctx.mechanic != "cost":
            normalized_ctx = make_context(
                timing=normalized_ctx.timing, source=normalized_ctx.source,
                source_type=normalized_ctx.source_type, actor=normalized_ctx.actor or payer,
                target=normalized_ctx.target or payer, owner=normalized_ctx.owner,
                mechanic="cost", subtype=self._cost_context_subtype(cost_type), amount=remaining,
                tags=normalized_ctx.tags, event_id=normalized_ctx.event_id,
                parent_event_id=normalized_ctx.parent_event_id)
        owner_detail = self._apply_numeric_cost_part(payer, cost_type, owner_amount, normalized_ctx)
        ally_detail = (self._apply_numeric_cost_part(ally, cost_type, ally_amount, normalized_ctx)
                       if ally is not None else None)
        return {
            "cost_type": cost_type,
            "requested": amount,
            "dragon_heart_offset": offset,
            "remaining": remaining,
            "owner": owner_detail,
            "shared_with": ally_detail,
            "cost_share_target_ref": cost_share_target_ref or None,
            "actual_paid": owner_detail.get("paid", 0) + (ally_detail or {}).get("paid", 0),
            "ctx": normalized_ctx.to_dict() if normalized_ctx is not None else None,
        }

    @staticmethod
    def _cost_context_subtype(cost_type: str) -> str:
        return {
            "流血": "bleed", "衰老": "aging", "枯竭": "exhaust",
            "萎缩": "shrink", "疲惫": "fatigue", "异变": "mutation",
            "冷却": "cooldown", "失忆": "amnesia", "唯一": "unique",
        }.get(cost_type, cost_type)

    @staticmethod
    def _blood_oath_context_allows(ctx: Optional[EffectContext]) -> bool:
        """血誓戒只认明确主动流血代价来源；战始/局外/回终自动流血均不触发。"""
        if ctx is None:
            return False
        if ctx.mechanic != "cost" or ctx.subtype != "bleed":
            return False
        if "active_payment" not in ctx.tags:
            return False
        return ctx.timing in {"round_start", "player_action", "reaction"}

    def _pay_bleed_cost(
        self, payer: Entity, amount: int, dragon_heart_use: int = 0,
        *, cost_context: Optional[EffectContext] = None,
    ) -> dict:
        """
        支付单个承担者的"流血X"代价；血契拆分由 pay_numeric_cost 在外层完成。
        血誓戒：玩家在明确主动时点（回始/玩家行动/反应）首次主动支付流血代价时，
        获得等同于本次流血的格挡；若支付后生命≤30%[血限]，改为获得等量生命。
        血契分担时只按玩家本人实际承担的部分触发。
        dragon_heart_use：本次希望消耗"流血龙心"抵消的点数(龙心谷"炼心"产出)，抵消后剩余部分才真正支付。
        cost_context：代价来源上下文，未显式传入则不触发“主动/时点”监听。
        """
        cost_context = normalize_context(cost_context)
        actual, offset = self._offset_with_dragon_heart(payer, "流血", amount, dragon_heart_use)
        # 「失去生命前」自动反应窗口：非攻击流血代价，生命尚未扣减。
        life_before_logs: list = []
        if actual > 0 and payer.is_alive and self._attack_after_window_target is not payer:
            life_before_logs = self._fire_before_life_lost(payer, cost_context) or []
        self._hp_loss_recording += 1  # 代价失血由 _record_hp_loss_event 接管，抑制兜底钩子
        try:
            detail = payer.take_damage(actual, "代价")
        finally:
            self._hp_loss_recording -= 1
        detail["dragon_heart_offset"] = offset
        if life_before_logs:
            detail["reaction_logs"] = life_before_logs
        hp_loss_ctx = self._record_hp_loss_event(payer, actual, cost_context, subtype="cost")
        if hp_loss_ctx:
            detail["hp_loss_ctx"] = hp_loss_ctx
        if (cost_context is None and actual > 0 and payer is self.state.player
                and self._relic_active(payer, "血誓戒")):
            detail["context_warning"] = "流血代价缺少EffectContext；需要来源上下文的监听不会触发"
        if detail.get("died"):
            self._check_hp_zero_death(payer, ctx=hp_loss_ctx or cost_context)
        if (payer is self.state.player and actual > 0 and not payer.blood_oath_used_this_round
                and self._relic_active(payer, "血誓戒")
                and self._blood_oath_context_allows(cost_context)):
            payer.blood_oath_used_this_round = True
            if payer.blood_limit > 0 and payer.current_hp / payer.blood_limit <= 0.3:
                heal_detail = self.state.apply_heal(payer, actual, ctx={
                    "timing": cost_context.timing, "source": "血誓戒", "source_type": "relic",
                    "actor": payer, "target": payer, "owner": payer,
                    "mechanic": "heal", "subtype": "blood_oath", "amount": actual,
                    "tags": {"relic"}, "parent_event_id": cost_context.event_id,
                })
                detail["blood_oath"] = {"type": "life", "amount": heal_detail["actual_heal"], "heal_ctx": heal_detail.get("heal_ctx")}
            else:
                payer.gain_shield(actual)
                detail["blood_oath"] = {"type": "shield", "amount": actual}
        self._bank_lianxin(payer, "流血", actual)
        if actual > 0:
            nail = self._on_cost_paid(payer, cost_context)
            if nail:
                detail["brand_nail"] = nail
        return detail

    def _offset_with_dragon_heart(self, payer: Entity, cost_type: str, amount: int, dragon_heart_use: int) -> tuple:
        """
        用一枚匹配类型的【××龙心】抵消本次代价：最多抵消 min(请求量, 龙心当前耐久, 原始代价)。
        返回 (抵消后实际需支付的数值, 实际消耗的龙心点数)。
        """
        if dragon_heart_use <= 0 or amount <= 0:
            return amount, 0
        heart_name = f"{cost_type}龙心"
        heart = next((c for c in self.state.consumables
                      if c.kind == "dragon_heart" and c.dragon_heart_type == cost_type
                      and c.name == heart_name and not c.is_depleted), None)
        if heart is None:
            return amount, 0
        offset = min(dragon_heart_use, heart.current_uses, amount)
        if offset <= 0:
            return amount, 0
        heart.current_uses -= offset
        return amount - offset, offset

    def _bank_lianxin(self, payer: Entity, cost_type: str, actual_paid: int):
        """
        炼心待生效时，玩家下一次实际支付(抵消后仍>0)的数值型代价，转化为等值的【××龙心】消耗品。
        同名消耗品自动合并耐久与耐久上限（沿用既有消耗品合并规则）。
        """
        if not (payer is self.state.player and self.state.pending_lianxin and actual_paid > 0):
            return
        self.state.pending_lianxin = False
        heart_name = f"{cost_type}龙心"
        existing = next((c for c in self.state.consumables if c.name == heart_name and c.kind == "dragon_heart"), None)
        if existing:
            existing.current_uses += actual_paid
            existing.max_uses += actual_paid
        else:
            self.state.consumables.append(Consumable(
                name=heart_name, effect=f"消耗Y点耐久可抵消Y点{cost_type}代价",
                current_uses=actual_paid, max_uses=actual_paid,
                kind="dragon_heart", dragon_heart_type=cost_type))

