"""【勾魂】迁移验证：round_start 内嵌块 → ROUND_START 相位 Mechanism（priority 40）。

验证点：
  - mana 动词（统一法力入口）逐字覆盖旧勾魂语义：lost=min(当前法力, 层数)、下限0；
  - 条件：has_status(勾魂) 且 轮回者 且 存活；
  - 顺序：自愈(10) → 衰败(20) → 洞察·结算(30) → 勾魂(40) → 狂暴·标记（尚未迁移，硬编码块在其后）。
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


def _arena(mana=20, entity_type="轮回者", alive=True, gouhun_value=None):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=0, speed_limit=10, current_speed=5)
    ent = Entity("E", entity_type, blood_limit=50, current_hp=50,
                 mana_limit=50, current_mana=mana)
    if gouhun_value is not None:
        ent.add_status(StatusEffect(name="勾魂", remaining_rounds=-1,
                                    value=gouhun_value, source="x"))
    ent.is_alive = alive
    state.player = player
    state.enemies = [ent]
    return state, CombatEngine(state, DiceEngine()), player, ent


def _old_gouhun(combat, entity):
    """round_start 勾魂旧块的逐行复刻（仅测试对照用，非生产代码）。"""
    if entity.has_status("勾魂") and entity.entity_type == "轮回者" and entity.is_alive:
        drain = entity.get_status_value("勾魂")
        lost = min(entity.current_mana, drain)
        entity.current_mana -= lost
        return {"type": "gouhun_mana", "entity": entity.name, "lost": lost}
    return None


def _run_new(combat, entity):
    results = combat._dispatch_phase(Phase.ROUND_START, target=entity)
    return next((r for r in results if isinstance(r, dict)
                 and r.get("type") == "gouhun_mana"), None)


# ==================== 1. 注册 / 旧实现删除 ====================

def test_gouhun_is_registered():
    mech = MECHANISMS.get("勾魂")
    assert mech is not None and mech.when.matches_phase(Phase.ROUND_START)
    assert mech.priority == 40
    from engine.mechanisms.registry import MECHANISMS as REG
    assert [m.name for m in REG.phase_mechanisms(Phase.ROUND_START)] == \
        ["自愈", "衰败", "洞察·结算", "勾魂", "狂暴·标记", "畸变·标记"]


def test_old_gouhun_block_removed():
    assert '"type": "gouhun_mana"' not in COMBAT_SOURCE, "核心管线不得残留旧勾魂条目代码"
    assert check_migrated_mechanism_guards() == []


# ==================== 2. 触发语义 ====================

def test_gouhun_normal():
    state, combat, player, ent = _arena(mana=20, gouhun_value=7)
    results = combat._dispatch_phase(Phase.ROUND_START, target=ent)
    entries = [r for r in results if isinstance(r, dict) and r.get("type") == "gouhun_mana"]
    assert entries == [{"type": "gouhun_mana", "entity": "E", "lost": 7}]
    assert ent.current_mana == 13


def test_gouhun_floor_zero_and_zero_entry():
    """法力不足：lost 封底为当前法力；层数0/法力0：照常产生条目（lost=0）。"""
    state, combat, player, ent = _arena(mana=3, gouhun_value=10)
    entry = _run_new(combat, ent)
    assert entry["lost"] == 3 and ent.current_mana == 0

    state, combat, player, ent = _arena(mana=5, gouhun_value=0)
    entry = _run_new(combat, ent)
    assert entry == {"type": "gouhun_mana", "entity": "E", "lost": 0}
    assert ent.current_mana == 5, "旧块语义：条目照常产生"


def test_gouhun_no_status_no_entry():
    state, combat, player, ent = _arena(mana=20)
    assert _run_new(combat, ent) is None
    assert ent.current_mana == 20


def test_gouhun_skips_non_reincarnator_and_dead():
    state, combat, player, ent = _arena(mana=20, entity_type="怪物", gouhun_value=5)
    assert _run_new(combat, ent) is None and ent.current_mana == 20

    state, combat, player, ent = _arena(mana=20, gouhun_value=5, alive=False)
    assert _run_new(combat, ent) is None and ent.current_mana == 20


def test_gouhun_multi_source_status_sums():
    state, combat, player, ent = _arena(mana=50)
    ent.add_status(StatusEffect(name="勾魂", remaining_rounds=-1, value=4, source="a"))
    ent.add_status(StatusEffect(name="勾魂", remaining_rounds=-1, value=6, source="b"))
    entry = _run_new(combat, ent)
    assert entry["lost"] == 10 and ent.current_mana == 40


# ==================== 3. 顺序 / 只触发一次 ====================

def test_gouhun_order_after_dongcha():
    """洞察·结算(30) → 勾魂(40)：同实体条目顺序不变。"""
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=30, speed_limit=10, current_speed=5)
    player._dongcha_pending = 10
    player.add_status(StatusEffect(name="勾魂", remaining_rounds=-1, value=5, source="x"))
    state.player = player
    state.enemies = []
    combat = CombatEngine(state, DiceEngine())
    res = combat.round_start()
    types = [e.get("type") for e in res["effects"] if e.get("entity") == "P"]
    assert types.index("dongcha_mana") < types.index("gouhun_mana"), \
        "洞察 必须先于 勾魂（旧循环顺序）"
    # 数值链：30 + 50(回始补法) = 80 + 10(洞察) = 90；min(90, 5) = 5 → 85
    assert player.current_mana == 85, f"实际 {player.current_mana}"


def test_gouhun_executes_exactly_once():
    state, combat, player, ent = _arena(mana=20, gouhun_value=7)
    results = combat._dispatch_phase(Phase.ROUND_START, target=ent)
    assert sum(1 for r in results
               if isinstance(r, dict) and r.get("type") == "gouhun_mana") == 1
    assert ent.current_mana == 13, "若双触发会得到 6（13-7）"


# ==================== 4. 参考实现 sweep ====================

def test_gouhun_reference_sweep_zero_mismatch():
    """旧勾魂块 vs 新机制：层数×实体类型×存活×当前法力 全场景逐结果一致。"""
    mismatches = []
    total = 0
    for value, etype, alive, mana in itertools.product(
            [None, 0, 5, 10], ["轮回者", "怪物"], [False, True], [0, 3, 20, 50]):
        total += 1
        state_a, combat_a, _, ent_a = _arena(mana=mana, entity_type=etype,
                                             alive=alive, gouhun_value=value)
        state_b, combat_b, _, ent_b = _arena(mana=mana, entity_type=etype,
                                             alive=alive, gouhun_value=value)
        entry_a = _old_gouhun(combat_a, ent_a)
        entry_b = _run_new(combat_b, ent_b)
        if entry_a != entry_b:
            mismatches.append(("entry", value, etype, alive, mana, entry_a, entry_b))
        if ent_a.current_mana != ent_b.current_mana:
            mismatches.append(("mana", value, etype, alive, mana,
                               ent_a.current_mana, ent_b.current_mana))

    assert not mismatches, f"{total} 组场景出现 {len(mismatches)} 组差异: {mismatches[:3]}"


def test_gouhun_registry_unique_and_no_special_api():
    from engine.mechanisms.registry import MECHANISMS as REG
    assert len([m for m in REG.all() if m.name == "勾魂"]) == 1
    from engine.mechanisms import verb_names
    assert not any("gouhun" in v for v in verb_names())
