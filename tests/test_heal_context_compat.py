from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Consumable, Entity, GameState, Relic, StatusEffect


def test_apply_heal_legacy_context_warning_and_overheal_storage_ctx():
    state = GameState(phase="in_combat")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=95)
    state.player = player
    bottle = Consumable("龙血瓶", "", current_uses=10, max_uses=10)
    state.consumables = [bottle]

    detail = state.apply_heal(player, 12)

    assert detail["actual_heal"] == 5
    assert detail["overheal"] == 7
    assert detail["heal_ctx"]["mechanic"] == "heal"
    assert detail["heal_ctx"]["tags"] == ["legacy_context"]
    assert "context_warning" in detail
    assert detail["dragon_blood_bottle_stored"] == 7
    assert detail["dragon_blood_bottle_ctx"]["mechanic"] == "heal_storage"
    assert detail["dragon_blood_bottle_ctx"]["parent_event_id"] == detail["heal_ctx"]["event_id"]
    assert bottle.current_uses == 17 and bottle.max_uses == 17


def test_daowen_heal_effect_has_context_without_warning():
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    caster = Entity("P", "轮回者", blood_limit=100, current_hp=50)
    target = caster
    state.player = caster
    combat = CombatEngine(state, DiceEngine())

    result = combat.apply_daowen_effect("再生", {"x": 3, "target_heal": 9}, caster, target)
    heal = result["effects"][0]

    assert heal["actual_heal"] == 9
    assert "context_warning" not in heal
    assert heal["heal_ctx"]["source"] == "再生"
    assert heal["heal_ctx"]["source_type"] == "daowen"
    assert heal["heal_ctx"]["subtype"] == "daowen"


def test_huoxue_and_blood_lineage_heals_have_contexts():
    state = GameState(phase="in_combat", combat_subphase="await_round_end")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=70)
    state.player = player
    state.relics = []
    combat = CombatEngine(state, DiceEngine())

    player.add_status(StatusEffect("活血", value=1, remaining_rounds=-1, source="test"))
    player.hp_lost_this_round = 8
    result = combat.round_end()
    huoxue = next(e for e in result["effects"] if e.get("type") == "huoxue_heal")
    assert huoxue["actual"] == 4
    assert huoxue["heal_ctx"]["source"] == "活血"
    assert huoxue["heal_ctx"]["mechanic"] == "heal"

    # 单独测血族血脉，避免活血干扰。
    state = GameState(phase="in_combat", combat_subphase="await_round_end")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=70)
    state.player = player
    state.relics = []
    player.relics = []
    state.relics.append(Relic("血族血脉", "", tags=["血族"]))
    player.damage_dealt_this_round = 6
    combat = CombatEngine(state, DiceEngine())
    result = combat.round_end()
    lineage = next(e for e in result["effects"] if e.get("type") == "blood_lineage_heal")
    assert lineage["amount"] == 6
    assert lineage["heal_ctx"]["source"] == "血族血脉"
    assert lineage["heal_ctx"]["subtype"] == "blood_lineage"
