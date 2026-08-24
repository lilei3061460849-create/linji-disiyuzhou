#!/usr/bin/env python3
"""封印流 vs 护卫流 —— 最终死斗（第8场）擂台测试。

双方都是真实一阶胜者（winner_02，法46）的乱葬岗最终形态：
  封印流：胜者 + 封印道纹 + 2铁卫员工（60血/5×10）
  护卫流：胜者 + 3护卫者朋友（54血/2×4）+ 护卫命令（无消耗强制挡伤）

死斗流程（引擎 _trigger_final_crown）：
  第7场战终 → 无候选则封存挑战者；有候选则双方进入第8场死斗。
  本脚本：挑战者 setup 乱葬岗，守擂者快照写入 sealed_candidate_path，
  current_battle=6 → battle_start(=7) 秒怪 → battle_end → final_crown → duel_start。

死斗循环（build_learner 同款）：
  round_start → 挑战者侧行动（护卫命令+杀伐）→ resolve_ally_phases
  → _resolve_monster_turn（守擂者侧：杀伐/封印道纹自动发动）→ round_end。

用法：python3 sim/duel_seal_vs_guard.py --rounds 5
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.setup_support import finish_initial_daowen

from engine.api import GameEngine
from sim.build_learner import _resolve_monster_turn, round_start_relic_choices
from sim.handplay_dungeon_with_winner import load_winner
from sim.alt_path_test import resolve_monster_turn
from sim.duel_common import run_duel_alternating
from sim.duel_pvp import run_duel_pvp


def setup_challenger(snapshot_path: str, defender_path: str, seed: int, db: str):
    """挑战者进入乱葬岗，守擂者写入封存槽。返回引擎（已加载挑战者）。"""
    e = GameEngine(db_path=db, rng_seed=seed, sealed_candidate_path=defender_path)
    with open(snapshot_path, encoding="utf-8") as f:
        snap = json.load(f)
    p0 = snap["player"]
    e.execute_action("setup_attributes", {"name": p0["name"], "blood_points": 10,
                                          "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    load_winner(e, snap)
    # 死斗无局外阶段（README 312）：双方继承第7场战终的实际损耗+碎片，擂台前不回血。
    # （此前硬设满血是测试缺陷，抹掉了快照真实损耗。）
    return e


def clear_battle_7(e):
    """第7场仪式战从简：挑战者已通关乱葬岗（两流派各自达标），这里直接清场
    触发 final_crown → duel_start，保证双方满状态进入死斗擂台。"""
    e.state.energy = 0
    e.state.current_battle = 6  # battle_start 会 +1 → 7 → battle_end 触发 final_crown
    from sim.optional_actions import start_battle
    bs, _art = start_battle(e)
    if not bs.get("success"):
        return bs
    # 仪式战：直接判定清场（不消耗挑战者状态）
    for m in e.state.enemies:
        m.is_alive = False
    be = e.execute_action("battle_end", {})
    # 员工工资待决会阻塞战终（封印流挑战者带铁卫），先结算再重试
    from sim.guard_full_run import settle_wages
    guard = 0
    while (be.get("success") and be.get("completed") is False
           and be.get("pending_wage_decisions")):
        settle_wages(e, [])
        be = e.execute_action("battle_end", {})
        guard += 1
        if guard > 5:
            break
    return be


def make_challenger_act(e, log):
    """挑战者侧一次行动（对称驱动每步调用1次）：
    每回合第一步先护卫命令（把盟友变肉盾），其余步走命令攻击/杀伐输出。
    返回 True=成功行动（duel_turn 已 advance 到 opponent）；False=无行动。"""
    p = e.state.player
    state = {"last_round": -1, "guarded": False}

    def act_once():
        if not p or not p.is_alive:
            return False
        # 新回合：先做一轮护卫（每回合1次，避免无消耗护卫刷屏不输出）
        if e.state.current_round != state["last_round"]:
            state["last_round"] = e.state.current_round
            state["guarded"] = False
        if not state["guarded"]:
            for prefix, entities in (("friend", e.state.friends), ("employee", e.state.employees)):
                for idx, ally in enumerate(entities):
                    if not ally.is_alive or ally.has_retreated:
                        continue
                    if prefix == "employee" and not ally.is_deployed:
                        continue
                    r = e.execute_action("command_ally", {"ally_ref": f"{prefix}:{idx}", "instruction": "护卫 9"})
                    if r.get("success"):
                        state["guarded"] = True
                        log.append(f"  命令{ally.name}护卫（挡9次伤）")
                        return True
            state["guarded"] = True  # 无盟友可护卫也标记，避免反复尝试
        # 命令盟友攻击（每次1个）
        for prefix, entities in (("friend", e.state.friends), ("employee", e.state.employees)):
            for idx, ally in enumerate(entities):
                if not ally.is_alive or ally.has_retreated:
                    continue
                if prefix == "employee" and not ally.is_deployed:
                    continue
                r2 = e.execute_action("command_ally", {"ally_ref": f"{prefix}:{idx}", "instruction": "攻击"})
                if r2.get("success"):
                    return True
        # 杀伐：死斗只允许一名轮回者离开——优先杀守擂主将（轮回者），再打肉盾
        enemies = [x for x in e.state.enemies if x.is_alive]
        if enemies:
            lord = next((x for x in enemies if x.entity_type == "轮回者"), None)
            target = lord or min(enemies, key=lambda x: x.current_hp)
            x = max(1, p.current_mana - 3)
            if x >= 1:
                r4 = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": x,
                                                     "target_ref": f"enemy:{e.state.enemies.index(target)}",
                                                     "trigger_spell_choices": {}})
                if r4.get("success"):
                    dmg = sum(ef.get("actual_damage", 0) for ef in
                              (r4.get("execution", {}).get("effects") or []))
                    log.append(f"  杀伐X={x} 打{target.name} → {dmg}伤")
                    return True
        return False

    return act_once


def run_duel(challenger_snap: str, defender_snap: str, seed: int,
             challenger_name: str, defender_name: str, db: str):
    """跑一场死斗，返回 (挑战者胜?, 回合数, 日志)。"""
    e = setup_challenger(challenger_snap, defender_snap, seed, db)
    be = clear_battle_7(e)
    crown = be.get("result", {}).get("final_crown", {})
    if crown.get("outcome") != "duel_start":
        return None, 0, [f"未进入死斗: {crown.get('outcome')} {be.get('error','')}"]
    logs = [f"⚔ 第8场死斗：{challenger_name}(挑战者) vs {defender_name}(守擂)"]
    opponent = e.state.enemies
    logs.append(f"  守擂方：{[(x.name, x.blood_limit) for x in opponent]}")
    logs.append(f"  挑战方：{e.state.player.name}({e.state.player.blood_limit}) + "
                + "、".join(f"{x.name}{x.blood_limit}" for x in e.state.friends)
                + "、" + "、".join(f"{x.name}{x.blood_limit}" for x in e.state.employees))
    # PvP 对称交替驱动：双方都按轮回者规则（法力制/出手次数=速限/3/自由控X）
    log_buf = []
    act = make_challenger_act(e, log_buf)
    result = run_duel_pvp(e, act, max_rounds=60, max_steps=400, log=log_buf)
    # 收集过程中日志（简单起见只保留首末）
    for line in log_buf[:6]:
        logs.append(f"  {line}")
    p2 = e.state.player
    logs.append(f"  最终：挑战者 hp={p2.current_hp if p2 else 0}/{p2.blood_limit if p2 else 0} "
                f"守擂={[(x.name, x.current_hp) for x in e.state.enemies if x.is_alive]}")
    logs.append(f"  判定：{result.get('winner')}（{result.get('reason')}，第{result.get('rounds')}回合）")
    challenger_won = result.get("winner") == "challenger"
    if challenger_won:
        logs.append("🏆 挑战者获胜！")
    else:
        logs.append("💀 守擂方获胜")
    return challenger_won, result.get("rounds", 0), logs


def build_snapshots():
    """基于真实一阶胜者（winner_02 法46）构建两流派快照（每次运行重建）。"""
    import shutil
    base = json.load(open("data/real_winners/winner_02.json", encoding="utf-8"))
    seal_snap = "data/real_winners_duel_seal.json"
    guard_snap = "data/real_winners_duel_guard.json"
    # 封印流：+封印道纹 +2铁卫员工（60血/5×10）
    seal = json.loads(json.dumps(base))
    seal["player"]["dao_wen"]["封印"] = 0
    seal["employees"] = []
    for i in range(2):
        seal["employees"].append({
            "name": f"铁卫{i+1}", "entity_type": "员工",
            "blood_limit": 60, "current_hp": 60, "mana_limit": 0, "current_mana": 0,
            "speed_limit": 0, "current_speed": 0, "attack_count": 5, "attack_power": 10,
            "shield": 0, "is_flying": False, "is_alive": True, "shards": 0, "is_debt_bound": False,
            "dao_wen": {}, "spells": [], "relics": [], "status_effects": []})
    json.dump(seal, open(seal_snap, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    # 护卫流：+3护卫者朋友（54血/2×4）
    guard = json.loads(json.dumps(base))
    guard["friends"] = []
    for i in range(3):
        guard["friends"].append({
            "name": f"护卫者{i+1}", "entity_type": "朋友",
            "blood_limit": 54, "current_hp": 54, "mana_limit": 0, "current_mana": 0,
            "speed_limit": 0, "current_speed": 0, "attack_count": 2, "attack_power": 4,
            "shield": 0, "is_flying": False, "is_alive": True, "shards": 0, "is_debt_bound": False,
            "dao_wen": {}, "spells": [], "relics": [], "status_effects": []})
    # 死斗继承第7场战终损耗：快照 current_hp 原样保留（不补齐满血），
    # 双方以真实残血+碎片进擂台。若某侧先手判定因残血偏袒另一方，
    # 那正是"损耗影响死斗"的真实规则，不应人为抹平。
    json.dump(seal, open(seal_snap, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(guard, open(guard_snap, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return seal_snap, guard_snap


def main():
    import argparse
    import shutil
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=4)
    a = ap.parse_args()
    seal_snap, guard_snap = build_snapshots()
    results = []
    for i in range(a.rounds):
        seed = 20000 + i * 97
        # 交替换边：偶=封印流挑战，奇=护卫流挑战
        if i % 2 == 0:
            cs, ds, cn, dn = seal_snap, guard_snap, "封印流", "护卫流"
        else:
            cs, ds, cn, dn = guard_snap, seal_snap, "护卫流", "封印流"
        # 每局用 fresh 快照副本（_trigger_final_crown 会删除封存槽文件）
        cs_tmp = tempfile.mktemp(suffix=".json")
        ds_tmp = tempfile.mktemp(suffix=".json")
        shutil.copy(cs, cs_tmp)
        shutil.copy(ds, ds_tmp)
        db = tempfile.mktemp(suffix=".db")
        won, rnd, logs = run_duel(cs_tmp, ds_tmp, seed, cn, dn, db)
        results.append((cn, dn, won, rnd))
        print(f"\n===== 第{i+1}局 seed={seed} =====")
        for l in logs:
            print(l)
    print("\n===== 死斗汇总 =====")
    seal_wins = sum(1 for cn, dn, won, _ in results if won and cn == "封印流")
    guard_wins = sum(1 for cn, dn, won, _ in results if won and cn == "护卫流")
    print(f"共{len(results)}局：封印流胜 {seal_wins} 局，护卫流胜 {guard_wins} 局")


if __name__ == "__main__":
    main()
