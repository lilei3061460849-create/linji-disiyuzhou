"""封印仅移出怪物；雕塑对任何非轮回者；轮回者开局攻面板 0×0。

封印 规则正文：使 X 个[目标]怪物移出本场战斗。
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
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
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

def test_setup_reincarnator_attack_panel_derives_from_speed_and_mana():
    """DM裁定 2026-09-10：轮回者攻次=当前速度、攻力=当前法力（原「初始1×1面板」口径废止）。

    夹具把当前速度设为12、当前法力设为40，因此攻次/攻力应分别为 12/40。
    雕塑**不再**排除轮回者（见下两条）。
    """
    engine = _engine("one_atk")
    p = engine.state.player
    assert p.effective_attack_count() == p.current_speed == 12
    assert p.effective_attack_power() == p.current_mana == 40
    # DM裁定 2026-09-09：轮回者既有攻击力，雕塑不再排除轮回者
    assert engine.combat._can_be_sculptured(p) is True


def test_sculpture_monster_and_weiguang_on_both_sides():
    """正常路径：怪物与己方微光者攻力归 0 都化为雕塑。"""
    state = GameState()
    # DM裁定 2026-09-10：轮回者攻次=当前速度、攻力=当前法力，写 attack_count/attack_power
    # 面板无效；不给当前速度/法力的话玩家自己就是 0×0，会先被雕塑、污染计数断言。
    state.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                          speed_limit=1, current_speed=1, mana_limit=1, current_mana=1)
    # DM裁定 2026-09-10：雕塑触发条件为攻次与攻力**都**为0（原「攻力归0即可」废止）
    m = _monster("石像鬼", hp=100, atk=0, power=0)
    friend = Entity(name="岩行者", entity_type="朋友", blood_limit=40, current_hp=40,
                    attack_count=0, attack_power=0, is_deployed=True)
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
    """边界：攻次与攻力**都**归 0 才触发（DM裁定 2026-09-10）；仍留 3×1 的微光者不触发。"""
    state = GameState()
    # DM裁定 2026-09-10：轮回者攻次=当前速度、攻力=当前法力，写 attack_count/attack_power
    # 面板无效；不给当前速度/法力的话玩家自己就是 0×0，会先被雕塑、污染计数断言。
    state.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                          speed_limit=1, current_speed=1, mana_limit=1, current_mana=1)
    emp = Entity(name="打手", entity_type="员工", blood_limit=48, current_hp=48,
                 attack_count=0, attack_power=0, is_deployed=True)
    temp = Entity(name="路人", entity_type="临时朋友", blood_limit=20, current_hp=20,
                  attack_count=0, attack_power=0)
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


def test_sculpture_includes_chizu_and_reincarnator():
    """DM裁定 2026-09-09：赤族攻力归 0 雕塑；**双方轮回者 0×0 同样雕塑**。

    旧口径下这条断言是「只 1 座雕塑、双方轮回者存活」；裁定后 0×0 的轮回者
    与怪物同理，攻次/攻力归 0 即失去攻击手段 → 化为雕塑。
    """
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
    assert sum(1 for p in paths if p["type"] == "sculpture") == 3
    assert chizu.is_sculptured and not chizu.is_alive
    assert player.is_sculptured and not player.is_alive
    assert foe.is_sculptured and not foe.is_alive
    names = {c.name for c in state.consumables if c.kind == "sculpture"}
    assert names == {"赤仆雕塑", "贾凡雕塑", "敌对轮回者雕塑"}


def test_sculpture_now_includes_zero_attack_reincarnator():
    """DM裁定 2026-09-09：只剩轮回者时，攻次/攻力 0 同样产生雕塑（旧口径为不产生）。"""
    state = GameState()
    player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                    attack_count=0, attack_power=0)
    state.player = player
    foe = Entity(name="敌对轮回者", entity_type="轮回者", blood_limit=70, current_hp=70,
                 attack_count=0, attack_power=0)
    state.enemies.append(foe)
    combat = CombatEngine(state, DiceEngine())
    paths = combat.settle_victory_paths()
    assert sum(1 for p in paths if p["type"] == "sculpture") == 2
    assert player.is_sculptured and foe.is_sculptured
    assert {c.name for c in state.consumables if c.kind == "sculpture"} == {
        "贾凡雕塑", "敌对轮回者雕塑"}
