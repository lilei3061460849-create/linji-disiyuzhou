"""封印仅移出怪物；雕塑对任何非轮回者；轮回者开局攻面板 0×0。

封印 README：使 X 个[目标]怪物移出本场战斗。
雕塑：用户裁定对任何非轮回者（怪物/微光者/赤族等）；轮回者不触发。
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import DaoWen, DaoWenInstance, Entity, GameState


def _engine(suffix):
    os.makedirs("/tmp/linji_tests", exist_ok=True)
    engine = GameEngine(db_path=f"/tmp/linji_tests/test_seal_sculp_{suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    finish_initial_daowen(engine)
    engine.state.current_region = "龙心谷"
    engine.state.phase = "in_combat"
    p = engine.state.player
    p.dao_wen["封印"] = DaoWenInstance(
        DaoWen(name="封印", formula="", cost_type="消耗", cost_formula="10X", effect_formula=""))
    p.speed_limit = 12
    p.current_speed = 12
    p.current_mana = 40
    p.mana_limit = 40
    return engine


def _monster(name, hp=80, atk=4, power=6):
    return Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
                  attack_count=atk, attack_power=power)


# ========================================================================
# 封印
# ========================================================================

def test_seal_removes_x_monsters():
    """正常路径：封印X=1（代价：异变8X）移出一只活怪，另一只留下，不产击杀标记。"""
    engine = _engine("seal_happy")
    a = _monster("怪甲")
    b = _monster("怪乙")
    engine.state.enemies.extend([a, b])
    engine.execute_action("round_start", {})
    mana = engine.state.player.current_mana
    r = engine.execute_action("use_daowen", {"daowen_name": "封印", "x": 1, "target": "怪甲"})
    assert r["success"], r
    assert engine.state.player.current_mana == mana  # 封印改为代价：异变8X，不消耗法力
    assert engine.state.player.mutation_count == 8
    seal = next(e for e in r["execution"]["effects"] if e["type"] == "seal")
    assert seal["removed"] == 1
    assert seal["targets"] == ["怪甲"]
    assert not a.is_alive and a.removed_without_kill
    assert b.is_alive and not b.removed_without_kill


def test_seal_skips_reincarnator_and_weiguang_then_takes_monster():
    """边界：敌方混有轮回者/微光者/怪物时，只移出怪物。"""
    engine = _engine("seal_bound")
    foe = Entity(name="敌对轮回者", entity_type="轮回者", blood_limit=70, current_hp=70,
                 mana_limit=20, current_mana=20, speed_limit=6, current_speed=6,
                 attack_count=1, attack_power=1)
    ally = Entity(name="敌方朋友", entity_type="朋友", blood_limit=30, current_hp=30,
                  attack_count=3, attack_power=4)
    m = _monster("真怪")
    engine.state.enemies.extend([foe, ally, m])
    engine.execute_action("round_start", {})
    r = engine.execute_action("use_daowen", {"daowen_name": "封印", "x": 2, "target": foe.name})
    assert r["success"], r
    seal = next(e for e in r["execution"]["effects"] if e["type"] == "seal")
    assert seal["removed"] == 1
    assert seal["targets"] == ["真怪"]
    assert foe.is_alive and ally.is_alive
    assert not m.is_alive and m.removed_without_kill


def test_seal_on_duel_reincarnator_only_removes_zero():
    """错误输入/对照：场上没有怪物时封印仍支付异变8X，移出 0，轮回者留下。"""
    engine = _engine("seal_invalid")
    foe = Entity(name="敌对轮回者", entity_type="轮回者", blood_limit=70, current_hp=70,
                 attack_count=1, attack_power=1)
    emp = Entity(name="叛变员工", entity_type="员工", blood_limit=40, current_hp=40,
                 attack_count=2, attack_power=4)
    engine.state.enemies.extend([foe, emp])
    engine.execute_action("round_start", {})
    mana = engine.state.player.current_mana
    r = engine.execute_action("use_daowen", {"daowen_name": "封印", "x": 1, "target": foe.name})
    assert r["success"], r
    assert engine.state.player.current_mana == mana  # 异变8X代价，不消耗法力
    assert engine.state.player.mutation_count == 8
    seal = next(e for e in r["execution"]["effects"] if e["type"] == "seal")
    assert seal["removed"] == 0
    assert seal["targets"] == []
    assert foe.is_alive and emp.is_alive


# ========================================================================
# 雕塑
# ========================================================================

def test_setup_reincarnator_attack_panel_is_zero():
    """正常路径：setup 后轮回者攻击次数/攻击力为 0×0。"""
    engine = _engine("zero_atk")
    p = engine.state.player
    assert p.attack_count == 0
    assert p.attack_power == 0


def test_sculpture_monster_and_weiguang_on_both_sides():
    """正常路径：怪物与己方微光者攻力归 0 都化为雕塑。"""
    state = GameState()
    state.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                          attack_count=0, attack_power=0)
    m = _monster("石像鬼", hp=100, atk=2, power=0)
    friend = Entity(name="岩行者", entity_type="朋友", blood_limit=40, current_hp=40,
                    attack_count=3, attack_power=0, is_deployed=True)
    state.enemies.append(m)
    state.friends.append(friend)
    combat = CombatEngine(state, DiceEngine())
    paths = combat.settle_victory_paths()
    kinds = [p["type"] for p in paths]
    assert kinds.count("sculpture") == 2
    assert m.is_sculptured and not m.is_alive
    assert friend.is_sculptured and not friend.is_alive
    names = {c.name for c in state.consumables if c.kind == "sculpture"}
    assert names == {"石像鬼雕塑", "岩行者雕塑"}


def test_sculpture_employee_and_temp_friend_zero_count():
    """边界：员工攻次归 0、临时朋友攻力归 0 也触发；攻力仍为 1 的微光者不触发。"""
    state = GameState()
    state.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60)
    emp = Entity(name="打手", entity_type="员工", blood_limit=48, current_hp=48,
                 attack_count=0, attack_power=6, is_deployed=True)
    temp = Entity(name="路人", entity_type="临时朋友", blood_limit=20, current_hp=20,
                  attack_count=2, attack_power=0)
    ok = Entity(name="力士", entity_type="朋友", blood_limit=30, current_hp=30,
                attack_count=3, attack_power=1, is_deployed=True)
    state.employees.append(emp)
    state.temp_friends.append(temp)
    state.friends.append(ok)
    combat = CombatEngine(state, DiceEngine())
    paths = combat.settle_victory_paths()
    assert sum(1 for p in paths if p["type"] == "sculpture") == 2
    assert emp.is_sculptured and temp.is_sculptured
    assert ok.is_alive and not ok.is_sculptured


def test_sculpture_includes_chizu_skips_reincarnator():
    """边界：赤族攻力归 0 雕塑；双方轮回者 0×0 不雕塑。"""
    state = GameState()
    player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                    attack_count=0, attack_power=0)
    state.player = player
    foe = Entity(name="敌对轮回者", entity_type="轮回者", blood_limit=70, current_hp=70,
                 attack_count=0, attack_power=0)
    chizu = Entity(name="赤仆", entity_type="赤族", blood_limit=30, current_hp=30,
                   attack_count=0, attack_power=0)
    state.enemies.append(foe)
    state.friends.append(chizu)
    combat = CombatEngine(state, DiceEngine())
    paths = combat.settle_victory_paths()
    assert sum(1 for p in paths if p["type"] == "sculpture") == 1
    assert chizu.is_sculptured and not chizu.is_alive
    assert player.is_alive and not player.is_sculptured
    assert foe.is_alive and not foe.is_sculptured
    assert any(c.name == "赤仆雕塑" for c in state.consumables)


def test_sculpture_skips_reincarnator_even_if_forced_zero():
    """错误输入/对照：只剩轮回者时，攻次/攻力 0 不产生雕塑。"""
    state = GameState()
    player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                    attack_count=0, attack_power=0)
    state.player = player
    foe = Entity(name="敌对轮回者", entity_type="轮回者", blood_limit=70, current_hp=70,
                 attack_count=0, attack_power=0)
    state.enemies.append(foe)
    combat = CombatEngine(state, DiceEngine())
    paths = combat.settle_victory_paths()
    assert not any(p["type"] == "sculpture" for p in paths)
    assert player.is_alive and foe.is_alive
    assert state.consumables == []
