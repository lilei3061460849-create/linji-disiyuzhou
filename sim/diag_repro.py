#!/usr/bin/env python3
"""同 seed 多次 `play()` 的逐层复现性诊断（2026-09-18 沉淀，来自实战定位）。

**用途**：出现「同一 seed＋同一决策 rng，两次跑结果不同」时（典型受害者＝
`tests/test_build_learner.py::test_fixed_seed_is_reproducible`），一条命令定位
**第一层分歧**，不必再手写一次性插桩脚本。

分层（从外到内，越深越接近根因）：

  L1 result   —— `play()` 的返回字典（症状层：哪个键不同）
  L2 actions  —— 真实 `execute_action` 流（action_type＋关键参数＋success＋error＋回合）
  L3 picks    —— 怪物战术选 X（`pick_monster_daowen_x` 的档位上限与选出的 X）
  L4 previews —— 每档预演的提交与结果（success/error）
  L5 scores   —— `TacticalAI._score_candidate` 的 (label, score)
  L6 rejects  —— 引擎侧「不能发动道纹」类拒绝，附拒绝瞬间的账本内容（键＝runtime_id）
  L7 ledgers  —— 收工审计：全部实体键账本（键＝runtime_id，清单取自 engine/ledger_isolation.py）
                 里的**垃圾键**（键不对应任何在场实体）

**L7 是这一类 bug 的指纹**：预演走副本执行（deepcopy state）时若复用真实
`CombatEngine` 的 `_monster_daowen_round_used`／`_monster_activated`／
`_resonance_rewrites`（当时都按 `id(entity)` 建索引），副本实体的 id 就会写进真实账本；
副本被回收后地址复用，后来的真实怪/新副本**继承**这条记录 → 明明没发动过却报
「不能发动道纹【X】」，而且是否命中取决于内存分配顺序 → 不可复现。
2026-09-18 修的 `engine/ai_preview.py::preview_sequence` 正是这个（修法＝进副本前
deepcopy 隔离账本、finally 归还）。**跑完若 L7 报垃圾键 > 0，说明又有一条副本执行
路径没做隔离**，按 L6 的账本快照去查是谁写的。

跑法（一局约 20 秒，默认两次约 40 秒）：

    PYTHONPATH=. .venv/bin/python sim/diag_repro.py
    PYTHONPATH=. .venv/bin/python sim/diag_repro.py --runs 3 --region 扭曲都市 --seed 7
    PYTHONPATH=. .venv/bin/python sim/diag_repro.py --layer actions --context 8
    PYTHONPATH=. .venv/bin/python sim/diag_repro.py --quick      # 只装 L1/L2/L7 钩子
    PYTHONPATH=. .venv/bin/python sim/diag_repro.py --dump /tmp/repro

本工具**只读不改**引擎：全部靠 monkeypatch 记录，跑完还原。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 账本清单不在本文件定义：取自 engine/ledger_isolation.py（单一权威，预演/死斗推演同源）
from engine.ledger_isolation import ENTITY_KEYED_LEDGERS  # noqa: E402

HEX32 = re.compile(r"[0-9a-f]{32}")


def _norm(obj) -> str:
    """把日志项压成可比较字符串；token/uuid 归一化（它们本来就是每次随机的）。"""
    return HEX32.sub("TOK", json.dumps(obj, ensure_ascii=False, default=str))


# --------------------------------------------------------------------------
# 钩子安装
# --------------------------------------------------------------------------

def install_hooks(rec: dict, deep: bool) -> list:
    """给各层装记录钩子；返回还原用的 (obj, attr, old) 列表。"""
    undo = []

    def patch(obj, attr, wrapper):
        old = getattr(obj, attr)
        undo.append((obj, attr, old))
        setattr(obj, attr, wrapper(old))

    from engine.api import GameEngine

    def _ea(old):
        def ea(self, action_type, params=None, **kw):
            r = old(self, action_type, params or {}, **kw)
            p = params or {}
            if action_type == "resolve_monster_phase":
                sig = [(c.get("actor_ref"), (c.get("daowen") or {}).get("name"),
                        (c.get("daowen") or {}).get("x"), len(c.get("attack_actions") or []),
                        c.get("attack_first", False))
                       for c in p.get("choices", [])]
            elif action_type == "use_daowen":
                sig = [p.get("daowen_name"), p.get("x"), p.get("target")]
            else:
                sig = str(p)[:80]
            rec["actions"].append((action_type, _norm(sig), bool(r.get("success")),
                                   self.state.current_round,
                                   _norm(str(r.get("error") or ""))[:80]))
            return r
        return ea
    patch(GameEngine, "execute_action", _ea)

    from engine.combat import CombatEngine

    def _live_entities(engine):
        st = engine.state
        out = []
        for attr in ("player",):
            e = getattr(st, attr, None)
            if e is not None:
                out.append(e)
        for attr in ("friends", "employees", "temp_friends", "enemies"):
            out.extend(getattr(st, attr, []) or [])
        return out

    for fname in ("_validate_monster_daowen_schema", "_resolve_monster_daowen_choice"):
        def _mk(old, _fname=fname):
            def f(self, monster, choice, *a, **kw):
                try:
                    return old(self, monster, choice, *a, **kw)
                except ValueError as ex:
                    if "不能发动道纹" in str(ex):
                        name = choice.get("name", "")
                        inst = monster.dao_wen.get(name)
                        rec["rejects"].append({
                            "fn": _fname, "monster": monster.name, "daowen": name,
                            "round": self.state.current_round,
                            "in_round_used": name in self._monster_round_used(monster),
                            "can_use": inst.can_use() if inst else None,
                            "cooldown": getattr(inst, "cooldown_remaining", None),
                            "ledgers": {k: _norm(v) for k, v in
                                        ((lk, getattr(self, lk, None)) for lk in ENTITY_KEYED_LEDGERS)},
                        })
                    raise
            return f
        patch(CombatEngine, fname, _mk)

    if deep:
        import sim.monster_targets as mt

        def _pick(old):
            def pick(engine, monster, option, choice_tpl, token, all_choices=None):
                x = old(engine, monster, option, choice_tpl, token, all_choices=all_choices)
                rec["picks"].append((monster.name, option.get("name"),
                                     option.get("max_x") or option.get("x"), x))
                return x
            return pick
        patch(mt, "pick_monster_daowen_x", _pick)

        from engine.ai_preview import ActionPreview

        def _pv(old):
            def pv(self, action_type, params):
                xs = None
                if action_type == "resolve_monster_phase":
                    xs = tuple((c.get("daowen") or {}).get("x")
                               for c in (params or {}).get("choices", []))
                out = old(self, action_type, params)
                if xs is not None:
                    res = out.get("result") or {}
                    rec["previews"].append((xs, bool(res.get("success")),
                                            _norm(str(res.get("error") or ""))[:60]))
                return out
            return pv
        patch(ActionPreview, "preview", _pv)

        from engine.ai_tactics import TacticalAI

        def _sc(old):
            def sc(self, diff, label, kind=None, target=None):
                v = old(self, diff, label, kind=kind, target=target)
                rec["scores"].append((label, None if v is None else round(v, 6)))
                return v
            return sc
        patch(TacticalAI, "_score_candidate", _sc)

    # L7：登记本轮创建的每个 CombatEngine（强引用，诊断工具不在乎内存）
    def _ce(old):
        def ce(self, *a, **kw):
            old(self, *a, **kw)
            rec["engines"].append(self)
        return ce
    patch(CombatEngine, "__init__", _ce)
    return undo


def audit_ledgers(engines: list) -> dict:
    """L7：账本里有多少键不对应任何在场实体（＝副本执行留下的垃圾）。

    清单与判定口径都取自 `engine/ledger_isolation.py`（单一权威），本工具不再自己抄一份：
      junk_keys    —— 只数「键必须始终在场」的账本（_monster_activated／
                      _monster_daowen_round_used／_resonance_rewrites），**门禁**：>0 即不可复现；
      unbound_keys —— 键可以合法滞留的账本（_monster_evolved：怪进化/逃跑后 id 仍留存；
                      _dodge_counts：换回合才清），只作参考，不判失败。
    """
    from engine.ledger_isolation import audit_ledgers as audit_one

    junk = unbound = 0
    detail: dict = {}
    for eng in engines:
        if getattr(eng, "state", None) is None:
            continue
        a = audit_one(eng)
        junk += a["junk_keys"]
        unbound += a["unbound_keys"]
        for lname, n in a["detail"].items():
            detail[f"{id(eng) % 100000}:{lname}"] = n
    return {"junk_keys": junk, "unbound_keys": unbound, "detail": detail,
            "engines": len(engines)}


# --------------------------------------------------------------------------
# 跑与比
# --------------------------------------------------------------------------

def run_once(args, rec: dict) -> dict:
    import sim.build_learner as bl
    t = time.time()
    result = bl.play(args.starter, json.loads(args.learn), args.region, args.seed,
                     battles=args.battles, rng=random.Random(args.decision_seed))
    rec["elapsed"] = round(time.time() - t, 1)
    rec["ledger_audit"] = audit_ledgers(rec["engines"])
    return result


def first_diff(a: list, b: list) -> int:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return -1 if len(a) == len(b) else min(len(a), len(b))


def report(args, runs: list, results: list) -> int:
    ok = True
    print("=" * 78)
    print("L1 result（play() 返回）")
    base = results[0]
    for i, r in enumerate(results[1:], 1):
        if r == base:
            print(f"  run{i + 1} == run1 ✓")
            continue
        ok = False
        keys = sorted({k for k in set(base) | set(r) if base.get(k) != r.get(k)})
        print(f"  run{i + 1} != run1 ✗  不同的键: {keys}")
        for k in keys[: args.keys]:
            print(f"    - {k}: run1={_norm(base.get(k))[:160]}")
            print(f"      {' ' * len(k)}  run{i + 1}={_norm(r.get(k))[:160]}")

    layers = [("L2 actions", "actions"), ("L3 picks", "picks"), ("L4 previews", "previews"),
              ("L5 scores", "scores"), ("L6 rejects", "rejects")]
    for title, key in layers:
        if not any(r[key] for r in runs):
            continue
        print("=" * 78)
        counts = " ".join(f"run{i + 1}={len(r[key])}" for i, r in enumerate(runs))
        print(f"{title}  （{counts}）")
        for i, r in enumerate(runs[1:], 1):
            d = first_diff(runs[0][key], r[key])
            if d < 0:
                print(f"  run{i + 1} 与 run1 逐条一致 ✓")
                continue
            ok = False
            print(f"  run{i + 1} 与 run1 第一处分歧在 #{d} ✗")
            lo = max(0, d - args.context)
            for j in range(lo, min(len(runs[0][key]), d + args.context + 1)):
                mark = ">>" if j == d else "  "
                a = runs[0][key][j] if j < len(runs[0][key]) else None
                b = r[key][j] if j < len(r[key]) else None
                print(f"  {mark} #{j} run1  : {_norm(a)[:200]}")
                print(f"  {mark} #{j} run{i + 1}: {_norm(b)[:200]}")

    print("=" * 78)
    print("L7 ledgers（收工审计：runtime_id 键账本里的垃圾键）")
    for i, r in enumerate(runs):
        a = r["ledger_audit"]
        flag = "✓" if a["junk_keys"] == 0 else "✗ 有副本写入真实账本的残留"
        print(f"  run{i + 1}: engines={a['engines']} junk_keys={a['junk_keys']} "
              f"unbound_keys={a.get('unbound_keys', 0)}（参考，不判失败） {flag}"
              + (f"  detail={a['detail']}" if a["detail"] else ""))
        if a["junk_keys"]:
            ok = False
    print("=" * 78)
    print("结论：" + ("各层逐条一致，本 seed 可复现 ✓" if ok else "存在分歧，见上面第一个 ✗ 层"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="同 seed 多次 play() 的逐层复现性诊断")
    ap.add_argument("--runs", type=int, default=2, help="跑几次（默认 2）")
    ap.add_argument("--seed", type=int, default=42, help="引擎 seed")
    ap.add_argument("--decision-seed", type=int, default=9, help="决策 rng seed")
    ap.add_argument("--region", default="龙心谷")
    ap.add_argument("--starter", default="杀伐")
    ap.add_argument("--learn", default='["庇护", "再生"]', help="JSON 数组")
    ap.add_argument("--battles", type=int, default=7)
    ap.add_argument("--layer", default=None, help="只打印某一层的计数（actions/picks/previews/scores/rejects）")
    ap.add_argument("--context", type=int, default=3, help="分歧点前后各打印几条")
    ap.add_argument("--keys", type=int, default=6, help="L1 最多展开几个不同的键")
    ap.add_argument("--quick", action="store_true", help="只装 L1/L2/L6/L7 钩子（更快，看不到选X/预演/评分）")
    ap.add_argument("--dump", default=None, help="把每次跑的日志写成 JSON 到该目录")
    args = ap.parse_args()

    runs, results = [], []
    for i in range(args.runs):
        rec = {"actions": [], "picks": [], "previews": [], "scores": [], "rejects": [],
               "engines": []}
        undo = install_hooks(rec, deep=not args.quick)
        try:
            results.append(run_once(args, rec))
        finally:
            for obj, attr, old in reversed(undo):
                setattr(obj, attr, old)
        engines = rec.pop("engines")
        rec["ledger_audit"] = audit_ledgers(engines)
        runs.append(rec)
        print(f"[run{i + 1}] {rec['elapsed']}s  actions={len(rec['actions'])} "
              f"picks={len(rec['picks'])} previews={len(rec['previews'])} "
              f"scores={len(rec['scores'])} rejects={len(rec['rejects'])} "
              f"junk_keys={rec['ledger_audit']['junk_keys']}")
        if args.dump:
            d = Path(args.dump)
            d.mkdir(parents=True, exist_ok=True)
            payload = {k: v for k, v in rec.items()}
            payload["result"] = results[-1]
            (d / f"run{i + 1}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    if args.layer:
        for i, r in enumerate(runs):
            print(f"run{i + 1} {args.layer}={len(r.get(args.layer, []))}")
        return 0
    return report(args, runs, results)


if __name__ == "__main__":
    raise SystemExit(main())
