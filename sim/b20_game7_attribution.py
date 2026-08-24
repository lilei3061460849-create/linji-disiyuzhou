#!/usr/bin/env python3
"""第二十批：第七场（b7）死亡归因台账 + Pareto（DM 第六阶段口径）。

不改任何生产行为：与 b19 current 臂同源（learned_policy + spend_shards），
death_trace 钩子只读取证。逐局记录：种子 / 构筑 / 第七场敌人 / HP / mana /
道纹 / 剩余消耗品 / 死亡前3回合状态 / 最终死亡原因（DM 8 类互斥归类）。

DM 8 类映射（基于 _death_trace_payload 证据，文档化启发式）：
  1 爆发伤害      末回始 HP≥40% 且非自爆/非RNG嫌疑
  2 持续伤害      近3回始 HP% 单调下降且末回始<40%
  3 mana枯竭      死亡时法力=0 且末回始<40%
  4 道纹/构筑劣势  自爆（崩解/癌变 death_subtype）优先判此；否则敌道纹总数>1.5×玩家
  5 速度劣势      以上皆不且玩家速度<敌方最高速
  6 消耗品未及时用  以上皆不且带药阵亡（unused_consumables）
  7 RNG           rng_suspect：末回始≥60% 且速度不劣（无机制解释的高血线暴毙）
  8 其他          残余

用法: python3 sim/b20_game7_attribution.py --games 500 --seed 88001 \
      --out data/experiments/b20_game7_attribution.json
"""
import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_learner as bl  # noqa: E402

SELF_DETONATE = ("崩解", "癌变", "凡庸")
CLASS_NAMES = {1: "爆发伤害", 2: "持续伤害", 3: "mana枯竭", 4: "道纹/构筑劣势",
               5: "速度劣势", 6: "消耗品未及时使用", 7: "RNG", 8: "其他"}


def classify(tr: dict) -> int:
    """DM 8 类互斥归因（顺序=优先级，全部依据 trace 证据）。"""
    st = tr.get("death_subtype", "")
    prim = tr.get("primary")
    fl = tr.get("flags", {})
    if any(k in st for k in SELF_DETONATE):
        return 4
    if prim == "rng_suspect":
        return 7
    if prim == "burst":
        return 1
    if prim == "sustained":
        return 2
    if prim == "mana_exhaust":
        return 3
    if prim == "build_disadv":
        return 4
    if fl.get("speed_disadv"):
        return 5
    if fl.get("unused_consumables"):
        return 6
    return 8


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=500)
    ap.add_argument("--seed", type=int, default=88001)
    ap.add_argument("--kb", default="data/build_knowledge.json")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    kb = json.load(open(args.kb, encoding="utf-8"))
    pol = bl.learned_policy(kb)
    builds = [[k[0], list(k[1]), round(m, 3)]
              for m, k in bl.build_scoreboard(kb, 2)[:2]]
    rng = random.Random(args.seed)
    ledger = []
    n_games = n_invalid = n_b7_reached = 0
    for i in range(args.games):
        starter, learn, mean = builds[i % len(builds)]
        region = bl.REGIONS[i % len(bl.REGIONS)]
        gs = rng.randrange(1, 2 ** 31 - 1)
        r = bl.play(starter, learn, region, gs, rng=random.Random(gs),
                    spend_shards=True, policy=pol, death_trace=True)
        if r.get("invalid"):
            n_invalid += 1
            continue
        n_games += 1
        tr = r.get("death_trace")
        if not tr:
            continue
        if tr.get("battle") != 7:
            continue          # 只要第七场失败
        n_b7_reached += 1
        rounds = tr.get("rounds") or []
        rec = {
            "seed": gs, "region": region, "kb_mean": mean,
            "build": {"starter": starter, "learn": learn},
            "b7_enemies": [{"name": x["name"], "daowen": x["daowen"],
                            "speed": x["speed"]} for x in tr.get("enemies", [])],
            "death_hp": tr.get("hp_pct_at_last_round"),
            "death_mana": rounds[-1].get("mana") if rounds else None,
            "player_daowen": tr.get("player_daowen"),
            "unused_consumables": tr.get("unused_items"),
            "last3_rounds": rounds[-3:],
            "death_subtype": tr.get("death_subtype"),
            "death_source": tr.get("death_source"),
            "killer": (r.get("pm") or {}).get("killer"),
            "engine_primary": tr.get("primary"),
            "flags": tr.get("flags"),
            "dm_class": classify(tr),
        }
        ledger.append(rec)

    pareto = Counter(rec["dm_class"] for rec in ledger)
    out = {"batch": 20, "games": n_games, "invalid": n_invalid,
           "b7_deaths": len(ledger), "builds": builds,
           "pareto": {f"{k}_{CLASS_NAMES[k]}": pareto.get(k, 0) for k in range(1, 9)},
           "ledger": ledger}
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"games={n_games} invalid={n_invalid} b7_deaths={len(ledger)}")
    for k in range(1, 9):
        n = pareto.get(k, 0)
        pct = 100.0 * n / max(1, len(ledger))
        print(f"  {k} {CLASS_NAMES[k]:8s} {n:4d} ({pct:5.1f}%)")
    print("->", args.out)


if __name__ == "__main__":
    main()
