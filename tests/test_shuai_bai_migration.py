"""【衰败】迁移验证：round_start 内嵌块 → ROUND_START 相位 Mechanism。

本阶段验证目标：
  - 衰败是普通声明：ROUND_START + SELF + has_status/is_alive 条件 + damage 动词；
  - 零新抽象、零管线改动（分发点沿用既有 ROUND_START 锚点）；
  - 自愈(10) → 衰败(20) → 洞察/勾魂/... 顺序与迁移前完全一致；
  - 伤害走完整管线（格挡/加减区/濒死/死后效果原样生效）。
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
from engine.mechanisms import MECHANISMS, Phase
from engine.models import Entity, GameState, StatusEffect
from engine.validator import check_migrated_mechanism_guards

ROOT = Path(__file__).resolve().parents[1]
COMBAT_SOURCE = (ROOT / "engine" / "combat.py").read_text(encoding="utf-8")


def _arena(player_hp: int = 100, player_bl: int | None = None,
           enemy_hp: int = 100, enemy_bl: int | None = None):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=player_bl if player_bl is not None else player_hp,
                    current_hp=player_hp, mana_limit=50, current_mana=50,
                    speed_limit=10, current_speed=5)
    enemy = Entity("M", "怪物", blood_limit=enemy_bl if enemy_bl is not None else enemy_hp,
                   current_hp=enemy_hp)
    state.player = player
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def _events(combat, event_type):
    return [e for e in combat.event_stream if e.event_type == event_type]


def _give_shuaibai(entity, value, source="x"):
    entity.add_status(StatusEffect(name="衰败", remaining_rounds=-1, value=value, source=source))


def _entry(entries, entity_name, entry_type="shuaibai_tick"):
    return next((e for e in entries if e.get("type") == entry_type
                 and e.get("entity") == entity_name), None)


class _OldShuaibaiReference:
    """round_start 衰败旧块的逐行复刻（仅测试对照用，非生产代码）。"""

    def run(self, combat, entity):
        if entity.has_status("衰败") and entity.is_alive:
            xv = entity.get_status_value("衰败")
            dmg_n = math.ceil(entity.current_hp * 10 * xv / 100)
            if dmg_n > 0:
                source_name = next((status.source for status in entity.status_effects
                                    if status.name == "衰败"), "")
                source_entity = combat._find_named(source_name)
                rd = combat._apply_hostile_damage(entity, dmg_n, source=source_entity, ctx={
                    "timing": "round_start", "source": "衰败", "source_type": "daowen",
                    "actor": source_entity, "target": entity, "mechanic": "damage",
                    "subtype": "dot", "amount": dmg_n,
                    "tags": {"daowen", "round_start"},
                })
                return {"type": "shuaibai_tick", "entity": entity.name,
                        "damage": rd["actual_damage"], "died": rd["died"]}
        return None


def _run_new_mechanism(combat, entity):
    results = combat._dispatch_phase(Phase.ROUND_START, target=entity)
    return next((r for r in results if isinstance(r, dict)
                 and r.get("type") == "shuaibai_tick"), None)


def _stable(entry):
    if entry is None:
        return None
    return (entry.get("type"), entry.get("entity"),
            entry.get("damage"), entry.get("died"))


# ==================== 1. 注册 / 声明形态 ====================

def test_shuaibai_is_registered_mechanism():
    mech = MECHANISMS.get("衰败")
    assert mech is not None
    assert mech.when.matches_phase(Phase.ROUND_START)
    assert mech.priority == 20, "旧位置=回始效果循环第二位（自愈10之后、洞察之前）"
    from engine.mechanisms.registry import MECHANISMS as REG
    assert [m.name for m in REG.phase_mechanisms(Phase.ROUND_START)] == ["自愈", "衰败"]


def test_old_shuaibai_if_removed_from_pipeline():
    assert 'has_status("衰败")' not in COMBAT_SOURCE, "核心管线不得残留衰败专用分支"
    assert check_migrated_mechanism_guards() == []


# ==================== 2. 触发 / 数值 ====================

def test_shuaibai_no_status_no_trigger():
    state, combat, player, enemy = _arena(enemy_hp=50)
    res = combat.round_start()
    assert _entry(res["effects"], "M") is None and _entry(res["effects"], "P") is None
    assert enemy.current_hp == 50 and player.current_hp == 100
    assert _events(combat, CombatEventType.DAMAGE_APPLIED) == []


def test_shuaibai_normal_trigger():
    state, combat, player, enemy = _arena(enemy_hp=100)
    _give_shuaibai(enemy, 2)   # ceil(100×20/100)=20
    res = combat.round_start()
    entry = _entry(res["effects"], "M")
    assert entry == {"type": "shuaibai_tick", "entity": "M", "damage": 20, "died": False}
    assert enemy.current_hp == 80
    assert player.current_hp == 100, "无衰败者不受影响"


def test_shuaibai_layers_and_hp():
    """层数 × 当前生命：ceil(当前生命 × 10X / 100)。"""
    cases = [
        (1, 100, 10, 90, 10),
        (3, 100, 30, 70, 30),
        (2, 50, 10, 40, 10),
        (5, 30, 15, 15, 15),
        (1, 5, 1, 4, 1),     # ceil(5×10/100)=1
    ]
    for layers, hp, dmg, hp_after, entry_damage in cases:
        state, combat, player, enemy = _arena(enemy_hp=hp)
        _give_shuaibai(enemy, layers)
        res = combat.round_start()
        entry = _entry(res["effects"], "M")
        assert entry is not None
        assert entry["damage"] == entry_damage and entry["died"] is False
        assert enemy.current_hp == hp_after, f"layers={layers} hp={hp}"


def test_shuaibai_value_zero_no_entry():
    state, combat, player, enemy = _arena(enemy_hp=50)
    _give_shuaibai(enemy, 0)   # dmg_n=0 → 与旧代码一致：无伤害无条目
    res = combat.round_start()
    assert _entry(res["effects"], "M") is None
    assert enemy.current_hp == 50


def test_shuaibai_dead_entity_skipped():
    state, combat, player, enemy = _arena(enemy_hp=50)
    _give_shuaibai(enemy, 3)
    enemy.is_alive = False     # 条件含 is_alive：死者不结算
    results = combat._dispatch_phase(Phase.ROUND_START, target=enemy)
    assert results == []


def test_shuaibai_huaisi_does_not_block():
    """坏死禁疗，不影响衰败伤害（规则对照场景）。"""
    state, combat, player, enemy = _arena(enemy_hp=100)
    _give_shuaibai(enemy, 1)
    enemy.add_status(StatusEffect(name="坏死", remaining_rounds=-1, value=1, source="x"))
    res = combat.round_start()
    entry = _entry(res["effects"], "M")
    assert entry is not None and entry["damage"] == 10
    assert enemy.current_hp == 90


# ==================== 3. 完整伤害管线 / 死亡 ====================

def test_shuaibai_goes_through_full_damage_pipeline_shield():
    """伤害走完整管线：格挡照常吸收（与旧实现一致）。"""
    state, combat, player, enemy = _arena(enemy_hp=100)
    _give_shuaibai(enemy, 2)   # 20 伤
    enemy.shield = 7
    res = combat.round_start()
    entry = _entry(res["effects"], "M")
    assert entry["damage"] == 13
    assert enemy.current_hp == 87 and enemy.shield == 0


def test_shuaibai_death_scenario():
    """衰败致死：走统一死亡管线（ENTITY_DIED、死亡 ctx 来源=衰败）。"""
    state, combat, player, enemy = _arena(enemy_hp=1)
    _give_shuaibai(enemy, 5)   # ceil(1×50/100)=1 → 命零
    res = combat.round_start()
    entry = _entry(res["effects"], "M")
    assert entry is not None and entry["died"] is True and entry["damage"] == 1
    assert enemy.is_alive is False
    died = _events(combat, CombatEventType.ENTITY_DIED)
    assert len(died) == 1
    assert died[0].ctx["source"] == "衰败"
    # 死亡 ctx 的 subtype 统一为 hp_zero（父事件链指向伤害事件：subtype=dot）
    assert died[0].ctx["subtype"] == "hp_zero"
    assert died[0].ctx["parent_event_id"] is not None


# ==================== 4. EffectContext / CombatEvent ====================

def test_shuaibai_effect_context():
    state, combat, player, enemy = _arena(enemy_hp=100)
    _give_shuaibai(enemy, 1, source="P")   # 来源=玩家名字 → actor=玩家
    res = combat.round_start()
    entry = _entry(res["effects"], "M")
    assert entry is not None and entry["damage"] == 10

    damage_events = _events(combat, CombatEventType.DAMAGE_APPLIED)
    assert len(damage_events) == 1
    assert damage_events[0].ctx["source"] == "衰败"
    assert damage_events[0].ctx["source_type"] == "daowen"
    assert damage_events[0].ctx["mechanic"] == "damage"
    assert damage_events[0].ctx["subtype"] == "dot"
    assert damage_events[0].ctx["actor"] == "P", "来源按状态 source 名字解析"
    assert set(damage_events[0].ctx["tags"]) == {"daowen", "round_start"}


def test_shuaibai_unknown_source_actor_none():
    state, combat, player, enemy = _arena(enemy_hp=100)
    _give_shuaibai(enemy, 1, source="不存在的人")
    combat.round_start()
    damage_events = _events(combat, CombatEventType.DAMAGE_APPLIED)
    assert damage_events[0].ctx["actor"] is None


# ==================== 5. 顺序 / 组合 / 只触发一次 ====================

def test_ziyu_then_shuaibai_order():
    """自愈(10) → 衰败(20)：effects 顺序与事件顺序均与迁移前一致。"""
    state, combat, player, enemy = _arena(enemy_hp=50, enemy_bl=100)
    enemy.add_status(StatusEffect(name="自愈", remaining_rounds=-1, value=1, source="x"))
    _give_shuaibai(enemy, 2)
    res = combat.round_start()

    types = [e.get("type") for e in res["effects"] if e.get("entity") == "M"]
    assert types.index("self_heal") < types.index("shuaibai_tick"), \
        "自愈必须先于衰败结算"

    # 数值链：50 + 10(自愈) = 60；ceil(60×20/100)=12 → 48
    assert enemy.current_hp == 48, f"实际 {enemy.current_hp}"

    event_types = [e.event_type for e in combat.event_stream
                   if e.target_name == "M" or e.actor_name == "M"]
    assert event_types[0] == CombatEventType.HEAL_APPLIED
    assert event_types[1] == CombatEventType.DAMAGE_APPLIED, \
        "事件顺序必须与迁移前一致（先治疗事件后伤害事件）"


def test_shuaibai_before_dongcha_gouhun():
    """衰败之后仍有洞察/勾魂硬编码块：条目顺序不变。"""
    state, combat, player, enemy = _arena(enemy_hp=100)
    _give_shuaibai(player, 1)
    player._dongcha_pending = 5
    player.add_status(StatusEffect(name="勾魂", remaining_rounds=-1, value=3, source="x"))
    res = combat.round_start()
    types = [e.get("type") for e in res["effects"] if e.get("entity") == "P"]
    assert "shuaibai_tick" in types and "dongcha_mana" in types and "gouhun_mana" in types
    assert types.index("shuaibai_tick") < types.index("dongcha_mana") < types.index("gouhun_mana"), \
        f"顺序异常: {types}"


def test_shuaibai_executes_exactly_once():
    state, combat, player, enemy = _arena(enemy_hp=100)
    _give_shuaibai(enemy, 2)
    combat.round_start()
    assert enemy.current_hp == 80, "若重复触发会得到 60（80-20）"
    entries = [e for e in combat.round_start()["effects"]
               if e.get("type") == "shuaibai_tick" and e.get("entity") == "M"]
    assert len(entries) == 1, "每回合每实体恰好一条 shuaibai_tick"


# ==================== 6. 迁移前后参考实现 sweep ====================

def test_shuaibai_reference_sweep_zero_mismatch():
    """旧衰败块 vs 新 Mechanism：层数×生命×格挡×来源 全场景逐结果一致。"""
    old = _OldShuaibaiReference()
    mismatches = []
    total = 0
    for layers, hp, shield, source in itertools.product(
            [None, 1, 2, 5], [1, 5, 50, 100], [0, 7, 100], ["", "x", "P"]):
        total += 1
        state_a, combat_a, _, enemy_a = _arena(enemy_hp=hp)
        state_b, combat_b, _, enemy_b = _arena(enemy_hp=hp)
        for ent in (enemy_a, enemy_b):
            if layers is not None:
                _give_shuaibai(ent, layers, source=source)
            ent.shield = shield

        entry_a = old.run(combat_a, enemy_a)
        entry_b = _run_new_mechanism(combat_b, enemy_b)

        if _stable(entry_a) != _stable(entry_b):
            mismatches.append(("entry", layers, hp, shield, source,
                               _stable(entry_a), _stable(entry_b)))
        if enemy_a.current_hp != enemy_b.current_hp or enemy_a.shield != enemy_b.shield:
            mismatches.append(("panel", layers, hp, shield, source,
                               (enemy_a.current_hp, enemy_a.shield),
                               (enemy_b.current_hp, enemy_b.shield)))
        if enemy_a.is_alive != enemy_b.is_alive:
            mismatches.append(("alive", layers, hp, shield, source))
        if len(_events(combat_a, CombatEventType.DAMAGE_APPLIED)) != \
                len(_events(combat_b, CombatEventType.DAMAGE_APPLIED)):
            mismatches.append(("events", layers, hp, shield, source))

    assert not mismatches, f"{total} 组场景出现 {len(mismatches)} 组差异: {mismatches[:3]}"


def test_shuaibai_reference_combo_with_longlin():
    """关键组合：衰败 + 龙鳞（伤害管线加减区参与），旧块 vs 新机制一致。"""
    old = _OldShuaibaiReference()
    state_a, combat_a, _, enemy_a = _arena(enemy_hp=60)
    state_b, combat_b, _, enemy_b = _arena(enemy_hp=60)
    for ent in (enemy_a, enemy_b):
        _give_shuaibai(ent, 2)   # ceil(60×20/100)=12，龙鳞5 → 7
        ent.add_status(StatusEffect(name="龙鳞", remaining_rounds=-1, value=5, source="x"))

    entry_a = old.run(combat_a, enemy_a)
    entry_b = _run_new_mechanism(combat_b, enemy_b)

    assert _stable(entry_a) == _stable(entry_b)
    assert entry_b["damage"] == 7, "衰败伤害必须经过龙鳞减免（完整管线）"
    assert enemy_a.current_hp == enemy_b.current_hp
    assert enemy_a.shield == enemy_b.shield


# ==================== 7. 生产扫描 ====================

def test_shuaibai_single_registration_and_no_special_api():
    """衰败只有一个注册点、一条执行路径、无专用 API。"""
    from engine.mechanisms.registry import MECHANISMS as REG
    shuaibai_mechs = [m for m in REG.all() if m.name == "衰败"]
    assert len(shuaibai_mechs) == 1

    import engine.mechanisms.builtins as builtins_module
    source = Path(builtins_module.__file__).read_text(encoding="utf-8")
    assert source.count('MECHANISMS.register(SHUAIBAI)') == 1

    # 无专用 Verb / Condition / Target / dispatch
    from engine.mechanisms import verb_names
    assert not any("shuai" in v or "decay" in v for v in verb_names())
    assert 'def decay' not in COMBAT_SOURCE and 'def shuaibai' not in COMBAT_SOURCE
