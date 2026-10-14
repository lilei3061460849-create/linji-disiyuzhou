"""贯穿 / 回锋刀失速 / 守夜灯敌回 口径修正。

1. 贯穿：你造成的伤害（道纹/遗物/雕塑等）一律无视格挡，不只普攻。
2. 回锋刀：每失去1点当前速度后对显式[目标]造成3点伤害；折速疲惫必须提交目标。
3. 守夜灯：[回始]加[法限]10%的法力，不再清空（2026-09-13 用户改版）。

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
        # 2026-09-15 删除白板后：有合法道纹选项就必须提交一个（本局怪物普遍有道纹）
        dw = None
        if actor.get("daowen_required"):
            opt = next((o for o in actor["daowen_options"] if o["name"] != "变形"),
                       actor["daowen_options"][0])
            dw = {"name": opt["name"], "dodge": False, "blood_shadow": False,
                  "trigger_spell_choices": {}}
            if opt.get("requires_target"):
                dw["target_ref"] = "player:0"
        choices.append({
            "actor_ref": actor["actor_ref"], "daowen": dw,
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
    assert m.current_hp == max(0, hp - 10 * 10)   # 杀伐X：X²（2026-09-13，原 5X）


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
    dmg = 10 * 10                    # 杀伐X：X²（2026-09-13）；格挡40吸收40，余60落到生命
    assert m.shield == max(0, 40 - dmg)
    assert m.current_hp == 80 - max(0, dmg - 40)


# ---------- 回锋刀 ----------
# 2026-10-11 清理：这里原有 3 条用【苍白之花】战始疲惫5 驱动失速来测回锋刀的用例。
# 该战始触发点是硬编码的遗物联动（回锋刀挂在「苍白之花是否付疲惫5」上），
# 索引里回锋刀只有「每失去1点速度后」与[回始]两个触发点，故按 2026-10-11 用户令
# 「按照索引重写程序」删除该联动，3 条用例随之删除。
# 两个合法触发点的覆盖：失速即时伤见 tests/test_speed_context_compat.py，
# 洗劫截取回锋刀伤害见 tests/test_relic_field_triggers.py，回始缺口伤见下面这条。







def test_round_start_gap_damage_is_separate_from_zhesu(tmp_path):
    """边界：失速即时伤与回始缺口伤是两条独立条款，不互相吞掉。

    2026-10-11：失速来源由已删除的战始联动改为战中失速，条款本身不变。
    """
    e = _engine(tmp_path, "hf_gap")
    e.state.relics.append(Relic("回锋刀", ""))
    p = e.state.player
    p.speed_limit = max(p.speed_limit, 8)
    p.current_speed = p.speed_limit
    begin_battle(e, relic_choices={})
    # 2026-10-12 出怪下界 1→2：第1场可能出 2 只怪。本条要验的是「失速即时伤」与
    # 「回始缺口伤」两条独立条款的数值，靶子必须唯一且活得过 30 点总伤，
    # 否则第二只怪参战/靶子提前阵亡都会把断言变成 0 == 负数。
    e.state.enemies[:] = e.state.enemies[:1]
    e.state.monster_reinforcements = []
    m = e.state.enemies[0]
    m.blood_limit = max(m.blood_limit, 200)
    m.current_hp = m.blood_limit
    # 战中失速5点（不依赖任何战始遗物联动）→ 回锋刀按每点3伤即时结算
    e.combat._lose_current_speed(p, 5, "enemy:0", require_huifeng=True, ctx={
        "timing": "player_action", "source": "普通攻击", "source_type": "attack",
        "actor": p, "target": p, "mechanic": "speed_change", "subtype": "current_speed",
        "amount": -5, "tags": set(),
    })
    after_loss = m.current_hp
    begin_round(e, relic_choices={"回锋刀": {"enemy_index": 0}})
    assert after_loss == m.blood_limit - 15     # 失速5 × 每点3伤
    assert m.current_hp == after_loss - 15      # 回始按缺口(速限-当前速度)=5 再打15


def test_round_start_huifeng_fizzles_when_no_enemy_alive(tmp_path):
    """边界（2026-10-14 修 bone_angel/wushen 红）：回锋刀回始缺口伤>0，但场上
    无存活敌人（如增援未到）→ 不发 demand、直接哑火，不得整单报错。

    引擎此前要求不可能的提交（可选[]），整局 sim 被误标 invalid。
    """
    e = _engine(tmp_path, "hf_empty")
    e.state.relics.append(Relic("回锋刀", ""))
    p = e.state.player
    p.speed_limit = max(p.speed_limit, 8)
    p.current_speed = p.speed_limit - 5  # 缺口5 → 缺口伤15
    begin_battle(e, relic_choices={})
    e.state.enemies[:] = e.state.enemies[:1]
    m = e.state.enemies[0]
    m.is_alive = False
    m.current_hp = 0
    e.state.monster_reinforcements = [{"name": "增援怪", "arrive_round": 99}]  # 增援未到：战斗未结束
    r = begin_round(e, relic_choices={})
    assert r["success"], r.get("error")


# ---------- 守夜灯 ----------

def test_lamp_grants_at_round_start_and_keeps_it(tmp_path):
    """正常路径（2026-09-13 新口径）：[回始]授予 ceil(法限*10%)，怪物阶段不再授予也不清空。"""
    e = _engine(tmp_path, "lamp_ok")
    e.state.relics.append(Relic("守夜灯", ""))
    begin_battle(e)
    p = e.state.player
    # 满池时授予会被上限全额吃掉，先花干净再看[回始]授予。
    p.current_mana = 0
    p._shouyedeng_granted = 0
    tenth = math.ceil(p.mana_limit * 0.1)
    begin_round(e)
    assert p.current_mana == tenth, f"[回始]应授予{tenth}，实{p.current_mana}"
    before = p.current_mana
    phase = _monster_phase_no_attack(e)
    assert phase["success"], phase
    # 怪物阶段既不授予也不清空。本局可能随机持有【承露盏】(每失去10生命得1法力)，
    # 故按净变化断言：只允许承露盏那份回充。
    toll = p.hp_lost_this_battle // 10 if e.state.side_has(p, "承露盏") else 0
    assert p.current_mana == min(p.mana_limit, before + toll), \
        f"守夜灯法力应留存（承露盏回充{toll}）"
    assert not [d for d in phase["result"]["details"] if d.get("type") == "shouyedeng_grant"]
    assert not [d for d in phase["result"]["details"] if d.get("type") == "shouyedeng_clear"]


def test_lamp_grant_is_capped_by_mana_limit(tmp_path):
    """边界：授予量向上取整，且落地后不得超过[法限]。"""
    e = _engine(tmp_path, "lamp_bound")
    e.state.relics.append(Relic("守夜灯", ""))
    p = e.state.player
    p.mana_limit = 21
    p.current_mana = 14
    e.combat._grant_shouyedeng(p)
    assert p.current_mana == 14 + math.ceil(21 * 0.1)  # ceil(2.1)=3 → 17
    # 贴着上限时溢出丢弃
    p.current_mana = 20
    p._shouyedeng_granted = 0
    e.combat._grant_shouyedeng(p)
    assert p.current_mana == 21


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
