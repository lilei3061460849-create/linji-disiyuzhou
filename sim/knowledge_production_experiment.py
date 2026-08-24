"""第十八批（DM 2026-08-23）知识产量实验：当前学习器还在真学习，还是只在高效复制？

六配置 × 双主种子，全部从同一生产KB快照（gen490）出发，唯一变量=学习器
如何探索/确认/传播/遗忘（§十四：环境/规则/怪物/奖励零改动）：

  A    生产基准：75% 确认精英回注 + 25% UCB + 每15代深挖top3×2（=E75=D15）
  B    冻结精英库：算法同 A，库成员冻结于起始快照，禁止新构筑入库
  C50  50% 精英 / 50% UCB（§一.C / §七）
  C25  25% 精英 / 75% UCB
  D0   75/25 无深挖（§八）
  D5   75/25 每5代深挖（更频繁）

逐代记录（§二）：新构筑数 / 入库数 / 通道（elite_copy/elite_mutate/explore）/
采用率 / 库规模；候选管线（§五）：发现（首评≥3.0)→独立复测（累计≥3评、
随机新种子流）→确认（终均值≥2.0)→迁移探针（DEFAULT_POLICY 24局）；
有效新知识=首评≥3.0 ∧ 复测通过 ∧ 与当时库成员共享≤2槽（非微小变异）；
疑似过拟合=复测均值<首评50%。

每15代依赖度探针（§四）：24局记录终局构筑与库成员的槽位共享度
（0/1/2/3/4 → 0%/25%/50%/75%/100% 继承档）。探针不动KB。

阶段二 knockout（§十）：对 A 臂终末库 K0原样/K1删库首/K2库首降权/
K3换差不多构筑（同种子holdout流91020×120局），检验精英真实因果价值。

死斗（§十二）：只记 telemetry duel 计数增量，不以PVE指标代理。
生产KB只读：全程深拷贝，不落盘生产（§十一）。
"""
import argparse
import copy
import os
import json
import random
import sys

sys.path.insert(0, "/home/user/linji-disiyuzhou")
from sim import build_learner as bl

GENS = int(os.environ.get("KPE_GENS", "120"))
RUNS = 6
HOLDOUT_SEED = 91020
HOLDOUT_N = int(os.environ.get("KPE_HOLDOUT_N", "120"))
PROBE_EVERY = 15
PROBE_N = int(os.environ.get("KPE_PROBE_N", "24"))
PROBE_SEED = 55001
CAND_THRESHOLD = 3.0     # 发现：单次评估候选门槛
CONFIRM_EVALS = 3        # 确认：累计评估次数（含首评）
CONFIRM_MEAN = 2.0       # 确认：终均值门槛
OVERFIT_RATIO = 0.5      # 疑似过拟合：复测均值 < 首评×0.5
NEAR_COPY_SHARED = 3     # 与库成员共享≥3/4槽位 = 微小变异

CONFIGS = {
    "A":   {"ratio": 0.75, "deepen": 15, "freeze": False},
    "B":   {"ratio": 0.75, "deepen": 15, "freeze": True},
    "C50": {"ratio": 0.50, "deepen": 15, "freeze": False},
    "C25": {"ratio": 0.25, "deepen": 15, "freeze": False},
    "D0":  {"ratio": 0.75, "deepen": 0,  "freeze": False},
    "D5":  {"ratio": 0.75, "deepen": 5,  "freeze": False},
}


def shared_slots(starter, learn, key):
    return (1 if starter == key[0] else 0) + len(set(learn) & set(key[1]))


def percentile(sorted_vals, lo, hi):
    if not sorted_vals:
        return 0.0
    a = sorted_vals[int(lo * (len(sorted_vals) - 1))]
    b = sorted_vals[int(hi * (len(sorted_vals) - 1))]
    return (a + b) / 2


def run_arm(cfg_name: str, seed: int, kb_path: str, out: str, kb_out: str = None):
    cfg = CONFIGS[cfg_name]
    with open(kb_path, encoding="utf-8") as f:
        k = copy.deepcopy(json.load(f))
    bl.PRIOR_MODE = "confirmed"
    bl.PRIOR_RATIO = cfg["ratio"]
    bl.DEEPEN_EVERY = cfg["deepen"]
    orig_elite = bl.elite_library
    frozen_keys = None
    if cfg["freeze"]:
        frozen_keys = {key for _, key in orig_elite(k)}

        def frozen_library(kk, top=None, _f=frozen_keys):
            pool = [(m, key) for m, key in bl.build_scoreboard(kk, bl.PRIOR_MIN_EVALS)
                    if key in _f]
            return pool[: (bl.PRIOR_TOP if top is None else top)]
        bl.elite_library = frozen_library

    start_lib = [key for _, key in orig_elite(k)]
    duel0 = copy.deepcopy(k.get("telemetry", {}).get("duels", {}))
    rng = random.Random(seed)
    tele = k.setdefault("telemetry", {})

    rows = []            # 每代：gen/region/channel/key/score/is_new/lib尺寸
    lib_snap = {}        # gen -> 当时活跃库 keys（用于发现时刻距离度量）
    probe_rows = []      # 依赖度探针
    for _ in range(GENS):
        k["generation"] += 1
        g = k["generation"]
        region = bl.REGIONS[g % len(bl.REGIONS)]
        pol = bl.learned_policy(k)
        lib_now = [key for _, key in bl.elite_library(k)]
        lib_snap[g] = lib_now
        starter, learn, meta = bl.propose(k, rng, region, return_meta=True)
        key = (starter, tuple(learn))
        is_new = all(h["starter"] != starter or tuple(h["learn"]) != tuple(learn)
                     for h in k["history"])
        adopted = any(shared_slots(starter, learn, key2) >= NEAR_COPY_SHARED
                      for key2 in lib_now)
        score, valid, invalid = bl.fitness(
            starter, learn, RUNS, g, random_seeds=True, rng=rng, telemetry=tele,
            spend_shards=True, region=region, policy=pol)
        k["total_games"] = k.get("total_games", 0) + valid
        k["invalid_games"] = k.get("invalid_games", 0) + invalid
        if valid:
            bl.update(k, starter, learn, score)
        rows.append({"gen": g, "region": region, "channel": meta["channel"],
                     "key": [starter, list(learn)], "score": round(score, 3),
                     "is_new": is_new, "adopted": adopted})
        if cfg["deepen"] and g % cfg["deepen"] == 0:
            for _, (ds0, dl0) in bl.build_scoreboard(k)[:bl.DEEPEN_TOP]:
                for _ in range(bl.DEEPEN_RUNS):
                    ds, dv, di = bl.fitness(
                        ds0, list(dl0), RUNS, g, random_seeds=True, rng=rng,
                        telemetry=tele, spend_shards=True, region=region, policy=pol)
                    k["total_games"] = k.get("total_games", 0) + dv
                    k["invalid_games"] = k.get("invalid_games", 0) + di
                    if dv:
                        bl.update(k, ds0, list(dl0), ds)
        if g % PROBE_EVERY == 0:
            probe_rows.append(run_dependency_probe(k, g))
        if g % 20 == 0:
            print(f"  [{cfg_name} s{seed}] gen{g} score={score:.2f} "
                  f"ch={meta['channel']}", flush=True)
    bl.elite_library = orig_elite

    # ---- 候选管线（§五 发现→复测→确认 三阶段判定） ----
    first_seen = {}
    for r in rows:
        key = (r["key"][0], tuple(r["key"][1]))
        if key not in first_seen:
            first_seen[key] = {"gen": r["gen"], "channel": r["channel"],
                               "first_score": r["score"], "is_new": r["is_new"]}
    counts, sums = {}, {}
    for h in k["history"]:
        key = (h["starter"], tuple(h["learn"]))
        counts[key] = counts.get(key, 0) + 1
        sums[key] = sums.get(key, 0.0) + h.get("score", 0.0)
    discoveries = []
    for key, fs in first_seen.items():
        c = counts.get(key, 0)
        mean_end = sums.get(key, 0.0) / c if c else 0.0
        # 深化评估发生在 deepen 块里也计入 history——同一口径
        max_shared = max((shared_slots(key[0], list(key[1]), k2)
                          for k2 in lib_snap.get(fs["gen"], [])), default=0)
        cand = fs["first_score"] >= CAND_THRESHOLD
        retested = c >= CONFIRM_EVALS
        rec = {"key": [key[0], list(key[1])], "gen": fs["gen"],
               "channel": fs["channel"], "is_new": fs["is_new"],
               "first_score": fs["first_score"], "evals": c,
               "mean_end": round(mean_end, 3),
               "max_shared_at_discovery": max_shared,
               "candidate": cand,
               "retested": retested,
               "retest_pass": retested and mean_end >= CONFIRM_MEAN,
               "overfit_suspect": retested and mean_end < fs["first_score"] * OVERFIT_RATIO}
        rec["valid_new_knowledge"] = bool(
            fs["is_new"] and rec["retest_pass"] and max_shared < NEAR_COPY_SHARED)
        discoveries.append(rec)

    # ---- UCB 通道审计（§六） ----
    def channel_audit(ch):
        sub = [r for r in rows if r["channel"] == ch]
        keys = {(r["key"][0], tuple(r["key"][1])) for r in sub}
        return {
            "count": len(sub), "uniq_keys": len(keys),
            "mean_score": round(sum(r["score"] for r in sub) / len(sub), 3) if sub else None,
            "candidates": sum(1 for d in discoveries
                              if d["channel"] == ch and d["candidate"]),
            "confirm_pass": sum(1 for d in discoveries
                                if d["channel"] == ch and d["retest_pass"]),
            "valid_new_knowledge": sum(1 for d in discoveries
                                       if d["channel"] == ch and d["valid_new_knowledge"]),
            "garbage_lt1": sum(1 for r in sub if r["score"] < 1.0),
        }
    audit = {ch: channel_audit(ch)
             for ch in ["elite_copy", "elite_mutate", "explore"]}

    # ---- 学习曲线（§九，10代窗口） ----
    g0 = rows[0]["gen"]
    windows = []
    for w0 in range(g0, g0 + GENS, 10):
        seg = [r for r in rows if w0 <= r["gen"] < w0 + 10]
        if not seg:
            continue
        scores = sorted(r["score"] for r in seg)
        windows.append({
            "gen": f"{w0}-{seg[-1]['gen']}",
            "pop_mean": round(sum(scores) / len(scores), 3),
            "top10p": round(sum(scores[-max(1, len(scores) // 10):])
                            / max(1, len(scores) // 10), 3),
            "common_p40_70": round(percentile(scores, 0.40, 0.70), 3),
            "new_builds": sum(1 for r in seg if r["is_new"]),
            "adopt_rate": round(sum(1 for r in seg if r["adopted"]) / len(seg), 3),
            "lib_pool": len(bl.build_scoreboard(k, bl.PRIOR_MIN_EVALS)),
        })
    # 局部锁死窗口（采用率≥0.9 且均分不超前窗）
    locks = 0
    for i in range(1, len(windows)):
        if (windows[i]["adopt_rate"] >= 0.9
                and windows[i]["pop_mean"] <= windows[i - 1]["pop_mean"] - 0.05):
            locks += 1
    # 活跃库成员更替次数（新增速度：只在活库计入；B 冻结=必为0）
    churn = 0
    prev_lib = set(lib_snap.get(g0, []))
    for g in sorted(lib_snap):
        cur = set(lib_snap[g])
        if cur != prev_lib:
            churn += len(cur - prev_lib)
        prev_lib = cur

    # ---- holdout：终末 KB 各自口径 × 独立种子流 ----
    holdout = run_holdout(k)

    duel1 = k.get("telemetry", {}).get("duels", {})
    duel_delta = {kk: (duel1.get(kk, 0) - duel0.get(kk, 0))
                  for kk in ("fought", "won", "sealed_no_duel")}
    result = {"config": cfg_name, "seed": seed, "cfg": cfg,
              "start_gen": rows[0]["gen"] - 1, "end_gen": rows[-1]["gen"],
              "start_library": [list(x) for x in [  # 可读化
                  (kk[0], list(kk[1])) for kk in start_lib]],
              "frozen_keys": sorted([str(x) for x in frozen_keys]) if frozen_keys else None,
              "holdout": holdout, "windows": windows, "probes": probe_rows,
              "discoveries": discoveries, "audit_channel": audit,
              "lock_windows": locks, "lib_churn": churn,
              "final_pool": [[round(m, 3), k_[0], list(k_[1])]
                             for m, k_ in bl.build_scoreboard(k, bl.PRIOR_MIN_EVALS)[:10]],
              "duel_delta": duel_delta,
              "total_games": k.get("total_games", 0),
              "invalid_games": k.get("invalid_games", 0),
              "rows": rows}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    if kb_out:
        with open(kb_out, "w", encoding="utf-8") as f:
            json.dump(k, f, ensure_ascii=False, indent=1)
    print(json.dumps({"done": cfg_name, "seed": seed, "holdout": holdout,
                      "valid_new_knowledge": audit and sum(
                          d["valid_new_knowledge"] for d in discoveries)},
                     ensure_ascii=False), flush=True)


def run_dependency_probe(k: dict, g: int) -> dict:
    """§四 精英依赖度探针：沿用当前 propose 口径独立种子流打 PROBE_N 局，
    记录终局构筑与活跃库成员的槽位共享度与通关深度。只读，不动 KB。"""
    pol = bl.learned_policy(k)
    lib_keys = [key for _, key in bl.elite_library(k)]
    pr = random.Random(PROBE_SEED + g)   # 跨臂同检查点同种子流（配对）
    dep_buckets = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    n = cleared = inv = 0
    for i in range(PROBE_N):
        region = bl.REGIONS[i % 3]
        starter, learn, meta = bl.propose(k, pr, region, return_meta=True)
        s = pr.randrange(1, 2 ** 31 - 1)
        r = bl.play(starter, learn, region, s, rng=random.Random(s),
                    spend_shards=True, policy=pol)
        if r.get("invalid"):
            inv += 1
            continue
        n += 1
        cleared += r["cleared"]
        final = r.get("final_daowen") or []
        sh = max((len(set(final) & set(k2[1])) + (1 if k2[0] in final else 0)
                  for k2 in lib_keys), default=0)
        dep_buckets[min(sh, 4)] += 1
    return {"gen": g, "n": n, "invalid": inv,
            "avg_cleared": round(cleared / max(1, n), 3),
            "dep_buckets": dep_buckets,
            "dep_full": round(dep_buckets[4] / max(1, n), 3),
            "dep_zero": round(dep_buckets[0] / max(1, n), 3)}


def run_holdout(k: dict) -> dict:
    pol = bl.learned_policy(k)
    hr = random.Random(HOLDOUT_SEED)
    total = cleared = b1 = full = inv = 0
    for i in range(HOLDOUT_N):
        region = bl.REGIONS[i % 3]
        starter, learn = bl.propose(k, hr, region)
        s = hr.randrange(1, 2 ** 31 - 1)
        r = bl.play(starter, learn, region, s, rng=random.Random(s),
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


# --------------------------------------------------------------------------
# 阶段二：精英库失效测试（§十）+ 迁移探针（§五阶段四）
# --------------------------------------------------------------------------

def run_knockout(kb_path: str, out: str) -> None:
    with open(kb_path, encoding="utf-8") as f:
        base = json.load(f)
    bl.PRIOR_MODE = "confirmed"
    bl.PRIOR_RATIO = 0.75
    board = bl.build_scoreboard(base, bl.PRIOR_MIN_EVALS)[:bl.PRIOR_TOP]
    top1 = board[0][1]
    # 结构相近的弱者：与库首共享≥2槽且均值最低的合格构筑
    sib = None
    for m, key in bl.build_scoreboard(base, bl.PRIOR_MIN_EVALS):
        if key != top1 and shared_slots(key[0], list(key[1]), top1) >= 2:
            if sib is None or m < sib[0]:
                sib = (m, key)
    orig = bl.elite_library
    cells = {}
    # K0 原样
    cells["K0_normal"] = lambda kk, top=None: orig(kk, top)
    # K1 删除库首
    cells["K1_delete_top1"] = lambda kk, top=None: [
        (m, key) for m, key in orig(kk, top or 99) if key != top1][:bl.PRIOR_TOP]
    # K2 库首降权：top3 内部轮换，库首降到末位（70%取lib[0]通道让给他人，
    # 库首仅余 30%×1/3 的随机成员通道；成员资格保留以区别于 K1 删除）
    def _downweight(kk, top=None):
        lst = orig(kk, top or bl.PRIOR_TOP)
        if len(lst) > 1:
            lst = lst[1:] + lst[:1]
        return lst
    cells["K2_downweight_top1"] = _downweight
    # K3 库首替换为结构相近弱者
    def _replace(kk, top=None, _sib=sib):
        lst = [(m, key) for m, key in orig(kk, 99) if key != top1]
        if _sib and all(key != _sib[1] for _, key in lst[:1]):
            lst = [(_sib[0], _sib[1])] + [x for x in lst if x[1] != _sib[1]]
        return lst[: (bl.PRIOR_TOP if top is None else top)]
    cells["K3_replace_sibling"] = _replace

    results = {}
    for name, libfn in cells.items():
        k = copy.deepcopy(base)
        bl.elite_library = libfn
        h = run_holdout(k)
        # 核查操纵生效：统计 120 局提案命中库首的次数
        hr = random.Random(HOLDOUT_SEED)
        hits = 0
        for i in range(HOLDOUT_N):
            starter, learn = bl.propose(k, hr, bl.REGIONS[i % 3])
            if (starter, tuple(learn)) == top1:
                hits += 1
        results[name] = {"holdout": h, "top1_hits_in_120": hits}
        print(f"  {name}: {h} top1命中={hits}", flush=True)
    bl.elite_library = orig
    results["_meta"] = {"top1": [top1[0], list(top1[1])],
                        "sibling": [[sib[1][0], list(sib[1][1])], round(sib[0], 3)]
                        if sib else None,
                        "library": [[kk[0], list(kk[1]), round(m, 3)]
                                    for m, kk in board]}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print(json.dumps({k2: v["holdout"] for k2, v in results.items()
                      if k2.startswith("K")}, ensure_ascii=False), flush=True)


def run_migration(kb_path: str, discover_path: str, out: str) -> None:
    """§五阶段四：有效新知识交给普通个体（DEFAULT_POLICY）独立种子复测。"""
    disc = json.load(open(discover_path))
    cands = [d for d in disc["discoveries"] if d["valid_new_knowledge"]]
    with open(kb_path, encoding="utf-8") as f:
        k = json.load(f)
    out_rows = []
    for d in cands[:4]:
        starter, learn = d["key"][0], d["key"][1]
        mr = random.Random(88001)
        scores = []
        for i in range(24):
            s = mr.randrange(1, 2 ** 31 - 1)
            r = bl.play(starter, list(learn), bl.REGIONS[i % 3], s,
                        rng=random.Random(s), spend_shards=True,
                        policy=bl.DEFAULT_POLICY)
            if not r.get("invalid"):
                scores.append(r["cleared"])
        mig = sum(scores) / len(scores) if scores else 0.0
        out_rows.append({"key": d["key"], "confirm_mean": d["mean_end"],
                         "migration_mean_default_policy": round(mig, 3),
                         "transferable": mig >= 0.8 * d["mean_end"]})
    json.dump(out_rows, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(out_rows, ensure_ascii=False), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["arm", "knockout", "migrate"], required=True)
    ap.add_argument("--config", default=None)
    ap.add_argument("--seed", type=int, default=20260823)
    ap.add_argument("--kb", default="data/build_knowledge.json")
    ap.add_argument("--discover", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--kb-out", default=None)
    a = ap.parse_args()
    if a.phase == "arm":
        run_arm(a.config, a.seed, a.kb, a.out, a.kb_out)
    elif a.phase == "knockout":
        run_knockout(a.kb, a.out)
    else:
        run_migration(a.kb, a.discover, a.out)


if __name__ == "__main__":
    main()
