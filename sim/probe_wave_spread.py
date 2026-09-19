"""波及扩散矩阵探针（2026-09-19 用户令）。

要回答的两个问题：
  ① 怪物用【波及】标记「自己＋轮回者」之后，轮回者发动各条道纹会发生什么？
  ② 反过来，轮回者用【波及】标记「自己＋怪物」之后，怪物发动各条道纹会发生什么？

两条先验事实（本探针实测复核，不照抄注释）：
  - 波及**不能标记自己**：玩家侧 `api.py::_resolve_daowen_dodge` 校验「存活非自身角色」；
    怪物侧 prepare 的 `dodge_target_options` 直接排除 `actor_ref`。
  - 标记带 `source`＝挂标记者的名字，而 `_wave_targets(caster)` 只认「施法者自己挂出去的」标记
    → 波及是**单向**的：别人挂在你身上的标记，不会让你自己的道纹扩散。
  所以「自己也被标记」这个前提没法由自己完成，只能由对方挂上来（＝互标）。探针因此把
  用户问的两种情形与其合法等价形（互标）都测到，并各配一条**无标记基线**做对照。

场景（caster＝本次发动道纹的一方）：
  P0 无标记，轮回者发动            ← 基线
  P1 怪物→轮回者 单向标记，轮回者发动   ＝问题①
  P3 互标，轮回者发动                    ＝问题①的合法等价形（两边都带波及效果）
  M0 无标记，怪物发动                ← 基线
  M2 轮回者→怪物 单向标记，怪物发动     ＝问题②
  M4 互标，怪物发动                      ＝问题②的合法等价形
  S5 两个波及目标（轮回者＋两只怪）→ 演示「数值平分」为何在 1v1 里看不到

夹具要点（都是为了「观测到的变化只来自道纹」）：
  - 怪物命中数＝`effective_attack_count()`＝当前速度（2026-09-17）→ **怪物发动**的场景把
    怪物速度设 0，零命中，普攻不造成任何伤害；**轮回者发动**的场景怪物根本不进阶段，
    速度可以放开到 6，于是速度类道纹作用在怪物身上也看得见。
  - 速度类道纹在「怪物发动」场景里对 0 速怪物看不出数值，另有对照跑（怪速 6，含 6 次普攻，
    表格里会标出来）。
  - 双方都预留 100 点已损生命，治疗/回复类效果才看得出来。
  - 按道纹的**自然目标**发动：先不提交 target_ref（引擎对可选目标道纹兜底为施法者自身）；
    引擎回「需要显式指定目标」时，自用类道纹（MONSTER_SELF_DAOWEN）补自己的 ref、
    其余补敌方 ref——否则探针替道纹选目标，会把「探针选了谁」误当成「波及把效果送去哪」。
  - 每条道纹一场独立战斗（独立 db、固定 rng_seed），只装这一条道纹。
  - 标记用引擎自己的 `_toggle_wave_mark`（真实发动内部就是调它，见 api.py:2445），
    这样标记不消耗出手、不牵扯怪物阶段 token；真实发动路径由 S0 单独实测。

用法：
    python3 sim/probe_wave_spread.py --out /tmp/wave.md          # 全 70 条道纹
    python3 sim/probe_wave_spread.py --daowen 杀伐 再生 --scenario P1 P3
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine                                    # noqa: E402
from engine.daowen import DaoWenEngine                               # noqa: E402
from engine.models import DaoWen, DaoWenInstance, Entity             # noqa: E402
from sim.monster_targets import (MONSTER_SELF_DAOWEN, apply_wave_submission,  # noqa: E402
                                 pick_monster_daowen_target)
from tests.setup_support import finish_initial_daowen                # noqa: E402

X_PROBE = 2          # 统一 X=2：数值平分时能直接看出「总量不变、一分为二」
MONSTER = "实验怪"
MONSTER_B = "实验怪乙"
HP = 2000            # 双方血限；current_hp = HP-100（留已损生命给治疗类看）
SPEED_FAMILY = ("减速", "萎缩", "急速", "加速", "洞察", "眩晕", "坠落", "滑翔", "飞行", "全速")

# (代号, 发动方, 标记态, 怪物速度, 说明)
SCENARIOS = [
    ("P0", "轮回者", "none",   6, "无标记（基线）"),
    ("P1", "轮回者", "m2p",    6, "怪物→轮回者 单向标记"),
    ("P3", "轮回者", "mutual", 6, "互标"),
    ("M0", "怪物",   "none",   0, "无标记（基线）"),
    ("M2", "怪物",   "p2m",    0, "轮回者→怪物 单向标记"),
    ("M4", "怪物",   "mutual", 0, "互标"),
]


# ---------------------------------------------------------------- 夹具
def _engine(tag: str, seed: int = 20260919) -> GameEngine:
    os.makedirs("/tmp/linji_probe", exist_ok=True)
    db = f"/tmp/linji_probe/wave_{tag}.db"
    if os.path.exists(db):
        os.remove(db)
    e = GameEngine(db_path=db, rng_seed=seed)
    e.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.phase = "in_combat"
    p = e.state.player
    p.current_mana = 200
    p.mana_limit = 200
    p.blood_limit = HP
    p.current_hp = HP - 100
    return e


def _give(entity: Entity, name: str, x_free: bool = False) -> None:
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗",
               cost_formula="X", effect_formula=""), x_value=0, x_free=x_free)


def _monster(e: GameEngine, name: str = MONSTER, speed: int = 0) -> Entity:
    m = Entity(name=name, entity_type="怪物", blood_limit=HP, current_hp=HP - 100,
               attack_count=0, attack_power=10, speed_limit=speed, current_speed=speed)
    m.mana_limit = 200
    m.current_mana = 200
    e.state.enemies.append(m)
    return m


def _combat(e: GameEngine) -> None:
    e.state.combat_subphase = "player_actions"
    e.state.pending_monster_phase = {}
    e.state.current_round = 2
    e.execute_action("round_start", {"relic_choices": {}})


def _mark(e: GameEngine, caster: Entity, target: Entity) -> bool:
    return e.combat._toggle_wave_mark(target, caster)


def _marks(e: GameEngine, player: Entity, monster: Entity, mode: str) -> None:
    if mode in ("m2p", "mutual"):
        _mark(e, monster, player)      # 怪物挂的标记落在轮回者身上
    if mode in ("p2m", "mutual"):
        _mark(e, player, monster)      # 轮回者挂的标记落在怪物身上


# ---------------------------------------------------------------- 观测
def _snap(e: GameEngine) -> dict:
    out = {}
    for ent in [e.state.player] + list(e.state.enemies) + list(e.state.friends):
        out[ent.name] = {
            "hp": ent.current_hp, "mana": ent.current_mana,
            "speed": ent.current_speed, "bl": ent.blood_limit,
            "ml": ent.mana_limit, "mut": getattr(ent, "mutation_count", 0),
            "shield": getattr(ent, "shield", 0),
            "st": sorted(f"{s.name}{s.value}@{s.source}" for s in ent.status_effects),
            "alive": ent.is_alive,
        }
    return out


def _diff(before: dict, after: dict) -> list[str]:
    d = []
    for who, b in before.items():
        a = after.get(who)
        if a is None:
            d.append(f"{who}:消失")
            continue
        for k, label in (("hp", "生命"), ("mana", "法力"), ("speed", "速度"),
                         ("bl", "血限"), ("ml", "法限"), ("mut", "异变"),
                         ("shield", "格挡")):
            if a[k] != b[k]:
                d.append(f"{who}.{label}{b[k]}→{a[k]}")
        if a["alive"] != b["alive"]:
            d.append(f"{who}.存活={a['alive']}")
        added = [s for s in a["st"] if s not in b["st"]]
        gone = [s for s in b["st"] if s not in a["st"]]
        if added:
            d.append(f"{who}+状态[{','.join(added)}]")
        if gone:
            d.append(f"{who}-状态[{','.join(gone)}]")
    return d


NUM_KEYS = ("actual_damage", "actual_heal", "amount", "value", "damage", "heal",
            "speed_after", "blood_limit_after", "cost", "pieces")


def _execution(r: dict) -> dict:
    """两种接口的执行日志形状不同：use_daowen 在 r["execution"]，
    resolve_monster_phase 在 r["result"]["details"][0]["execution"]。"""
    r = r or {}
    ex = r.get("execution")
    if isinstance(ex, dict) and ex:
        return ex
    for d in ((r.get("result") or {}).get("details") or []):
        if isinstance(d, dict) and isinstance(d.get("execution"), dict):
            return d["execution"]
    return {}


def _effects(r: dict, limit: int = 4) -> str:
    """把 execution.effects 压成 `类型→对象:数字` 的紧凑串（谁受到了什么）。"""
    ex = _execution(r).get("effects") or []
    out = []
    for ef in ex[:limit]:
        if not isinstance(ef, dict):
            continue
        num = next((f"{k}={ef[k]}" for k in NUM_KEYS if k in ef and ef[k] not in (None, "")), "")
        st = ef.get("status") or ef.get("name") or ""
        tag = f"{ef.get('type', '?')}→{ef.get('target', '?')}"
        if st:
            tag += f"[{st}{ef.get('value', '')}]"
        out.append(tag + (f":{num}" if num else ""))
    more = f" …+{len(ex) - limit}" if len(ex) > limit else ""
    return "；".join(out) + more if out else "—"


def _spread(r: dict) -> str:
    ws = _execution(r).get("wave_spread")
    if not ws:
        return "—"
    pieces = ws.get("pieces") or {}
    pk = ";".join(f"{k}={v}" for k, v in list(pieces.items())[:2])
    return f"扩散→{','.join(ws.get('targets', []))}" + (f"（{pk}）" if pk else "")


# ---------------------------------------------------------------- 发动
def _player_cast(e: GameEngine, name: str) -> tuple[dict, str]:
    """走真实行动接口发动轮回者道纹；按道纹的自然目标（先不指定，缺目标再指定敌方）。"""
    action = None
    for a in e.get_available_actions().get("actions", []):
        s = a.get("params_schema", {})
        if a.get("action_type") == "use_daowen" and s.get("daowen_name") == name:
            action = a
            break
    if action is None:
        return {"success": False, "error": "不在 use_daowen 列表里（此刻不可发动）"}, "—"
    if action.get("available") is False:
        return {"success": False, "error": f"被标记不可发动：{action.get('reason', '')}"}, "—"
    s = action["params_schema"]
    xs = s.get("x", {})
    xmax = xs.get("maximum", X_PROBE) if isinstance(xs, dict) else int(xs or X_PROBE)
    p: dict = {"daowen_name": name, "x": max(1, min(X_PROBE, xmax)),
               "dodge": False, "blood_shadow": False, "trigger_spell_choices": {}}
    if "actor_ref" in s:
        p["actor_ref"] = s["actor_ref"]
    if isinstance(s.get("y"), dict):
        p["y"] = 1
    if name == "波及":
        refs = e.combat._combat_entity_refs()
        me = e.state.player
        cands = [r for r, ent in refs.items() if ent.is_alive and ent is not me]
        p["x"] = min(p["x"], len(cands)) if cands else 0
        p["dodge_targets"] = [{"target_ref": r, "dodge": False, "blood_shadow": False}
                              for r in cands[:p["x"]]]
        return e.execute_action("use_daowen", p), "X个目标"
    r = e.execute_action("use_daowen", p)
    if not r.get("success") and "需要显式指定目标" in str(r.get("error", "")):
        # 引擎要求显式目标时，按道纹的**自然目标**补：自用类打自己，其余打敌人。
        # 分类沿用怪物 AI 的权威表 MONSTER_SELF_DAOWEN（sim/monster_targets.py，
        # 2026-08-21 做过分类覆盖审计）；怪物侧 pick_monster_daowen_target 用的就是它。
        refs = e.combat._combat_entity_refs()
        me = e.state.player
        self_ref = next((k for k, v in refs.items() if v is me), "")
        foes = [t["ref"] for t in (s.get("target_ref") or [])
                if str(t.get("ref", "")).startswith("enemy:")]
        if name in MONSTER_SELF_DAOWEN and self_ref:
            p["target_ref"] = self_ref
            r = e.execute_action("use_daowen", p)
            return r, "显式自身（自用类）"
        p["target_ref"] = foes[0] if foes else "enemy:0"
        r = e.execute_action("use_daowen", p)
        return r, "显式敌方"
    return r, "未指定（引擎兜底自身）"


def _attack_block(actor: dict) -> list[dict]:
    if not actor["base_attack_actions"] or not actor["attack_target_options"]:
        return []
    tgt = actor["attack_target_options"][0]["ref"]
    to = next(t for t in actor["attack_target_options"] if t["ref"] == tgt)
    spells = {timing: {sp["spell_name"]: {"use": False}
                       for sp in to.get("spell_options", {}).get(timing, [])}
              for timing in ("before", "after", "damage_after", "life_before")}
    hits = [{"target_ref": tgt, "dodge": False, "blood_shadow": False,
             "spell_choices": spells} for _ in range(actor["base_hits_per_attack"])]
    return [{"hits": hits} for _ in range(actor["base_attack_actions"])]


def _monster_cast(e: GameEngine, name: str) -> tuple[dict, str]:
    """走真实两阶段接口发动怪物道纹（prepare → resolve）。"""
    prepared = e.execute_action("prepare_monster_phase", {})
    if not prepared.get("success"):
        return {"success": False, "error": f"prepare 失败：{prepared.get('error', '')}"}, "—"
    actor = next((a for a in prepared["result"]["actors"] if a["actor_ref"] == "enemy:0"), None)
    if actor is None:
        return {"success": False,
                "error": f"prepare 未给出 enemy:0（skipped={prepared['result'].get('skipped')}）"}, "—"
    option = next((o for o in actor["daowen_options"] if o["name"] == name), None)
    if option is None:
        have = [o["name"] for o in actor["daowen_options"]]
        return {"success": False, "error": f"prepare 未列出该道纹（列出的是 {have}）"}, "—"
    dao: dict = {"name": name, "dodge": False, "blood_shadow": False,
                 "trigger_spell_choices": {
                     h: {sp["spell_name"]: {"use": False} for sp in ss}
                     for h, ss in option.get("trigger_spell_options", {}).items()}}
    tgt = "自身/未指定"
    if option.get("requires_target"):
        dao["target_ref"] = pick_monster_daowen_target(e.combat, actor["actor_ref"], option)
        tgt = "显式目标"
    if option.get("dodge_submission") == "per_target":
        apply_wave_submission(dao, option)
        tgt = "X个目标"
    elif option.get("x_free"):
        dao["x"] = max(1, min(X_PROBE, int(option.get("max_x") or X_PROBE)))
    r = e.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"],
        "choices": [{"actor_ref": actor["actor_ref"], "daowen": dao,
                     "attack_actions": _attack_block(actor)}]})
    return r, tgt


# ---------------------------------------------------------------- 单条实验
def run_case(name: str, code: str, tag: str, monster_speed: int = 0) -> dict:
    side, mode = ("轮回者", None)
    for c, s, m, sp, _desc in SCENARIOS:
        if c == code:
            side, mode = s, m
            break
    e = _engine(f"{tag}_{name}_{code}")
    p = e.state.player
    m = _monster(e, speed=monster_speed)
    _give(p, name)
    _give(m, name, x_free=True)
    _combat(e)
    _marks(e, p, m, mode or "none")
    before = _snap(e)
    if side == "轮回者":
        r, tgt = _player_cast(e, name)
    else:
        r, tgt = _monster_cast(e, name)
    after = _snap(e)
    ok = bool(r.get("success"))
    skipped = bool(r.get("skipped")) or bool((r.get("result") or {}).get("daowen_skipped"))
    return {
        "daowen": name, "code": code, "caster": side, "target": tgt,
        "ok": ok, "skipped": skipped,
        "err": "" if ok else str(r.get("error", ""))[:100],
        "spread": _spread(r if ok else {}),
        "fx": _effects(r if ok else {}),
        "diff": _diff(before, after),
        "marks_after": sorted(f"{s.source}→{ent.name}" for ent in (p, m)
                              for s in ent.status_effects if s.name == "波及"),
    }


# ---------------------------------------------------------------- S0 / S5
def run_s0() -> list[str]:
    out = []
    # (a) 轮回者把「自己」列进波及目标
    e = _engine("s0a")
    p, m = e.state.player, _monster(e, speed=6)
    _give(p, "波及")
    _combat(e)
    self_ref = next(r for r, ent in e.combat._combat_entity_refs().items() if ent is p)
    r = e.execute_action("use_daowen", {
        "daowen_name": "波及", "x": 2, "dodge": False, "blood_shadow": False,
        "trigger_spell_choices": {},
        "dodge_targets": [{"target_ref": self_ref, "dodge": False, "blood_shadow": False},
                          {"target_ref": "enemy:0", "dodge": False, "blood_shadow": False}]})
    out.append(f"轮回者把自己列进波及目标：success={r.get('success')} "
               f"error={str(r.get('error', ''))[:44]}｜自己被标记={p.has_status('波及')} "
               f"怪物被标记={m.has_status('波及')}（整笔被拒，一个都没挂上）")

    # (b) 怪物侧 prepare 的候选里有没有自己
    e = _engine("s0b")
    p, m = e.state.player, _monster(e, speed=6)
    _give(m, "波及", x_free=True)
    _combat(e)
    prepared = e.execute_action("prepare_monster_phase", {})
    actor = next(a for a in prepared["result"]["actors"] if a["actor_ref"] == "enemy:0")
    opt = next(o for o in actor["daowen_options"] if o["name"] == "波及")
    cands = [t["ref"] for t in opt["dodge_target_options"]]
    out.append(f"怪物波及 prepare：max_x={opt.get('max_x')}（场上角色总数="
               f"{len(e.combat._combat_entity_refs())}）；候选={cands}；"
               f"自己在候选里吗={'enemy:0' in cands}")

    # (c) 真实互标可达性（证明矩阵里的标记态不是探针凭空造的）
    e = _engine("s0c")
    p, m = e.state.player, _monster(e, speed=6)
    _give(p, "波及")
    _give(m, "波及", x_free=True)
    _combat(e)
    r1 = e.execute_action("use_daowen", {
        "daowen_name": "波及", "x": 1, "dodge": False, "blood_shadow": False,
        "trigger_spell_choices": {},
        "dodge_targets": [{"target_ref": "enemy:0", "dodge": False, "blood_shadow": False}]})
    r2, _t = _monster_cast(e, "波及")
    out.append(f"真实互标：轮回者发动 success={r1.get('success')}"
               f"（标记={r1.get('dodge', {}).get('wave_marked')}）；"
               f"怪物发动 success={r2.get('success')} err={str(r2.get('error', ''))[:40]}")
    out.append(f"互标后：标记清单={sorted(f'{s.source}→{ent.name}' for ent in (p, m) for s in ent.status_effects if s.name == '波及')}"
               f"｜轮回者的 _wave_targets={[x.name for x in e.combat._wave_targets(p)]}"
               f"｜怪物的 _wave_targets={[x.name for x in e.combat._wave_targets(m)]}"
               f"（各自只认自己挂出去的标记）")

    # (d) 就算硬把标记挂在自己身上，扩散也不会认
    e = _engine("s0d")
    p, m = e.state.player, _monster(e, speed=6)
    _give(p, "杀伐")
    _combat(e)
    _mark(e, p, p)          # 直接用引擎原语给自己挂标记（真实发动做不到）
    out.append(f"硬给自己挂标记后：自己有波及状态={p.has_status('波及')}，"
               f"但 _wave_targets(自己)={[x.name for x in e.combat._wave_targets(p)]}"
               f"（`entity is caster` 直接跳过自己）")
    return out


def run_s5(names: list[str]) -> list[str]:
    out = ["| 道纹 | 目标提交 | 结果 | 扩散 | 效果 | 状态变化 |",
           "| --- | --- | --- | --- | --- | --- |"]
    for i, name in enumerate(names):
        e = _engine(f"s5_{i}")
        p = e.state.player
        ma = _monster(e, MONSTER, speed=6)
        mb = _monster(e, MONSTER_B, speed=6)
        _give(p, name)
        _combat(e)
        _mark(e, p, ma)
        _mark(e, p, mb)
        before = _snap(e)
        if name == "波及":
            r, tgt = {"success": True}, "X个目标"
        else:
            r, tgt = _player_cast(e, name)
        after = _snap(e)
        ok = bool(r.get("success"))
        out.append(
            f"| {name} | {tgt} | {'成功' if ok else '失败：' + str(r.get('error', ''))[:50]} "
            f"| {_spread(r if ok else {})} | {_effects(r if ok else {})[:180]} "
            f"| {'；'.join(_diff(before, after))[:180] or '（无变化）'} |")
    return out


# ---------------------------------------------------------------- 主程序
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daowen", nargs="*", default=None)
    ap.add_argument("--scenario", nargs="*", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    names = args.daowen or sorted(DaoWenEngine.list_all())
    codes = args.scenario or [c for c, *_ in SCENARIOS]
    lines: list[str] = []
    A = lines.append
    A("# 波及扩散矩阵（探针 sim/probe_wave_spread.py，2026-09-19）")
    A("")
    A(f"统一 X={X_PROBE}（受 schema 上限与可负担性收敛）；每条道纹一场独立战斗、只装这一条道纹；"
      f"双方血限 {HP}、当前生命 {HP - 100}（留已损生命给治疗类看）。")
    A("")
    A("## S0 前提实测：能不能标记自己")
    A("")
    for row in run_s0():
        A(f"- {row}")
    A("")
    A("## 场景")
    A("")
    A("| 代号 | 发动方 | 标记态 | 怪物速度 | 说明 |")
    A("| --- | --- | --- | --- | --- |")
    for c, s, m, sp, desc in SCENARIOS:
        A(f"| {c} | {s} | {m} | {sp} | {desc} |")
    A("")
    A("怪物速度 0 ＝零命中（命中数＝当前速度），怪物发动那几行不含普攻伤害；"
      "轮回者发动时怪物不进阶段，速度放开到 6，速度类道纹作用在怪物身上也看得见。")
    A("")
    A("## 矩阵")
    A("")
    A("| 道纹 | 态 | 发动方 | 目标提交 | 结果 | 扩散 | 效果（谁受到了什么） | 状态变化 | 发动后标记 |")
    A("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, name in enumerate(names):
        for code, side, _mode, speed, _desc in SCENARIOS:
            if code not in codes:
                continue
            c = run_case(name, code, f"{i}", monster_speed=speed)
            res = "成功" if c["ok"] and not c["skipped"] else ("跳过" if c["skipped"] else "失败")
            err = f" — {c['err']}" if c["err"] else ""
            diff = "；".join(c["diff"]) or "（无变化）"
            A(f"| {name} | {code} | {c['caster']} | {c['target']} | {res}{err} | {c['spread']} "
              f"| {c['fx'][:200]} | {diff[:200]} | {','.join(c['marks_after']) or '—'} |")
    A("")
    A("## 对照：速度类道纹在「怪物发动」场景（怪速 6，含 6 次普攻）")
    A("")
    A("上面 M0/M2/M4 用的是 0 速怪物（零命中、无普攻污染），代价是速度类道纹作用在怪物自己身上"
      "看不出数值。这里把怪物速度放开到 6 重跑一遍：「贾凡.生命」的下降是普攻造成的，不是道纹。")
    A("")
    A("| 道纹 | 态 | 发动方 | 目标提交 | 结果 | 扩散 | 效果 | 状态变化 |")
    A("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, name in enumerate(n for n in names if n in SPEED_FAMILY):
        for code, side, _mode, _sp, _desc in SCENARIOS:
            if side != "怪物" or code not in codes:
                continue
            c = run_case(name, code, f"sp{i}", monster_speed=6)
            res = "成功" if c["ok"] and not c["skipped"] else ("跳过" if c["skipped"] else "失败")
            err = f" — {c['err']}" if c["err"] else ""
            A(f"| {name} | {code} | {c['caster']} | {c['target']} | {res}{err} | {c['spread']} "
              f"| {c['fx'][:180]} | {'；'.join(c['diff'])[:200] or '（无变化）'} |")
    A("")
    A("## S5 补充：两个波及目标时才看得到「数值平分」")
    A("")
    A("1v1 互标时，本次[目标]与波及目标是同一个人，去重后只剩 1 个 → 不平分（全额打在对方身上）。"
      "下面用「轮回者＋两只怪，两只都被轮回者标记」演示总量不变、一分为二。")
    A("")
    for row in run_s5([n for n in ("杀伐", "再生", "庇护", "减速", "贯穿") if n in names] or ["杀伐"]):
        A(row)
    A("")
    text = "\n".join(lines) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"written {args.out}: {len(lines)} lines")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
