#!/usr/bin/env python3
"""嫁祸目标=怪物（敌人自残流）——乱葬岗完整7场挑战。

战法：轮回者每回合【嫁祸X】目标=怪物 → 怪物打玩家的伤害转给怪物自己
（玩家免伤+怪物自残双收益）。铁卫天然吸火当第二层肉盾，杀伐补刀。
"""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance
from sim.build_learner import round_start_relic_choices
from sim.handplay_dungeon_with_winner import load_winner
from sim.alt_path_test import resolve_monster_turn
from sim.guard_full_run import grant_guards, pre_battle, settle_wages

def grant_jiahuo(e):
    if "嫁祸" not in e.state.player.dao_wen:
        e.state.player.dao_wen["嫁祸"] = DaoWenInstance(
            DaoWen(name="嫁祸", formula="", cost_type="", cost_formula="", effect_formula=""), x_value=0)

def player_turn(e, log):
    p = e.state.player
    out = []
    # 1) 嫁祸目标=怪物（选攻击次数最多的怪，转伤最大化）
    enemies = [x for x in e.state.enemies if x.is_alive]
    if enemies and "嫁祸" in p.dao_wen and p.current_mana >= 15:
        target = max(enemies, key=lambda x: x.attack_count * x.attack_power)
        x = 2 if p.current_mana >= 45 else 1  # 嫁祸X=2(30法)挡2次+怪自残，剩余法力杀伐
        if x >= 1:
            r = e.execute_action("use_daowen", {"daowen_name": "嫁祸", "x": x,
                                                "target_ref": f"enemy:{e.state.enemies.index(target)}",
                                                "trigger_spell_choices": {}})
            if r.get("success"):
                log.append(f"  嫁祸X={x} 目标={target.name}（怪打轮回者→转伤给{target.name}自己）")
                out.append(r)
    # 2) 命令铁卫攻击
    for idx, emp in enumerate(e.state.employees):
        if emp.is_alive and emp.is_deployed and not emp.has_retreated:
            r2 = e.execute_action("command_ally", {"ally_ref": f"employee:{idx}", "instruction": "攻击"})
            if r2.get("success"):
                out.append(r2)
    # 3) 杀伐补刀
    for _ in range(max(1, (p.speed_limit + 2) // 3)):
        if not p.is_alive: break
        enemies = [x for x in e.state.enemies if x.is_alive]
        if not enemies: break
        target = min(enemies, key=lambda x: x.current_hp)
        x = max(1, p.current_mana - 3)
        if x >= 1:
            r4 = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": x,
                                                 "target_ref": f"enemy:{e.state.enemies.index(target)}",
                                                 "trigger_spell_choices": {}})
            if r4.get("success"):
                dmg = sum(ef.get("actual_damage", 0) for ef in (r4.get("execution", {}).get("effects") or []))
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
            seed = 9000 + int(w.split("_")[1].split(".")[0]) * 67 + si * 37
            with open(os.path.join("data/real_winners", w), encoding="utf-8") as f:
                snap = json.load(f)
            e = GameEngine(db_path=tempfile.mktemp(suffix=".db"), rng_seed=seed,
                           sealed_candidate_path="/tmp/jm_full.json")
            p0 = snap["player"]
            e.execute_action("setup_attributes", {"name": p0["name"], "blood_points": 10,
                                                  "speed_points": 8, "mana_points": 7})
            e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
            setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
            e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
            load_winner(e, snap)
            grant_jiahuo(e)
            grant_guards(e)
            logs = []
            cleared = 0
            for b in range(1, 8):
                p = e.state.player
                if not p or not p.is_alive:
                    logs.append(f"  第{b}场：阵亡"); break
                pre_battle(e, logs)
                from sim.optional_actions import start_battle, start_round
                bs, _art = start_battle(e)
                if not bs.get("success"):
                    logs.append(f"  第{b}场 battle_start失败：{bs.get('error')}"); break
                names = list(bs.get("enemies") or [])
                logs.append(f"  第{b}场出怪：{names}")
                won = False
                for rnd in range(1, 30):
                    p = e.state.player
                    if not p or not p.is_alive: break
                    if not [x for x in e.state.enemies if x.is_alive]: won = True; break
                    rs, _rsart = start_round(e)
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
    print(f"\n===== 嫁祸给怪物(自残流) 汇总（{tot}局）=====")
    for i in range(1, 8):
        n = sum(1 for c in stats if c >= i)
        print(f"  活过第{i}场: {n}/{tot} ({n/tot*100:.0f}%)")

if __name__ == "__main__":
    main()
