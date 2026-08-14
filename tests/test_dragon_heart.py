"""
pytest 风格测试 - 里程碑9a：龙心谷"炼心"/【××龙心】消耗品系统（终音法器"共心环"的前置依赖）

原文："龙心谷专属行动：【炼心】局外（消耗1精力）或战斗（下次行动精力-1）中均可发动：
直到你下一次支付数值为X的代价后，获得对应类型的【××龙心】（消耗品（耐久X））。
消耗【××龙心】Y点耐久，可抵消Y点同类型代价；未被抵消的剩余代价照常支付。"

此前"炼心"只返回一句提示文字，从未真正生成过龙心消耗品对象，是纯占位。

运行方式：
    python -m pytest tests/test_dragon_heart.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity


def _new_engine(db_suffix: str) -> GameEngine:
    engine = GameEngine(db_path=f"data/test_dragonheart_{db_suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 10, "speed_points": 8, "mana_points": 7})
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "龙心谷"})
    optional = {"折速法印", "三相残韵盘"}
    choice = next((n for n in setup["result"]["relic_choices"] if n not in optional),
                  setup["result"]["relic_choices"][0])
    engine.execute_action("choose_discovered_relic", {"relic_name": choice})
    engine.state.player.dao_wen["血债"] = DaoWenInstance(
        DaoWen(name="血债", formula="", cost_type="流血", cost_formula="X", effect_formula=""))
    engine.state.energy = 3
    return engine


def _start_with_enemy(engine):
    engine.state.energy = 0
    engine.execute_action("battle_start", {"relic_choices": {}})
    engine.state.enemies.clear()
    engine.state.enemies.append(Entity(name="靶怪", entity_type="怪物", blood_limit=999, current_hp=999))
    engine.execute_action("round_start", {})


# ========================================================================
# 正常路径
# ========================================================================

def test_lianxin_converts_next_cost_into_matching_dragon_heart():
    """正常路径：炼心后，下一次实际支付的流血代价转化为等值的[流血龙心]消耗品"""
    engine = _new_engine("convert_ok")
    r = engine.execute_action("pre_battle_action", {"sub_action": "炼心"})
    assert r["success"] is True
    assert engine.state.pending_lianxin is True

    _start_with_enemy(engine)
    engine.execute_action("use_daowen", {"daowen_name": "血债", "x": 8, "target": "靶怪"})
    hearts = [c for c in engine.state.consumables if c.kind == "dragon_heart"]
    assert len(hearts) == 1
    assert hearts[0].name == "流血龙心"
    assert hearts[0].current_uses == 8 and hearts[0].max_uses == 8
    assert engine.state.pending_lianxin is False, "转化一次后应清空待生效标记"


def test_dragon_heart_offsets_future_same_type_cost():
    """正常路径：消耗龙心耐久可抵消未来同类型代价，未抵消的剩余部分照常支付"""
    engine = _new_engine("offset_ok")
    engine.execute_action("pre_battle_action", {"sub_action": "炼心"})
    _start_with_enemy(engine)
    engine.execute_action("use_daowen", {"daowen_name": "血债", "x": 8, "target": "靶怪"})  # 获得流血龙心(8/8)

    hp_before = engine.state.player.current_hp
    r = engine.execute_action("use_daowen", {"daowen_name": "血债", "x": 5, "target": "靶怪", "dragon_heart_use": 3})
    assert r["success"] is True
    bleed_effect = next(e for e in r["execution"]["effects"] if e["type"] == "bleed_cost")
    assert bleed_effect["dragon_heart_offset"] == 3
    assert bleed_effect["actual_damage"] == 2, "5点代价被抵消3点，实付2点"
    assert engine.state.player.current_hp == hp_before - 2
    heart = next(c for c in engine.state.consumables if c.kind == "dragon_heart")
    assert heart.current_uses == 5, "8点耐久用掉3点，剩5点"


def test_dragon_heart_merges_with_existing_same_name_consumable():
    """正常路径：再次炼心生成同名龙心时，按既有消耗品合并规则叠加耐久与耐久上限"""
    engine = _new_engine("merge_ok")
    engine.execute_action("pre_battle_action", {"sub_action": "炼心"})
    _start_with_enemy(engine)
    engine.execute_action("use_daowen", {"daowen_name": "血债", "x": 4, "target": "靶怪"})  # 流血龙心(4/4)

    engine.state.pending_lianxin = True
    engine.execute_action("use_daowen", {"daowen_name": "血债", "x": 6, "target": "靶怪"})  # 再转化一次
    hearts = [c for c in engine.state.consumables if c.kind == "dragon_heart"]
    assert len(hearts) == 1, "同名龙心应合并为一个对象，不是并存两个"
    assert hearts[0].current_uses == 10 and hearts[0].max_uses == 10


def test_lianxin_in_battle_does_not_cost_action_but_defers_energy():
    """正常路径：战斗中发动炼心不消耗出手，改为下一次局外行动多消耗1点精力"""
    engine = _new_engine("in_battle_ok")
    _start_with_enemy(engine)
    actions_before = engine.state.player.actions_used_this_round
    r = engine.execute_action("lianxin_in_battle", {})
    assert r["success"] is True
    assert engine.state.player.actions_used_this_round == actions_before, "不应消耗出手"
    assert engine.state.pending_energy_penalty == 1

    engine.state.phase = "pre_battle"  # 模拟该场战斗已合法结束
    engine.state.energy = 3
    engine.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "曲解"})
    assert engine.state.energy == 3 - 1 - 1, "应额外多扣1点精力(基础1点+炼心追加1点)"
    assert engine.state.pending_energy_penalty == 0, "结算后应清零，不能重复扣"


def test_different_cost_types_produce_different_named_hearts():
    """正常路径：不同代价类型(流血/衰老/疲惫)炼心后应产生对应命名的独立龙心，互不混用"""
    engine = _new_engine("types_ok")
    engine.state.player.dao_wen["透支"] = DaoWenInstance(
        DaoWen(name="透支", formula="", cost_type="衰老", cost_formula="X", effect_formula=""))
    engine.execute_action("pre_battle_action", {"sub_action": "炼心"})
    _start_with_enemy(engine)
    engine.execute_action("use_daowen", {"daowen_name": "透支", "x": 3, "target": "轮回者"})
    names = {c.name for c in engine.state.consumables if c.kind == "dragon_heart"}
    assert names == {"衰老龙心"}


# ========================================================================
# 边界条件
# ========================================================================

def test_dragon_heart_use_capped_by_current_durability():
    """边界：请求抵消的点数超过龙心当前耐久时，最多只按当前耐久抵消，不会倒扣出负数耐久"""
    engine = _new_engine("cap_durability")
    engine.execute_action("pre_battle_action", {"sub_action": "炼心"})
    _start_with_enemy(engine)
    engine.execute_action("use_daowen", {"daowen_name": "血债", "x": 3, "target": "靶怪"})  # 流血龙心(3/3)

    r = engine.execute_action("use_daowen", {"daowen_name": "血债", "x": 10, "target": "靶怪", "dragon_heart_use": 999})
    bleed_effect = next(e for e in r["execution"]["effects"] if e["type"] == "bleed_cost")
    assert bleed_effect["dragon_heart_offset"] == 3, "最多抵消到当前耐久为止"
    assert bleed_effect["actual_damage"] == 7
    heart = next(c for c in engine.state.consumables if c.kind == "dragon_heart")
    assert heart.current_uses == 0


def test_dragon_heart_use_capped_by_raw_cost_not_overdrawn():
    """边界：请求抵消的点数超过本次原始代价时，最多只按原始代价抵消，不会倒欠"""
    engine = _new_engine("cap_rawcost")
    engine.execute_action("pre_battle_action", {"sub_action": "炼心"})
    _start_with_enemy(engine)
    engine.execute_action("use_daowen", {"daowen_name": "血债", "x": 20, "target": "靶怪"})  # 流血龙心(20/20)

    r = engine.execute_action("use_daowen", {"daowen_name": "血债", "x": 3, "target": "靶怪", "dragon_heart_use": 20})
    bleed_effect = next(e for e in r["execution"]["effects"] if e["type"] == "bleed_cost")
    assert bleed_effect["dragon_heart_offset"] == 3, "本次代价只有3，最多抵消3，不能抵消超过代价本身"
    assert bleed_effect["actual_damage"] == 0
    heart = next(c for c in engine.state.consumables if c.kind == "dragon_heart")
    assert heart.current_uses == 17


def test_fully_offset_cost_does_not_trigger_pending_lianxin_banking():
    """边界：本次代价被完全抵消为0时，即使炼心处于待生效状态，也不应银行一个耐久为0的龙心"""
    engine = _new_engine("zero_actual_no_bank")
    engine.execute_action("pre_battle_action", {"sub_action": "炼心"})
    _start_with_enemy(engine)
    engine.execute_action("use_daowen", {"daowen_name": "血债", "x": 10, "target": "靶怪"})  # 流血龙心(10/10)

    engine.state.pending_lianxin = True  # 再次准备炼心
    engine.execute_action("use_daowen", {"daowen_name": "血债", "x": 4, "target": "靶怪", "dragon_heart_use": 10})
    hearts = [c for c in engine.state.consumables if c.kind == "dragon_heart"]
    assert len(hearts) == 1 and hearts[0].current_uses == 6, "本次代价被完全抵消，不应银行新龙心"
    assert engine.state.pending_lianxin is True, "本次未实际支付，炼心待生效状态应保留，等待下一次真正支付"


# ========================================================================
# 错误输入 / 非法配置
# ========================================================================

def test_dragon_heart_use_wrong_type_does_nothing():
    """错误输入：请求用[流血龙心]抵消[衰老]代价这种类型不匹配的情况，应完全不生效"""
    engine = _new_engine("wrong_type")
    engine.state.player.dao_wen["透支"] = DaoWenInstance(
        DaoWen(name="透支", formula="", cost_type="衰老", cost_formula="X", effect_formula=""))
    engine.execute_action("pre_battle_action", {"sub_action": "炼心"})
    _start_with_enemy(engine)
    engine.execute_action("use_daowen", {"daowen_name": "血债", "x": 8, "target": "靶怪"})  # 流血龙心(8/8)

    blood_limit_before = engine.state.player.blood_limit
    r = engine.execute_action("use_daowen", {"daowen_name": "透支", "x": 3, "target": "轮回者", "dragon_heart_use": 5})
    aging_effect = next(e for e in r["execution"]["effects"] if e["type"] == "aging_cost")
    assert aging_effect["dragon_heart_offset"] == 0, "流血龙心不能抵消衰老代价"
    assert engine.state.player.blood_limit == blood_limit_before - 3
    heart = next(c for c in engine.state.consumables if c.kind == "dragon_heart")
    assert heart.current_uses == 8, "类型不匹配时不应扣减流血龙心耐久"


def test_no_pending_lianxin_does_not_create_heart():
    """错误输入/非预期状态：没有发动过炼心时，正常支付代价不应意外生成龙心"""
    engine = _new_engine("no_pending")
    _start_with_enemy(engine)
    engine.execute_action("use_daowen", {"daowen_name": "血债", "x": 5, "target": "靶怪"})
    hearts = [c for c in engine.state.consumables if c.kind == "dragon_heart"]
    assert hearts == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
