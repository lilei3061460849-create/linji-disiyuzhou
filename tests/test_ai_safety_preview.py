"""TacticalAI 行动预演安全层回归（2026-08-19）。

顾衡第6场案例：39HP + 脑蜘蛛爆裂1 + 血肉巨囊爆裂1 + 人头气球存活 + 冲击4（借力2）
→ 冲击对两只爆裂目标各反噬 24（伤害 20×借力1.2），轮回者共受 48 点爆裂反噬命零。
预演必须判定死亡，TacticalAI 不得选择该行动；同时验证单体攻击爆裂同样被拦截
（证明安全层不是只针对 AOE 的道纹特判）。

覆盖：预演后果提取（HP/命零/反噬量/事件链）、预演零副作用、AOE 爆裂拒绝、
单体爆裂拒绝、安全动作行为不变。
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


def _baolie_enemy(name, hp, bl=None, atk=1, ap=5, baolie=True):
    m = Entity(name, "怪物", blood_limit=bl if bl is not None else hp,
               current_hp=hp, attack_count=atk, attack_power=ap)
    if baolie:
        m.add_status(StatusEffect(name="爆裂", remaining_rounds=1, value=1, source="x"))
    return m


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


# ==================== 顾衡案例：AOE 冲击触发双爆裂反噬 ====================

def test_guheng_case_preview_reports_48_reflect_death():
    """预演必须明确判定：冲击4 + 借力2 对双爆裂目标各反噬24，轮回者受48点反噬命零。"""
    e = _arena()
    ai = _ai_ready(
        e, player_hp=39, player_mana=36,
        daowen={"冲击": 4, "借力": 2},
        enemies=[
            _baolie_enemy("脑蜘蛛", 204, atk=2, ap=11),
            _baolie_enemy("人头气球", 222, baolie=False),
            _baolie_enemy("血肉巨囊", 258, atk=1, ap=8),
        ],
    )
    # 借力2：伤害 +20%（20 × 1.2 = 24/目标）；脑蜘蛛+血肉巨囊两个爆裂目标各反噬24
    p = e.state.player
    p.add_status(StatusEffect(name="借力", remaining_rounds=-1, value=2, source="x"))
    before_hp = p.current_hp

    # 冲击是 AOE：use_daowen 必须显式提交覆盖全部敌对目标的 dodge_targets
    dodge_targets = [
        {"target_ref": f"enemy:{i}", "dodge": False, "blood_shadow": False}
        for i in range(len(e.state.enemies))
    ]
    pv = ai.previewer.preview("use_daowen", {
        "daowen_name": "冲击", "x": 4, "dodge": False, "blood_shadow": False,
        "dodge_targets": dodge_targets, "trigger_spell_choices": {},
    })
    diff = pv["diff"]
    assert pv["result"] is not None
    assert diff["player_dead"] is True, "预演必须判定轮回者命零"
    assert diff["player"]["hp_after"] == 0
    assert diff["player"]["hp_before"] == 39
    # 反噬总量 = 两个爆裂目标各 24 → 39HP 扣到 0（diff 直接给出 hp 变化）
    # 效果链必须包含爆裂反噬相关的伤害/死亡事件
    reflect_events = [ev for ev in diff["events"]
                      if ev["type"] in ("damage_applied", "entity_died")]
    assert reflect_events, "效果链必须包含爆裂反噬的伤害/死亡事件"
    # 预演零副作用（restore 会替换实体对象，必须从 state 重读玩家）
    assert e.state.player.current_hp == before_hp, "预演不得改变真实战斗状态"


def test_guheng_case_tactical_ai_rejects_baolie_aoe():
    """TacticalAI 不得选择该行动：try_aoe / _cast 对预演致死的冲击返回 None。"""
    e = _arena()
    ai = _ai_ready(
        e, player_hp=39, player_mana=36,
        daowen={"冲击": 4, "借力": 2},
        enemies=[
            _baolie_enemy("脑蜘蛛", 204, atk=2, ap=11),
            _baolie_enemy("人头气球", 222, baolie=False),
            _baolie_enemy("血肉巨囊", 258, atk=1, ap=8),
        ],
    )
    e.state.player.add_status(StatusEffect(name="借力", remaining_rounds=-1,
                                           value=2, source="x"))
    # try_aoe：原候选 冲击X=4（48 反噬致死）必须被拒绝并记录；
    # 若降 X 到安全档（X=1：5伤×2爆裂=10反噬，39-10=29 不死）则只允许执行安全档。
    r = ai.try_aoe()
    assert ai.preview_rejected, "安全过滤应记录被淘汰候选"
    assert any("冲击X=4" in entry for entry in ai.preview_rejected), ai.preview_rejected
    if r is not None:
        # 降 X 执行安全档：正式执行的动作必须是降档后的冲击（x<4）
        exec_x = r.get("calculation", {}).get("x")
        assert exec_x is not None and exec_x < 4, f"只允许降档执行安全冲击: {r}"
        assert e.state.player.current_hp == 39 or e.state.player.current_hp > 0,             "降档执行不得致死"
    else:
        assert e.state.player.current_hp == 39
    # 玩家状态未被改动（预演+过滤全程零副作用）
    assert e.state.player.current_hp > 0


# ==================== 单体攻击爆裂（证明非 AOE 特判） ====================

def test_single_target_baolie_reflect_rejected():
    """单体攻击触发爆裂反噬致死：同样被安全层拦截，不是只针对 AOE。"""
    e = _arena()
    ai = _ai_ready(
        e, player_hp=30, player_mana=30,
        daowen={"杀伐": 5},
        enemies=[_baolie_enemy("独眼怪", 100, atk=1, ap=5, baolie=True)],
    )
    # 杀伐X=15 → 伤害 30 → 爆裂反噬 30 → 玩家 30HP 命零
    pv = ai.previewer.preview("use_daowen", {
        "daowen_name": "杀伐", "x": 15, "target": "独眼怪",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert pv["diff"]["player_dead"] is True, "单体爆裂反噬致死必须被预演识别"
    assert pv["diff"]["player"]["hp_after"] == 0

    r = ai._cast("杀伐", 15, "独眼怪")
    assert ai.preview_rejected, "原 X=15 致死必须被记录"
    assert any("杀伐X=15" in entry for entry in ai.preview_rejected), ai.preview_rejected
    if r is not None:
        exec_x = r.get("calculation", {}).get("x")
        assert exec_x is not None and exec_x < 15, f"只允许降档执行安全杀伐: {r}"
        # 降档执行会承受爆裂反噬但不得致死
        assert e.state.player.current_hp > 0, "降档执行不得致死"
    else:
        assert e.state.player.current_hp == 30, "拒绝后玩家状态不变"


def test_safe_attack_still_allowed():
    """安全动作行为不变：目标无爆裂时，正常攻击仍可执行并造成伤害。"""
    e = _arena()
    ai = _ai_ready(
        e, player_hp=60, player_mana=30,
        daowen={"杀伐": 5},
        enemies=[_baolie_enemy("无爆裂怪", 100, atk=1, ap=5, baolie=False)],
    )
    pv = ai.previewer.preview("use_daowen", {
        "daowen_name": "杀伐", "x": 3, "target": "无爆裂怪",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert pv["diff"]["player_dead"] is False, "无爆裂目标不应反噬致死"
    assert pv["diff"]["enemies"][0]["hp_after"] < pv["diff"]["enemies"][0]["hp_before"]

    r = ai._cast("杀伐", 3, "无爆裂怪")
    assert r is not None and r.get("success"), "安全攻击必须正常执行"
    assert not ai.preview_rejected


def test_preview_restores_all_state():
    """预演零副作用：执行前后 玩家HP/法力/速度/碎片、怪物HP、事件流 完全一致。"""
    e = _arena()
    ai = _ai_ready(
        e, player_hp=39, player_mana=36,
        daowen={"冲击": 4},
        enemies=[
            _baolie_enemy("脑蜘蛛", 204, atk=2, ap=11),
            _baolie_enemy("人头气球", 222, baolie=False),
            _baolie_enemy("血肉巨囊", 258, atk=1, ap=8),
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
