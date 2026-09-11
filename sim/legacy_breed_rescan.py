#!/usr/bin/env python3
"""养蛊复测（2026-09-10 遗言桥配置）：LegacyAwareAI + 正典《死者之书》+ 默认普攻池。

与 sim/breed_parallel_scan.py 同口径，仅三处不同：
  · 战斗 AI 换遗言桥（DM裁定 2026-09-10：可参考不照做）；
  · 读正典 死者之书.md（只读；写入必须经 DM submit_ruling 审核，管线无自动写路径）；
  · 胜者快照落 data/legacy_scan_winners/（实验室目录），**绝不触碰 data/breed_winners**。
产出：cleared 分布对照（基线=2026-09-10 上午 200 局诊断）。
"""
from __future__ import annotations

import collections
import json
import multiprocessing as mp
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.build_learner import _play
from sim.breed_and_duel import BUILDS
from sim.legacy_mentor import LegacyAwareAI

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIN_LAB = os.path.join(ROOT, "data", "legacy_scan_winners")

SEED_START = int(sys.argv[1]) if len(sys.argv) > 1 else 1
SEED_END = int(sys.argv[2]) if len(sys.argv) > 2 else 300


def _run(job):
    build, seed = job
    try:
        seal = tempfile.mktemp(suffix=".json")
        r = _play(BUILDS[build]["starter"], BUILDS[build]["learn"], "扭曲都市",
                  seed=seed, battles=7, attrs=BUILDS[build]["attrs"],
                  spend_shards=True, xiuxing=BUILDS[build].get("xiuxing"),
                  ai_cls=LegacyAwareAI,
                  lab_paths={"sealed_path": seal, "db_path": tempfile.mktemp(suffix=".db"),
                             "death_book_path": os.path.join(ROOT, "死者之书.md")})
        cleared = r.get("cleared") or 0
        if cleared < 7:
            return {"build": build, "seed": seed, "cleared": cleared,
                    "invalid": bool(r.get("invalid"))}
        # 7 通关（预期 0）：快照落实验室目录
        snap = None
        try:
            with open(seal, encoding="utf-8") as f:
                data = json.load(f)
            for tier, queue in (data.get("candidates") or {}).items():
                for s in (queue or []):
                    if isinstance(s, dict) and s.get("player"):
                        snap = s
                        break
                if snap:
                    break
        except Exception:
            pass
        if snap is not None:
            os.makedirs(WIN_LAB, exist_ok=True)
            snap["origin"] = {"build": build, "seed": seed, "cleared": cleared}
            with open(os.path.join(WIN_LAB, f"{build}_{seed}.json"), "w",
                      encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False, indent=2)
        return {"build": build, "seed": seed, "cleared": cleared, "WINNER": True}
    except Exception as exc:
        return {"build": build, "seed": seed, "error": str(exc)[:80]}


def main() -> None:
    jobs = [(b, s) for b in BUILDS for s in range(SEED_START, SEED_END + 1)]
    dist: collections.Counter = collections.Counter()
    wins: list = []
    t0 = time.time()
    done = 0
    with mp.Pool(processes=os.cpu_count() or 2) as pool:
        for res in pool.imap_unordered(_run, jobs, chunksize=8):
            done += 1
            if res.get("WINNER"):
                wins.append(res)
                print(f"  🏆 {res['build']} seed={res['seed']} cleared=7", flush=True)
                dist["c7"] += 1
            elif res.get("error"):
                dist["error"] += 1
            elif res.get("invalid"):
                dist["invalid"] += 1
            else:
                dist[f"c{res.get('cleared')}"] += 1
            if done % 300 == 0:
                print(f"[{done}/{len(jobs)}] {dict(sorted(dist.items()))} "
                      f"耗时={time.time()-t0:.0f}s", flush=True)
    print(f"\n复测完成：{done} 局，耗时 {time.time()-t0:.0f}s", flush=True)
    print("cleared 分布:", dict(sorted(dist.items())), flush=True)
    print("7 通关胜者:", len(wins), flush=True)


if __name__ == "__main__":
    main()
