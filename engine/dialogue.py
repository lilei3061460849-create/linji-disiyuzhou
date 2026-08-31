"""战场公开频道（报告.md 硬伤3）：台词的**发布**与**读取**。

## DM 裁定（2026-08-30，用户原话）

1. **台词可真可假，引擎绝不标记真伪**——「你都标出来了，还猜什么」。
   因此：
   - 频道条目里**禁止**出现任何真伪字段（`truth` / `is_true` / `虚张` / `实际意图` …）；
   - 连"姿态标签"也必须是**中性修辞姿态**（施压/示弱/试探/夸口/随口），
     描述的是"这句话摆出什么架势"，而**不是**"这句话是不是真的"。
     叫「虚张」就等于替读者判了假，一样犯规。
2. **完全不碰 AI**——`TacticalAI` 不读频道、不改 `_w()` 性格调制、不动行动权重与战术。
   博弈只发生在**人读日志**与**未来的外部决策层**；本模块只负责"发出去 + 收得到"。
3. **时机自由**——出手前、出手后、任何时候都能说，不设时序约束。
4. **不产生任何数值效果**——`utter()` 只读状态、写一个字符串，绝不改任何面板字段。

## 那"干涉到当前情况"体现在哪

台词**必须挂钩当前真实战局**（否则就是废话）：素材一律从说话方**此刻真实持有**的
道纹 / 真实血量 / 真实法力里取，说的是这一局真有的东西。但说话方**是否兑现**不作保证——
示弱可以是真的弹尽粮绝，也可以是满法力装穷，引擎两边都不标。
读的人只能自己判断，这就是博弈。
"""

from __future__ import annotations

import random
from typing import Any, Optional

# ---- 中性修辞姿态（注意：不得引入任何"真伪"含义的标签） ----
POSTURES: tuple[str, ...] = ("施压", "示弱", "试探", "夸口", "随口")

# 频道条目里一旦出现这些字段，博弈就毁了（读者不用猜了）。
# `tests/test_dialogue_channel.py` 会逐条扫描钉住这一点。
_TRUTH_MARKERS: frozenset[str] = frozenset({
    # 英文
    "truth", "is_true", "is_false", "true", "false", "veracity", "honest",
    "lie", "lying", "bluff", "bluffing", "fake", "real", "genuine",
    "actual", "actual_intent", "real_intent", "intent_truth", "claim_true",
    "deception", "deceptive", "sincere", "insincere", "credible",
    # 中文
    "真假", "真伪", "真实", "虚假", "虚张", "说谎", "真话", "假话",
    "实际意图", "真实意图", "底牌", "可信", "是否属实",
})


# ---------------------------------------------------------------- 素材

def _hp_word(entity: Any) -> str:
    """按真实血量比例给一个措辞（注意：措辞取自真实状态，但说话方可以不用它）。"""
    if entity is None or getattr(entity, "blood_limit", 0) <= 0:
        return "这点"
    ratio = entity.current_hp / entity.blood_limit
    if ratio >= 0.75:
        return "完好"
    if ratio >= 0.40:
        return "半截"
    if ratio > 0:
        return "见底"
    return "空"


def _mana_word(entity: Any) -> str:
    mana = getattr(entity, "current_mana", 0) or 0
    limit = getattr(entity, "mana_limit", 0) or 0
    if limit <= 0:
        return "空的"
    if mana >= limit:
        return "满的"
    if mana >= limit * 0.5:
        return "够用"
    if mana > 0:
        return "见底"
    return "空的"


def _held_daowen(entity: Any, rng: random.Random) -> str:
    """说话方**此刻真实持有**的一个道纹名（没持有就回退成"底牌"）。"""
    pool = [n for n in getattr(entity, "dao_wen", {}) or {}]
    return rng.choice(sorted(pool)) if pool else "底牌"


def _foe(state: Any, actor: Any) -> Any:
    """取说话方当前面对的对手（死斗里 actor 可能是守擂者，所以两边都要看）。"""
    enemies = list(getattr(state, "enemies", []) or [])
    if actor in enemies:
        return getattr(state, "player", None)
    alive = [e for e in enemies if getattr(e, "is_alive", False)]
    return alive[0] if alive else (enemies[0] if enemies else None)


# ---------------------------------------------------------------- 台词模板

# 花括号里的素材一律来自当前真实战局；但措辞方向可以与事实相反（虚张/示弱），
# 且引擎**不记录**哪一次相反。
_TEMPLATES: dict[str, tuple[str, ...]] = {
    "施压": (
        "我手里还压着【{daowen}】，你{hp_word}的身子能接几下？",
        "【{daowen}】我已经捏热了，别逼我现在就甩出去。",
        "我的法力是{mana_word}，你猜够不够把你按死在这里？",
        "下一手我不留了——【{daowen}】你也见识过。",
    ),
    "示弱": (
        "法力{mana_word}……【{daowen}】都快捏不住了。",
        "这口气我接不住，让我缓一手。",
        "血已经{hp_word}了，你赢面很大。",
        "别逼我，我手上真没什么能压你的东西了。",
    ),
    "试探": (
        "你先动，还是我先动？",
        "要是你下回合还敢用【{daowen}】，我可就不客气了。",
        "我猜你会先保命——猜错了算我输。",
        "你那手【{daowen}】，是留着收尾用的吧？",
    ),
    "夸口": (
        "下一手就结束你，用不着第二下。",
        "你那点花样，我闭着眼都能拆。",
        "省点力气吧，等会儿你用不上。",
        "这场从开局起就没悬念了。",
    ),
    "随口": (
        "有意思。",
        "继续。",
        "别停。",
        "就这？",
    ),
}

# 示弱/夸口 允许"与事实相反的措辞"（虚张），其余姿态用真实素材。
_BLUFFABLE = frozenset({"示弱", "夸口"})
_HP_WORDS = ("完好", "半截", "见底", "空")
_MANA_WORDS = ("满的", "够用", "见底", "空的")


# ---------------------------------------------------------------- 发布 / 读取

def _trait(personality: Optional[dict], dim: str) -> float:
    entry = ((personality or {}).get("traits") or {}).get(dim)
    if not entry:
        return 0.0
    return float(entry.get("score", 0.0)) * float(entry.get("confidence", 0.0) or 0.0)


def pick_posture(actor: Any, state: Any, personality: Optional[dict],
                 rng: random.Random) -> str:
    """按**当前局势 + 性格**挑一个修辞姿态（与真伪无关）。"""
    weights: dict[str, float] = {"施压": 1.0, "示弱": 1.0, "试探": 1.0,
                                 "夸口": 0.6, "随口": 0.4}
    limit = getattr(actor, "blood_limit", 0) or 0
    hp = getattr(actor, "current_hp", 0) or 0
    ratio = (hp / limit) if limit else 1.0
    foe = _foe(state, actor)
    foe_hp = getattr(foe, "current_hp", 0) or 0

    # 局势：自己血少 → 更想示弱/试探；对手血少 → 更想施压/夸口
    if ratio <= 0.35:
        weights["示弱"] += 1.4
        weights["试探"] += 0.6
        weights["夸口"] -= 0.4
    if foe and foe_hp <= max(1, (getattr(foe, "blood_limit", 0) or 1) * 0.35):
        weights["施压"] += 1.2
        weights["夸口"] += 0.8
        weights["示弱"] -= 0.5

    # 性格：冒险/利己更爱施压与夸口；守义/求稳更常试探
    weights["施压"] += 0.8 * max(0.0, _trait(personality, "risk_preference"))
    weights["夸口"] += 0.8 * max(0.0, -_trait(personality, "moral_baseline"))
    weights["试探"] += 0.8 * max(0.0, _trait(personality, "decision_habit"))
    weights["示弱"] += 0.8 * max(0.0, _trait(personality, "reaction_pattern") * -1)

    names = list(POSTURES)
    picks = [max(0.0, weights.get(n, 0.0)) for n in names]
    if not any(picks):
        return rng.choice(names)
    return rng.choices(names, weights=picks, k=1)[0]


def utter(state: Any, actor: Any, *, posture: Optional[str] = None,
          rng: Optional[random.Random] = None,
          personality: Optional[dict] = None) -> dict:
    """发布一句台词到战场公开频道，返回该条目。

    **只写字符串，不改任何数值。** 条目字段固定为
    `{round, battle, speaker, posture, text}`——不含任何真伪标记。
    """
    rng = rng or random
    if personality is None:
        personality = _load_personality(state, actor)
    posture = posture or pick_posture(actor, state, personality, rng)

    if posture not in _TEMPLATES:
        posture = "随口"
    text = rng.choice(_TEMPLATES[posture])

    # 素材取自**当前真实战局**；示弱/夸口 允许换成与事实相反的措辞（虚张），
    # 但引擎不记录这次到底反没反。
    hp_word = _hp_word(actor)
    mana_word = _mana_word(actor)
    if posture in _BLUFFABLE and rng.random() < 0.5:
        hp_word = rng.choice(_HP_WORDS)
        mana_word = rng.choice(_MANA_WORDS)

    text = text.format(daowen=_held_daowen(actor, rng),
                       hp_word=hp_word, mana_word=mana_word)

    entry = {
        "round": getattr(state, "current_round", 0),
        "battle": getattr(state, "current_battle", 0),
        "speaker": getattr(actor, "name", "??"),
        "posture": posture,
        "text": text,
    }
    channel = getattr(state, "battle_channel", None)
    if channel is None:                       # 旧存档/未挂字段时兜底
        channel = []
        state.battle_channel = channel
    channel.append(entry)
    return entry


def _load_personality(state: Any, actor: Any) -> Optional[dict]:
    try:
        from engine.personality import get_personality
        return get_personality(state, actor)
    except Exception:
        return None


def read_channel(state: Any, viewer: Any = None) -> list[dict]:
    """读取战场公开频道。

    频道是**公开**的：不按 `viewer` 过滤——双方与观战者看到的必须是同一份，
    否则就不叫"说给对手听"。`viewer` 只为调用点可读性保留。
    """
    return list(getattr(state, "battle_channel", None) or [])


def clear_channel(state: Any) -> None:
    """[战终]清空频道（台词是局内信息，不跨战斗保留）。"""
    state.battle_channel = []


def truth_markers() -> frozenset[str]:
    """暴露禁用字段表，供回归测试扫描（thouse 契约）。"""
    return frozenset(_TRUTH_MARKERS)


def format_channel(state: Any) -> list[str]:
    """把公开频道格式化成可读文本（供日志/观战打印）。

    只输出 `回合 / 说话人 / 修辞姿态 / 台词` 四项——**不输出任何真伪判断**。
    """
    out = []
    for e in read_channel(state):
        out.append(f"第{e.get('round', 0)}回合 {e.get('speaker', '??')}"
                   f"（{e.get('posture', '随口')}）：{e.get('text', '')}")
    return out


# ================================================================ 听者如何"读"这句话
#
# 硬伤3 的核心（DM 2026-08-30 解除红线 E 后追加）：
#   说话没有实质数值作用，但**让敌人忌惮**就是最大的作用。
#   A 说自己没法力了 → B 得自己判断：是真的弹尽粮绝，还是在骗我全力压上然后收割。
#   B 信不信，**只取决于 B 的性格**——引擎绝不代 B 查 A 的法力。

# 各姿态**声称**的内容（是"说了什么"，不是"是不是真的"）
_CLAIM_BY_POSTURE: dict[str, str] = {
    "示弱": "weak",     # 声称自己不行了
    "施压": "strong",   # 声称自己还有货、要压过来
    "夸口": "strong",
    "试探": "probe",    # 不下断言，只抛话头
    "随口": "none",
}


def opponent_said(state: Any, listener: Any) -> Optional[dict]:
    """取对手最近一句台词（公开频道里最后一条**不是自己说的**）。"""
    name = getattr(listener, "name", None)
    for entry in reversed(read_channel(state)):
        if entry.get("speaker") != name:
            return entry
    return None


def belief_from_traits(traits: Optional[dict], claim: str,
                       repeats: int = 0) -> float:
    """听者的**性格**决定它信不信这句话。返回 ∈ [-1, 1]。

    > 0：信以为真（对手真虚 / 威胁是真的）
    < 0：怀疑是反话（陷阱 / 虚张声势）

    刻意**不接收**说话方的任何真实状态——B 只能靠自己的性格与记忆判断，
    一偷看 A 的法力就真相大白，博弈就没了。

    性格维度与符号（与 `engine/ai_tactics.py` 的 `_w()` 同口径）：
      interpersonal_tendency +信任      → 更愿意信人
      moral_baseline         +守义      → 更愿意信人（不预设对方使诈）
      decision_habit         +先观察    → 不轻信，先看看
      risk_preference        +冒险      → 示弱时更愿"当真去收割"；施压时更不信邪
      emotional_stability    −易波动    → 判断更极端（放大，不改方向）
      reaction_pattern       +从容      → 不被试探带话茬
      expression_style       +直言      → 不吃威胁那套
    """
    if claim not in ("weak", "strong", "probe"):
        return 0.0
    t = traits or {}

    def w(dim: str) -> float:
        return float(t.get(dim, 0.0) or 0.0)

    cred = (0.15
            + 0.45 * w("interpersonal_tendency")   # 信任
            + 0.30 * w("moral_baseline")           # 守义
            - 0.45 * w("decision_habit")           # 先观察后行动
            + 0.20 * w("emotional_stability"))     # 沉稳（易波动者此项为负）

    if claim == "weak":
        # 对手喊虚：冒险者更愿意"当它是真的"然后压上收割
        cred += 0.35 * w("risk_preference")
    elif claim == "strong":
        # 对手威胁：冒险与直言的人不吃这套
        cred += 0.25 * w("risk_preference") - 0.20 * w("expression_style")
    elif claim == "probe":
        cred -= 0.15 * w("reaction_pattern")

    # 狼来了：同一姿态反复说，可信度衰减
    cred -= 0.12 * max(0, repeats)
    # 易波动者判断更极端：只放大幅度，不改变方向
    cred *= 1.0 + 0.35 * max(0.0, -w("emotional_stability"))
    return max(-1.0, min(1.0, cred))


def read_opponent(state: Any, listener: Any, *, traits: Optional[dict] = None,
                  personality: Optional[dict] = None) -> Optional[dict]:
    """听者读对手那句话，给出自己的判断。读不到（没台词 / 无实质主张）返回 None。

    返回 `{speaker, posture, claim, belief, repeats}`——
    `belief` 是**听者主观的可信度**，不是这句话的真假；说话方的真实状态
    一个字段都不碰。
    """
    entry = opponent_said(state, listener)
    if entry is None:
        return None
    claim = _CLAIM_BY_POSTURE.get(entry.get("posture", ""), "none")
    if claim == "none":
        return None
    if traits is None and personality is not None:
        traits = {dim: _trait(personality, dim) for dim in (
            "interpersonal_tendency", "moral_baseline", "decision_habit",
            "risk_preference", "emotional_stability", "reaction_pattern",
            "expression_style")}
    # 同一说话方说过**同姿态**的次数（狼来了效应）
    repeats = sum(1 for e in read_channel(state)
                  if e.get("speaker") == entry.get("speaker")
                  and e.get("posture") == entry.get("posture")) - 1
    return {
        "speaker": entry.get("speaker"),
        "posture": entry.get("posture"),
        "claim": claim,
        "belief": belief_from_traits(traits, claim, max(0, repeats)),
        "repeats": max(0, repeats),
    }
