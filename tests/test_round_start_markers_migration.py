"""【狂暴·标记】【畸变·标记】迁移验证：round_start 纯报告块 → ROUND_START 相位机制。

两个标记块是同一类"零动词报告块"（priority 50/60），故共用本测试文件。
验证点：
  - 条目形状逐字一致（extra_attack_ready / deform_pending）；
  - 畸变标记的 blood_loss 是原始乘积（与结算块的 max(0,...) 不同，旧块原文如此）；
  - 顺序：…勾魂(40) → 狂暴·标记(50) → 畸变·标记(60)，即旧循环末尾两位；
  - 与【畸变·结算】（ROUND_END）互不干扰。
"""
from __future__ import annotations

import itertools
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.mechanisms import MECHANISMS, Phase
from engine.models import Entity, GameState, StatusEffect
from engine.validator import check_migrated_mechanism_guards

ROOT = Path(__file__).resolve().parents[1]
COMBAT_SOURCE = (ROOT / "engine" / "combat.py").read_text(encoding="utf-8")


def _arena(ac=0, ap=0, statuses=()):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=0, speed_limit=10, current_speed=5)
    ent = Entity("E", "怪物", blood_limit=50, current_hp=50,
                 attack_count=ac, attack_power=ap)
    for name, value in statuses:
        ent.add_status(StatusEffect(name=name, remaining_rounds=-1,
                                    value=value, source="x"))
    state.player = player
    state.enemies = [ent]
    return state, CombatEngine(state, DiceEngine()), player, ent


def _phase_results(combat, entity):
    return combat._dispatch_phase(Phase.ROUND_START, target=entity)


def _entry(results, entry_type):
    return next((r for r in results if isinstance(r, dict)
                 and r.get("type") == entry_type), None)


# ==================== 1. 注册 / 旧实现删除 ====================

def test_markers_registered_and_ordered():
    mech_k = MECHANISMS.get("狂暴·标记")
    mech_j = MECHANISMS.get("畸变·标记")
    assert mech_k is not None and mech_k.when.matches_phase(Phase.ROUND_START)
    assert mech_j is not None and mech_j.when.matches_phase(Phase.ROUND_START)
    assert mech_k.priority == 50 and mech_j.priority == 60
    from engine.mechanisms.registry import MECHANISMS as REG
    assert [m.name for m in REG.phase_mechanisms(Phase.ROUND_START)] == \
        ["自愈", "衰败", "洞察·结算", "勾魂", "狂暴·标记", "畸变·标记"]


def test_old_marker_blocks_removed():
    assert '"type": "extra_attack_ready"' not in COMBAT_SOURCE
    assert '"type": "deform_pending"' not in COMBAT_SOURCE
    assert check_migrated_mechanism_guards() == []


# ==================== 2. 狂暴·标记 ====================

def test_kuangbao_marker_shapes_and_conditions():
    state, combat, player, ent = _arena(statuses=(("狂暴", 3),))
    results = _phase_results(combat, ent)
    entry = _entry(results, "extra_attack_ready")
    assert entry == {"type": "extra_attack_ready", "entity": "E",
                     "note": "该实体本回合有一次额外攻击机会"}
    # 数值层数不影响标记（旧块只看状态存在性）
    state, combat, player, ent = _arena(statuses=(("狂暴", 9),))
    assert _entry(_phase_results(combat, ent), "extra_attack_ready")["entity"] == "E"

    # 无状态 → 无条目
    state, combat, player, ent = _arena()
    assert _entry(_phase_results(combat, ent), "extra_attack_ready") is None


def test_kuangbao_marker_does_not_touch_state():
    state, combat, player, ent = _arena(statuses=(("狂暴", 3),))
    before = (ent.current_hp, ent.current_mana, ent.attack_count, ent.attack_power)
    _phase_results(combat, ent)
    assert (ent.current_hp, ent.current_mana, ent.attack_count, ent.attack_power) == before


# ==================== 3. 畸变·标记 ====================

def test_jibian_marker_shapes_and_raw_product():
    """blood_loss 是原始乘积（非结算块的 max(0,...)），逐字复刻旧块。"""
    state, combat, player, ent = _arena(ac=2, ap=3, statuses=(("畸变", 1),))
    results = _phase_results(combat, ent)
    entry = _entry(results, "deform_pending")
    assert entry == {"type": "deform_pending", "entity": "E",
                     "blood_loss": 6, "note": "回终结算"}

    # 面板 0：blood_loss=0（原始乘积，无封底）
    state, combat, player, ent = _arena(ac=0, ap=5, statuses=(("畸变", 2),))
    assert _entry(_phase_results(combat, ent), "deform_pending")["blood_loss"] == 0

    # 无状态 → 无条目
    state, combat, player, ent = _arena(ac=2, ap=3)
    assert _entry(_phase_results(combat, ent), "deform_pending") is None


def test_jibian_marker_does_not_change_blood_limit():
    """标记只是预告；真实扣血限发生在 ROUND_END 的【畸变·结算】。"""
    state, combat, player, ent = _arena(ac=2, ap=3, statuses=(("畸变", 1),))
    results = _phase_results(combat, ent)
    assert _entry(results, "deform_pending") is not None
    assert ent.blood_limit == 50, "标记块不得扣血限"

    # 回终结算仍照常工作（两机制互不干扰）
    res = combat.round_end()
    assert any(e.get("type") == "deform_blood_limit_loss" for e in res["effects"])
    assert ent.blood_limit == 44


# ==================== 4. 顺序 / 只触发一次 ====================

def test_markers_order_in_round_start():
    """勾魂(40) → 狂暴·标记(50) → 畸变·标记(60)：旧循环末尾顺序不变。"""
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=30, speed_limit=10, current_speed=5)
    player.add_status(StatusEffect(name="勾魂", remaining_rounds=-1, value=2, source="x"))
    player.add_status(StatusEffect(name="狂暴", remaining_rounds=-1, value=1, source="x"))
    player.add_status(StatusEffect(name="畸变", remaining_rounds=-1, value=1, source="x"))
    player.attack_count, player.attack_power = 1, 4
    state.player = player
    state.enemies = []
    combat = CombatEngine(state, DiceEngine())
    res = combat.round_start()
    types = [e.get("type") for e in res["effects"] if e.get("entity") == "P"]
    assert types.index("gouhun_mana") < types.index("extra_attack_ready") < \
        types.index("deform_pending"), f"顺序异常: {types}"


def test_markers_execute_exactly_once():
    state, combat, player, ent = _arena(ac=2, ap=3,
                                        statuses=(("狂暴", 1), ("畸变", 1)))
    results = _phase_results(combat, ent)
    assert sum(1 for r in results
               if isinstance(r, dict) and r.get("type") == "extra_attack_ready") == 1
    assert sum(1 for r in results
               if isinstance(r, dict) and r.get("type") == "deform_pending") == 1


# ==================== 5. 参考实现 sweep ====================

def test_markers_reference_sweep_zero_mismatch():
    """旧标记块 vs 新机制：状态有无×攻击面板 全场景逐结果一致。"""
    def old_markers(entity):
        entries = []
        if entity.has_status("狂暴"):
            entries.append({"type": "extra_attack_ready", "entity": entity.name,
                            "note": "该实体本回合有一次额外攻击机会"})
        if entity.has_status("畸变"):
            x = entity.get_status_value("畸变")
            blood_loss = entity.attack_count * entity.attack_power
            entries.append({"type": "deform_pending", "entity": entity.name,
                            "blood_loss": blood_loss, "note": "回终结算"})
        return entries

    mismatches = []
    total = 0
    for kuang, jibian, (ac, ap) in itertools.product(
            [False, True], [False, True], [(0, 0), (2, 3), (5, 1)]):
        total += 1
        statuses = []
        if kuang:
            statuses.append(("狂暴", 2))
        if jibian:
            statuses.append(("畸变", 3))
        state_a, combat_a, _, ent_a = _arena(ac=ac, ap=ap, statuses=statuses)
        state_b, combat_b, _, ent_b = _arena(ac=ac, ap=ap, statuses=statuses)

        old = old_markers(ent_a)
        new = [r for r in _phase_results(combat_b, ent_b) if isinstance(r, dict)]
        if old != new:
            mismatches.append((kuang, jibian, ac, ap, old, new))
        if (ent_a.blood_limit, ent_a.current_hp) != (ent_b.blood_limit, ent_b.current_hp):
            mismatches.append(("state", kuang, jibian, ac, ap))

    assert not mismatches, f"{total} 组场景出现 {len(mismatches)} 组差异: {mismatches[:3]}"


def test_markers_registry_unique():
    from engine.mechanisms.registry import MECHANISMS as REG
    assert len([m for m in REG.all() if m.name == "狂暴·标记"]) == 1
    assert len([m for m in REG.all() if m.name == "畸变·标记"]) == 1
