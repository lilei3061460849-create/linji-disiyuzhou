from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.setup_support import finish_initial_daowen
from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, Relic


def test_blood_oath_ring_does_not_trigger_on_prebattle_event_bleed():
    """血誓戒只应监听战斗回合内流血；局外事件流血不应白给格挡。"""
    state = GameState(phase="pre_battle")
    player = Entity(
        "探针", "轮回者",
        blood_limit=90, current_hp=90,
        mana_limit=10, current_mana=10,
        speed_limit=5, current_speed=5,
    )
    state.player = player
    state.relics = [Relic("血誓戒", "")]
    combat = CombatEngine(state, DiceEngine())

    detail = combat.pay_numeric_cost(player, "流血", 10)

    assert detail["owner"]["paid"] == 10
    assert player.current_hp == 80
    assert player.shield == 0
    assert player.blood_oath_used_this_round is False
    assert "blood_oath" not in detail["owner"]["detail"]


def test_brand_nail_target_ref_validates_after_monster_draw(tmp_path):
    """烙痕钉战始选 enemy:0 时，抽怪前允许延迟校验，抽怪后锁定真实目标。"""
    engine = GameEngine(db_path=str(tmp_path / "brand_nail.db"), rng_seed=508)
    setup = engine.execute_action("setup_attributes", {
        "name": "探针", "blood_points": 15, "speed_points": 5, "mana_points": 5,
    })
    engine.execute_action("choose_discovered_relic", {
        "relic_name": setup["result"]["relic_choices"][0],
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    engine.execute_action("setup_choose_region", {"region": "龙心谷"})
    engine.state.relics = [Relic("烙痕钉", "")]
    engine.state.energy = 0
    assert engine.state.enemies == []

    result = engine.execute_action("battle_start", {
        "relic_choices": {"烙痕钉": {"target_ref": "enemy:0"}},
    })

    assert result["success"], result
    assert engine.state.enemies, "battle_start 应先抽出真实怪物，再完成烙痕钉锁定"
    assert "烙痕钉：已锁定目标" in result["relic_logs"]
    assert engine.state.event_modifiers["brand_nail_target_ref"] == "enemy:0"


def test_brand_nail_bad_target_rolls_back_after_deferred_validation(tmp_path):
    """延迟校验不能放过不存在的 enemy:999；失败也应保持 action 原子性。"""
    engine = GameEngine(db_path=str(tmp_path / "brand_nail_bad.db"), rng_seed=508)
    setup = engine.execute_action("setup_attributes", {
        "name": "探针", "blood_points": 15, "speed_points": 5, "mana_points": 5,
    })
    engine.execute_action("choose_discovered_relic", {
        "relic_name": setup["result"]["relic_choices"][0],
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    engine.execute_action("setup_choose_region", {"region": "龙心谷"})
    engine.state.relics = [Relic("烙痕钉", "")]
    engine.state.energy = 0

    result = engine.execute_action("battle_start", {
        "relic_choices": {"烙痕钉": {"target_ref": "enemy:999"}},
    })

    assert not result["success"]
    assert "烙痕钉" in result["error"]
    assert engine.state.current_battle == 0
    assert engine.state.enemies == []
    assert "brand_nail_target_ref" not in engine.state.event_modifiers
