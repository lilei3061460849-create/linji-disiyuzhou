#!/usr/bin/env python3
"""禁区清单与待办逐项探测(2026-08-26,只读生产代码,结果落 data/experiments/)。

覆盖 报告.md 禁区/待办中可用引擎直接验证的项:
  A. 血僵/尸霸 强度加样本(禁区:此前各 1 次实战样本)
  B. 乱葬岗组合补测:执念/纸人/哀嚎者/腐疫鼠(待办1)
  C. 玩家主动施放 勾魂/冥气/镇尸(待办2)
  D. 冥气削速限 机制复现(禁区:组合放大口径)
  E. 勾魂削法力 机制复现(禁区:压制口径)
"""
from __future__ import annotations
import json
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity
from engine.monsters import parse_monster_pool
from sim.build_learner import _resolve_monster_turn
import sim.build_learner as bl
from tests.setup_support import finish_initial_daowen

ROOT = Path(__file__).resolve().parents[1]
POOL = parse_monster_pool(ROOT / "副本索引.md")["乱葬岗"]


def _forced_engine(seed, monster_def, tmp="/tmp/zp.db"):
    e = GameEngine(db_path=tmp, rng_seed=seed,
                   sealed_candidate_path="/tmp/zp_sealed.json",
                   death_book_path="/tmp/zp_book.md")
    e.state.forced_monsters_next_battle = [dict(monster_def)]
    return e


def forced_battle(monster_name, seed, learn=("庇护", "再生", "封印", "再生")):
    """中期战力(4纹+修行成长)在乱葬岗第 1 场强制单挑指定怪,返回结果。

    口径:杀伐起手 + 学庇护/再生/封印 + 3 档 tier3 修行(碎片注入 120),
    模拟打到二阶时的成长面板;裸装口径已测(全灭,1回合),见首轮数据。
    """
    md = next(m for m in POOL if m["name"] == monster_name)
    e = _forced_engine(seed, md, tmp=f"/tmp/zp_{monster_name}_{seed}.db")
    e.execute_action("setup_attributes", {
        "name": "探测员", "blood_points": 6, "speed_points": 8, "mana_points": 11})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    from engine.ai_tactics import TacticalAI
    from sim.build_learner import start_battle_with_artifacts, start_round_with_artifacts
    e.state.shards = 120
    e.state.energy = 0
    seen = set()
    for dw in learn:
        if dw in seen or dw == "杀伐":
            continue
        seen.add(dw)
        e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": dw})
        e.state.energy = 0
    for _ in range(3):
        e.execute_action("pre_battle_action", {
            "sub_action": "修行", "tier": 3,
            "allocations": {"speed_points": 1, "mana_points": 2}})
        e.state.energy = 0
    bs, _ = start_battle_with_artifacts(e)
    if not bs.get("success"):
        return {"ok": False, "err": str(bs.get("error"))[:60]}
    ai = TacticalAI(e)
    rounds = 0
    while rounds < 40:
        if not e.state.player or not e.state.player.is_alive:
            return {"ok": True, "win": False, "rounds": rounds, "killer": monster_name}
        if not [x for x in e.state.enemies if x.is_alive]:
            return {"ok": True, "win": True, "rounds": rounds}
        rs, _ = start_round_with_artifacts(e)
        if not rs.get("success"):
            return {"ok": False, "err": "round_start:" + str(rs.get("error"))[:50]}
        ai.new_round()
        ai.take_turn()
        rounds += 1
        mp = _resolve_monster_turn(e)
        if not mp.get("success"):
            return {"ok": False, "err": "monster:" + str(mp.get("error"))[:50]}
        if mp.get("result", {}).get("player_dead"):
            return {"ok": True, "win": False, "rounds": rounds, "killer": monster_name}
        e.execute_action("round_end", {})
    return {"ok": True, "win": False, "rounds": rounds, "killer": "超时(计负)"}


def player_cast_probe():
    """待办2:玩家主动施放 勾魂/冥气/镇尸 是否可用、效果为何。"""
    out = {}
    e = GameEngine(db_path="/tmp/zp_cast.db", rng_seed=9,
                   sealed_candidate_path="/tmp/zp_sealed.json",
                   death_book_path="/tmp/zp_book.md")
    e.execute_action("setup_attributes", {
        "name": "探测员", "blood_points": 6, "speed_points": 8, "mana_points": 11})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.state.energy = 0
    enemy = Entity("试验傀儡", "怪物", blood_limit=200, current_hp=200,
                   attack_count=1, attack_power=5, mana_limit=20, current_mana=20,
                   speed_limit=10, current_speed=10)
    e.state.enemies.clear()
    e.state.enemies.append(enemy)
    from sim.build_learner import start_battle_with_artifacts, start_round_with_artifacts
    start_battle_with_artifacts(e)
    start_round_with_artifacts(e)
    p = e.state.player
    for name in ("勾魂", "冥气", "镇尸"):
        p.dao_wen[name] = DaoWenInstance(DaoWen(
            name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    for name, x in (("勾魂", 2), ("冥气", 3), ("镇尸", 2)):
        before = {"mana_limit": enemy.mana_limit, "speed_limit": enemy.speed_limit,
                  "atk_cnt": enemy.attack_count}
        refs = e.combat._combat_entity_refs()
        ref = next((k for k, v in refs.items() if v is enemy), "enemy:0")
        r = e.execute_action("use_daowen", {"daowen_name": name, "x": x,
                                            "target_ref": ref, "dodge": False,
                                            "blood_shadow": False})
        after = {"mana_limit": enemy.mana_limit, "speed_limit": enemy.speed_limit,
                 "atk_cnt": enemy.attack_count}
        out[name] = {"success": bool(r.get("success")),
                     "error": str(r.get("error", ""))[:60],
                     "summary": str((r.get("calculation") or {}).get("summary", ""))[:60],
                     "enemy_delta": {k: after[k] - before[k] for k in before}}
    return out


def main():
    report = {"date": "2026-08-26", "battles": {}, "player_casts": player_cast_probe()}
    for mname, seeds in (("血僵", 12), ("尸霸", 12), ("执念", 8), ("纸人", 8),
                         ("哀嚎者", 8), ("腐疫鼠", 8)):
        rows = [forced_battle(mname, 7000 + i * 17) for i in range(seeds)]
        ok = [r for r in rows if r.get("ok")]
        wins = sum(1 for r in ok if r.get("win"))
        avg_rounds = sum(r.get("rounds", 0) for r in ok) / max(1, len(ok))
        report["battles"][mname] = {
            "n": len(rows), "valid": len(ok), "wins": wins,
            "win_rate": round(wins / max(1, len(ok)), 3),
            "avg_rounds": round(avg_rounds, 1),
            "invalid_errs": [r.get("err") for r in rows if not r.get("ok")][:3],
        }
        print(f"{mname}: {wins}/{len(ok)} 胜 (均 {avg_rounds:.1f} 回合)", flush=True)
    out = ROOT / "data" / "experiments" / "zone_todo_probes_2026-08-26.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("已存", out)
    print("\n玩家施放:", json.dumps(report["player_casts"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
