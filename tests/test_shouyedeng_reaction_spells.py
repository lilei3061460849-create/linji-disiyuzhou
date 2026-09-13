"""守夜灯法力与反应法术。

2026-09-13 用户改版：守夜灯从「[敌回始]授予[法限]50%、[敌回终]清空」
改为「[回始]授予[法限]10%、不清空」。授予因此发生在玩家回合开始，
进入怪物阶段时法力已在池中，静态校验不再需要预付预算——
原先 2026-08-21 那套「预计算 pending grant」的补丁随之作废。
本文件现在验证：授予量为 ceil(法限*0.1)、法力留存不清空、
以及不足时仍照常拒绝。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance, Relic, Spell
from tests.setup_support import finish_initial_daowen


def _engine(tmp_path):
    e = GameEngine(db_path=str(tmp_path / "t.db"), rng_seed=7,
                   sealed_candidate_path=str(tmp_path / "s.json"))
    e.execute_action("setup_attributes", {
        "name": "守夜者", "blood_points": 11, "speed_points": 8, "mana_points": 6})
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


def _monster_phase_submit(e, spell_x=None, use_spell=True):
    """构造怪物攻击玩家的提交：怪物普攻1次，玩家可提交先发制人反应。

    返回 (prepared, choice) 供 resolve 使用。
    """
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
    spell_choices = {timing: {} for timing in ("before", "after")}
    for sp in target_option.get("spell_options", {}).get("before", []) or []:
        if sp["spell_name"] == "先发制人" and use_spell:
            if spell_x is None:
                spell_x = 2
            spell_choices["before"]["先发制人"] = {
                "use": True,
                "cycles": [[{"x": spell_x, "target_ref": sp["steps"][0]["target_ref"],
                             "dodge": False}]],
            }
        else:
            spell_choices["before"][sp["spell_name"]] = {"use": False}
    for sp in target_option.get("spell_options", {}).get("after", []) or []:
        spell_choices["after"][sp["spell_name"]] = {"use": False}
    hits = [{"target_ref": "player:0", "dodge": False, "blood_shadow": False,
             "spell_choices": spell_choices} for _ in range(actor["base_hits_per_attack"])]
    choice = {"actor_ref": actor["actor_ref"], "daowen": None,
              "attack_actions": [{"hits": hits} for _ in range(actor["base_attack_actions"])]}
    return prepared, choice


def test_shouyedeng_mana_enables_reaction_spell(tmp_path):
    """[回始]守夜灯授予的法力可用于随后的怪物阶段反应法术。"""
    e = _engine(tmp_path)
    e.state.relics.append(Relic(name="守夜灯", effect="", tags=[]))
    p = e.state.player
    p.current_mana = 0
    p.mana_limit = 20  # 守夜灯授予 ceil(20*0.1)=2
    grant = e.combat._shouyedeng_pending_grant(p)
    assert grant == 2, f"守夜灯应授予2法力，实{grant}"
    e.combat._grant_shouyedeng(p)  # [回始]授予
    assert p.current_mana == 2
    prepared, choice = _monster_phase_submit(e, spell_x=2)  # 消耗2 ≤ 2
    res = e.combat.resolve_monster_phase([choice], prepared)
    # 先发制人应已触发（怪物受到反打伤害）
    fired = any(
        lg.get("spell") == "先发制人" and lg.get("execution")
        for hit in res for lg in (hit.get("spell_logs") or []))
    assert fired, "先发制人应使用守夜灯法力触发"
    # 执行后法力扣除正确：0 +2(守夜灯[回始]) -2(先发制人) = 0；不再有[敌回终]清空
    assert p.current_mana == 0, f"法力应只被法术消耗扣光，实{p.current_mana}"
    # 靶怪是[怪物]（读面板 1击×3伤），反打不抵消来袭：玩家应恰好掉 3 点。
    # 旧写法 "<= 57" 是把当时血限60硬编进断言（60-3=57）；血限随加点定价变成66后失效。
    assert p.current_hp == p.blood_limit - 3, \
        f"守夜灯法力被用于反打，玩家仍应受靶怪那 3 点伤害，实{p.blood_limit - p.current_hp}"


def test_shouyedeng_mana_persists_after_monster_phase(tmp_path):
    """守夜灯法力不再被清空：怪物阶段结束后没花掉的部分留在池里。"""
    e = _engine(tmp_path)
    e.state.relics.append(Relic(name="守夜灯", effect="", tags=[]))
    p = e.state.player
    p.current_mana = 0
    p.mana_limit = 20
    e.combat._grant_shouyedeng(p)  # [回始]授予 ceil(20*0.1)=2
    assert p.current_mana == 2
    prepared, choice = _monster_phase_submit(e, spell_x=1)  # 只花1法力
    res = e.combat.resolve_monster_phase([choice], prepared)
    assert not [x for x in res if x.get("type") == "shouyedeng_grant"], \
        "授予发生在[回始]，怪物阶段不应再授予"
    assert not [x for x in res if x.get("type") == "shouyedeng_clear"], \
        "守夜灯法力不再清空"
    assert p.current_mana == 1, f"2授予-1消耗=1应留存，实{p.current_mana}"


def test_shouyedeng_grants_once_per_round(tmp_path):
    """每回合只授予一次：同一回合重复调用不再发放。"""
    e = _engine(tmp_path)
    e.state.relics.append(Relic(name="守夜灯", effect="", tags=[]))
    p = e.state.player
    p.current_mana = 0
    p.mana_limit = 20
    assert e.combat._grant_shouyedeng(p) is not None
    assert e.combat._grant_shouyedeng(p) is None, "同回合第二次不应授予"
    assert p.current_mana == 2


def test_insufficient_real_mana_still_rejected(tmp_path):
    """守夜灯授予量不足以覆盖法术消耗 → 仍拒绝（不满足真实法力需求）。"""
    e = _engine(tmp_path)
    e.state.relics.append(Relic(name="守夜灯", effect="", tags=[]))
    p = e.state.player
    p.current_mana = 0
    p.mana_limit = 20
    e.combat._grant_shouyedeng(p)  # 授予2；先发制人 X=3 需3 > 2
    prepared, choice = _monster_phase_submit(e, spell_x=3)
    try:
        e.combat.resolve_monster_phase([choice], prepared)
        raise AssertionError("法力不足的提交应被拒绝")
    except ValueError as exc:
        assert "法力不足" in str(exc), f"应报法力不足：{exc}"
    assert not p.has_status("勾魂"), "被拒后无副作用"


def test_no_shouyedeng_still_rejects_when_mana_zero(tmp_path):
    """无守夜灯且当前法力=0 → 反应法术提交仍被拒绝（原行为保持）。"""
    e = _engine(tmp_path)
    p = e.state.player
    p.current_mana = 0
    prepared, choice = _monster_phase_submit(e, spell_x=2)
    try:
        e.combat.resolve_monster_phase([choice], prepared)
        raise AssertionError("无守夜灯法力时提交应被拒绝")
    except ValueError as exc:
        assert "法力不足" in str(exc), f"应报法力不足：{exc}"


def test_normal_mana_source_works_without_shouyedeng(tmp_path):
    """不影响普通法力来源：无守夜灯、法力充足时反应法术照常触发。"""
    e = _engine(tmp_path)
    p = e.state.player
    p.current_mana = 20  # 普通法力（如回始补满）
    prepared, choice = _monster_phase_submit(e, spell_x=3)
    res = e.combat.resolve_monster_phase([choice], prepared)
    fired = any(
        lg.get("spell") == "先发制人" and lg.get("execution")
        for hit in res for lg in (hit.get("spell_logs") or []))
    assert fired, "普通法力下先发制人应触发"
