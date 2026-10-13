"""账本（Ledger）：机制的「可累积私有账目」唯一访问入口。

背景（2026-10-03 迁移【逼债】【清算】时新增）：
    两机制的旧实现把账目直接挂在实体私有列表上，访问点散落三处——
      * 写入：`combat_parts/daowen_effect.py`（发动时挂账）；
      * 读取+清账：`combat.py` 回始结算块与回终清账块；
      * 清账：`api.py` 战终统一重置。
    迁移后所有访问都必须经过本模块（`ledger_of` / `clear_ledger`），
    机制声明层与管线层不再各写一份 `entity._bizhai.append(...)`。

存储：
    仍走 Entity 的既有字段（`_bizhai` / `_qingsuan`，dataclass 字段，随存档走），
    不新造全局表、不改序列化口径——迁移只搬家访问入口，不搬家数据。
    `_mechanism_state`（registry.Mechanism.state_of）留给不需要跨存档的机制状态。

约定：
    账目是**列表**（同一次战斗可叠加多笔，逐笔结算），不是计数器；
    列表为空 = 无账。清理的时机由机制自己在声明层决定（见 builtins 的「对账」机制）。
"""
from __future__ import annotations

from typing import Any

# 机制名 → Entity 存储字段。新增账本制机制时在此登记一行即可。
_STORAGE: dict[str, str] = {
    "逼债": "_bizhai",
    "清算": "_qingsuan",
}


def ledger_names() -> list[str]:
    """已登记的账本机制名（供审计/测试枚举）。"""
    return sorted(_STORAGE)


def _storage_attr(mechanism: str) -> str:
    try:
        return _STORAGE[mechanism]
    except KeyError:
        raise ValueError(
            f"机制[{mechanism}]未登记账本存储；请在 engine/mechanisms/ledger.py 的 _STORAGE 中登记"
        ) from None


def ledger_of(entity: Any, mechanism: str) -> list:
    """取得该实体在该机制上的账目列表（惰性创建，返回**可写**列表）。"""
    attr = _storage_attr(mechanism)
    entries = getattr(entity, attr, None)
    if entries is None:
        entries = []
        setattr(entity, attr, entries)
    return entries


def clear_ledger(entity: Any, mechanism: str) -> int:
    """清空账目，返回被清掉的笔数（0 = 本来就是空的，调用方可据此跳过）。"""
    attr = _storage_attr(mechanism)
    entries = getattr(entity, attr, None) or []
    count = len(entries)
    if count:
        setattr(entity, attr, [])
    return count
