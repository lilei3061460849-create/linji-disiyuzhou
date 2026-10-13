"""第十九批 Part1+2：第七场死亡归因 + 消耗品影子策略（规则零改动，影子只动AI时机）。

Part2 四臂（同一主种子流=逐局配对，同构筑同种子唯一变量=消耗品门）：
  A current  现行 ≤40%血线门（预演风险过滤始终保留）
  B hp40     协议字面复刻B——经代码审计 B≡A（不占出手+本就回合初先试），不重复跑
  C hp60     ≤60%血线门
  D predict  上回合净掉血≥当前HP时提前用药
构筑=生产KB确认精英池前2名（封印/杀伐/再生/庇护 + 封印/杀伐/再生/增殖），
区域轮转，policy= learned_policy(生产KB) 全臂共享。
--trace 仅A臂开：收集第七场失败局完整死亡归因（Part1数据集）。

Part1 指标：b7失败主因Pareto（burst/sustained/rng_suspect/mana_exhaust/build_disadv/other）
+ 旗标（带药/蓝尽/速度劣势）+ 杀手道纹频率。
Part2 指标：死亡率/第七场死亡率/全清率/消耗品实际使用率/带药死亡比例。
"""
import argparse
import json
import random
import sys

sys.path.insert(0, "/home/user/linji-disiyuzhou")
from sim import build_learner as bl


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", required=True,
                    choices=["current", "hp40", "hp60", "predict"])
    ap.add_argument("--games", type=int, default=600)
    ap.add_argument("--seed", type=int, default=77001)
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--kb", default="data/build_knowledge.json")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    with open(a.kb, encoding="utf-8") as f:
        k = json.load(f)
    pol = bl.learned_policy(k)
    builds = [[kk[0], list(kk[1])]
              for _, kk in bl.build_scoreboard(k, 2)[:2]]
    print(f"[{a.policy}] 构筑={builds} policy={pol}", flush=True)

    rng = random.Random(a.seed)
    n = inv = cleared_sum = b1 = fc = deaths = 0
    b7_deaths = []
    death_battle_hist = {}
    use_games = 0          # 本局消耗品实际使用>0
    use_total = 0
    dead_with_items = 0    # 带药阵亡（死亡时仍有可用消耗品）
    killers = {}
    for i in range(a.games):
        starter, learn = builds[i % len(builds)]
        region = bl.REGIONS[i % len(bl.REGIONS)]
        gs = rng.randrange(1, 2 ** 31 - 1)
        r = bl.play(starter, learn, region, gs, rng=random.Random(gs),
                    spend_shards=True, policy=pol,
                    consumable_policy=a.policy,
                    death_trace=a.trace)
        if r.get("invalid"):
            inv += 1
            continue
        n += 1
        c = r["cleared"]
        cleared_sum += c
        b1 += c >= 1
        fc += c == 7
        died = c < 7
        if died:
            deaths += 1
            b_of_death = c + 1
            death_battle_hist[b_of_death] = death_battle_hist.get(b_of_death, 0) + 1
            pm = r.get("pm") or {}
            if pm.get("unused_items"):
                dead_with_items += 1
            if pm.get("killer"):
                killers[pm["killer"]] = killers.get(pm["killer"], 0) + 1
            if b_of_death == 7 and r.get("death_trace"):
                b7_deaths.append({"trace": r["death_trace"],
                                  "killer": pm.get("killer", "?"),
                                  "final_daowen": r.get("final_daowen", [])})
        u = r.get("consumable_uses", 0)
        use_total += u
        use_games += u > 0
        if (i + 1) % 200 == 0:
            print(f"  [{a.policy}] {i+1}/{a.games} 均{cleared_sum/max(1,n):.3f} "
                  f"b7死 {len(b7_deaths) if a.trace else '-'}", flush=True)
    out = {"policy": a.policy, "games": n, "invalid": inv,
           "avg": round(cleared_sum / max(1, n), 3),
           "b1": round(b1 / max(1, n), 3), "full_clear": fc,
           "deaths": deaths,
           "death_rate": round(deaths / max(1, n), 3),
           "death_battle_hist": death_battle_hist,
           "b7_death_rate": round(
               death_battle_hist.get(7, 0) / max(1, n), 4),
           "use_games": use_games, "use_total": use_total,
           "use_rate_games": round(use_games / max(1, n), 3),
           "dead_with_items": dead_with_items,
           "dead_with_items_share": round(dead_with_items / max(1, deaths), 3),
           "killers": dict(sorted(killers.items(), key=lambda kv: -kv[1])[:15]),
           "b7_death_traces": b7_deaths}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps({k2: v for k2, v in out.items()
                      if k2 != "b7_death_traces"}, ensure_ascii=False), flush=True)
    print(f"b7_traces={len(b7_deaths)}", flush=True)


if __name__ == "__main__":
    main()
