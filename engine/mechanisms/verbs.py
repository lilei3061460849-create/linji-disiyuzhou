"""Verb：基础行为动词注册表。

"同一种基础行为只实现一次"。每个动词的实现体都是现有统一结算入口，
**不新增任何游戏逻辑**。机制层只通过 apply_verb 执行，禁止直接改
current_hp / current_mana / is_alive。

没有真正统一入口的行为不强行注册：
- mana：2026-08-19 起已注册（三个现存站点语义可被逐字覆盖：洞察/勾魂/缄默面具）。
- redirect / mitigate / reflect / split / spawn：特殊控制流，MVP 不迁移
  （审计报告 §3.3 的"命名动词"清单，后续阶段处理）。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

VerbFn = Callable[[Any, dict, Optional[dict]], Any]

_VERBS: dict[str, VerbFn] = {}


def register_verb(name: str, fn: VerbFn) -> None:
    if name in _VERBS:
        raise ValueError(f"动词[{name}]重复注册")
    _VERBS[name] = fn


def get_verb(name: str) -> VerbFn:
    try:
        return _VERBS[name]
    except KeyError:
        raise ValueError(f"未注册动词: {name}") from None


def apply_verb(combat, name: str, spec: dict, ctx=None) -> Any:
    """机制层唯一允许的执行入口：按动词名调用现有统一结算。"""
    return get_verb(name)(combat, spec, ctx)


def verb_names() -> list[str]:
    return sorted(_VERBS)


def _ctx_of(spec: dict, fallback_ctx) -> Optional[dict]:
    return spec.get("ctx", fallback_ctx)


# ---- 各动词实现体 = 现有统一入口的薄包装（零新逻辑） ----


def _verb_damage(combat, spec, ctx):
    return combat._apply_hostile_damage(
        spec["target"], spec["amount"],
        damage_type=spec.get("damage_type", "普通"),
        source=spec.get("source"),
        ctx=_ctx_of(spec, ctx),
    )


def _verb_heal(combat, spec, ctx):
    return combat.state.apply_heal(spec["target"], spec["amount"], ctx=_ctx_of(spec, ctx))


def _verb_hp_loss(combat, spec, ctx):
    return combat._raw_hp_loss(spec["target"], spec["amount"], ctx=_ctx_of(spec, ctx))


def _verb_blood_limit(combat, spec, ctx):
    return combat._apply_blood_limit_change(
        spec["target"], spec["delta"], spec.get("source", "mechanism"),
        spec.get("polarity", "debuff"),
        ctx=_ctx_of(spec, ctx),
        source_type=spec.get("source_type", ""),
        subtype=spec.get("subtype", ""),
        actor=spec.get("actor"),
        owner=spec.get("owner"),
        tags=spec.get("tags"),
        clamp_hp=spec.get("clamp_hp", True),
        lethal=spec.get("lethal", True),
    )


def _verb_cost(combat, spec, ctx):
    return combat.pay_numeric_cost(
        spec["payer"], spec["cost_type"], spec["amount"],
        cost_share_target_ref=spec.get("cost_share_target_ref", ""),
        dragon_heart_use=spec.get("dragon_heart_use", 0),
        cost_context=spec.get("cost_context", _ctx_of(spec, ctx)),
    )


def _verb_status(combat, spec, ctx):
    from ..models import StatusEffect
    target = spec["target"]
    effect = StatusEffect(
        name=spec["name"],
        remaining_rounds=spec.get("duration", -1),
        value=spec.get("value", spec.get("amount", 0)),
        source=spec.get("source", "mechanism"),
    )
    target.add_status(effect)  # 统一状态授予入口
    return {"status": effect.name, "value": effect.value,
            "target": getattr(target, "name", "")}


def _verb_speed(combat, spec, ctx):
    delta = spec.get("delta", spec.get("amount", 0))
    if delta > 0:
        return combat._gain_speed(spec["target"], delta, ctx=_ctx_of(spec, ctx))
    if delta < 0:
        return combat._lose_current_speed(spec["target"], -delta, ctx=_ctx_of(spec, ctx))
    return 0


def _verb_shield(combat, spec, ctx):
    target = spec["target"]
    target.gain_shield(spec.get("amount", spec.get("value", 0)))
    return {"target": getattr(target, "name", ""), "shield": target.shield}


def _verb_mutation(combat, spec, ctx):
    return spec["target"].add_mutation(spec.get("layers", spec.get("amount", 0)))


def _verb_mana(combat, spec, ctx):
    """当前法力的统一增减动词（2026-08-19 起成为统一入口）。

    语义 = 现有全部法力站点的字面约定：
      - 获得（delta>0）：current_mana += delta，随后 clamp_immortal_body
        （不朽之躯钳制——所有既有获取点均如此，如洞察/缄默面具/回始法力/血契）；
      - 失去（delta<0）：实际失去 = min(当前法力, -delta)，下限 0
        （勾魂语义；失去不受不朽之躯钳制——该遗物只限制"获得"）；
      - delta==0：无操作。
    返回 {delta: 实际带符号变化, gained, lost, current_mana}。
    """
    target = spec["target"]
    delta = int(spec.get("delta", spec.get("amount", 0)))
    if delta > 0:
        target.current_mana += delta
        combat.clamp_immortal_body(target)
        return {"delta": delta, "gained": delta, "lost": 0,
                "current_mana": target.current_mana}
    if delta < 0:
        lost = min(target.current_mana, -delta)
        target.current_mana -= lost
        return {"delta": -lost, "gained": 0, "lost": lost,
                "current_mana": target.current_mana}
    return {"delta": 0, "gained": 0, "lost": 0, "current_mana": target.current_mana}


def _verb_depart(combat, spec, ctx):
    reason = spec.get("reason", "mechanism")
    spec["target"].depart_battle(reason)
    return {"target": getattr(spec["target"], "name", ""), "departure_reason": reason}


def _verb_execute(combat, spec, ctx):
    # 统一命零判定+死亡通知（非离场类"直接宣布死亡"）。
    return combat._check_hp_zero_death(spec["target"], ctx=_ctx_of(spec, ctx))


register_verb("damage", _verb_damage)
register_verb("heal", _verb_heal)
register_verb("hp_loss", _verb_hp_loss)
register_verb("blood_limit", _verb_blood_limit)
register_verb("cost", _verb_cost)
register_verb("status", _verb_status)
register_verb("speed", _verb_speed)
register_verb("shield", _verb_shield)
register_verb("mutation", _verb_mutation)
register_verb("mana", _verb_mana)
register_verb("depart", _verb_depart)
register_verb("execute", _verb_execute)
