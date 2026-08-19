"""Target：规则里"对谁"的标准词汇。

选择器 = 触发器快照(TriggerContext) → 实体列表。
每个词汇的语义按现有代码实际口径定义，不发明新含义；真正特殊的目标才允许 custom()。

例："使所有敌人获得1点疯狂" → target = ALL_ENEMIES，而不是每个机制手写 lambda。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .triggers import TriggerContext


@dataclass(frozen=True)
class TargetSelector:
    name: str
    select: Callable[[TriggerContext], list]

    def __call__(self, ctx: TriggerContext) -> list:
        return self.select(ctx)


def custom(fn: Callable[[TriggerContext], list], *, name: str = "custom") -> TargetSelector:
    """确实特殊的目标才允许手写选择器（审计报告 §7）。"""
    return TargetSelector(name=name, select=fn)


def _state(ctx: TriggerContext):
    return ctx.state or getattr(ctx.combat, "state", None)


def _select_subject(ctx: TriggerContext) -> list:
    return [ctx.target] if ctx.target is not None else []


def _select_source(ctx: TriggerContext) -> list:
    return [ctx.source] if ctx.source is not None else []


def _select_all(ctx: TriggerContext) -> list:
    state = _state(ctx)
    if state is None:
        return []
    return list(state.get_all_player_side() + state.get_all_enemy_side())


def _side_of(ctx: TriggerContext):
    """触发主体所在阵营；无法判定时返回 None。"""
    state = _state(ctx)
    if state is None:
        return None
    subject = ctx.target or ctx.source
    if subject is None:
        return None
    checker = getattr(state, "on_player_side", None)
    if checker is None:
        return None
    return bool(checker(subject))


def _select_allies(ctx: TriggerContext) -> list:
    state = _state(ctx)
    if state is None:
        return []
    side = _side_of(ctx)
    if side is None:
        return []
    pool = state.get_all_player_side() if side else state.get_all_enemy_side()
    return [e for e in pool if e is not ctx.target]


def _select_enemies(ctx: TriggerContext) -> list:
    state = _state(ctx)
    if state is None:
        return []
    side = _side_of(ctx)
    if side is None:
        return []
    return list(state.get_all_enemy_side() if side else state.get_all_player_side())


def _select_random_enemy(ctx: TriggerContext) -> list:
    enemies = _select_enemies(ctx)
    if not enemies:
        return []
    combat = ctx.combat
    dice = getattr(combat, "dice", None) if combat is not None else None
    if dice is not None and hasattr(dice, "auto_roll"):
        # 与引擎其它随机选择同口径：走 DiceEngine，保证可复现。
        roll = dice.auto_roll(
            "mechanism_random_enemy", [e.name for e in enemies],
            context="机制目标选择·随机敌人")
        idx = max(0, int(roll["player_number"]) - 1)
        return [enemies[min(idx, len(enemies) - 1)]]
    import random
    return [random.choice(enemies)]


def _select_dead(ctx: TriggerContext) -> list:
    from ..combat_events import CombatEventType
    if ctx.event is not None and ctx.event.event_type == CombatEventType.ENTITY_DIED:
        return [ctx.target] if ctx.target is not None else []
    return []


SELF = TargetSelector("self", _select_subject)        # 触发主体（相位路径=受伤者）
TARGET = TargetSelector("target", _select_subject)    # 同 SELF：触发器指向的目标
SOURCE = TargetSelector("source", _select_source)     # 行为发起者
ALL = TargetSelector("all", _select_all)              # 双方全部存活实体
ALL_ALLIES = TargetSelector("all_allies", _select_allies)    # 触发主体同阵营（不含主体）
ALL_ENEMIES = TargetSelector("all_enemies", _select_enemies)  # 触发主体敌对阵营
RANDOM_ENEMY = TargetSelector("random_enemy", _select_random_enemy)
DEAD_ENTITY = TargetSelector("dead_entity", _select_dead)  # ENTITY_DIED 事件的死者
