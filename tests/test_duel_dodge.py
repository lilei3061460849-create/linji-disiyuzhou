#!/usr/bin/env python3
"""死斗闪避中继（engine/ai_tactics.py::_attack_steps）测试。

规则依据（README:167）：被选定为非必中判定的目标后，可选择消耗 1 点当前速度完全
闪避本次判定；成功闪避后本局速度-1，[战终]复原。攻次=当前速度，闪避的隐性代价是
自己反打少一击——所以只有致命击或"伤害>自己攻力"才值得闪。
2026-09-10 修复：2026-08-26 的中继遗留在旧驱动分支，TacticalAI 路径写死 dodge=False，
PvP 双方从未行使闪避权（用户指出"后手方站着让人打"）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.ai_tactics import TacticalAI  # noqa: E402
from engine.enums import EntityType  # noqa: E402
from engine.models import Entity  # noqa: E402


def _duelist(name, hp, mana, speed):
    return Entity(name=name, entity_type=EntityType.REINCARNATOR.value,
                  blood_limit=36, current_hp=hp, mana_limit=mana,
                  current_mana=mana, speed_limit=speed, current_speed=speed)


def _refs(attacker, target):
    # 引擎约定：默认行动者=player:0（挑战席），目标在敌方槽位
    return {"player:0": attacker, "enemy:0": target}


def _flags(monkeypatch, attacker, target, hit_count, duel=True, in_final=True):
    """直跑 _resolve：把 prepare 快照与实体表喂进去，回收逐击 dodge 旗标。"""
    e = type("E", (), {"state": type("S", (), {"in_final_duel": in_final and duel})(),
                       "combat": type("C", (), {"_combat_entity_refs": staticmethod(
                           lambda: _refs(attacker, target))})()})()
    ai = TacticalAI(e)
    prev = {"result": {"token": "t", "hit_count": hit_count,
                       "target_options": [{"ref": "enemy:0", "name": target.name,
                                           "can_dodge": target.current_speed > 0}]}}
    out = ai._attack_steps(target.name)[1][1](prev)
    return [h["dodge"] for h in out["hits"]]


def test_value_and_lethal_dodges_respect_speed_budget(monkeypatch):
    """正常路径：伤害>目标攻力→逐击闪避；致命击必闪；预算=当前速度，逐击递减。"""
    monkeypatch.delenv("LJ_AI_DUEL_DODGE", raising=False)
    # 强攻手(攻力16×12击) vs 满血防守者(攻力12、速12)：16>12 值得闪，12击只够闪10次
    f = _flags(monkeypatch, _duelist("攻", 36, 16, 12), _duelist("守", 26, 12, 10), 12)
    assert sum(f) == 10 and f[:10] == [True] * 10 and f[10:] == [False] * 2
    # 4血防守者：每击都致命，预算内全闪
    f2 = _flags(monkeypatch, _duelist("攻", 36, 16, 12), _duelist("守", 4, 12, 10), 12)
    assert sum(f2) == 10 and all(f2[:10])


def test_low_value_hits_are_not_wasted(monkeypatch):
    """边界：伤害≤目标攻力且不致命→不闪（留着速度反打）；速度归零即无闪避。"""
    monkeypatch.delenv("LJ_AI_DUEL_DODGE", raising=False)
    # 弱攻手(攻力10) vs 满血防守者(攻力12)：10<12 且不致命 → 前3击硬吃；
    # 连吃3击后血剩6，第4击起每击致命 → 转入闪避（留速度反打但不送命）
    f = _flags(monkeypatch, _duelist("攻", 36, 10, 12), _duelist("守", 36, 12, 8), 6)
    assert f == [False] * 3 + [True] * 3
    # 速度0：can_dodge=False，连致命击也只能硬吃
    f2 = _flags(monkeypatch, _duelist("攻", 36, 20, 4), _duelist("守", 4, 12, 0), 4)
    assert f2 == [False] * 4


def test_bizhong_and_switch_off(monkeypatch):
    """非法/关闸：必中覆盖的击不得提交闪避（引擎会整包拒绝）；开关关闭=旧行为。"""
    monkeypatch.delenv("LJ_AI_DUEL_DODGE", raising=False)
    a = _duelist("攻", 36, 16, 12)
    a._bizhong_left = 2                      # 前两击必中，第三击起才可闪
    f = _flags(monkeypatch, a, _duelist("守", 4, 12, 10), 5)
    assert f[:2] == [False, False] and sum(f) == 3
    # 关闸（LJ_AI_DUEL_DODGE=0）：一切归为旧行为 dodge=False
    monkeypatch.setenv("LJ_AI_DUEL_DODGE", "0")
    f2 = _flags(monkeypatch, _duelist("攻", 36, 16, 12), _duelist("守", 4, 12, 10), 5)
    assert f2 == [False] * 5
