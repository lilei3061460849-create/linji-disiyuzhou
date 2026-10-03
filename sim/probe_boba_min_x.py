#!/usr/bin/env python3
"""探针：验【波及】X下限=2（2026-10-03 用户令）。

回答两件事，全部走生产引擎实跑，不手算：
  1. 玩家侧：X=1 是否被拒（引擎 resolve 下限）、X=2 是否成立、两个目标是否都
     拿到【波及】标记；只有 1 个合法目标时提交 X=2 的报错文本长什么样。
  2. 怪物侧：扭曲都市【孢子母体】（面板带波及）的 prepare 是否按下限过滤——
     只有 1 个合法目标时不出波及选项、补上第 2 个目标后选项出现且带 min_x=2。

跑法（沙箱无 /tmp 持久化，注意先 mkdir）：
    python3 sim/probe_boba_min_x.py            # 人类可读
    python3 sim/probe_boba_min_x.py --json /tmp/verify/boba_min_x.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "sim")

from engine.daowen import DaoWenEngine          # noqa: E402
from engine.models import Entity                # noqa: E402
from engine.api import GameEngine               # noqa: E402
from tests.setup_support import begin_battle, begin_round, finish_initial_daowen  # noqa: E402


def _sandbox(tmp: Path) -> GameEngine:
    e = GameEngine(db_path=str(tmp / "d.db"), save_dir=str(tmp),
                   sealed_candidate_path=str(tmp / "x.json"), rng_seed=7)
    assert e.execute_action("setup_attributes", {
        "name": "甲", "blood_points": 11, "speed_points": 8, "mana_points": 6})["success"]
    assert finish_initial_daowen(e)["success"]
    assert e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})["success"]
    assert e.execute_action("setup_choose_region", {"region": "扭曲都市"})["success"]
    assert begin_battle(e)["success"]
    assert begin_round(e)["success"]
    e.state.relics = []
    p = e.state.player
    p.blood_limit, p.current_hp = 400, 200
    p.mana_limit, p.current_mana = 60, 55
    p.speed_limit, p.current_speed = 5, 5
    p.shield = 0
    en = e.state.enemies[0]
    en.blood_limit, en.current_hp = 3000, 3000
    en.speed_limit, en.current_speed = 3, 3
    en.mana_limit, en.current_mana = 20, 20
    en.shield = 0
    en.dao_wen = {}
    e._grant_named_daowen(p, "波及")
    return e


def _add_friend(e: GameEngine, name: str = "乙") -> Entity:
    f = Entity(name=name, entity_type="朋友", blood_limit=54, current_hp=54,
               mana_limit=10, current_mana=10, speed_limit=2, current_speed=2)
    f.is_alive = True
    e.state.friends.append(f)
    return f


def _cast(e: GameEngine, x: int, refs: list[str]) -> dict:
    params = {"actor_ref": "player:0", "daowen_name": "波及", "x": x,
              "target_ref": refs[0], "dodge": False, "blood_shadow": False,
              "trigger_spell_choices": {},
              "dodge_targets": [{"target_ref": r, "dodge": False, "blood_shadow": False}
                                for r in refs[:x]]}
    r = e.execute_action("use_daowen", params)
    return {"x": x, "targets": list(refs[:x]), "ok": bool(r.get("success")),
            "error": r.get("error", "")}


def _marks(e: GameEngine) -> dict:
    out = {}
    for ref, ent in e.combat._combat_entity_refs().items():
        if ent.has_status("波及"):
            out[ref] = ent.get_status_value("波及")
    return out


def _engine_level() -> dict:
    out = {}
    for x in (1, 2):
        try:
            calc = DaoWenEngine.resolve("波及", x)
            out[x] = {"ok": True, "cost": calc.get("cost"), "mark_targets": calc.get("mark_targets")}
        except ValueError as ex:
            out[x] = {"ok": False, "error": str(ex)}
    return {"x_min_table": dict(DaoWenEngine.X_MIN), "resolve": out}


def _player_level() -> dict:
    """两目标场景：1 个敌人 + 1 个朋友。"""
    with tempfile.TemporaryDirectory() as td:
        e = _sandbox(Path(td))
        _add_friend(e)
        before_no_friend = None
        r1 = _cast(e, 1, ["enemy:0", "friend:0"])
        marks_after_1 = _marks(e)
        r2 = _cast(e, 2, ["enemy:0", "friend:0"])
        marks_after_2 = _marks(e)
    with tempfile.TemporaryDirectory() as td:
        e = _sandbox(Path(td))           # 只有 1 个合法目标（无朋友）
        before_no_friend = _cast(e, 2, ["enemy:0"])
    return {"x1": r1, "x2": r2,
            "marks_after_x1": marks_after_1, "marks_after_x2": marks_after_2,
            "x2_with_single_target": before_no_friend}


def _monster_level() -> dict:
    """孢子母体（面板含波及）的 prepare 选项：目标不足下限 → 过滤；够下限 → 出现且带 min_x。

    用 FFA 擂台（monster_arena）搭台：2 席时每席只有 1 个对手（<下限2）；
    3 席时每席有 2 个对手（=下限2）。
    """
    import monster_arena as A
    from engine.monsters import make_monster_entity

    pool = {d["name"]: d for d in A.monster_pool()}
    spore = dict(pool["孢子母体"])

    def _boba_opts(prep):
        opts = []
        if prep.get("success"):
            for actor in prep["result"].get("actors", []):
                for o in actor.get("daowen_options", []):
                    if o.get("name") == "波及":
                        opts.append({"actor": actor.get("actor_ref"), "name": o.get("name"),
                                     "x": o.get("x"), "x_free": o.get("x_free"),
                                     "max_x": o.get("max_x"), "min_x": o.get("min_x"),
                                     "wave_effective_x": o.get("wave_effective_x"),
                                     "dodge_target_options": len(o.get("dodge_target_options") or [])})
        return opts

    out = {}
    # 情形 A：普通战斗沙盒（玩家+1敌），把敌人换成孢子母体 → 该怪只有玩家1个合法目标 → 应过滤。
    with tempfile.TemporaryDirectory() as td:
        e = _sandbox(Path(td))
        e.state.enemies = [make_monster_entity(dict(spore))]
        prep = e.execute_action("prepare_monster_phase", {})
        refs = sorted(e.combat._combat_entity_refs())
        out["单目标（只有玩家可波及）"] = {"prepare_ok": bool(prep.get("success")),
                                          "refs": refs, "boba_options": _boba_opts(prep),
                                          "error": prep.get("error", "")}

    # 情形 B：FFA 两席（孢子母体 + 1 怪；观众位列 refs 里可被波及）→ 出现且 min_x=2。
    e = A.build_arena([dict(spore), dict(next(d for n, d in pool.items() if n != "孢子母体"))],
                      seed=0, free_actions=True)
    prep = e.execute_action("prepare_monster_phase", {})
    out["FFA两席"] = {"prepare_ok": bool(prep.get("success")),
                      "refs": sorted(e.combat._combat_entity_refs()),
                      "boba_options": _boba_opts(prep), "error": prep.get("error", "")}

    # 情形 C：FFA 三席（孢子母体 + 2 怪）→ 出现，min_x=2，候选 X 全 ≥2。
    e = A.build_arena([dict(spore)] + [dict(d) for n, d in list(pool.items())
                                       if n != "孢子母体"][:2], seed=0, free_actions=True)
    prep = e.execute_action("prepare_monster_phase", {})
    out["FFA三席"] = {"prepare_ok": bool(prep.get("success")),
                      "refs": sorted(e.combat._combat_entity_refs()),
                      "boba_options": _boba_opts(prep), "error": prep.get("error", "")}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    report = {"engine": _engine_level(), "player": _player_level(), "monster": _monster_level()}

    print("== 1. 引擎层：X_MIN 表与 resolve ==")
    print("   X_MIN:", report["engine"]["x_min_table"])
    for x, r in report["engine"]["resolve"].items():
        print(f"   resolve(波及, X={x}) → " + (f"ok 代价={r['cost']} 标记数={r['mark_targets']}"
                                              if r["ok"] else f"拒绝：{r['error']}"))
    print("\n== 2. 玩家侧（2 个合法目标：敌+友）==")
    p = report["player"]
    print(f"   X=1 → {'通过' if p['x1']['ok'] else '拒绝'}｜{p['x1']['error']}")
    print(f"      X=1 后标记表: {p['marks_after_x1']}")
    print(f"   X=2 → {'通过' if p['x2']['ok'] else '拒绝'}｜{p['x2']['error']}")
    print(f"      X=2 后标记表: {p['marks_after_x2']}")
    print(f"   只有 1 个合法目标时提交 X=2 → {'通过' if p['x2_with_single_target']['ok'] else '拒绝'}"
          f"｜{p['x2_with_single_target']['error']}")
    print("\n== 3. 怪物侧（孢子母体 prepare）==")
    for label, m in report["monster"].items():
        print(f"   {label}: prepare={'ok' if m['prepare_ok'] else '失败'}｜波及选项={m['boba_options'] or '未提供'}")
        if not m["prepare_ok"]:
            print(f"      error={m['error']}")

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
