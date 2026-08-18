#!/usr/bin/env python3
"""命令朋友替你扛伤——乱葬岗完整7场挑战。

胜者：龙心谷一阶胜者 + 岩行者（龙心谷事件「断桥余烬·接过伤者」结算获得，背负1）。
战法：每回合命令岩行者「发动背负 打 轮回者」（挡1次伤害）→ 命令岩行者攻击 →
      玩家杀伐输出（冥煞附煞）→ 庇护兜底。逐回合输出过程数据（伤害转嫁/血条）。
"""
import json
import os
import sys
import tempfile

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity
from sim.build_learner import round_start_relic_choices
from sim.handplay_dungeon_with_winner import load_winner
from sim.alt_path_test import resolve_monster_turn


def grant_yanxingzhe(e):
    """龙心谷事件「断桥余烬·接过伤者」结算：岩行者（2×4/54，背负1）加入。"""
    if any(fr.name == "岩行者" for fr in e.state.friends):
        return
    fr = Entity("岩行者", "朋友", blood_limit=54, current_hp=54,
                attack_count=2, attack_power=4)
    fr.dao_wen["背负"] = DaoWenInstance(
        DaoWen(name="背负", formula="", cost_type="", cost_formula="", effect_formula=""), x_value=1)
    e.state.friends.append(fr)


def grant_guards(e, n=2):
    """罪孽都市雇佣的高攻铁卫（5×10=50威胁/60血）：怪物天然集火它们，是第二层肉盾。"""
    for i in range(n):
        if any(emp.name == f"铁卫{i+1}" for emp in e.state.employees):
            continue
        emp = Entity(f"铁卫{i+1}", "员工", blood_limit=60, current_hp=60,
                     attack_count=5, attack_power=10, is_deployed=True)
        e.state.employees.append(emp)


def pre_battle(e, log):
    while e.state.energy > 0:
        p = e.state.player
        if p and p.current_hp < p.blood_limit:
            r = e.execute_action("pre_battle_action", {
                "sub_action": "休整", "tier": 3,
                "heal_allocations": [{"target_ref": "player:0",
                                      "amount": 48 + e.state.rest_heal_bonus}]})
            if r.get("success"):
                continue
        r = e.execute_action("pre_battle_action", {
            "sub_action": "附煞", "mode": "选择", "sha_qi": "冥煞", "daowen_name": "杀伐"})
        if r.get("success"):
            continue
        e.execute_action("pre_battle_action", {
            "sub_action": "修行", "tier": 1,
            "allocations": {"speed_points": 0, "mana_points": 1}})


def player_turn(e, log):
    """命令岩行者挡伤（背负）+ 命令攻击 + 玩家杀伐。"""
    p = e.state.player
    out = []
    fr = next((f for f in e.state.friends if f.name == "岩行者" and f.is_alive), None)
    # 1) 命令岩行者发动背负保护轮回者（每回合1次）
    if fr and not fr.has_retreated:
        r = e.execute_action("command_ally", {
            "ally_ref": "friend:0", "instruction": "发动背负 打 轮回者"})
        if r.get("success"):
            log.append("  命令岩行者「发动背负 打 轮回者」（轮回者下次受伤由岩行者承担）")
            out.append(r)
        # 2) 命令岩行者攻击（保持输出）
        r2 = e.execute_action("command_ally", {"ally_ref": "friend:0", "instruction": "攻击"})
        if r2.get("success"):
            log.append("  命令岩行者攻击")
            out.append(r2)
    # 2.5) 命令铁卫攻击（保持威胁分+输出）
    for idx, emp in enumerate(e.state.employees):
        if emp.is_alive and emp.is_deployed and not emp.has_retreated:
            r3 = e.execute_action("command_ally", {"ally_ref": f"employee:{idx}", "instruction": "攻击"})
            if r3.get("success"):
                log.append(f"  命令{emp.name}攻击")
                out.append(r3)
    # 3) 玩家杀伐秒怪
    for _ in range(max(1, (p.speed_limit + 2) // 3)):
        if not p.is_alive:
            break
        enemies = [x for x in e.state.enemies if x.is_alive]
        if not enemies:
            break
        target = min(enemies, key=lambda x: x.current_hp)
        x = max(1, p.current_mana - 3)
        if x >= 1:
            r = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": x,
                                                "target_ref": f"enemy:{e.state.enemies.index(target)}",
                                                "trigger_spell_choices": {}})
            if r.get("success"):
                dmg = sum(ef.get("actual_damage", 0) for ef in
                          (r.get("execution", {}).get("effects") or []))
                log.append(f"  杀伐X={x} 打{target.name} → {dmg}伤")
                out.append(r)
                continue
        break
    return out


def settle_wages(e, logs):
    """战终工资：为待决员工逐个 pay（碎片够就付，不够则拒付）。"""
    while True:
        pending = {k: v for k, v in e.state.pending_wage_decisions.items() if v is not None}
        if not pending:
            break
        name = next(iter(pending))
        wage = pending[name]
        if e.state.shards >= wage:
            r = e.execute_action("pay_employee_wage", {"name": name, "decision": "pay"})
            logs.append(f"  支付{name}工资{wage}")
        else:
            r = e.execute_action("pay_employee_wage", {"name": name, "decision": "refuse"})
            logs.append(f"  拒付{name}工资{wage}（碎片不足，{name}离队）")
        if not r.get("success"):
            logs.append(f"  工资结算异常：{r.get('error')}")
            break


def battle_one(e, battle_no, logs):
    from sim.optional_actions import start_battle, start_round
    bs, _art = start_battle(e)
    if not bs.get("success"):
        logs.append(f"  第{battle_no}场 battle_start失败：{bs.get('error')}")
        return False
    for _a in _art:
        logs.append(f"  法器：{_a.get('action')}")
    names = list(bs.get("enemies") or [])
    logs.append(f"  第{battle_no}场出怪：{names}")
    p = e.state.player
    fr = e.state.friends[0]
    won = False
    for rnd in range(1, 30):
        if not p or not p.is_alive:
            break
        if not [x for x in e.state.enemies if x.is_alive]:
            won = True
            break
        rs, _rsart = start_round(e)
        if not rs.get("success"):
            logs.append(f"    R{rnd} round_start失败 {rs.get('error')}")
            break
        for _a in _rsart:
            logs.append(f"    R{rnd} 法器：{_a.get('action')}")
        log = []
        player_turn(e, log)
        for line in log:
            logs.append(f"    R{rnd}{line}")
        if not [x for x in e.state.enemies if x.is_alive]:
            won = True
            break
        if not p.is_alive:
            break
        e.execute_action("resolve_ally_phases", {})
        mp = resolve_monster_turn(e, log)
        if not mp.get("success"):
            logs.append(f"    R{rnd} 怪物阶段失败 {mp.get('error')}")
            break
        # 过程数据：谁掉了多少血
        for d in (mp.get("result", {}).get("details") or []):
            for h in (d.get("hits") or []):
                logs.append(f"    R{rnd} [怪{d.get('attacker')}→{h.get('target')}] 伤{h.get('damage_dealt')}"
                            f" 撤退={bool(h.get('retreated'))} 目标hp={h.get('target_hp_after')}")
        logs.append(f"    R{rnd} 后 玩家hp={p.current_hp} 岩行者hp={fr.current_hp}"
                    f" 岩行者退场={fr.has_retreated} 敌={[(m.name, m.current_hp) for m in e.state.enemies if m.is_alive]}")
        if mp["result"].get("player_dead"):
            break
        e.execute_action("round_end", {})
    return won


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--winners", type=int, default=3)
    ap.add_argument("--seeds", type=int, default=2)
    a = ap.parse_args()
    winners = sorted(os.listdir("data/real_winners"))[:a.winners]
    stats = []
    for w in winners:
        for si in range(a.seeds):
            seed = 7000 + int(w.split("_")[1].split(".")[0]) * 59 + si * 29
            with open(os.path.join("data/real_winners", w), encoding="utf-8") as f:
                snap = json.load(f)
            e = GameEngine(db_path=tempfile.mktemp(suffix=".db"), rng_seed=seed,
                           sealed_candidate_path="/tmp/guard_full.json")
            p0 = snap["player"]
            e.execute_action("setup_attributes", {"name": p0["name"],
                                                  "blood_points": 10, "speed_points": 8, "mana_points": 7})
            finish_initial_daowen(e)
            e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
            setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
            e.execute_action("choose_discovered_relic",
                             {"relic_name": setup["result"]["relic_choices"][0]})
            load_winner(e, snap)
            grant_yanxingzhe(e)
            grant_guards(e)
            logs = []
            cleared = 0
            for b in range(1, 8):
                p = e.state.player
                if not p or not p.is_alive:
                    logs.append(f"  第{b}场：阵亡")
                    break
                pre_battle(e, logs)
                won = battle_one(e, b, logs)
                if won and e.state.player and e.state.player.is_alive:
                    be = e.execute_action("battle_end", {})
                    # 员工工资待决会阻塞战终，先结算工资再重试
                    guard = 0
                    while (be.get("success") and be.get("completed") is False
                           and be.get("pending_wage_decisions")):
                        settle_wages(e, logs)
                        be = e.execute_action("battle_end", {})
                        guard += 1
                        if guard > 5:
                            break
                    if not be.get("success"):
                        logs.append(f"  第{b}场 battle_end失败：{be.get('error')}")
                        break
                    cleared += 1
                    logs.append(f"  第{b}场 ✅ 碎片+{be.get('result',{}).get('shard_reward',0)}")
                    crown = be.get("result", {}).get("final_crown", {})
                    if crown.get("outcome") == "sealed":
                        logs.append("  ★ 乱葬岗通关，胜者被封存！")
                        break
                else:
                    logs.append(f"  第{b}场 ❌")
                    break
            stats.append(cleared)
            print(f"===== {w} + 岩行者 · seed={seed} =====")
            for l in logs:
                print(l)
            print(f"  结果：通关 {cleared}/7 场")
    tot = len(stats)
    print(f"\n===== 命令岩行者挡伤 汇总（{tot}局）=====")
    for i in range(1, 8):
        n = sum(1 for c in stats if c >= i)
        print(f"  活过第{i}场: {n}/{tot} ({n/tot*100:.0f}%)")


if __name__ == "__main__":
    main()
