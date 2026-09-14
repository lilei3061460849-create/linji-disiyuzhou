"""以【封印】为核心的已学习自动法术回归测试。

重点：持有【封印】道纹本身不会凭空授予法术；只有把一个以【封印】为
效果步骤、触发条件为“自身回合结束”的 Spell 学进角色后，才会自动触发。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity, Spell
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


def _learn_seal_spell(e, name="封印术测试名"):
    e.state.player.spells.append(Spell(
        name=name,
        required_daowen=["封印"],
        trigger_condition="自身回合结束",
        effect_flow="发动封印X于任意目标",
    ))


def test_seal_does_not_grant_a_spell_by_itself():
    e = _engine()
    assert e.state.player.spells == []
    e.state.combat_subphase = "player_actions"
    actions = e._get_combat_actions()
    seal_actions = [a for a in actions["actions"]
                    if a.get("action_type") == "use_daowen"
                    and a.get("params_schema", {}).get("daowen_name") == "封印"]
    assert seal_actions, "仅持有道纹时，封印仍应作为普通道纹候选；不能凭空多出法术"


def test_learned_seal_spell_triggers_at_own_turn_end():
    e = _engine()
    _learn_seal_spell(e)
    p = e.state.player
    monster = e.state.enemies[0]
    assert e.execute_action("round_start", {})["success"]

    speed_before = p.current_speed
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
    assert any(log.get("spell") == "封印术测试名" and log.get("target") == monster.name
               for log in logs), logs
    assert p.mutation_count == 1
    assert e.state.enemies == [], "自动法术结算后当前怪物应暂离"
    assert len(e.state.delayed_monster_reentries) == 1
    assert p.actions_used_this_round == 2, "自动法术不应额外消耗主动出手"


def test_automatic_seal_spell_does_not_need_manual_spell_choices():
    e = _engine()
    _learn_seal_spell(e)
    assert e.execute_action("round_start", {})["success"]
    assert resolve_attack(e)["success"]
    assert resolve_attack(e)["success"]

    # prepare_monster_phase 不提交 spell_choices；已学习的“自身回合结束”法术
    # 在真实触发点自动装配参数，普通法术才继续走显式 spell_choices 契约。
    prepared = e.execute_action("prepare_monster_phase", {})
    assert prepared["success"], prepared
    assert prepared["result"]["actors"] == []
