#!/usr/bin/env python3
"""追问②：怪物为什么不先普攻、再发动道纹？先花掉法力不就等于先削弱自己？

用法（仓库根目录）：python sim/probe_monster_cast_order.py

结论分三层，全部现场复算（生产引擎 `GameEngine` ＋ 本局真实流水 `trace.jsonl`）：

1. 契约层——引擎**不给**「先攻击后发动」这个选项：
   · 怪物阶段是一次原子提交（token + choices），执行体内部固定「先结算道纹，再逐击循环普攻」
     （`engine/combat_parts/monster_phase.py` 的 `_execute_monster_phase`）；
   · prepare 列出合法道纹时，resolve **必须**从中提交一个，无选项才允许 null
     （《AI_EXPERIENCE.md·怪物准则》末段；本脚本现场提交 daowen=None 复现拒绝原文）。

2. 数值层——[攻击力]＝[当前法力]，且**在每一击结算时**才读取（`engine/combat.py:702`），
   所以「先付法力」必然先削弱自己的普攻。同一面板（血限 252／法力 7／速度 2）：
   · 发动【坏死X=3】（耗 6/7）→ 逐击伤害 1、1（合计 2）；
   · 发动【畸变X=3】（冷却代价，不耗法力）→ 逐击伤害 7、7（合计 14）。
   两者相差的 12 点，全部来自道纹把法力池吃掉后 [攻力] 跟着掉。

3. 本局流水——这个代价被系统性地放大：
   77 个「发动了道纹且当回合有普攻」的怪物回合，实测普攻合计 129 点；
   若这些回合按满法力结算（击数 × 法限）合计 1043 点，差 914 点。
   其中 35 个回合普攻 0 伤（千手蜈蚣 20、肠水母 10、血肉巨囊 5），
   直接喂出 7 次【凡庸】自爆（规则：连续五回合未能使敌对角色生命减少 → 原地炸裂[命零]）。
   反面样本：千手蜈蚣第 1 回合发【畸变X=7】（冷却，不耗法力）→ 4 击 × 4 伤 = 16，满法力结算。
   [回始]法力回满（怪物出厂自带遗物【某人的偏爱】）→ 每回合重复同一模式，代价每回合都要再付一次。
"""
from __future__ import annotations
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import daowen_pairwise_analysis as harness  # noqa: E402  同目录 harness（同一沙盒构造）

TRACE = ROOT / "data" / "handplay_20260916" / "trace.jsonl"
PANEL = {"blood_limit": 252, "mana_limit": 7, "speed": 2}      # 孢子母体型面板（法力 7＝坏死X=3 耗 6 + 普攻每击 1）


# ---------------------------------------------------------------- 引擎现场实验
def _sandbox(tag: str, daowen: str):
    from engine.models import DaoWen, DaoWenInstance
    tmp = Path(tempfile.mkdtemp())
    e = harness.build_sandbox(tmp / tag)
    en = e.state.enemies[0]
    en.blood_limit = en.current_hp = PANEL["blood_limit"]
    en.mana_limit = en.current_mana = PANEL["mana_limit"]
    en.speed_limit = en.current_speed = en.attack_count = PANEL["speed"]
    en.dao_wen = {daowen: DaoWenInstance(
        DaoWen(name=daowen, formula="", cost_type="", cost_formula="", effect_formula=""),
        x_value=0, x_free=True)}
    return e


def _submit(daowen: str, cast: dict | None) -> dict:
    """在全新沙盒里提交一次怪物阶段；返回可核对的读数。"""
    e = _sandbox(daowen, daowen)
    res = e.execute_action("prepare_monster_phase", {})["result"]
    opt = next((o for o in res["actors"][0]["daowen_options"] if o["name"] == daowen), None)
    hit = {"target_ref": "player:0", "dodge": False, "blood_shadow": False,
           "spell_choices": {"before": {}, "after": {}}}
    choice = {"actor_ref": "enemy:0", "daowen": cast,
              "attack_actions": [{"hits": [dict(hit) for _ in range(PANEL["speed"])]}]}
    hp0, mana0 = e.state.player.current_hp, e.state.enemies[0].current_mana
    out = e.execute_action("resolve_monster_phase", {"token": res["token"], "choices": [choice]})
    det = ((out.get("result") or {}).get("details")) or []
    return {"ok": bool(out.get("success")), "error": out.get("error", ""),
            "order": [("道纹：" + str(d["daowen_activated"])) if d.get("daowen_activated")
                      else f"普攻第{d.get('hit_index')}击" for d in det],
            "per_hit": [d["damage_dealt"] for d in det if "damage_dealt" in d],
            "player_hp": (hp0, e.state.player.current_hp),
            "monster_mana": (mana0, e.state.enemies[0].current_mana),
            "max_x": opt.get("max_x") if opt else None}


def player_order_facts() -> dict:
    """玩家侧对照：出手槽自由排序——先普攻、再发动道纹都合法，用完 2 次出手后第 3 个动作被拒。"""
    tmp = Path(tempfile.mkdtemp())
    e = harness.build_sandbox(tmp / "player", granted=("再生",))

    def attack():
        prep = e.execute_action("prepare_attack", {"actor_ref": "player:0"})
        if not prep.get("success"):
            return prep
        res = prep["result"]
        hits = [{"target_ref": "enemy:0", "dodge": False, "blood_shadow": False,
                 "spell_choices": {"before": {}, "after": {}}}
                for _ in range(int(res.get("hit_count", 1)))]
        return e.execute_action("resolve_attack", {"token": res["token"], "hits": hits})

    first = attack()
    cast = e.execute_action("use_daowen", {"actor_ref": "player:0", "daowen_name": "再生", "x": 1,
                                           "target_ref": "player:0", "dodge": False,
                                           "blood_shadow": False, "trigger_spell_choices": {}})
    third = attack()
    return {"attack_then_cast": (bool(first.get("success")), bool(cast.get("success"))),
            "third_action_rejected": third.get("error", "")}


def refill_facts() -> dict:
    """实测怪物[回始]法力回满：发动一次耗 6/7 的道纹，下一回合开始再看池子。"""
    e = _sandbox("refill", "坏死")
    before = e.state.enemies[0].current_mana
    res = e.execute_action("prepare_monster_phase", {})["result"]
    hit = {"target_ref": "player:0", "dodge": False, "blood_shadow": False,
           "spell_choices": {"before": {}, "after": {}}}
    e.execute_action("resolve_monster_phase", {"token": res["token"], "choices": [
        {"actor_ref": "enemy:0",
         "daowen": {"name": "坏死", "x": 3, "dodge": False, "blood_shadow": False,
                    "target_ref": "player:0"},
         "attack_actions": [{"hits": [dict(hit) for _ in range(PANEL["speed"])]}]}]})
    after_cast = e.state.enemies[0].current_mana
    harness._advance_round(e)                      # 回终 → 回始
    next_round = e.state.enemies[0].current_mana
    return {"start": before, "after_cast": after_cast, "next_round": next_round,
            "relics": [r.name for r in e.state.enemies[0].relics],
            "player_relics": [r.name for r in e.state.player.relics]}


def engine_facts() -> dict:
    return {
        "player_order": player_order_facts(),
        "refill": refill_facts(),
        "no_cast_rejected": _submit("坏死", None),
        "cast_costs_mana": _submit("坏死", {"name": "坏死", "x": 3, "dodge": False,
                                            "blood_shadow": False, "target_ref": "player:0"}),
        "cast_no_mana": _submit("畸变", {"name": "畸变", "x": 3, "dodge": False,
                                         "blood_shadow": False, "target_ref": "player:0"}),
    }


# ---------------------------------------------------------------- 本局流水聚合
def cost_of(name: str, x: int) -> int:
    from engine.daowen import DaoWenEngine
    DaoWenEngine.register_all()
    c = DaoWenEngine.resolve(name, x)
    return c.get("cost", 0) if c.get("cost_type") == "消耗" else 0


def _find_type(obj, kind: str, out: list | None = None) -> list:
    """在任意嵌套的引擎返回里找出指定 type 的事件（用于精确计数【凡庸】这类结算）。"""
    out = [] if out is None else out
    if isinstance(obj, list):
        for x in obj:
            _find_type(x, kind, out)
    elif isinstance(obj, dict):
        if obj.get("type") == kind:
            out.append(obj)
        for v in obj.values():
            _find_type(v, kind, out)
    return out


def trace_facts() -> dict:
    """逐个怪物回合：发动道纹的耗法力 → 当回合普攻逐击伤害（=发动后的剩余法力）。"""
    if not TRACE.exists():
        return {}
    recs = [json.loads(l) for l in TRACE.read_text(encoding="utf-8").splitlines() if l.strip()]
    starts = [r["seq"] for r in recs if r["action"] == "battle_start"]
    battle = lambda seq: sum(1 for s in starts if seq >= s)
    rounds = []
    for r in recs:
        if r["action"] != "resolve_monster_phase":
            continue
        det = ((r.get("result") or {}).get("result") or {}).get("details") or []
        casts = {d["monster"]: (d.get("daowen_activated"), d.get("x"))
                 for d in det if d.get("daowen_activated")}
        hits = defaultdict(list)
        for d in det:
            if "damage_dealt" in d:
                hits[d["attacker"]].append(d["damage_dealt"])
        for monster, (name, x) in casts.items():
            h = hits.get(monster, [])
            if not h:
                continue
            cost = cost_of(name, x)
            rounds.append({"battle": battle(r["seq"]), "round": r["state"]["round"],
                           "monster": monster, "daowen": name, "x": x, "cost": cost,
                           "hits": len(h), "per_hit": min(h), "total": sum(h),
                           "full_mana": len(h) * (cost + min(h))})
    agg: dict = defaultdict(lambda: {"rounds": 0, "actual": 0, "full": 0, "zero": 0, "panel": set()})
    for x in rounds:
        a = agg[x["monster"]]
        a["rounds"] += 1
        a["actual"] += x["total"]
        a["full"] += x["full_mana"]
        a["zero"] += 1 if x["total"] == 0 else 0
        a["panel"].add((x["battle"], x["daowen"], x["x"], x["cost"], x["hits"], x["per_hit"]))
    med = len(_find_type([r.get("result") for r in recs], "mediocrity"))
    return {"rounds": len(rounds), "per_monster": dict(agg),
            "actual": sum(x["total"] for x in rounds), "full": sum(x["full_mana"] for x in rounds),
            "zero_rounds": sum(1 for x in rounds if x["total"] == 0), "mediocrity": med}


def lines() -> list[str]:
    f, tf = engine_facts(), trace_facts()
    a_, b_, c_ = f["no_cast_rejected"], f["cast_costs_mana"], f["cast_no_mana"]
    if not (a_ and b_["ok"] and c_["ok"] and tf):
        return []
    L: list[str] = []
    A = L.append
    A("### 四-2 怪物为什么不先普攻、再发动道纹")
    A("")
    pf = f.get("player_order", {})
    A(f"**第一层：不是「不想」，是引擎不接受。** 先看玩家侧对照（同一沙盒现场提交）："
      f"玩家的出手槽是自由排序的——先普攻再发动道纹两步都被接受（{pf.get('attack_then_cast')}），"
      f"直到第 3 个动作才因出手用尽被拒（`{pf.get('third_action_rejected')}`）。"
      f"怪物侧完全没有这种自由：怪物阶段是一次原子提交（token + choices），"
      f"执行体内部固定**先结算道纹、再逐击循环普攻**；而且只要 prepare 列了合法道纹，resolve 就**必须**提交一个。"
      f"现场把同一只怪（面板 血限 {PANEL['blood_limit']}／法力 {PANEL['mana_limit']}／速度 {PANEL['speed']}）"
      f"的 daowen 提交成 `null`，被引擎当场拒绝：")
    A("")
    A(f"> `{a_['error']}`")
    A("")
    A(f"这与《AI_EXPERIENCE.md·怪物准则》末段一致：「prepare列出合法选项时，resolve必须从中提交一个（无选项才允许null）」。"
      f"换句话说，**「先普攻、后发动」这种打法在这个引擎里不存在提交形态**——怪物侧没有玩家的自由行动顺序"
      f"（玩家可以先 `prepare_attack/resolve_attack` 再 `use_daowen`，怪物不行）。")
    A("")
    A(f"**第二层：你的直觉是对的——先付法力就是先削弱自己。** [攻击力]＝[当前法力]，"
      f"而且这个值是在**每一击结算时**才读的（`engine/combat.py:702` `damage = attacker.effective_attack_power()`）。"
      f"同一面板换道纹，结果直接分叉：")
    A("")
    A(f"| 该回合发的道纹 | 代价 | 结算顺序 | 逐击伤害 | 该回合普攻合计 | 怪物法力 |")
    A(f"| --- | --- | --- | --- | --- | --- |")
    A(f"| 【坏死X=3】 | 消耗法力 6 | {' → '.join(b_['order'])} | {b_['per_hit']} | {sum(b_['per_hit'])} | "
      f"{b_['monster_mana'][0]} → {b_['monster_mana'][1]} |")
    A(f"| 【畸变X=3】 | 冷却（不耗法力） | {' → '.join(c_['order'])} | {c_['per_hit']} | {sum(c_['per_hit'])} | "
      f"{c_['monster_mana'][0]} → {c_['monster_mana'][1]} |")
    A("")
    A(f"同一个怪物、同一个面板、同一回合：发一个**花法力**的道纹，普攻就只剩 "
      f"{sum(b_['per_hit'])} 点；换成不花法力的道纹，是 {sum(c_['per_hit'])} 点。"
      f"差的 {sum(c_['per_hit']) - sum(b_['per_hit'])} 点不是随机，而是「道纹先扣法力池、[攻力] 跟着掉」的必然结果。"
      f"（面板与回合都是从本局真实怪物身上推出来的：孢子母体每回合【坏死X=3】耗 6 点，普攻恰好剩 1 点×2 击。）")
    A("")
    A(f"**第三层：本局把这个代价放大成了 7 次自爆。** 在 `data/handplay_20260916/trace.jsonl` 里，"
      f"「当回合发动了道纹（含固定 X）且打出了普攻」的怪物回合共 **{tf['rounds']}** 个：")
    A("")
    A(f"| 怪物 | 这类回合 | 实测普攻合计 | 按满法力结算（击数×法限） | 差 | 其中 0 伤回合 | 明细（道纹X／耗法力／击数×每击） |")
    A("| --- | --- | --- | --- | --- | --- | --- |")
    for monster, a in sorted(tf["per_monster"].items(), key=lambda kv: -kv[1]["full"]):
        det = "；".join(f"第{b}场 {nm}X={x} 耗{c} {h}击×{p}" for b, nm, x, c, h, p in sorted(a["panel"]))
        A(f"| {monster} | {a['rounds']} | {a['actual']} | {a['full']} | {a['full'] - a['actual']} | {a['zero']} | {det} |")
    A(f"| **合计** | **{tf['rounds']}** | **{tf['actual']}** | **{tf['full']}** | **{tf['full'] - tf['actual']}** | "
      f"**{tf['zero_rounds']}** | — |")
    A("")
    A(f"* 这 {tf['rounds']} 个回合实测普攻一共打掉林寂 {tf['actual']} 点（与《二》「承伤 129」逐位一致），"
      f"若这些回合按满法力结算（击数 × 法限；反事实推算，不是实测）本应是 {tf['full']} 点，"
      f"差 **{tf['full'] - tf['actual']}** 点。")
    A(f"* 其中 **{tf['zero_rounds']} 个回合普攻 0 伤**——正是这一族回合喂出了本局 **{tf['mediocrity']} 次【凡庸】自爆**"
      f"（规则：连续五回合未能使敌对角色生命减少 → 凭空全身炸裂[命零]）。"
      f"也就是说，怪物「先花法力再打人」的顺序，实际效果是让它们在林寂面前连续空转。")
    A("* 反面样本（同一条流水里）：千手蜈蚣第 1 回合发【畸变X=7】（冷却代价、不耗法力）→ 4 击 × 4 伤 = 16 点，"
      "满法力结算；从第 2 回合起改发【衰败X=1】（耗 4/4 法力）→ 4 击 × 0 伤。同一只怪，两种道纹两种结果。")
    A(f"* 为什么每回合都重复同一模式：怪物[回始]法力回满（出厂自带遗物【某人的偏爱】）——"
      f"这一池法力不花也只是闲置一回合，所以每个回合都是「先付钱、再打人」重来一次。")
    A(f"* 至于「为什么总是这一个道纹」：本局是手操驱动器在提交（`sim/handplay_cycle_20260916.py::_do_monster_phase`），"
      f"它按 **prepare 列出的第一个合法选项** `daowen_options[0]` 取，不做「道纹收益 vs 普攻损失」的比较；"
      f"生产侧则是《怪物准则》的固定优先级（自保→输出→控制→机制，入口 `sim/monster_targets.py::"
      f"pick_monster_daowen_option`）——两者都不会因为「发动会削弱本回合普攻」而改选或不选。")
    A("")
    rf = f.get("refill", {})
    A(f"**第四层（反事实：如果顺序反过来会怎样）**——按你的设想，怪物先普攻、再花法力发动道纹，"
      f"本轮伤害就该是 `击数 × 法限`（满法力），而代价呢？实测：怪物法力 {rf.get('start')} → "
      f"发动【坏死X=3】（耗 6）后 {rf.get('after_cast')} → **下一回合[回始]又回到 {rf.get('next_round')}**"
      f"（怪物出厂自带遗物【某人的偏爱】{rf.get('relics')}；轮回者没有这件遗物 {rf.get('player_relics')}，是一池制）。"
      f"也就是说，留在法力池里的余量**不花白不花**——下回合[回始]会被直接覆盖。")
    A("")
    A(f"于是「先普攻、后发动」的组合＝**满法力打满伤害 ＋ 道纹照常生效 ＋ 法力代价为零**。"
      f"按这个口径，本局那 {tf['rounds']} 个回合的普攻会从实测 {tf['actual']} 点变成 {tf['full']} 点，"
      f"而怪物一分钱没多付（差值 {tf['full'] - tf['actual']} 点全部白拿）。")
    A("")
    A(f"**这就是为什么引擎不让先攻后发**（推断设计意图，非文档原文）：规则正文只写「怪物每回合 1 次攻击 + 1 种道纹」"
      f"（`AI_EXPERIENCE.md`·怪物准则 8），**没有规定先后**；引擎把顺序定死在执行体里"
      f"（道纹 → 逐击普攻）。在当前顺序下，怪物花在道纹上的法力**正好从自己这一轮的普攻里扣**，"
      f"而[回始]回满这条怪物资源优势才不等于「免费开道纹」——它换来的是「每回合都能开得起道纹」，"
      f"要付的是当回合的基础伤害。顺序一旦反过来，这份代价就消失了。")
    A("")
    A("**所以**：怪物「不先攻击」不是策略选择的问题，而是①引擎契约强制发动、②执行顺序固定为道纹→普攻、"
      "③[攻力]＝[当前法力] 逐击读取——三条叠加，等于用当回合的输出换取道纹效果。"
      "这是规则层的既定交换，不是 bug；但它对本局的影响是可量化的：上面那张表的差值与 7 次【凡庸】。")
    A("")
    return L


def summary() -> list[str]:
    b_ = _submit("坏死", {"name": "坏死", "x": 3, "dodge": False, "blood_shadow": False,
                          "target_ref": "player:0"})
    c_ = _submit("畸变", {"name": "畸变", "x": 3, "dodge": False, "blood_shadow": False,
                          "target_ref": "player:0"})
    tf = trace_facts()
    if not (b_["ok"] and c_["ok"] and tf):
        return []
    return [f"**怪物为什么不先普攻再发道纹**：引擎不给这个顺序——怪物阶段固定「先结算道纹、再逐击普攻」，"
            f"且 prepare 有合法选项时 resolve 必须提交一个（提交 `null` 会被拒：『{_submit('坏死', None)['error']}』）。"
            f"而 [攻力]＝[当前法力] 是逐击读取的，所以先付法力就是先削弱自己：同面板发【坏死X=3】（耗 6/7）"
            f"普攻 {sum(b_['per_hit'])} 点，改发不耗法力的【畸变X=3】则是 {sum(c_['per_hit'])} 点。"
            f"本局 {tf['rounds']} 个这类回合实测普攻 {tf['actual']} 点（满法力反事实 {tf['full']} 点），"
            f"其中 {tf['zero_rounds']} 个 0 伤回合直接喂出 {tf['mediocrity']} 次【凡庸】自爆。"
            f"若真允许「先攻后发」，法力代价会被[回始]回满抵消为零（实测：发动后 1/7 → 下一回合 7/7，"
            f"靠出厂遗物【某人的偏爱】）——等于白拿道纹效果又保满伤害，这就是引擎把顺序定死的原因（详见《四》）。"]


def main() -> None:
    for ln in summary():
        print(ln)
    print()
    for ln in lines():
        print(ln)


if __name__ == "__main__":
    main()
