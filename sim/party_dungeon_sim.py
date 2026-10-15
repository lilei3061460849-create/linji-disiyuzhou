#!/usr/bin/env python3
"""微光者组队进副本（乱葬岗第一场）引擎驱动。

宿主只是方便引擎开局的「空气」：不出手、不被怪物选为目标、血量设得极大。
微光者作为朋友（Entity 类型 朋友）加入，怪物攻击只落在微光者身上。
战斗流程（战始/回始/怪物阶段/回终/战终）全部走 GameEngine.execute_action。

用法：python3 sim/party_dungeon_sim.py --n 3 --blood 120 --atk-count 2 --atk-power 4 --runs 20
"""
import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.setup_support import begin_battle, begin_round
from engine.models import Entity
from sim.party_probe import new_engine
from sim.handplay_dungeon_with_winner import _resolve_monster_turn_hand

HOST_HP = 10_000_000  # 宿主：空气，血量无限大


def add_party(e, n, blood, atk_count, atk_power):
    for i in range(n):
        fr = Entity(f"微光者{i+1}", "朋友", blood_limit=blood, current_hp=blood,
                    attack_count=atk_count, attack_power=atk_power)
        e.state.friends.append(fr)


def living_friends(e):
    return [f for f in e.state.friends if f.is_alive and not f.has_retreated]


def friend_ref(e, fr):
    return f"friend:{e.state.friends.index(fr)}"


def _friend_only_target(options, refs):
    """目标选择：只从微光者中选（宿主为空气，不作为目标）。"""
    friends = [o["ref"] for o in options if o["ref"].startswith("friend:")]
    if friends:
        return min(friends, key=lambda r: refs[r].current_hp if r in refs else 0)
    return options[0]["ref"]


def monster_phase(e, log):
    """怪物阶段：复用手操驱动的引擎对接（道纹/闪避/法术提交），仅替换目标选择为微光者。"""
    import sim.handplay_dungeon_with_winner as hp
    orig = hp.choose_attack_target
    hp.choose_attack_target = _friend_only_target
    try:
        return _resolve_monster_turn_hand(e, log)
    finally:
        hp.choose_attack_target = orig


def host_minimal_action(e):
    """宿主唯一的动作：每回合一次杀伐X=1 打最弱的存活敌人（仅为避开凡庸判定）。"""
    p = e.state.player
    enemies = [m for m in e.state.enemies if m.is_alive]
    if not p or not p.is_alive or not enemies or p.current_mana < 1:
        return None
    target = min(enemies, key=lambda m: m.current_hp)
    idx = e.state.enemies.index(target)
    return e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1,
                                           "target_ref": f"enemy:{idx}",
                                           "trigger_spell_choices": {}})


def run_battle(e, log, max_rounds=30):
    bs = begin_battle(e)
    if not bs.get("success"):
        return {"ok": False, "error": bs.get("error")}
    won = False
    for rnd in range(1, max_rounds + 1):
        if not [m for m in e.state.enemies if m.is_alive]:
            won = True
            break
        if not living_friends(e):
            break
        rs = begin_round(e)
        if not rs.get("success"):
            return {"ok": False, "error": rs.get("error")}
        # 宿主最小出手：避免【凡庸】（连续5回合未出手/未减少敌血 → 命零）。杀伐X=1打最弱敌人。
        host_minimal_action(e)
        # 微光者各自攻击（命令攻击）
        for fr in living_friends(e):
            r = e.execute_action("command_ally", {"ally_ref": friend_ref(e, fr), "instruction": "攻击"})
            if not r.get("success"):
                log.append(f"  R{rnd} {fr.name} 攻击失败：{r.get('error')}")
        e.execute_action("resolve_ally_phases", {})
        if not [m for m in e.state.enemies if m.is_alive]:
            won = True
            break
        mp = monster_phase(e, log)
        if not mp.get("success"):
            return {"ok": False, "error": mp.get("error")}
        e.execute_action("round_end", {})
        alive = len(living_friends(e))
        log.append(f"  R{rnd} 存活微光者={alive} 敌剩={len([m for m in e.state.enemies if m.is_alive])}")
    if won:
        be = e.execute_action("battle_end", {})
        shard = be.get("result", {}).get("shard_reward", 0) if be.get("success") else 0
        return {"ok": True, "won": True, "shards": shard, "rounds": rnd}
    return {"ok": True, "won": False, "shards": 0, "rounds": rnd}


def one_run(seed, n, blood, atk_count, atk_power):
    e = new_engine(seed)
    e.state.player.blood_limit = HOST_HP
    e.state.player.current_hp = HOST_HP
    add_party(e, n, blood, atk_count, atk_power)
    log = []
    res = run_battle(e, log)
    res["survivors"] = len(living_friends(e))
    res["log"] = log
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--blood", type=int, default=120)
    ap.add_argument("--atk-count", type=int, default=2)
    ap.add_argument("--atk-power", type=int, default=4)
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    wins, shards, surv = 0, [], []
    outcome = Counter()
    for seed in range(1, a.runs + 1):
        r = one_run(seed, a.n, a.blood, a.atk_count, a.atk_power)
        if not r.get("ok"):
            outcome["引擎错误"] += 1
            if a.verbose:
                print(f"seed={seed} 引擎错误：{r.get('error')}")
            continue
        if r["won"]:
            wins += 1
            outcome["胜"] += 1
            shards.append(r["shards"])
        else:
            outcome["败/全灭"] += 1
        surv.append(r["survivors"])
        if a.verbose:
            print(f"seed={seed} won={r['won']} rounds={r['rounds']} survivors={r['survivors']} shards={r['shards']}")
            for line in r["log"]:
                print(line)
    total = a.runs
    print(f"配置：{a.n}名微光者 血限{a.blood} 攻击{a.atk_count}×{a.atk_power} 宿主=空气 副本=乱葬岗第一场 局数={total}")
    print(f"结果：{dict(outcome)}")
    print(f"胜率={wins/total:.0%}  平均存活={sum(surv)/max(1,len(surv)):.2f}")
    if shards:
        print(f"胜场碎片：平均={sum(shards)/len(shards):.1f} 最小={min(shards)} 最大={max(shards)}")


if __name__ == "__main__":
    main()
