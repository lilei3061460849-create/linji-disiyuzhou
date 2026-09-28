"""【招架】与遗物【承露盏】（已按 2026-09-28 用户令同步招架新规则：10%当前生命减伤/次，次数=当前生命，无回合锁）。"""
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

def test_parry_subtracts_10pct_current_hp_from_each_hit():
    """正常路径：招架期间每次受击减去 floor(当前生命×10%)；命中次数消耗次数。
    注意是「每次」而非「本轮合计」。"""
    _, combat, player, enemy = _arena(bl=100, hp=100)
    player.parrying_this_round = True
    player.parry_uses_remaining_this_round = player.current_hp  # 100次
    # 当前HP=100，每击减 10 点
    combat._apply_hostile_damage(player, 25, source=enemy)
    assert player.current_hp == 85, f"25-10=15，实掉{100 - player.current_hp}"
    # 第二击时 HP=85 → floor(85*10%)=8 → 25-8=17 → HP=68
    combat._apply_hostile_damage(player, 25, source=enemy)
    assert player.current_hp == 68, "第二击按HP=85计算减8，实掉17"


def test_parry_does_nothing_without_declaration():
    """对照：没有声明招架时不减伤。"""
    _, combat, player, enemy = _arena(bl=100, hp=100)
    combat._apply_hostile_damage(player, 25, source=enemy)
    assert player.current_hp == 75, "未招架应全额承伤"


def test_parry_reduction_tracks_current_hp():
    """边界（核心张力）：减免按结算时的当前生命算，不是声明时快照；
    掉血后招架减伤同步变弱，越打越扛不住。"""
    _, combat, player, enemy = _arena(bl=100, hp=100)
    player.parrying_this_round = True
    player.parry_uses_remaining_this_round = player.current_hp
    combat._apply_hostile_damage(player, 20, source=enemy)
    # HP 100 → 减 10 → 实掉10 → HP=90
    assert player.current_hp == 90
    # HP=90 → 每击减 9
    combat._apply_hostile_damage(player, 20, source=enemy)
    assert player.current_hp == 79, "HP=90 → 减9 → 20-9=11 实掉"


def test_parry_uses_depletes_after_hp_hits():
    """次数上限：每回合可用次数=声明时的当前生命，用完后续受击不再减伤。"""
    _, combat, player, enemy = _arena(bl=100, hp=100)
    player.parrying_this_round = True
    player.parry_uses_remaining_this_round = 3
    # 低伤害击打（1点），减伤 floor(hp*10%)=10，所以前3击全部免伤（不掉血）
    for _ in range(3):
        hp_before = player.current_hp
        combat._apply_hostile_damage(player, 1, source=enemy)
        assert player.current_hp == hp_before, "1点伤害在招架10点减免下应完全免伤"
    assert player.parry_uses_remaining_this_round == 0
    hp_before = player.current_hp
    combat._apply_hostile_damage(player, 1, source=enemy)
    assert player.current_hp == hp_before - 1, "次数用尽后不再减伤"


def test_parry_floors_at_zero_never_heals():
    """边界：减免不会把伤害打成负数（低血时 10% 取整可能为0，不回血）。"""
    _, combat, player, enemy = _arena(bl=100, hp=100)
    player.parrying_this_round = True
    player.parry_uses_remaining_this_round = 100
    combat._apply_hostile_damage(player, 5, source=enemy)
    # HP=100 减10但伤害只有5 → 减到0
    assert player.current_hp == 100, "伤害归零，不得反向回血"


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


def test_parry_with_sub_10_hp_reduces_nothing():
    """边界：HP<10时 10% 取整=0，招架合法但不减伤，仍消耗次数。"""
    _, combat, player, enemy = _arena(bl=9, hp=9)
    player.parrying_this_round = True
    player.parry_uses_remaining_this_round = 9
    before_uses = player.parry_uses_remaining_this_round
    combat._apply_hostile_damage(player, 7, source=enemy)
    assert player.current_hp == 2, f"9血→10%=0减伤，应全额掉7；实掉{9 - player.current_hp}"
    assert player.parry_uses_remaining_this_round == before_uses - 1, "仍消耗1次次数"


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


def test_declare_parry_succeeds_and_sets_stance():
    """正常路径：声明招架成功，进入姿态，不消耗出手，可用次数=当前生命。"""
    e = _combat_engine("declare")
    p = e.state.player
    used_before = p.actions_used_this_round
    hp_before = p.current_hp
    r = e.execute_action("declare_parry", {})
    assert r["success"], r
    assert p.parrying_this_round is True
    assert p.parry_uses_remaining_this_round == hp_before
    assert p.actions_used_this_round == used_before, "招架不消耗出手"
    assert r["result"]["reduction_preview"] == hp_before // 10
    assert r["result"]["uses"] == hp_before


def test_cannot_parry_twice_in_same_round():
    """错误输入：同一回合重复声明被拒。"""
    e = _combat_engine("twice")
    assert e.execute_action("declare_parry", {})["success"]
    r2 = e.execute_action("declare_parry", {})
    assert not r2["success"] and "已处于招架" in r2["error"]


def test_parry_does_not_lock_next_round():
    """2026-09-28 新规则：不再有"上回合招架→本回合禁用"锁；下回合可以重新声明。"""
    e = _combat_engine("nolock")
    p = e.state.player
    assert e.execute_action("declare_parry", {})["success"]
    e.state.combat_subphase = "await_round_end"
    e.execute_action("round_end", {})
    e.execute_action("round_start", {})
    assert p.parrying_this_round is False, "回始须清空姿态"
    assert p.parry_uses_remaining_this_round == 0, "回始须清空次数"
    # 旧字段保留但不再起作用
    assert p.parry_locked_this_round is False
    assert e.execute_action("declare_parry", {})["success"], "下回合仍可声明招架"


def test_parry_is_listed_in_available_actions():
    """招架出现在可用行动表里。"""
    e = _combat_engine("listed")
    actions = e.get_available_actions()["actions"]
    entry = next((a for a in actions if a["action_type"] == "declare_parry"), None)
    assert entry is not None, "招架必须出现在可用行动表"


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

def test_parry_and_chenglu_compose_on_the_same_hit():
    """同一次受击上，招架先减伤（10%当前HP），剩下的失血再喂承露盏。"""
    _, combat, player, enemy = _arena(mana=4, mana_limit=20, bl=200, hp=200,
                                      relics=("承露盏",))
    player.parrying_this_round = True
    player.parry_uses_remaining_this_round = 200
    combat._apply_hostile_damage(player, 30, source=enemy)
    # 200HP → 招架减 floor(200*10%)=20 → 实际失血 10 → 10//10=1法力
    assert player.current_hp == 190, f"30-20=10，实掉{200 - player.current_hp}"
    assert player.current_mana == 5, "失血10 → +1法力（4→5）"


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
