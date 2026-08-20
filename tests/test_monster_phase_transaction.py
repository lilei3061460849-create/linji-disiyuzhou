"""怪物阶段事务一致性回归（2026-08-19 P0）。

目标：任何非法 resolve 输入不得留下任何战斗副作用。
覆盖：非法 hits / 非法 attack schema / 非法 dodge / 非法·重复使用本回合道纹；
合法提交行为完全不变。非法提交前后 round_used、道纹层数、资源、HP、事件流逐项一致。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.combat_events import CombatEventType
from engine.dice import DiceEngine
from engine.models import DaoWen, DaoWenInstance, Entity
from tests.setup_support import finish_initial_daowen
from tests.monster_phase_support import resolve_monster_phase as advance_round1


def _arena(seed: int = 1):
    """罪孽都市（赎金是罪孽专属）开战并推进到第 2 回合，怪物持有 赎金。"""
    import tempfile
    save_dir = tempfile.mkdtemp(prefix="txn")
    e = GameEngine(db_path=os.path.join(save_dir, "g.db"), rng_seed=seed,
                   save_dir=save_dir)
    e.execute_action("setup_attributes", {"name": "L", "blood_points": 10,
                                          "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "罪孽都市"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.energy = 0
    assert e.execute_action("battle_start", {"relic_choices": {}})["success"]
    m = e.state.enemies[0]
    m.dao_wen["赎金"] = DaoWenInstance(
        DaoWen(name="赎金", formula="", cost_type="", cost_formula="", effect_formula=""),
        x_value=2)
    m.shards = 100
    # 第 1 回合（白板）走完整两阶段 API
    assert e.execute_action("round_start", {"relic_choices": {}})["success"]
    prep1 = e.execute_action("prepare_monster_phase", {})
    assert prep1["success"], prep1
    from tests.monster_phase_support import _decline_spells as _ds
    choices1 = []
    for a in prep1["result"]["actors"]:
        tgt = a["attack_target_options"][0]["ref"]
        to = next(t for t in a["attack_target_options"] if t["ref"] == tgt)
        hits = [{"target_ref": tgt, "dodge": False, "blood_shadow": False,
                 "spell_choices": _ds(to)} for _ in range(a["base_hits_per_attack"])]
        choices1.append({"actor_ref": a["actor_ref"], "daowen": None,
                         "attack_actions": [{"hits": hits}
                                            for _ in range(a["base_attack_actions"])]})
    assert e.execute_action("resolve_monster_phase",
                            {"token": prep1["result"]["token"],
                             "choices": choices1})["success"]
    assert e.execute_action("round_end", {})["success"]
    # 第 2 回合：怪物可发动道纹
    assert e.execute_action("round_start", {"relic_choices": {}})["success"]
    prep = e.execute_action("prepare_monster_phase", {})
    assert prep["success"], prep
    return e, m, prep


def _snapshot(e):
    """记录可对比的战斗状态快照（事件流只保留类型序列，timestamp 忽略）。

    注意：失败回滚后实体对象会被快照副本替换，必须从 e.state 重读，
    不能持有失败前的旧引用。
    """
    m = e.state.enemies[0]
    return {
        "round_used": sorted(e.combat._monster_round_used(m)),
        "activated": sorted(e.combat._monster_activated.get(id(m), set())),
        "x": {k: v.x_value for k, v in m.dao_wen.items()},
        "monster_shards": m.shards,
        "player_shards": e.state.shards,
        "player_hp": e.state.player.current_hp,
        "player_mana": e.state.player.current_mana,
        "player_speed": e.state.player.current_speed,
        "player_shield": e.state.player.shield,
        "monster_actions_used": m.actions_used_this_round,
        "events": [(ev.event_type.value, ev.actor_name, ev.target_name)
                   for ev in e.state.combat_events],
    }


def _actors(prep):
    return {a["actor_ref"]: a for a in prep["result"]["actors"]}


def _decline_spells(option):
    return {
        timing: {sp["spell_name"]: {"use": False}
                 for sp in option.get("spell_options", {}).get(timing, [])}
        for timing in ("before", "after")
    }


def _legal_hits(engine, prep, actor):
    """按 prepare 快照构造合法攻击（hits 数 = 道纹执行后 attack_count，变形除外）。"""
    a = _actors(prep)[actor]
    hits_per = max(0, engine.state.enemies[int(actor.split(":", 1)[1])].attack_count
                   - engine.state.enemies[int(actor.split(":", 1)[1])].get_status_value("手雷减攻"))
    target_ref = a["attack_target_options"][0]["ref"]
    target_option = next(t for t in a["attack_target_options"] if t["ref"] == target_ref)
    return [{"hits": [{"target_ref": target_ref, "dodge": False, "blood_shadow": False,
                       "spell_choices": _decline_spells(target_option)}
                      for _ in range(hits_per)]}
            for _ in range(a["base_attack_actions"])]


def test_illegal_hits_count_rolls_back_all_side_effects():
    """非法 hits（数量不足）：道纹已执行（赎金夺碎片）后动态校验失败 → 必须整体回滚。"""
    e, m, prep = _arena()
    actor = next(iter(_actors(prep)))
    before = _snapshot(e)

    # 合法道纹 + 攻击只提交 1 次命中（schema 要求 2 次以上）→ 执行阶段动态失败
    choice = {
        "actor_ref": actor,
        "daowen": {"name": "赎金", "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {}, "target_ref": "player:0"},
        "attack_actions": [{"hits": [{"target_ref": "player:0", "dodge": False,
                                      "blood_shadow": False,
                                      "spell_choices": {"before": {}, "after": {}}},
                                     {"target_ref": "player:0", "dodge": False,
                                      "blood_shadow": False,
                                      "spell_choices": {"before": {}, "after": {}}}]}],
    }
    with pytest.raises(ValueError):
        e.combat.resolve_monster_phase([choice], prep["result"])

    after = _snapshot(e)
    assert after == before, f"非法 hits 必须零副作用:\n{before}\n{after}"


def test_illegal_attack_actions_count_no_side_effects():
    """非法 attack schema（attack_actions 数量错误）：静态校验执行前拦截，道纹不得执行。"""
    e, m, prep = _arena()
    actor = next(iter(_actors(prep)))
    before = _snapshot(e)

    legal_hits = _legal_hits(e, prep, actor)
    # 故意少提交一个 attack_action（合法数量 +1）
    choice = {
        "actor_ref": actor,
        "daowen": {"name": "赎金", "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {}, "target_ref": "player:0"},
        "attack_actions": legal_hits + legal_hits[:1],
    }
    with pytest.raises(ValueError):
        e.combat.resolve_monster_phase([choice], prep["result"])

    after = _snapshot(e)
    assert after == before, "非法 attack_actions 数量必须零副作用"


def test_illegal_dodge_no_speed_rolls_back():
    """非法 dodge（目标速度 0 仍提交闪避）：执行阶段动态校验失败 → 整体回滚。"""
    e, m, prep = _arena()
    actor = next(iter(_actors(prep)))
    m.dao_wen.pop("必中", None)   # 移除必中，让 dodge 的速度校验真正生效
    assert e.combat.bizhong_remaining(m) == 0
    e.state.player.current_speed = 0
    before = _snapshot(e)

    legal_hits = _legal_hits(e, prep, actor)
    # 把第一个 hit 改成 dodge=True（速度 0 → 非法）
    legal_hits[0]["hits"][0]["dodge"] = True
    choice = {
        "actor_ref": actor,
        "daowen": {"name": "赎金", "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {}, "target_ref": "player:0"},
        "attack_actions": legal_hits,
    }
    with pytest.raises(ValueError):
        e.combat.resolve_monster_phase([choice], prep["result"])

    after = _snapshot(e)
    assert after == before, "非法 dodge 必须零副作用（含速度被回滚为 0）"


def test_illegal_reused_daowen_this_round_no_side_effects():
    """非法/重复使用本回合道纹：round_used 已含该道纹仍提交 → 静态校验拦截，零副作用。"""
    e, m, prep = _arena()
    actor = next(iter(_actors(prep)))
    # 模拟本回合已用过 赎金（引擎记录 round_used）
    e.combat._monster_round_used(m).add("赎金")
    before = _snapshot(e)

    choice = {
        "actor_ref": actor,
        "daowen": {"name": "赎金", "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {}, "target_ref": "player:0"},
        "attack_actions": _legal_hits(e, prep, actor),
    }
    with pytest.raises(ValueError):
        e.combat.resolve_monster_phase([choice], prep["result"])

    after = _snapshot(e)
    assert after == before, "重复使用本回合道纹必须零副作用"


def test_api_level_failure_leaves_no_side_effects():
    """API 层（execute_action）非法提交：返回 success=False，战斗状态零副作用。"""
    e, m, prep = _arena()
    actor = next(iter(_actors(prep)))
    before = _snapshot(e)

    choice = {
        "actor_ref": actor,
        "daowen": {"name": "赎金", "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {}, "target_ref": "player:0"},
        "attack_actions": [{"hits": [{"target_ref": "player:0", "dodge": False,
                                      "blood_shadow": False,
                                      "spell_choices": {"before": {}, "after": {}}},
                                     {"target_ref": "player:0", "dodge": False,
                                      "blood_shadow": False,
                                      "spell_choices": {"before": {}, "after": {}}}]}],
    }
    res = e.execute_action("resolve_monster_phase",
                           {"token": prep["result"]["token"], "choices": [choice]})
    assert res["success"] is False, "非法 hits 必须被拒绝"

    after = _snapshot(e)
    assert after == before, "API 层非法提交必须零副作用"


def test_legal_submission_behavior_unchanged():
    """合法提交行为完全不变：道纹生效、攻击结算、事件流正常。"""
    e, m, prep = _arena()
    actor = next(iter(_actors(prep)))
    e.state.shards = 100
    m.shards = 100

    choice = {
        "actor_ref": actor,
        "daowen": {"name": "赎金", "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {}, "target_ref": "player:0"},
        "attack_actions": _legal_hits(e, prep, actor),
    }
    results = e.combat.resolve_monster_phase([choice], prep["result"])

    # 道纹已激活：round_used 含 赎金，X 递增（+2×副本阶级）
    assert "赎金" in e.combat._monster_round_used(m)
    # 赎金X=2 → 夺 10X=20 碎片：玩家 100→80，怪物 100→120（caster 入账）
    assert m.shards == 120, f"赎金应入账 20: {m.shards}"
    assert e.state.shards == 80
    # 攻击结算存在
    attack_rows = [r for r in results if r.get("attacker") == m.name
                   and "damage_dealt" in r]
    assert attack_rows, "合法提交必须结算攻击"
    # 事件流有伤害事件
    assert any(ev.event_type == CombatEventType.DAMAGE_APPLIED
               for ev in e.state.combat_events)
