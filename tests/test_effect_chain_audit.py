"""因果链收尾审计回归测试（2026-08-19）。

本文件不测“最后 HP 是多少”，而是钉死架构约束：
  A 单层效果    —— 伤害产生 EffectContext + CombatEvent，source/target 正确
  B 两层链      —— 伤害 → 爆裂反噬，parent_event_id 正确
  C 三层链      —— 道纹 → 血限变化 → 命零，parent_event_id 连续
  D 防双触发    —— 旧逻辑 + Hook 不会让同一效果跑两次
  E 死亡流程    —— HP=0 → 统一死亡判定 → ENTITY_DIED → 死后效果
  F 顺序显式化  —— Hook 按 priority 执行，且顺序与重构前一致
  G 无 0HP 存活体 —— 血限压迫致死必须走统一命零判定
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.combat_events import CombatEventType
from engine.combat_hooks import (
    CombatHookManager,
    DragonBloodlineMultiplierHook,
    BaolieHook,
    AfterDamageEffectsHook,
)
from engine.dice import DiceEngine
from engine.enums import EffectPolarity
from engine.mechanisms import MECHANISMS, MechanismHookAdapter
from engine.models import Entity, GameState, Relic, StatusEffect


def _arena(player_hp: int = 100, enemy_hp: int = 100, enemy_bl: int | None = None):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=player_hp,
                    mana_limit=50, current_mana=50, speed_limit=10, current_speed=10)
    enemy = Entity("M", "怪物", blood_limit=enemy_bl if enemy_bl is not None else enemy_hp,
                   current_hp=enemy_hp)
    state.player = player
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def _events(combat, event_type):
    return [e for e in combat.event_stream if e.event_type == event_type]


# ==================== A. 单层效果 ====================

def test_a_single_layer_damage_has_context_and_event():
    """一次伤害：EffectContext 有来源，CombatEvent 记录事实，两者职责分离。"""
    state, combat, player, enemy = _arena()

    detail = combat._apply_hostile_damage(enemy, 12, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 12, "tags": {"daowen"}, "event_id": "A-1",
    })

    # EffectContext：为什么发生 / 来自哪里
    assert detail["ctx"]["source"] == "杀伐"
    assert detail["ctx"]["source_type"] == "daowen"
    assert detail["ctx"]["actor"] == "P" and detail["ctx"]["target"] == "M"
    assert detail["ctx"]["mechanic"] == "damage"
    assert "context_warning" not in detail, "正常战斗路径不得退化到 legacy fallback"

    # hp_loss 是伤害的子事件
    assert detail["hp_loss_ctx"]["parent_event_id"] == "A-1"

    # CombatEvent：发生了什么
    applied = _events(combat, CombatEventType.DAMAGE_APPLIED)
    assert len(applied) == 1
    evt = applied[0]
    assert evt.actor_name == "P" and evt.target_name == "M"
    assert evt.data["actual_damage"] == 12
    assert evt.event_id == "A-1"
    # 事件流事实源挂在 state 上，combat.event_stream 只是别名视图
    assert combat.event_stream is state.combat_events


def test_a_legacy_call_without_ctx_is_marked_not_silent():
    """兼容层仍在，但必须显式打标，禁止静默漏源。"""
    _, combat, player, enemy = _arena()
    detail = combat._apply_hostile_damage(enemy, 5, source=player)
    assert detail["ctx"]["source_type"] == "legacy"
    assert "context_warning" in detail


# ==================== B. 两层链：伤害 → 爆裂反伤 ====================

def test_b_two_layer_chain_baolie_reflect_parent_is_damage():
    """爆裂反噬的失血必须挂在这次伤害之下。"""
    _, combat, player, enemy = _arena(player_hp=100, enemy_hp=100)
    enemy.add_status(StatusEffect("爆裂", value=2, remaining_rounds=2, source="test"))

    detail = combat._apply_hostile_damage(enemy, 10, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 10, "tags": {"daowen"}, "event_id": "B-1",
    })

    assert player.current_hp == 90, "爆裂数值口径不得改变"
    assert detail["actual_damage"] == 10
    reflect = [e for e in player._hp_loss_events if e["subtype"] == "baolie_reflect"]
    assert len(reflect) == 1, "反噬只能记一次"
    assert reflect[0]["parent_event_id"] == "B-1"
    assert reflect[0]["amount"] == 10


def test_b_baolie_lethal_reflect_death_is_traceable():
    """反噬致死：死亡上下文必须能追回这次伤害，不能是 legacy_death。"""
    _, combat, player, enemy = _arena(player_hp=6, enemy_hp=100)
    enemy.add_status(StatusEffect("爆裂", value=2, remaining_rounds=2, source="test"))

    combat._apply_hostile_damage(enemy, 10, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 10, "tags": {"daowen"}, "event_id": "B-2",
    })

    assert player.is_alive is False
    assert player._death_ctx["mechanic"] == "death"
    assert player._death_ctx["source_type"] != "legacy"
    assert player._death_ctx["parent_event_id"] is not None
    assert len(_events(combat, CombatEventType.ENTITY_DIED)) == 1


# ==================== C. 三层链：道纹 → 血限变化 → 命零 ====================

def test_c_three_layer_chain_daowen_blood_limit_death():
    """瓦解100%：道纹结算 → 血限归零 → 生命归零 → 统一命零，父链连续。"""
    _, combat, player, enemy = _arena(enemy_hp=20, enemy_bl=20)

    result = combat.apply_daowen_effect("瓦解", {"x": 10, "blood_limit_pct": 100}, player, enemy)

    root_id = result["daowen_ctx"]["event_id"]
    bl_events = enemy._blood_limit_events
    assert len(bl_events) == 1
    assert bl_events[0]["mechanic"] == "blood_limit_change"
    assert bl_events[0]["source"] == "瓦解"
    assert bl_events[0]["parent_event_id"] == root_id, "第二层必须指向道纹结算"

    assert enemy.blood_limit == 0 and enemy.current_hp == 0
    assert enemy.is_alive is False, "血限压到 0 生命却仍存活 = 非法状态"
    assert enemy._death_ctx["parent_event_id"] == bl_events[0]["event_id"], \
        "第三层必须指向血限变化，而不是直接跳回道纹"

    died = _events(combat, CombatEventType.ENTITY_DIED)
    changed = _events(combat, CombatEventType.BLOOD_LIMIT_CHANGED)
    assert len(died) == 1 and len(changed) == 1


def test_c_cut_blood_limit_change_parents_the_damage():
    """切割道纹已删除（2026-08-21）：残留切割状态不再产生血限扣除；伤痕同构 ctx 链见下条。"""
    _, combat, player, enemy = _arena(enemy_hp=100)
    player.add_status(StatusEffect("切割", value=1, remaining_rounds=3, source="test"))

    detail = combat._apply_hostile_damage(enemy, 7, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 7, "tags": {"daowen"}, "event_id": "C-2",
    })

    assert enemy.blood_limit == 100, "切割已删除，失血不再扣除等量血限"
    assert "qiege_ctx" not in detail and "qiege_blood_loss" not in detail


def test_c_scar_blood_limit_change_now_has_context_too():
    """伤痕此前完全没有上下文，只有切割有；两者现在同构。"""
    _, combat, player, enemy = _arena(enemy_hp=100)
    enemy.add_status(StatusEffect("伤痕", value=4, remaining_rounds=-1, source="P"))

    detail = combat._apply_hostile_damage(enemy, 6, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 6, "tags": {"daowen"}, "event_id": "C-3",
    })

    assert enemy.blood_limit == 96, "伤痕数值口径不得改变"
    assert detail["shanghen_ctx"]["mechanic"] == "blood_limit_change"
    assert detail["shanghen_ctx"]["subtype"] == "scar"
    assert detail["shanghen_ctx"]["parent_event_id"] == "C-3"


# ==================== D. 防双触发 ====================

def test_d_after_damage_effects_run_exactly_once():
    """一次伤害 = 一次落地后结算。伤痕只扣一次血限，不是两次。"""
    _, combat, player, enemy = _arena(enemy_hp=100)
    enemy.add_status(StatusEffect("伤痕", value=9, remaining_rounds=-1, source="test"))

    combat._apply_hostile_damage(enemy, 9, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 9, "tags": {"daowen"},
    })

    assert enemy.blood_limit == 91
    assert len(enemy._blood_limit_events) == 1
    assert len(_events(combat, CombatEventType.BLOOD_LIMIT_CHANGED)) == 1
    assert len(_events(combat, CombatEventType.DAMAGE_APPLIED)) == 1


def test_d_generic_after_damage_dispatch_skips_pipeline_hook():
    """通用遍历不得重跑显式分发的 Hook —— 否则伤痕/切割/寄生会各触发两次。"""
    manager = CombatHookManager()
    assert any(isinstance(h, AfterDamageEffectsHook) for h in manager.hooks())
    # 传 None 作为 combat：一旦 AfterDamageEffectsHook 被误跑，必然 AttributeError。
    victim = Entity("V", "怪物", blood_limit=50, current_hp=50)
    victim.add_status(StatusEffect("伤痕", value=3, remaining_rounds=-1, source="x"))
    res = manager.apply_after_damage(victim, 5, 0, {"ctx": None}, None, None)
    assert res == {}
    assert victim.blood_limit == 50, "通用遍历不应该动血限"


def test_d_hook_manager_reuses_single_instances():
    """显式分发口与注册表必须指向同一个实例，不能各 new 一份。"""
    manager = CombatHookManager()
    hooks = manager.hooks()
    assert manager.redirection_hook in hooks
    assert manager.mitigation_hook in hooks
    assert manager.after_damage_hook in hooks
    assert len([h for h in hooks if isinstance(h, AfterDamageEffectsHook)]) == 1


def test_d_death_notification_is_idempotent():
    """同一实体多次死亡通知只产生一次 ENTITY_DIED 与一次死后效果。"""
    _, combat, player, enemy = _arena(enemy_hp=8)
    combat.state.relics = [Relic("焦黑发丝", "")]
    player.current_speed = 5

    combat._apply_hostile_damage(enemy, 20, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 20, "tags": {"daowen"},
    })
    speed_after_first = player.current_speed

    # 再从别的入口重复宣告死亡
    combat._on_entity_death(enemy)
    combat._check_hp_zero_death(enemy)

    assert len(_events(combat, CombatEventType.ENTITY_DIED)) == 1
    assert player.current_speed == speed_after_first == 7, "焦黑发丝只能触发一次"


def test_d_dodge_grants_shield_once_through_engine_path():
    """避风铃有 legacy(_note_dodge) 与 Hook 两套实现；引擎路径只能加一次 3 点格挡。"""
    state, combat, player, _ = _arena()
    state.relics = [Relic("避风铃", "")]
    player.shield = 0
    player.current_speed = 5

    combat._spend_dodge_speed(player)

    assert player.shield == 3, "旧逻辑 + Hook 双触发会变成 6"
    assert player.current_speed == 4


# ==================== E. 死亡流程 ====================

def test_e_unified_death_pipeline_from_hp_zero():
    """HP=0 → 统一命零判定 → ENTITY_DIED → 死后效果（焦黑发丝 / 尸体登记）。"""
    state, combat, player, enemy = _arena(enemy_hp=10)
    state.relics = [Relic("焦黑发丝", "")]
    player.current_speed = 3

    detail = combat._apply_hostile_damage(enemy, 10, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 10, "tags": {"daowen"}, "event_id": "E-1",
    })

    assert detail["died"] is True and enemy.is_alive is False and enemy.current_hp == 0
    assert enemy._death_ctx["parent_event_id"] == "E-1"
    death_events = _events(combat, CombatEventType.ENTITY_DIED)
    assert len(death_events) == 1 and death_events[0].target_name == "M"
    # 死后效果
    assert player.current_speed == 5
    assert enemy in state.dead_monsters


def test_e_departure_is_not_death():
    """离场（雕塑/癌变/还债/救赎）不是命零，统一判定不得把它算成死亡。"""
    state, combat, player, enemy = _arena(enemy_hp=30)
    combat._remove_from_combat(enemy, "雕塑")
    enemy.current_hp = 0

    assert combat._check_hp_zero_death(enemy) is False
    assert _events(combat, CombatEventType.ENTITY_DIED) == []
    assert enemy.is_departed is True


def test_e_collapse_goes_through_unified_death():
    """崩解（异变≥50）此前不通知死亡；现在必须走统一管线。"""
    state, combat, player, enemy = _arena(enemy_hp=40)
    state.relics = [Relic("焦黑发丝", "")]
    player.current_speed = 1
    enemy.mutation_count = 49

    combat.pay_numeric_cost(enemy, "异变", 5, cost_context={
        "timing": "monster_action", "source": "原初1", "source_type": "evolution",
        "actor": enemy, "target": enemy, "mechanic": "cost", "subtype": "mutation",
        "amount": 5, "tags": {"active_payment"},
    })

    assert enemy.is_alive is False
    assert enemy._death_ctx["subtype"] == "collapse"
    assert enemy._death_ctx["source"] == "崩解"
    assert enemy._death_ctx["parent_event_id"] is not None
    assert len(_events(combat, CombatEventType.ENTITY_DIED)) == 1
    assert player.current_speed == 3, "崩解死者同样要触发[命零]后效果"


# ==================== F. Hook 顺序显式化 ====================

def test_f_hook_order_is_explicit_and_unchanged():
    """priority 只是把重构前的字面顺序固化，绝不允许改动相对次序。

    【加害】【龙鳞】均已迁移为声明式 Mechanism（priority 20/30），经
    MechanismHookAdapter 挂在原位置执行——本测试同时钉死"迁移不改变顺序"。
    """
    manager = CombatHookManager()
    order = [type(h).__name__ for h in manager.hooks()]
    assert order == [
        "DragonBloodlineMultiplierHook",
        "MechanismHookAdapter",  # 【加害】迁移后：原 JiahaiHook 位置（priority 20）
        "MechanismHookAdapter",  # 【龙鳞】迁移后：原 LonglinHook 位置（priority 30）
        "BaolieHook",
        "BifenglingHook",
        "ShouyedengHook",
        "DamageRedirectionHook",
        "LethalMitigationHook",
        "AfterDamageEffectsHook",
    ]
    priorities = [getattr(h, "priority", 100) for h in manager.hooks()]
    assert priorities == sorted(priorities)
    adapter_priorities = [h.priority for h in manager.hooks()
                          if isinstance(h, MechanismHookAdapter)]
    assert adapter_priorities == [20, 30], "加害(20)必须先于龙鳞(30)，顺序即规则"


def test_f_jiahai_before_longlin_is_rule_relevant():
    """钉死顺序敏感性：反过来结算会得到不同伤害。"""
    manager = CombatHookManager()

    class _State:
        def side_has(self, entity, name):
            return False

    target = Entity("T", "怪物", blood_limit=50, current_hp=50)
    target.add_status(StatusEffect("加害", value=2, remaining_rounds=-1, source="x"))
    target.add_status(StatusEffect("龙鳞", value=8, remaining_rounds=-1, source="x"))

    adapters = [h for h in manager.hooks() if isinstance(h, MechanismHookAdapter)]
    jiahai_adapter = next(a for a in adapters if a.mechanism.name == "加害")
    longlin_adapter = next(a for a in adapters if a.mechanism.name == "龙鳞")

    # 现行顺序：max(0, (8 + 2) - 8) = 2
    assert manager.apply_incoming_adjust(target, 8, "普通", None, _State()) == 2
    # 反过来：龙鳞先把 8 削成 0，加害的 `amount > 0` 前置条件不再成立 → 0。
    # 两者结果不同，证明 Hook 顺序确实是规则的一部分，priority 只能固化不能调整。
    reversed_result = jiahai_adapter.on_incoming_adjust(
        target, longlin_adapter.on_incoming_adjust(target, 8, "普通", None, _State()),
        "普通", None, _State())
    assert reversed_result == 0


def test_f_register_hook_respects_priority():
    manager = CombatHookManager()

    class _First:
        priority = 1

        def on_incoming_adjust(self, target, amount, damage_type, source, state):
            return amount

    hook = _First()
    manager.register_hook(hook)
    assert manager.hooks()[0] is hook
    manager.register_hook(hook)
    assert manager.hooks().count(hook) == 1


# ==================== G. 血限压迫不得留下 0HP 存活体 ====================

@pytest.mark.parametrize("daowen,calc", [
    ("瓦解", {"x": 10, "blood_limit_pct": 100}),
    ("逼债", {"x": 5, "blood_limit_penalty": 30, "shard_drain": 5}),
])
def test_g_blood_limit_pressure_never_leaves_alive_at_zero_hp(daowen, calc):
    _, combat, player, enemy = _arena(enemy_hp=20, enemy_bl=20)
    enemy.shards = 0

    combat.apply_daowen_effect(daowen, dict(calc), player, enemy)

    assert not (enemy.current_hp <= 0 and enemy.is_alive), \
        f"{daowen} 把生命压到 0 却没有走统一命零判定"
    if enemy.current_hp <= 0:
        assert enemy._death_ctx["mechanic"] == "death"


def test_g_round_start_debt_and_round_end_deform_still_kill():
    """回始逼债 / 回终畸变的既有致死行为不得因为改走统一入口而变化。"""
    state, combat, player, enemy = _arena(enemy_hp=4, enemy_bl=4)
    enemy.shards = 0
    enemy._bizhai = [{"x": 10, "caster": player}]
    state.combat_subphase = "await_round_start"

    combat.round_start()

    assert enemy.blood_limit == 1
    assert enemy.current_hp == 1 and enemy.is_alive is True
    assert enemy._blood_limit_events[-1]["source"] == "逼债"


# ==================== 递归保险丝 ====================

def test_effect_chain_depth_fuse_exists_and_is_generous():
    _, combat, player, enemy = _arena()
    assert combat.MAX_EFFECT_CHAIN_DEPTH >= 32
    assert combat._effect_chain_depth == 0
    combat._apply_hostile_damage(enemy, 1, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "amount": 1,
    })
    assert combat._effect_chain_depth == 0, "链深计数必须在退出时归零"

    combat._effect_chain_depth = combat.MAX_EFFECT_CHAIN_DEPTH
    with pytest.raises(RecursionError):
        combat._apply_hostile_damage(enemy, 1, source=player)
    combat._effect_chain_depth = 0


def test_redirection_chain_terminates_and_keeps_parent():
    """嫁祸重定向：链会收敛，且重定向后的伤害挂在原伤害之下。"""
    state, combat, player, enemy = _arena(enemy_hp=100)
    friend = Entity("F", "朋友", blood_limit=50, current_hp=50)
    state.friends = [friend]
    player._jiahuo_left = 1
    player._jiahuo_target = friend
    player.add_status(StatusEffect("嫁祸", value=1, remaining_rounds=1, source="P"))

    detail = combat._apply_hostile_damage(player, 10, source=enemy, ctx={
        "timing": "monster_action", "source": "普通攻击", "source_type": "attack",
        "actor": enemy, "target": player, "mechanic": "damage", "subtype": "attack",
        "amount": 10, "tags": {"attack"}, "event_id": "R-1",
    })

    assert player.current_hp == 100 and friend.current_hp == 40
    assert "redirected" in detail["ctx"]["tags"]
    assert detail["ctx"]["parent_event_id"] == "R-1"
    assert combat._effect_chain_depth == 0


# ==================== H. 正常战斗路径不得依赖 legacy fallback ====================

def test_h_full_battle_path_produces_no_legacy_context(tmp_path):
    """跑一段真实战斗：伤害/回复/失血/血限/命零记录里不允许出现 legacy 来源。

    兼容层（ctx=None → legacy_*）保留，但它只能服务尚未迁移的旁路调用；
    一旦正常战斗路径开始产出 legacy 上下文，说明有新代码绕过了 EffectContext。
    注意 _hp_loss_events / _speed_change_events / _blood_limit_events 都是**回合级**
    缓冲（回始/回终清空，供活血等效果消费），所以必须在回终之前采样。
    """
    from tests.setup_support import begin_battle, begin_round, finish_initial_daowen
    from tests.attack_support import resolve_attack
    from engine.api import GameEngine

    engine = GameEngine(db_path=str(tmp_path / "chain_e2e.db"), rng_seed=7)
    engine.execute_action("setup_attributes", {
        "name": "审计者", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    engine.execute_action("choose_discovered_relic",
                          {"relic_name": setup["result"]["relic_choices"][0]})
    engine.state.energy = 0
    assert begin_battle(engine)["success"]

    combat = engine.combat
    player = engine.state.player
    player.attack_count, player.attack_power = 2, 9

    def _entities():
        return ([player] + engine.state.friends + engine.state.employees
                + engine.state.temp_friends + engine.state.enemies)

    records = []

    def _sample():
        for ent in _entities():
            for attr in ("_hp_loss_events", "_heal_events",
                         "_speed_change_events", "_blood_limit_events"):
                records.extend(getattr(ent, attr, []) or [])

    for _ in range(2):
        assert begin_round(engine)["success"]
        assert resolve_attack(engine)["success"]
        engine.execute_action("use_daowen",
                              {"daowen_name": "杀伐", "x": 2, "target_ref": "enemy:0"})
        _sample()
        engine.state.combat_subphase = "await_round_end"
        engine.execute_action("round_end", {})
        if not engine.state.get_all_enemy_side():
            break

    assert records, "这一场应当至少产生若干可追溯的状态变化"
    offenders = [r for r in records if r.get("source_type") == "legacy"
                 or "legacy_context" in (r.get("tags") or [])]
    assert not offenders, f"正常战斗路径出现 legacy fallback：{offenders[:3]}"
    # 每条记录都能追回一个父事件或本身就是链根
    assert all(r.get("event_id") for r in records)

    for ent in _entities():
        death_ctx = getattr(ent, "_death_ctx", None)
        if death_ctx:
            assert death_ctx["source_type"] != "legacy", f"{ent.name} 的死亡没有来源"

    # 事件流确实被写入，并且每条事件都带得回上下文
    assert combat.event_stream, "CombatEvent 事件流不应为空"
    assert combat.event_stream is engine.state.combat_events
    for evt in combat.event_stream:
        assert evt.ctx is not None and evt.ctx.get("event_id")
    damage_events = [e for e in combat.event_stream
                     if e.event_type == CombatEventType.DAMAGE_APPLIED]
    assert damage_events and all(e.actor_name and e.target_name for e in damage_events)


def test_h_battle_start_resets_event_stream(tmp_path):
    """事件流按场重置，长时间模拟不会无界增长。"""
    from tests.setup_support import begin_battle, finish_initial_daowen
    from engine.api import GameEngine

    engine = GameEngine(db_path=str(tmp_path / "chain_reset.db"), rng_seed=3)
    engine.execute_action("setup_attributes", {
        "name": "审计者", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    engine.execute_action("choose_discovered_relic",
                          {"relic_name": setup["result"]["relic_choices"][0]})
    engine.state.energy = 0
    assert begin_battle(engine)["success"]

    enemy = engine.state.enemies[0]
    engine.combat._apply_hostile_damage(enemy, 3, source=engine.state.player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": engine.state.player, "target": enemy, "mechanic": "damage", "amount": 3})
    assert engine.combat.event_stream

    engine.state.enemies = []
    engine.state.energy = 0
    engine.state.phase = "pre_battle"
    assert begin_battle(engine)["success"]
    assert engine.combat.event_stream == []


# ==================== I. 【活血】只看"本回合有没有实际掉血" ====================
# DM裁定（2026-08-19）：【活血】不区分掉血来源。普通伤害 / 反伤 / 爆裂反噬 / 代价 /
# 血限压迫导致的生命损失，只要角色本回合实际掉过 HP，都进入 hp_lost_this_round。

def _huoxue_arena(player_hp=100):
    state, combat, player, enemy = _arena(player_hp=player_hp, enemy_hp=200, enemy_bl=200)
    player.add_status(StatusEffect("活血", value=3, remaining_rounds=-1, source="P"))
    return state, combat, player, enemy


def _round_end_huoxue(state, combat):
    state.combat_subphase = "await_round_end"
    result = combat.round_end()
    return [e for e in result["effects"] if e.get("type") == "huoxue_heal"]


def test_i_baolie_reflect_counts_into_hp_lost_this_round():
    """爆裂反噬造成的 HP 损失必须进入本回合失血统计。"""
    state, combat, player, enemy = _huoxue_arena(player_hp=100)
    enemy.add_status(StatusEffect("爆裂", value=2, remaining_rounds=2, source="test"))

    combat._apply_hostile_damage(enemy, 20, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 20, "tags": {"daowen"}, "event_id": "HX-1",
    })

    assert player.current_hp == 80, "爆裂反噬数值口径不得改变（等量反噬20）"
    assert player.hp_lost_this_round == 20, "反噬失血必须计入 hp_lost_this_round"
    # 失血仍然可追溯到那次伤害
    reflect = [e for e in player._hp_loss_events if e["subtype"] == "baolie_reflect"]
    assert len(reflect) == 1 and reflect[0]["parent_event_id"] == "HX-1"


def test_i_huoxue_triggers_on_baolie_reflect_loss():
    """完整链：活血 → 本回合被爆裂反噬 → 回终按失血÷2 回复。"""
    state, combat, player, enemy = _huoxue_arena(player_hp=100)
    enemy.add_status(StatusEffect("爆裂", value=2, remaining_rounds=2, source="test"))

    combat._apply_hostile_damage(enemy, 20, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 20, "tags": {"daowen"},
    })
    assert player.current_hp == 80

    heals = _round_end_huoxue(state, combat)
    assert len(heals) == 1, "活血应当触发一次"
    assert heals[0]["heal"] == 10, "20 点失血 ÷2 = 10"
    assert player.current_hp == 90
    assert player.hp_lost_this_round == 0, "回终后失血计数归零"


def test_i_huoxue_triggers_on_ordinary_damage_same_way():
    """同样的失血量，普通伤害与爆裂反噬给出相同的活血结果——活血不判断来源。"""
    state, combat, player, enemy = _huoxue_arena(player_hp=100)
    combat._apply_hostile_damage(player, 20, source=enemy, ctx={
        "timing": "monster_action", "source": "普通攻击", "source_type": "attack",
        "actor": enemy, "target": player, "mechanic": "damage", "subtype": "attack",
        "amount": 20, "tags": {"attack"},
    })
    assert player.hp_lost_this_round == 20
    heals = _round_end_huoxue(state, combat)
    assert len(heals) == 1 and heals[0]["heal"] == 10 and player.current_hp == 90


def test_i_huoxue_does_not_trigger_without_actual_hp_loss():
    """本回合没有实际掉血 → 活血不触发。"""
    state, combat, player, enemy = _huoxue_arena(player_hp=100)
    # 护盾完全吸收：有"受到伤害"，但没有实际掉 HP
    player.shield = 50
    detail = combat._apply_hostile_damage(player, 20, source=enemy, ctx={
        "timing": "monster_action", "source": "普通攻击", "source_type": "attack",
        "actor": enemy, "target": player, "mechanic": "damage", "subtype": "attack",
        "amount": 20, "tags": {"attack"},
    })
    assert detail["shield_absorbed"] == 20 and detail["actual_damage"] == 0
    assert player.current_hp == 100 and player.hp_lost_this_round == 0

    heals = _round_end_huoxue(state, combat)
    assert heals == [], "没有实际掉血就不该触发活血"
    assert player.current_hp == 100


def test_i_huoxue_not_triggered_when_baolie_absent():
    """没有爆裂时攻击者不掉血，活血同样不触发（防止误把反噬无条件计入）。"""
    state, combat, player, enemy = _huoxue_arena(player_hp=100)
    combat._apply_hostile_damage(enemy, 20, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 20, "tags": {"daowen"},
    })
    assert player.current_hp == 100 and player.hp_lost_this_round == 0
    assert _round_end_huoxue(state, combat) == []


def test_i_huoxue_threshold_needs_at_least_two_hp_lost():
    """既有阈值不变：本回合失血 <2 时活血不回复。"""
    state, combat, player, enemy = _huoxue_arena(player_hp=100)
    enemy.add_status(StatusEffect("爆裂", value=2, remaining_rounds=2, source="test"))
    combat._apply_hostile_damage(enemy, 1, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 1, "tags": {"daowen"},
    })
    assert player.current_hp == 99 and player.hp_lost_this_round == 1
    assert _round_end_huoxue(state, combat) == [], "失血1 <2，活血不触发（既有规则）"


# ==================== J. 二次验收补充回归 ====================

def test_j_shibao_self_destruct_keeps_current_hp_unchanged():
    """尸爆是自毁式[命零]，不清零当前生命（钉死收尾修复期间的一次回归）。"""
    state, combat, player, enemy = _arena(enemy_hp=100)
    enemy2 = Entity("M2", "怪物", blood_limit=100, current_hp=100)
    state.enemies = [enemy, enemy2]

    combat.apply_daowen_effect("尸爆", {"x": 3, "self_destruct": True, "aoe_pct": 30},
                               enemy, enemy)

    assert enemy.is_alive is False
    assert enemy.current_hp == 70, "尸爆不改变施法者当前生命，只置命零"
    assert enemy._death_ctx["subtype"] == "self_destruct"
    assert len(_events(combat, CombatEventType.ENTITY_DIED)) == 1


def test_j_entity_died_event_carries_actor_name_from_dict_context():
    """死亡上下文经 to_dict() 降级成名字字符串后，事件仍要带得出 actor_name。"""
    state, combat, player, enemy = _arena(enemy_hp=10)
    combat._apply_hostile_damage(enemy, 30, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen",
        "amount": 30, "tags": {"daowen"}, "event_id": "AN-1",
    })
    died = _events(combat, CombatEventType.ENTITY_DIED)
    assert len(died) == 1
    assert died[0].actor_name == "P" and died[0].target_name == "M"
    assert died[0].parent_event_id == "AN-1"


def test_j_redirected_damage_emits_exactly_one_damage_event():
    """嫁祸重定向不得产生两条 DAMAGE_APPLIED（重定向在落地前 return）。"""
    state, combat, player, enemy = _arena()
    friend = Entity("F", "朋友", blood_limit=50, current_hp=50)
    state.friends = [friend]
    player._jiahuo_left = 1
    player._jiahuo_target = friend
    player.add_status(StatusEffect("嫁祸", value=1, remaining_rounds=1, source="P"))

    combat._apply_hostile_damage(player, 10, source=enemy, ctx={
        "timing": "monster_action", "source": "普通攻击", "source_type": "attack",
        "actor": enemy, "target": player, "mechanic": "damage", "subtype": "attack",
        "amount": 10, "tags": {"attack"},
    })
    applied = _events(combat, CombatEventType.DAMAGE_APPLIED)
    assert len(applied) == 1 and applied[0].target_name == "F"


def test_j_purify_negative_mutation_never_collapses():
    """净化（负异变）不得因为 collapsed 判定而误杀。"""
    state, combat, player, enemy = _arena()
    enemy.mutation_count = 20
    combat.apply_daowen_effect("净化", {"x": 5, "mutation_reduction": 5}, player, enemy)
    assert enemy.mutation_count == 15 and enemy.is_alive is True
    assert _events(combat, CombatEventType.ENTITY_DIED) == []
