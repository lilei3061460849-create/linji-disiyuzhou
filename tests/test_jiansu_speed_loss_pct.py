"""【减速】改版回归（DM 裁定 2026-09-18：使[目标]失去其当前速度的10X%）。

旧版正文是「代价：异变5X。使[目标]速度减半，持续X」，而 `duration` 在实现里
**从未被读取**——没有挂任何状态，就是一次性把当前速度砍半。于是 X 只把异变代价
从 5 涨到 25、效果一点不变，减速X=2…5 被 X=1 严格支配（"没人会用减速"的根因）。

新版把 X 变成**幅度**参数：失去当前速度的 10X%，X=5 恰好等于旧版减半；取整向下，
与旧式 `lost = cur - ceil(cur/2)`（恒等于 `floor(cur/2)`）逐位一致。
速度是一池制（[回始]不回填、[战终]复原），所以正文不再写「持续X」。

覆盖：幅度随 X 线性 / X=5 与旧减半逐位相同 / 低 X 打低速度取整为 0（如实钉住）/
削的是速度池不是[速限]（与【冥气】分轴）/ 代价与键位不变量 / 波及各按自身速度 /
正文口径一致。
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


def test_jiansu_x5_matches_the_old_halving_bit_for_bit():
    """边界：X=5 与旧版「速度减半」在当前速度 1…24 上逐位一致"""
    state, combat, player, monster = _battle(player_speed=24)

    for speed in range(1, 25):
        player.current_speed = speed
        _cast_jiansu(combat, monster, player, x=5)
        expected_lost = speed - math.ceil(speed / 2)   # 旧实现
        assert player.current_speed == speed - expected_lost, f"当前速度{speed}时与旧减半不一致"


def test_jiansu_low_x_on_low_speed_rounds_down_to_zero():
    """边界：向下取整的如实后果——低 X 打低速度目标会算出 0 点（不扣、也不报错）"""
    state, combat, player, monster = _battle(player_speed=2)
    player.current_speed = 2

    result = _cast_jiansu(combat, monster, player, x=1)

    effect = next(e for e in result["effects"] if e["type"] == "speed_loss_pct")
    assert effect["lost"] == 0
    assert player.current_speed == 2, "10% × 2 = 0.2 → 向下取整为 0"
    assert not player.has_status("减速"), "本效果不挂状态（旧版的持续X从来没被实现）"


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
