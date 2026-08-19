from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, StatusEffect


def _state() -> tuple[GameState, CombatEngine, Entity, Entity]:
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=50)
    enemy = Entity("M", "怪物", blood_limit=100, current_hp=100)
    state.player = player
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def test_parasite_heal_records_parent_damage_context():
    _, combat, player, enemy = _state()
    enemy.add_status(StatusEffect("寄生", value=1, remaining_rounds=-1, source=player.name))

    detail = combat._apply_hostile_damage(enemy, 10, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 10, "tags": {"daowen"}, "event_id": "damage-1",
    })

    heal = detail["jisheng_heal"]
    assert heal["actual_heal"] == 2
    assert player.current_hp == 52
    assert heal["heal_ctx"]["mechanic"] == "heal"
    assert heal["heal_ctx"]["subtype"] == "parasite"
    assert heal["heal_ctx"]["parent_event_id"] == "damage-1"


def test_cut_blood_limit_change_records_parent_damage_context():
    _, combat, player, enemy = _state()
    player.add_status(StatusEffect("切割", value=1, remaining_rounds=3, source="test"))

    detail = combat._apply_hostile_damage(enemy, 7, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 7, "tags": {"daowen"}, "event_id": "damage-2",
    })

    assert detail["qiege_blood_loss"] == 7
    assert enemy.blood_limit == 93
    assert detail["qiege_ctx"]["mechanic"] == "blood_limit_change"
    assert detail["qiege_ctx"]["subtype"] == "cut"
    assert detail["qiege_ctx"]["parent_event_id"] == "damage-2"


def test_fuyuesuo_heal_records_parent_damage_context_and_heals_player():
    _, combat, player, enemy = _state()
    friend = Entity("F", "朋友", blood_limit=40, current_hp=40)
    friend.add_status(StatusEffect("负岳索", value=1, remaining_rounds=-1, source="负岳索"))
    combat.state.friends = [friend]

    detail = combat._apply_hostile_damage(friend, 6, source=enemy, ctx={
        "timing": "monster_action", "source": "普通攻击", "source_type": "attack",
        "actor": enemy, "target": friend, "mechanic": "damage", "subtype": "attack",
        "amount": 6, "tags": {"attack"}, "event_id": "damage-3",
    })

    assert friend.current_hp == 34
    assert player.current_hp == 56
    heal = detail["fuyuesuo_heal"]
    assert heal["actual_heal"] == 6
    assert heal["heal_ctx"]["source"] == "负岳索"
    assert heal["heal_ctx"]["parent_event_id"] == "damage-3"
    assert not friend.has_status("负岳索")
