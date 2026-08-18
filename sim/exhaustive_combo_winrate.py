#!/usr/bin/env python3
"""
穷举组合胜率测试（brute force sweep）

按穷举法枚举开局组合的全笛卡尔积：
  属性分配 × 初始残韵 × 策略族（胜利路径×构筑，见 STRATEGIES） × 副本
每个组合跑 N 个种子（每局走完整 7 场通关流程，全部走公开 action，
遵循新开局流程：属性 → 发现遗物3选1 → 发现初始道纹3选1 → 残韵 → 副本）。

两阶段：
  1) 粗扫：全部组合 × --seeds 个种子；
  2) 精扫：粗扫胜率前 --top 名组合 × --refine-seeds 个种子复核。

输出：
  data/exhaustive_combo_results.json   全量数据
  终端打印胜率排名（含开局遗物相关性统计）

用法：
  PYTHONPATH=. python3 sim/exhaustive_combo_winrate.py                # 默认 8+30 种子
  PYTHONPATH=. python3 sim/exhaustive_combo_winrate.py --seeds 4      # 快速粗扫
  PYTHONPATH=. python3 sim/exhaustive_combo_winrate.py --jobs 2       # 并行进程数
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ---------------- 穷举维度 ----------------

# 25点属性分配（血/速/法），1点=6血限=1速限=2法限
ATTR_SPLITS: dict[str, tuple[int, int, int]] = {
    "均衡10-8-7":  (10, 8, 7),
    "厚血13-5-7":  (13, 5, 7),
    "高法7-8-10":  (7, 8, 10),
    "法爆6-8-11":  (6, 8, 11),
    "疾速7-11-7":  (7, 11, 7),
}

RESONANCES = ["转换", "反转", "曲解"]

# 策略族 = 胜利路径 × 对应构筑（配置×策略族的笛卡尔积才是完整的组合穷举）。
#   kind=tactical → 由 TacticalAI 按"保命→收割→输出"打击杀路径
#   kind=policy   → 专用策略函数（见 sim/alt_win_paths_probe.py）
STRATEGIES: dict[str, dict] = {
    "击杀·杀伐系": {"kind": "tactical",
                    "learn": ["庇护", "再生", "冲击", "血债", "慈悲"],
                    "starter": ["杀伐", "血债", "冲击", "庇护"]},
    "击杀·切割系": {"kind": "tactical",
                    "learn": ["切割", "贯穿", "透支", "束缚", "封印", "缓慢", "增殖"],
                    "starter": ["切割", "贯穿", "杀伐", "血债"]},
    # 转化猎道：专属/转化道纹无法局外学习，只能在战斗中用残韵改写怪物道纹获得
    # （施法者永久获得转化产物）。该族局外精力优先「领悟」补残韵（1精力=+1残韵），
    # 由 TacticalAI 的残韵插队在每场战斗中转化高价值怪物道纹，跨场滚雪球——
    # 这是穷举中唯一系统性测试残韵消耗与专属道纹 combo 的策略族。
    "转化猎道":     {"kind": "tactical", "lingwu": True,
                    "learn": ["庇护", "再生"],
                    "starter": ["杀伐", "血债", "冲击", "庇护"]},
    # 定向猎道·控制链：从零合法成型的控制 combo——
    #   局外学习 缓慢/束缚（杀伐闭环），领悟囤 反转/曲解 残韵；
    #   战斗中定向转化怪物道纹：反转必中→蒙蔽、反转疯狂→无力、
    #   曲解减速→眩晕、反转强化→弱化（施法者永久获得转化产物）。
    #   获取难度真实计价：遇不到对应怪物就猎不到，绝不凭空授予。
    "定向猎道·控制链": {"kind": "hunter", "lingwu": True,
                    "lingwu_cycle": ["反转", "曲解", "反转"],
                    "learn": ["庇护", "再生", "缓慢", "束缚"],
                    "starter": ["杀伐", "血债", "冲击", "庇护"]},
    "凡庸盾守":     {"kind": "policy", "policy": "stall",
                    "learn": ["庇护", "杀伐"],
                    "starter": ["庇护", "杀伐", "再生", "血债"]},
    "癌变奶怪":     {"kind": "policy", "policy": "cancer",
                    "learn": ["再生", "庇护", "杀伐"],
                    "starter": ["再生", "庇护", "杀伐", "慈悲"]},
    "奶大砍小·混合": {"kind": "policy", "policy": "hybrid",
                    "learn": ["再生", "庇护", "杀伐"],
                    "starter": ["再生", "杀伐", "庇护", "血债"]},
}

REGIONS = ["罪孽都市", "扭曲都市", "龙心谷"]

# 需要额外显式提交/交互的遗物：默认发现时避开（可以不用但不能不让用）
INTERACTIVE_RELICS = {"折速法印", "三相残韵盘", "回锋刀", "血契", "无所求"}


# ---------------- 单局模拟（全公开 action） ----------------

# 定向猎道转化图：怪物道纹 →（残韵，转化产物）
HUNT_MAP = {
    "必中": ("反转", "蒙蔽"),
    "疯狂": ("反转", "无力"),
    "减速": ("曲解", "眩晕"),
    "强化": ("反转", "弱化"),
    "自愈": ("反转", "衰败"),
}


def _directed_hunt(e, ai):
    """残韵插队（不占出手）：按 HUNT_MAP 定向转化在场怪物道纹。"""
    p = e.state.player
    for m in e.state.enemies:
        if not m.is_alive:
            continue
        for src, (rtype, product) in HUNT_MAP.items():
            if (src in m.dao_wen and product not in p.dao_wen
                    and e.state.resonance.get(rtype, 0) > 0):
                r = e.execute_action("use_resonance", {
                    "source_daowen": src, "resonance_type": rtype, "target": m.name})
                if r.get("success"):
                    ai.used[f"猎道·{product}"] = ai.used.get(f"猎道·{product}", 0) + 1
                    ai.resolve_pending_redemption()


def _monster_evolution_step(e, rng) -> int:
    """怪物方困境响应（README 怪物准则#3：陷入困境强制进化/逃跑二选一）。

    修复（2026-08-18）：旧穷举驱动器从不调用 declare_evolution，怪物困境
    窗口全部被浪费（单次扫描实测扭曲都市 214 个困境回合 0 触发），数据系统性
    偏乐观。现按 pick_best_report 同一口径接线：异变预算内优先进化。
    """
    fired = 0
    try:
        opts = e.combat.get_plight_evolution_options()
    except Exception:
        return 0
    for o in opts:
        pool = o.get("borrowable_daowen") or []
        max_x = o.get("max_x_by_mutation", 0)
        if pool and max_x >= 1:
            r = e.execute_action("declare_evolution", {
                "monster": o["monster"], "daowen": rng.choice(pool),
                "x": min(max_x, 2)})
            if r.get("success"):
                fired += 1
    return fired


def run_one(attr_key: str, resonance: str, build: str, region: str,
            seed: int, battles: int = 7) -> dict:
    from engine.api import GameEngine
    from engine.ai_tactics import TacticalAI
    from sim.build_learner import _resolve_monster_turn
    from sim.optional_actions import battle_start_relic_choices, round_start_relic_choices
    from sim.alt_win_paths_probe import (
        player_round_stall, player_round_cancer, player_round_hybrid)
    import random
    _rng = random.Random(seed * 7919 + 13)  # 进化借用道纹的选取独立于引擎随机源

    strategy = STRATEGIES[build]
    policy_fns = {"stall": player_round_stall, "cancer": player_round_cancer,
                  "hybrid": player_round_hybrid}

    blood, speed, mana = ATTR_SPLITS[attr_key]
    e = GameEngine(db_path=f"/tmp/exhaustive_{os.getpid()}.db", rng_seed=seed)
    e.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": blood, "speed_points": speed, "mana_points": mana})

    # 新开局流程：先发现遗物3选1
    relic_choices = list(e.state.pending_relic_choices)
    relic_pick = next((n for n in relic_choices if n not in INTERACTIVE_RELICS),
                      next((n for n in relic_choices if n != "无所求"), relic_choices[0]))
    e.execute_action("choose_discovered_relic", {"relic_name": relic_pick})

    # 再发现初始道纹3选1（按策略族偏好；候选没有偏好项则取第一项）
    daowen_choices = list(e.state.pending_initial_daowen_choices)
    daowen_pick = next((n for n in strategy["starter"] if n in daowen_choices),
                       daowen_choices[0])
    e.execute_action("setup_choose_initial_daowen", {"daowen_name": daowen_pick})

    e.execute_action("setup_choose_resonance", {"resonance_type": resonance})
    e.execute_action("setup_choose_region", {"region": region})

    ai = TacticalAI(e) if strategy["kind"] in ("tactical", "hunter") else None
    hunter = strategy["kind"] == "hunter"
    policy = policy_fns.get(strategy.get("policy", ""))
    to_learn = list(strategy["learn"])
    cleared = 0

    def _fail(battle_no: int) -> dict:
        return {"cleared": cleared, "won": False, "died_at": battle_no,
                "relic": relic_pick, "daowen": daowen_pick,
                "final_daowen": list(e.state.player.dao_wen) if e.state.player else [],
                "resonance_used": sum(v for k, v in (ai.used if ai else {}).items()
                                      if k.startswith("残韵·"))}

    for battle_no in range(1, battles + 1):
        while e.state.energy > 0:
            if to_learn:
                name = to_learn.pop(0)
                r = e.execute_action("pre_battle_action",
                                     {"sub_action": "学习", "sub": "daowen", "name": name})
                if not r.get("success"):
                    err = str(r.get("error", ""))
                    if "已经掌握" in err:
                        continue  # 初始道纹已覆盖，跳过且不耗精力
                    to_learn.insert(0, name)
                    e.execute_action("pre_battle_action",
                                     {"sub_action": "修行", "tier": 1, "to": "mana"})
            elif strategy.get("lingwu"):
                # 猎道/转化族：富余精力用于领悟补残韵（可指定残韵配比）
                cycle = strategy.get("lingwu_cycle", ["反转", "转换", "曲解"])
                rtype = cycle[(e.state.energy + battle_no) % len(cycle)]
                r = e.execute_action("pre_battle_action",
                                     {"sub_action": "领悟", "resonance_type": rtype})
                if not r.get("success"):
                    e.execute_action("pre_battle_action",
                                     {"sub_action": "修行", "tier": 1, "to": "mana"})
            else:
                e.execute_action("pre_battle_action",
                                 {"sub_action": "修行", "tier": 1,
                                  "to": "mana" if battle_no % 2 else "speed"})

        started = e.execute_action("battle_start",
                                   {"relic_choices": battle_start_relic_choices(e)})
        if not started.get("success"):
            return _fail(battle_no)

        for _ in range(40):
            if e.state.battle_over():
                break
            e.execute_action("round_start", {"relic_choices": round_start_relic_choices(e)})
            if ai is not None:
                if hunter:
                    _directed_hunt(e, ai)
                ai.new_round()
                ai.take_turn()
            else:
                policy(e)
            if e.state.battle_won():
                break
            # 怪物困境响应：进化接线（异变预算内借用轮回者道纹）
            _monster_evolution_step(e, _rng)
            if e.state.battle_over():
                break
            mp = _resolve_monster_turn(e)
            if not mp.get("success") or mp["result"].get("player_dead"):
                break
            e.execute_action("round_end", {})

        if e.state.battle_lost() or not e.state.battle_won():
            return _fail(battle_no)
        if not e.execute_action("battle_end", {}).get("success"):
            return _fail(battle_no)
        cleared += 1

    return {"cleared": cleared, "won": True, "died_at": None,
            "relic": relic_pick, "daowen": daowen_pick,
            "final_daowen": list(e.state.player.dao_wen) if e.state.player else [],
            "resonance_used": sum(v for k, v in (ai.used if ai else {}).items()
                                  if k.startswith("残韵·"))}


def bench_combo(combo: tuple[str, str, str, str], seeds: list[int]) -> dict:
    attr_key, resonance, build, region = combo
    wins, total_cleared, errors = 0, 0, 0
    relic_stats: Counter = Counter()
    relic_wins: Counter = Counter()
    daowen_stats: Counter = Counter()
    daowen_wins: Counter = Counter()
    acquired: Counter = Counter()   # 经残韵转化获得的道纹（不在杀伐闭环、非局外可学）
    resonance_used = 0
    deaths: Counter = Counter()
    from engine.gamedata import SHAFA_LOOP_DAOWEN
    for s in seeds:
        try:
            r = run_one(attr_key, resonance, build, region, seed=s)
        except Exception:
            errors += 1
            continue
        total_cleared += r["cleared"]
        relic_stats[r["relic"]] += 1
        daowen_stats[r["daowen"]] += 1
        resonance_used += r.get("resonance_used", 0)
        for name in r.get("final_daowen", []):
            if name not in SHAFA_LOOP_DAOWEN:
                acquired[name] += 1
        if r["won"]:
            wins += 1
            relic_wins[r["relic"]] += 1
            daowen_wins[r["daowen"]] += 1
        else:
            deaths[r["died_at"]] += 1
    n = len(seeds) - errors
    return {
        "combo": {"attrs": attr_key, "resonance": resonance,
                  "build": build, "region": region},
        "runs": n, "wins": wins, "errors": errors,
        "win_rate": (wins / n) if n else 0.0,
        "avg_cleared": (total_cleared / n) if n else 0.0,
        "deaths": {str(k): v for k, v in deaths.items()},
        "relic_picked": dict(relic_stats),
        "relic_won": dict(relic_wins),
        "daowen_picked": dict(daowen_stats),
        "daowen_won": dict(daowen_wins),
        "acquired_daowen": dict(acquired),
        "resonance_used": resonance_used,
    }


def _worker(args):
    combo, seeds = args
    return bench_combo(combo, seeds)


def sweep(combos: list[tuple], seeds: list[int], jobs: int, label: str) -> list[dict]:
    t0 = time.time()
    results: list[dict] = []
    total = len(combos)
    tasks = [(c, seeds) for c in combos]
    if jobs <= 1:
        for i, task in enumerate(tasks, 1):
            results.append(_worker(task))
            if i % 20 == 0 or i == total:
                print(f"  [{label}] {i}/{total} ({time.time() - t0:.0f}s)", flush=True)
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(_worker, t): t for t in tasks}
            for i, f in enumerate(as_completed(futures), 1):
                results.append(f.result())
                if i % 20 == 0 or i == total:
                    print(f"  [{label}] {i}/{total} ({time.time() - t0:.0f}s)", flush=True)
    return results


def print_ranking(results: list[dict], top: int, title: str):
    ranked = sorted(results, key=lambda r: (-r["win_rate"], -r["avg_cleared"]))
    print(f"\n=== {title}（前{top}名） ===")
    print(f"{'#':<3}{'属性':<12}{'残韵':<5}{'策略族':<10}{'副本':<7}"
          f"{'胜率':>7}{'平均通关':>9}{'局数':>5}")
    print("-" * 66)
    for i, r in enumerate(ranked[:top], 1):
        c = r["combo"]
        print(f"{i:<3}{c['attrs']:<12}{c['resonance']:<5}{c['build']:<10}{c['region']:<7}"
              f"{r['win_rate']*100:>6.1f}%{r['avg_cleared']:>9.2f}{r['runs']:>5}")
    return ranked


def aggregate_key(results: list[dict], picked_key: str, won_key: str) -> list[tuple[str, int, int, float]]:
    picked: Counter = Counter()
    won: Counter = Counter()
    for r in results:
        picked.update(r[picked_key])
        won.update(r[won_key])
    rows = []
    for name, n in picked.most_common():
        w = won.get(name, 0)
        rows.append((name, n, w, w / n if n else 0.0))
    rows.sort(key=lambda x: -x[3])
    return rows


def aggregate_relics(results: list[dict]) -> list[tuple[str, int, int, float]]:
    return aggregate_key(results, "relic_picked", "relic_won")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=8, help="粗扫每组种子数")
    ap.add_argument("--refine-seeds", type=int, default=30, help="精扫每组种子数")
    ap.add_argument("--top", type=int, default=20, help="进入精扫的组合数")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2)),
                    help="并行进程数")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "exhaustive_combo_results.json"))
    args = ap.parse_args()

    combos = list(itertools.product(ATTR_SPLITS, RESONANCES, STRATEGIES, REGIONS))
    print(f"穷举组合总数：{len(ATTR_SPLITS)}属性 × {len(RESONANCES)}残韵 × "
          f"{len(STRATEGIES)}策略族 × {len(REGIONS)}副本 = {len(combos)}组，"
          f"每组{args.seeds}局 → 共{len(combos) * args.seeds}局（粗扫）")

    coarse = sweep(combos, list(range(1, args.seeds + 1)), args.jobs, "粗扫")
    ranked = print_ranking(coarse, args.top, f"粗扫排名（每组{args.seeds}局）")

    top_combos = [(r["combo"]["attrs"], r["combo"]["resonance"],
                   r["combo"]["build"], r["combo"]["region"])
                  for r in ranked[:args.top]]
    print(f"\n精扫：前{args.top}名组合 × {args.refine_seeds}种子 "
          f"= {args.top * args.refine_seeds}局")
    refined = sweep(top_combos, list(range(1001, 1001 + args.refine_seeds)),
                    args.jobs, "精扫")
    refined_ranked = print_ranking(refined, args.top, f"精扫复核（每组{args.refine_seeds}局）")

    print("\n=== 开局遗物 → 胜率相关性（全部粗扫局） ===")
    print(f"{'遗物':<8}{'被选局数':>8}{'获胜':>6}{'胜率':>8}")
    print("-" * 32)
    for name, n, w, rate in aggregate_relics(coarse):
        print(f"{name:<8}{n:>8}{w:>6}{rate*100:>7.1f}%")

    print("\n=== 初始道纹 → 胜率相关性（全部粗扫局） ===")
    print(f"{'道纹':<8}{'被选局数':>8}{'获胜':>6}{'胜率':>8}")
    print("-" * 32)
    for name, n, w, rate in aggregate_key(coarse, "daowen_picked", "daowen_won"):
        print(f"{name:<8}{n:>8}{w:>6}{rate*100:>7.1f}%")

    total_res = sum(r.get("resonance_used", 0) for r in coarse)
    acquired_total: Counter = Counter()
    for r in coarse:
        acquired_total.update(r.get("acquired_daowen", {}))
    print(f"\n=== 残韵消耗与转化/专属道纹获得（全部粗扫局，共消耗残韵{total_res}次） ===")
    for name, n in acquired_total.most_common(12):
        print(f"  {name}: 持有局数{n}")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({
            "meta": {
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "coarse_seeds": args.seeds, "refine_seeds": args.refine_seeds,
                "dimensions": {
                    "attrs": list(ATTR_SPLITS), "resonances": RESONANCES,
                    "strategies": {k: {kk: vv for kk, vv in v.items() if kk != "policy"}
                                   for k, v in STRATEGIES.items()},
                    "regions": REGIONS,
                },
            },
            "coarse": coarse,
            "refined": refined,
            "relic_correlation": [
                {"relic": n, "picked": p, "won": w, "win_rate": r}
                for n, p, w, r in aggregate_relics(coarse)
            ],
            "daowen_correlation": [
                {"daowen": n, "picked": p, "won": w, "win_rate": r}
                for n, p, w, r in aggregate_key(coarse, "daowen_picked", "daowen_won")
            ],
        }, f, ensure_ascii=False, indent=1)
    print(f"\n完整数据已写入 {args.out}")
    return refined_ranked


if __name__ == "__main__":
    main()
