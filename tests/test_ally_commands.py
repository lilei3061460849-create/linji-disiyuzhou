"""
pytest 风格测试 - 里程碑3：朋友/员工听从轮回者指令，对非自身目标发动攻击/道纹

设计口径(用户已确认)："朋友/员工AI听从轮回者的指令，对其他非自身目标发动道纹/发动攻击"——
即不做自主决策式AI(不猜"该攻击谁"、不接入独立LLM决策)，而是复用现有的按名指定行动者/目标接口，
由发出指令的一方(玩家/主控AI)显式指定"谁行动、对谁行动"，引擎只负责校验与结算。

覆盖范围：
1. 攻击：验证[朋友]/[员工]通过现有 attack 动作的 attacker 参数发动攻击时，
   目标自动限定为对方阵营(既有实现，本文件补齐回归测试证明其确实可用于非玩家角色)。
2. 道纹：generalize 后的 use_daowen 新增 actor 参数——
   a) [朋友]/[员工]与怪物/微光者同属不持有法力的一方，发动道纹不支付法力，只消耗出手
   b) 必须显式指定一个非自身的目标，否则拒绝
   c) 玩家自身发动道纹的原有行为(法力制、默认自身为目标)保持不变，向后兼容

不在本文件覆盖范围内：朋友/员工每回合"自动"（无需玩家逐次下令）触发攻击/道纹的自动化调度，
该调度属于战斗流程驱动层，超出本次"听从指令"范围。

运行方式：
    python -m pytest tests/test_ally_commands.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tests.attack_support import resolve_attack as resolve_player_attack
from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance


def _new_engine_with_enemy(db_suffix: str, region: str = "龙心谷") -> GameEngine:
    engine = GameEngine(db_path=f"data/test_ally_{db_suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 10, "speed_points": 8, "mana_points": 7})
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": region})
    engine.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    engine.state.energy = 0
    choices = {}
    relic = engine.state.relics[0].name
    if relic in ("折速法印", "三相残韵盘"):
        choices[relic] = {"use": False}
    engine.execute_action("battle_start", {"relic_choices": choices})
    # battle_start会自动出怪(见engine/monsters.py)，这里替换为受控的单一测试怪物，保证断言确定性
    engine.state.enemies.clear()
    engine.state.enemies.append(Entity(name="测试怪", entity_type="怪物", blood_limit=100, current_hp=100,
                                        attack_count=1, attack_power=1))
    engine.execute_action("round_start", {})
    return engine


def _give_daowen(entity: Entity, name: str):
    entity.dao_wen[name] = DaoWenInstance(DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


# ========================================================================
# 正常路径
# ========================================================================

def test_friend_attack_commanded_by_player_hits_enemy_side():
    """正常路径：指令[朋友]攻击，目标自动限定为敌方阵营，伤害真实结算"""
    engine = _new_engine_with_enemy("friend_attack")
    engine.state.friends.append(Entity(name="岩行者", entity_type="朋友", blood_limit=54, current_hp=54,
                                        attack_count=2, attack_power=4))
    enemy = engine.state.enemies[0]
    r = resolve_player_attack(engine, "岩行者", [0, 0])
    assert r["success"] is True
    assert r["result"]["attacker"] == "岩行者"
    assert len(r["result"]["hits"]) == 2


def test_deployed_employee_can_be_commanded_to_use_daowen_on_enemy():
    """正常路径：已部署[员工]听从指令对敌方发动道纹，无需法力(与怪物同规则)，只消耗出手"""
    engine = _new_engine_with_enemy("emp_daowen", region="罪孽都市")
    emp = Entity(name="工头", entity_type="员工", blood_limit=96, current_hp=96,
                 attack_count=4, attack_power=8, is_deployed=False)
    engine.state.employees.append(emp)
    _give_daowen(emp, "杀伐")
    emp.current_mana = 0
    emp.mana_limit = 0
    engine.execute_action("deploy_employee", {"name": "工头"})

    enemy = engine.state.enemies[0]
    hp_before = enemy.current_hp
    r = engine.execute_action("use_daowen", {"actor": "工头", "daowen_name": "杀伐", "x": 5, "target": "测试怪"})
    assert r["success"] is True, r
    assert enemy.current_hp == hp_before - 10, "杀伐5应造成2*5=10点伤害"
    assert emp.current_mana == 0, "员工不应被扣减法力(本就没有法力)"


def test_player_self_cast_unaffected_backward_compatible():
    """正常路径：不传actor时玩家自身发动仍走法力制；带[目标]效果须按R03显式指定自身。"""
    engine = _new_engine_with_enemy("player_selfcast")
    player = engine.state.player
    _give_daowen(player, "庇护")
    mana_before = player.current_mana
    r = engine.execute_action("use_daowen", {"daowen_name": "庇护", "x": 3, "target": player.name})
    assert r["success"] is True
    assert player.current_mana == mana_before - 3, "玩家自身发动仍应正常扣除法力"
    assert player.shield == 6, "庇护3=消耗3法力获得2*3=6点格挡"


# ========================================================================
# 边界条件
# ========================================================================

def test_zero_attack_count_ally_has_zero_action_budget_and_cannot_act():
    """边界：出手次数公式=攻击次数/3(向上取整)，攻击次数为0的盟友出手预算=0，
    指令其攻击必须被拒绝(不是"能行动但0次命中"，而是压根没有出手可用)。
    R05已允许【雇佣】创建0攻击次数员工；本测试锁定其出手预算仍为0，防止此类角色
    盟友意外携带0攻击次数时，行为依然可预期而不是崩溃。"""
    engine = _new_engine_with_enemy("zero_atk")
    friend = Entity(name="纯辅助", entity_type="朋友", blood_limit=30, current_hp=30,
                     attack_count=0, attack_power=0)
    engine.state.friends.append(friend)
    assert friend.action_count == 0
    r = resolve_player_attack(engine, "纯辅助", [])
    assert r["success"] is False
    assert "出手已用完" in r["error"]


def test_undeployed_employee_cannot_be_commanded():
    """边界：未部署(待命中)的[员工]不应能被指令发动道纹——不在战场上，指令找不到它"""
    engine = _new_engine_with_enemy("undeployed_cmd", region="罪孽都市")
    emp = Entity(name="候补员工", entity_type="员工", blood_limit=204, current_hp=204,
                 attack_count=1, attack_power=2, is_deployed=False)
    engine.state.employees.append(emp)
    _give_daowen(emp, "杀伐")
    # 故意不调用 deploy_employee
    r = engine.execute_action("use_daowen", {"actor": "候补员工", "daowen_name": "杀伐", "x": 1, "target": "测试怪"})
    assert r["success"] is False, "待命中的员工不应能被指令行动"


# ========================================================================
# 错误输入 / 非法配置：校验器应当拒绝
# ========================================================================

def test_ally_command_without_target_is_rejected():
    """错误输入：听从指令发动道纹时不给目标，必须被拒绝，不能静默默认为自身"""
    engine = _new_engine_with_enemy("no_target")
    friend = Entity(name="哨兵", entity_type="朋友", blood_limit=40, current_hp=40)
    _give_daowen(friend, "庇护")
    engine.state.friends.append(friend)
    r = engine.execute_action("use_daowen", {"actor": "哨兵", "daowen_name": "庇护", "x": 1})
    assert r["success"] is False
    assert friend.shield == 0, "被拒绝的指令不能产生任何效果"


def test_ally_command_targeting_self_is_rejected():
    """错误输入：听从指令时把目标指定为自己，必须被拒绝(哪怕该道纹本可以合法targeting self)"""
    engine = _new_engine_with_enemy("self_target")
    friend = Entity(name="哨兵2", entity_type="朋友", blood_limit=40, current_hp=40)
    _give_daowen(friend, "庇护")
    engine.state.friends.append(friend)
    r = engine.execute_action("use_daowen", {"actor": "哨兵2", "daowen_name": "庇护", "x": 1, "target": "哨兵2"})
    assert r["success"] is False
    assert friend.shield == 0


def test_ally_command_for_nonexistent_actor_is_rejected():
    """错误输入：指令一个不存在/未参战的角色，必须报错"""
    engine = _new_engine_with_enemy("no_actor")
    r = engine.execute_action("use_daowen", {"actor": "不存在的人", "daowen_name": "杀伐", "x": 1, "target": "测试怪"})
    assert r["success"] is False


def test_use_daowen_invalid_target_name_no_longer_silently_defaults_to_self():
    """错误输入(既有回归加固)：给出一个不存在的target名字，必须报错，不能静默把目标改成施法者自己"""
    engine = _new_engine_with_enemy("bad_target")
    player = engine.state.player
    _give_daowen(player, "杀伐")
    r = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": "查无此怪"})
    assert r["success"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
