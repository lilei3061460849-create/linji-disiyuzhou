"""封印延迟回场；雕塑对任何非轮回者；轮回者开局攻面板 0×0。

封印X：支付异变X，使一个目标怪物延后X回合再入场；暂离不是死亡或永久离场。
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
    engine.state.combat_subphase = "await_round_start"
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

def test_seal_delays_one_monster_and_reenters_on_scheduled_round():
    """封印X=2支付异变2，只让一个目标暂离，并在R+X回始回场。"""
    engine = _engine("seal_happy")
    a = _monster("怪甲")
    b = _monster("怪乙")
    engine.state.enemies.extend([a, b])
    engine.execute_action("round_start", {})  # R1
    mana = engine.state.player.current_mana
    r = engine.execute_action("use_daowen", {"daowen_name": "封印", "x": 2, "target": "怪甲"})
    assert r["success"], r
    assert engine.state.player.current_mana == mana  # 封印是异变代价，不消耗法力
    assert engine.state.player.mutation_count == 2
    seal = next(e for e in r["execution"]["effects"] if e["type"] == "seal")
    assert seal["target"] == "怪甲"
    assert seal["delay_rounds"] == 2
    assert seal["return_round"] == 3
    assert a.is_alive and not a.is_departed and not a.removed_without_kill
    assert a not in engine.state.enemies
    assert engine.state.delayed_monster_reentries[0]["monster"] is a
    assert b in engine.state.enemies and b.is_alive
    assert not engine.state.battle_won(), "暂离怪物仍阻塞战斗胜利"

    # 结束R1、进入R2：尚未回场。
    engine.state.combat_subphase = "await_round_end"
    engine.execute_action("round_end", {})
    r2 = engine.execute_action("round_start", {})
    assert engine.state.current_round == 2
    assert a not in engine.state.enemies
    assert not any(e.get("type") == "seal_reentry" for e in r2["result"]["effects"])

    # 进入R3回始：原怪物回场，当回合白板标记由 spawned_round 提供。
    engine.state.combat_subphase = "await_round_end"
    engine.execute_action("round_end", {})
    r3 = engine.execute_action("round_start", {})
    assert engine.state.current_round == 3
    assert a in engine.state.enemies and a.is_alive
    assert engine.state.delayed_monster_reentries == []
    assert any(e.get("type") == "seal_reentry" and e["entity"] == "怪甲"
               for e in r3["result"]["effects"])


def test_seal_requires_a_monster_target():
    """封印不能把轮回者/员工当作怪物暂离目标。"""
    engine = _engine("seal_target_type")
    foe = Entity(name="敌对轮回者", entity_type="轮回者", blood_limit=70, current_hp=70,
                 attack_count=1, attack_power=1)
    engine.state.enemies.append(foe)
    engine.execute_action("round_start", {})
    r = engine.execute_action("use_daowen", {"daowen_name": "封印", "x": 1, "target": foe.name})
    assert not r["success"]
    assert "怪物" in r["error"]
    assert engine.state.player.mutation_count == 0
    assert foe.is_alive and foe in engine.state.enemies


def test_seal_delayed_monster_is_not_an_alt_victory_or_shardless_removal():
    """延迟中的怪物不写旧版封印离场字段；回场后命零走正常碎片路径。"""
    engine = _engine("seal_death")
    monster = _monster("回场怪", hp=80)
    engine.state.enemies.append(monster)
    engine.execute_action("round_start", {})
    r = engine.execute_action("use_daowen", {"daowen_name": "封印", "x": 1, "target": monster.name})
    assert r["success"], r
    assert engine.state.player.mutation_count == 1
    assert not engine.state.battle_won()
    assert engine.execute_action("battle_end", {})["success"] is False

    engine.state.combat_subphase = "await_round_end"
    engine.execute_action("round_end", {})
    engine.execute_action("round_start", {})
    monster = next(e for e in engine.state.enemies if e.name == "回场怪")
    monster.current_hp = 0
    monster.is_alive = False
    end = engine.execute_action("battle_end", {})
    assert end["success"], end
    assert end["result"]["removed_via_alt_path"] == []
    assert end["result"]["death_shard_rewards"]


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
