"""统一 AI 入口回归测试。

AIPlayer 是对外唯一的玩家控制器；战斗战术不再由调用方另行创建 TacticalAI。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from engine.ai_player import AIPlayer
from engine.api import GameEngine
from tests.setup_support import begin_battle, begin_round, finish_initial_daowen


def _combat_engine(tmp_path):
    engine = GameEngine(
        db_path=str(tmp_path / "unified.db"),
        rng_seed=4,
        sealed_candidate_path=str(tmp_path / "sealed.json"),
    )
    assert engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })["success"]
    assert finish_initial_daowen(engine)["success"]
    assert engine.execute_action("setup_choose_resonance", {
        "resonance_type": "反转",
    })["success"]
    setup = engine.execute_action("setup_choose_region", {"region": "龙心谷"})
    assert setup["success"], setup
    assert begin_battle(engine)["success"]
    assert begin_round(engine)["success"]
    return engine


def test_ai_player_is_the_combat_and_high_level_entrypoint(tmp_path):
    """同一个 AIPlayer 同时提供开局控制器接口和招架战斗决策。"""
    engine = _combat_engine(tmp_path)
    engine.state.player.dao_wen.clear()
    engine.state.resonance.clear()
    engine.state.player.current_hp = 10
    enemy = engine.state.enemies[0]
    enemy.attack_count = 1
    enemy.attack_power = 12

    ai = AIPlayer(engine)
    result = ai.play_turn("战斗中评估本轮威胁")

    assert result["result"]["success"], result
    assert result["result"]["action"] == "贾凡招架"
    assert ai.last_decision["action"] == "declare_parry"
    assert len(ai.get_history()) == 1
