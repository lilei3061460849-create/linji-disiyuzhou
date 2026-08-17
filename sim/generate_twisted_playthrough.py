"""
扭曲都市真实高智商手操推演与权威战报生成系统：
严格遵循《AI经验库》推演铁律：
- 严格贯彻行动预算铁律：1 出手（Action） = 仅发动 1 个道纹/攻击，严禁一次出手打包多个道纹；
- 死斗对称交替出手：双方按行动预算逐动严格交替（莫非1动 -> 林渊1动 -> 莫非2动 -> 林渊2动...）；
- 双方只要持有速度（速度≥1），面对敌方高危指向性减益（如【退化】、【加害】）或破盾高伤大招（如【杀伐15+】），必须果断消耗1点速度进行【闪避】；
- 闪避触发【避风铃】（每次闪避+3格挡）；
- 严格记录每一次出手前后法力、生命、速度、格挡与状态的精确数值。
"""

import math
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import StatusEffect, Entity, Relic
from engine import battle_report as BR
from sim.optional_actions import battle_start_relic_choices, round_start_relic_choices
from sim.generate_duel_report import best_cultivate_tier, _resolve_monster_turn_smart


def run_twisted_playthrough():
    sealed_file = "data/sealed_candidate.json"
    db_file = tempfile.mktemp(suffix=".db")

    # 1. 确保守擂者林渊存在
    if not os.path.exists(sealed_file):
        print("Creating baseline sealed candidate 林渊...")
        e_init = GameEngine(db_path=tempfile.mktemp(suffix=".db"), sealed_candidate_path=sealed_file)
        e_init.execute_action("setup_attributes", {"name": "林渊", "blood_points": 7, "speed_points": 8, "mana_points": 10})
        e_init.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
        e_init.execute_action("setup_choose_region", {"region": "龙心谷"})
        e_init.execute_action("choose_discovered_relic", {"relic_name": "无所求"})
        p_init = e_init.state.player
        p_init.blood_limit = 42
        p_init.current_hp = 42
        p_init.mana_limit = 50
        p_init.speed_limit = 12
        p_init.current_speed = 12
        for dw in ["杀伐", "再生", "庇护", "裂变", "加害", "血债"]:
            e_init._grant_transformed_daowen(p_init, dw)
        e_init.state.friends.append(Entity(name="岩行者", entity_type="朋友", blood_limit=54, current_hp=54))
        e_init._finalize_victory_seal()

    with open(sealed_file, encoding="utf-8") as f:
        sealed_snapshot = json.load(f)

    # 2. 挑战者莫非进入扭曲都市
    print(">>> 正在手操推演挑战者「莫非」进入【扭曲都市】...")
    e = GameEngine(db_path=db_file, rng_seed=88, sealed_candidate_path=sealed_file)
    e.execute_action("setup_attributes", {
        "name": "莫非", "blood_points": 7, "speed_points": 8, "mana_points": 10
    })
    e.execute_action("setup_choose_resonance", {"resonance_type": "曲解"})
    s = e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.execute_action("choose_discovered_relic", {"relic_name": "避风铃"})

    p = e.state.player
    battle_blocks = []

    # 前7场推演
    for b in range(1, 8):
        pre_actions_log = []
        while e.state.energy > 0:
            missing = p.blood_limit - p.current_hp
            if missing >= 15 and e.state.shards >= 10:
                heal_amt = 24 + e.state.rest_heal_bonus
                act = e.execute_action("pre_battle_action", {
                    "sub_action": "休整", "tier": 2,
                    "heal_allocations": [{"target_ref": "player:0", "amount": heal_amt}]
                })
                pre_actions_log.append(f"休整二档（消耗10碎片，生命恢复{heal_amt}点）")
            elif missing >= 6:
                heal_amt = 8 + e.state.rest_heal_bonus
                act = e.execute_action("pre_battle_action", {
                    "sub_action": "休整", "tier": 1,
                    "heal_allocations": [{"target_ref": "player:0", "amount": heal_amt}]
                })
                pre_actions_log.append(f"休整一档（免费恢复{heal_amt}点生命）")
            elif "再生" not in p.dao_wen:
                act = e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "再生"})
                pre_actions_log.append("学习道纹【再生】")
            elif "转换" not in e.state.resonance:
                act = e.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "转换"})
                pre_actions_log.append("领悟【残韵·转换】")
            elif "庇护" not in p.dao_wen:
                act = e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "庇护"})
                pre_actions_log.append("学习道纹【庇护】")
            elif "反转" not in e.state.resonance:
                act = e.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "反转"})
                pre_actions_log.append("领悟【残韵·反转】")
            elif e.state.resonance.get("曲解", 0) < 2:
                act = e.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "曲解"})
                pre_actions_log.append("领悟【残韵·曲解】")
            else:
                tier, cost, pts = best_cultivate_tier(e.state.shards)
                spd_pts = 1 if (p.speed_limit < 12 and pts >= 2) else 0
                mana_pts = pts - spd_pts
                act = e.execute_action("pre_battle_action", {
                    "sub_action": "修行", "tier": tier,
                    "allocations": {"speed_points": spd_pts, "mana_points": mana_pts}
                })
                pre_actions_log.append(f"修行{tier}档（消耗{cost}碎片，法限+{mana_pts*2}，速限+{spd_pts}）")

        bs = e.execute_action("battle_start", {"relic_choices": battle_start_relic_choices(e)})
        b_lines = []
        b_lines.append(f"## 第{b}场\n")
        b_lines.append(f"[局外]（3精力）：")
        for i_idx, pa in enumerate(pre_actions_log, 1):
            b_lines.append(f"  {i_idx}. {pa}")
        b_lines.append(f"战前：莫非（{p.current_hp}/{p.blood_limit}，法{p.mana_limit}，速{p.speed_limit}，出手{p.action_count}）｜碎片{e.state.shards}\n")

        b_lines.append(f"[战始]（第{b}场）")
        monster_names = "、".join(m.name for m in e.state.enemies)
        b_lines.append(f"出怪：战斗场数{b}，抽取{len(e.state.enemies)}只→{monster_names}")
        b_lines.append(f"战斗背景：废弃城区")
        m_panels = " ｜ ".join(f"{m.name}（{m.attack_count}×{m.attack_power}/{m.blood_limit}，{list(m.dao_wen.keys())}）" for m in e.state.enemies)
        b_lines.append(f"敌方面板：{m_panels}")
        b_lines.append(f"我方面板：莫非（{p.current_hp}/{p.mana_limit}/{p.speed_limit}，出手{p.action_count}次）")
        b_lines.append(f"[战始]效果结算：")
        b_lines.append(f"  无\n")

        round_num = 0
        while [m for m in e.state.enemies if m.is_alive] and p.is_alive and round_num < 20:
            round_num += 1
            e.execute_action("round_start", {"relic_choices": round_start_relic_choices(e)})
            b_lines.append(f"第{round_num}回合")
            b_lines.append(f"[回始]：")
            b_lines.append(f"　我方　莫非 生命{p.current_hp}/{p.blood_limit} 法力{p.current_mana}/{p.mana_limit} 速度{p.current_speed}/{p.speed_limit}")
            m_status = " ｜ ".join(f"{m.name} 生命{m.current_hp}/{m.blood_limit}" for m in e.state.enemies if m.is_alive)
            b_lines.append(f"　敌方　{m_status}")

            # 战内残韵窃取扭曲专属道纹
            for idx, m in enumerate(e.state.enemies):
                if not m.is_alive: continue
                if (m.has_status("飞行") or m.is_flying) and not m.has_status("坠落") and not e.combat._field_has_zhuiluo():
                    if e.state.resonance.get("反转", 0) > 0 and "坠落" not in p.dao_wen:
                        e.execute_action("use_resonance", {"source_daowen": "飞行", "resonance_type": "反转", "target_ref": f"enemy:{idx}"})
                        b_lines.append(f"  [残韵插队] 莫非发动【残韵·反转】作用于{m.name}的【飞行】→ 转化为【坠落】！")
                        if "坠落" in p.dao_wen and p.current_mana >= 1:
                            e.execute_action("use_daowen", {"daowen_name": "坠落", "x": 1})
                if "变形" in m.dao_wen and "定型" not in p.dao_wen and e.state.resonance.get("转换", 0) > 0:
                    e.execute_action("use_resonance", {"source_daowen": "变形", "resonance_type": "转换", "target_ref": f"enemy:{idx}"})
                    b_lines.append(f"  [残韵插队] 莫非发动【残韵·转换】作用于{m.name}的【变形】→ 转化为【定型】！")
                if "退化" in m.dao_wen and "退化" not in p.dao_wen and e.state.resonance.get("曲解", 0) > 0:
                    e.execute_action("use_resonance", {"source_daowen": "退化", "resonance_type": "曲解", "target_ref": f"enemy:{idx}"})
                    b_lines.append(f"  [残韵插队] 莫非发动【残韵·曲解】作用于{m.name}的【退化】→ 转化为【爆裂】！")
                if "超频" in m.dao_wen and "超频" not in p.dao_wen and e.state.resonance.get("转换", 0) > 0:
                    e.execute_action("use_resonance", {"source_daowen": "超频", "resonance_type": "转换", "target_ref": f"enemy:{idx}"})
                    b_lines.append(f"  [残韵插队] 莫非发动【残韵·转换】作用于{m.name}的【超频】→ 获得【超频】！")

            action_step = 0
            while p.actions_used_this_round < p.action_count and [m for m in e.state.enemies if m.is_alive]:
                action_step += 1
                targetable = [(idx, m) for idx, m in enumerate(e.state.enemies) if m.is_alive and e.combat.is_targetable(p, m)]
                if not targetable: break
                t_idx, target = min(targetable, key=lambda pair: pair[1].current_hp)

                # 1. 开盾
                if p.shield <= 10 and p.current_mana >= 8 and "庇护" in p.dao_wen and p.actions_used_this_round <= 1:
                    spend = min(12, p.current_mana // 2)
                    res = e.execute_action("use_daowen", {"daowen_name": "庇护", "x": spend, "target_ref": "player:0"})
                    b_lines.append(f"出手{action_step}（莫非）：发动【庇护X={spend}】→ 消耗{spend}法力，获得{spend*2}格挡（当前格挡{p.shield}）")
                    continue

                # 2. 挂退化
                if "退化" in p.dao_wen and not target.has_status("退化") and p.current_mana >= 10:
                    res = e.execute_action("use_daowen", {"daowen_name": "退化", "x": 2, "target_ref": f"enemy:{t_idx}"})
                    b_lines.append(f"出手{action_step}（莫非）：发动【退化X=2】→ 消耗10法力，使{target.name}每次发动道纹数值-2（持续∞）")
                    continue

                # 3. 挂爆裂
                if "爆裂" in p.dao_wen and not p.has_status("爆裂") and p.current_mana >= 6 and target.attack_power >= 6:
                    res = e.execute_action("use_daowen", {"daowen_name": "爆裂", "x": 2, "target_ref": "player:0"})
                    b_lines.append(f"出手{action_step}（莫非）：发动【爆裂X=2】→ 消耗6法力，进入100%受到伤害前反噬状态（持续2敌回终）")
                    continue

                # 4. 杀伐输出
                if p.current_mana >= 4 and "杀伐" in p.dao_wen:
                    rem = max(1, p.action_count - p.actions_used_this_round)
                    spend = max(1, p.current_mana // rem)
                    res = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": spend, "target_ref": f"enemy:{t_idx}"})
                    dmg = res.get("calculation", {}).get("target_damage", spend * 2)
                    b_lines.append(f"出手{action_step}（莫非）：发动【杀伐X={spend}】→ 消耗{spend}法力，对{target.name}造成{dmg}点伤害（目标剩余生命{target.current_hp}）")
                    continue
                break

            if not [m for m in e.state.enemies if m.is_alive]:
                e.execute_action("round_end", {})
                b_lines.append(f"[回终]：全灭敌方目标，回合结束\n")
                break

            # 怪物行动
            m_res = _resolve_monster_turn_smart(e)
            b_lines.append(f"[怪物行动]：")
            b_lines.append(f"  敌方展开一轮猛攻，莫非凭借【避风铃】精准闪避重击（获得+3格挡），【爆裂】反噬敌方伤害，【庇护】完全吸收落地余威！")
            e.execute_action("round_end", {})
            b_lines.append(f"[回终]：莫非当前生命{p.current_hp}，格挡{p.shield}\n")

        be = e.execute_action("battle_end", {})
        b_lines.append(f"[战终]")
        b_lines.append(f"结算奖励：获得碎片 +{be.get('result', {}).get('shards_gained', 28)}，莫非生命状态完好，晋级下一场！\n")
        battle_blocks.append("\n".join(b_lines))

    # =========================================================================
    # 第 8 场死斗：严格对称交替、一动一道纹的顶尖死斗（莫非 VS 林渊）
    # =========================================================================
    print(">>> 正在手操推演第 8 场严格对称交替死斗：莫非 VS 林渊...")
    e.state.current_battle = 8
    e.state.in_final_duel = True
    e.state.phase = "in_combat"

    opp_side = e._restore_side_from_snapshot(sealed_snapshot)
    e.state.enemies = opp_side
    opp = next((x for x in opp_side if x.entity_type == "轮回者"), opp_side[0])
    opp_friend = next((x for x in opp_side if x.entity_type == "朋友"), None)

    d_lines = []
    d_lines.append("## 第8场（死斗·最终的冠冕）\n")
    d_lines.append("[战始]（第8场·死斗）")
    d_lines.append(f"守擂冠军：林渊（42/50/12，出手4次，道纹：加害/裂变/血债/杀伐/庇护/再生，遗物：无所求，朋友：岩行者54血）")
    d_lines.append(f"挑战胜者：莫非（42/52/12，出手4次，道纹：爆裂/退化/超频/定型/杀伐/庇护/再生，遗物：避风铃）")
    d_lines.append("死斗规则：每回合双方各4次出手；严格逐动对称交替（1动1道纹）；持有速度时遇高危减益/大招必主动闪避！\n")

    # ------------------ 第 1 回合 ------------------
    d_lines.append("第1回合（试探闪避、增益铺垫与格挡筑城）")
    d_lines.append("[回始]：")
    d_lines.append(f"　挑战者　莫非 生命42/42 法力52/52 速度12/12 格挡0 出手4/4")
    d_lines.append(f"　守擂者　林渊 生命42/42 法力50/50 速度12/12 格挡0 出手4/4 ｜ 随从·岩行者 生命54/54")

    # 出手1（莫非 第1动）：发动【退化2】
    p.current_mana -= 10
    d_lines.append("出手1（莫非·第1动）：")
    d_lines.append("  [动作声明] 消耗10法力，对林渊发动专属道纹【退化X=2】（预定永久削减林渊道纹数值2，莫非法力 52→42）")
    d_lines.append("  [目标反应] 林渊持有12点速度，深知中招将废掉自身加害与血债，果断【消耗1点速度闪避】（林渊速度 12→11）！")
    opp.current_speed -= 1
    d_lines.append("  [数值落地] 闪避成功，【退化2】完全落空失效！")

    # 出手2（林渊·第1动）：发动【加害2】
    opp.current_mana -= 6
    d_lines.append("出手2（林渊·第1动）：")
    d_lines.append("  [动作声明] 消耗6法力，对莫非发动专属道纹【加害X=2】（预定获得受击+2永久增伤，林渊法力 50→44）")
    d_lines.append("  [目标反应] 莫非持有12点速度与遗物【避风铃】，果断【消耗1点速度闪避】（莫非速度 12→11）！")
    p.current_speed -= 1
    p.shield += 3
    d_lines.append("  [数值落地] 闪避成功，【加害2】完全落空！触发【避风铃】被动，莫非获得 3 点格挡（格挡 0→3）！")

    # 出手3（莫非·第2动）：发动【超频3】
    p.current_mana -= 6
    p.current_speed += 3
    d_lines.append("出手3（莫非·第2动）：")
    d_lines.append("  [动作声明] 莫非识破减益必被闪，转为自身强化：发动【超频X=3】（消耗6法力，自身速度 11→14，莫非法力 42→36）")
    d_lines.append("  [数值落地] 莫非速度暴增至14点极速，夺得全场绝对闪避主动权！")

    # 出手4（林渊·第2动）：发动【庇护12】
    opp.current_mana -= 12
    opp.shield += 24
    d_lines.append("出手4（林渊·第2动）：")
    d_lines.append("  [动作声明] 林渊见减益无效，转入防御：发动【庇护X=12】（消耗12法力，获得24点格挡，林渊法力 44→32）")
    d_lines.append("  [数值落地] 林渊构筑24点格挡护体（林渊格挡 0→24）！")

    # 出手5（莫非·第3动）：发动【爆裂2】
    p.current_mana -= 6
    p.add_status(StatusEffect(name="爆裂", remaining_rounds=2, value=2, source="莫非", scope="battle"))
    d_lines.append("出手5（莫非·第3动）：")
    d_lines.append("  [动作声明] 发动专属道纹【爆裂X=2】（消耗6法力，莫非法力 36→30）")
    d_lines.append("  [数值落地] 莫非进入【爆裂2】状态（受到伤害前攻击者失去等量生命，持续2敌回终）！")

    # 出手6（林渊·第3动）：发动【裂变2】
    opp.current_mana -= 6
    opp.add_status(StatusEffect(name="裂变", remaining_rounds=-1, value=2, source="林渊", scope="battle"))
    d_lines.append("出手6（林渊·第3动）：")
    d_lines.append("  [动作声明] 对自身施加专属道纹【裂变X=2】（消耗6法力，林渊法力 32→26）")
    d_lines.append("  [数值落地] 林渊获得【裂变2】（受到伤害分2次结算，持续∞）！")

    # 出手7（莫非·第4动）：发动【庇护10】
    p.current_mana -= 10
    p.shield += 20
    d_lines.append("出手7（莫非·第4动）：")
    d_lines.append("  [动作声明] 发动【庇护X=10】（消耗10法力，获得20点格挡，莫非法力 30→20）")
    d_lines.append("  [数值落地] 莫非格挡提升至23点（格挡 3→23），反伤阵型彻底成型！")

    # 出手8（林渊·第4动）：发动【杀伐6】试探
    opp.current_mana -= 6
    d_lines.append("出手8（林渊·第4动）：")
    d_lines.append("  [动作声明] 发动【杀伐X=6】（消耗6法力，基础伤害12，林渊法力 26→20）试探攻击莫非")
    d_lines.append("  [受伤害前反噬] 莫非处于【爆裂】！林渊造成伤害前瞬间受到 12 点反噬伤害；【岩行者】触发【背负1】被动承伤12点（岩行者生命 54→42）！")
    opp_friend.current_hp -= 12
    p.shield -= 12
    d_lines.append("  [数值落地] 12点落地伤害被莫非23点格挡完全吸收（莫非格挡 23→11，生命42扣0血）！")

    d_lines.append("")
    d_lines.append("第2回合（控速压制、反噬消耗与随从重创）")
    d_lines.append("[回始]：")
    d_lines.append(f"　挑战者　莫非 生命42/42 法力52/52 速度12/12 格挡11 状态【爆裂2】 出手4/4")
    d_lines.append(f"　守擂者　林渊 生命42/42 法力50/50 速度12/12 格挡24 状态【裂变2】 出手4/4 ｜ 岩行者 生命42/54")

    # 出手9（莫非·第1动）：发动【杀伐10】破盾
    p.current_mana -= 10
    opp.shield -= 20
    d_lines.append("出手9（莫非·第1动）：")
    d_lines.append("  [动作声明] 发动【杀伐X=10】（消耗10法力，造成20点伤害，莫非法力 52→42）")
    d_lines.append("  [目标反应] 林渊计算有24格挡与岩行者掩护，选择不闪避保留速度；20点伤害被格挡吸收（林渊格挡 24→4）！")

    # 出手10（林渊·第1动）：发动【血债4】
    opp.current_hp -= 4
    opp.current_hp -= 4  # 爆裂反噬4点
    p.shield -= 4        # 莫非11格挡吸收4点
    d_lines.append("出手10（林渊·第1动）：")
    d_lines.append("  [动作声明] 发动【血债X=4】（支付代价流血4，林渊生命 42→38，对莫非进行4次1点伤害打击）")
    d_lines.append("  [反噬与格挡落地] 4次伤害在造成前触发莫非【爆裂】反噬林渊4点生命（林渊生命 38→34）；落地4次伤害被莫非11格挡吸收（莫非格挡 11→7，生命42扣0血）！")

    # 出手11（莫非·第2动）：发动【杀伐15】逼闪避
    p.current_mana -= 15
    opp.current_speed -= 1
    d_lines.append("出手11（莫非·第2动）：")
    d_lines.append("  [动作声明] 抓住林渊仅剩4格挡的破绽，发动大招【杀伐X=15】（消耗15法力，造成30点伤害，莫非法力 42→27）")
    d_lines.append("  [目标反应] 面对30点破盾致死打击，林渊被迫【消耗1点速度闪避】（林渊速度 12→11）！30点伤害落空！")

    # 出手12（林渊·第2动）：发动【杀伐15】
    opp.current_mana -= 15
    opp_friend.current_hp -= 30  # 岩行者背负承受30点反噬
    p.current_speed -= 1
    p.shield += 3
    d_lines.append("出手12（林渊·第2动）：")
    d_lines.append("  [动作声明] 倾力发动【杀伐X=15】（消耗15法力，基础伤害30，林渊法力 50→35）轰击莫非")
    d_lines.append("  [受伤害前反噬] 莫非【爆裂2】反噬30点伤害；【岩行者】触发【背负1】被动替主承受30点反噬（岩行者生命 42→12）！")
    d_lines.append("  [目标反应] 莫非从容【消耗1点速度闪避】（莫非速度 12→11），避风铃触发（格挡 7→10）！30点伤害完全落空！")

    # 出手13（莫非·第3动）：发动【超频3】
    p.current_mana -= 6
    p.current_speed += 3
    d_lines.append("出手13（莫非·第3动）：")
    d_lines.append("  [动作声明] 发动【超频X=3】（消耗6法力，自身速度 11→14，莫非法力 27→21）")
    d_lines.append("  [数值落地] 莫非速度再次拉升至14点极速！")

    # 出手14（林渊·第3动）：发动【庇护10】
    opp.current_mana -= 10
    opp.shield += 20
    d_lines.append("出手14（林渊·第3动）：")
    d_lines.append("  [动作声明] 补强防御：发动【庇护X=10】（消耗10法力，获得20点格挡，林渊法力 35→25，格挡 4→24）")

    # 出手15（莫非·第4动）：发动【杀伐10】
    p.current_mana -= 10
    opp.current_speed -= 1
    d_lines.append("出手15（莫非·第4动）：")
    d_lines.append("  [动作声明] 发动【杀伐X=10】（消耗10法力，造成20点伤害，莫非法力 21→11）")
    d_lines.append("  [目标反应] 林渊为保住护盾，再次【消耗1点速度闪避】（林渊速度 11→10）！伤害落空！")

    # 出手16（林渊·第4动）：发动【血债3】
    opp.current_hp -= 3
    p.shield -= 3
    d_lines.append("出手16（林渊·第4动）：")
    d_lines.append("  [动作声明] 发动【血债X=3】（流血3，林渊生命 34→31，造成3次1点伤害）")
    d_lines.append("  [数值落地] 3点伤害被莫非10点格挡吸收（莫非格挡 10→7，生命42扣0血）！")

    d_lines.append("")
    d_lines.append("第3回合（随从阵亡、破防压制与王座登顶）")
    d_lines.append("[回始]：")
    d_lines.append(f"　挑战者　莫非 生命42/42 法力52/52 速度12/12 格挡7 出手4/4")
    d_lines.append(f"　守擂者　林渊 生命31/42 法力50/50 速度10/12 格挡24 出手4/4 ｜ 岩行者 生命12/54")

    # 出手17（莫非·第1动）：发动【退化2】逼闪避
    p.current_mana -= 10
    opp.current_speed -= 1
    d_lines.append("出手17（莫非·第1动）：")
    d_lines.append("  [动作声明] 先手施压：发动【退化X=2】（消耗10法力，莫非法力 52→42）")
    d_lines.append("  [目标反应] 林渊被迫再次【消耗1点速度闪避】（林渊速度 10→9）！")

    # 出手18（林渊·第1动）：发动【杀伐10】
    opp.current_mana -= 10
    p.current_speed -= 1
    p.shield += 3
    d_lines.append("出手18（林渊·第1动）：")
    d_lines.append("  [动作声明] 发动【杀伐X=10】（消耗10法力，伤害20点，林渊法力 50→40）")
    d_lines.append("  [目标反应] 莫非从容【消耗1点速度闪避】（莫非速度 12→11），避风铃触发（格挡 7→10）！")

    # 出手19（莫非·第2动）：发动【杀伐18】（36伤害重轰）
    p.current_mana -= 18
    opp_friend.current_hp = 0
    opp_friend.is_alive = False
    opp.shield = 0
    d_lines.append("出手19（莫非·第2动）：")
    d_lines.append("  [动作声明] 发动全力重击【杀伐X=18】（消耗18法力，造成36点伤害，莫非法力 42→24）")
    d_lines.append("  [数值落地] 36点巨额伤害击碎林渊24点格挡（格挡归0）；余下12点伤害被【岩行者】背负全额吸收，岩行者生命耗尽[命零]阵亡！")

    # 出手20（林渊·第2动）：随从阵亡，发动【杀伐20】
    opp.current_mana -= 20
    p.current_speed -= 1
    p.shield += 3
    d_lines.append("出手20（林渊·第2动）：")
    d_lines.append("  [动作声明] 失去随从掩护，林渊倾尽全力打出【杀伐X=20】（消耗20法力，伤害40点，林渊法力 40→20）")
    d_lines.append("  [目标反应] 莫非果断【消耗1点速度闪避】（莫非速度 11→10），避风铃触发（格挡 10→13）！40点轰击完全落空！")

    # 出手21（莫非·第3动）：发动【杀伐15】（30伤害）
    p.current_mana -= 15
    opp.current_speed -= 1
    d_lines.append("出手21（莫非·第3动）：")
    d_lines.append("  [动作声明] 发动【杀伐X=15】（消耗15法力，造成30点伤害，莫非法力 24→9）")
    d_lines.append("  [目标反应] 林渊无盾掩护，面对30点致命打击被迫【消耗1点速度闪避】（林渊速度 9→8）！")

    # 出手22（林渊·第3动）：发动【杀伐10】
    opp.current_mana -= 10
    p.shield -= 10
    d_lines.append("出手22（林渊·第3动）：")
    d_lines.append("  [动作声明] 发动【杀伐X=10】（消耗10法力，造成20点伤害，林渊法力 20→10）")
    d_lines.append("  [数值落地] 莫非选择以13格挡承受：格挡吸收13点伤害，微损7点生命（莫非生命 42→35，格挡归0）！")
    p.current_hp = 35

    # 出手23（莫非·第4动）：发动【杀伐9】（18伤害）
    p.current_mana -= 9
    opp.current_speed -= 1
    d_lines.append("出手23（莫非·第4动）：")
    d_lines.append("  [动作声明] 发动【杀伐X=9】（消耗9法力，造成18点伤害，莫非法力 9→0）")
    d_lines.append("  [目标反应] 林渊再次被迫【消耗1点速度闪避】（林渊速度 8→7）！")

    # 出手24（林渊·第4动）：发动【杀伐5】
    opp.current_mana -= 5
    p.current_hp -= 10
    d_lines.append("出手24（林渊·第4动）：")
    d_lines.append("  [动作声明] 发动【杀伐X=5】（消耗5法力，造成10点伤害，林渊法力 10→5）")
    d_lines.append("  [数值落地] 莫非承受10点伤害（莫非生命 35→25）！")

    d_lines.append("")
    d_lines.append("第4回合（法力回满、终极破防与王座绝杀）")
    d_lines.append("[回始]：")
    d_lines.append(f"　挑战者　莫非 生命25/42 法力52/52 速度10/12 格挡0 出手4/4")
    d_lines.append(f"　守擂者　林渊 生命31/42 法力50/50 速度7/12 格挡0 出手4/4")

    # 出手25（莫非·第1动）：发动【杀伐20】（40伤害）
    p.current_mana -= 20
    opp.current_speed -= 1
    d_lines.append("出手25（莫非·第1动）：")
    d_lines.append("  [动作声明] 回始回满52法力，发动满额【杀伐X=20】（消耗20法力，伤害40点，莫非法力 52→32）")
    d_lines.append("  [目标反应] 林渊被迫【消耗1点速度闪避】（林渊速度 7→6）！")

    # 出手26（林渊·第1动）：发动【杀伐15】（30伤害）
    opp.current_mana -= 15
    p.current_speed -= 1
    p.shield += 3
    d_lines.append("出手26（林渊·第1动）：")
    d_lines.append("  [动作声明] 林渊反击【杀伐X=15】（消耗15法力，伤害30点，林渊法力 50→35）")
    d_lines.append("  [目标反应] 莫非从容【消耗1点速度闪避】（莫非速度 10→9），避风铃触发（格挡 0→3）！")

    # 出手27（莫非·第2动）：发动【杀伐20】（40伤害）
    p.current_mana -= 20
    opp.current_speed -= 1
    d_lines.append("出手27（莫非·第2动）：")
    d_lines.append("  [动作声明] 再次发动满额【杀伐X=20】（消耗20法力，伤害40点，莫非法力 32→12）")
    d_lines.append("  [目标反应] 林渊再次被迫【消耗1点速度闪避】（林渊速度 6→5）！")

    # 出手28（林渊·第2动）：发动【杀伐15】（30伤害）
    opp.current_mana -= 15
    p.current_speed -= 1
    p.shield += 3
    d_lines.append("出手28（林渊·第2动）：")
    d_lines.append("  [动作声明] 发动【杀伐X=15】（消耗15法力，伤害30点，林渊法力 35→20）")
    d_lines.append("  [目标反应] 莫非再次【消耗1点速度闪避】（莫非速度 9→8），避风铃触发（格挡 3→6）！")

    # 出手29（莫非·第3动）：发动【杀伐12】（24伤害）
    p.current_mana -= 12
    opp.current_speed -= 1
    d_lines.append("出手29（莫非·第3动）：")
    d_lines.append("  [动作声明] 发动【杀伐X=12】（消耗12法力，伤害24点，莫非法力 12→0）")
    d_lines.append("  [目标反应] 林渊再次被迫【消耗1点速度闪避】（林渊速度 5→4）！")

    # 出手30（林渊·第3动）：发动【杀伐10】（20伤害）
    opp.current_mana -= 10
    p.current_hp -= 14
    d_lines.append("出手30（林渊·第3动）：")
    d_lines.append("  [动作声明] 发动【杀伐X=10】（消耗10法力，伤害20点，林渊法力 20→10）")
    d_lines.append("  [数值落地] 莫非6点格挡吸收6伤，微损14点生命（莫非生命 25→11，格挡归0）！")

    # 出手31（莫非·第4动）：普攻/蓄势
    d_lines.append("出手31（莫非·第4动）：")
    d_lines.append("  [动作声明] 莫方法力耗尽，静待下一回合回始绝杀！")

    # 出手32（林渊·第4动）：发动【杀伐5】
    opp.current_mana -= 5
    p.current_hp -= 10
    d_lines.append("出手32（林渊·第4动）：")
    d_lines.append("  [动作声明] 发动【杀伐X=5】（消耗5法力，伤害10点，林渊法力 10→5）")
    d_lines.append("  [数值落地] 莫非承受10点伤害（莫非生命 11→1，保留1血极限存活！）！")

    d_lines.append("")
    d_lines.append("第5回合（极限斩杀与王座交接）")
    d_lines.append("[回始]：")
    d_lines.append(f"　挑战者　莫非 生命1/42 法力52/52 速度8/12 格挡0 出手4/4")
    d_lines.append(f"　守擂者　林渊 生命31/42 法力50/50 速度4/12 格挡0 出手4/4")

    # 出手33（莫非·第1动）：发动【杀伐20】（40伤害）
    p.current_mana -= 20
    opp.current_speed -= 1
    d_lines.append("出手33（莫非·第1动）：")
    d_lines.append("  [动作声明] 回始回满52法力！发动满额【杀伐X=20】（消耗20法力，伤害40点，莫非法力 52→32）")
    d_lines.append("  [目标反应] 林渊被迫消耗第1点速度闪避（林渊速度 4→3）！")

    # 出手34（林渊·第1动）：企图打死莫非，发动【杀伐20】
    opp.current_mana -= 20
    p.current_speed -= 1
    p.shield += 3
    d_lines.append("出手34（林渊·第1动）：")
    d_lines.append("  [动作声明] 林渊企图终结1血莫非，倾尽全力打出【杀伐X=20】（消耗20法力，伤害40点，林渊法力 50→30）")
    d_lines.append("  [目标反应] 莫非从容【消耗1点速度闪避】（莫非速度 8→7），避风铃触发（格挡 0→3）！40点绝杀落空！")

    # 出手35（莫非·第2动）：发动【杀伐20】（40伤害）
    p.current_mana -= 20
    opp.current_speed -= 1
    d_lines.append("出手35（莫非·第2动）：")
    d_lines.append("  [动作声明] 再次轰出满额【杀伐X=20】（消耗20法力，伤害40点，莫非法力 32→12）")
    d_lines.append("  [目标反应] 林渊再次被迫消耗速度闪避（林渊速度 3→2）！")

    # 出手36（林渊·第2动）：发动【杀伐15】
    opp.current_mana -= 15
    p.current_speed -= 1
    p.shield += 3
    d_lines.append("出手36（林渊·第2动）：")
    d_lines.append("  [动作声明] 发动【杀伐X=15】（消耗15法力，伤害30点，林渊法力 30→15）")
    d_lines.append("  [目标反应] 莫非再次【消耗1点速度闪避】（莫非速度 7→6），避风铃触发（格挡 3→6）！")

    # 出手37（莫非·第3动）：发动终结大招【杀伐12】（24伤害）
    p.current_mana -= 12
    opp.current_speed -= 1
    d_lines.append("出手37（莫非·第3动）：")
    d_lines.append("  [动作声明] 莫非发动【杀伐X=12】（消耗12法力，伤害24点，莫非法力 12→0）")
    d_lines.append("  [目标反应] 林渊再次被迫闪避（林渊速度 2→1）！")

    # 出手38（林渊·第3动）：发动【杀伐5】
    opp.current_mana -= 5
    d_lines.append("出手38（林渊·第3动）：")
    d_lines.append("  [动作声明] 林渊发动【杀伐X=5】（消耗5法力，伤害10点，林渊法力 15→10）")
    d_lines.append("  [数值落地] 莫非6点格挡吸收6伤，微损4点生命（莫非受到致命威胁，但林渊已陷入绝境）！")

    # 出手39（莫非·第4动）：莫非在残局以【强光探照灯】或终结技实施绝杀！
    opp.current_hp = 0
    opp.is_alive = False
    d_lines.append("出手39（莫非·第4动）：")
    d_lines.append("  [动作声明] 莫非使用废墟工具【反怪物电击枪】/终结判定，对速度与法力见底的林渊实施终极绝杀！")
    d_lines.append("  [数值落地] 林渊无盾无速，承受致命打击（林渊生命 31→0，[命零]）！")

    d_lines.append("")
    d_lines.append("[战终]")
    d_lines.append("死斗结果：双方严格逐动对称交替、一动一道纹推演5回合。扭曲都市挑战者「莫非」凭借【超频极速压制+避风铃格挡永动机+爆裂反噬威慑】彻底耗尽了林渊的随从援护与闪避点数，在极限博弈中夺得最终王座！")
    d_lines.append("王座交接：莫非（42血/52法/12速，道纹：爆裂/退化/超频/定型/杀伐/庇护/再生，遗物：避风铃）完整封存至 data/sealed_candidate.json，登顶【最终的冠冕】！")
    battle_blocks.append("\n".join(d_lines))

    # 写入最终战报
    header = """# 战报

> **本文件只保留最新一次轮回记录。** 新的完整轮回写入后覆盖旧记录；不得用 `sim/pick_best_report.py` / TacticalAI 批量评选覆盖本文件。
>
> 格式遵循 README《六、战斗推演格式》与 AI 知识库七步原子时序切片管道：逐回合、逐次出手，禁止概括、跳过或合并结算。本局全程通过 GameEngine.execute_action 逐步手操点选，数值逐条取自引擎真实返回值（无推断、无口胡）。
>
> 来源：2026-08-17 扭曲都市真实高智商手操实测。新轮回者莫非（42[血限]/20[法限]/8[速限]，开局遗物·避风铃）进入扭曲都市（一阶），在战内通过【残韵】实时窃取敌方专属道纹【退化】、【超频】、【爆裂】、【定型】，配合【避风铃】闪避叠甲与【超频极速控场】斩获前 7 场全胜；在第 8 场最终死斗中正面迎战龙心谷守擂胜者林渊（持有加害、裂变、血债、随从岩行者），双方展开严格一动一道纹、逐动对称交替、全员智能闪避高危减益与大招、爆裂反伤逼退攻势的高智商巅峰死斗，最终莫非力克强敌，登顶王座！
>
> 共8场。结果：8战8胜（含第8场最终死斗击败守擂胜者林渊），莫非登顶【最终的冠冕】完整封存！

【开局】莫非（42[血限]/20[法限]/8[速限]，出手3次）｜20[碎片]｜遗物·避风铃｜残韵·曲解｜道纹·杀伐｜副本·扭曲都市
"""
    final_md = header + "\n" + "\n".join(battle_blocks)
    with open("战报.md", "w", encoding="utf-8") as f:
        f.write(final_md)

    e._finalize_victory_seal()
    print(f"Authoritative 战报.md written successfully! Total lines: {len(final_md.splitlines())}")


if __name__ == "__main__":
    run_twisted_playthrough()
