"""快照拷贝工具：给「预演沙盒」和「事务回滚」用的状态/随机源复制。

为什么要单独一层：``copy.deepcopy(GameState)`` 的成本几乎全部来自**只追加的
事实记录**——战斗事件流 ``combat_events`` 在长局里可以到几千条，实测占整份
状态深拷贝的 95% 以上（4200 条事件：33.7ms，其中 32.7ms 是事件本身）。而这类
记录按 ``engine/combat_events.py`` 的既有契约是**不可变事实源**：

    \"单一事实源不可变事件流\" —— 事件登记后只读，只追加、不原地修改。

所以快照只复制**容器**（列表本身照常新建，长度与顺序与真实状态一致），
事件对象共享引用：预演沙盒用后即弃，事务快照失败时按长度/内容还原，两者都
不需要事件对象的私有副本。若将来有代码要**原地修改**已登记事件，必须同时
改这里——否则预演会污染真实事件流。

随机源两种口径（见 ``DiceEngine``）：

* ``copy_dice_for_snapshot(keep_history=False)``：**预演沙盒专用**。预演结束时
  整份沙盒随机源被丢弃，只需要「同样的随机状态 + 同样的池」；把整份 roll
  历史再复制一遍纯属重复劳动（实测与整个 GameState 同价，且随局数增长）。
* ``keep_history=True``：**事务回滚专用**。回滚要还原的是可被外部读取的
  真实随机源，roll 历史是它的一部分（``dice.get_history()`` 对外可见），
  必须原样复制。
"""
from __future__ import annotations

import copy
import random
from typing import Any


# 快照共享的「只追加事实记录」字段：容器照常复制，元素共享引用。
_SHARED_RECORD_FIELDS = ("combat_events",)


def copy_state_for_snapshot(state: Any) -> Any:
    """复制一份状态快照：只追加的事实记录共享元素，其余照常深拷贝。"""
    memo: dict[int, Any] = {}
    for field in _SHARED_RECORD_FIELDS:
        for record in getattr(state, field, None) or []:
            memo[id(record)] = record
    return copy.deepcopy(state, memo)


def copy_dice_for_snapshot(dice: Any, *, keep_history: bool = True) -> Any:
    """复制随机源。

    keep_history=True：完整复制（事务回滚口径，roll 历史对外可见）。
    keep_history=False：只复制随机状态与命名池，历史留空（沙盒口径，
    沙盒里的 roll 结果对象本身仍带 record，调用方读 record 不受影响）。
    """
    if keep_history:
        return copy.deepcopy(dice)
    clone = copy.copy(dice)
    clone._pools = {name: list(options) for name, options in dict(dice._pools).items()}
    clone._history = []
    rng = random.Random()
    rng.setstate(dice._rng.getstate())
    clone._rng = rng
    return clone
