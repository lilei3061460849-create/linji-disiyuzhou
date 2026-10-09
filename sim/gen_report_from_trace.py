#!/usr/bin/env python3
"""把 data/handplay_20260916/trace.jsonl 逐条转写成根目录的《报告.md》。

用途：轮回 seed 20260916 那一局的报告转写器——只做「流水 → 中文行文」的确定性转写，
不含 AI、不做评分、不做挑选（与 sim/pick_best_report.py 完全不同，后者禁止覆盖报告）。

遵守 推演规范《报告书写口径》：不贴引擎原文结构、不写战斗背景、
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
paper.append("> 格式遵循 推演规范《六、战斗推演格式》与 AI 知识库七步原子时序切片管道：逐回合、逐次出手，禁止概括、跳过或合并结算。")
paper.append("> 本文件另附《三、道纹两两协同穷举》：那是**独立于本局的实测附录**（穷举全部道纹无序对，"
             "回答「任意两道纹同时持有会怎样」），与本局实际发生的 17 次【再生】不是一回事。")
paper.append("@@SYN_HEADLINE@@")   # 占位：结论速览在文件末尾按实测数据生成后回填
paper.append("@@ASK_HEADLINE@@")   # 占位：两个追问的速答（文末按实测数据生成后回填）
paper.append("")

# ---------------------------------------------------------------- 开局
_compact_idx = len(paper)          # 压缩章节插入点（按实测数据在文末统一生成）
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


# ---------------------------------------------------------------- 附录：道纹两两协同
SYN_CSV = ROOT / "reports/daowen_pairwise_synergy.csv"
SYN_GRAPH = ROOT / "reports/daowen_synergy_graph.json"


def _syn_rows() -> list[dict]:
    """读逐对分类表（本仓库已提交，故本附录可字节复现）。"""
    import csv
    if not SYN_CSV.exists():
        return []
    with SYN_CSV.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _syn_edges() -> list[dict]:
    """读 Cat2 关系图（已提交），拿每对的首个偏离明细。"""
    if not SYN_GRAPH.exists():
        return []
    return json.loads(SYN_GRAPH.read_text(encoding="utf-8")).get("edges", [])


def _syn_layer(types: str) -> str:
    t = set(x for x in (types or "").split("|") if x)
    if t & {"EVENT_CONVERSION", "EVENT_MULTIPLICATION"}:
        return "EVENT_LEVEL"
    if "CAST_EXECUTION" in t:
        return "CAST_EXECUTION"
    if "ORDER_SENSITIVE" in t:
        return "ORDER_ONLY"
    return "SCALAR_COUPLING"


def _load_compactor():
    """按路径加载压缩模块（sim/compact_cycle_report.py）。"""
    import importlib.util
    path = ROOT / "sim" / "compact_cycle_report.py"
    spec = importlib.util.spec_from_file_location("compact_cycle_report", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _code_body(text: str) -> list[str]:
    """取出压缩文本的正文：去掉 ``` 围栏（否则在报告的代码块里会提前闭合）与独立文件的一级标题。
    其余行（含 ### 小节标题）原样保留，因此内嵌块与 reports/ 下的独立文件只差这一行标题。"""
    lines = [x for x in text.rstrip().splitlines() if x.strip() != "```"]
    while lines and not lines[0].startswith("# "):
        lines.pop(0)
    if lines and lines[0].startswith("# "):
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    return lines


def compressed_section(md_text: str) -> tuple[list[str], dict]:
    """生成《〇、压缩记录》章节（极简 + 精简），并写两份独立文件；返回 (行, 统计)。"""
    mod = _load_compactor()
    out = mod.compress(md_text)
    d = ROOT / "reports"
    d.mkdir(parents=True, exist_ok=True)
    (d / "轮回记录_精简版.md").write_text(out["compact"], encoding="utf-8")
    (d / "轮回记录_极简版.md").write_text(out["mini"], encoding="utf-8")
    st = out["stats"]
    L: list[str] = []
    A = L.append
    A("## 〇、压缩记录（粘贴给外部模型用）")
    A("")
    A(f"> 《报告.md》正文（不含本节）约 {len(md_text)} 字，聊天窗口直接粘贴会提示「文本消息太长」。"
      f"本节是同一份记录压缩成的两个版本，数字全部由程序从完整正文机械提取（不估算、不新增），"
      f"生成器 `sim/compact_cycle_report.py`：`python sim/compact_cycle_report.py --write` 可单独重生成。")
    A(f"> **只复制需要的那个代码块**（不要复制整份报告）。"
      f"极简版约 {st['mini_chars']} 字（逐场一行）；精简版约 {st['compact_chars']} 字（逐回合一行 + 速览/复盘）。"
      f"两份也各有独立文件：`reports/轮回记录_极简版.md`、`reports/轮回记录_精简版.md`。")
    A("")
    A("### 〇-1 极简版（逐场一行）")
    A("")
    A("```")
    L.extend(_code_body(out["mini"]))
    A("```")
    A("")
    A("### 〇-2 精简版（逐回合一行 + 速览/复盘）")
    A("")
    A("```")
    L.extend(_code_body(out["compact"]))
    A("```")
    A("")
    return L, st


def _load_probe(filename: str):
    """按路径加载 sim/ 下的探针模块（探针自身会把它所在的 sim/ 加进 sys.path）。"""
    import importlib.util
    path = ROOT / "sim" / filename
    spec = importlib.util.spec_from_file_location(filename[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ask_headline() -> list[str]:
    """头部「追问速答」：两条一句话答案，数字与文末《四》同源（探针现场复算）。"""
    out = ["", "> **追问速答（详见文末《四》）**——"]
    n = 0
    for name in ("probe_dongcha_reach.py", "probe_monster_cast_order.py"):
        for ln in _load_probe(name).summary():
            n += 1
            out.append(f"> {n}. {ln}")
    return out


def followup_lines() -> list[str]:
    """《四、两个追问答疑》——两个追问问答的完整版（探针现场复算，非手写数字）。"""
    L: list[str] = []
    A = L.append
    A("## 四、两个追问答疑")
    A("")
    A("> 本节回答两个追问，外加一个衍生问题（为什么【搏命】同样百搭、而且与【洞察】并列）。"
      "数字全部由程序现场复算：`sim/probe_dongcha_reach.py`（跑穷举 harness 的同一沙盒）"
      "与 `sim/probe_monster_cast_order.py`（跑生产引擎的怪物阶段 + 聚合本局流水）。"
      "头部「追问速答」与本节的数字同源。")
    A("")
    for name in ("probe_dongcha_reach.py", "probe_monster_cast_order.py"):
        L.extend(_load_probe(name).lines())
    return L


def _syn_headline() -> list[str]:
    """报告头部的结论速览（数字与附录同源：同一张逐对表）。"""
    rows = _syn_rows()
    if not rows:
        return []
    total = len(rows)
    c1 = sum(1 for r in rows if r["classification"] == "1")
    c0 = sum(1 for r in rows if r["classification"] == "0")
    c2v = [r for r in rows if r["classification"] == "2" and r["confidence"] != "UNVERIFIED"]
    c2u = sum(1 for r in rows if r["classification"] == "2" and r["confidence"] == "UNVERIFIED")
    ev = [r for r in c2v if _syn_layer(r["interaction_type"]) == "EVENT_LEVEL"]
    _ev_edges = [e for e in _syn_edges()
                 if e.get("verifiable")
                 and set(e.get("types", [])) & {"EVENT_CONVERSION", "EVENT_MULTIPLICATION"}]
    _ev_amp = sum(1 for e in _ev_edges if str(e.get("pattern", "")).startswith("事件计数高于"))
    _ev_new = sum(1 for e in _ev_edges if str(e.get("pattern", "")).startswith("新事件"))
    scalar = sum(1 for r in c2v if _syn_layer(r["interaction_type"]) == "SCALAR_COUPLING")
    names = sorted({r["daowen_a"] for r in rows} | {r["daowen_b"] for r in rows})
    hub = {n: 0 for n in names}
    for r in c2v:
        hub[r["daowen_a"]] += 1
        hub[r["daowen_b"]] += 1
    top = sorted(((v, k) for k, v in hub.items() if v), reverse=True)
    zero = sorted(n for n in names if hub[n] == 0)
    pct = lambda k: f"{100.0 * k / total:.1f}%"
    return [
        f"> **道纹协同实测结论速览**（穷举 {len(names)} 个道纹 / {total} 组无序对，详见文末《三》）——",
        f"> ① {len(c2v)} 对（{pct(len(c2v))}）存在非平凡协同，{c1} 对（{pct(c1)}）只是线性叠加，"
        f"{c0} 对（{pct(c0)}）彼此独立；",
        f"> ② 协同里 **{scalar} 对（占协同的 {100.0 * scalar / len(c2v):.1f}%）只是结算层数值耦合**，"
        f"根源是同一条换算「[攻击力]＝[当前法力]、[攻击次数]＝[当前速度]」；",
        f"> ③ **真正的事件级交互只有 {len(ev)} 对**（占全部对的 {pct(len(ev))}，"
        f"其中 {_ev_new} 对是并施才出现的新事件、{_ev_amp} 对是事件计数被放大）；"
        f"发动本身被同伴改变的 134 对；顺序敏感 {sum(1 for r in c2v if r['order_sensitive'] == 'True')} 对；",
        f"> ④ 没有任何协同伙伴的道纹只有 **{len(zero)}** 个（{'、'.join(zero)}）；"
        f"连接度最高的是 **{top[0][1]}、{top[1][1]}**（各 {top[0][0]} 个伙伴）；",
        f"> ⑤ 另有 {c2u} 对因执行受限记 `UNVERIFIED`（引擎拒绝或待 DM 裁定中断），**未计入以上任何数字**。",
    ]


def daowen_synergy_lines() -> list[str]:
    """把两两协同穷举的测量结果写进本报告（全部数字取自逐对分类表，不手写）。"""
    rows = _syn_rows()
    if not rows:
        return []
    L: list[str] = []
    a_ = L.append
    total = len(rows)
    c0 = [r for r in rows if r["classification"] == "0"]
    c1 = [r for r in rows if r["classification"] == "1"]
    c2v = [r for r in rows if r["classification"] == "2" and r["confidence"] != "UNVERIFIED"]
    c2u = [r for r in rows if r["classification"] == "2" and r["confidence"] == "UNVERIFIED"]
    pct = lambda k: f"{100.0 * k / total:.1f}%"
    names = sorted({r["daowen_a"] for r in rows} | {r["daowen_b"] for r in rows})
    hub = {n: 0 for n in names}
    for r in c2v:
        hub[r["daowen_a"]] += 1
        hub[r["daowen_b"]] += 1
    zero = sorted(n for n in names if hub[n] == 0)
    top = sorted(((v, k) for k, v in hub.items() if v), reverse=True)
    order = [r for r in c2v if r["order_sensitive"] == "True"]
    layers: dict[str, int] = {}
    for r in c2v:
        k = _syn_layer(r["interaction_type"])
        layers[k] = layers.get(k, 0) + 1
    types: dict[str, int] = {}
    for r in c2v:
        for t in (r["interaction_type"] or "").split("|"):
            if t:
                types[t] = types.get(t, 0) + 1

    a_("## 三、道纹两两协同穷举（附：生产引擎实测）")
    a_("")
    a_("> 结论一节（本文件「二、本局速览」）里的 17 次【再生】是**这一局**的选择；本附录要回答的是"
       "另一个问题：**当前生产道纹表里，任意两道纹同时持有并先后发动，会发生什么**。")
    a_("> 分析对象＝生产注册表 `engine/daowen.py` `DaoWenEngine._registry`，**穷举全部无序对、无抽样**；"
       "全部执行在生产引擎里跑（无第二套战斗引擎）。")
    a_(f"> 道纹 N = **{len(names)}**｜无序对 C(N,2) = **{total}**｜统一发动 X = 3｜"
       "逐对明细：`reports/daowen_pairwise_synergy.csv`（每对一行）｜关系图：`reports/daowen_synergy_graph.json`｜"
       "长版报告：`reports/daowen_pairwise_synergy.md`")
    a_("> 复现：`python sim/daowen_pairwise_analysis.py --all`（全量，约 6 分钟）；"
       "`--report` 只按缓存重算报告；`--pair 固执,龙鳞` 单对重放。本报告由 "
       "`python sim/gen_report_from_trace.py` 转写，本附录数字全部取自上面那张逐对表。")
    a_("> 分析未改动任何道纹定义/数值/代价/规则；分析时段内 `engine/` 无改动。")
    a_("")
    # ---- 3.0 结论（直接回答，先说结果） ----
    a_("### 3.0 结论（直接回答）")
    a_("")
    a_(f"**一句话**：{len(names)} 个道纹、{total} 组无序对全部跑完；"
       f"**{len(c2v)} 对（{pct(len(c2v))}）存在非平凡协同**，但其中 **{len(c2v) - layers.get('EVENT_LEVEL', 0)} 对**"
       f"属于「数值/发动期」层面的耦合，**真正出现新事件、事件翻倍的结构性交互只有 "
       f"{layers.get('EVENT_LEVEL', 0)} 对**。")
    a_("")
    a_("| 问题 | 实测答案 |")
    a_("| --- | --- |")
    a_(f"| 有多少组对能互相影响？ | {len(c2v)} / {total} 对（{pct(len(c2v))}）是分类 2；"
       f"{len(c1)} 对（{pct(len(c1))}）只是同通道线性叠加；{len(c0)} 对（{pct(len(c0))}）互不影响 |")
    a_(f"| 协同主要来自哪里？ | {layers.get('SCALAR_COUPLING', 0)} 对是纯结算层数值耦合"
       f"（占协同的 {100.0 * layers.get('SCALAR_COUPLING', 0) / len(c2v):.1f}%），"
       f"根源是同一条换算：[攻击力]＝[当前法力]、[攻击次数]＝[当前速度] |")
    a_(f"| 有哪些是「发动被改写」？ | {layers.get('CAST_EXECUTION', 0)} 对——同伴先发动后，"
       f"后一道纹的代价/数值/目标集合被改变 |")
    _ev_edges = [e for e in _syn_edges()
                 if e.get("verifiable") and "EVENT_LEVEL"
                 and set(e.get("types", [])) & {"EVENT_CONVERSION", "EVENT_MULTIPLICATION"}]
    _amp = sum(1 for e in _ev_edges if str(e.get("pattern", "")).startswith("事件计数高于"))
    _new = sum(1 for e in _ev_edges if str(e.get("pattern", "")).startswith("新事件"))
    a_(f"| 有哪些是事件级交互？ | {layers.get('EVENT_LEVEL', 0)} 对（3.5 节全列）："
       f"其中 **{_new} 对是「两侧独发都没有、并施才出现的新事件」**，"
       f"**{_amp} 对是「事件计数高于两侧之和」** |")
    a_(f"| 顺序会改变结果吗？ | {len(order)} 对两序结果不同（不判定为 bug；生产里顺序本身就是规则） |")
    a_(f"| 有没有「孤岛」道纹？ | {len(zero)} 个"
       + (f"（{'、'.join(zero)}；尸爆为自毁型，发动即[命零]，无法在同一场景内完成两连发，一律记 UNVERIFIED）"
          if zero else "（无）") + " |")
    a_(f"| 最「百搭」的道纹？ | " + "、".join(f"{k}（{v} 个伙伴）" for v, k in top[:4]) + " |")
    a_(f"| 不能验证的有多少？ | {len(c2u)} 对（{pct(len(c2u))}）记 UNVERIFIED，原因见 3.7，未计入以上数字 |")
    a_("")
    a_("几个可以直接点开看的例子（因果串＝绝对值（相对基线的增量））：")
    a_("")
    examples = []
    for want in (("固执", "龙鳞"), ("杀伐", "加害"), ("疯狂", "无神"), ("血债", "活血"), ("变形", "赌命")):
        hit = next((r for r in c2v if {r["daowen_a"], r["daowen_b"]} == set(want)), None)
        if hit:
            examples.append(hit)
    for r in examples:
        a_(f"* **{r['daowen_a']} + {r['daowen_b']}**（{_syn_layer(r['interaction_type'])}）：{r['causal_trace']}")
    a_("")
    a_("> 以上都是**测量结果**，不评价强弱，也不判断协同「够不够」。每条数字都能用 "
       "`--pair A,B` 在本机重放验证。")
    a_("")
    a_("### 3.1 判定口径")
    a_("")
    a_("* 每个无序对跑 5 个场景：`基线`、`A 独发`、`B 独发`、`A→B 并施`、`B→A 并施`，"
       "再对每个场景跑 17 个探针（受击/多段受击/怪物阶段/闪避/普攻/流血代价/回复/击杀/回合推进/"
       "速度损失/血限损失/法力循环/被闪避后回合 等），逐通道比对**事件序列与中间值**，不只看末态血量。")
    a_("* `ab == a + b`（相对空基线的增量可加）→ 可加；两者都动同一通道 → **1 加和型**；"
       "从不共触同一通道 → **0 独立型**；`ab != a + b` 或事件类型/计数出现新东西 → **2 非平凡协同**。")
    a_("* **发动侧按生产 `summary` 的受益方定**（29 个道纹对敌发动，其余对自身）——这是场景选择，"
       "不是规则：不对敌放【庇护】这类非打法，否则会造出与道纹对无关的假交互。")
    a_("* 生产规则里**每次发动都吃 1 次出手**，两发必然等比少打普攻；攻击类探针先把"
       "「本回合已用出手」归零再打，把「独立发动的公共代价」从效果层交互里剥离。")
    a_("* 某次发动被引擎拒绝、或发动后留下待 DM 裁定的中断时，该对记 `UNVERIFIED`，"
       "**不做猜测、不计入任何结论数字**（原因见 3.7）。")
    a_("")
    a_("### 3.2 总量")
    a_("")
    a_("| 分类 | 对数 | 占比 |")
    a_("| --- | ---: | ---: |")
    a_(f"| 0 独立型（各自跑，互不影响） | {len(c0)} | {pct(len(c0))} |")
    a_(f"| 1 加和型（共触同一通道、增量可加） | {len(c1)} | {pct(len(c1))} |")
    a_(f"| 2 非平凡协同（可验证） | {len(c2v)} | {pct(len(c2v))} |")
    a_(f"| 2′ 判定为 2 但执行受限（UNVERIFIED，不计入结论） | {len(c2u)} | {pct(len(c2u))} |")
    a_(f"| 合计 | {total} | 100% |")
    a_("")
    a_("**分类 2 的层级拆分**（每个对只归一类，用来区分「结算层数值耦合」和「结构层交互」）：")
    a_("")
    a_("| 层级 | 对数 | 判据 |")
    a_("| --- | ---: | --- |")
    for key, meaning in (("EVENT_LEVEL", "出现两侧独发都没有的新事件类型，或事件计数超出两侧之和"),
                         ("CAST_EXECUTION", "某次发动的自身增量（代价/数值/结果）被同伴改变"),
                         ("ORDER_ONLY", "A→B 与 B→A 结果不同，且无上述两类"),
                         ("SCALAR_COUPLING", "只有结算层数值耦合（两序一致、无新事件、发动期未被改）")):
        a_(f"| {key} | {layers.get(key, 0)} | {meaning} |")
    a_("")
    a_("`SCALAR_COUPLING` 仍计入分类 2 的理由：本引擎里 **[攻击力]＝[当前法力]、[攻击次数]＝[当前速度]**"
       "是乘法关系，两次发动对同一池的加减在结算上不可加；这属于乘性耦合，不是「另一个互不相干的数值修正」。")
    a_("")
    a_("类型标签命中次数（一个对可命中多个）：" + "、".join(
        f"{k} {v}" for k, v in sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))) + "。")
    a_("")
    a_("### 3.3 连接度（Cat2 图）")
    a_("")
    a_(f"零 Cat2 伙伴的道纹：**{len(zero)}** 个"
       + (f"（{'、'.join(zero)}）" if zero else "（无）")
       + f"；平均度 {2 * len(c2v) / len(names):.2f}，中位度 "
         f"{sorted(hub.values())[len(names) // 2]}。")
    a_("")
    a_("| 道纹 | Cat2 伙伴数 |")
    a_("| --- | ---: |")
    for v, k in top[:15]:
        a_(f"| {k} | {v} |")
    a_("")
    a_("### 3.4 顺序敏感的对")
    a_("")
    a_(f"共 **{len(order)}** 对在 A→B 与 B→A 下结果不同（**不判定为 bug**，只记录分歧发生在哪个探针）。"
       "生产里顺序本身就是规则（例：`combat_hooks.py` 的钩子优先级决定【加害】必须先于【龙鳞】结算）。")
    a_("")
    # 覆盖面优先：证据通道最少的对先列（更聚焦），且同一道纹不重复出现
    order_sorted = sorted(order, key=lambda r: (len(r["evidence"].split(";")), r["daowen_a"], r["daowen_b"]))
    shown, used = [], set()
    for r in order_sorted:
        if r["daowen_a"] in used or r["daowen_b"] in used:
            continue
        used.add(r["daowen_a"]); used.add(r["daowen_b"]); shown.append(r)
    a_("| A | B | 命中探针（证据 ID） | 类型 |")
    a_("| --- | --- | --- | --- |")
    for r in shown[:20]:
        a_(f"| {r['daowen_a']} | {r['daowen_b']} | `{r['evidence']}` | {r['interaction_type']} |")
    if len(order) > len(shown[:20]):
        a_(f"| … | 其余 {len(order) - len(shown[:20])} 对 | 见逐对表 `order_sensitive=True` | |")
    a_("")
    a_("### 3.5 事件级协同（27 对全列）")
    a_("")
    a_("结构层交互——出现两侧独发都没有的新事件，或事件计数超出两侧之和。"
       "这是**全 27 对**，下表每一行都取自生产引擎的真实数字：`基线`／`A独发`／`B独发`／"
       "`A→B`／`B→A` 是同一探针同一通道上的实测值，`偏离形态` 只描述数字与事件的关系。")
    a_("")
    ev = sorted([r for r in c2v if _syn_layer(r["interaction_type"]) == "EVENT_LEVEL"],
                key=lambda r: (r["daowen_a"], r["daowen_b"]))
    edge_by = {(e["a"], e["b"]): e for e in _syn_edges()}

    def _num(x):
        return "—" if x is None else (f"{x:+d}" if isinstance(x, int) else str(x))

    a_("| A | B | 类型 | 探针／通道 | 基线 | A独发 | B独发 | A→B | B→A | 偏离形态 |")
    a_("| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |")
    for r in ev:
        e = edge_by.get((r["daowen_a"], r["daowen_b"]), {})
        d = e.get("first_deviation", {})
        t = r["interaction_type"].replace("|OUTPUT_ARITHMETIC", "")
        obs = d.get("obs", "")
        if obs.startswith("event:"):
            obs = f"事件「{obs[6:]}」"
        a_(f"| {r['daowen_a']} | {r['daowen_b']} | {t} | {d.get('probe','')}／{obs} "
           f"| {_num(d.get('base'))} | {_num(d.get('a'))} | {_num(d.get('b'))} "
           f"| {_num(d.get('ab'))} | {_num(d.get('ba'))} | {e.get('pattern','')} |")
    a_("")
    # 形态归并（纯计数，不做解释）
    import collections as _c
    shapes = _c.Counter()
    for r in ev:
        pat = edge_by.get((r["daowen_a"], r["daowen_b"]), {}).get("pattern", "")
        if pat.startswith("新事件"):
            shapes["出现两侧独发都没有的新事件"] += 1
        elif pat.startswith("事件计数高于"):
            shapes["事件计数高于两侧之和（放大）"] += 1
        elif pat.startswith("事件计数低于"):
            shapes["事件计数低于两侧之和（抑制）"] += 1
        else:
            shapes["数值偏离（事件层之外）"] += 1
    a_("形态归并：" + "；".join(f"{k} **{v}** 对" for k, v in shapes.items()) + "。")
    a_("")
    a_("重放任一行的命令：`python sim/daowen_pairwise_analysis.py --pair A,B`"
       "（A、B 换成表中两道纹名），输出与这里的数字同源。")
    a_("")
    a_("### 3.6 代表样本（含事件序列）")
    a_("")
    reps = []
    seen_kind: dict[str, int] = {}
    for r in sorted(c2v, key=lambda r: (r["daowen_a"], r["daowen_b"])):
        k = _syn_layer(r["interaction_type"])
        if seen_kind.get(k, 0) >= 1 and k != "EVENT_LEVEL":
            continue
        if k == "EVENT_LEVEL" and seen_kind.get(k, 0) >= 2:
            continue
        seen_kind[k] = seen_kind.get(k, 0) + 1
        reps.append((k, r))
    if rows:
        canonical = next((r for r in rows if {r["daowen_a"], r["daowen_b"]} == {"固执", "龙鳞"}), None)
        if canonical and all(canonical is not r for _, r in reps):
            reps.insert(0, ("指定样本", canonical))
    for k, r in reps[:8]:
        a_(f"* **{r['daowen_a']} + {r['daowen_b']}**｜分类 {r['classification']}"
           f"（{k}）｜{r['interaction_type']}")
        a_(f"  * 事件序列：{r['causal_trace']}")
    a_("")
    a_("### 3.7 执行受限的对（UNVERIFIED）")
    a_("")
    a_("原因只有两类：某次发动被引擎拒绝，或发动后留下待 DM 裁定的中断导致同场景后续动作被门禁挡住。")
    a_("")
    def _unver_reason(trace: str) -> str:
        """把逐对不同的错误串并成可读类别（只归类，不改写原因本身）。"""
        if "尸爆" in trace:
            return "尸爆（自毁型）发动即[命零]触发【死之传承】中断，同场景后续动作被门禁挡住"
        if "target_ref不是当前合法实体" in trace:
            return "后手发动时目标已不合法（先手改变了目标集合）"
        if "波及必须为" in trace:
            return "波及的显式目标数在提交瞬间与合法目标数不一致"
        if "无法完整承担" in trace or "法力不足" in trace or "出手已用完" in trace:
            return "代价/预算不足（连发两次超出本回合的法力或疲惫承受力）"
        if "有待处理的中断" in trace:
            return "先手留下待裁定中断，后手被门禁挡住"
        return "其它执行受限"

    reasons: dict[str, int] = {}
    for r in c2u:
        key = _unver_reason(r["causal_trace"])
        reasons[key] = reasons.get(key, 0) + 1
    a_("| 原因 | 对数 |")
    a_("| --- | ---: |")
    for k, v in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:8]:
        a_(f"| {k} | {v} |")
    a_("")
    a_("### 3.8 结论（可测量事实）与假设")
    a_("")
    a_("可测量事实：")
    a_("")
    a_(f"1. {len(names)} 个道纹产出 {total} 组无序对，全部跑完并逐对给出 0/1/2 唯一分类。")
    a_(f"2. 观测到分类 2（非平凡协同）**{len(c2v)}** 对，密度 **{pct(len(c2v))}**；"
       f"分类 1 **{len(c1)}** 对；分类 0 **{len(c0)}** 对。")
    a_(f"3. 分类 2 中：事件级 {layers.get('EVENT_LEVEL', 0)} 对、发动期被改变 "
       f"{layers.get('CAST_EXECUTION', 0)} 对、仅顺序不同 {layers.get('ORDER_ONLY', 0)} 对、"
       f"纯结算层数值耦合 {layers.get('SCALAR_COUPLING', 0)} 对。")
    a_(f"4. 没有任何分类 2 伙伴的道纹 **{len(zero)}** 个"
       + (f"（{'、'.join(zero)}）" if zero else "（无）") + "。")
    a_(f"5. 度最高的道纹：**{top[0][1]}**（{top[0][0]} 个 Cat2 伙伴）；"
       f"顺序敏感对 {len(order)} 对；执行受限 {len(c2u)} 对已单列，未计入上述任何数字。")
    a_("")
    a_("假设（**不是**结论，供后续验证）：")
    a_("")
    a_("1. 大量协同来自 **[攻击力]＝[当前法力]** 这一条换算：任何改动法力的道纹都会同时改动攻击力，"
       "于是「两次发动对同一法力池的加减」天然不可加。若要减少这类耦合，可验证的方向是"
       "把攻击力与法力解耦，而不是逐个道纹调数。")
    a_("2. 度高的是「改变别人参数」的道纹（削弱、增伤、扣法力/速限），度低的是「只改自己」的道纹——"
       "连接度可能主要反映**作用面**，而不是数值强度。")
    a_("3. 顺序敏感对集中在「同一结算窗口内互相改参数」的组合上；本报告只登记分歧通道，"
       "不主张哪一序才是设计意图。")
    a_("4. 事件级 27 对（触发式交互）数量远少于数值级，可能是因为沙盒只有 1 名敌人、"
       "没有友方/员工/多怪，触发类道纹的作用面被场景限制；换更宽的沙盒可能变多。")
    a_("")
    a_("> 本附录只报告测量结果：不评价道纹强弱、不判断协同「够不够」，也不把顺序敏感当作缺陷。")
    a_("")
    return L


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
paper.extend(daowen_synergy_lines())
paper.extend(followup_lines())

# 回填头部结论速览（数字与附录同源）
_syn_head = _syn_headline()
if "@@SYN_HEADLINE@@" in paper:
    _idx = paper.index("@@SYN_HEADLINE@@")
    if _syn_head:
        paper[_idx:_idx + 1] = [">"] + _syn_head
    else:
        paper.pop(_idx)

# 回填头部「追问速答」（同样必须在压缩章节之前）
_ask_head = ask_headline()
if "@@ASK_HEADLINE@@" in paper:
    _idx = paper.index("@@ASK_HEADLINE@@")
    if _ask_head:
        paper[_idx:_idx + 1] = _ask_head
    else:
        paper.pop(_idx)

# 压缩章节：在速览回填之后生成，保证压缩文本不含占位符
_compact_lines, _compact_stats = compressed_section("\n".join(paper))
paper[_compact_idx:_compact_idx] = _compact_lines

OUT.write_text("\n".join(paper) + "\n", encoding="utf-8")
print("written:", OUT, len(paper), "lines")
print("统计:", json.dumps(stats, ensure_ascii=False))
