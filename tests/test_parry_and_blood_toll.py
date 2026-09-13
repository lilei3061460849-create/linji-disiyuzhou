"""【招架】与遗物【血偿契】，以及「所有属性不得超过其上限」的全局裁定。

三条均为 2026-09-13 用户裁定：

1. 【招架】—— 面对敌方攻击的第三种选项（原先只有闪避/不闪避）。
   本轮你每次受到的伤害减去等同你法力的数值；下回合不能使用招架。
   声明式、回合级：一次声明覆盖本轮全部受击，不消耗出手也不消耗速度，
   代价只记在下一个回合（裸奔一轮）。

2. 遗物【血偿契】—— 每累计失去10点生命，获得1点法力。本场累计、余数滚存、
   [战始]归零。设计意图是与一切卖血套路搭配（【透支】流血4X、【血影】流血10、
   法术【血炼周天】= 再生⇄透支 自持循环）。

3. 全局上限 —— 所有属性一律不得超过其上限（当前生命≤[血限]、当前法力≤[法限]、
   当前速度≤[速限]）。此前只有遗物【不朽之躯】才有此效果，现已成为通用规则。

每条覆盖 正常路径 / 边界 / 错误输入。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, Relic


def _arena(mana=10, mana_limit=10, bl=100, hp=100, relics=()):
    """轻量战场：只要 player + 一个怪，足够驱动伤害管线与失血总账。"""
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=bl, current_hp=hp,
                    mana_limit=mana_limit, current_mana=mana,
                    speed_limit=10, current_speed=10)
    enemy = Entity("M", "怪物", blood_limit=100, current_hp=100,
                   attack_count=1, attack_power=20)
    state.player = player
    state.enemies = [enemy]
    state.relics = [Relic(n, "") for n in relics]
    return state, CombatEngine(state, DiceEngine()), player, enemy


# ==================== 1. 招架：减伤 ====================

def test_parry_subtracts_current_mana_from_each_hit():
    """正常路径：招架期间每次受到的伤害各减去等同当前法力的数值。

    注意是「每次」而不是「本轮合计」——两次 15 点各减 10，而不是总共减 10。
    """
    _, combat, player, enemy = _arena(mana=10)
    player.parrying_this_round = True
    combat._apply_hostile_damage(player, 15, source=enemy)
    assert player.current_hp == 95, f"15-10=5，实掉{100 - player.current_hp}"
    combat._apply_hostile_damage(player, 15, source=enemy)
    assert player.current_hp == 90, "第二次受击同样各减10（每次，不是每轮）"


def test_parry_does_nothing_without_declaration():
    """对照：没有声明招架时，同样的法力不会产生任何减伤。"""
    _, combat, player, enemy = _arena(mana=10)
    combat._apply_hostile_damage(player, 15, source=enemy)
    assert player.current_hp == 85, "未招架应全额承伤"


def test_parry_reduction_tracks_mana_spent_this_round():
    """边界（本机制的核心张力）：减免按**结算时**的法力算，不是声明时的快照。

    法力同时就是攻力，本轮花掉的每一点都会同步削弱自己的招架——
    想硬扛就别出手，想出手就扛不住。
    """
    _, combat, player, enemy = _arena(mana=10)
    player.parrying_this_round = True
    combat._apply_hostile_damage(player, 12, source=enemy)
    assert player.current_hp == 98, "满池10 → 12-10=2"
    player.spend_mana(8)          # 出手花掉 8 点
    combat._apply_hostile_damage(player, 12, source=enemy)
    assert player.current_hp == 88, "只剩2法力 → 12-2=10"


def test_parry_floors_at_zero_never_heals():
    """边界：减免不会把伤害打成负数（法力远大于伤害时只是归零，不回血）。"""
    _, combat, player, enemy = _arena(mana=50, mana_limit=50)
    player.parrying_this_round = True
    combat._apply_hostile_damage(player, 5, source=enemy)
    assert player.current_hp == 100, "伤害归零，不得反向回血"


def test_parry_does_not_reduce_cost_damage():
    """边界：【代价】不受招架减免。

    否则招架会顺带免掉【透支】的流血，卖血流直接变成无代价——
    与格挡不吸收代价是同一条口径。
    """
    _, combat, player, _ = _arena(mana=10)
    player.parrying_this_round = True
    combat.pay_numeric_cost(player, "流血", 8, cost_context={
        "timing": "player_action", "source": "透支", "source_type": "daowen",
        "actor": player, "target": player, "mechanic": "cost",
        "subtype": "bleed", "amount": 8, "tags": {"active_payment"}})
    assert player.current_hp == 92, "代价须全额支付，不被招架减免"


def test_parry_with_zero_mana_reduces_nothing():
    """错误输入/边界：法力为0时招架合法但无效（减0），不应崩溃。"""
    _, combat, player, enemy = _arena(mana=0)
    player.parrying_this_round = True
    combat._apply_hostile_damage(player, 7, source=enemy)
    assert player.current_hp == 93


# ==================== 2. 招架：声明与回合锁 ====================

def _combat_engine(suffix):
    e = GameEngine(db_path=f"data/test_parry_{suffix}.db", rng_seed=1)
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
    """正常路径：声明招架成功，进入姿态，且**不消耗出手**。"""
    e = _combat_engine("declare")
    p = e.state.player
    used_before = p.actions_used_this_round
    r = e.execute_action("declare_parry", {})
    assert r["success"], r
    assert p.parrying_this_round is True
    assert p.actions_used_this_round == used_before, "招架不消耗出手"
    assert r["result"]["reduction_preview"] == p.current_mana


def test_cannot_parry_twice_in_same_round():
    """错误输入：同一回合重复声明被拒。"""
    e = _combat_engine("twice")
    assert e.execute_action("declare_parry", {})["success"]
    r2 = e.execute_action("declare_parry", {})
    assert not r2["success"] and "已处于招架" in r2["error"]


def test_parry_locks_out_the_following_round():
    """正常路径：招架后下回合被禁用；再下一回合恢复可用。"""
    e = _combat_engine("lock")
    p = e.state.player
    assert e.execute_action("declare_parry", {})["success"]
    e.state.combat_subphase = "await_round_end"
    e.execute_action("round_end", {})
    e.execute_action("round_start", {})
    assert p.parry_locked_this_round is True
    assert p.parrying_this_round is False, "姿态须在回始清空"
    r = e.execute_action("declare_parry", {})
    assert not r["success"] and "上回合已招架" in r["error"]
    # 再过一个回合：没招架过 → 锁解除
    e.state.combat_subphase = "await_round_end"
    e.execute_action("round_end", {})
    e.execute_action("round_start", {})
    assert p.parry_locked_this_round is False
    assert e.execute_action("declare_parry", {})["success"], "隔一回合应恢复可用"


def test_parry_is_listed_in_available_actions():
    """正常路径：招架出现在可用行动表里（否则玩家/AI 根本发现不了它）。"""
    e = _combat_engine("listed")
    actions = e.get_available_actions()["actions"]
    entry = next((a for a in actions if a["action_type"] == "declare_parry"), None)
    assert entry is not None, "招架必须出现在可用行动表"
    assert entry.get("available") is True


# ==================== 3. 血偿契 ====================

def test_blood_toll_grants_one_mana_per_ten_hp_lost():
    """正常路径：累计失去10生命 → +1法力。"""
    _, combat, player, enemy = _arena(mana=0, mana_limit=10, relics=("血偿契",))
    combat._apply_hostile_damage(player, 10, source=enemy)
    assert player.current_mana == 1, "满10点应换1法力"
    assert player.hp_lost_this_battle == 10


def test_blood_toll_remainder_carries_over():
    """边界：余数滚存——分笔挨打照样在第10点上结账。"""
    _, combat, player, enemy = _arena(mana=0, mana_limit=10, relics=("血偿契",))
    combat._apply_hostile_damage(player, 7, source=enemy)
    assert player.current_mana == 0, "不足10点不结账"
    combat._apply_hostile_damage(player, 5, source=enemy)
    assert player.current_mana == 1, "7+5=12 跨过第10点，结1次"
    combat._apply_hostile_damage(player, 8, source=enemy)
    assert player.current_mana == 2, "累计20 → 共2次"


def test_blood_toll_pays_out_multiple_at_once():
    """边界：一次掉25血应一次结算2点（不是只结1点）。"""
    _, combat, player, enemy = _arena(mana=0, mana_limit=10, bl=200, hp=200,
                                      relics=("血偿契",))
    combat._apply_hostile_damage(player, 25, source=enemy)
    assert player.current_mana == 2, "25//10=2"


def test_blood_toll_counts_cost_bleed_not_just_attacks():
    """正常路径（与卖血套路搭配的关键）：【代价】流血同样计入。

    【透支】流血4X 本来是纯支出，血偿契让它每满10点返还1法力。
    挂在唯一失血总账上，所以来源无关——挨打、流血代价、爆裂反噬一视同仁。
    """
    _, combat, player, _ = _arena(mana=0, mana_limit=10, relics=("血偿契",))
    combat.pay_numeric_cost(player, "流血", 12, cost_context={
        "timing": "player_action", "source": "透支", "source_type": "daowen",
        "actor": player, "target": player, "mechanic": "cost",
        "subtype": "bleed", "amount": 12, "tags": {"active_payment"}})
    assert player.current_mana == 1, "透支的流血也算失去生命"


def test_blood_toll_is_capped_by_mana_limit():
    """边界：返还的法力同样不得超过[法限]（全局上限）。"""
    _, combat, player, enemy = _arena(mana=9, mana_limit=10, relics=("血偿契",))
    combat._apply_hostile_damage(player, 30, source=enemy)
    assert player.current_mana == 10, "3点返还只落地1点，其余被法限吃掉"


def test_blood_toll_does_nothing_without_the_relic():
    """对照：未持有该遗物时失血不产生任何法力。"""
    _, combat, player, enemy = _arena(mana=0, mana_limit=10)
    combat._apply_hostile_damage(player, 30, source=enemy)
    assert player.current_mana == 0


def test_blood_toll_resets_between_battles():
    """边界：本场累计——[战始]归零，上一场的余数不带进新战斗。"""
    _, combat, player, enemy = _arena(mana=0, mana_limit=10, relics=("血偿契",))
    combat._apply_hostile_damage(player, 9, source=enemy)
    assert player.hp_lost_this_battle == 9 and player.current_mana == 0
    combat.reset_monster_activation()   # 战始重置入口
    assert player.hp_lost_this_battle == 0 and player.blood_toll_paid == 0
    combat._apply_hostile_damage(player, 9, source=enemy)
    assert player.current_mana == 0, "新战斗重新计数，9+9 不得凑成一次结算"


# ==================== 4. 全局上限 ====================

def test_all_attributes_capped_at_their_limits():
    """正常路径：生命/法力/速度一律不得超过各自上限（无需任何遗物）。"""
    _, combat, player, _ = _arena(mana=10, mana_limit=10)
    player.current_mana = 999
    player.current_speed = 999
    player.current_hp = 999
    combat.clamp_immortal_body(player)
    assert player.current_mana == player.mana_limit
    assert player.current_speed == player.speed_limit
    assert player.current_hp == player.blood_limit


def test_cap_does_not_block_refilling_a_spent_pool():
    """对照：封顶 ≠ 失效。花掉的部分照样能被重新填回来。"""
    _, combat, player, _ = _arena(mana=10, mana_limit=10)
    player.spend_mana(6)
    player.current_mana += 3
    combat.clamp_immortal_body(player)
    assert player.current_mana == 7, "未满池时获得须如实入账"


# ==================== 5. 招架 × 血偿契 × 卖血流 ====================

def test_parry_and_blood_toll_compose_on_the_same_hit():
    """集成：同一次受击上，招架先减伤，剩下的失血再喂血偿契。

    顺序是有意义的：招架在 _incoming_adjust 里先把伤害卸掉，血偿契记的是
    **实际失去的生命**。所以扛得越稳，换到的法力越少——这两件东西互相牵制，
    不是无脑叠加。
    """
    _, combat, player, enemy = _arena(mana=4, mana_limit=20, bl=200, hp=200,
                                      relics=("血偿契",))
    player.parrying_this_round = True
    combat._apply_hostile_damage(player, 24, source=enemy)
    # 24 - 4(法力) = 20 实际失血 → 20//10 = 2 法力
    assert player.current_hp == 180, f"24-4=20，实掉{200 - player.current_hp}"
    assert player.current_mana == 6, "失血20 → +2法力（4→6）"


def test_blood_toll_feeds_the_touzhi_regeneration_loop():
    """集成（用户点名的搭配）：血偿契把【血炼周天】的闭环从净零变成净赚法力。

    闭环本身：【再生X】消耗X法力→回复4X生命；【透支X】流血4X→获得X法力，
    严格 4:1 双向汇率，每轮生命净零、法力净零。
    挂上血偿契后，透支那 4X 流血额外按每10点返1法力结算——闭环开始产出法力。
    这里直接驱动一轮 X=3：流血12 → 得3法力(透支) + 1法力(血偿契 12//10)。
    """
    _, combat, player, _ = _arena(mana=0, mana_limit=20, bl=200, hp=200,
                                  relics=("血偿契",))
    combat.pay_numeric_cost(player, "流血", 12, cost_context={
        "timing": "player_action", "source": "透支", "source_type": "daowen",
        "actor": player, "target": player, "mechanic": "cost",
        "subtype": "bleed", "amount": 12, "tags": {"active_payment"}})
    toll_mana = player.current_mana
    assert toll_mana == 1, "透支流血12 → 血偿契返1法力"
    player.current_mana += 3          # 透支本体产出
    combat.clamp_immortal_body(player)
    assert player.current_mana == 4, "本轮共得4法力（透支3 + 血偿契1）"
    assert player.current_hp == 188
