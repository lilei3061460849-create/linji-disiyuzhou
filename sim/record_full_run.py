#!/usr/bin/env python3
"""跑一场护卫命令流完整乱葬岗轮回，输出逐回合结构化数据（供战报.md撰写）。"""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.api import GameEngine
from engine.models import Entity
from sim.build_learner import round_start_relic_choices
from sim.handplay_dungeon_with_winner import load_winner
from sim.alt_path_test import resolve_monster_turn
from sim.guard_full_run import pre_battle, settle_wages
from sim.guard_cmd_full_run import grant_friends, player_turn

def main():
    winner = sys.argv[1] if len(sys.argv) > 1 else "data/real_winners/winner_02.json"
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 13146
    with open(winner, encoding="utf-8") as f:
        snap = json.load(f)
    e = GameEngine(db_path=tempfile.mktemp(suffix=".db"), rng_seed=seed,
                   sealed_candidate_path="/tmp/rec.json")
    p0 = snap["player"]
    e.execute_action("setup_attributes", {"name": p0["name"], "blood_points": 10,
                                          "speed_points": 8, "mana_points": 7})
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    load_winner(e, snap)
    grant_friends(e)
    print(f"胜者：{p0['name']} 血{p0['blood_limit']} 法{p0['mana_limit']} 速{p0['speed_limit']} 碎片{snap.get('shards')}")
    print(f"道纹：{sorted(p0['dao_wen'])} 法术：{[s['name'] for s in p0['spells']]}")
    print(f"朋友：{[(f.name, f.blood_limit, f.attack_count, f.attack_power) for f in e.state.friends]}")
    print("遗物：", [r.name for r in e.state.relics])
    print()
    for b in range(1, 8):
        p = e.state.player
        if not p or not p.is_alive:
            print(f"== 第{b}场：轮回者已阵亡 ==")
            break
        pre_battle(e, [])
        active = {r.name for r in e.state.relics if e.state.sealed_relics.get(r.name, 0) <= 0}
        bs = e.execute_action("battle_start", {"relic_choices": {
            n: {"use": False} for n in ("三相残韵盘", "折速法印", "猩红果实", "苍白之花") if n in active}})
        if not bs.get("success"):
            print(f"== 第{b}场 battle_start失败：{bs.get('error')} ==")
            break
        names = list(bs.get("enemies") or [])
        print(f"== 第{b}场 [战始] ==")
        print(f"出怪：{names}")
        for m in e.state.enemies:
            dw = "、".join(f"{k}{v.x_value}" for k, v in m.dao_wen.items())
            print(f"  敌方 {m.name}：{m.attack_count}×{m.attack_power}/{m.blood_limit}（{dw}）")
        print(f"我方面板：{p.name} {p.current_hp}/{p.blood_limit} 法{p.current_mana} 速{p.current_speed} | " +
              "、".join(f"{f.name}{f.current_hp}/{f.blood_limit}" for f in e.state.friends))
        won = False
        for rnd in range(1, 30):
            if not p or not p.is_alive:
                break
            if not [x for x in e.state.enemies if x.is_alive]:
                won = True
                break
            e.execute_action("round_start", {"relic_choices": round_start_relic_choices(e)})
            print(f"[回始]：第{rnd}回合")
            print(f"  玩家 hp={p.current_hp}/{p.blood_limit} 法={p.current_mana} 速={p.current_speed} | " +
                  "、".join(f"{f.name}{f.current_hp}/{f.blood_limit}" for f in e.state.friends if f.is_alive) +
                  f" | 敌=" + "、".join(f"{m.name}{m.current_hp}" for m in e.state.enemies if m.is_alive))
            log = []
            player_turn(e, log)
            for line in log:
                print(f"  玩家回合：{line.lstrip()}")
            if not [x for x in e.state.enemies if x.is_alive]:
                won = True
                break
            if not p.is_alive:
                break
            e.execute_action("resolve_ally_phases", {})
            mp = resolve_monster_turn(e, log)
            if not mp.get("success"):
                print(f"  怪物阶段失败：{mp.get('error')}")
                break
            for d in (mp.get("result", {}).get("details") or []):
                for h in (d.get("hits") or []):
                    tgt = h.get("target", "?")
                    dmg = h.get("damage_dealt", 0)
                    hp_after = h.get("target_hp_after")
                    retreat = " 撤退" if h.get("retreated") else ""
                    guard = ""
                    if tgt == p.name and dmg == 0 and hp_after is None:
                        guard = "（被护卫者承担）"
                    print(f"  怪物：{d.get('attacker')} 攻击 {tgt} 造成 {dmg} 伤 目标hp={hp_after}{retreat}{guard}")
            if mp["result"].get("player_dead"):
                print("  玩家阵亡")
                break
            e.execute_action("round_end", {})
            print(f"[回终]：玩家 hp={p.current_hp} 护卫者=" +
                  "、".join(f"{f.name}{f.current_hp}" for f in e.state.friends if f.is_alive) +
                  f" 敌=" + "、".join(f"{m.name}{m.current_hp}" for m in e.state.enemies if m.is_alive))
        if won and e.state.player and e.state.player.is_alive:
            be = e.execute_action("battle_end", {})
            guard = 0
            while (be.get("success") and be.get("completed") is False and be.get("pending_wage_decisions")):
                settle_wages(e, [])
                be = e.execute_action("battle_end", {})
                guard += 1
                if guard > 5:
                    break
            if not be.get("success"):
                print(f"== 第{b}场 battle_end失败：{be.get('error')} ==")
                break
            print(f"[战终]：第{b}场通关，碎片+{be.get('result',{}).get('shard_reward',0)} → 共{e.state.shards}")
            crown = be.get("result", {}).get("final_crown", {})
            if crown.get("outcome") == "sealed":
                print(f"★ 最终的冠冕：{crown.get('sealed_name')} 连同队伍完整封存，乱葬岗通关！")
                break
        else:
            print(f"[死亡结算]：第{b}场阵亡")
            break

if __name__ == "__main__":
    main()
