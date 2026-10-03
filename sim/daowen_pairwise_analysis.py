#!/usr/bin/env python3
"""道纹两两协同穷举分析（observational measurement；不修改任何游戏规则）。

    70 道纹 → C(70,2) = 2415 组无序对，全部跑一遍。

依据全部取生产代码：
  * 词汇表：`engine.daowen.DaoWenEngine._registry`（生产注册表，唯一事实源）
  * 目标语义：`engine.combat_parts.monster_phase._daowen_requires_target` 同口径
    （= 计算函数签名里有没有 `target` 参数），再由**执行结果**标定施加侧
  * 场景：生产 `GameEngine` 搭受控沙盒；发动走 `use_daowen`；
    伤害/代价/回复/速度/血限走生产总线（`_apply_hostile_damage` /
    `pay_numeric_cost` / `apply_heal` / `_lose_current_speed` / `_apply_blood_limit_change`）；
    敌方攻击走真实两阶段怪物阶段 `prepare_monster_phase` + `resolve_monster_phase`。
    绝不复制第二套战斗逻辑。

基线复用：∅（不发动）与每个道纹的 solo 只跑一次，全对共用——所以 solo 结果在
所有含该道纹的对里逐位一致，可加复合的分母不会漂。

用法：
    python sim/daowen_pairwise_analysis.py --all            # 全量（慢，可断点续跑）
    python sim/daowen_pairwise_analysis.py --vocab          # 只导出词汇表
    python sim/daowen_pairwise_analysis.py --pair 固执,龙鳞  # 单对重放（校验用）
    python sim/daowen_pairwise_analysis.py --report         # 只用缓存重算报告
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPORTS = ROOT / "reports"
CACHE = REPORTS / "_cache"
CACHE.mkdir(parents=True, exist_ok=True)

import unicodedata

CAST_X = 3                      # 统一发动 X
SB = dict(                       # 受控沙盒参数（固定常量，保证可复现）
    player_hp=200, player_bl=400, player_mana=55, player_ml=60,   # 留缺口使「回复/得法力」可见；池够两次最贵发动(2×25)
    player_speed=5, player_sl=5,
    enemy_hp=3000, enemy_bl=3000, enemy_mana=20, enemy_ml=20,
    enemy_speed=3, enemy_sl=3,
    shards=100, fake_shards=400,
)
EMPTY_SIDE = {"hp": 0, "bl": 0, "mana": 0, "speed": 0, "shield": 0,
              "atk": 0, "pow": 0, "mut": 0,
              "statuses": {}, "status_rounds": {}, "alive": False}


# ===========================================================================
# 0. 词汇表（生产注册表）
# ===========================================================================

def vocabulary() -> dict:
    import inspect
    from engine.daowen import DaoWenEngine as D
    D.register_all()
    out = {}
    for name, fn in D._registry.items():
        sig = inspect.signature(fn)
        kw = {}
        for p in sig.parameters:
            if p == "x":
                kw[p] = CAST_X
            elif p == "y":
                kw[p] = 1
            else:
                kw[p] = None
        calc = fn(**kw)
        out[name] = {
            "formula": (inspect.getdoc(fn) or "").splitlines()[0],
            "requires_target": "target" in sig.parameters,
            "calc_at_x3": {k: v for k, v in calc.items() if k != "summary"},
            "summary_at_x3": calc.get("summary", ""),
        }
    return out


def region_of(name: str) -> str:
    from engine.gamedata import REGION_EXCLUSIVE_DAOWEN
    for region, names in REGION_EXCLUSIVE_DAOWEN.items():
        if name in names:
            return region
    return "通用/怪物"


# ===========================================================================
# 1. 沙盒
# ===========================================================================

def build_sandbox(tmp: Path, granted: tuple = ()):
    from engine.api import GameEngine
    from tests.setup_support import begin_battle, begin_round, finish_initial_daowen
    e = GameEngine(db_path=str(tmp / "d.db"), save_dir=str(tmp),
                   sealed_candidate_path=str(tmp / "x.json"), rng_seed=7)
    assert e.execute_action("setup_attributes", {
        "name": "甲", "blood_points": 11, "speed_points": 8, "mana_points": 6})["success"]
    assert finish_initial_daowen(e)["success"]
    assert e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})["success"]
    assert e.execute_action("setup_choose_region", {"region": "扭曲都市"})["success"]
    assert begin_battle(e)["success"]
    assert begin_round(e)["success"]
    p = e.state.player
    e.state.relics = []
    p.blood_limit, p.current_hp = SB["player_bl"], SB["player_hp"]
    p.mana_limit, p.current_mana = SB["player_ml"], SB["player_mana"]
    p.speed_limit, p.current_speed = SB["player_sl"], SB["player_speed"]
    p.shield = 0
    en = e.state.enemies[0]
    en.attack_count, en.attack_power = SB["enemy_speed"], SB["enemy_mana"]
    en.blood_limit, en.current_hp = SB["enemy_bl"], SB["enemy_hp"]
    en.speed_limit, en.current_speed = SB["enemy_sl"], SB["enemy_speed"]
    en.mana_limit, en.current_mana = SB["enemy_ml"], SB["enemy_mana"]
    en.shield = 0
    en.dao_wen = {}                     # 受控：敌方不发动道纹，普攻可预测（3 击 × 20）
    e.state.shards = SB["shards"]
    e.state.fake_shards = SB["fake_shards"]
    for name in granted:
        e._grant_named_daowen(p, name)
    return e


# ===========================================================================
# 2. 事件采集
# ===========================================================================

def _side(x):
    if x is None:
        return dict(EMPTY_SIDE)
    return {"hp": x.current_hp, "bl": x.blood_limit, "mana": x.current_mana,
            "speed": x.current_speed, "shield": x.shield,
            "atk": x.effective_attack_count(), "pow": x.effective_attack_power(),
            "mut": x.mutation_count,
            "statuses": {s.name: s.value for s in x.status_effects},
            "status_rounds": {s.name: s.remaining_rounds for s in x.status_effects},
            "alive": bool(x.is_alive)}


def snapshot(e) -> dict:
    en = e.state.enemies[0] if getattr(e.state, "enemies", None) else None
    return {"player": _side(e.state.player), "enemy": _side(en),
            "shards": e.state.shards, "fake_shards": e.state.fake_shards,
            "rerolls": getattr(getattr(e, "dice", None), "rerolls_pending", 0),
            "friends": len(getattr(e.state, "friends", []) or [])}


EVENT_KEYS = ("target", "amount", "raw_damage", "actual_damage", "shield_absorbed",
              "hp_before", "hp_after", "gained", "lost", "spent", "damage", "value",
              "duration", "capped_by", "source", "status", "heal", "actual_heal",
              "died", "dragon_heart_offset", "blood_loss", "blood_limit_after",
              "hit_index", "hit_total", "damage_type", "attacker", "owner",
              "blood_limit_before", "pool")


def flat_events(obj, out: list, depth: int = 0):
    if depth > 10:
        return out
    if isinstance(obj, list):
        for it in obj:
            flat_events(it, out, depth + 1)
        return out
    if not isinstance(obj, dict):
        return out
    t = obj.get("type")
    if t is None and "raw_damage" in obj and "actual_damage" in obj:
        t = "damage"
    elif t is None and "hit_index" in obj and "attacker" in obj:
        t = "hit"
    if isinstance(t, str):
        ev = {"type": t}
        for k in EVENT_KEYS:
            v = obj.get(k)
            if isinstance(v, (int, float, str, bool)):
                ev[k] = v
        out.append(ev)
    for v in obj.values():
        if isinstance(v, (dict, list)):
            flat_events(v, out, depth + 1)
    return out


def event_sig(events: list) -> list:
    """事件表 → 可比较的紧凑签名（保序）。"""
    sig = []
    for ev in events:
        bits = [ev["type"]]
        for k in ("target", "actual_damage", "raw_damage", "amount", "gained", "lost",
                  "spent", "value", "duration", "capped_by", "status", "damage",
                  "shield_absorbed", "hp_before", "hp_after", "died", "blood_loss",
                  "dragon_heart_offset", "hit_index"):
            if k in ev:
                bits.append(f"{k}={ev[k]}")
        sig.append(":".join(bits))
    return sig


OBS = [("p_hp", "player", "hp"), ("p_bl", "player", "bl"), ("p_mana", "player", "mana"),
       ("p_speed", "player", "speed"), ("p_shield", "player", "shield"),
       ("p_atk", "player", "atk"), ("p_pow", "player", "pow"), ("p_mut", "player", "mut"),
       ("e_hp", "enemy", "hp"), ("e_bl", "enemy", "bl"), ("e_mana", "enemy", "mana"),
       ("e_speed", "enemy", "speed"), ("e_shield", "enemy", "shield"),
       ("e_atk", "enemy", "atk"), ("e_pow", "enemy", "pow"), ("e_mut", "enemy", "mut"),
       ("shards", None, "shards"), ("fake", None, "fake_shards"),
       ("rerolls", None, "rerolls"), ("friends", None, "friends")]


def measure(before: dict, after: dict) -> dict:
    d = {}
    for name, side, field in OBS:
        try:
            src_a = after if side is None else after[side]
            src_b = before if side is None else before[side]
            d[name] = src_a[field] - src_b[field]
        except (KeyError, TypeError):
            d[name] = 0
    for side in ("player", "enemy"):
        for st, v in after[side]["statuses"].items():
            if st not in before[side]["statuses"]:
                d[f"st+{side[:1]}:{st}"] = v
            elif v != before[side]["statuses"][st]:
                d[f"st~{side[:1]}:{st}"] = v - before[side]["statuses"][st]
    for side in ("player", "enemy"):
        bsr = before[side].get("status_rounds", {})
        asr = after[side].get("status_rounds", {})
        for st in set(bsr) | set(asr):
            if st in bsr and st in asr and asr[st] != bsr[st]:
                d[f"stR{side[:1]}:{st}"] = asr[st] - bsr[st]
        for st in set(before[side]["statuses"]) - set(after[side]["statuses"]):
            d[f"st-{side[:1]}:{st}"] = -before[side]["statuses"][st]
    if before["player"]["alive"] and not after["player"]["alive"]:
        d["p_died"] = 1
    if before["enemy"]["alive"] and not after["enemy"]["alive"]:
        d["e_died"] = 1
    return d


# ===========================================================================
# 3. 探针（生产总线）
# ===========================================================================

def _dmg(e, target, amount, source=None):
    return e.combat._apply_hostile_damage(target, amount, "普通", source=source)


def _monster_phase(e, *, dodge: bool):
    prep = e.execute_action("prepare_monster_phase", {})
    if not prep.get("success"):
        return [prep]
    res = prep["result"]
    choices = []
    for actor in res.get("actors") or []:
        n_hits = max(1, int(actor.get("base_hits_per_attack", 1)))
        one_hit = {"target_ref": "player:0", "dodge": bool(dodge),
                   "blood_shadow": False,
                   "spell_choices": {"before": {}, "after": {}}}
        one_action = {"hits": [dict(one_hit) for _ in range(n_hits)]}
        choices.append({"actor_ref": actor["actor_ref"], "daowen": None,
                        "attack_actions": [copy.deepcopy(one_action)
                                           for _ in range(int(actor.get("base_attack_actions", 0)))]})
    return [e.execute_action("resolve_monster_phase",
                             {"token": res["token"], "choices": choices})]


PROBES = {
    "incoming_big":      lambda e: [_dmg(e, e.state.player, 100, source=e.state.enemies[0])],
    "incoming_multi":    lambda e: [_dmg(e, e.state.player, 20, source=e.state.enemies[0]) for _ in range(5)],
    "enemy_phase":       lambda e: _monster_phase(e, dodge=False),
    "enemy_phase_dodge": lambda e: _monster_phase(e, dodge=True),
    "outgoing_big":      lambda e: [_dmg(e, e.state.enemies[0], 100, source=e.state.player)],
    "outgoing_multi":    lambda e: [_dmg(e, e.state.enemies[0], 20, source=e.state.player) for _ in range(5)],
    "bleed_cost":        lambda e: [e.combat.pay_numeric_cost(
        e.state.player, "流血", 40,
        cost_context={"timing": "player_action", "source": "probe",
                      "source_type": "probe", "tags": {"active_payment"}})],
    "heal":              lambda e: [e.state.apply_heal(e.state.player, 60)],
    "kill_enemy":        lambda e: [_dmg(e, e.state.enemies[0], 99999, source=e.state.player)],
    "rounds":            lambda e: [x for _ in range(3) for x in _advance_round(e)],
    "speed_loss":        lambda e: [e.combat._lose_current_speed(e.state.player, 3)],
    "bl_loss":           lambda e: [e.combat._apply_blood_limit_change(
        e.state.player, -50, "probe", "debuff", source_type="probe", subtype="probe")],
    "mana_cycle":        lambda e: [e.state.player.spend_mana(5),
                                    {"type": "mana_gain", "target": "甲", "gained": 5}],
    "player_attacks":    lambda e: _player_attacks(e),
    "player_attacks_dodged": lambda e: _player_attacks(e, times=1, dodge=True),
    "hurt_then_attack":  lambda e: [_dmg(e, e.state.player, 50, source=e.state.enemies[0])]
                                   + _player_attacks(e, times=1),
    "dodge_then_round":  lambda e: _advance_round(e, dodge=True),
}


def _neutralize_action_budget(e):
    """把「本回合已用出手」归零，只用于攻击类探针。

    生产规则（engine/api.py::_action_budget_of）：每次 use_daowen 都消耗 1 次出手，
    所以任何两次独立发动都会等比减少本回合的普攻次数。这是「独立发动」的公共代价，
    与具体道纹对无关；按大纲「两个独立发动不构成协同」，必须在测量效果层交互前剥离。
    只归零已用计数，不改 action_count（出手上限被道纹改变时仍可观测）。
    """
    for ent in [e.state.player] + list(e.state.enemies):
        if ent is not None and hasattr(ent, "actions_used_this_round"):
            ent.actions_used_this_round = 0


def _player_attacks(e, times: int = 2, dodge: bool = False):
    """走生产攻击接口的两阶段提交（真实普攻）。"""
    _neutralize_action_budget(e)
    out = []
    for _ in range(times):
        prep = e.execute_action("prepare_attack", {"actor_ref": "player:0"})
        if not prep.get("success"):
            out.append(prep)
            break
        res = prep["result"]
        hits = [{"target_ref": "enemy:0", "dodge": dodge, "blood_shadow": False,
                 "spell_choices": {"before": {}, "after": {}}}
                for _ in range(int(res.get("hit_count", 1)))]
        out.append(e.execute_action("resolve_attack", {"token": res["token"], "hits": hits}))
    return out


def _subphase(e) -> str:
    return getattr(e.state, "combat_subphase", "")


def _advance_round(e, dodge: bool = False):
    """真正走完一个回合：怪物阶段 → 回终 → 回始。

    生产子阶段是串行的：玩家行动阶段只能提交 `prepare_monster_phase`，怪物阶段只能
    提交 `resolve_monster_phase`，之后才轮到 `await_round_end` / `await_round_start`
    （`api.py:962-1000` 的门禁表）。所以「回合推进」这一族场景必须按这条链走，
    否则 round_start/round_end 会被门禁直接拒绝（此前的探针就踩了这个坑）。
    """
    out = []
    if _subphase(e) == "player_actions":
        out += _monster_phase(e, dodge=dodge)
    else:
        out += _monster_phase(e, dodge=dodge) if _subphase(e) == "monster_actions" else []
    if _subphase(e) == "await_round_end" and any(x.is_alive for x in e.state.enemies):
        out.append(e.execute_action("round_end", {}))
    if _subphase(e) == "await_round_start":
        out.append(e.execute_action("round_start", {"relic_choices": {}}))
    return out

PROBE_NAMES = list(PROBES)


def cast_params(e, name: str, x: int, target_ref: str) -> dict:
    """生产 use_daowen 的提交参数；波及需要显式 dodge_targets（X=合法目标数）。"""
    params = {"actor_ref": "player:0", "daowen_name": name, "x": x,
              "target_ref": target_ref, "dodge": False, "blood_shadow": False,
              "trigger_spell_choices": {}}
    if name == "波及":
        refs = [r for r in ("enemy:0",) if r in e.combat._combat_entity_refs()]
        n = max(1, min(x, len(refs)))
        params["x"] = n
        params["dodge_targets"] = [{"target_ref": r, "dodge": False, "blood_shadow": False}
                                   for r in refs[:n]]
    return params


def _pending_count(e) -> int:
    try:
        return len(getattr(e, "_pending_interrupts", []) or [])
    except Exception:
        return 0


def run_scene(e, casts: list) -> dict:
    cast_log = []
    for c in casts:
        before = snapshot(e)
        n_before = _pending_count(e)
        params = cast_params(e, c["name"], c["x"], c["target_ref"])
        r = e.execute_action("use_daowen", params)
        after = snapshot(e)
        n_after = _pending_count(e)
        intr = getattr(e, "_pending_interrupts", []) or []
        cast_log.append({"name": c["name"], "x": params["x"], "target": c["target_ref"],
                         "ok": bool(r.get("success")), "error": r.get("error", ""),
                         "events": flat_events(r.get("result") or {}, []),
                         "pending_interrupts_before": n_before,
                         "pending_interrupts_after": n_after,
                         "interrupt_type": (intr[-1].interrupt_type.value
                                            if (n_after > n_before and intr
                                                and hasattr(intr[-1], "interrupt_type"))
                                            else ""),
                         "delta": measure(before, after)})
    probe_log = {}
    for pname, fn in PROBES.items():
        sub = copy.deepcopy(e)
        before = snapshot(sub)
        try:
            raw = fn(sub)
            ok, err = True, ""
        except Exception as exc:
            raw, ok, err = [], False, f"{type(exc).__name__}: {exc}"
        after = snapshot(sub)
        probe_log[pname] = {"ok": ok, "error": err,
                            "events": flat_events(raw, []),
                            "delta": measure(before, after)}
    return {"casts": cast_log, "probes": probe_log}


# ===========================================================================
# 4. 场景缓存（基线 / solo / 对）
# ===========================================================================

def _w(p: Path, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _r(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


SCENE_VERSION = 3   # 沙盒常量/探针集/快照口径的版本戳：改动它们必须 +1，否则旧缓存会被误用
                    # （v3：自然目标侧 + 出手预算中性化 + 回合链修正 + 资源/rerolls 通道 + 池 55/60）


def _cached(path: Path):
    """读缓存，但版本戳不符就当作不存在（防止改了口径还在用旧场景）。"""
    if not path.exists():
        return None
    try:
        data = _r(path)
    except Exception:
        return None
    if isinstance(data, dict) and data.get("_v") == SCENE_VERSION:
        return data
    return None


def get_baseline(force=False) -> dict:
    f = CACHE / "baseline.json"
    if not force:
        hit = _cached(f)
        if hit is not None:
            return hit
    tmp = Path(tempfile.mkdtemp())
    scene = run_scene(build_sandbox(tmp), [])
    shutil.rmtree(tmp, ignore_errors=True)
    scene["_v"] = SCENE_VERSION
    _w(f, scene)
    return scene


def solo_path(name: str, target: str) -> Path:
    return CACHE / "solo" / f"{name}__{target.replace(':', '')}.json"


def get_solo(name: str, target: str, force=False) -> dict:
    f = solo_path(name, target)
    if not force:
        hit = _cached(f)
        if hit is not None:
            return hit
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    scene = run_scene(build_sandbox(tmp, (name,)),
                      [{"name": name, "x": CAST_X, "target_ref": target}])
    shutil.rmtree(tmp, ignore_errors=True)
    scene["target"] = target
    scene["_v"] = SCENE_VERSION
    _w(f, scene)
    return scene


def get_pair(a: str, ta: str, b: str, tb: str, force=False) -> dict:
    f = CACHE / "pairs" / f"{a}__{b}.json"
    if not force:
        hit = _cached(f)
        if hit is not None:
            return hit
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    e = build_sandbox(tmp, (a, b))
    e_ba = copy.deepcopy(e)                      # 未发动过的副本，用于 B→A
    ab = run_scene(e, [{"name": a, "x": CAST_X, "target_ref": ta},
                       {"name": b, "x": CAST_X, "target_ref": tb}])
    ba = run_scene(e_ba, [{"name": b, "x": CAST_X, "target_ref": tb},
                          {"name": a, "x": CAST_X, "target_ref": ta}])
    shutil.rmtree(tmp, ignore_errors=True)
    out = {"ab": ab, "ba": ba, "_v": SCENE_VERSION,
           "targets": {a: ta, b: tb}}
    _w(f, out)
    return out


# ===========================================================================
# 5. 分类
# ===========================================================================

CAST_CHANNEL = "__cast__"


def _delta_of(scene: dict, probe: str) -> dict:
    if probe == CAST_CHANNEL:
        tot = {}
        for c in scene["casts"]:
            for k, v in c["delta"].items():
                tot[k] = tot.get(k, 0) + v
        return tot
    return scene["probes"][probe]["delta"]


def _events_of(scene: dict, probe: str) -> list:
    if probe == CAST_CHANNEL:
        return event_sig([e for c in scene["casts"] for e in c["events"]])
    return event_sig(scene["probes"][probe]["events"])


ALL_CHANNELS = [CAST_CHANNEL] + PROBE_NAMES


def _evtypes(evs) -> dict:
    c = {}
    for e in evs:
        t = e.split(":", 1)[0]
        c[t] = c.get(t, 0) + 1
    return c


RESOURCE_OBS = {"p_mana", "p_shield", "e_mana", "e_shield", "shards", "fake"}


def scene_problem(scene: dict) -> str:
    """场景未能忠实执行的原因（空串=正常）。

    只有两类：某次发动被生产引擎拒绝（含法力不足、目标非法），或发动后留下待 DM
    裁定的中断，导致同一场景里的后续发动被门禁挡住（api.py:1084）。后者不是道纹对
    之间的规则，分析里必须显式排除而不是当成协同。
    """
    errs = []
    for c in scene.get("casts", []):
        if not c["ok"]:
            errs.append(f"{c['name']}发动失败({c['error'][:32]})")
        if c.get("pending_interrupts_after", 0) > 0:
            errs.append(f"{c['name']}留下待裁定中断({c.get('interrupt_type','?')})")
    return "；".join(errs)


def classify_pair(a: str, b: str, base: dict, sa: dict, sb: dict, pair: dict) -> dict:
    ab_scene, ba_scene = pair["ab"], pair["ba"]
    findings = []
    shared_channels = set()

    for probe in ALL_CHANNELS:
        if probe in base["probes"] and not base["probes"][probe].get("ok", True):
            continue  # 探针在生产入口上抛错 → 该通道不可用，不参与判定
        db = _delta_of(base, probe)
        da_solo = _delta_of(sa, probe)
        db_solo = _delta_of(sb, probe)
        da_ab = _delta_of(ab_scene, probe)
        da_ba = _delta_of(ba_scene, probe)
        for k in sorted(set(db) | set(da_solo) | set(db_solo) | set(da_ab) | set(da_ba)):
            v_base = db.get(k, 0)
            va = da_solo.get(k, 0) - v_base
            vb = db_solo.get(k, 0) - v_base
            vab = da_ab.get(k, 0) - v_base
            vba = da_ba.get(k, 0) - v_base
            if va == 0 and vb == 0 and vab == 0:
                continue
            if va != 0 and vb != 0:
                shared_channels.add(k)
            if vab != va + vb or vba != va + vb:
                findings.append({"probe": probe, "obs": k, "base": v_base,
                                 "a": va, "b": vb, "ab": vab, "ba": vba,
                                 "expected_additive": va + vb})

        ev_base = _evtypes(_events_of(base, probe))
        ev_a = _evtypes(_events_of(sa, probe))
        ev_b = _evtypes(_events_of(sb, probe))
        ev_ab = _evtypes(_events_of(ab_scene, probe))
        for t, n in ev_ab.items():
            if t not in ev_base and t not in ev_a and t not in ev_b:
                findings.append({"probe": probe, "obs": f"event:{t}",
                                 "kind": "NEW_EVENT_TYPE", "count": n,
                                 "base": ev_base.get(t, 0), "a": ev_a.get(t, 0),
                                 "b": ev_b.get(t, 0), "ab": n})
            elif n > ev_a.get(t, 0) + ev_b.get(t, 0) and (ev_a.get(t, 0) + ev_b.get(t, 0)) > 0:
                findings.append({"probe": probe, "obs": f"event:{t}", "kind": "EVENT_COUNT",
                                 "ab": n, "a": ev_a.get(t, 0), "b": ev_b.get(t, 0)})

    order_sensitive = False
    order_detail = ""
    diverged = [p for p in ALL_CHANNELS
                if (_delta_of(ab_scene, p) != _delta_of(ba_scene, p)
                    or _events_of(ab_scene, p) != _events_of(ba_scene, p))]
    if diverged:
        order_sensitive = True
        # 优先报探针级分歧（比"发动窗口"更有信息量），没有才退回 __cast__
        order_detail = next((p for p in diverged if p != CAST_CHANNEL), CAST_CHANNEL)
        order_detail = (order_detail + f"（共{len(diverged)}个通道分歧）"
                        if len(diverged) > 1 else order_detail)

    validity = {k: scene_problem(s) for k, s in
                (("A独发", sa), ("B独发", sb), ("A→B", ab_scene), ("B→A", ba_scene))}
    problems = {k: v for k, v in validity.items() if v}

    if not findings:
        cls = 1 if shared_channels else 0
        types = []
        kind = ""
    else:
        cls = 2
        types = set()
        for f in findings:
            if f.get("kind") == "NEW_EVENT_TYPE":
                types.add("EVENT_CONVERSION")
            elif f.get("kind") == "EVENT_COUNT":
                types.add("EVENT_MULTIPLICATION")
            elif f["obs"] in RESOURCE_OBS:
                types.add("RESOURCE_FEEDBACK")
            elif f["probe"] == CAST_CHANNEL:
                types.add("CAST_EXECUTION")     # 发动本身的代价/数值/结果被改变（族 A）
            else:
                types.add("OUTPUT_ARITHMETIC")  # 结算层的数值耦合（族 D/F）
        if order_sensitive:
            types.add("ORDER_SENSITIVE")
        if "EVENT_CONVERSION" in types or "EVENT_MULTIPLICATION" in types:
            kind = "EVENT_LEVEL"
        elif "CAST_EXECUTION" in types:
            kind = "CAST_EXECUTION"
        elif "ORDER_SENSITIVE" in types:
            kind = "ORDER_ONLY"
        else:
            kind = "SCALAR_COUPLING"
        types = sorted(types)

    probes_hit = sorted({f["probe"] for f in findings})

    def _cell(scene, probe, obs):
        if obs.startswith("event:"):
            return [x for x in _events_of(scene, probe) if x.startswith(obs[6:])]
        return _delta_of(scene, probe).get(obs, 0)

    causal = ""
    if findings:
        f0 = findings[0]
        p0, o0 = f0["probe"], f0["obs"]
        causal = (f"{p0} / {o0}：基线={_cell(base, p0, o0)}"
                  f"；{a}独发={_cell(sa, p0, o0)}；{b}独发={_cell(sb, p0, o0)}"
                  f"；并施 A→B={_cell(ab_scene, p0, o0)}；B→A={_cell(ba_scene, p0, o0)}"
                  + (f"（可加预期={f0['expected_additive']}）" if "expected_additive" in f0
                     else f"（{f0.get('kind','')}）"))
    return {"classification": cls, "interaction_types": types, "synergy_kind": kind,
            "causal": causal,
            "findings": findings[:6], "n_findings": len(findings),
            "shared_channels": sorted(shared_channels),
            "probes_hit": probes_hit, "order_sensitive": order_sensitive,
            "order_detail": order_detail,
            "scene_problems": problems,
            "verifiable": not problems,
            "order_sensitive_note": ("两序均已忠实执行" if not problems else "执行受限，顺序对比不作数"),
            "exercised_a": bool(sa["casts"][0]["ok"] or sa["casts"][0]["events"]) if sa["casts"] else False,
            "exercised_b": bool(sb["casts"][0]["ok"] or sb["casts"][0]["events"]) if sb["casts"] else False}


# ===========================================================================
# 6. 目标语义标定
# ===========================================================================

# 自然目标侧（场景参数，不是规则改动）。判据只有一条：生产 summary 里这个效果的
# 受益方是施法者还是目标。写进下面「敌侧」名单的，summary 都明确把伤害/减益施加
# 在未选定目标身上（受益方=施法者），故对敌发动才是真实打法；其余默认对自己发动。
# 名单是穷举的（70 个道纹里属于敌侧的都在这里），可在报告第 1 节逐条核对。
HOSTILE_SIDE = {
    "杀伐": "对未选定目标造成9点伤害",
    "血债": "选择未选定目标 3次，每次造成1点伤害",
    "束缚": "使未选定目标无法行动",
    "封印": "使未选定目标延后3回合再入场",
    "减速": "使未选定目标速度减半",
    "自残": "使未选定目标对自身打出3次攻击",
    "无神": "使未选定目标选择目标时强制改为自身",
    "弱化": "使未选定目标攻击力-3",
    "无力": "回始使未选定目标出手次数-3",
    "眩晕": "使未选定目标无法出手",
    "蒙蔽": "使未选定目标下3次造成的伤害无效",
    "衰败": "使未选定目标[回始]失去30%当前生命",
    "寄生": "使未选定目标受到伤害的60%转化为未知施法者的回复",
    "畸变": "回终使未选定目标失去0血限",
    "坏死": "使未选定目标无法获得回复",
    "退化": "使未选定目标每次发动道纹数值-3(最低0)",
    "加害": "使未选定目标每次受到伤害+3",
    "逼债": "[回始]使未选定目标失去3碎片",
    "豪夺": "夺取未选定目标一件遗物",
    "清算": "[回始]使未选定目标失去0格挡",
    "赎金": "夺取未选定目标 30碎片或3速度",
    "伤痕": "使未选定目标每次掉血后血限-3",
    "瓦解": "未选定目标血限-30%",
    "冥气": "3回合内未选定目标每失去速度速限-2",
    "勾魂": "使未选定目标法力消耗翻倍",
    "镇尸": "使未选定目标无法获得回复",
    "嫁祸": "自身下3次受伤由未选定目标承担",
    "变形": "使[目标]当前速度与当前法力互换",
    "坠落": "所有飞行角色无法飞行且造成伤害减半",
}
# AoE / 无目标语义 / 需要场上无对象的道纹：对自身发动，目标参数不改变效果。
SELF_SIDE_NOTE = {
    "波及": "对最多3个目标建立/解除波及标记（AoE）",
    "疯狂": "所有角色出手次数+3（全场）",
    "缄默": "封禁全场[命零]触发效果（全场）",
    "尸爆": "[命零]对全体敌造成伤害（触发式）",
    "赌命": "每[回始]随机一名存活角色失去生命（随机单目标）",
    "分裂": "创造自身复制体（自身）",
    "招魂": "唤回已灭怪物作为临时朋友（需要已灭怪物）",
    "背负": "目标受伤由自身承担（正常打法为友方；沙盒无友方，取自身）",
    "定型": "目标攻击次数与攻击力无法被改变（攻守皆可，取自身）",
    "裂变": "受伤分3次结算（保护自身）",
}


def natural_target(name: str) -> str:
    return "enemy:0" if name in HOSTILE_SIDE else "player:0"


def visibility(scene: dict, base: dict) -> float:
    """该 solo 相对基线在所有探针/通道上造成的总偏离量（= 被真正发动了的程度）。"""
    tot = 0.0
    for probe in PROBE_NAMES:
        db = _delta_of(base, probe)
        ds = _delta_of(scene, probe)
        for k in set(db) | set(ds):
            tot += abs(ds.get(k, 0) - db.get(k, 0))
        tot += 0.5 * abs(len(_events_of(scene, probe)) - len(_events_of(base, probe)))
    return tot


def resolve_targets(names, base, force=False) -> dict:
    """每个道纹的规范发动侧 = 自然目标侧（见 HOSTILE_SIDE 名单），并记录两侧可见度。"""
    f = CACHE / "targets.json"
    if f.exists() and not force:
        cached = _r(f)
        if set(cached) == set(names) and all("basis" in v for v in cached.values()):
            return cached
    out = {}
    for n in names:
        tgt = natural_target(n)
        other = "enemy:0" if tgt == "player:0" else "player:0"
        sc = get_solo(n, tgt, force=force)
        v = visibility(sc, base)
        out[n] = {"target": tgt,
                  "basis": "summary:hostile" if n in HOSTILE_SIDE else "default:self",
                  "summary_note": HOSTILE_SIDE.get(n, SELF_SIDE_NOTE.get(n, "")),
                  "visibility": v,
                  "alt_target": other,
                  "alt_visibility": visibility(get_solo(n, other, force=force), base),
                  "cast_ok": bool(sc["casts"][0]["ok"]) if sc["casts"] else False}
    _w(f, out)
    return out


# ===========================================================================
# 7. 全量扫描
# ===========================================================================

def sweep(names, targets, base, do_solo=True, do_pairs=True, verbose=True, force=False):
    t0 = time.time()
    if do_solo:
        for i, n in enumerate(names, 1):
            get_solo(n, targets[n]["target"], force=force)
            if verbose and i % 10 == 0:
                print(f"  solo {i}/{len(names)}  ({time.time()-t0:.0f}s)", flush=True)
    if do_pairs:
        done = 0
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                get_pair(a, targets[a]["target"], b, targets[b]["target"], force=force)
                done += 1
                if verbose and done % 100 == 0:
                    print(f"  pair {done}  ({time.time()-t0:.0f}s)", flush=True)
    return time.time() - t0


def cache_consistency(names, targets) -> list:
    """快速自检：每对缓存里的记录目标与当前 resolve_targets 是否一致（防串味）。"""
    bad = []
    for a, b in ((names[i], names[j]) for i, x in enumerate(names) for j, y in enumerate(names) if j > i):
        f = CACHE / "pairs" / f"{a}__{b}.json"
        data = _cached(f)
        if data is None:
            bad.append(f"{a}+{b}:缓存缺失或版本不符")
            continue
        tg = data.get("targets", {})
        if tg.get(a) != targets[a]["target"] or tg.get(b) != targets[b]["target"]:
            bad.append(f"{a}+{b}:目标侧不符")
        if len(bad) > 5:
            break
    return bad


def build_rows(names, targets, base):
    rows = []
    for i, a in enumerate(names):
        sa = get_solo(a, targets[a]["target"])
        for b in names[i + 1:]:
            sb = get_solo(b, targets[b]["target"])
            pair = get_pair(a, targets[a]["target"], b, targets[b]["target"])
            r = classify_pair(a, b, base, sa, sb, pair)
            r["daowen_a"], r["daowen_b"] = a, b
            rows.append(r)
    return rows


# ===========================================================================
# 8. 输出
# ===========================================================================

def causal_string(a, b, base, sa, sb, pair, finding) -> str:
    probe, obs = finding["probe"], finding["obs"]
    def g(sc):
        if obs.startswith("event:"):
            return [e for e in _events_of(sc, probe) if e.startswith(obs[6:])]
        return _delta_of(sc, probe).get(obs, 0)
    return (f"probe={probe} obs={obs} | 基线={g(base)} "
            f"| {a}独发={g(sa)} | {b}独发={g(sb)} "
            f"| {a}+{b}={g(pair['ab'])}（可加预期 {finding.get('expected_additive', '?')}）")


def evidence_ids(a, b, r):
    return ";".join(f"{a}+{b}@{p}" for p in r["probes_hit"][:3]) or f"{a}+{b}@none"


def write_outputs(names, targets, vocab, rows, extra):
    import csv
    REPORTS.mkdir(parents=True, exist_ok=True)

    (CACHE / "rows.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    csv_path = REPORTS / "daowen_pairwise_synergy.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["daowen_a", "daowen_b", "classification", "scenarios_tested",
                    "interaction_detected", "interaction_type", "causal_trace",
                    "order_sensitive", "evidence", "confidence"])
        for r in rows:
            a, b = r["daowen_a"], r["daowen_b"]
            cls = r["classification"]
            if cls == 2:
                causal = r["causal"]
                conf = "HIGH"
            elif cls == 1:
                causal = ("同通道线性叠加：" + ",".join(r["shared_channels"][:4]))
                conf = "HIGH"
            else:
                causal = "所有探针/通道上均无可加偏离（各自独立）"
                conf = "MEDIUM"
            if cls == 2 and r["n_findings"] > 0 and r["order_sensitive"]:
                conf = "HIGH"
            if not r.get("verifiable", True):
                conf = "UNVERIFIED"
                causal = f"[执行受限 {r['scene_problems']}] " + (causal or "")
                if cls == 2:
                    cls = 2  # 分类保留，但置信度 UNVERIFIED，不计入结论统计
            w.writerow([a, b, cls, len(PROBE_NAMES),
                        1 if (r["classification"] == 2 and r.get("verifiable", True)) else 0,
                        "|".join(r["interaction_types"]),
                        causal, bool(r["order_sensitive"]),
                        evidence_ids(a, b, r), conf])

    # 邻接 / 图
    nodes = []
    for n in names:
        nodes.append({"name": n, "region": region_of(n),
                      "requires_target": vocab[n]["requires_target"],
                      "formula": vocab[n]["formula"],
                      "cast_target": targets[n]["target"],
                      "visibility": round(targets[n]["visibility"], 3)})
    edges = [{"a": r["daowen_a"], "b": r["daowen_b"], "classification": 2,
              "types": r["interaction_types"], "verifiable": bool(r.get("verifiable", True)),
              "scene_problems": r.get("scene_problems", {}),
              "evidence": [f"{r['daowen_a']}+{r['daowen_b']}@{p}" for p in r["probes_hit"]],
              "order_sensitive": r["order_sensitive"]}
             for r in rows if r["classification"] == 2]
    graph = {"nodes": nodes, "edges": edges,
             "meta": {"head": extra["head"], "daowen_count": len(names),
                      "pair_count": len(rows), "cast_x": CAST_X,
                      "note": "边=Cat2（非平凡协同）。图不是质量评分。"}}
    (REPORTS / "daowen_synergy_graph.json").write_text(
        json.dumps(graph, ensure_ascii=False, indent=1), encoding="utf-8")
    return csv_path


def co_obtainable(a: str, b: str) -> bool:
    ra, rb = region_of(a), region_of(b)
    exclusive = ("扭曲都市", "罪孽都市", "龙心谷", "乱葬岗")
    if ra in exclusive and rb in exclusive and ra != rb:
        return False
    return True


def most_connected(rows, names, verifiable_only=False):
    deg = {n: 0 for n in names}
    for r in rows:
        if r["classification"] == 2 and (not verifiable_only or r.get("verifiable", True)):
            deg[r["daowen_a"]] += 1
            deg[r["daowen_b"]] += 1
    return deg


def write_report_md(names, targets, vocab, rows, head, elapsed, run_cmd, base):
    n = len(names)
    total = len(rows)
    c2_all = [r for r in rows if r["classification"] == 2]
    c2 = [r for r in c2_all if r.get("verifiable", True)]
    unver = [r for r in rows if not r.get("verifiable", True)]
    deg = most_connected(rows, names, verifiable_only=True)
    c1 = [r for r in rows if r["classification"] == 1]
    c0 = [r for r in rows if r["classification"] == 0]
    pct = lambda k: f"{100.0*k/total:.1f}%"

    types = {}
    for r in c2:
        for t in r["interaction_types"]:
            types[t] = types.get(t, 0) + 1
    order_pairs = [r for r in c2 if r["order_sensitive"]]
    zero = sorted([x for x in names if deg[x] == 0])
    nonzero = sorted(((deg[x], x) for x in names if deg[x] > 0), reverse=True)
    mean_deg = 2 * len(c2) / n if n else 0
    median = sorted(deg.values())[n // 2]

    probes_used = {}
    for r in c2:
        for p in r["probes_hit"]:
            probes_used[p] = probes_used.get(p, 0) + 1

    kinds = {}
    for r in c2:
        k = r.get("synergy_kind") or "SCALAR_COUPLING"
        kinds[k] = kinds.get(k, 0) + 1

    L = []
    A = L.append
    A("# 道纹两两协同穷举分析（观测报告）")
    A("")
    A(f"> 仓库 HEAD：`{head}`｜分析对象：**当前生产道纹词汇表**（`engine/daowen.py` "
      f"`DaoWenEngine._registry`）")
    A(f"> 道纹 N = **{n}**｜无序对 C(N,2) = **{total}**｜统一发动 X = {CAST_X}｜扫描耗时 {elapsed:.0f}s")
    A(f"> 复现：`{run_cmd}`")
    A(f"> 被分析的引擎源码指纹：`engine_src_sha256={engine_fingerprint()[:16]}`"
      f"（对 `engine/**/*.py` 排序后拼接取 sha256；用于核对分析对象没被换过）")
    A("")
    A("> 本文件只做**测**，不做改。未修改任何道纹定义、数值、代价或规则。")
    A("")
    A("## 1. 方法与口径")
    A("")
    A("* **词汇表来源**：`engine/daowen.py::DaoWenEngine._registry`（生产注册表）。"
      "文档（`全道纹索引.md`）只用于交叉核对，凡与代码不一致处按代码记录，"
      "差异在第 9 节列出。")
    A("* **执行**：生产 `GameEngine`。发动走 `use_daowen`；伤害走 `CombatEngine._apply_hostile_damage`"
      "（引擎统一敌对伤害入口）；代价走 `pay_numeric_cost`；回复走 `GameState.apply_heal`；"
      "速度/血限走 `_lose_current_speed` / `_apply_blood_limit_change`；"
      "敌方攻击走真实两阶段怪物阶段 `prepare_monster_phase` + `resolve_monster_phase`。")
    A("* **没有第二套战斗引擎**：分析脚本只调用上述生产入口并读取生产返回值。")
    A("* **受控沙盒**（常量，非规则）：轮回者 生命 200/400、法力 55/60（留缺口使「回复/得法力」"
      "可见，且够两次最贵发动）、速度 5/5；"
      "敌方 生命 3000/3000、法力 20/20、速度 3/3（其道纹清空，使普攻恒为 3 击 × 20 点）。")
    A(f"* **探针**（{len(PROBE_NAMES)} 个，每个跑在独立 deepcopy 上）：" + "、".join(f"`{p}`" for p in PROBE_NAMES))
    A("* **发动侧（场景参数）**：按生产 `summary` 里「效果的受益方」定侧——summary 明确把伤害/减益"
      f"落在未选定目标身上的 {len(HOSTILE_SIDE)} 个道纹对敌发动（`HOSTILE_SIDE`），其余对自身发动；"
      "两侧各跑一次 solo，两侧可见度都记进 `daowen_synergy_graph.json`（`visibility`/`alt_visibility`）。"
      "**这是场景选择，不是规则改动**：生产引擎没有任何目标限制元数据，"
      "「对敌放庇护」这类非打法不进入判定，否则会造出与道纹对无关的假交互。")
    A("* **发动消耗出手**：生产规则里每次 `use_daowen` 都吃 1 次出手（`api.py::_action_budget_of`），"
      "所以任何两次独立发动都会等比减少本回合普攻次数。攻击类探针先把\n"
      "  `actions_used_this_round` 归零再打（只归零已用计数，不改 `action_count`），"
      "把「独立发动的公共代价」从效果层交互里剥离。")
    A("* **执行门禁**：若某次发动被引擎拒绝、或发动后留下待 DM 裁定的中断"
      "（`api.py:1084` 会挡住同场景后续动作），该对记 `confidence=UNVERIFIED`，"
      "不计入结论统计，只在第 6 节列出原因。")
    A("* **分类判据**（对每个探针 × 每个可观测通道）：")
    A("  * 设 A 独发、B 独发相对空基线的增量为 `a`、`b`，并施为 `ab`。")
    A("  * `ab == a + b` → 可加；两者都动同一通道 → **1 加和型**；从不共触同一通道 → **0 独立型**。")
    A("  * `ab != a + b`（或事件类型/计数出现新东西）→ **2 非平凡协同**，并记录因果串。")
    A("* **分类 2 不等于「设计得好」**：本文件只报告测量结果。")
    A("")
    A("## 2. 总量")
    A("")
    A("| 分类 | 对数 | 占比 |")
    A("| --- | ---: | ---: |")
    A(f"| 0 独立型 | {len(c0)} | {pct(len(c0))} |")
    A(f"| 1 加和型 | {len(c1)} | {pct(len(c1))} |")
    A(f"| 2 非平凡协同（可验证） | {len(c2)} | {pct(len(c2))} |")
    A(f"| 2' 判定为 2 但执行受限（UNVERIFIED，不计入结论） | {len(unver)} | {pct(len(unver))} |")
    A(f"| 合计 | {total} | 100% |")
    A("")
    A("> 每个对的 `classification` 只取 0/1/2 一个值；`2'` 是把「分类 2 但执行没有忠实复现」"
      "的对单独列出来（CSV 里 `confidence=UNVERIFIED`），它们不参与第 10 节任何计数。")
    A("")
    A(f"参战双方分属互斥副本、单局内不可能同时持有（仍照跑，只作可达性标注）的对：")
    A(f"**{sum(1 for r in rows if not co_obtainable(r['daowen_a'], r['daowen_b']))}** 对。")
    A("")
    A("## 3. 交互类型分布（仅分类 2，可验证）")
    A("")
    A("| 类型标签 | 对数 | 含义 |")
    A("| --- | ---: | --- |")
    kind_meaning = {
        "CAST_EXECUTION": "发动本身的代价/数值/结果被同伴改变（大纲族 A：输出→输入）",
        "EVENT_MULTIPLICATION": "事件计数超出两侧独发之和（族 B）",
        "EVENT_CONVERSION": "出现两侧独发都没有的新事件类型（族 C）",
        "ORDER_SENSITIVE": "A→B 与 B→A 结果不同（族 D：结算顺序；不视为 bug）",
        "RESOURCE_FEEDBACK": "偏离发生在资源通道上（族 E：法力/格挡/碎片/出手）",
        "OUTPUT_ARITHMETIC": "结算层数值耦合（族 F：乘性/上限/下限；本引擎里 攻击力=当前法力、攻击次数=当前速度）",
    }
    for t, c in sorted(types.items(), key=lambda kv: -kv[1]):
        A(f"| {t} | {c} | {kind_meaning.get(t,'')} |")
    A("")
    A("按**首个命中层级**分层（每个对只归一类，用于区分「结算层数值耦合」与「结构层交互」）：")
    A("")
    A("| 层级 | 对数 | 判据 |")
    A("| --- | ---: | --- |")
    for k, meaning in (("EVENT_LEVEL", "出现新事件类型或事件计数超可加"),
                       ("CAST_EXECUTION", "某次发动的自身增量被同伴改变（代价/数值/结果）"),
                       ("ORDER_ONLY", "两序结果不同且无上述两类"),
                       ("SCALAR_COUPLING", "只有结算层数值耦合（无事件、无发动期差异、两序一致）")):
        A(f"| {k} | {kinds.get(k,0)} | {meaning} |")
    A("")
    A(f"> `SCALAR_COUPLING` 之所以仍计入分类 2：本引擎的攻击力=当前法力、攻击次数=当前速度是"
      f"**乘法关系**，两次发动对同一池的加减在结算上不可加（大纲族 F）。这不是「另一个数值修正符」"
      f"——后者指两次互不相干的独立加成。")
    A("")
    A("命中的探针分布（一个对可命中多个）：")
    A("")
    A("| 探针 | 命中对数 |")
    A("| --- | ---: |")
    for p, c in sorted(probes_used.items(), key=lambda kv: -kv[1]):
        A(f"| `{p}` | {c} |")
    A("")
    A("## 4. 度数分布（Cat2 图）")
    A("")
    A(f"* 平均度 **{mean_deg:.2f}**｜中位度 **{median}**｜最大度 **{nonzero[0][0] if nonzero else 0}**")
    A(f"* 零 Cat2 伙伴的道纹 **{len(zero)}** 个：{('、'.join(zero)) if zero else '（无）'}")
    A("")
    A("| 道纹 | Cat2 伙伴数 | 副本归属 |")
    A("| --- | ---: | --- |")
    for d, x in nonzero[:25]:
        A(f"| {x} | {d} | {region_of(x)} |")
    A("")
    A("## 5. 发动顺序敏感的对")
    A("")
    A(f"共 **{len(order_pairs)}** 对在 A→B 与 B→A 下结果不同（**不判定为 bug**，"
      "只记录分歧发生在哪个探针）；其中执行受限的对已剔除。")
    A("")
    if order_pairs:
        A("| A | B | 分歧探针 | 类型 |")
        A("| --- | --- | --- | --- |")
        for r in order_pairs[:40]:
            A(f"| {r['daowen_a']} | {r['daowen_b']} | `{r['order_detail']}` | "
              f"{'|'.join(r['interaction_types'])} |")
        if len(order_pairs) > 40:
            A(f"| … | 其余 {len(order_pairs)-40} 对见 CSV | | |")
    A("")
    A("## 6. 执行受限的对（UNVERIFIED，不计入结论）")
    A("")
    A("原因只有两类：某次发动被生产引擎拒绝，或发动后留下待 DM 裁定的中断"
      "（`api.py:1084` 门禁会挡住同场景后续动作）。这类对**不做猜测**，只登记原因。")
    A("")
    def _norm(reason: str) -> str:
        """把逐对不同的错误串归并成可读类别（不改变原因本身，只做归类）。"""
        if "尸爆留下待裁定中断" in reason:
            return "尸爆（自毁型，`self_destruct: true`）发动后[命零]触发死之传承中断，同场景后续发动被门禁挡住"
        if "target_ref不是当前合法实体" in reason:
            return "后手发动时目标已不合法（先手改变/移除了目标集合）"
        if "波及必须为1个目标显式提交dodge_targets" in reason:
            return "波及的显式目标数在提交瞬间与合法目标数不一致"
        if "法力不足" in reason:
            return "法力预算不足（连发两次超出池预算）"
        if "有待处理的中断等待DM裁定" in reason:
            return "先手留下待裁定中断，后手被门禁挡住"
        return reason
    if unver:
        reasons = {}
        for r in unver:
            for k, v in (r.get("scene_problems") or {}).items():
                key = _norm(v)
                reasons[key] = reasons.get(key, 0) + 1
        A("| 原因 | 对数 |")
        A("| --- | ---: |")
        for v, c in sorted(reasons.items(), key=lambda kv: -kv[1])[:12]:
            A(f"| {v} | {c} |")
        A("")
        A("| A | B | 涉及场景 |")
        A("| --- | --- | --- |")
        for r in unver[:30]:
            cats = sorted({_norm(v) for v in (r.get("scene_problems") or {}).values()})
            A(f"| {r['daowen_a']} | {r['daowen_b']} | {'；'.join(cats)} |")
        if len(unver) > 30:
            A(f"| … | 其余 {len(unver)-30} 对见 CSV | |")
    else:
        A("（无）")
    A("")
    A("## 7. 代表样本（含事件序列）")
    A("")
    for item in representative(rows, names, targets, base, n=8):
        r = item["row"]
        A(f"### {r['daowen_a']}（发动侧 {item['cast_a']}）+ {r['daowen_b']}"
          f"（发动侧 {item['cast_b']}）｜分类 {r['classification']}"
          f"{'' if r.get('verifiable', True) else '｜UNVERIFIED'}")
        A("")
        A("因果串：")
        A("")
        A("```")
        A(r["causal"] or "（无偏离，判为 0/1 型）")
        A("```")
        A("")
        A("事件序列对照（命中通道）：")
        A("")
        A("```")
        for d in item["diagrams"]:
            for line in d:
                A(line)
            A("")
        A("```")
        A("")
    A("## 8. 校验")
    A("")
    A(f"* 对数 = C({n},2) = {n*(n-1)//2}，实际生成 {total} 对，无重复（见第 10 节校验输出）。")
    A("* 每个道纹都出现在矩阵中（`出现次数 = N-1`）。")
    A("* 分类 2 的对全部带可执行证据 ID（`A+B@探针`），可用 `--pair A,B` 单对重放。")
    A(f"* 执行受限（发不出手/留下待裁定中断）的对 **{len(unver)}** 个，全部标 "
      "`confidence=UNVERIFIED`，未计入任何结论数字（清单见第 6 节）。")
    A("")
    A("## 9. 与文档的交叉核对")
    A("")
    A(doc_drift_note(names, vocab))
    A("")
    A("## 10. 结论（可测量事实）")
    A("")
    A(f"* {n} 个道纹产出 {total} 组无序对。")
    A(f"* 观测到分类 2（非平凡协同）**{len(c2)}** 对（可验证），密度 **{pct(len(c2))}**；"
      f"另有 **{len(unver)}** 对因执行受限标 UNVERIFIED，未计入。")
    A(f"* 分类 1（加和型）**{len(c1)}** 对；分类 0（独立型）**{len(c0)}** 对。")
    A(f"* 有 **{len(zero)}** 个道纹没有任何分类 2 伙伴。")
    A(f"* 最大交互枢纽 **{nonzero[0][1] if nonzero else '—'}**，"
      f"分类 2 伙伴 **{nonzero[0][0] if nonzero else 0}** 个。")
    A(f"* 分类 2 的图中：平均度 {mean_deg:.2f}，中位度 {median}，"
      f"度>0 的节点 {len(nonzero)}/{n}。")
    A(f"* 顺序敏感对 {len(order_pairs)} 对（A→B 与 B→A 结果不同的可验证 Cat2）。")
    A(f"* 分类 2 的层级拆分：事件级 **{kinds.get('EVENT_LEVEL',0)}** 对、"
      f"发动期被改变 **{kinds.get('CAST_EXECUTION',0)}** 对、"
      f"仅顺序不同 **{kinds.get('ORDER_ONLY',0)}** 对、"
      f"纯结算层数值耦合 **{kinds.get('SCALAR_COUPLING',0)}** 对。")
    A("")
    A("### 解释（假设，不是结论）")
    A("")
    A("> 以下条目是**从数据出发的假设**，用于后续验证，不构成本次测量的事实。")
    A("")
    for h in hypotheses(names, rows, deg):
        A(f"* {h}")
    A("")
    (REPORTS / "daowen_pairwise_synergy.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def _wpad(text: str, width: int) -> str:
    """按终端显示宽度补空格（CJK 记 2 列），让事件序列图对齐。"""
    w = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)
    return text + " " * max(0, width - w)


def event_diagram(a, b, base, sa, sb, pair, finding, limit: int = 4) -> list:
    """把「基线 / A独发 / B独发 / A+B / B+A」在命中通道上的事件序列画成文本对照。"""
    probe, obs = finding["probe"], finding["obs"]
    def ev(sc):
        if probe not in sc["probes"] and probe != CAST_CHANNEL:
            return 0
        if obs.startswith("event:"):
            return [x for x in _events_of(sc, probe) if x.startswith(obs[6:])]
        return _delta_of(sc, probe).get(obs, 0)
    rows = [("基线", ev(base)), (f"{a}独发", ev(sa)), (f"{b}独发", ev(sb)),
            (f"{a}→{b}", ev(pair["ab"])), (f"{b}→{a}", ev(pair["ba"]))]
    width = max(sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
                    for ch in lab) for lab, _ in rows)
    out = []
    for label, val in rows:
        if isinstance(val, list):
            shown = " → ".join(val[:limit]) + (" → …" if len(val) > limit else "")
            out.append(f"  {_wpad(label, width)} | {probe}/{obs} : {shown or '（无）'}")
        else:
            out.append(f"  {_wpad(label, width)} | {probe}/{obs} : {val}")
    return out


def representative(rows, names, targets, base, n=8):
    """挑代表样本：可验证 Cat2（含顺序敏感 / 事件类优先）、Cat1、Cat0 各取一些。"""
    c2 = [r for r in rows if r["classification"] == 2 and r.get("verifiable", True)]
    c2.sort(key=lambda r: (not r["order_sensitive"], r["n_findings"]), reverse=False)
    c2 = ([r for r in c2 if any(f["probe"] != CAST_CHANNEL for f in r["findings"])]
          or c2)
    c1 = [r for r in rows if r["classification"] == 1]
    c0 = [r for r in rows if r["classification"] == 0 and r.get("verifiable", True)]
    picked, seen = [], set()
    pools = ((c2, max(1, n - 2)), (c1, 1), (c0, 1))
    for pool, k in pools:
        taken = 0
        for r in pool:
            key = (r["daowen_a"], r["daowen_b"])
            if key in seen:
                continue
            seen.add(key)
            picked.append(r)
            taken += 1
            if taken >= k:
                break
    out = []
    for r in picked[:n]:
        a, b = r["daowen_a"], r["daowen_b"]
        sa = get_solo(a, targets[a]["target"])
        sb = get_solo(b, targets[b]["target"])
        pair = get_pair(a, targets[a]["target"], b, targets[b]["target"])
        diagrams = [event_diagram(a, b, base, sa, sb, pair, f) for f in r["findings"][:2]]
        if not diagrams:
            # 0/1 型没有 finding：固定用普攻探针画一张对照图（若该探针可用）
            if base["probes"]["player_attacks"].get("ok", True):
                diagrams = [event_diagram(a, b, base, sa, sb, pair,
                                          {"probe": "player_attacks", "obs": "e_hp"})]
            else:
                diagrams = [["  （无 finding，且普攻探针不可用）"]]
        out.append({"row": r, "diagrams": diagrams,
                    "cast_a": targets[a]["target"], "cast_b": targets[b]["target"]})
    return out


def engine_fingerprint() -> str:
    """被分析引擎源码的指纹：排序后拼接 engine/**/*.py 内容取 sha256。"""
    import hashlib
    h = hashlib.sha256()
    for f in sorted((ROOT / "engine").rglob("*.py")):
        h.update(f.relative_to(ROOT).as_posix().encode("utf-8"))
        h.update(f.read_bytes())
    return h.hexdigest()


def doc_drift_note(names, vocab):
    """与 全道纹索引.md 交叉核对（只报告差异，不改文档）。"""
    idx = ROOT / "全道纹索引.md"
    if not idx.exists():
        return "（未找到 `全道纹索引.md`，跳过）"
    text = idx.read_text(encoding="utf-8")
    missing = [n for n in names if f"### {n}" not in text and f"| {n} |" not in text]
    extra = []
    for line in text.splitlines():
        if line.startswith("### "):
            nm = line[4:].strip()
            if nm and nm not in names:
                extra.append(nm)
    summary_mismatch = []
    for n, v in vocab.items():
        calc = v["calc_at_x3"]
        s = v["summary_at_x3"] or ""
        for key in ("cost", "target_heal", "target_damage", "damage_reduction"):
            if key in calc and isinstance(calc[key], int):
                if str(calc[key]) not in s:
                    summary_mismatch.append(f"{n}.{key}={calc[key]} vs summary「{s}」")
                    break
    out = []
    out.append(f"* 生产注册表 {len(names)} 个道纹中，索引文档未列出的：" +
               (("、".join(missing)) if missing else "无"))
    out.append(f"* 索引文档列出但生产注册表没有的：" + (("、".join(extra)) if extra else "无"))
    out.append(f"* `summary` 文案与实际返回字段不一致的道纹 **{len(summary_mismatch)}** 个"
               f"（文案只是提示串，结算读的是字段值）：")
    for m in summary_mismatch[:12]:
        out.append(f"  * `{m}`")
    return "\n".join(out)


def hypotheses(names, rows, deg):
    c2 = [r for r in rows if r["classification"] == 2]
    zero = [x for x in names if deg[x] == 0]
    cross = [r for r in c2 if not co_obtainable(r["daowen_a"], r["daowen_b"])]
    h = []
    h.append(f"H1：「组合稀少」可能是**采样假象**——本次无差别穷举在 "
             f"{len(rows)} 对中观测到 {len(c2)} 对非平凡交互；"
             f"人类/报告样本通常只看少数几对，若未覆盖高连接节点（如枢纽道纹的邻域）会低估总量。")
    h.append(f"H2：交互高度**不均衡**——度中位数 {sorted(deg.values())[len(names)//2]}、"
             f"最大度 {max(deg.values())}，说明协同更可能集中在少数「机制枢纽」上，"
             f"而不是均匀铺开；可在这些枢纽上做定向设计或定向验证。")
    h.append(f"H3：{len(zero)} 个道纹零 Cat2 伙伴，可能是**机制上确实孤立**"
             f"（只读写自己的私有状态），也可能是**探针未覆盖其触发条件**"
             f"（本分析的探针是有限集合，不是全部可达状态）。二者需分别用"
             f"「单道纹可达状态」分析区分。")
    h.append(f"H4：{len(cross)} 对分类 2 的双方分属互斥副本，单局无法同时持有——"
             f"若把「可达性」也算进「有多少组合」，实际密度会低于本表；"
             f"反之若允许跨副本（朋友/员工/怪物侧），本表就是可达的。")
    return h


# ===========================================================================
# 9. CLI
# ===========================================================================

def _head() -> str:
    import subprocess
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                                       text=True).strip()
    except Exception:
        return "UNKNOWN"


def replay_verify(names, targets, base, n_each: int = 20) -> dict:
    """独立重放校验：从干净进程/新沙盒重跑样本对，比对分类是否复现。"""
    rows = _r(CACHE / "rows.json")
    c2 = [r for r in rows if r["classification"] == 2 and r.get("verifiable", True)]
    c0 = [r for r in rows if r["classification"] == 0]
    picked = ([("Cat2", r) for r in c2[:n_each]] + [("Cat0", r) for r in c0[:n_each]])
    out = {"checked": 0, "matched": 0, "mismatch": [], "cat2_checked": 0, "cat0_checked": 0}
    for kind, r in picked:
        a, b = r["daowen_a"], r["daowen_b"]
        sa = get_solo(a, targets[a]["target"], force=True)
        sb = get_solo(b, targets[b]["target"], force=True)
        pr = get_pair(a, targets[a]["target"], b, targets[b]["target"], force=True)
        rr = classify_pair(a, b, base, sa, sb, pr)
        out["checked"] += 1
        out["cat2_checked" if kind == "Cat2" else "cat0_checked"] += 1
        if rr["classification"] == r["classification"]:
            out["matched"] += 1
        else:
            out["mismatch"].append({"pair": [a, b], "was": r["classification"],
                                    "now": rr["classification"], "kind": kind})
    _w(CACHE / "replay.json", out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="全量扫描（默认）")
    ap.add_argument("--vocab", action="store_true")
    ap.add_argument("--pair", default="", help="单对重放：--pair A,B")
    ap.add_argument("--report", action="store_true", help="只用缓存重算报告")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--replay", type=int, default=0,
                    help="重放校验数量（每类），如 --replay 20")
    args = ap.parse_args()

    from engine.daowen import DaoWenEngine as D
    D.register_all()
    names = D.list_all()
    vocab = vocabulary()
    _w(CACHE / "vocabulary.json", vocab)

    if args.vocab:
        print(f"{len(names)} 道纹，C(N,2)={len(names)*(len(names)-1)//2}")
        for n in names:
            print(f"  {n:4s} [{('T' if vocab[n]['requires_target'] else '-')}] "
                  f"{vocab[n]['formula'][:70]}")
        return

    base = get_baseline(force=args.force)
    targets = resolve_targets(names, base, force=args.force)

    if args.pair:
        a, b = [x.strip() for x in args.pair.split(",")]
        sa = get_solo(a, targets[a]["target"], force=True)
        sb = get_solo(b, targets[b]["target"], force=True)
        pair = get_pair(a, targets[a]["target"], b, targets[b]["target"], force=True)
        r = classify_pair(a, b, base, sa, sb, pair)
        r["daowen_a"], r["daowen_b"] = a, b
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return

    t0 = time.time()
    if not args.report:
        print(f"扫描 {len(names)} 道纹 / {len(names)*(len(names)-1)//2} 对 …", flush=True)
        sweep(names, targets, base, verbose=True, force=args.force)
    elapsed = time.time() - t0
    if not args.report:
        _w(CACHE / "meta.json", {"elapsed": elapsed})
    else:
        meta = CACHE / "meta.json"
        elapsed = _r(meta)["elapsed"] if meta.exists() else 0.0   # 报告重算沿用最近一次扫描耗时
    bad_cache = cache_consistency(names, targets)
    print(f"缓存自检：{'通过' if not bad_cache else '异常 ' + '；'.join(bad_cache[:5])}", flush=True)
    if args.report:
        print("从场景缓存重建分类 …", flush=True)
    rows = build_rows(names, targets, base)

    write_outputs(names, targets, vocab, rows,
                  {"head": _head(), "base": base})
    write_report_md(names, targets, vocab, rows, _head(), elapsed,
                    "python sim/daowen_pairwise_analysis.py --all", base)

    deg = most_connected(rows, names, verifiable_only=True)
    c2 = [r for r in rows if r["classification"] == 2 and r.get("verifiable", True)]
    c1 = [r for r in rows if r["classification"] == 1]
    c0 = [r for r in rows if r["classification"] == 0]
    print(f"\nHEAD={_head()}")
    print(f"N={len(names)}  pairs={len(rows)}  (C(N,2)={len(names)*(len(names)-1)//2})")
    print(f"Cat0={len(c0)}  Cat1={len(c1)}  Cat2={len(c2)}  ({100*len(c2)/len(rows):.1f}%)")
    print(f"零 Cat2 伙伴：{len([x for x in names if deg[x]==0])}  "
          f"最大度：{max(deg.values())} ({max(deg, key=deg.get)})")
    print(f"报告：reports/daowen_pairwise_synergy.md / .csv / daowen_synergy_graph.json")
    if args.replay:
        rv = replay_verify(names, targets, base, args.replay)
        print(f"重放校验：{rv['matched']}/{rv['checked']} 分类一致"
              f"（Cat2 {rv['cat2_checked']}，Cat0 {rv['cat0_checked']}）")
        for m in rv["mismatch"][:5]:
            print(f"  MISMATCH {m['pair']} {m['was']}→{m['now']}")


if __name__ == "__main__":
    main()
