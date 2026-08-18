"""故事文档候选战报·引擎手操驱动器二号(2026-08-17)。

逐步通过 GameEngine.execute_action 点选,记录全部真实输入输出到 /tmp/story_log2.json。
角色:秦无衣(7血/8速/10法=42血限/8速限/20法限),初始道纹·杀伐,残韵·曲解,副本·罪孽都市。
局外:学习庇护(0碎片)+学习再生(10碎片)+领悟转换。
战术修正(相对一号驱动):敌方【贯穿】生效后不再立盾、改为速度闪避;濒血用【再生】;
怪物道纹按自身利益优先级出手(疯狂已按2026-08-17全局裁定无需目标)。
"""
import json
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from sim.optional_actions import battle_start_relic_choices, round_start_relic_choices

LOG = []


def act(e, action, params):
    r = e.execute_action(action, params)
    LOG.append({"action": action, "params": params, "response": r})
    return r


MONSTER_DAO_PRIORITY = ["贯穿", "洗劫", "疯狂", "狂暴", "强化", "必中", "自愈", "飞行"]


def main():
    e = GameEngine(db_path="/tmp/story2_dm.db", rng_seed=20260817,
                   sealed_candidate_path="/tmp/story2_sealed.json")
    act(e, "setup_attributes", {"name": "秦无衣", "blood_points": 7, "speed_points": 8, "mana_points": 10})
Nonefinish_initial_daowen(e)
    act(e, "setup_choose_resonance", {"resonance_type": "反转"})
    s = act(e, "setup_choose_region", {"region": "罪孽都市"})
    act(e, "choose_discovered_relic", {"relic_name": s["result"]["relic_choices"][0]})

    # ---- 局外(3精力):学习庇护(0) / 学习再生(10) / 领悟转换 ----
    act(e, "pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "庇护"})
    act(e, "pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "再生"})
    act(e, "pre_battle_action", {"sub_action": "领悟", "resonance_type": "转换"})

    # ---- 战始 ----
    act(e, "battle_start", {"relic_choices": battle_start_relic_choices(e)})

    p = e.state.player
    while any(m.is_alive for m in e.state.enemies) and p.is_alive:
        act(e, "round_start", {"relic_choices": round_start_relic_choices(e)})

        # ---- 残韵反转陷阱:敌方持有【疯狂】时,预埋反转,将其激活烧成【无力】 ----
        if e.state.resonance.get("反转", 0) > 0:
            for idx, m in enumerate(e.state.enemies):
                if m.is_alive and "疯狂" in m.dao_wen:
                    trap = act(e, "use_resonance", {"source_daowen": "疯狂",
                                                    "resonance_type": "反转",
                                                    "target_ref": f"enemy:{idx}"})
                    if not trap.get("success"):
                        break
                    break

        # ---- 我方出手(逐次点选) ----
        while p.actions_used_this_round < p.action_count and any(m.is_alive for m in e.state.enemies):
            alive = [m for m in e.state.enemies if m.is_alive and e.combat.is_targetable(p, m)]
            if not alive:
                break
            target = min(alive, key=lambda m: m.current_hp)
            t_ref = f"enemy:{e.state.enemies.index(target)}"
            # 最优解(一号/二号驱动的教训):对高血量竞速型怪物,防御税拖慢斩杀线。
            # 全速闪避免费吃伤害,法力全灌杀伐;仅在速度不足以闪掉的本回合来伤
            # 将致命时,精准【再生】一次续命,其余全部进攻。
            incoming = 0
            for m in e.state.enemies:
                if not m.is_alive:
                    continue
                v = 2 if (m.get_status_value("疯狂") or
                          ("疯狂" in m.dao_wen and e.state.current_round >= 2)) else 0
                incoming += max(0, (1 + v) * m.attack_count - p.current_speed) * m.attack_power
            if (incoming > p.current_hp + p.shield and "再生" in p.dao_wen
                    and p.current_mana >= 6 and p.current_hp < p.blood_limit
                    and not p.has_status("坏死")):
                r = act(e, "use_daowen", {"daowen_name": "再生", "x": 6, "target": p.name})
                if not r.get("success") or p.current_hp >= p.blood_limit:
                    pass  # 满血或被拒即止,避免重复施法死循环
                else:
                    continue
            if p.current_mana <= 0:
                break  # 法力耗尽,无合法进攻手段,余手放弃
            rem = max(1, p.action_count - p.actions_used_this_round)
            spend = max(1, min(p.current_mana, p.current_mana // rem))
            act(e, "use_daowen", {"daowen_name": "杀伐", "x": spend, "target_ref": t_ref})

        if not any(m.is_alive for m in e.state.enemies):
            act(e, "round_end", {})
            break

        # ---- 怪物回合 ----
        prepared = act(e, "prepare_monster_phase", {})
        if not prepared.get("success"):
            break
        choices_out = []
        for actor in prepared["result"]["actors"]:
            refs = e.combat._combat_entity_refs()
            monster = refs.get(actor["actor_ref"])
            per_hit = monster.attack_power if monster else 0
            pierce = bool(monster and monster.has_status("贯穿"))
            dao = None
            if actor["daowen_options"]:
                names = [o["name"] for o in actor["daowen_options"]]
                option = next((o for o in sorted(actor["daowen_options"],
                                                 key=lambda o: MONSTER_DAO_PRIORITY.index(o["name"])
                                                 if o["name"] in MONSTER_DAO_PRIORITY else 99)),
                              actor["daowen_options"][0])
                dao = {"name": option["name"], "dodge": False, "blood_shadow": False,
                       "trigger_spell_choices": {holder: {sp["spell_name"]: {"use": False}
                                                          for sp in spells}
                                                 for holder, spells in option.get("trigger_spell_options", {}).items()}}
                if option.get("requires_target"):
                    dao["target_ref"] = option["target_options"][0]["ref"]
                if option.get("dodge_submission") == "per_target":
                    dao["dodge_targets"] = [{"target_ref": t["ref"], "dodge": False, "blood_shadow": False}
                                            for t in option.get("dodge_target_options", [])]
            attacks = []
            shield_sim = p.shield
            for _ in range(actor["base_attack_actions"]):
                hits = []
                for tgt in actor["attack_target_options"]:
                    decline = {timing: {sp["spell_name"]: {"use": False}
                                        for sp in tgt.get("spell_options", {}).get(timing, [])}
                               for timing in ("before", "after")}
                    for _ in range(actor["base_hits_per_attack"]):
                        # 闪避声明:速度在,一切皆闪(免费减伤,法力留给杀伐)
                        dodge = tgt["ref"] == "player:0" and p.current_speed > 0
                        if tgt["ref"] == "player:0" and not dodge:
                            shield_sim = max(0, shield_sim - per_hit)
                        hits.append({"target_ref": tgt["ref"], "dodge": dodge, "blood_shadow": False,
                                     "spell_choices": decline})
                attacks.append({"hits": hits})
            choices_out.append({"actor_ref": actor["actor_ref"], "daowen": dao, "attack_actions": attacks})
        act(e, "resolve_monster_phase", {"token": prepared["result"]["token"], "choices": choices_out})

        act(e, "round_end", {})

    result = act(e, "battle_end", {})
    print(json.dumps({"battle_end_ok": result.get("success"),
                      "battle_end": result.get("result", result.get("error")),
                      "shards": e.state.shards,
                      "player_hp": f"{p.current_hp}/{p.blood_limit}",
                      "alive": p.is_alive}, ensure_ascii=False, indent=2))
    with open("/tmp/story_log2.json", "w", encoding="utf-8") as f:
        json.dump(LOG, f, ensure_ascii=False, indent=1)
    print(f"LOG entries: {len(LOG)}")


if __name__ == "__main__":
    main()
