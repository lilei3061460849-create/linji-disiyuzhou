#!/usr/bin/env python3
"""《死者之书》遗言 → 战术 AI 的「参考桥」（sim 层，2026-09-10 DM 裁定实装）。

DM 裁定 2026-09-10：**角色可以参考遗言，但不要百分百按照遗言行动。**

设计（三条硬边界，全部为了满足"参考不照做"）：
  1. 遗言只以「评分偏见」参与候选评估（改 `_score_candidate` 的加项），
     引擎自身的局势评分仍是主导——遗言是前车之鉴，不是剧本；
  2. 单候选的遗言调整幅度有上限（±HINT_CLAMP）——再信遗言也不能完全压过自己的判断；
  3. 每个角色有「信从度」adherence ∈ [0.35, 0.85]（按 名字+局种子 确定抽取），
     且**每回合每条遗言**独立掷签决定"这回合听不听"——同一个人也不会回回都听。

遗言正文仍是《死者之书.md》的唯一事实源（≤20 字单句，DM 审核）。本桥只读
`state.death_book_legacies`（引擎启动时从书页装入），**不回写、不改引擎**。
遗言 → 建议的映射是关键词表（数据驱动，加遗言不用改桥）：
"""
from __future__ import annotations

import random
from typing import Any, Optional

from engine.ai_tactics import TacticalAI

# 单候选遗言调整幅度上限：遗言永远只能"偏置"评分，不能彻底接管决策。
HINT_CLAMP = 35.0
# 信从度区间：下限保证"参考"确实发生，上限 <1 保证"不百分百照做"。
ADHERENCE_MIN, ADHERENCE_MAX = 0.35, 0.85

# 遗言关键词 → 建议键（ combat 层）。新增遗言优先加关键词，不动代码结构。
HINT_KEYWORDS: dict[str, tuple[str, ...]] = {
    # 「法力一池不回填，蓝就是拳」：普攻力=当前法力，空手花蓝=自废武功
    "mana_is_fist": ("法力", "一池", "普攻", "攻力", "蓝"),
    # 「五回合打不掉敌人血，凡庸会收你命」（含贾希希叠盾案）
    "mediocrity_panic": ("凡庸", "五回合", "不掉血", "未扣敌血", "叠盾"),
    # 「回复过量会癌变」
    "cancer_warn": ("癌变", "回复过量", "治疗"),
    # 「封印攒异变，五十层崩解」
    "collapse_warn": ("崩解", "异变"),
}


def match_hints(texts: list) -> list[str]:
    """从遗言正文列表抽出可执行建议键（去重排序，保证同书同序）。"""
    keys: set[str] = set()
    for entry in texts or []:
        text = entry.get("text", "") if isinstance(entry, dict) else str(entry)
        for key, words in HINT_KEYWORDS.items():
            if any(w in (text or "") for w in words):
                keys.add(key)
    return sorted(keys)


class LegacyMentor:
    """一个角色对整本遗言的"师父"：负责信从度与逐回合掷签。

    seed_key 用 局种子+角色名：同一局里每个角色有自己的信从度；
    同种子同角色完全可复现（推演铁律：随机统一由确定源生成）。
    """

    def __init__(self, legacy_texts: list, seed_key: str):
        self.hints = match_hints(legacy_texts)
        self.adherence = 0.0
        self.rng = random.Random(seed_key)
        if self.hints:                       # 无遗言=无师父，信从度不消费随机数
            self.adherence = self.rng.uniform(ADHERENCE_MIN, ADHERENCE_MAX)
        self.requests = 0
        self.follows = 0
        self.trail: list[str] = []
        self._round_cache: dict[tuple[str, int], bool] = {}

    def heed(self, key: str, round_no: int) -> bool:
        """本回合是否听这条遗言：每回合每条只掷一次签（回合内口径一致）。"""
        if key not in self.hints:
            return False
        cache_key = (key, round_no)
        if cache_key not in self._round_cache:
            self.requests += 1
            follow = self.rng.random() < self.adherence
            self._round_cache[cache_key] = follow
            if follow:
                self.follows += 1
            if len(self.trail) < 400:
                self.trail.append(f"R{round_no}:{key}{'听' if follow else '不听'}")
        return self._round_cache[cache_key]


class LegacyAwareAI(TacticalAI):
    """会翻《死者之书》的战术 AI：遗言作评分偏见，幅度有上限、采纳看掷签。

    无遗言/无匹配键时与 TacticalAI 完全等价（偏见恒为 0，且不消费随机数）。
    """

    def __init__(self, engine: Any, verbose: bool = False, actor: Any = None,
                 enemies: Optional[list] = None, actor_ref: Optional[str] = None):
        super().__init__(engine, verbose=verbose, actor=actor,
                         enemies=enemies, actor_ref=actor_ref)
        seed = getattr(getattr(engine, "dice", None), "_seed", 0) or 0
        who = getattr(self.player, "name", "") or "无名"
        self.mentor = LegacyMentor(list(getattr(engine.state, "death_book_legacies", []) or []),
                                   f"{seed}:{who}:legacy")

    def _score_candidate(self, diff: dict, label: str,
                         kind: Optional[str] = None,
                         target: Optional[str] = None) -> Optional[float]:
        base = super()._score_candidate(diff, label, kind, target)
        if base is None or not self.mentor.hints:
            return base
        p = diff.get("player", {})
        enemies = diff.get("enemies", [])
        mana_spent = max(0, p.get("mana_before", 0) - p.get("mana_after", 0))
        heal = max(0, p.get("hp_after", 0) - p.get("hp_before", 0))
        mutation = max(0, p.get("mutation_delta", 0))
        enemy_hp_loss = sum(max(0, e.get("hp_before", 0) - e.get("hp_after", 0))
                            for e in enemies)
        round_no = int(getattr(self.engine.state, "current_round", 0) or 0)
        adjust = 0.0
        if self.mentor.heed("mana_is_fist", round_no):
            # 蓝就是拳：不掉敌血还花蓝的手，扣分；普攻常驻加分
            if enemy_hp_loss <= 0 and mana_spent > 0:
                adjust -= 10.0
            if kind == "attack":
                adjust += 8.0
        if (self.mentor.heed("mediocrity_panic", round_no)
                and getattr(self, "_rounds_since_damage", 0) >= 3
                and (enemy_hp_loss > 0 or kind == "attack")):
            adjust += 25.0        # 凡庸只剩两回合：先破敌血
        if self.mentor.heed("cancer_warn", round_no) and heal > 0:
            try:
                threshold = self.engine.combat.cancer_threshold_of(self.player)
            except Exception:
                threshold = 0
            if threshold and (getattr(self.player, "total_healed", 0) + heal
                              >= 0.85 * threshold):
                adjust -= 25.0    # 癌变线前贪回复=拿回复当死亡进度条
        if (self.mentor.heed("collapse_warn", round_no) and mutation > 0
                and getattr(self.player, "mutation_count", 0) + mutation >= 40):
            adjust -= 25.0        # 崩解线前再攒异变=自掘
        adjust = max(-HINT_CLAMP, min(HINT_CLAMP, adjust))
        return base + adjust
