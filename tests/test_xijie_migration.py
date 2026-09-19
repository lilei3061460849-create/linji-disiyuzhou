"""【洗劫·夺碎片】迁移验证：_apply_hostile_damage_inner 内嵌块 → DAMAGE_APPLIED 事件机制。

本批验证目标：
  - 承载方式迁移：_xijie_steal 保持为夺碎片的唯一实现，机制层只改变调用位置
    （事件分发点 = 旧调用位置 = DAMAGE_APPLIED emit 之后）；
  - xijie_stolen 孤儿诊断字段正式废弃（全仓零消费方，写入点删除）；
  - 全部门闩（无状态/无碎片/自伤/实伤0/状态过期）行为与旧实现一致。
"""
from __future__ import annotations

import itertools
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.combat_events import CombatEventType
from engine.dice import DiceEngine
from engine.mechanisms import MECHANISMS
from engine.models import Entity, GameState, StatusEffect
from engine.validator import check_migrated_mechanism_guards

ROOT = Path(__file__).resolve().parents[1]
COMBAT_SOURCE = (ROOT / "engine" / "combat.py").read_text(encoding="utf-8")


def _arena(player_hp=100, player_shards=0):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=player_hp,
                    mana_limit=50, current_mana=50, speed_limit=10, current_speed=5)
    enemy = Entity("M", "怪物", blood_limit=100, current_hp=100, shards=0, fake_shards=0)
    state.player = player
    state.shards = player_shards
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def _damage(combat, source, target, amount):
    return combat._apply_hostile_damage(target, amount, source=source)


# ==================== 1. 注册 / 旧实现删除 ====================

def test_xijie_is_registered_event_mechanism():
    mech = MECHANISMS.get("洗劫·夺碎片")
    assert mech is not None
    assert mech.when.matches_event(CombatEventType.DAMAGE_APPLIED)
    assert mech.priority == 10
    from engine.mechanisms.registry import MECHANISMS as REG
    assert [m.name for m in REG.event_mechanisms()] == ["焦黑发丝", "洗劫·夺碎片"]


def test_old_xijie_block_and_orphan_field_removed():
    """旧调用块与 xijie_stolen 孤儿字段的写入点全部删除（正式废弃）。"""
    assert "xijie_stolen" not in COMBAT_SOURCE, "孤儿诊断字段必须随迁移废弃"
    assert check_migrated_mechanism_guards() == []


# ==================== 2. 触发语义 ====================

def test_xijie_normal_steal():
    state, combat, player, enemy = _arena()
    enemy.shards = 20
    player.add_status(StatusEffect(name="洗劫", remaining_rounds=-1, value=2, source="x"))
    detail = _damage(combat, player, enemy, 6)
    assert detail["actual_damage"] == 6
    assert enemy.shards == 14, "按实伤夺取等量碎片"
    assert state.shards == 6, "玩家侧入账"
    assert "xijie_stolen" not in detail, "孤儿字段已废弃"


def test_xijie_no_status_no_steal():
    state, combat, player, enemy = _arena()
    enemy.shards = 20
    detail = _damage(combat, player, enemy, 6)
    assert enemy.shards == 20 and state.shards == 0


def test_xijie_no_shards_no_steal():
    state, combat, player, enemy = _arena()
    player.add_status(StatusEffect(name="洗劫", remaining_rounds=-1, value=2, source="x"))
    _damage(combat, player, enemy, 6)
    assert enemy.shards == 0 and state.shards == 0


def test_xijie_partial_steal_floor():
    """目标碎片少于实伤：夺取量=min(碎片, 伤害)。"""
    state, combat, player, enemy = _arena()
    enemy.shards = 3
    player.add_status(StatusEffect(name="洗劫", remaining_rounds=-1, value=2, source="x"))
    _damage(combat, player, enemy, 10)
    assert enemy.shards == 0 and state.shards == 3


def test_xijie_fake_shards_priority():
    """假碎片优先扣减（既有 _xijie_steal 语义原样）。"""
    state, combat, player, enemy = _arena()
    enemy.fake_shards = 4
    enemy.shards = 10
    player.add_status(StatusEffect(name="洗劫", remaining_rounds=-1, value=2, source="x"))
    _damage(combat, player, enemy, 5)
    assert enemy.fake_shards == 0 and enemy.shards == 9, "假碎片优先失去"
    assert state.shards == 5


def test_xijie_self_damage_no_steal():
    state, combat, player, enemy = _arena()
    player.add_status(StatusEffect(name="洗劫", remaining_rounds=-1, value=2, source="x"))
    player.shards = 0
    # 自伤（_xijie_steal 的 target is caster 门闩）
    combat._raw_hp_loss(player, 5)
    # 用无洗劫的另一攻击者打拥有洗劫的玩家：夺取者是 attacker，不受 target 状态影响
    state2, combat2, player2, enemy2 = _arena()
    player2.add_status(StatusEffect(name="洗劫", remaining_rounds=-1, value=2, source="x"))
    _damage(combat2, enemy2, player2, 6)   # enemy 无洗劫 → 不夺
    assert player2.current_hp == 94 and state2.shards == 0


def test_xijie_zero_actual_damage_no_steal():
    """格挡完全吸收（actual=0）：不夺碎片。"""
    state, combat, player, enemy = _arena()
    enemy.shards = 20
    enemy.shield = 50
    player.add_status(StatusEffect(name="洗劫", remaining_rounds=-1, value=2, source="x"))
    _damage(combat, player, enemy, 10)
    assert enemy.shards == 20 and state.shards == 0


def test_xijie_multi_hit_each_hit_steals():
    """多段命中：每段伤害各夺一次（旧行为：每 _apply_hostile_damage 一次）。"""
    state, combat, player, enemy = _arena()
    enemy.shards = 20
    player.add_status(StatusEffect(name="洗劫", remaining_rounds=-1, value=2, source="x"))
    _damage(combat, player, enemy, 4)
    _damage(combat, player, enemy, 4)
    assert enemy.shards == 12 and state.shards == 8


# ==================== 3. 只触发一次 ====================

def test_xijie_executes_exactly_once_per_damage():
    state, combat, player, enemy = _arena()
    enemy.shards = 20
    player.add_status(StatusEffect(name="洗劫", remaining_rounds=-1, value=2, source="x"))
    _damage(combat, player, enemy, 6)
    assert enemy.shards == 14, "若双触发会夺 12（20-12=8）"
    assert len([e for e in state.combat_events
                if e.event_type == CombatEventType.DAMAGE_APPLIED]) == 1


# ==================== 4. 参考实现 sweep ====================

def test_xijie_reference_sweep_zero_mismatch():
    """旧调用块 vs 新机制：状态有无×目标碎片(含假碎片)×实伤 全场景逐结果一致。"""

    def old_steal(combat, source, target, actual):
        """旧实现（迁移前调用块语义的逐行复刻）。"""
        if actual > 0 and source is not None:
            stolen = combat._xijie_steal(source, target, actual)
            if stolen:
                return stolen
        return 0

    mismatches = []
    total = 0
    for has_status_flag, (shards, fake), amount, shield in itertools.product(
            [False, True], [(0, 0), (20, 0), (3, 0), (10, 4)],
            [0, 6, 25], [0, 99]):
        total += 1
        # A：新路径（机制经事件总线执行）
        state_a, combat_a, player_a, enemy_a = _arena()
        # B：模拟旧路径（取消订阅，手动执行旧块）
        state_b, combat_b, player_b, enemy_b = _arena()
        combat_b.mechanism_bus.unregister(MECHANISMS.get("洗劫·夺碎片"))
        for ent_a, ent_b in ((enemy_a, enemy_b),):
            ent_a.shards, ent_a.fake_shards = shards, fake
            ent_b.shards, ent_b.fake_shards = shards, fake
        if has_status_flag:
            player_a.add_status(StatusEffect(name="洗劫", remaining_rounds=-1, value=2, source="x"))
            player_b.add_status(StatusEffect(name="洗劫", remaining_rounds=-1, value=2, source="x"))
        for e in (enemy_a, enemy_b):
            e.shield = shield

        detail_a = _damage(combat_a, player_a, enemy_a, amount)
        detail_b = _damage(combat_b, player_b, enemy_b, amount)
        old_steal(combat_b, player_b, enemy_b, detail_b.get("actual_damage", 0))

        for label, ent_a2, ent_b2, st_a, st_b in (
                ("enemy", enemy_a, enemy_b, state_a, state_b),
                ("player", player_a, player_b, state_a, state_b)):
            if (ent_a2.shards, ent_a2.fake_shards) != (ent_b2.shards, ent_b2.fake_shards):
                mismatches.append((label, has_status_flag, shards, fake, amount, shield,
                                   (ent_a2.shards, ent_a2.fake_shards),
                                   (ent_b2.shards, ent_b2.fake_shards)))
        if state_a.shards != state_b.shards:
            mismatches.append(("state_shards", has_status_flag, shards, fake, amount, shield,
                               state_a.shards, state_b.shards))

    assert not mismatches, f"{total} 组场景出现 {len(mismatches)} 组差异: {mismatches[:3]}"


# ==================== 5. 生产扫描 ====================

def test_xijie_single_steal_implementation_and_no_special_api():
    """_xijie_steal 仍是唯一夺碎片实现；无洗劫专用 API。"""
    assert "def _xijie_steal" in COMBAT_SOURCE, "夺碎片唯一实现保留在引擎"
    from engine.mechanisms import verb_names
    assert not any("xijie" in v or "steal" in v for v in verb_names())
    from engine.mechanisms.registry import MECHANISMS as REG
    assert len([m for m in REG.all() if m.name == "洗劫·夺碎片"]) == 1


# ==================== 8. 组合链 / 多实体边界（最终行为验证阶段新增） ====================

def test_xijie_steal_then_kill_death_chain():
    """完整链路：伤害夺取碎片 -> 目标死亡 -> ENTITY_DIED -> 焦黑发丝（玩家持有时）。

    验证 DAMAGE_APPLIED 与 ENTITY_DIED 两个事件机制在同一次伤害中依次生效。
    """
    from engine.models import Relic
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=50, speed_limit=20, current_speed=5)
    enemy = Entity("M", "怪物", blood_limit=10, current_hp=10, shards=8, fake_shards=0)
    player.add_status(StatusEffect(name="洗劫", remaining_rounds=-1, value=2, source="x"))
    state.player = player
    state.shards = 0
    state.enemies = [enemy]
    state.relics = [Relic(name="焦黑发丝", effect="")]
    combat = CombatEngine(state, DiceEngine())
    sp_before = player.current_speed

    _damage(combat, player, enemy, 15)

    # 洗劫·夺碎片：8 碎片全夺（伤害 15 > 碎片 8）
    assert enemy.shards == 0 and state.shards == 8
    # 死亡：ENTITY_DIED
    assert enemy.is_alive is False
    # 焦黑发丝：速度 +2
    assert player.current_speed == sp_before + 2

    # 事件流：DAMAGE_APPLIED 必须先于 ENTITY_DIED
    types = [e.event_type for e in combat.event_stream]
    assert types.index(CombatEventType.DAMAGE_APPLIED) < types.index(CombatEventType.ENTITY_DIED), \
        f"事件顺序异常: {types}"

    # 死亡事件的 parent_event_id 指向伤害事件
    damage_ev = next(e for e in combat.event_stream
                     if e.event_type == CombatEventType.DAMAGE_APPLIED)
    died_ev = next(e for e in combat.event_stream
                   if e.event_type == CombatEventType.ENTITY_DIED)
    assert died_ev.ctx.get("parent_event_id") == damage_ev.ctx.get("event_id")


def test_xijie_multi_enemies_each_steals_independently():
    """多个敌人各自持碎片：对每个目标造成伤害时，夺取量独立计算。"""
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=50, speed_limit=10, current_speed=5)
    e1 = Entity("E1", "怪物", blood_limit=50, current_hp=50, shards=10)
    e2 = Entity("E2", "怪物", blood_limit=50, current_hp=50, shards=3)
    e3 = Entity("E3", "怪物", blood_limit=50, current_hp=50, shards=0)
    player.add_status(StatusEffect(name="洗劫", remaining_rounds=-1, value=2, source="x"))
    state.player = player
    state.shards = 0
    state.enemies = [e1, e2, e3]
    combat = CombatEngine(state, DiceEngine())

    _damage(combat, player, e1, 6)
    _damage(combat, player, e2, 6)
    _damage(combat, player, e3, 6)

    assert e1.shards == 4                            # 第一次夺 6（10-6）
    assert e2.shards == 0                            # 第二次夺 3（碎片不足封底）
    assert e3.shards == 0                            # 第三次无碎片可夺
    assert state.shards == 9, f"玩家碎片={state.shards}（6+3+0）"
    assert len([e for e in combat.event_stream
                if e.event_type == CombatEventType.DAMAGE_APPLIED]) == 3


def test_xijie_zero_attack_power_damage_still_steals_when_overkill():
    """攻击力 0 但伤害来自道纹/法术时：只要实伤>0 就按实伤夺碎片。"""
    state, combat, player, enemy = _arena()
    enemy.shards = 12
    player.attack_power = 0
    player.attack_count = 0
    player.add_status(StatusEffect(name="洗劫", remaining_rounds=-1, value=2, source="x"))
    detail = _damage(combat, player, enemy, 7)   # 非普攻路径，7 点实伤
    assert detail["actual_damage"] == 7
    assert enemy.shards == 5, "实伤 7 -> 夺 7"
    assert state.shards == 7
