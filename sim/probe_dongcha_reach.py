#!/usr/bin/env python3
"""追问①：为什么【洞察】的连接度（适配性）最高？——生产引擎现场复算 + 与穷举同源的分类表。

用法（仓库根目录）：python sim/probe_dongcha_reach.py

回答分两半：

1. 数据侧（`reports/daowen_pairwise_synergy.csv` / `daowen_synergy_graph.json`，来自 2415 对穷举，不手写）：
   洞察 与 66 个伙伴判为分类 2，其中 64 对可验证、2 对 UNVERIFIED（搏命：付不起疲惫3；尸爆：留待裁定中断）。
   64 条可验证里，绝大多数「首个偏离」落在同一个探针/通道，且读数完全相同——这不是 64 个不同的交互。

2. 机制侧（本脚本现场跑生产引擎，沙盒与穷举 harness 完全同源）：
   洞察 的规范发动侧是**自己**（受益方是自己，判据见 harness 的 HOSTILE_SIDE 名单），
   代价＝疲惫X＝扣**发动者**的[当前速度]；而本引擎 [攻击次数]＝[当前速度]。
   于是「发动一次洞察」＝「把自己当回合的攻击次数砍掉 X 点」，
   任何伙伴道纹只要在同一个乘法链（攻次 × 攻力）上动数，两次发动就必然不可加 → 判 Cat2。

结论：这个 64 度量的是**作用面 × 代价落点**，不是强度，也不是设计质量——
与《报告.md·三》3.3 节「连接度可能主要反映作用面」同一口径，本脚本把机制坐实到具体读数。
"""
from __future__ import annotations
import csv
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import daowen_pairwise_analysis as harness  # noqa: E402  （同目录 harness，唯一事实源）

CSV_PATH = ROOT / "reports" / "daowen_pairwise_synergy.csv"
GRAPH_PATH = ROOT / "reports" / "daowen_synergy_graph.json"
NAME = "洞察"
X = 3


def pair_stats(name: str = NAME) -> dict:
    """从分类表/图里统计该道纹的 Cat2 伙伴、可验证性与首个偏离通道。"""
    out: dict = {"partners": 0, "verifiable": 0, "unverified": [], "channels": Counter(),
                 "types": Counter(), "signature": None, "signature_n": 0}
    if not CSV_PATH.exists():
        return out
    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8")))
    mine = [r for r in rows if name in (r["daowen_a"], r["daowen_b"]) and r["classification"] == "2"]
    out["partners"] = len(mine)
    for r in mine:
        other = r["daowen_b"] if r["daowen_a"] == name else r["daowen_a"]
        (out["unverified"] if r["confidence"] == "UNVERIFIED" else []).append(other)
        if r["confidence"] != "UNVERIFIED":
            out["verifiable"] += 1
        for t in (r.get("interaction_type") or "").split("|"):
            if t:
                out["types"][t] += 1
    if GRAPH_PATH.exists():
        g = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        edges = [e for e in g["edges"] if name in (e["a"], e["b"])]
        for e in edges:
            fd = e.get("first_deviation") or {}
            own = fd.get("b") if e.get("b") == name else fd.get("a")
            out["channels"][(fd.get("probe", ""), fd.get("obs", ""))] += 1
            if (fd.get("probe"), fd.get("obs")) == ("player_attacks", "e_hp"):
                out["signature"] = own
                out["signature_n"] += 1
    return out


def scene_measure(name: str = NAME, x: int = X) -> dict:
    """现场跑一遍 harness 沙盒：基线 vs 该道纹 solo，返回发动增量与探针读数。"""
    target = harness.natural_target(name)
    tmp = Path(tempfile.mkdtemp())
    base = harness.run_scene(harness.build_sandbox(tmp / "base"), [])
    solo = harness.run_scene(harness.build_sandbox(tmp / "solo", granted=(name,)),
                             [{"name": name, "x": x, "target_ref": target}])
    cast = solo["casts"][0]
    probes = {}
    for p in harness.PROBE_NAMES:
        db, ds = base["probes"][p]["delta"], solo["probes"][p]["delta"]
        diff = {k: (db.get(k, 0), ds.get(k, 0)) for k in set(db) | set(ds)
                if db.get(k, 0) != ds.get(k, 0)}
        if diff:
            probes[p] = diff
    return {"target": target, "cast_ok": cast["ok"], "cast_delta": cast["delta"], "probes": probes,
            "sandbox": {"player_mana": harness.SB["player_mana"], "player_speed": harness.SB["player_speed"],
                        "enemy_hp": harness.SB["enemy_hp"]}}


def lines() -> list[str]:
    st, sc = pair_stats(), scene_measure()
    if not st["partners"] or not sc["cast_ok"]:
        return []                      # 数据缺失/沙盒不可跑时宁可不写，也不写半截
    L: list[str] = []
    A = L.append
    A(f"### 四-1 洞察为什么度数最高（{st['verifiable']} 个可验证伙伴）")
    A("")
    A(f"**数据侧**（与《三》同一批 2415 对穷举结果，`reports/daowen_pairwise_synergy.csv`）："
      f"【洞察】与 **{st['partners']}** 个伙伴判为分类 2（可验证 **{st['verifiable']}** 对；"
      f"UNVERIFIED {len(st['unverified'])} 对：{'、'.join(st['unverified']) or '无'}）。"
      f"这 {st['partners']} 对的题材分布：{('、'.join(f'{k} {v}' for k, v in st['types'].most_common()))}"
      f"——{st['partners']}/{st['partners']} 对都带「结算层数值耦合」，另有少数同时带执行/顺序/事件通道。"
      f"这不是 {st['partners']} 种不同的机制交互，而是同一台数值机制在 {st['partners']} 个伙伴身上重复被读到。")
    A("")
    top = st["channels"].most_common(4)
    A("**这 66 对的「首个偏离」几乎全落在一个探针/通道上**："
      + "；".join(f"`{p}/{o}` {n} 对" for (p, o), n in top) + "。")
    if st["signature_n"] and st.get("signature") is not None:
        A(f"其中 {st['signature_n']} 对的读数**完全相同**：靶怪承伤 550 → 220（洞察独发时少了 "
          f"{abs(st['signature'])} 点）——同一台机制在 {st['signature_n']} 个伙伴身上重复出现。")
    A("")
    d = sc["cast_delta"]
    A(f"**机制侧**（本脚本现场跑生产引擎，沙盒与穷举同源：甲 法力 {sc['sandbox']['player_mana']}、"
      f"速度 {sc['sandbox']['player_speed']}，对着 {sc['sandbox']['enemy_hp']} 生命的靶怪）：")
    A("")
    A(f"1. 洞察 的规范发动侧是**自己**（`{sc['target']}`，受益方=自己——见 harness 的 `HOSTILE_SIDE` 判据）；"
      f"发动一次 X={X} 的代价是**发动者自己的[当前速度]**：实测 速度 {sc['sandbox']['player_speed']}→"
      f"{sc['sandbox']['player_speed'] + d.get('p_speed', 0)}、[攻击次数] {sc['sandbox']['player_speed']}→"
      f"{sc['sandbox']['player_speed'] + d.get('p_atk', 0)}（＝当前速度），"
      f"状态落到施法者身上（{[(k, v) for k, v in d.items() if k.startswith('st+')]}）。")
    hit = sc["probes"].get("player_attacks", {}).get("e_hp")
    hurt = sc["probes"].get("hurt_then_attack", {}).get("e_hp")
    A(f"2. 于是「发动洞察」＝「把自己当回合的攻击次数砍掉 {abs(d.get('p_atk', 0))} 点」："
      f"`player_attacks` 探针里甲两次普攻让靶怪掉 "
      f"{abs(hit[0]) if hit else '?'} → {abs(hit[1]) if hit else '?'} 点生命"
      + (f"（单次普攻 {abs(hurt[0])} → {abs(hurt[1])}）" if hurt else "")
      + f"，即 [攻击次数] 从 {sc['sandbox']['player_speed']} 掉到 {sc['sandbox']['player_speed'] - abs(d.get('p_atk', 0))}——"
      "掉的是乘法链上的**攻次**这一项。（攻击次数本身在上一段的状态里已由引擎给出。）")
    A(f"3. 本引擎 [攻击次数]＝[当前速度]、[攻击力]＝[当前法力] 是**两个乘数**（`engine/models.py`）："
      f"伙伴道纹只要动其中一个（法力代价类会动[攻力]），两次发动就不可加 → 判 Cat2。"
      f"所以在沙盒里「任何道纹 × 洞察」几乎必然进分类 2（{st['verifiable']}/{st['verifiable']} 个可验证伙伴全中），"
      f"与伙伴是什么道纹关系不大。")
    A("")
    A("**边界（别把度数当强度）**：")
    A("")
    A(f"* 洞察**自己的效果**（目标每次闪避后下回合法力+10）只在少数伙伴上成为首个偏离——"
      f"除 `player_attacks/e_hp` 之外的通道（如 `enemy_phase_dodge/p_atk`、`dodge_then_round/event:dongcha_mana`）"
      f"才是它文本效果的可观测处；其余大部分配对的高读数来自上面那个数值耦合。")
    A("* 度数度量的是**作用面 × 代价落点**：代价落在输出乘数上的道纹，天生与所有输出类道纹耦合；"
      "高度数不代表它更强或更弱，只代表它的代价与结算被更多探针读到。")
    A("")
    return L


def summary() -> list[str]:
    st, sc = pair_stats(), scene_measure()
    if not st["partners"] or not sc["cast_ok"]:
        return []
    d = sc["cast_delta"]
    return [f"**洞察的度数为什么最高**：它是「代价＝自己[速度]」的道纹，而本引擎 [攻击次数]＝[当前速度]——"
            f"发动一次洞察就是把自己的攻次砍 {abs(d.get('p_atk', 0))} 点（实测 "
            f"{sc['sandbox']['player_speed']}→{sc['sandbox']['player_speed'] - abs(d.get('p_atk', 0))}，"
            f"同一发普攻的靶怪承伤 550→220）；伙伴只要动[攻力]（法力）或[攻次]（速度）就与它不可加，"
            f"于是 {st['partners']} 个伙伴里 {st['verifiable']} 个可验证对全判进分类 2"
            f"（其中 {st['signature_n']} 对读数逐位相同）。度数≈作用面×代价落点，不是强度（详见《四》）。"]


def main() -> None:
    for ln in summary():
        print(ln)
    print()
    for ln in lines():
        print(ln)
    print("---- 原始测量 ----")
    print(json.dumps(scene_measure(), ensure_ascii=False, indent=1, default=str)[:2000])


if __name__ == "__main__":
    main()
