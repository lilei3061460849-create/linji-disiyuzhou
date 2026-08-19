"""跨 Engine 实例存档生命周期回归（2026-08-19 修复）。

背景：事件观察者（combat_events._StateEventObserver）持引擎 id。跨进程/跨实例
读档时，pickle 恢复的观察者仍指旧引擎，事件机制会静默失效。
修复：load_game 成功替换 state 后重新绑定当前 CombatEngine 观察者。

本文件是真实跨实例场景：新 GameEngine 实例加载旧实例写出的存档。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.combat_events import CombatEventType, get_combat_event_observer
from engine.models import Entity, Relic, StatusEffect


def _setup_engine(db_path, save_dir, seed, *, with_battle=True):
    engine = GameEngine(db_path=db_path, save_dir=save_dir, rng_seed=seed)
    if not with_battle:
        return engine
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=20, current_mana=20, speed_limit=20, current_speed=5)
    enemy = Entity("M", "怪物", blood_limit=50, current_hp=50, shards=10)
    engine.state.player = player
    engine.state.enemies = [enemy]
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.current_round = 1
    return engine


def test_cross_engine_load_rebinds_observer_and_event_mechanisms_fire(tmp_path):
    save_dir = str(tmp_path)
    # 旧实例：写档
    e1 = _setup_engine(os.path.join(save_dir, "r1.db"), save_dir, seed=1)
    e1.state.relics = [Relic("焦黑发丝", "")]
    e1.state.player.add_status(StatusEffect("洗劫", -1, 2, "e"))
    assert e1.save_game("x")["success"]

    # 新实例（模拟下次启动）：读同一存档
    e2 = _setup_engine(os.path.join(save_dir, "r2.db"), save_dir, seed=2)
    res = e2.load_game("x")
    assert res["success"]

    # 观察者必须重新绑定到当前引擎（修复点）
    obs = get_combat_event_observer(e2.state)
    assert obs is not None
    assert obs._engine_id == id(e2.combat), "观察者必须指向当前 CombatEngine"

    m2 = e2.state.enemies[0]
    p2 = e2.state.player
    speed_before = p2.current_speed

    # 命零 → 焦黑发丝（ENTITY_DIED 事件机制）必须触发
    e2.combat._apply_hostile_damage(m2, 60, source=p2)
    assert m2.is_alive is False
    assert p2.current_speed == speed_before + 2, "跨进程读档后焦黑发丝必须生效"

    # 洗劫·夺碎片（DAMAGE_APPLIED 事件机制）在加载战斗中仍生效
    state2 = e2.state
    state2.combat_events.clear()
    m3 = Entity("M2", "怪物", blood_limit=40, current_hp=40, shards=8)
    state2.enemies = [m3]
    shards_before_second = state2.shards
    e2.combat._apply_hostile_damage(m3, 5, source=p2)
    assert m3.shards == 3, "洗劫·夺碎片在加载后战斗必须生效"
    assert state2.shards == shards_before_second + 5

    # HEAL_APPLIED 总线在加载后仍可分发（事件基础设施同链路）
    hits = []
    from engine.mechanisms import MECHANISMS, Mechanism, Trigger
    dummy = Mechanism(name="读档治疗观察", when=Trigger.event(CombatEventType.HEAL_APPLIED),
                      effect=lambda ctx, targets: hits.append(1))
    MECHANISMS.register(dummy)
    try:
        e2.combat.mechanism_bus.register(dummy)
        e2.state.apply_heal(p2, 1)
        assert hits == [1]
    finally:
        e2.combat.mechanism_bus.unregister(dummy)
        MECHANISMS.unregister("读档治疗观察")


def test_cross_engine_load_without_battle_no_observer_no_error(tmp_path):
    """非战斗态存档跨实例加载：无观察者绑定需求，零行为变化。"""
    save_dir = str(tmp_path)
    e1 = _setup_engine(os.path.join(save_dir, "r1.db"), save_dir, seed=3,
                       with_battle=False)
    assert e1.save_game("y")["success"]
    e2 = _setup_engine(os.path.join(save_dir, "r2.db"), save_dir, seed=4,
                       with_battle=False)
    res = e2.load_game("y")
    assert res["success"]
    # 加载后重新绑定观察者（修复无条件执行，绑定到当前引擎——无害且一致）
    obs = get_combat_event_observer(e2.state)
    assert obs is not None and obs._engine_id == id(e2.combat)


def test_same_engine_load_still_works(tmp_path):
    """同实例读档（既有路径）不受修复影响。"""
    save_dir = str(tmp_path)
    e = _setup_engine(os.path.join(save_dir, "r.db"), save_dir, seed=5)
    e.state.relics = [Relic("焦黑发丝", "")]
    assert e.save_game("z")["success"]
    assert e.load_game("z")["success"]
    obs = get_combat_event_observer(e.state)
    assert obs is not None and obs._engine_id == id(e.combat)
