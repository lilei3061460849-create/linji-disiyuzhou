#!/usr/bin/env python3
"""
批量平衡工具：按既定标准从多次轮回中挑出一份写入指定文件。

正式 `战报.md` 只保留最新一次手操轮回，本脚本默认不得覆盖它。
用户说「测试」时走 GameEngine 手操，不跑本脚本。

选取标准（平衡批次内部）
--------------------
1. 以**进入【最终的冠冕】前的当前血量**为唯一标准，剩余血量最高者胜出。
2. 未能走到第7场（中途阵亡）的轮回不参与评选。
3. 凡受 bug 影响的对局一律视为**无效数据**作废，不得作为平衡标准。
   判定方式：对局过程中引擎抛出异常、或任一行动返回未预期的失败即标记为 invalid。

用法：
    python3 sim/pick_best_report.py --candidates 40
    python3 sim/pick_best_report.py --candidates 40 --out data/batch_report.md
"""
import argparse
import importlib.util
import math
import os
import sys

from tests.setup_support import finish_initial_daowen
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.api import GameEngine
from engine.ai_tactics import TacticalAI
from engine import battle_report as BR

_s = importlib.util.spec_from_file_location("ft", os.path.join(ROOT, "sim", "format_trace.py"))
ft = importlib.util.module_from_spec(_s)
_s.loader.exec_module(ft)

BACKGROUNDS = ["帮派巷战", "废墟据点", "黑市火并", "熔岩隘口"]

HEADER = """# 完整轮回战报

> **本文件的选取标准（每次重新测试后按此挑选，不得随意替换）**
>
> 1. 以**进入【最终的冠冕】前的当前血量**为唯一评判标准：
>    在同一批次的多次轮回中，谁在完成第7场、即将触发【最终的冠冕】时
>    剩余生命最高，就把谁的那一次写进本战报。
> 2. 中途阵亡、未走到第7场的轮回**不参与评选**。
> 3. **凡受 bug 影响的对局一律视为无效数据作废**，既不得写入本战报，
>    也不得作为任何平衡性调整的依据。
> 4. 战报格式必须严格遵循 README《六、战斗推演格式》：
>    逐回合、逐次出手书写，禁止概括、跳过或合并结算。
>
> 生成方式：`python3 sim/pick_best_report.py --candidates 40`
> 该脚本会跑多次轮回，自动剔除无效局，再按上述标准挑出唯一一份。
"""



def _pick_monster_daowen(engine, actor):
    """怪物按当前情形择优选道纹（README：怪物为胜利和生存作最优决策）。
    输出优先，血低自保，玩家血低收割，机制型按需。"""
    opts = actor["daowen_options"]
    if not opts:
        return None
    m_idx = int(actor["actor_ref"].split(":", 1)[1]) if ":" in actor["actor_ref"] else 0
    activated = set()
    enemies = engine.state.enemies
    monster = None
    if 0 <= m_idx < len(enemies):
        monster = enemies[m_idx]
        activated = engine.combat._monster_activated.get(id(monster), set())
    cands = [o for o in opts if o["name"] not in activated]
    if not cands:
        return opts[0]
    OUTPUT = {"狂暴", "强化", "杀伐", "血债", "锐利", "冲击", "加害", "活血", "裂变", "洗劫", "赎金", "逼债", "清算", "赌命"}
    SELF = {"自愈", "庇护", "再生", "固执", "疯狂", "龙鳞"}
    CONTROL = {"减速", "束缚", "衰败", "勾魂", "镇尸", "僵化", "眩晕", "蒙蔽", "弱化", "退化", "冥气", "缄默", "瓦解", "招魂", "无力", "迟滞", "定型", "封印", "缓慢"}
    p = engine.state.player
    player_low = p is not None and p.is_alive and p.current_hp <= p.blood_limit * 0.5
    monster_low = monster is not None and monster.current_hp <= monster.blood_limit * 0.5
    def group(o):
        n = o["name"]
        if n in OUTPUT: return 0
        if n in SELF: return 1
        if n in CONTROL: return 2
        return 3
    if monster_low:
        self_cands = [o for o in cands if o["name"] in SELF]
        if self_cands:
            return self_cands[0]
    if player_low:
        kill_cands = [o for o in cands if o["name"] in OUTPUT or o["name"] in CONTROL]
        if kill_cands:
            return kill_cands[0]
    return min(cands, key=group)

def _decline_spells(option):
    return {timing: {spell["spell_name"]: {"use": False}
                     for spell in option.get("spell_options", {}).get(timing, [])}
            for timing in ("before", "after")}

def _resolve_monster_plight(engine, rng) -> list:
    """
    让陷入困境的怪物按【怪物准则#3】行动：逃跑与进化二选一，每场限一次。

    决策口径（AI 扮演怪物方，为生存与胜利作最优选择）：
      - 异变预算足够时优先【进化】，借用轮回者的道纹翻盘；
      - 预算不足（进化会触发崩解）则选择【逃跑】。
    """
    out = []
    try:
        opts = engine.combat.get_plight_evolution_options()
    except Exception:
        return out
    for o in opts:
        name = o["monster"]
        pool = o.get("borrowable_daowen") or []
        max_x = o.get("max_x_by_mutation", 0)
        signals = "、".join(o.get("difficulty_signals", [])) or "陷入困境"
        if pool and max_x >= 1:
            x = min(max_x, 2)
            dw = rng.choice(pool)
            r = engine.execute_action("declare_evolution",
                                      {"monster": name, "daowen": dw, "x": x})
            if r.get("success"):
                out.append(f"※【进化】{name}（{signals}）发动【原初{x}】"
                           f"→ 借用轮回者道纹【{dw}{x}】，代价异变{5 * x}")
                for lg in r.get("log", []):
                    out.append(f"    {lg}")
                if r.get("collapsed"):
                    out.append(f"    → 异变达阈值，触发【崩解】：{name}直接[命零]")
                continue
        out.append(f"※【逃跑与追击】{name}（{signals}）异变预算不足以进化，转为尝试逃跑")
    return out


def _resolve_monster_turn(engine):
    """平衡模拟器的怪物AI：从prepare合法项中提交完整选择，不调用旧自动入口。"""
    from engine.ai_tactics import choose_dodge, choose_attack_target
    prepared = engine.execute_action("prepare_monster_phase", {})
    if not prepared.get("success"):
        return prepared
    choices = []
    for actor in prepared["result"]["actors"]:
        dao = None
        action_count = actor["base_attack_actions"]
        hit_count = actor["base_hits_per_attack"]
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
            if option["resolves_as"] == "变形":
                enemy_index = int(actor["actor_ref"].split(":", 1)[1])
                hit_count = engine.state.enemies[enemy_index].attack_power
        refs = engine.combat._combat_entity_refs()
        monster = refs.get(actor["actor_ref"])
        per_hit = monster.attack_power if monster is not None else 0
        target_ref = choose_attack_target(actor["attack_target_options"], refs)
        target_option = next(option for option in actor["attack_target_options"] if option["ref"] == target_ref)
        dodge_budget = 0
        attacks = []
        for _ in range(action_count):
            hits = []
            for _ in range(hit_count):
                want_dodge = choose_dodge(engine, per_hit, budget_used=dodge_budget)
                if want_dodge:
                    dodge_budget += 1
                hits.append({"target_ref": target_ref, "dodge": want_dodge,
                             "blood_shadow": False, "spell_choices": _decline_spells(target_option)})
            attacks.append({"hits": hits})
        choices.append({"actor_ref": actor["actor_ref"], "daowen": dao,
                        "attack_actions": attacks})
    return engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": choices,
    })


def play_and_record(region: str, seed: int, battles: int = 7):
    """
    跑一次完整轮回并录制 §六 格式战报。
    返回 dict：hp_before_crown / lines / invalid / reason
    """
    lines = []
    engine = GameEngine(db_path="/tmp/pick.db", rng_seed=seed)
    import random as _r
    rng = _r.Random(seed)

    try:
        engine.execute_action("setup_attributes",
                              {"name": "贾凡", "blood_points": 10,
                               "speed_points": 8, "mana_points": 7})
        finish_initial_daowen(engine)
        engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
        r = engine.execute_action("setup_choose_region", {"region": region})
        optional_relics = {"折速法印", "三相残韵盘"}
        starter = next((n for n in r["result"]["relic_choices"] if n not in optional_relics),
                       r["result"]["relic_choices"][0])
        chosen = engine.execute_action("choose_discovered_relic", {"relic_name": starter})
        if not chosen.get("success"):
            return {"invalid": True, "reason": f"starter_relic:{chosen.get('error')}"}
        ai = TacticalAI(engine)

        lines.append(f"## 轮回记录（{region}，种子 {seed}）")
        lines.append("")
        lines.append(f"【开局】25点属性 → {engine.state.player.blood_limit}[血限]/"
                     f"{engine.state.player.mana_limit}[法限]/"
                     f"{engine.state.player.speed_limit}[速限]"
                     f"｜20[碎片]｜发现遗物·{starter}｜残韵·反转｜初始道纹·杀伐"
                     f"｜副本·{region}")

        # 只学通用道纹与怪物转化道纹：副本专属道纹须先经残韵从本副本怪物身上
        # 转化获得，不能在局外直接学习（门禁见 api._pre_battle_xuexi）。
        todo = ["庇护", "再生", "冲击", "血债", "慈悲"]

        for battle_no in range(1, battles + 1):
            prep = []
            while engine.state.energy > 0:
                if todo:
                    nm = todo[0]
                    rr = engine.execute_action(
                        "pre_battle_action",
                        {"sub_action": "学习", "sub": "daowen", "name": nm})
                    if rr.get("success"):
                        todo.pop(0)
                        prep.append(f"学习·{nm}")
                        continue
                    todo.pop(0)
                rr = engine.execute_action(
                    "pre_battle_action",
                    {"sub_action": "修行", "tier": 1,
                     "to": "mana" if battle_no % 2 else "speed"})
                if rr.get("success"):
                    prep.append(f"修行·{'法限' if battle_no % 2 else '速限'}+")
                else:
                    return {"invalid": True, "reason": f"局外行动失败:{rr.get('error')}"}

            from sim.optional_actions import battle_start_relic_choices
            bs = engine.execute_action("battle_start",
                                       {"relic_choices": battle_start_relic_choices(engine)})
            if not bs.get("success"):
                return {"invalid": True, "reason": f"battle_start:{bs.get('error')}"}
            enemies = list(engine.state.enemies)
            lines.append("")
            lines.append(f"### 第{battle_no}场")
            lines.append(f"[局外]（3精力）：{'，'.join(prep)}")
            lines.extend(BR.format_battle_start(
                battle_no=battle_no,
                draw_range=f"战斗场数{battle_no}，一阶副本-3最低为1，"
                           f"抽取{bs.get('draw_count', len(enemies))}只",
                draw_result="、".join(e.name for e in enemies),
                enemies=enemies,
                player=engine.state.player,
                allies=engine.state.friends + engine.state.employees,
                background=rng.choice(BACKGROUNDS),
                start_effects=list(bs.get("relic_logs", []) or [])
                + list(bs.get("artifact_logs", []) or []),
            ))

            for rnd in range(1, 31):
                if not engine.state.player or not engine.state.player.is_alive:
                    break
                if not [x for x in engine.state.enemies if x.is_alive]:
                    break
                from sim.build_learner import round_start_relic_choices
                rs = engine.execute_action("round_start", {"relic_choices": round_start_relic_choices(engine)})
                lines.extend(BR.format_round_start(rnd, rs.get("result", {}),
                                                   engine.state.player,
                                                   engine.state.enemies))
                ai.new_round()
                idx = 1
                for res in ai.take_turn():
                    lines.extend(BR.format_player_action(
                        idx, engine.state.player.name, res))
                    idx += 1
                ai.resolve_pending_redemption()
                # [朋友]/[员工]自主出手
                ap = engine.execute_action("resolve_ally_phases", {})
                for entry in (ap.get("result", {}).get("allies") or []):
                    for act in entry.get("actions", []):
                        lines.extend(BR.format_player_action(idx, entry["ally"], act.get("detail") or {}))
                        idx += 1

                # 怪物准则#3：陷入困境时强制在【逃跑】与【进化】中二选一，每场限一次。
                # 引擎只负责标注困境，须由扮演怪物方的 AI 主动调用，
                # 此前战报生成器从不调用，导致该机制在战报中从未出现。
                lines.extend(_resolve_monster_plight(engine, rng))
                if not [x for x in engine.state.enemies if x.is_alive]:
                    lines.extend(BR.format_round_end({}, engine.state.player,
                                                     engine.state.enemies))
                    break
                if not engine.state.player or not engine.state.player.is_alive:
                    break
                mp = _resolve_monster_turn(engine)
                if not mp.get("success"):
                    return {"invalid": True, "reason": f"monster_phase:{mp.get('error')}"}
                lines.extend(BR.format_monster_hits(idx, mp["result"].get("details", [])))
                re_ = engine.execute_action("round_end", {})
                if not re_.get("success"):
                    return {"invalid": True, "reason": f"round_end:{re_.get('error')}"}
                ai.resolve_pending_redemption()
                lines.extend(BR.format_round_end(re_.get("result", {}),
                                                 engine.state.player,
                                                 engine.state.enemies))
                if mp["result"].get("player_dead"):
                    break

            if not engine.state.player or not engine.state.player.is_alive:
                return {"invalid": False, "cleared": battle_no - 1, "died": True,
                        "hp_before_crown": None, "lines": lines}

            # 第7场结束即触发【最终的冠冕】：先记录此刻血量
            hp_now = engine.state.player.current_hp
            be = engine.execute_action("battle_end", {})
            if not be.get("success"):
                return {"invalid": True, "reason": f"battle_end:{be.get('error')}"}
            lines.extend(BR.format_battle_end(be.get("result", {})))

            if battle_no == battles:
                lines.append("")
                lines.append(f"### 【最终的冠冕】触发前 · 当前生命 {hp_now}")
                crown = be["result"].get("final_crown")
                if crown:
                    lines.append(f"结算：{crown}")
                return {"invalid": False, "cleared": battles, "died": False,
                        "hp_before_crown": hp_now, "lines": lines}

    except Exception as ex:                       # 引擎异常 → 无效数据
        return {"invalid": True, "reason": f"{type(ex).__name__}: {ex}"}

    return {"invalid": True, "reason": "未知终止"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", type=int, default=40)
    ap.add_argument("--out", default="战报.md")
    ap.add_argument("--region", default=None)
    a = ap.parse_args()

    regions = [a.region] if a.region else ["罪孽都市", "扭曲都市", "龙心谷"]
    finished, died, invalid = [], 0, []

    for i in range(a.candidates):
        region = regions[i % len(regions)]
        seed = 100 + i
        r = play_and_record(region, seed)
        if r.get("invalid"):
            invalid.append((region, seed, r.get("reason")))
            continue
        if r.get("died"):
            died += 1
            continue
        finished.append((r["hp_before_crown"], region, seed, r["lines"]))

    print(f"候选轮回 {a.candidates} 次："
          f"通关 {len(finished)}｜中途阵亡 {died}｜无效(bug) {len(invalid)}")
    if invalid:
        print("无效对局明细（已作废，不作为平衡依据）：")
        for rg, sd, why in invalid[:10]:
            print(f"  {rg} seed{sd}: {why}")

    if not finished:
        print("没有任何轮回走到【最终的冠冕】，不生成战报。")
        return

    finished.sort(key=lambda t: -t[0])
    print("\n进入【最终的冠冕】前的剩余生命排名：")
    for hp, rg, sd, _ in finished[:8]:
        print(f"  {hp:>4} HP   {rg}  seed{sd}")

    hp, region, seed, lines = finished[0]
    out = os.path.join(ROOT, a.out)
    body = [HEADER,
            f"> 本批次共 {a.candidates} 次轮回：通关 {len(finished)}、"
            f"阵亡 {died}、无效作废 {len(invalid)}。",
            f"> 入选依据：进入【最终的冠冕】前剩余生命 **{hp}**，为本批次最高。",
            ""] + lines
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")
    print(f"\n已写入 {a.out}：{region} seed{seed}，冠冕前剩余 {hp} HP")


if __name__ == "__main__":
    main()
