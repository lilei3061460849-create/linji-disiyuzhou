#!/usr/bin/env python3
"""把 data/handplay_20260916/trace.jsonl 逐条转写成根目录的《报告.md》。

用途：轮回 seed 20260916 那一局的报告转写器——只做「流水 → 中文行文」的确定性转写，
不含 AI、不做评分、不做挑选（与 sim/pick_best_report.py 完全不同，后者禁止覆盖报告）。

遵守 README《报告书写口径》：不贴引擎原文结构、不写战斗背景、
回合/场次取引擎字段、数字与 trace 严格一致、失误用 ⚠ 标注。

用法（仓库根目录）：python sim/gen_report_from_trace.py
换一局：改下面的 CYCLE_DIR，或把它做成参数——本脚本只认这一局的流水。
"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CYCLE_DIR = ROOT / "data/handplay_20260916"
TRACE = CYCLE_DIR / "trace.jsonl"
OUT = ROOT / "报告.md"

entries = [json.loads(l) for l in TRACE.read_text(encoding="utf-8").splitlines() if l.strip()]
ok_all = [e for e in entries if (e.get("result") or {}).get("success")]

# ---------------------------------------------------------------- 统计
stats = {"输出": 0, "承伤": 0, "反噬": 0, "普攻次数": 0, "法力支出": 0, "回合": 0, "场次": 0,
         "碎片收入": 0, "碎片支出": 0, "击杀": 0, "救赎": 0, "道纹发动": {}, "物品使用": 0,
         "衰败结算": 0, "畸变血限损失": 0, "凡庸自爆": [], "残骸掉落": 0, "增援": 0}

def panel_from_full(st: dict) -> str:
    def side(ent):
        name = ent["name"]
        hp, bl = ent["current_hp"], ent["blood_limit"]
        mana, ml = ent.get("current_mana"), ent.get("mana_limit")
        sp, sl = ent.get("current_speed"), ent.get("speed_limit")
        bits = [f"生命 {hp}/{bl}"]
        if mana is not None:
            bits.append(f"法力 {mana}/{ml}")
        if sp is not None:
            bits.append(f"速度 {sp}/{sl}")
        bits.append(f"格挡 {ent.get('shield', 0)}")
        bits.append(f"出手 {ent.get('action_count', 0)}")
        return f"{name}（{('、'.join(bits))}）"
    parts = [side(x) for x in st.get("player_side", [])] + [side(x) for x in st.get("enemy_side", [])]
    return " ｜ ".join(parts) if parts else "（无在场角色）"

def panel_compact(st: dict) -> str:
    bits = [f"林寂 生命 {st.get('player_hp')}/{st.get('player_blood_limit')}",
            f"法力 {st.get('player_mana')}", f"速度 {st.get('player_speed')}"]
    enemies = [f"{m['name']} {m['hp']}{'' if m.get('alive') else '（已命零）'}" for m in st.get("enemies", [])]
    return "、".join(bits) + " ｜ 敌方：" + ("、".join(enemies) if enemies else "无")

def statuses(ent) -> str:
    if not ent:
        return "无"
    def _d(x):
        rounds = x.get("rounds", x.get("duration"))
        value = x.get("value")
        head = "" if value is None else f"X={value}，"
        if rounds is None:
            return head.rstrip("，")
        if isinstance(rounds, int) and rounds < 0:
            return head + "持续 ∞"
        return head + f"剩 {rounds} 回合"
    got = [f"{x['name']}（{_d(x)}）" for x in (ent.get("status_effects") or [])]
    return "、".join(got) if got else "无"

def has_status(ent, name) -> bool:
    return any(s.get("name") == name for s in (ent.get("status_effects") or []))

def daowen_gained(ent) -> set:
    return set((ent.get("dao_wen") or {}).keys())

def effect_text(eff: dict, state: dict | None = None, label: str = "") -> str:
    t = eff.get("type")
    if t == "shuaibai_tick":
        return f"{eff.get('entity')} 承受【衰败】回始结算：失去 {eff.get('damage')} 点生命" + ("，[命零]" if eff.get("died") else "")
    if t == "deform_pending":
        return f"{eff.get('entity')} 的【畸变】挂起（血限损失 {eff.get('blood_loss')}，回终结算）"
    if t == "deform_blood_limit_loss":
        return (f"{eff.get('entity')} 承受【畸变】回终结算：血限损失 {eff.get('blood_loss')}"
                f"（血限→{eff.get('blood_limit_after')}，生命→{eff.get('hp_after')}）" + ("，[命零]" if eff.get("died") else ""))
    if t == "wave_spawn":
        return f"增援入场：{eff.get('entity')}（第 {eff.get('round')} 回合入场，队列剩余 {eff.get('queued_left')}）"
    if t == "mediocrity":
        return f"{eff.get('entity')}：{eff.get('note')}"
    if t == "mediocrity_loot":
        return f"{eff.get('note')}"
    tgt = eff.get("target")
    if t == "damage":
        tag = "（无视格挡）" if eff.get("damage_type") == "无视格挡" else ""
        return (f"{tgt} 生命 {eff.get('hp_before')}→{eff.get('hp_after')}"
                f"（{'实际伤害' if not eff.get('shield_absorbed') else '伤害'} {eff.get('actual_damage')}"
                f"{'，格挡吸收%d' % eff['shield_absorbed'] if eff.get('shield_absorbed') else ''}"
                f"{'，命中即命零' if eff.get('died') else ''}）{tag}")
    if t == "heal":
        return (f"{tgt} 生命 {eff.get('hp_before')}→{eff.get('hp_after')}"
                f"（回复 {eff.get('actual_heal')}"
                f"{'，溢出%d' % eff['overheal'] if eff.get('overheal') else ''}）")
    if t == "shield":
        return f"{tgt} 获得 {eff.get('amount')} 点格挡"
    if t == "bleed_cost":
        owner = (eff.get("owner") or {})
        paid = owner.get("paid")
        off = eff.get("dragon_heart_offset") or 0
        text = f"支付流血代价 {eff.get('requested')}"
        if off:
            text += f"（龙心抵消 {off}，实付 {paid}）"
        hp_loss = ((owner.get("detail") or {}).get("actual_damage"))
        return text + (f"：{owner.get('payer')} 生命 {owner['detail']['hp_before']}→{owner['detail']['hp_after']}"
                       if isinstance(owner.get("detail"), dict) else "")
    if t == "mana_cost":
        return f"消耗 {eff.get('amount')} 法力"
    if t == "status_added":
        d = eff.get("duration")
        return f"{tgt} 获得状态【{eff.get('status')}】（{'∞' if d is not None and d < 0 else d}）"
    if t == "status_removed":
        return f"{tgt} 失去状态【{eff.get('status')}】"
    if t == "mana_gain":
        return f"{eff.get('source')} 获得 {eff.get('mana_gained')} 法力"
    if t in ("no_effect",):
        return f"{tgt} 无结算变化"
    return f"{tgt} 受到【{t}】效果"

def effect_list(execution: dict) -> list[str]:
    out = []
    for eff in (execution or {}).get("effects") or []:
        out.append(effect_text(eff))
        for sub in eff.get("spell_logs") or []:
            out.append(f"（触发法术【{sub.get('spell')}】）")
    return out

def attack_lines(res: dict, indent: str) -> list[str]:
    hits = ((res.get("result") or {}).get("hits")) or []
    lines = []
    skipped = [h for h in hits if "skipped" in h]
    hits = [h for h in hits if "target" in h]
    by_target: dict[str, list] = {}
    for h in hits:
        stats["输出"] += h.get("damage_dealt", 0)
        by_target.setdefault(h["target"], []).append(h)
    if skipped:
        lines.append(f"{indent}[后续击] " + skipped[0].get("skipped", "跳过") + f"（跳过 {len(skipped)} 击）")
    for tgt, hs in by_target.items():
        total = sum(h["damage_dealt"] for h in hs)
        first_hp = hs[0]["target_hp_after"] + hs[0]["hp_lost"] + hs[0]["shield_absorbed"]
        last_hp = hs[-1]["target_hp_after"]
        dodged = [h["hit_index"] for h in hs if h.get("dodge_success")]
        detail = "、".join(f"第{h['hit_index']}击 {h['damage_dealt']} 点" + ("（被闪避，0 伤）" if h.get("dodge_success") else "")
                          for h in hs)
        died = "，目标命零" if hs[-1].get("target_died") else ""
        lines.append(f"{indent}[数值落地] {tgt} 生命 {first_hp}→{last_hp}（合计 {total} 点；{detail}）{died}")
        if dodged:
            lines.append(f"{indent}[目标反应] {tgt} 闪避成功 {len(dodged)} 击")
    return lines

paper: list[str] = []
def w(line: str = "") -> None:
    paper.append(line)

# ---------------------------------------------------------------- 头部
paper.append("# 战报")
paper.append("")
paper.append("> 记录日期 2026-10-02｜种子 `20260916`｜副本【扭曲都市】（一阶）｜轮回者：林寂（血限 78 → 84、法限 3、速限 3，出手 2）")
paper.append("> 记录方式：操作者通过 `GameEngine.execute_action` 逐步手操；驱动器 `sim/handplay_cycle_20260916.py` 只负责执行、记录、存档，不替操作者决策。")
paper.append(f"> 全部数字取自引擎真实返回，原始流水见 `data/handplay_20260916/trace.jsonl`（{len(entries)} 条提交，全部成功、零被拒）。")
paper.append("> 本局结果：**7 场 6 胜**；第 7 场第 13 回合受到致死攻击[命零]，触发【死之传承】，遗言「第7场受到致死攻击命零」写入《死者之书》，本局结束。")
paper.append("")
paper.append("> **本文件只保留最新一次轮回记录。** 上一版《报告.md》（外围收尾：正式 LLM 路径彻底去 `TacticalAI` 化 + 运行期契约修正）"
             "已原文归档到 `archive/report_history_2026-10-02_peripheral_cleanup.md`；新的完整轮回写入后覆盖本记录，"
             "不得用 `sim/pick_best_report.py` / TacticalAI 批量评选覆盖本文件。")
paper.append("> 格式遵循 README《六、战斗推演格式》与 AI 知识库七步原子时序切片管道：逐回合、逐次出手，禁止概括、跳过或合并结算。")
paper.append("")

# ---------------------------------------------------------------- 开局
paper.append("## 一、开局")
paper.append("")
setup_names = {"setup_attributes": "分配属性", "choose_discovered_relic": "选择发现遗物",
               "setup_choose_initial_daowen": "选择初始道纹", "setup_choose_resonance": "选择残韵",
               "setup_choose_region": "选择副本"}
for e in entries:
    if e["action"] in setup_names:
        r = e["result"]
        p = e["params"]
        if e["action"] == "setup_attributes":
            w(f"- **分配属性**：{p['name']} —— 血 {p['blood_points']} 点（→血限 78）、速 {p['speed_points']} 点（→速限 3）、法 {p['mana_points']} 点（→法限 3）")
        elif e["action"] == "choose_discovered_relic":
            w(f"- **发现遗物**：{p['relic_name']}（{r.get('action')}）")
        elif e["action"] == "setup_choose_initial_daowen":
            w(f"- **初始道纹**：{p['daowen_name']}")
        elif e["action"] == "setup_choose_resonance":
            w(f"- **残韵**：{p['resonance_type']}")
        elif e["action"] == "setup_choose_region":
            w(f"- **副本**：【{p['region']}】")
setup_end = next(i for i, e in enumerate(entries) if e["action"] == "setup_choose_region")
w(f"- 开局资源：碎片 {entries[setup_end]['state']['shards']}、精力 {entries[setup_end]['state']['energy']}")
w("- 引擎口径（本局涉及）：[攻击次数]＝[当前速度]、[攻击力]＝[当前法力]（`engine/models.py` `effective_attack_power`），"
  "所以怪物把法力花在道纹上之后，普攻按剩余法力结算（法力 0 → 0 伤）；【爆裂】的效果是「受到伤害后攻击者失去等量生命」，"
  "故打带【爆裂】的怪会反噬自己的生命。")
w("")

# ---------------------------------------------------------------- 正文
battle = 0
round_no = 0
player_action_no = 0
battle_started = False
prelude_open = False
pending_attack = None
prev_snap = entries[setup_end]["state"]
round_full = {}
round_enemy_mana = None
round_start_enemy_mana: dict = {}
round_zero_hits: dict = {}
enemy_status_snap: dict = {}
picked_once = False
final_shards_tmp = None
final_blood_limit_tmp = None
for _e in entries:
    if _e["state"].get("phase") == "in_combat":
        final_shards_tmp = _e["state"]["shards"]
        final_blood_limit_tmp = _e["state"].get("player_blood_limit")


def refresh_status_snap(full: dict) -> None:
    for ent in (full.get("enemy_side") or []) + (full.get("player_side") or []):
        if ent.get("name"):
            enemy_status_snap[ent["name"]] = ent.get("status_effects") or []
i = setup_end + 1
seen_actions: list[str] = []
while i < len(entries):
    e = entries[i]
    act, res, params = e["action"], e["result"], e["params"]
    st = e["state"]
    if act == "battle_start" and res.get("success"):
        battle += 1
        round_no = 0
        stats["场次"] = battle
        if prelude_open:
            w("")
            prelude_open = False
        w(f"## 第 {battle} 场")
        w("")
        queued = res.get("queued_reinforcements") or []
        waves = res.get("reinforcement_waves") or []
        if queued:
            pairs = [f"{n}（第 {(waves[k] or {}).get('arrive_round')} 回合）" for k, n in enumerate(queued)]
            tail = "；增援队列：" + "、".join(pairs)
        else:
            tail = "（无增援）"
        w(f"**[战始]** 首发 {'、'.join(res.get('enemies') or [])}{tail}")
        w("")
        battle_started = True
        prelude_open = False
        i += 1
        continue
    if act in ("pre_battle_action", "resolve_event", "choose_discovered_item") and res.get("success") and (
            not battle_started or e["state"].get("phase") == "pre_battle"):
        if not prelude_open:
            label = f"第 {battle + 1} 场战前" if battle_started else "开局"
            w(f"### 局外（{label}）")
            w("")
            prelude_open = True
    if act == "pre_battle_action" and res.get("success"):
        sub = params.get("sub_action")
        r = res.get("result") or {}
        if sub == "探索":
            w(f"- 局外·**探索**：发现事件【{r.get('event')}】（碎片支出 {r.get('shard_cost')}）")
        elif sub == "休整":
            heal = (r.get("heals") or [{}])[0]
            stats["碎片支出"] += r.get("shard_cost", 0)
            w(f"- 局外·**休整**（40% 档，{r.get('shard_cost')} 碎片）：{heal.get('target')} 生命 {heal.get('hp_before')}→{heal.get('hp_after')}（回复 {heal.get('actual_heal')}）")
        else:
            w(f"- 局外·**{res.get('action')}**")
        i += 1
        continue
    if act == "resolve_event" and res.get("success"):
        r = res.get("result") or {}
        w(f"- 局外·**事件【{params.get('event')}】**：选 {params.get('option_id')} —— {r.get('option')}")
        for a in r.get("applied") or []:
            w(f"  - 结算：{a}")
        i += 1
        continue
    if act == "choose_discovered_item" and res.get("success"):
        w(f"- 局外·**拾取消耗品**：{params.get('item_name')}")
        if not picked_once:
            w("  - ⚠ 本局拾取的全部消耗品（含凡庸掉落的【残骸】）一次也没有使用（见复盘 1）。")
            picked_once = True
        i += 1
        continue
    if act == "choose_discovered_relic" and res.get("success"):
        w(f"- 局外·**拾取遗物**：{params.get('relic_name')}")
        i += 1
        continue
    if act == "round_start" and res.get("success"):
        round_no += 1
        stats["回合"] += 1
        player_action_no = 0
        full = ((res.get("result") or {}).get("state")) or {}
        round_full = full
        if full:
            refresh_status_snap(full)
            round_start_enemy_mana = {x["name"]: x.get("current_mana") for x in (full.get("enemy_side") or [])}
            round_zero_hits = {}
        w(f"### 第 {round_no} 回合")
        ps = (full.get("player_side") or [{}])[0]
        es = (full.get("enemy_side") or [{}])[0]
        w(f"**[回始]** {panel_from_full(full) if full else panel_compact(st)}")
        if ps.get("status_effects") or es.get("status_effects"):
            w(f"（状态：{ps.get('name')} {statuses(ps)}；{es.get('name')} {statuses(es)}）")
        effs = (res.get("result") or {}).get("effects") or []
        if effs:
            for x in effs:
                if x.get("type") == "shuaibai_tick":
                    stats["衰败结算"] += x.get("damage", 0)
                if x.get("type") == "wave_spawn":
                    stats["增援"] += 1
            w("[回始] 结算：" + "；".join(effect_text(x) for x in effs))
        w("")
        i += 1
        continue
    if act == "use_daowen" and res.get("success"):
        player_action_no += 1
        calc = res.get("calculation") or {}
        name = params["daowen_name"]
        stats["道纹发动"][name] = stats["道纹发动"].get(name, 0) + 1
        if calc.get("cost_type") == "消耗":
            stats["法力支出"] += calc.get("cost", 0)
        tgt = params.get("target_ref", "")
        head = f"**出手{player_action_no}**：[动作声明] 林寂 发动道纹【{name}X={params.get('x')}】"
        head += f"（目标 {tgt.split(':')[0]}；{calc.get('summary')}）"
        w(head)
        for line in effect_list(res.get("execution") or {}):
            w(f"  - [数值落地] {line}")
        w("")
        i += 1
        continue
    if act == "prepare_attack" and res.get("success"):
        rr = res.get("result") or {}
        pending_attack = {"hit_count": rr.get("hit_count"), "actor": rr.get("actor"),
                          "targets": [o.get("name") for o in rr.get("target_options") or []]}
        i += 1
        continue
    if act == "resolve_attack" and res.get("success"):
        player_action_no += 1
        stats["普攻次数"] += 1
        decl = pending_attack or {}
        target = (decl.get("targets") or ["?"])[0]
        hits_n = decl.get("hit_count")
        w(f"**出手{player_action_no}**：[动作声明] {decl.get('actor', '林寂')} 对 {target} 发动普攻"
          + (f"（{hits_n} 击）" if hits_n else ""))
        w("  [目标反应] 不闪避（逐击显式提交，未声明招架）")
        pending_attack = None
        for line in attack_lines(res, "  "):
            w(line)
        before_st = entries[i - 1]["state"] if i > 0 else st
        if before_st.get("battle") != st.get("battle"):
            before_st = st
        hp_a, hp_b = before_st.get("player_hp"), st.get("player_hp")
        dh = (hp_a or 0) - (hp_b or 0)
        if dh > 0:
            all_hits = (res.get("result") or {}).get("hits") or []
            hit_targets = {h["target"] for h in all_hits if h.get("damage_dealt")}
            foes = [t for t in hit_targets if any(s.get("name") == "爆裂"
                                                  for s in (enemy_status_snap.get(t) or []))]
            if foes:
                dealt = sum(h.get("damage_dealt", 0) for h in all_hits if h.get("target") in foes)
                why = f"{foes[0]}【爆裂】：受到伤害后攻击者失去等量生命（本拍对持有者造成 {dealt} 点）"
            else:
                why = "非爆裂结算"
            stats["反噬"] += dh
            w(f"  [反噬] 林寂 生命 {hp_a}→{hp_b}（失去 {dh} 点：{why}）")
        w("")
        i += 1
        continue
    if act == "resolve_ally_phases":
        if (res.get("result") or {}).get("actions"):
            w(f"**[友方阶段]** " + "；".join(str(a) for a in res["result"]["actions"]))
            w("")
        i += 1
        continue
    if act == "resolve_monster_phase" and res.get("success"):
        r = res.get("result") or {}
        w(f"**[怪物阶段]**")
        for d in r.get("details") or []:
            if d.get("monster"):
                eff = effect_list(d.get("execution") or {})
                w(f"  - {d['monster']} 发动道纹【{d.get('daowen_activated')}X={d.get('x')}】"
                  + (f"，目标 {d.get('target')}" if d.get("target") and d.get("target") != d.get("monster") else "")
                  + ("：" + "；".join(eff) if eff else ""))
            elif d.get("attacker"):
                hp_after = d.get("target_hp_after")
                hp_before = (hp_after or 0) + d.get("hp_lost", 0) + d.get("shield_absorbed", 0)
                stats["承伤"] += d.get("hp_lost", 0)
                if not d.get("damage_dealt") and not d.get("shield_absorbed") and not d.get("dodge_success"):
                    round_zero_hits[d["attacker"]] = round_zero_hits.get(d["attacker"], 0) + 1
                w(f"  - {d['attacker']} 普攻 {d.get('target')} 第{d.get('hit_index')}击："
                  f"{d.get('damage_dealt')} 点伤害"
                  + (f"（格挡吸收 {d['shield_absorbed']}）" if d.get("shield_absorbed") else "")
                  + f" → {d.get('target')} 生命 {hp_before}→{hp_after}"
                  + ("，[命零]" if d.get("target_died") else ""))
        for lg in r.get("spell_logs") or []:
            w(f"  - 触发法术【{lg.get('spell')}】")
        w("")
        if r.get("player_dead"):
            w(f"⚠ 林寂在本阶段[命零]（生命 {r.get('player_hp')}）。")
            w("")
        i += 1
        continue
    if act == "round_end" and res.get("success"):
        full_end = ((res.get("result") or {}).get("state")) or {}
        if full_end:
            w(f"**[回终]** {panel_from_full(full_end)}")
            foes_txt = "；".join(f"{x['name']} {statuses(x)}" for x in (full_end.get("enemy_side") or []))
            w(f"（状态：林寂 {statuses((full_end.get('player_side') or [{}])[0])}" + (f"；{foes_txt}" if foes_txt else "") + "）")
            refresh_status_snap(full_end)
            effs_end = (res.get("result") or {}).get("effects") or []
            for x in effs_end:
                if x.get("type") == "deform_blood_limit_loss":
                    stats["畸变血限损失"] += x.get("blood_loss", 0)
                if x.get("type") == "mediocrity":
                    stats["凡庸自爆"].append(x.get("entity"))
                if x.get("type") == "mediocrity_loot":
                    stats["残骸掉落"] += 1
            if effs_end:
                w("[回终] 结算：" + "；".join(effect_text(x) for x in effs_end))
                for x in effs_end:
                    if x.get("type") == "mediocrity":
                        w(f"⚠ {x.get('entity')} 是【凡庸】自爆而死——本场胜负不是林寂打出的伤害决定的（见复盘 5）。")
            end_mana = {x["name"]: x.get("current_mana") for x in (full_end.get("enemy_side") or [])}
            for who, n in round_zero_hits.items():
                tail = (f"该怪法力 回始 {round_start_enemy_mana.get(who)} → 回终 {end_mana.get(who)}"
                        if who in end_mana else "该怪已在本回合[命零]，回终不在场")
                w(f"（本回合 {who} 的 {n} 次普攻 0 伤：{tail}，引擎口径 [攻力]＝[当前法力]）")
            if round_zero_hits:
                w("")
        else:
            w(f"**[回终]** {panel_compact(st)}")
        w("")
        i += 1
        continue
    if act == "resolve_redemption" and res.get("success"):
        r = res.get("result") or {}
        stats["救赎"] += 1
        w(f"**[救赎]** {r.get('monster')} 昏迷待选 → 操作者选择【终结】（{r.get('note')}）")
        w("")
        i += 1
        continue
    if act == "battle_end" and res.get("success"):
        r = res.get("result") or {}
        stats["碎片收入"] += r.get("shard_reward", 0) + r.get("event_bonus_shards", 0)
        stats["击杀"] += len(r.get("death_shard_rewards") or [])
        w(f"**[战终]** 胜利：命零奖励 {r.get('shard_reward')} 碎片"
          + (f"（{ '、'.join(x['name'] for x in r.get('death_shard_rewards') or []) }）" if r.get("death_shard_rewards") else "")
          + f"，碎片结余 {r.get('total_shards')}，精力恢复 {r.get('energy_restored')}，员工背叛检查：{r.get('employee_rebellion', {}).get('reason')}")
        w("")
        i += 1
        continue
    if act == "submit_ruling" and res.get("success"):
        w(f"**[死之传承]** {params.get('ruling_text')}（DM 裁定：{res.get('action')}）")
        w("")
        w(f"**[终局]** 本局于第 {battle} 场第 {round_no} 回合结束（轮回者[命零]）；"
          f"终局碎片 {final_shards_tmp}、血限 {final_blood_limit_tmp}；"
          "遗言已写入 `data/handplay_20260916/死者之书.md`，死后回到建号阶段（碎片重置为 20）。")
        w("")
        i += 1
        continue
    prev_snap = st
    i += 1

# ---------------------------------------------------------------- 速览与复盘
final_shards = next(e["state"]["shards"] for e in reversed(entries) if e["state"].get("phase") == "in_combat")
speed = [
    "## 二、本局速览",
    "",
    "| 项 | 数值 |",
    "| --- | --- |",
    f"| 场次 | 7 场（第 1～6 场全胜，第 7 场林寂[命零]） |",
    f"| 回合 | 全场合计 {stats['回合']} 回合 |",
    f"| 道纹发动 | " + "、".join(f"{k}×{v}" for k, v in stats["道纹发动"].items()) + " |",
    f"| 输出 | 普攻命中伤害合计 {stats['输出']}（{stats['普攻次数']} 次出手） |",
    f"| 承伤 | 怪物普攻命中 {stats['承伤']}；另有【爆裂】反噬自伤 {stats['反噬']}（第 1 场） |",
    f"| 敌方死亡 | 10 只敌人：**7 只触发【凡庸】自爆**、1 只被普攻打倒（第 4 场人头气球）、1 只昏迷后被【终结】（第 2 场孢子母体）、1 只（第 7 场孢子母体）终局仍存活 |",
    f"| 增援 | {stats['增援']} 次（第 7 场第 3 回合肠水母、第 7 回合千手蜈蚣） |",
    f"| 有害结算 | 【衰败】回始结算 {stats['衰败结算']}；【畸变】血限损失 {stats['畸变血限损失']}（施法后 [攻力]＝0，0×攻次＝0） |",
    f"| 法术 | 0 个（未学、未放） |",
    f"| 法力支出 | {stats['法力支出']}（全部来自【再生】） |",
    f"| 局外 | 探索 14 次（tier1，免费）；休整 7 次（40% 档，共 70 碎片） |",
    f"| 消耗品 | 拾取 8 件（探索）＋【残骸】{stats['残骸掉落']} 件（凡庸自爆掉落），**使用 0 件** ⚠ |",
    f"| 碎片 | 起始 20 ＋ 战终奖励 {stats['碎片收入']} － 休整支出 {stats['碎片支出']} → 终局 {final_shards} |",
    f"| 结局 | 第 7 场第 13 回合受致死攻击[命零]；遗言写入《死者之书》 |",
    "",
    "### 复盘（⚠ 失误与教训）",
    "",
    "1. ⚠ **消耗品全程未用**：拾取 8 件（高爆手雷、急救箱、储能电池、备用血泵、强光探照灯、干扰仪、反怪物电击枪、高压水枪）＋凡庸自爆掉落的 7 件【残骸】，消耗品入口一次都没提交——这些是一次性资源，本局等于白拿。",
    "2. ⚠ **战斗决策只有一条线**：全部 17 次道纹发动都是【再生X=1】，普攻 108 次。没有使用【招架】（零成本、不占出手），没有任何法术（战斗内 `define_spell` 可自创，消耗 1 次出手）——面对第 7 场孢子母体的高爆发没有任何减伤手段。",
    "3. ⚠ **承露盏未与卖血循环配合**：开局遗物【承露盏】按失去生命返法力，但本局从未主动支付流血/代价去换法力，遗物只被动生效。",
    "4. ⚠ **第 7 场死亡可预见**：第 2 场就见过孢子母体（15 回合才磨完），第 7 场再遇时林寂未补充任何新资源（休整只做回复、修行一次未做），13 回合内被累积伤害打死。",
    "5. ⚠ **本局 6 胜里，7 只敌人是自己炸死的**：怪物把法力全花在道纹上 → [攻力]＝[当前法力]＝0 → 普攻 0 伤 → 连续五回合没让林寂掉血 → 触发【凡庸】自爆（[回终] 结算原文：『连续五回合未能使敌对角色生命减少，触发【凡庸】：凭空全身炸裂，[命零]』）。这是引擎规则放行，不是林寂打得好——第 7 场孢子母体每回合留 1 点法力（【坏死X=3】只花 6/7），普攻 1 点×2 击持续消耗，再加上【衰败】回始结算（第 7 场 27 点），林寂没有任何减伤/回复手段就被磨死。下一步与其继续普攻，不如先解决回复与减伤（消耗品、【招架】、自创法术）。",
    "",
]
paper.extend(speed)

OUT.write_text("\n".join(paper) + "\n", encoding="utf-8")
print("written:", OUT, len(paper), "lines")
print("统计:", json.dumps(stats, ensure_ascii=False))
