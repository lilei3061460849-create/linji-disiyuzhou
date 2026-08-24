#!/usr/bin/env python3
"""第十九批 Part1 补充探针：高场次死亡的【真实死因】与异变层数（只读取证）。

b19_consumable_shadow 的 trace 只分了"HP形态"，缺两个关键分量：
  (1) 玩家 _death_ctx 的 subtype（hp_zero / 崩解 / 衰老…）：封印类道纹每次
      发动支付 异变8X，阈值50直接命零——b6/b7 死亡里有多少是自爆？
  (2) 死亡/进场时的 player.mutation_count：残骸(+10异变)在濒临崩解时会被
      预演判 LETHAL 而拒绝——验证"带药而死"是否其实是"药已变毒"。

只读不改：行为与 sim/b19_consumable_shadow 的 current 臂完全一致
（同种子77001、KB均分top2、learned_policy、spend_shards）。
用法: python3 sim/b19_death_source_probe.py --games 500 --out /tmp/b19_probe.json
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_learner as bl  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=500)
    ap.add_argument("--seed", type=int, default=77001)
    ap.add_argument("--kb", default="data/build_knowledge.json")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.kb, encoding="utf-8") as f:
        kb = json.load(f)
    pol = bl.learned_policy(kb)
    builds = [[k[0], list(k[1]), round(m, 3)]
              for m, k in bl.build_scoreboard(kb, 2)[:2]]
    rng = __import__("random").Random(args.seed)
    deaths = []
    run = {"games": 0, "invalid": 0}
    for i in range(args.games):
        starter, learn, mean = builds[i % len(builds)]
        region = bl.REGIONS[i % len(bl.REGIONS)]
        gs = rng.randrange(1, 2 ** 31 - 1)
        r = bl.play(starter, learn, region, gs, rng=__import__("random").Random(gs),
                    spend_shards=True, policy=pol, death_trace=True)
        if r.get("invalid"):
            run["invalid"] += 1
            continue
        run["games"] += 1
        if r["cleared"] == 7:          # 全清无死亡（含死斗），无 payload
            continue
        tr = r.get("death_trace") or {}      # play() 直接回 payload（无 trace 外壳）
        if tr.get("battle", 0) < 5:   # 只取 b5+ 的高场次死亡
            continue
        rec = {
            "battle": tr.get("battle"),
            "primary": tr.get("primary"),
            "entry_hp": (tr.get("rounds") or [{}])[0].get("hp_pct"),
            "killer": (r.get("pm") or {}).get("杀手"),
            "player_daowen": tr.get("player_daowen"),
            "unused_items": tr.get("unused_items"),
            "final_mana": (tr.get("rounds") or [{}])[-1].get("mana"),
            "mutation": tr.get("mutation_total"),
            "death_subtype": tr.get("death_subtype"),
            "death_source": tr.get("death_source"),
        }
        deaths.append(rec)
    out = {"run": run, "builds": builds, "deaths": deaths}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    from collections import Counter
    print("n", len(deaths),
          Counter(d["death_subtype"] for d in deaths).most_common())


if __name__ == "__main__":
    main()
