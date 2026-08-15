#!/usr/bin/env python3
"""护卫命令（无消耗强制挡伤）——乱葬岗完整7场挑战。

战法：每回合对每个存活盟友命令「护卫 9」（无消耗、不占出手，强制背负标记）
→ 3盟友=27次挡伤，覆盖3怪墙15次/4怪墙20+次全部命中 → 玩家满血
→ 命令盟友攻击（护卫不占出手，照常输出）→ 玩家杀伐秒怪。
"""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.api import GameEngine
from sim.build_learner import round_start_relic_choices
from sim.handplay_dungeon_with_winner import load_winner
from sim.alt_path_test import resolve_monster_turn
from sim.guard_full_run import pre_battle, settle_wages
from engine.models import Entity


def grant_friends(e, n=3):
    """3名朋友（54血/2×4，无道纹——护卫是强制命令，不依赖盟友道纹）。"""
    for i in range(n):
        if any(fr.name == f"护卫者{i+1}" for fr in e.state.friends):
            continue
        fr = Entity(f"护卫者{i+1}", "朋友", blood_limit=54, current_hp=54,
                    attack_count=2, attack_power=4)
        e.state.friends.append(fr)


def player_turn(e, log):
    p = e.state.player
    out = []
    # 1) 护卫：对每个存活盟友命令「护卫 9」（无消耗，不占出手）
    for prefix, entities in (("friend", e.state.friends), ("employee", e.state.employees)):
        for idx, ally in enumerate(entities):
            if not ally.is_alive or ally.has_retreated:
                continue
            if prefix == "employee" and not ally.is_deployed:
                continue
            r = e.execute_action("command_ally", {
                "ally_ref": f"{prefix}:{idx}", "instruction": "护卫 9"})
            if r.get("success"):
                log.append(f"  命令{ally.name}护卫（替轮回者挡9次伤）")
                out.append(r)
    # 2) 命令盟友攻击（护卫不占出手，照常输出）
    for prefix, entities in (("friend", e.state.friends), ("employee", e.state.employees)):
        for idx, ally in enumerate(entities):
            if not ally.is_alive or ally.has_retreated:
                continue
            if prefix == "employee" and not ally.is_deployed:
                continue
            r2 = e.execute_action("command_ally", {"ally_ref": f"{prefix}:{idx}", "instruction": "攻击"})
            if r2.get("success"):
                out.append(r2)
    # 3) 玩家杀伐秒怪
    for _ in range(max(1, (p.speed_limit + 2) // 3)):
        if not p.is_alive:
            break
        enemies = [x for x in e.state.enemies if x.is_alive]
        if not enemies:
            break
        target = min(enemies, key=lambda x: x.current_hp)
        x = max(1, p.current_mana - 3)
        if x >= 1:
            r4 = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": x,
                                                 "target_ref": f"enemy:{e.state.enemies.index(target)}",
                                                 "trigger_spell_choices": {}})
            if r4.get("success"):
                dmg = sum(ef.get("actual_damage", 0) for ef in
                          (r4.get("execution", {}).get("effects") or []))
                log.append(f"  杀伐X={x} 打{target.name} → {dmg}伤")
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
            seed = 13000 + int(w.split("_")[1].split(".")[0]) * 73 + si * 43
            with open(os.path.join("data/real_winners", w), encoding="utf-8") as f:
                snap = json.load(f)
            e = GameEngine(db_path=tempfile.mktemp(suffix=".db"), rng_seed=seed,
                           sealed_candidate_path="/tmp/gc_full.json")
            p0 = snap["player"]
            e.execute_action("setup_attributes", {"name": p0["name"], "blood_points": 10,
                                                  "speed_points": 8, "mana_points": 7})
            e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
            setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
            e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
            load_winner(e, snap)
            grant_friends(e)  # 3名朋友（54血/2×4），朋友无需部署/无工资
            logs = []
            cleared = 0
            for b in range(1, 8):
                p = e.state.player
                if not p or not p.is_alive:
                    logs.append(f"  第{b}场：阵亡")
                    break
                pre_battle(e, logs)
                active = {r.name for r in e.state.relics if e.state.sealed_relics.get(r.name, 0) <= 0}
                bs = e.execute_action("battle_start", {"relic_choices": {
                    n: {"use": False} for n in ("三相残韵盘", "折速法印", "猩红果实", "苍白之花") if n in active}})
                if not bs.get("success"):
                    logs.append(f"  第{b}场 battle_start失败：{bs.get('error')}")
                    break
                names = list(bs.get("enemies") or [])
                logs.append(f"  第{b}场出怪：{names}")
                won = False
                for rnd in range(1, 30):
                    p = e.state.player
                    if not p or not p.is_alive:
                        break
                    if not [x for x in e.state.enemies if x.is_alive]:
                        won = True
                        break
                    e.execute_action("round_start", {"relic_choices": round_start_relic_choices(e)})
                    log = []
                    player_turn(e, log)
                    for line in log:
                        logs.append(f"    R{rnd}{line}")
                    if not [x for x in e.state.enemies if x.is_alive]:
                        won = True
                        break
                    if not p.is_alive:
                        break
                    e.execute_action("resolve_ally_phases", {})
                    mp = resolve_monster_turn(e, log)
                    if not mp.get("success"):
                        logs.append(f"    R{rnd} 怪物阶段失败 {mp.get('error')}")
                        break
                    if mp["result"].get("player_dead"):
                        break
                    e.execute_action("round_end", {})
                if won and e.state.player and e.state.player.is_alive:
                    be = e.execute_action("battle_end", {})
                    guard = 0
                    while (be.get("success") and be.get("completed") is False
                           and be.get("pending_wage_decisions")):
                        settle_wages(e, logs)
                        be = e.execute_action("battle_end", {})
                        guard += 1
                        if guard > 5:
                            break
                    if not be.get("success"):
                        logs.append(f"  第{b}场 battle_end失败：{be.get('error')}")
                        break
                    cleared += 1
                    logs.append(f"  第{b}场 ✅ 碎片+{be.get('result',{}).get('shard_reward',0)}")
                    crown = be.get("result", {}).get("final_crown", {})
                    if crown.get("outcome") == "sealed":
                        logs.append("  ★ 乱葬岗通关，胜者被封存！")
                        break
                else:
                    logs.append(f"  第{b}场 ❌")
                    break
            stats.append(cleared)
            print(f"===== {w} · seed={seed} =====")
            for l in logs:
                print(l)
            print(f"  结果：通关 {cleared}/7 场")
    tot = len(stats)
    print(f"\n===== 护卫命令流 汇总（{tot}局）=====")
    for i in range(1, 8):
        n = sum(1 for c in stats if c >= i)
        print(f"  活过第{i}场: {n}/{tot} ({n/tot*100:.0f}%)")


if __name__ == "__main__":
    main()
