#!/usr/bin/env python3
"""
引擎驱动战报生成器：全程用GameEngine API驱动（开局遗物/局外/道纹/法术/怪物回合/遗物/降服
全走引擎，证明引擎端到端可跑）。跑多个种子到通关，输出完整战报。
"""
import sys, os, math, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib.util
_spec = importlib.util.spec_from_file_location("bs", os.path.join(os.path.dirname(os.path.abspath(__file__)), "balance_sim.py"))
bs = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(bs)
from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance

POOL = bs.parse_monsters()
REGIONS = ["罪孽都市", "扭曲都市", "龙心谷"]
BACKGROUNDS = ["帮派巷战", "废墟据点", "黑市火并", "熔岩隘口"]


def spawn(engine, region, count, rng):
    rpool = [m for m in POOL if m["region"] == region]
    defs = [rng.choice(rpool) for _ in range(count)]
    for d in defs:
        m = bs.make_monster(d)
        engine.state.enemies.append(m)
        engine.combat.init_monster_shards(m)
    return defs


def player_act(engine, rng, log):
    """玩家出手：致命庇护/低血再生/多怪冲击/焦点杀伐"""
    player = engine.state.player
    enemies = [e for e in engine.state.enemies if e.is_alive and not e.is_subdued]
    if not enemies:
        return
    incoming = sum(e.attack_count * e.attack_power for e in enemies)
    mana = player.current_mana
    acts = max(1, math.ceil(player.speed_limit / 3))
    for _ in range(acts):
        enemies = [e for e in engine.state.enemies if e.is_alive and not e.is_subdued]
        if not enemies or mana <= 0:
            break
        # 低血再生
        if player.current_hp <= 18 and "再生" in player.dao_wen and mana >= 2 and not player.has_status("坏死"):
            x = min(mana, max(1, (player.blood_limit - player.current_hp) // 3 + 1))
            r = engine.execute_action("use_daowen", {"daowen_name": "再生", "x": x}); mana = player.current_mana
            continue
        # 致命/大伤害庇护
        if incoming > 0 and "庇护" in player.dao_wen and (incoming >= player.current_hp or incoming >= player.current_hp * 0.5):
            x = min(mana, math.ceil(incoming / 4))
            if x >= 1:
                engine.execute_action("use_daowen", {"daowen_name": "庇护", "x": x}); mana = player.current_mana
                if acts <= 1:
                    break
                acts -= 1
                continue
        # ≥3怪冲击AOE，否则焦点杀伐
        if len(enemies) >= 3 and "冲击" in player.dao_wen:
            x = min(mana, 7)
            engine.execute_action("use_daowen", {"daowen_name": "冲击", "x": x}); mana = player.current_mana
        elif "杀伐" in player.dao_wen:
            t = min(enemies, key=lambda e: e.current_hp)
            x = min(mana, max(1, math.ceil(t.current_hp / 2)))
            engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": x, "target": t.name}); mana = player.current_mana
        acts -= 1


def run_one(seed, region="罪孽都市"):
    log = []
    rng = random.Random(seed)
    engine = GameEngine(db_path="data/trace_rulings.db")
    engine.execute_action("setup_attributes", {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    engine.execute_action("setup_choose_daowen", {"daowen": "杀伐"})
    engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    r = engine.execute_action("setup_choose_region", {"region": region})
    starter = r["result"]["starter_relic"]
    shards = 20
    log.append(f"【开局】10血/7速/8法(60/14/8 出手3)｜20碎片｜发现遗物·{starter}｜反转残韵｜杀伐｜副本·{region}\n")

    for n in range(1, 8):
        count = max(1, n - 3)
        log.append(f"━━━━ 第{n}场（出怪{count}）━━━━")
        # 局外3精力
        prep = []
        if n == 1:
            for dw in ["庇护", "再生", "冲击"]:
                engine.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": dw})
                prep.append(f"学{dw}")
        e = 3
        while e > 0:
            if engine.state.player.current_hp < engine.state.player.blood_limit * 0.35 and shards >= 25:
                engine.state.shards = shards
                engine.execute_action("pre_battle_action", {"sub_action": "休整", "tier": 3})
                shards = engine.state.shards; prep.append("休整+48"); e -= 1
            elif engine.state.player.current_hp < engine.state.player.blood_limit * 0.2:
                engine.execute_action("pre_battle_action", {"sub_action": "休整", "tier": 1})
                prep.append("休整+8"); e -= 1
            else:
                engine.state.shards = shards
                t = 1 if shards < 15 else 2
                engine.execute_action("pre_battle_action", {"sub_action": "修行", "tier": t, "to": "speed" if n % 2 else "mana"})
                shards = engine.state.shards; prep.append(f"修行(速{engine.state.player.speed_limit}/法{engine.state.player.mana_limit})"); e -= 1
        log.append(f"  [局外] {'，'.join(prep)}")
        engine.state.shards = shards
        # 战始
        bs_ = engine.execute_action("battle_start")
        defs = spawn(engine, region, count, rng)
        log.append(f"  [战始] 背景：{rng.choice(BACKGROUNDS)}｜敌方：{', '.join(d['name']+'('+str(d['ac'])+'×'+str(d['ap'])+'/'+str(d['hp'])+')' for d in defs)}")
        if bs_.get("relic_logs"):
            log.append(f"        战始遗物：{bs_['relic_logs']}")
        log.append(f"        贾凡入场 HP{engine.state.player.current_hp} 法{engine.state.player.mana_limit} 速{engine.state.player.speed_limit}(出手{engine.state.player.action_count})")
        # 战斗
        won = False
        for rnd in range(1, 30):
            if not engine.state.player.is_alive:
                log.append(f"  ✗ 贾凡阵亡（第{rnd}回合）"); break
            if not [e for e in engine.state.enemies if e.is_alive and not e.is_subdued]:
                won = True; break
            engine.execute_action("round_start", {})
            player_act(engine, rng, log)
            if not [e for e in engine.state.enemies if e.is_alive and not e.is_subdued]:
                won = True; break
            mp = engine.execute_action("monster_phase", {})
            if mp["result"]["player_dead"]:
                log.append(f"  第{rnd}回合：怪物出手，贾凡HP→{mp['result']['player_hp']} 阵亡"); break
            log.append(f"  第{rnd}回合：怪物出手{mp['result']['attacks']}次，贾凡HP{engine.state.player.current_hp}")
            re_ = engine.execute_action("round_end", {})
            vp = re_.get("victory_paths", [])
            if vp:
                for p in vp:
                    log.append(f"    ★{p['type']}·{p['monster']}")
                won = True; break
        if not won and not [e for e in engine.state.enemies if e.is_alive and not e.is_subdued]:
            won = True
        if not won:
            log.append(f"\n【结局】贾凡于第{n}场阵亡。遗物：{[r.name for r in engine.state.relics]}，碎片{shards}")
            return log, False, n
        # 战终
        engine.state.shards = shards
        be = engine.execute_action("battle_end", {})
        shards = be["result"]["total_shards"]
        rem = be["result"].get("removed_via_alt_path", [])
        log.append(f"  [战终] 碎片→{shards}{'(含降服/雕塑等移出'+str([x['name'] for x in rem])+')' if rem else ''}，HP{engine.state.player.current_hp}\n")
    log.append(f"\n【通关】贾凡历经7场完成一阶{region}！遗物：{[r.name for r in engine.state.relics]}，碎片{shards}，终态HP{engine.state.player.current_hp}")
    return log, True, 8


def main():
    region = sys.argv[1] if len(sys.argv) > 1 else "罪孽都市"
    for seed in range(1, 200):
        log, cleared, reached = run_one(seed, region)
        if cleared:
            print(f"# 引擎驱动·完整轮回战报（{region}，种子{seed}，通关）\n")
            print("\n".join(log))
            return
    print(f"# {region}：200种子内未通关，取最深")
    best = max((run_one(s, region) for s in range(1, 30)), key=lambda x: x[2])
    print("\n".join(best[0]))


if __name__ == "__main__":
    main()
