"""DM实验协议§十 对照：原学习器(legacy) vs 新学习器(confirmed+深挖复评)。

两臂从**同一生产KB快照**出发，同主种子跑 N 代生产主循环，再用各臂最终KB
按各自 propose 口径在固定 holdout 种子流（91020，与扩散实验同口径）打120局。
唯一变量=学习器如何利用已确认经验（游戏规则、引擎、种子流完全一致）。

用法：
  python3 sim/production_ab.py --arm legacy --gens 60 --seed 20260823 \
      --kb data/build_knowledge.json --out /tmp/prod_ab_legacy.json
  python3 sim/production_ab.py --arm confirmed ... --out /tmp/prod_ab_confirmed.json
注意：--kb 不会被写回（深拷贝），生产数据安全；两臂可并行两个进程。
"""
import argparse
import copy
import json
import random
import sys

sys.path.insert(0, "/home/user/linji-disiyuzhou")
from sim import build_learner as bl

HOLDOUT_SEED = 91020          # 与 sim/elite_diffusion_experiment.py 同口径
HOLDOUT_N = 120
RUNS = 6


def run_generations(k: dict, gens: int, seed: int) -> list:
    """生产主循环复刻（主种子配对）；模式已由外部设置 bl.PRIOR_MODE/DEEPEN_EVERY。"""
    rng = random.Random(seed)
    tele = k.setdefault("telemetry", {})
    rows = []
    for _ in range(gens):
        k["generation"] += 1
        g = k["generation"]
        region = bl.REGIONS[g % len(bl.REGIONS)]
        pol = bl.learned_policy(k)
        starter, learn = bl.propose(k, rng, region)
        score, valid, invalid = bl.fitness(
            starter, learn, RUNS, g, random_seeds=True, rng=rng,
            telemetry=tele, spend_shards=True, region=region, policy=pol)
        k["total_games"] = k.get("total_games", 0) + valid
        k["invalid_games"] = k.get("invalid_games", 0) + invalid
        if valid:
            bl.update(k, starter, learn, score)
        deepened = 0
        if bl.DEEPEN_EVERY and g % bl.DEEPEN_EVERY == 0:
            for _, (d_starter, d_learn) in bl.build_scoreboard(k)[:bl.DEEPEN_TOP]:
                for _ in range(bl.DEEPEN_RUNS):
                    ds, dv, di = bl.fitness(
                        d_starter, list(d_learn), RUNS, g, random_seeds=True,
                        rng=rng, telemetry=tele, spend_shards=True,
                        region=region, policy=pol)
                    k["total_games"] = k.get("total_games", 0) + dv
                    k["invalid_games"] = k.get("invalid_games", 0) + di
                    if dv:
                        bl.update(k, d_starter, list(d_learn), ds)
                deepened += 1
        rows.append({"gen": g, "score": round(score, 3), "deep": deepened})
        print(f"  gen{g:>4} {region} 【{starter}】{'+'.join(learn)} -> {score:.2f}"
              + (f"  深挖{deepened}构筑" if deepened else ""), flush=True)
    return rows


def run_holdout(k: dict) -> dict:
    """用本臂最终KB与自己的propose口径，固定种子流测 通关均分/首胜/全清。"""
    pol = bl.learned_policy(k)
    hr = random.Random(HOLDOUT_SEED)
    total = cleared = b1 = full = inv = 0
    for i in range(HOLDOUT_N):
        region = bl.REGIONS[i % 3]
        starter, learn = bl.propose(k, hr, region)
        seed = hr.randrange(1, 2 ** 31 - 1)
        r = bl.play(starter, learn, region, seed, rng=random.Random(seed),
                    spend_shards=True, policy=pol)
        if r.get("invalid"):
            inv += 1
            continue
        total += 1
        cleared += r["cleared"]
        b1 += r["cleared"] >= 1
        full += r["cleared"] == 7
    return {"n": total, "avg": round(cleared / max(1, total), 3),
            "b1": round(b1 / max(1, total), 3), "full_clear": full,
            "invalid": inv}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=["legacy", "confirmed"], required=True)
    ap.add_argument("--gens", type=int, default=60)
    ap.add_argument("--seed", type=int, default=20260823)
    ap.add_argument("--kb", default="data/build_knowledge.json")
    ap.add_argument("--kb-out", default=None,
                    help="终末知识库落盘路径（默认不落盘）")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    if a.arm == "legacy":
        bl.PRIOR_MODE = "legacy"
        bl.DEEPEN_EVERY = 0
    else:
        bl.PRIOR_MODE = "confirmed"
        bl.DEEPEN_EVERY = 15

    # 深拷贝指定KB，绝不写回生产数据
    with open(a.kb, encoding="utf-8") as f:
        k = copy.deepcopy(json.load(f))
    print(f"[{a.arm}] 起始 gen={k['generation']} 库"
          f"{[(round(m, 2), kk[0]) for m, kk in bl.elite_library(k)]}", flush=True)
    rows = run_generations(k, a.gens, a.seed)
    holdout = run_holdout(k)
    bc = k.get("best_confirmed")
    lib = [(round(m, 3), kk[0], list(kk[1])) for m, kk in bl.elite_library(k, top=8)]
    result = {"arm": a.arm, "gens": a.gens, "seed": a.seed,
              "start_gen": k["generation"] - a.gens, "end_gen": k["generation"],
              "holdout": holdout, "best_confirmed": bc, "elite_library": lib,
              "total_games": k.get("total_games", 0),
              "invalid_games": k.get("invalid_games", 0),
              "gen_scores": [r["score"] for r in rows]}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    if a.kb_out:
        with open(a.kb_out, "w", encoding="utf-8") as f:
            json.dump(k, f, ensure_ascii=False, indent=1)
    print(json.dumps({a.arm: {"holdout": holdout,
                              "best_confirmed": bc}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
