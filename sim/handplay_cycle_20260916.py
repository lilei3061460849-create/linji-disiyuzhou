#!/usr/bin/env python3
"""手操轮回会话驱动器（2026-09-16）。

沿用 sim/handplay_cycle_20260915.py 的约定：本脚本只做三件事——**执行、记录、存档**，
绝不替 AI 做任何决策。每一回合做什么、打谁、发不发动道纹，全部写在 `policy()` 里，
那是 AI（本轮的操作者）的决定；驱动器只负责把决定提交给引擎、把引擎真实返回原样
追加进 trace.jsonl，并在每次调用后存档。

与 0915 版的区别：0915 版按命令行传入的 steps 列表逐步执行（适合精调单场）；
本版增加 `__cycle__` 指令，按 `policy()` 连续跑完一整个轮回（第 1～7 场 + 封存/死斗），
用于验证「一整轮能不能被真实跑通」，仍然步步落 trace。

用法：
    .venv/bin/python sim/handplay_cycle_20260916.py --steps '[{"action":"__state__"}]'
    .venv/bin/python sim/handplay_cycle_20260916.py --steps '[{"action":"__cycle__"}]'
    .venv/bin/python sim/handplay_cycle_20260916.py --steps '[{"action":"__cycle__","params":{"max_battles":2}}]'
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.api import GameEngine  # noqa: E402

SESSION_DIR = os.path.join(ROOT, "data", "handplay_20260916")
TRACE_PATH = os.path.join(SESSION_DIR, "trace.jsonl")
SLOT = "cycle"
INVOCATION = ""


def _engine() -> GameEngine:
    os.makedirs(SESSION_DIR, exist_ok=True)
    book = os.path.join(SESSION_DIR, "死者之书.md")
    if not os.path.exists(book):
        shutil.copyfile(os.path.join(ROOT, "死者之书.md"), book)
    return GameEngine(
        db_path=os.path.join(SESSION_DIR, "rulings.db"),
        save_dir=SESSION_DIR,
        rng_seed=20260916,
        death_book_path=book,
    )


# ========================================================================
# 记录：每次引擎调用都原样落 trace（报告的唯一事实源）
# ========================================================================

def _next_seq() -> int:
    if not os.path.exists(TRACE_PATH):
        return 1
    with open(TRACE_PATH, encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip()) + 1


def _record(action: str, params: dict, result: dict, engine: GameEngine,
            retry: bool = False) -> None:
    entry = {
        "seq": _next_seq(),
        "invocation": INVOCATION,
        "action": action,
        "params": params,
        "result": result,
        "retry": retry,
        "state": {
            "phase": engine.state.phase,
            "combat_subphase": getattr(engine.state, "combat_subphase", ""),
            "battle": engine.state.current_battle,
            "round": engine.state.current_round,
            "shards": engine.state.shards,
            "energy": engine.state.energy,
            "player_hp": engine.state.player.current_hp if engine.state.player else None,
            "player_blood_limit": engine.state.player.blood_limit if engine.state.player else None,
            "player_mana": (f"{engine.state.player.current_mana}/{engine.state.player.mana_limit}"
                            if engine.state.player else None),
            "player_speed": (f"{engine.state.player.current_speed}/{engine.state.player.speed_limit}"
                             if engine.state.player else None),
            "daowen": {k: v.x_value for k, v in
                       (engine.state.player.dao_wen.items() if engine.state.player else [])},
            "enemies": [{"name": m.name, "hp": m.current_hp, "alive": m.is_alive}
                        for m in engine.state.enemies],
        },
    }
    with open(TRACE_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _run(engine: GameEngine, action: str, params: dict, quiet: bool = False) -> dict:
    """执行一次引擎调用并落 trace + 存档。"""
    result = engine.execute_action(action, params)
    if not quiet:
        ok = "✓" if result.get("success") else "✗"
        detail = result.get("error") or result.get("action") or ""
        print(f"      {ok} {action} {json.dumps(params, ensure_ascii=False)[:110]}"
              f" → {str(detail)[:110]}")
    _record(action, params, result, engine)
    engine.save_game(SLOT)
    return result


# ========================================================================
# AI 的决策（本轮操作者的战术意图，驱动器不改动这些内容）
# ========================================================================

OPENING = {
    "name": "林寂",
    # 25 点属性点：血13/速6/法6 → 血限78、速限3、法限3
    # 速法按正文 2点=1限，血按 1点=6血限。首场怪物普遍 200+ 血、每回合 8 点上下的
    # 输出，血限决定能撑几轮，法力够发动一次【再生3】自持。
    "blood_points": 13, "speed_points": 6, "mana_points": 6,
    "relic": "承露盏",        # 开局发现：承露盏按失去生命返法力，配合透支/再生
    "initial_daowen": "再生",  # 开局候选 庇护/再生/透支 → 取再生（自持优先）
    "resonance": "转换",
    "region": "扭曲都市",
}


def policy_player_action(engine: GameEngine, action_index: int,
                         last_action: bool = False) -> dict:
    """轮回者一次主动出手的决策。返回 {"kind": ..., ...}。

    战术（"蓝即拳"是这套战斗的全部张力所在）：
      1. **最后一手必须打伤害**——【凡庸】是"连续 5 回合未使敌对角色掉血"即命零，
         把两次出手都拿去治疗等于自杀。
      2. **治疗必须留得住拳**：[攻击力] = 当前法力，法力花光则普攻 0 伤。
         所以只在法力 ≥ 2 时花 1 点治疗，绝不把池子清空。
      3. 有【杀伐】且法力够 → 打 X² 伤害；否则普攻最弱敌人。
    """
    p = engine.state.player
    alive = [m for m in engine.state.enemies if m.is_alive]
    if not p or not alive:
        return {"kind": "none"}
    target = min(alive, key=lambda m: m.current_hp)
    hp_ratio = p.current_hp / max(1, p.blood_limit)

    # 正文：仅轮回者 [攻击力] = 当前法力、"蓝即拳"。把法力花光 = 普攻归零 = 凡庸。
    # 所以治疗只在"留得住拳"的前提下做：法力至少留 1 点，否则宁可硬扛。
    if not last_action:
        if hp_ratio < 0.35 and "再生" in p.dao_wen and p.current_mana >= 2:
            return {"kind": "daowen", "name": "再生", "x": 1, "target": "self"}
    # 伤害手：杀伐 X² 优先，否则普攻
    if "杀伐" in p.dao_wen and p.current_mana >= 2:
        return {"kind": "daowen", "name": "杀伐", "x": min(2, p.current_mana),
                "target": target.name}
    return {"kind": "attack", "target": target.name}


def policy_pre_battle(engine: GameEngine) -> dict | None:
    """局外行动决策。返回 None 表示不做了、直接开下一场。"""
    st = engine.state
    if st.energy <= 0:
        return None
    p = st.player
    if p and p.current_hp < p.blood_limit * 0.5:
        return {"sub_action": "休整", "sub": 2}     # 40% 档，10 碎片
    return {"sub_action": "探索", "sub": 1}          # 其余精力拿事件与碎片


def policy_event_choice(engine: GameEngine, options: list[dict]) -> dict:
    """事件选项决策：优先选真拒绝（不吃代价，还能拿拒绝奖励的残韵）。"""
    from engine.api import GameEngine as _GE
    for opt in options:
        text = opt.get("option", "")
        if _GE._is_reject_option_text(text):
            return dict(opt.get("params_schema") or {})
    return dict((options[0].get("params_schema") or {})) if options else {}


def _drain_events(engine: GameEngine) -> None:
    """把当前待结算的事件处理掉（可能连开多个）。"""
    for _ in range(6):
        avail = engine.get_available_actions()
        if avail.get("phase") != "事件待结算":
            return
        options = avail.get("actions") or []
        if not options:
            return
        params = policy_event_choice(engine, options)
        if not params:
            return
        _run(engine, "resolve_event", params)


def _drain_interrupts(engine: GameEngine) -> None:
    """处理待裁定的中断。

    【死之传承】由操作者（DM）审核遗言：这里按草稿通过（approve）。
    遗言写入《死者之书》后本轮轮回结束。
    """
    for _ in range(4):
        pend = getattr(engine, "_pending_interrupts", None) or []
        if not pend:
            return
        top = pend[0]
        raw = getattr(top, "interrupt_type", None)
        itype = getattr(raw, "value", None) or str(raw)
        if itype == "死之传承":
            draft = (getattr(top, "context", None) or {}).get("draft") or {}
            text = draft.get("text") or "无话可说"
            # submit_ruling 是引擎方法，不在 action_type 派发表里；
            # 走 execute_action 会被"有待处理的中断"门禁挡回，必须直接调用。
            result = engine.submit_ruling("死之传承", text,
                                          {"action": "approve", "text": text})
            print(f"      {'✓' if result.get('success') else '✗'} submit_ruling"
                  f" 死之传承 → {text} {result.get('error') or ''}")
            _record("submit_ruling",
                    {"interrupt_type": "死之传承", "ruling_text": text,
                     "ruling_data": {"action": "approve", "text": text}},
                    result, engine)
            engine.save_game(SLOT)
            continue
        return


def _drain_discoveries(engine: GameEngine) -> None:
    """处理必须选掉的「发现待选」（遗物/消耗品），否则任何行动都会被拒。"""
    for _ in range(6):
        avail = engine.get_available_actions()
        phase = avail.get("phase") or ""
        acts = avail.get("actions") or []
        if not acts or "待选" not in phase:
            return
        first = acts[0]
        if "item_name" in first:
            _run(engine, "choose_discovered_item", {"item_name": first["item_name"]})
        elif "relic_name" in first:
            _run(engine, "choose_discovered_relic", {"relic_name": first["relic_name"]})
        else:
            return


# ========================================================================
# 执行器
# ========================================================================

def _do_player_action(engine: GameEngine, decision: dict) -> bool:
    """执行一次主动出手；返回是否真的消耗了出手（失败会让上层改走下一步）。"""
    if decision.get("kind") == "none":
        return False
    if decision.get("kind") == "daowen":
        name = decision["name"]
        p = engine.state.player
        x = int(decision.get("x", 1))
        x = max(1, min(x, max(0, p.current_mana)))
        params = {"actor_ref": "player:0", "daowen_name": name, "x": x,
                  "dodge": False, "blood_shadow": False, "trigger_spell_choices": {}}
        if decision.get("target") == "self":
            params["target_ref"] = "player:0"   # 自身也要显式提交，否则引擎报缺少[目标]
        else:
            idx = next((i for i, m in enumerate(engine.state.enemies)
                        if m.name == decision.get("target")), None)
            if idx is not None:
                params["target_ref"] = f"enemy:{idx}"
        return _run(engine, "use_daowen", params).get("success", False)
    # 普攻：prepare → resolve 两阶段，逐击目标由 AI 指定
    actor = "player:0"
    prep = _run(engine, "prepare_attack", {"actor_ref": actor})
    if not prep.get("success"):
        return False
    res = prep["result"]
    token = res["token"]
    want = decision.get("target")
    hits = []
    for i in range(res.get("hit_count", 0)):
        opts = res.get("target_options") or []
        pick = next((o for o in opts if o.get("name") == want), None) or (opts[0] if opts else None)
        if pick is None:
            break
        hits.append({"target_ref": pick["ref"], "dodge": False, "blood_shadow": False,
                     "spell_choices": ({
                         tw: {sp["spell_name"]: {"use": False}
                              for sp in (pick.get("spell_options") or {}).get(tw) or []}
                         for tw in ("before", "after")} if pick.get("spell_options")
                         else {"before": {}, "after": {}})})
    if not hits:
        return False
    return _run(engine, "resolve_attack", {"token": token, "hits": hits}).get("success", False)


def _do_monster_phase(engine: GameEngine) -> None:
    """怪物阶段两阶段提交：AI 指定每只怪的道纹与目标，驱动器只连 token。"""
    prep = _run(engine, "prepare_monster_phase", {}, quiet=True)
    if not prep.get("success"):
        return
    res = prep["result"]
    token = res["token"]
    choices = []
    for actor in res.get("actors") or []:
        ref = actor["actor_ref"]
        entry: dict = {"actor_ref": ref}
        opts = actor.get("daowen_options") or []
        if opts:
            opt = opts[0]
            dw: dict = {"name": opt["name"], "dodge": False, "blood_shadow": False}
            if opt.get("requires_target"):
                hostile = [t for t in (opt.get("target_options") or [])
                           if not t["ref"].startswith("enemy")]
                pool = hostile or (opt.get("target_options") or [])
                if pool:
                    dw["target_ref"] = pool[0]["ref"]
            if opt.get("trigger_spell_options"):
                dw["trigger_spell_choices"] = {
                    h: {sp["spell_name"]: {"use": False} for sp in spells}
                    for h, spells in opt["trigger_spell_options"].items()}
            entry["daowen"] = dw
        else:
            entry["daowen"] = None
        # 攻击提交结构：attack_actions 是长度＝base_attack_actions 的列表，
        # 每项再含一个 hits 列表（每次攻击 base_hits_per_attack 击）。
        targets = actor.get("attack_target_options") or []
        n_actions = int(actor.get("base_attack_actions", 0))
        if targets and n_actions > 0:
            hostile = [t for t in targets if not t["ref"].startswith("enemy")]
            pick = (hostile or targets)[0]
            hit: dict = {"target_ref": pick["ref"], "dodge": False, "blood_shadow": False}
            if pick.get("spell_options"):
                hit["spell_choices"] = {
                    tw: {sp["spell_name"]: {"use": False}
                         for sp in (pick["spell_options"].get(tw) or [])}
                    for tw in ("before", "after")}
            one_action = {"hits": [hit] * max(1, int(actor.get("base_hits_per_attack", 1)))}
            entry["attack_actions"] = [one_action] * n_actions
        else:
            entry["attack_actions"] = []
        choices.append(entry)
    _run(engine, "resolve_monster_phase", {"token": token, "choices": choices})


def _play_battle(engine: GameEngine, max_rounds: int = 30) -> None:
    st = engine.state
    for _ in range(max_rounds):
        if not (st.player and st.player.is_alive):
            break
        if not [m for m in st.enemies if m.is_alive]:
            break
        _run(engine, "round_start", {"relic_choices": {}})
        p = st.player
        # 出手预算：用完为止（正文：轮回者固定 2 次）。
        # 被拒的行动不扣出手，故带尝试上限：同一轮最多试 4 次，随后改走下一步，
        # 避免"决策永远不合法"时死循环。
        tries = 0
        while (p and p.is_alive and p.actions_used_this_round < p.action_count
               and [m for m in st.enemies if m.is_alive] and tries < 4):
            tries += 1
            remaining = p.action_count - p.actions_used_this_round
            decision = policy_player_action(engine, p.actions_used_this_round,
                                            last_action=(remaining <= 1))
            if not _do_player_action(engine, decision) and decision.get("kind") != "attack":
                decision = {"kind": "attack",
                            "target": min((m for m in st.enemies if m.is_alive),
                                          key=lambda m: m.current_hp).name}
                _do_player_action(engine, decision)
        if p and p.is_alive:
            _run(engine, "resolve_ally_phases", {}, quiet=True)
        if [m for m in st.enemies if m.is_alive]:
            _do_monster_phase(engine)
        if st.player and st.player.is_alive:
            _run(engine, "round_end", {}, quiet=True)


def _do_opening(engine: GameEngine) -> None:
    _run(engine, "setup_attributes",
         {"name": OPENING["name"], "blood_points": OPENING["blood_points"],
          "speed_points": OPENING["speed_points"], "mana_points": OPENING["mana_points"]})
    _run(engine, "choose_discovered_relic", {"relic_name": OPENING["relic"]})
    _run(engine, "setup_choose_initial_daowen", {"daowen_name": OPENING["initial_daowen"]})
    _run(engine, "setup_choose_resonance", {"resonance_type": OPENING["resonance"]})
    _run(engine, "setup_choose_region", {"region": OPENING["region"]})


def run_cycle(engine: GameEngine, max_battles: int = 7) -> None:
    st = engine.state
    _do_opening(engine)
    for battle in range(1, max_battles + 1):
        print(f"\n───────── 第 {battle} 场 ─────────")
        if not (st.player and st.player.is_alive):
            print("  轮回者已命零，本轮到此为止。")
            break
        # 正文：精力耗尽后才能进入战斗。先把 3 点精力花在局外（含事件）。
        for _ in range(8):
            _drain_discoveries(engine)
            _drain_events(engine)
            if st.energy <= 0:
                break
            decision = policy_pre_battle(engine)
            if not decision:
                break
            _run(engine, "pre_battle_action", decision)
        _drain_discoveries(engine)
        _drain_events(engine)
        _run(engine, "battle_start", {})
        _play_battle(engine)
        if not (st.player and st.player.is_alive):
            # 命零：先过【死之传承】审核，再结算战终。
            _drain_interrupts(engine)
            _run(engine, "battle_end", {})
            print("  轮回者命零，本轮轮回到此结束。")
            break
        _run(engine, "battle_end", {})
        if not (st.player and st.player.is_alive):
            _drain_interrupts(engine)
            print("  轮回者命零。")
            break
    print("\n───────── 轮回结束 ─────────")
    print(json.dumps({
        "battle": st.current_battle,
        "shards": st.shards,
        "player_hp": st.player.current_hp if st.player else None,
        "alive": bool(st.player and st.player.is_alive),
        "daowen": {k: v.x_value for k, v in (st.player.dao_wen.items() if st.player else [])},
        "relics": [r.name for r in st.relics],
    }, ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="")
    ap.add_argument("--slot", default=SLOT)
    ap.add_argument("--new", action="store_true", help="删除存档与 trace，从头开一局")
    args = ap.parse_args()

    global INVOCATION
    INVOCATION = f"{int(time.time())}-{os.getpid()}"
    steps = json.loads(args.steps) if args.steps else []

    engine = _engine()
    if args.new:
        for path in (os.path.join(SESSION_DIR, f"save_{args.slot}.json"), TRACE_PATH):
            if os.path.exists(path):
                os.remove(path)
    if os.path.exists(os.path.join(SESSION_DIR, f"save_{args.slot}.json")):
        loaded = engine.load_game(args.slot)
        print(f"（读档 {args.slot}：{loaded.get('success')}）")
    else:
        print("（新开局）")

    for step in steps or [{"action": "__state__"}]:
        action = step.get("action", "")
        params = step.get("params", {})
        if action == "__cycle__":
            run_cycle(engine, int(params.get("max_battles", 7)))
        elif action == "__state__":
            st = engine.state
            print(json.dumps({
                "phase": st.phase, "combat_subphase": getattr(st, "combat_subphase", ""),
                "battle": st.current_battle, "round": st.current_round,
                "shards": st.shards, "energy": st.energy,
                "player_hp": st.player.current_hp if st.player else None,
                "daowen": {k: v.x_value for k, v in
                           (st.player.dao_wen.items() if st.player else [])},
                "enemies": [(m.name, m.current_hp) for m in st.enemies],
            }, ensure_ascii=False, indent=1))
        else:
            _run(engine, action, params)

    try:
        engine.save_game(args.slot)
    except Exception as exc:  # pragma: no cover
        print(f"（收尾存档失败：{exc}）")
    print(f"trace: {TRACE_PATH}")


if __name__ == "__main__":
    main()
