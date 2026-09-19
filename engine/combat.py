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
# 常量定义在 models（授予点在 Entity.__post_init__），此处只读取以判定效果。
from .models import MONSTER_MANA_RELIC

# 【凡庸】连续无所作为的回合阈值：连续 N 回合未出手、或连续 N 回合未能使敌对角色
# 生命减少 → 凭空全身炸裂。这是**规则层**的反乌龟机制，必须优先于 sim 层的死锁
# 防护（后者只是防卡死的程序兜底，不得抢在规则之前结束战斗，更不得擅定胜负）。
# sim/duel_pvp.py 直接导入本常量推导兜底阈值，避免两处硬编码各自漂移。
# 阈值唯一事实源在 Entity（engine/models.py::Entity.MEDIOCRITY_ROUNDS）。
MEDIOCRITY_ROUNDS = Entity.MEDIOCRITY_ROUNDS


# 实现分片：方法体在 engine/combat_parts/*.py，本文件保留门面与核心结算。
# 拆分不改变任何行为与对外契约（CombatEngine 仍是唯一入口）。
from .combat_parts.cost_payment import CostPaymentMixin
from .combat_parts.damage_death import DamageDeathMixin
from .combat_parts.daowen_effect import DaowenEffectMixin
from .combat_parts.monster_life import MonsterLifeMixin
from .combat_parts.spells import SpellReactionMixin
from .combat_parts.monster_phase import MonsterPhaseMixin


class CombatEngine(
    CostPaymentMixin,
    DamageDeathMixin,
    DaowenEffectMixin,
    MonsterLifeMixin,
    SpellReactionMixin,
    MonsterPhaseMixin,
):
    """战斗计算引擎"""
    
    # 副本专属道纹
    REGION_EXCLUSIVE_DAOWEN = {
        "扭曲都市": {"变形","定型","畸变","搏命","超频","坏死","爆裂","退化"},
        "罪孽都市": {"点金","逼债","抵扣","清算","赎金","假钞","赌命","消灾"},
        "龙心谷":   {"加害","龙鳞","逆鳞","活血","裂变","嫁祸","背负","伤痕"},
    }
    
    # 原始怪物道纹（道纹归属规则：各组起点）——【原初X】可借用范围
    ORIGINAL_MONSTER_DAOWEN = ("狂暴", "全力", "疯狂", "减速", "必中", "自愈", "飞行")
    # 【原初X】借用一种原始怪物道纹的门票＝异变5X（execute_evolution 直接结算）。
    # 2026-09-18 用户令：删除怪物「家族税」（旧口径＝原始怪物道纹每次发动都额外付
    # 异变5X）。发动时只按各道纹自身正文代价支付——狂暴/全力/疯狂/减速/飞行＝异变5X，
    # 必中＝异变X（2026-09-18 用户令由 5X 降价），自愈＝冷却X——一律走统一代价总线，
    # 怪物与轮回者/同伴同口径。
    # 必中为次数型（下X次选择[目标]无法闪避），余数记在 entity._bizhong_left。
    YUANCHU_COST_RATE = 5
    # 波及X（2026-08-21）：你发动的道纹同时作用于所有拥有波及效果的目标。
    # 数值键：效果的总数值在所有目标（本次[目标]+波及目标，均排除施法者自身）
    # 之间平均分配；无法整除时余数按随机数分配。多目标不会复制或增加总数值。
    # 状态类效果（减速按百分比削速/固定面板/持续状态等）对波及目标原样生效，不入下表。
    WAVE_NUMERIC_KEYS = (
        "target_damage", "total_damage", "hits", "aoe_damage", "hp_percent_loss",
        "target_heal", "heal_percent", "heal_missing_percent", "mutation_reduction",
        "target_shield", "shield_drain",
        "blood_limit_reduction", "hp_reduction", "blood_limit_increase", "blood_limit_penalty",
        "attack_boost", "attack_reduction",
        "speed_boost", "speed_penalty",
        "self_attack_count", "invalid_damage_hits",
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
        self._resonance_rewrites: dict[str, dict[str, str]] = {}   # 键＝Entity.runtime_id
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
        self._monster_activated: dict = {}        # 键＝Entity.runtime_id
        self._monster_evolved: set = set()        # 元素＝Entity.runtime_id
        self._monster_daowen_round_used: dict = {}  # 键＝Entity.runtime_id

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
        amount = self.hook_manager.apply_incoming_adjust(target, amount, damage_type, None, self.state)
        return self._apply_parry_reduction(target, amount, damage_type)

    def _apply_parry_reduction(self, target: Entity, amount: int, damage_type: str) -> int:
        """招架（2026-09-13）：本轮每次受到的伤害减去等同当前法力的数值。

        接在 _incoming_adjust 末尾 = 所有伤害通道的公共咽喉（攻击、道纹、反噬
        都经 _apply_hostile_damage → _incoming_adjust），不必逐路径接线。
        减免在格挡之前结算：招架是"卸力"，格挡是"挨下来再吸收"，先卸后吸。
        【代价】不在此列（上面已 return），与格挡口径一致——代价是自己付的，
        不是"受到的伤害"，否则招架会顺带免掉透支的流血，卖血流直接变无代价。
        """
        if amount <= 0 or target is None or not getattr(target, "parrying_this_round", False):
            return amount
        reduction = max(0, target.current_mana)
        if reduction <= 0:
            return amount
        return max(0, amount - reduction)

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
        """任何属性都不得超过其上限（用户裁定 2026-09-13，全局化）。

        原先这条只在持有【不朽之躯】时生效，其余情况允许当前法力/速度超池。
        现改为**无条件对所有角色生效**：当前生命≤[血限]、当前法力≤[法限]、
        当前速度≤[速限]。理由是上限不再只是"初始值"，它同时定义了攻次(速度)
        与攻力(法力)，超池等于凭空突破面板；【不朽之躯】的原文效果因此成为
        通用规则的一部分（该遗物本身保留，不再独占此项）。

        只限制"获得"的当前值，不动上限本身；属性点（修行/无所求）提升的是
        上限，不受本限制。方法名保留兼容既有 12 处调用点与测试。
        """
        if entity is None:
            return
        entity.current_mana = min(entity.current_mana, entity.mana_limit)
        entity.current_speed = min(entity.current_speed, entity.speed_limit)
        entity.current_hp = min(entity.current_hp, entity.blood_limit)

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
        """守夜灯：[回始]将授予的法力量（0=本次不会授予）。

        判定条件与 _grant_shouyedeng 完全一致但不实际发放，供静态校验预计算
        法力预算使用（当前法力=0 时先发制人等合法反应法术不应被误判「法力不足」）。

        用户裁定 2026-09-13：改为 **[回始]获得[法限]10%的法力**（原为
        [敌回始]获得50%、[敌回终]清空）。一池制下这是唯一的常规回血手段，
        10% 只是让长局不至于彻底断供，不构成回填。
        """
        if entity is None or not entity.is_alive or entity.entity_type != "轮回者":
            return 0
        if not self.state.side_has(entity, "守夜灯"):
            return 0
        if entity is self.state.player and self.state.sealed_relics.get("守夜灯", 0) > 0:
            return 0
        if getattr(entity, "_shouyedeng_granted", 0):
            return 0
        return math.ceil(entity.mana_limit * 0.1)   # 全局整数规则：向上取整

    def _grant_shouyedeng(self, entity: Optional[Entity]) -> Optional[dict]:
        """守夜灯：[回始]获得[法限]10%法力，每回合一次，不再清空。"""
        gained = self._shouyedeng_pending_grant(entity)
        if gained <= 0:
            return None
        before = entity.current_mana
        entity.current_mana += gained
        self.clamp_immortal_body(entity)
        # 记实际落地量（上限可能吃掉一部分），供战报如实呈现。
        entity._shouyedeng_granted = entity.current_mana - before
        return {"type": "shouyedeng_grant", "entity": entity.name,
                "gained": entity._shouyedeng_granted, "declared": gained}

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
            act = self._monster_activated.get(entity.runtime_id, set())
            # 疯狂(2026-08-17全局裁定)：状态盖到所有角色，怪物从自身状态读+X。
            # 不再走激活集合分支，避免与全局状态双重计数。
            n += entity.get_status_value("疯狂")
            if "狂暴" in act or entity.has_status("狂暴"):
                n += 1
            n -= entity.get_status_value("无力")
            return max(0, n)
        return DaoWenEngine.single_round_action_count(entity)

    def _current_context_timing(self) -> str:
        forced = getattr(self, "_forced_context_timing", "")
        if forced:
            return forced
        if getattr(self.state, "phase", "") == "pre_battle":
            return "pre_battle"
        sub = getattr(self.state, "combat_subphase", "") or ""
        return sub or getattr(self.state, "phase", "") or "unknown"

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
        # DM裁定 2026-09-10：轮回者每击伤害=当前法力（换算仅限轮回者）
        damage = attacker.effective_attack_power()
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
            # 2026-09-16 用户令：[回始]法力恢复至上限是遗物【某人的偏爱】的效果，
            # 不是怪物种族自带的能力。每只怪物出厂自带该遗物（见 engine/monsters.py），
            # 因此这里读遗物而非读实体类型——"谁持有谁生效"走正常物品逻辑，
            # 可被继承、可被【封印】等机制作用。
            # 微光者（[朋友]/[员工]）与轮回者都不持有，仍是一池制（[回始]不回填）。
            # 这是怪物侧的核心资源优势，也是"让轮回者吃苦头"的主要来源。
            if e.mana_limit > 0 and any(r.name == MONSTER_MANA_RELIC for r in e.relics):
                e.current_mana = e.mana_limit
            e.hp_lost_this_round = 0
            # 招架：上回合招架过 → 本回合禁用；本回合姿态清空等待重新声明。
            # 顺序要紧：先用旧的 parrying 值算出本回合的锁，再清姿态。
            e.parry_locked_this_round = bool(getattr(e, "parrying_this_round", False))
            e.parrying_this_round = False
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
        # DM裁定 2026-09-09：[回始]不再回填法力。法力改为**一池制**——[战始]给满
        # 等同[法限]的一池，整场只出不进，[战终]复原（与[速度]同口径）。
        # 原「勾魂：[回始]不获得法力」随本段一起取消，【勾魂】改为消耗法力翻倍
        # （见 models.py::spend_mana）。

        # 遗物：回始触发（回锋刀按速限缺口造伤）。
        relic_logs = self.process_relics(TriggerTiming.ROUND_START, {"relic_choices": relic_choices or {}})
        effects.extend({"type": "relic", "log": l} for l in relic_logs)
        # 守夜灯（用户裁定 2026-09-13 改为[回始]授予[法限]10%，不再清空）：
        # 玩家与死斗对手（同为轮回者）同口径，在回始遗物之后结算。
        for entity in ([self.state.player] if self.state.player else []) + list(self.state.enemies):
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

        # 【封印X】延迟回场：在第 R+X 回合始把原怪物重新加入敌方列表。
        # 回场当回合记录 spawned_round；2026-09-15 用户令取消白板后，回场当回合
        # 同样可以发动道纹（spawned_round 仅作出生回合记录）。
        delayed = list(getattr(self.state, "delayed_monster_reentries", []) or [])
        due = [entry for entry in delayed if entry.get("return_round", 0) <= self.state.current_round]
        if due:
            for entry in due:
                monster = entry["monster"]
                monster.is_alive = True
                monster.is_departed = False
                monster.departure_reason = ""
                monster.removed_without_kill = False
                monster.spawned_round = self.state.current_round
                monster._delayed_by_seal = False
                self.state.enemies.append(monster)
                self.state.delayed_monster_reentries.remove(entry)
                effects.append({"type": "seal_reentry", "entity": monster.name,
                                "round": self.state.current_round,
                                "delay_rounds": entry.get("delay_rounds", 0)})

        # 波次出怪（2026-09-11 用户令）：R4/R7/R10…回始增援1只直到上限。
        # 死斗无增援；增援怪进场当回合即可发动道纹（2026-09-15 用户令删除白板）。
        queue = list(getattr(self.state, "monster_reinforcements", []) or [])
        if (queue and not self.state.in_final_duel
                and self.state.current_round >= 4
                and (self.state.current_round - 1) % 3 == 0):
            from .monsters import make_monster_entity
            monster_def = self.state.monster_reinforcements.pop(0)
            m = make_monster_entity(monster_def)
            m.spawned_round = self.state.current_round
            self.init_monster_shards(m)
            self.state.enemies.append(m)
            effects.append({"type": "wave_spawn", "entity": m.name,
                            "round": self.state.current_round,
                            "queued_left": len(self.state.monster_reinforcements)})

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

            # DM裁定 2026-09-10：格挡不再[敌回终]每回合清除。
            # 原口径下 4 法力换 8 点格挡、一回合就被抹掉，等于没有价值；改为保留到
            # 被打掉或[战终]统一清除（api.py 战终处理里仍会 clear_shield）。
            # 注意：这使「庇护」的持续1回合失效为整场有效，是本次裁定的预期结果。
            
        # 法力清空（敌回终）
        # 规则：[法限]用于发动道纹与法术，法力[敌回终]清空。
        # 死斗双方都是轮回者，必须与回始同一套循环：每个存活轮回者各自清空。
        # 朋友/员工/怪物没有法限，不走这条。
        # DM裁定 2026-09-09：[敌回终]不再清空法力（法力一池制，只在[战终]复原）。
        
        # 持续效果递减。爆裂按[敌回终]：己方身上在此拍；敌方身上改在怪物回合开始时减。
        player_side_ids = {id(e) for e in self.state.get_all_player_side()}
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            skip = ("爆裂",) if id(entity) not in player_side_ids else ()
            expired = entity.tick_status_effects(skip_names=skip)
            if expired:
                # 只有“持续期间直接改写面板”的效果到期即还原；畸变/伤痕/逼债等
                # 已经产生的累计局内后果保留到战终，再由battle作用域统一回滚。
                panel_modifier_sources = {"全力", "弱化"}
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
                    # 2026-09-17 重做：还原的是互换前的**当前速度与当前法力**
                    # （旧版还原遗留字段 attack_power/attack_count）。
                    # 注意：互换时被上限钳掉的部分不会随还原回来——那是永久损失。
                    entity.current_speed, entity.current_mana = entity._bianxing_original
                    delattr(entity, "_bianxing_original")
                    self.clamp_immortal_body(entity)
                    effects.append({"type": "bianxing_restore", "entity": entity.name,
                                    "current_speed": entity.current_speed,
                                    "current_mana": entity.current_mana})
                # 干扰到期自动由 tick 清理，无需额外
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
    
    # ========== 大流程：员工背叛 / 死之传承 ==========

    def check_employee_rebellion(self) -> dict:
        """
        员工背叛（[战终]检查）：所有[员工]攻击次数×攻击力相加，
        若 ≥ 轮回者当前生命 + 所有[朋友]攻击总值，则所有员工背叛夺取《死者之书》。
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
                    "options": ["镇压（与所有背叛员工开战）", "让利（本场每名员工工资+5碎片）", "谈判（给出合理方案）"]}
        return {"rebellion": False, "employee_attack_total": emp_atk, "threshold": threshold}

    def trigger_death_legacy(self, legacy: dict[str, str] | str) -> dict:
        """新增一页遗言；一句话，上限=`state.death_book_capacity`（默认20字）。

        DM裁定 2026-08-31：遗言废止三段式，只留一句话。校验统一委托
        `engine/death_book.py::validate_legacy`（一个效果只有一个正式执行入口）。
        """
        from .death_book import validate_legacy

        normalized = validate_legacy(legacy, self.state.death_book_capacity)
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
        for name in ("三相残韵盘",):
            if name not in active:
                continue
            decision = choices.get(name)
            if not isinstance(decision, dict) or not isinstance(decision.get("use"), bool):
                raise ValueError(f"持有【{name}】时必须显式提交relic_choices.{name}.use布尔值")
            if not decision["use"]:
                continue
            if name == "三相残韵盘":
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
            "苍白之花" in active and isinstance(choices.get("苍白之花"), dict)
            and choices["苍白之花"].get("use")
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
                "苍白之花" in relics and isinstance(choices.get("苍白之花"), dict)
                and choices["苍白之花"].get("use")
            )
            if "回锋刀" in relics and using_fatigue:
                ref = self._huifeng_ref_from_choice(choices.get("回锋刀"))
                target = self._combat_entity_refs().get(ref)
                if (target is None or not target.is_alive
                        or not self.state.on_enemy_side(target)):
                    raise ValueError("回锋刀触发必须显式提交合法敌方目标引用")
                self._remember_huifeng_target(player, ref)
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
        # 机制系统：BATTLE_END 相位分发。位置=战终遗物段顶部（2026-09-17 新增相位，
        # 首个注册者是改版后的【缄默面具】：[战终]法限+X）。
        # process_relics 只宣布时点，具体机制条件/效果都在声明层。
        if trigger == "battle_end":
            logs.extend(self._dispatch_phase(Phase.BATTLE_END, target=player))
        if trigger == "battle_end" and "三相残韵盘" in relics and self._sanxiang_consumed:
            others = [t for t in ("转换", "反转", "曲解") if t != self._sanxiang_consumed]
            for t in others:
                self.state.resonance[t] = self.state.resonance.get(t, 0) + 1
            logs.append(f"三相残韵盘：战终获得{'、'.join(others)}残韵各1")
        return logs

    # ========== 怪物回合（两阶段显式决策） ==========
    # 怪物已激活的道纹 / 已进化的怪物（均按战斗重置）
    _monster_activated: dict = {}
    _monster_evolved: set = set()  # 进化（原初X）：本场已进化的怪物 runtime_id 集合
    _monster_daowen_round_used: dict = {}  # 本回合已发动的道纹（DM裁定2026-08-18：跨回合可重复发动）


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
        """更新凡庸连续计数；达阈值返回原因，不立刻结算。

        2026-09-18 用户裁定：**护卫算[员工]/[朋友]的一次出手**。正在护卫的盟友
        （`_beifu_left > 0`，来自 `command_ally` 的「护卫 X」指令或【背负】）替轮回者
        承担伤害，这本身就是它本回合的贡献 → 「未出手」计数清零（视为已出手），
        「未使敌掉血」计数**冻结不推进**（它确实没打伤害，但纯护卫不该被【凡庸】炸裂）。
        护卫次数用尽后两条计数照常推进（冻结不清零，之前的累计接着算）。
        """
        if (entity.entity_type in ("朋友", "员工")
                and getattr(entity, "_beifu_left", 0) > 0):
            entity.no_action_rounds = 0
            return None
        if entity.actions_used_this_round <= 0:
            entity.no_action_rounds += 1
        else:
            entity.no_action_rounds = 0
        if entity.damage_dealt_this_round <= 0:
            entity.no_damage_rounds += 1
        else:
            entity.no_damage_rounds = 0
        if (entity.no_action_rounds >= MEDIOCRITY_ROUNDS
                or entity.no_damage_rounds >= MEDIOCRITY_ROUNDS):
            return ("连续五回合未出手" if entity.no_action_rounds >= MEDIOCRITY_ROUNDS
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
        """获取当前战斗状态摘要（含封印暂离队列）。"""
        return {
            "round": self.state.current_round,
            "player_side": [e.to_dict() for e in self.state.get_all_player_side()],
            "enemy_side": [e.to_dict() for e in self.state.get_all_enemy_side()],
            "delayed_monster_reentries": [
                {"name": entry["monster"].name, "return_round": entry["return_round"]}
                for entry in getattr(self.state, "delayed_monster_reentries", [])
            ],
        }
