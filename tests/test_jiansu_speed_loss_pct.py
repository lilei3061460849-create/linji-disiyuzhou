"""【减速】改版回归（DM 裁定 2026-09-18：使[目标]失去其当前速度的10X%）。

旧版正文是「代价：异变5X。使[目标]速度减半，持续X」，而 `duration` 在实现里
**从未被读取**——没有挂任何状态，就是一次性把当前速度砍半。于是 X 只把异变代价
从 5 涨到 25、效果一点不变，减速X=2…5 被 X=1 严格支配（"没人会用减速"的根因）。

新版把 X 变成**幅度**参数：失去当前速度的 10X%，X=5 即"失去一半"。取整按正文
「整数规则：所有计算都向上取整」（`ceil(cur × 10X / 100)`），因此奇数速度上 X=5 比
旧版减半多削 1 点；向上取整同时消灭了"低 X 空放"——只有当前速度为 0 才削 0 点。
X 不另设封顶：刹车是异变本身（达 50 层【崩解】直接命零，怪物侧探测按生存线卡在 9）。
速度是一池制（[回始]不回填、[战终]复原），所以正文不再写「持续X」。

覆盖：幅度随 X 线性 / X=5＝失去一半（向上取整）/ 低 X 至少削 1 点 / 速度 0 削 0 /
X 只受崩解线约束（不设数值封顶）/ 削的是速度池不是[速限]（与【冥气】分轴）/
代价与键位不变量 / 波及各按自身速度 / 正文口径一致。
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.daowen import DaoWenEngine
from engine.dice import DiceEngine
from engine.enums import CostType
from engine.models import Entity, GameState

DaoWenEngine.register_all()


def _battle(*, player_speed=20, monster_speed=6):
    """最小战斗：怪物 脑蜘蛛 对轮回者 沈昼 施放【减速】（减速是原始怪物道纹）。"""
    state = GameState(phase="in_combat", combat_subphase="monster_action")
    player = Entity("沈昼", "轮回者", blood_limit=60, current_hp=60,
                    mana_limit=20, current_mana=20,
                    speed_limit=player_speed, current_speed=player_speed)
    monster = Entity("脑蜘蛛", "怪物", blood_limit=40, current_hp=40,
                     mana_limit=30, current_mana=30,
                     speed_limit=monster_speed, current_speed=monster_speed)
    state.player = player
    state.enemies = [monster]
    return state, CombatEngine(state, DiceEngine()), player, monster


def _cast_jiansu(combat, caster, target, x=1):
    return combat.apply_daowen_effect(
        "减速", DaoWenEngine.calculate_jiansu(x, target), caster, target)


# ---------------------------------------------------------------- 幅度随 X 线性


def test_jiansu_loss_scales_linearly_with_x():
    """正常路径：当前速度40 的目标，X=1…5 依次失去 4/8/12/16/20 点"""
    state, combat, player, monster = _battle(player_speed=40)

    for x, expected in ((1, 4), (2, 8), (3, 12), (4, 16), (5, 20)):
        player.current_speed = 40
        result = _cast_jiansu(combat, monster, player, x=x)
        effect = next(e for e in result["effects"] if e["type"] == "speed_loss_pct")
        assert effect["pct"] == 10 * x
        assert effect["lost"] == expected, f"X={x} 应失去{expected}点"
        assert player.current_speed == 40 - expected


def test_jiansu_x5_loses_half_rounded_up():
    """边界：X=5＝失去一半，按正文「整数规则：所有计算都向上取整」→
    奇数速度上比旧版减半（floor）多削 1 点；剩余恒为 speed // 2"""
    state, combat, player, monster = _battle(player_speed=24)

    for speed in range(1, 25):
        player.current_speed = speed
        _cast_jiansu(combat, monster, player, x=5)
        assert player.current_speed == speed // 2, (
            f"当前速度{speed}：失去 ceil({speed}×50%)={math.ceil(speed / 2)}，应剩 {speed // 2}")


def test_jiansu_low_x_still_takes_at_least_one_point():
    """边界：向上取整消灭了"低X空放"——速度2 时 X=1 也削 1 点，怪物不会白花一次出手"""
    state, combat, player, monster = _battle(player_speed=2)
    player.current_speed = 2

    result = _cast_jiansu(combat, monster, player, x=1)

    effect = next(e for e in result["effects"] if e["type"] == "speed_loss_pct")
    assert effect["lost"] == 1, "ceil(2×10%) = 1"
    assert player.current_speed == 1
    assert not player.has_status("减速"), "本效果不挂状态（旧版的持续X从来没被实现）"


def test_jiansu_on_zero_speed_loses_nothing():
    """边界：当前速度为 0 时削 0 点（唯一算出 0 的情形），不报错、不倒扣"""
    state, combat, player, monster = _battle(player_speed=0)

    result = _cast_jiansu(combat, monster, player, x=3)

    effect = next(e for e in result["effects"] if e["type"] == "speed_loss_pct")
    assert effect["lost"] == 0
    assert player.current_speed == 0


def test_jiansu_x_is_bounded_only_by_the_collapse_line():
    """X 不另设数值封顶（DM 裁定：45 层异变不是小代价）——刹车是崩解线本身。
    新怪 max_x=9（45 层，离 50 只差一次代价）；已叠到 45 层时连 X=1 都不再提供。"""
    state, combat, player, monster = _battle()

    assert monster.mutation_count == 0
    assert combat._monster_max_daowen_x(monster, "减速", player) == 9

    monster.mutation_count = 45
    assert combat._monster_max_daowen_x(monster, "减速", player) == 0, \
        "45 层时再付异变5X 即达 50 → 崩解命零，引擎不主动提供自杀档"


# ---------------------------------------------------------------- 与【冥气】分轴


def test_jiansu_cuts_the_speed_pool_not_the_speed_limit():
    """减速削的是整场速度池（一池制，[战终]才复原），[速限]不受影响——
    这正是它与【冥气】（每失去一次速度[速限]-2、整场累积）的分轴。"""
    state, combat, player, monster = _battle(player_speed=20)
    limit_before = player.speed_limit

    _cast_jiansu(combat, monster, player, x=2)

    assert player.current_speed == 16, "失去当前速度的20%"
    assert player.speed_limit == limit_before, "[速限]一点不动"


# ---------------------------------------------------------------- 数据不变量


def test_jiansu_calc_keeps_mutation_cost_and_drops_dead_fields():
    calc = DaoWenEngine.calculate_jiansu(3)

    assert calc["cost_type"] == CostType.MUTATION.value
    assert calc["cost_mutation"] == 15, "代价仍是异变5X，本次只改效果幅度"
    assert calc["speed_loss_pct"] == 30, "消费点认的就是这个键"
    assert "duration" not in calc, "旧版的 duration 是死参数，随改版删除"
    assert "speed_halved" not in calc
    assert "30%" in calc["summary"]


def test_jiansu_docstring_matches_its_rule_text():
    doc = DaoWenEngine.calculate_jiansu.__doc__.strip().splitlines()[0]
    calc = DaoWenEngine.calculate_jiansu(2)

    assert doc == "减速X：代价：异变5X。使[目标]失去其当前速度的10X%"
    assert "持续" not in doc, "一池制速度没有'持续'可言，正文不得再写持续X"
    assert calc["summary"].startswith("异变+10")


# ---------------------------------------------------------------- 波及


def test_jiansu_hits_each_wave_target_by_its_own_speed():
    """波及目标是按百分比削速的状态类效果：各按自身当前速度结算，不做数值平分"""
    state, combat, player, monster = _battle(player_speed=20, monster_speed=10)
    ally = Entity("同行者", "朋友", blood_limit=30, current_hp=30,
                  mana_limit=5, current_mana=5, speed_limit=10, current_speed=5)
    state.friends = [ally]
    combat._toggle_wave_mark(player, monster)
    combat._toggle_wave_mark(ally, monster)

    result = _cast_jiansu(combat, monster, player, x=2)

    lost = {e["target"]: e["lost"] for e in result["effects"] if e["type"] == "speed_loss_pct"}
    assert lost == {"沈昼": 4, "同行者": 1}, "20%×20=4、20%×5=1，各按自身速度而非平分"
    assert player.current_speed == 16 and ally.current_speed == 4
