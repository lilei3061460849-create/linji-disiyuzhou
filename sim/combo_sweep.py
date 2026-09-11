#!/usr/bin/env python3
"""道纹搭配穷举台（2026-09-10 用户指令：穷举测试是否存在任何能 7 通关的搭配）。

结构性去冗余（关键事实）：
  · 初始道纹发现池按**种子**固定——同种子所有构筑面对同一候选池；
    `choose_discovered_initial_daowen(prefer=S)` 只在 S ∈ 池内才选中 S，
    否则一律选 choices[0]。⇒ starter 维度每种子只需测池内成员（≤3 个）。
  · 学习搭档/属性在战斗 2+ 起效，按 (starter ∈ 池) × 搭档组展开。

用法（分相执行，断点续跑：done.jsonl 去重）：
    python3 sim/combo_sweep.py phaseA          # starter=池内成员 × 24 种子
    python3 sim/combo_sweep.py phaseB          # 伤害系 starter × 6 搭档组 × 池内种子
    python3 sim/combo_sweep.py phaseC          # 厨子 sink 大全套 + 属性面
    python3 sim/combo_sweep.py summary         # 汇总 done.jsonl

输出：data/combo_sweep/done.jsonl（逐跑）+ stdout 汇总。
7 通关的跑会以 **CLEAR7** 前缀醒目打印（快照落在临时目录，不进 breed_winners）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "combo_sweep")
DONE_PATH = os.path.join(OUT_DIR, "done.jsonl")

XIUXING_SPEED = {"tier3": {"speed_points": 2, "mana_points": 0},
                 "tier2": {"speed_points": 2, "mana_points": 0}}
XIUXING_MANA = {"tier3": {"speed_points": 0, "mana_points": 2},
                "tier2": {"speed_points": 0, "mana_points": 2}}

ATTRS_SPEED = {"blood_points": 3, "speed_points": 10, "mana_points": 12}   # 全速（A/B 最优）
ATTRS_BALA = {"blood_points": 5, "speed_points": 8, "mana_points": 12}     # 半血半速
ATTRS_MANA = {"blood_points": 3, "speed_points": 6, "mana_points": 16}     # 偏法

BASE_LEARN = ["庇护", "再生"]
PARTNER_PAIRS = [
    ["血债", "波及"], ["贯穿", "波及"], ["增殖", "束缚"],
    ["超频", "庇护"], ["爆裂", "庇护"], ["固执", "庇护"],
]
DAMAGE_STARTERS = ["杀伐", "封印", "血债", "贯穿"]
KITCHEN_SINK = ["透支", "封印", "杀伐", "血债", "贯穿", "波及", "庇护", "再生", "固执", "束缚"]


def _done_keys() -> set:
    if not os.path.exists(DONE_PATH):
        return set()
    keys = set()
    with open(DONE_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                keys.add((row["phase"], row["seed"], row["starter"],
                          tuple(row["learn"]), json.dumps(row["attrs"], sort_keys=True)))
            except Exception:
                continue
    return keys


def _record(phase: str, seed: int, starter: str, learn: list, attrs: dict, cleared) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(DONE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"phase": phase, "seed": seed, "starter": starter,
                            "learn": learn, "attrs": attrs, "cleared": cleared},
                           ensure_ascii=False) + "\n")
    tag = "**CLEAR7**" if (cleared or 0) >= 7 else ""
    print(f"[{phase}] seed{seed:>2} starter={starter:<3} learn={','.join(learn[:3])}"
          f"{'…' if len(learn) > 3 else ''} → cleared={cleared} {tag}", flush=True)


def run_one(phase: str, seed: int, starter: str, learn: list, attrs: dict,
            xiuxing=XIUXING_SPEED, keyset=None) -> None:
    key = (phase, seed, starter, tuple(learn), json.dumps(attrs, sort_keys=True))
    if keyset is not None and key in keyset:
        return
    import sim.breed_and_duel as bd
    cfg = {"starter": starter, "learn": learn, "attrs": attrs, "xiuxing": xiuxing}
    try:
        r = bd._breed_one(f"穷举{starter}", cfg, seed, tempfile.mkdtemp())
        cleared = r.get("cleared") or 0
    except Exception as exc:
        cleared = f"ERR:{type(exc).__name__}:{str(exc)[:60]}"
    _record(phase, seed, starter, learn, attrs, cleared)


def pool_for_seed(seed: int) -> list:
    """只走到开局遗物选择（触发初始道纹发现），读候选池（不跑战斗，~0.1s）。

    注意顺序：道纹发现在「选择开局遗物」成功后才被 offer（api.py choose_discovered_relic，
    source=开局发现 分支）；setup_choose_region 的遗物不是开局发现源。
    """
    import tempfile
    from engine.api import GameEngine
    e = GameEngine(db_path=tempfile.mktemp(suffix=".db"), rng_seed=seed)
    e.execute_action("setup_attributes", {"name": "贾凡", "blood_points": 3,
                                          "speed_points": 10, "mana_points": 12})
    if e.state.pending_relic_choices:
        e.execute_action("choose_discovered_relic",
                         {"relic_name": e.state.pending_relic_choices[0]})
    choices = list(e.state.pending_initial_daowen_choices)
    e.state.pending_initial_daowen_choices = []
    return choices


def phaseA(keyset) -> None:
    """starter 维度穷举：每种子 × 池内每个成员当 starter（基准搭档 庇护+再生）。"""
    for seed in range(1, 25):
        pool = pool_for_seed(seed)
        for pick in pool:
            run_one("A", seed, pick, BASE_LEARN, ATTRS_SPEED, keyset=keyset)


def phaseB(keyset) -> None:
    """搭档维度：伤害系 starter ∈ 池的 (种子, starter) 格 × 6 搭档组。"""
    for seed in range(1, 25):
        pool = pool_for_seed(seed)
        for st in DAMAGE_STARTERS:
            if st not in pool:
                continue
            for pair in PARTNER_PAIRS:
                run_one("B", seed, st, pair, ATTRS_SPEED, keyset=keyset)


def phaseD(keyset) -> None:
    """种子维度扩展：头部构筑 × 种子 25–96（采样更大的发现池/怪物序列空间）。"""
    for seed in range(25, 97):
        for st in ("束缚", "封印", "固执", "贯穿"):
            run_one("D", seed, st, BASE_LEARN, ATTRS_SPEED, keyset=keyset)


def phaseC(keyset) -> None:
    """厨子大全套（learn 10 纹）× 全速 + 属性面（杀伐基准 × 偏法/半血）。"""
    for seed in range(1, 25):
        run_one("C", seed, "杀伐", KITCHEN_SINK, ATTRS_SPEED, keyset=keyset)
        run_one("C", seed, "杀伐", BASE_LEARN, ATTRS_MANA,
                xiuxing=XIUXING_MANA, keyset=keyset)
        run_one("C", seed, "封印", ["杀伐", "再生"], ATTRS_SPEED, keyset=keyset)


def summary() -> None:
    rows = []
    with open(DONE_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    print(f"总跑数 {len(rows)}")
    best = {}
    for r in rows:
        c = r["cleared"] if isinstance(r["cleared"], int) else -1
        k = (r["starter"], ",".join(r["learn"][:2]) + ("…" if len(r["learn"]) > 2 else ""))
        b, n = best.get(k, (0, 0))
        best[k] = (max(b, c), n + 1)
    print(f"{'搭配':<30}{'最佳':>4}{'跑数':>5}")
    for (k, (b, n)) in sorted(best.items(), key=lambda t: -t[1][0]):
        print(f"{str(k):<30}{b:>4}{n:>5}")
    clears = [r for r in rows if r["cleared"] == 7]
    six = [r for r in rows if r["cleared"] == 6]
    print(f"\n7 通关: {len(clears)}/{len(rows)}")
    for r in clears:
        print("  **CLEAR7**", r["phase"], "seed", r["seed"], r["starter"], r["learn"])
    print(f"6/7 最近距离: {len(six)} 跑")
    for r in six:
        print("  6/7:", r["phase"], "seed", r["seed"], r["starter"], r["learn"][:3])


def main() -> None:
    phase = sys.argv[1] if len(sys.argv) > 1 else "phaseA"
    if phase == "summary":
        summary()
        return
    keyset = _done_keys()
    t0 = time.time()
    {"phaseA": phaseA, "phaseB": phaseB, "phaseC": phaseC,
     "phaseD": phaseD}[phase](keyset)
    print(f"== {phase} 完成，耗时 {time.time() - t0:.0f}s，累计 {len(keyset)}+ 新跑 ==")


if __name__ == "__main__":
    main()
