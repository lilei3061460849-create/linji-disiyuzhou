"""【焦黑发丝】迁移验证：_on_entity_death 内嵌块 → ENTITY_DIED 事件 Mechanism。

本批验证目标：
  - 事件路径机制成为生产订阅者（TriggerBus 首个生产消费者）；
  - 通用 PLAYER 目标解析（ctx.resolve("player")）与显式实体传递（_emit → dispatch）；
  - 死亡管线顺序不变：发出 ENTITY_DIED 即分发（= 旧位置，招魂/分裂之前）。
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
from engine.models import Entity, GameState, Relic, StatusEffect
from engine.validator import check_migrated_mechanism_guards

ROOT = Path(__file__).resolve().parents[1]
COMBAT_SOURCE = (ROOT / "engine" / "combat.py").read_text(encoding="utf-8")


def _arena(player_speed: int = 5, player_mana: int = 0):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=player_mana,
                    speed_limit=20, current_speed=player_speed)
    enemy = Entity("M", "怪物", blood_limit=30, current_hp=30)
    state.player = player
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def _events(combat, event_type):
    return [e for e in combat.event_stream if e.event_type == event_type]


def _kill_by_damage(combat, target, source):
    combat._apply_hostile_damage(target, target.current_hp + 10, source=source)


def _kill_by_bleed(combat, target):
    target.current_hp = 3
    combat._pay_bleed_cost(target, 5)


def _kill_by_collapse(combat, target):
    target.mutation_count = 49
    combat.pay_numeric_cost(target, "异变", 5, cost_context={
        "timing": "monster_action", "source": "原初1", "source_type": "evolution",
        "actor": target, "target": target, "mechanic": "cost", "subtype": "mutation",
        "amount": 5, "tags": {"active_payment"},
    })


class _OldJiaohheiReference:
    """_on_entity_death 焦黑发丝旧块的复刻（仅测试对照用，非生产代码）。

    调用约定：死亡已经发生（entity._death_ctx 已写入），在"旧位置"（emit 之后、
    招魂之前）执行。对照引擎需先取消焦黑发丝机制的总线订阅。
    """

    def run(self, combat, entity):
        if entity.entity_type == "怪物":
            if combat.state.player and combat._relic_active(combat.state.player, "焦黑发丝"):
                death_ctx = entity._death_ctx
                combat._gain_speed(combat.state.player, 2, ctx={
                    "timing": death_ctx["timing"], "source": "焦黑发丝", "source_type": "relic",
                    "actor": combat.state.player, "target": combat.state.player,
                    "owner": combat.state.player,
                    "mechanic": "speed_change", "subtype": "current_speed", "amount": 2,
                    "tags": {"relic", "death_trigger"},
                    "parent_event_id": death_ctx["event_id"],
                })


def _prepare(state, combat, *, held=False, sealed=0):
    if held:
        state.relics = [Relic("焦黑发丝", "")]
    if sealed:
        state.sealed_relics["焦黑发丝"] = sealed


# ==================== 1. 注册 / 订阅 / 旧实现删除 ====================

def test_jiaohhei_is_registered_event_mechanism():
    mech = MECHANISMS.get("焦黑发丝")
    assert mech is not None
    assert mech.when.matches_event(CombatEventType.ENTITY_DIED)
    assert mech.priority == 10
    from engine.mechanisms.registry import MECHANISMS as REG
    assert [m.name for m in REG.event_mechanisms()] == ["焦黑发丝"]


def test_bus_has_exactly_one_production_subscription():
    _, combat, _, _ = _arena()
    listeners = combat.mechanism_bus.listeners_for(CombatEventType.ENTITY_DIED)
    assert len(listeners) == 1 and listeners[0].name == "焦黑发丝", \
        "生产事件订阅必须唯一，杜绝双触发"


def test_old_jiaohhei_if_removed():
    """旧生产分支已删除；注释中的历史说明允许保留（阶段三协议）。"""
    assert 'self._relic_active(self.state.player, "焦黑发丝")' not in COMBAT_SOURCE, \
        "焦黑发丝的旧生产调用模式不得残留"
    assert check_migrated_mechanism_guards() == []


# ==================== 2. 触发语义 ====================

def test_jiaohhei_triggers_on_monster_death():
    state, combat, player, enemy = _arena(player_speed=5)
    _prepare(state, combat, held=True)
    _kill_by_damage(combat, enemy, player)
    assert player.current_speed == 7
    assert enemy.is_alive is False


def test_jiaohhei_no_relic_no_trigger():
    state, combat, player, enemy = _arena(player_speed=5)
    _kill_by_damage(combat, enemy, player)
    assert player.current_speed == 5


def test_jiaohhei_sealed_no_trigger():
    state, combat, player, enemy = _arena(player_speed=5)
    _prepare(state, combat, held=True, sealed=2)
    _kill_by_damage(combat, enemy, player)
    assert player.current_speed == 5, "封印期间不触发（抵扣X）"


def test_jiaohhei_non_monster_death_no_trigger():
    """轮回者/朋友命零不触发（entity_type 条件）。"""
    state, combat, player, enemy = _arena(player_speed=5)
    _prepare(state, combat, held=True)
    # 玩家自己命零
    _kill_by_damage(combat, player, enemy)
    assert player.is_alive is False
    assert player.current_speed == 5, "轮回者死亡不触发"

    state2, combat2, player2, _ = _arena(player_speed=5)
    _prepare(state2, combat2, held=True)
    friend = Entity("F", "朋友", blood_limit=10, current_hp=10)
    state2.friends = [friend]
    _kill_by_damage(combat2, friend, player2)
    # 朋友受致命伤 → 撤退保护（不死亡、无 ENTITY_DIED）
    assert friend.has_retreated is True and friend.is_alive is True
    assert player2.current_speed == 5, "未发生死亡 → 不触发"


def test_jiaohhei_bleed_and_collapse_deaths():
    """失血命零与崩解命零同样触发（既有规则：所有怪物死亡来源）。"""
    state, combat, player, enemy = _arena(player_speed=5)
    _prepare(state, combat, held=True)
    _kill_by_bleed(combat, enemy)
    assert enemy.is_alive is False and player.current_speed == 7

    state2, combat2, player2, enemy2 = _arena(player_speed=5)
    _prepare(state2, combat2, held=True)
    _kill_by_collapse(combat2, enemy2)
    assert enemy2.is_alive is False and player2.current_speed == 7


def test_jiaohhei_speed_verb_semantics():
    """速度经统一入口：玩家有【加速】时获得量翻倍（既有 _gain_speed 语义）。"""
    state, combat, player, enemy = _arena(player_speed=5)
    _prepare(state, combat, held=True)
    player.add_status(StatusEffect(name="加速", remaining_rounds=1, value=1, source="x"))
    _kill_by_damage(combat, enemy, player)
    assert player.current_speed == 9, "加速：+2×2=4"


# ==================== 3. ctx / 事件 / 顺序 ====================

def test_jiaohhei_effect_context_and_parent_chain():
    state, combat, player, enemy = _arena(player_speed=5)
    _prepare(state, combat, held=True)
    _kill_by_damage(combat, enemy, player)

    died = _events(combat, CombatEventType.ENTITY_DIED)
    assert len(died) == 1
    speed_event = player._speed_change_events[-1]
    assert speed_event["source"] == "焦黑发丝"
    assert speed_event["source_type"] == "relic"
    assert speed_event["mechanic"] == "speed_change"
    assert speed_event["subtype"] == "current_speed"
    assert speed_event["amount"] == 2
    assert set(speed_event["tags"]) == {"relic", "death_trigger"}
    assert speed_event["parent_event_id"] == died[0].ctx["event_id"], "速度事件必须挂在死亡事件下"


def test_jiaohhei_zhaohun_unaffected():
    """招魂尸体登记与死亡 ctx 不受迁移影响。"""
    state, combat, player, enemy = _arena()
    _prepare(state, combat, held=True)
    _kill_by_damage(combat, enemy, player)
    assert enemy in state.dead_monsters
    assert enemy._death_ctx["subtype"] == "hp_zero"
    assert enemy._death_ctx["source"] == "普通攻击" or enemy._death_ctx["source"] != ""


# ==================== 4. 只触发一次 ====================

def test_jiaohhei_executes_exactly_once():
    state, combat, player, enemy = _arena(player_speed=5)
    _prepare(state, combat, held=True)
    _kill_by_damage(combat, enemy, player)
    assert player.current_speed == 7, "若双触发会得到 9（7+2）"
    assert len(_events(combat, CombatEventType.ENTITY_DIED)) == 1


# ==================== 5. 参考实现 sweep ====================

def test_jiaohhei_reference_sweep_zero_mismatch():
    """旧块 vs 新机制：死亡来源×持有×封印×加速 全场景逐结果一致。"""
    old = _OldJiaohheiReference()
    kill_kinds = ["damage", "bleed", "collapse"]
    mismatches = []
    total = 0
    for kind, held, sealed, speed_up in itertools.product(
            kill_kinds, [False, True], [0, 2], [False, True]):
        total += 1
        # 引擎 A = 新路径（生产）；引擎 B = 取消订阅后手动执行旧参考块
        state_a, combat_a, player_a, enemy_a = _arena(player_speed=5)
        state_b, combat_b, player_b, enemy_b = _arena(player_speed=5)
        for state, combat in ((state_a, combat_a), (state_b, combat_b)):
            _prepare(state, combat, held=held, sealed=sealed)
        if speed_up:
            player_a.add_status(StatusEffect(name="加速", remaining_rounds=1, value=1, source="x"))
            player_b.add_status(StatusEffect(name="加速", remaining_rounds=1, value=1, source="x"))

        # B：模拟迁移前（取消订阅，死亡后手动跑旧块）
        combat_b.mechanism_bus.unregister(MECHANISMS.get("焦黑发丝"))

        if kind == "damage":
            _kill_by_damage(combat_a, enemy_a, player_a)
            _kill_by_damage(combat_b, enemy_b, player_b)
            old.run(combat_b, enemy_b)
        elif kind == "bleed":
            _kill_by_bleed(combat_a, enemy_a)
            _kill_by_bleed(combat_b, enemy_b)
            old.run(combat_b, enemy_b)
        else:
            _kill_by_collapse(combat_a, enemy_a)
            _kill_by_collapse(combat_b, enemy_b)
            old.run(combat_b, enemy_b)

        if player_a.current_speed != player_b.current_speed:
            mismatches.append(("speed", kind, held, sealed, speed_up,
                               player_a.current_speed, player_b.current_speed))
        ev_a = getattr(player_a, "_speed_change_events", [])
        ev_b = getattr(player_b, "_speed_change_events", [])
        if [e["source"] for e in ev_a] != [e["source"] for e in ev_b]:
            mismatches.append(("speed_events", kind, held, sealed, speed_up))
        if enemy_a.is_alive != enemy_b.is_alive:
            mismatches.append(("alive", kind, held, sealed, speed_up))

    assert total == len(kill_kinds) * 2 * 2 * 2
    assert not mismatches, f"{total} 组场景出现 {len(mismatches)} 组差异: {mismatches[:3]}"


def test_jiaohhei_reference_matches_existing_regression_paths():
    """与既有测试同口径的三条路径：伤害命零 / 流血命零 / 崩解命零 / 封印。"""
    old = _OldJiaohheiReference()
    for kind in ("damage", "bleed", "collapse", "sealed"):
        state_a, combat_a, player_a, enemy_a = _arena(player_speed=5)
        state_b, combat_b, player_b, enemy_b = _arena(player_speed=5)
        for state, combat in ((state_a, combat_a), (state_b, combat_b)):
            _prepare(state, combat, held=True, sealed=2 if kind == "sealed" else 0)
        combat_b.mechanism_bus.unregister(MECHANISMS.get("焦黑发丝"))

        if kind == "sealed":
            _kill_by_damage(combat_a, enemy_a, player_a)
            _kill_by_damage(combat_b, enemy_b, player_b)
            old.run(combat_b, enemy_b)
        elif kind == "damage":
            _kill_by_damage(combat_a, enemy_a, player_a)
            _kill_by_damage(combat_b, enemy_b, player_b)
            old.run(combat_b, enemy_b)
        elif kind == "bleed":
            _kill_by_bleed(combat_a, enemy_a)
            _kill_by_bleed(combat_b, enemy_b)
            old.run(combat_b, enemy_b)
        else:
            _kill_by_collapse(combat_a, enemy_a)
            _kill_by_collapse(combat_b, enemy_b)
            old.run(combat_b, enemy_b)

        assert player_a.current_speed == player_b.current_speed, kind
