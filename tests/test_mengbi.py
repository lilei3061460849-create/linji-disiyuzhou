"""蒙蔽X：消耗5X，使[目标]下X次造成的伤害无效。

此前测绿的是：怪物激活 / 强光探照灯 直接 add_status，以及状态已在身上时
resolve_attack / apply_daowen_effect 会扣层。轮回者 use_daowen 只算出
invalid_damage_hits，从未挂状态。
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.daowen import DaoWenEngine
from engine.models import DaoWen, DaoWenInstance, Entity, StatusEffect


def _engine(suffix):
    os.makedirs("/tmp/linji_tests", exist_ok=True)
    engine = GameEngine(db_path=f"/tmp/linji_tests/test_mengbi_{suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    finish_initial_daowen(engine)
    engine.state.current_region = "龙心谷"
    engine.state.phase = "in_combat"
    p = engine.state.player
    p.dao_wen["蒙蔽"] = DaoWenInstance(
        DaoWen(name="蒙蔽", formula="", cost_type="消耗", cost_formula="5X", effect_formula=""))
    p.speed_limit = 12
    p.current_speed = 12
    p.current_mana = 40
    p.mana_limit = 40
    return engine


def _enemy_caster(engine, name="对手"):
    foe = Entity(name=name, entity_type="轮回者", blood_limit=80, current_hp=80,
                 mana_limit=20, current_mana=20, speed_limit=12, current_speed=12,
                 attack_count=1, attack_power=8)
    foe.dao_wen["杀伐"] = DaoWenInstance(
        DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    engine.state.enemies.append(foe)
    return foe


def test_use_daowen_mengbi_blocks_next_hits():
    """正常路径：蒙蔽2 后，目标接下来两次伤害归零，第三次落地。"""
    engine = _engine("happy")
    foe = _enemy_caster(engine)
    engine.execute_action("round_start", {})
    mana_before = engine.state.player.current_mana
    r = engine.execute_action("use_daowen", {
        "daowen_name": "蒙蔽", "x": 2, "target": foe.name,
    })
    assert r["success"], r
    assert foe.has_status("蒙蔽")
    assert foe.get_status_value("蒙蔽") == 2
    assert engine.state.player.current_mana == mana_before - 10

    hp = engine.state.player.current_hp
    player = engine.state.player

    def _shaifa():
        calc = DaoWenEngine.resolve("杀伐", 3, target=player, caster=foe)
        return engine.combat.apply_daowen_effect("杀伐", calc, foe, player)

    a1 = _shaifa()
    assert a1.get("mengbi_blocked") is True
    assert player.current_hp == hp
    assert foe.get_status_value("蒙蔽") == 1

    a2 = _shaifa()
    assert a2.get("mengbi_blocked") is True
    assert player.current_hp == hp
    assert not foe.has_status("蒙蔽")

    a3 = _shaifa()
    assert not a3.get("mengbi_blocked")
    assert player.current_hp == hp - 6


def test_mengbi_x1_blocks_one_attack_then_expires():
    """边界：X=1 只挡一次普攻，层数归零后下一击照常。"""
    engine = _engine("bound")
    foe = _enemy_caster(engine)
    engine.execute_action("round_start", {})
    r = engine.execute_action("use_daowen", {
        "daowen_name": "蒙蔽", "x": 1, "target": foe.name,
    })
    assert r["success"]
    assert foe.get_status_value("蒙蔽") == 1
    hp = engine.state.player.current_hp
    d1 = engine.combat.resolve_attack(foe, engine.state.player, dodge=False)
    assert d1.get("blocked_by") == "蒙蔽"
    assert d1["damage_dealt"] == 0
    assert engine.state.player.current_hp == hp
    assert not foe.has_status("蒙蔽")
    d2 = engine.combat.resolve_attack(foe, engine.state.player, dodge=False)
    assert d2.get("blocked_by") != "蒙蔽"
    assert d2["damage_dealt"] == 8
    assert engine.state.player.current_hp == hp - 8


def test_mengbi_stacks_and_rejects_bad_input():
    """错误输入：法力不足 / X<1 失败；叠加两次层数相加。"""
    engine = _engine("invalid")
    foe = _enemy_caster(engine)
    engine.execute_action("round_start", {})

    engine.state.player.current_mana = 4
    r1 = engine.execute_action("use_daowen", {
        "daowen_name": "蒙蔽", "x": 1, "target": foe.name,
    })
    assert r1["success"] is False
    assert "法力不足" in r1["error"]
    assert not foe.has_status("蒙蔽")

    engine.state.player.current_mana = 40
    r2 = engine.execute_action("use_daowen", {
        "daowen_name": "蒙蔽", "x": 0, "target": foe.name,
    })
    assert r2["success"] is False
    assert "X必须≥1" in r2["error"]

    r3 = engine.execute_action("use_daowen", {
        "daowen_name": "蒙蔽", "x": 1, "target": foe.name,
    })
    assert r3["success"]
    r4 = engine.execute_action("use_daowen", {
        "daowen_name": "蒙蔽", "x": 2, "target": foe.name,
    })
    assert r4["success"]
    assert foe.get_status_value("蒙蔽") == 3

    r5 = engine.execute_action("use_daowen", {
        "daowen_name": "蒙蔽", "x": 1, "target": "路人",
    })
    assert r5["success"] is False
    assert foe.get_status_value("蒙蔽") == 3


def test_mengbi_dodged_does_not_apply_status():
    """边界：目标有速度并闪避时，法力与出手仍扣，蒙蔽不挂上。"""
    engine = _engine("dodge")
    foe = _enemy_caster(engine)
    engine.execute_action("round_start", {})
    mana = engine.state.player.current_mana
    spd = foe.current_speed
    r = engine.execute_action("use_daowen", {
        "daowen_name": "蒙蔽", "x": 1, "target": foe.name, "dodge": True,
    })
    assert r["success"]
    assert r["dodge"]["fully_dodged"] is True
    assert not foe.has_status("蒙蔽")
    assert engine.state.player.current_mana == mana - 5
    assert foe.current_speed == spd - 1
