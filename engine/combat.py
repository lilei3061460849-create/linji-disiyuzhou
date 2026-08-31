"""
战斗计算引擎
负责战斗中的数值对撞、回合推进、伤害结算
所有数值计算在此完成，AI禁止自行计算
"""
from __future__ import annotations
import math
import weakref
from typing import Optional, Any
from .models import Entity, StatusEffect, GameState, DaoWenInstance, DaoWen, Spell, Consumable
from .daowen import DaoWenEngine, ResonanceEngine
from .dice import DiceEngine
from .enums import (ActionPhase, TriggerTiming, InterruptType, DamageType,
                    EffectScope, EffectPolarity, CostType)
from .dm_rulings import Interrupt
from .combat_events import CombatEvent, CombatEventType, register_combat_event_observer
from .combat_hooks import CombatHookManager
from .effect_context import EffectContext, make_context, normalize_context
from .mechanisms import MECHANISMS, Phase, TriggerBus, TriggerContext
from .personality import remove_personality


class CombatEngine:
    """战斗计算引擎"""
    
    # 副本专属道纹
    REGION_EXCLUSIVE_DAOWEN = {
        "扭曲都市": {"变形","定型","畸变","僵化","超频","坏死","爆裂","退化"},
        "罪孽都市": {"洗劫","逼债","抵扣","清算","赎金","假钞","赌命","消灾"},
        "龙心谷":   {"加害","龙鳞","逆鳞","活血","裂变","嫁祸","背负","伤痕"},
    }
    
    # 原始怪物道纹（道纹归属规则：各组起点）——【原初X】可借用范围
    ORIGINAL_MONSTER_DAOWEN = ("狂暴", "强化", "疯狂", "减速", "必中", "自愈", "飞行")
    # 原始怪物道纹每次实际发动时支付异变5X（X按该次发动时递增后的数值计算，
    # 见 README 怪物准则9·道纹递增）；效果持续期间（未再次发动）不再重复计费。
    # 必中为次数型（下X次选择[目标]无法闪避），余数记在 entity._bizhong_left。
    YUANCHU_COST_RATE = 5
    # 波及X（2026-08-21）：你发动的道纹同时作用于所有拥有波及效果的目标。
    # 数值键：效果的总数值在所有目标（本次[目标]+波及目标，均排除施法者自身）
    # 之间平均分配；无法整除时余数按随机数分配。多目标不会复制或增加总数值。
    # 状态类效果（减速减半/固定面板/持续状态等）对波及目标原样生效，不入下表。
    WAVE_NUMERIC_KEYS = (
        "target_damage", "total_damage", "hits", "aoe_damage", "hp_percent_loss",
        "target_heal", "heal_percent", "mutation_reduction",
        "target_shield", "shield_drain",
        "blood_limit_reduction", "hp_reduction", "blood_limit_increase", "blood_limit_penalty",
        "attack_boost", "attack_reduction",
        "speed_boost", "speed_penalty",
        "targets_removed", "self_attack_count", "invalid_damage_hits",
    )
    
    def __init__(self, state: GameState, dice: DiceEngine):
        self.state = state
        self.dice = dice
        self.combat_log: list[dict] = []  # 完整战斗日志
        self.hook_manager = CombatHookManager()
        self.mechanism_bus = TriggerBus()
        for mechanism in MECHANISMS.event_mechanisms():
            self.mechanism_bus.register(mechanism)
        # 唯一事件分发点：本引擎作为 GameState 的事件观察者，所有经
        # state.emit_combat_event 发出的事件（_emit 的三种 + apply_heal 的
        # HEAL_APPLIED）统一进入 TriggerBus，杜绝双发路径。
        # 观察者是可 pickle 的模块级类实例（持引擎 id，经弱引用表解析），
        # 不会把引擎带进存档/快照（见 combat_events.py 注释）。
        register_combat_event_observer(self.state, self)
        # 三相残韵盘本场消耗的残韵
        self._sanxiang_consumed = ""
        # 残韵改写：entity_id → {源道纹: 变化后道纹}，只改下一次发动结算，不改持有
        self._resonance_rewrites: dict[int, dict[str, str]] = {}
        # 效果链深度保险丝（见 MAX_EFFECT_CHAIN_DEPTH）。
        self._effect_chain_depth = 0
        # AFTER_LIFE_LOST(失去生命后) 反应法术在“非攻击失血”路径的自动触发状态：
        #   _resolving_life_lost_reactions > 0 表示正在结算某次反应法术——反应
        #     自身引发的失血不得再次触发反应（否则以牙还牙/血债会互相连锁死循环）。
        #   _attack_after_window_target 是当前攻击中、其失血由 resolve_attack 的
        #     反应窗口结算的目标；该目标失血不再重复走 hook（避免与窗口双发）。
        self._resolving_life_lost_reactions = 0
        self._attack_after_window_target = None
        # 「失去生命后」统一拦截（2026-08-30）：
        #   _hp_loss_recording>0 表示当前正由既有降血入口（_record_hp_loss_event /
        #     _apply_blood_limit_change / _apply_numeric_cost_part / 血限压迫等）接管，
        #     Entity.__setattr__ 的兜底钩子被抑制，避免与既有触发点双发。
        #   _hp_loss_ctx 是当前一次降血的来源上下文，供兜底钩子还原 attacker。
        self._hp_loss_recording = 0
        self._hp_loss_ctx = None
        # 把本引擎绑定到已有战斗实体上：之后任何 current_hp 下降都会经
        # Entity._fire_hp_loss → _on_entity_hp_fallen 上报（仅对引擎自有的实体生效）。
        self._bind_existing_hp_hooks()
        # 怪物战斗记述的实例化（2026-08-23）：这三个集合原本是类属性，
        # 仅靠 reset_monster_activation 在战始降级为实例属性——绕过战始的引擎
        # （测试夹具/模拟器直驱）会把 add() 写进跨实例共享的类集合，且 id()
        # 复用会让后建的怪物“被已进化”，发生跨用例/跨局串扰。
        self._monster_activated: dict = {}
        self._monster_evolved: set = set()
        self._monster_daowen_round_used: dict = {}

    # 效果链深度上限。这是**防御性保险丝**，不是游戏规则：
    # 任何合法的 嫁祸/背负 重定向链都远低于此值（重定向每跳都会递减 _jiahuo_left/_beifu_left，
    # 本身就收敛）。设成这么大是为了保证它永远不会改变任何现有战斗结果，
    # 只在真的出现 A→B→A→B 死循环时把它截断成一次可诊断的异常。
    MAX_EFFECT_CHAIN_DEPTH = 64

    @property
    def event_stream(self) -> list[CombatEvent]:
        """战斗事件流（“发生了什么”）。事实源在 state 上，随存档一起走。"""
        return self.state.combat_events

    def _emit(self, event_type: CombatEventType, *, actor=None, target=None,
              ctx: Optional[EffectContext | dict] = None, **data) -> CombatEvent:
        """登记一条 CombatEvent。ctx 只作为来源快照附带，不参与任何判定。

        机制分发由 GameState.emit_combat_event 内的事件观察者统一完成
        （见 __init__ 的 register_combat_event_observer）——本方法不再单独分发，
        避免同一事件被分发两次。
        """
        if isinstance(ctx, EffectContext):
            ctx = ctx.to_dict()
        return self.state.emit_combat_event(
            event_type, actor=actor, target=target, ctx=ctx, **data)

    def _dispatch_phase(self, phase: str, *, target=None, source=None,
                        amount: int = 0, damage_type: str = "") -> list:
        """宣布一个管线相位时点（机制系统，通用分发，不含任何机制判断）。

        已注册的相位机制按 priority 升序执行；返回值是各机制的报告条目
        （由调用点并入 effects/战报，保证报告与迁移前一致）。

        调用约定：管线只负责"在既有语义位置上宣布时点"，具体机制逻辑
        全部在声明层（engine/mechanisms/）。当前接线相位：
        INCOMING_ADJUST（Hook 路径）与 ROUND_START（本方法）。
        """
        results = []
        for mechanism in MECHANISMS.phase_mechanisms(phase):
            ctx = TriggerContext(combat=self, state=self.state, phase=phase,
                                 target=target, source=source,
                                 amount=amount, damage_type=damage_type)
            if mechanism.condition is not None and not mechanism.condition(ctx):
                continue
            targets = mechanism.target.select(ctx) if mechanism.target is not None else []
            result = mechanism.effect(ctx, targets)
            if result is not None:
                results.append(result)
        return results

    def _battle_delta(self, entity: Entity, field_name: str, delta: int,
                      source: str, polarity: str) -> int:
        """战斗中未注明永久且不是代价/伤害的面板变化统一登记为局内效果。"""
        return self.state.apply_scoped_delta(
            entity, field_name, delta,
            scope=EffectScope.BATTLE.value,
            polarity=polarity,
            source=source,
        )

    # ========== 伤害计算 ==========
    
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
    
    def _is_flying(self, entity: Entity) -> bool:
        return bool(getattr(entity, "is_flying", False)
                    or entity.has_status("飞行")
                    or entity.has_status("滑翔"))

    def _field_has_zhuiluo(self) -> bool:
        for e in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            if e.has_status("坠落"):
                return True
        return False

    def _tick_baolie(self, entities) -> list:
        """只递减爆裂。持续X按持有者的[敌回终]计数。"""
        logs = []
        for entity in entities:
            keep = tuple(s.name for s in entity.status_effects if s.name != "爆裂")
            expired = entity.tick_status_effects(skip_names=keep)
            if expired:
                logs.append({"type": "baolie_expired", "entity": entity.name})
        return logs

    def _incoming_adjust(self, target: Entity, amount: int, damage_type: str = "普通") -> int:
        if amount <= 0 or damage_type == "代价" or target is None:
            return amount
        return self.hook_manager.apply_incoming_adjust(target, amount, damage_type, None, self.state)

    def _record_speed_change_event(
        self, entity: Entity, amount: int,
        ctx: Optional[EffectContext | dict] = None,
        *, field: str = "current_speed",
    ) -> Optional[dict]:
        if entity is None or amount == 0:
            return None
        parent = normalize_context(ctx)
        if parent is not None and parent.mechanic == "speed_change":
            speed_ctx = parent
        else:
            speed_ctx = make_context(
                timing=parent.timing if parent else self._current_context_timing(),
                source=parent.source if parent else "legacy_speed_change",
                source_type=parent.source_type if parent else "legacy",
                actor=parent.actor if parent else entity,
                target=entity,
                owner=parent.owner if parent else None,
                mechanic="speed_change",
                subtype=field,
                amount=amount,
                tags=(set(parent.tags) if parent else {"legacy_context"}),
                parent_event_id=parent.event_id if parent else None,
            )
        record = speed_ctx.to_dict()
        events = getattr(entity, "_speed_change_events", None)
        if events is None:
            entity._speed_change_events = []
            events = entity._speed_change_events
        events.append(record)
        return record

    def _gain_speed(
        self, entity: Entity, amount: int,
        ctx: Optional[EffectContext | dict] = None,
    ) -> int:
        if amount <= 0:
            return 0
        if entity.has_status("加速"):
            amount *= 2
        before = entity.current_speed
        entity.current_speed += amount
        self.clamp_immortal_body(entity)
        gained = entity.current_speed - before
        self._record_speed_change_event(entity, gained, ctx, field="current_speed")
        return gained

    def clamp_immortal_body(self, entity: Entity) -> None:
        """不朽之躯：获得的[法力]/[速度]无法超过[法限]/[速限]。

        只限制“获得”的当前法力/当前速度不得超过各自上限；属性点（修行/无所求）
        提升的是上限本身，不受本限制。朋友/员工不继承（side_has 已排除）。
        """
        if entity is None or not self.state.side_has(entity, "不朽之躯"):
            return
        entity.current_mana = min(entity.current_mana, entity.mana_limit)
        entity.current_speed = min(entity.current_speed, entity.speed_limit)

    def _relic_active(self, entity: Entity, name: str) -> bool:
        if entity is None or not self.state.side_has(entity, name):
            return False
        if entity is self.state.player:
            return self.state.sealed_relics.get(name, 0) <= 0
        return True

    def _note_dodge(self, entity: Entity, relic_target_ref: Optional[str] = None) -> dict:
        """闪避专属句：正文写「每次闪避后」的效果。失速/归零不在这里。"""
        extra = {}
        if self._relic_active(entity, "避风铃"):
            entity.gain_shield(3)
            extra["avoid_wind_shield"] = 3
        if entity.has_status("急速"):
            entity._jisu_dodges = getattr(entity, "_jisu_dodges", 0) + 1
            if entity._jisu_dodges >= 2:
                entity._jisu_dodges -= 2
                extra["jisu_speed"] = self._gain_speed(entity, 1, ctx={
                    "timing": self._current_context_timing(), "source": "急速", "source_type": "daowen",
                    "actor": entity, "target": entity, "mechanic": "speed_change", "subtype": "current_speed",
                    "amount": 1, "tags": {"daowen", "dodge_followup"},
                })
        if entity.has_status("洞察"):
            entity._dongcha_pending = getattr(entity, "_dongcha_pending", 0) + 10
            extra["dongcha_pending"] = entity._dongcha_pending
        return extra

    def _spend_dodge_speed(self, entity: Entity, relic_target_ref: Optional[str] = None) -> dict:
        """闪避：先走失速总线，再结算闪避专属句。"""
        extra = {}
        extra["lost_speed"] = self._lose_current_speed(
            entity, 1, relic_target_ref, require_huifeng=self._huifeng_active(entity),
            ctx={"timing": self._current_context_timing(), "source": "闪避", "source_type": "action",
                 "actor": entity, "target": entity, "mechanic": "speed_change", "subtype": "current_speed",
                 "amount": -1, "tags": {"dodge", "active_payment"}})
        extra.update(self._note_dodge(entity))
        return extra

    def _huifeng_active(self, entity: Entity) -> bool:
        if entity is None or not self.state.side_has(entity, "回锋刀"):
            return False
        if entity is self.state.player:
            return self.state.sealed_relics.get("回锋刀", 0) <= 0
        return True

    def _huifeng_ref_from_choice(self, decision: Any) -> str:
        if not isinstance(decision, dict):
            return ""
        ref = decision.get("target_ref") or decision.get("dodge_relic_target_ref") or ""
        if isinstance(ref, str) and ref:
            return ref
        index = decision.get("enemy_index")
        if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
            return f"enemy:{index}"
        return ""

    def _remember_huifeng_target(self, holder: Entity, ref: str) -> None:
        if not ref:
            return
        if holder is self.state.player:
            self.state.event_modifiers["huifeng_target_ref"] = ref
        elif holder is not None:
            holder._huifeng_target_ref = ref
            self.state.event_modifiers["huifeng_target_ref_opponent"] = ref

    def _resolve_huifeng_target(self, holder: Entity, explicit_ref: Optional[str] = None):
        refs = self._combat_entity_refs()
        stored = ""
        if holder is self.state.player:
            stored = self.state.event_modifiers.get("huifeng_target_ref") or ""
        elif holder is not None:
            stored = (getattr(holder, "_huifeng_target_ref", "")
                      or self.state.event_modifiers.get("huifeng_target_ref_opponent")
                      or "")
        for ref in (explicit_ref or "", stored):
            if not ref:
                continue
            target = refs.get(ref)
            if (target is not None and target.is_alive
                    and self.state.on_player_side(target) != self.state.on_player_side(holder)):
                return ref, target
        return None, None

    def _trigger_huifeng_on_speed_loss(
        self, holder: Entity, lost: int, explicit_ref: Optional[str] = None,
        *, require_target: bool = False,
        speed_ctx: Optional[dict] = None,
    ) -> Optional[dict]:
        """回锋刀：每失去1点当前速度后，对已显式提交的[目标]造成3点伤害。不自动选目标。"""
        if lost <= 0 or not self._huifeng_active(holder):
            return None
        ref, target = self._resolve_huifeng_target(holder, explicit_ref)
        if target is None:
            if require_target:
                raise ValueError("回锋刀触发必须显式提交合法敌方目标引用")
            return None
        self._remember_huifeng_target(holder, ref)
        detail = self._apply_hostile_damage(target, 3 * lost, source=holder, ctx={
            "timing": (speed_ctx or {}).get("timing") or self._current_context_timing(),
            "source": "回锋刀", "source_type": "relic", "actor": holder, "target": target,
            "owner": holder, "mechanic": "damage", "subtype": "relic", "amount": 3 * lost,
            "tags": {"relic", "speed_loss_followup"},
            "parent_event_id": (speed_ctx or {}).get("event_id"),
        })
        if holder is not None and detail.get("actual_damage", 0) > 0:
            holder.damage_dealt_this_round += detail["actual_damage"]
        return {"target": target.name, "lost_speed": lost, **detail}

    def _lose_current_speed(
        self, entity: Entity, amount: int, relic_target_ref: Optional[str] = None,
        *, require_huifeng: bool = False,
        ctx: Optional[EffectContext | dict] = None,
    ) -> int:
        """失去当前速度的唯一入口。回锋刀按失去点数造伤；避风铃在归零时+15。"""
        if entity is None or amount <= 0:
            return 0
        before = entity.current_speed
        entity.current_speed = max(0, entity.current_speed - amount)
        lost = before - entity.current_speed
        speed_ctx = self._record_speed_change_event(entity, -lost, ctx, field="current_speed")
        if lost and before > 0 and entity.current_speed == 0:
            self._trigger_bifengling_zero(entity)
        self._trigger_huifeng_on_speed_loss(
            entity, lost, relic_target_ref, require_target=require_huifeng,
            speed_ctx=speed_ctx)
        # 冥气X：[目标]每失去一次速度[速限]-2，持续X。
        # 累计局内后果（同畸变/伤痕），经 BATTLE 账本登记，[战终]逆向清除。
        if lost and entity.has_status("冥气"):
            penalty = entity.get_status_value("冥气") or 0
            if penalty:
                self._battle_delta(entity, "speed_limit", -penalty, "冥气",
                                   EffectPolarity.DEBUFF.value)
                entity.current_speed = min(entity.current_speed, entity.speed_limit)
        return lost

    def _trigger_bifengling_zero(self, entity: Entity) -> None:
        """避风铃：当前速度归零时获得15点格挡。只认字段变为0。"""
        if not self._relic_active(entity, "避风铃"):
            return
        entity.gain_shield(15)

    def note_mana_inflicted(self, source: Entity, target: Entity, amount: int) -> None:
        """寒冰法力：对[目标]每累计施加10法力，使其本回合出手次数-1。"""
        if amount <= 0 or source is None or target is None:
            return
        if not self._relic_active(source, "寒冰法力"):
            return
        before_tier = target.mana_inflicted_this_round // 10
        target.mana_inflicted_this_round += amount
        new_stacks = target.mana_inflicted_this_round // 10 - before_tier
        if new_stacks > 0:
            target.add_status(StatusEffect(
                name="无力", value=new_stacks, remaining_rounds=1, source="寒冰法力"))

    def _shouyedeng_pending_grant(self, entity: Optional[Entity]) -> int:
        """守夜灯：[敌回始]将授予的法力量（0=本次不会授予）。

        判定条件与 _grant_shouyedeng 完全一致但不实际发放。怪物阶段的法术
        静态校验发生在[敌回始]守夜灯发放之前（执行阶段才授予），因此校验
        反应法术预算时必须预计算这笔法力，否则当前法力=0 时先发制人等
        合法反应法术会被误判为「法力不足」（2026-08-21 实战确认）。
        """
        if entity is None or not entity.is_alive or entity.entity_type != "轮回者":
            return 0
        if not self.state.side_has(entity, "守夜灯"):
            return 0
        if entity is self.state.player and self.state.sealed_relics.get("守夜灯", 0) > 0:
            return 0
        if getattr(entity, "_shouyedeng_granted", 0):
            return 0
        return math.ceil(entity.mana_limit / 2)

    def _grant_shouyedeng(self, entity: Optional[Entity]) -> Optional[dict]:
        """守夜灯：[敌回始]获得法限50%法力，每回合一次。"""
        gained = self._shouyedeng_pending_grant(entity)
        if gained <= 0:
            return None
        entity.current_mana += gained
        self.clamp_immortal_body(entity)
        entity._shouyedeng_granted = gained
        return {"type": "shouyedeng_grant", "entity": entity.name, "gained": gained}

    def _clear_shouyedeng(self, entity: Optional[Entity]) -> Optional[dict]:
        """守夜灯：该法力[敌回终]清空（只扣本回合授予量）。"""
        if entity is None:
            return None
        granted = getattr(entity, "_shouyedeng_granted", 0) or 0
        entity._shouyedeng_granted = 0
        if granted <= 0:
            return None
        before = entity.current_mana
        entity.current_mana = max(0, entity.current_mana - granted)
        return {"type": "shouyedeng_clear", "entity": entity.name,
                "cleared": before - entity.current_mana, "granted": granted}

    def _jieli_boost(self, dealer: Entity, amount: int) -> int:
        if amount <= 0 or not dealer.has_status("借力"):
            return amount
        return math.ceil(amount * (1 + 10 * dealer.get_status_value("借力") / 100))

    def _find_named(self, name: str) -> Optional[Entity]:
        for e in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            if e.name == name:
                return e
        return None

    def single_round_action_count(self, entity: Entity) -> int:
        if entity is None:
            return 0
        if entity.entity_type == "怪物":
            n = 2
            act = self._monster_activated.get(id(entity), set())
            # 疯狂(2026-08-17全局裁定)：状态盖到所有角色，怪物从自身状态读+X。
            # 不再走激活集合分支，避免与全局状态双重计数。
            n += entity.get_status_value("疯狂")
            if "狂暴" in act or entity.has_status("狂暴"):
                n += 1
            n -= entity.get_status_value("无力")
            return max(0, n)
        return DaoWenEngine.single_round_action_count(entity)

    def _current_context_timing(self) -> str:
        if getattr(self.state, "phase", "") == "pre_battle":
            return "pre_battle"
        sub = getattr(self.state, "combat_subphase", "") or ""
        return sub or getattr(self.state, "phase", "") or "unknown"

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
        return record

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
        # 离场（雕塑/癌变/还债/救赎/封印/逃跑）不是命零，绝不在此处宣布死亡。
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
        # 死因优先级：离场原因 > 调用方显式给出的死亡上下文 subtype > 兜底 hp_zero。
        # （【崩解】【凡庸】【尸爆】等特殊死因靠这一步才能留在 _death_ctx 里。）
        subtype = getattr(entity, "departure_reason", "")
        if not subtype and parent is not None and parent.mechanic == "death" and parent.subtype:
            subtype = parent.subtype
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
            tags=(set(parent.tags) if parent else {"legacy_context"}),
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
            cause=death_ctx.subtype,
        )
        if entity.entity_type == "怪物":
            # 乱葬岗·招魂：记录本场已命零怪物尸体
            if not getattr(self.state, "dead_monsters", None):
                self.state.dead_monsters = []
            if entity not in self.state.dead_monsters:
                self.state.dead_monsters.append(entity)
        # 乱葬岗·分裂：[命零]创造X个复制体（血限20%，无分裂道纹）；缄默时全场命零效果被禁
        if getattr(self.state, "_pending_split_clones", 0) > 0:
            silenced = any(e.has_status("缄默") for e in self.state.get_all_player_side()
                           + self.state.get_all_enemy_side())
            if not silenced and entity.entity_type != "怪物":
                clones = self.state._pending_split_clones
                base_hp = max(1, math.ceil(entity.blood_limit * 20 / 100))
                for i in range(clones):
                    clone = Entity(name=f"{entity.name}·裂{i + 1}", entity_type="临时朋友",
                                   blood_limit=base_hp, current_hp=base_hp,
                                   attack_count=max(0, entity.attack_count),
                                   attack_power=entity.attack_power)
                    for dw_name, dw_inst in entity.dao_wen.items():
                        if dw_name == "分裂":
                            continue  # 复制体无分裂道纹
                        clone.dao_wen[dw_name] = dw_inst
                    self._bind_hp_hook(clone)
                    self.state.temp_friends.append(clone)
                self.state._pending_split_clones = 0
                self._split_clones_spawned = clones
            else:
                self.state._pending_split_clones = 0

    # ---- F2 全量：罪孽/扭曲专属道纹的公共辅助 ----
    def _shards_of(self, entity: Entity) -> int:
        """实体可失去/被夺取的碎片量；假碎片优先，负债不抵消仍可支付的假碎片。"""
        if entity is self.state.player:
            return self.state.fake_shards + max(0, self.state.shards)
        return entity.fake_shards + max(0, entity.shards)

    def _lose_shards_of(self, entity: Entity, amount: int) -> int:
        """实体失去碎片（假碎片优先，玩家走 state.lose_shards）。返回实际失去的真碎片数。"""
        if entity is self.state.player:
            return self.state.lose_shards(amount)
        return entity.lose_shards(amount)

    def _raw_hp_loss(
        self, entity: Entity, amount: int,
        ctx: Optional[EffectContext | dict] = None,
    ) -> dict:
        """直接生命损失（绕过格挡；爆裂反射/赌命用），计入失血追踪，含命零判定。"""
        before = entity.current_hp
        reaction_logs: list = []
        # 「失去生命前」自动反应窗口：非攻击直接失血（爆裂/赌命等），生命尚未扣减。
        if amount > 0 and entity.is_alive and self._attack_after_window_target is not entity:
            reaction_logs = self._fire_before_life_lost(entity, ctx) or []
        self._hp_loss_recording += 1  # 直接失血由 _record_hp_loss_event 接管，抑制兜底钩子
        try:
            entity.current_hp = max(0, entity.current_hp - max(0, amount))
        finally:
            self._hp_loss_recording -= 1
        lost = before - entity.current_hp
        entity.hp_lost_this_round += lost
        parent = normalize_context(ctx)
        hp_loss_ctx = self._record_hp_loss_event(entity, lost, parent, subtype="raw")
        # died 的口径与重构前保持一致：只看生命是否归零（已命零者仍报 True）；
        # 真正的死亡通知交给统一入口，重复通知由 _on_entity_death 幂等吸收。
        died = entity.current_hp <= 0
        if died:
            self._check_hp_zero_death(entity, ctx=hp_loss_ctx or parent)
        result = {"hp_before": before, "hp_after": entity.current_hp, "lost": lost, "died": died}
        if hp_loss_ctx:
            result["hp_loss_ctx"] = hp_loss_ctx
        if reaction_logs:
            result["reaction_logs"] = reaction_logs
        if ctx is None and lost > 0:
            result["context_warning"] = "直接失去生命缺少EffectContext；已按legacy来源兼容记录"
        return result

    def _seal_one_relic(self, target: Entity, rounds: int) -> str:
        """抵扣X：封印目标拥有的一件遗物，持续X回合。返回被封印的遗物名；目标无遗物返回\"\"。"""
        if target is self.state.player:
            holder = self.state
            owned = [r.name for r in self.state.relics]
        else:
            # 引擎中怪物/同伴无 relic 字段 → 视为不拥有遗物，封印无效果
            holder = target
            owned = []
        # 目标无遗物 → 无效果
        if not owned:
            return ""
        # 封印第一件未在封印中的遗物；若全部已封印则延长第一件的剩余回合
        for rname in owned:
            if holder.sealed_relics.get(rname, 0) <= 0:
                holder.sealed_relics[rname] = max(1, rounds)
                return rname
        first = owned[0]
        holder.sealed_relics[first] = max(holder.sealed_relics.get(first, 0), rounds)
        return first

    def _xijie_steal(self, caster: Entity, target: Entity, damage_amount: int) -> int:
        """洗劫X：造成伤害时夺取[目标]等量[碎片]（假碎片优先由目标侧扣减；夺取量=min(目标碎片,伤害)）"""
        if damage_amount <= 0 or target is caster or not caster.has_status("洗劫"):
            return 0
        avail = self._shards_of(target)
        if avail <= 0:
            return 0  # 若[目标]没有[碎片]则夺取无效
        steal = min(avail, damage_amount)
        self._lose_shards_of(target, steal)
        if caster is self.state.player:
            self.state.shards += steal
        else:
            caster.shards += steal
        return steal

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

    def resolve_attack(
        self,
        attacker: Entity,
        target: Entity,
        hit_index: int = 0,
        is_must_hit: bool = False,
        dodge: bool = False,
        blood_shadow: bool = False,
        spell_choices: Optional[dict] = None,
        entity_refs: Optional[dict[str, Entity]] = None,
        dodge_relic_target_ref: Optional[str] = None,
        cost_share_target_ref: str = "",
    ) -> dict:
        """
        解析一次攻击
        dodge: 目标是否选择闪避（由AI决策）
        blood_shadow: 目标是否选择用【血影】遗物(流血10取消本次判定)代替常规闪避
        """
        entity_refs = entity_refs or self._combat_entity_refs()
        spell_choices = spell_choices if spell_choices is not None else {"before": {}, "after": {}}
        # 攻击失血由本方法的反应窗口结算，标记在下方置位。先清掉可能残留的旧标记，
        # 避免异常中断后误伤后续失血的自动触发。
        self._attack_after_window_target = None
        self.validate_spell_reaction_submission(target, attacker, spell_choices, entity_refs)
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

        # 必中：显式必中，或消耗一次「下X次选择[目标]」余数
        must_hit = is_must_hit or self.consume_bizhong(attacker)

        # 血影（初拥之夜遗物，仅玩家自身持有）：非必中判定下，可流血10取消本次判定，是常规闪避外的另一选项
        if (blood_shadow and not must_hit and self.state.side_has(target, "血影")):
            self.pay_numeric_cost(
                target, "流血", 10,
                cost_share_target_ref=cost_share_target_ref,
                cost_context={"timing": "reaction", "source": "血影", "source_type": "relic", "tags": {"active_payment"}})
            result["blood_shadow_success"] = True
            result["note"] = "血影：流血10，本次判定被取消"
            return result

        # 闪避判定
        if dodge:
            if must_hit:
                result["dodge_success"] = False
                result["dodge_fail_reason"] = "必中攻击无法闪避"
            elif target.current_speed >= 1:
                extra = self._spend_dodge_speed(target, dodge_relic_target_ref)
                result["dodge_success"] = True
                result["speed_after_dodge"] = target.current_speed
                if extra:
                    result["dodge_extra"] = extra
                # 「闪避时」开放可扩展触发：持有者成功闪避后触发（注册/接线见
                # spell_dsl.EXTRA_TRIGGERS 与 _fire_auto_reaction）。此分支位于
                # _attack_after_window_target 置位之前，不会与普攻伤害窗口冲突。
                result["spell_logs"] = self._fire_auto_reaction(
                    target, "闪避时", make_context(
                        timing=self._current_context_timing(), source="普通攻击",
                        source_type="attack", actor=attacker, target=target,
                        mechanic="dodge", subtype="dodge_success", amount=0,
                        tags={"attack", "dodge"}, parent_event_id=None))
                return result
            else:
                result["dodge_success"] = False
                result["dodge_fail_reason"] = "速度不足"
        
        # 伤害结算
        damage = attacker.attack_power
        # 逆鳞（F2）：下次伤害+全部层数后清空
        if hasattr(attacker, "_nilin") and getattr(attacker, "_nilin", 0) > 0:
            bonus = attacker._nilin
            damage += bonus
            result["nilin_bonus"] = bonus
            attacker._nilin = 0
        damage = self._jieli_boost(attacker, damage)
        if attacker.has_status("坠落"):
            damage = math.ceil(damage / 2)
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
        # 震岳龙躯（真龙之心遗物）：激活期间，自身受到超出15点的伤害无效
        if self.state.side_body_shield(target) > 0:
            damage = min(damage, 15)
        # 本次攻击造成的失血由 resolve_attack 的既有反应窗口结算；标记该目标，
        # 使失血后 hook 不再对同一目标重复触发。
        self._attack_after_window_target = target
        # 反应法术由攻击prepare列出、resolve显式提交；计算层不再自动选择X或目标。
        if damage > 0:
            slogs = self._resolve_spell_reactions(
                ActionPhase.BEFORE_DAMAGE_TAKEN.value, target, attacker,
                spell_choices["before"], entity_refs,
            )
            if slogs:
                result["spell_logs"] = slogs
            if not attacker.is_alive:
                damage = 0
        # 失去生命前：受到伤害前反应已结算完毕、伤害数值已最终确定，但生命尚未
        # 真正扣减——这是"失去生命前"与"受到伤害前"语义区分之处（前者关心
        # "即将失去多少生命"，后者关心"即将挨这一下打"）。复用同一套反应法术
        # 流水线，只是挂接点更靠近扣血这一刻。
        if damage > 0 and target.is_alive:
            slogs_life_before = self._resolve_spell_reactions(
                ActionPhase.BEFORE_LIFE_LOST.value, target, attacker,
                spell_choices.get("life_before", {}), entity_refs,
            )
            if slogs_life_before:
                result.setdefault("spell_logs", []).extend(slogs_life_before)
            if not attacker.is_alive:
                damage = 0
        # 裂变：受到伤害分X次结算；依全局整数规则，每次除法向上取整。
        if target.has_status("裂变") and damage > 0:
            xv = target.get_status_value("裂变") or 1
            if xv > 1:
                per = math.ceil(damage / xv)
                ta = ts = 0; died = False
                for _ in range(xv):
                    dr = self._apply_hostile_damage(
                        target, per, "普通" if not ignore_shield else "无视格挡", attacker,
                        ctx={"timing": self._current_context_timing(), "source": "普通攻击", "source_type": "attack",
                             "actor": attacker, "target": target, "mechanic": "damage", "subtype": "attack",
                             "amount": per, "tags": {"attack", "split_hit"}})
                    ta += dr["actual_damage"]; ts += dr["shield_absorbed"]; died = died or dr["died"]
                damage_result = {"actual_damage": ta, "shield_absorbed": ts, "hp_after": target.current_hp, "died": died, "split": xv}
            else:
                damage_result = self._apply_hostile_damage(
                    target, damage, "普通" if not ignore_shield else "无视格挡", attacker,
                    ctx={"timing": self._current_context_timing(), "source": "普通攻击", "source_type": "attack",
                         "actor": attacker, "target": target, "mechanic": "damage", "subtype": "attack",
                         "amount": damage, "tags": {"attack"}})
        else:
            damage_result = self._apply_hostile_damage(
                target, damage, "普通" if not ignore_shield else "无视格挡", attacker,
                ctx={"timing": self._current_context_timing(), "source": "普通攻击", "source_type": "attack",
                     "actor": attacker, "target": target, "mechanic": "damage", "subtype": "attack",
                     "amount": damage, "tags": {"attack"}})
        result["damage_dealt"] = damage_result["actual_damage"]
        result["shield_absorbed"] = damage_result["shield_absorbed"]
        result["hp_lost"] = damage_result["actual_damage"]
        result["target_died"] = damage_result["died"]
        result["target_hp_after"] = damage_result["hp_after"]
        # 撤退：朋友/员工即将命零时自动撤退（伤害清零、保留生命、退出本场），透传给战报渲染
        if damage_result.get("retreated"):
            result["retreated"] = True
        if damage_result["actual_damage"] > 0:
            attacker.damage_dealt_this_round += damage_result["actual_damage"]
        if "split" in damage_result:
            result["split"] = damage_result["split"]
        # 受到伤害后：这一击已经完整落地（格挡/固执/伤害减免均已算完），与
        # "受到伤害前"对称的挂接点。判定用damage（这一击最终确定的伤害数值，
        # 落地前就已确定，不受格挡是否吸收影响）而不是actual_damage——
        # 即使格挡把伤害全部吸收，也应算"受到了一次伤害"，只是生命没有
        # 实际减少（与"失去生命后"要求actual_damage>0严格区分）。
        if damage > 0 and target.is_alive:
            slogs_damage_after = self._resolve_spell_reactions(
                ActionPhase.AFTER_DAMAGE_TAKEN.value, target, attacker,
                spell_choices.get("damage_after", {}), entity_refs,
            )
            if slogs_damage_after:
                result.setdefault("spell_logs", []).extend(slogs_damage_after)
        if damage_result["actual_damage"] > 0 and target.is_alive:
            slogs2 = self._resolve_spell_reactions(
                ActionPhase.AFTER_LIFE_LOST.value, target, attacker,
                spell_choices["after"], entity_refs,
            )
            if slogs2:
                result.setdefault("spell_logs", []).extend(slogs2)
        self._attack_after_window_target = None
        
        # 结算后效果
        # 兴奋：每次出手后速度+1（X 只管持续）
        if attacker.has_status("兴奋"):
            result["speed_boost_from_excitement"] = self._gain_speed(attacker, 1, ctx={
                "timing": self._current_context_timing(), "source": "兴奋", "source_type": "daowen",
                "actor": attacker, "target": attacker, "mechanic": "speed_change", "subtype": "current_speed",
                "amount": 1, "tags": {"daowen", "action_followup"},
            })

        return result

    # ========== 回合管理 ==========

    def round_start(self, relic_choices: Optional[dict] = None) -> dict:
        """
        回始结算
        1. 拥有者获得等同当前法限的法力（加法，从不赋值到法限）
        2. 结算回始类效果
        3. 返回需要决策的信息
        """
        effects = []
        player = self.state.player
        if player is not None:
            if self.state.current_round == 0:
                shield = self.state.event_modifiers.pop("next_battle_first_round_shield", 0)
                if shield:
                    player.gain_shield(shield)
                    effects.append({"type": "event_first_round_shield", "amount": shield})
            leather = self.state.event_modifiers.pop("leather_shield_next", 0)
            if leather and self.state.side_has(player, "皮衣"):
                player.gain_shield(leather)
                effects.append({"type": "leather_shield", "amount": leather})
        
        # 活血追踪归零 + 出手预算归零（回始重置本回合已用出手次数）+ 血誓戒每回合限一次归零 + 血族血脉判定归零
        for e in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            e.hp_lost_this_round = 0
            if hasattr(e, "_hp_loss_events"):
                e._hp_loss_events = []
            if hasattr(e, "_speed_change_events"):
                e._speed_change_events = []
            if hasattr(e, "_blood_limit_events"):
                e._blood_limit_events = []
            e.actions_used_this_round = 0
            e.blood_oath_used_this_round = False
            e.mana_inflicted_this_round = 0
            e.damage_dealt_this_round = 0
        # 回始：每个轮回者获得等同当前法限的法力。战始已清零；折速法印在战始+=；血契在本段之后+=。
        # 守夜灯按[敌回始]授予，不在回始叠加。
        # 死斗里封存对手也是轮回者，必须同样获得法力，否则只能普攻1点。
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            if entity.entity_type != "轮回者" or not entity.is_alive:
                continue
            old_mana = entity.current_mana
            # 勾魂（持续X）：[回始]不获得法力（不扣已有法力，只是回填被压制）。
            if entity.has_status("勾魂"):
                effects.append({
                    "type": "mana_refill_blocked",
                    "entity": entity.name,
                    "by": "勾魂",
                    "from": old_mana,
                    "to": old_mana,
                    "gained": 0,
                })
                continue
            gained = entity.mana_limit
            entity.current_mana += gained
            self.clamp_immortal_body(entity)
            effects.append({
                "type": "mana_refill",
                "entity": entity.name,
                "from": old_mana,
                "to": entity.current_mana,
                "gained": gained,
            })

        # 遗物：回始触发（回锋刀按速限缺口造伤）。守夜灯改走[敌回始]。
        relic_logs = self.process_relics(TriggerTiming.ROUND_START, {"relic_choices": relic_choices or {}})
        effects.extend({"type": "relic", "log": l} for l in relic_logs)
        # 死斗对手的[敌回始]≈玩家行动开始：在回始法力之后授予守夜灯。
        for entity in self.state.enemies:
            granted = self._grant_shouyedeng(entity) if entity.entity_type == "轮回者" else None
            if granted:
                effects.append(granted)
        
        # 结算回始效果
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            # 机制系统：ROUND_START 相位分发。位置即原【自愈】结算位置（本循环第一项）。
            # round_start 只负责宣布时点，具体机制由声明层按 priority 执行；
            # 机制的报告条目并入 effects，战报格式与迁移前一致。
            effects.extend(self._dispatch_phase(Phase.ROUND_START, target=entity))

        # ---- F2：罪孽专属道纹 [回始] 结算（逼债/清算/赌命） ----
        # 逼债X：目标失去X碎片，无力支付的部分记为负债（碎片扣负，DM裁定D 2026-08-22，
        # 旧"否则失去2X血限"废止）。负债≥20触发【还债】（仅怪物，见 settle_victory_paths）；
        # 玩家被挂逼债无力支付时同样计负债——玩家负债不触发还债，但冻结一切
        # 碎片支出（假碎片仍可花，见 _shards_of 口径）。
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            for entry in list(getattr(entity, "_bizhai", [])):
                x = entry["x"]
                if self._shards_of(entity) >= x:
                    self._lose_shards_of(entity, x)
                    effects.append({"type": "bizhai", "entity": entity.name, "lost_shards": x})
                else:
                    if entity is self.state.player:
                        use_fake = min(self.state.fake_shards, x)
                        self.state.fake_shards -= use_fake
                        self.state.shards -= (x - use_fake)
                        now = self.state.shards
                    else:
                        use_fake = min(entity.fake_shards, x)
                        entity.fake_shards -= use_fake
                        entity.shards -= (x - use_fake)
                        now = entity.shards
                    effects.append({"type": "bizhai_debt", "entity": entity.name,
                                    "obligation": x, "shards_now": now,
                                    "debt_now": max(0, -now)})
        # 清算X：目标失去[你碎片]点格挡（你=施法者当前碎片，每回始读取）
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            for entry in list(getattr(entity, "_qingsuan", [])):
                caster = entry["caster"]
                drain = max(0, self._shards_of(caster))
                lost = min(entity.shield, drain)
                entity.shield -= lost
                effects.append({"type": "qingsuan", "entity": entity.name, "lost_shield": lost, "drain": drain})
        # 赌命X：按场上存活角色从轮回者方开始发放数字，投随机数，对应目标失去30%当前生命
        duming_holders = [e for e in self.state.get_all_player_side() + self.state.get_all_enemy_side()
                          if e.is_alive and e.has_status("赌命")]
        for holder in duming_holders:
            alive = [e for e in self.state.get_all_player_side() + self.state.get_all_enemy_side() if e.is_alive]
            if len(alive) < 1:
                continue
            roll = self.dice.auto_roll(f"赌命_r{self.state.current_round}", [e.name for e in alive],
                                       context=f"{holder.name}发动赌命")
            idx = int(roll["player_number"]) - 1
            tgt = alive[min(max(idx, 0), len(alive) - 1)]
            d = math.ceil(tgt.blood_limit * 30 / 100)  # 用户裁定口径：血限30%
            rd = self._raw_hp_loss(tgt, d, ctx={
                "timing": "round_start", "source": "赌命", "source_type": "daowen",
                "actor": holder, "target": tgt, "mechanic": "hp_loss", "subtype": "percent",
                "amount": d, "tags": {"daowen", "round_start"},
            })
            effects.append({"type": "duming", "caster": holder.name, "target": tgt.name,
                            "roll": idx + 1, "of": len(alive), "damage": rd["lost"], **rd})

        self.state.current_round += 1
        
        return {
            "round": self.state.current_round,
            "phase": "回始",
            "effects": effects,
            "state": self._get_combat_state()
        }
    
    def round_end(self, blood_lineage_cost_share_target_ref: str = "") -> dict:
        """
        回终结算
        1. 回终类效果结算
        2. 格挡清空
        3. 持续X剩余回合-1
        """
        effects = []
        mediocrity_ready: list[tuple[Entity, str]] = []

        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            # 机制系统：ROUND_END 相位分发。锚定语义：回终第一循环顶部、凡庸 tick 之前
            # （原畸变·结算位置）。凡庸之后的回终机制禁止注册到 ROUND_END
            # （会改变既有顺序），详见机制迁移台账。
            effects.extend(self._dispatch_phase(Phase.ROUND_END, target=entity))
            # 凡庸只在此拍更新计数；达阈值者稍后按「非轮回者优先」结算。
            if entity.is_alive:
                why = self._tick_mediocrity_counters(entity)
                if why:
                    mediocrity_ready.append((entity, why))

        # 多个角色同时触发凡庸时，非轮回者优先；同档保持原遍历顺序。
        # DM裁定（2026-08-18）：「非轮回者优先」的意义在于——按优先序逐个结算，
        # 一旦先炸裂的角色清空了某一方战场（战斗胜负因此已定），立即中断剩余凡庸结算；
        # 幸存侧尚未结算的待爆者不再炸裂（其计数随战斗结束清零）。
        # 注意：只有本拍凡庸「炸出来」的清空才中断；战场在结算开始前就已空置
        # （如战斗尚未开始的空场脚手架）不援引此裁定。
        mediocrity_ready.sort(key=lambda item: item[0].entity_type == "轮回者")
        decided_before_tick = self._mediocrity_battle_decided()
        for entity, why in mediocrity_ready:
            if not decided_before_tick and self._mediocrity_battle_decided():
                entity.no_action_rounds = 0
                entity.no_damage_rounds = 0
                effects.append({
                    "type": "mediocrity_interrupted", "entity": entity.name,
                    "note": f"{why}，但战场已因先前的【凡庸】清空、战斗结束：剩余凡庸中断结算"})
                continue
            effects.extend(self._apply_mediocrity(entity, why))

        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            # 血族血脉：持有者这一侧各自结算（死斗两边各一份）
            if entity.entity_type == "轮回者" and self.state.side_has(entity, "血族血脉"):
                if entity.damage_dealt_this_round > 0:
                    heal_detail = self.state.apply_heal(entity, entity.damage_dealt_this_round, ctx={
                        "timing": "round_end", "source": "血族血脉", "source_type": "relic",
                        "actor": entity, "target": entity, "owner": entity,
                        "mechanic": "heal", "subtype": "blood_lineage", "amount": entity.damage_dealt_this_round,
                        "tags": {"relic", "round_end"},
                    })
                    effects.append({"type": "blood_lineage_heal", "entity": entity.name,
                                     "amount": heal_detail["actual_heal"],
                                     "heal_ctx": heal_detail.get("heal_ctx")})
                else:
                    payment = self.pay_numeric_cost(
                        entity, "流血", 20,
                        cost_share_target_ref=(blood_lineage_cost_share_target_ref
                                               if entity is self.state.player else ""),
                        cost_context={"timing": "round_end", "source": "血族血脉", "source_type": "relic", "tags": {"automatic"}})
                    effects.append({"type": "blood_lineage_bleed", "entity": entity.name,
                                     "cost": payment, "amount": payment["actual_paid"]})

            # 赤族诅咒：[回终]固定流血20
            # 注意：这里刻意**不**改走 pay_numeric_cost——那会额外触发血誓戒/烙痕钉/血契，属于改规则。
            # 本次只补齐来源上下文与统一死亡判定，数值口径不动。
            if entity.entity_type == "赤族" and entity.is_alive:
                curse_ctx = make_context(
                    timing="round_end", source="赤族诅咒", source_type="bloodline",
                    actor=entity, target=entity, owner=entity,
                    mechanic="cost", subtype="bleed", amount=20,
                    tags={"bloodline", "round_end", "automatic"})
                bleed_detail = entity.take_damage(20, "代价")
                loss_ctx = self._record_hp_loss_event(
                    entity, bleed_detail.get("actual_damage", 0), curse_ctx, subtype="cost")
                if bleed_detail.get("died"):
                    self._check_hp_zero_death(entity, ctx=loss_ctx or curse_ctx)
                effects.append({"type": "chizu_curse_bleed", "entity": entity.name,
                                 "amount": bleed_detail["actual_damage"], "died": bleed_detail["died"]})

            # 格挡清空
            if entity.shield > 0:
                effects.append({
                    "type": "shield_clear",
                    "entity": entity.name,
                    "cleared": entity.shield
                })
                entity.clear_shield()
            
        # 法力清空（敌回终）
        # 规则：[法限]用于发动道纹与法术，法力[敌回终]清空。
        # 死斗双方都是轮回者，必须与回始同一套循环：每个存活轮回者各自清空。
        # 朋友/员工/怪物没有法限，不走这条。
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            if entity.entity_type != "轮回者" or not entity.is_alive:
                continue
            if entity.current_mana > 0:
                effects.append({
                    "type": "mana_clear",
                    "entity": entity.name,
                    "cleared": entity.current_mana
                })
                entity.current_mana = 0
        
        # 持续效果递减。爆裂按[敌回终]：己方身上在此拍；敌方身上改在怪物回合开始时减。
        player_side_ids = {id(e) for e in self.state.get_all_player_side()}
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            skip = ("爆裂",) if id(entity) not in player_side_ids else ()
            expired = entity.tick_status_effects(skip_names=skip)
            if expired:
                # 只有“持续期间直接改写面板”的效果到期即还原；畸变/伤痕/逼债等
                # 已经产生的累计局内后果保留到战终，再由battle作用域统一回滚。
                panel_modifier_sources = {"强化", "弱化", "僵化"}
                rolled_back = self.state.rollback_scoped_sources(
                    entity, set(expired) & panel_modifier_sources)
                effects.append({
                    "type": "status_expired",
                    "entity": entity.name,
                    "expired_effects": expired,
                    "rolled_back_deltas": rolled_back,
                })
                # 逆鳞（F2）：状态到期时清空计层
                if "逆鳞" in expired and hasattr(entity, "_nilin"):
                    entity._nilin = 0
                # 嫁祸到期时清理计数（若未因次数耗尽）
                if "嫁祸" in expired and hasattr(entity, "_jiahuo_left"):
                    entity._jiahuo_left = 0
                    if hasattr(entity, "_jiahuo_target"):
                        delattr(entity, "_jiahuo_target")
                if ("飞行" in expired or "滑翔" in expired) and not self._is_flying(entity):
                    entity.is_flying = False
                if "变形" in expired and hasattr(entity, "_bianxing_original"):
                    entity.attack_power, entity.attack_count = entity._bianxing_original
                    delattr(entity, "_bianxing_original")
                    effects.append({"type": "bianxing_restore", "entity": entity.name,
                                    "attack_power": entity.attack_power,
                                    "attack_count": entity.attack_count})
                # 干扰/手雷减攻到期自动由 tick 清理，无需额外
            # F2：逼债/清算状态消失即清账（∞/持续X到期后不再逐回始结算）
            if not entity.has_status("逼债") and getattr(entity, "_bizhai", None):
                entity._bizhai = []
            if not entity.has_status("清算") and getattr(entity, "_qingsuan", None):
                entity._qingsuan = []
            # F2：抵扣封印回合递减，归零解封
            for rname in list(getattr(entity, "sealed_relics", {}).keys()):
                entity.sealed_relics[rname] -= 1
                if entity.sealed_relics[rname] <= 0:
                    del entity.sealed_relics[rname]
        # 玩家侧抵扣封印回合递减（state.sealed_relics）
        for rname in list(self.state.sealed_relics.keys()):
            self.state.sealed_relics[rname] -= 1
            if self.state.sealed_relics[rname] <= 0:
                del self.state.sealed_relics[rname]

        # 震岳龙躯：两边各自递减
        if self.state.dragon_body_shield_rounds > 0:
            self.state.dragon_body_shield_rounds -= 1
            effects.append({"type": "dragon_body_tick", "side": "player",
                            "remaining": self.state.dragon_body_shield_rounds})
        if self.state.opponent_dragon_body_shield_rounds > 0:
            self.state.opponent_dragon_body_shield_rounds -= 1
            effects.append({"type": "dragon_body_tick", "side": "opponent",
                            "remaining": self.state.opponent_dragon_body_shield_rounds})

        # 皮衣记录本回合实际失去生命；优先使用 HP loss 事件，兼容未迁移旧路径。
        if self.state.player and self.state.side_has(self.state.player, "皮衣"):
            hp_loss_events = getattr(self.state.player, "_hp_loss_events", []) or []
            event_loss = sum(int(e.get("amount") or 0) for e in hp_loss_events)
            leather_loss = event_loss if event_loss > 0 else self.state.player.hp_lost_this_round
            if leather_loss > 0:
                self.state.event_modifiers["leather_shield_next"] = leather_loss

        # 活血：有活血状态的实体，回终按本回合累计失血÷2回复
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            if entity.has_status("活血") and entity.hp_lost_this_round >= 2:
                heal_n = entity.hp_lost_this_round // 2
                h = self.state.apply_heal(entity, heal_n, ctx={
                    "timing": "round_end", "source": "活血", "source_type": "daowen",
                    "actor": entity, "target": entity, "owner": entity,
                    "mechanic": "heal", "subtype": "huoxue", "amount": heal_n,
                    "tags": {"daowen", "round_end"},
                })
                effects.append({"type": "huoxue_heal", "entity": entity.name,
                                "heal": heal_n, "actual": h["actual_heal"],
                                "heal_ctx": h.get("heal_ctx")})
            entity.hp_lost_this_round = 0
            if hasattr(entity, "_hp_loss_events"):
                entity._hp_loss_events = []
            if hasattr(entity, "_speed_change_events"):
                entity._speed_change_events = []
            if hasattr(entity, "_blood_limit_events"):
                entity._blood_limit_events = []

        # 手术·强制移植：本场第三回终仍保持原移植道纹时转为怪物。
        if self.state.current_round >= 3:
            for entity in list(self.state.friends + self.state.employees):
                transplanted = getattr(entity, "_transplanted_daowen", "")
                if transplanted and transplanted in entity.dao_wen:
                    if entity in self.state.friends: self.state.friends.remove(entity)
                    if entity in self.state.employees: self.state.employees.remove(entity)
                    entity.entity_type = "怪物"; entity.is_deployed = True
                    self.state.enemies.append(entity)
                    effects.append({"type": "transplant_monster", "entity": entity.name,
                                    "daowen": transplanted})

        # 多路径胜利结算（雕塑/癌变/还债）
        settled = self.settle_victory_paths()
        if settled:
            effects.extend(settled)

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
        被特殊手段破解主要优势时，均应立即进行困境检查
        
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
        
        # 困境探针（裁定⑦ 2026-08-10）：≥1个劣势信号即判定困境
        # （原口径≥2，导致进化在模拟策略下结构性不可达：4574场埋点15314次检查，信号分布{0:14267,1:1047,≥2:0}）
        if len(difficulty_signals) >= 1:
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
        2. 逃跑方必须消耗自身出手企图拖延时间逃跑
        3. 追击方阻截成功后逃跑失败
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
                f"规则：{escaper.name}必须消耗自身出手企图拖延时间逃跑。\n"
                f"追击方在正常回合内一边抵御其余敌对角色攻击，一边阻截逃跑方；阻截成功后逃跑失败继续战斗，否则逃脱成功。\n"
                f"请DM裁定{escaper.name}的拖延阻截是否成功。"
            ),
            options=[
                {"id": "escape_success", "label": "逃脱成功", "description": "拖延有效，逃脱成功"},
                {"id": "escape_fail", "label": "逃脱失败", "description": "被阻截，继续战斗"},
            ],
            state_snapshot=self.state.to_dict()
        )
    
    # ========== 进化（原初X，引擎直接结算，无需DM中断） ==========
    
    def execute_evolution(self, monster: Entity, daowen_name: str, x: int) -> dict:
        """
        特殊事件【进化】：怪物发动【原初X】（README·特殊事件）。
        原初X：代价：异变5X。选择一种**当前轮回者已持有**、且自身未持有的道纹，
        [战终]前视为持有该道纹（其数值固定为本次X），借用的道纹发动时照常支付其自身代价。

        设计意图：借用对象改为"轮回者的道纹"而非固定的7种原始怪物道纹，
        使玩家的构筑本身成为风险来源——越依赖某条公式化路线，被复制时反噬越重，
        从而抑制"无脑最优解"。同时保持怪物与轮回者的身份区隔
        （怪物仍不持有法力/速度/残韵/局外阶段，只是临时借用道纹）。
        前置（怪物准则#3）：须处于困境；逃跑与进化二选一，每场战斗限一次。
        """
        if not monster.is_alive:
            return {"success": False, "error": f"{monster.name}已命零"}
        if id(monster) in self._monster_evolved:
            return {"success": False, "error": f"{monster.name}本场已选择过逃跑/进化（每场战斗限一次）"}
        difficulty = self.check_monster_difficulty(monster)
        if not difficulty:
            return {"success": False, "error": f"{monster.name}未陷入困境，不能进化"}
        player = self.state.player
        player_daowen = list(player.dao_wen.keys()) if player else []
        if daowen_name not in player_daowen:
            return {"success": False,
                    "error": f"【{daowen_name}】不在轮回者当前持有的道纹中，"
                             f"原初X只能借用：{'、'.join(player_daowen) if player_daowen else '（轮回者无道纹）'}"}
        if daowen_name in monster.dao_wen:
            return {"success": False, "error": f"{monster.name}已持有【{daowen_name}】，原初X只能借用自身未持有的原始怪物道纹"}
        if not isinstance(x, int) or isinstance(x, bool) or x < 1:
            return {"success": False, "error": "X必须为≥1的整数"}
        
        # 支付代价：异变5X（代价从做出选择开始生效，优先于效果结算）
        cost = self.YUANCHU_COST_RATE * x
        pay = monster.add_mutation(cost)
        self._monster_evolved.add(id(monster))
        log = [f"{monster.name}发动【原初{x}】：异变+{cost}（当前{pay['mutation_total']}层）"]
        
        if pay["collapsed"]:
            self._on_entity_death(monster, ctx=self._collapse_context(monster, {
                "timing": self._current_context_timing(), "source": f"原初{x}",
                "source_type": "evolution", "actor": monster, "target": monster,
                "mechanic": "cost", "subtype": "mutation", "amount": cost,
                "tags": {"evolution", "active_payment"}}))
            log.append(f"异变达到{pay['mutation_total']}层，触发【崩解】：{monster.name}直接命零，进化效果中断")
            return {"success": True, "action": "进化·原初X", "collapsed": True,
                    "log": log, "mutation": pay,
                    "state": self._get_combat_state()}
        
        # 借用：战终前视为持有（enemies于[战终]清空，借用自动到期）
        borrowed = DaoWen(
            name=daowen_name,
            formula=f"{daowen_name}X",
            cost_type="代价",
            cost_formula="异变5X",
            effect_formula="",
            is_monster_original=True,
            tags=["原初借用"],
        )
        monster.dao_wen[daowen_name] = DaoWenInstance(dao_wen=borrowed, x_value=x)
        log.append(f"{monster.name}[战终]前视为持有【{daowen_name}{x}】，发动时照常支付其自身代价")
        return {"success": True, "action": "进化·原初X", "collapsed": False,
                "borrowed": {"name": daowen_name, "x": x},
                "difficulty_signals": difficulty.get("signals", []),
                "log": log, "mutation": pay,
                "state": self._get_combat_state()}
    
    def get_plight_evolution_options(self) -> list[dict]:
        """
        供AI决策（事实源计算）：当前存活、处于困境、且本场未选择过逃跑/进化的怪物，
        及其【原初X】可用参数。怪物准则#3：陷入困境时强制逃跑/进化二选一，每场限一次；
        AI扮演怪物方，自行决定是否调用 declare_evolution 及参数。
        """
        options = []
        for m in self.state.enemies:
            if not m.is_alive or id(m) in self._monster_evolved:
                continue
            difficulty = self.check_monster_difficulty(m)
            if not difficulty:
                continue
            # 异变预算：门票异变5X后若达到阈值则触发【崩解】直接命零、借用中断。
            # max_x_by_mutation = 不崩解的最大X；超出属于合法但纯亏的自杀式选择，不禁止。
            max_x = max(0, (Entity.MUTATION_COLLAPSE_THRESHOLD - 1 - m.mutation_count) // self.YUANCHU_COST_RATE)
            options.append({
                "monster": m.name,
                "difficulty_signals": difficulty.get("signals", []),
                "mutation_layers": m.mutation_count,
                "max_x_by_mutation": max_x,
                # 借用池 = 轮回者当前持有、且该怪物尚未持有的道纹
                "borrowable_daowen": [d for d in (self.state.player.dao_wen if self.state.player else {})
                                      if d not in m.dao_wen],
            })
        return options
    
    # ========== 多路径胜利系统 ==========
    # 所有阈值数值均为占位初值，需经测试调整（见 AI_EXPERIENCE.md）

    PROLIFERATION_THRESHOLD = 2.0  # 癌变：README「累计恢复量达血限×2」；过量回复按原值计（双倍机制已删，DM裁定2026-08-18）
    CANCER_THRESHOLD = PROLIFERATION_THRESHOLD  # 别名：增生旧名已统一为癌变，二者同阈值
    DEBT_THRESHOLD = 20           # 还债：怪物负债达到20碎片时触发（DM裁定2026-08-22 由10上调）
    SCULPTURE_DAMAGE = 15         # 雕塑：每点耐久可造成的伤害
    SCULPTURE_SHIELD = 20         # 雕塑：每点耐久可获得的格挡

    def cancer_threshold_of(self, entity: Entity) -> int:
        """README：累计恢复量达到血限×2（过量按原值计入 total_healed，双倍机制已删）。"""
        if entity.blood_limit <= 0:
            return 0
        return math.ceil(entity.blood_limit * self.PROLIFERATION_THRESHOLD)

    def check_cancer(self, entity: Entity) -> Optional[dict]:
        """任一角色恢复量达阈值即癌变。怪物仍吸收进书；轮回者/同伴直接命零。"""
        if entity is None or not entity.is_alive or entity.is_proliferated:
            return None
        if self.state.side_has(entity, "第一杯"):
            return None
        threshold = self.cancer_threshold_of(entity)
        if threshold <= 0 or entity.total_healed < threshold:
            return None
        parent_heal = None
        heal_events = getattr(entity, "_heal_events", []) or []
        if heal_events:
            parent_heal = normalize_context(heal_events[-1])
        cancer_ctx = make_context(
            timing=parent_heal.timing if parent_heal else self._current_context_timing(),
            source="癌变", source_type="system", actor=None, target=entity, owner=None,
            mechanic="cancer", subtype="heal_threshold", amount=entity.total_healed,
            tags={"threshold", "heal_listener"},
            parent_event_id=parent_heal.event_id if parent_heal else None,
        )
        if entity.entity_type == "怪物":
            return self._proliferate_monster(entity, ctx=cancer_ctx)
        return self._cancer_character(entity, ctx=cancer_ctx)

    REDEMPTION_HP_RATIO = 0.10

    def monster_has_original_daowen(self, monster: Entity) -> bool:
        from .gamedata import ORIGINAL_MONSTER_DAOWEN
        return any(name in ORIGINAL_MONSTER_DAOWEN for name in monster.dao_wen)

    def redemption_hp_threshold(self, monster: Entity) -> int:
        if monster is None or monster.blood_limit <= 0:
            return 0
        return math.ceil(monster.blood_limit * self.REDEMPTION_HP_RATIO)

    def check_redemption(self, monster: Entity) -> Optional[dict]:
        """救赎：当前生命≤血限10%，且没有七种原始怪物道纹。"""
        if monster is None or monster.entity_type != "怪物" or not monster.is_alive:
            return None
        if monster.is_sculptured or monster.is_proliferated or monster.is_debt_bound:
            return None
        if getattr(monster, "removed_without_kill", False):
            return None
        if self.state.pending_redemption:
            return None
        if self.monster_has_original_daowen(monster):
            return None
        if monster.current_hp > self.redemption_hp_threshold(monster):
            return None
        return self._queue_redemption(monster, "low_hp_no_original")

    def _queue_redemption(self, monster: Entity, cause: str) -> dict:
        """怪物融化离场，等待接纳/无视。不产碎片。"""
        snapshot = {
            "name": monster.name,
            "attack_count": monster.attack_count,
            "attack_power": monster.attack_power,
            "blood_limit": monster.blood_limit,
            "dao_wen": {name: inst.x_value for name, inst in monster.dao_wen.items()},
            "cause": cause,
            "mutation": monster.mutation_count,
        }
        self._remove_from_combat(monster, "救赎", ctx={
            "timing": self._current_context_timing(), "source": "救赎", "source_type": "system",
            "target": monster, "mechanic": "leave", "subtype": "redemption",
            "tags": {"leave", "no_shards"},
        })
        monster._redeemed = True
        self.state.pending_redemption = snapshot
        return {
            "type": "redemption",
            "monster": monster.name,
            "cause": cause,
            "note": (
                f"随着最后一缕恶意消散，{monster.name}的身躯开始融化，"
                "原地只剩下一个昏迷的微光者"
            ),
        }
    def _can_be_sculptured(self, entity: Entity) -> bool:
        """雕塑对任何非轮回者生效。轮回者攻次/攻力归0不触发。"""
        return entity.entity_type != "轮回者"

    def settle_victory_paths(self) -> list[dict]:
        """
        回终多路径胜利结算（依次检查：雕塑 / 癌变 / 还债）
        雕塑：任何非轮回者（怪物/微光者/赤族等），不视为击杀，不提供碎片。
        还债：仅怪物。
        癌变对任一角色生效。
        """
        results = []
        for monster in list(self.state.enemies):
            if not monster.is_alive or monster.is_sculptured \
                    or monster.is_proliferated or monster.is_debt_bound:
                continue

            # 1. 雕塑：攻击次数或攻击力之一归0（任何非轮回者）
            if self._can_be_sculptured(monster) and (
                    monster.attack_count <= 0 or monster.attack_power <= 0):
                results.append(self._sculpture_monster(monster))
                continue

            # 2. 救赎：残血且没有七种原始怪物道纹
            redemption = self.check_redemption(monster)
            if redemption:
                results.append(redemption)
                continue

            # 3. 癌变：累计受到恢复量达阈值
            cancer = self.check_cancer(monster)
            if cancer:
                results.append(cancer)
                continue

            # 3. 还债：负债达阈值（仅怪物；shards为负）
            if monster.entity_type == "怪物" and monster.shards <= -self.DEBT_THRESHOLD:
                results.append(self._debt_bind_monster(monster))
                continue

        seen = {id(e) for e in self.state.enemies}
        for ally in list(self.state.get_all_player_side()):
            if id(ally) in seen:
                continue
            if (ally.is_alive and not ally.is_sculptured and not ally.is_proliferated
                    and not ally.is_debt_bound
                    and self._can_be_sculptured(ally)
                    and (ally.attack_count <= 0 or ally.attack_power <= 0)):
                results.append(self._sculpture_monster(ally))
                continue
            cancer = self.check_cancer(ally)
            if cancer:
                results.append(cancer)
        return results

    def _remove_from_combat(
        self, monster: Entity, reason: str = "离场",
        ctx: Optional[EffectContext | dict] = None,
    ):
        """将怪物移出战斗（不视为击杀）——统一走【离场】。"""
        parent = normalize_context(ctx)
        leave_ctx = make_context(
            timing=parent.timing if parent else self._current_context_timing(),
            source=reason, source_type="system", actor=parent.actor if parent else None,
            target=monster, owner=parent.owner if parent else None,
            mechanic="leave", subtype=reason, amount=0,
            tags=(set(parent.tags) if parent else set()) | {"leave", "no_shards"},
            parent_event_id=parent.event_id if parent else None,
        )
        monster._leave_ctx = leave_ctx.to_dict()
        monster.depart_battle(reason)

    def _cancer_character(self, entity: Entity, ctx: Optional[EffectContext | dict] = None) -> dict:
        """轮回者/同伴癌变：累计恢复达血限×2 → 直接命零。不吸收进书、不加休整+8。"""
        cancer_ctx = normalize_context(ctx)
        entity.is_proliferated = True
        entity.is_cancer = True
        self._hp_loss_recording += 1  # 癌变直接命零=特殊死因，不触发「失去生命后」
        try:
            entity.current_hp = 0
        finally:
            self._hp_loss_recording -= 1
        if entity is self.state.player:
            self.state.last_death_cause = "cancer"
        self._check_hp_zero_death(entity, ctx=cancer_ctx)
        return {
            "type": "cancer",
            "type_alias": "proliferation",
            "entity": entity.name,
            "entity_type": entity.entity_type,
            "absorbed_heal": entity.total_healed,
            "threshold": self.cancer_threshold_of(entity),
            "ctx": cancer_ctx.to_dict() if cancer_ctx else None,
            "note": f"{entity.name}累计承受{entity.total_healed}点恢复，触发【癌变】：直接[命零]",
        }

    def _sculpture_monster(self, monster: Entity) -> dict:
        """雕塑：怪物/微光者攻击次数或攻击力归0→化为雕塑消耗品（耐久=血限5%）"""
        durability = max(1, math.ceil(monster.blood_limit * 0.05))
        reason = "攻击次数归0" if monster.attack_count <= 0 else "攻击力归0"
        monster.is_sculptured = True
        self._remove_from_combat(monster, "雕塑", ctx={
            "timing": self._current_context_timing(), "source": "雕塑", "source_type": "system",
            "target": monster, "mechanic": "leave", "subtype": "sculpture", "tags": {"leave", "no_shards"},
        })
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

    def _proliferate_monster(self, monster: Entity, ctx: Optional[EffectContext | dict] = None) -> dict:
        """癌变：累计受到恢复量达阈值→吸收进死者之书，强化休整（旧名 增生）"""
        cancer_ctx = normalize_context(ctx)
        monster.is_proliferated = True
        # 兼容：同时写入癌变别名，便于外部以新名读取
        monster.is_cancer = True  # type: ignore[attr-defined]
        self._remove_from_combat(monster, "癌变", ctx=cancer_ctx)
        absorbed = monster.total_healed
        # 正文：每只被吸收的癌变怪物使局外【休整】永久额外产生8点恢复量，可叠加。
        boost = 8
        self.state.rest_heal_bonus += boost
        self.state.death_book_wisdom.append(f"癌变·{monster.name}：休整恢复量+{boost}")
        return {
            "type": "proliferation",  # 保留旧 key 兼容；新 key 见下一行
            "type_alias": "cancer",
            "monster": monster.name,
            "absorbed_heal": absorbed,
            "rest_boost": boost,
            "rest_heal_bonus_total": self.state.rest_heal_bonus,
            "ctx": cancer_ctx.to_dict() if cancer_ctx else None,
            "note": (f"{monster.name}累计承受{absorbed}点恢复被癌变吸收进《死者之书》，"
                     f"局外【休整】恢复量永久+{boost}（累计+{self.state.rest_heal_bonus}）"),
        }

    def _debt_bind_monster(self, monster: Entity) -> dict:
        """还债：负债达阈值→视为员工；负债还清后离开（走独立的负债经济轨道，不受出战支援/工资/黑名单约束）"""
        monster.is_debt_bound = True
        monster.is_departed = True
        monster.departure_reason = "还债"
        # 转为员工（保留当前面板），其待还负债记录于 shards（负值）。
        # 注意：还债者以员工身份继续参战，不置 is_alive=False，故不走 depart_battle，
        # 仅记录 is_departed/departure_reason 供战报分类；其已从 enemies 列表移除。
        monster.entity_type = "员工"
        monster.is_deployed = True  # "视为其参战"：立即出战，不需要玩家消耗出手派遣
        self.state.employees.append(monster)
        self.state.enemies.remove(monster)
        return {
            "type": "debt_bind",
            "monster": monster.name,
            "debt": -monster.shards,
            "note": (f"{monster.name}负债达{-monster.shards}，触发还债，视为[员工]参战；"
                     f"还清负债（支付{-monster.shards}碎片）后该员工离队"),
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
        if mode not in ("damage", "shield"):
            return {"success": False, "error": "雕塑mode必须是damage或shield"}
        if mode == "damage" and target is None:
            return {"success": False, "error": "伤害模式需指定目标"}
        if mode == "shield" and self.state.player is None:
            return {"success": False, "error": "没有玩家，无法获得格挡"}
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
            dmg = self._apply_hostile_damage(target, self.SCULPTURE_DAMAGE, source=self.state.player, ctx={
                "timing": self._current_context_timing(), "source": "雕塑", "source_type": "consumable",
                "actor": self.state.player, "target": target, "mechanic": "damage", "subtype": "sculpture",
                "amount": self.SCULPTURE_DAMAGE, "tags": {"consumable", "sculpture"},
            })
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

    # 兼容旧接口名（降服已删，改为指代多路径胜利结算）
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
    # 一阶副本集合
    TIER1_REGIONS = {"罪孽都市", "扭曲都市", "龙心谷"}

    @classmethod
    def monster_spawn_count(cls, battle_number: int, region: str) -> int:
        """出怪数量=战斗场数；一阶副本直接-3，最低1（实测定值，原-2通关率仅6%）"""
        if region in cls.TIER1_REGIONS:
            return max(1, battle_number - 3)
        return max(1, battle_number)

    # ========== 许愿（2026-08-19 新增，替代急中生智） ==========

    def initiate_wish(self, wisher: Entity, wish_text: str, target: Optional[Entity] = None) -> Interrupt:
        """特殊事件【许愿】：轮回者向"某人"祈求时触发。

        规则（2026-08-19）：
        1. 轮回者许下一个愿望；愿望本身没有固定的可行范围，也不存在"无法实现"的愿望；
        2. "某人"会以能够实现愿望、但最符合其扭曲本质的方式实现愿望；
        3. 愿望的代价与扭曲方式由愿望本身决定，不预先公开；
        4. 引擎只负责抛出中断并提交现场状态，实现方式与代价完全由 DM 裁定。
        """
        ctx = {
            "wisher": wisher.name,
            "wish_text": wish_text,
            "target": target.name if target is not None else None,
            "wisher_hp": wisher.current_hp,
            "wisher_daowen": list(wisher.dao_wen.keys()),
            "current_round": self.state.current_round,
        }
        return Interrupt(
            interrupt_type=InterruptType.WISH,
            context=ctx,
            description=(
                f"{wisher.name}向「某人」许下一个愿望：「{wish_text}」\n\n"
                f"规则：\n"
                f"1. 愿望没有固定的可行范围，不存在「无法实现」的愿望；\n"
                f"2. 「某人」会以能够实现愿望、但最符合其扭曲本质的方式实现；\n"
                f"3. 愿望的代价与扭曲方式由愿望本身决定，不预先公开。\n\n"
                f"请DM裁定「某人」以何种扭曲方式实现该愿望、以及轮回者付出的代价。"
            ),
            options=[
                {"id": "wish_resolved", "label": "实现愿望（扭曲方式）",
                 "description": "「某人」以最符合其扭曲本质的方式实现愿望，代价由愿望本身决定"},
            ],
            state_snapshot=self.state.to_dict()
        )

    def initiate_negotiation(self, proposal: str) -> Interrupt:
        """
        员工叛变·谈判声明：给出合理的谈判方案破解叛乱，需要DM裁定方案是否成立。
        """
        return Interrupt(
            interrupt_type=InterruptType.STAFF_MUTINY,
            context={
                "employees": [e.name for e in self.state.employees],
                "employee_attack_total": sum(e.attack_count * e.attack_power for e in self.state.employees),
                "player_hp": self.state.player.current_hp if self.state.player else 0,
                "shards": self.state.shards,
                "proposal": proposal,
            },
            description=(
                f"轮回者尝试以谈判方案破解员工叛变：\n\n{proposal}\n\n"
                f"请DM裁定该方案是否合理、能否平息叛乱。"
            ),
            options=[
                {"id": "negotiation_success", "label": "谈判成功", "description": "叛乱平息，方案对应的代价/效果按DM裁定生效"},
                {"id": "negotiation_fail", "label": "谈判失败", "description": "叛乱未平息，需改用镇压或让利处理"},
            ],
            state_snapshot=self.state.to_dict()
        )

    # ========== 辅助方法 ==========
    
    def apply_daowen_effect(
        self, name: str, calc: dict, caster: Entity, target: Entity,
        dragon_heart_use: int = 0, *, cost_share_target_ref: str = "",
        aoe_targets_override: Optional[list[Entity]] = None,
    ) -> dict:
        """应用道纹效果；aoe_targets_override用于绑定两阶段决策的目标快照。"""
        result = {"daowen": name, "effects": []}
        x = calc.get("x", 0)
        if name in ("自食", "固执"):
            target = caster
        # 本次道纹结算的根上下文。由它派生的血限/生命/命零变化都以此为父事件，
        # 这样「杀伐 → 伤害 → 血限下降 → 命零」在链上是连续的。
        daowen_ctx = make_context(
            timing=self._current_context_timing(),
            source=name, source_type="daowen",
            actor=caster, target=target, owner=caster,
            mechanic="daowen_resolution", subtype=name, amount=x,
            tags={"daowen"},
        )
        result["daowen_ctx"] = daowen_ctx.to_dict()

        # ---- 波及X（2026-08-21）：你发动的道纹同时作用于所有拥有波及效果的目标 ----
        # 数值型效果的总数值在所有目标（本次[目标]+波及目标，均排除施法者自身）间平分，
        # 余数随机分配；状态类效果对波及目标原样生效。多目标不复制或增加总数值。
        wave_status_targets: list[Entity] = [target]
        wave_pieces: dict[str, list[int]] = {}
        if name != "波及":
            wave_targets = self._wave_targets(caster)
            if wave_targets:
                effective: list[Entity] = []
                for wt in ([target] if target is not caster and target.is_alive else []) + wave_targets:
                    if wt.is_alive and wt not in effective:
                        effective.append(wt)
                if effective:
                    wave_status_targets = effective
                    numeric_keys = [k for k in self.WAVE_NUMERIC_KEYS if k in calc]
                    if numeric_keys and len(effective) >= 2:
                        wave_pieces = {k: self._divide_flat(calc[k], len(effective))
                                       for k in numeric_keys}
                        result["wave_spread"] = {
                            "targets": [e.name for e in effective],
                            "pieces": {k: list(v) for k, v in wave_pieces.items()},
                        }
                    elif len(effective) >= 2:
                        result["wave_spread"] = {
                            "targets": [e.name for e in effective], "status_only": True}

        # ---- 乱葬岗·附煞（sha_qi）效果修正 ----
        # 施法者持有的道纹实例可能带煞气；对消耗/持续/伤害/回复做代数修正。
        inst = caster.dao_wen.get(name)
        sha = getattr(inst, "sha_qi", "") if inst is not None else ""
        if sha:
            if sha == "法煞" and calc.get("cost", 0) > 0:
                calc["cost"] = max(calc.get("x", 1), calc["cost"] - calc.get("x", 1))
            if sha == "魂煞" and calc.get("duration") not in (None, -1):
                calc["duration"] += calc.get("x", 1)
            if sha == "冥煞":
                for k in ("target_damage", "total_damage", "aoe_damage"):
                    if k in calc:
                        calc[k] = calc[k] * 2
            if sha == "血煞" and "target_heal" in calc:
                calc["target_heal"] *= 2
            if sha == "心煞":
                result["sha_qi_cooldown_boost"] = True

        # 【冷却X】代价：README「冷却X：使用后该道纹记为【X(0)/Y】，[战终]后已完成
        # 战斗场数+1，达到Y时才能再次使用」。此前从未写入 cooldown_remaining，
        # 导致 固执/束缚/畸变/迟滞 可在同一场里无限重复发动（束缚因此支配全局）。
        if calc.get("cost_type") == "冷却":
            inst = caster.dao_wen.get(name)
            if inst is not None:
                inst.cooldown_remaining = max(inst.cooldown_remaining,
                                              int(calc.get("cost", 0)))
                result["cooldown_set"] = inst.cooldown_remaining

        # 蒙蔽(施法者伤害类道纹归零) / 坏死/镇尸(目标禁疗)
        mengbi_blocked = caster.has_status("蒙蔽") and ("target_damage" in calc or "aoe_damage" in calc)
        if mengbi_blocked:
            for s in caster.status_effects:
                if s.name == "蒙蔽" and s.value > 0:
                    s.value -= 1
                    if s.value <= 0: caster.status_effects.remove(s)
                    break
            result["mengbi_blocked"] = True
        huaisi_block = self._heal_blocked(target) and "target_heal" in calc

        # ---- 逆鳞加成（F2）：施法者若有层数，下次伤害+层数后清空 ----
        nilin_bonus = 0
        if hasattr(caster, "_nilin") and getattr(caster, "_nilin", 0) > 0 and any(k in calc for k in ("target_damage", "total_damage", "aoe_damage", "hp_percent_loss")):
            nilin_bonus = caster._nilin
            caster._nilin = 0
            result["nilin_bonus"] = nilin_bonus
            # 状态层数虽清空，但 status 本身仍按 duration 存在（仅清空计数）

        # ---- 伤害类 ----
        if "target_damage" in calc:
            base = calc["target_damage"] + (nilin_bonus if nilin_bonus else 0)
            base = self._jieli_boost(caster, base)
            if caster.has_status("坠落") and base > 0:
                base = math.ceil(base / 2)
            dmg_amount = 0 if mengbi_blocked else base
            if "target_damage" in wave_pieces:
                # 波及：修正后总数值平分（余数随机分配），逆鳞已计入首段总值。
                pieces = self._divide_flat(dmg_amount, len(wave_status_targets))
                wave_pieces["target_damage"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    piece = 0 if mengbi_blocked else piece
                    dmg = self._apply_hostile_damage(
                        wt, piece, source=caster,
                        ctx={"timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                             "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                             "mechanic": "damage", "subtype": "daowen", "amount": piece,
                             "tags": {"daowen", "wave"}})
                    result["effects"].append({"type": "damage", "target": wt.name, **dmg})
                    if dmg.get("actual_damage", 0) > 0:
                        caster.damage_dealt_this_round += dmg["actual_damage"]
                nilin_bonus = 0
            else:
                dmg = self._apply_hostile_damage(
                    target, dmg_amount, source=caster,
                    ctx={"timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                         "source": name, "source_type": "daowen", "actor": caster, "target": target,
                         "mechanic": "damage", "subtype": "daowen", "amount": dmg_amount,
                         "tags": {"daowen"}})
                result["effects"].append({"type": "damage", "target": target.name, **dmg})
                if dmg.get("actual_damage", 0) > 0:
                    caster.damage_dealt_this_round += dmg["actual_damage"]
        if name == "血债" or ("hits" in calc and calc.get("damage_per_hit") == 1 and "target_damage" not in calc):
            hits = calc.get("hits", 1)
            if "hits" in wave_pieces:
                # 波及：总命中次数平分，每个目标的份额每次1点伤害。
                for wt, piece in zip(wave_status_targets, wave_pieces["hits"]):
                    total_act = 0
                    total_abs = 0
                    for _ in range(piece):
                        if not wt.is_alive:
                            break
                        hit_amount = 0 if mengbi_blocked else 1
                        dmg_i = self._apply_hostile_damage(
                            wt, hit_amount, source=caster,
                            ctx={"timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                                 "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                                 "mechanic": "damage", "subtype": "daowen_hit", "amount": hit_amount,
                                 "tags": {"daowen", "multi_hit", "wave"}})
                        total_act += dmg_i.get("actual_damage", 0)
                        total_abs += dmg_i.get("shield_absorbed", 0)
                    dmg = {"raw_damage": piece, "actual_damage": total_act, "shield_absorbed": total_abs,
                           "hp_after": wt.current_hp, "died": not wt.is_alive}
                    result["effects"].append({"type": "damage", "target": wt.name, **dmg})
                    if total_act > 0:
                        caster.damage_dealt_this_round += total_act
            else:
                total_act = 0
                total_abs = 0
                for _ in range(hits):
                    if not target.is_alive:
                        break
                    hit_amount = 0 if mengbi_blocked else 1
                    dmg_i = self._apply_hostile_damage(
                        target, hit_amount, source=caster,
                        ctx={"timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                             "source": name, "source_type": "daowen", "actor": caster, "target": target,
                             "mechanic": "damage", "subtype": "daowen_hit", "amount": hit_amount,
                             "tags": {"daowen", "multi_hit"}})
                    total_act += dmg_i.get("actual_damage", 0)
                    total_abs += dmg_i.get("shield_absorbed", 0)
                dmg = {"raw_damage": hits, "actual_damage": total_act, "shield_absorbed": total_abs,
                       "hp_after": target.current_hp, "died": not target.is_alive}
                result["effects"].append({"type": "damage", "target": target.name, **dmg})
                if total_act > 0:
                    caster.damage_dealt_this_round += total_act
        elif "total_damage" in calc and "target_damage" not in calc:  # 其他多段
            add = nilin_bonus
            nilin_bonus = 0
            chunk = self._jieli_boost(caster, calc["total_damage"] + add)
            if caster.has_status("坠落") and chunk > 0:
                chunk = math.ceil(chunk / 2)
            dmg_amount = 0 if mengbi_blocked else chunk
            if "total_damage" in wave_pieces:
                pieces = self._divide_flat(dmg_amount, len(wave_status_targets))
                wave_pieces["total_damage"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    piece = 0 if mengbi_blocked else piece
                    dmg = self._apply_hostile_damage(wt, piece, source=caster, ctx={
                        "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                        "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                        "mechanic": "damage", "subtype": "daowen", "amount": piece,
                        "tags": {"daowen", "wave"},
                    })
                    if add:
                        dmg["nilin_bonus"] = add
                        add = 0
                    result["effects"].append({"type": "damage", "target": wt.name, **dmg})
                    if dmg.get("actual_damage", 0) > 0:
                        caster.damage_dealt_this_round += dmg["actual_damage"]
            else:
                dmg = self._apply_hostile_damage(target, dmg_amount, source=caster, ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": target,
                    "mechanic": "damage", "subtype": "daowen", "amount": dmg_amount, "tags": {"daowen"},
                })
                if add:
                    dmg["nilin_bonus"] = add
                result["effects"].append({"type": "damage", "target": target.name, **dmg})
                if dmg.get("actual_damage", 0) > 0:
                    caster.damage_dealt_this_round += dmg["actual_damage"]

        # ---- 乱葬岗·附煞后置：锁煞（造成伤害后触发） ----
        if sha == "锁煞":
            dealt = 0
            for ef in result.get("effects", []):
                if ef.get("type") == "damage":
                    dealt += ef.get("actual_damage", 0) or 0
            if dealt > 0 and target.is_alive:
                if target.entity_type == "轮回者":
                    drain = min(target.current_mana, dealt)
                    target.current_mana -= drain
                    result["sha_qi_lock_mana"] = drain

        if "aoe_damage" in calc:
            a = 0 if mengbi_blocked else self._jieli_boost(caster, calc["aoe_damage"])
            if caster.has_status("坠落") and a > 0:
                a = math.ceil(a / 2)
            # 逆鳞加成仅作用于首个目标的首段伤害
            if nilin_bonus:
                a += nilin_bonus
                result["nilin_bonus"] = nilin_bonus
                nilin_bonus = 0
            if aoe_targets_override is not None:
                # 两阶段怪物决策必须只结算prepare时列出的目标，且已闪避目标已由调用方剔除。
                aoe_targets = [e for e in aoe_targets_override if e.is_alive]
            elif caster in self.state.get_all_player_side():
                aoe_targets = self.state.get_all_enemy_side()
            else:
                aoe_targets = self.state.get_all_player_side()
            for enemy in aoe_targets:
                # 对首个敌人附加剩余加成（若前未消耗）
                dmg_a = a
                if nilin_bonus and enemy is aoe_targets[0]:
                    dmg_a += nilin_bonus
                    nilin_bonus = 0
                dmg = self._apply_hostile_damage(enemy, dmg_a, source=caster, ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": enemy,
                    "mechanic": "damage", "subtype": "aoe_daowen", "amount": dmg_a, "tags": {"daowen", "aoe"},
                })
                result["effects"].append({"type": "aoe_damage", "target": enemy.name, **dmg})
                if dmg.get("actual_damage", 0) > 0:
                    caster.damage_dealt_this_round += dmg["actual_damage"]
        if "hp_percent_loss" in calc and name != "赌命":  # 赌命已改为[回始]随机结算（F2），此处仅保留其他百分比道纹
            if "hp_percent_loss" in wave_pieces:
                # 波及：总数值=各目标按当前生命×百分比之和，再平分。
                total = sum(math.ceil(wt.current_hp * calc["hp_percent_loss"] / 100)
                            for wt in wave_status_targets) + (nilin_bonus if nilin_bonus else 0)
                if nilin_bonus:
                    result["nilin_bonus"] = nilin_bonus
                    nilin_bonus = 0
                pieces = self._divide_flat(total, len(wave_status_targets))
                wave_pieces["hp_percent_loss"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    dmg = self._apply_hostile_damage(wt, piece, source=caster, ctx={
                        "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                        "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                        "mechanic": "damage", "subtype": "percent", "amount": piece,
                        "tags": {"daowen", "percent", "wave"},
                    })
                    result["effects"].append({"type": "pct_damage", "target": wt.name, **dmg})
                    if dmg.get("actual_damage", 0) > 0:
                        caster.damage_dealt_this_round += dmg["actual_damage"]
            else:
                d = math.ceil(target.current_hp * calc["hp_percent_loss"] / 100) + (nilin_bonus if nilin_bonus else 0)
                if nilin_bonus:
                    result["nilin_bonus"] = nilin_bonus
                    nilin_bonus = 0
                dmg = self._apply_hostile_damage(target, d, source=caster, ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": target,
                    "mechanic": "damage", "subtype": "percent", "amount": d, "tags": {"daowen", "percent"},
                })
                result["effects"].append({"type": "pct_damage", "target": target.name, **dmg})
                if dmg.get("actual_damage", 0) > 0:
                    caster.damage_dealt_this_round += dmg["actual_damage"]

        # ---- 回复类 ----
        if "target_heal" in calc:
            if "target_heal" in wave_pieces:
                pieces = self._divide_flat(calc["target_heal"], len(wave_status_targets))
                wave_pieces["target_heal"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    if self._heal_blocked(wt):
                        result["effects"].append(
                            {"type": "heal", "target": wt.name, "blocked_by": "坏死"})
                        continue
                    result["effects"].append({"type": "heal", "target": wt.name, **self.state.apply_heal(wt, piece, ctx={
                        "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                        "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                        "owner": caster, "mechanic": "heal", "subtype": "daowen", "amount": piece,
                        "tags": {"daowen", "wave"},
                    })})
            elif not huaisi_block:
                heal_amount = calc["target_heal"]
                result["effects"].append({"type": "heal", "target": target.name, **self.state.apply_heal(target, heal_amount, ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": target,
                    "owner": caster, "mechanic": "heal", "subtype": "daowen", "amount": heal_amount,
                    "tags": {"daowen"},
                })})
        # 自愈的 heal_percent 只在[回始]结算，发动当下不奶。
        if "heal_percent" in calc and name != "自愈":
            if "heal_percent" in wave_pieces:
                # 波及：总数值=各目标按血限×百分比之和，再平分。
                total = sum(math.ceil(wt.blood_limit * calc["heal_percent"] / 100)
                            for wt in wave_status_targets)
                pieces = self._divide_flat(total, len(wave_status_targets))
                wave_pieces["heal_percent"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    if self._heal_blocked(wt):
                        result["effects"].append(
                            {"type": "heal_pct", "target": wt.name, "blocked_by": "坏死"})
                        continue
                    result["effects"].append({"type": "heal_pct", "target": wt.name, **self.state.apply_heal(wt, piece, ctx={
                        "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                        "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                        "owner": caster, "mechanic": "heal", "subtype": "daowen_pct", "amount": piece,
                        "tags": {"daowen", "wave"},
                    })})
            elif not self._heal_blocked(target):
                h = math.ceil(target.blood_limit * calc["heal_percent"] / 100)
                result["effects"].append({"type": "heal_pct", "target": target.name, **self.state.apply_heal(target, h, ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": target,
                    "owner": caster, "mechanic": "heal", "subtype": "daowen_pct", "amount": h,
                    "tags": {"daowen"},
                })})
        if "mutation_reduction" in calc:
            if "mutation_reduction" in wave_pieces:
                pieces = self._divide_flat(calc["mutation_reduction"], len(wave_status_targets))
                wave_pieces["mutation_reduction"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    pay = wt.add_mutation(-piece)
                    result["effects"].append({
                        "type": "mutation_reduction", "target": wt.name,
                        "reduced": piece, "mutation_total": pay["mutation_total"],
                    })
                    if wt.entity_type == "怪物":
                        redemption = self.check_redemption(wt)
                        if redemption:
                            result["effects"].append(redemption)
            else:
                reduced = int(calc["mutation_reduction"])
                pay = target.add_mutation(-reduced)
                result["effects"].append({
                    "type": "mutation_reduction", "target": target.name,
                    "reduced": reduced, "mutation_total": pay["mutation_total"],
                })
                if target.entity_type == "怪物":
                    redemption = self.check_redemption(target)
                    if redemption:
                        result["effects"].append(redemption)

        if "target_heal" in calc or ("heal_percent" in calc and name != "自愈"):
            for cancer_target in wave_status_targets:
                cancer = self.check_cancer(cancer_target)
                if cancer:
                    result["effects"].append(cancer)

        # ---- 格挡/血限 ----
        if "target_shield" in calc:
            if "target_shield" in wave_pieces:
                pieces = self._divide_flat(calc["target_shield"], len(wave_status_targets))
                wave_pieces["target_shield"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    wt.gain_shield(piece)
                    result["effects"].append({"type": "shield", "target": wt.name, "amount": piece})
            else:
                s = calc["target_shield"]; target.gain_shield(s)
                result["effects"].append({"type": "shield", "target": target.name, "amount": s})
        if "shield_drain" in calc:  # 清算：目标失格挡
            lost = min(target.shield, calc["shield_drain"]); target.shield -= lost
            result["effects"].append({"type": "shield_drain", "target": target.name, "lost": lost})
        if "blood_limit_reduction" in calc:
            _hp_before = target.current_hp
            # clamp_hp/lethal 关掉：本效果的生命封顶要在 hp_reduction 之后统一做一次。
            _blr = self._apply_blood_limit_change(
                target, -calc["blood_limit_reduction"], name, EffectPolarity.DEBUFF.value,
                ctx=daowen_ctx, source_type="daowen", subtype="blood_limit_reduction",
                actor=caster, owner=caster, clamp_hp=False, lethal=False)
            # README 第460行"[血限]及当前生命同时 -4X"：两者是各自独立的扣减。
            # 此前实现只做 current_hp=min(current_hp, blood_limit)（血限压顶），
            # 对残血目标等于毫无效果。合并成一次写入：既保持与两步扣减相同的终值，
            # 又让 Entity.__setattr__ 的「失去生命后」钩子恰好触发一次。
            if "hp_reduction" in calc:
                _target_hp = target.current_hp - calc["hp_reduction"]
            else:
                _target_hp = target.current_hp
            self._hp_loss_ctx = daowen_ctx
            target.current_hp = max(0, min(_target_hp, target.blood_limit))
            self._check_hp_zero_death(target, ctx=_blr["ctx"] or daowen_ctx)
            _hp_cut_tmp = _hp_before - target.current_hp
            # 血限压迫导致的当前生命减少，同样属于"使敌对角色生命减少"，
            # 必须计入本回合伤害统计，否则纯压血限流派会被【凡庸】判定为无所作为而自爆。
            _hp_cut = _hp_before - target.current_hp
            if _hp_cut > 0 and target is not caster:
                caster.damage_dealt_this_round += _hp_cut
            result["effects"].append({"type": "blood_limit_reduction", "target": target.name,
                                      "new_blood_limit": target.blood_limit,
                                      "hp_reduced": _hp_cut})
        if "blood_limit_increase" in calc:
            if "blood_limit_increase" in wave_pieces:
                pieces = self._divide_flat(calc["blood_limit_increase"], len(wave_status_targets))
                wave_pieces["blood_limit_increase"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    # 不朽之躯（初拥之夜遗物）：血限无法增加，对该实体的增殖等血限增长一律归零
                    if self.state.side_has(wt, "不朽之躯"):
                        result["effects"].append({"type": "blood_limit_increase", "target": wt.name,
                                                   "increase": 0, "blocked_by": "不朽之躯"})
                        continue
                    increase = self._apply_blood_limit_change(
                        wt, piece, name, EffectPolarity.BUFF.value,
                        ctx=daowen_ctx, source_type="daowen", subtype="blood_limit_increase",
                        actor=caster, owner=caster, clamp_hp=False, lethal=False)["applied"]
                    result["effects"].append({"type": "blood_limit_increase", "target": wt.name,
                                              "increase": increase})
            else:
                # 不朽之躯（初拥之夜遗物）：血限无法增加，对该实体的增殖等血限增长一律归零
                if self.state.side_has(target, "不朽之躯"):
                    result["effects"].append({"type": "blood_limit_increase", "target": target.name,
                                               "increase": 0, "blocked_by": "不朽之躯"})
                else:
                    increase = self._apply_blood_limit_change(
                        target, calc["blood_limit_increase"], name, EffectPolarity.BUFF.value,
                        ctx=daowen_ctx, source_type="daowen", subtype="blood_limit_increase",
                        actor=caster, owner=caster, clamp_hp=False, lethal=False)["applied"]
                    result["effects"].append({"type": "blood_limit_increase", "target": target.name,
                                              "increase": increase})
        # 【逼债】旧"碎片不足则失血限"路径已按 DM裁定D（2026-08-22）废止并移除：
        # 无力支付统一记为负债（见 round_start 的 F2 结算），唯一血限语义不再存在。

        # ---- 攻击面板修改 ----
        _panel_keys = ("attack_boost", "attack_reduction", "attack_fixed", "attack_count_fixed")
        # 波及扩散：attack_boost/reduction 数值平分；attack_fixed/attack_count_fixed
        # （固定面板为状态类）对波及目标原样生效。
        panel_targets = wave_status_targets if (
            any(k in wave_pieces for k in ("attack_boost", "attack_reduction"))
            or (any(k in calc for k in ("attack_fixed", "attack_count_fixed"))
                and len(wave_status_targets) > 1)) else [target]
        for panel_idx, panel_target in enumerate(panel_targets):
            panel_locked = panel_target.has_status("定型") and any(k in calc for k in _panel_keys)
            if panel_locked:
                result["effects"].append({"type": "dingxing_block", "target": panel_target.name})
            if (not panel_locked) and "attack_boost" in calc:
                piece = (wave_pieces.get("attack_boost") or [calc["attack_boost"]])[panel_idx]
                self._battle_delta(
                    panel_target, "attack_power", piece,
                    name, EffectPolarity.BUFF.value)
                result["effects"].append({"type": "attack_boost", "target": panel_target.name,
                                          "attack_power": panel_target.attack_power})
            if (not panel_locked) and "attack_reduction" in calc:
                amount = (wave_pieces.get("attack_reduction") or [calc["attack_reduction"]])[panel_idx]
                delta = max(0, panel_target.attack_power - amount) - panel_target.attack_power
                self._battle_delta(
                    panel_target, "attack_power", delta, name, EffectPolarity.DEBUFF.value)
                result["effects"].append({"type": "attack_reduction", "target": panel_target.name,
                                          "attack_power": panel_target.attack_power})
            if (not panel_locked) and "attack_fixed" in calc:
                self._battle_delta(
                    panel_target, "attack_power", calc["attack_fixed"] - panel_target.attack_power,
                    name, EffectPolarity.NEUTRAL.value)
                result["effects"].append({"type": "attack_fixed", "target": panel_target.name,
                                          "attack_power": panel_target.attack_power})
            if (not panel_locked) and "attack_count_fixed" in calc:
                self._battle_delta(
                    panel_target, "attack_count", calc["attack_count_fixed"] - panel_target.attack_count,
                    name, EffectPolarity.NEUTRAL.value)
                result["effects"].append({"type": "attack_count_fixed", "target": panel_target.name,
                                          "attack_count": panel_target.attack_count})
        bianxing_blocked = False
        if name == "变形":  # 自身攻击力与攻击次数互换；持续结束后还原首次变形前面板
            if caster.has_status("定型"):
                bianxing_blocked = True
                result["effects"].append({"type": "dingxing_block", "target": caster.name})
            else:
                if not hasattr(caster, "_bianxing_original"):
                    caster._bianxing_original = (caster.attack_power, caster.attack_count)
                caster.attack_power, caster.attack_count = caster.attack_count, caster.attack_power
                result["effects"].append({"type": "swap", "target": caster.name,
                                          "attack_power": caster.attack_power,
                                          "attack_count": caster.attack_count})

        # ---- 速度修改 ----
        if "speed_boost" in calc:
            if "speed_boost" in wave_pieces:
                pieces = self._divide_flat(calc["speed_boost"], len(wave_status_targets))
                wave_pieces["speed_boost"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    gained = self._gain_speed(wt, piece, ctx={
                        "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                        "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                        "owner": caster, "mechanic": "speed_change", "subtype": "current_speed",
                        "amount": piece, "tags": {"daowen", "wave"},
                    })
                    result["effects"].append({"type": "speed_boost", "target": wt.name,
                                              "speed": wt.current_speed, "gained": gained})
            else:
                gained = self._gain_speed(target, calc["speed_boost"], ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": target,
                    "owner": caster, "mechanic": "speed_change", "subtype": "current_speed",
                    "amount": calc["speed_boost"], "tags": {"daowen"},
                })
                result["effects"].append({"type": "speed_boost", "target": target.name,
                                          "speed": target.current_speed, "gained": gained})
        if "speed_halved" in calc:
            # 减速X：速度减半为状态类效果（非数值平分），对波及目标原样生效。
            for wt in wave_status_targets:
                lost = wt.current_speed - math.ceil(wt.current_speed / 2)
                self._lose_current_speed(wt, lost, ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                    "owner": caster, "mechanic": "speed_change", "subtype": "current_speed",
                    "amount": -lost, "tags": {"daowen"},
                })
                result["effects"].append({"type": "speed_halved", "target": wt.name,
                                          "speed": wt.current_speed})
        if "speed_penalty" in calc and (name != "赎金" or self._shards_of(target) <= 0):
            self._lose_current_speed(target, calc["speed_penalty"], ctx={
                "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                "source": name, "source_type": "daowen", "actor": caster, "target": target,
                "owner": caster, "mechanic": "speed_change", "subtype": "current_speed",
                "amount": -calc["speed_penalty"], "tags": {"daowen"},
            })
            result["effects"].append({"type": "speed_penalty", "target": target.name,
                                      "lost": calc["speed_penalty"], "speed": target.current_speed})

        # ---- 碎片系（罪孽）----
        if "shard_steal" in calc and self._shards_of(target) > 0:
            # 赎金是“有碎片则夺取，若无碎片才失速”的二选一；不得把不足额偷取扩成负债。
            gained = min(self._shards_of(target), calc["shard_steal"])
            self._lose_shards_of(target, gained)
            if caster is self.state.player:
                self.state.shards += gained
            else:
                caster.shards += gained
            result["effects"].append({"type": "shard_steal", "target": target.name, "gained": gained})
        if "fake_shards" in calc:  # 假钞：获得10X假碎片（假碎片与真碎片分离存储）
            if caster is self.state.player:
                self.state.fake_shards += calc["fake_shards"]
            else:
                caster.fake_shards = getattr(caster, "fake_shards", 0) + calc["fake_shards"]
            result["effects"].append({"type": "fake_shards", "gained": calc["fake_shards"]})
        if "cost_shards" in calc and name != "消灾":  # 消灾的付费在专属分支统一处理
            spent = self._lose_shards_of(self.state.player, calc["cost_shards"]) if caster is self.state.player else 0
            result["effects"].append({"type": "cost_shards", "spent": spent})

        # ---- 数值代价：统一走代价总线；【血契】可按显式引用共同承担 ----
        cost_spec = None
        if "cost_hp" in calc:
            cost_spec = ("流血", calc["cost_hp"], "bleed_cost")
        elif "cost_blood_limit" in calc:
            cost_spec = ("衰老", calc["cost_blood_limit"], "aging_cost")
        elif "cost_speed" in calc:
            cost_spec = ("疲惫", calc["cost_speed"], "fatigue_cost")
        elif "cost_mutation" in calc and caster.entity_type != "怪物":
            # 怪物原始道纹在两阶段怪物流程中已先支付异变5X；此处只补轮回者/同伴的同类代价。
            cost_spec = ("异变", calc["cost_mutation"], "mutation_cost")
        if cost_spec is not None:
            cost_type, amount, effect_type = cost_spec
            timing = "monster_action" if caster.entity_type == "怪物" else "player_action"
            payment = self.pay_numeric_cost(
                caster, cost_type, amount,
                cost_share_target_ref=cost_share_target_ref,
                dragon_heart_use=dragon_heart_use,
                cost_context={"timing": timing, "source": name, "source_type": "daowen", "tags": {"active_payment"}},
            )
            cost_effect = {"type": effect_type, **payment}
            if cost_type == "流血":
                # 保留既有公开字段，同时以owner/shared_with暴露血契拆分详情。
                cost_effect["actual_damage"] = payment["actual_paid"]
                cost_effect["hp_after"] = caster.current_hp
            result["effects"].append(cost_effect)
        elif cost_share_target_ref:
            raise ValueError("该道纹没有可由【血契】共同承担的数值代价")
        if "mana_gain" in calc:
            caster.current_mana += calc["mana_gain"]
            self.clamp_immortal_body(caster)
            result["effects"].append({"type": "mana_gain", "source": caster.name, "mana_gained": calc["mana_gain"]})

        # ---- 乱葬岗（二阶）专属道纹效果 ----
        if name == "瓦解" and calc.get("blood_limit_pct"):
            pct = calc["blood_limit_pct"]
            if len(wave_status_targets) > 1:
                # 波及：总数值=各目标血限×百分比之和，再平分。
                total = sum(math.ceil(wt.blood_limit * pct / 100) for wt in wave_status_targets)
                pieces = self._divide_flat(total, len(wave_status_targets))
                for wt, piece in zip(wave_status_targets, pieces):
                    self._apply_blood_limit_change(
                        wt, -piece, "瓦解", EffectPolarity.DEBUFF.value,
                        ctx=daowen_ctx, source_type="daowen", subtype="disintegrate",
                        actor=caster, owner=caster)
                    result["effects"].append({"type": "wajie", "target": wt.name,
                                              "blood_limit_pct": pct, "blood_limit_cut": piece,
                                              "blood_limit_after": wt.blood_limit})
            else:
                cut = math.ceil(target.blood_limit * pct / 100)
                self._apply_blood_limit_change(
                    target, -cut, "瓦解", EffectPolarity.DEBUFF.value,
                    ctx=daowen_ctx, source_type="daowen", subtype="disintegrate",
                    actor=caster, owner=caster)
                result["effects"].append({"type": "wajie", "target": target.name,
                                          "blood_limit_pct": pct, "blood_limit_cut": cut,
                                          "blood_limit_after": target.blood_limit})
        if name == "镇尸" and calc.get("no_heal"):
            for st_target in wave_status_targets:
                st_target.add_status(StatusEffect(name="镇尸", value=1,
                                                  remaining_rounds=calc.get("duration", 1),
                                                  source=caster.name))
                result["effects"].append({"type": "zhenshi", "target": st_target.name,
                                          "duration": calc.get("duration", 1)})
        if name == "勾魂" and calc.get("no_mana_gain"):
            # 勾魂X（2026-08-30 改版，报告.md 硬伤2-C）：持续X回合[回始]无法获得法力。
            # 旧版为「[回始]失去2X法力，持续∞」（永久扣蓝），已废止。
            for st_target in wave_status_targets:
                st_target.add_status(StatusEffect(name="勾魂", value=1,
                                                  remaining_rounds=calc.get("duration", x),
                                                  source=caster.name))
                result["effects"].append({"type": "gouhun", "target": st_target.name,
                                          "no_mana_gain": True,
                                          "duration": calc.get("duration", x)})
        if name == "冥气" and calc.get("speed_loss_speed_limit"):
            for st_target in wave_status_targets:
                st_target.add_status(StatusEffect(name="冥气", value=calc["speed_loss_speed_limit"],
                                                  remaining_rounds=calc.get("duration", 1),
                                                  source=caster.name))
                result["effects"].append({"type": "mingqi", "target": st_target.name,
                                          "speed_loss_speed_limit": calc["speed_loss_speed_limit"],
                                          "duration": calc.get("duration", 1)})
        if name == "缄默" and calc.get("silence_death_triggers"):
            for st_target in wave_status_targets:
                st_target.add_status(StatusEffect(name="缄默", value=1,
                                                  remaining_rounds=calc.get("duration", 1),
                                                  source=caster.name))
                result["effects"].append({"type": "qianmo", "target": st_target.name,
                                          "duration": calc.get("duration", 1)})
        if name == "尸爆" and calc.get("self_destruct"):
            # [命零]对全体敌方打出自身血限10X%伤害
            if caster.is_alive and caster.current_hp > 0:
                pct = calc["aoe_pct"]
                dmg = math.ceil(caster.blood_limit * pct / 100)
                for enemy in [e for e in self.state.get_all_enemy_side() if e.is_alive]:
                    rd = self._apply_hostile_damage(enemy, dmg, source=caster, ctx={
                        "timing": "player_action" if caster is self.state.player else "monster_action",
                        "source": "尸爆", "source_type": "daowen", "actor": caster, "target": enemy,
                        "mechanic": "damage", "subtype": "self_destruct_aoe", "amount": dmg,
                        "tags": {"daowen", "aoe", "self_destruct"},
                    })
                    result["effects"].append({"type": "aoe_damage", "target": enemy.name, **rd})
                # 尸爆是「自毁式[命零]」，不是生命归零致死：正文未规定清零当前生命，
                # 因此这里刻意不走 _check_hp_zero_death（它会把 current_hp 抹成 0），
                # 而是直接置命零标记 + 统一死亡通知（带完整 ctx）。
                caster.is_alive = False
                self._on_entity_death(caster, ctx={
                    "timing": "player_action" if caster is self.state.player else "monster_action",
                    "source": "尸爆", "source_type": "daowen", "actor": caster, "target": caster,
                    "mechanic": "death", "subtype": "self_destruct", "tags": {"daowen", "self_destruct"},
                })
                result["self_destructed"] = True
        if name == "分裂" and calc.get("split_clones"):
            # [命零]时创造X个复制体（血限20%）
            self.state._pending_split_clones = calc["split_clones"]
            result["effects"].append({"type": "fenlie",
                                      "note": "本场[命零]时创造X个复制体（血限20%）",
                                      "clones": calc["split_clones"]})
        if name == "招魂" and calc.get("revive_temp_friend"):
            # 唤回1具已击灭的怪物尸体作临时朋友（生命20X）
            dead = [e for e in self.state.dead_monsters if e.entity_type == "怪物"]
            if dead:
                corpse = dead[-1]
                from .models import Entity
                revived = Entity(name=f"{corpse.name}（魂）", entity_type="临时朋友",
                                 blood_limit=calc["temp_hp"], current_hp=calc["temp_hp"],
                                 attack_count=corpse.attack_count, attack_power=corpse.attack_power)
                for dw_name, dw_inst in corpse.dao_wen.items():
                    revived.dao_wen[dw_name] = dw_inst
                self.state.temp_friends.append(revived)
                result["effects"].append({"type": "zhaohun", "name": revived.name,
                                          "hp": calc["temp_hp"], "corpse": corpse.name})
            else:
                result["effects"].append({"type": "zhaohun", "note": "没有可唤回的怪物尸体"})

        # ---- 特殊 ----
        if "self_attack_count" in calc:  # 自残：目标自打X次
            if "self_attack_count" in wave_pieces:
                pieces = self._divide_flat(calc["self_attack_count"], len(wave_status_targets))
                wave_pieces["self_attack_count"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    for _ in range(piece):
                        result["effects"].append({"type": "self_attack", "target": wt.name,
                            **self._apply_hostile_damage(wt, wt.attack_power, source=wt, ctx={
                                "timing": "player_action" if caster is self.state.player else "monster_action",
                                "source": name, "source_type": "daowen", "actor": wt, "target": wt,
                                "mechanic": "damage", "subtype": "self_attack", "amount": wt.attack_power,
                                "tags": {"daowen", "self_damage", "wave"},
                            })})
            else:
                for _ in range(calc["self_attack_count"]):
                    result["effects"].append({"type": "self_attack", "target": target.name,
                        **self._apply_hostile_damage(target, target.attack_power, source=target, ctx={
                            "timing": "player_action" if caster is self.state.player else "monster_action",
                            "source": name, "source_type": "daowen", "actor": target, "target": target,
                            "mechanic": "damage", "subtype": "self_attack", "amount": target.attack_power,
                            "tags": {"daowen", "self_damage"},
                        })})
        if "targets_removed" in calc:  # 封印：仅移出怪物（README：X个[目标]怪物）
            removed = 0
            removed_names = []
            if "targets_removed" in wave_pieces:
                # 波及：总名额平分（余数随机分配），优先作用于被标记的怪物。
                quota = dict(zip(wave_status_targets, wave_pieces["targets_removed"]))
                for e in list(self.state.enemies):
                    if removed >= calc["targets_removed"]:
                        break
                    if e.is_alive and e.entity_type == "怪物" and quota.get(e, 0) > 0:
                        quota[e] -= 1
                        self._remove_from_combat(e, "封印", ctx={
                            "timing": "player_action" if caster is self.state.player else "monster_action",
                            "source": name, "source_type": "daowen", "actor": caster, "target": e,
                            "mechanic": "leave", "subtype": "seal", "tags": {"leave", "no_shards"},
                        })
                        removed += 1
                        removed_names.append(e.name)
                # 剩余名额兜底：按原规则从敌人列表补足。
                for e in list(self.state.enemies):
                    if removed >= calc["targets_removed"]:
                        break
                    if e.is_alive and e.entity_type == "怪物":
                        self._remove_from_combat(e, "封印", ctx={
                            "timing": "player_action" if caster is self.state.player else "monster_action",
                            "source": name, "source_type": "daowen", "actor": caster, "target": e,
                            "mechanic": "leave", "subtype": "seal", "tags": {"leave", "no_shards"},
                        })
                        removed += 1
                        removed_names.append(e.name)
            else:
                for e in list(self.state.enemies):
                    if (e.is_alive and e.entity_type == "怪物"
                            and removed < calc["targets_removed"]):
                        self._remove_from_combat(e, "封印", ctx={
                            "timing": "player_action" if caster is self.state.player else "monster_action",
                            "source": name, "source_type": "daowen", "actor": caster, "target": e,
                            "mechanic": "leave", "subtype": "seal", "tags": {"leave", "no_shards"},
                        })
                        removed += 1
                        removed_names.append(e.name)
            result["effects"].append({
                "type": "seal", "removed": removed, "targets": removed_names,
            })

        # ---- 持续/触发状态（status_added）----
        if "guaranteed_hits" in calc:
            self.grant_bizhong(caster, int(calc["guaranteed_hits"]))
            result["effects"].append({"type": "bizhong", "target": caster.name,
                                      "count": calc["guaranteed_hits"],
                                      "remaining": self.bizhong_remaining(caster)})

        # 蒙蔽X：使[目标]下X次造成的伤害无效。次数型，不走 duration 挂状态。
        # 此前只算出 invalid_damage_hits，apply 不消费，轮回者 use_daowen 等于白扣 5X 法力。
        # 怪物侧走 _apply_control_to_player，探照灯走 add_status，所以旧测试都绿。
        if "invalid_damage_hits" in calc:
            if "invalid_damage_hits" in wave_pieces:
                pieces = self._divide_flat(calc["invalid_damage_hits"], len(wave_status_targets))
                wave_pieces["invalid_damage_hits"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    if piece > 0:
                        wt.add_status(StatusEffect(
                            name="蒙蔽", remaining_rounds=-1, value=piece, source=caster.name))
                        result["effects"].append({
                            "type": "mengbi",
                            "target": wt.name,
                            "count": piece,
                            "remaining": wt.get_status_value("蒙蔽"),
                        })
            else:
                hits = int(calc["invalid_damage_hits"])
                if hits > 0:
                    target.add_status(StatusEffect(
                        name="蒙蔽", remaining_rounds=-1, value=hits, source=caster.name))
                    result["effects"].append({
                        "type": "mengbi",
                        "target": target.name,
                        "count": hits,
                        "remaining": target.get_status_value("蒙蔽"),
                    })

        if name == "坠落":
            duration = x if calc.get("duration") in (None, 0) else calc["duration"]
            grounded = []
            for e in self.state.get_all_player_side() + self.state.get_all_enemy_side():
                if not e.is_alive:
                    continue
                if self._is_flying(e) or e.has_status("坠落"):
                    e.is_flying = False
                    e.status_effects = [s for s in e.status_effects if s.name not in ("飞行", "滑翔")]
                    e.add_status(StatusEffect(name="坠落", remaining_rounds=duration, value=x, source=caster.name))
                    grounded.append(e.name)
            result["effects"].append({"type": "zhuiluo", "targets": grounded, "duration": duration})
        elif ("duration" in calc and calc.get("duration") is not None
              and not (name == "变形" and bianxing_blocked)
              # 波及标记由 use_daowen/怪物结算逐目标处理，不走通用状态块
              # 乱葬岗道纹已在上方乱葬岗段自行 add_status，跳过通用状态处理避免重复叠加
              and name not in ("勾魂", "冥气", "缄默", "镇尸", "瓦解", "波及")):
            duration = calc["duration"] if calc["duration"] != 0 else -1
            effect_target = target if target else caster
            # 自身作用型道纹(变形/超频/自食等)作用于施法者
            # 洗劫：状态应挂在施法者上——"造成伤害时夺取等量碎片"以施法者为触发主体（与 sim/balance_sim 口径一致）
            self_targeted = name in ("超频", "自食", "飞行", "滑翔", "狂暴", "自愈", "必中", "变形", "洗劫", "固执", "贯穿")
            if name == "疯狂":
                # 2026-08-17 用户裁定：疯狂X改为【所有角色出手+X】（全局，变相平衡）。
                # 状态盖到双方全部存活角色；出手口径各自读取自身疯狂状态：
                # 轮回者/朋友/员工走 Entity.action_count，怪物攻击轮数走 _monster_attack_actions。
                for et_all in self.state.get_all_player_side() + self.state.get_all_enemy_side():
                    if not et_all.is_alive:
                        continue
                    et_all.add_status(StatusEffect(name="疯狂", remaining_rounds=duration,
                                                   value=x, source=caster.name))
                    result["effects"].append({"type": "status_added", "target": et_all.name,
                                              "status": name, "duration": duration, "value": x})
            elif self_targeted:
                et = caster
                if name in ("飞行", "滑翔") and self._field_has_zhuiluo():
                    et.is_flying = False
                    et.add_status(StatusEffect(name="坠落", remaining_rounds=1, value=x, source=caster.name))
                    result["effects"].append({"type": "zhuiluo_block_flight", "target": et.name})
                else:
                    et.add_status(StatusEffect(name=name, remaining_rounds=duration, value=x, source=caster.name))
                    result["effects"].append({"type": "status_added", "target": et.name,
                                              "status": name, "duration": duration, "value": x})
            else:
                # 波及：状态类效果对每个拥有波及效果的目标（含本次[目标]）原样生效。
                for et in wave_status_targets:
                    if not et.is_alive:
                        continue
                    et.add_status(StatusEffect(name=name, remaining_rounds=duration, value=x, source=caster.name))
                    result["effects"].append({"type": "status_added", "target": et.name,
                                              "status": name, "duration": duration, "value": x})

        # ---- 龙心谷专属 4 件（F2）：逆鳞/嫁祸/背负/伤痕 的 combat 侧实装 ----
        # 逆鳞X：目标每失去1HP积1层，下次伤害+全部层后清空，持续X（已通过 duration 加状态，此处初始化计数）
        if name == "逆鳞":
            for st_target in wave_status_targets:
                if not hasattr(st_target, "_nilin"):
                    st_target._nilin = 0
                result["effects"].append({"type": "nilin_setup", "target": st_target.name, "x": x})
        # 嫁祸X：自身下X次受伤由目标承担（无持续，仅计数）
        # 存 runtime_id 而非实体引用：自施/互指会形成实体引用环，
        # 事务回滚 _restore_state_in_place 的递归会无限深入（2026-08-22 学习遥测 RecursionError）。
        elif name == "嫁祸":
            caster._jiahuo_left = x
            caster._jiahuo_target = target.runtime_id
            caster.add_status(StatusEffect(name="嫁祸", value=x, remaining_rounds=x, source=caster.name))
            result["effects"].append({"type": "jiahuo", "caster": caster.name, "target": target.name, "count": x})
        # 背负X：目标下X次受伤由自身承担（同样只存 runtime_id）
        elif name == "背负":
            caster._beifu_left = x
            caster._beifu_target = target.runtime_id
            # 在目标侧加标记便于查询
            for st_target in wave_status_targets:
                st_target.add_status(StatusEffect(name="被背负", value=x, remaining_rounds=-1, source=caster.name))
                result["effects"].append({"type": "beifu", "caster": caster.name, "target": st_target.name, "count": x})
        # 伤痕X：目标每次掉血后血限-X，永久（已通过 duration 加伤痕状态，此处仅补日志）
        elif name == "伤痕":
            for st_target in wave_status_targets:
                result["effects"].append({"type": "shanghen", "target": st_target.name, "x": x})

        # ---- F2 全量：罪孽都市（逼债/清算/赌命/消灾/抵扣）的注册与即时结算 ----
        # 逼债X：[回始]使[目标]失去X碎片，否则失去2X血限（二选一）。此处仅挂账，[回始]在 round_start 结算。
        if name == "逼债":
            for st_target in wave_status_targets:
                st_target._bizhai.append({"x": x, "caster": caster})
                st_target.add_status(StatusEffect(name="逼债", value=x, remaining_rounds=-1, source=caster.name))
                result["effects"].append({"type": "bizhai_register", "target": st_target.name, "x": x})
        # 清算X：[回始]使[目标]失去你[碎片]点格挡，持续X。此处仅挂账。
        elif name == "清算":
            for st_target in wave_status_targets:
                st_target._qingsuan.append({"x": x, "caster": caster})
                result["effects"].append({"type": "qingsuan_register", "target": st_target.name, "x": x})
        # 赌命X：玩家侧在_action_use_daowen预检付费；怪物侧由两阶段决策结算器付费。
        # 状态经 duration 挂在施法者上，[回始]在 round_start 按存活角色随机结算。
        elif name == "赌命":
            result["effects"].append({"type": "duming_register", "caster": caster.name, "x": x})
        # 消灾X：玩家侧在_action_use_daowen预检付费；怪物侧由两阶段决策结算器付费；此处登记重投次数。
        elif name == "消灾":
            self.dice.set_rerolls(self.dice.rerolls_pending + x)
            result["effects"].append({"type": "xiaozai_rerolls", "added": x, "total": self.dice.rerolls_pending})
        # 抵扣X：封印[目标]拥有的一件遗物，持续X（目标无遗物则无效果）
        elif name == "抵扣":
            for st_target in wave_status_targets:
                sealed = self._seal_one_relic(st_target, x)
                result["effects"].append({"type": "dikou", "target": st_target.name,
                                          "sealed": sealed or None, "rounds": x})

        # 波及X：标记建立/解除由 use_daowen 与怪物两阶段结算按显式提交逐目标处理，
        # 此处仅登记结算信息（通用状态块已排除波及，避免重复挂状态）。
        if name == "波及":
            result["effects"].append({"type": "boba_register", "target": target.name, "x": x})

        return result



    # 法术流程注册表；计算层只描述步骤，不再替持有者选择X、目标或闪避。
    SPELL_FLOWS = {
        "先发制人": {"trigger": ActionPhase.BEFORE_DAMAGE_TAKEN.value, "steps": [("杀伐", "attacker")]},
        "后发制人": {"trigger": ActionPhase.BEFORE_DAMAGE_TAKEN.value, "steps": [("庇护", "self")]},
        "生生不息": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("再生", "self")]},
        "以牙还牙": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("再生", "self"), ("杀伐", "attacker")]},
        "借力打力": {"trigger": ActionPhase.BEFORE_DAMAGE_TAKEN.value, "steps": [("庇护", "self"), ("杀伐", "attacker")]},
        "不死不休": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("血债", "attacker")], "loop": True},
        "千刀万剐": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("再生", "self"), ("血债", "attacker")], "loop": True},
        "咎由自取": {"trigger": "目标发动道纹前", "steps": [("坠落", "target"), ("杀伐", "target"), ("血债", "target")]},
    }

    # 自创法术文本→执行：解析 trigger_condition / effect_flow 为 SPELL_FLOWS 同构结构。
    # 2026-08-29 重写：接入 engine.spell_dsl（触发时机词汇表扩展、显式目标声明、
    # 条件分支、真循环）。学习环节（engine/api.py._pre_battle_xuexi）已经用同一
    # 个解析器做过强校验，这里理论上不会再遇到解析失败；仍保留 try/except 兜底，
    # 解析失败时返回 None（不触发），而不是让战斗结算抛出未处理异常。
    #
    # 全部 11 种触发时机现已全部接线：
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
        TriggerTiming.ENEMY_ROUND_START.value,
        TriggerTiming.ENEMY_ROUND_END.value,
        ActionPhase.AFTER_DAMAGE_TAKEN.value,
        ActionPhase.BEFORE_LIFE_LOST.value,
    )

    def _parse_custom_spell(self, spell) -> Optional[dict]:
        """把自创法术的文本解析为 SPELL_FLOWS 同构结构；解析失败返回None。"""
        from .spell_dsl import parse_spell_definition, SpellDslError
        try:
            parsed = parse_spell_definition(
                spell.trigger_condition or "", spell.effect_flow or "",
                set(DaoWenEngine.list_all()))
        except SpellDslError:
            return None
        return {"trigger": parsed.trigger, "steps": parsed.steps, "loop": parsed.loop,
                "dsl": True}

    def _eligible_spell_flows(self, holder: Entity, trigger: str) -> dict[str, dict]:
        flows = {}
        if holder is None or not holder.is_alive:
            return flows
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
        from .spell_dsl import ActionStep
        if isinstance(step, ActionStep):
            return step.target
        # 旧内置 SPELL_FLOWS 用 (daowen, role) 元组，role 取值 self/attacker/target。
        return step[1]

    @staticmethod
    def _step_daowen(step) -> str:
        from .spell_dsl import ActionStep
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
        from .spell_dsl import IfStep, evaluate_condition
        resolver = self._condition_resolver(holder, attacker)
        flat = []
        for step in steps:
            if isinstance(step, IfStep):
                branch = step.then_steps if evaluate_condition(step.condition, resolver) else step.else_steps
                flat.extend(branch)
            else:
                flat.append(step)
        return flat

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

        extra_mana：静态校验阶段预计算的[敌回始]守夜灯法力（见
        _shouyedeng_pending_grant）；执行阶段不传（实际法力已含授予值），
        避免重复计算。
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
                flat_steps = self._flatten_flow_steps(flow["steps"], holder, attacker)
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
                        hostile = self.state.on_player_side(holder) != self.state.on_player_side(expected_target)
                        if hostile:
                            if not isinstance(entry.get("dodge"), bool):
                                raise ValueError("敌对法术步骤必须显式提交dodge")
                            if entry["dodge"]:
                                speed_budget[expected_ref] -= 1
                                if speed_budget[expected_ref] < 0:
                                    raise ValueError("法术目标速度不足以闪避")

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

        extra_mana：静态校验阶段预计算的[敌回始]守夜灯法力（见
        _shouyedeng_pending_grant）；执行阶段不传，避免重复计算。
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

        怪物的 spells 恒为空列表（引擎从不给怪物挂法术），扫描全体 refs
        对怪物零开销；死斗对手若通过完整封存快照持有自创法术，同样会被
        正确扫描到（不局限于玩家侧）。
        """
        return {ref: entity for ref, entity in refs.items()
                if entity.is_alive and entity.spells}

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
                entries.append({"spell_name": name, "steps": steps, "loop": bool(flow.get("loop"))})
            result[holder_ref] = entries
        return result

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
            cycle.append({"x": x, "target_ref": reverse.get(id(target)), "dodge": False})
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
                flat_steps = self._flatten_flow_steps(flow["steps"], holder, attacker)
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
                    "options": ["镇压（与所有叛变员工开战）", "让利（本场每名员工工资+5碎片）", "谈判（给出合理方案）"]}
        return {"rebellion": False, "employee_attack_total": emp_atk, "threshold": threshold}

    def trigger_death_legacy(self, legacy: dict[str, str]) -> dict:
        """新增一页三段式遗言；每段必填且不得超过20字。"""
        required = ("trigger_point", "fork", "cost_budget")
        if not isinstance(legacy, dict):
            raise ValueError("遗言必须是包含 trigger_point/fork/cost_budget 的对象")
        if set(legacy) != set(required):
            raise ValueError("遗言字段必须且只能是 trigger_point/fork/cost_budget")

        normalized = {}
        for field_name in required:
            value = legacy[field_name]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"遗言字段 {field_name} 必须是非空字符串")
            value = value.strip()
            if len(value) > self.state.death_book_capacity:
                raise ValueError(
                    f"遗言字段 {field_name} 超过{self.state.death_book_capacity}字上限")
            normalized[field_name] = value

        self.state.death_book_legacies.append(normalized)
        return {
            "triggered": True,
            "legacy": normalized,
            "total_legacies": len(self.state.death_book_legacies),
        }

    def validate_battle_start_relic_choices(self, choices: dict) -> None:
        """校验所有可选[战始]遗物参数；缺项不得由计算层猜测或代选。"""
        if not isinstance(choices, dict):
            raise ValueError("relic_choices必须是对象")
        active = {r.name for r in self.state.relics if self.state.sealed_relics.get(r.name, 0) <= 0}
        player = self.state.player
        for name in ("折速法印", "三相残韵盘"):
            if name not in active:
                continue
            decision = choices.get(name)
            if not isinstance(decision, dict) or not isinstance(decision.get("use"), bool):
                raise ValueError(f"持有【{name}】时必须显式提交relic_choices.{name}.use布尔值")
            if not decision["use"]:
                continue
            if name == "折速法印":
                x = decision.get("x")
                if not isinstance(x, int) or isinstance(x, bool) or x < 1 or not player:
                    raise ValueError("折速法印x必须是正整数")
                self.validate_numeric_cost(
                    player, "疲惫", x, decision.get("cost_share_target_ref", ""))
            elif name == "三相残韵盘":
                resonance = decision.get("resonance_type", "")
                if resonance not in ("转换", "反转", "曲解") or self.state.resonance.get(resonance, 0) < 1:
                    raise ValueError("三相残韵盘必须显式选择一种当前持有的resonance_type")
        for name in ("猩红果实", "苍白之花"):
            if name in active:
                decision = choices.get(name)
                if not isinstance(decision, dict) or not isinstance(decision.get("use"), bool):
                    raise ValueError(f"持有【{name}】时必须显式提交use布尔值")
                if decision["use"] and name == "猩红果实":
                    self.validate_numeric_cost(
                        player, "流血", 10, decision.get("cost_share_target_ref", ""))
                if decision["use"] and name == "苍白之花":
                    self.validate_numeric_cost(
                        player, "疲惫", 5, decision.get("cost_share_target_ref", ""))
        refs = self._combat_entity_refs()
        if "负岳索" in active:
            decision = choices.get("负岳索")
            legal = {ref for ref, entity in refs.items()
                     if entity in self.state.friends + self.state.employees and entity.is_alive}
            if not isinstance(decision, dict) or decision.get("target_ref") not in legal:
                raise ValueError(f"负岳索必须显式选择朋友/员工target_ref，可选{sorted(legal)}")
        if "炉心坠" in active:
            decision = choices.get("炉心坠")
            hearts = {item.name for item in self.state.consumables if item.kind == "dragon_heart"}
            if not isinstance(decision, dict) or decision.get("heart_name") not in hearts:
                raise ValueError(f"炉心坠必须显式选择龙心heart_name，可选{sorted(hearts)}")
        if "烙痕钉" in active:
            decision = choices.get("烙痕钉")
            legal = {ref for ref, entity in refs.items() if self.state.on_enemy_side(entity)}
            target_ref = decision.get("target_ref") if isinstance(decision, dict) else None
            # 普通战斗的第一次战始静态校验发生在抽怪前，此时敌方引用尚未生成。
            # 允许形如 enemy:0 的稳定引用先通过语法校验；抽怪后 process_relics 会再次
            # 调用本校验，并按真实 enemies 集合完成存在性/存活性校验。
            deferred_enemy_ref = (
                not legal and isinstance(target_ref, str)
                and target_ref.startswith("enemy:") and target_ref[6:].isdigit()
            )
            if (not isinstance(decision, dict)
                    or (target_ref not in legal and not deferred_enemy_ref)):
                raise ValueError(f"烙痕钉必须显式选择敌方target_ref，可选{sorted(legal)}")
        using_fatigue = (
            ("折速法印" in active and isinstance(choices.get("折速法印"), dict)
             and choices["折速法印"].get("use"))
            or ("苍白之花" in active and isinstance(choices.get("苍白之花"), dict)
                and choices["苍白之花"].get("use"))
        )
        if "回锋刀" in active and using_fatigue and not self._huifeng_ref_from_choice(choices.get("回锋刀")):
            raise ValueError("回锋刀触发必须显式提交合法敌方目标引用")

    def validate_round_start_relic_choices(self, choices: dict) -> None:
        if not isinstance(choices, dict):
            raise ValueError("relic_choices必须是对象")
        active = {r.name for r in self.state.relics if self.state.sealed_relics.get(r.name, 0) <= 0}
        player = self.state.player
        damage = 3 * max(0, player.speed_limit - player.current_speed) if player else 0
        if "回锋刀" in active and damage > 0:
            decision = choices.get("回锋刀")
            index = decision.get("enemy_index") if isinstance(decision, dict) else None
            if (not isinstance(index, int) or isinstance(index, bool) or index < 0
                    or index >= len(self.state.enemies) or not self.state.enemies[index].is_alive):
                legal = [i for i, enemy in enumerate(self.state.enemies) if enemy.is_alive]
                raise ValueError(f"回锋刀触发时必须显式提交合法relic_choices.回锋刀.enemy_index，可选{legal}")
        if "余火印" in active:
            decision = choices.get("余火印")
            if not isinstance(decision, dict) or not isinstance(decision.get("use"), bool):
                raise ValueError("余火印每个回始必须显式提交use布尔值")
            if decision["use"]:
                heart = next((item for item in self.state.consumables
                              if item.name == decision.get("heart_name") and item.kind == "dragon_heart"), None)
                x = decision.get("x")
                if (heart is None or not isinstance(x, int) or isinstance(x, bool)
                        or x < 1 or x > heart.current_uses):
                    raise ValueError("余火印必须提交合法heart_name与1~当前耐久的x")
        def _validate_blood_pact_decision(holder: Entity, key: str, allow_heart: bool):
            decision = choices.get(key)
            if not isinstance(decision, dict) or not isinstance(decision.get("use"), bool):
                raise ValueError(f"{key}每个回始必须显式提交use布尔值")
            if not decision["use"]:
                return
            x = decision.get("x")
            heart_use = decision.get("dragon_heart_use", 0)
            if not isinstance(x, int) or isinstance(x, bool) or x < 1:
                raise ValueError(f"{key}.x必须是正整数")
            if (not isinstance(heart_use, int) or isinstance(heart_use, bool)
                    or heart_use < 0 or (heart_use and not allow_heart)):
                raise ValueError(f"{key}.dragon_heart_use非法")
            heart = next((item for item in self.state.consumables
                          if item.kind == "dragon_heart" and item.dragon_heart_type == "流血"
                          and not item.is_depleted), None) if allow_heart else None
            offset = min(heart_use, heart.current_uses, 4 * x) if heart else 0
            self.validate_numeric_cost(
                holder, "流血", 4 * x - offset,
                decision.get("cost_share_target_ref", ""),
            )

        if "血契" in active:
            _validate_blood_pact_decision(player, "血契", True)
        opponent = next((entity for entity in self.state.enemies
                         if entity.entity_type == "轮回者" and entity.is_alive), None)
        if opponent is not None and self._has_active_blood_pact(opponent):
            _validate_blood_pact_decision(opponent, "对手血契", False)

    def process_relics(self, trigger: str | TriggerTiming, ctx: dict = None) -> list:
        """遗物效果触发框架。可选效果只读取AI显式提交的ctx，不设置启发式默认值。"""
        timing_map = {
            TriggerTiming.BATTLE_START: "battle_start", TriggerTiming.BATTLE_END: "battle_end",
            TriggerTiming.ROUND_START: "round_start", TriggerTiming.ROUND_END: "round_end",
        }
        trigger = timing_map.get(trigger, trigger)
        ctx = ctx or {}
        player = self.state.player
        logs = []
        if not player:
            return logs
        # 抵扣X（F2）：被封印的遗物在封印期间不触发任何效果
        relics = {r.name for r in self.state.relics if self.state.sealed_relics.get(r.name, 0) <= 0}

        if trigger == "round_start":
            choices = ctx.get("relic_choices", {})
            self.validate_round_start_relic_choices(choices)
            if "回锋刀" in relics:
                d = 3 * max(0, player.speed_limit - player.current_speed)
                if d > 0:
                    enemy = self.state.enemies[choices["回锋刀"]["enemy_index"]]
                    self._remember_huifeng_target(player, f"enemy:{choices['回锋刀']['enemy_index']}")
                    self._apply_hostile_damage(enemy, d, source=player, ctx={
                        "timing": "round_start", "source": "回锋刀", "source_type": "relic",
                        "actor": player, "target": enemy, "owner": player,
                        "mechanic": "damage", "subtype": "round_start_gap", "amount": d,
                        "tags": {"relic", "round_start"},
                    })
                    logs.append(f"回锋刀：对{enemy.name}造{d}伤")
            if "血契" in relics and choices["血契"]["use"]:
                decision = choices["血契"]
                x = decision["x"]
                payment = self.pay_numeric_cost(
                    player, "流血", 4 * x,
                    cost_share_target_ref=decision.get("cost_share_target_ref", ""),
                    dragon_heart_use=decision.get("dragon_heart_use", 0),
                    cost_context={"timing": "round_start", "source": "血契", "source_type": "relic", "tags": {"active_payment"}},
                )
                player.current_mana += x
                self.clamp_immortal_body(player)
                shared = payment.get("shared_with")
                shared_note = f"，与{shared['payer']}共同承担" if shared else ""
                logs.append(f"血契：流血{4*x}{shared_note}，+{x}法力")
            opponent = next((entity for entity in self.state.enemies
                             if entity.entity_type == "轮回者" and entity.is_alive), None)
            if (opponent is not None and self._has_active_blood_pact(opponent)
                    and choices["对手血契"]["use"]):
                decision = choices["对手血契"]
                x = decision["x"]
                payment = self.pay_numeric_cost(
                    opponent, "流血", 4 * x,
                    cost_share_target_ref=decision.get("cost_share_target_ref", ""),
                    cost_context={"timing": "round_start", "source": "血契", "source_type": "relic", "tags": {"active_payment"}})
                opponent.current_mana += x
                self.clamp_immortal_body(opponent)
                shared = payment.get("shared_with")
                shared_note = f"，与{shared['payer']}共同承担" if shared else ""
                logs.append(f"对手血契：流血{4*x}{shared_note}，+{x}法力")
            if "余火印" in relics and choices["余火印"]["use"]:
                x = choices["余火印"]["x"]
                heart = next(item for item in self.state.consumables
                             if item.name == choices["余火印"]["heart_name"] and item.kind == "dragon_heart")
                heart.current_uses -= x
                player.current_mana += 2 * x
                self.clamp_immortal_body(player)
                logs.append(f"余火印：消耗{heart.name}耐久{x}，+{2*x}法力")
        if trigger == "battle_start":
            choices = ctx.get("relic_choices", {})
            self.validate_battle_start_relic_choices(choices)
            using_fatigue = (
                ("折速法印" in relics and isinstance(choices.get("折速法印"), dict)
                 and choices["折速法印"].get("use"))
                or ("苍白之花" in relics and isinstance(choices.get("苍白之花"), dict)
                    and choices["苍白之花"].get("use"))
            )
            if "回锋刀" in relics and using_fatigue:
                ref = self._huifeng_ref_from_choice(choices.get("回锋刀"))
                target = self._combat_entity_refs().get(ref)
                if (target is None or not target.is_alive
                        or not self.state.on_enemy_side(target)):
                    raise ValueError("回锋刀触发必须显式提交合法敌方目标引用")
                self._remember_huifeng_target(player, ref)
            if "折速法印" in relics and choices["折速法印"]["use"]:
                decision = choices["折速法印"]
                x = decision["x"]
                self.pay_numeric_cost(
                    player, "疲惫", x,
                    cost_share_target_ref=decision.get("cost_share_target_ref", ""),
                    cost_context={"timing": "battle_start", "source": "折速法印", "source_type": "relic", "tags": {"active_payment"}})
                player.current_mana += 6 * x
                self.clamp_immortal_body(player)
                logs.append(f"折速法印：疲惫{x}，+{6*x}法力")
            if "三相残韵盘" in relics and choices["三相残韵盘"]["use"]:
                consume = choices["三相残韵盘"]["resonance_type"]
                self.state.resonance[consume] -= 1
                self._sanxiang_consumed = consume
                logs.append(f"三相残韵盘：消耗{consume}残韵")
            if "猩红果实" in relics and choices["猩红果实"]["use"]:
                decision = choices["猩红果实"]
                self.pay_numeric_cost(
                    player, "流血", 10,
                    cost_share_target_ref=decision.get("cost_share_target_ref", ""),
                    cost_context={"timing": "battle_start", "source": "猩红果实", "source_type": "relic", "tags": {"active_payment"}})
                self.state.event_modifiers["scarlet_fruit_active"] = True
                logs.append("猩红果实：流血10；战终血限+2")
            if "苍白之花" in relics and choices["苍白之花"]["use"]:
                decision = choices["苍白之花"]
                self.pay_numeric_cost(
                    player, "疲惫", 5,
                    cost_share_target_ref=decision.get("cost_share_target_ref", ""),
                    cost_context={"timing": "battle_start", "source": "苍白之花", "source_type": "relic", "tags": {"active_payment"}})
                self.state.event_modifiers["pale_flower_active"] = True
                logs.append("苍白之花：疲惫5；战终精力+1")
            # 机制系统：BATTLE_START 相位分发。位置即原缄默面具/帮派令结算位置
            # （缄默面具=5、帮派令=10 同相位按 priority 保持原序；负岳索之前）——顺序与迁移前一致。
            # 帮派令已迁移为声明层 Mechanism（engine/mechanisms/builtins.py）；
            # process_relics 只宣布时点，具体机制条件/效果都在声明层。
            logs.extend(self._dispatch_phase(Phase.BATTLE_START, target=player))
            refs = self._combat_entity_refs()
            if "负岳索" in relics:
                target = refs[choices["负岳索"]["target_ref"]]
                target.add_status(StatusEffect("负岳索", -1, 1, "负岳索"))
                logs.append(f"负岳索：保护{target.name}首次受伤")
            if "炉心坠" in relics:
                heart = next(item for item in self.state.consumables
                             if item.name == choices["炉心坠"]["heart_name"] and item.kind == "dragon_heart")
                heart.current_uses += 10; heart.max_uses += 10
                logs.append(f"炉心坠：{heart.name}耐久+10")
            if "烙痕钉" in relics:
                self.state.event_modifiers["brand_nail_target_ref"] = choices["烙痕钉"]["target_ref"]
                logs.append("烙痕钉：已锁定目标")
            for ally in self.state.friends + self.state.employees:
                if ally.is_alive and any(relic.name == "防弹插板" for relic in ally.relics):
                    ally.gain_shield(15)
                    logs.append(f"防弹插板：{ally.name}+15格挡")
        if trigger == "battle_end" and "三相残韵盘" in relics and self._sanxiang_consumed:
            others = [t for t in ("转换", "反转", "曲解") if t != self._sanxiang_consumed]
            for t in others:
                self.state.resonance[t] = self.state.resonance.get(t, 0) + 1
            logs.append(f"三相残韵盘：战终获得{'、'.join(others)}残韵各1")
        return logs

    # ========== 怪物回合（两阶段显式决策） ==========
    # 怪物已激活的道纹 / 已进化的怪物（均按战斗重置）
    _monster_activated: dict = {}
    _monster_evolved: set = set()  # 进化（原初X）：本场已进化的怪物 id 集合
    _monster_daowen_round_used: dict = {}  # 本回合已发动的道纹（DM裁定2026-08-18：跨回合可重复发动）

    def reset_monster_activation(self):
        """战始重置怪物激活状态与战斗遗物状态"""
        self._monster_activated = {}
        self._monster_evolved = set()  # 进化（原初X）：每场战斗限一次
        self._monster_daowen_round_used = {}
        self._sanxiang_consumed = ""
        self._resonance_rewrites = {}

    def _monster_round_used(self, monster: Entity) -> set:
        """该怪物本回合已发动的道纹集合（换回合自动清空）。

        DM裁定（2026-08-18，README怪物准则9）：怪物可在不同回合重复发动同一
        道纹（冷却类由 can_use 管辖），每回合每道纹至多一次；重复使用的代价
        由道纹递增机制承担（每次实际发动 X+2×副本阶级）。
        _monster_activated 保留为持续激活口径（狂暴出手加成等），不再作发动门禁。
        """
        rec = self._monster_daowen_round_used.get(id(monster))
        if rec is None or rec[0] != self.state.current_round:
            rec = (self.state.current_round, set())
            self._monster_daowen_round_used[id(monster)] = rec
        return rec[1]
    def consume_resonance_rewrite(self, entity: Entity, source: str) -> Optional[str]:
        bucket = self._resonance_rewrites.get(id(entity)) or {}
        dest = bucket.pop(source, None)
        if dest and not bucket:
            self._resonance_rewrites.pop(id(entity), None)
        return dest

    def _monster_attack_actions(self, m: Entity, activated: set) -> int:
        """怪物攻击出手数 = 1 + 疯狂X(自身状态) + 狂暴1(若激活)。

        疯狂2026-08-17全局裁定：发动方把疯狂状态盖到所有角色，怪物从自身状态读+X；
        激活集合口径仅保留给狂暴。发动当回合的状态在resolve阶段才落下，
        prepare在本回合道纹结算前已快照出手数，因此疯狂自下回合生效的时序不变。
        高爆手雷修改的是每轮攻击中的"攻击次数"，不再同时削减攻击出手数。
        """
        n = 1
        n += m.get_status_value("疯狂")
        if "狂暴" in activated or m.has_status("狂暴"):
            n += 1
        return max(0, n)

    def _combat_entity_refs(self) -> dict[str, Entity]:
        """为两阶段决策提供本场稳定的显式目标引用，避免同名实体歧义。"""
        refs: dict[str, Entity] = {}
        if self.state.player and self.state.player.is_alive:
            refs["player:0"] = self.state.player
        for prefix, entities in (
            ("friend", self.state.friends),
            ("employee", self.state.employees),
            ("temp_friend", self.state.temp_friends),
            ("enemy", self.state.enemies),
        ):
            for i, entity in enumerate(entities):
                if not entity.is_alive or entity.has_retreated:
                    continue
                if prefix == "employee" and not entity.is_deployed:
                    continue
                self._bind_hp_hook(entity)  # 确认战斗实体已绑定「失去生命后」兜底钩子（幂等）
                refs[f"{prefix}:{i}"] = entity
        return refs

    def _daowen_requires_target(self, name: str) -> bool:
        import inspect
        DaoWenEngine.register_all()
        func = DaoWenEngine._registry.get(name)
        return bool(func and "target" in inspect.signature(func).parameters)

    def prepare_monster_phase(self) -> dict:
        """只枚举合法选择，不决定道纹、目标或闪避，也不改变战斗数值。"""
        refs = self._combat_entity_refs()
        player_refs = [
            {"ref": ref, "name": e.name}
            for ref, e in refs.items()
            if self.state.on_player_side(e)
        ]
        all_targets = [{"ref": ref, "name": e.name} for ref, e in refs.items()]
        # 白板回合（首回合怪物只普攻不出道纹）不适用于死斗：守擂主将是轮回者，
        # 与挑战者同样应首回合就能发动道纹，否则挑战者首回合秒杀裸奔主将（不对称）。
        whiteboard = self.state.current_round <= 1 and not self.state.in_final_duel
        actors = []
        skipped = []
        for index, monster in enumerate(self.state.enemies):
            if not monster.is_alive or monster.removed_without_kill:
                continue
            actor_ref = f"enemy:{index}"
            if not self.can_act(monster):
                skipped.append({"actor_ref": actor_ref, "monster": monster.name, "reason": "无法行动"})
                continue
            activated = self._monster_activated.get(id(monster), set())
            round_used = self._monster_round_used(monster)
            daowen_options = []
            if not whiteboard and not monster.has_status("干扰"):
                for name, inst in monster.dao_wen.items():
                    if (name in round_used or not inst.can_use()
                            or name not in DaoWenEngine.list_all()):
                        continue
                    if name == "赌命" and getattr(monster, "fake_shards", 0) < inst.x_value:
                        continue
                    if (name == "消灾" and monster.fake_shards < 50 * inst.x_value
                            and monster.shards < 5 * inst.x_value):
                        continue
                    rewritten_as = (self._resonance_rewrites.get(id(monster)) or {}).get(name)
                    effective_name = rewritten_as or name
                    requires_target = self._daowen_requires_target(effective_name)
                    legal_targets = ([target for target in all_targets
                                      if self.is_targetable(monster, refs[target["ref"]])]
                                     if requires_target else [])
                    if "龙威" in self.state.dragon_traits and self.state.player and self.state.player.is_alive:
                        legal_targets = [target for target in legal_targets
                                         if (not self.state.on_player_side(refs[target["ref"]])
                                             or target["ref"] == "player:0")]
                    if requires_target and not legal_targets:
                        continue
                    # 过滤当前付不起数值代价的候选（改写后代价可能超出怪物资源）
                    preview_target = (refs[legal_targets[0]["ref"]] if legal_targets else monster)
                    preview_calc = DaoWenEngine.resolve(
                        effective_name, inst.x_value, target=preview_target, caster=monster)
                    if not self._monster_can_pay_calc_cost(monster, preview_calc):
                        continue
                    # 波及X：必须显式提交X个互不重复的合法目标。DM裁定2026-08-23：
                    # 面板X超过当前合法目标数时按目标数**自适应降X**（有效X=
                    # min(面板X, 合法目标数)），与玩家侧 _max_legal_daowen_x 的
                    # 目标数封顶口径一致——永不因目标不足而不可结算/死锁；仅当
                    # 合法目标数为0时本道纹才真正无法发动，prepare才过滤。
                    # （取代2026-08-22 BUG-01的"不足X即过滤"方案：过滤让面板波及
                    # 怪在solo场上1/111场才开得出火，属于非符合预期效果。）
                    dodge_target_options: list[dict] = []
                    wave_effective_x = 0
                    if effective_name == "波及":
                        dodge_target_options = [target for target in all_targets
                                                if target["ref"] != actor_ref
                                                and self.is_targetable(monster, refs[target["ref"]])]
                        if not dodge_target_options:
                            continue
                        wave_effective_x = min(inst.x_value, len(dodge_target_options))
                    daowen_options.append({
                        "name": name,
                        "resolves_as": effective_name,
                        "x": inst.x_value,
                        "wave_effective_x": wave_effective_x,
                        "requires_target": requires_target,
                        "target_options": legal_targets,
                        "dodge_submission": ("per_target" if effective_name == "波及"
                                             else ("single_if_hostile" if requires_target else "none")),
                        "dodge_target_options": dodge_target_options,
                        "trigger_spell_options": self.prepare_daowen_trigger_spells(monster),
                    })
            attack_targets = [
                target for target in player_refs
                if self.is_targetable(monster, refs[target["ref"]])
            ]
            # 【龙威】是规则约束而非策略默认：敌方只能把持有者列为合法攻击目标。
            if "龙威" in self.state.dragon_traits and self.state.player and self.state.player.is_alive:
                attack_targets = [target for target in attack_targets if target["ref"] == "player:0"]
            for target_option in attack_targets:
                entity = refs[target_option["ref"]]
                blood_pact_options = self.blood_shadow_cost_share_options(entity)
                target_option["can_blood_shadow"] = (
                    self.state.side_has(entity, "血影")
                    and (entity.current_hp > 10 or bool(blood_pact_options)))
                target_option["blood_shadow_cost_share_target_options"] = blood_pact_options
                target_option["spell_options"] = self.prepare_spell_reactions(entity, monster)
                target_option["dodge_relic_target_options"] = [
                    candidate for candidate in all_targets
                    if self.state.on_player_side(refs[candidate["ref"]]) != self.state.on_player_side(entity)
                ] if self.state.side_has(entity, "回锋刀") else []
            # 没有任何合法攻击目标（如solo对手飞行而怪物不飞）→ 本回合不出手。
            # 否则怪物阶段无法被满足：每击都必须引用合法目标，提交永远失败
            # （与【波及】目标数限制同族：prepare不得给出无法满足的义务）。
            base_actions = (0 if not attack_targets
                            else self._monster_attack_actions(monster, activated))
            actors.append({
                "actor_ref": actor_ref,
                "monster": monster.name,
                "daowen_required": bool(daowen_options),
                "daowen_options": daowen_options,
                "attack_target_options": attack_targets,
                "base_attack_actions": base_actions,
                "base_hits_per_attack": max(0, monster.attack_count - monster.get_status_value("手雷减攻")),
                "dodge_must_be_explicit": True,
            })
        return {"round": self.state.current_round, "actors": actors, "skipped": skipped}

    def _monster_can_pay_calc_cost(self, caster: Entity, calc: dict) -> bool:
        """怪物发动道纹前校验数值代价是否付得起（不实际支付）。

        残韵改写/状态变化可能让怪物付不起代价（如速度归零后发动
        【洞察】(疲惫3)）——付不起就不该被 prepare 列为合法项，
        也不该在 resolve 时硬报错。
        """
        if caster is None or not caster.entity_type == "怪物":
            return True
        for key, capacity in (
                ("cost_hp", getattr(caster, "current_hp", 0)),
                ("cost_blood_limit", getattr(caster, "blood_limit", 0)),
                ("cost_speed", getattr(caster, "current_speed", 0)),
        ):
            amount = calc.get(key, 0)
            if amount and capacity < amount:
                return False
        return True

    def _resolve_monster_daowen_choice(
        self, monster: Entity, choice: dict, refs: dict[str, Entity], activated: set,
        prepared_option: dict,
    ) -> dict:
        name = choice.get("name", "")
        inst = monster.dao_wen.get(name)
        if (inst is None or name in self._monster_round_used(monster)
                or not inst.can_use()):
            raise ValueError(f"{monster.name}不能发动道纹【{name}】")
        if name not in DaoWenEngine.list_all():
            raise ValueError(f"未知道纹【{name}】")
        rewritten_as = (self._resonance_rewrites.get(id(monster)) or {}).get(name)
        effective_name = rewritten_as or name
        requires_target = self._daowen_requires_target(effective_name)
        target_ref = choice.get("target_ref", "")
        if requires_target:
            target = refs.get(target_ref)
            if target is None:
                raise ValueError(f"道纹【{effective_name}】必须提交合法target_ref")
            if target is not monster and not self.is_targetable(monster, target):
                raise ValueError(f"目标{target.name}当前不可被{monster.name}选中")
        else:
            if target_ref:
                raise ValueError(f"道纹【{effective_name}】不接受target_ref")
            target = monster

        # 先完成完整闪避提交的静态校验，再支付任何代价或改变激活状态。
        calc = DaoWenEngine.resolve(effective_name, inst.x_value, target=target, caster=monster)
        # 动态代价校验：残韵改写/状态变化后怪物可能付不起代价（如速度归零后
        # 【洞察】(疲惫3)）——本次视为无法发动并跳过，不硬报错、不占出手。
        if not self._monster_can_pay_calc_cost(monster, calc):
            return {"monster": monster.name, "daowen_skipped": name,
                    "resolves_as": effective_name, "reason": "无法支付代价"}
        hostile = self.state.on_player_side(target) != self.state.on_player_side(monster)
        dodge = choice.get("dodge")
        blood_shadow = choice.get("blood_shadow", False)
        aoe_dodge_choices: list[tuple[Entity, bool, dict]] = []
        must_hit_preview = self.bizhong_remaining(monster) > 0
        if effective_name == "波及":
            # 波及X：选择X个[目标]建立/解除波及效果（持续∞）。每个目标显式提交闪避。
            submitted_dodges = choice.get("dodge_targets")
            # DM裁定2026-08-23自适应降X：以prepare快照的wave_effective_x为准
            # （min(面板X, 合法目标数)），驱动与校验始终同一口径。
            mark_count = int(prepared_option.get("wave_effective_x")
                             or calc.get("mark_targets", inst.x_value))
            if not isinstance(submitted_dodges, list) or len(submitted_dodges) != mark_count:
                raise ValueError(f"道纹【波及】必须为{mark_count}个目标显式提交dodge_targets")
            expected_ref_list = [
                target.get("ref") for target in prepared_option.get("dodge_target_options", [])
            ]
            expected_refs = set(expected_ref_list)
            received: dict[str, dict] = {}
            for entry in submitted_dodges:
                if (not isinstance(entry, dict) or not isinstance(entry.get("dodge"), bool)
                        or not isinstance(entry.get("blood_shadow"), bool)
                        or not isinstance(entry.get("target_ref"), str)
                        or entry["dodge"] and entry["blood_shadow"]):
                    raise ValueError("dodge_targets每项必须包含target_ref与布尔值dodge/blood_shadow")
                ref = entry["target_ref"]
                if ref in received or ref not in expected_refs:
                    raise ValueError("波及dodge_targets必须覆盖X个不重复的合法目标")
                received[ref] = entry
            for entry in submitted_dodges:
                ref = entry["target_ref"]
                entity = refs.get(ref)
                if entity is None or not entity.is_alive or entity is monster:
                    raise ValueError("prepare中的波及目标已失效，请重新prepare_monster_phase")
                want_dodge = entry["dodge"]
                if want_dodge and not must_hit_preview and entity.current_speed < 1:
                    raise ValueError(f"{entity.name}速度不足，不能选择闪避")
                if entry["blood_shadow"] and (must_hit_preview or not self.state.side_has(entity, "血影")
                                                or entity.current_hp <= 10):
                    raise ValueError(f"{entity.name}不能使用血影")
                if want_dodge and not must_hit_preview and self.state.side_has(entity, "回锋刀"):
                    allowed = {t_opt["ref"] for t_opt in prepared_option["target_options"]
                               if not self.state.on_player_side(refs[t_opt["ref"]])}
                    if entry.get("dodge_relic_target_ref") not in allowed:
                        raise ValueError("回锋刀触发必须显式提交合法目标")
                aoe_dodge_choices.append((entity, want_dodge, entry))
            if dodge not in (None, False):
                raise ValueError("波及使用dodge_targets，不接受dodge=true")
        elif requires_target and hostile:
            if not isinstance(dodge, bool) or not isinstance(blood_shadow, bool):
                raise ValueError(f"道纹【{effective_name}】必须显式提交布尔值dodge/blood_shadow")
            if dodge and blood_shadow:
                raise ValueError("不能同时闪避并使用血影")
            if dodge and not must_hit_preview and target.current_speed < 1:
                raise ValueError(f"{target.name}速度不足，不能选择闪避")
            if blood_shadow and (must_hit_preview or not self.state.side_has(target, "血影")
                                 or target.current_hp <= 10):
                raise ValueError(f"{target.name}不能使用血影")
            if dodge and not must_hit_preview and self.state.side_has(target, "回锋刀"):
                allowed = {entry["ref"] for entry in prepared_option["target_options"]
                           if self.state.on_player_side(refs[entry["ref"]]) != self.state.on_player_side(target)}
                if choice.get("dodge_relic_target_ref") not in allowed:
                    raise ValueError("回锋刀触发必须显式提交合法目标")
            if choice.get("dodge_targets") not in (None, []):
                raise ValueError(f"道纹【{effective_name}】不接受dodge_targets")
        else:
            if (dodge not in (None, False) or blood_shadow not in (None, False)
                    or choice.get("dodge_targets") not in (None, [])):
                raise ValueError(f"道纹【{effective_name}】当前结算不接受闪避提交")

        trigger_choices = choice.get("trigger_spell_choices", {})
        # 执行阶段：守夜灯法力已在[敌回始]实际授予，无需预计算（extra_mana 默认0）。
        self.validate_daowen_trigger_spells(monster, trigger_choices, refs)
        trigger_logs = self.resolve_daowen_trigger_spells(monster, trigger_choices, refs)
        if not monster.is_alive:
            return {"monster": monster.name, "daowen_activated": name,
                    "interrupted_by_spell": True, "trigger_spell_logs": trigger_logs}

        # 残韵改写只替换本次结算：不支付源道纹代价，也不将源道纹记为已激活。
        if rewritten_as:
            consumed = self.consume_resonance_rewrite(monster, name)
            if consumed != rewritten_as:
                raise ValueError("残韵改写已变化，请重新prepare_monster_phase")
        # 原始怪物道纹发动时支付异变5X；选择导致崩解仍是合法结算，效果中断。
        elif name in self.ORIGINAL_MONSTER_DAOWEN:
            paid = monster.add_mutation(self.YUANCHU_COST_RATE * inst.x_value)
            if paid["collapsed"]:
                # 修复：此前直接 return，崩解死者从不进入统一死亡管线
                # （不产生 _death_ctx、不进 dead_monsters、不触发焦黑发丝/分裂）。
                self._on_entity_death(monster, ctx=self._collapse_context(monster, {
                    "timing": "monster_action", "source": name, "source_type": "daowen",
                    "actor": monster, "target": monster, "mechanic": "cost",
                    "subtype": "mutation", "amount": self.YUANCHU_COST_RATE * inst.x_value,
                    "tags": {"daowen", "active_payment"}}))
                return {"monster": monster.name, "collapsed": name,
                        "note": "支付异变后触发【崩解】，道纹效果中断"}
        elif name == "封印":
            # 封印X：代价：异变8X（2026-08-21）；支付后崩解仍按统一死亡管线结算。
            paid = monster.add_mutation(8 * inst.x_value)
            if paid["collapsed"]:
                self._on_entity_death(monster, ctx=self._collapse_context(monster, {
                    "timing": "monster_action", "source": name, "source_type": "daowen",
                    "actor": monster, "target": monster, "mechanic": "cost",
                    "subtype": "mutation", "amount": 8 * inst.x_value,
                    "tags": {"daowen", "active_payment"}}))
                return {"monster": monster.name, "collapsed": name,
                        "note": "支付异变后触发【崩解】，道纹效果中断"}
        elif name == "赌命":
            if monster.fake_shards < inst.x_value:
                raise ValueError(f"{monster.name}假碎片不足，不能发动【赌命】")
            monster.fake_shards -= inst.x_value
        elif name == "消灾":
            fake_cost, real_cost = 50 * inst.x_value, 5 * inst.x_value
            if monster.fake_shards >= fake_cost:
                monster.fake_shards -= fake_cost
            else:
                # DM裁定2026-08-22（方案A）：怪物的真碎片类代价允许**透支成负债**——
                # 余额不足不再拒绝发动，而是把 shards 扣成负数。负债是【还债】路径的
                # 唯一产生入口；此前余额门禁让怪物碎片守恒≥0，还债在20万+局中零触发
                # （_shards_of 早已设计"负债不抵消假碎片"语义，缺的就是产生入口）。
                # 仅怪物适用：玩家/朋友/员工/api 侧维持余额不足拒绝发动。
                monster.shards -= real_cost

        if not rewritten_as:
            activated.add(name)
            self._monster_round_used(monster).add(name)
            # 怪物道纹递增（DM裁定2026-08-18，README怪物准则9）：每实际发动一次，
            # 该道纹X本场累加+2×副本阶级。只在真正完成发动时累加（无法支付代价、
            # 崩解中断、被控跳过的回合均不计）；残韵改写的一次性结算不递增源道纹。
            # 怪物无法力概念，递增只放大效果数值与真实代价；实例随战斗结束消散。
            from .gamedata import REGION_TIERS
            inst.x_value += 2 * REGION_TIERS.get(self.state.current_region, 1)
        monster.actions_used_this_round += 1

        aoe_targets_override = None
        if effective_name == "波及":
            # 波及X：按显式提交逐目标建立/解除波及标记（持续∞，[战终]清除）。
            wave_marked: list[str] = []
            wave_unmarked: list[str] = []
            for entity, want_dodge, entry in aoe_dodge_choices:
                if must_hit_preview:
                    self.consume_bizhong(monster)
                    marked = self._toggle_wave_mark(entity, monster)
                    (wave_marked if marked else wave_unmarked).append(entity.name)
                elif entry["blood_shadow"]:
                    self.pay_numeric_cost(
                        entity, "流血", 10,
                        cost_share_target_ref=entry.get("cost_share_target_ref", ""),
                        cost_context={"timing": "reaction", "source": "血影", "source_type": "relic", "tags": {"active_payment"}})
                elif want_dodge:
                    self._spend_dodge_speed(entity, entry.get("dodge_relic_target_ref"))
                else:
                    marked = self._toggle_wave_mark(entity, monster)
                    (wave_marked if marked else wave_unmarked).append(entity.name)
            execution = self.apply_daowen_effect(effective_name, calc, monster, target)
            execution["wave_marked"] = wave_marked
            execution["wave_unmarked"] = wave_unmarked
            return {"monster": monster.name, "daowen_activated": name, "x": inst.x_value,
                    "resolves_as": effective_name, "resonance_rewrite": bool(rewritten_as),
                    "target": target.name, "execution": execution,
                    "trigger_spell_logs": trigger_logs}
        elif requires_target and hostile:
            if must_hit_preview:
                self.consume_bizhong(monster)
            elif blood_shadow:
                self.pay_numeric_cost(
                    target, "流血", 10,
                    cost_share_target_ref=choice.get("cost_share_target_ref", ""),
                    cost_context={"timing": "reaction", "source": "血影", "source_type": "relic", "tags": {"active_payment"}})
                return {"monster": monster.name, "daowen_activated": name,
                        "resolves_as": effective_name, "target": target.name, "blood_shadow": True,
                        "trigger_spell_logs": trigger_logs}
            elif dodge:
                self._spend_dodge_speed(target, choice.get("dodge_relic_target_ref"))
                return {"monster": monster.name, "daowen_activated": name,
                        "resolves_as": effective_name, "target": target.name, "dodged": True,
                        "trigger_spell_logs": trigger_logs}

        execution = self.apply_daowen_effect(
            effective_name, calc, monster, target,
            aoe_targets_override=aoe_targets_override,
        )
        return {"monster": monster.name, "daowen_activated": name, "x": inst.x_value,
                "resolves_as": effective_name, "resonance_rewrite": bool(rewritten_as),
                "target": target.name, "execution": execution,
                "trigger_spell_logs": trigger_logs}

    def _validate_monster_daowen_schema(
        self, monster: Entity, choice: dict, refs: dict[str, Entity],
        prepared_option: dict, pending_shouyedeng: int = 0,
    ) -> None:
        """道纹选择的静态 schema 校验（零副作用）。

        只校验与执行状态无关的结构/引用/布尔提交；依赖执行后状态的数值
        （目标当前速度、血影所需生命、本回合已使用集合）留给执行阶段动态
        校验 + 快照回滚兜底，避免把"道纹执行会改变的状态"提前固化。
        """
        name = choice.get("name", "")
        inst = monster.dao_wen.get(name)
        if (inst is None or name in self._monster_round_used(monster)
                or not inst.can_use()):
            raise ValueError(f"{monster.name}不能发动道纹【{name}】")
        if name not in DaoWenEngine.list_all():
            raise ValueError(f"未知道纹【{name}】")
        rewritten_as = (self._resonance_rewrites.get(id(monster)) or {}).get(name)
        effective_name = rewritten_as or name
        requires_target = self._daowen_requires_target(effective_name)
        target_ref = choice.get("target_ref", "")
        if requires_target:
            target = refs.get(target_ref)
            if target is None:
                raise ValueError(f"道纹【{effective_name}】必须提交合法target_ref")
            if target is not monster and not self.is_targetable(monster, target):
                raise ValueError(f"目标{target.name}当前不可被{monster.name}选中")
        else:
            if target_ref:
                raise ValueError(f"道纹【{effective_name}】不接受target_ref")
            target = monster

        hostile = self.state.on_player_side(target) != self.state.on_player_side(monster)
        dodge = choice.get("dodge")
        blood_shadow = choice.get("blood_shadow", False)
        if effective_name == "波及":
            submitted_dodges = choice.get("dodge_targets")
            # DM裁定2026-08-23自适应降X：与执行阶段同一口径（prepare快照）。
            mark_count = int(prepared_option.get("wave_effective_x") or inst.x_value)
            if not isinstance(submitted_dodges, list) or len(submitted_dodges) != mark_count:
                raise ValueError(f"道纹【波及】必须为{mark_count}个目标显式提交dodge_targets")
            expected_refs = {
                t_opt.get("ref") for t_opt in prepared_option.get("dodge_target_options", [])
            }
            received: dict[str, dict] = {}
            for entry in submitted_dodges:
                if (not isinstance(entry, dict) or not isinstance(entry.get("dodge"), bool)
                        or not isinstance(entry.get("blood_shadow"), bool)
                        or not isinstance(entry.get("target_ref"), str)
                        or entry["dodge"] and entry["blood_shadow"]):
                    raise ValueError("dodge_targets每项必须包含target_ref与布尔值dodge/blood_shadow")
                ref = entry["target_ref"]
                if ref in received or ref not in expected_refs:
                    raise ValueError("波及dodge_targets必须提交X个不重复的合法目标")
                received[ref] = entry
            if dodge not in (None, False):
                raise ValueError("波及使用dodge_targets，不接受dodge=true")
        elif requires_target and hostile:
            if not isinstance(dodge, bool) or not isinstance(blood_shadow, bool):
                raise ValueError(f"道纹【{effective_name}】必须显式提交布尔值dodge/blood_shadow")
            if dodge and blood_shadow:
                raise ValueError("不能同时闪避并使用血影")
            if choice.get("dodge_targets") not in (None, []):
                raise ValueError(f"道纹【{effective_name}】不接受dodge_targets")
        else:
            if (dodge not in (None, False) or blood_shadow not in (None, False)
                    or choice.get("dodge_targets") not in (None, [])):
                raise ValueError(f"道纹【{effective_name}】当前结算不接受闪避提交")

        trigger_choices = choice.get("trigger_spell_choices", {})
        self.validate_daowen_trigger_spells(
            monster, trigger_choices, refs, extra_mana=pending_shouyedeng)

    def _validate_monster_phase_static(
        self, submitted: dict[str, dict], prepared: dict,
    ) -> None:
        """怪物阶段全部输入的静态 schema 校验（零副作用）。

        在任何执行（龙息/守夜灯/道纹/攻击）之前拦截与执行状态无关的非法输入：
          - 道纹选择/目标引用/闪避提交结构（_validate_monster_daowen_schema）
          - attack_actions 数量（vs prepare 快照）
          - 每次命中的 target_ref/dodge/blood_shadow 布尔/血影资格/回锋刀目标/法术提交

        依赖执行后状态的数量校验（hits 命中数、目标当前速度、目标存活性）不在此
        判定——它们必须按执行时的真实状态校验（如【变形】会改变命中数），失败由
        resolve_monster_phase 的快照回滚保证零副作用。
        """
        expected = {actor["actor_ref"]: actor for actor in prepared["actors"]}
        refs = self._combat_entity_refs()
        # 守夜灯：[敌回始]在怪物阶段执行时才授予。静态校验先于执行，因此
        # 预计算本次将授予的法力，纳入反应法术预算（否则当前法力=0时
        # 先发制人等合法反应法术会被误判「法力不足」，2026-08-21 实战确认）。
        pending_shouyedeng = 0
        if bool(prepared.get("actors") or prepared.get("skipped")):
            pending_shouyedeng = self._shouyedeng_pending_grant(self.state.player)
        for actor_ref, choice in submitted.items():
            monster = refs.get(actor_ref)
            if monster is None or not monster.is_alive:
                continue  # 与执行循环一致：死斗部分提交/已死者跳过
            expected_actor = expected[actor_ref]

            dao_choice = choice.get("daowen")
            options = {o["name"] for o in expected_actor["daowen_options"]}
            if options and not isinstance(dao_choice, dict):
                raise ValueError(f"{monster.name}必须从合法选项中提交一个daowen对象")
            if not options and dao_choice is not None:
                raise ValueError(f"{monster.name}本次没有合法道纹选项，daowen必须为null")
            if isinstance(dao_choice, dict):
                if dao_choice.get("name") not in options:
                    raise ValueError(f"{monster.name}提交的道纹不在prepare合法选项中")
                prepared_option = next(
                    option for option in expected_actor["daowen_options"]
                    if option["name"] == dao_choice["name"]
                )
                if (prepared_option["requires_target"]
                        and dao_choice.get("target_ref") not in {
                            target["ref"] for target in prepared_option["target_options"]
                        }):
                    raise ValueError(f"{monster.name}提交的道纹目标不在prepare合法选项中")
                self._validate_monster_daowen_schema(
                    monster, dao_choice, refs, prepared_option,
                    pending_shouyedeng=pending_shouyedeng,
                )

            attack_actions = choice.get("attack_actions")
            expected_actions = expected_actor["base_attack_actions"]
            if not isinstance(attack_actions, list) or len(attack_actions) != expected_actions:
                raise ValueError(f"{monster.name}必须提交{expected_actions}个attack_actions")
            legal_attack_options = {
                target["ref"]: target for target in expected_actor["attack_target_options"]
            }
            for attack_action in attack_actions:
                if (not isinstance(attack_action, dict)
                        or not isinstance(attack_action.get("hits"), list)):
                    raise ValueError("每个attack_action必须包含hits列表")
                for hit in attack_action["hits"]:
                    if (not isinstance(hit, dict) or not isinstance(hit.get("dodge"), bool)
                            or not isinstance(hit.get("blood_shadow"), bool)):
                        raise ValueError("每次攻击必须显式提交target_ref、dodge与blood_shadow")
                    if hit["dodge"] and hit["blood_shadow"]:
                        raise ValueError("同一次判定不能同时闪避并使用血影")
                    if hit.get("target_ref") not in legal_attack_options:
                        raise ValueError("怪物攻击目标不在prepare合法选项中")
                    target = refs.get(hit.get("target_ref", ""))
                    if target is None or not self.state.on_player_side(target):
                        raise ValueError("怪物攻击target_ref必须是prepare列出的己方目标")
                    if not self.is_targetable(monster, target):
                        raise ValueError(f"{target.name}当前不可被{monster.name}选中")
                    option = legal_attack_options[hit["target_ref"]]
                    if hit["blood_shadow"] and not option.get("can_blood_shadow"):
                        raise ValueError(f"{target.name}不能使用血影")
                    if hit["dodge"] and self.state.side_has(target, "回锋刀"):
                        allowed = {entry["ref"] for entry in option["dodge_relic_target_options"]}
                        if hit.get("dodge_relic_target_ref") not in allowed:
                            raise ValueError("回锋刀触发必须显式提交合法目标")
                    self.validate_spell_reaction_submission(
                        target, monster, hit.get("spell_choices"), refs,
                        extra_mana=pending_shouyedeng,
                    )

    def _monster_phase_snapshot(self) -> dict:
        """怪物阶段执行前快照：state（含实体/事件流/碎片/消耗品）+ 引擎侧怪物状态。"""
        import copy
        return {
            "state": copy.deepcopy(self.state),
            "activated": copy.deepcopy(self._monster_activated),
            "round_used": copy.deepcopy(self._monster_daowen_round_used),
            "rewrites": copy.deepcopy(self._resonance_rewrites),
            "sanxiang": self._sanxiang_consumed,
            "dice": copy.deepcopy(self.dice),
            "split_spawned": getattr(self, "_split_clones_spawned", 0),
        }

    def _monster_phase_restore(self, snap: dict) -> None:
        """恢复执行前快照：任何非法 resolve 输入/执行中异常都不得留下战斗副作用。

        原地恢复 self.state 的内容（保持 combat.state 与 api 层 engine.state 是
        同一对象引用），并把实体/列表替换为快照副本——外部代码在失败后应重新
        从 state 读取实体，不得继续使用失败前的旧引用。
        """
        state = self.state
        restored = snap["state"]
        state.__dict__.clear()
        state.__dict__.update(restored.__dict__)
        self._monster_activated = snap["activated"]
        self._monster_daowen_round_used = snap["round_used"]
        self._resonance_rewrites = snap["rewrites"]
        self._sanxiang_consumed = snap["sanxiang"]
        self.dice = snap["dice"]
        self._split_clones_spawned = snap["split_spawned"]

    def resolve_monster_phase(self, choices: list[dict], prepared: dict) -> list[dict]:
        """严格按传入的prepare快照验证并结算；任何非法输入由API事务整体回滚。"""
        if not isinstance(choices, list):
            raise ValueError("choices必须是列表")
        if not isinstance(prepared, dict) or not isinstance(prepared.get("actors"), list):
            raise ValueError("prepared必须是prepare_monster_phase返回的合法快照")
        expected = {actor["actor_ref"]: actor for actor in prepared["actors"]}
        submitted: dict[str, dict] = {}
        for choice in choices:
            if not isinstance(choice, dict):
                raise ValueError("每个怪物选择必须是对象")
            ref = choice.get("actor_ref", "")
            if ref in submitted:
                raise ValueError(f"重复提交怪物选择: {ref}")
            submitted[ref] = choice
        # 死斗交替（对称）：守擂侧每步只结算1个actor，其余本步不动
        # （逐出手交替与挑战者侧一致，修复守擂方机制性必胜）。
        if not self.state.in_final_duel and set(submitted) != set(expected):
            raise ValueError(f"必须为全部可行动怪物各提交一次选择；需要{sorted(expected)}，收到{sorted(submitted)}")
        # 事务一致性（2026-08-19）：先完成全部静态 schema 校验（零副作用），
        # 再执行任何龙息/守夜灯/道纹/攻击。依赖执行后状态的动态校验
        # （hits 命中数、目标速度/存活）在执行阶段进行，失败即快照回滚，
        # 保证"任何非法 resolve 输入不得留下任何战斗副作用"。
        self._validate_monster_phase_static(submitted, prepared)
        snapshot = self._monster_phase_snapshot()
        try:
            return self._execute_monster_phase(submitted, prepared, expected)
        except Exception:
            self._monster_phase_restore(snapshot)
            raise

    def _execute_monster_phase(
        self, submitted: dict[str, dict], prepared: dict, expected: dict[str, dict],
    ) -> list[dict]:
        """校验全部通过后的怪物阶段执行（原 resolve_monster_phase 执行体，语义不变）。"""
        refs = self._combat_entity_refs()
        results: list[dict] = []
        enemy_turn = bool(prepared.get("actors") or prepared.get("skipped"))
        if enemy_turn:
            granted = self._grant_shouyedeng(self.state.player)
            if granted:
                results.append(granted)
        opponent = next((entity for entity in self.state.enemies
                         if entity.entity_type == "轮回者" and entity.is_alive), None)
        cleared_opp = self._clear_shouyedeng(opponent)
        if cleared_opp:
            results.append(cleared_opp)
        results.extend(self._tick_baolie(self.state.get_all_enemy_side()))
        results.extend(prepared["skipped"])
        for actor_ref in submitted:  # 死斗部分提交：只结算本步提交的actor
            monster = refs.get(actor_ref)
            if monster is None or not monster.is_alive:
                continue
            choice = submitted[actor_ref]
            breath = self.apply_opposing_longxi(monster)
            if breath:
                results.append({"monster": monster.name, **breath})
                if not monster.is_alive:
                    continue
            activated = self._monster_activated.setdefault(id(monster), set())
            # 攻击出手数以“道纹结算前”的已激活集合为准：狂暴/疯狂是[回始]持续效果，
            # 本回合刚发动时从下回合起生效，prepare列出的 base_attack_actions 也是按
            # 结算前状态给出的——两处必须一致，否则按 prepare 提交必然失败。
            activated_before = set(activated)

            dao_choice = choice.get("daowen")
            options = {o["name"] for o in expected[actor_ref]["daowen_options"]}
            if options and not isinstance(dao_choice, dict):
                raise ValueError(f"{monster.name}必须从合法选项中提交一个daowen对象")
            if not options and dao_choice is not None:
                raise ValueError(f"{monster.name}本次没有合法道纹选项，daowen必须为null")
            if isinstance(dao_choice, dict):
                if dao_choice.get("name") not in options:
                    raise ValueError(f"{monster.name}提交的道纹不在prepare合法选项中")
                prepared_option = next(
                    option for option in expected[actor_ref]["daowen_options"]
                    if option["name"] == dao_choice["name"]
                )
                if (prepared_option["requires_target"]
                        and dao_choice.get("target_ref") not in {
                            target["ref"] for target in prepared_option["target_options"]
                        }):
                    raise ValueError(f"{monster.name}提交的道纹目标不在prepare合法选项中")
                dao_result = self._resolve_monster_daowen_choice(
                    monster, dao_choice, refs, activated, prepared_option,
                )
                results.append(dao_result)
                if not monster.is_alive:
                    continue

            attack_actions = choice.get("attack_actions")
            # 出手数按prepare快照校验：2026-08-17疯狂全局裁定后，状态在本actor
            # 道纹结算中即盖到全场，若此处按当前状态重算会把"自下回合生效"提前到
            # 本回合，导致按prepare提交必然失败；快照即契约（两处必须一致）。
            expected_actions = expected[actor_ref]["base_attack_actions"]
            if not isinstance(attack_actions, list) or len(attack_actions) != expected_actions:
                raise ValueError(f"{monster.name}必须提交{expected_actions}个attack_actions")
            hits_per_action = max(0, monster.attack_count - monster.get_status_value("手雷减攻"))
            for action_index, attack_action in enumerate(attack_actions):
                if not isinstance(attack_action, dict) or not isinstance(attack_action.get("hits"), list):
                    raise ValueError("每个attack_action必须包含hits列表")
                hits = attack_action["hits"]
                if len(hits) != hits_per_action:
                    raise ValueError(f"{monster.name}每个攻击出手必须提交{hits_per_action}次命中选择")
                monster.actions_used_this_round += 1
                legal_attack_options = {
                    target["ref"]: target for target in expected[actor_ref]["attack_target_options"]
                }
                for hit_index, hit in enumerate(hits):
                    if (not isinstance(hit, dict) or not isinstance(hit.get("dodge"), bool)
                            or not isinstance(hit.get("blood_shadow"), bool)):
                        raise ValueError("每次攻击必须显式提交target_ref、dodge与blood_shadow")
                    if hit["dodge"] and hit["blood_shadow"]:
                        raise ValueError("同一次判定不能同时闪避并使用血影")
                    if hit.get("target_ref") not in legal_attack_options:
                        raise ValueError("怪物攻击目标不在prepare合法选项中")
                    target = refs.get(hit.get("target_ref", ""))
                    if target is None or not self.state.on_player_side(target):
                        raise ValueError("怪物攻击target_ref必须是prepare列出的己方目标")
                    if not self.is_targetable(monster, target):
                        raise ValueError(f"{target.name}当前不可被{monster.name}选中")
                    if not target.is_alive:
                        results.append({"attacker": monster.name, "target": target.name,
                                        "skipped": "预选目标已命零", "hit_index": hit_index + 1})
                        continue
                    must_hit = self.bizhong_remaining(monster) > 0
                    if hit["dodge"] and not must_hit and target.current_speed < 1:
                        raise ValueError(f"{target.name}速度不足，不能选择闪避")
                    option = legal_attack_options[hit["target_ref"]]
                    if hit["blood_shadow"] and not option.get("can_blood_shadow"):
                        raise ValueError(f"{target.name}不能使用血影")
                    if hit["dodge"] and not must_hit and self.state.side_has(target, "回锋刀"):
                        allowed = {entry["ref"] for entry in option["dodge_relic_target_options"]}
                        if hit.get("dodge_relic_target_ref") not in allowed:
                            raise ValueError("回锋刀触发必须显式提交合法目标")
                    self.validate_spell_reaction_submission(
                        target, monster, hit.get("spell_choices"), refs,
                    )
                    attack_target = monster if monster.has_status("无神") else target
                    # 无神重定向（README 479：目标强制选自身）：受击方已变为怪物自身，
                    # 但 hit["spell_choices"] 描述的是名义目标（玩家侧）的反应法术——
                    # resolve_attack 会按受击方资格集校验（见 1294 行），键集错配
                    # 必然报"必须逐一覆盖[]"，且此矛盾无法由提交方调和（同一字典需
                    # 同时匹配玩家与怪物的资格集）——引擎契约缺陷，曾占平衡模拟
                    # 无效局 46+/8000（2026-08-22 定位修复）。
                    # 重定向时受击反应按空提交校验（怪物无 spells，资格集恒空）；
                    # 若将来怪物可持反应法术，应新增 hit["self_spell_choices"] 契约字段。
                    reaction_choices = (hit.get("spell_choices")
                                        if attack_target is target else {"before": {}, "after": {}})
                    resolved = self.resolve_attack(
                        monster, attack_target, dodge=hit["dodge"], blood_shadow=hit["blood_shadow"],
                        spell_choices=reaction_choices, entity_refs=refs,
                        dodge_relic_target_ref=hit.get("dodge_relic_target_ref"),
                        cost_share_target_ref=hit.get("cost_share_target_ref", ""),
                    )
                    resolved.update({"hit_index": hit_index + 1, "hit_total": hits_per_action,
                                     "attack_action_index": action_index + 1,
                                     "new_action": (hit_index == 0)})
                    results.append(resolved)
                    if not monster.is_alive:
                        break
                if not monster.is_alive:
                    break
        if not self.state.in_final_duel:
            cleared = self._clear_shouyedeng(self.state.player)
            if cleared:
                results.append(cleared)
        return results

    def buyaicai_escape_cost(self, monster: Entity) -> dict:
        """买路财：失去等同于怪物20%[血限]的[碎片]可安全撤退；碎片不足可用2生命=1碎片补"""
        if not monster:
            return {"can_escape": False, "reason": "无目标"}
        cost = math.ceil(monster.blood_limit * 0.2)
        short = max(0, cost - self.state.shards)
        life_cost = short * 2  # 1碎片=2生命
        return {"can_escape": True, "shard_cost": cost, "shortfall_shards": short,
                "extra_life_cost": life_cost}

    def apply_opposing_longxi(self, actor: Entity) -> Optional[dict]:
        """若对方持有龙息，actor 行动前受 10×当前回合必中伤害。"""
        if actor is None or not actor.is_alive:
            return None
        foe_has = False
        if self.state.on_player_side(actor):
            foe_has = "龙息" in self.state.opponent_dragon_traits
        elif self.state.on_enemy_side(actor):
            foe_has = "龙息" in self.state.dragon_traits
        if not foe_has:
            return None
        dmg = 10 * max(1, self.state.current_round)
        source = (self.state.player if self.state.on_enemy_side(actor)
                  else next((e for e in self.state.enemies if e.entity_type == "轮回者"), None))
        detail = self._apply_hostile_damage(actor, dmg, "必中", source, ctx={
            "timing": self._current_context_timing(), "source": "龙息", "source_type": "relic",
            "actor": source, "target": actor, "mechanic": "damage", "subtype": "dragon_breath",
            "amount": dmg, "tags": {"relic", "must_hit"},
        })
        detail["dragon_breath"] = dmg
        return detail

    def _mediocrity_battle_decided(self) -> bool:
        """凡庸中断判定：战斗胜负已定（统一判定 GameState.battle_over）。

        DM裁定（2026-08-18）：非轮回者优先炸裂后若战斗胜负已定，
        另一方尚未结算的凡庸不再触发。
        """
        return self.state.battle_over()

    def _tick_mediocrity_counters(self, entity: Entity) -> Optional[str]:
        """更新凡庸连续计数；达阈值返回原因，不立刻结算。"""
        if entity.actions_used_this_round <= 0:
            entity.no_action_rounds += 1
        else:
            entity.no_action_rounds = 0
        if entity.damage_dealt_this_round <= 0:
            entity.no_damage_rounds += 1
        else:
            entity.no_damage_rounds = 0
        if entity.no_action_rounds >= 5 or entity.no_damage_rounds >= 5:
            return ("连续五回合未出手" if entity.no_action_rounds >= 5
                    else "连续五回合未能使敌对角色生命减少")
        return None

    def _apply_mediocrity(self, entity: Entity, why: str) -> list[dict]:
        """结算一名角色的凡庸。调用方必须已按非轮回者优先排好序。"""
        if not entity.is_alive:
            return []
        self._hp_loss_recording += 1  # 凡庸直接命零=特殊死因，不触发「失去生命后」
        try:
            entity.current_hp = 0
        finally:
            self._hp_loss_recording -= 1
        self._check_hp_zero_death(entity, ctx={
            "timing": "round_end", "source": "凡庸", "source_type": "system",
            "actor": entity, "target": entity, "mechanic": "death", "subtype": "mediocrity",
            "amount": 0, "tags": {"system", "round_end", "mediocrity"}})
        entity.no_action_rounds = 0
        entity.no_damage_rounds = 0
        if entity is self.state.player:
            self.state.last_death_cause = "mediocrity"
        effects = [{"type": "mediocrity", "entity": entity.name,
                    "note": f"{why}，触发【凡庸】：凭空全身炸裂，[命零]"}]
        if entity.entity_type == "怪物":
            self.state.consumables.append(
                Consumable(name="残骸", effect="局内使用恢复20生命并获得异变10",
                           current_uses=1, max_uses=1))
            effects.append({"type": "mediocrity_loot", "entity": entity.name,
                            "note": "轮回者获得消耗品【残骸】(1/1)"})
        return effects

    def can_act(self, entity: Entity) -> bool:
        """是否可出手（眩晕/束缚下不可）"""
        return (entity.is_alive
                and not entity.has_status("眩晕")
                and not entity.has_status("束缚"))

    def is_targetable(self, attacker: Entity, target: Entity) -> bool:
        """目标是否可被选中。滑翔视同飞行；坠落压住全场飞行。"""
        if self._field_has_zhuiluo() or target.has_status("坠落"):
            return True
        if self._is_flying(target):
            return self._is_flying(attacker)
        return True

    def _get_combat_state(self) -> dict:
        """获取当前战斗状态摘要"""
        return {
            "round": self.state.current_round,
            "player_side": [e.to_dict() for e in self.state.get_all_player_side()],
            "enemy_side": [e.to_dict() for e in self.state.get_all_enemy_side()],
        }
