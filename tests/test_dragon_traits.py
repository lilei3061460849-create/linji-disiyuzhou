"""
pytest 风格测试 - 里程碑：真龙之心（龙心谷终音法器，龙性资源 + 8种龙族禀赋）

原文：
"每消耗12X龙性，获得X种不同龙族禀赋（6X衰老=2X枯竭=X萎缩=12X龙性）"。
8种禀赋：龙族血脉/龙威/龙族利爪/龙息/震岳龙躯/吞骸龙胃/断尾求生/烬翼。

覆盖范围：pay_for_dragon_nature/unlock_dragon_trait 的门槛与汇率校验 + 8种禀赋各自的机制效果。
运行方式：
    python -m pytest tests/test_dragon_traits.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance


def _new_engine(region="龙心谷", name="老张", speed=8, mana=7, dbsuffix="a"):
    engine = GameEngine(db_path=f"data/test_dragon_traits_{dbsuffix}.db", rng_seed=1,
                         sealed_candidate_path=f"data/test_dragon_traits_{dbsuffix}_sealed.json")
    blood = 25 - speed - mana
    engine.execute_action("setup_attributes",
                           {"blood_points": blood, "speed_points": speed, "mana_points": mana, "name": name})
    engine.execute_action("setup_choose_daowen", {"daowen": "杀伐"})
    engine.execute_action("setup_choose_region", {"region": region})
    engine.state.player.dao_wen["杀伐"] = DaoWenInstance(
        DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    return engine


def _with_heart(engine, nature=100):
    engine.state.artifacts_owned.append("真龙之心")
    engine.state.dragon_nature = nature
    return engine


def _resolve_full_attack(engine, attacker_name, target, n_hits=1):
    r = engine.execute_action("attack", {"attacker": attacker_name, "target_selections": [0] * n_hits})
    for _ in r["result"]["hits"]:
        engine.execute_action("dodge_decision", {"attacker": attacker_name, "target": target.name, "dodge": False})


# ========================================================================
# 龙性经济：pay_for_dragon_nature / unlock_dragon_trait
# ========================================================================

def test_pay_for_dragon_nature_without_artifact_rejected():
    """错误输入：没有真龙之心时无法换取龙性"""
    engine = _new_engine(dbsuffix="gate1")
    r = engine.execute_action("pay_for_dragon_nature", {"cost_type": "衰老", "x": 1})
    assert r["success"] is False


def test_pay_for_dragon_nature_exchange_rates():
    """正常路径：6衰老=2枯竭=1萎缩=12龙性 的汇率关系（即1衰老=2龙性，1枯竭=6龙性，1萎缩=12龙性）"""
    engine = _new_engine(dbsuffix="rate")
    _with_heart(engine, nature=0)
    player = engine.state.player
    bl_before, ml_before, sl_before = player.blood_limit, player.mana_limit, player.speed_limit

    r1 = engine.execute_action("pay_for_dragon_nature", {"cost_type": "衰老", "x": 6})
    assert r1["result"]["dragon_nature_gained"] == 12
    assert player.blood_limit == bl_before - 6

    r2 = engine.execute_action("pay_for_dragon_nature", {"cost_type": "枯竭", "x": 2})
    assert r2["result"]["dragon_nature_gained"] == 12
    assert player.mana_limit == ml_before - 2

    r3 = engine.execute_action("pay_for_dragon_nature", {"cost_type": "萎缩", "x": 1})
    assert r3["result"]["dragon_nature_gained"] == 12
    assert player.speed_limit == sl_before - 1

    assert engine.state.dragon_nature == 36


def test_pay_for_dragon_nature_error_invalid_type_or_x():
    """错误输入：非法cost_type或x<1"""
    engine = _new_engine(dbsuffix="pay_err")
    _with_heart(engine, nature=0)
    assert engine.execute_action("pay_for_dragon_nature", {"cost_type": "未知", "x": 1})["success"] is False
    assert engine.execute_action("pay_for_dragon_nature", {"cost_type": "衰老", "x": 0})["success"] is False


def test_unlock_dragon_trait_requires_12_nature_and_rejects_duplicates():
    """边界+错误输入：解锁1种禀赋消耗12龙性；龙性不足/未知禀赋/重复解锁均应拒绝"""
    engine = _new_engine(dbsuffix="unlock")
    _with_heart(engine, nature=11)
    r_low = engine.execute_action("unlock_dragon_trait", {"trait": "龙威"})
    assert r_low["success"] is False

    engine.state.dragon_nature = 12
    r_ok = engine.execute_action("unlock_dragon_trait", {"trait": "龙威"})
    assert r_ok["success"] is True
    assert engine.state.dragon_nature == 0
    assert "龙威" in engine.state.dragon_traits

    r_dup = engine.execute_action("unlock_dragon_trait", {"trait": "龙威"})
    assert r_dup["success"] is False

    r_unknown = engine.execute_action("unlock_dragon_trait", {"trait": "不存在"})
    assert r_unknown["success"] is False


# ========================================================================
# 1. 龙族血脉
# ========================================================================

def test_dragon_bloodline_instakills_monsters_and_doubles_nonmonster_damage():
    """正常路径：对怪物造成伤害后直接命零；对非怪物造成伤害翻倍"""
    engine = _new_engine(dbsuffix="bloodline")
    player = engine.state.player
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "龙族血脉"})
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    monster = Entity(name="怪物", entity_type="怪物", blood_limit=999999, current_hp=999999)
    engine.state.enemies.append(monster)
    engine.execute_action("round_start", {})
    _resolve_full_attack(engine, player.name, monster)
    assert monster.is_alive is False, "怪物应被直接命零"

    non_monster = Entity(name="敌方轮回者", entity_type="轮回者", blood_limit=100, current_hp=100)
    dmg = engine.combat.resolve_attack(player, non_monster, is_must_hit=True)
    assert dmg["damage_dealt"] == player.attack_power * 2


# ========================================================================
# 2. 龙威（无代码变更的既有行为断言，避免今后误改）
# ========================================================================

def test_dragon_might_monsters_always_target_player():
    """回归性断言：怪物阶段固定只攻击玩家本人，因此龙威("敌方必须优先选择自身为目标")天然恒定满足"""
    engine = _new_engine(dbsuffix="might")
    player = engine.state.player
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "龙威"})
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    monster = Entity(name="怪物", entity_type="怪物", blood_limit=30, current_hp=30, attack_power=1, attack_count=1)
    engine.state.enemies.append(monster)
    engine.state.friends.append(Entity(name="队友", entity_type="朋友", blood_limit=30, current_hp=30, is_deployed=True))
    engine.execute_action("round_start", {})
    results = engine.combat.run_monster_phase()
    targets = {r["target"] for r in results if "target" in r}
    assert targets == {player.name}


# ========================================================================
# 3. 龙族利爪
# ========================================================================

def test_dragon_claw_initial_stats_and_growth_per_action():
    """正常路径：初始3次攻击1点攻击力；每完成一次行动后攻击次数+1、攻击力+2"""
    engine = _new_engine(dbsuffix="claw")
    player = engine.state.player
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "龙族利爪"})
    assert player.attack_count == 3
    assert player.attack_power == 1

    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    engine.state.enemies.append(Entity(name="怪物", entity_type="怪物", blood_limit=999, current_hp=999))
    engine.execute_action("round_start", {})
    _resolve_full_attack(engine, player.name, engine.state.enemies[0], n_hits=3)
    assert player.attack_count == 4
    assert player.attack_power == 3


# ========================================================================
# 4. 龙息
# ========================================================================

def test_dragon_breath_deals_10_times_round_damage_before_each_monster_acts():
    """正常路径：所有敌方目标行动前，受到10×当前回合数的必中伤害"""
    engine = _new_engine(dbsuffix="breath")
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "龙息"})
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    monster = Entity(name="怪物", entity_type="怪物", blood_limit=100, current_hp=100, attack_power=1, attack_count=1)
    engine.state.enemies.append(monster)
    engine.execute_action("round_start", {})  # current_round -> 1
    results = engine.combat.run_monster_phase()
    breath_entries = [r for r in results if r.get("dragon_breath")]
    assert breath_entries and breath_entries[0]["dragon_breath"] == 10
    assert monster.current_hp == 90


def test_dragon_breath_can_kill_monster_before_it_acts():
    """边界：龙息伤害足以命零怪物时，该怪物本回合跳过后续行动"""
    engine = _new_engine(dbsuffix="breath_kill")
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "龙息"})
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    weak = Entity(name="弱怪", entity_type="怪物", blood_limit=5, current_hp=5, attack_power=1, attack_count=1)
    engine.state.enemies.append(weak)
    engine.execute_action("round_start", {})
    results = engine.combat.run_monster_phase()
    assert weak.is_alive is False
    assert not any(r.get("attacker") == "弱怪" for r in results), "已被龙息命零的怪物不应再发动攻击"


# ========================================================================
# 5. 震岳龙躯
# ========================================================================

def test_dragon_body_caps_damage_at_15_and_ticks_down_each_round():
    """正常路径：激活后自身受到超出15的伤害无效，持续X回合后失效"""
    engine = _new_engine(dbsuffix="body")
    player = engine.state.player
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "震岳龙躯"})
    r = engine.execute_action("activate_dragon_body", {"x": 2})
    assert r["success"] is True
    assert engine.state.dragon_body_shield_rounds == 2

    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    heavy = Entity(name="重击怪", entity_type="怪物", blood_limit=50, current_hp=50, attack_power=999, attack_count=1)
    engine.state.enemies.append(heavy)
    engine.execute_action("round_start", {})
    hp_before = player.current_hp
    result = engine.combat.resolve_attack(heavy, player, is_must_hit=True)
    assert result["damage_dealt"] == 15
    assert player.current_hp == hp_before - 15

    engine.execute_action("round_end", {})
    assert engine.state.dragon_body_shield_rounds == 1
    engine.execute_action("round_start", {})
    engine.execute_action("round_end", {})
    assert engine.state.dragon_body_shield_rounds == 0

    engine.execute_action("round_start", {})
    hp_before2 = player.current_hp
    result2 = engine.combat.resolve_attack(heavy, player, is_must_hit=True)
    assert result2["damage_dealt"] == 999, "护体失效后应恢复正常伤害"


def test_dragon_body_activation_boundary_insufficient_nature():
    """边界：龙性不足6X时无法激活"""
    engine = _new_engine(dbsuffix="body_err")
    _with_heart(engine, nature=5)
    engine.execute_action("unlock_dragon_trait", {"trait": "震岳龙躯"})
    r = engine.execute_action("activate_dragon_body", {"x": 1})
    assert r["success"] is False


# ========================================================================
# 6. 吞骸龙胃
# ========================================================================

def test_dragon_stomach_devour_heals_and_boosts_dragon_heart():
    """正常路径：吞噬已命零的怪物，回复12，并可指定一枚龙心使其耐久+6"""
    from engine.models import Consumable
    engine = _new_engine(dbsuffix="devour")
    player = engine.state.player
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "吞骸龙胃"})
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    dead = Entity(name="尸体", entity_type="怪物", blood_limit=30, current_hp=0, is_alive=False)
    engine.state.enemies.append(dead)
    engine.state.consumables.append(
        Consumable(name="衰老龙心", effect="", current_uses=4, max_uses=4, kind="dragon_heart",
                   dragon_heart_type="衰老"))
    player.current_hp = 10
    r = engine.execute_action("devour_monster", {"monster": "尸体", "dragon_heart": "衰老龙心"})
    assert r["success"] is True
    assert player.current_hp == 22
    heart = next(c for c in engine.state.consumables if c.name == "衰老龙心")
    assert heart.current_uses == 10
    assert heart.max_uses == 10


def test_dragon_stomach_error_target_still_alive():
    """错误输入：目标怪物尚未命零时拒绝"""
    engine = _new_engine(dbsuffix="devour_err")
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "吞骸龙胃"})
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    alive = Entity(name="活怪", entity_type="怪物", blood_limit=30, current_hp=30)
    engine.state.enemies.append(alive)
    r = engine.execute_action("devour_monster", {"monster": "活怪", "dragon_heart": ""})
    assert r["success"] is False


# ========================================================================
# 7. 断尾求生
# ========================================================================

def test_tail_sacrifice_removes_declared_trait_to_negate_lethal_damage():
    """正常路径：预声明后，即将命零的伤害改为移除该禀赋抵消"""
    engine = _new_engine(dbsuffix="tail")
    player = engine.state.player
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "断尾求生"})
    engine.execute_action("unlock_dragon_trait", {"trait": "龙威"})
    engine.execute_action("declare_tail_sacrifice", {"trait": "龙威"})
    engine.execute_action("battle_start", {})
    player.current_hp = 5
    result = engine.combat._apply_hostile_damage(player, 9999, "普通")
    assert result.get("tail_sacrificed") == "龙威"
    assert player.current_hp == 5
    assert player.is_alive is True
    assert "龙威" not in engine.state.dragon_traits
    assert engine.state.dragon_tail_sacrifice_declared == ""


def test_tail_sacrifice_error_declare_self_or_unowned_trait():
    """错误输入：不能声明牺牲自身(断尾求生)，也不能声明未持有的其他禀赋"""
    engine = _new_engine(dbsuffix="tail_err")
    _with_heart(engine)
    engine.execute_action("unlock_dragon_trait", {"trait": "断尾求生"})
    r_self = engine.execute_action("declare_tail_sacrifice", {"trait": "断尾求生"})
    assert r_self["success"] is False
    r_unowned = engine.execute_action("declare_tail_sacrifice", {"trait": "龙威"})
    assert r_unowned["success"] is False


# ========================================================================
# 8. 烬翼
# ========================================================================

def test_dragon_wings_costs_dragon_nature_for_flight():
    """正常路径：[回始]消耗3X点龙性，获得飞行X"""
    engine = _new_engine(dbsuffix="wings")
    player = engine.state.player
    _with_heart(engine, nature=30)
    engine.execute_action("unlock_dragon_trait", {"trait": "烬翼"})
    nature_before = engine.state.dragon_nature
    r = engine.execute_action("use_dragon_wings", {"x": 3})
    assert r["success"] is True
    assert engine.state.dragon_nature == nature_before - 9
    assert player.get_status_value("飞行") == 3


def test_dragon_wings_boundary_insufficient_nature_rejected():
    """边界：龙性不足3X时拒绝"""
    engine = _new_engine(dbsuffix="wings_err")
    _with_heart(engine, nature=2)
    engine.execute_action("unlock_dragon_trait", {"trait": "烬翼"})
    r = engine.execute_action("use_dragon_wings", {"x": 1})
    assert r["success"] is False
