#!/usr/bin/env python3
"""测试【承露盏】+【再生/透支/杀伐】+三阶法术【血溅五步】。

这是针对一个明确构筑的真实引擎测试，不是遗物池粗扫：
1. 开局真实发现并选择【承露盏】；
2. 开局初始道纹固定选【杀伐】（否则开局3点精力不足以在首战前同时学完
   再生、透支和三阶自创法术）；
3. 首个局外窗口强制按【再生】→【透支】学习；
4. 三者齐备后，通过 GameEngine 的 custom_spell 审核/批准流程学习【血溅五步】；
5. 之后用当前完整 _play 战斗流程测试七场 PvE。

运行：
  PYTHONPATH=. /tmp/linji-venv/bin/python sim/test_chenglu_blood_splash.py --runs 50 --workers 8
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.api import GameEngine
import sim.build_learner as build_learner
import tests.setup_support as setup_support
from sim.build_learner import _play
from sim.relic_winrate import BenchmarkAI

TARGET_RELIC = "承露盏"
INITIAL_DAOWEN = "杀伐"
ATTRS = {"blood_points": 7, "speed_points": 6, "mana_points": 12}
REGION = "龙心谷"
RESONANCE = "反转"
LEARN = ["再生", "透支"]
BLOOD_SPLASH = {
    "name": "血溅五步",
    "required_daowen": ["再生", "透支", "杀伐"],
    "trigger_condition": "失去生命后",
    # DSL 要求每一步显式写目标身份；“攻击者”就是本次受击的怪物。
    # 触发时法力≥2才走再生→杀伐→透支；法力不足时跳过杀伐，
    # 走再生→透支把法力循环起来。
    "effect_flow": "若自身 法力 大于等于 2 则 发动再生X于自身；发动杀伐X于攻击者；发动透支X于自身 否则 发动再生X于自身；发动透支X于自身→循环",
}
DEFAULT_OPPONENT = ROOT / "data" / "real_winners" / "winner_01.json"
_ORIGINAL_RESOLVE = setup_support.resolve_opening_relic
_ORIGINAL_CHOOSE = build_learner.choose_pre_battle


def _probe(seed: int, db_path: str) -> tuple[list[str], list[str]]:
    e = GameEngine(db_path=db_path, rng_seed=seed,
                   sealed_candidate_path=os.devnull, death_book_path=os.devnull)
    r = e.execute_action("setup_attributes", {"name": "贾凡", **ATTRS})
    if not r.get("success"):
        raise RuntimeError(r)
    relics = list(r["result"]["relic_choices"])
    if TARGET_RELIC not in relics:
        return relics, []
    r = e.execute_action("choose_discovered_relic", {"relic_name": TARGET_RELIC})
    if not r.get("success"):
        raise RuntimeError(r)
    return relics, list(e.state.pending_initial_daowen_choices)


def _select_seeds(runs: int, start: int, max_scan: int, probe_dir: Path) -> list[int]:
    out = []
    for seed in range(start, start + max_scan):
        _relics, daowen = _probe(seed, str(probe_dir / f"{seed}.db"))
        if INITIAL_DAOWEN in daowen:
            out.append(seed)
        if len(out) >= runs:
            return out
    return out


def _worker_init() -> None:
    setup_support.resolve_opening_relic = _ORIGINAL_RESOLVE
    build_learner.choose_pre_battle = _ORIGINAL_CHOOSE


def _run_one(task: tuple[int, str | None, str]) -> dict[str, Any]:
    seed, opponent_path, run_root = task

    def choose_relic(engine, prefer=None):
        return _ORIGINAL_RESOLVE(engine, prefer=TARGET_RELIC)

    # 只对首个局外窗口注入用户指定的两个道纹学习动作；动作本身仍经引擎校验。
    opening_plan = [
        ("学习", {"sub": "daowen", "tier": 1, "name": "再生"}),
        ("学习", {"sub": "daowen", "tier": 1, "name": "透支"}),
    ]

    def choose_opening_plan(engine, todo, battle_no, rng, policy):
        if opening_plan:
            action, params = opening_plan.pop(0)
            return action, params
        return _ORIGINAL_CHOOSE(engine, todo, battle_no, rng, policy)

    setup_support.resolve_opening_relic = choose_relic
    build_learner.choose_pre_battle = choose_opening_plan
    run_dir = Path(run_root) / f"{os.getpid()}_{seed}"
    run_dir.mkdir(parents=True, exist_ok=True)
    sealed = run_dir / "sealed.json"
    if opponent_path:
        shutil.copyfile(opponent_path, sealed)
    book = run_dir / "death_book.md"
    book.write_text("", encoding="utf-8")
    try:
        with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink):
            result = _play(
                INITIAL_DAOWEN,
                list(LEARN),
                REGION,
                seed=seed,
                battles=7,
                spend_shards=False,
                policy=None,
                resonance=RESONANCE,
                attrs=dict(ATTRS),
                spell_plan=[dict(BLOOD_SPLASH)],
                ai_cls=BenchmarkAI,
                # 终止归因需要引擎提供死亡上下文；否则“本场未清除”会被
                # 运行器误写成“死亡场次”。
                death_trace=True,
                lab_paths={
                    "db_path": str(run_dir / "engine.db"),
                    "sealed_path": str(sealed),
                    "death_book_path": str(book),
                },
            )
        valid = not bool(result.get("invalid"))
        pm = result.get("pm") or {}
        trace = result.get("death_trace") or {}
        death_observed = bool(trace.get("death_subtype"))
        sculpture_observed = (not death_observed
                              and any(str(item).endswith("雕塑")
                                      for item in trace.get("unused_items", [])))
        termination_battle = pm.get("battle") if pm else None
        termination_kind = ("death" if death_observed else
                            "sculpture" if sculpture_observed else
                            "nondeath_termination" if termination_battle is not None else "unknown")
        return {
            "seed": seed,
            "valid": valid,
            "invalid_reason": result.get("reason", "") if not valid else "",
            "cleared_battles": int(result.get("cleared", 0) or 0),
            "pve7_completed": valid and int(result.get("cleared", 0) or 0) >= 7,
            "full_win": valid and bool(result.get("won")),
            "final_duel_fought": bool(result.get("duel_fought")),
            "final_duel_won": bool(result.get("duel_won")),
            # death_battle 只记录确有死亡上下文的局；此前直接使用 pm.battle，
            # 会把40回合未清场但玩家仍存活的局误计为死亡。
            "death_battle": trace.get("battle") if death_observed else None,
            "death_subtype": trace.get("death_subtype", "") if death_observed else "",
            "death_source": trace.get("death_source", "") if death_observed else "",
            "termination_battle": termination_battle,
            "termination_kind": termination_kind,
            "sculpture_termination": sculpture_observed,
            "termination_primary": "sculpture" if sculpture_observed else trace.get("primary", ""),
            "killer": pm.get("killer", "") if pm else "",
            "spell_plan": "血溅五步",
        }
    except Exception as exc:
        return {
            "seed": seed, "valid": False,
            "invalid_reason": f"uncaught {type(exc).__name__}: {exc}",
            "cleared_battles": 0, "pve7_completed": False, "full_win": False,
            "final_duel_fought": False, "final_duel_won": False,
            "death_battle": None, "death_subtype": "", "death_source": "",
            "termination_battle": None, "termination_kind": "unknown",
            "sculpture_termination": False, "termination_primary": "",
            "killer": "", "spell_plan": "血溅五步",
        }
    finally:
        setup_support.resolve_opening_relic = _ORIGINAL_RESOLVE
        build_learner.choose_pre_battle = _ORIGINAL_CHOOSE
        for path in run_dir.glob("*"):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            run_dir.rmdir()
        except OSError:
            pass


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [r for r in rows if r["valid"]]
    invalid = [r for r in rows if not r["valid"]]
    n = len(valid)
    cleared = [r["cleared_battles"] for r in valid]
    deaths = [r for r in valid if r.get("death_battle") is not None]
    nondeath_terminations = [r for r in valid
                             if r.get("termination_kind") == "nondeath_termination"]
    sculptures = [r for r in valid if r.get("sculpture_termination")]
    duel = [r for r in valid if r["final_duel_fought"]]
    return {
        "requested_runs": len(rows),
        "valid_runs": n,
        "invalid_runs": len(invalid),
        "invalid_reasons": dict(Counter(r["invalid_reason"] for r in invalid)),
        "pve7_completed_runs": sum(r["pve7_completed"] for r in valid),
        "pve7_completion_rate": sum(r["pve7_completed"] for r in valid) / n if n else None,
        "full_win_runs": sum(r["full_win"] for r in valid),
        "full_win_rate": sum(r["full_win"] for r in valid) / n if n else None,
        "final_duel_fought_runs": len(duel),
        "final_duel_wins": sum(r["final_duel_won"] for r in duel),
        "average_cleared_battles": sum(cleared) / n if n else None,
        "average_survival_battles": sum(cleared) / n if n else None,
        "survival_distribution": dict(Counter(str(c) for c in cleared)),
        "termination_distribution": dict(Counter(r.get("termination_kind", "unknown")
                                                   for r in valid)),
        "termination_battle_distribution": dict(sorted(
            Counter(str(r["termination_battle"]) for r in valid
                    if r.get("termination_battle") is not None).items(),
            key=lambda item: int(item[0]))),
        "death_distribution": dict(sorted(Counter(str(r["death_battle"]) for r in deaths).items(),
                                           key=lambda item: int(item[0]))),
        "death_subtype_distribution": dict(Counter(r.get("death_subtype", "") for r in deaths)),
        "death_source_distribution": dict(Counter(r.get("death_source", "") for r in deaths)),
        "nondeath_termination_distribution": dict(Counter(
            r.get("termination_primary", "") for r in nondeath_terminations)),
        "deaths_observed": len(deaths),
        "sculpture_terminations_observed": len(sculptures),
        "nondeath_terminations_observed": len(nondeath_terminations),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=50)
    ap.add_argument("--start-seed", type=int, default=1)
    ap.add_argument("--max-scan", type=int, default=10000)
    ap.add_argument("--workers", type=int, default=max(1, min(8, os.cpu_count() or 1)))
    ap.add_argument("--opponent", default=str(DEFAULT_OPPONENT))
    ap.add_argument("--output", default=str(ROOT / "data" / "real_runs" / "chenglu_blood_splash_20260914.json"))
    args = ap.parse_args()
    opponent = Path(args.opponent) if args.opponent else None
    if opponent is not None and not opponent.exists():
        raise SystemExit(f"守擂者快照不存在: {opponent}")

    with tempfile.TemporaryDirectory(prefix="chenglu_probe_", dir="/tmp") as probe_tmp, \
            tempfile.TemporaryDirectory(prefix="chenglu_runs_", dir="/tmp") as run_tmp:
        seeds = _select_seeds(args.runs, args.start_seed, args.max_scan, Path(probe_tmp))
        if len(seeds) < args.runs:
            raise SystemExit(f"有效开局候选不足：{len(seeds)}/{args.runs}")
        tasks = [(seed, str(opponent) if opponent else None, run_tmp) for seed in seeds]
        ctx = mp.get_context("fork" if hasattr(os, "fork") else "spawn")
        with ctx.Pool(args.workers, initializer=_worker_init) as pool:
            rows = list(pool.imap_unordered(_run_one, tasks, chunksize=1))

    rows.sort(key=lambda r: r["seed"])
    result = {
        "meta": {
            "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "engine": "engine.api.GameEngine via sim.build_learner._play",
            "script": "sim/test_chenglu_blood_splash.py",
            "opponent_snapshot": str(opponent.relative_to(ROOT)) if opponent else None,
            "definition": "valid run; pve7=7场PvE完成；full_win=_play won=True（包含最终死斗）",
        },
        "fixed_config": {
            "relic": TARGET_RELIC, "attributes": ATTRS,
            "initial_daowen": INITIAL_DAOWEN, "learn": LEARN,
            "spell": BLOOD_SPLASH, "region": REGION,
            "resonance": RESONANCE,
            "ai": "BenchmarkAI = WinOnlyAI + each battle max one Seal activation",
            "spend_shards": False,
            "opening_plan": ["学习再生", "学习透支", "学习血溅五步"],
        },
        "summary": summarize(rows),
        "runs": rows,
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入 {out}")
    print(json.dumps(result["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
