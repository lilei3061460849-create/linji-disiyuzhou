#!/usr/bin/env python3
"""嫁祸转伤（命令挡伤强化版）——乱葬岗完整7场挑战。

配置：龙心谷胜者（法高）+ 轮回者持【嫁祸】（龙心谷道纹，X自由）+
      铁卫员工×2（威胁50天然抗伤）+ 岩行者（背负1命令挡伤）。
战法：R1 嫁祸X=3 转伤给铁卫1 → 命令铁卫攻击 → 杀伐秒怪 → 庇护兜底。
数据全部取自引擎返回值（逐回合转伤/血条）。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance
from sim.build_learner import round_start_relic_choices
from sim.handplay_dungeon_with_winner import load_winner
from sim.alt_path_test import resolve_monster_turn
from sim.guard_full_run import grant_guards, grant_yanxingzhe, pre_battle, settle_wages


def grant_jiahuo(e):
    """龙心谷胜者持嫁祸（龙心谷专属道纹，经残韵解锁后可学，X自由控）。"""
    if "嫁祸" not in e.state.player.dao_wen:
        e.state.player.dao_wen["嫁祸"] = DaoWenInstance(
            DaoWen(name="嫁祸", formula="", cost_type="", cost_formula="", effect_formula=""), x_value=0)


def player_turn(e, log):
    p = e.state.player
    out = []
    jh_done = False
    # 1) 嫁祸：转伤给铁卫1（每场1次，X=3挡3次）
    if "嫁祸" in p.dao_wen:
        g = next((x for x in e.state.employees if x.is_alive and not x.has_retreated), None)
        if g and not getattr(g, "_jh_used", False) and p.current_mana >= 15:
            r = e.execute_action("use_daowen", {
                "daowen_name": "嫁祸", "x": 3, "target_ref": f"employee:{e.state.employees.index(g)}",
                "trigger_spell_choices": {}})
            if r.get("success"):
                log.append(f"  嫁祸X=3 → 轮回者下3次受伤由{g.name}承担")
                g._jh_used = True
                out.append(r)
                jh_done = True
    # 2) 命令铁卫攻击（威胁+输出）
    for idx, emp in enumerate(e.state.employees):
        if emp.is_alive and emp.is_deployed and not emp.has_retreated:
            r2 = e.execute_action("command_ally", {"ally_ref": f"employee:{idx}", "instruction": "攻击"})
            if r2.get("success"):
                out.append(r2)
    # 3) 命令岩行者背负（每回合挡1次）
    fr = next((f for f in e.state.friends if f.name == "岩行者" and f.is_alive and not f.has_retreated), None)
    if fr:
        r3 = e.execute_action("command_ally", {"ally_ref": "friend:0", "instruction": "发动背负 打 轮回者"})
        if r3.get("success"):
            log.append("  命令岩行者「发动背负 打 轮回者」")
            out.append(r3)
    # 4) 玩家杀伐秒怪
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
    ap.add_argument("--winners", type=int, default=4)
    ap.add_argument("--seeds", type=int, default=3)
    a = ap.parse_args()
    winners = sorted(os.listdir("data/real_winners"))[:a.winners]
    stats = []
    for w in winners:
        for si in range(a.seeds):
            seed = 8000 + int(w.split("_")[1].split(".")[0]) * 61 + si * 31
            with open(os.path.join("data/real_winners", w), encoding="utf-8") as f:
                snap = json.load(f)
            e = GameEngine(db_path=tempfile.mktemp(suffix=".db"), rng_seed=seed,
                           sealed_candidate_path="/tmp/jh_full.json")
            p0 = snap["player"]
            e.execute_action("setup_attributes", {"name": p0["name"],
                                                  "blood_points": 10, "speed_points": 8, "mana_points": 7})
            e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
            setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
            e.execute_action("choose_discovered_relic",
                             {"relic_name": setup["result"]["relic_choices"][0]})
            load_winner(e, snap)
            grant_jiahuo(e)
            grant_guards(e)
            grant_yanxingzhe(e)
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
            print(f"===== {w} + 嫁祸/铁卫/岩行者 · seed={seed} =====")
            for l in logs:
                print(l)
            print(f"  结果：通关 {cleared}/7 场")
    tot = len(stats)
    print(f"\n===== 嫁祸转伤流 汇总（{tot}局）=====")
    for i in range(1, 8):
        n = sum(1 for c in stats if c >= i)
        print(f"  活过第{i}场: {n}/{tot} ({n/tot*100:.0f}%)")


if __name__ == "__main__":
    main()
