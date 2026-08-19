"""已迁移到声明层的机制（MVP 只迁移一个：【加害】）。

迁移协议（迁移前后必须同时满足）：
  1. 规则语义与旧 JiahaiHook 完全一致：amount>0、代价除外、状态值相加（缺省按0）；
  2. priority 保持原值 20——加害必须先于龙鳞(30)，顺序即规则；
  3. 执行路径唯一：经 CombatHookManager 上的 MechanismHookAdapter 执行，
     旧 JiahaiHook 类已删除，不存在两条分发路径。
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

MECHANISMS.register(JIAHAI)
