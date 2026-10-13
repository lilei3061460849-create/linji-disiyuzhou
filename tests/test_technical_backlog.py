"""无需规则裁定的待办：死斗命中提交、资源超时分类。"""
from __future__ import annotations

import time

import pytest

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity


def duel_with_monster(tmp_path, seed=30601):
    e = GameEngine(db_path=str(tmp_path / f"duel-{seed}.db"), rng_seed=seed,
                   save_dir=str(tmp_path / "saves"),
                   sealed_candidate_path=str(tmp_path / "sealed.json"),
                   death_book_path=str(tmp_path / "deaths.md"))
    e.state.player = Entity("挑战者", "轮回者", blood_limit=300, current_hp=300,
                            mana_limit=10, current_mana=10, speed_limit=8, current_speed=8)
    lord = Entity("守擂者", "轮回者", blood_limit=100, current_hp=100,
                  mana_limit=5, current_mana=5, speed_limit=2, current_speed=2)
    monster = Entity("多攻次怪", "怪物", blood_limit=100, current_hp=100,
                     attack_count=2, attack_power=7, mana_limit=10, current_mana=10)
    monster.dao_wen["变形"] = DaoWenInstance(DaoWen(
        name="变形", formula="", cost_type="异变", cost_formula="5X", effect_formula=""),
        x_value=1)
    e.state.enemies = [lord, monster]
    e.state.in_final_duel = True
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    e.state.duel_turn = "opponent_side"
    return e


@pytest.mark.parametrize("seed", [30601, 7])
def test_duel_monster_submits_all_hits_after_shape_change(tmp_path, seed):
    from sim.duel_common import _resolve_monster_turn_one
    e = duel_with_monster(tmp_path, seed)
    result, acted_ref = _resolve_monster_turn_one(e, {"enemy:0"})
    assert result.get("success"), result
    assert acted_ref == "enemy:1"
    assert e.state.enemies[1].attack_count == 2
    assert e.state.player.current_hp == 300 - 2 * 2
    assert sum("hit_index" in item for item in result["result"]["details"]) == 2


def test_real_slow_duel_reports_resource_timeout_without_a_verdict(tmp_path):
    from sim.duel_pvp import run_duel_pvp
    e = duel_with_monster(tmp_path)
    e.state.enemies = e.state.enemies[:1]
    e.state.combat_subphase = "await_round_start"
    e.state.duel_turn = "player_side"
    calls = []

    def slow_action():
        calls.append(1)
        time.sleep(0.03)
        return True

    result = run_duel_pvp(e, slow_action, use_tactical=False, max_wall_seconds=0.015,
                          max_rounds=2, max_steps=5)
    assert calls, "必须真的执行慢行动，不只是把墙钟改成零"
    assert result.get("timeout") and result.get("timeout_kind") == "wall_clock", result
    assert result.get("winner") is None


def test_build_summary_separates_resource_timeouts_from_invalid_and_loss():
    from sim.build_learner import summarize_build_results
    complete = {"cleared": 7, "won": True, "duel_fought": True,
                "duel_won": True, "invalid": False}
    timeout = {"cleared": 7, "won": False, "duel_fought": True,
               "duel_timeout": True, "invalid": False}
    result = summarize_build_results([complete, timeout], invalid=1)
    assert result["valid"] == 1 and result["invalid"] == 1, result
    assert result["timeouts"] == 1
    assert result["full_rate"] == 1.0 and result["pvp_rate"] == 1.0


def test_duel_recovers_real_hit_schema_drift_without_double_execution(tmp_path, monkeypatch):
    from sim.duel_common import _resolve_monster_turn_one
    e = duel_with_monster(tmp_path)
    execute = e.execute_action
    errors = []

    def drifting_snapshot(action, params):
        result = execute(action, params)
        if action == "prepare_monster_phase" and result.get("success"):
            # 故障注入只改变现场面板；报错、事务回滚和最终提交全部由真实引擎处理。
            e.state.enemies[1].attack_count += 1
        elif action == "resolve_monster_phase" and not result.get("success"):
            errors.append(result["error"])
        return result

    monkeypatch.setattr(e, "execute_action", drifting_snapshot)
    result, ref = _resolve_monster_turn_one(e, {"enemy:0"})
    assert errors and "必须提交3次命中" in errors[0]
    assert result.get("success"), result
    assert ref == "enemy:1"
    details = result["result"]["details"]
    assert sum("hit_index" in item for item in details) == 3
    assert sum("daowen_activated" in item for item in details) == 1
    assert e.state.player.current_hp == 300 - 3 * 3


def test_play_does_not_reclassify_a_timeout_as_invalid_or_log_a_loss(monkeypatch):
    import sim.build_learner as bl
    monkeypatch.setattr(bl, "_play", lambda *args, **kwargs: {
        "cleared": 7, "won": False, "invalid": False, "duel_timeout": True,
        "duel_fought": True})
    telemetry = {}
    result = bl.play("杀伐", [], "乱葬岗", telemetry=telemetry)
    assert result["invalid"] is False and result["timeout_kind"] == "wall_clock"
    assert not telemetry.get("outcomes")


def test_evaluation_records_resource_timeouts_separately(monkeypatch):
    import sim.build_learner as bl
    samples = iter([
        {"cleared": 7, "won": True, "duel_fought": True, "duel_won": True},
        {"cleared": 7, "won": False, "duel_timeout": True, "reason": "pvp_timeout"},
        {"cleared": 0, "won": False, "invalid": True, "reason": "schema error"},
    ])
    monkeypatch.setattr(bl, "play", lambda *args, **kwargs: next(samples))
    telemetry = {}
    result = bl.evaluate_build("杀伐", [], 3, 1, telemetry=telemetry)
    assert (result["valid"], result["invalid"], result["timeouts"]) == (1, 1, 1)
    assert result["full_trials"] == 1 and result["full_rate"] == 1.0
    assert telemetry["timeout_reasons"] == {"pvp_timeout": 1}
    assert telemetry["invalid_reasons"] == {"schema error": 1}
    assert telemetry["outcomes"]["loss"] == 0
