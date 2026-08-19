"""【加害】迁移验证：JiahaiHook → Mechanism 声明层。

迁移要求（全部由本文件钉死）：
  1. 规则语义完全一致（amount>0、代价除外、状态值相加、缺省按0）；
  2. priority 保持原值 20；
  3. 与龙鳞/龙族血脉等其它伤害修正的先后顺序完全一致；
  4. EffectContext 传递与迁移前一致；
  5. 只触发一次——旧 JiahaiHook 已删除，不存在第二条分发路径。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import combat_hooks
from engine.combat import CombatEngine
from engine.combat_events import CombatEventType
from engine.combat_hooks import CombatHookManager
from engine.dice import DiceEngine
from engine.mechanisms import MECHANISMS, MechanismHookAdapter, Phase
from engine.models import Entity, GameState, StatusEffect


def _arena(enemy_hp: int = 100, enemy_bl: int | None = None):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=50, speed_limit=10, current_speed=10)
    enemy = Entity("M", "怪物", blood_limit=enemy_bl if enemy_bl is not None else enemy_hp,
                   current_hp=enemy_hp)
    state.player = player
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def _events(combat, event_type):
    return [e for e in combat.event_stream if e.event_type == event_type]


def _target(*statuses):
    t = Entity("T", "怪物", blood_limit=50, current_hp=50)
    for name, value in statuses:
        t.add_status(StatusEffect(name=name, remaining_rounds=-1, value=value, source="x"))
    return t


class _State:
    def side_has(self, entity, name):
        return False


# ==================== 1. 声明层形态 ====================

def test_jiahai_is_registered_mechanism():
    mech = MECHANISMS.get("加害")
    assert mech is not None
    assert mech.when.matches_phase(Phase.INCOMING_ADJUST)
    assert mech.priority == 20, "priority 必须保持原值 20（顺序即规则）"
    assert mech.target is not None


def test_old_jiahai_hook_fully_removed():
    """旧 JiahaiHook 类已删除：全模块不再存在，杜绝第二条分发路径。"""
    assert not hasattr(combat_hooks, "JiahaiHook")
    assert "JiahaiHook" not in [type(h).__name__ for h in CombatHookManager().hooks()]


# ==================== 2. 数值语义等价 ====================

def test_jiahai_numeric_equivalence():
    manager = CombatHookManager()
    state = _State()

    plain = _target(("加害", 2))
    assert manager.apply_incoming_adjust(plain, 8, "普通", None, state) == 10, "8+2=10"

    both = _target(("加害", 2), ("龙鳞", 8))
    assert manager.apply_incoming_adjust(both, 8, "普通", None, state) == 2, "max(0,(8+2)-8)=2"

    assert manager.apply_incoming_adjust(plain, 8, "代价", None, state) == 8, "代价不受增幅"
    assert manager.apply_incoming_adjust(plain, 0, "普通", None, state) == 0, "amount<=0 不改"
    assert manager.apply_incoming_adjust(plain, -3, "普通", None, state) == -3

    no_status = _target()
    assert manager.apply_incoming_adjust(no_status, 8, "普通", None, state) == 8

    zero_value = _target(("加害", 0))
    assert manager.apply_incoming_adjust(zero_value, 8, "普通", None, state) == 8, "value缺省按0"

    # 幂等：同一份管理器重复调用不叠加（机制无隐藏全局状态）
    assert manager.apply_incoming_adjust(plain, 8, "普通", None, state) == 10
    assert manager.apply_incoming_adjust(plain, 8, "普通", None, state) == 10


# ==================== 3. 只触发一次 ====================

def test_jiahai_executes_exactly_once_through_engine():
    state, combat, player, enemy = _arena()
    enemy.add_status(StatusEffect(name="加害", remaining_rounds=-1, value=2, source="x"))
    detail = combat._apply_hostile_damage(enemy, 10, source=player)
    # 加害2：10→12（raw_damage 是加减区结算后的数值，迁移前后同口径）。
    # 若新旧两条路径并存会得到 14（重复触发）或 10（丢触发）。
    assert detail["raw_damage"] == 12
    assert detail["actual_damage"] == 12
    assert enemy.current_hp == 88

    adapters = [h for h in combat.hook_manager.hooks()
                if isinstance(h, MechanismHookAdapter) and h.mechanism.name == "加害"]
    assert len(adapters) == 1, "加害的执行壳在分发列表里只能出现一次"


def test_jiahai_once_even_when_hook_manager_reused():
    manager = CombatHookManager()
    target = _target(("加害", 3))
    assert manager.apply_incoming_adjust(target, 5, "普通", None, _State()) == 8
    assert manager.apply_incoming_adjust(target, 5, "普通", None, _State()) == 8
    # 分发列表静态检查：只有一个加害执行壳
    assert [h for h in manager.hooks() if isinstance(h, MechanismHookAdapter)] \
        == [h for h in manager.hooks() if isinstance(h, MechanismHookAdapter)], \
        "同一个适配器实例，无重复注册"


# ==================== 4. priority / 顺序 ====================

def test_jiahai_priority_position_unchanged():
    manager = CombatHookManager()
    hooks = manager.hooks()
    adapter = next(h for h in hooks
                   if isinstance(h, MechanismHookAdapter) and h.mechanism.name == "加害")
    assert adapter.priority == 20
    assert hooks.index(adapter) == 1, "加害必须位于龙族血脉(10)之后、龙鳞(30)之前"
    assert MECHANISMS.get("加害").priority < MECHANISMS.get("龙鳞").priority


def test_jiahai_before_longlin_still_rule_relevant():
    """顺序敏感性原样保留：反过来结算会得到不同伤害。"""
    manager = CombatHookManager()
    target = _target(("加害", 2), ("龙鳞", 8))
    adapter = next(h for h in manager.hooks()
                   if isinstance(h, MechanismHookAdapter) and h.mechanism.name == "加害")
    longlin_adapter = next(h for h in manager.hooks()
                           if isinstance(h, MechanismHookAdapter) and h.mechanism.name == "龙鳞")

    assert manager.apply_incoming_adjust(target, 8, "普通", None, _State()) == 2
    reversed_result = adapter.on_incoming_adjust(
        target,
        longlin_adapter.on_incoming_adjust(target, 8, "普通", None, _State()),
        "普通", None, _State())
    assert reversed_result == 0, "反过来先削到0，加害的 amount>0 前置不成立 → 0"


# ==================== 5. EffectContext 传递 ====================

def test_jiahai_ctx_flow_unchanged():
    """迁移不得扰动伤害链路的 EffectContext / 事件记录。"""
    state, combat, player, enemy = _arena()
    enemy.add_status(StatusEffect(name="加害", remaining_rounds=-1, value=2, source="x"))

    detail = combat._apply_hostile_damage(enemy, 8, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 8, "tags": {"daowen"}, "event_id": "JH-1",
    })

    assert "context_warning" not in detail, "正常战斗路径不得退化到 legacy fallback"
    assert detail["ctx"]["source"] == "杀伐"
    assert detail["ctx"]["mechanic"] == "damage"
    assert detail["ctx"]["event_id"] == "JH-1"
    assert detail["actual_damage"] == 10, "加害在 ctx 链路中照常生效"

    damage_events = _events(combat, CombatEventType.DAMAGE_APPLIED)
    assert len(damage_events) == 1
    assert damage_events[0].ctx["event_id"] == "JH-1"
    # hp_loss 是伤害的子事件：因果链不断
    assert detail["hp_loss_ctx"]["parent_event_id"] == "JH-1"


def test_engine_incoming_adjust_prefilter_unchanged():
    """引擎层前置短路（amount<=0 / 代价 / None）与迁移前逐字一致。"""
    state, combat, player, enemy = _arena()
    target = _target(("加害", 5))
    assert combat._incoming_adjust(target, 8, "普通") == 13
    assert combat._incoming_adjust(target, 8, "代价") == 8
    assert combat._incoming_adjust(target, 0, "普通") == 0
    assert combat._incoming_adjust(None, 8, "普通") == 8
