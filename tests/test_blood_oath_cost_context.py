from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, Relic


def _state_with_oath_and(*relics: str) -> tuple[GameState, CombatEngine, Entity]:
    state = GameState(phase="in_combat", combat_subphase="await_round_start")
    player = Entity(
        "探针", "轮回者",
        blood_limit=90, current_hp=90,
        mana_limit=10, current_mana=0,
        speed_limit=5, current_speed=5,
    )
    state.player = player
    state.relics = [Relic("血誓戒", ""), *(Relic(name, "") for name in relics)]
    state.enemies = [Entity("靶怪", "怪物", blood_limit=100, current_hp=100)]
    return state, CombatEngine(state, DiceEngine()), player


def test_blood_oath_ignores_battle_start_scarlet_fruit_context():
    """战始流血不是回始/行动/反应窗口，不触发血誓戒，也不消耗首次标记。"""
    state, combat, player = _state_with_oath_and("猩红果实")

    logs = combat.process_relics("battle_start", {
        "relic_choices": {"猩红果实": {"use": True}},
    })

    assert logs == ["猩红果实：流血10；战终血限+2"]
    assert player.current_hp == 80
    assert player.shield == 0
    assert player.blood_oath_used_this_round is False


def test_blood_oath_triggers_on_first_round_start_blood_pact_context():
    """第一回合回始 current_round 仍为0，但上下文为 round_start，应正常触发血誓戒。"""
    state, combat, player = _state_with_oath_and("血契")
    state.current_round = 0

    result = combat.round_start({"血契": {"use": True, "x": 1}})

    assert result["round"] == 1
    assert player.current_hp == 86
    assert player.shield == 4
    assert player.blood_oath_used_this_round is True


def test_battle_start_bleed_does_not_consume_then_round_start_can_trigger():
    """先战始猩红果实，再第一回始血契：首次主动流血应留给回始血契。"""
    state, combat, player = _state_with_oath_and("猩红果实", "血契")
    combat.process_relics("battle_start", {
        "relic_choices": {"猩红果实": {"use": True}},
    })
    assert player.current_hp == 80
    assert player.shield == 0
    assert player.blood_oath_used_this_round is False

    result = combat.round_start({"血契": {"use": True, "x": 1}})

    assert result["round"] == 1
    assert player.current_hp == 76
    assert player.shield == 4
    assert player.blood_oath_used_this_round is True


def test_uncategorized_direct_bleed_does_not_trigger_blood_oath():
    """未显式传入 cost_context 的底层扣血不触发主动/时点监听。"""
    _, combat, player = _state_with_oath_and()

    detail = combat.pay_numeric_cost(player, "流血", 5)

    assert detail["owner"]["paid"] == 5
    assert player.current_hp == 85
    assert player.shield == 0
    assert player.blood_oath_used_this_round is False
    assert "context_warning" in detail["owner"]["detail"]


def test_player_action_bleed_context_triggers_blood_oath():
    """玩家行动来源的主动流血仍触发血誓戒。"""
    _, combat, player = _state_with_oath_and()

    detail = combat.pay_numeric_cost(
        player, "流血", 5,
        cost_context={"timing": "player_action", "source": "血债", "source_type": "daowen", "tags": {"active_payment"}},
    )

    assert detail["owner"]["detail"]["blood_oath"] == {"type": "shield", "amount": 5}
    assert player.current_hp == 85
    assert player.shield == 5
    assert player.blood_oath_used_this_round is True
