"""统一效果来源上下文（兼容层）。

本模块只提供轻量描述对象，不接管现有战斗结算。
核心目标：让已迁移的监听器不再仅凭字段变化/phase 猜来源。

迁移策略：核心入口继续接受 ctx=None；已迁移调用点显式传 EffectContext/dict。
上下文只保存 parent_event_id，不嵌套完整 parent，避免事件链无限膨胀。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count
from typing import Any, Mapping, Optional


_event_counter = count(1)


@dataclass(frozen=True)
class EffectContext:
    timing: str = ""
    source: str = ""
    source_type: str = ""
    actor: Any = None
    target: Any = None
    owner: Any = None
    mechanic: str = ""
    subtype: Optional[str] = None
    amount: Optional[int] = None
    tags: frozenset[str] = field(default_factory=frozenset)
    event_id: str = ""
    parent_event_id: Optional[str] = None

    def to_dict(self) -> dict:
        """返回可序列化的浅层字典；实体只保留名字，避免日志/快照递归。"""
        def _entity_ref(entity: Any) -> Any:
            if entity is None:
                return None
            return getattr(entity, "name", str(entity))

        return {
            "timing": self.timing,
            "source": self.source,
            "source_type": self.source_type,
            "actor": _entity_ref(self.actor),
            "target": _entity_ref(self.target),
            "owner": _entity_ref(self.owner),
            "mechanic": self.mechanic,
            "subtype": self.subtype,
            "amount": self.amount,
            "tags": sorted(self.tags),
            "event_id": self.event_id,
            "parent_event_id": self.parent_event_id,
        }


def next_event_id(prefix: str = "evt") -> str:
    return f"{prefix}-{next(_event_counter)}"


def make_context(
    *,
    timing: str = "",
    source: str = "",
    source_type: str = "",
    actor: Any = None,
    target: Any = None,
    owner: Any = None,
    mechanic: str = "",
    subtype: Optional[str] = None,
    amount: Optional[int] = None,
    tags: Optional[set[str] | frozenset[str] | list[str] | tuple[str, ...]] = None,
    event_id: Optional[str] = None,
    parent_event_id: Optional[str] = None,
) -> EffectContext:
    return EffectContext(
        timing=timing,
        source=source,
        source_type=source_type,
        actor=actor,
        target=target,
        owner=owner,
        mechanic=mechanic,
        subtype=subtype,
        amount=amount,
        tags=frozenset(tags or ()),
        event_id=event_id or next_event_id(),
        parent_event_id=parent_event_id,
    )


def normalize_context(ctx: EffectContext | Mapping[str, Any] | None) -> Optional[EffectContext]:
    """兼容 dict / EffectContext / None。未知字段被忽略。"""
    if ctx is None:
        return None
    if isinstance(ctx, EffectContext):
        return ctx
    if isinstance(ctx, Mapping):
        tags = ctx.get("tags") or ()
        if isinstance(tags, str):
            tags = (tags,)
        return make_context(
            timing=str(ctx.get("timing") or ""),
            source=str(ctx.get("source") or ""),
            source_type=str(ctx.get("source_type") or ""),
            actor=ctx.get("actor"),
            target=ctx.get("target"),
            owner=ctx.get("owner"),
            mechanic=str(ctx.get("mechanic") or ""),
            subtype=ctx.get("subtype"),
            amount=ctx.get("amount"),
            tags=set(tags),
            event_id=ctx.get("event_id") or None,
            parent_event_id=ctx.get("parent_event_id"),
        )
    raise TypeError(f"ctx must be EffectContext, mapping, or None; got {type(ctx)!r}")

