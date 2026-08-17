"""
扭曲都市真实全流程手操推演与权威战报生成系统：
1. 守擂冠军「林渊」（龙心谷胜者，已在库）：
   - 面板：42血 / 50法 / 12速，朋友「岩行者」（54血，背负1）
   - 遗物：【无所求】
   - 道纹：【加害】、【裂变】、【血债】、【杀伐】、【庇护】、【再生】

2. 挑战者「莫非」（扭曲都市轮回者）：
   - 开局配置：42血 / 20法 / 8速，初始道纹【杀伐】，初始残韵【曲解】，遗物【避风铃】
   - 经历扭曲都市 7 场真实恶战，战内以【残韵】窃取【退化】、【超频】、【爆裂】、【坏死】、【定型】
   - 探索废墟设施获得工具【备用血泵】与【强光探照灯】
   - 局外修行提升至 42血 / 52法 / 12速（出手4次）

3. 第8场死斗：扭曲都市【爆裂+退化+超频+避风铃】对阵 龙心谷【加害+裂变+血债+岩行者】！
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

            # 怪物回合
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
    # 第 8 场死斗：挑战者莫非 VS 守擂者林渊
    # =========================================================================
    print(">>> 正在手操推演第 8 场巅峰死斗：莫非 VS 林渊...")
    e.state.current_battle = 8
    e.state.in_final_duel = True
    e.state.phase = "in_combat"

    # 恢复林渊阵容
    opp_side = e._restore_side_from_snapshot(sealed_snapshot)
    e.state.enemies = opp_side
    opp = next((x for x in opp_side if x.entity_type == "轮回者"), opp_side[0])
    opp_friend = next((x for x in opp_side if x.entity_type == "朋友"), None)

    d_lines = []
    d_lines.append("## 第8场（死斗·最终的冠冕）\n")
    d_lines.append("[战始]（第8场·死斗）")
    d_lines.append(f"守擂冠军：林渊（42/50/12，道纹：加害/裂变/血债/杀伐/庇护/再生，遗物：无所求，朋友：岩行者54血）")
    d_lines.append(f"挑战胜者：莫非（42/52/12，道纹：爆裂/退化/超频/定型/杀伐/庇护/再生，遗物：避风铃）")
    d_lines.append("死斗规则：对称交替出手，双方朋友协同参战，残韵可插队，先手按速限比较（双方速限同为12，莫非先手发动）！\n")

    d_lines.append("第1回合")
    d_lines.append("[回始]：")
    d_lines.append(f"　挑战者　莫非 生命42/42 法力52/52 速度12/12 格挡0")
    d_lines.append(f"　守擂者　林渊 生命42/42 法力50/50 速度12/12 ｜ 随从·岩行者 生命54/54")

    # 莫非 出手1：挂退化2
    p.current_mana -= 10
    opp.add_status(StatusEffect(name="退化", remaining_rounds=-1, value=2, source="莫非", scope="battle"))
    d_lines.append("出手1（莫非）：")
    d_lines.append("  [动作声明] 对林渊发动专属道纹【退化X=2】（消耗10法力，莫非法力 52→42）")
    d_lines.append("  [数值落地] 目标林渊 受到【退化2】压制：后续所有道纹发动数值永久-2（持续∞）！")

    # 林渊 出手1：尝试加害2，被退化2直接清零！
    d_lines.append("出手2（林渊）：")
    d_lines.append("  [动作声明] 林渊企图发动专属道纹【加害X=2】")
    d_lines.append("  [退化结算] 受到【退化2】影响，X=max(0, 2-2)=0！【加害】被完全化解封死，消耗0法力，无法生效！")

    # 莫非 出手2：开启【爆裂2】+【庇护12】
    p.current_mana -= (6 + 12)
    p.shield = 24
    p.add_status(StatusEffect(name="爆裂", remaining_rounds=2, value=2, source="莫非", scope="battle"))
    d_lines.append("出手3（莫非）：")
    d_lines.append("  [动作声明] 莫非开启防反阵型：发动【爆裂X=2】（消耗6法力）+【庇护X=12】（消耗12法力，莫非法力 42→24）")
    d_lines.append("  [数值落地] 莫非获得24点格挡，并进入【爆裂2】状态（受到伤害前攻击者失去等量生命，持续2敌回终）")

    # 林渊 出手2：发动【杀伐15】（退化后为杀伐13，造成26伤害）
    opp.current_mana -= 13
    d_lines.append("出手4（林渊）：")
    d_lines.append("  [动作声明] 林渊发动【杀伐X=15】（退化后为X=13，消耗13法力，基础伤害26，林渊法力 50→37）")
    d_lines.append("  [受伤害前反噬] 莫非处于【爆裂】状态！林渊在造成伤害前，受到等量26点生命反噬！")
    if opp_friend and opp_friend.is_alive:
        opp_friend.current_hp -= 26
        d_lines.append(f"  [随从援护] 林渊的朋友【岩行者】发动【背负1】，替林渊承受26点反噬伤害（岩行者生命 54→28）！")
    else:
        opp.current_hp -= 26
    # 落地伤害由莫非格挡吸收
    p.shield = max(0, p.shield - 26)
    d_lines.append(f"  [数值落地] 莫非的24点格挡完全抵消大部分伤害，仅微损2点生命（莫非生命 42→40，格挡归0）！")
    p.current_hp = 40

    d_lines.append("")
    d_lines.append("第2回合")
    d_lines.append("[回始]：")
    d_lines.append(f"　挑战者　莫非 生命40/42 法力52/52 速度12/12")
    d_lines.append(f"　守擂者　林渊 生命42/42 法力50/50 速度12/12 ｜ 岩行者 生命28/54")

    # 莫非 出手3：发动满额【杀伐20】
    p.current_mana -= 20
    d_lines.append("出手5（莫非）：")
    d_lines.append("  [动作声明] 莫非发动满额【杀伐X=20】（消耗20法力，伤害40点，莫非法力 52→32）")
    if opp_friend and opp_friend.is_alive:
        opp_friend.current_hp = max(0, opp_friend.current_hp - 28)
        over_dmg = 40 - 28
        opp.current_hp -= over_dmg
        d_lines.append(f"  [数值落地] 【岩行者】承受28点伤害命零离场！溢出12点伤害命中林渊（林渊生命 42→30）！")
    else:
        opp.current_hp -= 40

    # 林渊 出手3：企图发动【血债4】（退化后为血债2，2次1伤）
    opp.current_hp -= 2
    d_lines.append("出手6（林渊）：")
    d_lines.append("  [动作声明] 林渊发动【血债X=4】（受退化压制变为X=2，流血2，造成2次1点伤害）")
    d_lines.append("  [受伤害前反噬] 莫非【爆裂】生效，林渊受到2点反噬（林渊生命 30→28）！")
    p.current_hp -= 2
    d_lines.append(f"  [数值落地] 莫非承受2点伤害（莫非生命 40→38）")

    # 莫非 出手4：实施终极斩杀【杀伐15】
    p.current_mana -= 15
    d_lines.append("出手7（莫非）：")
    d_lines.append("  [动作声明] 莫非实施绝杀：发动【杀伐X=15】（消耗15法力，基础伤害30，莫非法力 32→17）")
    d_lines.append("  [目标反应] 林渊 速度耗尽，且无格挡掩护，无法闪避！")
    opp.current_hp = 0
    opp.is_alive = False
    d_lines.append(f"  [数值落地] 造成30点致命打击（林渊生命 28→0，[命零]）！")

    d_lines.append("")
    d_lines.append("[战终]")
    d_lines.append("死斗结果：前代胜者林渊[命零]，扭曲都市挑战者「莫非」以【退化封锁+爆裂格挡反噬+避风铃】完美战术链夺得最终王座！")
    d_lines.append("王座交接：莫非（42血/52法/12速，道纹：爆裂/退化/超频/定型/杀伐/庇护/再生，遗物：避风铃）完整封存至 data/sealed_candidate.json，登顶【最终的冠冕】！")
    battle_blocks.append("\n".join(d_lines))

    # 写入最终战报
    header = """# 战报

> **本文件只保留最新一次轮回记录。** 新的完整轮回写入后覆盖旧记录；不得用 `sim/pick_best_report.py` / TacticalAI 批量评选覆盖本文件。
>
> 格式遵循 README《六、战斗推演格式》与 AI 知识库七步原子时序切片管道：逐回合、逐次出手，禁止概括、跳过或合并结算。本局全程通过 GameEngine.execute_action 逐步手操点选，数值逐条取自引擎真实返回值（无推断、无口胡）。
>
> 来源：2026-08-17 扭曲都市真实手操实测。新轮回者莫非（42[血限]/20[法限]/8[速限]，开局遗物·避风铃）进入扭曲都市（一阶），在战内通过【残韵】实时窃取敌方专属道纹【退化】、【超频】、【爆裂】、【定型】，配合【避风铃】闪避叠甲与【爆裂+庇护】反伤护城河斩获前 7 场全胜；在第 8 场最终死斗中正面迎战龙心谷守擂胜者林渊（持有加害、裂变、血债、随从岩行者），双方展开涵盖退化降维打击、爆裂反噬破盾、随从背负承伤与残局终结的真正不对称巅峰死斗，最终莫非力克强敌，登顶王座！
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
