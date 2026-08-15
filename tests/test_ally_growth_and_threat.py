"""伙伴成长 + 怪物威胁目标选择 契约测试。

需求：
1. 每存活过一场战斗的[朋友]/[员工]，攻击次数+1（上限9）；达到9后改为攻击力+1。
2. 怪物攻击挑威胁最大的目标（攻击力×攻击次数 + 输出道纹加成；同威胁血低优先）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.ai_tactics import monster_threat, choose_attack_target
from engine.models import Entity


def _engine(suffix: str) -> GameEngine:
    e = GameEngine(db_path=f"/tmp/test_growth_{suffix}.db", rng_seed=1)
    e.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    e.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    return e


def _end_battle_with_ally_alive(e, ally):
    e.state.energy = 0
    e.state.enemies.append(Entity("怪", "怪物", blood_limit=30, current_hp=30,
                                  attack_count=1, attack_power=1))
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {"relic_choices": {}})
    for m in list(e.state.enemies):
        m.is_alive = False
    return e.execute_action("battle_end", {})


# ---------- 正常路径：伙伴成长 ----------

def test_normal_ally_grows_attack_count_each_survived_battle():
    """正常路径：朋友每存活一场攻击次数+1。"""
    e = _engine("g1")
    ally = Entity("岩行者", "朋友", blood_limit=54, current_hp=54,
                  attack_count=2, attack_power=4)
    e.state.friends.append(ally)
    be1 = _end_battle_with_ally_alive(e, ally)
    assert ally.attack_count == 3, f"第1场后应3，实{ally.attack_count}"
    assert "攻击次数3" in be1["result"]["ally_growth"][0]
    be2 = _end_battle_with_ally_alive(e, ally)
    assert ally.attack_count == 4, f"第2场后应4，实{ally.attack_count}"


def test_normal_growth_caps_at_nine_then_attack_power():
    """正常路径：攻击次数达到9后，改为攻击力+1。"""
    e = _engine("gcap")
    ally = Entity("岩行者", "朋友", blood_limit=54, current_hp=54,
                  attack_count=8, attack_power=4)
    e.state.friends.append(ally)
    be1 = _end_battle_with_ally_alive(e, ally)
    assert ally.attack_count == 9 and ally.attack_power == 4, \
        f"8→9应为攻击次数，实{ally.attack_count}x{ally.attack_power}"
    be2 = _end_battle_with_ally_alive(e, ally)
    assert ally.attack_count == 9 and ally.attack_power == 5, \
        f"9后再过场应为攻击力+1，实{ally.attack_count}x{ally.attack_power}"
    assert "攻击力5" in be2["result"]["ally_growth"][0]


def test_boundary_dead_ally_does_not_grow():
    """边界：已命零的朋友不成长。"""
    e = _engine("gdead")
    ally = Entity("岩行者", "朋友", blood_limit=54, current_hp=0, attack_count=2, attack_power=4)
    ally.is_alive = False
    e.state.energy = 0
    e.state.enemies.append(Entity("怪", "怪物", blood_limit=30, current_hp=30,
                                  attack_count=1, attack_power=1))
    e.state.friends.append(ally)
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {"relic_choices": {}})
    for m in list(e.state.enemies):
        m.is_alive = False
    e.execute_action("battle_end", {})
    assert ally.attack_count == 2, "死亡朋友不成长"


def test_boundary_undeployed_employee_does_not_grow():
    """边界：未部署（待命）员工不参战，不成长。"""
    e = _engine("gemp")
    emp = Entity("医生", "员工", blood_limit=50, current_hp=50,
                 attack_count=1, attack_power=1, is_deployed=False)
    e.state.energy = 0
    e.state.enemies.append(Entity("怪", "怪物", blood_limit=30, current_hp=30,
                                  attack_count=1, attack_power=1))
    e.state.employees.append(emp)
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {"relic_choices": {}})
    for m in list(e.state.enemies):
        m.is_alive = False
    e.execute_action("battle_end", {})
    assert emp.attack_count == 1, "未部署员工不成长"


# ---------- 正常路径：怪物威胁目标 ----------

def test_monster_threat_scores():
    """正常路径：威胁分 = 攻击力×攻击次数 + 输出道纹加成。"""
    player = Entity("P", "轮回者", blood_limit=60, current_hp=60,
                    attack_count=0, attack_power=0)
    player.dao_wen["杀伐"] = object()  # 输出道纹
    ally = Entity("岩行者", "朋友", blood_limit=54, current_hp=54,
                  attack_count=5, attack_power=6)
    ally.dao_wen["背负"] = object()  # 非输出道纹
    assert monster_threat(ally) == 30, f"5×6=30，实{monster_threat(ally)}"
    assert monster_threat(player) == 10, f"0×0+杀伐=10，实{monster_threat(player)}"


def test_monster_targets_highest_threat():
    """正常路径：怪物攻击目标挑威胁最大的（高攻朋友 > 玩家）。"""
    refs = {
        "player:0": Entity("P", "轮回者", blood_limit=60, current_hp=60,
                           attack_count=0, attack_power=0),
        "friend:0": Entity("岩行者", "朋友", blood_limit=54, current_hp=54,
                           attack_count=5, attack_power=6),
    }
    options = [{"ref": "player:0"}, {"ref": "friend:0"}]
    assert choose_attack_target(options, refs) == "friend:0", \
        f"应打威胁最大的朋友，实{choose_attack_target(options, refs)}"


def test_boundary_same_threat_prefers_lower_hp():
    """边界：威胁分相同时选当前生命最低者。"""
    refs = {
        "friend:0": Entity("A", "朋友", blood_limit=50, current_hp=50,
                           attack_count=3, attack_power=4),
        "friend:1": Entity("B", "朋友", blood_limit=50, current_hp=20,
                           attack_count=3, attack_power=4),
    }
    options = [{"ref": "friend:0"}, {"ref": "friend:1"}]
    assert choose_attack_target(options, refs) == "friend:1", "同威胁应打血低者"
