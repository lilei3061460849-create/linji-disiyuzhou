"""封存候选人进入乱葬岗（二阶副本）的兼容性测试。

验证：一阶通关封存的角色快照（player+friends+employees+遗物+道纹）能作为
起始状态进入乱葬岗对局，战斗全流程（回始/出手/怪物阶段/战终）不崩。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.ai_tactics import TacticalAI
from engine.models import Entity
from sim.build_learner import _resolve_monster_turn, round_start_relic_choices


def _seal_candidate(e):
    """从当前引擎状态生成封存快照（复用引擎内部序列化）。"""
    return e._serialize_full_character()


def _engine_with_snapshot(snapshot, seed=1):
    """用封存快照的玩家作为起始角色，进入乱葬岗。"""
    e = GameEngine(db_path="/tmp/test_sealed_dg.db", rng_seed=seed)
    e.execute_action("setup_attributes", {"name": "贾凡", "blood_points": 10,
                                          "speed_points": 8, "mana_points": 7})
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    # 用快照玩家替换（封存角色带成长后的属性/道纹/遗物）
    player = e._deserialize_entity_full(snapshot["player"])
    e.state.player = player
    for f in snapshot.get("friends", []):
        e.state.friends.append(e._deserialize_entity_full(f))
    for emp in snapshot.get("employees", []):
        e.state.employees.append(e._deserialize_entity_full(emp))
    e.state.shards = snapshot.get("shards", 20)
    e.state.resonance = dict(snapshot.get("resonance") or {})
    for r in snapshot.get("relics", []):
        from engine.models import Relic
        e.state.relics.append(Relic(name=r["name"], effect=r.get("effect", ""), tags=r.get("tags") or []))
    return e


def test_sealed_candidate_fights_in_dungeon():
    """正常路径：封存角色（成长后属性/道纹/遗物）能在乱葬岗打完整场战斗。"""
    # 造一个"成长后"的快照（模拟一阶封存）
    e0 = GameEngine(db_path="/tmp/test_seal_src.db", rng_seed=1)
    e0.execute_action("setup_attributes", {"name": "贾凡", "blood_points": 11,
                                           "speed_points": 8, "mana_points": 6})
    e0.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e0.execute_action("setup_choose_region", {"region": "龙心谷"})
    e0.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    # 成长：+3法限 +2速限 + 学道纹 + 1朋友
    e0.state.player.mana_limit += 6
    e0.state.player.speed_limit += 3
    for dw in ("庇护", "再生"):
        e0.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen",
                                                "tier": 1, "names": [dw]})
    e0.state.friends.append(Entity("岩行者", "朋友", blood_limit=54, current_hp=54,
                                   attack_count=3, attack_power=5))
    e0.state.shards = 80
    snapshot = _seal_candidate(e0)

    e = _engine_with_snapshot(snapshot, seed=7)
    p = e.state.player
    assert p.mana_limit >= 12 and p.speed_limit >= 8, "封存角色应带成长属性"
    assert "庇护" in p.dao_wen and "再生" in p.dao_wen, "封存角色应带已学道纹"
    assert len(e.state.friends) == 1, "封存角色应带朋友"

    # 打一场乱葬岗战斗（显式提交战始遗物）
    e.state.energy = 0
    active = {r.name for r in e.state.relics if e.state.sealed_relics.get(r.name, 0) <= 0}
    bs_choices = {n: {"use": False} for n in ("三相残韵盘", "折速法印", "猩红果实", "苍白之花") if n in active}
    bs = e.execute_action("battle_start", {"relic_choices": bs_choices})
    assert bs["success"], bs
    assert len(e.state.enemies) >= 1, "乱葬岗应出怪"
    ai = TacticalAI(e)
    survived = False
    for rnd in range(1, 30):
        if not e.state.player or not e.state.player.is_alive:
            break  # 玩家阵亡：死之传承中断会阻塞后续行动，属正常失败路径
        rs = e.execute_action("round_start", {"relic_choices": round_start_relic_choices(e)})
        assert rs["success"], rs
        if not e.state.player.is_alive:
            break
        ai.new_round()
        ai.take_turn()
        if not [x for x in e.state.enemies if x.is_alive]:
            survived = True
            break
        if not e.state.player.is_alive:
            break
        e.execute_action("resolve_ally_phases", {})
        if not [x for x in e.state.enemies if x.is_alive]:
            survived = True
            break
        if not e.state.player.is_alive:
            break
        mp = _resolve_monster_turn(e)
        assert mp["success"], mp
        if not e.state.player.is_alive:
            break
        e.execute_action("round_end", {})
        if not [x for x in e.state.enemies if x.is_alive]:
            survived = True
            break
    # 战斗必须正常推进（无论胜负都不崩）
    assert e.state.current_battle == 1
    assert e.state.current_round >= 1
    assert survived or not e.state.player.is_alive, "战斗应分出胜负或玩家阵亡，不卡死"


def test_sealed_candidate_dungeon_growth_applies():
    """正常路径：封存角色在乱葬岗战终后，朋友仍按成长规则+1攻击次数。"""
    e0 = GameEngine(db_path="/tmp/test_seal_src2.db", rng_seed=1)
    e0.execute_action("setup_attributes", {"name": "贾凡", "blood_points": 11,
                                           "speed_points": 8, "mana_points": 6})
    e0.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e0.execute_action("setup_choose_region", {"region": "龙心谷"})
    e0.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    e0.state.friends.append(Entity("岩行者", "朋友", blood_limit=54, current_hp=54,
                                   attack_count=2, attack_power=4))
    snapshot = _seal_candidate(e0)

    e = _engine_with_snapshot(snapshot, seed=8)
    ally = e.state.friends[0]
    atk_before = ally.attack_count
    e.state.energy = 0
    e.state.enemies.append(Entity("怪", "怪物", blood_limit=30, current_hp=30,
                                  attack_count=1, attack_power=1))
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {"relic_choices": round_start_relic_choices(e)})
    for m in list(e.state.enemies):
        m.is_alive = False
    be = e.execute_action("battle_end", {})
    assert be["success"], be
    assert ally.attack_count == atk_before + 1, f"乱葬岗战终朋友应成长，实{ally.attack_count}"
