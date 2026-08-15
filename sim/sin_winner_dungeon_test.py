#!/usr/bin/env python3
"""罪孽都市胜者（带逼债）手操乱葬岗完整7场。

策略：每回合对每只怪 逼债X（回始削2X血限，无视闪避/格挡/飞行）→ 杀伐补刀
→ 庇护保命 → 反应法术（先发制人/生生不息/后发制人）。每场战前休整+附煞冥煞。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.alt_path_test import setup_engine, resolve_monster_turn
from sim.build_learner import round_start_relic_choices


def pre_battle(e, log):
    while e.state.energy > 0:
        p = e.state.player
        if p and p.current_hp < p.blood_limit:
            r = e.execute_action("pre_battle_action", {
                "sub_action": "休整", "tier": 3,
                "heal_allocations": [{"target_ref": "player:0",
                                      "amount": 48 + e.state.rest_heal_bonus}]})
            if r.get("success"):
                continue
        r = e.execute_action("pre_battle_action", {
            "sub_action": "附煞", "mode": "选择", "sha_qi": "冥煞", "daowen_name": "杀伐"})
        if r.get("success"):
            log.append("  附煞·冥煞·杀伐")
            continue
        e.execute_action("pre_battle_action", {
            "sub_action": "修行", "tier": 1,
            "allocations": {"speed_points": 0, "mana_points": 1}})


def player_turn(e, log):
    """命令员工替轮回者抗伤 + 杀伐输出。

    机制：怪物挑威胁分最高者打（攻力×攻次+输出道纹×10）。高攻员工威胁分
    （50+）压过玩家（30-40），怪物便集火员工，玩家得以满血输出。
    每回合：派遣未上场的员工（R1，消耗出手）→ 命令员工攻击（保持威胁+输出）
    → 玩家杀伐秒怪 → 庇护兜底。"""
    p = e.state.player
    out = []
    # 1) 派遣未上场员工（R1）
    for idx, emp in enumerate(e.state.employees):
        if emp.is_alive and not emp.is_deployed:
            r = e.execute_action("deploy_employee", {"employee_ref": f"employee:{idx}"})
            if r.get("success"):
                log.append(f"  派遣{emp.name}出战（威胁{emp.attack_count}×{emp.attack_power}）")
                out.append(r)
    # 2) 命令员工攻击（威胁保持 + 输出）
    for idx, emp in enumerate(e.state.employees):
        if emp.is_alive and emp.is_deployed and not emp.has_retreated:
            r = e.execute_action("command_ally", {"ally_ref": f"employee:{idx}", "instruction": "攻击"})
            if r.get("success"):
                log.append(f"  命令{emp.name}攻击")
                out.append(r)
    # 3) 玩家杀伐秒怪（冥煞伤害+100%）
    for _ in range(max(1, (p.speed_limit + 2) // 3)):
        if not p.is_alive:
            break
        enemies = [x for x in e.state.enemies if x.is_alive]
        if not enemies:
            break
        target = min(enemies, key=lambda x: x.current_hp)
        x = max(1, p.current_mana - 3)
        if x >= 1:
            r = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": x,
                                                "target_ref": f"enemy:{e.state.enemies.index(target)}",
                                                "trigger_spell_choices": {}})
            if r.get("success"):
                dmg = sum(ef.get("actual_damage", 0) for ef in
                          (r.get("execution", {}).get("effects") or []))
                log.append(f"  杀伐X={x} 打{target.name} → {dmg}伤")
                out.append(r)
                continue
        # 庇护兜底
        threat = sum(x.attack_count * x.attack_power for x in enemies)
        if threat > p.current_hp + p.shield and "庇护" in p.dao_wen and p.current_mana >= 2:
            r = e.execute_action("use_daowen", {"daowen_name": "庇护", "x": 2,
                                                "target_ref": "player:0",
                                                "trigger_spell_choices": {}})
            if r.get("success"):
                log.append(f"  庇护X=2（盾{p.shield}）")
                out.append(r)
                continue
        break
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--winners", type=int, default=6)
    ap.add_argument("--seeds", type=int, default=2)
    a = ap.parse_args()
    winners = sorted(os.listdir("data/real_winners_sin"))[:a.winners]
    stats = []
    for w in winners:
        for si in range(a.seeds):
            seed = 5000 + int(w.split("_")[1].split(".")[0]) * 47 + si * 19
            db = tempfile.mktemp(suffix=".db")
            e = setup_engine(os.path.join("data/real_winners_sin", w), seed, db)
            logs = []
            cleared = 0
            for b in range(1, 8):
                p = e.state.player
                if not p or not p.is_alive:
                    logs.append(f"  第{b}场：阵亡")
                    break
                pre_battle(e, logs)
                active = {r.name for r in e.state.relics if e.state.sealed_relics.get(r.name, 0) <= 0}
                bs_choices = {n: {"use": False} for n in ("三相残韵盘", "折速法印", "猩红果实", "苍白之花")
                              if n in active}
                bs = e.execute_action("battle_start", {"relic_choices": bs_choices})
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
                        break
                    if mp["result"].get("player_dead"):
                        break
                    e.execute_action("round_end", {})
                if won and e.state.player and e.state.player.is_alive:
                    be = e.execute_action("battle_end", {})
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
            print(f"===== 罪孽都市胜者 {w} · seed={seed} =====")
            for l in logs:
                print(l)
            print(f"  结果：通关 {cleared}/7 场")
    tot = len(stats)
    print(f"\n===== 罪孽都市胜者(逼债流) 汇总（{tot}局）=====")
    for i in range(1, 8):
        n = sum(1 for c in stats if c >= i)
        print(f"  活过第{i}场: {n}/{tot} ({n/tot*100:.0f}%)")


if __name__ == "__main__":
    main()
