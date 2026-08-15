#!/usr/bin/env python3
"""多路径胜利策略完整7场挑战：用真实胜者+策略，看能否通关整个乱葬岗。

测试策略：毒奶(癌变) / 蒙蔽(凡庸) / 石化(雕塑) / 封印(移出) / 混合(封印+毒奶兜底)。
每场战前休整+附煞，战斗内按策略显式决策。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.alt_path_test import (setup_engine, pre_battle, strategy_turn,
                               resolve_monster_turn, STRATEGIES)
from sim.build_learner import round_start_relic_choices


def battle_one(e, strategy: str, battle_no: int, log_out):
    """打一场，返回 (win, path, rounds, detail)。"""
    from sim.optional_actions import start_battle
    bs, _art = start_battle(e)
    names = list(bs.get("enemies") or [])
    log_out.append(f"  第{battle_no}场出怪：{names}")
    result = {"win": False, "path": None, "rounds": 0, "detail": ""}
    PATH_TYPES = ("mediocrity", "sculpture", "cancer", "proliferation", "debt_bind", "seal")
    for rnd in range(1, 25):
        p = e.state.player
        if not p or not p.is_alive:
            result["detail"] = f"玩家阵亡于第{rnd}回合"
            break
        if not [x for x in e.state.enemies if x.is_alive]:
            result["win"] = True
            result["rounds"] = rnd
            break
        rs, _rsart = start_round(e)
        log = []
        strategy_turn(e, strategy, log)
        if not [x for x in e.state.enemies if x.is_alive]:
            result["win"] = True
            result["rounds"] = rnd
            break
        if not p.is_alive:
            result["detail"] = f"玩家阵亡于第{rnd}回合(玩家回合后)"
            break
        e.execute_action("resolve_ally_phases", {})
        mp = resolve_monster_turn(e, log)
        if not mp.get("success"):
            result["detail"] = f"怪物阶段失败: {mp.get('error')}"
            break
        if mp["result"].get("player_dead"):
            result["detail"] = f"玩家阵亡于第{rnd}回合(怪物阶段)"
            break
        re_ = e.execute_action("round_end", {})
        for ef in re_.get("result", {}).get("effects", []) or []:
            if ef.get("type") in PATH_TYPES:
                who = ef.get("entity", "")
                if ef["type"] == "mediocrity" and who == (p.name if p else ""):
                    continue
                result["path"] = "凡庸" if ef["type"] == "mediocrity" else ef["type"]
                result["detail"] = f"第{rnd}回合 {ef.get('note','')}"
    if result["path"] is None:
        for m in e.state.enemies:
            if m.is_sculptured:
                result["path"] = "雕塑"
            elif m.is_proliferated:
                result["path"] = "癌变"
            elif getattr(m, "removed_without_kill", False):
                result["path"] = "封印/移出"
            elif not m.is_alive:
                result["path"] = "击杀"
    return result


def full_run(strategy: str, winner_path: str, seed: int, db: str):
    e = setup_engine(winner_path, seed, db)
    logs = []
    cleared = 0
    for b in range(1, 8):
        p = e.state.player
        if not p or not p.is_alive:
            logs.append(f"  第{b}场：轮回者已阵亡")
            break
        pre_battle(e, strategy, logs)  # 每场战前休整+附煞
        r = battle_one(e, strategy, b, logs)
        logs.append(f"  第{b}场 {'✅' if r['win'] else '❌'} 路径={r['path']} 回合={r['rounds']} {r['detail']}")
        if not r["win"]:
            break
        be = e.execute_action("battle_end", {})
        cleared += 1
        logs.append(f"  战终：碎片+{be.get('result', {}).get('shard_reward', 0)}")
        # 第7场战终触发最终的冠冕
        crown = be.get("result", {}).get("final_crown", {})
        if crown.get("outcome") == "sealed":
            logs.append("  ★ 完整的冠冕：胜者被封存，乱葬岗通关！")
            break
        if crown.get("outcome") == "duel_start" or e.state.in_final_duel:
            logs.append("  ★ 进入第8场死斗")
            break
    return cleared, logs


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", choices=STRATEGIES + ("混合",), default="毒奶")
    ap.add_argument("--winners", type=int, default=4)
    ap.add_argument("--seeds", type=int, default=2)
    a = ap.parse_args()

    winners = sorted(os.listdir("data/real_winners"))[:a.winners]
    stats = []
    for w in winners:
        for si in range(a.seeds):
            seed = 3000 + int(w.split("_")[1].split(".")[0]) * 41 + si * 13
            db = tempfile.mktemp(suffix=".db")
            cleared, logs = full_run(a.strategy, os.path.join("data/real_winners", w), seed, db)
            stats.append(cleared)
            print(f"===== {a.strategy}流 · {w} · seed={seed} =====")
            for l in logs:
                print(l)
            print(f"  结果：通关 {cleared}/7 场")
    tot = len(stats)
    print(f"\n===== {a.strategy}流 汇总（{tot}局）=====")
    for i in range(1, 8):
        n = sum(1 for c in stats if c >= i)
        print(f"  活过第{i}场: {n}/{tot} ({n/tot*100:.0f}%)")


if __name__ == "__main__":
    main()


def full_run_mixed(winner_path: str, seed: int, db: str):
    """混合：封印主攻（第1回合移出全部），若法力不足/失败则毒奶兜底。"""
    from sim.alt_path_test import setup_engine, pre_battle, resolve_monster_turn
    from sim.build_learner import round_start_relic_choices
    e = setup_engine(winner_path, seed, db)
    logs = []
    cleared = 0
    for b in range(1, 8):
        p = e.state.player
        if not p or not p.is_alive:
            logs.append(f"  第{b}场：轮回者已阵亡")
            break
        # 局外：休整 + 学封印（第1场学） + 附煞
        while e.state.energy > 0:
            p = e.state.player
            if p and p.current_hp < p.blood_limit:
                r = e.execute_action("pre_battle_action", {
                    "sub_action": "休整", "tier": 3,
                    "heal_allocations": [{"target_ref": "player:0",
                                          "amount": 48 + e.state.rest_heal_bonus}]})
                if r.get("success"):
                    continue
            if "封印" not in p.dao_wen:
                r = e.execute_action("pre_battle_action", {
                    "sub_action": "学习", "sub": "daowen", "tier": 1, "names": ["封印"]})
                if r.get("success"):
                    logs.append("  学习·封印")
                    continue
            e.execute_action("pre_battle_action", {
                "sub_action": "修行", "tier": 1,
                "allocations": {"speed_points": 0, "mana_points": 1}})
        from sim.optional_actions import start_battle
        bs, _art = start_battle(e)
        names = list(bs.get("enemies") or [])
        logs.append(f"  第{b}场出怪：{names}")
        n = len([x for x in e.state.enemies if x.is_alive])
        won = False
        for rnd in range(1, 25):
            p = e.state.player
            if not p or not p.is_alive:
                break
            if not [x for x in e.state.enemies if x.is_alive]:
                won = True
                break
            rs, _rsart = start_round(e)
            # 第1回合：封印全部；法力不足则毒奶
            if rnd == 1 and p.current_mana >= 10 * n and "封印" in p.dao_wen:
                r = e.execute_action("use_daowen", {"daowen_name": "封印", "x": n,
                                                    "target_ref": "enemy:0",
                                                    "trigger_spell_choices": {}})
                if r.get("success"):
                    logs.append(f"  R1 封印X={n} → 移出{n}只怪物")
                    won = True
                    break
            # 毒奶兜底：对每只怪奶
            for m in [x for x in e.state.enemies if x.is_alive]:
                x = min(p.current_mana // 8, 8)
                if x >= 2 and "再生" in p.dao_wen:
                    r = e.execute_action("use_daowen", {"daowen_name": "再生", "x": x,
                                                        "target_ref": f"enemy:{e.state.enemies.index(m)}",
                                                        "trigger_spell_choices": {}})
            if not [x for x in e.state.enemies if x.is_alive]:
                won = True
                break
            e.execute_action("resolve_ally_phases", {})
            mp = resolve_monster_turn(e, [])
            if not mp.get("success"):
                break
            if mp["result"].get("player_dead"):
                break
            e.execute_action("round_end", {})
        if won and e.state.player and e.state.player.is_alive:
            be = e.execute_action("battle_end", {})
            cleared += 1
            logs.append(f"  第{b}场 ✅ 战终碎片+{be.get('result',{}).get('shard_reward',0)}")
            crown = be.get("result", {}).get("final_crown", {})
            if crown.get("outcome") == "sealed":
                logs.append("  ★ 乱葬岗通关，胜者被封存！")
                break
            if crown.get("outcome") == "duel_start" or e.state.in_final_duel:
                logs.append("  ★ 进入第8场死斗")
                break
        else:
            logs.append(f"  第{b}场 ❌ 失败")
            break
    return cleared, logs


if __name__ == "__main__" and os.environ.get("MIXED_TEST"):
    winners = sorted(os.listdir("data/real_winners"))[:6]
    stats = []
    for w in winners:
        for si in range(2):
            seed = 4000 + int(w.split("_")[1].split(".")[0]) * 43 + si * 11
            db = tempfile.mktemp(suffix=".db")
            cleared, logs = full_run_mixed(os.path.join("data/real_winners", w), seed, db)
            stats.append(cleared)
            print(f"===== 混合流 · {w} · seed={seed} =====")
            for l in logs:
                print(l)
            print(f"  结果：通关 {cleared}/7 场")
    tot = len(stats)
    print(f"\n===== 混合流(封印+毒奶) 汇总（{tot}局）=====")
    for i in range(1, 8):
        n = sum(1 for c in stats if c >= i)
        print(f"  活过第{i}场: {n}/{tot} ({n/tot*100:.0f}%)")
