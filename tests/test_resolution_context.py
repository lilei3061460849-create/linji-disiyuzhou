from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState


def _engine_with_enemy(enemy: Entity) -> GameEngine:
    engine = GameEngine(rng_seed=1)
    player = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    engine.state.player = player
    engine.state.enemies = [enemy]
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "await_round_end"
    engine.state.current_battle = 1
    engine.combat.state = engine.state
    return engine


def test_battle_end_death_gives_shard_reward_context():
    enemy = Entity("M", "怪物", blood_limit=100, current_hp=0)
    enemy.is_alive = False
    enemy.battle_start_blood_limit = 100
    enemy.dao_wen = {}
    enemy._death_ctx = {
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "mechanic": "death", "subtype": "hp_zero", "event_id": "death-1",
        "tags": [], "parent_event_id": "damage-1", "actor": "P", "target": "M", "owner": None, "amount": 0,
    }
    engine = _engine_with_enemy(enemy)

    result = engine.execute_action("battle_end", {})

    assert result["success"], result
    rewards = result["result"]["death_shard_rewards"]
    assert rewards and rewards[0]["reward"] == 2
    assert rewards[0]["ctx"]["subtype"] == "death_shard_reward"
    assert rewards[0]["ctx"]["parent_event_id"] == "death-1"
    assert result["result"]["removed_via_alt_path"] == []


def test_battle_end_departure_has_no_shard_context():
    enemy = Entity("M", "怪物", blood_limit=100, current_hp=50)
    enemy.battle_start_blood_limit = 100
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    state.player = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    state.enemies = [enemy]
    combat = CombatEngine(state, DiceEngine())
    combat._remove_from_combat(enemy, "癌变", ctx={
        "timing": "player_action", "source": "癌变", "source_type": "system",
        "target": enemy, "mechanic": "leave", "subtype": "cancer", "event_id": "leave-1",
        "tags": {"leave", "no_shards"},
    })

    engine = GameEngine(rng_seed=1)
    engine.state = state
    engine.combat.state = state
    engine.combat = combat
    state.combat_subphase = "await_round_end"
    state.current_battle = 1

    result = engine.execute_action("battle_end", {})

    assert result["success"], result
    assert result["result"]["shard_reward"] == 0
    assert result["result"]["death_shard_rewards"] == []
    removed = result["result"]["removed_via_alt_path"]
    assert removed and removed[0]["ctx"]["subtype"] == "leave_no_shards"
    assert removed[0]["ctx"]["parent_event_id"] == enemy._leave_ctx["event_id"]
    assert enemy._leave_ctx["parent_event_id"] == "leave-1"
