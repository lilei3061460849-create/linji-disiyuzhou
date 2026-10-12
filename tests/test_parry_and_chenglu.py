"""遗物【承露盏】与属性上限护栏；附两条仍然成立的招架边界。

2026-10-11 清理：原本这里还有 11 条按「回合姿态式招架」写的测试（declare_parry / 每次受伤减 10% 当前生命 / 可挡次数=当前生命）。
该口径已被引擎废弃——`declare_parry` 现在直接返回错误，招架改为在
`resolve_attack` 的对应 hit 上逐击提交 `parry=true`（真刀免疫并返还1速度、
空刀不返还）。逐击招架的覆盖在 `tests/test_blade_combat_rework.py`，
本文件不再重复。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, Relic


def _arena(mana=10, mana_limit=10, bl=100, hp=100, relics=()):
    state = GameState()
    state.rng_seed = 1
    dice = DiceEngine(seed=1)
    player = Entity(name="P", entity_type="轮回者",
                    blood_limit=bl, current_hp=hp, mana_limit=mana_limit, current_mana=mana,
                    speed_limit=5, current_speed=5, attack_count=1, attack_power=0)
    enemy = Entity(name="E", entity_type="怪物",
                   blood_limit=50, current_hp=50, mana_limit=0, current_mana=0,
                   speed_limit=4, current_speed=4, attack_count=1, attack_power=10)
    state.player = player
    state.enemies = [enemy]
    state.relics = [Relic(name=n, effect="") for n in relics]
    combat = CombatEngine(state, dice)
    return state, combat, player, enemy


# ==================== 1. 招架：减伤口径（2026-09-28 新规则） ====================

def test_parry_does_nothing_without_declaration():
    """对照：没有声明招架时不减伤。"""
    _, combat, player, enemy = _arena(bl=100, hp=100)
    combat._apply_hostile_damage(player, 25, source=enemy)
    assert player.current_hp == 75, "未招架应全额承伤"


def test_parry_does_not_reduce_cost_damage():
    """边界：【代价】不受招架减免。"""
    _, combat, player, _ = _arena(bl=100, hp=100)
    player.parrying_this_round = True
    player.parry_uses_remaining_this_round = 100
    combat.pay_numeric_cost(player, "流血", 8, cost_context={
        "timing": "player_action", "source": "透支", "source_type": "daowen",
        "actor": player, "target": player, "mechanic": "cost",
        "subtype": "bleed", "amount": 8, "tags": {"active_payment"}})
    assert player.current_hp == 92, "代价须全额支付，不被招架减免"


# ==================== 2. 招架：声明（无锁） ====================

def _combat_engine(suffix):
    e = GameEngine(db_path=f"/tmp/test_parry_{suffix}.db", rng_seed=1)
    e.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    from tests.setup_support import finish_initial_daowen, begin_battle, begin_round
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = e.execute_action("setup_choose_region", {"region": "罪孽都市"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.energy = 0
    begin_battle(e)
    begin_round(e)
    return e


# ==================== 3. 承露盏（旧测试保持，仅数值微调）====================

def test_chenglu_accumulates_and_grants_mana():
    _, combat, player, enemy = _arena(mana=0, mana_limit=10, relics=("承露盏",))
    combat._apply_hostile_damage(player, 5, source=enemy)
    assert player.current_mana == 0
    combat._apply_hostile_damage(player, 5, source=enemy)
    assert player.current_mana == 1, "累计失血10 → +1法力"


def test_chenglu_no_relic_no_mana():
    _, combat, player, enemy = _arena(mana=0, mana_limit=10)
    combat._apply_hostile_damage(player, 30, source=enemy)
    assert player.current_mana == 0


def test_chenglu_resets_between_battles():
    _, combat, player, enemy = _arena(mana=0, mana_limit=10, relics=("承露盏",))
    combat._apply_hostile_damage(player, 9, source=enemy)
    assert player.hp_lost_this_battle == 9 and player.current_mana == 0
    combat.reset_monster_activation()
    assert player.hp_lost_this_battle == 0 and player.chenglu_paid == 0
    combat._apply_hostile_damage(player, 9, source=enemy)
    assert player.current_mana == 0


# ==================== 4. 全局上限 ====================

def test_all_attributes_capped_at_their_limits():
    _, combat, player, _ = _arena(mana=10, mana_limit=10)
    player.current_mana = 999
    player.current_speed = 999
    player.current_hp = 999
    combat.clamp_immortal_body(player)
    assert player.current_mana == player.mana_limit
    assert player.current_speed == player.speed_limit
    assert player.current_hp == player.blood_limit


def test_cap_does_not_block_refilling_a_spent_pool():
    _, combat, player, _ = _arena(mana=10, mana_limit=10)
    player.spend_mana(6)
    player.current_mana += 3
    combat.clamp_immortal_body(player)
    assert player.current_mana == 7


# ==================== 5. 招架 × 承露盏 ====================

def test_chenglu_feeds_the_touzhi_regeneration_loop():
    _, combat, player, _ = _arena(mana=0, mana_limit=20, bl=200, hp=200,
                                  relics=("承露盏",))
    combat.pay_numeric_cost(player, "流血", 12, cost_context={
        "timing": "player_action", "source": "透支", "source_type": "daowen",
        "actor": player, "target": player, "mechanic": "cost",
        "subtype": "bleed", "amount": 12, "tags": {"active_payment"}})
    assert player.current_mana == 1, "透支流血12 → 承露盏返1法力"
    player.current_mana += 3
    combat.clamp_immortal_body(player)
    assert player.current_mana == 4
    assert player.current_hp == 188
