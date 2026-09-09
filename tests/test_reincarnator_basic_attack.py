"""轮回者普攻（DM裁定 2026-09-09）：攻击次数/攻击力初始 1×1，属性点 1:1 追加。

裁定原文：「初始1×1，属性点可以提升这两者，1属性点=1攻击次数=1攻击力」。
因此加点从三维（血/速/法）变五维，25 点总预算不变；普攻走既有两阶段攻击接口
（prepare_attack/resolve_attack），伤害 = 攻击次数 × 攻击力（未被闪避时）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine  # noqa: E402
from engine.models import Entity  # noqa: E402
from tests.attack_support import resolve_attack  # noqa: E402
from tests.setup_support import finish_initial_daowen  # noqa: E402


def _engine(suffix, **points):
    os.makedirs("/tmp/linji_tests", exist_ok=True)
    e = GameEngine(db_path=f"/tmp/linji_tests/test_basic_attack_{suffix}.db", rng_seed=7,
                   sealed_candidate_path=f"/tmp/linji_tests/test_basic_attack_s_{suffix}.json")
    r = e.execute_action("setup_attributes", {"name": "白某", **points})
    assert r.get("success"), r
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "曲解"})
    e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.state.phase = "in_combat"
    e.state.current_round = 2          # 跳过白板回合
    return e


def test_attack_panel_is_one_by_one_when_no_points_spent_on_it():
    """旧三维加点（6/8/11=25）仍然合法，普攻面板是裁定给的初始 1×1。"""
    e = _engine("base", blood_points=6, speed_points=8, mana_points=11)
    p = e.state.player
    assert (p.attack_count, p.attack_power) == (1, 1)
    # 三维口径不变：1点=6血限=1速限=2法限
    assert (p.blood_limit, p.speed_limit, p.mana_limit) == (36, 8, 22)


def test_attribute_points_buy_attack_count_and_power_one_to_one():
    e = _engine("atk", blood_points=6, speed_points=8, mana_points=5,
                attack_count_points=3, attack_power_points=3)
    p = e.state.player
    assert (p.attack_count, p.attack_power) == (4, 4)     # 初始1 + 加点3
    assert (p.blood_limit, p.speed_limit, p.mana_limit) == (36, 8, 10)


def test_budget_is_still_exactly_25_across_five_dimensions():
    e = GameEngine(db_path="/tmp/linji_tests/test_basic_attack_budget.db", rng_seed=7)
    r = e.execute_action("setup_attributes", {
        "name": "白某", "blood_points": 6, "speed_points": 8, "mana_points": 7,
        "attack_count_points": 3, "attack_power_points": 3})       # 合计 27
    assert r["success"] is False
    assert "25" in r["error"] and "27" in r["error"]
    assert "攻击次数" in r["instruction"] and "攻击力" in r["instruction"]


def test_basic_attack_lands_for_attack_count_times_attack_power():
    """普攻真的能打到人：这是「凡庸」时钟此前无解的洞（候选池里 0 个掉血手段）。"""
    e = _engine("hit", blood_points=9, speed_points=8, mana_points=5,
                attack_count_points=1, attack_power_points=2)   # 9+8+5+1+2 = 25
    p = e.state.player
    assert (p.attack_count, p.attack_power) == (2, 3)
    # 速度 0 → 无法闪避（combat.monster_dodge_check：当前速度≤已用预算即不闪避）
    m = Entity(name="尸卒", entity_type="怪物", blood_limit=40, current_hp=40,
               attack_count=1, attack_power=1, speed_limit=0, current_speed=0)
    e.state.enemies.append(m)
    r = resolve_attack(e)
    assert r.get("success"), r
    assert m.current_hp == 40 - p.attack_count * p.attack_power, (
        f"普攻应按 攻击次数×攻击力 结算，实际敌血 {m.current_hp}")
