"""贯穿 / 回锋刀失速 / 守夜灯敌回 口径修正。

1. 贯穿：你造成的伤害（道纹/遗物/雕塑等）一律无视格挡，不只普攻。
2. 回锋刀：每失去1点当前速度后对显式[目标]造成3点伤害；折速疲惫必须提交目标。
3. 守夜灯：[敌回始]加法限50%，该法力[敌回终]清空；回始只获得法限。

每条覆盖正常路径 / 边界 / 非法输入。
"""
from __future__ import annotations

import math
import os
import sys

import pytest

from tests.setup_support import begin_battle, begin_round, finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity, Relic, StatusEffect


def _engine(tmp_path, suffix="fix"):
    e = GameEngine(db_path=str(tmp_path / f"{suffix}.db"), rng_seed=3)
    e.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = e.execute_action("setup_choose_region", {"region": "罪孽都市"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.energy = 0
    return e


def _give(entity, name, x=1):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
        x_value=x,
    )


def _monster_phase_no_attack(engine):
    prepared = engine.execute_action("prepare_monster_phase", {})
    assert prepared["success"], prepared
    choices = []
    for actor in prepared["result"]["actors"]:
        hits = [[{"target_ref": "player:0", "dodge": False, "blood_shadow": False,
                  "spell_choices": {"before": {}, "after": {}}}
                 for _ in range(actor["base_hits_per_attack"])]
                for _ in range(actor["base_attack_actions"])]
        choices.append({
            "actor_ref": actor["actor_ref"], "daowen": None,
            "attack_actions": [{"hits": h} for h in hits],
        })
    return engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": choices,
    })


# ---------- 贯穿 ----------

def test_pierce_shaifa_ignores_shield(tmp_path):
    """正常路径：持贯穿后杀伐打有格挡目标，格挡不动、生命按伤害扣除。"""
    e = _engine(tmp_path, "pierce_ok")
    begin_battle(e)
    begin_round(e)
    p, m = e.state.player, e.state.enemies[0]
    m.shield = 40
    m.current_hp = m.blood_limit = 80
    p.add_status(StatusEffect(name="贯穿", value=1, remaining_rounds=-1, source="test"))
    p.current_mana = 20
    hp, sh = m.current_hp, m.shield
    r = e.execute_action("use_daowen", {
        "daowen_name": "杀伐", "x": 10, "target_ref": "enemy:0",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert r["success"], r
    assert m.shield == sh, "贯穿伤害不得消耗格挡"
    assert m.current_hp == hp - 5 * 10   # 杀伐X：5X 伤害（DM裁定 2026-09-10，原 2X）


def test_pierce_does_not_rewrite_cost_damage(tmp_path):
    """边界：贯穿不把代价改成无视格挡；流血代价仍按代价结算。"""
    e = _engine(tmp_path, "pierce_bound")
    begin_battle(e)
    p = e.state.player
    p.add_status(StatusEffect(name="贯穿", value=1, remaining_rounds=-1, source="test"))
    p.shield = 30
    hp = p.current_hp
    e.combat.pay_numeric_cost(p, "流血", 8)
    assert p.shield == 30
    assert p.current_hp == hp - 8


def test_pierce_absent_still_blocked_by_shield(tmp_path):
    """非法/对照：没有贯穿时杀伐仍被格挡吸收。"""
    e = _engine(tmp_path, "pierce_no")
    begin_battle(e)
    begin_round(e)
    p, m = e.state.player, e.state.enemies[0]
    m.shield = 40
    m.current_hp = 80
    p.current_mana = 20
    r = e.execute_action("use_daowen", {
        "daowen_name": "杀伐", "x": 10, "target_ref": "enemy:0",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert r["success"], r
    dmg = 5 * 10                     # 杀伐X：5X（DM裁定 2026-09-10）；格挡40吸收40，余10落到生命
    assert m.shield == max(0, 40 - dmg)
    assert m.current_hp == 80 - max(0, dmg - 40)


# ---------- 回锋刀 + 疲惫类战始遗物 ----------
# 原用【折速法印】(疲惫X→6X法力) 驱动失速；该遗物已于 2026-09-13 删除
# （效果改为道纹【搏命】），改用同为疲惫代价的【苍白之花】(固定疲惫5)。

def test_zhesu_fatigue_triggers_huifeng(tmp_path):
    """正常路径：苍白之花疲惫5失去5速（速限不足则清零），回锋刀按实际失速打伤害。"""
    e = _engine(tmp_path, "hf_ok")
    e.state.relics.append(Relic("苍白之花", ""))
    e.state.relics.append(Relic("回锋刀", ""))
    p = e.state.player
    # 苍白之花固定疲惫5：当前速度必须付得起（[战始]按速限给满，故两者同抬）
    p.speed_limit = max(p.speed_limit, 8)
    p.current_speed = p.speed_limit
    limit = p.speed_limit
    begin_battle(e, relic_choices={
        "苍白之花": {"use": True},
        "回锋刀": {"enemy_index": 0},
    })
    m = e.state.enemies[0]
    assert p.current_speed == limit - 5
    assert m.current_hp == m.blood_limit - 15   # 失速5 × 每点3伤


def test_zhesu_without_target_is_atomic(tmp_path):
    """非法：持回锋刀发动疲惫类遗物却不提交目标，战始失败且不扣速、不加力。"""
    e = _engine(tmp_path, "hf_illegal")
    e.state.relics = [Relic("苍白之花", ""), Relic("回锋刀", "")]
    p = e.state.player
    p.speed_limit = max(p.speed_limit, 8)
    p.current_speed = p.speed_limit
    speed, mana, battle = p.current_speed, p.current_mana, e.state.current_battle
    bad = begin_battle(e, relic_choices={
        "苍白之花": {"use": True},
    })
    assert not bad["success"]
    assert "回锋刀" in bad["error"]
    assert p.current_speed == speed
    assert p.current_mana == mana
    assert e.state.current_battle == battle


def test_zhesu_declined_does_not_need_huifeng_target(tmp_path):
    """边界：疲惫类遗物显式拒绝时不必提交回锋刀目标，也不造伤。"""
    e = _engine(tmp_path, "hf_bound")
    e.state.relics.append(Relic("苍白之花", ""))
    e.state.relics.append(Relic("回锋刀", ""))
    r = begin_battle(e, relic_choices={
        "苍白之花": {"use": False},
    })
    assert r["success"], r
    m = e.state.enemies[0]
    assert e.state.player.current_speed == e.state.player.speed_limit
    assert m.current_hp == m.blood_limit


def test_round_start_gap_damage_is_separate_from_zhesu(tmp_path):
    """边界：失速即时伤与回始缺口伤是两条独立条款，不互相吞掉。"""
    e = _engine(tmp_path, "hf_gap")
    e.state.relics.append(Relic("苍白之花", ""))
    e.state.relics.append(Relic("回锋刀", ""))
    e.state.player.speed_limit = max(e.state.player.speed_limit, 8)
    e.state.player.current_speed = e.state.player.speed_limit
    begin_battle(e, relic_choices={
        "苍白之花": {"use": True},
        "回锋刀": {"enemy_index": 0},
    })
    m = e.state.enemies[0]
    after_bs = m.current_hp
    begin_round(e, relic_choices={"回锋刀": {"enemy_index": 0}})
    assert after_bs == m.blood_limit - 15      # 战始失速5 × 每点3伤
    assert m.current_hp == after_bs - 15        # 回始按缺口(速限-当前速度)=5 再打15


# ---------- 守夜灯 ----------

def test_lamp_grants_at_enemy_turn_and_clears_that_amount(tmp_path):
    """正常路径：回始只加法限；敌回始+50%；敌回终只清该授予量。"""
    e = _engine(tmp_path, "lamp_ok")
    e.state.relics.append(Relic("守夜灯", ""))
    begin_battle(e)
    begin_round(e)
    p = e.state.player
    assert p.current_mana == p.mana_limit
    limit = p.mana_limit
    half = math.ceil(limit / 2)
    phase = _monster_phase_no_attack(e)
    assert phase["success"], phase
    # 敌回终已清授予量，剩下回始法力（怪物若未消耗玩家法力）。
    assert p.current_mana == limit
    grants = [d for d in phase["result"]["details"] if d.get("type") == "shouyedeng_grant"]
    clears = [d for d in phase["result"]["details"] if d.get("type") == "shouyedeng_clear"]
    assert grants and grants[0]["gained"] == half
    assert clears and clears[0]["granted"] == half


def test_lamp_spent_partially_leaves_remainder(tmp_path):
    """边界：敌回始21，花掉10，敌回终扣7，剩4。"""
    e = _engine(tmp_path, "lamp_bound")
    e.state.relics.append(Relic("守夜灯", ""))
    p = e.state.player
    p.mana_limit = 14
    p.current_mana = 14
    e.combat._grant_shouyedeng(p)
    assert p.current_mana == 21
    p.current_mana -= 10
    e.combat._clear_shouyedeng(p)
    assert p.current_mana == 4


def test_lamp_does_not_grant_without_enemy_turn(tmp_path):
    """非法/对照：空场没有敌回，回始不加守夜灯，resolve 也不授予。"""
    e = _engine(tmp_path, "lamp_empty")
    e.state.relics.append(Relic("守夜灯", ""))
    begin_battle(e)
    e.state.enemies.clear()
    begin_round(e)
    p = e.state.player
    assert p.current_mana == p.mana_limit
    phase = _monster_phase_no_attack(e)
    assert phase["success"], phase
    assert p.current_mana == p.mana_limit
    assert not any(d.get("type") == "shouyedeng_grant" for d in phase["result"]["details"])
