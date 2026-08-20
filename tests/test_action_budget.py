"""
pytest 风格测试 - 里程碑4b：出手预算校验

DM裁定记录：
- 出手次数公式按entity_type分流：轮回者=速限/3(向上取整)；[朋友]/[员工](微光者)=攻击次数/3(向上取整)；
  怪物走独立的run_monster_phase固定规则，不受此约束。
- 消耗1出手的动作：attack、use_daowen(含指挥朋友/员工)、deploy_employee(消耗玩家的出手)、
  declare_wish、declare_escape。
- 不消耗出手：consume_item(原文明确)、use_resonance(可任意时刻插队)。
- 预算按回合重置(round_start时归零本回合已用出手数)。

运行方式：
    python -m pytest tests/test_action_budget.py -v
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tests.attack_support import resolve_attack as resolve_player_attack
from tests.monster_phase_support import resolve_monster_phase
from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance, Consumable


def _give_daowen(entity: Entity, name: str):
    entity.dao_wen[name] = DaoWenInstance(DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


def _new_engine(db_suffix: str, speed_points: int = 8) -> GameEngine:
    engine = GameEngine(db_path=f"data/test_budget_{db_suffix}.db", rng_seed=1)
    mana_points = 7
    blood_points = 25 - speed_points - mana_points  # 属性点总和必须=25
    engine.execute_action("setup_attributes",
                           {"blood_points": blood_points, "speed_points": speed_points, "mana_points": mana_points})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})  # 雇佣为罪孽都市专属
    choices = setup["result"]["relic_choices"]
    engine.execute_action("choose_discovered_relic", {"relic_name": choices[0]})
    engine.state.energy = 0
    relic_choices = {}
    if engine.state.relics[0].name in ("折速法印", "三相残韵盘"):
        relic_choices[engine.state.relics[0].name] = {"use": False}
    engine.execute_action("battle_start", {"relic_choices": relic_choices})
    # battle_start会自动出怪，这里替换为受控的单一测试怪物，保证断言确定性
    engine.state.enemies.clear()
    engine.state.enemies.append(Entity(name="靶怪", entity_type="怪物", blood_limit=999, current_hp=999,
                                        attack_count=1, attack_power=1))
    engine.execute_action("round_start", {})
    engine.state.player.current_mana = 999
    return engine


# ========================================================================
# 正常路径
# ========================================================================

def test_player_action_count_formula_and_budget_enforced():
    """正常路径：速限8=出手预算3(ceil(8/3))，用满3次后第4次被拒绝"""
    engine = _new_engine("player_budget", speed_points=8)
    player = engine.state.player
    assert player.action_count == 3
    for i in range(3):
        r = resolve_player_attack(engine, player.name, [])
        assert r["success"] is True, f"第{i+1}次攻击应成功: {r}"
    r4 = resolve_player_attack(engine, player.name, [])
    assert r4["success"] is False
    assert "出手已用完" in r4["error"]


def test_budget_resets_at_round_start():
    """正常路径：用完预算后，下一回合round_start必须重置，可以继续行动"""
    engine = _new_engine("reset_budget", speed_points=3)  # action_count=1
    player = engine.state.player
    assert player.action_count == 1
    r1 = resolve_player_attack(engine, player.name, [])
    assert r1["success"] is True
    r2 = resolve_player_attack(engine, player.name, [])
    assert r2["success"] is False

    resolve_monster_phase(engine.combat, {"靶怪": None})
    engine.state.combat_subphase = "await_round_end"  # 单元测试直接调用CombatEngine，手动同步API子阶段
    engine.execute_action("round_end", {})
    engine.execute_action("round_start", {})
    r3 = resolve_player_attack(engine, player.name, [])
    assert r3["success"] is True, "新回合应重置出手预算"


def test_ally_action_count_uses_attack_count_not_speed():
    """正常路径：[朋友]/[员工]出手预算公式=攻击次数/3，与自身速限(通常为0)无关"""
    engine = _new_engine("ally_formula")
    friend = Entity(name="力士", entity_type="朋友", blood_limit=40, current_hp=40,
                     attack_count=7, attack_power=3, speed_limit=0)
    engine.state.friends.append(friend)
    assert friend.action_count == 3, "ceil(7/3)=3，与speed_limit=0无关"


def test_deploy_employee_consumes_player_budget_not_employee():
    """正常路径：deploy_employee扣的是玩家的出手，不是被部署员工的出手"""
    engine = _new_engine("deploy_budget", speed_points=3)  # 玩家action_count=1
    engine.state.employees.append(Entity(
        name="小李", entity_type="员工", blood_limit=96, current_hp=96,
        attack_count=4, attack_power=8, is_deployed=False,
    ))
    player = engine.state.player
    r = engine.execute_action("deploy_employee", {"name": "小李"})
    assert r["success"] is True
    assert player.actions_used_this_round == 1
    r2 = resolve_player_attack(engine, player.name, [])
    assert r2["success"] is False, "玩家出手(1次)已被deploy_employee用掉，不应再能攻击"


def test_consume_item_and_resonance_do_not_consume_budget():
    """正常路径：消耗品与残韵不占用出手预算，用完全部出手后仍可使用"""
    engine = _new_engine("no_budget_actions", speed_points=3)  # action_count=1
    player = engine.state.player
    player.dao_wen["杀伐"] = DaoWenInstance(DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    engine.state.consumables.append(Consumable(name="急救箱", effect="使自身获得[回复25]", current_uses=2, max_uses=2))
    resolve_player_attack(engine, player.name, [])  # 用光唯一1次出手
    r_item = engine.execute_action("consume_item", {"name": "急救箱"})
    assert r_item["success"] is True, "消耗品不应受出手预算限制"

    engine.state.resonance["反转"] = 1
    r_res = engine.execute_action("use_resonance", {"source_daowen": "杀伐", "resonance_type": "反转"})
    assert r_res["success"] is True, f"残韵可任意时刻插队使用，不应受出手预算限制: {r_res}"


# ========================================================================
# 边界条件
# ========================================================================

def test_ally_command_daowen_shares_same_budget_pool_as_attack():
    """边界：同一实体的攻击与道纹共用同一份出手预算，不是分别独立计数"""
    engine = _new_engine("shared_pool", speed_points=3)  # action_count=1
    player = engine.state.player
    player.dao_wen["杀伐"] = DaoWenInstance(DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    r1 = resolve_player_attack(engine, player.name, [])
    assert r1["success"] is True
    r2 = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": "靶怪"})
    assert r2["success"] is False, "预算已被攻击用完，发动道纹也应被拒绝"


def test_failed_target_lookup_does_not_consume_budget():
    """边界：因攻击者不存在而提前失败的行动，不应消耗任何人的出手（不能因为无效指令白白扣掉一次机会）"""
    engine = _new_engine("no_consume_on_fail", speed_points=3)
    player = engine.state.player
    r_fail = resolve_player_attack(engine, "不存在的攻击者", [])
    assert r_fail["success"] is False
    assert player.actions_used_this_round == 0, "查找攻击者失败不应消耗任何人的出手"
    # 验证出手预算确实完好：随后一次正常攻击应能成功
    r_ok = resolve_player_attack(engine, player.name, [])
    assert r_ok["success"] is True


# ========================================================================
# 错误输入 / 非法配置
# ========================================================================

def test_declare_wish_and_escape_consume_budget_and_reject_when_exhausted():
    """错误输入：出手耗尽后声明许愿/逃跑必须被拒绝"""
    engine = _new_engine("wit_escape_budget", speed_points=3)  # action_count=1
    player = engine.state.player
    resolve_player_attack(engine, player.name, [])  # 用掉唯一1次出手
    r_wit = engine.execute_action("declare_wish", {"wish_text": "愿望测试", "target_ref": "enemy:0"})
    assert r_wit["success"] is False
    assert "出手已用完" in r_wit["error"]

    r_escape = engine.execute_action("declare_escape", {})
    assert r_escape["success"] is False
    assert "出手已用完" in r_escape["error"]


def test_deploy_employee_rejected_when_player_budget_exhausted():
    """错误输入：玩家出手耗尽后不能再派遣员工，必须报错而不是免费执行"""
    engine = _new_engine("deploy_no_budget", speed_points=3)  # action_count=1
    engine.state.employees.append(Entity(
        name="小周", entity_type="员工", blood_limit=96, current_hp=96,
        attack_count=4, attack_power=8, is_deployed=False,
    ))
    player = engine.state.player
    resolve_player_attack(engine, player.name, [])  # 用掉唯一1次出手
    r = engine.execute_action("deploy_employee", {"name": "小周"})
    assert r["success"] is False
    emp = next(e for e in engine.state.employees if e.name == "小周")
    assert emp.is_deployed is False, "预算不足时不应部署成功"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
