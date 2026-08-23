#!/usr/bin/env python3
"""命令朋友/员工替你扛伤——显式护卫机制验证。

引擎机制（已实装）：背负X（龙心谷）：选择目标，其下X次受到伤害由自身承担。
龙心谷事件「接过伤者」给朋友岩行者（2×4/54，背负1）。
本脚本：轮回者命令岩行者「发动背负 打 轮回者」→ 怪物打轮回者的伤害转给岩行者。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.setup_support import finish_initial_daowen

from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance
from sim.build_learner import round_start_relic_choices
from sim.handplay_dungeon_with_winner import load_winner


def make_beifu_friend(name="岩行者", x=1, hp=54, atk_count=2, atk_power=4):
    fr = Entity(name, "朋友", blood_limit=hp, current_hp=hp,
                attack_count=atk_count, attack_power=atk_power)
    fr.dao_wen["背负"] = DaoWenInstance(
        DaoWen(name="背负", formula="", cost_type="", cost_formula="", effect_formula=""), x_value=x)
    return fr


def main():
    winner = sys.argv[1] if len(sys.argv) > 1 else "data/real_winners_sin/winner_01.json"
    with open(winner, encoding="utf-8") as f:
        snap = json.load(f)
    e = GameEngine(db_path=tempfile.mktemp(suffix=".db"), rng_seed=7,
                   sealed_candidate_path="/tmp/guard.json")
    p0 = snap["player"]
    e.execute_action("setup_attributes", {"name": p0["name"],
                                          "blood_points": 10, "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    load_winner(e, snap)
    e.state.friends.append(make_beifu_friend())
    # 休整回满
    while e.state.energy > 0:
        p = e.state.player
        if p and p.current_hp < p.blood_limit:
            e.execute_action("pre_battle_action", {
                "sub_action": "休整", "tier": 3,
                "heal_allocations": [{"target_ref": "player:0", "amount": 48}]})
            continue
        e.execute_action("pre_battle_action", {
            "sub_action": "修行", "tier": 1,
            "allocations": {"speed_points": 0, "mana_points": 1}})
    e.state.energy = 0
    from sim.optional_actions import start_battle
    bs, _art = start_battle(e)
    print("出怪:", bs.get("enemies"))

    for rnd in range(1, 15):
        p = e.state.player
        fr = e.state.friends[0]
        if not p.is_alive:
            print(f"R{rnd} 玩家阵亡")
            break
        if not [x for x in e.state.enemies if x.is_alive]:
            print(f"R{rnd} 清场")
            break
        from sim.optional_actions import start_round
        rs, _rsart = start_round(e)
        print(f"\nR{rnd} 回始：玩家hp={p.current_hp} 岩行者hp={fr.current_hp} 速={p.current_speed}")
        # 命令岩行者：发动背负 打 轮回者（X=1，挡1次伤害）
        r = e.execute_action("command_ally", {
            "ally_ref": "friend:0", "instruction": "发动背负 打 轮回者"})
        print("  命令岩行者「发动背负 打 轮回者」:", r.get("success"), r.get("error", ""))
        if r.get("success"):
            detail = r.get("result", {}).get("detail") or {}
            print("    → 效果:", json.dumps(detail.get("effects"), ensure_ascii=False)[:200])
        # 玩家杀伐输出
        for m in [x for x in e.state.enemies if x.is_alive][:1]:
            if p.current_mana >= 2:
                rr = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": min(p.current_mana, 10),
                                                     "target_ref": f"enemy:{e.state.enemies.index(m)}",
                                                     "trigger_spell_choices": {}})
                if rr.get("success"):
                    dmg = sum(ef.get("actual_damage", 0) for ef in
                              (rr.get("execution", {}).get("effects") or []))
                    print(f"  杀伐打{m.name} → {dmg}伤")
        # 怪物阶段：看伤害是否转给岩行者
        from sim.alt_path_test import resolve_monster_turn
        mp = resolve_monster_turn(e, [])
        for d in (mp.get("result", {}).get("details") or []):
            for h in (d.get("hits") or []):
                print(f"  [怪{d.get('attacker')}→{h.get('target')}] 伤{h.get('damage_dealt')} "
                      f"撤退={h.get('retreated')} 目标hp_after={h.get('target_hp_after')}")
        print(f"  怪物阶段后：玩家hp={p.current_hp} 岩行者hp={fr.current_hp} "
              f"岩行者退场={fr.has_retreated} 敌={[(m.name, m.current_hp) for m in e.state.enemies if m.is_alive]}")
        if mp["result"].get("player_dead"):
            print("  玩家阵亡")
            break
        e.execute_action("round_end", {})


if __name__ == "__main__":
    main()
