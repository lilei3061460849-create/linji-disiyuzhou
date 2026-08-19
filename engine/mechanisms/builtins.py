"""已迁移到声明层的机制（当前：加害、龙鳞、自愈、帮派令、衰败、畸变·结算、焦黑发丝）。

迁移协议（迁移前后必须同时满足）：
  1. 规则语义与旧实现完全一致（加害：amount+状态值；龙鳞：max(0, amount-状态值)；
     自愈：无坏死时回复 ceil(血限×10X/100)；帮派令：[战始]获得【洗劫3】；
     衰败：[回始]对自己造成 ceil(当前生命×10X/100) 点伤害，走完整伤害管线；
     畸变·结算：[回终]失去(攻击力×攻击次数)点血限，血限压 0 连带命零统一判定；
     焦黑发丝：怪物命零 → 玩家速度+2（经统一速度入口））；
  2. priority 保持原值或按旧代码位置固化：加害=20、龙鳞=30（伤害加减区）；
     自愈=10、衰败=20（回始效果循环）；帮派令=10（战始遗物段）；
     畸变·结算=10（回终第一循环顶部、凡庸前）；焦黑发丝=10（命零反应第一位）；
  3. 执行路径唯一：伤害相位经 CombatHookManager 上的 MechanismHookAdapter，
     回合/战始/回终相位经 CombatEngine._dispatch_phase，事件机制经 TriggerBus
     （订阅于战斗实例构造时）——旧类/旧 if 已删除。
     注意：回始的【畸变标记】块与【畸变·结算】是同一道纹的两处字面规则，
     本文件只迁移了结算部分（标记块仍为回始循环内的既有报告逻辑）。
"""
from __future__ import annotations

import math

from ..combat_events import CombatEventType
from .conditions import (
    all_, amount_positive, damage_type_not, entity_type, has_status, is_alive,
    not_, relic_active,
)
from .registry import MECHANISMS, Mechanism
from .targets import SELF, TARGET
from .triggers import Phase, Trigger, TriggerContext
from .verbs import apply_verb


def _jiahai_effect(ctx: TriggerContext, targets: list) -> dict:
    """旧 JiahaiHook 语义：amount + status_value（value 缺失按 0）。"""
    value = ctx.target.get_status_value("加害") or 0
    return {"amount": ctx.amount + value}


def _longlin_effect(ctx: TriggerContext, targets: list) -> dict:
    """旧 LonglinHook 语义：max(0, amount - status_value)（value 缺失按 0）。"""
    value = ctx.target.get_status_value("龙鳞") or 0
    return {"amount": max(0, ctx.amount - value)}


def _ziyu_effect(ctx: TriggerContext, targets: list) -> dict:
    """旧 round_start 自愈块语义（逐字复刻）：

    回复 heal = ceil(血限 × 10X / 100)，必须经统一 heal 动词 → apply_heal
    （龙血瓶溢出等既有副作用原样生效）；返回与旧代码同形状的报告条目。
    """
    entity = ctx.target
    x = entity.get_status_value("自愈")
    heal_pct = 10 * x
    heal_amount = math.ceil(entity.blood_limit * heal_pct / 100)
    heal_result = apply_verb(ctx.combat, "heal", {
        "target": entity,
        "amount": heal_amount,
        "ctx": {
            "timing": "round_start", "source": "自愈", "source_type": "daowen",
            "actor": entity, "target": entity, "owner": entity,
            "mechanic": "heal", "subtype": "self_heal", "amount": heal_amount,
            "tags": {"daowen", "round_start"},
        },
    })
    return {
        "type": "self_heal",
        "entity": entity.name,
        "heal": heal_amount,
        "actual": heal_result["actual_heal"],
        "heal_ctx": heal_result.get("heal_ctx"),
    }


JIAHAI = Mechanism(
    name="加害",
    when=Trigger.phase(Phase.INCOMING_ADJUST),
    effect=_jiahai_effect,
    target=TARGET,
    condition=all_(
        amount_positive(),          # amount > 0
        damage_type_not("代价"),    # 代价伤害不受增幅
        has_status("加害", of="target"),
    ),
    priority=20,                    # 原 JiahaiHook.priority = 20，不得调整
)

LONGLIN = Mechanism(
    name="龙鳞",
    when=Trigger.phase(Phase.INCOMING_ADJUST),
    effect=_longlin_effect,
    target=TARGET,
    condition=all_(
        amount_positive(),          # amount > 0
        damage_type_not("代价"),    # 代价伤害不受减免（代价绝对无法被格挡吸收的同族语义）
        has_status("龙鳞", of="target"),
    ),
    priority=30,                    # 原 LonglinHook.priority = 30，不得调整（须后于加害）
)

def _gangpailing_effect(ctx: TriggerContext, targets: list) -> str:
    """旧 process_relics 帮派令块语义（逐字复刻）：

    player.add_status(StatusEffect("洗劫", 3, 3, "帮派令")) → 洗劫 value=3、
    remaining_rounds=3、source=帮派令；经统一 status 动词（统一状态授予入口），
    返回与旧代码完全相同的日志串。
    """
    apply_verb(ctx.combat, "status", {
        "target": ctx.target,
        "name": "洗劫",
        "duration": 3,   # 旧实现 StatusEffect("洗劫", 3, 3, "帮派令")：回合数=3
        "value": 3,      # 层数=3
        "source": "帮派令",
    })
    return "帮派令：获得洗劫3"


ZIYU = Mechanism(
    name="自愈",
    when=Trigger.phase(Phase.ROUND_START),
    effect=_ziyu_effect,
    target=SELF,
    condition=all_(
        has_status("自愈", of="self"),
        not_(has_status("坏死", of="self")),   # 坏死禁疗（与旧条件逐字同义）
    ),
    priority=10,    # 旧代码位置=回始效果循环第一位；后续回始机制按 20/30/... 递增
)

def _shuaibai_effect(ctx: TriggerContext, targets: list) -> dict | None:
    """旧 round_start 衰败块语义（逐字复刻）：

    对自己造成 dmg = ceil(当前生命 × 10X / 100) 点伤害，走**完整伤害管线**
    （damage 动词 → _apply_hostile_damage：格挡/加减区/濒死保护/死后效果原样生效）；
    dmg<=0 时不产生报告条目。来源按状态 source 名字解析（可能为 None）。
    """
    entity = ctx.target
    xv = entity.get_status_value("衰败")
    dmg_n = math.ceil(entity.current_hp * 10 * xv / 100)
    if dmg_n > 0:
        source_name = next((status.source for status in entity.status_effects
                            if status.name == "衰败"), "")
        source_entity = ctx.combat._find_named(source_name)
        rd = apply_verb(ctx.combat, "damage", {
            "target": entity,
            "amount": dmg_n,
            "source": source_entity,
            "ctx": {
                "timing": "round_start", "source": "衰败", "source_type": "daowen",
                "actor": source_entity, "target": entity, "mechanic": "damage",
                "subtype": "dot", "amount": dmg_n,
                "tags": {"daowen", "round_start"},
            },
        })
        return {"type": "shuaibai_tick", "entity": entity.name,
                "damage": rd["actual_damage"], "died": rd["died"]}
    return None


def _jibian_settle_effect(ctx: TriggerContext, targets: list) -> dict:
    """旧 round_end 畸变·结算块语义（逐字复刻）：

    失去 blood_loss = max(0, 攻击力×攻击次数) 点血限（不是伤害）；
    经 blood_limit 动词（统一血限入口：clamp_hp/lethal 默认 True=旧调用同参）；
    即使 blood_loss=0（攻击面板为 0）也照常产生报告条目。
    """
    entity = ctx.target
    blood_loss = max(0, entity.attack_count * entity.attack_power)
    before_limit = entity.blood_limit
    delta = max(0, entity.blood_limit - blood_loss) - entity.blood_limit
    apply_verb(ctx.combat, "blood_limit", {
        "target": entity,
        "delta": delta,
        "source": "畸变",
        "polarity": "debuff",
        "ctx": {
            "timing": "round_end", "source": "畸变", "source_type": "daowen",
            "actor": entity, "target": entity,
            "mechanic": "blood_limit_change", "subtype": "deform",
            "amount": delta, "tags": {"daowen", "round_end"},
        },
        "source_type": "daowen",
        "subtype": "deform",
        "tags": {"daowen", "round_end", "blood_limit_loss"},
    })
    return {
        "type": "deform_blood_limit_loss",
        "entity": entity.name,
        "blood_loss": before_limit - entity.blood_limit,
        "blood_limit_after": entity.blood_limit,
        "hp_after": entity.current_hp,
        "died": not entity.is_alive,
    }


GANGPAILING = Mechanism(
    name="帮派令",
    when=Trigger.phase(Phase.BATTLE_START),
    effect=_gangpailing_effect,
    target=SELF,
    condition=relic_active("帮派令", of="target"),
    # 旧位置=战始遗物段（缄默面具之后、负岳索之前）；后续战始遗物机制按 20/30/... 递增
    priority=10,
)

SHUAIBAI = Mechanism(
    name="衰败",
    when=Trigger.phase(Phase.ROUND_START),
    effect=_shuaibai_effect,
    target=SELF,
    condition=all_(
        has_status("衰败", of="self"),
        is_alive(of="self"),
    ),
    # 旧位置=回始效果循环第二位（自愈=10 之后、洞察之前）；后续回始机制按 30/40/... 递增
    priority=20,
)

def _jiaohheifasi_effect(ctx: TriggerContext, targets: list) -> None:
    """旧 _on_entity_death 焦黑发丝块语义（逐字复刻）：

    怪物命零 → 玩家速度 +2，经 speed 动词 → _gain_speed（加速等既有语义原样）；
    速度事件 ctx 挂在死亡事件下（timing/parent_event_id 取自死亡事件 ctx）。
    """
    death_ctx = (ctx.event.ctx or {}) if ctx.event is not None else {}
    player = ctx.resolve("player")
    apply_verb(ctx.combat, "speed", {
        "target": player,
        "delta": 2,
        "ctx": {
            "timing": death_ctx.get("timing", ""),
            "source": "焦黑发丝", "source_type": "relic",
            "actor": player, "target": player, "owner": player,
            "mechanic": "speed_change", "subtype": "current_speed", "amount": 2,
            "tags": {"relic", "death_trigger"},
            "parent_event_id": death_ctx.get("event_id"),
        },
    })
    return None


JIBIAN_SETTLE = Mechanism(
    name="畸变·结算",
    when=Trigger.phase(Phase.ROUND_END),
    effect=_jibian_settle_effect,
    target=SELF,
    condition=all_(
        has_status("畸变", of="self"),
        is_alive(of="self"),
    ),
    # 旧位置=回终第一逐实体循环顶部（凡庸 tick 之前）；后续回终机制按 20/30/... 递增
    priority=10,
)

JIAOHHEIFASI = Mechanism(
    name="焦黑发丝",
    when=Trigger.event(CombatEventType.ENTITY_DIED),
    effect=_jiaohheifasi_effect,
    target=None,   # 无目标列表：效果对象是玩家，经 ctx.resolve("player") 获取
    condition=all_(
        entity_type("怪物", of="target"),
        relic_active("焦黑发丝", of="player"),   # 玩家持有且未封印（抵扣X）
    ),
    # 旧位置=_on_entity_death 内 ENTITY_DIED 发出点（招魂/分裂之前）；命零反应第一位
    priority=10,
)

MECHANISMS.register(JIAHAI)
MECHANISMS.register(LONGLIN)
MECHANISMS.register(ZIYU)
MECHANISMS.register(GANGPAILING)
MECHANISMS.register(SHUAIBAI)
MECHANISMS.register(JIBIAN_SETTLE)
MECHANISMS.register(JIAOHHEIFASI)

