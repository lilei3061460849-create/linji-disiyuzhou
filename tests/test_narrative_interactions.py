"""朋友/员工事件代价契约测试（回归：曾漏扣流血/碎片）。

断桥余烬·接过伤者：流血10；逆行者·让他同行：失去10碎片。
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine


def _engine(region: str = "龙心谷") -> GameEngine:
    e = GameEngine(db_path="/tmp/test_ally_cost.db", rng_seed=1)
    e.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = e.execute_action("setup_choose_region", {"region": region})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.phase = "pre_battle"
    return e


def test_error_ally_join_costs_are_paid():
    """错误输入回归：朋友/员工加入事件的代价必须真实扣除（曾漏扣）。

    断桥余烬·接过伤者：流血10；逆行者·让他同行：失去10碎片。
    """
    # 断桥余烬
    e = _engine("龙心谷")
    e.event_pool.current = "断桥余烬"
    hp0 = e.state.player.current_hp
    r = e.execute_action("resolve_event", {"event": "断桥余烬", "option_id": 1})
    assert r["success"]
    assert e.state.player.current_hp == hp0 - 10, f"应流血10，实{hp0 - e.state.player.current_hp}"
    assert any(f.name == "岩行者" for f in e.state.friends)
    # 逆行者
    e2 = _engine("龙心谷")
    e2.event_pool.current = "逆行者"
    s0 = e2.state.shards
    r2 = e2.execute_action("resolve_event", {"event": "逆行者", "option_id": 1})
    assert r2["success"]
    assert e2.state.shards == s0 - 10, f"应失去10碎片，实{s0 - e2.state.shards}"
    assert any(f.name == "赴火者" for f in e2.state.friends)
