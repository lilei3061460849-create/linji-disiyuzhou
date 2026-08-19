from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, Relic


def test_damage_death_records_death_context_parent_event():
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    enemy = Entity("M", "怪物", blood_limit=10, current_hp=10)
    state.player = player
    state.enemies = [enemy]
    combat = CombatEngine(state, DiceEngine())

    detail = combat._apply_hostile_damage(enemy, 15, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 15, "tags": {"daowen"}, "event_id": "damage-kill-1",
    })

    assert detail["died"] is True
    assert enemy.is_alive is False
    assert enemy._death_ctx["mechanic"] == "death"
    assert enemy._death_ctx["parent_event_id"] == "damage-kill-1"
    assert enemy._death_ctx["source"] == "杀伐"


def test_charred_hair_speed_gain_parent_is_death_event():
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=60, current_hp=60, speed_limit=5, current_speed=5)
    enemy = Entity("M", "怪物", blood_limit=10, current_hp=10)
    state.player = player
    state.enemies = [enemy]
    state.relics = [Relic("焦黑发丝", "")]
    combat = CombatEngine(state, DiceEngine())

    combat._apply_hostile_damage(enemy, 15, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 15, "tags": {"daowen"}, "event_id": "damage-kill-2",
    })

    assert player.current_speed == 7
    speed_events = getattr(player, "_speed_change_events", [])
    assert speed_events[-1]["source"] == "焦黑发丝"
    assert speed_events[-1]["parent_event_id"] == enemy._death_ctx["event_id"]


def test_raw_hp_loss_death_records_legacy_death_context():
    state = GameState(phase="in_combat", combat_subphase="round_end")
    player = Entity("P", "轮回者", blood_limit=10, current_hp=5)
    state.player = player
    combat = CombatEngine(state, DiceEngine())

    result = combat._raw_hp_loss(player, 9)

    assert result["died"] is True
    assert player._death_ctx["mechanic"] == "death"
    assert player._death_ctx["tags"] == ["legacy_context"]
    assert player._death_ctx["parent_event_id"] == result["hp_loss_ctx"]["event_id"]
