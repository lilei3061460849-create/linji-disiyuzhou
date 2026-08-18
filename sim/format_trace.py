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

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine import battle_report as BR
from engine.ai_tactics import TacticalAI
from sim.build_learner import _resolve_monster_turn as _resolve_monster_turn

BACKGROUNDS = ["帮派巷战", "废墟据点", "黑市火并", "熔岩隘口"]


def _decline_spells(option):
    return {timing: {spell["spell_name"]: {"use": False}
                     for spell in option.get("spell_options", {}).get(timing, [])}
            for timing in ("before", "after")}

def run(region: str = "龙心谷", seed: int = 7, battles: int = 3) -> list[str]:
    rng = random.Random(seed)
    # rng_seed 交给引擎自身的随机源，保证同一 seed 产出完全一致的战报（可复现）
    engine = GameEngine(db_path="/tmp/format_trace.db", rng_seed=seed)
    engine.execute_action("setup_attributes",
                          {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    r = engine.execute_action("setup_choose_region", {"region": region})
    optional = {"折速法印", "三相残韵盘"}
    starter = next((name for name in r["result"]["relic_choices"] if name not in optional),
                   r["result"]["relic_choices"][0])
    engine.execute_action("choose_discovered_relic", {"relic_name": starter})
    ai = TacticalAI(engine)

    # 局外：学习道纹与法术，让 AI 有牌可打（否则只能发初始道纹）
    for dw in ("庇护", "再生", "冲击"):
        engine.execute_action("pre_battle_action",
                              {"sub_action": "学习", "sub": "daowen", "name": dw})
    for sp in ("后发制人", "生生不息"):
        engine.execute_action("pre_battle_action",
                              {"sub_action": "学习", "sub": "spell", "name": sp})

    out = [f"# 战报（{region}，种子{seed}）· 按 README《六、战斗推演格式》",
           "",
           f"【开局】25点属性→{engine.state.player.blood_limit}[血限]/"
           f"{engine.state.player.mana_limit}[法限]/{engine.state.player.speed_limit}[速限]"
           f"｜20[碎片]｜发现遗物·{starter}｜残韵·反转｜初始道纹·杀伐｜副本·{region}"]

    for battle_no in range(1, battles + 1):
        engine.state.energy = 0
        from sim.optional_actions import battle_start_relic_choices
        bs = engine.execute_action("battle_start",
                                   {"relic_choices": battle_start_relic_choices(engine)})
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
            from sim.build_learner import round_start_relic_choices
            rs = engine.execute_action("round_start", {"relic_choices": round_start_relic_choices(engine)})
            out.extend(BR.format_round_start(rnd, rs.get("result", {}),
                                             engine.state.player, engine.state.enemies))

            # 我方出手：由 TacticalAI 按优先级决策（保命/残韵/控场/收割/AOE/续航）
            idx = 1
            for res in ai.take_turn():
                out.extend(BR.format_player_action(idx, engine.state.player.name, res))
                idx += 1
            # [朋友]/[员工]自主出手
            ap = engine.execute_action("resolve_ally_phases", {})
            for entry in (ap.get("result", {}).get("allies") or []):
                for act in entry.get("actions", []):
                    out.extend(BR.format_player_action(idx, entry["ally"], act.get("detail") or {}))
                    idx += 1

            if not [e for e in engine.state.enemies if e.is_alive]:
                out.extend(BR.format_round_end({}, engine.state.player, engine.state.enemies))
                break
            if not engine.state.player.is_alive:
                out.extend(BR.format_round_end({}, engine.state.player, engine.state.enemies))
                out.append("")
                out.append("【结局】轮回者[命零]")
                break

            mp = _resolve_monster_turn(engine)
            if not mp.get("success"):
                out.append(f"【怪物阶段失败】{mp.get('error')}")
                return out
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
