"""第十九批 Part3：死斗专项实验室（与PVE学习完全解耦；禁止用PVE均分做代理）。

机制理解（代码实证）：
  - 擂主=封存槽文件队首快照（完整角色，含队伍/遗物）；挑战者全清7场后触发；
  - 挑战者胜→擂主永久出局、挑战者进阶封存；挑战者负/超时→擂主回队首；
  - 死斗走同一战斗管线：挑战者在玩家相位先动（结构先手恒挑战者）；
  - 超时→判擂主卫冕（日志[死斗超时]回合N）。

实验设计（擂主生态固定=受控对照）：
  warmup: 生产KB人口在独立lab封存槽自然全清，攒出真实擂主队列（生态快照文件）
  eval:   每局开始**重置封存文件=生态快照**——所有候选面对完全相同的擂主队列；
          候选=生产确认精英池（按均值top8）。只按死斗表现晋升（§三协议）。
          指标：死斗胜率+Wilson CI、回合数、超时占比、击杀/被击杀、
          挑战者/擂主构筑对照。
  holdout生态：换主种子重做一次 warmup 得到生态B——在生态B上复测，
          双生态 Wilson 下界均≥0.35 才晋升"死斗专属精英"（死斗迁移门）。
"""
import argparse
import json
import os
import random
import shutil
import sys

sys.path.insert(0, "/home/user/linji-disiyuzhou")
from sim import build_learner as bl

LAB = {"db_path": "/tmp/b19_duel.db",
       "sealed_path": "/tmp/b19_duel_sealed.json",
       "death_book_path": "/tmp/b19_duel_book.md"}
ECO_A = "/tmp/b19_eco_A.json"
ECO_B = "/tmp/b19_eco_B.json"
PROMOTE_WILSON = 0.35    # 死斗晋升线（PVE内部基线≈14%，取其2.5倍）


def wilson(w: int, n: int, z: float = 1.96) -> tuple:
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = w / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    return (round(p, 3), round(max(0.0, (c - m) / d), 3), round(min(1.0, (c + m) / d), 3))


def ecology_builds(path: str) -> list:
    """生态快照里各擂主的构筑指纹（初始+道纹名）。"""
    try:
        data = json.load(open(path, encoding="utf-8"))
    except OSError:
        return []
    out = []
    for q in (data.get("candidates") or {}).get("1", []):
        pl = q.get("player") or {}
        dw = sorted((pl.get("dao_wen") or {}).keys()) if isinstance(
            pl.get("dao_wen"), dict) else sorted(pl.get("dao_wen") or [])
        out.append(dw)
    return out


def run_warmup(seed: int, games: int, eco_out: str, kb_path: str) -> None:
    if os.path.exists(LAB["sealed_path"]):
        os.remove(LAB["sealed_path"])
    with open(kb_path, encoding="utf-8") as f:
        k = json.load(f)
    pol = bl.learned_policy(k)
    rng = random.Random(seed)
    seals = 0
    for i in range(games):
        region = bl.REGIONS[i % len(bl.REGIONS)]
        starter, learn = bl.propose(k, rng, region)
        gs = rng.randrange(1, 2 ** 31 - 1)
        r = bl.play(starter, learn, region, gs, rng=random.Random(gs),
                    spend_shards=True, policy=pol, lab_paths=LAB)
        if r.get("invalid"):
            continue
        seals += bool(r.get("sealed")) or (r["cleared"] == 7 and not r.get("duel_fought"))
        if i % 100 == 99 and os.path.exists(LAB["sealed_path"]):
            try:
                cur = json.load(open(LAB["sealed_path"], encoding="utf-8"))
                seals = len((cur.get("candidates") or {}).get("1", []))
            except (ValueError, OSError):
                seals = 0
            print(f"  warmup {i+1}/{games} 擂主队列={seals}", flush=True)
        if seals >= 4 and i >= 200:
            break
    if os.path.exists(LAB["sealed_path"]):
        shutil.copy(LAB["sealed_path"], eco_out)
    print(f"warmup done: {i+1}局 队列={ecology_builds(eco_out)}", flush=True)


def run_eval(seed: int, games: int, eco: str, out: str, kb_path: str,
             min_duels: int = 24, max_games: int = 0) -> None:
    with open(kb_path, encoding="utf-8") as f:
        k = json.load(f)
    pol = bl.learned_policy(k)
    candidates = [[kk[0], list(kk[1]), round(m, 3)]
                  for m, kk in bl.build_scoreboard(k, 2)[:8]]
    if getattr(run_eval, "_cand_filter", None):
        idx = [int(x) for x in run_eval._cand_filter.split(",")]
        candidates = [candidates[i] for i in idx]
    eco_builds = ecology_builds(eco)
    print(f"[eval] 生态擂主={eco_builds}", flush=True)
    print(f"[eval] 候选={[c[:2] for c in candidates]}", flush=True)
    per = {json.dumps(c[:2], ensure_ascii=False): {
        "build": c[:2], "games": 0, "duels": 0, "won": 0, "lost_kill": 0,
        "lost_timeout": 0, "rounds": [], "opp_hist": {}} for c in candidates}
    for c in candidates:
        key = json.dumps(c[:2], ensure_ascii=False)
        rec = per[key]
        rng = random.Random(seed + 7)
        cap = max_games or games
        for i in range(cap):
            # 受控对照：每局重置擂主生态（挑战者胜/负都可能在真实槽里留下变化）
            shutil.copy(eco, LAB["sealed_path"])
            region = bl.REGIONS[i % len(bl.REGIONS)]
            gs = rng.randrange(1, 2 ** 31 - 1)
            r = bl.play(c[0], c[1], region, gs, rng=random.Random(gs),
                        spend_shards=True, policy=pol, lab_paths=LAB)
            if r.get("invalid"):
                continue
            rec["games"] += 1
            if not r.get("duel_fought"):
                continue
            rec["duels"] += 1
            rec["opp_hist"][r.get("duel_opponent", "?")] = \
                rec["opp_hist"].get(r.get("duel_opponent", "?"), 0) + 1
            if r.get("duel_won"):
                rec["won"] += 1
            elif r.get("duel_timeout"):
                rec["lost_timeout"] += 1
            else:
                rec["lost_kill"] += 1
            if r.get("duel_rounds"):
                rec["rounds"].append(r["duel_rounds"])
            if rec["duels"] >= min_duels and i >= games // 2:
                break
        print(f"  {c[0]}+{'/'.join(c[1])}: {rec['won']}/{rec['duels']} "
              f"({rec['games']}局) t/o负{rec['lost_timeout']} 被杀负{rec['lost_kill']}",
              flush=True)
    rows = []
    for key, rec in per.items():
        p, lo, hi = wilson(rec["won"], rec["duels"])
        rows.append({"build": rec["build"], "games": rec["games"],
                     "duels": rec["duels"], "won": rec["won"],
                     "wr": p, "wilson": [lo, hi],
                     "lost_timeout": rec["lost_timeout"],
                     "lost_kill": rec["lost_kill"],
                     "rounds_avg": round(sum(rec["rounds"]) / len(rec["rounds"]), 2)
                     if rec["rounds"] else None,
                     "opp_hist": rec["opp_hist"]})
    result = {"eco": eco, "eco_builds": eco_builds, "seed": seed,
              "rows": sorted(rows, key=lambda x: -x["wr"])}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(json.dumps([{"+".join(map(str, r["build"])): [r["wr"], r["wilson"],
                       r["duels"], r["lost_timeout"], r["lost_kill"]]}
                      for r in result["rows"]], ensure_ascii=False), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["warmup", "eval"], required=True)
    ap.add_argument("--seed", type=int, default=88001)
    ap.add_argument("--games", type=int, default=400)
    ap.add_argument("--eco", default=ECO_A)
    ap.add_argument("--kb", default="data/build_knowledge.json")
    ap.add_argument("--out", default=None)
    ap.add_argument("--cand", default=None, help="候选下标选择，如 0,1,2,3 分片")
    ap.add_argument("--min-duels", type=int, default=24)
    a = ap.parse_args()
    if a.cand is not None:
        run_eval._cand_filter = a.cand
    if a.phase == "warmup":
        run_warmup(a.seed, a.games, a.eco, a.kb)
    else:
        run_eval(a.seed, a.games, a.eco, a.out, a.kb,
                 min_duels=a.min_duels)


if __name__ == "__main__":
    main()
