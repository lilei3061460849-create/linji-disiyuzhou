"""
pytest 风格测试 - 里程碑7：最终的冠冕 / 第8场最终死斗

原文：
"完成第7场后触发【最终的冠冕】：若无封存候选，本次轮回角色被完整封存，玩家以新的轮回者重新开始；
若已有封存候选，双方进入死斗。先手顺序：[速限]→[法限]→[血限]→当前生命。每轮交替消耗出手次数，
残韵可任意时刻插队，无法逃跑。死斗只允许一名轮回者离开。胜者进入下一阶副本，败者失去轮回者身份，
触发【死之传承】。"

设计要点(已与用户逐条确认)：
1. 持久化：JSON文件(sealed_candidate_path)，可在不同GameEngine实例间共享，模拟跨playthrough持久化。
2. 死斗范围：双方各自带队伍(朋友/员工)一起参战，不是严格1v1。
3. 交替出手：新增state.in_final_duel/duel_turn，attack/use_daowen强制按边交替，
   非当前出手方的行动一律拒绝。
4. 二阶及以上副本未实现：胜者同样被完整封存(而非接入不存在的下一阶内容)，
   成为下一位挑战者的候选人，形成擂台循环。

注：一旦第二位候选人到达触发死斗，引擎会立即消耗(删除)候选人文件——无论死斗最终结果如何，
该文件都不会在死斗流程中段残留，因此本文件内的清理代码统一用 _cleanup() 做存在性检查再删除。

运行方式：
    python -m pytest tests/test_final_duel.py -v
"""
import os
os.makedirs("/tmp/linji_tests", exist_ok=True)
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance


def _cleanup(path):
    if os.path.exists(path):
        os.remove(path)


def _new_candidate(db_suffix, sealed_path, speed_points=8, region="龙心谷", name=None):
    engine = GameEngine(db_path=f"data/test_duel_{db_suffix}.db", rng_seed=1, sealed_candidate_path=sealed_path)
    mana_points = 7
    blood_points = 25 - speed_points - mana_points
    params = {"blood_points": blood_points, "speed_points": speed_points, "mana_points": mana_points}
    if name:
        params["name"] = name
    engine.execute_action("setup_attributes", params)
    engine.execute_action("setup_choose_daowen", {"daowen": "杀伐"})
    engine.execute_action("setup_choose_region", {"region": region})
    engine.state.player.dao_wen["杀伐"] = DaoWenInstance(
        DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    return engine


def _finish_battle_7(engine):
    engine.state.current_battle = 7
    engine.state.enemies.clear()
    return engine.execute_action("battle_end", {})


# ========================================================================
# 正常路径
# ========================================================================

def test_first_candidate_gets_sealed_and_state_resets():
    """正常路径：无封存候选时，完成第7场自动封存当前角色(含队伍)，玩家状态重置为可开新角色"""
    path = "data/test_duel_seal1.json"
    _cleanup(path)
    engine = _new_candidate("seal1", path, name="老张")
    engine.state.friends.append(Entity(name="队友甲", entity_type="朋友", blood_limit=30, current_hp=30))
    r = _finish_battle_7(engine)
    crown = r["result"]["final_crown"]
    assert crown["outcome"] == "sealed"
    assert crown["sealed_name"] == "老张"
    assert engine.state.player is None, "封存后应重置为空白状态，等待新轮回者"
    assert os.path.exists(path)
    _cleanup(path)


def test_second_candidate_triggers_duel_with_correct_first_mover():
    """正常路径：已有候选时，第二位到达者立即进入死斗；先手按速限比较"""
    path = "data/test_duel_order.json"
    _cleanup(path)
    slow = _new_candidate("order_slow", path, speed_points=5, name="慢速者")  # 速限5
    _finish_battle_7(slow)

    fast = _new_candidate("order_fast", path, speed_points=13, name="快速者")  # 速限13
    r = _finish_battle_7(fast)
    crown = r["result"]["final_crown"]
    assert crown["outcome"] == "duel_start"
    assert fast.state.in_final_duel is True
    assert crown["first_mover"] == "player_side", "速限13>5，后到达的挑战者应先手"
    assert fast.state.duel_turn == "player_side"
    assert crown["opponent_name"] == "慢速者"
    _cleanup(path)


def test_duel_includes_friends_and_employees_on_both_sides():
    """正常路径：死斗带队伍，双方的朋友/员工都应出现在各自阵营里"""
    path = "data/test_duel_teams.json"
    _cleanup(path)
    sealed = _new_candidate("teams_sealed", path, name="被封存者")
    sealed.state.friends.append(Entity(name="旧队友", entity_type="朋友", blood_limit=20, current_hp=20))
    _finish_battle_7(sealed)

    challenger = _new_candidate("teams_challenger", path, name="挑战者")
    challenger.state.friends.append(Entity(name="新队友", entity_type="朋友", blood_limit=20, current_hp=20))
    r = _finish_battle_7(challenger)
    assert r["result"]["final_crown"]["outcome"] == "duel_start"
    enemy_names = [e.name for e in challenger.state.enemies]
    assert "被封存者" in enemy_names
    assert "旧队友" in enemy_names, "对手的朋友应一同出现在敌方阵营"
    assert any(f.name == "新队友" for f in challenger.state.friends), "挑战者自己的朋友应保留在己方"
    _cleanup(path)


def test_strict_turn_alternation_enforced():
    """正常路径：交替出手——同一方连续行动第二次必须被拒绝，轮到对方后才能行动"""
    path = "data/test_duel_alt.json"
    _cleanup(path)
    sealed = _new_candidate("alt_sealed", path, speed_points=5, name="对手")
    _finish_battle_7(sealed)
    challenger = _new_candidate("alt_challenger", path, speed_points=13, name="挑战者")
    _finish_battle_7(challenger)
    opp = challenger.state.enemies[0]

    assert challenger.state.duel_turn == "player_side"
    r1 = challenger.execute_action("attack", {"attacker": "挑战者", "target_selections": [0]})
    assert r1["success"] is True
    assert challenger.state.duel_turn == "opponent_side"

    r2 = challenger.execute_action("attack", {"attacker": "挑战者", "target_selections": [0]})
    assert r2["success"] is False, "还没轮到自己这边，不能连续行动第二次"

    r3 = challenger.execute_action("attack", {"attacker": opp.name, "target_selections": [0]})
    assert r3["success"] is True
    assert challenger.state.duel_turn == "player_side", "对方行动后应轮回挑战者"
    _cleanup(path)


def test_escape_forbidden_during_duel():
    """正常路径：死斗中无法逃跑"""
    path = "data/test_duel_escape.json"
    _cleanup(path)
    sealed = _new_candidate("escape_sealed", path, name="对手")
    _finish_battle_7(sealed)
    challenger = _new_candidate("escape_challenger", path, name="挑战者")
    _finish_battle_7(challenger)

    r = challenger.execute_action("declare_escape", {})
    assert r["success"] is False
    assert "逃跑" in r["error"]
    _cleanup(path)


def test_victory_seals_winner_forming_arena_cycle():
    """正常路径：胜利后先领取终音法器，再连同队伍完整封存，成为下一位挑战者的候选人(二阶未实现，用封存代替进阶)"""
    path = "data/test_duel_victory.json"
    _cleanup(path)
    sealed = _new_candidate("victory_sealed", path, name="手下败将")
    _finish_battle_7(sealed)
    winner = _new_candidate("victory_winner", path, name="常胜者")
    _finish_battle_7(winner)

    r = winner.execute_action("resolve_final_duel", {"outcome": "victory"})
    assert r["success"] is True
    assert r["result"]["pending_terminal_choice"] == "龙心谷"
    assert winner.state.player is not None, "领取终音法器前不应重置状态"

    r2 = winner.execute_action("choose_terminal_artifact", {"choice": 2})  # 2号=负岳碑，不涉及初拥之夜
    assert r2["success"] is True
    assert r2["result"]["seal"]["sealed_name"] == "常胜者"
    assert winner.state.player is None, "领取终音法器后应重置状态，等待下一次以新角色开始"
    assert os.path.exists(path), "胜者应被封存，形成擂台循环供下一位挑战者对战"

    import json
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["player"]["name"] == "常胜者"
    assert data["artifacts_owned"] == ["负岳碑"]
    _cleanup(path)


def test_defeat_triggers_reset_without_resealing():
    """正常路径：战败方触发死之传承并重置，不会被封存(封存槽位保持空缺)"""
    path = "data/test_duel_defeat.json"
    _cleanup(path)
    sealed = _new_candidate("defeat_sealed", path, name="对手")
    _finish_battle_7(sealed)
    loser = _new_candidate("defeat_loser", path, name="失败者")
    _finish_battle_7(loser)

    r = loser.execute_action("resolve_final_duel", {"outcome": "defeat", "death_book_wisdom": "败了"})
    assert r["success"] is True
    assert r["result"]["death_book_wisdom"] == "败了"
    assert loser.state.player is None
    assert not os.path.exists(path), "败者不应被封存，槽位保持空缺"


# ========================================================================
# 边界条件
# ========================================================================

def test_priority_tiebreak_falls_through_to_current_hp():
    """边界：速限/法限/血限都相同时，按当前生命决出先手"""
    path = "data/test_duel_tiebreak.json"
    _cleanup(path)
    a = _new_candidate("tie_a", path, speed_points=8, name="甲")  # 血10 速8 法7 -> 相同分配
    _finish_battle_7(a)
    b = _new_candidate("tie_b", path, speed_points=8, name="乙")
    b.state.player.current_hp -= 1  # 乙当前生命比甲低
    r = _finish_battle_7(b)
    crown = r["result"]["final_crown"]
    assert crown["first_mover"] == "opponent_side", "速限/法限/血限全部相同时，当前生命更高的甲应先手"
    _cleanup(path)


def test_name_collision_between_challenger_and_opponent_is_resolved():
    """边界：双方轮回者同名(默认都叫"轮回者")时，引擎必须能正确区分，不能把伤害错发给自己"""
    path = "data/test_duel_collision.json"
    _cleanup(path)
    sealed = _new_candidate("collision_sealed", path)  # 不传name，走默认"轮回者"
    _finish_battle_7(sealed)
    challenger = _new_candidate("collision_challenger", path, speed_points=13)  # 同样默认"轮回者"
    _finish_battle_7(challenger)

    opp = challenger.state.enemies[0]
    assert opp.name != challenger.state.player.name, "同名对手必须被改名以保证可寻址"
    hp_before = opp.current_hp
    r = challenger.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 3, "target": opp.name})
    assert r["success"] is True
    assert opp.current_hp == hp_before - 6, "伤害必须真正命中改名后的对手，而不是误伤自己"
    assert challenger.state.player.current_hp == challenger.state.player.blood_limit, "挑战者自己不应被误伤"
    _cleanup(path)


# ========================================================================
# 错误输入 / 非法配置
# ========================================================================

def test_resolve_final_duel_rejected_without_active_duel():
    """错误输入：没有进行中的死斗时调用resolve_final_duel必须报错"""
    engine = GameEngine(db_path="/tmp/linji_tests/test_duel_noactive.db", rng_seed=1,
                         sealed_candidate_path="data/test_duel_noactive.json")
    engine.execute_action("setup_attributes", {"blood_points": 10, "speed_points": 8, "mana_points": 7})
    r = engine.execute_action("resolve_final_duel", {"outcome": "victory"})
    assert r["success"] is False


def test_resolve_final_duel_rejects_invalid_outcome():
    """错误输入：outcome必须是victory/defeat"""
    path = "data/test_duel_badoutcome.json"
    _cleanup(path)
    sealed = _new_candidate("bad_sealed", path, name="对手")
    _finish_battle_7(sealed)
    challenger = _new_candidate("bad_challenger", path, name="挑战者")
    _finish_battle_7(challenger)

    r = challenger.execute_action("resolve_final_duel", {"outcome": "tie"})
    assert r["success"] is False
    assert challenger.state.in_final_duel is True, "非法输入不应改变进行中状态"
    _cleanup(path)


def test_action_from_non_duel_side_entity_rejected():
    """错误输入：指令一个既不在挑战者阵营也不在对手阵营的实体行动，必须报错"""
    path = "data/test_duel_ghost.json"
    _cleanup(path)
    sealed = _new_candidate("ghost_sealed", path, name="对手")
    _finish_battle_7(sealed)
    challenger = _new_candidate("ghost_challenger", path, name="挑战者")
    _finish_battle_7(challenger)

    r = challenger.execute_action("attack", {"attacker": "路人甲", "target_selections": [0]})
    assert r["success"] is False
    _cleanup(path)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
