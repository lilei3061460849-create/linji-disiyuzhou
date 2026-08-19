"""HEAL_APPLIED 事件总线接入验证（架构修复 Step 1）。

修复内容：models.apply_heal 直发的 HEAL_APPLIED 现在经通用事件观察者
（weakref 表，不进 state.__dict__）进入 TriggerBus；CombatEngine._emit 不再
单独分发——全仓事件只有一条分发路径。
本文件不迁移任何机制，只验证事件基础设施。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.combat_events import CombatEventType, get_combat_event_observer
from engine.dice import DiceEngine
from engine.mechanisms import MECHANISMS, Mechanism, Trigger
from engine.models import Consumable, Entity, GameState, Relic

ROOT = Path(__file__).resolve().parents[1]


def _arena():
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=50, speed_limit=10, current_speed=5)
    enemy = Entity("M", "怪物", blood_limit=50, current_hp=50)
    state.player = player
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def _heal_events(state):
    return [e for e in state.combat_events
            if e.event_type == CombatEventType.HEAL_APPLIED]


# ==================== 1. 事件仍只产生一次 + payload 一致 ====================

def test_heal_applied_single_emission_and_payload():
    state, combat, player, enemy = _arena()
    player.current_hp = 90
    combat.state.apply_heal(player, 10, ctx={
        "timing": "player_action", "source": "再生", "source_type": "daowen",
        "actor": player, "target": player, "mechanic": "heal", "subtype": "daowen",
        "amount": 10, "tags": {"daowen"}, "event_id": "H-1",
    })
    events = _heal_events(state)
    assert len(events) == 1, "HEAL_APPLIED 必须只发出一次"
    ev = events[0]
    assert ev.target_name == "P" and ev.actor_name == "P"
    assert ev.data == {
        "heal_amount": 10, "actual_heal": 10, "overheal": 0,
        "hp_before": 90, "hp_after": 100,
    }
    assert ev.ctx["source"] == "再生"
    assert ev.ctx["event_id"] == "H-1"


# ==================== 2. TriggerBus 收到事件 + 实体对象正确传递 ====================

def test_heal_bus_receives_event_with_entity_objects():
    state, combat, player, enemy = _arena()
    player.current_hp = 90
    received = []
    dummy = Mechanism(
        name="治疗观察员",
        when=Trigger.event(CombatEventType.HEAL_APPLIED),
        effect=lambda ctx, targets: received.append(
            (ctx.target, ctx.source, ctx.event.data["actual_heal"])))
    combat.mechanism_bus.register(dummy)
    try:
        combat.state.apply_heal(player, 5)
        assert received == [(player, None, 5)], "实体对象必须同一（is 语义）"
        assert received[0][0] is player
    finally:
        combat.mechanism_bus.unregister(dummy)


def test_heal_dummy_via_registry_subscription_then_cleanup():
    """经全局注册表注册 → 战斗实例构造时订阅 → 触发一次 → 双端清理。"""
    fired = []
    dummy = Mechanism(
        name="治疗观察·注册表",
        when=Trigger.event(CombatEventType.HEAL_APPLIED),
        effect=lambda ctx, targets: fired.append(ctx.event.target_name))
    MECHANISMS.register(dummy)
    try:
        state = GameState(phase="in_combat", combat_subphase="player_actions")
        player = Entity("P", "轮回者", blood_limit=100, current_hp=90,
                        mana_limit=50, current_mana=50, speed_limit=10, current_speed=5)
        state.player = player
        state.enemies = [Entity("M", "怪物", blood_limit=50, current_hp=50)]
        combat = CombatEngine(state, DiceEngine())
        combat.state.apply_heal(player, 6)
        assert fired == ["P"], "注册表订阅的 dummy 必须触发且只触发一次"
    finally:
        MECHANISMS.unregister("治疗观察·注册表")
        combat.mechanism_bus.unregister(dummy)


# ==================== 3. parent_event_id 链 / ctx 保持 ====================

def test_heal_parent_event_chain_preserved():
    state, combat, player, enemy = _arena()
    combat.state.apply_heal(player, 3, ctx={
        "timing": "round_start", "source": "自愈", "source_type": "daowen",
        "actor": player, "target": player, "mechanic": "heal", "subtype": "self_heal",
        "amount": 3, "tags": {"daowen", "round_start"},
        "event_id": "H-9", "parent_event_id": "PARENT-1",
    })
    ev = _heal_events(state)[0]
    assert ev.ctx["parent_event_id"] == "PARENT-1"
    assert ev.ctx["event_id"] == "H-9"
    assert ev.ctx["subtype"] == "self_heal"
    assert set(ev.ctx["tags"]) == {"daowen", "round_start"}


# ==================== 4. 治疗/溢出/零治疗语义不变 ====================

def test_heal_zero_and_overflow_semantics_unchanged():
    state, combat, player, enemy = _arena()
    # 0 治疗：照常发事件、actual=0、生命不变
    player.current_hp = 50
    combat.state.apply_heal(player, 0)
    ev = _heal_events(state)[-1]
    assert ev.data["actual_heal"] == 0 and player.current_hp == 50

    # 溢出 + 龙血瓶：溢出进入耐久存储（既有语义）
    state2, combat2, player2, _ = _arena()
    player2.current_hp = 90
    state2.consumables = [Consumable(name="龙血瓶", effect="", current_uses=5, max_uses=5)]
    combat2.state.apply_heal(player2, 30)
    ev2 = _heal_events(state2)[-1]
    assert ev2.data["actual_heal"] == 10 and ev2.data["overheal"] == 20
    bottle = next(c for c in state2.consumables if c.name == "龙血瓶")
    assert bottle.current_uses == 25 and bottle.max_uses == 25
    assert len(_heal_events(state2)) == 1


# ==================== 5. 无双发路径 ====================

def test_damage_path_still_dispatches_exactly_once():
    """_emit 不再显式分发后，伤害事件仍经观察者分发恰好一次。"""
    state, combat, player, enemy = _arena()
    hits = []
    dummy = Mechanism(
        name="伤害观察员",
        when=Trigger.event(CombatEventType.DAMAGE_APPLIED),
        effect=lambda ctx, targets: hits.append(1))
    combat.mechanism_bus.register(dummy)
    try:
        combat._apply_hostile_damage(enemy, 10, source=player)
        assert hits == [1], "伤害事件必须恰好分发一次"
    finally:
        combat.mechanism_bus.unregister(dummy)


def test_no_engine_no_observer_no_dispatch_no_error():
    """局外/无战斗上下文：apply_heal 照常发事件，无观察者时零行为变化。"""
    state = GameState(phase="setup")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=90,
                    mana_limit=50, current_mana=50, speed_limit=10, current_speed=5)
    state.player = player
    assert get_combat_event_observer(state) is None
    detail = state.apply_heal(player, 5)
    assert detail["actual_heal"] == 5
    assert len(_heal_events(state)) == 1


def test_observer_not_in_state_dict_and_restore_safe():
    """观察者挂在 state 上但：不进入序列化 to_dict；deepcopy/pickle 快照安全
    （观察者是可 pickle 的类实例，只存引擎 id；不把引擎带进存档）。"""
    import copy
    import pickle
    state, combat, player, enemy = _arena()
    obs = get_combat_event_observer(state)
    assert obs is not None
    # to_dict 不含观察者（序列化协议不变）
    d = state.to_dict()
    assert "_mechanism_event_observer" not in d
    # deepcopy 快照：观察者复制为新实例但 engine_id 不变，仍解析回原引擎
    snap = copy.deepcopy(state)
    fired = []
    dummy = Mechanism(name="快照观察", when=Trigger.event(CombatEventType.HEAL_APPLIED),
                      effect=lambda ctx, targets: fired.append(1))
    combat.mechanism_bus.register(dummy)
    try:
        snap.apply_heal(snap.player, 1)
        assert fired == [1], "快照恢复后的观察者必须仍指向活跃引擎"
    finally:
        combat.mechanism_bus.unregister(dummy)
    # pickle 往返（存档路径）：可序列化，观察者随档恢复
    payload = pickle.dumps(state)
    restored = pickle.loads(payload)
    assert get_combat_event_observer(restored) is not None
