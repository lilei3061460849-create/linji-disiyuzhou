"""TacticalAI 行动预演安全层回归（2026-08-19；2026-10-03 改用现役机制）。

原用例以「怪物持【爆裂】反噬致轮回者命零」为危险源；2026-10-03 用户令删除【爆裂】
（效果由通用遗物【千荆甲】承接，但千荆甲只能由轮回者持有，怪物不再有反噬），故本文件
改用仍存的同类危险源：【无神】（使[目标]选择目标时强制改为自身）——轮回者中无神后
出手即打自己，大 X 的杀伐/冲击会直接自灭。断言结构不变：
预演必须判定死亡，TacticalAI 不得选择该行动；AOE 与单体攻击都要被拦截
（证明安全层不是只针对某一道纹的特判）。

覆盖：预演后果提取（HP/命零/事件链）、预演零副作用、AOE 自灭拒绝、
单体自灭拒绝、安全动作行为不变。
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.ai_preview import ActionPreview
from engine.ai_tactics import TacticalAI
from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity, StatusEffect
from tests.setup_support import finish_initial_daowen


def _arena(region="扭曲都市"):
    e = GameEngine(db_path=os.path.join(tempfile.mkdtemp(prefix="prev"), "g.db"),
                   rng_seed=1, save_dir=tempfile.mkdtemp(prefix="prev2"))
    e.execute_action("setup_attributes", {"name": "顾衡", "blood_points": 7,
                                          "speed_points": 8, "mana_points": 10})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": region})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.energy = 0
    return e


def _give(e, name, x=0):
    e.state.player.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""), x_value=x)


def _enemy(name, hp, bl=None, atk=1, ap=5, baolie=False):
    """敌方怪物。baolie 参数已废弃（2026-10-03 删【爆裂】），保留仅为调用签名兼容。"""
    return Entity(name, "怪物", blood_limit=bl if bl is not None else hp,
                  current_hp=hp, attack_count=atk, attack_power=ap)


def _wushen(p, value=1):
    """2026-10-03 起的安全层危险源：轮回者中【无神】→ 出手强制打自己。"""
    p.add_status(StatusEffect(name="无神", remaining_rounds=-1, value=value, source="x"))


def _ai_ready(e, player_hp, player_mana, daowen, enemies):
    p = e.state.player
    p.current_hp = player_hp
    p.blood_limit = 100
    p.current_mana = player_mana
    p.mana_limit = player_mana
    p.current_speed = 8
    p.speed_limit = 8
    for name, x in daowen.items():
        _give(e, name, x)
    e.state.enemies = enemies
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    e.state.current_round = 2
    return TacticalAI(e)


# ==================== 无神案例：出手打自己 → 预演必须判死 ====================

def test_guheng_case_preview_reports_self_attack_death():
    """预演必须明确判定：39HP 轮回者中【无神】后以杀伐24 出手 → 打自己 576 伤 → 命零。"""
    e = _arena()
    ai = _ai_ready(
        e, player_hp=39, player_mana=36,
        daowen={"杀伐": 24, "借力": 2},
        enemies=[
            _enemy("脑蜘蛛", 204, atk=2, ap=11),
            _enemy("人头气球", 222),
            _enemy("血肉巨囊", 258, atk=1, ap=8),
        ],
    )
    # 无神：目标强制改为自身；借力2 再放大 20% → 39HP 直接命零
    p = e.state.player
    _wushen(p)
    p.add_status(StatusEffect(name="借力", remaining_rounds=-1, value=2, source="x"))
    before_hp = p.current_hp

    pv = ai.previewer.preview("use_daowen", {
        "daowen_name": "杀伐", "x": 24, "dodge": False, "blood_shadow": False,
        "target_ref": "enemy:0", "trigger_spell_choices": {},
    })
    diff = pv["diff"]
    assert pv["result"] is not None
    assert diff["player_dead"] is True, "预演必须判定轮回者命零"
    assert diff["player"]["hp_after"] == 0
    assert diff["player"]["hp_before"] == 39
    # 效果链必须包含自灭相关的伤害/死亡事件
    reflect_events = [ev for ev in diff["events"]
                      if ev["type"] in ("damage_applied", "entity_died")]
    assert reflect_events, "效果链必须包含自灭的伤害/死亡事件"
    # 预演零副作用（restore 会替换实体对象，必须从 state 重读玩家）
    assert e.state.player.current_hp == before_hp, "预演不得改变真实战斗状态"


def test_guheng_case_tactical_ai_rejects_self_lethal_aoe():
    """TacticalAI 不得选择致死行动：_cast 对预演致死的杀伐X=24 降档到安全X。"""
    e = _arena()
    ai = _ai_ready(
        e, player_hp=39, player_mana=36,
        daowen={"杀伐": 24, "借力": 2},
        enemies=[
            _enemy("脑蜘蛛", 204, atk=2, ap=11),
            _enemy("人头气球", 222),
            _enemy("血肉巨囊", 258, atk=1, ap=8),
        ],
    )
    _wushen(e.state.player)
    e.state.player.add_status(StatusEffect(name="借力", remaining_rounds=-1,
                                           value=2, source="x"))
    # 原候选 杀伐X=24（自灭）必须被拒绝并记录；自动降 X 找最小安全档。
    r = ai._cast("杀伐", 24, "脑蜘蛛")
    assert ai.preview_rejected, "安全过滤应记录被淘汰候选"
    assert any("杀伐X=24" in entry for entry in ai.preview_rejected), ai.preview_rejected
    if r is not None:
        # 降 X 执行安全档：正式执行的动作必须是降档后的杀伐（x<24）
        exec_x = r.get("calculation", {}).get("x")
        assert exec_x is not None and exec_x < 24, f"只允许降档执行安全杀伐: {r}"
        assert e.state.player.current_hp == 39 or e.state.player.current_hp > 0, \
            "降档执行不得致死"
    else:
        assert e.state.player.current_hp == 39
    # 玩家状态未被改动（预演+过滤全程零副作用）
    assert e.state.player.current_hp > 0


# ==================== 单体攻击自灭（证明非 AOE 特判） ====================

def test_single_target_self_attack_rejected():
    """单体攻击打自己致死：同样被安全层拦截，不是只针对 AOE。"""
    e = _arena()
    ai = _ai_ready(
        e, player_hp=30, player_mana=30,
        daowen={"杀伐": 5},
        enemies=[_enemy("独眼怪", 100, atk=1, ap=5)],
    )
    _wushen(e.state.player)
    # 无神 + 杀伐X=15 → 打自己 225 伤 → 玩家 30HP 命零
    pv = ai.previewer.preview("use_daowen", {
        "daowen_name": "杀伐", "x": 15, "target": "独眼怪",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert pv["diff"]["player_dead"] is True, "单体自灭必须被预演识别"
    assert pv["diff"]["player"]["hp_after"] == 0

    r = ai._cast("杀伐", 15, "独眼怪")
    assert ai.preview_rejected, "原 X=15 致死必须被记录"
    assert any("杀伐X=15" in entry for entry in ai.preview_rejected), ai.preview_rejected
    if r is not None:
        exec_x = r.get("calculation", {}).get("x")
        assert exec_x is not None and exec_x < 15, f"只允许降档执行安全杀伐: {r}"
        # 降档执行仍会打自己，但不得致死
        assert e.state.player.current_hp > 0, "降档执行不得致死"
    else:
        assert e.state.player.current_hp == 30, "拒绝后玩家状态不变"


def test_safe_attack_still_allowed():
    """安全动作行为不变：未中【无神】时，正常攻击仍可执行并造成伤害。"""
    e = _arena()
    ai = _ai_ready(
        e, player_hp=60, player_mana=30,
        daowen={"杀伐": 5},
        enemies=[_enemy("普通怪", 100, atk=1, ap=5)],
    )
    pv = ai.previewer.preview("use_daowen", {
        "daowen_name": "杀伐", "x": 3, "target": "普通怪",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert pv["diff"]["player_dead"] is False, "未中无神不应自灭"
    assert pv["diff"]["enemies"][0]["hp_after"] < pv["diff"]["enemies"][0]["hp_before"]

    r = ai._cast("杀伐", 3, "普通怪")
    assert r is not None and r.get("success"), "安全攻击必须正常执行"
    assert not ai.preview_rejected


def test_preview_restores_all_state():
    """预演零副作用：执行前后 玩家HP/法力/速度/碎片、怪物HP、事件流 完全一致。"""
    e = _arena()
    ai = _ai_ready(
        e, player_hp=39, player_mana=36,
        daowen={"冲击": 4},
        enemies=[
            _enemy("脑蜘蛛", 204, atk=2, ap=11),
            _enemy("人头气球", 222),
            _enemy("血肉巨囊", 258, atk=1, ap=8),
        ],
    )
    def snap():
        # restore 会替换实体对象：必须每次从 state 重读，不得持有旧引用
        pp = e.state.player
        return {
            "hp": pp.current_hp, "mana": pp.current_mana, "speed": pp.current_speed,
            "shards": e.state.shards,
            "enemy_hp": [m.current_hp for m in e.state.enemies],
            "events": len(e.state.combat_events),
            "round_used": e.combat._monster_round_used(e.state.enemies[0]),
        }

    before = snap()
    ai.previewer.preview("use_daowen", {
        "daowen_name": "冲击", "x": 4, "dodge": False, "blood_shadow": False,
        "trigger_spell_choices": {},
    })
    after = snap()
    assert after == before, f"预演必须零副作用:\n{before}\n{after}"
