"""TacticalAI 实时决策验证（2026-08-26 防公式化改造）。

背景：固定战术表（TACTICAL_ROLES）与固定策略顺序已删除。本文件锁定三件事：
1. 决策由**当前状态**驱动——同一构筑在不同局面打出不同的牌；
2. 决策由**角色性格**（personality_traits）调制——均势窗口内不同性格倾斜不同，
   但局势极端时（低威胁/致死威胁）性格不越权（性格是倾向不是规则）；
3. 决策只依赖**可见信息**——未持有的道纹/不存在的目标绝不出现。
"""
import math
import os
import sys

import pytest

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.ai_tactics import TacticalAI
from engine.models import DaoWen, DaoWenInstance


def _engine(tmp_path, seed=4, learn=("庇护", "再生")):
    e = GameEngine(db_path=str(tmp_path / "dyn.db"), rng_seed=seed)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    for dw in learn:
        e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": dw})
    e.state.energy = 0
    choices = {}
    relic = e.state.relics[0].name
    if relic in ("折速法印", "三相残韵盘"):
        choices[relic] = {"use": False}
    e.execute_action("battle_start", {"relic_choices": choices})
    e.execute_action("round_start", {})
    return e


def _set_threat(e, power, count):
    for m in e.state.enemies:
        m.attack_power, m.attack_count = power, count


def _with_personality(e, personality):
    for dim, d in personality:
        for i in range(5):
            e.update_personality(e.state.player, dim, d, evidence=f"行为证据{i}")


def _nth_action(e, personality, n=1, mana=30, threat=None):
    """跑第 n 手(跳过前面的首试配额行动),返回 action 描述。"""
    _with_personality(e, personality)
    e.state.player.current_mana = mana
    if threat:
        _set_threat(e, *threat)
    ai = TacticalAI(e)
    ai.new_round()
    action = None
    for _ in range(n):
        r = ai.take_action()
        action = (r or {}).get("action", "无")
    return action


# ---------- 1. 状态驱动 ----------

def test_state_driven_low_threat_prefers_offense(tmp_path):
    """低威胁局面(威胁≈血限1/3以下):防御无价值,应推进输出。"""
    e = _engine(tmp_path)
    assert "杀伐" in _nth_action(e, [], n=2, threat=(3, 4))


def test_state_driven_high_threat_prefers_defense(tmp_path):
    """高威胁局面(威胁≈2/3血限):任何性格都应转入防御(局势压倒倾向)。"""
    e = _engine(tmp_path)
    action = _nth_action(e, [("risk_preference", 1)], n=2, threat=(4, 10))
    assert "庇护" in action or "再生" in action, f"高威胁下极端冒险者仍不设防: {action}"


def test_state_driven_killable_target_gets_finished(tmp_path):
    """可收割目标:实时评分应选择恰好击杀(击杀加成+终结加成)。"""
    e = _engine(tmp_path)
    m = e.state.enemies[0]
    m.current_hp = 6            # 杀伐X=3 恰好击杀
    action = _nth_action(e, [], n=1)
    assert "杀伐" in action, f"可收割局面未推进输出: {action}"
    assert not m.is_alive or m.current_hp <= 0, "收割候选应被优先执行"


# ---------- 2. 性格调制(均势窗口翻转;极端局势不越权) ----------

def test_personality_risk_flips_contested_window(tmp_path):
    """均势窗口(威胁≈血限一半):求稳者立盾,冒险者/无性格者继续输出。"""
    e1 = _engine(tmp_path / "a")
    steady = _nth_action(e1, [("risk_preference", -1)], n=2, threat=(5, 6))
    assert "庇护" in steady, f"求稳者在半血威胁下未立盾: {steady}"

    e2 = _engine(tmp_path / "b")
    rash = _nth_action(e2, [("risk_preference", 1)], n=2, threat=(5, 6))
    assert "杀伐" in rash, f"冒险者在半血威胁下未推进输出: {rash}"


def test_personality_weights_flow_into_scoring(tmp_path):
    """评分层:性格权重 = score×confidence,随证据置信度增长。"""
    e = _engine(tmp_path)
    ai = TacticalAI(e)
    ai._refresh_personality()
    assert ai._w("risk_preference") == 0.0    # 无性格=零权重
    e.update_personality(e.state.player, "risk_preference", +1, evidence="接下死斗")
    ai._refresh_personality()
    w1 = ai._w("risk_preference")
    for i in range(4):
        e.update_personality(e.state.player, "risk_preference", +1, evidence=f"第{i+2}次冒险")
    ai._refresh_personality()
    w2 = ai._w("risk_preference")
    assert 0 < w1 < w2 < 1.0, "权重应随证据置信度单调增强且不越界"


def test_score_layer_personality_monotonic_on_self_harm(tmp_path):
    """评分层:同一自伤后果,冒险>无性格>求稳(单调);节约重罚法力支出。"""
    e = _engine(tmp_path)
    diff = {
        "player": {"hp_before": 40, "hp_after": 34, "bl_before": 60, "bl_after": 60,
                   "mana_before": 30, "mana_after": 30, "speed_before": 8,
                   "speed_after": 8, "shield_before": 0, "shield_after": 0,
                   "mutation_delta": 0, "dead": False},
        "enemies": [{"name": "怪", "hp_before": 50, "hp_after": 50, "dead": False}],
        "allies": [], "events": [], "player_dead": False,
    }
    scores = {}
    for tag, times in (("冒险", 5), ("无性格", 0), ("求稳", 5)):
        e2 = _engine(tmp_path / tag)
        if times:
            for i in range(times):
                e2.update_personality(e2.state.player, "risk_preference",
                                      +1 if tag == "冒险" else -1, evidence=f"e{i}")
        ai = TacticalAI(e2)
        ai._refresh_personality()
        scores[tag] = ai._score_candidate(diff, "试探X=1", "damage", "怪")
    assert scores["冒险"] > scores["无性格"] > scores["求稳"], (
        f"自伤后果评分应随风险偏好单调: {scores}")

    # 节约:法力支出罚更重
    mana_diff = {
        "player": {"hp_before": 40, "hp_after": 40, "bl_before": 60, "bl_after": 60,
                   "mana_before": 20, "mana_after": 4, "speed_before": 8,
                   "speed_after": 8, "shield_before": 0, "shield_after": 0,
                   "mutation_delta": 0, "dead": False},
        "enemies": [{"name": "怪", "hp_before": 50, "hp_after": 50, "dead": False}],
        "allies": [], "events": [], "player_dead": False,
    }
    e3 = _engine(tmp_path / "节")
    for i in range(5):
        e3.update_personality(e3.state.player, "resource_view", +1, evidence=f"省{i}")
    ai3 = TacticalAI(e3)
    ai3._refresh_personality()
    plain = TacticalAI(_engine(tmp_path / "普"))
    plain._refresh_personality()
    frugal = ai3._score_candidate(mana_diff, "大费X=16", "damage", "怪")
    neutral = plain._score_candidate(mana_diff, "大费X=16", "damage", "怪")
    assert frugal < neutral, "节约性格应更重地惩罚大额法力支出"


# ---------- 3. 可见信息边界 ----------

def test_ai_never_acts_beyond_visible_information(tmp_path):
    """无道纹无残韵:不得凭空行动;持有清单外道纹绝不出现。"""
    e = _engine(tmp_path, learn=())
    e.state.player.dao_wen.clear()
    e.state.resonance.clear()
    ai = TacticalAI(e)
    assert ai.take_action() is None


def test_full_battle_runs_legally_without_personality(tmp_path):
    """无性格基线:完整打完一场战斗,全部结果 success,不抛异常。"""
    e = _engine(tmp_path)
    ai = TacticalAI(e)
    rounds = 0
    while (e.state.enemies and any(m.is_alive for m in e.state.enemies)
           and e.state.player.is_alive and rounds < 25):
        ai.new_round()
        for r in ai.take_turn():
            assert r.get("success") is True, r
        # 推进回合(怪物阶段由引擎结算)
        e.state.combat_subphase = "await_round_end"
        rr = e.execute_action("round_end", {})
        assert rr.get("success") is True, rr
        rounds += 1
    assert rounds < 25 or not e.state.player.is_alive or not any(
        m.is_alive for m in e.state.enemies)
