"""【封印】自动法术与普攻行动预算回归测试。

测试走真实 GameEngine 的 prepare/resolve/round phase 接口：不直接改生命或胜负，
只把一个已持有【封印】的轮回者和真实怪物放入战场，验证自动触发的真实结算。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity
from tests.attack_support import resolve_attack
from tests.setup_support import finish_initial_daowen


def _engine():
    os.makedirs("/tmp/linji_tests", exist_ok=True)
    e = GameEngine(db_path=tempfile.mktemp(prefix="seal_auto_", suffix=".db"), rng_seed=17)
    result = e.execute_action("setup_attributes", {
        "name": "自动封印测试者", "blood_points": 7,
        "speed_points": 6, "mana_points": 6,
    })
    assert result["success"], result
    finish_initial_daowen(e)
    p = e.state.player
    p.dao_wen["封印"] = DaoWenInstance(
        DaoWen(name="封印", formula="", cost_type="异变", cost_formula="X", effect_formula=""))
    e.state.current_region = "龙心谷"
    e.state.phase = "in_combat"
    e.state.combat_subphase = "await_round_start"
    e.state.enemies[:] = [Entity(
        name="测试怪", entity_type="怪物", blood_limit=500, current_hp=500,
        attack_count=1, attack_power=1, speed_limit=0, current_speed=0,
    )]
    return e


def test_seal_is_registered_as_automatic_spell_at_enemy_round_start():
    e = _engine()
    p = e.state.player
    monster = e.state.enemies[0]
    assert e.execute_action("round_start", {})["success"]

    speed_before = p.current_speed
    # action_count 是轮回者本回合的真实主动预算；两次普攻都走真实两阶段接口。
    first = resolve_attack(e)
    second = resolve_attack(e)
    assert first["success"], first
    assert second["success"], second
    assert p.actions_used_this_round == p.action_count == 2
    assert p.current_speed == speed_before, "普攻不应消耗速度"
    assert monster.current_hp < monster.blood_limit, "普攻必须先造成真实伤害"

    prepared = e.execute_action("prepare_monster_phase", {})
    assert prepared["success"], prepared
    logs = prepared["result"]["spell_logs"]
    assert any(log.get("spell") == "封印·己方回合结束" and log.get("target") == monster.name
               for log in logs), logs
    assert p.mutation_count == 1
    assert e.state.enemies == [], "自动封印后当前怪物应暂离，怪物阶段没有攻击者"
    assert len(e.state.delayed_monster_reentries) == 1
    assert p.actions_used_this_round == 2, "自动法术不应额外消耗主动出手"


def test_automatic_seal_does_not_need_manual_spell_choices():
    e = _engine()
    assert e.execute_action("round_start", {})["success"]
    assert resolve_attack(e)["success"]
    assert resolve_attack(e)["success"]

    # prepare_monster_phase 不提交 spell_choices；自动法术由引擎在真实触发点装配，
    # 普通怪物阶段仍按其当前合法选项结算。
    prepared = e.execute_action("prepare_monster_phase", {})
    assert prepared["success"], prepared
    assert prepared["result"]["actors"] == []
