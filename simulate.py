#!/usr/bin/env python3
"""
第四宇宙引擎 · 真实压力模拟器
=====================================
与此前 AI_EXPERIENCE.md 中无法复现的"1920局压力测试"不同：
本脚本的每一局都完整经过 engine/api.py 的公开行动接口结算，
数字可用 `python3 simulate.py` 原样复现（种子固定）。

用法:
    python3 simulate.py                 # 默认 100 局/副本/策略
    python3 simulate.py --runs 500      # 自定义局数
    python3 simulate.py --seed 42       # 自定义种子
    python3 simulate.py --verbose 1     # 打印单局详情

诚实声明：
- 怪物AI与本模拟器中的轮回者策略均为确定性启发式，非最优解；
- 事件系统/遗物(除4件)/员工/副本专属行动未实装，不参与模拟；
- 随机数规则：本模拟器以种子化随机源扮演"提供数字的玩家/DM"角色。
"""
import argparse
import json
import os
import random
import shutil
import sys
from typing import Optional

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from engine.api import GameEngine
from engine.gamedata import MONSTER_POOLS, monster_spawn_count, REGION_BATTLE_COUNT


# ============================================================
# 策略层（扮演 AI 玩家与怪物，只调用公开行动接口）
# ============================================================

class Policy:
    """轮回者策略：局外成长序列 + 战斗行为"""

    def __init__(self, name: str):
        self.name = name

    # ---- 局外 ----
    def pre_battle_plan(self, engine: GameEngine, battle_no: int) -> list[dict]:
        """按战斗场次返回局外行动序列（每项为 pre_battle_action 的参数）"""
        if self.name == "naive_dps":
            # 朴素输出：只修行+休整，不学转化道纹
            return [
                {"sub_action": "修行", "tier": 1},
                {"sub_action": "修行", "tier": 1},
                {"sub_action": "休整", "tier": 1},
            ]
        if self.name == "balanced":
            plans = {
                1: [
                    {"sub_action": "学习", "learn_type": "transform_daowen", "names": ["再生"], "tier": 1},
                    {"sub_action": "学习", "learn_type": "transform_daowen", "names": ["庇护"], "tier": 1},
                    {"sub_action": "休整", "tier": 3},
                ],
            }
            default = [
                {"sub_action": "修行", "tier": 1},
                {"sub_action": "修行", "tier": 1},
                {"sub_action": "休整", "tier": 3},
            ]
            return plans.get(battle_no, default)
        if self.name == "combo":
            plans = {
                1: [
                    {"sub_action": "学习", "learn_type": "transform_daowen", "names": ["再生"], "tier": 1},
                    {"sub_action": "学习", "learn_type": "transform_daowen", "names": ["庇护"], "tier": 1},
                    {"sub_action": "休整", "tier": 3},
                ],
                2: [
                    {"sub_action": "学习", "learn_type": "spell", "names": ["借力打力"], "tier": 1},
                    {"sub_action": "学习", "learn_type": "spell", "names": ["以牙还牙"], "tier": 1},
                    {"sub_action": "休整", "tier": 3},
                ],
            }
            default = [
                {"sub_action": "修行", "tier": 1},
                {"sub_action": "修行", "tier": 1},
                {"sub_action": "休整", "tier": 3},
            ]
            return plans.get(battle_no, default)
        raise ValueError(self.name)

    # ---- 战斗：轮回者回合 ----
    def player_acts(self, engine: GameEngine) -> None:
        player = engine.state.player
        enemies = engine.state.get_all_enemy_side()
        while enemies and player.is_alive:
            budget = engine._player_action_budget()
            if engine.state.actions_used >= budget:
                break
            if player.current_mana < 1:
                break
            target = min(enemies, key=lambda m: m.current_hp)
            mana = player.current_mana

            use = None
            # 防御判断：预测敌方下轮总火力，超过当前生命+格挡的60%则先庇护
            threat = sum(m.attack_count * m.attack_power for m in enemies)
            need_shield = threat > player.current_hp + player.shield * 2 and "庇护" in player.dao_wen
            if need_shield and player.shield < threat // 2:
                use = ("庇护", max(1, mana - 2))
            elif "杀伐" in player.dao_wen:
                use = ("杀伐", mana)
            elif "再生" in player.dao_wen and player.current_hp < player.blood_limit * 0.5:
                use = ("再生", max(1, mana // 2))
            if use is None:
                break
            r = engine.execute_action("use_daowen", {
                "daowen_name": use[0], "x": use[1], "target": target.name,
            })
            if not r.get("success"):
                break
            enemies = engine.state.get_all_enemy_side()

    def dodge_decisions(self, engine: GameEngine, monster, hit_total: int) -> list[bool]:
        """作为防御方为每次命中给出闪避决策"""
        player = engine.state.player
        dodges = []
        for _ in range(hit_total):
            if player is None or player.current_speed < 1:
                dodges.append(False)
                continue
            incoming = monster.attack_power
            lethal_soon = player.current_hp <= incoming * 2
            low_shield = player.shield < incoming
            dodges.append(bool(low_shield and (lethal_soon or player.current_speed > 4)))
        return dodges

    # ---- 战斗：怪物回合 ----
    def monster_acts(self, engine: GameEngine, monster) -> list[dict]:
        acts = []
        defender = engine.state.player
        # 一次性增益：先上关键面板道纹，再输出
        buffs = []
        for dw in ["飞行", "活力", "必中", "自愈", "庇护", "强化"]:
            if dw in monster.dao_wen and not monster.has_status(dw):
                buffs.append(dw)
        for dw in buffs:
            panel_x = 1
            for m in MONSTER_POOLS[engine.state.current_region]:
                if m["name"].startswith(monster.name.rstrip("0123456789")):
                    panel_x = m["daowen"].get(dw, 1)
                    break
            acts.append({"type": "use_daowen", "daowen": dw, "x": max(1, panel_x),
                         "target": monster.name})
        hit_total = 1 if monster.has_status("迟滞") else monster.attack_count
        acts.append({
            "type": "attack_round",
            "target": defender.name if defender else "",
            "dodges": self.dodge_decisions(engine, monster, hit_total),
        })
        return acts


# ============================================================
# 单局轮回模拟
# ============================================================

def handle_pending(engine: GameEngine, rng: random.Random, verbose: bool) -> dict:
    """处理所有挂起的中断与随机请求（种子化随机源扮演提供数字的玩家）"""
    guard = 0
    while guard < 50:
        guard += 1
        if engine._pending_interrupts:
            intr = engine._pending_interrupts[0]
            itype = intr.interrupt_type.value
            if itype == "死之传承":
                engine.submit_ruling("死之传承", "")
                return {"ended": "death"}
            engine.submit_ruling(itype, "", {"choice": "concede"})
            continue
        if engine._pending_random:
            meta = engine._pending_random["meta"]
            pool = meta.get("pool", [])
            if pool:
                engine.execute_action("random_number", {
                    "pool_name": engine._pending_random["pool_name"],
                    "number": rng.randint(1, len(pool)),
                })
                continue
        break
    return {"ended": None}


def run_campaign(policy_name: str, region: str, seed: int, verbose: bool = False) -> dict:
    """完整跑完一局轮回（7场常规战斗，胜则封存冠冕）"""
    rng = random.Random(seed)
    db_dir = f"data/sim/{seed}_{policy_name}_{region}"
    shutil.rmtree(db_dir, ignore_errors=True)
    os.makedirs(db_dir, exist_ok=True)

    engine = GameEngine(db_path=f"{db_dir}/rulings.db")
    policy = Policy(policy_name)

    # 开局（沿用经验库中的标准分配 10/8/7）
    engine.execute_action("setup_attributes", {
        "name": "模拟者", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    engine.execute_action("setup_choose_daowen", {"daowen": "杀伐"})
    engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    engine.execute_action("setup_choose_region", {"region": region})

    # 开局遗物发现（抽3选1：优先已实装遗物）
    engine.execute_action("discover_relic_setup", {})
    pool_result = handle_pending(engine, rng, verbose)
    last = engine._last_result or {}
    if last.get("candidates"):
        engine.execute_action("discover_relic_setup", {"chosen": last["candidates"][0]})

    log = {"policy": policy_name, "region": region, "seed": seed,
           "battles_won": 0, "furthest_battle": 0, "death_battle": None,
           "crown_sealed": False, "duel": None, "relics": []}
    log["relics"] = [r.name for r in engine.state.relics]

    for battle_no in range(1, REGION_BATTLE_COUNT + 1):
        log["furthest_battle"] = battle_no
        if verbose:
            print(f"\n--- 第{battle_no}场 | HP {engine.state.player.current_hp}/{engine.state.player.blood_limit}"
                  f" 法限{engine.state.player.mana_limit} 速限{engine.state.player.speed_limit}"
                  f" 碎片{engine.state.shards} 道纹{list(engine.state.player.dao_wen.keys())}")

        # ---- 局外 ----
        for act in policy.pre_battle_plan(engine, battle_no):
            if engine.state.energy <= 0:
                break
            r = engine.execute_action("pre_battle_action", act)
            handle_pending(engine, rng, verbose)
            if act.get("sub_action") == "修行" and r.get("success"):
                pts = engine.state.attribute_points
                if pts > 0:
                    # 交替加速度与法力
                    if engine.state.player.speed_limit <= engine.state.player.mana_limit:
                        engine.execute_action("spend_attribute_points", {"to": "速限", "points": pts})
                    else:
                        engine.execute_action("spend_attribute_points", {"to": "法限", "points": pts})
        # 未花完的精力用免费休整兜底（休整档位失败自动降档）
        while engine.state.energy > 0:
            spent = False
            for tier in (3, 2, 1):
                r = engine.execute_action("pre_battle_action", {"sub_action": "休整", "tier": tier})
                if r.get("success"):
                    spent = True
                    break
            if not spent:  # 碎片连t1都付不起时只能领悟残韵烧精力
                r = engine.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "反转"})
                if not r.get("success"):
                    break

        # ---- 战始 ----
        engine.execute_action("battle_start", {"battle_background": "模拟背景"})
        handle_pending(engine, rng, verbose)
        if engine.state.phase != "in_combat":
            log["death_battle"] = battle_no
            break

        # ---- 回合循环 ----
        max_rounds = 60
        finished = False
        for _ in range(max_rounds):
            engine.execute_action("round_start", {})

            # 反应型防御：学会后发制人后，怪物出手前插队开启
            player = engine.state.player
            if any(s.name == "借力打力" for s in player.spells) and player.current_mana >= 4:
                enemies = engine.state.get_all_enemy_side()
                if enemies:
                    engine.execute_action("use_spell", {
                        "spell_name": "借力打力",
                        "trigger_timing": "受到伤害前",
                        "target": enemies[0].name,
                        "x": max(1, player.current_mana // 2),
                        "y": 1,
                    })

            policy.player_acts(engine)

            for monster in list(engine.state.enemies):
                if not monster.is_alive or not engine.state.player.is_alive:
                    continue
                acts = policy.monster_acts(engine, monster)
                engine.execute_action("monster_turn", {"monster": monster.name, "acts": acts})

            r = engine.execute_action("round_end", {})
            ended = handle_pending(engine, rng, verbose)
            if ended.get("ended") == "death":
                log["death_battle"] = battle_no
                finished = True
                break
            if r.get("battle_finished") or not engine.state.get_all_enemy_side():
                engine.execute_action("battle_end", {})
                log["battles_won"] += 1
                finished = True
                break

        if not finished or log["death_battle"]:
            if not log["death_battle"]:
                log["death_battle"] = battle_no
            break

        if engine.state.phase == "game_over":
            log["crown_sealed"] = True
            break
        if engine.state.phase == "dead_duel":
            log["duel"] = "triggered"
            break

    if engine.state.phase == "game_over" and engine.state.player.is_alive:
        log["crown_sealed"] = True

    return log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=100, help="每个 副本×策略 的局数")
    ap.add_argument("--seed", type=int, default=20250731, help="基础种子")
    ap.add_argument("--verbose", type=int, default=0)
    args = ap.parse_args()

    regions = ["扭曲都市", "罪孽都市", "龙心谷"]
    policies = ["naive_dps", "balanced", "combo"]

    all_logs = []
    print("=" * 70)
    print(f"真实模拟：{args.runs} 局 × {len(regions)}副本 × {len(policies)}策略  "
          f"（种子{args.seed}，可复现）")
    print("=" * 70)

    for policy in policies:
        for region in regions:
            wins, deaths, furthest_sum, battle_dist = 0, 0, 0, {}
            for i in range(args.runs):
                seed = args.seed + i * 7919
                log = run_campaign(policy, region, seed, verbose=bool(args.verbose))
                all_logs.append(log)
                if log["crown_sealed"]:
                    wins += 1
                if log["death_battle"]:
                    deaths += 1
                    battle_dist[log["death_battle"]] = battle_dist.get(log["death_battle"], 0) + 1
                furthest_sum += log["furthest_battle"]
            rate = wins / args.runs * 100
            print(f"\n[{policy} / {region}] 局数:{args.runs}")
            print(f"  通关(封存冠冕): {wins} ({rate:.1f}%)  死亡: {deaths}  "
                  f"平均推进: {furthest_sum/args.runs:.2f}场")
            print(f"  死亡分布(按场次): {dict(sorted(battle_dist.items()))}")

    out = "data/sim_results.json"
    os.makedirs("data", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "config": {"runs": args.runs, "seed": args.seed,
                       "regions": regions, "policies": policies},
            "logs": all_logs,
        }, f, ensure_ascii=False)
    print(f"\n明细已写入 {out}（共{len(all_logs)}局）")
    print("复现命令: python3 simulate.py --runs %d --seed %d" % (args.runs, args.seed))


if __name__ == "__main__":
    main()
