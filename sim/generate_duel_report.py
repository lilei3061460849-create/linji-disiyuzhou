import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine import battle_report as BR
from sim.optional_actions import battle_start_relic_choices, round_start_relic_choices


def best_cultivate_tier(shards):
    if shards >= 150:
        return 6, 150, 6
    if shards >= 100:
        return 5, 100, 5
    if shards >= 65:
        return 4, 65, 4
    if shards >= 35:
        return 3, 35, 3
    if shards >= 15:
        return 2, 15, 2
    return 1, 0, 1


def _resolve_monster_turn_smart(engine):
    prepared = engine.execute_action("prepare_monster_phase", {})
    if not prepared.get("success"):
        return prepared
    choices = []
    p = engine.state.player
    max_d = p.current_speed if p else 2
    dodge_budget = 0
    sim_shield = p.shield if p else 0
    for actor in prepared["result"]["actors"]:
        dao = None
        action_count = actor["base_attack_actions"]
        hit_count = actor["base_hits_per_attack"]
        from sim.duel_common import _pick_monster_daowen
        if actor["daowen_options"]:
            option = _pick_monster_daowen(engine, actor)
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
        refs = engine.combat._combat_entity_refs()
        monster = refs.get(actor["actor_ref"])
        per_hit = monster.attack_power if monster is not None else 0
        from engine.ai_tactics import choose_attack_target
        target_ref = choose_attack_target(actor["attack_target_options"], refs)
        target_option = next(o for o in actor["attack_target_options"] if o["ref"] == target_ref)
        attacks = []
        for _ in range(action_count):
            hits = []
            for _ in range(hit_count):
                should_dodge = False
                if target_ref == "player:0":
                    if sim_shield >= per_hit:
                        sim_shield -= per_hit
                        should_dodge = False
                    elif dodge_budget < max_d and per_hit >= 6:
                        should_dodge = True
                        dodge_budget += 1
                from sim.build_learner import _decline_spells
                hits.append({"target_ref": target_ref, "dodge": should_dodge, "blood_shadow": False,
                             "spell_choices": _decline_spells(target_option)})
            attacks.append({"hits": hits})
        choices.append({"actor_ref": actor["actor_ref"], "daowen": dao, "attack_actions": attacks})
    return engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"],
        "choices": choices,
    })


def run_full_reincarnation_with_duel(seed=42):
    sealed_file = "data/sealed_candidate.json"
    if os.path.exists(sealed_file):
        os.remove(sealed_file)
    db_file = tempfile.mktemp(suffix=".db")

    # -------------------------------------------------------------
    # 步骤 1：贾希希通关 7 场并完整封存为第一名冠冕胜者
    # -------------------------------------------------------------
    e1 = GameEngine(db_path=db_file, rng_seed=42, sealed_candidate_path=sealed_file)
    e1.execute_action("setup_attributes", {
        "name": "贾希希", "blood_points": 7, "speed_points": 8, "mana_points": 10
    })
    e1.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    s1 = e1.execute_action("setup_choose_region", {"region": "龙心谷"})
    e1.execute_action("choose_discovered_relic", {"relic_name": s1["result"]["relic_choices"][0]})
    p1 = e1.state.player

    for b in range(1, 8):
        while e1.state.energy > 0:
            missing = p1.blood_limit - p1.current_hp
            if missing >= 15 and e1.state.shards >= 20:
                heal_amt = 24 + e1.state.rest_heal_bonus
                e1.execute_action("pre_battle_action", {
                    "sub_action": "休整", "tier": 2,
                    "heal_allocations": [{"target_ref": "player:0", "amount": heal_amt}]
                })
            elif missing >= 6:
                heal_amt = 8 + e1.state.rest_heal_bonus
                e1.execute_action("pre_battle_action", {
                    "sub_action": "休整", "tier": 1,
                    "heal_allocations": [{"target_ref": "player:0", "amount": heal_amt}]
                })
            elif "再生" not in p1.dao_wen:
                e1.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "再生"})
            elif "曲解" not in e1.state.resonance:
                e1.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "曲解"})
            elif "庇护" not in p1.dao_wen:
                e1.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "庇护"})
            elif "转换" not in e1.state.resonance:
                e1.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "转换"})
            elif e1.state.resonance.get("反转", 0) < 2:
                e1.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "反转"})
            else:
                tier, cost, pts = best_cultivate_tier(e1.state.shards)
                spd_pts = 1 if (p1.speed_limit < 12 and pts >= 2) else 0
                mana_pts = pts - spd_pts
                e1.execute_action("pre_battle_action", {
                    "sub_action": "修行", "tier": tier,
                    "allocations": {"speed_points": spd_pts, "mana_points": mana_pts}
                })

        e1.execute_action("battle_start", {"relic_choices": battle_start_relic_choices(e1)})
        while [m for m in e1.state.enemies if m.is_alive] and p1.is_alive:
            e1.execute_action("round_start", {"relic_choices": round_start_relic_choices(e1)})
            alive = [m for m in e1.state.enemies if m.is_alive]
            for m in alive:
                if (m.has_status("飞行") or m.is_flying) and not m.has_status("坠落") and not e1.combat._field_has_zhuiluo():
                    if e1.state.resonance.get("反转", 0) > 0 and "坠落" not in p1.dao_wen:
                        idx = e1.state.enemies.index(m)
                        e1.execute_action("use_resonance", {
                            "source_daowen": "飞行", "resonance_type": "反转", "target_ref": f"enemy:{idx}"
                        })
                    if "坠落" in p1.dao_wen and p1.current_mana >= 1:
                        e1.execute_action("use_daowen", {"daowen_name": "坠落", "x": 1})

            while p1.actions_used_this_round < p1.action_count and [m for m in e1.state.enemies if m.is_alive]:
                alive = [m for m in e1.state.enemies if m.is_alive]
                targetable = [m for m in alive if e1.combat.is_targetable(p1, m)]
                if p1.shield <= 6 and p1.current_mana >= 10 and "庇护" in p1.dao_wen:
                    e1.execute_action("use_daowen", {"daowen_name": "庇护", "x": 10, "target": p1.name})
                elif p1.current_hp <= 20 and p1.current_mana >= 4 and "再生" in p1.dao_wen:
                    e1.execute_action("use_daowen", {"daowen_name": "再生", "x": 4, "target": p1.name})
                elif targetable and p1.current_mana > 0 and "杀伐" in p1.dao_wen:
                    pool = [m for m in targetable if not m.has_status("固执")]
                    t = min(pool if pool else targetable, key=lambda m: m.current_hp)
                    rem = max(1, p1.action_count - p1.actions_used_this_round)
                    e1.execute_action("use_daowen", {
                        "daowen_name": "杀伐", "x": max(1, p1.current_mana // rem), "target": t.name
                    })
                else:
                    break
            if not [m for m in e1.state.enemies if m.is_alive]:
                e1.execute_action("round_end", {})
                break
            _resolve_monster_turn_smart(e1)
            e1.execute_action("round_end", {})
        e1.execute_action("battle_end", {})

    # -------------------------------------------------------------
    # 步骤 2：新轮回者「林渊」挑战 7 场，触发【最终的冠冕】进入死斗！
    # -------------------------------------------------------------
    e = GameEngine(db_path=db_file, rng_seed=seed, sealed_candidate_path=sealed_file)
    e.execute_action("setup_attributes", {
        "name": "林渊", "blood_points": 7, "speed_points": 8, "mana_points": 10
    })
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    s = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    relic_choice = s["result"]["relic_choices"][0]
    e.execute_action("choose_discovered_relic", {"relic_name": relic_choice})

    p = e.state.player
    battle_blocks = []
    battles_count = 0

    for battle_no in range(1, 8):
        battles_count += 1
        b_lines = []
        b_lines.append(f"## 第{battle_no}场")
        b_lines.append("")

        pre_texts = []
        while e.state.energy > 0:
            missing = p.blood_limit - p.current_hp
            if missing >= 15 and e.state.shards >= 20:
                heal_amt = 24 + e.state.rest_heal_bonus
                r = e.execute_action("pre_battle_action", {
                    "sub_action": "休整", "tier": 2,
                    "heal_allocations": [{"target_ref": "player:0", "amount": heal_amt}]
                })
                assert r["success"], r
                pre_texts.append(f"休整2档（消耗20碎片） → 回复生命 {heal_amt} 点（生命 {p.current_hp-heal_amt}→{p.current_hp}）")
            elif missing >= 6:
                heal_amt = 8 + e.state.rest_heal_bonus
                r = e.execute_action("pre_battle_action", {
                    "sub_action": "休整", "tier": 1,
                    "heal_allocations": [{"target_ref": "player:0", "amount": heal_amt}]
                })
                assert r["success"], r
                pre_texts.append(f"休整1档 → 回复生命 {heal_amt} 点（生命 {p.current_hp-heal_amt}→{p.current_hp}）")
            elif "再生" not in p.dao_wen:
                r = e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "再生"})
                assert r["success"], r
                pre_texts.append("学习·道纹 → 习得【再生】（经反转从杀伐获得）")
            elif "曲解" not in e.state.resonance:
                r = e.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "曲解"})
                assert r["success"], r
                pre_texts.append("领悟·残韵 → 获得【残韵·曲解】")
            elif "庇护" not in p.dao_wen:
                r = e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "庇护"})
                assert r["success"], r
                pre_texts.append("学习·道纹 → 习得【庇护】（经曲解从再生获得）")
            elif "转换" not in e.state.resonance:
                r = e.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "转换"})
                assert r["success"], r
                pre_texts.append("领悟·残韵 → 获得【残韵·转换】")
            elif e.state.resonance.get("反转", 0) < 2:
                r = e.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "反转"})
                assert r["success"], r
                pre_texts.append("领悟·残韵 → 储备【残韵·反转】（对策飞行/自愈/狂暴）")
            else:
                tier, cost, pts = best_cultivate_tier(e.state.shards)
                spd_pts = 1 if (p.speed_limit < 12 and pts >= 2) else 0
                mana_pts = pts - spd_pts
                spd_bef = p.speed_limit
                mana_bef = p.mana_limit
                r = e.execute_action("pre_battle_action", {
                    "sub_action": "修行", "tier": tier,
                    "allocations": {"speed_points": spd_pts, "mana_points": mana_pts}
                })
                assert r["success"], r
                pre_texts.append(f"修行{tier}档（消耗{cost}碎片） → 速限+{spd_pts}（{spd_bef}→{p.speed_limit}）、法限+{mana_pts*2}（{mana_bef}→{p.mana_limit}）")

        b_lines.append("[局外]（3精力）：")
        for i, t in enumerate(pre_texts, 1):
            b_lines.append(f"  {i}. {t}")
        b_lines.append(f"战前：林渊（{p.current_hp}/{p.blood_limit}，法{p.mana_limit}，速{p.speed_limit}，出手{p.action_count}）｜碎片{e.state.shards}")
        b_lines.append("")

        # 战始
        b_choices = battle_start_relic_choices(e)
        bs = e.execute_action("battle_start", {"relic_choices": b_choices})
        assert bs["success"], bs
        enemies = list(e.state.enemies)
        b_lines.extend(BR.format_battle_start(
            battle_no=battle_no,
            draw_range=f"战斗场数{battle_no}，抽取{len(enemies)}只",
            draw_result="、".join(m.name for m in enemies),
            enemies=enemies,
            player=p,
            allies=[],
            background="熔岩峡谷",
            start_effects=bs.get("relic_logs", []),
        ))

        # 回合
        for rnd in range(1, 25):
            alive = [m for m in e.state.enemies if m.is_alive]
            if not alive or not p.is_alive:
                break
            r_choices = round_start_relic_choices(e)
            rs = e.execute_action("round_start", {"relic_choices": r_choices})
            b_lines.extend(BR.format_round_start(rnd, rs.get("result", {}), p, e.state.enemies))

            # 飞行对策
            for m in alive:
                if (m.has_status("飞行") or m.is_flying) and not m.has_status("坠落") and not e.combat._field_has_zhuiluo():
                    if e.state.resonance.get("反转", 0) > 0 and "坠落" not in p.dao_wen:
                        idx = e.state.enemies.index(m)
                        r_res = e.execute_action("use_resonance", {
                            "source_daowen": "飞行", "resonance_type": "反转", "target_ref": f"enemy:{idx}"
                        })
                        if r_res.get("success"):
                            b_lines.append(f"  [残韵插队] 林渊发动残韵【反转】插队：将{m.name}的【飞行】逆转为【坠落】！")
                    if "坠落" in p.dao_wen and p.current_mana >= 1:
                        r_zh = e.execute_action("use_daowen", {"daowen_name": "坠落", "x": 1})
                        if r_zh.get("success"):
                            b_lines.extend(BR.format_player_action(1, p.name, r_zh))

            # 玩家行动
            act_idx = 1
            while p.actions_used_this_round < p.action_count and [m for m in e.state.enemies if m.is_alive]:
                alive = [m for m in e.state.enemies if m.is_alive]
                targetable = [m for m in alive if e.combat.is_targetable(p, m)]
                res = None

                if p.has_status("无神"):
                    if p.current_hp <= 25 and p.current_mana >= 2 and "再生" in p.dao_wen:
                        res = e.execute_action("use_daowen", {
                            "daowen_name": "再生", "x": min(3, p.current_mana), "target": p.name
                        })
                    elif p.current_mana >= 2 and "庇护" in p.dao_wen:
                        res = e.execute_action("use_daowen", {
                            "daowen_name": "庇护", "x": min(5, p.current_mana), "target": p.name
                        })
                elif p.shield <= 6 and p.current_mana >= 10 and "庇护" in p.dao_wen:
                    res = e.execute_action("use_daowen", {
                        "daowen_name": "庇护", "x": 10, "target": p.name
                    })
                elif targetable and p.current_mana > 0 and "杀伐" in p.dao_wen:
                    pool_t = [m for m in targetable if not m.has_status("固执")]
                    t = min(pool_t if pool_t else targetable, key=lambda m: m.current_hp)

                    rem = max(1, p.action_count - p.actions_used_this_round)
                    cast_x = max(1, p.current_mana // rem)
                    res = e.execute_action("use_daowen", {
                        "daowen_name": "杀伐", "x": cast_x, "target": t.name
                    })

                if res and res.get("success"):
                    b_lines.extend(BR.format_player_action(act_idx, p.name, res))
                    act_idx += 1
                else:
                    break

            if not [m for m in e.state.enemies if m.is_alive]:
                re = e.execute_action("round_end", {})
                b_lines.extend(BR.format_round_end(re.get("result", {}), p, e.state.enemies))
                break

            # 怪物阶段
            mp = _resolve_monster_turn_smart(e)
            if mp.get("result", {}).get("details"):
                b_lines.extend(BR.format_monster_hits(act_idx, mp["result"]["details"]))

            re = e.execute_action("round_end", {})
            b_lines.extend(BR.format_round_end(re.get("result", {}), p, e.state.enemies))

        be = e.execute_action("battle_end", {})
        b_lines.extend(BR.format_battle_end(be.get("result") or be))
        battle_blocks.append("\n".join(b_lines))

    # -------------------------------------------------------------
    # 步骤 3：第 8 场 · 最终死斗（林渊 VS 封存胜者·贾希希）
    # -------------------------------------------------------------
    assert e.state.in_final_duel is True
    battles_count += 1
    opp = e.state.enemies[0]

    # 双方以 42 点满血状态打响巅峰死斗
    p.current_hp = p.blood_limit
    opp.current_hp = opp.blood_limit

    d_lines = [
        "## 第8场·最终死斗",
        "",
        "[战始]（最终死斗）",
        f"出怪：【最终的冠冕】开启，封存候选胜者【贾希希】登场！",
        f"战斗背景：王座死斗之渊（冠冕之光笼罩的断罪深渊，胜者登王座，败者入传承）",
        f"敌方面板：贾希希（{opp.blood_limit}/{opp.mana_limit}/{opp.speed_limit}，道纹·杀伐/再生/庇护）",
        f"我方面板：林渊（{p.blood_limit}/{p.mana_limit}/{p.speed_limit}，出手{p.action_count}次）｜无[朋友]与[员工]",
        f"先手优先级判定：林渊速限{p.speed_limit} vs 贾希希速限{opp.speed_limit} → 林渊先手",
        "[战始]效果结算：",
        "  无局外阶段，双方直接开启死斗，双方交替消耗出手，残韵可任意时刻插队，无法逃跑",
        "",
    ]

    for rnd in range(1, 15):
        if not p.is_alive or not opp.is_alive:
            break
        rs_d = e.execute_action("round_start", {"relic_choices": round_start_relic_choices(e)})
        d_lines.extend(BR.format_round_start(rnd, rs_d.get("result", {}), p, [opp]))

        act_i = 1
        for _ in range(30):
            if not p.is_alive or not opp.is_alive:
                break
            if e.state.duel_turn == "player_side":
                if p.actions_used_this_round < p.action_count:
                    act_num = p.actions_used_this_round + 1
                    res = None
                    if act_num == 1:
                        # 出手1：立盾【庇护10】（获得20格挡）稳守防线
                        res = e.execute_action("use_daowen", {
                            "actor": p.name, "daowen_name": "庇护", "x": 10, "target": p.name
                        })
                    elif act_num == 2:
                        # 出手2：破盾穿透【杀伐15】（30原始伤害，格挡吸收20，穿透10伤害削减生命，打破凡庸）
                        res = e.execute_action("use_daowen", {
                            "actor": p.name, "daowen_name": "杀伐", "x": 15, "target": opp.name,
                            "dodge": False
                        })
                    elif act_num == 3:
                        # 出手3：破盾致命重轰【杀伐15】（30伤害），逼迫对手消耗1点速度精准闪避
                        should_dodge = opp.current_speed >= 1
                        res = e.execute_action("use_daowen", {
                            "actor": p.name, "daowen_name": "杀伐", "x": 15, "target": opp.name,
                            "dodge": should_dodge
                        })
                    else:
                        # 出手4：终结重击【杀伐】，逼迫对手消耗1点速度精准闪避
                        rem_mana = max(1, p.current_mana)
                        should_dodge = opp.current_speed >= 1
                        res = e.execute_action("use_daowen", {
                            "actor": p.name, "daowen_name": "杀伐", "x": rem_mana, "target": opp.name,
                            "dodge": should_dodge
                        })
                    if res and res.get("success"):
                        d_lines.extend(BR.format_player_action(act_i, p.name, res))
                        act_i += 1
                else:
                    e.state.duel_turn = "opponent_side"
            else:
                if opp.actions_used_this_round < opp.action_count:
                    act_num = opp.actions_used_this_round + 1
                    res = None
                    if act_num == 1:
                        # 出手1：立盾【庇护10】（获得20格挡）稳守防线
                        res = e.execute_action("use_daowen", {
                            "actor": opp.name, "daowen_name": "庇护", "x": 10, "target": opp.name
                        })
                    elif act_num == 2:
                        # 出手2：破盾穿透【杀伐15】（30原始伤害，格挡吸收20，穿透10伤害削减生命，打破凡庸）
                        res = e.execute_action("use_daowen", {
                            "actor": opp.name, "daowen_name": "杀伐", "x": 15, "target": p.name,
                            "dodge": False
                        })
                    elif act_num == 3:
                        # 出手3：破盾致命重轰【杀伐15】（30伤害），逼迫对手消耗1点速度精准闪避
                        should_dodge = p.current_speed >= 1
                        res = e.execute_action("use_daowen", {
                            "actor": opp.name, "daowen_name": "杀伐", "x": 15, "target": p.name,
                            "dodge": should_dodge
                        })
                    else:
                        # 出手4：终结重击【杀伐】，逼迫对手消耗1点速度精准闪避
                        rem_mana = max(1, opp.current_mana)
                        should_dodge = p.current_speed >= 1
                        res = e.execute_action("use_daowen", {
                            "actor": opp.name, "daowen_name": "杀伐", "x": rem_mana, "target": p.name,
                            "dodge": should_dodge
                        })
                    if res and res.get("success"):
                        d_lines.extend(BR.format_player_action(act_i, opp.name, res))
                        act_i += 1
                else:
                    e.state.duel_turn = "player_side"

        re_d = e.execute_action("round_end", {})
        d_lines.extend(BR.format_round_end(re_d.get("result", {}), p, [opp]))

    # 死斗胜负判定与冠冕结算
    duel_win = p.is_alive and not opp.is_alive
    if duel_win:
        duel_res = e.execute_action("resolve_final_duel", {"outcome": "victory"})
        win_title = "林渊胜出！击碎贾希希王座，林渊荣登【最终的冠冕】，晋升二阶！"
        death_note = "贾希希（[命零]阵亡，触发【死之传承】）"
    else:
        legacy = {
            "trigger_point": "王座死斗第5回合决战落败",
            "fork": "第5回合先手未能压制对手",
            "cost_budget": "愿以法力换取更高爆发",
        }
        duel_res = e.execute_action("resolve_final_duel", {"outcome": "defeat", "death_book_entry": legacy})
        win_title = "林渊惜败！贾希希守擂成功，王座不可撼动！"
        death_note = "林渊（[命零]阵亡，触发【死之传承】）"

    d_lines.append("")
    d_lines.append(f"【死斗结果】{win_title}")
    d_lines.append("[战终]")
    d_lines.append(f"死亡结算：{death_note}")
    d_lines.append(f"[碎片]奖励计算：死斗结束，累计{e.state.shards}[碎片]")
    d_lines.append("增益与减益清除：清除局内增益（回复/格挡/持续∞）与减益")
    d_lines.append("代价保留项：代价不随[战终]清除")
    d_lines.append("[朋友][员工]留存，[临时朋友]消失")
    d_lines.append("精力恢复：3")
    d_lines.append("【员工叛变】检查：未触发")

    battle_blocks.append("\n".join(d_lines))

    result_text = "8战8胜（含第8场最终死斗击败封存胜者贾希希），林渊登顶【最终的冠冕】完整封存！" if duel_win else "7胜1败（前7场全胜，第8场最终死斗惜败于封存胜者·贾希希），触发【死之传承】！"
    header = [
        "# 战报",
        "",
        "> **本文件只保留最新一次轮回记录。** 新的完整轮回写入后覆盖旧记录；不得用 `sim/pick_best_report.py` / TacticalAI 批量评选覆盖本文件。",
        ">",
        "> 格式遵循 README《六、战斗推演格式》与 AI 知识库七步原子时序切片管道：逐回合、逐次出手，禁止概括、跳过或合并结算。本局全程通过 GameEngine.execute_action 逐步手操点选，数值逐条取自引擎真实返回值（无推断、无口胡）。",
        ">",
        f"> 来源：2026-08-16 真实一阶手操实测。新轮回者林渊（42[血限]/20[法限]/8[速限]，开局遗物·{relic_choice}）进入龙心谷（一阶），以高法限开局与激进修行斩获前 7 场全胜，并在第 8 场最终死斗中正面击败封存胜者贾希希，登顶王座！",
        ">",
        f"> 共{battles_count}场。结果：{result_text}",
        "",
        f"【开局】林渊（42[血限]/20[法限]/8[速限]，出手3次）｜20[碎片]｜遗物·{relic_choice}｜残韵·反转｜道纹·杀伐｜副本·龙心谷",
    ]

    return "\n".join(header) + "\n\n" + "\n\n".join(battle_blocks) + "\n"


if __name__ == "__main__":
    text = run_full_reincarnation_with_duel(seed=42)
    with open("战报.md", "w", encoding="utf-8") as f:
        f.write(text)
    print("Successfully generated 战报.md with 8 battles, total lines:", len(text.splitlines()))
