"""开局发现 / 新残韵 / 钱袋免疫癌变 / 净化 / 救赎。

每项覆盖正常、边界、非法三类。
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.gamedata import SHAFA_LOOP_DAOWEN, ORIGINAL_MONSTER_DAOWEN
from engine.models import DaoWen, DaoWenInstance, Entity, Relic
from tests.setup_support import finish_initial_daowen


def _engine(suffix, seed=11):
    os.makedirs("/tmp/linji_tests", exist_ok=True)
    return GameEngine(db_path=f"/tmp/linji_tests/redemp_{suffix}.db", rng_seed=seed)


def _give(entity, name, cost_type="消耗"):
    entity.dao_wen[name] = DaoWenInstance(DaoWen(
        name=name, formula="", cost_type=cost_type, cost_formula="X", effect_formula=""))


def _ready_combat(engine, region="罪孽都市"):
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = engine.execute_action("setup_choose_region", {"region": region})
    optional = {"折速法印", "三相残韵盘", "回锋刀", "血契"}
    choice = next((n for n in setup["result"]["relic_choices"] if n not in optional),
                  setup["result"]["relic_choices"][0])
    engine.execute_action("choose_discovered_relic", {"relic_name": choice})
    engine.state.energy = 0
    relic_choices = {r.name: {"use": False} for r in engine.state.relics
                     if r.name in ("折速法印", "三相残韵盘")}
    engine.execute_action("battle_start", {"relic_choices": relic_choices})
    round_choices = {}
    if any(r.name == "回锋刀" for r in engine.state.relics):
        round_choices["回锋刀"] = {"enemy_index": 0}
    if any(r.name == "血契" for r in engine.state.relics):
        round_choices["血契"] = {"use": False}
    engine.execute_action("round_start", {"relic_choices": round_choices})
    return engine


# ---------- 开局发现 ----------

def test_initial_daowen_discovery_normal():
    engine = _engine("init_ok")
    r = engine.execute_action("setup_attributes", {
        "name": "试者", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    assert r["success"]
    choices = r["result"]["daowen_choices"]
    assert len(choices) == 3 and len(set(choices)) == 3
    assert set(choices) <= set(SHAFA_LOOP_DAOWEN)
    assert engine.state.player.dao_wen == {}
    picked = engine.execute_action("setup_choose_initial_daowen", {"daowen_name": choices[1]})
    assert picked["success"]
    assert list(engine.state.player.dao_wen) == [choices[1]]
    assert engine.state.pending_initial_daowen_choices == []


def test_initial_daowen_discovery_boundary_three_unique_from_loop():
    engine = _engine("init_bound", seed=99)
    engine.execute_action("setup_attributes", {
        "name": "试者", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    choices = engine.state.pending_initial_daowen_choices
    assert all(name in SHAFA_LOOP_DAOWEN for name in choices)
    rolls = [h for h in engine.dice.get_history()
             if str(h.get("pool_name", "")).startswith("initial_daowen_discovery_")]
    assert len(rolls) == 3


def test_initial_daowen_discovery_rejects_illegal():
    engine = _engine("init_bad")
    engine.execute_action("setup_attributes", {
        "name": "试者", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    choices = list(engine.state.pending_initial_daowen_choices)
    bad = engine.execute_action("setup_choose_initial_daowen", {"daowen_name": "净化"})
    assert not bad["success"]
    assert engine.state.player.dao_wen == {}
    assert engine.state.pending_initial_daowen_choices == choices
    resonance = engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    assert not resonance["success"]


# ---------- 残韵：永久改写 + 施法者同时获得 ----------

def test_resonance_permanently_converts_and_grants():
    engine = _ready_combat(_engine("res_ok"))
    engine.state.resonance["反转"] = 1
    _give(engine.state.player, "杀伐")
    monster = Entity(name="狂怪", entity_type="怪物", blood_limit=80, current_hp=80,
                     attack_count=2, attack_power=4)
    _give(monster, "狂暴")
    monster._had_monster_daowen = True
    engine.state.enemies[:] = [monster]
    r = engine.execute_action("use_resonance", {
        "source_daowen": "狂暴", "resonance_type": "反转", "target_ref": "enemy:0",
    })
    assert r["success"]
    assert "狂暴" not in monster.dao_wen and "自残" in monster.dao_wen
    assert "自残" in engine.state.player.dao_wen
    assert "自残" not in ORIGINAL_MONSTER_DAOWEN


def test_resonance_does_not_duplicate_same_name():
    engine = _ready_combat(_engine("res_dup"))
    engine.state.resonance["反转"] = 1
    _give(engine.state.player, "杀伐")
    _give(engine.state.player, "再生")
    r = engine.execute_action("use_resonance", {
        "source_daowen": "杀伐", "resonance_type": "反转",
    })
    assert r["success"]
    assert list(engine.state.player.dao_wen).count("再生") == 1
    assert "杀伐" not in engine.state.player.dao_wen


def test_resonance_refuses_missing_stock_and_original_grant():
    engine = _ready_combat(_engine("res_bad"))
    _give(engine.state.player, "杀伐")
    engine.state.resonance["反转"] = 0
    r = engine.execute_action("use_resonance", {
        "source_daowen": "杀伐", "resonance_type": "反转",
    })
    assert not r["success"]
    assert "杀伐" in engine.state.player.dao_wen
    granted = engine._grant_transformed_daowen(engine.state.player, "疯狂")
    assert granted is False
    assert "疯狂" not in engine.state.player.dao_wen


# ---------- 钱袋免疫癌变 ----------

def test_moneybag_blocks_player_cancer():
    engine = _ready_combat(_engine("bag_ok"))
    engine.state.relics.append(Relic(name="钱袋", effect=""))
    player = engine.state.player
    player.total_healed = engine.combat.cancer_threshold_of(player)
    assert engine.combat.check_cancer(player) is None
    assert player.is_alive


def test_moneybag_threshold_exactly_two_times_blood_limit():
    engine = _ready_combat(_engine("bag_th"))
    player = engine.state.player
    assert engine.combat.cancer_threshold_of(player) == player.blood_limit * 2
    player.total_healed = player.blood_limit * 2 - 1
    assert engine.combat.check_cancer(player) is None
    player.total_healed = player.blood_limit * 2
    hit = engine.combat.check_cancer(player)
    assert hit is not None and not player.is_alive


def test_moneybag_does_not_protect_friends():
    engine = _ready_combat(_engine("bag_ally"))
    engine.state.relics.append(Relic(name="钱袋", effect=""))
    friend = Entity(name="同伴", entity_type="朋友", blood_limit=30, current_hp=30)
    engine.state.friends.append(friend)
    friend.total_healed = engine.combat.cancer_threshold_of(friend)
    hit = engine.combat.check_cancer(friend)
    assert hit is not None
    assert friend.is_proliferated


# ---------- 净化 ----------

def test_jinghua_reduces_mutation():
    engine = _ready_combat(_engine("jh_ok"))
    player = engine.state.player
    _give(player, "净化")
    player.current_mana = 50
    monster = Entity(name="疫怪", entity_type="怪物", blood_limit=80, current_hp=80,
                     attack_count=2, attack_power=4)
    monster.mutation_count = 12
    _give(monster, "强化")
    engine.state.enemies[:] = [monster]
    r = engine.execute_action("use_daowen", {
        "daowen_name": "净化", "x": 5, "target_ref": "enemy:0",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert r["success"]
    assert monster.mutation_count == 7
    assert r["calculation"]["cost"] == 25


def test_jinghua_can_go_negative_and_trigger_redemption():
    engine = _ready_combat(_engine("jh_neg"))
    player = engine.state.player
    _give(player, "净化")
    player.current_mana = 200
    player.speed_limit = 30
    monster = Entity(name="净怪", entity_type="怪物", blood_limit=80, current_hp=80,
                     attack_count=2, attack_power=4)
    monster.mutation_count = 0
    _give(monster, "强化")
    engine.state.enemies[:] = [monster]
    r = engine.execute_action("use_daowen", {
        "daowen_name": "净化", "x": 30, "target_ref": "enemy:0",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert r["success"]
    assert monster.mutation_count == -30
    assert engine.state.pending_redemption
    assert not monster.is_alive


def test_jinghua_rejects_non_positive_x():
    engine = _ready_combat(_engine("jh_bad"))
    _give(engine.state.player, "净化")
    engine.state.player.current_mana = 20
    monster = Entity(name="靶", entity_type="怪物", blood_limit=40, current_hp=40)
    engine.state.enemies[:] = [monster]
    r = engine.execute_action("use_daowen", {
        "daowen_name": "净化", "x": 0, "target_ref": "enemy:0",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert not r["success"]
    assert monster.mutation_count == 0


# ---------- 救赎 ----------

def test_redemption_accept_creates_halved_friend():
    engine = _ready_combat(_engine("rd_ok"))
    monster = Entity(name="悔怪", entity_type="怪物", blood_limit=81, current_hp=81,
                     attack_count=3, attack_power=5)
    monster._had_monster_daowen = True
    engine.state.enemies[:] = [monster]
    engine.combat.check_redemption(monster)
    assert engine.state.pending_redemption
    blocked = engine.execute_action("round_end", {})
    assert not blocked["success"] and "救赎" in blocked["error"]
    r = engine.execute_action("resolve_redemption", {"option": 1, "name": "微光阿清"})
    assert r["success"]
    friend = next(f for f in engine.state.friends if f.name == "微光阿清")
    assert friend.blood_limit == math.ceil(81 / 2)
    assert friend.attack_count == math.ceil(3 / 2)
    assert friend.attack_power == math.ceil(5 / 2)
    assert friend.dao_wen == {}
    assert not engine.state.pending_redemption


def test_redemption_ignore_and_no_false_positive():
    engine = _ready_combat(_engine("rd_ig"))
    exclusive = Entity(name="塔中影", entity_type="怪物", blood_limit=60, current_hp=60,
                       attack_count=2, attack_power=4)
    _give(exclusive, "杀伐")
    engine.state.enemies[:] = [exclusive]
    assert engine.combat.check_redemption(exclusive) is None
    exclusive.mutation_count = -30
    hit = engine.combat.check_redemption(exclusive)
    assert hit is not None
    r = engine.execute_action("resolve_redemption", {"option": "无视"})
    assert r["success"]
    assert engine.state.friends == []
    assert not engine.state.pending_redemption


def test_redemption_rejects_bad_accept():
    engine = _ready_combat(_engine("rd_bad"))
    monster = Entity(name="悔怪", entity_type="怪物", blood_limit=40, current_hp=40,
                     attack_count=2, attack_power=2)
    monster._had_monster_daowen = True
    engine.state.enemies[:] = [monster]
    engine.combat.check_redemption(monster)
    missing = engine.execute_action("resolve_redemption", {"option": 1})
    assert not missing["success"]
    assert engine.state.pending_redemption
    dup = engine.execute_action("resolve_redemption", {"option": 1, "name": "贾凡"})
    assert not dup["success"]
    bad = engine.execute_action("resolve_redemption", {"option": 3})
    assert not bad["success"]
    assert engine.state.pending_redemption
