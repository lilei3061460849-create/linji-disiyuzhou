"""故事文档候选战报·引擎手操驱动器(2026-08-17)。

逐步通过 GameEngine.execute_action 点选,记录全部真实输入输出到 /tmp/story_log.json。
角色:宋昭(7血/8速/10法=42血限/8速限/20法限),初始道纹·杀伐,残韵·曲解,副本·罪孽都市。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from sim.optional_actions import battle_start_relic_choices, round_start_relic_choices

LOG = []


def act(e, action, params):
    r = e.execute_action(action, params)
    LOG.append({"action": action, "params": params, "response": r})
    return r


def main():
    e = GameEngine(db_path="/tmp/story_dm.db", rng_seed=20260817,
                   sealed_candidate_path="/tmp/story_sealed.json")
    act(e, "setup_attributes", {"name": "宋昭", "blood_points": 7, "speed_points": 8, "mana_points": 10})
    act(e, "setup_choose_resonance", {"resonance_type": "曲解"})
    s = act(e, "setup_choose_region", {"region": "罪孽都市"})
    choices = s["result"]["relic_choices"]
    act(e, "choose_discovered_relic", {"relic_name": choices[0]})

    # ---- 局外(3精力):学习庇护 / 领悟转换 / 修行1档(法) ----
    act(e, "pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "庇护"})
    act(e, "pre_battle_action", {"sub_action": "领悟", "resonance_type": "转换"})
    act(e, "pre_battle_action", {"sub_action": "修行", "tier": 1,
                                 "allocations": {"speed_points": 0, "mana_points": 1}})

    # ---- 战始 ----
    act(e, "battle_start", {"relic_choices": battle_start_relic_choices(e)})

    p = e.state.player
    round_no = 0
    while any(m.is_alive for m in e.state.enemies) and p.is_alive:
        round_no += 1
        act(e, "round_start", {"relic_choices": round_start_relic_choices(e)})

        # ---- 我方出手(逐次点选) ----
        while p.actions_used_this_round < p.action_count and any(m.is_alive for m in e.state.enemies):
            alive = [m for m in e.state.enemies if m.is_alive and e.combat.is_targetable(p, m)]
            if not alive:
                break
            target = min(alive, key=lambda m: m.current_hp)
            t_ref = f"enemy:{e.state.enemies.index(target)}"
            # 首手立盾:庇护;其余杀伐灌伤害
            if p.actions_used_this_round == 0 and "庇护" in p.dao_wen and p.current_mana >= 10:
                act(e, "use_daowen", {"daowen_name": "庇护", "x": 10, "target": p.name})
                continue
            rem = max(1, p.action_count - p.actions_used_this_round)
            spend = max(1, min(p.current_mana, p.current_mana // rem))
            act(e, "use_daowen", {"daowen_name": "杀伐", "x": spend, "target_ref": t_ref})

        if not any(m.is_alive for m in e.state.enemies):
            act(e, "round_end", {})
            break

        # ---- 怪物回合:prepare 后逐 actor 结算 ----
        prepared = act(e, "prepare_monster_phase", {})
        if not prepared.get("success"):
            break
        choices_out = []
        for actor in prepared["result"]["actors"]:
            refs = e.combat._combat_entity_refs()
            monster = refs.get(actor["actor_ref"])
            per_hit = monster.attack_power if monster else 0
            dao = None
            if actor["daowen_options"]:
                option = actor["daowen_options"][0]  # 怪物按自身利益激活首个合法道纹
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
            for _ in range(actor["base_attack_actions"]):
                hits = []
                for tgt in actor["attack_target_options"]:
                    decline = {timing: {sp["spell_name"]: {"use": False}
                                        for sp in tgt.get("spell_options", {}).get(timing, [])}
                               for timing in ("before", "after")}
                    for _ in range(actor["base_hits_per_attack"]):
                        # 闪避声明:重伤(>=10)或破盾伤必闪,轻伤吃下
                        dodge = (tgt["ref"] == "player:0" and per_hit >= 10 and p.current_speed > 0)
                        hits.append({"target_ref": tgt["ref"], "dodge": dodge, "blood_shadow": False,
                                     "spell_choices": decline})
                attacks.append({"hits": hits})
            choices_out.append({"actor_ref": actor["actor_ref"], "daowen": dao, "attack_actions": attacks})
        act(e, "resolve_monster_phase", {"token": prepared["result"]["token"], "choices": choices_out})

        act(e, "round_end", {})

    result = act(e, "battle_end", {})
    print(json.dumps({"battle_end": result, "shards": e.state.shards,
                      "player_hp": f"{p.current_hp}/{p.blood_limit}",
                      "alive": p.is_alive}, ensure_ascii=False, indent=2))
    with open("/tmp/story_log.json", "w", encoding="utf-8") as f:
        json.dump(LOG, f, ensure_ascii=False, indent=1)
    print(f"LOG entries: {len(LOG)}")


if __name__ == "__main__":
    main()
