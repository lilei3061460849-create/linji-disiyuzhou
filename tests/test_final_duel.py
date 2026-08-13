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
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance


def _cleanup(path):
    if os.path.exists(path):
        os.remove(path)


def _new_candidate(db_suffix, sealed_path, speed_points=8, region="龙心谷", name=None,
                   death_book_path=None):
    kwargs = dict(db_path=f"data/test_duel_{db_suffix}.db", rng_seed=1,
                  sealed_candidate_path=sealed_path)
    if death_book_path:
        kwargs["death_book_path"] = death_book_path
    engine = GameEngine(**kwargs)
    mana_points = 7
    blood_points = 25 - speed_points - mana_points
    params = {"blood_points": blood_points, "speed_points": speed_points, "mana_points": mana_points}
    if name:
        params["name"] = name
    engine.execute_action("setup_attributes", params)
    engine.execute_action("setup_choose_daowen", {"daowen": "杀伐"})
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": region})
    engine.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    engine.state.player.dao_wen["杀伐"] = DaoWenInstance(
        DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    return engine


def _finish_battle_7(engine):
    engine.state.current_battle = 7
    engine.state.enemies.clear()
    engine.state.phase = "in_combat"
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
    r1 = challenger.execute_action("attack", {"attacker": "挑战者", "target_selections": []})
    assert r1["success"] is True
    assert challenger.state.duel_turn == "opponent_side"

    r2 = challenger.execute_action("attack", {"attacker": "挑战者", "target_selections": []})
    assert r2["success"] is False, "还没轮到自己这边，不能连续行动第二次"

    r3 = challenger.execute_action("attack", {"attacker": opp.name, "target_selections": []})
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
    book_path = "data/test_duel_defeat_book.md"
    _cleanup(path)
    _cleanup(book_path)
    Path(book_path).write_text("# 死者之书\n\n## 遗言\n\n当前没有遗言。\n", encoding="utf-8")
    sealed = _new_candidate("defeat_sealed", path, name="对手", death_book_path=book_path)
    _finish_battle_7(sealed)
    loser = _new_candidate("defeat_loser", path, name="失败者", death_book_path=book_path)
    _finish_battle_7(loser)

    legacy = {
        "trigger_point": "最终死斗落败",
        "fork": "最后一次出手选择错误",
        "cost_budget": "愿以速度换取机会",
    }
    r = loser.execute_action("resolve_final_duel", {"outcome": "defeat", "death_book_entry": legacy})
    assert r["success"] is True
    interrupt = r.get("interrupt") or {}
    assert interrupt.get("interrupt_type") == "死之传承"
    ruling = loser.submit_ruling("死之传承", "通过", {"action": "approve", **legacy})
    assert ruling["success"] is True
    assert ruling["death_book"]["legacy"] == legacy
    assert loser.state.player is None
    assert not os.path.exists(path), "败者不应被封存，槽位保持空缺"
    assert "最终死斗落败" in Path(book_path).read_text(encoding="utf-8")
    _cleanup(book_path)


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
    rs = challenger.execute_action("round_start", {})
    assert rs["success"] is True, rs
    hp_before = opp.current_hp
    r = challenger.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 3, "target": opp.name})
    assert r["success"] is True, r
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

    r = challenger.execute_action("attack", {"attacker": "路人甲", "target_selections": []})
    assert r["success"] is False
    _cleanup(path)


def test_duel_opponent_reincarnator_can_cast_and_both_gain_mana():
    """正常路径：死斗对手是轮回者，回始双方获得法力，对手可发动杀伐打到挑战者"""
    path = "data/test_duel_oppcast.json"
    _cleanup(path)
    sealed = _new_candidate("oppcast_sealed", path, speed_points=5, name="封存贾凡")
    _finish_battle_7(sealed)
    challenger = _new_candidate("oppcast_challenger", path, speed_points=13, name="挑战贾凡")
    r = _finish_battle_7(challenger)
    assert r["result"]["final_crown"]["outcome"] == "duel_start"
    opp = next(e for e in challenger.state.enemies if e.entity_type == "轮回者")

    rs = challenger.execute_action("round_start", {})
    names = [e.get("entity") for e in rs["result"].get("effects", []) if e.get("type") == "mana_refill"]
    assert "挑战贾凡" in names
    assert opp.name in names
    assert challenger.state.player.current_mana == challenger.state.player.mana_limit
    assert opp.current_mana == opp.mana_limit

    # 挑战者速限更高，先手让出一手后再由对手杀伐
    skip = challenger.execute_action("use_daowen", {
        "daowen_name": "杀伐", "x": 1, "target": opp.name,
    })
    assert skip["success"] is True, skip
    hp_before = challenger.state.player.current_hp
    cast = challenger.execute_action("use_daowen", {
        "actor": opp.name, "daowen_name": "杀伐", "x": 3, "target": "挑战贾凡",
    })
    assert cast["success"] is True, cast
    assert challenger.state.player.current_hp == hp_before - 6
    assert opp.current_mana == opp.mana_limit - 3
    assert challenger.state.duel_turn == "player_side"
    _cleanup(path)


def test_duel_opponent_impact_hits_player_side_not_self():
    """边界：对手发动冲击必须打挑战者一侧，不能打到自己"""
    path = "data/test_duel_oppaoe.json"
    _cleanup(path)
    sealed = _new_candidate("oppaoe_sealed", path, speed_points=5, name="封存贾凡")
    sealed.state.player.dao_wen["冲击"] = DaoWenInstance(
        DaoWen(name="冲击", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    _finish_battle_7(sealed)
    challenger = _new_candidate("oppaoe_challenger", path, speed_points=13, name="挑战贾凡")
    _finish_battle_7(challenger)
    opp = next(e for e in challenger.state.enemies if e.entity_type == "轮回者")
    challenger.execute_action("round_start", {})
    skip = challenger.execute_action("use_daowen", {
        "daowen_name": "杀伐", "x": 1, "target": opp.name,
    })
    assert skip["success"] is True, skip
    hp_self = opp.current_hp
    hp_player = challenger.state.player.current_hp
    r = challenger.execute_action("use_daowen", {
        "actor": opp.name, "daowen_name": "冲击", "x": 4, "target": "挑战贾凡",
    })
    assert r["success"] is True, r
    assert opp.current_hp == hp_self
    assert challenger.state.player.current_hp == hp_player - 4
    _cleanup(path)


def test_duel_target_daowen_can_be_dodged_with_speed():
    """正常路径：杀伐带[目标]，对手有速度时可闪避，法力与出手仍扣除，生命不变"""
    path = "data/test_duel_dodge.json"
    _cleanup(path)
    sealed = _new_candidate("dodge_sealed", path, speed_points=8, name="封存贾凡")
    _finish_battle_7(sealed)
    challenger = _new_candidate("dodge_challenger", path, speed_points=13, name="挑战贾凡")
    _finish_battle_7(challenger)
    opp = next(e for e in challenger.state.enemies if e.entity_type == "轮回者")
    challenger.execute_action("round_start", {})
    hp = opp.current_hp
    spd = opp.current_speed
    mana = challenger.state.player.current_mana
    r = challenger.execute_action("use_daowen", {
        "daowen_name": "杀伐", "x": 3, "target": opp.name, "dodge": True,
    })
    assert r["success"] is True, r
    assert r["dodge"]["fully_dodged"] is True
    assert opp.current_hp == hp
    assert opp.current_speed == spd - 1
    assert challenger.state.player.current_mana == mana - 3
    _cleanup(path)


def test_duel_target_daowen_no_speed_cannot_dodge():
    """边界：速度为0时声明闪避失败，杀伐照常结算"""
    path = "data/test_duel_nododge.json"
    _cleanup(path)
    sealed = _new_candidate("nododge_sealed", path, speed_points=8, name="封存贾凡")
    _finish_battle_7(sealed)
    challenger = _new_candidate("nododge_challenger", path, speed_points=13, name="挑战贾凡")
    _finish_battle_7(challenger)
    opp = next(e for e in challenger.state.enemies if e.entity_type == "轮回者")
    challenger.execute_action("round_start", {})
    opp.current_speed = 0
    hp = opp.current_hp
    r = challenger.execute_action("use_daowen", {
        "daowen_name": "杀伐", "x": 3, "target": opp.name, "dodge": True,
    })
    assert r["success"] is True, r
    assert r["dodge"].get("fully_dodged") is False
    assert opp.current_hp == hp - 6
    _cleanup(path)


def test_duel_opponent_chooses_zhesu_relic():
    """正常路径：对手自己决定是否发动折速；发动则疲惫X换6X法力"""
    path = "data/test_duel_zhesu.json"
    _cleanup(path)
    sealed = _new_candidate("zhesu_sealed", path, speed_points=8, name="封存贾凡")
    from engine.models import Relic
    sealed.state.relics.append(Relic(name="折速法印", effect="[战始]可疲惫X获得6X法力"))
    _finish_battle_7(sealed)
    challenger = _new_candidate("zhesu_challenger", path, speed_points=13, name="挑战贾凡")
    r = _finish_battle_7(challenger)
    crown = r["result"]["final_crown"]
    assert any(o["name"] == "折速法印" and o["side"] == "opponent_side" for o in crown["optional_relics"])
    opp = next(e for e in challenger.state.enemies if e.entity_type == "轮回者")
    refuse = challenger.execute_action("activate_duel_relic", {
        "side": "opponent_side", "relic": "折速法印", "use": False,
    })
    assert refuse["success"] is True
    assert opp.current_speed == 8
    use = challenger.execute_action("activate_duel_relic", {
        "side": "opponent_side", "relic": "折速法印", "use": True, "x": 4,
    })
    assert use["success"] is True, use
    assert opp.current_speed == 4
    assert opp.current_mana == 24
    bad = challenger.execute_action("activate_duel_relic", {
        "side": "opponent_side", "relic": "折速法印", "use": True, "x": 9,
    })
    assert bad["success"] is False
    _cleanup(path)


def test_duel_activate_relic_rejected_without_duel():
    """错误输入：没有死斗时不能发动死斗遗物"""
    engine = GameEngine(db_path="/tmp/linji_tests/test_duel_nrelic.db", rng_seed=1,
                         sealed_candidate_path="data/test_duel_nrelic.json")
    engine.execute_action("setup_attributes", {"blood_points": 10, "speed_points": 8, "mana_points": 7})
    r = engine.execute_action("activate_duel_relic", {
        "side": "player_side", "relic": "折速法印", "use": True, "x": 1,
    })
    assert r["success"] is False


def test_duel_opponent_cast_rejected_on_wrong_turn_and_without_mana():
    """错误输入：没轮到对手时不能发动；法力不足必须失败"""
    path = "data/test_duel_opperr.json"
    _cleanup(path)
    sealed = _new_candidate("opperr_sealed", path, speed_points=5, name="封存贾凡")
    _finish_battle_7(sealed)
    challenger = _new_candidate("opperr_challenger", path, speed_points=13, name="挑战贾凡")
    _finish_battle_7(challenger)
    opp = next(e for e in challenger.state.enemies if e.entity_type == "轮回者")

    assert challenger.state.duel_turn == "player_side"
    r1 = challenger.execute_action("use_daowen", {
        "actor": opp.name, "daowen_name": "杀伐", "x": 1, "target": "挑战贾凡",
    })
    assert r1["success"] is False
    assert "交替出手" in r1["error"]

    challenger.execute_action("round_start", {})
    challenger.execute_action("use_daowen", {
        "daowen_name": "杀伐", "x": 1, "target": opp.name,
    })
    assert challenger.state.duel_turn == "opponent_side"
    opp.current_mana = 0
    r2 = challenger.execute_action("use_daowen", {
        "actor": opp.name, "daowen_name": "杀伐", "x": 3, "target": "挑战贾凡",
    })
    assert r2["success"] is False
    assert "法力不足" in r2["error"]
    _cleanup(path)


def test_duel_round_end_clears_both_reincarnator_mana():
    """正常路径：回终清空双方轮回者剩余法力，各记一条 mana_clear"""
    path = "data/test_duel_manaclear.json"
    _cleanup(path)
    sealed = _new_candidate("manaclear_sealed", path, speed_points=5, name="封存贾凡")
    _finish_battle_7(sealed)
    challenger = _new_candidate("manaclear_challenger", path, speed_points=13, name="挑战贾凡")
    _finish_battle_7(challenger)
    opp = next(e for e in challenger.state.enemies if e.entity_type == "轮回者")
    player = challenger.state.player

    rs = challenger.execute_action("round_start", {})
    assert rs["success"] is True
    assert player.current_mana == player.mana_limit
    assert opp.current_mana == opp.mana_limit
    player.current_mana = 9
    opp.current_mana = 11

    re = challenger.execute_action("round_end", {})
    assert re["success"] is True, re
    effects = re["result"].get("effects", [])
    clears = [e for e in effects if e.get("type") == "mana_clear"]
    names = {e["entity"] for e in clears}
    assert player.name in names
    assert opp.name in names
    assert player.current_mana == 0
    assert opp.current_mana == 0
    _cleanup(path)


def test_duel_leftover_actions_continue_when_other_exhausted():
    """正常路径：对手出手用尽后，挑战者余手连动，不换边、不作废"""
    path = "data/test_duel_leftover.json"
    _cleanup(path)
    sealed = _new_candidate("leftover_sealed", path, speed_points=5, name="对手")  # ceil(5/3)=2
    _finish_battle_7(sealed)
    challenger = _new_candidate("leftover_challenger", path, speed_points=13, name="挑战者")  # ceil(13/3)=5
    _finish_battle_7(challenger)
    opp = next(e for e in challenger.state.enemies if e.entity_type == "轮回者")
    player = challenger.state.player
    challenger.execute_action("round_start", {})

    assert player.action_count == 5
    assert opp.action_count == 2
    assert challenger.state.duel_turn == "player_side"

    for _ in range(2):
        r_p = challenger.execute_action("attack", {"attacker": "挑战者", "target_selections": []})
        assert r_p["success"] is True, r_p
        assert challenger.state.duel_turn == "opponent_side"
        r_o = challenger.execute_action("attack", {"attacker": opp.name, "target_selections": []})
        assert r_o["success"] is True, r_o
        assert challenger.state.duel_turn == "player_side"

    assert opp.actions_used_this_round == 2
    assert player.actions_used_this_round == 2
    third = challenger.execute_action("attack", {"attacker": "挑战者", "target_selections": []})
    assert third["success"] is True, third
    assert challenger.state.duel_turn == "player_side", "对方出手已尽，本侧应连动"
    assert player.actions_used_this_round == 3
    _cleanup(path)


def test_duel_round_end_zero_mana_no_crash():
    """边界：双方法力已是 0 时回终仍成功，不伪造清空"""
    path = "data/test_duel_zeromana.json"
    _cleanup(path)
    sealed = _new_candidate("zeromana_sealed", path, speed_points=5, name="封存贾凡")
    _finish_battle_7(sealed)
    challenger = _new_candidate("zeromana_challenger", path, speed_points=13, name="挑战贾凡")
    _finish_battle_7(challenger)
    opp = next(e for e in challenger.state.enemies if e.entity_type == "轮回者")
    player = challenger.state.player
    assert player.current_mana == 0
    assert opp.current_mana == 0

    re = challenger.execute_action("round_end", {})
    assert re["success"] is True, re
    effects = re["result"].get("effects", [])
    clears = [e for e in effects if e.get("type") == "mana_clear"]
    assert clears == []
    assert player.current_mana == 0
    assert opp.current_mana == 0
    _cleanup(path)


def test_duel_last_leftover_then_both_exhausted_rejects():
    """边界：余手打完后双方都没预算，任一侧再出手失败，不抛异常"""
    path = "data/test_duel_bothexhaust.json"
    _cleanup(path)
    sealed = _new_candidate("bothexhaust_sealed", path, speed_points=5, name="对手")
    _finish_battle_7(sealed)
    challenger = _new_candidate("bothexhaust_challenger", path, speed_points=13, name="挑战者")
    _finish_battle_7(challenger)
    opp = next(e for e in challenger.state.enemies if e.entity_type == "轮回者")
    player = challenger.state.player
    challenger.execute_action("round_start", {})

    for _ in range(2):
        assert challenger.execute_action("attack", {"attacker": "挑战者", "target_selections": []})["success"]
        assert challenger.execute_action("attack", {"attacker": opp.name, "target_selections": []})["success"]
    for _ in range(3):
        r = challenger.execute_action("attack", {"attacker": "挑战者", "target_selections": []})
        assert r["success"] is True, r

    assert player.actions_used_this_round == 5
    assert opp.actions_used_this_round == 2

    extra_p = challenger.execute_action("attack", {"attacker": "挑战者", "target_selections": []})
    assert extra_p["success"] is False
    assert "出手已用完" in extra_p["error"]

    extra_o = challenger.execute_action("attack", {"attacker": opp.name, "target_selections": []})
    assert extra_o["success"] is False
    assert "交替出手" in extra_o["error"] or "出手已用完" in extra_o["error"]
    _cleanup(path)


def test_duel_still_rejects_wrong_side_while_other_has_actions():
    """错误输入：双方都有余手时，非当前边连出仍拒绝"""
    path = "data/test_duel_no_skip.json"
    _cleanup(path)
    sealed = _new_candidate("noskip_sealed", path, speed_points=5, name="对手")
    _finish_battle_7(sealed)
    challenger = _new_candidate("noskip_challenger", path, speed_points=13, name="挑战者")
    _finish_battle_7(challenger)
    opp = next(e for e in challenger.state.enemies if e.entity_type == "轮回者")
    challenger.execute_action("round_start", {})

    assert challenger.state.duel_turn == "player_side"
    first = challenger.execute_action("attack", {"attacker": "挑战者", "target_selections": []})
    assert first["success"] is True
    assert challenger.state.duel_turn == "opponent_side"

    twice = challenger.execute_action("attack", {"attacker": "挑战者", "target_selections": []})
    assert twice["success"] is False
    assert "交替出手" in twice["error"]
    assert challenger.state.player.actions_used_this_round == 1

    opp_ok = challenger.execute_action("attack", {"attacker": opp.name, "target_selections": []})
    assert opp_ok["success"] is True
    _cleanup(path)


def test_duel_round_end_does_not_clear_non_reincarnator_mana():
    """错误输入/对照：朋友不是轮回者，回终不清他的法力，也不记 mana_clear"""
    path = "data/test_duel_friendmana.json"
    _cleanup(path)
    sealed = _new_candidate("friendmana_sealed", path, speed_points=5, name="封存贾凡")
    _finish_battle_7(sealed)
    challenger = _new_candidate("friendmana_challenger", path, speed_points=13, name="挑战贾凡")
    _finish_battle_7(challenger)
    friend = Entity(name="旁观朋友", entity_type="朋友", blood_limit=20, current_hp=20,
                    mana_limit=10, current_mana=10)
    challenger.state.friends.append(friend)
    opp = next(e for e in challenger.state.enemies if e.entity_type == "轮回者")
    challenger.state.player.current_mana = 6
    opp.current_mana = 8

    re = challenger.execute_action("round_end", {})
    assert re["success"] is True, re
    clears = [e for e in re["result"].get("effects", []) if e.get("type") == "mana_clear"]
    clear_names = {e["entity"] for e in clears}
    assert "旁观朋友" not in clear_names
    assert friend.current_mana == 10
    assert challenger.state.player.current_mana == 0
    assert opp.current_mana == 0
    _cleanup(path)


def test_duel_opponent_dragon_bloodline_doubles_vs_challenger():
    """正常：对手持龙族血脉，打挑战者（非怪物）伤害翻倍；挑战者自己没有则不翻。"""
    path = "data/test_duel_opp_bloodline.json"
    _cleanup(path)
    sealed = _new_candidate("oppbl_sealed", path, speed_points=5, name="封存者")
    sealed.state.grant_relic("龙族血脉", "对非怪物翻倍", tag="龙族")
    _finish_battle_7(sealed)
    challenger = _new_candidate("oppbl_challenger", path, speed_points=13, name="挑战者")
    _finish_battle_7(challenger)
    opp = next(e for e in challenger.state.enemies if e.entity_type == "轮回者")
    player = challenger.state.player
    assert challenger.state.side_has(opp, "龙族血脉")
    assert not challenger.state.side_has(player, "龙族血脉")
    opp.attack_power = 5
    dmg = challenger.combat.resolve_attack(opp, player, is_must_hit=True)
    assert dmg["damage_dealt"] == 10
    player.attack_power = 5
    dmg2 = challenger.combat.resolve_attack(player, opp, is_must_hit=True)
    assert dmg2["damage_dealt"] == 5
    _cleanup(path)


def test_duel_opponent_blood_lineage_heals_at_round_end():
    """正常：对手持血族血脉，回终按本回合伤害回血；没造成伤害则流血20。"""
    path = "data/test_duel_opp_lineage.json"
    _cleanup(path)
    sealed = _new_candidate("opplin_sealed", path, speed_points=5, name="封存者")
    sealed.state.grant_relic("血族血脉", "回终回血或流血20", tag="血族")
    _finish_battle_7(sealed)
    challenger = _new_candidate("opplin_challenger", path, speed_points=13, name="挑战者")
    _finish_battle_7(challenger)
    opp = next(e for e in challenger.state.enemies if e.entity_type == "轮回者")
    challenger.execute_action("round_start", {})
    opp.damage_dealt_this_round = 8
    opp.current_hp = max(1, opp.blood_limit - 20)
    hp = opp.current_hp
    re = challenger.execute_action("round_end", {})
    heals = [e for e in re["result"]["effects"] if e.get("type") == "blood_lineage_heal"]
    assert any(e["entity"] == opp.name for e in heals)
    assert opp.current_hp == hp + 8

    challenger.execute_action("round_start", {})
    hp2 = opp.current_hp
    re2 = challenger.execute_action("round_end", {})
    bleeds = [e for e in re2["result"]["effects"] if e.get("type") == "blood_lineage_bleed"]
    assert any(e["entity"] == opp.name for e in bleeds)
    assert opp.current_hp == hp2 - 20
    _cleanup(path)


def test_duel_opponent_longxi_hits_challenger_before_act():
    """正常：对手持龙息，挑战者行动前受 10×回合 必中伤害。"""
    path = "data/test_duel_opp_longxi.json"
    _cleanup(path)
    sealed = _new_candidate("opplx_sealed", path, speed_points=5, name="封存者")
    sealed.state.grant_relic("龙息", "敌方行动前受伤", tag="龙族")
    _finish_battle_7(sealed)
    challenger = _new_candidate("opplx_challenger", path, speed_points=13, name="挑战者")
    _finish_battle_7(challenger)
    opp = next(e for e in challenger.state.enemies if e.entity_type == "轮回者")
    player = challenger.state.player
    challenger.execute_action("round_start", {})
    hp = player.current_hp
    r = challenger.execute_action("use_daowen", {
        "daowen_name": "杀伐", "x": 1, "target": opp.name,
    })
    assert r["success"], r
    assert player.current_hp == hp - 10
    _cleanup(path)


def test_duel_opponent_heart_and_tears_at_start():
    """正常：对手体外心脏翻自己；羔羊之泪开场打一轮全场50%。"""
    path = "data/test_duel_opp_art.json"
    _cleanup(path)
    sealed = _new_candidate("oppart_sealed", path, speed_points=5, name="封存者")
    sealed.state.artifacts_owned.extend(["体外心脏", "羔羊之泪"])
    base_bl = sealed.state.player.blood_limit
    base_hp = sealed.state.player.current_hp
    _finish_battle_7(sealed)
    challenger = _new_candidate("oppart_challenger", path, speed_points=13, name="挑战者")
    player_hp = challenger.state.player.current_hp
    r = _finish_battle_7(challenger)
    crown = r["result"]["final_crown"]
    assert crown["outcome"] == "duel_start"
    opp = next(e for e in challenger.state.enemies if e.entity_type == "轮回者")
    assert opp.blood_limit == base_bl * 2
    assert opp.current_hp == base_hp  # 翻倍后再掉 50%
    assert challenger.state.player.current_hp == player_hp - (player_hp + 1) // 2
    _cleanup(path)


def test_duel_side_has_rejects_friend_and_missing_trait():
    """错误：朋友不继承轮回者袋子；没持有的名字 side_has 为假。"""
    path = "data/test_duel_sidehas_err.json"
    _cleanup(path)
    sealed = _new_candidate("sideerr_sealed", path, speed_points=5, name="封存者")
    sealed.state.grant_relic("龙族血脉", "", tag="龙族")
    _finish_battle_7(sealed)
    challenger = _new_candidate("sideerr_challenger", path, speed_points=13, name="挑战者")
    challenger.state.grant_relic("血影", "", tag="血族")
    challenger.state.friends.append(Entity(
        name="跟班", entity_type="朋友", blood_limit=20, current_hp=20))
    _finish_battle_7(challenger)
    friend = next(f for f in challenger.state.friends if f.name == "跟班")
    opp = next(e for e in challenger.state.enemies if e.entity_type == "轮回者")
    assert challenger.state.side_has(friend, "血影") is False
    assert challenger.state.side_has(challenger.state.player, "龙族血脉") is False
    assert challenger.state.side_has(opp, "血影") is False
    _cleanup(path)


def test_duel_deploy_employee_advances_turn():
    """正常：死斗派遣花 1 出手后换到对手。"""
    path = "data/test_duel_deploy_turn.json"
    _cleanup(path)
    sealed = _new_candidate("deploy_sealed", path, speed_points=5, name="对手")
    _finish_battle_7(sealed)
    challenger = _new_candidate("deploy_challenger", path, speed_points=13, name="挑战者")
    _finish_battle_7(challenger)
    challenger.state.employees.append(Entity(
        name="打手", entity_type="员工", blood_limit=24, current_hp=24,
        attack_count=3, attack_power=6, is_deployed=False))
    challenger.execute_action("round_start", {})
    assert challenger.state.duel_turn == "player_side"
    r = challenger.execute_action("deploy_employee", {"name": "打手"})
    assert r["success"], r
    assert challenger.state.duel_turn == "opponent_side"
    emp = next(e for e in challenger.state.employees if e.name == "打手")
    assert emp.is_deployed is True
    twice = challenger.execute_action("attack", {"attacker": "挑战者", "target_selections": []})
    assert twice["success"] is False
    assert "交替出手" in twice["error"]
    _cleanup(path)


def test_duel_deploy_stays_when_opponent_exhausted():
    """边界：对手出手用尽后派遣不换边，本侧余手连动。"""
    path = "data/test_duel_deploy_leftover.json"
    _cleanup(path)
    sealed = _new_candidate("deployleft_sealed", path, speed_points=5, name="对手")
    _finish_battle_7(sealed)
    challenger = _new_candidate("deployleft_challenger", path, speed_points=13, name="挑战者")
    _finish_battle_7(challenger)
    opp = next(e for e in challenger.state.enemies if e.entity_type == "轮回者")
    challenger.state.employees.append(Entity(
        name="后援", entity_type="员工", blood_limit=24, current_hp=24,
        attack_count=3, attack_power=6, is_deployed=False))
    challenger.execute_action("round_start", {})
    for _ in range(2):
        assert challenger.execute_action("attack", {"attacker": "挑战者", "target_selections": []})["success"]
        assert challenger.execute_action("attack", {"attacker": opp.name, "target_selections": []})["success"]
    assert opp.actions_used_this_round == 2
    assert challenger.state.duel_turn == "player_side"
    r = challenger.execute_action("deploy_employee", {"name": "后援"})
    assert r["success"], r
    assert challenger.state.duel_turn == "player_side"
    _cleanup(path)


def test_duel_deploy_rejected_on_wrong_turn():
    """错误：没轮到挑战者时派遣必须拒绝，员工仍待命。"""
    path = "data/test_duel_deploy_wrong.json"
    _cleanup(path)
    sealed = _new_candidate("deploywrong_sealed", path, speed_points=5, name="对手")
    _finish_battle_7(sealed)
    challenger = _new_candidate("deploywrong_challenger", path, speed_points=13, name="挑战者")
    _finish_battle_7(challenger)
    challenger.state.employees.append(Entity(
        name="待命", entity_type="员工", blood_limit=24, current_hp=24,
        attack_count=3, attack_power=6, is_deployed=False))
    challenger.execute_action("round_start", {})
    assert challenger.execute_action("attack", {"attacker": "挑战者", "target_selections": []})["success"]
    assert challenger.state.duel_turn == "opponent_side"
    r = challenger.execute_action("deploy_employee", {"name": "待命"})
    assert r["success"] is False
    assert "交替出手" in r["error"]
    emp = next(e for e in challenger.state.employees if e.name == "待命")
    assert emp.is_deployed is False
    assert challenger.state.player.actions_used_this_round == 1
    _cleanup(path)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
