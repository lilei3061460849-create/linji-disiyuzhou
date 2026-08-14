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
from engine.ai_tactics import TacticalAI

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
        relic_choices = {}
        if starter in optional:
            relic_choices[starter] = {"use": False}
        bs = engine.execute_action("battle_start", {"relic_choices": relic_choices})
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
            rs = engine.execute_action("round_start", {"relic_choices": ({"血契": {"use": False}} if any(r.name == "血契" for r in engine.state.relics) else {})})
            out.extend(BR.format_round_start(rnd, rs.get("result", {}),
                                             engine.state.player, engine.state.enemies))

            # 我方出手：由 TacticalAI 按优先级决策（保命/残韵/控场/收割/AOE/续航）
            idx = 1
            for res in ai.take_turn():
                out.extend(BR.format_player_action(idx, engine.state.player.name, res))
                idx += 1

            if not [e for e in engine.state.enemies if e.is_alive]:
                out.extend(BR.format_round_end({}, engine.state.player, engine.state.enemies))
                break
            if not engine.state.player.is_alive:
                out.extend(BR.format_round_end({}, engine.state.player, engine.state.enemies))
                out.append("")
                out.append("【结局】轮回者[命零]")
                break

            prepared = engine.execute_action("prepare_monster_phase", {})
            monster_choices = []
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
                    if option["name"] == "活力":
                        action_count += option["x"]
                    elif option["name"] == "狂暴":
                        action_count += 1
                    elif option["name"] == "变形":
                        enemy_index = int(actor["actor_ref"].split(":", 1)[1])
                        hit_count = engine.state.enemies[enemy_index].attack_power
                target_ref = actor["attack_target_options"][0]["ref"]
                target_option = next(option for option in actor["attack_target_options"] if option["ref"] == target_ref)
                attacks = [{"hits": [{"target_ref": target_ref, "dodge": False, "blood_shadow": False,
                                       "spell_choices": _decline_spells(target_option)}
                                      for _ in range(hit_count)]}
                           for _ in range(action_count)]
                monster_choices.append({"actor_ref": actor["actor_ref"], "daowen": dao,
                                        "attack_actions": attacks})
            mp = engine.execute_action("resolve_monster_phase", {
                "token": prepared["result"]["token"], "choices": monster_choices,
            })
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
