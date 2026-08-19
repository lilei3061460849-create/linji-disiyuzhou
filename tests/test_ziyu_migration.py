"""【自愈】迁移验证：round_start 内嵌 if → ROUND_START 相位 Mechanism。

本阶段验证目标（不是简单搬代码）：
  - "回合开始"是普通 Trigger，"自愈"只是 Trigger+Condition+Target+Verb 组合；
  - ROUND_START 相位可容纳多个 Mechanism（dummy 验证），priority 决定顺序；
  - 迁移前后行为逐场景一致（参考实现 sweep）；
  - 回复必须经统一 heal Verb → apply_heal（龙血瓶等副作用原样生效）。
"""
from __future__ import annotations

import itertools
import math
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.combat_events import CombatEventType
from engine.dice import DiceEngine
from engine.mechanisms import MECHANISMS, Phase, Trigger, TriggerContext
from engine.models import Consumable, Entity, GameState, StatusEffect
from engine.validator import check_migrated_mechanism_guards

ROOT = Path(__file__).resolve().parents[1]


def _arena(enemy_hp: int = 100, enemy_bl: int | None = None):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=50, speed_limit=10, current_speed=5)
    enemy = Entity("M", "怪物", blood_limit=enemy_bl if enemy_bl is not None else enemy_hp,
                   current_hp=enemy_hp)
    state.player = player
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def _events(combat, event_type):
    return [e for e in combat.event_stream if e.event_type == event_type]


def _give_ziyu(entity, value, source="x"):
    entity.add_status(StatusEffect(name="自愈", remaining_rounds=-1, value=value, source=source))


def _give_huaisi(entity, value=1, source="x"):
    entity.add_status(StatusEffect(name="坏死", remaining_rounds=-1, value=value, source=source))


def _ziyu_entry(entries, entity_name):
    return next((e for e in entries if e.get("type") == "self_heal"
                 and e.get("entity") == entity_name), None)


class _OldZiyuReference:
    """round_start 自愈旧块的逐行复刻（仅测试对照用，非生产代码）。"""

    def run(self, state, entity):
        if entity.has_status("自愈") and not entity.has_status("坏死"):
            x = entity.get_status_value("自愈")
            heal_pct = 10 * x
            heal_amount = math.ceil(entity.blood_limit * heal_pct / 100)
            heal_result = state.apply_heal(entity, heal_amount, ctx={
                "timing": "round_start", "source": "自愈", "source_type": "daowen",
                "actor": entity, "target": entity, "owner": entity,
                "mechanic": "heal", "subtype": "self_heal", "amount": heal_amount,
                "tags": {"daowen", "round_start"},
            })
            return {
                "type": "self_heal",
                "entity": entity.name,
                "heal": heal_amount,
                "actual": heal_result["actual_heal"],
                "heal_ctx": heal_result.get("heal_ctx"),
            }
        return None


def _run_new_mechanism(combat, entity):
    """按生产路径执行：CombatEngine._dispatch_phase(ROUND_START, target=entity)。"""
    results = combat._dispatch_phase(Phase.ROUND_START, target=entity)
    return results[0] if results else None


def _stable(entry):
    if entry is None:
        return None
    return (entry.get("type"), entry.get("entity"), entry.get("heal"), entry.get("actual"))


# ==================== 1. 注册 / Trigger ====================

def test_ziyu_is_registered_mechanism():
    mech = MECHANISMS.get("自愈")
    assert mech is not None
    assert mech.when.matches_phase(Phase.ROUND_START)
    assert mech.priority == 10, "旧位置=回始效果循环第一位 → priority 10（钉死）"
    from engine.mechanisms.registry import MECHANISMS as REG
    assert [m.name for m in REG.phase_mechanisms(Phase.ROUND_START)] == ["自愈"]


def test_old_ziyu_if_removed_from_combat_pipeline():
    """旧 if 已从核心管线删除（护栏 + 源码扫描双重钉死）。"""
    assert check_migrated_mechanism_guards() == []
    source = (ROOT / "engine" / "combat.py").read_text(encoding="utf-8")
    assert 'has_status("自愈")' not in source, "combat.py 不得残留自愈硬编码分支"


# ==================== 2. 正常触发 / SELF / heal Verb ====================

def test_ziyu_normal_trigger_through_round_start():
    state, combat, player, enemy = _arena(enemy_hp=50, enemy_bl=100)
    _give_ziyu(enemy, 2)   # 回复 ceil(100 × 10×2 / 100) = 20
    res = combat.round_start()

    entry = _ziyu_entry(res["effects"], "M")
    assert entry is not None
    assert entry["heal"] == 20 and entry["actual"] == 20
    assert enemy.current_hp == 70
    assert player.current_hp == 100, "无自愈者不回复"


def test_ziyu_no_status_no_heal():
    state, combat, player, enemy = _arena(enemy_hp=50)
    res = combat.round_start()
    assert _ziyu_entry(res["effects"], "M") is None
    assert enemy.current_hp == 50
    assert _events(combat, CombatEventType.HEAL_APPLIED) == []


def test_ziyu_huaisi_blocks():
    state, combat, player, enemy = _arena(enemy_hp=50)
    _give_ziyu(enemy, 2)
    _give_huaisi(enemy)
    res = combat.round_start()
    assert _ziyu_entry(res["effects"], "M") is None, "坏死禁疗：不回复也不产生报告条目"
    assert enemy.current_hp == 50
    assert _events(combat, CombatEventType.HEAL_APPLIED) == []


def test_ziyu_stack_sums_and_formula():
    """层数求和 + 公式逐场景：ceil(血限 × 10X / 100)。"""
    cases = [
        (1, 100, 50, 10, 60),
        (3, 100, 50, 30, 80),
        (2, 45, 10, 9, 19),    # ceil(45×20/100)=9
        (3, 30, 1, 9, 10),     # ceil(30×30/100)=9
        (1, 100, 95, 10, 100),  # 溢出截断到血限
    ]
    for layers, bl, hp, heal, hp_after in cases:
        state, combat, player, enemy = _arena(enemy_hp=hp, enemy_bl=bl)
        _give_ziyu(enemy, layers)
        res = combat.round_start()
        entry = _ziyu_entry(res["effects"], "M")
        assert entry["heal"] == heal and entry["actual"] == min(heal, bl - hp)
        assert enemy.current_hp == hp_after, f"layers={layers} bl={bl} hp={hp}"


def test_ziyu_multi_source_status_sums():
    state, combat, player, enemy = _arena(enemy_hp=50, enemy_bl=100)
    _give_ziyu(enemy, 2, source="a")
    _give_ziyu(enemy, 3, source="b")
    combat.round_start()
    assert enemy.current_hp == 100, "X=2+3=5 → 回复 ceil(100×50/100)=50"


def test_ziyu_goes_through_heal_verb_with_dragon_bottle():
    """回复必须经统一 heal 动词：apply_heal 的龙血瓶溢出副作用原样生效。"""
    state, combat, player, enemy = _arena()
    player.current_hp = 90
    _give_ziyu(player, 3)   # 回复 ceil(100×30/100)=30，溢出 20
    state.consumables = [Consumable(name="龙血瓶", effect="", current_uses=5, max_uses=5)]
    combat.round_start()

    assert player.current_hp == 100
    bottle = next(c for c in state.consumables if c.name == "龙血瓶")
    # 初始耐久5 + 溢出20 = 25（apply_heal 的既有副作用原样生效）
    assert bottle.current_uses == 25 and bottle.max_uses == 25, "龙血瓶溢出存储必须与迁移前一致"


# ==================== 3. EffectContext / HEAL_APPLIED ====================

def test_ziyu_effect_context_and_heal_event():
    state, combat, player, enemy = _arena(enemy_hp=50)
    _give_ziyu(enemy, 1)
    res = combat.round_start()
    entry = _ziyu_entry(res["effects"], "M")

    heal_ctx = entry["heal_ctx"]
    assert heal_ctx["source"] == "自愈"
    assert heal_ctx["source_type"] == "daowen"
    assert heal_ctx["mechanic"] == "heal"
    assert heal_ctx["subtype"] == "self_heal"
    assert heal_ctx["timing"] == "round_start"
    assert heal_ctx["actor"] == "M" and heal_ctx["target"] == "M" and heal_ctx["owner"] == "M"
    assert set(heal_ctx["tags"]) == {"daowen", "round_start"}

    heal_events = _events(combat, CombatEventType.HEAL_APPLIED)
    assert len(heal_events) == 1
    assert heal_events[0].ctx["source"] == "自愈"
    assert heal_events[0].ctx["subtype"] == "self_heal"


# ==================== 4. 顺序 / 只触发一次 ====================

def test_ziyu_position_before_shuaibai_in_effects():
    """旧顺序：自愈先于衰败（同实体、同循环）。迁移后 effects 顺序不变。"""
    state, combat, player, enemy = _arena(enemy_hp=50)
    _give_ziyu(enemy, 1)
    _give_huaisi(enemy)  # 衰败不受坏死阻断；自愈被阻断 → 只应出现 shuaibai_tick
    enemy.add_status(StatusEffect(name="衰败", remaining_rounds=-1, value=1, source="x"))
    res = combat.round_start()
    types = [e.get("type") for e in res["effects"] if e.get("entity") == "M"]
    assert "self_heal" not in types and "shuaibai_tick" in types

    # 正常场景：自愈条目必须出现在衰败条目之前
    state, combat, player, enemy = _arena(enemy_hp=50)
    _give_ziyu(enemy, 1)
    enemy.add_status(StatusEffect(name="衰败", remaining_rounds=-1, value=1, source="x"))
    res = combat.round_start()
    types = [e.get("type") for e in res["effects"] if e.get("entity") == "M"]
    assert types.index("self_heal") < types.index("shuaibai_tick"), "自愈必须先于衰败结算"


def test_ziyu_executes_exactly_once():
    state, combat, player, enemy = _arena(enemy_hp=50, enemy_bl=100)
    _give_ziyu(enemy, 2)
    res = combat.round_start()
    entries = [e for e in res["effects"]
               if e.get("type") == "self_heal" and e.get("entity") == "M"]
    assert len(entries) == 1
    assert enemy.current_hp == 70, "若重复治疗会得到 90（70+20）"


# ==================== 5. 多 ROUND_START 机制（dummy 验证可扩展性） ====================

def test_multiple_round_start_mechanisms_priority_and_no_double():
    """用临时 dummy 机制验证：ROUND_START 可容纳多个机制、priority 决定顺序、
    每实体每机制恰好一次、且不改变生产规则。"""
    records = []

    from engine.mechanisms import Mechanism
    dummy_before = Mechanism(
        name="回始测试·前", when=Trigger.phase(Phase.ROUND_START),
        effect=lambda ctx, ts: records.append(("前", ctx.target.name, ctx.target.current_hp)),
        priority=5)
    dummy_after = Mechanism(
        name="回始测试·后", when=Trigger.phase(Phase.ROUND_START),
        effect=lambda ctx, ts: records.append(("后", ctx.target.name, ctx.target.current_hp)),
        priority=20)

    state, combat, player, enemy = _arena(enemy_hp=20, enemy_bl=100)
    _give_ziyu(enemy, 1)   # 回复 ceil(100×10/100)=10 → hp 20→30（自愈 priority=10，位于两个 dummy 之间）

    MECHANISMS.register(dummy_before)
    MECHANISMS.register(dummy_after)
    try:
        res = combat.round_start()
        # 实体池顺序：己方（P）→ 敌方（M）；每个实体内部按 priority：
        # 前(5) → 自愈(10) → 后(20)。敌人 hp 记录 20 → 30，证明 priority 顺序。
        assert records == [("前", "P", 100), ("后", "P", 100),
                           ("前", "M", 20), ("后", "M", 30)], \
            f"priority 顺序或执行次数异常: {records}"
        entry = _ziyu_entry(res["effects"], "M")
        assert entry["actual"] == 10, "dummy 机制不得干扰生产机制"
    finally:
        MECHANISMS.unregister("回始测试·前")
        MECHANISMS.unregister("回始测试·后")

    # 移除 dummy 后：生产结果与从未注册时完全一致（不影响生产规则）
    state2, combat2, player2, enemy2 = _arena(enemy_hp=20, enemy_bl=100)
    _give_ziyu(enemy2, 1)
    res2 = combat2.round_start()
    stable1 = (_ziyu_entry(res["effects"], "M")["heal"],
               _ziyu_entry(res["effects"], "M")["actual"], enemy.current_hp)
    stable2 = (_ziyu_entry(res2["effects"], "M")["heal"],
               _ziyu_entry(res2["effects"], "M")["actual"], enemy2.current_hp)
    assert stable1 == stable2
    assert MECHANISMS.get("回始测试·前") is None and MECHANISMS.get("回始测试·后") is None


# ==================== 6. 迁移前后语义对照 sweep ====================

def test_ziyu_reference_sweep_zero_mismatch():
    """旧自愈块 vs 新 Mechanism：层数×血限×生命×坏死 全场景逐结果一致。"""
    old = _OldZiyuReference()
    mismatches = []
    total = 0
    for layers, bl, hp, huaisi in itertools.product(
            [None, 1, 2, 3], [30, 45, 100], [1, 25, 50, 100], [False, True]):
        total += 1
        state_a, combat_a, _, enemy_a = _arena(enemy_hp=hp, enemy_bl=bl)
        state_b, combat_b, _, enemy_b = _arena(enemy_hp=hp, enemy_bl=bl)
        for ent in (enemy_a, enemy_b):
            if layers is not None:
                _give_ziyu(ent, layers)
            if huaisi:
                _give_huaisi(ent)

        entry_a = old.run(state_a, enemy_a)
        entry_b = _run_new_mechanism(combat_b, enemy_b)

        if _stable(entry_a) != _stable(entry_b):
            mismatches.append((layers, bl, hp, huaisi, _stable(entry_a), _stable(entry_b)))
        if enemy_a.current_hp != enemy_b.current_hp:
            mismatches.append(("hp", layers, bl, hp, huaisi,
                               enemy_a.current_hp, enemy_b.current_hp))
        if len(_events(combat_a, CombatEventType.HEAL_APPLIED)) != \
                len(_events(combat_b, CombatEventType.HEAL_APPLIED)):
            mismatches.append(("heal_events", layers, bl, hp, huaisi))

    assert not mismatches, f"{total} 组场景中出现 {len(mismatches)} 组差异: {mismatches[:3]}"


def test_ziyu_reference_bottle_overflow_matches():
    """玩家侧龙血瓶溢出：旧块 vs 新机制逐结果一致。"""
    old = _OldZiyuReference()
    state_a, combat_a, player_a, _ = _arena()
    state_b, combat_b, player_b, _ = _arena()
    for player, state in ((player_a, state_a), (player_b, state_b)):
        player.current_hp = 90
        _give_ziyu(player, 3)
        state.consumables = [Consumable(name="龙血瓶", effect="")]

    entry_a = old.run(state_a, player_a)
    entry_b = _run_new_mechanism(combat_b, player_b)
    assert _stable(entry_a) == _stable(entry_b)
    bottle_a = next(c for c in state_a.consumables if c.name == "龙血瓶")
    bottle_b = next(c for c in state_b.consumables if c.name == "龙血瓶")
    assert (bottle_a.current_uses, bottle_a.max_uses) == (bottle_b.current_uses, bottle_b.max_uses)


# ==================== 7. 护栏 ====================

def test_migration_guard_detects_planted_ziyu_hardcode(tmp_path):
    planted = tmp_path / "combat.py"
    planted.write_text('if entity.has_status("自愈"):\n    heal()\n', encoding="utf-8")
    violations = check_migrated_mechanism_guards([str(planted)])
    assert len(violations) == 1
    assert violations[0]["context"]["mechanism"] == "自愈"
