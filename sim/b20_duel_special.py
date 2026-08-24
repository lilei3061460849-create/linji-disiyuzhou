#!/usr/bin/env python3
"""第二十批：死斗专项（DM 第八阶段）——独立资产包持久化。

复用 sim/b19_duel_lab 的受控生态机制（warmup 攒真实擂主队列 → eval 同生态对照
→ 双生态 Wilson 下界迁移门），新增：把 DM 要求的四项独立资产落库——
  data/duel_lab/duel_elite_library.json   死斗精英库（迁移门通过者；可为空+未证实）
  data/duel_lab/duel_review.md           死斗复盘（胜率/CI/先后手/构筑/胜败因/回合）
  data/duel_lab/duel_holdout.json        死斗 holdout 证据（生态B复测）
  data/duel_lab/migration_gate.md        死斗迁移门规则与本次判定

机制事实（b19 已代码实证，本次沿用）：结构先手恒=挑战者（玩家相位先动）；
挑战者只能击杀取胜；超时判擂主卫冕；PVE 均分不得外推死斗。
用法: python3 sim/b20_duel_special.py [--skip-run]  # skip-run=只汇总已有json
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSET = ROOT / "data" / "duel_lab"
RAW = {"ecoA": ROOT / "data" / "experiments" / "b20_raw" / "b20_duel_eco_A.json",
       "evalA": ROOT / "data" / "experiments" / "b20_raw" / "b20_duel_eval_A.json",
       "ecoB": ROOT / "data" / "experiments" / "b20_raw" / "b20_duel_eco_B.json",
       "evalB": ROOT / "data" / "experiments" / "b20_raw" / "b20_duel_eval_B.json"}
SEED_A, SEED_B = 91001, 91002
GATE = 0.35  # 死斗迁移门：双生态 Wilson 下界均 ≥0.35 才准入精英库（b19 线，PVE基线≈14%×2.5）


def run(cmd: list) -> None:
    print("+", " ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        sys.exit(f"FAILED: {cmd}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=300)
    ap.add_argument("--min-duels", type=int, default=20)
    ap.add_argument("--skip-run", action="store_true")
    a = ap.parse_args()
    (ROOT / "data" / "experiments" / "b20_raw").mkdir(parents=True, exist_ok=True)
    ASSET.mkdir(parents=True, exist_ok=True)
    lab = ROOT / "sim" / "b19_duel_lab.py"

    if not a.skip_run:
        run([sys.executable, str(lab), "--phase", "warmup", "--seed", str(SEED_A),
             "--games", str(a.games), "--eco", str(RAW["ecoA"])])
        run([sys.executable, str(lab), "--phase", "warmup", "--seed", str(SEED_B),
             "--games", str(a.games), "--eco", str(RAW["ecoB"])])
        run([sys.executable, str(lab), "--phase", "eval", "--seed", str(SEED_A),
             "--games", str(a.games), "--eco", str(RAW["ecoA"]),
             "--out", str(RAW["evalA"]), "--min-duels", str(a.min_duels)])
        run([sys.executable, str(lab), "--phase", "eval", "--seed", str(SEED_B),
             "--games", str(a.games), "--eco", str(RAW["ecoB"]),
             "--out", str(RAW["evalB"]), "--min-duels", str(a.min_duels)])

    ea = json.loads(RAW["evalA"].read_text(encoding="utf-8"))
    eb = json.loads(RAW["evalB"].read_text(encoding="utf-8"))
    rows_b = {"+".join(map(str, r["build"])): r for r in eb["rows"]}
    elite, review_rows = [], []
    for r in ea["rows"]:
        name = "+".join(map(str, r["build"]))
        rb = rows_b.get(name)
        gate_ok = (r["wilson"][0] >= GATE and rb and rb["wilson"][0] >= GATE)
        entry = {"build": r["build"], "ecoA": {"duels": r["duels"], "won": r["won"],
                 "wr": r["wr"], "wilson": r["wilson"], "t/o负": r["lost_timeout"],
                 "被杀负": r["lost_kill"], "均回合": r["rounds_avg"], "对手": r["opp_hist"]},
                 "ecoB": ({"duels": rb["duels"], "won": rb["won"], "wr": rb["wr"],
                           "wilson": rb["wilson"], "t/o负": rb["lost_timeout"],
                           "被杀负": rb["lost_kill"], "均回合": rb["rounds_avg"]}
                          if rb else None),
                 "gate_pass": bool(gate_ok)}
        review_rows.append(entry)
        if gate_ok:
            elite.append(entry)

    verdict = ("未证实" if not elite else "已证实")
    (ASSET / "duel_elite_library.json").write_text(json.dumps(
        {"gate": f"双生态 Wilson 下界均≥{GATE}", "verdict": verdict,
         "elite": elite, "note": "空库=尚无构筑满足死斗迁移门；不得用PVE均分代替"},
        ensure_ascii=False, indent=1), encoding="utf-8")
    (ASSET / "duel_holdout.json").write_text(json.dumps(
        {"holdout_ecology": {"seed": SEED_B, "eco_builds": eb.get("eco_builds")},
         "rows_B": [v for v in rows_b.values()]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"# 死斗迁移门（DM 第八阶段资产）\n",
             f"- 门槛：候选构筑在生态A与生态B（holdout，种子{SEED_B}）上的死斗胜率 Wilson 95% 下界均 ≥ {GATE}。",
             "- PVE 均分/bc 一律不作死斗准入证据（PVE 与死斗分开评价）。",
             f"- 本次判定：**{verdict}**——{'无构筑过门' if not elite else '见精英库'}。\n"]
    (ASSET / "migration_gate.md").write_text("\n".join(lines), encoding="utf-8")

    rv = ["# 死斗复盘（b20，生态A/B 双测）\n",
          "- 结构：**先手恒=挑战者**（玩家相位先动）；挑战者胜因只能=击杀；",
          "  败因=被击杀 或 超时（判擂主卫冕）。资源使用粒度引擎未采集（诚实声明）。",
          f"- 生态A擂主阵容：{ea.get('eco_builds')}",
          f"- 生态B(holdout)擂主阵容：{eb.get('eco_builds')}\n",
          "| 构筑 | A 胜率(Wilson) | A 场 | B 胜率(Wilson) | B 场 | 过门 |",
          "|---|---|---|---|---|---|"]
    for e in review_rows:
        b = e["ecoB"] or {"wr": None, "wilson": [None, None], "duels": 0, "won": 0}
        rv.append("| {} | {}/{}={} {} | {}duels | {}/{}={} {} | {}duels | {} |".format(
            "+".join(map(str, e["build"])), e["ecoA"]["won"], e["ecoA"]["duels"],
            e["ecoA"]["wr"], e["ecoA"]["wilson"], e["ecoA"]["duels"],
            b["won"], b["duels"], b["wr"], b["wilson"], b["duels"],
            "✓" if e["gate_pass"] else "✗"))
    rv.append(f"\n**结论：{verdict}**（精英库 {'空' if not elite else '见库'}）")
    (ASSET / "duel_review.md").write_text("\n".join(rv), encoding="utf-8")
    print(f"verdict={verdict} elite={len(elite)}")
    print("assets ->", ASSET)


if __name__ == "__main__":
    main()
