"""已迁移到声明层的机制（当前：加害、龙鳞、自愈、帮派令）。

迁移协议（迁移前后必须同时满足）：
  1. 规则语义与旧实现完全一致（加害：amount+状态值；龙鳞：max(0, amount-状态值)；
     自愈：无坏死时回复 ceil(血限×10X/100)；帮派令：[战始]获得【洗劫3】，
     持有效遗物且未封印才触发）；
  2. priority 保持原值或按旧代码位置固化：加害=20、龙鳞=30（伤害加减区）；
     自愈=10（回始效果循环第一位）；帮派令=10（战始遗物段：缄默面具之后、负岳索之前）；
  3. 执行路径唯一：伤害相位经 CombatHookManager 上的 MechanismHookAdapter，
     回合/战始相位经 CombatEngine._dispatch_phase——旧类/旧 if 已删除。
"""
from __future__ import annotations

import math

from .conditions import (
    all_, amount_positive, damage_type_not, has_status, not_, relic_active,
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

GANGPAILING = Mechanism(
    name="帮派令",
    when=Trigger.phase(Phase.BATTLE_START),
    effect=_gangpailing_effect,
    target=SELF,
    condition=relic_active("帮派令", of="target"),
    # 旧位置=战始遗物段（缄默面具之后、负岳索之前）；后续战始遗物机制按 20/30/... 递增
    priority=10,
)

MECHANISMS.register(JIAHAI)
MECHANISMS.register(LONGLIN)
MECHANISMS.register(ZIYU)
MECHANISMS.register(GANGPAILING)

