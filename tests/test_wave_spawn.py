"""
pytest 风格测试 - 波次出怪（2026-09-11 用户令）

规则：R1只出第1只，R4/R7/R10…回始各增援1只直到上限（draw_count公式不变）；
增援怪进场当回合白板（不出道纹）；增援未到齐时战终门禁拦。

运行方式：
    python -m pytest tests/test_wave_spawn.py -v
"""
import os

os.makedirs("/tmp/linji_tests", exist_ok=True)
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.api import GameEngine
from tests.setup_support import finish_initial_daowen


def _new_engine(db_suffix: str) -> GameEngine:
    engine = GameEngine(db_path=f"data/test_wave_{db_suffix}.db", rng_seed=7)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "扭曲都市"})
    choice = setup["result"]["relic_choices"][0]
    engine.execute_action("choose_discovered_relic", {"relic_name": choice})
    engine.state.energy = 0
    return engine


def _kill_all_spawned(engine: GameEngine):
    for enemy in engine.state.enemies:
        enemy.current_hp = 0
        enemy.is_alive = False


def _pass_round(engine: GameEngine, kill_first: bool = False):
    """过一轮：回始→(可选清怪)→怪阶段(空交或普攻玩家)→回终。玩家赛前回满血防死。"""
    p = engine.state.player
    p.blood_limit = max(p.blood_limit, 9999)
    p.current_hp = p.blood_limit
    r = engine.execute_action("round_start", {})
    assert r["success"] is True
    if kill_first:
        _kill_all_spawned(engine)
    prep = engine.execute_action("prepare_monster_phase", {})
    assert prep["success"] is True
    actors = prep["result"].get("actors", [])
    choices = []
    for a in actors:
        if a.get("daowen_required"):
            opt = a["daowen_options"][0]
            dw = {"name": opt["name"], "dodge": False, "blood_shadow": False,
                  "trigger_spell_choices": {}}
            if opt.get("requires_target"):
                dw["target_ref"] = "player:0"
        else:
            dw = None
        n = a.get("base_attack_actions", 0)
        hits = [{"target_ref": "player:0", "dodge": False, "blood_shadow": False,
                 "spell_choices": {"before": {}, "after": {}}} for _ in range(n)]
        atk = [{"hits": hits}] if hits else []
        choices.append({"actor_ref": a["actor_ref"], "daowen": dw, "attack_actions": atk})
    rr = engine.execute_action("resolve_monster_phase",
                               {"token": prep["result"]["token"], "choices": choices})
    assert rr["success"] is True, rr
    # 测试替身击杀：免凡庸（波次机制与凡庸正交）
    p.damage_dealt_this_round = 1
    p.actions_used_this_round = 1
    re_ = engine.execute_action("round_end", {})
    assert re_["success"] is True, re_
    return r


def test_battle7_spawns_one_plus_three_queued():
    """正常路径：B7（上限4）战始只出1只，另3只进增援队列"""
    engine = _new_engine("queue")
    engine.state.current_battle = 6
    engine.state.energy = 0
    r = engine.execute_action("battle_start", {})
    assert r["success"] is True
    assert r["draw_count"] == 4
    assert len(engine.state.enemies) == 1
    assert len(engine.state.monster_reinforcements) == 3
    assert len(r["queued_reinforcements"]) == 3


def test_wave_schedule_r4_r7_r10():
    """正常路径：增援在R4/R7/R10回始各进场1只"""
    engine = _new_engine("schedule")
    engine.state.current_battle = 6
    engine.state.energy = 0
    engine.execute_action("battle_start", {})
    seen_rounds = []
    for _ in range(12):
        r = engine.execute_action("round_start", {})
        for eff in r["result"]["effects"]:
            if eff.get("type") == "wave_spawn":
                seen_rounds.append(eff["round"])
        _kill_all_spawned(engine)
        prep = engine.execute_action("prepare_monster_phase", {})
        rr = engine.execute_action("resolve_monster_phase",
                                   {"token": prep["result"]["token"], "choices": []})
        assert rr["success"] is True
        engine.state.player.damage_dealt_this_round = 1
        engine.state.player.actions_used_this_round = 1
        engine.execute_action("round_end", {})
        if not engine.state.monster_reinforcements:
            break
    assert seen_rounds == [4, 7, 10]
    assert len(engine.state.enemies) == 4


def test_reinforcement_whiteboard_on_arrival_round():
    """正常路径：增援怪进场当回合白板（daowen_required False），次回合恢复"""
    engine = _new_engine("whiteboard")
    engine.state.current_battle = 6
    engine.state.energy = 0
    engine.execute_action("battle_start", {})
    for _ in range(3):
        _pass_round(engine)
    r4 = engine.execute_action("round_start", {})  # R4：增援进场
    assert r4["result"]["round"] == 4
    assert len(engine.state.enemies) == 2
    prep = engine.execute_action("prepare_monster_phase", {})
    assert prep["success"] is True
    flags = {a["actor_ref"]: a["daowen_required"] for a in prep["result"]["actors"]}
    assert flags.get("enemy:0") is True, f"首怪R4应有道纹，实际{flags}"
    assert flags.get("enemy:1") is False, f"增援怪R4应白板，实际{flags}"
    # 交本轮怪阶段→回终→R5再看
    p = engine.state.player
    p.blood_limit = max(p.blood_limit, 9999)
    p.current_hp = p.blood_limit
    choices = []
    for a in prep["result"]["actors"]:
        if a.get("daowen_required"):
            opt = a["daowen_options"][0]
            dw = {"name": opt["name"], "dodge": False, "blood_shadow": False,
                  "trigger_spell_choices": {}}
            if opt.get("requires_target"):
                dw["target_ref"] = "player:0"
        else:
            dw = None
        atk = []
        for _ in range(a.get("base_attack_actions", 0)):
            idx = int(a["actor_ref"].split(":")[1])
            ac = engine.state.enemies[idx].attack_count
            hits = [{"target_ref": "player:0", "dodge": False, "blood_shadow": False,
                     "spell_choices": {"before": {}, "after": {}}} for _ in range(ac)]
            atk.append({"hits": hits})
        choices.append({"actor_ref": a["actor_ref"], "daowen": dw, "attack_actions": atk})
    rr = engine.execute_action("resolve_monster_phase",
                               {"token": prep["result"]["token"], "choices": choices})
    assert rr["success"] is True, rr
    engine.execute_action("round_end", {})
    engine.execute_action("round_start", {})
    prep5 = engine.execute_action("prepare_monster_phase", {})
    flags5 = {a["actor_ref"]: a["daowen_required"] for a in prep5["result"]["actors"]}
    assert flags5.get("enemy:1") is True, f"增援怪R5应恢复道纹，实际{flags5}"


def test_battle_end_blocked_while_queue_pending():
    """门禁：增援未到齐时战终必须拦；到齐清光后放行"""
    engine = _new_engine("gate")
    engine.state.current_battle = 6
    engine.state.energy = 0
    engine.execute_action("battle_start", {})
    _kill_all_spawned(engine)
    r = engine.execute_action("battle_end", {})
    assert r["success"] is False and "增援" in r["error"]
    assert engine.state.battle_won() is False
    while engine.state.monster_reinforcements:
        _pass_round(engine, kill_first=True)
    _kill_all_spawned(engine)
    r2 = engine.execute_action("battle_end", {})
    assert r2["success"] is True


def test_single_monster_battle_unaffected():
    """边界：B1（上限1）无队列、无增援，行为与旧版一致"""
    engine = _new_engine("solo")
    r = engine.execute_action("battle_start", {})
    assert r["draw_count"] == 1
    assert len(engine.state.enemies) == 1
    assert engine.state.monster_reinforcements == []
    assert r["queued_reinforcements"] == []
    _kill_all_spawned(engine)
    assert engine.execute_action("battle_end", {})["success"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
