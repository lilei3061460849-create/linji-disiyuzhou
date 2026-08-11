#!/usr/bin/env python3
"""
按 README《六、战斗推演格式》生成合规战报。

与旧的 sim/engine_trace.py 的区别：engine_trace 输出的是汇总行
（"第1回合：怪物出手2次，贾凡HP60"），违反"禁止概括、跳过或合并结算"。
本脚本全程走 GameEngine API，并用 engine/battle_report.py 排版，
每一次出手逐条列出，数值全部来自引擎返回值。

用法：
    python3 sim/format_trace.py [副本名] [种子]
例：
    python3 sim/format_trace.py 龙心谷 7
"""
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine import battle_report as BR

BACKGROUNDS = ["帮派巷战", "废墟据点", "黑市火并", "熔岩隘口"]


def run(region: str = "龙心谷", seed: int = 7, battles: int = 3) -> list[str]:
    rng = random.Random(seed)
    # rng_seed 交给引擎自身的随机源，保证同一 seed 产出完全一致的战报（可复现）
    engine = GameEngine(db_path="/tmp/format_trace.db", rng_seed=seed)
    engine.execute_action("setup_attributes",
                          {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    engine.execute_action("setup_choose_daowen", {"daowen": "杀伐"})
    engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    r = engine.execute_action("setup_choose_region", {"region": region})
    starter = r["result"]["starter_relic"]

    out = [f"# 战报（{region}，种子{seed}）· 按 README《六、战斗推演格式》",
           "",
           f"【开局】25点属性→{engine.state.player.blood_limit}[血限]/"
           f"{engine.state.player.mana_limit}[法限]/{engine.state.player.speed_limit}[速限]"
           f"｜20[碎片]｜发现遗物·{starter}｜残韵·反转｜初始道纹·杀伐｜副本·{region}"]

    for battle_no in range(1, battles + 1):
        bs = engine.execute_action("battle_start")
        enemies = list(engine.state.enemies)
        draw_count = bs.get("draw_count", len(enemies))
        start_effects = list(bs.get("relic_logs", []) or []) + list(bs.get("artifact_logs", []) or [])
        out.append("")
        out.extend(BR.format_battle_start(
            battle_no=battle_no,
            draw_range=f"战斗场数{battle_no}，一阶副本-3最低为1，抽取{draw_count}只",
            draw_result="、".join(e.name for e in enemies),
            enemies=enemies,
            player=engine.state.player,
            allies=[a for a in engine.state.friends + engine.state.employees],
            background=rng.choice(BACKGROUNDS),
            start_effects=start_effects,
        ))

        for rnd in range(1, 12):
            alive = [e for e in engine.state.enemies if e.is_alive]
            if not alive or not engine.state.player.is_alive:
                break
            rs = engine.execute_action("round_start", {})
            out.extend(BR.format_round_start(rnd, rs.get("result", {}),
                                             engine.state.player, engine.state.enemies))

            # 我方出手：焦点杀伐，X 由当前法力决定（自由控X规则）
            idx = 1
            acts = max(1, math.ceil(engine.state.player.speed_limit / 3))
            for _ in range(acts):
                alive = [e for e in engine.state.enemies if e.is_alive]
                if not alive or engine.state.player.current_mana <= 0:
                    break
                tgt = min(alive, key=lambda e: e.current_hp)
                x = min(engine.state.player.current_mana, max(1, math.ceil(tgt.current_hp / 2)))
                res = engine.execute_action("use_daowen",
                                            {"daowen_name": "杀伐", "x": x, "target": tgt.name})
                if not res.get("success"):
                    out.append(f"出手{idx}（{engine.state.player.name}）：发动失败——{res.get('error')}")
                    idx += 1
                    break
                out.extend(BR.format_player_action(idx, engine.state.player.name, res))
                idx += 1

            if not [e for e in engine.state.enemies if e.is_alive]:
                out.extend(BR.format_round_end({}, engine.state.player, engine.state.enemies))
                break

            mp = engine.execute_action("monster_phase", {})
            out.extend(BR.format_monster_hits(idx, mp["result"].get("details", [])))

            re_ = engine.execute_action("round_end", {})
            out.extend(BR.format_round_end(re_.get("result", {}),
                                           engine.state.player, engine.state.enemies))
            if mp["result"].get("player_dead"):
                out.append("")
                out.append("【结局】轮回者[命零]")
                return out

        be = engine.execute_action("battle_end", {})
        out.extend(BR.format_battle_end(be.get("result", {})))

    return out


if __name__ == "__main__":
    region = sys.argv[1] if len(sys.argv) > 1 else "龙心谷"
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    print("\n".join(run(region, seed)))
