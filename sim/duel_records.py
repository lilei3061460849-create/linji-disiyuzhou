#!/usr/bin/env python3
"""死斗详细过程记录器：把每一局死斗的逐手过程 + 双方终局账面写成 markdown（报告.md 数据源）。

口径（2026-08-31）：
  · 死斗一律走仓库自带 `sim/duel_diff_trace.py::traced_duel`（含面板不变量自检）；
    本器只解析它打印的 `[真实]` 行（预演世界的动作不会出现在这里）。
  · 终局账面（异变层数 / 累计恢复量 / 连续未出手·未使敌掉血回合 / `_death_ctx`）
    直接读引擎实体与死亡上下文，不做任何推算——四种「自因命零」的判据都出自这里。
  · 每局前 `random.seed(seed)`：对白渲染默认吃全局 random，不重置会漂移。

用法：
    python3 sim/duel_records.py                       # 全有序配对 × seed=1，分批输出 markdown
    python3 sim/duel_records.py --challenger 封印控制_23.json --defender 血厚耐打_130.json
    python3 sim/duel_records.py --reference            # 12 局参考死斗（seed=各自养成种子）
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim.duel_diff_trace as ddt   # noqa: E402

WINNER_DIR = ddt.WINNER_DIR
from engine.combat import MEDIOCRITY_ROUNDS as _MEDIOCRITY  # noqa: E402  凡庸线（引擎常量）
from engine.combat import CombatEngine as _CombatEngine  # noqa: E402

# 凡庸结算会把两个连续计数清零（engine/combat.py:5494-5495），事后读盘面永远是 0；
# 这里在 _apply_mediocrity 入口抓「活值 + 触发口径原文」，每局开跑前清空。
_LIVE: dict = {}
_ORIG_MEDIOCRITY = _CombatEngine._apply_mediocrity


def _mediocrity_hook(self, entity, why):
    _LIVE[id(entity)] = (getattr(entity, "no_action_rounds", 0),
                         getattr(entity, "no_damage_rounds", 0), why)
    return _ORIG_MEDIOCRITY(self, entity, why)


_CombatEngine._apply_mediocrity = _mediocrity_hook

_REAL = re.compile(r"^\[真实\] (\S+) (.*)$")
_DAOWEN = re.compile(r"'daowen_name': '([^']+)', 'x': (\d+)")
_RESONANCE = re.compile(r"'resonance_type': '([^']+)'")
_SIDE = re.compile(r"(挑战|守擂)(\S+?) hp=(-?\d+)/(\d+) 盾=(-?\d+) 法=(-?\d+)/(\d+) 速=(-?\d+)/(\d+) 出手=(\d+)/(\d+)")


def _parse_panel(line: str) -> dict:
    """把 traced_duel 的面板快照行解析成 {挑战|守擂: {...}}。"""
    out = {}
    for m in _SIDE.finditer(line):
        out[m.group(1)] = {"name": m.group(2), "hp": int(m.group(3)), "bl": int(m.group(4)),
                           "shield": int(m.group(5)), "mana": int(m.group(6)),
                           "ml": int(m.group(7)), "speed": int(m.group(8)),
                           "sl": int(m.group(9)), "used": int(m.group(10)),
                           "acts": int(m.group(11))}
    return out


def _duelists(engine):
    """死斗双方实体（挑战者=state.player，守擂=轮回者主将）。"""
    if engine is None:
        return []
    out = []
    if engine.state.player is not None:
        out.append(("挑战", engine.state.player))
    for e in engine.state.enemies:
        if e.entity_type == "轮回者":
            out.append(("守擂", e))
    return out


def _book(engine) -> dict:
    """终局账面：四种自因命零的判据全部来自引擎字段，不推算。"""
    out = {}
    for seat, ent in _duelists(engine):
        ctx = getattr(ent, "_death_ctx", None) or {}
        out[seat] = {
            "名字": ent.name, "hp": ent.current_hp, "血限": ent.blood_limit,
            "盾": ent.shield, "异变": getattr(ent, "mutation_count", 0),
            "累计回复": getattr(ent, "total_healed", 0),
            "癌变线": engine.combat.cancer_threshold_of(ent),
            "未出手回合": _LIVE.get(id(ent), (getattr(ent, "no_action_rounds", 0), 0, ""))[0],
            "未使敌掉血回合": _LIVE.get(id(ent), (0, getattr(ent, "no_damage_rounds", 0), ""))[1],
            "凡庸口径": _LIVE.get(id(ent), (0, 0, ""))[2],
            "死因subtype": ctx.get("subtype") or "",
            "死因source": ctx.get("source") or "",
            "死因actor": ctx.get("actor") or "",
            "存活": bool(ent.is_alive),
        }
    return out


def resolve(name: str) -> str:
    """档位名 → 快照路径；带分隔符的按路径原样用（淘汰赛的临时带伤快照）。"""
    return name if os.path.sep in name else os.path.join(WINNER_DIR, name)


def run_and_record(challenger: str, defender: str, seed: int,
                   return_engine: bool = False) -> dict:
    """跑一局并解析成结构化记录。`return_engine=True` 时附带引擎对象（仅供赛后序列化，勿落盘）。"""
    random.seed(seed)
    _LIVE.clear()  # 上一局的实体 id 可能被复用，必须逐局清空
    captured: dict = {}
    orig = ddt.run_duel_pvp

    def spy(e, *a, **kw):
        captured["e"] = e
        return orig(e, *a, **kw)

    ddt.run_duel_pvp = spy
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            verdict = ddt.traced_duel(resolve(challenger), resolve(defender), seed)
    finally:
        ddt.run_duel_pvp = orig
    engine = captured.get("e")

    rounds: list[list[str]] = []
    opening = {}
    prev: dict = {}
    lines_all = buf.getvalue().splitlines()
    for idx, raw in enumerate(lines_all):
        if raw.startswith("  开局:"):
            opening = _parse_panel(raw)
            prev = opening
            continue
        m = _REAL.match(raw)
        if not m:
            continue
        action, params = m.group(1), m.group(2)
        if action == "round_start":
            rounds.append([])
            continue
        if action not in ("use_daowen", "use_resonance"):
            continue
        # 紧随其后的两行是 - 前 / + 后 面板
        after = _parse_panel(lines_all[idx + 2]) if idx + 2 < len(lines_all) else {}
        seat = "守擂" if "'actor_ref': 'enemy:0'" in params else "挑战"
        dw = _DAOWEN.search(params)
        rs = _RESONANCE.search(params)
        label = (f"{dw.group(1)}X={dw.group(2)}" if dw
                 else f"残韵·{rs.group(1)}" if rs else action)
        foe = "挑战" if seat == "守擂" else "守擂"
        delta = ""
        if prev.get(foe) and after.get(foe):
            d_hp = after[foe]["hp"] - prev[foe]["hp"]
            d_sh = after[foe]["shield"] - prev[foe]["shield"]
            bits = []
            if d_hp:
                bits.append(f"血{d_hp:+d}")
            if d_sh:
                bits.append(f"盾{d_sh:+d}")
            delta = f"→{'/'.join(bits)}" if bits else ""
        mine = ""
        if prev.get(seat) and after.get(seat):
            d_mana = after[seat]["mana"] - prev[seat]["mana"]
            d_hp = after[seat]["hp"] - prev[seat]["hp"]
            bits = []
            if d_mana:
                bits.append(f"法{d_mana:+d}")
            if d_hp:
                bits.append(f"自血{d_hp:+d}")
            mine = f"（{'/'.join(bits)}）" if bits else ""
        if not rounds:
            rounds.append([])
        rounds[-1].append(f"{seat}{label}{delta}{mine}")
        prev = after or prev
    rec = {"challenger": challenger, "defender": defender, "seed": seed,
           "挑战档名": os.path.basename(challenger), "守擂档名": os.path.basename(defender),
           "开局": opening, "回合": rounds, "终局账面": _book(engine), "判定": verdict}
    if return_engine:
        rec["_engine"] = engine
    return rec


def render(rec: dict, index: int, batch_label: str) -> str:
    v = rec["判定"] or {}
    op = rec["开局"]
    book = rec["终局账面"]

    def _fmt(side):
        p = op.get(side) or {}
        # 名字取终局账面：开局快照打印在 _assign_duelist_names 之前，那时双方都还叫「贾凡」
        name = (book.get(side) or {}).get("名字") or p.get("name", "?")
        return (f"{side}{name} {p.get('hp', '?')}/{p.get('bl', '?')} "
                f"盾{p.get('shield', 0)} 法{p.get('mana', '?')}/{p.get('ml', '?')} "
                f"速{p.get('speed', '?')}/{p.get('sl', '?')} 出手{p.get('acts', '?')}")

    ch = rec.get("挑战档名", rec["challenger"])[:-5]
    df = rec.get("守擂档名", rec["defender"])[:-5]
    lines = [f"#### {batch_label}-{index:02d} ｜ {ch}（挑战席）"
             f" vs {df}（守擂席）｜ seed={rec['seed']}",
             f"- 开局：{_fmt('挑战')} ｜ {_fmt('守擂')}"]
    for i, acts in enumerate(rec["回合"], 1):
        if not acts:
            continue
        lines.append(f"- R{i}：" + " ｜ ".join(acts))
    for seat, b in rec["终局账面"].items():
        lines.append(
            f"- 终局{seat}（{b['名字']}）：血{b['hp']}/{b['血限']} 盾{b['盾']} "
            f"异变{b['异变']} 累计回复{b['累计回复']}/癌变线{b['癌变线']} "
            f"未出手{b['未出手回合']}回合 未使敌掉血{b['未使敌掉血回合']}回合"
            + (f"（凡庸触发口径：{b['凡庸口径']}；线={_MEDIOCRITY}回合）" if b.get("凡庸口径")
               else f"（未触发凡庸；线={_MEDIOCRITY}回合）")
            + ("" if b["存活"] else
               f" → 命零（subtype={b['死因subtype'] or 'hp_zero'}"
               f"{'，source=' + b['死因source'] if b['死因source'] else ''}"
               f"{'，actor=' + b['死因actor'] if b['死因actor'] else ''}）"))
    lines.append(f"- 判定：**{v.get('winner')}**，第{v.get('rounds')}回合 —— {v.get('reason')}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--challenger")
    ap.add_argument("--defender")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--reference", action="store_true", help="12 局参考死斗（seed=各自养成种子）")
    ap.add_argument("--batch-size", type=int, default=11, help="每批多少局（按挑战者分批）")
    ap.add_argument("--tag", default="B", help="批次标签前缀（矩阵=B，参考局=R）")
    args = ap.parse_args()

    files = sorted(f for f in os.listdir(WINNER_DIR) if f.endswith(".json"))
    if args.challenger and args.defender:
        pairs = [(args.challenger, args.defender, args.seed)]
    elif args.reference:
        pairs = []
        for fn in files:
            seed = json.load(open(os.path.join(WINNER_DIR, fn), encoding="utf-8"))["origin"]["seed"]
            opp = next(f for f in files if f != fn)
            pairs.append((fn, opp, seed))
    else:
        pairs = [(a, b, args.seed) for a in files for b in files if a != b]

    out: list[str] = []
    # 仓库自检（engine/document_validation.py）要求每个 .md 有一级标题：
    # 单独落盘的实录文件必须自带 H1，贴进报告时由报告自己的 H1 统领。
    out.append("# " + ("参考死斗实录（12 局 · seed=各自养成种子，本人坐挑战席）" if args.reference
                       else f"死斗实录（{len(pairs)} 局全有序配对 · seed={args.seed}）") + "\n")
    batch_no, in_batch = 0, 0
    for i, (a, b, seed) in enumerate(pairs, 1):
        if in_batch == 0:
            batch_no += 1
            out.append(f"\n## 死斗实录 · {args.tag}{batch_no}批（第{i}–{min(i + args.batch_size - 1, len(pairs))}局）\n")
        rec = run_and_record(a, b, seed)
        out.append(render(rec, i, f"{args.tag}{batch_no}"))
        out.append("")
        in_batch = (in_batch + 1) % args.batch_size
        print(f"[{i}/{len(pairs)}] {a} vs {b} seed={seed} → {rec['判定'].get('winner')}",
              file=sys.stderr)
    print("\n".join(out))


if __name__ == "__main__":
    main()
