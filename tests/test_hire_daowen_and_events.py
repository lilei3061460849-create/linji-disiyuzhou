"""
pytest 风格测试 - 里程碑2b：雇佣diy后置步骤(发现并选择转化道纹) + 龙心谷"追求者"事件补全

范围声明：
1. 雇佣(罪孽都市)后，引擎自动"发现"3个未持有的转化道纹候选(DiceEngine.auto_roll抽取)，
   玩家通过 choose_hired_daowen 从中选1个赋予该员工。
2. 龙心谷专属事件"追求者"：选项1(雇佣)真正创建员工实体+固定面板+固定道纹；
   选项2(拿走口粮)登记state.forced_monsters_next_battle，供未来"出怪"逻辑读取
   (出怪系统本身未实现，这里只保证"效果被引擎真实记录"而非静默丢弃)。

不在本文件覆盖范围内：朋友/员工听从指令发动攻击/道纹(下一个里程碑)。

运行方式：
    python -m pytest tests/test_hire_daowen_and_events.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.api import GameEngine
from engine.daowen import DaoWenEngine


def _new_engine(db_suffix: str, region: str = "罪孽都市") -> GameEngine:
    engine = GameEngine(db_path=f"data/test_hd_{db_suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 10, "speed_points": 8, "mana_points": 7})
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": region})
    engine.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    engine.state.energy = 3
    return engine


# ========================================================================
# 正常路径
# ========================================================================

def test_hire_discovers_three_distinct_transformed_daowen():
    """正常路径：雇佣后必须自动发现3个互不相同、且都属于19个转化道纹范畴的候选"""
    engine = _new_engine("discover_ok")
    r = engine.execute_action("pre_battle_action", {
        "sub_action": "雇佣", "name": "小赵", "blood_alloc": 17, "atk_bundles": 1,
    })
    choices = r["result"]["discovered_daowen_choices"]
    assert len(choices) == 3
    assert len(set(choices)) == 3, "3个候选必须互不相同"
    assert all(c in DaoWenEngine.TRANSFORMED_DAOWEN for c in choices)


def test_choose_hired_daowen_attaches_to_employee():
    """正常路径：从候选中选1个后，该道纹必须真正出现在员工的dao_wen里"""
    engine = _new_engine("choose_ok")
    r = engine.execute_action("pre_battle_action", {
        "sub_action": "雇佣", "name": "小钱", "blood_alloc": 17, "atk_bundles": 1,
    })
    choice = r["result"]["discovered_daowen_choices"][1]
    r2 = engine.execute_action("choose_hired_daowen", {"name": "小钱", "daowen": choice})
    assert r2["success"] is True
    emp = next(e for e in engine.state.employees if e.name == "小钱")
    assert choice in emp.dao_wen
    assert "小钱" not in engine.state.pending_daowen_choices, "选择完成后应清除待选记录"


def test_zhuiqiuzhe_event_option1_hires_real_employee_with_fixed_panel():
    """正常路径：龙心谷"追求者"选项1必须创建真实员工，面板与道纹数值完全按文档写死"""
    engine = _new_engine("zqz_hire", region="龙心谷")
    engine.state.shards = 50
    engine.event_pool.current = "追求者"
    r = engine.execute_action("resolve_event", {"event": "追求者", "option_id": 1})
    assert r["success"] is True
    assert engine.state.shards == 40, "应扣除10碎片"
    emp = next((e for e in engine.state.employees if e.name == "追求者"), None)
    assert emp is not None
    assert (emp.attack_count, emp.attack_power, emp.blood_limit) == (8, 2, 96)
    assert emp.is_deployed is False, "与DIY雇佣一致，默认待命"
    assert {k: v.x_value for k, v in emp.dao_wen.items()} == {"逆鳞": 2, "活血": 3, "固执": 3}


def test_zhuiqiuzhe_event_option2_queues_forced_monster_next_battle():
    """正常路径：选项2(拿走口粮)必须真实登记到forced_monsters_next_battle，而不是静默丢弃"""
    engine = _new_engine("zqz_food", region="龙心谷")
    engine.state.shards = 0
    engine.event_pool.current = "追求者"
    r = engine.execute_action("resolve_event", {"event": "追求者", "option_id": 2})
    assert r["success"] is True
    assert engine.state.shards == 50
    assert len(engine.state.forced_monsters_next_battle) == 1
    queued = engine.state.forced_monsters_next_battle[0]
    assert queued["name"] == "追求者"
    assert queued["dao_wen"] == {"逆鳞": 2, "活血": 3, "固执": 3}
    # 不应同时创建一个"员工"版本的追求者(选项2是怪物版，二者互斥)
    assert not any(e.name == "追求者" for e in engine.state.employees)


# ========================================================================
# 边界条件
# ========================================================================

def test_hire_multiple_employees_have_independent_discovery_pools():
    """边界：连续雇佣多名员工，各自的发现候选互不影响、互不复用同一批dice历史key"""
    engine = _new_engine("discover_multi")
    r1 = engine.execute_action("pre_battle_action", {
        "sub_action": "雇佣", "name": "甲员工", "blood_alloc": 17, "atk_bundles": 1,
    })
    r2 = engine.execute_action("pre_battle_action", {
        "sub_action": "雇佣", "name": "乙员工", "blood_alloc": 17, "atk_bundles": 1,
    })
    assert "甲员工" in engine.state.pending_daowen_choices
    assert "乙员工" in engine.state.pending_daowen_choices
    assert len(r1["result"]["discovered_daowen_choices"]) == 3
    assert len(r2["result"]["discovered_daowen_choices"]) == 3


# ========================================================================
# 错误输入 / 非法配置
# ========================================================================

def test_choose_hired_daowen_rejects_option_outside_discovered_choices():
    """错误输入：选择一个不在3个候选范围内的道纹，必须被拒绝"""
    engine = _new_engine("choose_bad")
    r = engine.execute_action("pre_battle_action", {
        "sub_action": "雇佣", "name": "小孙", "blood_alloc": 17, "atk_bundles": 1,
    })
    choices = set(r["result"]["discovered_daowen_choices"])
    illegal = next(d for d in DaoWenEngine.TRANSFORMED_DAOWEN if d not in choices)
    r2 = engine.execute_action("choose_hired_daowen", {"name": "小孙", "daowen": illegal})
    assert r2["success"] is False
    emp = next(e for e in engine.state.employees if e.name == "小孙")
    assert illegal not in emp.dao_wen


def test_choose_hired_daowen_rejects_unknown_employee():
    """错误输入：对未雇佣过/不存在的员工调用选择动作，必须报错而不是静默创建状态"""
    engine = _new_engine("choose_unknown")
    r = engine.execute_action("choose_hired_daowen", {"name": "根本不存在的人", "daowen": "弱化"})
    assert r["success"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
