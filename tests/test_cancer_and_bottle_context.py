from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Consumable, Entity, GameState


def test_cancer_context_uses_last_heal_event_and_death_parent():
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=10, current_hp=5)
    state.player = player
    combat = CombatEngine(state, DiceEngine())

    heal = state.apply_heal(player, 20, ctx={
        "timing": "player_action", "source": "再生", "source_type": "daowen",
        "actor": player, "target": player, "mechanic": "heal", "subtype": "daowen",
        "amount": 20, "tags": {"daowen"}, "event_id": "heal-cancer-1",
    })
    cancer = combat.check_cancer(player)

    assert cancer is not None
    assert cancer["ctx"]["mechanic"] == "cancer"
    assert cancer["ctx"]["parent_event_id"] == heal["heal_ctx"]["event_id"]
    assert player.is_alive is False
    assert player._death_ctx["parent_event_id"] == cancer["ctx"]["event_id"]


def test_monster_cancer_context_uses_last_heal_event_without_death():
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    monster = Entity("M", "怪物", blood_limit=10, current_hp=5)
    state.player = player
    state.enemies = [monster]
    combat = CombatEngine(state, DiceEngine())

    heal = state.apply_heal(monster, 20, ctx={
        "timing": "player_action", "source": "再生", "source_type": "daowen",
        "actor": player, "target": monster, "mechanic": "heal", "subtype": "daowen",
        "amount": 20, "tags": {"daowen"}, "event_id": "heal-cancer-2",
    })
    cancer = combat.check_cancer(monster)

    assert cancer is not None and cancer["type"] == "proliferation"
    assert cancer["ctx"]["parent_event_id"] == heal["heal_ctx"]["event_id"]
    assert monster.is_departed is True
    assert not hasattr(monster, "_death_ctx")


def test_dragon_blood_bottle_extract_heal_has_context(tmp_path):
    engine = GameEngine(db_path=str(tmp_path / "bottle_ctx.db"), rng_seed=1)
    player = Entity("P", "轮回者", blood_limit=100, current_hp=50)
    engine.state.player = player
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    bottle = Consumable("龙血瓶", "", current_uses=10, max_uses=10)
    engine.state.consumables = [bottle]

    result = engine.execute_action("consume_item", {
        "name": "龙血瓶", "amount": 4, "target_ref": "player:0",
    })

    assert result["success"], result
    heal = result["result"]["heal"]
    assert heal["actual_heal"] == 4
    assert heal["heal_ctx"]["source"] == "龙血瓶"
    assert heal["heal_ctx"]["subtype"] == "dragon_blood_bottle_extract"
    assert bottle.current_uses == 6
