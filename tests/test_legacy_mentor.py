#!/usr/bin/env python3
"""遗言桥（sim/legacy_mentor.py）测试：正常路径 / 边界 / 非法输入。

DM 裁定 2026-09-10：角色可以参考遗言，但不要百分百按照遗言行动。
对应三条硬边界：评分偏见有上限、信从度 <1、逐回合掷签。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.ai_tactics import TacticalAI
from sim.legacy_mentor import (HINT_CLAMP, LegacyAwareAI, LegacyMentor,
                               match_hints)

_HINT_TEXTS = [{"text": "法力一池不回填，蓝就是拳，别乱花"},
               {"text": "五回合打不掉敌人血，凡庸会收你命"}]


def _engine():
    e = GameEngine(db_path=tempfile.mktemp(suffix=".db"), rng_seed=3,
                   sealed_candidate_path=tempfile.mktemp(suffix=".json"),
                   death_book_path=tempfile.mktemp(suffix=".md"))
    r = e.execute_action("setup_attributes", {"name": "测试甲", "blood_points": 10,
                                              "speed_points": 8, "mana_points": 6})
    assert r.get("success"), r
    return e


def test_mentor_matches_and_stays_under_one():
    """正常路径：关键词命中建议键；信从度在 [0.35,0.85] 区间且逐种子可复现。"""
    m1 = LegacyMentor(_HINT_TEXTS, "3:林渊:legacy")
    m2 = LegacyMentor(_HINT_TEXTS, "3:林渊:legacy")
    assert m1.hints == ["mana_is_fist", "mediocrity_panic"]
    assert 0.35 <= m1.adherence <= 0.85, "信从度必须 <1（不百分百照做）"
    assert m1.adherence == m2.adherence, "同种子同角色信从度必须可复现"
    # 逐回合掷签：同一回合内口径一致（一次掷签，不随候选数漂移）
    m3 = LegacyMentor(_HINT_TEXTS, "9:阮烟:legacy")
    first = m3.heed("mana_is_fist", 1)
    assert all(m3.heed("mana_is_fist", 1) == first for _ in range(10))


def test_bias_clamped_and_zero_without_hints():
    """边界：有遗言时单候选调整幅度被 ±HINT_CLAMP 夹住；无遗言时与 TacticalAI 完全等分。"""
    diff = {"player": {"hp_before": 30, "hp_after": 30, "mana_before": 12,
                       "mana_after": 0, "shield_before": 0, "shield_after": 0,
                       "mutation_delta": 99},
            "enemies": [{"hp_before": 36, "hp_after": 0, "dead": True}]}
    e = _engine()
    # 书里塞满全部关键词的遗言 → 所有建议键都会试图拉偏评分
    e.state.death_book_legacies = [{"text": "法力凡庸五回合癌变治疗崩解异变叠盾"}]
    ai = LegacyAwareAI(e)
    assert ai.mentor.hints, "测试前提：建议键应命中"
    e.state.current_round = 1
    ai._rounds_since_damage = 99          # 逼出凡庸恐慌分支
    scores = set()
    for _ in range(60):                   # 多次掷签覆盖 听/不听 两种路径
        s = ai._score_candidate(dict(diff), "测试X=1", kind="attack")
        assert s is not None
        scores.add(s)
    base = TacticalAI._score_candidate(ai, dict(diff), "测试X=1", kind="attack")
    assert all(abs(s - base) <= HINT_CLAMP + 1e-9 for s in scores), \
        "遗言偏见必须被 ±HINT_CLAMP 夹住（参考不照做）"
    # 无遗言：与基类逐分相等，且不消费随机数
    ai._rounds_since_damage = 0         # 还原恐慌分支，保证两边实例状态一致
    e.state.death_book_legacies = []
    ai2 = LegacyAwareAI(e)
    assert ai2.mentor.hints == [] and ai2.mentor.adherence == 0.0
    base0 = TacticalAI._score_candidate(ai, dict(diff), "测试X=1", kind="attack")
    assert ai2._score_candidate(dict(diff), "测试X=1", kind="attack") == base0


def test_invalid_legacy_entries_do_not_crash():
    """非法输入：垃圾遗言条目/None/字符串混入都不得让桥崩溃，只当作无建议。"""
    garbage = [None, 42, {"foo": "bar"}, "纯字符串遗言：凡庸", {"text": ""}]
    assert match_hints(garbage) == ["mediocrity_panic"]   # 字符串条目也按正文匹配
    m = LegacyMentor(garbage, "1:x:legacy")
    assert isinstance(m.hints, list)
    e = _engine()
    e.state.death_book_legacies = [{"text": None}, "乱", {}]
    ai = LegacyAwareAI(e)
    diff = {"player": {}, "enemies": []}
    assert ai._score_candidate(diff, "测试X=1", kind="attack") == \
        TacticalAI._score_candidate(ai, diff, "测试X=1", kind="attack")
