#!/usr/bin/env python3
"""
自学习流派优化器：让 AI 通过多次轮回自己发现哪些道纹组合 1+1>2。

与 sim/build_winrate.py 的区别：
  build_winrate 是"固定套路"——我事先写死几个流派，只是测它们的胜率。
  build_learner 不预设任何流派：它自己组合道纹、跑轮回、根据胜负更新权重，
  并把学到的结果**写回 JSON**，下次启动继续在此基础上进化。

方法（多臂老虎机 + 协同增益挖掘）：
  1. 每轮从候选道纹池按 UCB1 采样一套 build（初始道纹 + 学习序列）
  2. 跑 N 局，得到 fitness（通关场数 + 胜负加权）
  3. 用 fitness 更新：
       - 单道纹价值   value[A]
       - 配对协同     synergy[A,B] = 含AB的平均分 - (含A平均 + 含B平均)/2
     synergy > 0 即 1+1>2
  4. 精英组合交叉变异产生下一代，持续迭代
  5. 全部状态存入 data/build_knowledge.json，可反复续跑累积经验

用法：
    python3 sim/build_learner.py --generations 20 --runs 6
    python3 sim/build_learner.py --report          # 只看已学到的知识
    python3 sim/build_learner.py --reset           # 清空重学
"""
import argparse
import json
import math
import os
import random
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.ai_tactics import TacticalAI, TACTICAL_ROLES

KNOWLEDGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "build_knowledge.json")

# 候选池：所有 AI 会主动使用的道纹（数据驱动，跟着 TACTICAL_ROLES 走）
CANDIDATES = sorted(TACTICAL_ROLES.keys())
# 可作为初始道纹的（README：开局在【杀伐】【锐利】中选一种）
STARTERS = ["杀伐", "锐利"]
REGIONS = ["罪孽都市", "扭曲都市", "龙心谷"]
BUILD_SIZE = 5          # 每套 build 学习的道纹数量


# --------------------------------------------------------------------------
# 一局轮回
# --------------------------------------------------------------------------

def play(starter: str, learn: list, region: str, seed: int, battles: int = 7) -> dict:
    e = GameEngine(db_path="/tmp/learner.db", rng_seed=seed)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    e.execute_action("setup_choose_daowen", {"daowen": starter})
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": region})

    ai = TacticalAI(e)
    todo = list(learn)
    cleared = 0

    for b in range(1, battles + 1):
        # 局外：学习优先，但**始终保留至少1点精力用于修行**，
        # 否则前期属性完全不成长（这正是早前 debuff 流派第1场就暴毙的原因之一）
        budget = e.state.energy
        learn_quota = max(0, min(len(todo), budget - 1)) if b <= 3 else len(todo)
        while e.state.energy > 0:
            if todo and learn_quota > 0:
                name = todo.pop(0)
                learn_quota -= 1
                if not e.execute_action("pre_battle_action",
                                        {"sub_action": "学习", "sub": "daowen",
                                         "name": name}).get("success"):
                    pass
            else:
                e.execute_action("pre_battle_action",
                                 {"sub_action": "修行", "tier": 1,
                                  "to": "mana" if b % 2 else "speed"})

        e.execute_action("battle_start")
        for _ in range(40):
            if not e.state.player or not e.state.player.is_alive:
                break
            if not [x for x in e.state.enemies if x.is_alive]:
                break
            e.execute_action("round_start", {})
            ai.new_round()
            ai.take_turn()
            if not [x for x in e.state.enemies if x.is_alive]:
                break
            mp = e.execute_action("monster_phase", {})
            if mp["result"].get("player_dead"):
                break
            e.execute_action("round_end", {})

        if not e.state.player or not e.state.player.is_alive:
            return {"cleared": cleared, "won": False}
        e.execute_action("battle_end", {})
        cleared += 1

    return {"cleared": cleared, "won": True}


def fitness(starter: str, learn: list, runs: int, gen: int) -> float:
    """适应度 = 平均通关场数 + 3×胜率（0~10）。跨代换种子，避免过拟合。"""
    total = 0.0
    for i in range(runs):
        seed = gen * 1000 + i * 7 + 1
        region = REGIONS[i % len(REGIONS)]
        r = play(starter, learn, region, seed)
        total += r["cleared"] + (3.0 if r["won"] else 0.0)
    return total / runs


# --------------------------------------------------------------------------
# 知识库
# --------------------------------------------------------------------------

def load() -> dict:
    if os.path.exists(KNOWLEDGE):
        with open(KNOWLEDGE, encoding="utf-8") as f:
            return json.load(f)
    return {"generation": 0, "trials": {}, "pair_scores": {}, "history": [], "best": None}


def save(k: dict) -> None:
    os.makedirs(os.path.dirname(KNOWLEDGE), exist_ok=True)
    with open(KNOWLEDGE, "w", encoding="utf-8") as f:
        json.dump(k, f, ensure_ascii=False, indent=1)


def ucb(k: dict, name: str, total_n: int) -> float:
    """UCB1：平衡"已知高分"与"尝试次数少"。"""
    t = k["trials"].get(name)
    if not t or t["n"] == 0:
        return 1e9                      # 没试过的优先试
    mean = t["sum"] / t["n"]
    return mean + 1.4 * math.sqrt(math.log(max(total_n, 2)) / t["n"])


def propose(k: dict, rng: random.Random) -> tuple:
    """生成下一套待测 build：50% 探索，50% 在精英基础上变异。"""
    total_n = sum(t["n"] for t in k["trials"].values()) or 1
    best = k.get("best")
    if best and rng.random() < 0.5:
        learn = list(best["learn"])
        starter = best["starter"]
        # 变异：替换1~2个位置
        for _ in range(rng.randint(1, 2)):
            if learn:
                i = rng.randrange(len(learn))
                pool = [c for c in CANDIDATES if c not in learn and c != starter]
                if pool:
                    ranked = sorted(pool, key=lambda c: -ucb(k, c, total_n))
                    learn[i] = rng.choice(ranked[:8])
        if rng.random() < 0.25:
            starter = rng.choice(STARTERS)
        return starter, learn

    starter = rng.choice(STARTERS)
    pool = [c for c in CANDIDATES if c != starter]
    ranked = sorted(pool, key=lambda c: -ucb(k, c, total_n))
    head = ranked[:12]
    rng.shuffle(head)
    return starter, head[:BUILD_SIZE]


def update(k: dict, starter: str, learn: list, score: float) -> None:
    members = [starter] + list(learn)
    for m in members:
        t = k["trials"].setdefault(m, {"n": 0, "sum": 0.0})
        t["n"] += 1
        t["sum"] += score
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            key = "|".join(sorted((members[i], members[j])))
            p = k["pair_scores"].setdefault(key, {"n": 0, "sum": 0.0})
            p["n"] += 1
            p["sum"] += score
    if not k.get("best") or score > k["best"]["score"]:
        k["best"] = {"starter": starter, "learn": list(learn), "score": score}
    k["history"].append({"gen": k["generation"], "starter": starter,
                         "learn": list(learn), "score": round(score, 3)})


def synergies(k: dict, min_n: int = 2) -> list:
    """协同增益：pair 均分 − 两个单体均分的平均。>0 即 1+1>2。"""
    out = []
    for key, p in k["pair_scores"].items():
        if p["n"] < min_n:
            continue
        a, b = key.split("|")
        ta, tb = k["trials"].get(a), k["trials"].get(b)
        if not ta or not tb or ta["n"] == 0 or tb["n"] == 0:
            continue
        solo = (ta["sum"] / ta["n"] + tb["sum"] / tb["n"]) / 2
        out.append((p["sum"] / p["n"] - solo, a, b, p["n"], p["sum"] / p["n"]))
    out.sort(reverse=True)
    return out


def report(k: dict) -> None:
    print(f"已学习代数：{k['generation']}｜累计试验：{len(k['history'])} 套")
    if k.get("best"):
        b = k["best"]
        print(f"\n★ 目前最优：初始【{b['starter']}】+ {b['learn']}   适应度 {b['score']:.2f}/10")

    ranked = sorted(((t["sum"] / t["n"], n, t["n"])
                     for n, t in k["trials"].items() if t["n"] > 0), reverse=True)
    print("\n【单道纹价值 Top12】(平均适应度 × 试验次数)")
    for v, n, cnt in ranked[:12]:
        print(f"  {n:<6}{v:6.2f}  ({cnt}次)")
    if len(ranked) > 12:
        print("  ...最低3个：", "、".join(f"{n}{v:.2f}" for v, n, _ in ranked[-3:]))

    syn = synergies(k)
    print("\n【协同增益 Top10  —— 1+1>2 的组合】")
    if not syn:
        print("  （数据不足，需要更多代数）")
    for d, a, b, n, avg in syn[:10]:
        print(f"  {a}+{b:<6} 增益{d:+.2f}  组合均分{avg:.2f} ({n}次)")
    neg = [s for s in syn if s[0] < 0]
    if neg:
        print("\n【负协同 —— 互相拖累，应避免同时携带】")
        for d, a, b, n, avg in neg[-5:]:
            print(f"  {a}+{b:<6} 增益{d:+.2f} ({n}次)")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--generations", type=int, default=10)
    ap.add_argument("--runs", type=int, default=6, help="每套build评估局数")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    if a.reset and os.path.exists(KNOWLEDGE):
        os.remove(KNOWLEDGE)
        print("已清空知识库")

    k = load()
    if a.report:
        report(k)
        return

    rng = random.Random(a.seed or None)
    for g in range(a.generations):
        k["generation"] += 1
        starter, learn = propose(k, rng)
        score = fitness(starter, learn, a.runs, k["generation"])
        update(k, starter, learn, score)
        star = " ★新最优" if k["best"]["score"] == score else ""
        print(f"第{k['generation']:>3}代  【{starter}】{'+'.join(learn):<28} → {score:5.2f}{star}")
        save(k)

    print()
    report(k)
    save(k)


if __name__ == "__main__":
    main()
