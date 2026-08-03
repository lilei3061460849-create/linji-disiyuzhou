#!/usr/bin/env python3
"""
7场全通关率模拟器（一阶副本）
- 出怪: 战斗场数-2最低1 → 1/1/1/2/3/4/5只
- HP/碎片跨场带；每场前局外(3精力)休整/学习；战终碎片奖励、临时朋友消失、精力回3
- 测真正"通关率"=7场全清的比例 + 最远场分布 + 各胜利路径使用次数

用法: python sim/run_sim.py [runs]
"""
import sys, os, math, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib.util
_spec = importlib.util.spec_from_file_location("bs", os.path.join(os.path.dirname(os.path.abspath(__file__)), "balance_sim.py"))
bs = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(bs)

from engine.models import Entity, GameState, DaoWen, DaoWenInstance
from engine.combat import CombatEngine
from engine.dice import DiceEngine

# 覆盖阈值（与combat.py一致）
bs.TUNING.update({"PROLIF_THRESHOLD":1.0,"DEBT_THRESHOLD":10,"TAMING_TURNS":3})

# ===== 可调设计参数（扫参用） =====
PARAMS = {
    "battle_offset": 3,      # 出怪 = max(1, n - offset)；2→1/1/1/2/3/4/5
    "rest_tier3": 48, "rest_tier3_cost": 25,
    "rest_tier2": 24, "rest_tier2_cost": 10,
    "rest_tier1": 8,
    "grow": "mix",           # 修行方向: speed/mana/mix
    "cap": 99,             # 每场怪物数上限
}


def make_run_player():
    # 真实开局：仅杀伐，60HP/14法/8速
    p = Entity(name="轮回者", entity_type="轮回者", blood_limit=60, current_hp=60,
               mana_limit=14, current_mana=14, speed_limit=8, current_speed=8,
               attack_count=1, attack_power=1)
    p.dao_wen["杀伐"] = bs.DaoWenInstance(
        dao_wen=DaoWen(name="杀伐",formula="",cost_type="消耗",cost_formula="X",effect_formula=""))
    return p


def has_dw(p, n): return n in p.dao_wen
def learn(p, n):
    p.dao_wen[n] = bs.DaoWenInstance(
        dao_wen=DaoWen(name=n,formula="",cost_type="消耗",cost_formula="X",effect_formula=""))

def cast_chongji(player, monsters, x):
    """冲击X：消耗X法力，对所有存活怪造成X伤害（蒙蔽下无效）"""
    if player.current_mana < x: return 0
    player.current_mana -= x
    if player.has_status("蒙蔽"):
        for st in player.status_effects:
            if st.name == "蒙蔽" and st.value > 0:
                st.value -= 1
                if st.value <= 0: player.status_effects.remove(st)
                break
        return 0
    tot = 0
    for m in monsters:
        if m.is_alive and not (m.is_subdued or m.is_sculptured or m.is_proliferated or m.is_debt_bound):
            m.current_hp = max(0, m.current_hp - x); tot += x
            if m.current_hp <= 0: m.is_alive = False
    return tot


def pre_battle_prep(player, shards, energy=3, battle_n=1):
    """局外：早期学核心道纹；之后休整(用碎片买大档)回血 + 修行成长。返回剩余碎片"""
    if battle_n == 1:
        for dw in ["庇护", "再生", "冲击"]:
            if not has_dw(player, dw) and energy > 0:
                learn(player, dw); energy -= 1
    while energy > 0:
        if player.current_hp < player.blood_limit:
            if player.current_hp <= player.blood_limit - PARAMS["rest_tier3"] and shards >= PARAMS["rest_tier3_cost"]:
                player.current_hp += PARAMS["rest_tier3"]; shards -= PARAMS["rest_tier3_cost"]; energy -= 1
            elif player.current_hp <= player.blood_limit - PARAMS["rest_tier2"] and shards >= PARAMS["rest_tier2_cost"]:
                player.current_hp += PARAMS["rest_tier2"]; shards -= PARAMS["rest_tier2_cost"]; energy -= 1
            else:
                player.current_hp = min(player.blood_limit, player.current_hp + PARAMS["rest_tier1"]); energy -= 1
        else:
            g = PARAMS["grow"]
            if g == "speed":
                player.speed_limit += 1; player.current_speed = player.speed_limit
            elif g == "mana":
                player.mana_limit += 2
            else:
                if battle_n % 2 == 0:
                    player.speed_limit += 1; player.current_speed = player.speed_limit
                else:
                    player.mana_limit += 2
            energy -= 1
    player.current_mana = player.mana_limit
    return shards


def alive_monsters(monsters):
    return [m for m in monsters if m.is_alive and not (m.is_subdued or m.is_sculptured
            or m.is_proliferated or m.is_debt_bound)]


def player_turn_multi(player, monsters, combat, rng):
    """多怪玩家回合：≥3怪用冲击AOE，否则焦点最低血怪；低血再生、大伤害庇护"""
    mana = player.current_mana
    actions = max(1, math.ceil(player.speed_limit / 3))
    alive = alive_monsters(monsters)
    if not alive: return
    incoming = sum(m.attack_count * m.attack_power for m in alive)
    if player.current_hp <= 0.35 * player.blood_limit and mana >= 2 and has_dw(player,"再生"):
        bs.cast_zaisheng(player, player, min(mana, 5)); mana = player.current_mana
    if incoming > 0 and has_dw(player,"庇护") and incoming >= player.current_hp * 0.3:
        bs.cast_bihu(player, min(mana, math.ceil(incoming/4))); mana = player.current_mana
    use_aoe = len(alive) >= 3 and has_dw(player,"冲击")
    while actions > 0 and mana > 0:
        alive = alive_monsters(monsters)
        if not alive: break
        if use_aoe and len(alive_monsters(monsters)) >= 2:
            cast_chongji(player, monsters, min(mana, 7)); mana = player.current_mana; actions -= 1
        else:
            target = min(alive_monsters(monsters), key=lambda m: m.current_hp)
            x = min(mana, max(1, math.ceil(target.current_hp/2)))
            bs.cast_shaifa(player, target, x); mana = player.current_mana; actions -= 1


def run_multi_battle(player, monster_defs, rng):
    """多怪战斗，mutate player。返回 {win, paths:[...]}"""
    region = monster_defs[0]["region"] if monster_defs else "扭曲都市"
    state = GameState(); state.current_region = region; state.player = player
    monsters = [bs.make_monster(md) for md in monster_defs]
    state.enemies = monsters
    combat = CombatEngine(state, DiceEngine())
    combat.PROLIFERATION_THRESHOLD = bs.TUNING["PROLIF_THRESHOLD"]
    combat.DEBT_THRESHOLD = bs.TUNING["DEBT_THRESHOLD"]
    combat.TAMING_REQUIRED_TURNS = bs.TUNING["TAMING_TURNS"]
    for m in monsters:
        combat.init_monster_shards(m)
    # 战始：速度复原、清除控场状态
    player.current_speed = player.speed_limit
    player.is_alive = True
    player.status_effects = []
    activated = {id(m): set() for m in monsters}
    paths_used = []
    max_rounds = 30
    for rnd in range(1, max_rounds+1):
        if not player.is_alive:
            return {"win": False, "paths": paths_used}
        if not alive_monsters(monsters):
            break
        # 回始
        player.current_mana = player.mana_limit
        player.shield = 0
        for m in monsters:
            if m.is_alive: bs.monster_round_start(m, activated[id(m)])
            if rnd > 1 and m.is_alive:
                act = bs.monster_activate(m, activated[id(m)], rng)
                if act: bs.apply_control_to_player(act, m, player)
        # 玩家出手
        player_turn_multi(player, monsters, combat, rng)
        if not alive_monsters(monsters):
            break
        # 所有怪攻击
        for m in monsters:
            if not m.is_alive or not player.is_alive: continue
            must_hit = "必中" in activated[id(m)]
            for _ in range(bs.get_monster_attack_actions(m, activated[id(m)])):
                if not player.is_alive: break
                bs.monster_attack_round(m, player, combat, rng, must_hit)
        if not player.is_alive:
            return {"win": False, "paths": paths_used}
        # 回终
        player.shield = 0; player.current_mana = 0
        for m in monsters: m.shield = 0
        settled = combat.settle_victory_paths()
        for s in settled: paths_used.append(s["type"])
        if not alive_monsters(monsters):
            return {"win": True, "paths": paths_used}
    return {"win": not alive_monsters(monsters) and player.is_alive, "paths": paths_used}


def battle_monster_count(n):
    c = max(1, n - PARAMS["battle_offset"])
    cap = PARAMS.get("cap", 99)
    return min(c, cap)


def run_full_run(rng, pool):
    player = make_run_player()
    shards = 20
    for n in range(1, 8):
        count = battle_monster_count(n)
        defs = [rng.choice(pool) for _ in range(count)]
        shards = pre_battle_prep(player, shards, energy=3, battle_n=n)
        res = run_multi_battle(player, defs, rng)
        if not res["win"]:
            return {"cleared": False, "reached": n, "paths": res["paths"], "final_hp": player.current_hp}
        # 战终碎片奖励（仅击杀，非杀伐移出）
        for md in defs:
            # 简化：每只击杀怪给 战始血限2%+道纹数5
            shards += math.ceil(md["hp"]*0.02) + len(md["dw"])*5
    return {"cleared": True, "reached": 8, "paths": [], "final_hp": player.current_hp}


def measure(runs, pool, seed=2026):
    rng = random.Random(seed)
    cleared = 0; reached_dist = {}; path_total = {}
    for _ in range(runs):
        r = run_full_run(rng, pool)
        if r["cleared"]: cleared += 1
        reached_dist[r["reached"]] = reached_dist.get(r["reached"], 0) + 1
        for p in r["paths"]: path_total[p] = path_total.get(p,0)+1
    return cleared/runs*100, reached_dist, path_total


def main():
    pool = bs.parse_monsters()
    args = sys.argv[1:]
    if args and args[0] == "sweep":
        # 扫参找30%：出怪offset × 修行方向
        runs = int(args[1]) if len(args) > 1 else 200
        print(f"扫参 (每次{runs}局)\n{'offset':>6}{'grow':>6} {'通关率':>7}  主死场")
        for off in [2, 3]:
            for grow in ["mix", "speed", "mana"]:
                PARAMS["battle_offset"] = off; PARAMS["grow"] = grow
                rate, dist, _ = measure(runs, pool)
                # 主死场
                dead = {k:v for k,v in dist.items() if k!=8}
                top = max(dead, key=dead.get) if dead else "-"
                print(f"{off:>6}{grow:>6} {rate:>6.1f}%  第{top}场" if top!="-" else f"{off:>6}{grow:>6} {rate:>6.1f}%")
        return
    runs = int(args[0]) if args else 300
    rate, reached_dist, path_total = measure(runs, pool)
    print(f"7场全通关率模拟  怪物池{len(pool)}只  跑{runs}次  offset={PARAMS['battle_offset']} grow={PARAMS['grow']}\n")
    print(f"=== 通关率（7场全清）: {rate:.1f}% ===\n")
    print("最远到达场分布:")
    for n in sorted(reached_dist):
        bar = "█" * int(reached_dist[n]/runs*50)
        tag = "通关" if n==8 else f"第{n}场败"
        print(f"  {tag}: {reached_dist[n]/runs*100:5.1f}%  {bar}")
    print(f"\n路径使用总次数: {path_total}")


if __name__ == "__main__":
    main()
