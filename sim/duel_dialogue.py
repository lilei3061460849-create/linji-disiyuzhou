"""PvP 死斗对白渲染（2026-08-30）。

角色（挑战者/守擂者）都是「轮回者」实体，共用同一套 personality 系统。本模块把
双方在关键时刻（开场/出血/残韵/低血/击杀/败北）的动作渲染成符合其性格倾向的台词，
并在死斗实录里以「角色名: …」呈现——PvP 不再是「你一我一刀、全程无对白」。

设计原则：
- 只做**渲染**，不侵入数值/决策。台词由实际发生的动作事件驱动，不预先编剧情。
- 性格由 engine.personality 按行为证据推断（TRAIT_DIMENSIONS）；无性格数据时给
  一套中性的中立台词兜底，绝不因「还没形成判断」而让角色哑火。
- 台词库刻意小：每个维度两向各 3~5 条 + 中立兜底，保证可读且不重复刷屏。
"""

from __future__ import annotations
from typing import Any, Optional


# ---- 各性格维度「倾向于+侧」与「-侧」的台词池（决定性动作时按方向随机取一种） ----

# 表达方式 expression_style: +直言(进攻性、直白) / -内敛(收敛、留白)
EXPRESSION = {
    "positive": [
        "你的破绽太明显了，我忍不住就戳了过去。",
        "废话不多说——这一下，你接不住。",
        "既然站在这里，就别指望我手下留情。",
    ],
    "negative": [
        "……你还是退下吧。",
        "我不想多言。",
        "（沉默地看了你一眼，攻势却半分没慢）",
    ],
}
# 反应模式 reaction_pattern: +从容 / -慌乱
REACTION = {
    "positive": [
        "血而已，我见惯了。继续。",
        "这点小伤动摇不了我。",
        "慌什么？胜负才刚开始。",
    ],
    "negative": [
        "这、这一下太狠了……不能慌，不能慌！",
        "怎么会这么大伤害……稳住，稳住！",
        "住手——！我不能倒在这里！",
    ],
}
# 风险偏好 risk_preference: +冒险 / -求稳
RISK = {
    "positive": [
        "残韵在手，就该赌这一把大的！",
        "拼着受点伤，也要把优势抢回来。",
        "有便宜不占，那不是我的风格。",
    ],
    "negative": [
        "先稳住局面，再谈进攻。",
        "我不冒无谓的险。",
        "稳一手，才有后面的胜算。",
    ],
}
# 道德底线 moral_baseline: +守义(不伤友) / -利己(优先自己)
MORAL = {
    "positive": [
        "我护得住身边的人，才有资格赢。",
        "赢，也要赢得堂堂正正。",
    ],
    "negative": [
        "成王败寇，活着才是硬道理。",
        "挡我路的，只好请它让一让了。",
    ],
}
# 情绪稳定性 emotional_stability: +沉稳 / -易波动
STABLE = {
    "positive": [
        "（呼吸平稳）局面尽在掌握。",
        "急，只会输得更快。",
    ],
    "negative": [
        "我、我有点控制不住节奏了……",
        "下一击，我非要你付出代价不可！",
    ],
}

# 中立兜底：任何性格维度尚未形成判断时使用，保证角色始终「会说话」。
NEUTRAL = {
    "opening": [
        "既然站上来了，那就各凭本事吧。",
        "让我看看，你能撑到第几回合。",
    ],
    "damage_out": [
        "中了！趁现在——",
        "这一击，你没法闪。",
    ],
    "damage_in": [
        "好快的攻势……",
        "哼，有两下子。",
    ],
    "resonance": [
        "残韵……是时候了。",
        "这变化，你没想到吧？",
    ],
    "low_hp": [
        "（喘息）还没完……",
        "胜负未分，别急着庆祝。",
    ],
    "kill": [
        "到此为止了。",
        "胜负已分。",
    ],
    "defeat": [
        "……是我输了。",
        "技不如人，无话可说。",
    ],
    "dodge": [
        "躲开了。",
        "这点速度，还难不倒我。",
    ],
    "stall": [
        "（双方僵持）都在等对方先露破绽么？",
        "这么耗下去，有意思么？",
    ],
}

# 按性格权重选台词（用表达方式倾向挑进攻/内敛口吻；其余维度作为情绪点缀）
def _pick(pool: list[str], rng) -> str:
    return pool[rng.randrange(len(pool))] if pool else ""


def _resolve_trait(personality: Optional[dict], dimension: str) -> Optional[float]:
    """取某维度的带符号倾向值（正=positive侧，负=negative侧），无数据返回 None。"""
    if not personality:
        return None
    traits = personality.get("traits") or {}
    entry = traits.get(dimension)
    if not entry:
        return None
    # entry 里同时有描述(value)与数值倾向(score)；渲染用 score（-1..1）。
    return entry.get("score")


def render_line(actor: Any, event: str, personality: Optional[dict] = None,
                rng=None) -> str:
    """按动作事件 + 角色性格渲染一句台词（含角色名前缀）。

    actor: Entity（提供 .name）。
    event: opening / damage_out / damage_in / resonance / low_hp / kill / defeat
           / dodge / stall。
    返回形如『玄夜: 你的破绽太明显了，我忍不住就戳了过去。』
    """
    import random as _r
    rng = rng or _r
    name = getattr(actor, "name", "??")

    # 优先按「表达方式」维度决定口吻；未形成判断时回退到中立池对应的情感。
    expr = _resolve_trait(personality, "expression_style")
    if event in ("opening", "damage_out", "resonance", "kill"):
        if expr is not None and expr >= 0.2:
            text = _pick(EXPRESSION["positive"] + EXPRESSION["negative"], rng)
        elif expr is not None and expr <= -0.2:
            text = _pick(EXPRESSION["negative"], rng)
        else:
            text = _pick(NEUTRAL[event], rng)
    elif event == "damage_in":
        react = _resolve_trait(personality, "reaction_pattern")
        if react is not None and react <= -0.2:
            text = _pick(REACTION["negative"], rng)
        else:
            text = _pick(REACTION["positive"] + NEUTRAL["damage_in"], rng)
        # 极端冒险/求稳会在受创后补一句行动倾向
        risk = _resolve_trait(personality, "risk_preference")
        if risk is not None and abs(risk) >= 0.2:
            text += " " + _pick(RISK["positive"] if risk > 0 else RISK["negative"], rng)
    elif event == "low_hp":
        stable = _resolve_trait(personality, "emotional_stability")
        text = _pick(STABLE["positive"] + NEUTRAL["low_hp"], rng) if (stable is None or stable >= 0) \
            else _pick(STABLE["negative"] + NEUTRAL["low_hp"], rng)
    elif event == "defeat":
        text = _pick(NEUTRAL["defeat"], rng)
    elif event == "dodge":
        text = _pick(NEUTRAL["dodge"], rng)
    elif event == "stall":
        text = _pick(NEUTRAL["stall"], rng)
    else:
        text = _pick(NEUTRAL.get(event) or NEUTRAL["opening"], rng)

    return f"{name}: {text}"


def peek_personality(engine, entity: Any) -> Optional[dict]:
    """从 engine 读取实体的性格摘要（无则返回 None）。"""
    from engine.personality import get_personality
    try:
        return get_personality(engine.state, entity)
    except Exception:
        return None
