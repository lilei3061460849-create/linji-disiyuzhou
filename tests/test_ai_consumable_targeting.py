"""AI 消耗品接口：对敌工具要能被 AI 真正用出来（2026-09-17 修复）。

修复前 `TacticalAI.try_consumable()` 提交 `consume_item` 时只带 `{"name": ...}`、
不带 `target_ref`：反怪物电击枪／强光探照灯一类**需敌方目标**的工具在预演阶段就失败
被跳过 —— AI 永远用不了它们（与怪物侧「可选目标道纹接口不可达」同族问题）。
同时默认血线门（生命>40% 就整轮不看消耗品）把控制/输出类道具一起挡在门外：
它们与自身血量无关，却因为 AI 满血而永远躺在包里。

修复后（本文件逐条钉住）：
- 是否需要目标由**预演返回的错误信息**判定（不写死物品名），需要则按「威胁最高」
  （攻次×攻力）补一个敌方 `target_ref` 重演，再走同一套风险分级；
- 「对敌」的判据是**预演后果**而不是"要不要目标"：全场型工具（高爆手雷）不需要
  target_ref，但它确实把敌人打到掉血，因此不受血线门限制；
- 血线门只管**自身受益类**：满血时不用备用血泵；残血时照旧可用；
- 自定义 `consumable_gate` 仍然一票否决；
- 无敌人时不炸、返回 None（需目标的与全场型的都一样）。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.ai_tactics import TacticalAI
from engine.api import TWISTED_TOOL_LIBRARY, GameEngine
from engine.models import Consumable, Entity
from tests.setup_support import finish_initial_daowen

GUN_TEXT = TWISTED_TOOL_LIBRARY["反怪物电击枪"][1]
NADE_TEXT = TWISTED_TOOL_LIBRARY["高爆手雷"][1]
PUMP_TEXT = "使自身获得20点［回复］"


def _gun() -> Consumable:
    """每例新建：Consumable 是可变对象，跨例共享会把耐久带脏。"""
    return Consumable(name="反怪物电击枪", effect=GUN_TEXT, current_uses=3, max_uses=3)


def _nade() -> Consumable:
    return Consumable(name="高爆手雷", effect=NADE_TEXT, current_uses=2, max_uses=2)


def _pump() -> Consumable:
    return Consumable(name="备用血泵", effect=PUMP_TEXT, current_uses=3, max_uses=3)


def _engine(suffix: str) -> GameEngine:
    e = GameEngine(db_path=os.path.join(tempfile.mkdtemp(prefix=f"ai_tool_{suffix}"), "g.db"),
                   rng_seed=1, save_dir=tempfile.mkdtemp(prefix=f"ai_tool2_{suffix}"))
    e.execute_action("setup_attributes", {
        "name": "沈昼", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.energy = 0
    return e


def _foe(name: str, speed: int, mana: int, hp: int = 200) -> Entity:
    """怪物：攻次＝当前速度、攻力＝当前法力（威胁＝两者相乘）。"""
    m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
               attack_count=speed, attack_power=mana, speed_limit=speed, mana_limit=mana)
    m.current_speed = speed
    m.current_mana = mana
    return m


def _in_combat(e: GameEngine, foes: list) -> None:
    e.state.enemies = foes
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    e.state.current_round = 2


# ---------------------------------------------------------------- 需目标的工具

def test_ai_shoots_gun_at_the_top_threat_even_at_full_hp():
    e = _engine("top_threat")
    p = e.state.player
    p.current_hp = p.blood_limit          # 满血：旧血线门会整轮不看消耗品
    weak, strong = _foe("甲怪", speed=1, mana=1), _foe("乙怪", speed=4, mana=9)
    _in_combat(e, [weak, strong])
    e.state.consumables = [_gun()]
    ai = TacticalAI(e)

    r = ai.try_consumable()

    assert r is not None and r.get("success"), "需目标的工具此前永远被跳过"
    assert strong.current_hp == 175, "应打在威胁最高（攻次×攻力）的那只身上"
    assert weak.current_hp == 200
    assert ai.used.get("消耗品·反怪物电击枪") == 1


def test_ai_uses_targeted_tool_below_the_hp_gate_too():
    """血线门放开后，残血时同样会用对敌工具（不是只在满血时才会）。"""
    e = _engine("hurt")
    p = e.state.player
    p.current_hp = max(1, int(p.blood_limit * 0.2))
    foe = _foe("甲怪", speed=2, mana=5)
    _in_combat(e, [foe])
    e.state.consumables = [_gun()]
    ai = TacticalAI(e)

    r = ai.try_consumable()

    assert r is not None and r.get("success")
    assert foe.current_hp == 175


# ---------------------------------------------------------------- 全场型工具

def test_ai_throws_aoe_grenade_at_full_hp_without_target():
    """全场型：不需要 target_ref，但预演里敌人确实掉血 → 不受血线门限制。"""
    e = _engine("aoe_full_hp")
    p = e.state.player
    p.current_hp = p.blood_limit
    weak, strong = _foe("甲怪", speed=1, mana=1), _foe("乙怪", speed=4, mana=9)
    _in_combat(e, [weak, strong])
    e.state.consumables = [_nade()]
    ai = TacticalAI(e)

    r = ai.try_consumable()

    assert r is not None and r.get("success"), "全场型工具不该被血线门挡住"
    assert (weak.current_hp, strong.current_hp) == (180, 180), "全场各 20 点"
    assert weak.get_status_value("无力") == 2 and strong.get_status_value("无力") == 2
    assert ai.used.get("消耗品·高爆手雷") == 1


def test_ai_grenade_still_works_when_hurt():
    e = _engine("aoe_hurt")
    p = e.state.player
    p.current_hp = max(1, int(p.blood_limit * 0.2))
    foe = _foe("甲怪", speed=2, mana=5)
    _in_combat(e, [foe])
    e.state.consumables = [_nade()]
    ai = TacticalAI(e)

    r = ai.try_consumable()

    assert r is not None and r.get("success")
    assert foe.current_hp == 180


# ---------------------------------------------------------------- 血线门仍在

def test_ai_still_skips_self_benefit_item_at_full_hp():
    e = _engine("gate_full")
    p = e.state.player
    p.current_hp = p.blood_limit
    _in_combat(e, [_foe("甲怪", speed=2, mana=5)])
    e.state.consumables = [_pump()]
    ai = TacticalAI(e)

    assert ai.try_consumable() is None, "自身受益类：满血不该浪费"
    assert e.state.consumables[0].current_uses == 3
    assert p.current_hp == p.blood_limit


def test_ai_uses_self_benefit_item_when_hurt():
    e = _engine("gate_hurt")
    p = e.state.player
    p.blood_limit = 60
    p.current_hp = 10                      # ≤40%
    _in_combat(e, [_foe("甲怪", speed=2, mana=5)])
    e.state.consumables = [_pump()]
    ai = TacticalAI(e)

    r = ai.try_consumable()

    assert r is not None and r.get("success")
    assert p.current_hp > 10


# ---------------------------------------------------------------- 边界与优先级

def test_ai_returns_none_when_there_is_no_enemy_to_target():
    e = _engine("no_foe")
    _in_combat(e, [])
    e.state.consumables = [_gun()]
    ai = TacticalAI(e)

    assert ai.try_consumable() is None
    assert e.state.consumables[0].current_uses == 3


def test_ai_returns_none_for_aoe_tool_without_enemies():
    e = _engine("no_foe_aoe")
    _in_combat(e, [])
    e.state.consumables = [_nade()]
    ai = TacticalAI(e)

    assert ai.try_consumable() is None, "场上没有敌方目标：拒绝使用"
    assert e.state.consumables[0].current_uses == 2


def test_custom_gate_still_vetoes_hostile_tools():
    e = _engine("custom_gate")
    foe = _foe("甲怪", speed=3, mana=7)
    _in_combat(e, [foe])
    e.state.consumables = [_gun(), _nade()]
    ai = TacticalAI(e)
    ai.consumable_gate = lambda _ai: False

    assert ai.try_consumable() is None, "自定义门一票否决，优先于目标补齐"
    assert foe.current_hp == 200 and not foe.has_status("无力")


def test_depleted_tool_is_not_considered():
    e = _engine("depleted")
    foe = _foe("甲怪", speed=3, mana=7)
    _in_combat(e, [foe])
    e.state.consumables = [Consumable(name="反怪物电击枪", effect=GUN_TEXT,
                                      current_uses=0, max_uses=3),
                           Consumable(name="高爆手雷", effect=NADE_TEXT,
                                      current_uses=0, max_uses=2)]
    ai = TacticalAI(e)

    assert ai.try_consumable() is None
    assert foe.current_hp == 200


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
