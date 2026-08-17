"""活力全局裁定回归（2026-08-17）：活力X → 所有角色出手次数+X。

- 正常：怪物激活活力后，双方全部存活角色获得活力状态；次回合玩家出手+X、
  怪物攻击出手数 1+X；无目标提交（全局道纹）。
- 边界：激活当回合的攻击出手数不变（自下回合生效）；X=0 不加成；已死角色不落状态。
- 错误：为活力提交 target_ref 必须被拒绝（全局道纹不接受目标）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity
from tests.monster_phase_support import resolve_monster_phase


def _engine(suffix):
    os.makedirs("/tmp/linji_tests", exist_ok=True)
    engine = GameEngine(db_path=f"/tmp/linji_tests/test_huoli_global_{suffix}.db", rng_seed=7)
    engine.execute_action("setup_attributes", {
        "name": "试者", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    engine.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    engine.state.phase = "in_combat"
    engine.state.current_round = 2  # 跳过白板回合，怪物可出道纹
    return engine


def _vitality_monster(engine, x=2, hp=100):
    m = Entity(name="活力怪", entity_type="怪物", blood_limit=hp, current_hp=hp,
               attack_count=1, attack_power=1)
    m.dao_wen["活力"] = DaoWenInstance(DaoWen(
        name="活力", formula="", cost_type="异变", cost_formula="5X", effect_formula=""),
        x_value=x)
    engine.state.enemies.append(m)
    return m


def test_normal_vitality_global_boost():
    """正常：激活活力 → 全场存活角色获状态；次回合玩家出手+X、怪物攻击轮 1+X。"""
    engine = _engine("normal")
    p = engine.state.player
    m = _vitality_monster(engine, x=2)
    base_actions = p.action_count

    resolved = resolve_monster_phase(engine.combat, daowen_choices={"enemy:0": "活力"})

    dao_entry = next(d for d in resolved if "daowen_activated" in d)
    assert dao_entry["daowen_activated"] == "活力"
    stamped = {e["target"] for e in dao_entry["execution"]["effects"] if e["type"] == "status_added"}
    assert stamped == {"试者", "活力怪"}, "活力必须盖到双方全部存活角色"
    assert p.get_status_value("活力") == 2 and m.get_status_value("活力") == 2
    assert p.action_count == base_actions + 2, "玩家出手次数应+X"

    engine.state.current_round = 3
    prepared = engine.combat.prepare_monster_phase()
    actor = next(a for a in prepared["actors"] if a["actor_ref"] == "enemy:0")
    assert actor["base_attack_actions"] == 1 + 2, "怪物攻击出手数应为 1+X"


def test_boundary_activation_round_and_dead_entities():
    """边界：激活当回合出手数不变（下回合生效）；X=0 不加成；死亡角色不落状态。"""
    engine = _engine("boundary")
    p = engine.state.player
    m = _vitality_monster(engine, x=2)
    dead = _vitality_monster(engine, hp=50)
    dead.name = "亡怪"
    dead.current_hp = 0
    dead.is_alive = False

    prepared = engine.combat.prepare_monster_phase()
    actor = next(a for a in prepared["actors"] if a["actor_ref"] == "enemy:0")
    assert actor["base_attack_actions"] == 1, "激活当回合不得提前享受活力"

    resolved = resolve_monster_phase(engine.combat, daowen_choices={"enemy:0": "活力"})
    dao_entry = next(d for d in resolved if "daowen_activated" in d)
    stamped = {e["target"] for e in dao_entry["execution"]["effects"] if e["type"] == "status_added"}
    assert "亡怪" not in stamped, "死亡角色不落活力状态"
    assert stamped == {"试者", "活力怪"}

    m0 = _vitality_monster(engine, x=0, hp=80)
    m0.dao_wen["活力"].x_value = 0
    engine.state.current_round = 4
    prepared2 = engine.combat.prepare_monster_phase()
    actor0 = next(a for a in prepared2["actors"] if a["actor_ref"] == f"enemy:{engine.state.enemies.index(m0)}")
    assert actor0["base_attack_actions"] == 1, "活力X=0 不产生加成"


def test_error_vitality_rejects_target_ref():
    """错误输入：为全局活力提交 target_ref 必须被拒绝。"""
    engine = _engine("error")
    _vitality_monster(engine, x=2)
    prepared = engine.combat.prepare_monster_phase()
    actor = next(a for a in prepared["actors"] if a["actor_ref"] == "enemy:0")
    option = next(o for o in actor["daowen_options"] if o["name"] == "活力")
    assert option["requires_target"] is False, "活力不得再要求目标"

    choice = {"actor_ref": "enemy:0",
              "daowen": {"name": "活力", "dodge": False, "blood_shadow": False,
                         "trigger_spell_choices": {}, "target_ref": "player:0"},
              "attack_actions": [{"hits": [{"target_ref": "player:0", "dodge": False,
                                            "blood_shadow": False, "spell_choices": {}}]}]}
    with pytest.raises(ValueError, match="不接受target_ref"):
        engine.combat.resolve_monster_phase([choice], prepared=prepared)
