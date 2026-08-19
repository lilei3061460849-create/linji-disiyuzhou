"""已迁移到声明层的机制（当前：加害、龙鳞）。

迁移协议（迁移前后必须同时满足）：
  1. 规则语义与旧 Hook 完全一致（加害：amount+状态值；龙鳞：max(0, amount-状态值)，
     两者均要求 amount>0、代价除外、状态值缺省按 0）；
  2. priority 保持原值：加害=20、龙鳞=30——加害必须先于龙鳞，顺序即规则；
  3. 执行路径唯一：经 CombatHookManager 上的 MechanismHookAdapter 执行，
     旧 JiahaiHook / LonglinHook 类已删除，不存在两条分发路径。
"""
from __future__ import annotations

from .conditions import all_, amount_positive, damage_type_not, has_status
from .registry import MECHANISMS, Mechanism
from .targets import TARGET
from .triggers import Phase, Trigger, TriggerContext


def _jiahai_effect(ctx: TriggerContext, targets: list) -> dict:
    """旧 JiahaiHook 语义：amount + status_value（value 缺失按 0）。"""
    value = ctx.target.get_status_value("加害") or 0
    return {"amount": ctx.amount + value}


def _longlin_effect(ctx: TriggerContext, targets: list) -> dict:
    """旧 LonglinHook 语义：max(0, amount - status_value)（value 缺失按 0）。"""
    value = ctx.target.get_status_value("龙鳞") or 0
    return {"amount": max(0, ctx.amount - value)}


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

MECHANISMS.register(JIAHAI)
MECHANISMS.register(LONGLIN)

