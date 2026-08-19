"""机制系统（MVP）单元测试：Verb / Mechanism / Trigger / Condition / Target。

验证目标不是测试数量，而是证明：
"声明层确实能够准确调用现有底层结算"，且不引入新的分发路径。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.combat_events import CombatEvent, CombatEventType
from engine.dice import DiceEngine
from engine.mechanisms import (
    ALL, ALL_ALLIES, ALL_ENEMIES, DEAD_ENTITY, MECHANISMS, RANDOM_ENEMY, SELF,
    SOURCE, TARGET, Mechanism, MechanismRegistry, Trigger, TriggerBus,
    TriggerContext, all_, any_, apply_verb, damage_type_not, entity_type,
    events_this_round, get_verb, has_status, hp_at_least, is_alive, not_,
    side_has, verb_names,
)
from engine.models import Entity, GameState, Relic, StatusEffect
from engine.validator import check_migrated_mechanism_guards, RuleValidator


def _arena(enemy_hp: int = 100, enemy_bl: int | None = None):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=50, speed_limit=10, current_speed=5)
    enemy = Entity("M", "怪物", blood_limit=enemy_bl if enemy_bl is not None else enemy_hp,
                   current_hp=enemy_hp)
    state.player = player
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def _damage_event(actor="P", target="M", round_no=1) -> CombatEvent:
    return CombatEvent(event_type=CombatEventType.DAMAGE_APPLIED, battle_no=1,
                       round_no=round_no, actor_name=actor, target_name=target,
                       data={"actual_damage": 3})


# ==================== 1. Mechanism 注册 ====================

def test_mechanism_registry_register_get_names():
    reg = MechanismRegistry()
    mech = Mechanism(name="测试机制", when=Trigger.event(CombatEventType.DAMAGE_APPLIED),
                     effect=lambda ctx, targets: None)
    reg.register(mech)
    assert reg.get("测试机制") is mech
    assert "测试机制" in reg.names()
    # 同一对象重复注册幂等
    assert reg.register(mech) is mech


def test_mechanism_registry_rejects_duplicate_name():
    reg = MechanismRegistry()
    reg.register(Mechanism(name="同名", when=Trigger.event(CombatEventType.DAMAGE_APPLIED),
                           effect=lambda ctx, targets: None))
    with pytest.raises(ValueError):
        reg.register(Mechanism(name="同名", when=Trigger.event(CombatEventType.DAMAGE_APPLIED),
                               effect=lambda ctx, targets: None))


def test_mechanism_requires_effect():
    with pytest.raises(ValueError):
        Mechanism(name="缺效果", when=Trigger.event(CombatEventType.DAMAGE_APPLIED))


def test_mechanism_state_is_per_entity_not_global():
    """机制定义全局化；机制状态按实体存放，不进全局 Mechanism 实例。"""
    mech = Mechanism(name="计数机制", when=Trigger.event(CombatEventType.DAMAGE_APPLIED),
                     effect=lambda ctx, targets: None, needs_state=True)
    a = Entity("A", "怪物", blood_limit=10, current_hp=10)
    b = Entity("B", "怪物", blood_limit=10, current_hp=10)
    mech.state_of(a)["hits"] = 1
    assert mech.state_of(a)["hits"] == 1
    assert mech.state_of(b) == {}          # 实体之间互相独立
    assert not hasattr(mech, "states")     # 没有全局状态表


# ==================== 2. Trigger 分发 ====================

def test_trigger_bus_no_listeners_is_noop():
    bus = TriggerBus()
    assert bus.dispatch(_damage_event(), None) == []


def test_trigger_bus_dispatches_in_priority_order():
    bus = TriggerBus()
    calls = []
    high = Mechanism(name="高优先", when=Trigger.event(CombatEventType.DAMAGE_APPLIED),
                     effect=lambda ctx, targets: calls.append("high"), priority=10)
    low = Mechanism(name="低优先", when=Trigger.event(CombatEventType.DAMAGE_APPLIED),
                    effect=lambda ctx, targets: calls.append("low"), priority=50)
    bus.register(low)
    bus.register(high)   # 注册顺序反着来，仍按 priority 执行
    bus.dispatch(_damage_event(), None)
    assert calls == ["high", "low"]

    bus.unregister(high)
    calls.clear()
    bus.dispatch(_damage_event(), None)
    assert calls == ["low"]
    assert bus.listeners_for(CombatEventType.DAMAGE_APPLIED) == [low]


def test_trigger_bus_condition_gates_effect():
    bus = TriggerBus()
    fired = []
    mech = Mechanism(name="条件机制", when=Trigger.event(CombatEventType.DAMAGE_APPLIED),
                     condition=is_alive(of="target"),
                     effect=lambda ctx, targets: fired.append(1))
    bus.register(mech)
    bus.dispatch(_damage_event(), None)   # combat=None → target 解析不出实体 → 条件不成立
    assert fired == []


def test_engine_event_path_reaches_bus_exactly_once():
    """引擎真实事件路径：DAMAGE_APPLIED → TriggerBus → Mechanism，恰好一次；
    事件记录本身与无订阅者时完全一致。"""
    state, combat, player, enemy = _arena()
    hits = []
    observer = Mechanism(
        name="伤害观察员",
        when=Trigger.event(CombatEventType.DAMAGE_APPLIED),
        effect=lambda ctx, targets: hits.append(ctx.event.target_name))
    combat.mechanism_bus.register(observer)

    detail = combat._apply_hostile_damage(enemy, 5, source=player)
    assert detail["actual_damage"] == 5
    assert hits == ["M"], "一次伤害只应分发一次"
    assert len(state.combat_events) == 1, "事件记录数量与迁移前一致"
    assert state.combat_events[0].event_type == CombatEventType.DAMAGE_APPLIED


def test_engine_event_without_subscriber_unchanged():
    state, combat, player, enemy = _arena()
    combat._apply_hostile_damage(enemy, 5, source=player)
    assert [e.event_type for e in state.combat_events] == [CombatEventType.DAMAGE_APPLIED]
    assert combat.mechanism_bus.listeners_for(CombatEventType.DAMAGE_APPLIED) == []


# ==================== 3. Condition ====================

def test_condition_combinators_basic():
    state, combat, player, enemy = _arena()
    enemy.add_status(StatusEffect(name="加害", remaining_rounds=-1, value=2, source="x"))
    state.relics = [Relic("避风铃", "")]
    ctx = TriggerContext(combat=combat, state=state, target=enemy, source=player,
                         amount=5, damage_type="普通")

    assert has_status("加害")(ctx) is True
    assert has_status("龙鳞")(ctx) is False
    assert is_alive()(ctx) is True
    assert entity_type("怪物")(ctx) is True
    assert entity_type("轮回者", of="source")(ctx) is True
    assert hp_at_least(90)(ctx) is True
    assert hp_at_least(101)(ctx) is False
    assert side_has("避风铃", of="source")(ctx) is True
    assert damage_type_not("代价")(ctx) is True
    assert damage_type_not("普通")(ctx) is False
    assert all_(has_status("加害"), damage_type_not("代价"))(ctx) is True
    assert any_(has_status("龙鳞"), damage_type_not("代价"))(ctx) is True
    assert not_(has_status("龙鳞"))(ctx) is True


def test_condition_events_this_round_composition():
    """审计报告验收示例：本回合既受到过伤害、又获得过回复。
    语义：该实体作为事件目标（承受方）。事实源=state.combat_events。"""
    state, combat, player, enemy = _arena(enemy_hp=80)
    combat._apply_hostile_damage(enemy, 3, source=player)   # DAMAGE_APPLIED，round=0
    ctx = TriggerContext(combat=combat, state=state, target=enemy)

    damaged = events_this_round(CombatEventType.DAMAGE_APPLIED)
    healed = events_this_round(CombatEventType.HEAL_APPLIED)
    assert damaged(ctx) is True
    assert healed(ctx) is False
    assert all_(damaged, healed)(ctx) is False

    state.apply_heal(enemy, 2)                              # HEAL_APPLIED，round=0
    assert all_(damaged, healed)(ctx) is True, "既受伤又回复的组合条件必须可表达"

    # 攻击者（作为行为发起方）不算"受到伤害"
    attacker_ctx = TriggerContext(combat=combat, state=state, target=player)
    assert damaged(attacker_ctx) is False


def test_condition_events_this_round_respects_round_window():
    state, combat, player, enemy = _arena()
    state.combat_events.append(_damage_event(target="M", round_no=2))
    state.current_round = 1
    ctx = TriggerContext(combat=combat, state=state, target=enemy)
    assert events_this_round(CombatEventType.DAMAGE_APPLIED)(ctx) is False
    state.current_round = 2
    assert events_this_round(CombatEventType.DAMAGE_APPLIED)(ctx) is True


# ==================== 4. Target ====================

def test_target_selectors():
    state, combat, player, enemy = _arena()
    friend = Entity("F", "朋友", blood_limit=20, current_hp=20)
    enemy2 = Entity("M2", "怪物", blood_limit=40, current_hp=40)
    state.friends = [friend]
    state.enemies = [enemy, enemy2]

    ctx = TriggerContext(combat=combat, state=state, target=enemy, source=player)
    assert SELF(ctx) == [enemy]
    assert TARGET(ctx) == [enemy]
    assert SOURCE(ctx) == [player]
    assert ALL(ctx) == [player, friend, enemy, enemy2]
    assert ALL_ALLIES(ctx) == [enemy2]        # 敌方阵营、不含主体
    assert ALL_ENEMIES(ctx) == [player, friend]
    assert RANDOM_ENEMY(ctx)[0] in (player, friend)

    dead_evt = CombatEvent(event_type=CombatEventType.ENTITY_DIED, battle_no=1,
                           round_no=1, actor_name="P", target_name="M")
    dead_ctx = TriggerContext(combat=combat, state=state, event=dead_evt, target=enemy)
    assert DEAD_ENTITY(dead_ctx) == [enemy]
    assert DEAD_ENTITY(ctx) == []             # 非死亡事件不产生死者目标


# ==================== 5. Verb ====================

def test_verb_registry_names_and_unknown():
    names = verb_names()
    for expected in ("damage", "heal", "hp_loss", "blood_limit", "cost", "status",
                     "speed", "shield", "mutation", "depart", "execute"):
        assert expected in names, f"基础动词 {expected} 应已注册"
    assert "mana" not in names, "法力无统一入口，MVP 不强行注册"
    with pytest.raises(ValueError):
        get_verb("不存在的动词")


def test_verb_damage_heal_hp_loss():
    state, combat, player, enemy = _arena()
    dmg = apply_verb(combat, "damage", {"target": enemy, "amount": 10, "source": player})
    assert dmg["actual_damage"] == 10 and enemy.current_hp == 90
    assert enemy.hp_lost_this_round == 10

    heal = apply_verb(combat, "heal", {"target": enemy, "amount": 5})
    assert heal["actual_heal"] == 5 and enemy.current_hp == 95

    loss = apply_verb(combat, "hp_loss", {"target": enemy, "amount": 3})
    assert loss["lost"] == 3 and enemy.current_hp == 92
    assert enemy.hp_lost_this_round == 13, "hp_loss 动词与统一入口同口径记账（伤害10+直接损失3）"


def test_verb_status_speed_shield():
    state, combat, player, enemy = _arena()
    apply_verb(combat, "status", {"target": enemy, "name": "疯狂", "value": 1})
    assert enemy.has_status("疯狂") and enemy.get_status_value("疯狂") == 1

    apply_verb(combat, "speed", {"target": player, "delta": 2})
    assert player.current_speed == 7
    apply_verb(combat, "speed", {"target": player, "delta": -1})
    assert player.current_speed == 6

    apply_verb(combat, "shield", {"target": enemy, "amount": 6})
    assert enemy.shield == 6


def test_verb_mutation_depart_execute():
    state, combat, player, enemy = _arena()
    apply_verb(combat, "mutation", {"target": enemy, "layers": 3})
    assert enemy.mutation_count == 3

    apply_verb(combat, "depart", {"target": enemy, "reason": "机制测试"})
    assert enemy.is_departed and enemy.departure_reason == "机制测试"
    assert enemy.is_alive is False and enemy.removed_without_kill is True

    state, combat, player, enemy = _arena()
    enemy.current_hp = 0
    assert apply_verb(combat, "execute", {"target": enemy}) is True
    assert enemy.is_alive is False
    assert [e.event_type for e in state.combat_events] == [CombatEventType.ENTITY_DIED]


def test_verb_blood_limit_and_cost():
    state, combat, player, enemy = _arena(enemy_bl=60)
    enemy.current_hp = 60
    res = apply_verb(combat, "blood_limit",
                     {"target": enemy, "delta": -5, "source": "测试", "polarity": "debuff"})
    assert enemy.blood_limit == 55
    assert enemy.current_hp == 55, "血限动词走统一入口：生命随血限封顶"
    assert res["died"] is False

    payment = apply_verb(combat, "cost", {"payer": player, "cost_type": "流血", "amount": 3})
    assert payment["actual_paid"] == 3 and player.current_hp == 97


# ==================== 6. 迁移护栏 ====================

def test_migration_guard_current_tree_clean():
    assert check_migrated_mechanism_guards() == [], "已迁移机制不得在核心管线留下同名硬编码分支"


def test_migration_guard_detects_planted_hardcode(tmp_path):
    planted = tmp_path / "combat.py"
    planted.write_text('if entity.has_status("加害"):\n    amount += 1\n', encoding="utf-8")
    violations = check_migrated_mechanism_guards([str(planted)])
    assert len(violations) == 1
    assert violations[0]["context"]["mechanism"] == "加害"
    assert violations[0]["severity"] == "error"


def test_migration_guard_ignores_unmigrated_and_comments(tmp_path):
    planted = tmp_path / "combat.py"
    planted.write_text(
        'if e.has_status("龙鳞"): pass\n'           # 未迁移机制：不管
        '# 注释里的 has_status("加害") 不算\n',     # 注释：不管
        encoding="utf-8")
    assert check_migrated_mechanism_guards([str(planted)]) == []


def test_migration_guard_exposed_on_validator():
    validator = RuleValidator()
    assert validator.migration_guard_violations == []
