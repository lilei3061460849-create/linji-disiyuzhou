"""精英扩散实验（DM实验协议2026-08-23）：验证"传播失败"是否为平台期瓶颈。

细胞（全部隔离生产KB，纯内存知识库，同主种子）：
  A    原学习器（propose 自带 50% 从 best 变异——best=单次评估虚高样本）
  B25  精英库先验回注 25%（选择器=确认精英库：≥2次评估均值排序）
  B50  精英库先验回注 50%
  B75  精英库先验回注 75%
  C    100% 强制复制精英库首位（诊断组：完全解决传播时的理论上界）
回注只提高先验权重（复制或1点变异），不强制；B细胞保留 UCB 探索通道。

逐代记录（DM指标）：
  群体均分 / 精英分位(top10%) / 普通个体分位(p40-70) / 精英采用率
  （提案与当时精英库任何成员 4位共享≥3）/ 精英复制成功率
  （提案==精英库成员时的得分均值=复制实战分）。
终末 holdout：各细胞用**自己的最终知识库**按其各自的propose口径产出120局
  （3副本轮转×40，固定种子流91020，与训练种子流不同）测首胜/场均/全清。
"""
import json
import random
import sys
from collections import defaultdict

sys.path.insert(0, "/home/user/linji-disiyuzhou")
from sim import build_learner as bl

# 2026-08-23：实验口径锚定——A 细胞与 B 细胞 fallback 走 bl.propose，必须保持
# 实验运行时的 legacy 语义（50%从best变异）；生产默认已切换为 confirmed 回注。
bl.PRIOR_MODE = "legacy"
bl.DEEPEN_EVERY = 0

GENS = 90
RUNS = 6
MASTER_SEED = int(__import__("os").environ.get("DIFF_SEED", "777"))
HOLDOUT_SEED = 91020
HOLDOUT_PER_REGION = 40
MUTATE_PROB = 0.5      # 回注时1点变异概率（其余=精确复制）
TOP_LIB = 3
MIN_EVALS = int(__import__("os").environ.get("DIFF_MIN_EVALS", "2"))
# 入库门槛均值（seed4242暴露的放大器风险：确认精英是1.47的平庸货时，
# 75%回注反而把种群拖在A组之下；加均值门禁后不够格的构筑不入库）。0=不设。
MIN_MEAN = float(__import__("os").environ.get("DIFF_MIN_MEAN", "0"))
# 高n复评助推（DM协议§八）：每 DIFF_DEEPEN_EVERY 代对"当前均值前3候选"追加
# DIFF_DEEPEN_RUNS 次评估（把6局一次的方差压下去，幻影在放大前先被复评杀死，
# 真精英更快达到入库置信）。=0 关闭。
DEEPEN_EVERY = int(__import__("os").environ.get("DIFF_DEEPEN_EVERY", "0"))
DEEPEN_TOP = 3


def fresh_kb() -> dict:
    return {"generation": 0, "trials": {}, "pair_scores": {}, "history": [],
            "best": None, "telemetry": {}, "behaviors": {}, "policy_learn": {},
            "lessons": [], "total_games": 0, "invalid_games": 0}


def elite_library(k: dict, top: int = TOP_LIB) -> list:
    """确认精英库：≥MIN_EVALS 次评估 且 均值≥MIN_MEAN，按均值降序取前 top。"""
    counts, sums = {}, {}
    for h in k["history"]:
        key = (h["starter"], tuple(h["learn"]))
        counts[key] = counts.get(key, 0) + 1
        sums[key] = sums.get(key, 0.0) + h["score"]
    ranked = sorted(((sums[key] / c, key) for key, c in counts.items()
                     if c >= MIN_EVALS and sums[key] / c >= MIN_MEAN), reverse=True)
    return ranked[:top]


def overlap_adopt(starter: str, learn: list, k: dict) -> bool:
    """采纳=与当时精英库中任一成员 4 个槽位共享 ≥3。"""
    for _, (s, learn2) in elite_library(k):
        shared = (1 if starter == s else 0) + len(set(learn) & set(learn2))
        if shared >= 3:
            return True
    return False


def propose_injected(k: dict, rng: random.Random, region: str, ratio: float):
    lib = elite_library(k)
    if lib and rng.random() < ratio:
        if len(lib) > 1 and rng.random() < 0.3:
            _, (starter, learn) = rng.choice(lib)
        else:
            _, (starter, learn) = lib[0]
        learn = list(learn)
        if rng.random() < MUTATE_PROB:
            cand = [c for c in bl.learnable_candidates(region)
                    if c not in learn and c != starter]
            if cand and learn:
                learn[rng.randrange(len(learn))] = rng.choice(cand)
        return starter, learn
    return bl.propose(k, rng, region)


def percentile(sorted_vals: list, lo: float, hi: float) -> float:
    if not sorted_vals:
        return 0.0
    a = sorted_vals[int(lo * (len(sorted_vals) - 1))]
    b = sorted_vals[int(hi * (len(sorted_vals) - 1))]
    return (a + b) / 2


def run_cell(name: str, ratio: float) -> dict:
    k = fresh_kb()
    rng = random.Random(MASTER_SEED)   # 细胞间同主种子（配对）
    tele = k["telemetry"]
    rows = []
    for _ in range(GENS):
        k["generation"] += 1
        g = k["generation"]
        region = bl.REGIONS[g % len(bl.REGIONS)]
        pol = bl.learned_policy(k)
        lib_keys = {(s, tuple(ls)) for _, (s, ls) in elite_library(k)}
        if name == "A":
            starter, learn = bl.propose(k, rng, region)
        else:
            starter, learn = propose_injected(k, rng, region, ratio)
        adopted = overlap_adopt(starter, learn, k)
        is_elite_copy = (starter, tuple(learn)) in lib_keys
        score, valid, invalid = bl.fitness(
            starter, learn, RUNS, g, random_seeds=True, rng=rng,
            telemetry=tele, spend_shards=True, region=region, policy=pol)
        k["total_games"] += valid
        k["invalid_games"] += invalid
        if valid:
            bl.update(k, starter, learn, score)
        # 高n复评助推：周期性深挖当前候选头部，把排名方差压下去
        if DEEPEN_EVERY and g % DEEPEN_EVERY == 0:
            counts: dict = {}
            sums: dict = {}
            for h in k["history"]:
                key = (h["starter"], tuple(h["learn"]))
                counts[key] = counts.get(key, 0) + 1
                sums[key] = sums.get(key, 0.0) + h["score"]
            top_keys = [key2 for _, key2 in
                        sorted(((sums[key2] / c, key2) for key2, c in counts.items()),
                               reverse=True)[:DEEPEN_TOP]]
            for d_starter, d_learn_t in top_keys:
                for _ in range(2):
                    ds, dv, di = bl.fitness(
                        d_starter, list(d_learn_t), RUNS, g,
                        random_seeds=True, rng=rng, telemetry=tele,
                        spend_shards=True, region=bl.REGIONS[g % 3], policy=pol)
                    k["total_games"] += dv
                    k["invalid_games"] += di
                    if dv:
                        bl.update(k, d_starter, list(d_learn_t), ds)
        rows.append({"gen": g, "score": score, "adopted": adopted,
                     "elite_copy": is_elite_copy})
    # 窗口化指标
    windows = []
    for w0 in range(1, GENS + 1, 20):
        seg = [r for r in rows if w0 <= r["gen"] < w0 + 20]
        if not seg:
            continue
        scores = sorted(r["score"] for r in seg)
        windows.append({
            "gen": f"{w0}-{w0+len(seg)-1}",
            "pop_mean": round(sum(scores) / len(scores), 3),
            "elite_top10": round(sum(scores[-max(1, len(scores) // 10):])
                                 / max(1, len(scores) // 10), 3),
            "common_p40_70": round(percentile(scores, 0.40, 0.70), 3),
            "adopt_rate": round(sum(1 for r in seg if r["adopted"]) / len(seg), 3),
        })
    copies = [r["score"] for r in rows if r["elite_copy"]]
    replication = round(sum(copies) / len(copies), 3) if copies else None
    # holdout：用细胞自己的最终知识库与自己propose口径，独立种子流
    pol = bl.learned_policy(k)
    hr = random.Random(HOLDOUT_SEED)
    h_total = 0; h_cle = 0; h_b1 = 0; h_full = 0; h_inv = 0; h_scores = []
    for i in range(HOLDOUT_PER_REGION * 3):
        region = bl.REGIONS[i % 3]
        if name == "A":
            starter, learn = bl.propose(k, hr, region)
        else:
            starter, learn = propose_injected(k, hr, region, ratio)
        seed = hr.randrange(1, 2 ** 31 - 1)
        r = bl.play(starter, learn, region, seed, rng=random.Random(seed),
                    spend_shards=True, policy=pol)
        if r.get("invalid"):
            h_inv += 1
            continue
        h_total += 1
        h_cle += r["cleared"]
        h_b1 += r["cleared"] >= 1
        h_full += r["cleared"] == 7
        h_scores.append(r["cleared"])
    holdout = {"n": h_total, "avg": round(h_cle / max(1, h_total), 3),
               "b1": round(h_b1 / max(1, h_total), 3),
               "full_clear": h_full, "invalid": h_inv}
    bc = k.get("best_confirmed")
    return {"cell": name, "ratio": ratio, "windows": windows,
            "replication_score": replication, "elite_copies": len(copies),
            "final_best_confirmed": bc, "holdout": holdout,
            "games": k["total_games"], "invalid": k["invalid_games"]}


def main():
    cells = sys.argv[1].split(",") if len(sys.argv) > 1 else ["A", "B25", "B50", "B75", "C"]
    ratio_of = {"A": 0.0, "B25": 0.25, "B50": 0.50, "B75": 0.75, "C": 1.0}
    results = [run_cell(name, ratio_of[name]) for name in cells]
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/diffusion_result.json"
    json.dump(results, open(out, "w"), ensure_ascii=False, indent=1)
    print(json.dumps([{c["cell"]: {"holdout": c["holdout"],
                                   "replication": c["replication_score"]}}
                      for c in results], ensure_ascii=False))


if __name__ == "__main__":
    main()
