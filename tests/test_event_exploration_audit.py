"""事件探索收益审计工具的专用测试(小规模,不改生产行为)。"""
import importlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim.build_learner as bl
import sim.event_exploration_audit as aea


# ---------- 快照与记录 ----------

def _engine(tmp_path):
    return aea.AuditEngine(db_path=str(tmp_path / "a.db"), rng_seed=1,
                           sealed_candidate_path=str(tmp_path / "s.json"),
                           death_book_path=str(tmp_path / "b.md"))


def test_audit_engine_records_pre_battle_diff(tmp_path):
    from tests.setup_support import finish_initial_daowen
    e = _engine(tmp_path)
    e.execute_action("setup_attributes", {
        "name": "甲", "blood_points": 6, "speed_points": 8, "mana_points": 11})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    shards_before = e.state.shards
    r = e.execute_action("pre_battle_action", {"sub_action": "修行", "tier": 1, "to": "mana"})
    assert r.get("success")
    entries = [x for x in e.audit_log if x["action_type"] == "pre_battle_action"]
    assert entries, "局外行动必须被审计引擎记录"
    entry = entries[-1]
    assert set(entry) >= {"action_type", "params", "ok", "before", "diff", "player_dead"}
    assert entry["params"].get("sub_action") == "修行"
    assert entry["before"]["shards"] == shards_before
    assert "mana_limit" in entry["diff"]


# ---------- 配对对照 ----------

def _mk_char(cid, cleared, actions):
    """actions: [(kind, battle, hp, shards, daowen_n)]"""
    log = []
    for i, (kind, battle, hp, shards, dn) in enumerate(actions, 1):
        log.append({"action_type": "pre_battle_action", "ok": True,
                    "params": {"sub_action": kind},
                    "before": {"battle": battle, "hp": hp, "shards": shards,
                               "daowen_n": dn},
                    "diff": {k: 0 for k in aea.RESOURCE_KEYS},
                    "player_dead": False, "desc": "", "event": None})
    return {"cid": cid, "invalid": False, "cleared": cleared, "won": cleared >= 7,
            "log": log}


def test_paired_contrast_filters_by_state_similarity():
    chars = [
        _mk_char(1, 5, [("探索", 2, 30, 20, 3)]),
        _mk_char(2, 4, [("修行", 2, 30, 20, 3)]),      # 应配对(状态全同)
        _mk_char(3, 1, [("修行", 2, 60, 90, 5)]),      # 不应配对(状态差远)
        _mk_char(4, 3, [("学习", 2, 33, 25, 3)]),      # 可配对(次近)
    ]
    pairs = aea.paired_contrast(chars)
    assert len(pairs) == 1, "同角色不与自己配对;状态差远的必须被过滤"
    ex, sf = pairs[0]
    assert ex["cid"] == 1 and sf["cid"] == 2


# ---------- 幸存者偏差统计 ----------

def test_survivorship_analysis_detects_bias_direction():
    # 早死角色探索少、长寿角色探索多(全程相关为正);固定窗口后仍为正→偏差不成立
    chars = []
    for i in range(10):
        n_acts = 3 + (i % 3) * 3          # 行动数不同
        acts = []
        for k in range(n_acts):
            kind = "探索" if k == 0 else "修行"
            acts.append((kind, 1, 30, 20, 3))
        log = [{"action_type": "pre_battle_action", "ok": True,
                "params": {"sub_action": kind},
                "before": {"battle": 1, "hp": 30, "shards": 20, "daowen_n": 3},
                "diff": {r: 0 for r in aea.RESOURCE_KEYS},
                "player_dead": False, "desc": "", "event": None} for kind, *_ in acts]
        chars.append({"cid": i, "invalid": False, "cleared": i % 4, "won": False,
                      "log": log})
    out = aea.survivorship_analysis(chars, window=3)
    assert "explore_rate_vs_cleared_full_life" in out
    assert "explore_count_in_first_window_vs_cleared" in out
    assert 0 <= out["early_rate_mean"] <= 1


# ---------- 判定规则 ----------

def _rep(explore_count=70, cc=None, surv=None, gain=1.0, deaths=(0, 0), resolutions=(74, 53)):
    zero = {k: {"mean": 0.0, "total": 0, "n": 0} for k in aea.RESOURCE_KEYS}
    zero["shards"]["mean"] = gain
    return {
        "reject": {"explore_count": explore_count, "explore_rate": 0.09,
                   "event_resolutions": resolutions[0], "event_death_count": deaths[0],
                   "event_immediate_diff": zero, "cleared_mean": 1.5},
        "greedy": {"event_resolutions": resolutions[1], "event_death_count": deaths[1],
                   "event_immediate_diff": zero},
        "policy_contrast": {"cleared_mean": {"reject": 1.5, "greedy": 1.4}},
        "causal_contrast": cc or {"n": 100, "significant": False,
                                  "paired_diff_mean": 0.0, "ci95": [-0.4, 0.3]},
        "survivorship": surv or {"explore_rate_vs_cleared_full_life": -0.05,
                                 "explore_count_in_first_window_vs_cleared": 0.12},
    }


def test_verdict_insufficient_data():
    rep = _rep(explore_count=6)
    assert aea.verdict_of(rep)["grade"] == "A"


def test_verdict_causal_positive_maps_to_C():
    rep = _rep(cc={"n": 100, "significant": True, "paired_diff_mean": 0.6,
                   "ci95": [0.2, 1.0]})
    assert aea.verdict_of(rep)["grade"] == "C"


def test_verdict_causal_negative_maps_to_D():
    rep = _rep(cc={"n": 100, "significant": True, "paired_diff_mean": -0.5,
                   "ci95": [-0.9, -0.1]})
    assert aea.verdict_of(rep)["grade"] == "D"


def test_verdict_bias_plus_small_gain_maps_to_F():
    rep = _rep(gain=1.0)   # 因果≈0 + 偏差确认 + 收益量级小
    v = aea.verdict_of(rep)
    assert v["grade"] == "F"
    assert any("幸存者偏差" in x for x in v["evidence"])


def test_verdict_bias_only_maps_to_B():
    rep = _rep(gain=5.0)   # 收益量级够大
    assert aea.verdict_of(rep)["grade"] == "B"


# ---------- 生产代码不受污染 ----------

def test_monkeypatch_restored_after_small_batch(tmp_path):
    real_engine = bl.GameEngine
    real_resolve = bl._resolve_pending_event
    chars, invalid = aea.run_batch(2, "greedy", seed_base=99, battles=2)
    assert bl.GameEngine is real_engine, "审计结束后必须还原 sim.build_learner.GameEngine"
    assert bl._resolve_pending_event is real_resolve
    assert len(chars) + invalid == 2
