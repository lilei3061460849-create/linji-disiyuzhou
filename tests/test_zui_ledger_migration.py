"""罪孽都市账本与遗物【灾厄轮盘】结算验证（原道纹【赌命】于2026-10-14改名并遗物化）。

被迁对象（旧实现都在 combat.py 回始效果循环之后的内嵌 for + 回终清账块）：
    【清算·结算】[回始] 目标失去[施法者当前碎片]点格挡
    遗物【灾厄轮盘】[回始] 随机存活目标失去30%当前生命（RNG 目标，骰子流名含回合号）
    【清算·对账】状态消失即清账
    （【逼债·结算/对账】随【逼债】删除一并移除，原用例已删）

本文件钉死的约束（迁移协议第 1/4 条）：
    1. 声明映射：when/target/condition/effect 均正确注册；priority 只作序列位置缺失时的后备；
    2. 顺序即规则：账本机制按来源道纹在角色序列中的位置结算，不保留旧固定顺序；
    3. 只触发一次：每次回始每笔账结算一次，叠加多笔=多条报告；
    4. 报告条目与旧实现同形（字段名不改，战报读法不变）；
    5. 账本唯一入口（engine/mechanisms/ledger.py），管线里不再有硬编码分支
       （护栏 check_migrated_mechanism_guards 必须为 0）。
"""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.mechanisms import MECHANISMS, Phase, Trigger
from engine.mechanisms.ledger import clear_ledger, ledger_names, ledger_of
from engine.api import GameEngine
from engine.daowen import DaoWenEngine, ResonanceEngine, RELICIZED_SLOT
from engine.gamedata import REGION_EXCLUSIVE_DAOWEN
from engine.models import DaoWen, DaoWenInstance, Entity, GameState, Relic, StatusEffect
from engine.validator import check_migrated_mechanism_guards


def _arena(player_hp: int = 100, enemy_hp: int = 100):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=player_hp,
                    mana_limit=50, current_mana=50, speed_limit=10, current_speed=10)
    enemy = Entity("M", "怪物", blood_limit=enemy_hp, current_hp=enemy_hp,
                   speed_limit=2, current_speed=2)
    state.player = player
    state.enemies = [enemy]
    state.shards = 0
    return state, CombatEngine(state, DiceEngine(seed=7)), player, enemy


def _effects_of(effects, kind):
    return [e for e in effects if e.get("type") == kind]


def test_duming_is_no_longer_a_daowen_and_is_in_the_relic_pool():
    DaoWenEngine.register_all()
    assert "赌命" not in DaoWenEngine._registry
    assert "赌命" not in REGION_EXCLUSIVE_DAOWEN["罪孽都市"]
    relic_names = {name for name, _ in GameEngine.RELIC_DEFS}
    assert "灾厄轮盘" in relic_names
    assert "赌命" not in relic_names
    with pytest.raises(ValueError):
        DaoWenEngine.resolve("赌命", 1)

    # 遗物化道纹的闭环节点统一用「？？」占位：保留边，但不能作为残韵结果获得。
    assert RELICIZED_SLOT == "？？"
    assert RELICIZED_SLOT not in DaoWenEngine._registry
    loop = ResonanceEngine.CLOSED_LOOPS["罪孽都市闭环"]
    assert ("假钞", "曲解", RELICIZED_SLOT) in loop
    assert (RELICIZED_SLOT, "反转", "消灾") in loop
    assert len(loop) == 8 and not any("赌命" in (src, dst) for src, _, dst in loop)
    options = ResonanceEngine.get_available_resonance("假钞")
    assert not any(option["target_daowen"] == "赌命" for option in options)
    assert any(option["target_daowen"] == RELICIZED_SLOT and option.get("reserved")
               for option in options)
    stock = {"曲解": 1}
    blocked = ResonanceEngine.apply_resonance("假钞", "曲解", True, True, stock)
    assert blocked["success"] is False and blocked.get("reserved") is True
    assert stock["曲解"] == 1
    blocked_explicit = ResonanceEngine.apply_resonance(
        "假钞", "曲解", True, True, stock, target_daowen=RELICIZED_SLOT)
    assert blocked_explicit["success"] is False and blocked_explicit.get("reserved") is True
    assert stock["曲解"] == 1


# ==================== 声明映射 ====================

def test_mechanisms_registered_with_expected_phase_and_priority():
    """清算和遗物灾厄轮盘注册在 ROUND_START_SETTLE，清算对账注册在 ROUND_END_RECONCILE。"""
    expected = {"清算·结算", "灾厄轮盘·结算"}
    for name in expected:
        m = MECHANISMS.get(name)
        assert m is not None, f"{name} 未注册"
        assert m.when.kind == "phase" and m.when.key == Phase.ROUND_START_SETTLE
        assert m.status_name == ("清算" if name == "清算·结算" else "")
    m = MECHANISMS.get("清算·对账")
    assert m is not None and m.when.key == Phase.ROUND_END_RECONCILE
    # 回始结算相位仅允许这两个机制；清算以状态来源的道纹序列排序，遗物灾厄轮盘固定后置。
    names = [m.name for m in MECHANISMS.phase_mechanisms(Phase.ROUND_START_SETTLE)]
    assert len(names) == 2 and set(names) == expected
    # 2026-10-13：【逼债·结算/对账】随道纹删除，必须已注销。
    assert MECHANISMS.get("逼债·结算") is None
    assert MECHANISMS.get("逼债·对账") is None


def test_legacy_ledger_status_order_is_recovered_from_caster_sequence():
    """旧存档的账本状态挂在目标身上，恢复顺序时必须查施法者而不是目标。"""
    state, _, player, enemy = _arena()
    for name in ("背负", "清算"):
        player.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="消耗",
                   cost_formula="X", effect_formula=""),
            x_value=1,
        )
    # 故意让目标自己的序列与施法者不同，确保不会误用状态持有者的顺序。
    for name in ("清算", "背负"):
        enemy.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="消耗",
                   cost_formula="X", effect_formula=""),
            x_value=1,
        )
        enemy.add_status(StatusEffect(name, remaining_rounds=-1, source=player.name))
    engine = GameEngine.__new__(GameEngine)
    engine.state = state
    engine._migrate_legacy_daowen_status_order()
    order = {status.name: status.daowen_order for status in enemy.status_effects}
    # 施法者序列（背负=0，清算=1），不是目标自己的序列（清算是0）——查施法者才对。
    assert order == {"清算": 1, "背负": 0}


def test_pipeline_has_no_hardcoded_branches_for_migrated_mechanisms():
    """护栏：核心管线不得再出现三机制的硬编码 has_status 分支。"""
    assert check_migrated_mechanism_guards() == []


def test_ledger_is_single_entry_point():
    """账本入口：登记名字、写读清一致；未登记机制必须报错（防私挂字段）。"""
    assert ledger_names() == ["清算"]   # sorted() 口径；2026-10-13 删【逼债】后仅剩清算
    _, _, _, enemy = _arena()
    ledger_of(enemy, "清算").append({"x": 3})
    assert enemy._qingsuan == [{"x": 3}]
    assert clear_ledger(enemy, "清算") == 1
    assert enemy._qingsuan == []
    with pytest.raises(ValueError):
        ledger_of(enemy, "不存在的机制")


# 2026-10-13：【逼债】删除，原数值口径四组用例已删。

def test_qingsuan_drains_shield_by_caster_shards():
    state, combat, player, enemy = _arena()
    enemy.shield = 50
    state.shards = 20                     # 施法者（玩家）碎片
    ledger_of(enemy, "清算").append({"x": 2, "caster": player})
    effects = combat.round_start()["effects"]
    got = _effects_of(effects, "qingsuan")
    assert len(got) == 1 and got[0]["lost_shield"] == 20 and got[0]["drain"] == 20
    assert enemy.shield == 30


def test_qingsuan_is_capped_by_shield_and_rereads_caster_shards_each_round():
    state, combat, player, enemy = _arena()
    enemy.shield = 5
    state.shards = 20
    ledger_of(enemy, "清算").append({"x": 2, "caster": player})
    first = _effects_of(combat.round_start()["effects"], "qingsuan")
    assert first[0]["lost_shield"] == 5          # 格挡只有 5 → 只掉 5
    state.shards = 3                              # 施法者碎片变了 → 下一轮按新值
    enemy.shield = 10
    second = _effects_of(combat.round_start()["effects"], "qingsuan")
    assert second[0]["drain"] == 3 and second[0]["lost_shield"] == 3


# ==================== 灾厄轮盘遗物 ====================

def test_disaster_wheel_hits_one_target_for_thirty_percent_current_hp():
    state, combat, player, enemy = _arena(player_hp=100, enemy_hp=200)
    state.relics.append(Relic("灾厄轮盘", "[回始]随机选一名存活角色，使其失去30%当前生命"))
    effects = combat.round_start()["effects"]
    got = _effects_of(effects, "duming")
    assert len(got) == 1
    entry = got[0]
    assert entry["caster"] == player.name
    assert entry["of"] == 2 and 1 <= entry["roll"] <= 2
    # 30% 按**出手前**的当前生命算（旧口径 ceil(当前生命×30/100)），报告里带 hp_before/hp_after
    assert entry["lost"] == math.ceil(entry["hp_before"] * 30 / 100)
    assert entry["damage"] == entry["lost"]
    assert entry["hp_after"] == entry["hp_before"] - entry["lost"]
    victim = player if entry["target"] == player.name else enemy
    assert victim.current_hp == entry["hp_after"]


def test_disaster_wheel_report_matches_the_rolled_target():
    """roll/of 与目标必须一对：第 roll 个存活角色就是被打的那个（号码口径）。"""
    state, combat, player, enemy = _arena()
    state.relics.append(Relic("灾厄轮盘", "[回始]随机选一名存活角色，使其失去30%当前生命"))
    entry = _effects_of(combat.round_start()["effects"], "duming")[0]
    alive = [e for e in (state.get_all_player_side() + state.get_all_enemy_side()) if e.is_alive]
    assert alive[entry["roll"] - 1].name == entry["target"]


def test_disaster_wheel_single_alive_always_targeted_and_consumes_nothing_extra():
    state, combat, player, enemy = _arena()
    enemy.is_alive = False
    state.relics.append(Relic("灾厄轮盘", "[回始]随机选一名存活角色，使其失去30%当前生命"))
    before = player.current_hp
    entry = _effects_of(combat.round_start()["effects"], "duming")[0]
    assert entry["target"] == player.name and entry["of"] == 1 and entry["roll"] == 1
    assert player.current_hp < before


def test_disaster_wheel_requires_an_active_unsealed_relic():
    state, combat, player, _ = _arena()
    assert not _effects_of(combat.round_start()["effects"], "duming")

    state.relics.append(Relic("灾厄轮盘", "[回始]随机选一名存活角色，使其失去30%当前生命"))
    state.sealed_relics["灾厄轮盘"] = 1
    assert not _effects_of(combat.round_start()["effects"], "duming")

    state.sealed_relics.clear()
    assert len(_effects_of(combat.round_start()["effects"], "duming")) == 1


# ==================== 顺序即规则 ====================

def test_disaster_wheel_settles_after_status_anchored_daowen_effects():
    """灾厄轮盘改为常驻遗物后不再跟随道纹序列，排在有状态锚点的清算之后。"""
    state, combat, player, enemy = _arena()
    second = Entity("M2", "怪物", blood_limit=100, current_hp=100, shield=40,
                    speed_limit=2, current_speed=2)
    state.enemies.append(second)
    state.relics.append(Relic("灾厄轮盘", "[回始]随机选一名存活角色，使其失去30%当前生命"))
    state.shards = 5
    for index, e in enumerate((enemy, second)):
        e.shards = 10
        e.shield = 40
        ledger_of(e, "清算").append({"x": 1, "caster": player})
        e.add_status(StatusEffect("清算", value=1, remaining_rounds=-1,
                                  source=player.name, daowen_order=index))
    kinds = [e["type"] for e in combat.round_start()["effects"]
             if e.get("type") in ("qingsuan", "duming")]
    assert kinds == ["qingsuan", "qingsuan", "duming"]


# ==================== 对账：状态消失即清账 ====================

def test_qingsuan_ledger_cleared_after_status_expires():
    """清算持续X：到期（状态被 tick 掉）后不得再多结算一轮。"""
    state, combat, player, enemy = _arena()
    enemy.shield = 40
    state.shards = 10
    enemy.add_status(StatusEffect("清算", value=1, remaining_rounds=1, source=player.name))
    ledger_of(enemy, "清算").append({"x": 1, "caster": player})
    assert _effects_of(combat.round_start()["effects"], "qingsuan")   # 本轮结算
    combat.round_end()                                               # 状态到期 → 对账清账
    assert not enemy.has_status("清算")
    assert ledger_of(enemy, "清算") == []
    enemy.shield = 40
    assert not _effects_of(combat.round_start()["effects"], "qingsuan")
