#!/usr/bin/env python3
"""乱葬岗多路径胜利实测：毒奶流(癌变) / 蒙蔽流(凡庸) / 石化流(雕塑) / 封印流(直接移出)。

用真实一阶胜者，逐回合显式决策，验证敌人能否通过非击杀路径被移除：
  癌变 = 累计恢复量 ≥ 血限×2（超出血限按双倍计）
  雕塑 = 攻击次数或攻击力归0（任何非轮回者）
  凡庸 = 连续5回合未出手 或 连续5回合未能使敌对角色生命减少
  封印 = 直接移出X个怪物

用法：
    python3 sim/alt_path_test.py --strategy 毒奶 --winners 3 --seeds 3
"""
import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Entity
from sim.build_learner import round_start_relic_choices, _resolve_pending_event
from sim.handplay_dungeon_with_winner import load_winner, _resolve_monster_turn_hand

STRATEGIES = ("毒奶", "蒙蔽", "石化", "封印")


def setup_engine(winner_path: str, seed: int, db: str):
    with open(winner_path, encoding="utf-8") as f:
        snapshot = json.load(f)
    e = GameEngine(db_path=db, rng_seed=seed, sealed_candidate_path="/tmp/alt_seal.json")
    p0 = snapshot["player"]
    e.execute_action("setup_attributes", {"name": p0["name"],
                                          "blood_points": 10, "speed_points": 8, "mana_points": 7})
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    load_winner(e, snapshot)
    return e


def pre_battle(e, strategy: str, log):
    """局外：休整回满 + 按策略附煞/领悟。返回(成功?, 说明)。"""
    notes = []
    fusha_done = False
    # 休整回满（用3档大回复）
    while e.state.energy > 0:
        p = e.state.player
        if p and p.current_hp < p.blood_limit:
            r = e.execute_action("pre_battle_action", {
                "sub_action": "休整", "tier": 3,
                "heal_allocations": [{"target_ref": "player:0",
                                      "amount": 48 + e.state.rest_heal_bonus}]})
            if r.get("success"):
                continue
        # 策略专属附煞（每种只做一次）
        if not fusha_done:
            if strategy == "毒奶" and "再生" in p.dao_wen:
                r = e.execute_action("pre_battle_action", {
                    "sub_action": "附煞", "mode": "选择", "sha_qi": "血煞", "daowen_name": "再生"})
                if r.get("success"):
                    notes.append("附煞·血煞·再生（回复+100%）")
                    fusha_done = True
                    continue
            if strategy == "石化" and "杀伐" in p.dao_wen and "反转" not in e.state.resonance:
                r = e.execute_action("pre_battle_action", {
                    "sub_action": "附煞", "mode": "选择", "sha_qi": "冥煞", "daowen_name": "杀伐"})
                if r.get("success"):
                    notes.append("附煞·冥煞·杀伐（伤害+100%）")
                    fusha_done = True
                    continue
        if strategy in ("石化",) and "反转" not in e.state.resonance:
            r = e.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "反转"})
            if r.get("success"):
                notes.append("领悟·反转")
                continue
        # 封印流：学封印（免费1档）
        if strategy == "封印" and "封印" not in p.dao_wen:
            r = e.execute_action("pre_battle_action", {
                "sub_action": "学习", "sub": "daowen", "tier": 1, "names": ["封印"]})
            if r.get("success"):
                notes.append("学习·封印")
                continue
        # 兜底修行
        e.execute_action("pre_battle_action", {
            "sub_action": "修行", "tier": 1,
            "allocations": {"speed_points": 0, "mana_points": 1}})
    for n in notes:
        log.append(f"  局外：{n}")
    return True


def resolve_monster_turn(e, log):
    """怪物阶段：理智闪避 + 反应法术触发（与手操脚本一致）。"""
    return _resolve_monster_turn_hand(e, log)


def strategy_turn(e, strategy: str, log):
    """玩家回合：按策略显式决策。返回出手列表。"""
    p = e.state.player
    out = []
    enemies = [x for x in e.state.enemies if x.is_alive]
    if not enemies:
        return out
    for _ in range(max(1, (p.speed_limit + 2) // 3)):
        if not p.is_alive:
            break
        enemies = [x for x in e.state.enemies if x.is_alive]
        if not enemies:
            break
        m = enemies[0]
        acted = False
        threat = sum(x.attack_count * x.attack_power for x in enemies)

        if strategy == "毒奶" and "再生" in p.dao_wen:
            # 毒奶：对怪猛奶（血煞翻倍+超限双倍 → 癌变），X尽量大
            x = min(p.current_mana, 8)
            if x >= 2:
                r = e.execute_action("use_daowen", {"daowen_name": "再生", "x": x,
                                                    "target_ref": f"enemy:{e.state.enemies.index(m)}",
                                                    "trigger_spell_choices": {}})
                if r.get("success"):
                    log.append(f"  毒奶：再生X={x} 奶{m.name}（累计恢复{m.total_healed}/{e.combat.cancer_threshold_of(m)}）")
                    out.append(r)
                    continue
        elif strategy == "蒙蔽" and "蒙蔽" in p.dao_wen:
            # 蒙蔽：下X次伤害无效。需覆盖怪物全部命中（攻击出手数×攻击次数，含狂暴加成）
            actions = 1
            if "狂暴" in m.dao_wen:
                actions += 1
            need = actions * m.attack_count
            have = m.get_status_value("蒙蔽") if m.has_status("蒙蔽") else 0
            need = max(0, need - have)
            x = min(p.current_mana // 5, need) if need > 0 else 0
            if x >= 1:
                r = e.execute_action("use_daowen", {"daowen_name": "蒙蔽", "x": x,
                                                    "target_ref": f"enemy:{e.state.enemies.index(m)}",
                                                    "trigger_spell_choices": {}})
                if r.get("success"):
                    log.append(f"  蒙蔽：蒙蔽X={x}（{m.name}命中{actions}×{m.attack_count}，已叠{have}+{x}）")
                    out.append(r)
                    continue
        elif strategy == "石化" and "弱化" in p.dao_wen:
            # 石化：弱化X → 攻击力-∞ → 归0 → 雕塑
            x = min(p.current_mana // 3, m.attack_power)
            if x >= 1:
                r = e.execute_action("use_daowen", {"daowen_name": "弱化", "x": x,
                                                    "target_ref": f"enemy:{e.state.enemies.index(m)}",
                                                    "trigger_spell_choices": {}})
                if r.get("success"):
                    log.append(f"  石化：弱化X={x}（{m.name}攻击力{m.attack_power}）")
                    out.append(r)
                    continue
        elif strategy == "石化" and "反转" in e.state.resonance:
            # 尝试残韵：若怪物持 强化 → 反转得弱化；必中→反转得蒙蔽（作为石化/控制的钥匙）
            for src in ("强化", "必中", "减速", "自愈"):
                if src in m.dao_wen:
                    r = e.execute_action("use_resonance", {
                        "source_daowen": src, "resonance_type": "反转", "target_ref": f"enemy:{e.state.enemies.index(m)}"})
                    if r.get("success"):
                        gained = [k for k in e.state.player.dao_wen if k not in ("杀伐", "庇护", "再生", "蒙蔽") or k == src]
                        log.append(f"  残韵：反转 {src} → 获得{list(e.state.player.dao_wen)}")
                        out.append(r)
                        acted = True
                        break
        elif strategy == "封印" and "封印" in p.dao_wen:
            # 封印：直接移出怪物（10X法力移出X只）
            n = len(enemies)
            r = e.execute_action("use_daowen", {"daowen_name": "封印", "x": n,
                                                "target_ref": f"enemy:{e.state.enemies.index(m)}",
                                                "trigger_spell_choices": {}})
            if r.get("success"):
                log.append(f"  封印：封印X={n} 移出{n}只怪物")
                out.append(r)
                continue

        # 保命：策略没出手时才上盾（蒙蔽/毒奶本身就是防御）
        if not acted and threat > p.current_hp + p.shield and "庇护" in p.dao_wen and p.current_mana >= 2:
            r = e.execute_action("use_daowen", {"daowen_name": "庇护", "x": 2,
                                                "target_ref": "player:0",
                                                "trigger_spell_choices": {}})
            if r.get("success"):
                log.append(f"  保命：庇护X=2（盾{p.shield}）")
                out.append(r)
                continue
        if acted:
            continue
        # 兜底：杀伐X=1 打最弱（避免自己凡庸；也为毒奶创造超限空间）
        target = min(enemies, key=lambda x: x.current_hp)
        if p.current_mana >= 1:
            r = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1,
                                                "target_ref": f"enemy:{e.state.enemies.index(target)}",
                                                "trigger_spell_choices": {}})
            if r.get("success"):
                log.append(f"  输出：杀伐X=1 打{target.name}")
                out.append(r)
                continue
        break
    return out


def run_strategy_battle(strategy: str, winner_path: str, seed: int, db: str):
    """打一场乱葬岗战斗，返回结果。"""
    e = setup_engine(winner_path, seed, db)
    log = []
    pre_battle(e, strategy, log)
    for line in log:
        print(" ", line)
    e.state.energy = 0
    active = {r.name for r in e.state.relics if e.state.sealed_relics.get(r.name, 0) <= 0}
    bs_choices = {n: {"use": False} for n in ("三相残韵盘", "折速法印", "猩红果实", "苍白之花")
                  if n in active}
    bs = e.execute_action("battle_start", {"relic_choices": bs_choices})
    names = list(bs.get("enemies") or [])
    print(f"  出怪：{names}")

    result = {"win": False, "path": None, "rounds": 0, "detail": ""}
    PATH_TYPES = ("mediocrity", "sculpture", "cancer", "proliferation", "debt_bind", "seal")

    def scan_paths(re_, rnd):
        """扫描回终 effects 与 victory_paths，记录触发路径。"""
        for ef in re_.get("result", {}).get("effects", []) or []:
            if ef.get("type") in PATH_TYPES:
                who = ef.get("entity", "")
                if ef["type"] == "mediocrity" and who == (e.state.player.name if e.state.player else ""):
                    continue  # 玩家的凡庸不算胜利
                result["path"] = "凡庸" if ef["type"] == "mediocrity" else ef["type"]
                result["detail"] = f"第{rnd}回合 {ef.get('note','')}"
        for vp in re_.get("victory_paths", []) or []:
            if vp.get("type") in ("sculpture", "cancer", "proliferation", "debt_bind"):
                result["path"] = "凡庸" if vp["type"] == "mediocrity" else vp["type"]
                result["detail"] = f"第{rnd}回合 {vp.get('note','')}"

    for rnd in range(1, 25):
        p = e.state.player
        if not p or not p.is_alive:
            result["detail"] = f"玩家阵亡于第{rnd}回合"
            break
        if not [x for x in e.state.enemies if x.is_alive]:
            result["win"] = True
            result["rounds"] = rnd
            break
        e.execute_action("round_start", {"relic_choices": round_start_relic_choices(e)})
        log.clear()
        strategy_turn(e, strategy, log)
        for line in log:
            print(f"    R{rnd} {line}")
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
        scan_paths(re_, rnd)
    # 战斗结束后的兜底判定：查怪物终态标记
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", choices=STRATEGIES, default="毒奶")
    ap.add_argument("--winners", type=int, default=3)
    ap.add_argument("--seeds", type=int, default=3)
    a = ap.parse_args()

    winners = sorted(os.listdir("data/real_winners"))[:a.winners]
    wins = 0
    path_counter = {}
    total = 0
    for w in winners:
        for si in range(a.seeds):
            seed = 2000 + int(w.split("_")[1].split(".")[0]) * 31 + si * 7
            db = tempfile.mktemp(suffix=".db")
            print(f"\n===== {a.strategy}流 · {w} · seed={seed} =====")
            r = run_strategy_battle(a.strategy, os.path.join("data/real_winners", w), seed, db)
            total += 1
            if r["win"]:
                wins += 1
                key = r["path"] or "击杀"
                path_counter[key] = path_counter.get(key, 0) + 1
                print(f"  ✅ 通关！路径={r['path']} 回合={r['rounds']} {r['detail']}")
            else:
                print(f"  ❌ 失败 {r['detail']}")
    print(f"\n===== {a.strategy}流汇总：{wins}/{total} 通关 =====")
    print("  路径分布:", path_counter)


if __name__ == "__main__":
    main()
