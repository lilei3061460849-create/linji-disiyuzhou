"""消耗品通用被动：自愈X（2026-10-14 用户令）。

**自愈X**：恢复 X 点当前耐久，不超过耐久上限。X 为固定点数（如自愈1）；
【无限肉块】的「自愈80%已损耐久」是比例特例（同族不同参）。

触发时机写在消耗品文本里，由本表的 kind 条目声明；将来剧情新增消耗品
只需加一行声明，不写新引擎代码（A（5/10）即按此形状举例，未落地）。

时机：
- battle_end（规则文本：战终）；
- target_death（规则文本：目标死亡时）＝持有者本场战斗出手选中过的目标死亡
  （2026-10-14 用户裁定：你选中的目标才算，没打过的不算；命中与否不影响“选中”，
  被闪避/挡掉照样算。只看选中集合，不限敌我、不问击杀者）。
  离场/暂离不是死亡，不走统一命零管线，天然排除。

数值规格 spec：
- int：固定点数（如 1＝自愈1）；
- ("percent_lost", p)：已损耐久的 p%，向上取整（仅无限肉块在用）。
"""
from __future__ import annotations

import math
from typing import Any

ZIYU_TRIGGER_BATTLE_END = "battle_end"
ZIYU_TRIGGER_TARGET_DEATH = "target_death"

# kind → [(时机, 数值规格)]
CONSUMABLE_ZIYU_TRIGGERS: dict[str, list[tuple[str, Any]]] = {
    "infinite_meat": [(ZIYU_TRIGGER_BATTLE_END, ("percent_lost", 80))],
}


def apply_zi_yu_trigger(items: list | None, trigger: str) -> list[dict]:
    """结算一批消耗品在某时机上的自愈，返回逐件日志。

    每件匹配条目必记一条日志（含 restored=0 的满耐久件，与旧口径一致）。
    """
    logs: list[dict] = []
    for item in items or []:
        entries = CONSUMABLE_ZIYU_TRIGGERS.get(getattr(item, "kind", ""), ())
        for trg, spec in entries:
            if trg != trigger:
                continue
            room = max(0, item.max_uses - item.current_uses)
            if isinstance(spec, tuple) and spec and spec[0] == "percent_lost":
                restored = min(room, math.ceil(room * spec[1] / 100)) if room else 0
            else:
                restored = min(room, max(0, int(spec)))
            if restored:
                item.current_uses += restored
            logs.append({"item": item.name, "trigger": trigger, "lost": room,
                         "restored": restored, "current_uses": item.current_uses,
                         "max_uses": item.max_uses})
    return logs


def note_action_targets(state, actor, targets) -> None:
    """记录一次成功出手的显式目标（本场战斗选中集合）。

    只记持有者（轮回者）本人的选择：朋友/员工代发动、怪物的出手不记。
    调用点保证：只在成功提交后调用；X=0、非法提交、默认自身（非显式目标）不传进来。
    """
    if actor is None or state is None or actor is not getattr(state, "player", None):
        return
    seen = set(getattr(state, "player_selected_targets", None) or [])
    for t in targets or []:
        rid = getattr(t, "runtime_id", "")
        if rid and rid not in seen:
            seen.add(rid)
            state.player_selected_targets.append(rid)
