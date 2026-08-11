"""
pytest 风格测试 - 里程碑4a：出怪系统(战始抽怪)

DM裁定记录：出怪数量公式采用 AI_EXPERIENCE.md 记录版"battle_number-3"（README.md已同步更正，
不再是曾经的"-2"）。一阶7场序列：1/1/1/1/2/3/4。

覆盖范围：
1. 数量公式：1/1/1/1/2/3/4 (最低1)
2. 只从当前副本自己的12怪物池抽取，不混入其他副本
3. 允许重复抽选同一怪物种族
4. 抽到的Entity面板(攻击次数/攻击力/血限)与道纹X值必须与README一致
5. "追求者·拿走口粮"登记的强制怪物，在下一场[战始]时真正额外加入敌方

不在本文件覆盖范围：战斗背景(纯叙事，本身无机制，故不需要测试)。

运行方式：
    python -m pytest tests/test_monster_draw.py -v
"""
import os
os.makedirs("/tmp/linji_tests", exist_ok=True)
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.api import GameEngine
from engine.monsters import compute_draw_count, parse_monster_pool


README_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md")


def _new_engine(db_suffix: str, region: str) -> GameEngine:
    engine = GameEngine(db_path=f"data/test_draw_{db_suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 10, "speed_points": 8, "mana_points": 7})
    engine.execute_action("setup_choose_daowen", {"daowen": "杀伐"})
    engine.execute_action("setup_choose_region", {"region": region})
    return engine


# ========================================================================
# 正常路径
# ========================================================================

def test_draw_count_formula_matches_confirmed_sequence():
    """正常路径：一阶7场出怪数量必须是 1/1/1/1/2/3/4（DM已裁定的-3公式）"""
    expected = [1, 1, 1, 1, 2, 3, 4]
    actual = [compute_draw_count(n, is_tier_one=True) for n in range(1, 8)]
    assert actual == expected


def test_battle_start_actually_populates_enemies_from_correct_region_pool():
    """正常路径：[战始]必须真正把敌方列表填满，且只使用当前副本自己的12怪物池"""
    engine = _new_engine("region_pool", "扭曲都市")
    pool_names = {m["name"] for m in engine.monster_pool["扭曲都市"]}
    r = engine.execute_action("battle_start", {})
    assert r["success"] is True
    assert r["draw_count"] == 1
    assert len(engine.state.enemies) == 1
    assert engine.state.enemies[0].name in pool_names
    assert engine.state.enemies[0].entity_type == "怪物"


def test_drawn_monster_panel_matches_readme_exactly():
    """正常路径：抽到的怪物面板(攻击次数/攻击力/血限)与道纹X值必须与README定义完全一致"""
    pools = parse_monster_pool(README_PATH)
    known = next(m for m in pools["龙心谷"] if m["name"] == "熔岩蜥")
    assert (known["attack_count"], known["attack_power"], known["blood_limit"]) == (3, 6, 234)
    assert known["dao_wen"] == {"加害": 2, "狂暴": 3, "冲击": 3}


def test_repetition_allowed_across_many_draws():
    """正常路径：允许重复抽选同一怪物种族——多场连续抽取应能抽出重复名字"""
    engine = _new_engine("repeat_ok", "龙心谷")
    all_names = []
    for _ in range(7):
        r = engine.execute_action("battle_start", {})
        all_names.extend(r["enemies"])
        engine.state.energy = 3
        engine.execute_action("battle_end", {})
    assert len(all_names) == 13  # 1+1+1+1+2+3+4 = 13
    assert len(set(all_names)) < len(all_names), "13次抽取(池仅12种)必然会出现至少一次重复(抽屉原理)"


def test_forced_monster_from_event_appears_extra_next_battle():
    """正常路径：追求者·拿走口粮登记的怪物，必须在下一场[战始]时真正额外出现"""
    engine = _new_engine("forced_monster", "龙心谷")
    engine.state.shards = 0
    engine.execute_action("resolve_event", {"event": "追求者", "option_id": 2})
    assert len(engine.state.forced_monsters_next_battle) == 1

    r = engine.execute_action("battle_start", {})
    names = r["enemies"]
    assert any("追求者" in n for n in names), f"追求者应额外出现，实际{names}"
    assert len(engine.state.enemies) == r["draw_count"] + 1, "额外怪物应叠加在正常出怪数量之上，而不是占用/替换名额"
    assert engine.state.forced_monsters_next_battle == [], "登记项使用后应清空，不能在第三场重复出现"

    zhuiqiuzhe = next(e for e in engine.state.enemies if e.name == "追求者")
    assert (zhuiqiuzhe.attack_count, zhuiqiuzhe.attack_power, zhuiqiuzhe.blood_limit) == (8, 2, 96)


# ========================================================================
# 边界条件
# ========================================================================

def test_draw_count_floors_at_one_for_high_battle_number_underflow():
    """边界：公式在场次很小时不能算出0或负数，必须floor在1"""
    assert compute_draw_count(1, is_tier_one=True) == 1
    assert compute_draw_count(2, is_tier_one=True) == 1
    assert compute_draw_count(3, is_tier_one=True) == 1


def test_unknown_region_draws_nothing_but_does_not_crash():
    """边界：current_region不在三个已知副本池中时，不应抛异常，只是不出怪"""
    engine = GameEngine(db_path="/tmp/linji_tests/test_draw_unknown.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 10, "speed_points": 8, "mana_points": 7})
    engine.execute_action("setup_choose_daowen", {"daowen": "杀伐"})
    engine.state.current_region = "尚未实现的副本"
    r = engine.execute_action("battle_start", {})
    assert r["success"] is True
    assert r["draw_count"] == 0
    assert engine.state.enemies == []


# ========================================================================
# 错误输入 / 非法配置
# ========================================================================

def test_parse_monster_pool_never_mixes_regions():
    """非法配置校验：三个副本池互不相混，且每池严格12只"""
    pools = parse_monster_pool(README_PATH)
    for region in ("扭曲都市", "罪孽都市", "龙心谷"):
        assert len(pools[region]) == 12, f"{region}应有12只怪物，实际{len(pools[region])}"
    all_names = [m["name"] for region in pools.values() for m in region]
    assert len(all_names) == len(set(all_names)), "三个副本的怪物名不应互相重复/串池"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
