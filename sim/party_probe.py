#!/usr/bin/env python3
"""探针：新开局 -> 乱葬岗（一阶）第一场战斗，打印状态与可用行动。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.setup_support import finish_initial_daowen, begin_battle
from engine.api import GameEngine


def new_engine(seed: int):
    db = tempfile.mktemp(suffix=".db")
    e = GameEngine(db_path=db, rng_seed=seed, sealed_candidate_path="/tmp/party_probe_seal.json")
    e.execute_action("setup_attributes", {"name": "宿主", "blood_points": 10,
                                          "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    return e


if __name__ == "__main__":
    e = new_engine(1)
    print("energy", e.state.energy, "region", e.state.current_region)
    print("player", e.state.player and (e.state.player.name, e.state.player.blood_limit))
    r = begin_battle(e)
    print("battle_start:", {k: r.get(k) for k in ("success", "error", "message")})
    print("enemies", [(m.name, m.current_hp, m.attack_count, m.attack_power) for m in e.state.enemies])
    acts = e.get_available_actions()
    print("actions keys", list(acts.keys())[:20])
