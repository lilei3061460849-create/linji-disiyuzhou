"""【缄默面具】迁移验证：process_relics 战始 if → BATTLE_START 相位 Mechanism。

验证点：
  - 经 mana 动词获得 20X 法力（含不朽之躯钳制）；X=0 仍钳制（旧块无条件 clamp）；
  - 顺序：缄默面具(5) → 帮派令(10)，同相位按 priority 保持原序（旧块紧邻分发点之前）；
  - 封印（抵扣X）不触发；【禁代价】是 api.py 的静态校验规则，不在本机制范围。
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
from engine.models import Entity, GameState, Relic
from engine.validator import check_migrated_mechanism_guards

ROOT = Path(__file__).resolve().parents[1]
COMBAT_SOURCE = (ROOT / "engine" / "combat.py").read_text(encoding="utf-8")


def _arena(mana=0, mana_limit=50, relics=(), sealed=None, x=None):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=mana_limit, current_mana=mana,
                    speed_limit=10, current_speed=5)
    state.player = player
    state.enemies = [Entity("M", "怪物", blood_limit=50, current_hp=50)]
    state.relics = [Relic(name, "") for name in relics]
    if sealed:
        for name, rounds in sealed.items():
            state.sealed_relics[name] = rounds
    if x is not None:
        state.event_modifiers["silent_mask_x"] = x
    return state, CombatEngine(state, DiceEngine()), player


def _old_silent_mask(combat, player):
    """process_relics 缄默面具旧块的逐行复刻（仅测试对照用，非生产代码）。"""
    state = combat.state
    relics = {r.name for r in state.relics if state.sealed_relics.get(r.name, 0) <= 0}
    if "缄默面具" in relics:
        x = state.event_modifiers.get("silent_mask_x", 0)
        player.current_mana += 20 * x
        combat.clamp_immortal_body(player)
        return f"缄默面具：+{20*x}法力"
    return None


def _run_new(combat, player):
    results = combat._dispatch_phase(Phase.BATTLE_START, target=player)
    return results[0] if results else None


# ==================== 1. 注册 / 旧实现删除 ====================

def test_silent_mask_registered_and_ordered():
    mech = MECHANISMS.get("缄默面具")
    assert mech is not None and mech.when.matches_phase(Phase.BATTLE_START)
    assert mech.priority == 5
    from engine.mechanisms.registry import MECHANISMS as REG
    assert [m.name for m in REG.phase_mechanisms(Phase.BATTLE_START)] == \
        ["缄默面具", "帮派令"]


def test_old_silent_mask_block_removed():
    assert 'if "缄默面具" in relics' not in COMBAT_SOURCE, "旧缄默面具块必须删除"
    assert check_migrated_mechanism_guards() == []


# ==================== 2. 触发语义 ====================

def test_silent_mask_normal():
    state, combat, player = _arena(relics=("缄默面具",), x=2)
    results = combat._dispatch_phase(Phase.BATTLE_START, target=player)
    assert results == ["缄默面具：+40法力"]
    assert player.current_mana == 40


def test_silent_mask_x_zero_still_logs_and_clamps():
    """X=0：旧块 +=0 后无条件 clamp 且照常产生日志——逐字保持。"""
    state, combat, player = _arena(mana=70, mana_limit=50,
                                   relics=("缄默面具", "不朽之躯"), x=0)
    results = combat._dispatch_phase(Phase.BATTLE_START, target=player)
    assert results == ["缄默面具：+0法力"]
    assert player.current_mana == 50, "X=0 时旧块仍执行不朽之躯钳制"


def test_silent_mask_immortal_clamp_on_gain():
    state, combat, player = _arena(mana=40, mana_limit=50,
                                   relics=("缄默面具", "不朽之躯"), x=1)
    combat._dispatch_phase(Phase.BATTLE_START, target=player)
    assert player.current_mana == 50, "获得 20 后被不朽之躯钳制到法限"


def test_silent_mask_not_held_no_entry():
    state, combat, player = _arena(x=2)
    assert _run_new(combat, player) is None
    assert player.current_mana == 0


def test_silent_mask_sealed_no_entry():
    state, combat, player = _arena(relics=("缄默面具",), x=2,
                                   sealed={"缄默面具": 2})
    assert _run_new(combat, player) is None
    assert player.current_mana == 0


# ==================== 3. 顺序 / 只触发一次 ====================

def test_silent_mask_before_gangpailing_in_full_process_relics():
    """完整战始路径：缄默面具(5) → 帮派令(10)，日志与状态顺序与迁移前一致。"""
    state, combat, player = _arena(relics=("缄默面具", "帮派令"), x=2)
    logs = combat.process_relics("battle_start", {"relic_choices": {}})
    assert logs == ["缄默面具：+40法力", "帮派令：获得洗劫3"]
    assert player.current_mana == 40
    assert player.has_status("洗劫") and player.get_status_value("洗劫") == 3


def test_silent_mask_executes_exactly_once():
    state, combat, player = _arena(relics=("缄默面具",), x=2)
    combat.process_relics("battle_start", {"relic_choices": {}})
    assert player.current_mana == 40, "若双触发会得到 80"


# ==================== 4. 参考实现 sweep ====================

def test_silent_mask_reference_sweep_zero_mismatch():
    """旧块 vs 新机制：持有×封印×X×不朽 全场景逐结果一致。"""
    mismatches = []
    total = 0
    for held, sealed, x, immortal in itertools.product(
            [False, True], [False, True], [0, 1, 2], [False, True]):
        total += 1
        relics_a = ([r for r in (["缄默面具"] if held else [])
                     + (["不朽之躯"] if immortal else [])])
        state_a, combat_a, player_a = _arena(mana=40, mana_limit=50,
                                             relics=tuple(relics_a),
                                             sealed={"缄默面具": 2} if sealed else None,
                                             x=x)
        state_b, combat_b, player_b = _arena(mana=40, mana_limit=50,
                                             relics=tuple(relics_a),
                                             sealed={"缄默面具": 2} if sealed else None,
                                             x=x)
        old_result = _old_silent_mask(combat_a, player_a)
        new_result = _run_new(combat_b, player_b)
        if old_result != new_result:
            mismatches.append(("result", held, sealed, x, immortal, old_result, new_result))
        if player_a.current_mana != player_b.current_mana:
            mismatches.append(("mana", held, sealed, x, immortal,
                               player_a.current_mana, player_b.current_mana))

    assert total == 2 * 2 * 3 * 2
    assert not mismatches, f"{total} 组场景出现 {len(mismatches)} 组差异: {mismatches[:3]}"


def test_silent_mask_registry_unique_and_no_special_api():
    from engine.mechanisms.registry import MECHANISMS as REG
    assert len([m for m in REG.all() if m.name == "缄默面具"]) == 1
    from engine.mechanisms import verb_names
    assert not any("silent" in v or "mask" in v for v in verb_names())
