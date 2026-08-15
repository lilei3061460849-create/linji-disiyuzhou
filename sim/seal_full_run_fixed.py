#!/usr/bin/env python3
"""封印流修正版：完整7场，battle_start校验+工资结算齐全。"""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.api import GameEngine
from sim.build_learner import round_start_relic_choices
from sim.handplay_dungeon_with_winner import load_winner
from sim.alt_path_test import resolve_monster_turn
from sim.guard_full_run import grant_guards, pre_battle, settle_wages

def player_turn(e, log):
    p = e.state.player
    out = []
    for _ in range(max(1, (p.speed_limit + 2) // 3)):
        if not p.is_alive: break
        enemies = [x for x in e.state.enemies if x.is_alive]
        if not enemies: break
        n = len(enemies)
        if "封印" in p.dao_wen and p.current_mana >= 10 * n:
            r = e.execute_action("use_daowen", {"daowen_name": "封印", "x": n,
                                                "target_ref": f"enemy:{e.state.enemies.index(enemies[0])}",
                                                "trigger_spell_choices": {}})
            if r.get("success"):
                log.append(f"  封印X={n} → 移出{n}只怪物")
                out.append(r)
                continue
        target = min(enemies, key=lambda x: x.current_hp)
        x = max(1, p.current_mana - 3)
        if x >= 1:
            r4 = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": x,
                                                 "target_ref": f"enemy:{e.state.enemies.index(target)}",
                                                 "trigger_spell_choices": {}})
            if r4.get("success"):
                out.append(r4)
                continue
        break
    return out

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--winners", type=int, default=6)
    ap.add_argument("--seeds", type=int, default=3)
    a = ap.parse_args()
    winners = sorted(os.listdir("data/real_winners"))[:a.winners]
    stats = []
    for w in winners:
        for si in range(a.seeds):
            seed = 11000 + int(w.split("_")[1].split(".")[0]) * 71 + si * 41
            with open(os.path.join("data/real_winners", w), encoding="utf-8") as f:
                snap = json.load(f)
            e = GameEngine(db_path=tempfile.mktemp(suffix=".db"), rng_seed=seed,
                           sealed_candidate_path="/tmp/seal_fix.json")
            p0 = snap["player"]
            e.execute_action("setup_attributes", {"name": p0["name"], "blood_points": 10,
                                                  "speed_points": 8, "mana_points": 7})
            e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
            setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
            e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
            load_winner(e, snap)
            # 学封印
            if "封印" not in e.state.player.dao_wen:
                rr = e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen",
                                                            "tier": 1, "names": ["封印"]})
            grant_guards(e)
            logs = []
            cleared = 0
            for b in range(1, 8):
                p = e.state.player
                if not p or not p.is_alive:
                    logs.append(f"  第{b}场：阵亡"); break
                pre_battle(e, logs)
                active = {r.name for r in e.state.relics if e.state.sealed_relics.get(r.name, 0) <= 0}
                bs = e.execute_action("battle_start", {"relic_choices": {
                    n2: {"use": False} for n2 in ("三相残韵盘", "折速法印", "猩红果实", "苍白之花") if n2 in active}})
                if not bs.get("success"):
                    logs.append(f"  第{b}场 battle_start失败：{bs.get('error')}"); break
                names = list(bs.get("enemies") or [])
                logs.append(f"  第{b}场出怪：{names}")
                won = False
                for rnd in range(1, 30):
                    p = e.state.player
                    if not p or not p.is_alive: break
                    if not [x for x in e.state.enemies if x.is_alive]: won = True; break
                    e.execute_action("round_start", {"relic_choices": round_start_relic_choices(e)})
                    log = []
                    player_turn(e, log)
                    for line in log: logs.append(f"    R{rnd}{line}")
                    if not [x for x in e.state.enemies if x.is_alive]: won = True; break
                    if not p.is_alive: break
                    e.execute_action("resolve_ally_phases", {})
                    mp = resolve_monster_turn(e, log)
                    if not mp.get("success"):
                        logs.append(f"    R{rnd} 怪物阶段失败 {mp.get('error')}"); break
                    if mp["result"].get("player_dead"): break
                    e.execute_action("round_end", {})
                if won and e.state.player and e.state.player.is_alive:
                    be = e.execute_action("battle_end", {})
                    guard = 0
                    while (be.get("success") and be.get("completed") is False
                           and be.get("pending_wage_decisions")):
                        settle_wages(e, logs)
                        be = e.execute_action("battle_end", {})
                        guard += 1
                        if guard > 5: break
                    if not be.get("success"):
                        logs.append(f"  第{b}场 battle_end失败：{be.get('error')}"); break
                    cleared += 1
                    logs.append(f"  第{b}场 ✅ 碎片+{be.get('result',{}).get('shard_reward',0)}")
                    crown = be.get("result", {}).get("final_crown", {})
                    if crown.get("outcome") == "sealed":
                        logs.append("  ★ 乱葬岗通关，胜者被封存！"); break
                else:
                    logs.append(f"  第{b}场 ❌"); break
            stats.append(cleared)
            print(f"===== {w} · seed={seed} =====")
            for l in logs: print(l)
            print(f"  结果：通关 {cleared}/7 场")
    tot = len(stats)
    print(f"\n===== 封印流(修正版) 汇总（{tot}局）=====")
    for i in range(1, 8):
        n = sum(1 for c in stats if c >= i)
        print(f"  活过第{i}场: {n}/{tot} ({n/tot*100:.0f}%)")

if __name__ == "__main__":
    main()
