#!/usr/bin/env python3
"""
道纹组合锦标赛（实验室模式）

问题：穷举里"转化猎道"猎到的道纹大半是死权重（不在 TACTICAL_ROLES，AI 从不发动），
所以"没有道纹组合打得过再生癌变"这一结论未经检验。本脚本正面检验它：

  实验室授予（战始直接持有目标套牌，绕过转化获取的随机性）——只测组合强度上限，
  不作为正规战报依据；获取可行性由转化猎道族另行验证。

流程：
  阶段1（边际价值）：核心[杀伐,庇护,再生] + 单张候选X → 每张跑N种子
  阶段2（组合）：阶段1前8名的全部两两组合 + 前5名的三张组合 → 每组N种子
  基线：癌变奶怪策略（同属性/同种子/同副本），以及纯核心组对照
  另附手工假设组：纯控锁链（蒙蔽+无力+眩晕+缓慢+束缚——把怪按在0输出等凡庸）

用法：PYTHONPATH=. python3 sim/daowen_combo_tournament.py [--seeds 10] [--region 扭曲都市]
"""
from __future__ import annotations

import argparse
import itertools
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.api import GameEngine
from engine.ai_tactics import TacticalAI
from engine.daowen import DaoWenEngine
from engine.gamedata import ORIGINAL_MONSTER_DAOWEN, SHAFA_LOOP_DAOWEN
from sim.build_learner import _resolve_monster_turn
from sim.optional_actions import battle_start_relic_choices, round_start_relic_choices
from sim.alt_win_paths_probe import player_round_cancer

INTERACTIVE_RELICS = {"折速法印", "三相残韵盘", "回锋刀", "血契", "无所求"}
CORE = ["杀伐", "庇护", "再生"]

# 候选池：AI 战术表内、玩家可持有（排除原始怪物道纹与核心已含）
def candidate_pool() -> list[str]:
    DaoWenEngine.register_all()
    return sorted(n for n in DaoWenEngine._registry
                  if n not in ORIGINAL_MONSTER_DAOWEN and n not in CORE)


def _setup(seed: int, region: str):
    e = GameEngine(db_path=f"/tmp/tourney_{os.getpid()}.db", rng_seed=seed)
    e.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 6, "speed_points": 8, "mana_points": 11})
    rc = list(e.state.pending_relic_choices)
    pick = next((n for n in rc if n not in INTERACTIVE_RELICS),
                next((n for n in rc if n != "无所求"), rc[0]))
    e.execute_action("choose_discovered_relic", {"relic_name": pick})
    dc = list(e.state.pending_initial_daowen_choices)
    dp = "杀伐" if "杀伐" in dc else dc[0]
    e.execute_action("setup_choose_initial_daowen", {"daowen_name": dp})
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": region})
    return e


def run_kit(kit: list[str], seed: int, region: str, policy=None, battles: int = 7) -> dict:
    e = _setup(seed, region)
    # 实验室授予：战始直接持有套牌（绕过获取随机性，测组合强度上限）
    for name in kit:
        e._grant_named_daowen(e.state.player, name)
    ai = TacticalAI(e) if policy is None else None
    cleared = 0
    used: Counter = Counter()
    for battle_no in range(1, battles + 1):
        while e.state.energy > 0:
            e.execute_action("pre_battle_action",
                             {"sub_action": "修行", "tier": 1, "to": "mana"})
        st = e.execute_action("battle_start",
                              {"relic_choices": battle_start_relic_choices(e)})
        if not st.get("success"):
            return {"cleared": cleared, "won": False, "used": dict(used)}
        for _ in range(40):
            if e.state.battle_over():
                break
            e.execute_action("round_start",
                             {"relic_choices": round_start_relic_choices(e)})
            if ai is not None:
                ai.new_round()
                ai.take_turn()
            else:
                policy(e)
            if e.state.battle_won():
                break
            mp = _resolve_monster_turn(e)
            if not mp.get("success") or mp["result"].get("player_dead"):
                break
            e.execute_action("round_end", {})
        if ai is not None:
            used.update(ai.used)
        if e.state.battle_lost() or not e.state.battle_won():
            return {"cleared": cleared, "won": False, "used": dict(used)}
        if not e.execute_action("battle_end", {}).get("success"):
            return {"cleared": cleared, "won": False, "used": dict(used)}
        cleared += 1
    return {"cleared": cleared, "won": True, "used": dict(used)}


def bench_kit(kit, seeds, region, policy=None):
    wins, cleared_sum = 0, 0
    used: Counter = Counter()
    for s in seeds:
        try:
            r = run_kit(kit, s, region, policy=policy)
        except Exception:
            continue
        wins += 1 if r["won"] else 0
        cleared_sum += r["cleared"]
        used.update(r.get("used", {}))
    n = len(seeds)
    return {"kit": kit, "wins": wins, "win_rate": wins / n,
            "avg_cleared": cleared_sum / n, "used": used}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--region", default="扭曲都市")
    args = ap.parse_args()
    seeds = list(range(1, args.seeds + 1))
    region = args.region

    # 基线
    base_core = bench_kit(list(CORE), seeds, region)
    base_nurse = bench_kit(list(CORE), seeds, region, policy=player_round_cancer)
    print(f"基线（{region}，{args.seeds}种子，属性6-8-11）：")
    print(f"  纯核心[杀伐+庇护+再生]×击杀AI: 胜{base_core['wins']} 平均通关{base_core['avg_cleared']:.2f}")
    print(f"  癌变奶怪策略               : 胜{base_nurse['wins']} 平均通关{base_nurse['avg_cleared']:.2f}")

    pool = candidate_pool()
    print(f"\n阶段1：核心+单张边际价值（候选{len(pool)}张）")
    stage1 = []
    for x in pool:
        r = bench_kit(CORE + [x], seeds, region)
        r["name"] = x
        stage1.append(r)
    stage1.sort(key=lambda r: (-r["avg_cleared"], -r["wins"]))
    for r in stage1[:12]:
        cast = r["used"].get(r["name"], 0)
        print(f"  +{r['name']:<4} 胜{r['wins']:>2} 平均通关{r['avg_cleared']:.2f}  实际发动{cast}次")
    never_cast = [r["name"] for r in stage1 if r["used"].get(r["name"], 0) == 0]
    if never_cast:
        print(f"  [警示] 授予后AI仍0次发动: {never_cast}")

    top8 = [r["name"] for r in stage1[:8]]
    print(f"\n阶段2：前8名两两组合 C(8,2)=28 + 前5名三张组合 C(5,3)=10")
    kits = [list(c) for c in itertools.combinations(top8, 2)]
    kits += [list(c) for c in itertools.combinations(top8[:5], 3)]
    stage2 = []
    for extra in kits:
        r = bench_kit(CORE + extra, seeds, region)
        r["name"] = "+".join(extra)
        stage2.append(r)
    stage2.sort(key=lambda r: (-r["avg_cleared"], -r["wins"]))
    for r in stage2[:15]:
        print(f"  +{r['name']:<14} 胜{r['wins']:>2} 平均通关{r['avg_cleared']:.2f}")

    # 手工假设组：纯控锁链
    control = bench_kit(CORE + ["蒙蔽", "无力", "眩晕", "缓慢", "束缚"], seeds, region)
    print(f"\n手工假设组 纯控锁链[蒙蔽+无力+眩晕+缓慢+束缚]: "
          f"胜{control['wins']} 平均通关{control['avg_cleared']:.2f}")
    print(f"  使用分布: {dict(control['used'])}")

    best = stage2[0] if stage2 else None
    print("\n=== 结论对照 ===")
    print(f"  癌变奶怪基线      : {base_nurse['avg_cleared']:.2f} 场 / 胜{base_nurse['wins']}")
    if best:
        print(f"  最强击杀系组合    : {best['name']} → {best['avg_cleared']:.2f} 场 / 胜{best['wins']}")
    print(f"  纯控锁链          : {control['avg_cleared']:.2f} 场 / 胜{control['wins']}")


if __name__ == "__main__":
    main()
