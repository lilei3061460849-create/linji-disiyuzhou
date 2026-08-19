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


def _give_jibian(entity, value=1, source="x"):
    """与 test_jibian_migration 同形：给实体挂【畸变】状态（回终结算用）。"""
    entity.add_status(StatusEffect(name="畸变", remaining_rounds=1, value=value, source=source))


def _deform_entries(entries, entity_name):
    """回终效果里挑出某实体的畸变·结算条目（与 test_jibian_migration 同形）。"""
    return [e for e in entries if e.get("type") == "deform_blood_limit_loss"
            and e.get("entity") == entity_name]


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
    # 回始相位机制按 priority：自愈(10) → 衰败(20) → 洞察(30) → 后续按 40/50/60 递增
    assert [m.name for m in REG.phase_mechanisms(Phase.ROUND_START)] == \
        ["自愈", "衰败", "洞察·结算", "勾魂", "狂暴·标记", "畸变·标记"]


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


# ==================== 8. 多实体 + 组合边界（最终行为验证阶段新增） ====================

def test_shuaibai_multiple_enemies_different_rates():
    """多个敌人持有不同的衰败层数，各自按各自公式独立结算。"""
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=50, speed_limit=10, current_speed=5)
    e1 = Entity("E1", "怪物", blood_limit=100, current_hp=100)
    e2 = Entity("E2", "怪物", blood_limit=100, current_hp=50)
    e3 = Entity("E3", "怪物", blood_limit=100, current_hp=30)
    _give_shuaibai(e1, 1)  # ceil(100*10/100)=10
    _give_shuaibai(e2, 2)  # ceil(50*20/100)=10
    _give_shuaibai(e3, 5)  # ceil(30*50/100)=15
    state.player = player
    state.enemies = [e1, e2, e3]
    combat = CombatEngine(state, DiceEngine())

    res = combat.round_start()
    entries = {e.get("entity"): e for e in res["effects"] if e.get("type") == "shuaibai_tick"}

    assert entries["E1"]["damage"] == 10
    assert entries["E2"]["damage"] == 10
    assert entries["E3"]["damage"] == 15
    assert e1.current_hp == 90
    assert e2.current_hp == 40
    assert e3.current_hp == 15


def test_shuaibai_kill_triggers_jiaohheifasi_chain():
    """衰败致死 -> ENTITY_DIED -> 焦黑发丝（玩家持有且未封印则+2速度）。

    测试完整事件链：round_start shuaibai_tick -> damage_applied -> entity_died -> 焦黑发丝 speed gain。
    """
    from engine.models import Relic
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=50, speed_limit=20, current_speed=5)
    enemy = Entity("M", "怪物", blood_limit=1, current_hp=1)
    _give_shuaibai(enemy, 5)   # ceil(1*50/100)=1 -> 命零
    state.player = player
    state.enemies = [enemy]
    state.relics = [Relic(name="焦黑发丝", effect="")]
    combat = CombatEngine(state, DiceEngine())
    sp_before = player.current_speed

    res = combat.round_start()
    entry = _entry(res["effects"], "M")
    assert entry is not None and entry["died"] is True

    # 事件流形状必须连续
    stream = combat.event_stream
    types = [e.event_type for e in stream]
    assert CombatEventType.DAMAGE_APPLIED in types, "衰败伤害事件必须发射"
    assert CombatEventType.ENTITY_DIED in types, "死亡事件必须发射"
    assert player.current_speed == sp_before + 2, "焦黑发丝必须触发（速度+2）"


def test_shuaibai_full_round_start_pipeline():
    """全 ROUND_START 管线 6 个机制在同实体上依次触发（顺序即规则）。

    旧代码行为：round_start 逐实体循环依次执行 自愈->衰败->洞察->勾魂->狂暴标记->畸变标记。
    新机制行为必须完全对齐。
    """
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=20, speed_limit=10, current_speed=5)
    entity = Entity("E", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=30, current_mana=20)
    state.player = player
    state.enemies = [entity]
    entity.add_status(StatusEffect(name="自愈", remaining_rounds=-1, value=1, source="x"))
    entity.add_status(StatusEffect(name="衰败", remaining_rounds=-1, value=1, source="x"))
    entity.add_status(StatusEffect(name="勾魂", remaining_rounds=-1, value=3, source="x"))
    entity._dongcha_pending = 5
    entity.add_status(StatusEffect(name="狂暴", remaining_rounds=-1, value=1, source="x"))
    entity.add_status(StatusEffect(name="畸变", remaining_rounds=-1, value=1, source="x"))
    combat = CombatEngine(state, DiceEngine())

    entity.current_hp = 80  # 让自愈效果可用

    # 走生产 round_start() 完整路径：法力回填 -> 遗物 -> ROUND_START 相位分发 -> F2 块
    res = combat.round_start()
    types = [e.get("type") for e in res["effects"] if e.get("entity") == "E"]

    # 六机制条目按旧 cycle 顺序出现（mana_refill 是回始法力回填，在其之前）
    expected = ["mana_refill", "self_heal", "shuaibai_tick", "dongcha_mana",
                "gouhun_mana", "extra_attack_ready", "deform_pending"]
    assert types == expected, f"管道类型顺序: {types}"

    # 数值链：hp 80->+10(自愈)=90->-ceil(90*10/100)=9 -> 81
    assert entity.current_hp == 81, f"实际 hp={entity.current_hp}"

    # mana: 20->+30(回填)=50->+5(洞察)=55->-3(勾魂)=52
    assert entity.current_mana == 52, f"实际 mana={entity.current_mana}"

    # 洞察 pending 清零
    assert getattr(entity, "_dongcha_pending", 0) == 0, "洞察 pending 必须在结算后清零"

    # 只触发一次：再次 round_start 不出现第二次六机制条目（状态仍在但数值不同）
    types2 = [e.get("type") for e in combat.round_start()["effects"] if e.get("entity") == "E"]
    assert types2.count("self_heal") == 1 and types2.count("shuaibai_tick") == 1


def test_shuaibai_with_jibian_scheduled():
    """实体同时有 衰败 和 畸变，不同相位互不干扰。

    ROUND_START 时衰败扣血；ROUND_END 时畸变扣血限。
    """
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=50, speed_limit=10, current_speed=5)
    enemy = Entity("M", "怪物", blood_limit=50, current_hp=50,
                   attack_count=2, attack_power=3)
    _give_shuaibai(enemy, 1)  # ceil(50*10/100)=5
    _give_jibian(enemy)       # max(0, attack_count*attack_power)=6
    state.player = player
    state.enemies = [enemy]
    combat = CombatEngine(state, DiceEngine())

    res_start = combat.round_start()
    assert _entry(res_start["effects"], "M")["damage"] == 5
    assert enemy.current_hp == 45, f"开局扣血后={enemy.current_hp}"

    # 直接回终
    res_end = combat.round_end()
    deform = _deform_entries(res_end["effects"], "M")
    assert len(deform) == 1
    assert deform[0]["blood_loss"] == 6
    assert enemy.blood_limit == 44, f"血限={enemy.blood_limit}"
    assert enemy.current_hp == 44, f"最终hp={enemy.current_hp} (血限下降后 hp 封顶到新血限)"

    # 检查事件流完整性
    damage_events = [e for e in combat.event_stream if e.event_type == CombatEventType.DAMAGE_APPLIED]
    bl_events = [e for e in combat.event_stream if e.event_type == CombatEventType.BLOOD_LIMIT_CHANGED]
    assert len(damage_events) == 1  # 只有衰败的伤害
    assert len(bl_events) == 1      # 只有畸变的血限变化
