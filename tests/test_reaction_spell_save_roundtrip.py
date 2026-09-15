"""反应法术冻结字段的存档往返（2026-09-15 修复的回归用例）。

缺陷：`CombatEngine._freeze_spell_decision_branch` 曾把 **CombatEngine 活引用**
写进调用方的法术决策 dict（`_engine_branch_owner`）。该 dict 会随 params 进入
`action_history`，于是任何一次反应法术结算之后 `save_game()` 的 pickle 直接抛
`Can't pickle local object 'all_.<locals>.cond'`（MECHANISMS 里的闭包），
整局存档失败——引擎其余部分完全正常，属于纯序列化缺陷。

修复：归属标记改存字符串（`_branch_owner_token`），语义不变（同一进程内同一
实例仍能通过校验），冻结字段随存档可序列化。

本文件验证：
1. 反应法术结算后 `save_game` 成功、`load_game` 能读回同一局面；
2. 冻结字段本身可 pickle（`action_history` 不再夹带活对象）。
"""
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity, Spell
from tests.setup_support import finish_initial_daowen


def _engine(tmp_path):
    e = GameEngine(db_path=str(tmp_path / "t.db"), rng_seed=11,
                   sealed_candidate_path=str(tmp_path / "s.json"))
    e.execute_action("setup_attributes", {
        "name": "白夜", "blood_points": 7, "speed_points": 8, "mana_points": 10})
    finish_initial_daowen(e)  # 开局：杀伐
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    p = e.state.player
    p.dao_wen["杀伐"] = DaoWenInstance(
        DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""), x_value=0)
    p.spells.append(Spell(name="先发制人", required_daowen=["杀伐"],
                          trigger_condition="受到伤害前",
                          effect_flow="受到伤害前→发动杀伐 X"))
    return e


def _fire_reaction_spell(e, spell_x=2):
    """让靶怪普攻玩家，玩家用【先发制人】反打一次；返回 resolve 明细。"""
    m = Entity(name="靶怪", entity_type="怪物", blood_limit=200, current_hp=200,
               attack_count=1, attack_power=3)
    e.state.enemies.append(m)
    e.combat.reset_monster_activation()
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    e.state.current_round = 2
    prepared = e.combat.prepare_monster_phase()
    actor = next(a for a in prepared["actors"] if a["actor_ref"] == "enemy:0")
    target_option = next(t for t in actor["attack_target_options"] if t["ref"] == "player:0")
    before = {sp["spell_name"]: {"use": False}
              for sp in target_option.get("spell_options", {}).get("before", []) or []}
    steps = next((sp["steps"] for sp in target_option.get("spell_options", {}).get("before", []) or []
                  if sp["spell_name"] == "先发制人"), None)
    assert steps, "靶怪攻击的提交里应带出【先发制人】候选"
    before["先发制人"] = {"use": True,
                       "cycles": [[{"x": spell_x, "target_ref": steps[0]["target_ref"],
                                    "dodge": False}]]}
    after = {sp["spell_name"]: {"use": False}
             for sp in target_option.get("spell_options", {}).get("after", []) or []}
    hits = [{"target_ref": "player:0", "dodge": False, "blood_shadow": False,
             "spell_choices": {"before": dict(before), "after": dict(after)}}
            for _ in range(actor["base_hits_per_attack"])]
    choice = {"actor_ref": actor["actor_ref"], "daowen": None,
              "attack_actions": [{"hits": hits} for _ in range(actor["base_attack_actions"])]}
    res = e.combat.resolve_monster_phase([choice], prepared)
    return res, choice


def test_reaction_spell_freezes_branch_with_string_owner(tmp_path):
    """冻结字段必须是可序列化的字符串标记，而非引擎活引用。"""
    e = _engine(tmp_path)
    res, choice = _fire_reaction_spell(e)
    fired = [lg for hit in res for lg in (hit.get("spell_logs") or [])
             if lg.get("spell") == "先发制人"]
    assert fired, "先发制人应当实际触发"
    owner = choice["attack_actions"][0]["hits"][0]["spell_choices"]["before"]["先发制人"]["_engine_branch_owner"]
    assert isinstance(owner, str), f"归属标记必须是字符串，实{type(owner).__name__}"
    pickle.dumps(choice)  # 冻结字段本身可序列化（缺陷期间这里会抛异常）


def test_reaction_spell_save_and_load_roundtrip(tmp_path):
    """反应法术结算后仍能存档、读档（缺陷期间此处 pickle 直接抛异常）。"""
    e = _engine(tmp_path)
    _fire_reaction_spell(e)
    saved = e.save_game("rt")
    assert saved.get("success"), f"存档应成功：{saved}"
    assert os.path.exists(saved["filepath"])
    pickle.dumps(e._action_history)  # 历史里不得再夹带活对象
    loaded = e.load_game("rt")
    assert loaded.get("success"), f"读档应成功：{loaded}"
    assert e.state.player.name == "白夜"
    assert e.state.player.current_hp == 42 - 3, "靶怪那 3 点伤害应已落地并被存档保留"
