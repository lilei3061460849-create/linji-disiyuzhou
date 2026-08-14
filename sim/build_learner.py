#!/usr/bin/env python3
"""
自学习流派优化器：让 AI 通过多次轮回自己发现哪些道纹组合 1+1>2。

与 sim/build_winrate.py 的区别：
  build_winrate 是"固定套路"——我事先写死几个流派，只是测它们的胜率。
  build_learner 不预设任何流派：它自己组合道纹、跑轮回、根据胜负更新权重，
  并把学到的结果**写回 JSON**，下次启动继续在此基础上进化。

方法（多臂老虎机 + 协同增益挖掘）：
  1. 每轮从候选道纹池按 UCB1 采样一套 build（初始道纹 + 学习序列）
  2. 跑 N 局，得到 fitness（通关场数 + 胜负加权）
  3. 用 fitness 更新：
       - 单道纹价值   value[A]
       - 配对协同     synergy[A,B] = 含AB的平均分 - (含A平均 + 含B平均)/2
     synergy > 0 即 1+1>2
  4. 精英组合交叉变异产生下一代，持续迭代
  5. 全部状态存入 data/build_knowledge.json，可反复续跑累积经验

用法：
    python3 sim/build_learner.py --generations 20 --runs 6
    python3 sim/build_learner.py --report          # 只看已学到的知识
    python3 sim/build_learner.py --reset           # 清空重学
"""
import argparse
import json
import math
import os
import random
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.ai_tactics import TacticalAI, TACTICAL_ROLES

KNOWLEDGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "build_knowledge.json")

# 候选池：所有 AI 会主动使用的道纹（数据驱动，跟着 TACTICAL_ROLES 走）
CANDIDATES = sorted(TACTICAL_ROLES.keys())

# 门禁修复后，并非所有道纹都能通过局外【学习】获得：
#   - 怪物转化道纹：须以自身已持有的道纹为起点经残韵变化获得（README 211/248）
#   - 副本专属道纹：须先经残韵从本副本怪物身上转化获得一种，才能学其余（README 156）
# 若仍按全池组 build，绝大多数 build 会因"学不上"而退化成同一套，数据失真。
# 故按副本给出"实际可通过学习获得"的候选池。
from engine.gamedata import (REGION_EXCLUSIVE_DAOWEN, ORIGINAL_MONSTER_DAOWEN,
                             MONSTER_TRANSFORM_DAOWEN)

_ALL_EXCLUSIVE = {d for v in REGION_EXCLUSIVE_DAOWEN.values() for d in v}


def _decline_spells(option):
    return {timing: {spell["spell_name"]: {"use": False}
                     for spell in option.get("spell_options", {}).get(timing, [])}
            for timing in ("before", "after")}

def learnable_candidates(region: str = None) -> list:
    """当前副本下可通过局外【学习】直接获得的道纹（不含需残韵转化的）。"""
    out = []
    for c in CANDIDATES:
        if c in ORIGINAL_MONSTER_DAOWEN or c in MONSTER_TRANSFORM_DAOWEN:
            continue
        if c in _ALL_EXCLUSIVE and c not in set(REGION_EXCLUSIVE_DAOWEN.get(region, ())):
            continue
        out.append(c)
    return out
# 可作为初始道纹的（README：开局在【杀伐】【锐利】中选一种）
STARTERS = ["杀伐", "锐利"]
REGIONS = ["罪孽都市", "扭曲都市", "龙心谷"]
BUILD_SIZE = 5          # 每套 build 学习的道纹数量


# --------------------------------------------------------------------------
# 一局轮回
# --------------------------------------------------------------------------

def _resolve_monster_turn(engine):
    prepared = engine.execute_action("prepare_monster_phase", {})
    if not prepared.get("success"):
        return prepared
    choices = []
    for actor in prepared["result"]["actors"]:
        dao = None
        action_count = actor["base_attack_actions"]
        hit_count = actor["base_hits_per_attack"]
        if actor["daowen_options"]:
            option = actor["daowen_options"][0]
            dao = {"name": option["name"], "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {holder: {sp["spell_name"]: {"use": False} for sp in spells}
                                               for holder, spells in option.get("trigger_spell_options", {}).items()}}
            if option["requires_target"]:
                dao["target_ref"] = option["target_options"][0]["ref"]
            if option["dodge_submission"] == "per_target":
                dao["dodge_targets"] = [
                    {"target_ref": target["ref"], "dodge": False, "blood_shadow": False}
                    for target in option["dodge_target_options"]
                ]
            if option["resolves_as"] == "活力":
                action_count += option["x"]
            elif option["resolves_as"] == "狂暴":
                action_count += 1
            elif option["resolves_as"] == "变形":
                enemy_index = int(actor["actor_ref"].split(":", 1)[1])
                hit_count = engine.state.enemies[enemy_index].attack_power
        target_ref = actor["attack_target_options"][0]["ref"]
        target_option = next(option for option in actor["attack_target_options"] if option["ref"] == target_ref)
        attacks = [{"hits": [{"target_ref": target_ref, "dodge": False, "blood_shadow": False,
                               "spell_choices": _decline_spells(target_option)}
                              for _ in range(hit_count)]}
                   for _ in range(action_count)]
        choices.append({"actor_ref": actor["actor_ref"], "daowen": dao,
                        "attack_actions": attacks})
    return engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": choices,
    })


def play(starter: str, learn: list, region: str, seed=None, battles: int = 7,
         rng: random.Random = None, policy: dict = None, telemetry: dict = None) -> dict:
    """
    跑一局轮回。seed=None 时引擎使用真随机源。

    policy: 局外行动权重 {行动名: 权重}，AI 按权重随机挑选可用行动。
            这样"选择率"是 AI 自己选出来的，而不是脚本写死的。
    telemetry: 传入则累计真实统计（行动选择/成功/失败原因/异常）。

    返回含 invalid 标记：本局若出现引擎异常，视为无效数据（不计入统计）。
    """
    rng = rng or random
    policy = policy or DEFAULT_POLICY
    e = GameEngine(db_path="/tmp/learner.db", rng_seed=seed)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    e.execute_action("setup_choose_daowen", {"daowen": starter})
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": region})
    optional_relics = {"折速法印", "鲜血契约", "三相残韵盘", "卖身契"}
    starter_relic = next((n for n in setup["result"]["relic_choices"] if n not in optional_relics),
                         setup["result"]["relic_choices"][0])
    e.execute_action("choose_discovered_relic", {"relic_name": starter_relic})

    ai = TacticalAI(e)
    todo = list(learn)
    cleared = 0

    def record(kind, name, detail=""):
        if telemetry is None:
            return
        telemetry.setdefault(kind, {})
        key = name if not detail else f"{name}｜{detail}"
        telemetry[kind][key] = telemetry[kind].get(key, 0) + 1

    for b in range(1, battles + 1):
        while e.state.energy > 0:
            before = e.state.energy
            act, params = choose_pre_battle(e, todo, b, rng, policy)
            record("attempted", act)
            try:
                r = e.execute_action("pre_battle_action", {"sub_action": act, **params})
            except Exception as ex:                      # 引擎抛异常 = bug，本局作废
                record("engine_error", act, f"{type(ex).__name__}: {ex}")
                return {"cleared": cleared, "won": False, "invalid": True,
                        "reason": f"pre_battle {act}: {ex}"}
            if r.get("success"):
                record("succeeded", act)
                if act == "学习" and params.get("name") in todo:
                    todo.remove(params["name"])
                if e.state.pending_relic_choices:
                    e.execute_action("choose_discovered_relic", {
                        "relic_name": e.state.pending_relic_choices[0],
                    })
                if e.state.pending_item_choices:
                    e.execute_action("choose_discovered_item", {
                        "item_name": e.state.pending_item_choices[0],
                    })
            else:
                record("failed", act, str(r.get("error"))[:60])
                # 失败必须退还精力，否则会死循环；引擎已退还，这里兜底防死锁
                if e.state.energy >= before:
                    e.execute_action("pre_battle_action",
                                     {"sub_action": "修行", "tier": 1, "to": "mana"})

        relic_choices = ({starter_relic: {"use": False}}
                         if starter_relic in optional_relics else {})
        bs = e.execute_action("battle_start", {"relic_choices": relic_choices})
        if not bs.get("success"):
            return {"cleared": cleared, "won": False, "invalid": True,
                    "reason": f"battle_start: {bs.get('error')}"}
        for _ in range(40):
            if not e.state.player or not e.state.player.is_alive:
                break
            if not [x for x in e.state.enemies if x.is_alive]:
                break
            e.execute_action("round_start", {})
            ai.new_round()
            try:
                ai.take_turn()
            except Exception as ex:
                record("engine_error", "combat", f"{type(ex).__name__}: {ex}")
                return {"cleared": cleared, "won": False, "invalid": True,
                        "reason": f"combat: {ex}"}
            if not [x for x in e.state.enemies if x.is_alive]:
                break
            mp = _resolve_monster_turn(e)
            if not mp.get("success"):
                return {"cleared": cleared, "won": False, "invalid": True,
                        "reason": f"monster_phase: {mp.get('error')}"}
            if mp["result"].get("player_dead"):
                break
            e.execute_action("round_end", {})

        if not e.state.player or not e.state.player.is_alive:
            return {"cleared": cleared, "won": False, "invalid": False}
        if [x for x in e.state.enemies if x.is_alive]:
            return {"cleared": cleared, "won": False, "invalid": False}
        ended = e.execute_action("battle_end", {})
        if not ended.get("success"):
            return {"cleared": cleared, "won": False, "invalid": True,
                    "reason": f"battle_end: {ended.get('error')}"}
        cleared += 1

    return {"cleared": cleared, "won": True, "invalid": False}


# 局外行动权重：AI 按此概率挑选。7项为引擎当前可用行动
# （忘忧/献祭需道具，雇佣仅罪孽都市，维修仅扭曲都市，炼心仅龙心谷）
DEFAULT_POLICY = {
    "修行": 30, "学习": 25, "休整": 15, "共鸣": 10,
    "探索": 8, "领悟": 6, "炼心": 2, "维修": 2, "雇佣": 2,
}

REGION_ACTION = {"炼心": "龙心谷", "维修": "扭曲都市", "雇佣": "罪孽都市"}


def choose_pre_battle(e, todo, battle_no, rng, policy):
    """AI 自主挑选一个局外行动（按权重），返回 (行动名, 参数)。"""
    p = e.state.player
    cands = []
    for act, w in policy.items():
        need = REGION_ACTION.get(act)
        if need and e.state.current_region != need:
            continue
        if act == "学习" and not todo:
            continue
        if act == "休整" and p and p.current_hp >= p.blood_limit:
            continue          # 满血不休整（无效行动，不该计入选择率）
        cands.append((act, w))
    if not cands:
        return "修行", {"tier": 1, "to": "mana"}

    total = sum(w for _, w in cands)
    pick = rng.uniform(0, total)
    acc = 0
    act = cands[-1][0]
    for a, w in cands:
        acc += w
        if pick <= acc:
            act = a
            break

    if act == "学习":
        return act, {"sub": "daowen", "name": todo[0]}
    if act == "修行":
        return act, {"tier": 1, "to": "mana" if battle_no % 2 else "speed"}
    if act == "休整":
        return act, {"tier": 1}
    if act == "领悟":
        return act, {"resonance_type": rng.choice(["转换", "反转", "曲解"])}
    if act == "维修":
        return act, {"tier": 1}
    if act == "雇佣":
        return act, {"name": f"雇员{rng.randrange(1000)}", "blood_alloc": 8, "atk_bundles": 4}
    return act, {}


def fitness(starter: str, learn: list, runs: int, gen: int,
            random_seeds: bool = False, rng: random.Random = None,
            telemetry: dict = None) -> tuple:
    """
    适应度 = 平均通关场数 + 3×胜率（0~10）。

    random_seeds=False（默认）：种子由代数推导，同一代可复现，便于排查。
    random_seeds=True：每局用真随机种子与随机副本，样本不重复，
      能避免"只在某几局上表现好"的过拟合，代价是结果不可逐局复现。

    返回 (score, valid_runs, invalid_runs)。
    出现引擎异常的对局视为**无效数据**，不计入分数与统计。
    """
    # 非随机模式必须完全可复现：局外行动的挑选也要用确定性 rng，
    # 否则同参数两次评估会因决策不同而给出不同分数。
    if rng is None:
        rng = random if random_seeds else random.Random(gen * 7919 + 13)
    total = 0.0
    valid = 0
    invalid = 0
    for i in range(runs):
        if random_seeds:
            seed = rng.randrange(1, 2 ** 31 - 1)
            region = rng.choice(REGIONS)
        else:
            seed = gen * 1000 + i * 7 + 1
            region = REGIONS[i % len(REGIONS)]
        r = play(starter, learn, region, seed, rng=rng, telemetry=telemetry)
        if r.get("invalid"):
            invalid += 1
            if telemetry is not None:
                telemetry.setdefault("invalid_reasons", {})
                key = str(r.get("reason"))[:80]
                telemetry["invalid_reasons"][key] = telemetry["invalid_reasons"].get(key, 0) + 1
            continue
        valid += 1
        total += r["cleared"] + (3.0 if r["won"] else 0.0)
        if telemetry is not None:
            telemetry.setdefault("outcomes", {"win": 0, "loss": 0, "cleared_sum": 0})
            telemetry["outcomes"]["win" if r["won"] else "loss"] += 1
            telemetry["outcomes"]["cleared_sum"] += r["cleared"]
            telemetry.setdefault("region_runs", {})
            telemetry["region_runs"][region] = telemetry["region_runs"].get(region, 0) + 1
    return (total / valid if valid else 0.0), valid, invalid


# --------------------------------------------------------------------------
# 知识库
# --------------------------------------------------------------------------

def load() -> dict:
    if os.path.exists(KNOWLEDGE):
        with open(KNOWLEDGE, encoding="utf-8") as f:
            return json.load(f)
    return {"generation": 0, "trials": {}, "pair_scores": {}, "history": [], "best": None}


def save(k: dict) -> None:
    os.makedirs(os.path.dirname(KNOWLEDGE), exist_ok=True)
    with open(KNOWLEDGE, "w", encoding="utf-8") as f:
        json.dump(k, f, ensure_ascii=False, indent=1)


def ucb(k: dict, name: str, total_n: int) -> float:
    """UCB1：平衡"已知高分"与"尝试次数少"。"""
    t = k["trials"].get(name)
    if not t or t["n"] == 0:
        return 1e9                      # 没试过的优先试
    mean = t["sum"] / t["n"]
    return mean + 1.4 * math.sqrt(math.log(max(total_n, 2)) / t["n"])


def propose(k: dict, rng: random.Random, region: str = None) -> tuple:
    """生成下一套待测 build：50% 探索，50% 在精英基础上变异。
    region 给定时只从该副本实际可学的道纹中取（门禁修复后必需）。"""
    total_n = sum(t["n"] for t in k["trials"].values()) or 1
    CAND = learnable_candidates(region)
    best = k.get("best")
    if best and rng.random() < 0.5:
        learn = list(best["learn"])
        starter = best["starter"]
        # 变异：替换1~2个位置
        for _ in range(rng.randint(1, 2)):
            if learn:
                i = rng.randrange(len(learn))
                pool = [c for c in CAND if c not in learn and c != starter]
                if pool:
                    ranked = sorted(pool, key=lambda c: -ucb(k, c, total_n))
                    learn[i] = rng.choice(ranked[:8])
        if rng.random() < 0.25:
            starter = rng.choice(STARTERS)
        return starter, learn

    starter = rng.choice(STARTERS)
    pool = [c for c in CAND if c != starter]
    ranked = sorted(pool, key=lambda c: -ucb(k, c, total_n))
    head = ranked[:12]
    rng.shuffle(head)
    return starter, head[:BUILD_SIZE]


def update(k: dict, starter: str, learn: list, score: float) -> None:
    members = [starter] + list(learn)
    for m in members:
        t = k["trials"].setdefault(m, {"n": 0, "sum": 0.0})
        t["n"] += 1
        t["sum"] += score
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            key = "|".join(sorted((members[i], members[j])))
            p = k["pair_scores"].setdefault(key, {"n": 0, "sum": 0.0})
            p["n"] += 1
            p["sum"] += score
    if not k.get("best") or score > k["best"]["score"]:
        k["best"] = {"starter": starter, "learn": list(learn), "score": score}
    k["history"].append({"gen": k["generation"], "starter": starter,
                         "learn": list(learn), "score": round(score, 3)})
    k["total_games"] = k.get("total_games", 0) + k.get("_last_runs", 0)


def synergies(k: dict, min_n: int = 2) -> list:
    """协同增益：pair 均分 − 两个单体均分的平均。>0 即 1+1>2。"""
    out = []
    for key, p in k["pair_scores"].items():
        if p["n"] < min_n:
            continue
        a, b = key.split("|")
        ta, tb = k["trials"].get(a), k["trials"].get(b)
        if not ta or not tb or ta["n"] == 0 or tb["n"] == 0:
            continue
        solo = (ta["sum"] / ta["n"] + tb["sum"] / tb["n"]) / 2
        out.append((p["sum"] / p["n"] - solo, a, b, p["n"], p["sum"] / p["n"]))
    out.sort(reverse=True)
    return out


def report_telemetry(k: dict) -> None:
    """真实运行数据：局外行动选择率、成功率、失败原因、无效数据。"""
    t = k.get("telemetry") or {}
    att = t.get("attempted", {})
    suc = t.get("succeeded", {})
    if not att:
        return
    total = sum(att.values())
    print(f"\n【局外行动真实选择率】(共 {total} 次决策，由AI按权重自主选择)")
    print(f"  {'行动':<6}{'选择次数':>8}{'选择率':>9}{'成功率':>9}")
    for act, n in sorted(att.items(), key=lambda kv: -kv[1]):
        ok = suc.get(act, 0)
        print(f"  {act:<6}{n:>8}{n/total*100:>8.1f}%{ok/n*100:>8.1f}%")

    fails = t.get("failed", {})
    if fails:
        print("\n【行动失败原因 Top8】(合法拒绝，非bug)")
        for k2, n in sorted(fails.items(), key=lambda kv: -kv[1])[:8]:
            print(f"  {n:>5}× {k2}")

    oc = t.get("outcomes")
    if oc:
        tot = oc["win"] + oc["loss"]
        if tot:
            print(f"\n【对局结果】有效 {tot} 局｜通关 {oc['win']}｜阵亡 {oc['loss']}"
                  f"｜总胜率 {oc['win']/tot*100:.1f}%｜平均通关 {oc['cleared_sum']/tot:.2f} 场")
    rr = t.get("region_runs")
    if rr:
        print("  副本分布：" + "、".join(f"{a}{b}局" for a, b in sorted(rr.items())))

    err = t.get("engine_error", {})
    inv = t.get("invalid_reasons", {})
    print(f"\n【数据有效性】无效对局 {k.get('invalid_games', 0)} 局"
          f"（引擎异常，已从统计中剔除）")
    if err:
        print("  引擎异常明细（这些是bug，需修复）：")
        for k2, n in sorted(err.items(), key=lambda kv: -kv[1])[:10]:
            print(f"    {n:>4}× {k2}")
    if inv and not err:
        for k2, n in sorted(inv.items(), key=lambda kv: -kv[1])[:5]:
            print(f"    {n:>4}× {k2}")
    if not err and not inv:
        print("  ✅ 本批次未出现任何引擎异常，全部数据有效")


def report(k: dict) -> None:
    print(f"已学习代数：{k['generation']}｜累计试验：{len(k['history'])} 套"
          f"｜有效对局：{k.get('total_games', 0)} 局"
          f"｜无效(bug) {k.get('invalid_games', 0)} 局")
    if k.get("best"):
        b = k["best"]
        print(f"\n★ 目前最优：初始【{b['starter']}】+ {b['learn']}   适应度 {b['score']:.2f}/10")

    ranked = sorted(((t["sum"] / t["n"], n, t["n"])
                     for n, t in k["trials"].items() if t["n"] > 0), reverse=True)
    print("\n【单道纹价值 Top12】(平均适应度 × 试验次数)")
    for v, n, cnt in ranked[:12]:
        print(f"  {n:<6}{v:6.2f}  ({cnt}次)")
    if len(ranked) > 12:
        print("  ...最低3个：", "、".join(f"{n}{v:.2f}" for v, n, _ in ranked[-3:]))

    report_telemetry(k)

    syn = synergies(k)
    print("\n【协同增益 Top10  —— 1+1>2 的组合】")
    if not syn:
        print("  （数据不足，需要更多代数）")
    for d, a, b, n, avg in syn[:10]:
        print(f"  {a}+{b:<6} 增益{d:+.2f}  组合均分{avg:.2f} ({n}次)")
    neg = [s for s in syn if s[0] < 0]
    if neg:
        print("\n【负协同 —— 互相拖累，应避免同时携带】")
        for d, a, b, n, avg in neg[-5:]:
            print(f"  {a}+{b:<6} 增益{d:+.2f} ({n}次)")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--generations", type=int, default=10)
    ap.add_argument("--runs", type=int, default=6, help="每套build评估局数")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--seed", type=int, default=0,
                    help="控制'提出哪套build'的采样随机性；0=每次运行都不同")
    ap.add_argument("--random-seeds", action="store_true",
                    help="每局用真随机种子与随机副本（推荐，避免过拟合到固定局面）")
    a = ap.parse_args()

    if a.reset and os.path.exists(KNOWLEDGE):
        os.remove(KNOWLEDGE)
        print("已清空知识库")

    k = load()
    if a.report:
        report(k)
        return

    rng = random.Random(a.seed or None)
    tele = k.setdefault("telemetry", {})
    for g in range(a.generations):
        k["generation"] += 1
        starter, learn = propose(k, rng)
        score, valid, invalid = fitness(starter, learn, a.runs, k["generation"],
                                        random_seeds=a.random_seeds, rng=rng,
                                        telemetry=tele)
        k["total_games"] = k.get("total_games", 0) + valid
        k["invalid_games"] = k.get("invalid_games", 0) + invalid
        if valid:                      # 全部无效的代不计入学习，避免污染权重
            update(k, starter, learn, score)
        star = " ★新最优" if k.get("best") and k["best"]["score"] == score else ""
        bad = f"  [无效{invalid}]" if invalid else ""
        print(f"第{k['generation']:>3}代  【{starter}】{'+'.join(learn):<28} → {score:5.2f}{star}{bad}")
        save(k)

    print()
    report(k)
    save(k)


if __name__ == "__main__":
    main()
