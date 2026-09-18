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
from .combat_parts.daowen_effect import DaowenEffectMixin
from .combat_parts.monster_life import MonsterLifeMixin
from .combat_parts.spells import SpellReactionMixin
from .combat_parts.monster_phase import MonsterPhaseMixin


class CombatEngine(
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
