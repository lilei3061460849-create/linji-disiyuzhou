"""罪孽都市·账本三机制迁移验证（2026-10-03）。

被迁对象（旧实现都在 combat.py 回始效果循环之后的三段内嵌 for + 回终清账块）：
    【逼债·结算】[回始] 目标失去X碎片，无力支付部分记负债（真碎片扣负）
    【清算·结算】[回始] 目标失去[施法者当前碎片]点格挡
    【赌命·结算】[回始] 随机存活目标失去30%当前生命（RNG 目标，骰子流名含回合号）
    【逼债·对账】【清算·对账】状态消失即清账

本文件钉死的约束（迁移协议第 1/4 条）：
    1. 声明映射：when/target/condition/effect 均正确注册；priority 只作序列位置缺失时的后备；
    2. 顺序即规则：三个账本机制按来源道纹在角色序列中的位置结算，不保留旧固定顺序；
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
from engine.models import DaoWen, DaoWenInstance, Entity, GameState, StatusEffect
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


# ==================== 声明映射 ====================

def test_mechanisms_registered_with_expected_phase_and_priority():
    """三个结算机制注册在 ROUND_START_SETTLE；priority 仅作缺少序列锚点时的后备。"""
    expected = {"逼债·结算", "清算·结算", "赌命·结算"}
    for name in expected:
        m = MECHANISMS.get(name)
        assert m is not None, f"{name} 未注册"
        assert m.when.kind == "phase" and m.when.key == Phase.ROUND_START_SETTLE
        assert m.status_name in {"逼债", "清算", "赌命"}
    for name in ("逼债·对账", "清算·对账"):
        m = MECHANISMS.get(name)
        assert m is not None and m.when.key == Phase.ROUND_END_RECONCILE
    # 相位内仅允许这三个账本结算机制；实际顺序由状态的道纹序列位置决定。
    names = [m.name for m in MECHANISMS.phase_mechanisms(Phase.ROUND_START_SETTLE)]
    assert len(names) == 3 and set(names) == expected


def test_legacy_ledger_status_order_is_recovered_from_caster_sequence():
    """旧存档的账本状态挂在目标身上，恢复顺序时必须查施法者而不是目标。"""
    state, _, player, enemy = _arena()
    for name in ("赌命", "清算", "逼债"):
        player.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="消耗",
                   cost_formula="X", effect_formula=""),
            x_value=1,
        )
    # 故意让目标自己的序列与施法者不同，确保不会误用状态持有者的顺序。
    for name in ("清算", "逼债"):
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
    assert order == {"清算": 1, "逼债": 2}


def test_pipeline_has_no_hardcoded_branches_for_migrated_mechanisms():
    """护栏：核心管线不得再出现三机制的硬编码 has_status 分支。"""
    assert check_migrated_mechanism_guards() == []


def test_ledger_is_single_entry_point():
    """账本入口：登记名字、写读清一致；未登记机制必须报错（防私挂字段）。"""
    assert ledger_names() == ["清算", "逼债"]   # sorted() 口径
    _, _, _, enemy = _arena()
    ledger_of(enemy, "逼债").append({"x": 3})
    assert enemy._bizhai == [{"x": 3}]
    assert clear_ledger(enemy, "逼债") == 1
    assert enemy._bizhai == []
    with pytest.raises(ValueError):
        ledger_of(enemy, "不存在的机制")


# ==================== 逼债：数值口径与报告 ====================

def test_bizhai_pays_shards_when_able():
    state, combat, player, enemy = _arena()
    enemy.shards = 20
    ledger_of(enemy, "逼债").append({"x": 10})
    effects = combat.round_start()["effects"]
    got = _effects_of(effects, "bizhai")
    assert len(got) == 1 and got[0]["entity"] == enemy.name and got[0]["lost_shards"] == 10
    assert enemy.shards == 10


def test_bizhai_records_debt_when_unable_to_pay():
    """无力支付 → 全额记负债（真碎片扣负），且不碰血限（旧裁定D：2X血限路径已废止）。"""
    state, combat, player, enemy = _arena(enemy_hp=4)
    enemy.shards = 0
    enemy.fake_shards = 0
    ledger_of(enemy, "逼债").append({"x": 10})
    effects = combat.round_start()["effects"]
    got = _effects_of(effects, "bizhai_debt")
    assert len(got) == 1
    assert got[0]["obligation"] == 10 and got[0]["shards_now"] == -10 and got[0]["debt_now"] == 10
    assert enemy.shards == -10
    assert enemy.blood_limit == 4 and enemy.current_hp == 4 and enemy.is_alive


def test_bizhai_spends_fake_shards_first():
    """假碎片优先：够付时不动真碎片、不记负债。"""
    state, combat, player, enemy = _arena()
    enemy.shards = 0
    enemy.fake_shards = 30
    ledger_of(enemy, "逼债").append({"x": 10})
    effects = combat.round_start()["effects"]
    assert _effects_of(effects, "bizhai") and not _effects_of(effects, "bizhai_debt")
    assert enemy.fake_shards == 20 and enemy.shards == 0


def test_bizhai_stacks_one_report_per_entry():
    """叠加两笔账 → 同一次回始结算两条（旧口径逐笔结算，不合并）。"""
    _, combat, _, enemy = _arena()
    enemy.shards = 50
    ledger_of(enemy, "逼债").append({"x": 3})
    ledger_of(enemy, "逼债").append({"x": 5})
    got = _effects_of(combat.round_start()["effects"], "bizhai")
    assert [e["lost_shards"] for e in got] == [3, 5]
    assert enemy.shards == 42


# ==================== 清算 ====================

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


# ==================== 赌命 ====================

def test_duming_hits_one_target_for_thirty_percent_current_hp():
    state, combat, player, enemy = _arena(player_hp=100, enemy_hp=200)
    player.add_status(StatusEffect("赌命", value=2, remaining_rounds=-1, source="P"))
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


def test_duming_report_matches_the_rolled_target():
    """roll/of 与目标必须一对：第 roll 个存活角色就是被打的那个（号码口径）。"""
    state, combat, player, enemy = _arena()
    player.add_status(StatusEffect("赌命", value=1, remaining_rounds=-1, source="P"))
    entry = _effects_of(combat.round_start()["effects"], "duming")[0]
    alive = [e for e in (state.get_all_player_side() + state.get_all_enemy_side()) if e.is_alive]
    assert alive[entry["roll"] - 1].name == entry["target"]


def test_duming_single_alive_always_targeted_and_consumes_nothing_extra():
    state, combat, player, enemy = _arena()
    enemy.is_alive = False
    player.add_status(StatusEffect("赌命", value=1, remaining_rounds=-1, source="P"))
    before = player.current_hp
    entry = _effects_of(combat.round_start()["effects"], "duming")[0]
    assert entry["target"] == player.name and entry["of"] == 1 and entry["roll"] == 1
    assert player.current_hp < before


# ==================== 顺序即规则 ====================

def test_settlement_order_follows_daowen_sequence_not_fixed_order():
    """赌命→清算→逼债：三个机制全部偏离旧固定顺序时，仍按来源道纹序列结算。"""
    state, combat, player, enemy = _arena()
    second = Entity("M2", "怪物", blood_limit=100, current_hp=100, shield=40,
                    speed_limit=2, current_speed=2)
    state.enemies.append(second)
    state.shards = 5
    for e in (enemy, second):
        e.shards = 10
        e.shield = 40
        ledger_of(e, "逼债").append({"x": 2})
        ledger_of(e, "清算").append({"x": 1, "caster": player})
        e.add_status(StatusEffect("清算", value=1, remaining_rounds=-1,
                                  source=player.name, daowen_order=1))
        e.add_status(StatusEffect("逼债", value=2, remaining_rounds=-1,
                                  source=player.name, daowen_order=2))
    player.add_status(StatusEffect("赌命", value=1, remaining_rounds=-1,
                                   source=player.name, daowen_order=0))
    kinds = [e["type"] for e in combat.round_start()["effects"]
             if e.get("type") in ("bizhai", "qingsuan", "duming")]
    assert kinds == ["duming", "qingsuan", "qingsuan", "bizhai", "bizhai"]


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


def test_bizhai_ledger_cleared_when_status_removed_but_kept_while_status_lasts():
    """逼债 ∞：状态在就继续逐回始结算；状态被移除（驱散/清算式清理）后清账。"""
    state, combat, player, enemy = _arena()
    enemy.shards = 100
    enemy.add_status(StatusEffect("逼债", value=2, remaining_rounds=-1, source=player.name))
    ledger_of(enemy, "逼债").append({"x": 2})
    assert _effects_of(combat.round_start()["effects"], "bizhai")
    combat.round_end()
    assert ledger_of(enemy, "逼债"), "∞ 状态还在时不得清账"
    enemy.status_effects = [s for s in enemy.status_effects if s.name != "逼债"]
    combat.round_end()
    assert ledger_of(enemy, "逼债") == [], "状态消失即清账"
    assert not _effects_of(combat.round_start()["effects"], "bizhai")
