"""死斗 PvP 防护回归测试(2026-08-26 死斗三事故)。

事故:守擂方(敌方轮回者)持激活【血契】时,引擎要求玩家每回始显式提交
"对手血契"决策;sim 从不构建该键 → round_start 永远失败 → 双 0 法力
空转 598 步 → 30s 超时判卫冕。同时补:死锁防护、守擂攻击的闪避中继。
"""
import os
import sys

import pytest

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.enums import GamePhase
from engine.models import DaoWen, DaoWenInstance, Entity, Relic
from sim.optional_actions import round_start_relic_choices


def _duel_engine(tmp_path, *, lord_relics=("血契",), lord_hp=1, lord_daowen=("再生", "封印", "庇护"),
                 challenger_hp=1, challenger_speed=8):
    e = GameEngine(db_path=str(tmp_path / "d.db"), rng_seed=1,
                   sealed_candidate_path=str(tmp_path / "s.json"),
                   death_book_path=str(tmp_path / "b.md"))
    e.execute_action("setup_attributes", {
        "name": "挑战者", "blood_points": 6, "speed_points": 8, "mana_points": 11})
    finish_initial_daowen(e)
    p = e.state.player
    p.current_hp = challenger_hp
    p.current_speed = challenger_speed
    lord = Entity("守擂", "轮回者", blood_limit=36, current_hp=lord_hp,
                  mana_limit=30, current_mana=0, speed_limit=12, current_speed=12)
    for n in lord_daowen:
        lord.dao_wen[n] = DaoWenInstance(DaoWen(
            name=n, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    e.state.enemies.clear()
    e.state.enemies.append(lord)
    e.state.opponent_relics = [Relic(name=n, effect="", tags=[]) for n in lord_relics]
    e.state.in_final_duel = True
    e.state.duel_turn = "player_side"
    e.state.phase = GamePhase.IN_COMBAT.value
    e.state.combat_subphase = "await_round_start"
    return e


def test_round_start_choices_include_opponent_blood_pact(tmp_path):
    """死斗+守擂持血契:回始选项必须含'对手血契',且 round_start 能成功(事故根因回归)。"""
    e = _duel_engine(tmp_path)
    choices = round_start_relic_choices(e)
    assert "对手血契" in choices, "守擂持血契时必须构建 '对手血契' 决策,否则回始永久失败"
    assert isinstance(choices["对手血契"]["use"], bool)
    r = e.execute_action("round_start", {"relic_choices": choices})
    assert r.get("success"), f"回始不应再被血契校验卡死: {r.get('error')}"
    assert e.state.player.current_mana >= e.state.player.mana_limit, "回始后法力应回满(叠加口径)"


def test_round_start_choices_no_opponent_pact_in_pve(tmp_path):
    """PVE(无敌方轮回者)不受影响:不构建对手血契键。"""
    e = _duel_engine(tmp_path, lord_relics=("血契",), lord_daowen=("再生",))
    e.state.in_final_duel = False
    e.state.enemies.clear()
    e.state.enemies.append(Entity("杂兵", "怪物", blood_limit=30, current_hp=30,
                                  attack_count=1, attack_power=3))
    e.state.opponent_relics = []
    choices = round_start_relic_choices(e)
    assert "对手血契" not in choices


def test_mediocrity_fires_before_deadlock_guard(tmp_path):
    """双 1 血 0 手段残局:应由规则层【凡庸】终结,而非 sim 死锁兜底判卫冕。

    2026-08-31 DM 裁定:凡庸是规则,死锁防护只是防卡死的程序兜底,兜底必须排在
    凡庸之后。故 max_rounds 必须给够凡庸所需的回合数,否则测不到「凡庸先炸」。
    """
    from sim.duel_pvp import (run_duel_pvp, MEDIOCRITY_ROUNDS,
                              DEADLOCK_MIN_ROUNDS)
    assert DEADLOCK_MIN_ROUNDS > MEDIOCRITY_ROUNDS, (
        "死锁兜底阈值必须严格大于凡庸阈值,否则兜底会抢在规则之前结束战斗")
    e = _duel_engine(tmp_path, lord_relics=(), lord_daowen=())   # 守擂无牌
    e.state.player.dao_wen.clear()                               # 挑战者无牌
    calls = []
    def act():                    # 模拟 build_learner 闭包恒 True 的最坏情形
        calls.append(1)
        return True
    r = run_duel_pvp(e, act, max_rounds=MEDIOCRITY_ROUNDS + 3, max_steps=200,
                     max_wall_seconds=25.0, log=[])
    assert "凡庸" in r["reason"], (
        f"残局应由规则层【凡庸】终结,而非程序兜底擅定胜负: {r}")
    assert "死锁" not in r["reason"], f"死锁兜底不应抢在凡庸之前: {r}"
    assert len(calls) < 200, "应提前终止,不允许整段空转"


def test_deadlock_guard_reports_error_without_verdict(tmp_path):
    """死锁兜底命中时**不判胜负**,只报错(2026-08-31 DM 裁定)。

    正常路径下凡庸会先终结战斗,兜底几乎不可达;这里下调兜底阈值强行走一次兜底
    分支,专门验证返回契约:winner 为 None 且带 error,绝不宣布擂主卫冕。
    """
    import sim.duel_pvp as dp
    from sim.duel_pvp import run_duel_pvp
    e = _duel_engine(tmp_path, lord_relics=(), lord_daowen=())
    e.state.player.dao_wen.clear()
    orig = dp.DEADLOCK_MIN_ROUNDS
    dp.DEADLOCK_MIN_ROUNDS = 1      # 强制兜底早于凡庸触发
    try:
        r = run_duel_pvp(e, lambda: True, max_rounds=3, max_steps=200,
                         max_wall_seconds=25.0, log=[])
    finally:
        dp.DEADLOCK_MIN_ROUNDS = orig
    assert r.get("winner") is None, (
        f"死锁兜底不得判胜负(不得宣布擂主卫冕): {r}")
    assert r.get("error") is True, f"死锁兜底必须报错: {r}"
    assert "死锁" in r["reason"], r


def test_duel_round_start_failure_is_explicit(tmp_path):
    """回始失败必须显式判卫冕并带原因,不允许吞掉后空转。"""
    from sim.duel_pvp import run_duel_pvp
    e = _duel_engine(tmp_path, lord_relics=(), lord_daowen=())
    e.state.combat_subphase = "player_actions"     # 阶段不符 → 回始必失败
    r = run_duel_pvp(e, lambda: True, max_rounds=2, max_steps=50,
                     max_wall_seconds=20.0, log=[])
    assert r["winner"] == "defender"
    assert "回始失败" in r["reason"], r


def test_lord_attack_relays_challenger_dodge(tmp_path):
    """守擂攻击必须代挑战者按 choose_dodge 声明闪避(修复 PvP 从不闪避)。"""
    from engine.ai_tactics import choose_dodge
    from sim.duel_pvp import _resolve_opponent_one
    e = _duel_engine(tmp_path, lord_relics=(), lord_daowen=(), lord_hp=36)
    lord = e.state.enemies[0]
    lord.attack_count, lord.attack_power = 4, 5      # 每击5伤 ≥ 闪避阈值(max(3,3.6)=4)
    e.execute_action("round_start", {"relic_choices": {}})
    e.state.duel_turn = "opponent_side"               # 出手权交守擂(引擎按轮次校验)
    e.state.player.current_speed = 8                 # 有速度可闪
    log = []
    acted = _resolve_opponent_one(e, log)
    assert acted, "守擂应能普攻"
    assert "被闪避" in log[-1], f"4击×5伤应触发挑战者闪避声明: {log}"


def test_choose_dodge_budget_still_gates(tmp_path):
    """闪避中继仍受 choose_dodge 预算约束:低伤不打断、无速度不闪。"""
    from engine.ai_tactics import choose_dodge
    e = _duel_engine(tmp_path)
    e.execute_action("round_start", {"relic_choices": {}})
    e.state.player.current_speed = 0
    assert choose_dodge(e, 20) is False, "无速度不得闪避"
    e.state.player.current_speed = 8
    assert choose_dodge(e, 1) is False, "1 点伤害低于阈值不应浪费速度闪避"
    assert choose_dodge(e, 12) is True, "高伤命中应当闪避"
