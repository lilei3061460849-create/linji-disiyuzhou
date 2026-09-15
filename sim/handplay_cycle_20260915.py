#!/usr/bin/env python3
"""手操轮回会话驱动器（2026-09-15，第 2 次轮回）。

用途：让 AI（DM/玩家）通过 GameEngine.execute_action 逐步点选手操一次完整轮回，
本脚本只做三件事——**执行、记录、存档**，绝不替 AI 做任何决策：

  1. 按命令行传入的 steps 列表（每一步都是 AI 写死的真实动作与参数）依次提交给引擎；
  2. 把每一步的**引擎真实返回**原样追加进 trace.jsonl（报告的唯一事实源）；
  3. 用引擎自带的 save_game/load_game 做会话断点续传（跨进程保持同一局状态）。

任何步骤失败都按引擎的原子契约处理：失败即整体回滚、零副作用，
驱动器只如实打印失败原因，交由 AI 修正后重交（重交本身也记入 trace，
并标注 retry=true，不计为游戏事件）。

用法：
    python3 sim/handplay_cycle_20260915.py --steps '[{"action":"...","params":{...}}]'
    python3 sim/handplay_cycle_20260915.py --steps-file /tmp/steps.json
    python3 sim/handplay_cycle_20260915.py --steps '[{"action":"__state__"}]'
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

SESSION_DIR = os.path.join(ROOT, "data", "handplay_20260915")
TRACE_PATH = os.path.join(SESSION_DIR, "trace.jsonl")
DISCARD_PATH = os.path.join(SESSION_DIR, "discarded_invocations.json")
SLOT = "cycle"
INVOCATION = ""
DISCARDED: list = []


def _engine() -> GameEngine:
    os.makedirs(SESSION_DIR, exist_ok=True)
    book = os.path.join(SESSION_DIR, "死者之书.md")
    if not os.path.exists(book):
        shutil.copyfile(os.path.join(ROOT, "死者之书.md"), book)
    return GameEngine(
        db_path=os.path.join(SESSION_DIR, "rulings.db"),
        save_dir=SESSION_DIR,
        rng_seed=915,
        sealed_candidate_path=os.path.join(ROOT, "data", "seals", "sealed_candidates.json"),
        death_book_path=book,
    )


def _ref(entity) -> str:
    return getattr(entity, "name", "?")


def _panel(e) -> str:
    dw = "、".join(f"{k}{v.x_value}" for k, v in e.dao_wen.items()) or "无"
    return (f"{e.name} 生命{e.current_hp}/{e.blood_limit} 法力{e.current_mana}/{e.mana_limit} "
            f"速度{e.current_speed}/{e.speed_limit} 攻{e.effective_attack_count()}×{e.effective_attack_power()} "
            f"格挡{e.shield} 出手{e.actions_used_this_round}/{e.action_count} 道纹[{dw}]"
            + (f" 状态[{_status(e)}]" if e.status_effects else ""))


def _status(e) -> str:
    out = []
    for st in e.status_effects:
        nm = getattr(st, "name", None) or getattr(st, "status_type", "?")
        out.append(f"{nm}{getattr(st, 'value', '')}({getattr(st, 'remaining_rounds', '')})")
    return "、".join(out)


def _enemy_lines(e) -> list[str]:
    out = []
    for i, m in enumerate(e.state.enemies):
        flag = "存活" if m.is_alive else "已命零"
        out.append(f"  敌方 enemy:{i} {_panel(m)} [{flag}]")
    for r in (e.state.monster_reinforcements or []):
        out.append(f"  待增援 {r.get('name')}")
    return out


def _state_block(e) -> list[str]:
    s = e.state
    lines = [f"【状态】阶段={s.phase}/{s.combat_subphase} 场次={s.current_battle} 回合={s.current_round} "
             f"精力={s.energy} 碎片={s.shards} 假碎片={s.fake_shards} 残韵={dict(s.resonance)} "
             f"异变={getattr(s.player, 'mutation', None)}"]
    p = s.player
    if p:
        lines.append(f"  我方 {_panel(p)} 池点数={s.attribute_points}")
        lines.append(f"  我方法术={[sp.name for sp in p.spells]}")
    lines.extend(_enemy_lines(e))
    if s.friends:
        lines.append("  朋友 " + "；".join(_panel(f) for f in s.friends))
    if s.employees:
        lines.append("  员工 " + "；".join(_panel(x) for x in s.employees))
    if s.relics:
        lines.append("  遗物 " + "、".join(r.name for r in s.relics))
    if s.consumables:
        lines.append("  消耗品 " + "、".join(f"{c.name}({c.current_uses}/{c.max_uses})" for c in s.consumables))
    return lines


def _next_seq() -> int:
    if not os.path.exists(TRACE_PATH):
        return 1
    n = 0
    with open(TRACE_PATH, encoding="utf-8") as fh:
        for _ in fh:
            n += 1
    return n + 1


def _record(action: str, params: dict, result: dict, engine: GameEngine, retry: bool = False) -> None:
    """把一次真实提交（成功或失败）原样追加进 trace.jsonl。"""
    record = {
        "seq": _next_seq(),
        "ts": time.time(),
        "invocation": INVOCATION,
        "action": action,
        "params": params,
        "success": bool(result.get("success")),
        "result": result,
        "battle": engine.state.current_battle,
        "round": engine.state.current_round,
        "retry": retry,
    }
    with open(TRACE_PATH, "a", encoding="utf-8") as fh:
        # default=str：引擎偶尔会在返回体里夹带不可序列化对象（如内部引擎句柄），
        # 记录层降级为字符串保住这笔真实流水，不改动引擎返回本身。
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _run_step(engine: GameEngine, action: str, params: dict) -> dict:
    """执行一个真实动作并打印渲染结果（供复合步复用）。"""
    result = engine.execute_action(action, params)
    try:
        lines = _render(action, result, engine)
    except Exception as exc:  # 渲染失败不得吞掉已结算的真实动作
        lines = [f"（渲染异常 {type(exc).__name__}: {exc}）",
                 "原始返回：" + json.dumps(result, ensure_ascii=False)[:3000]]
    print(f"    ▶ {action} {json.dumps(params, ensure_ascii=False)}")
    for line in lines:
        print("        " + line)
    _record(action, params, result, engine)
    engine.save_game(SLOT)
    return result


def _auto_attack(engine: GameEngine, params: dict) -> dict:
    """AI 已给逐击选择，驱动器只连接 prepare→resolve 的 token。"""
    actor = params.get("actor_ref", "player:0")
    prep = engine.execute_action("prepare_attack", {"actor_ref": actor})
    print(f"    ▶ prepare_attack actor_ref={actor}")
    for line in _render("prepare_attack", prep, engine):
        print("        " + line)
    _record("prepare_attack", {"actor_ref": actor}, prep, engine)
    engine.save_game(SLOT)
    if not prep.get("success"):
        return prep
    token = prep["result"]["token"]
    hits = [dict(h, spell_choices=h.get("spell_choices", {"before": {}, "after": {}}))
            for h in (params.get("hits") or [])]
    resolve = engine.execute_action("resolve_attack", {"token": token, "hits": hits})
    for line in _render("resolve_attack", resolve, engine):
        print("        " + line)
    _record("resolve_attack", {"token": token, "hits": hits}, resolve, engine)
    engine.save_game(SLOT)
    return resolve


def _build_monster_choices(prepared: dict, policy: dict) -> list[dict]:
    """把 AI 的怪物侧意图翻译成引擎要求的完整提交（候选合法性由引擎再次校验）。"""
    choices: list[dict] = []
    for actor in prepared.get("actors") or []:
        ref = actor["actor_ref"]
        spec = (policy or {}).get(ref) or {}
        want = spec.get("daowen", "__first__" if actor.get("daowen_required") else None)
        if want in ("__none__", "none"):
            want = None
        opts = actor.get("daowen_options") or []
        daowen = None
        if want is not None:
            if want == "__first__":
                want = opts[0]["name"] if opts else None
            opt = next((o for o in opts if o.get("name") == want), None)
            if opt is None:
                raise ValueError(f"{ref} 没有候选道纹【{want}】，候选={[o.get('name') for o in opts]}")
            daowen = {"name": opt["name"], "dodge": bool(spec.get("dodge", False)),
                      "blood_shadow": bool(spec.get("blood_shadow", False))}
            if opt.get("requires_target"):
                tgt = spec.get("target_ref")
                if tgt is None:
                    hostiles = [t["ref"] for t in opt.get("target_options") or []
                                if not t["ref"].startswith("enemy")]
                    tgt = (hostiles or [t["ref"] for t in opt.get("target_options") or []])[0]
                daowen["target_ref"] = tgt
            if opt.get("dodge_submission") == "per_target":
                daowen["dodge_targets"] = [
                    {"target_ref": t["ref"], "dodge": False, "blood_shadow": False}
                    for t in (opt.get("dodge_target_options") or [])]
            if opt.get("trigger_spell_options"):
                tw = spec.get("trigger_spells") or {}

                def _tdec(holder_name, sp, _tw=tw):
                    d = (_tw.get(holder_name) or {}).get(sp["spell_name"])
                    return d if isinstance(d, dict) else {"use": False}

                daowen["trigger_spell_choices"] = {
                    holder: {sp["spell_name"]: _tdec(holder, sp) for sp in spells}
                    for holder, spells in opt["trigger_spell_options"].items()}
        tgt_opts = actor.get("attack_target_options") or []
        if not tgt_opts:
            choices.append({"actor_ref": ref, "daowen": daowen, "attack_actions": []})
            continue
        tgt = spec.get("attack_target")
        if tgt is None:
            hostiles = [t["ref"] for t in tgt_opts if not t["ref"].startswith("enemy")]
            tgt = (hostiles or [t["ref"] for t in tgt_opts])[0]
        t_opt = next(t for t in tgt_opts if t["ref"] == tgt)
        hits_per = actor.get("base_hits_per_attack") or 1
        actions = actor.get("base_attack_actions") or 1
        spells = t_opt.get("spell_options") or {}
        # spec["spells"] = {"before": {"先发制人": {"use": True, "cycles": [[{"x":5,"target_ref":"enemy:0","dodge":False}]]}}}
        # 缺省一律不发动；数值与目标仍由引擎校验，驱动器不代填。
        wanted = spec.get("spells") or {}

        def _decision(kind, cand):
            d = (wanted.get(kind) or {}).get(cand["spell_name"])
            return d if isinstance(d, dict) else {"use": False}

        def _hit(_):
            # before/after 两个历史挂接点无论有无候选都必须提交（空字典=覆盖空候选）；
            # damage_after/life_before 无候选时可缺省（引擎契约）。
            sc = {k: {sp["spell_name"]: _decision(k, sp) for sp in (spells.get(k) or [])}
                  for k in ("before", "after")}
            for k in ("damage_after", "life_before"):
                if spells.get(k):
                    sc[k] = {sp["spell_name"]: _decision(k, sp) for sp in spells[k]}
            return {"target_ref": tgt, "dodge": bool(spec.get("dodge", False)),
                    "blood_shadow": bool(spec.get("blood_shadow", False)),
                    "spell_choices": sc}
        attack_actions = [{"hits": [_hit(i) for i in range(hits_per)]} for _ in range(actions)]
        choices.append({"actor_ref": ref, "daowen": daowen, "attack_actions": attack_actions})
    return choices


def _monster_phase(engine: GameEngine, policy: dict | None = None,
                  token: str | None = None, prep_only: bool = False) -> dict:
    """prepare→（按 AI 意图构建完整提交）→resolve 的怪物阶段管道。

    token 缺省时先做 prepare；显式传入 token 时直接复用引擎中待提交的
    prepared 快照（用于 AI 先看清候选再决定，避免重复 prepare 被拒）。
    """
    if token is None:
        prep = engine.execute_action("prepare_monster_phase", {})
        print("    ▶ prepare_monster_phase")
        for line in _render("prepare_monster_phase", prep, engine):
            print("        " + line)
        _record("prepare_monster_phase", {}, prep, engine)
        engine.save_game(SLOT)
        if not prep.get("success"):
            return prep
        token = prep["result"]["token"]
        prepared = prep["result"]
    else:
        pending = engine.state.pending_monster_phase or {}
        if pending.get("token") != token:
            print(f"        ✗ 待提交token不匹配：pending={pending.get('token')}")
            return {"success": False, "error": "token不匹配"}
        prepared = pending["options"]
        print(f"    ▶ 复用待提交怪物阶段 token={token}")
    if prep_only:
        print("        （仅准备，未提交；决策后再提交同一token）")
        return {"success": True, "action": "仅准备怪物阶段", "result": prepared}
    try:
        choices = _build_monster_choices(prepared, policy or {})
    except ValueError as exc:
        print(f"        ✗ 意图不合法：{exc}（未提交，token 仍在）")
        return {"success": False, "error": str(exc)}
    resolve = engine.execute_action("resolve_monster_phase", {"token": token, "choices": choices})
    for line in _render("resolve_monster_phase", resolve, engine):
        print("        " + line)
    _record("resolve_monster_phase", {"token": token, "choices": choices}, resolve, engine)
    engine.save_game(SLOT)
    return resolve


def _round(engine: GameEngine, params: dict) -> None:
    """AI 把一轮的完整计划写进 params，驱动器只负责执行与记录（含 token 管道）。"""
    rs_params = params.get("round_start") or {}
    _run_step(engine, "round_start", rs_params)
    if params.get("stop_here"):
        return
    for entry in params.get("actions") or []:
        p = engine.state.player
        if not p or not p.is_alive:
            print("    （轮回者已命零，停止本轮计划）")
            return
        if not [m for m in engine.state.enemies if m.is_alive] and params.get("stop_on_clear", True):
            print("    （敌方全灭，停止本轮计划）")
            return
        if "daowen" in entry:
            _run_step(engine, "use_daowen", entry["daowen"])
        elif "attack" in entry:
            _auto_attack(engine, entry["attack"])
        elif "item" in entry:
            _run_step(engine, "consume_item", entry["item"])
        elif "resonance" in entry:
            _run_step(engine, "use_resonance", entry["resonance"])
        elif "raw" in entry:
            _run_step(engine, entry["raw"].get("action"), entry["raw"].get("params") or {})
    p = engine.state.player
    if p and p.is_alive and (params.get("ally_phases", True)):
        _run_step(engine, "resolve_ally_phases", {})
    if p and p.is_alive and params.get("monster_phase", True) and [m for m in engine.state.enemies if m.is_alive]:
        _monster_phase(engine, params.get("monster") or {})
    if engine.state.player and engine.state.player.is_alive:
        _run_step(engine, "round_end", {})


def _render(action: str, result: dict, e: GameEngine) -> list[str]:
    """把引擎真实返回渲染成人读流水（只在有值时写值，不推算、不补写）。"""
    out: list[str] = []
    if not result.get("success"):
        out.append(f"✗ 拒绝：{result.get('error')}")
        for key in ("choices", "instruction", "recoverable", "token"):
            if result.get(key) not in (None, "", [], {}):
                out.append(f"    {key}={result[key]}")
        return out
    r = dict(result.get("result") or {})
    # 部分动作（如战始）把数据平铺在顶层，渲染时合并但以result为准。
    for k, v in result.items():
        if k not in ("success", "action", "instruction", "error"):
            r.setdefault(k, v)
    if action == "setup_attributes":
        out.append(f"✓ 属性：血限{r['blood_limit']} 法限{r['mana_limit']} 速限{r['speed_limit']} "
                   f"攻{r['attack_count']}×{r['attack_power']} 出手{r['action_count']} 碎片{r['shards']}")
        out.append(f"  遗物候选：{r['relic_choices']}")
    elif action == "choose_discovered_relic":
        extra = f"→ 初始道纹候选：{r['daowen_choices']}" if r.get("daowen_choices") else ""
        out.append(f"✓ 选择遗物【{r.get('relic')}】{extra}")
    elif action == "setup_choose_initial_daowen":
        out.append(f"✓ 初始道纹【{r['daowen']}】 持有={r['player_daowen']}")
    elif action == "setup_choose_resonance":
        out.append(f"✓ 残韵【{r['resonance_type']}】×{r['count']}")
    elif action == "setup_choose_region":
        out.append(f"✓ 副本【{r['region']}】 遗物={r.get('relics_owned')}")
    elif action == "pre_battle_action":
        out.append(f"✓ {result.get('action')}：{json.dumps(r, ensure_ascii=False)}")
    elif action == "resolve_event":
        out.append(f"✓ {result.get('action')}：{json.dumps(r, ensure_ascii=False)}")
    elif action == "choose_discovered_item":
        out.append(f"✓ 选择消耗品【{r.get('item')}】耐久{r.get('durability')}")
    elif action == "redeem_attribute_points":
        out.append(f"✓ 兑点数：{json.dumps(r, ensure_ascii=False)}")
    elif action == "battle_start":
        out.append(f"✓ 战始：抽怪{r.get('draw_count')}只 → {r.get('enemies')}")
        for key in ("relic_logs", "artifact_logs"):
            for lg in (r.get(key) or []):
                out.append(f"   [{key}] {json.dumps(lg, ensure_ascii=False)}")
        out.extend(_enemy_lines(e))
    elif action == "round_start":
        out.append(f"✓ 回始：effects={json.dumps(r.get('effects') or [], ensure_ascii=False)}")
        if r.get("spell_logs"):
            out.append(f"   法术：{json.dumps(r['spell_logs'], ensure_ascii=False)}")
        out.extend(_state_block(e)[1:])
    elif action == "use_daowen":
        calc = result.get("calculation") or {}
        exe = result.get("execution") or {}
        out.append(f"✓ 发动【{calc.get('dao_wen')}X={calc.get('x')}】（{result.get('action')}）"
                   f" cost={calc.get('cost_type')}{calc.get('cost')} 效果={calc.get('summary')}")
        out.append(f"   结算effects={json.dumps(exe.get('effects') or [], ensure_ascii=False)}")
        out.append(f"   dodge={json.dumps(result.get('dodge') or {}, ensure_ascii=False)}")
    elif action == "prepare_attack":
        out.append(f"✓ 攻击准备：{r.get('actor')} 击数={r.get('hit_count')} token={r.get('token')}")
        for t in (r.get("target_options") or []):
            out.append(f"   目标 {t['ref']}（{t['name']}）可闪避={t['can_dodge']} 血影={t['can_blood_shadow']}")
    elif action == "resolve_attack":
        out.append(f"✓ {result.get('action')}")
        for hit in (r.get("hits") or []):
            if hit.get("skipped"):
                out.append(f"   第{hit.get('hit_index')}击 跳过（{hit['skipped']}）")
                continue
            out.append(f"   第{hit.get('hit_index')}击 目标{hit.get('target')} "
                       f"闪避声明={hit.get('dodge_attempted')} 闪避成功={hit.get('dodge_success')} "
                       f"伤害={hit.get('damage_dealt')} 格挡吸收={hit.get('shield_absorbed')} "
                       f"失去生命={hit.get('hp_lost')} 目标命零={hit.get('target_died')}")
    elif action == "resolve_ally_phases":
        out.append(f"✓ 友方阶段：{json.dumps(r, ensure_ascii=False)}")
    elif action == "consume_item":
        out.append(f"✓ 消耗品：{json.dumps(r, ensure_ascii=False)}")
    elif action == "use_resonance":
        out.append(f"✓ 残韵改写：{json.dumps(r, ensure_ascii=False)}")
    elif action == "prepare_monster_phase":
        out.append(f"✓ 怪物阶段准备：token={r.get('token')}")
        for a in (r.get("actors") or []):
            opts = "；".join(
                f"{o['name']}" + (f"(X={o['x']}→{o.get('resolves_as')})" if o.get("resolves_as") != o["name"] else f"(X={o['x']})")
                + (f" 目标候选={[t['ref'] for t in o['target_options']]}" if o.get("requires_target") else "")
                + f" 闪避提交={o.get('dodge_submission')}"
                for o in (a.get("daowen_options") or []))
            out.append(f"   actor {a['actor_ref']}（{a['monster']}）普攻{a['base_attack_actions']}轮×{a.get('base_hits_per_attack')}击 "
                       f"攻击目标候选={[t['ref'] for t in a.get('attack_target_options') or []]}")
            out.append(f"     道纹候选：{opts or '无（白板/无可用）'}")
        for sk in (r.get("skipped") or []):
            out.append(f"   跳过 {sk}")
        if r.get("spell_logs"):
            out.append(f"   自动法术：{json.dumps(r['spell_logs'], ensure_ascii=False)}")
    elif action == "resolve_monster_phase":
        out.append(f"✓ 怪物阶段结算：本次条目{r.get('entries')} player_dead={r.get('player_dead')} "
                   f"玩家生命={r.get('player_hp')}")
        for d in (r.get("details") or []):
            out.append(f"   {json.dumps(d, ensure_ascii=False)}")
    elif action == "round_end":
        out.append(f"✓ 回终：effects={json.dumps(r.get('effects') or [], ensure_ascii=False)}")
        out.extend(_state_block(e)[1:])
    elif action == "battle_end":
        out.append(f"✓ 战终：{json.dumps(r, ensure_ascii=False)}")
    else:
        out.append(f"✓ {action}：{json.dumps(r, ensure_ascii=False)[:1500]}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="")
    ap.add_argument("--steps-file", default="")
    ap.add_argument("--slot", default=SLOT)
    ap.add_argument("--new", action="store_true", help="删除存档，从头开一局")
    args = ap.parse_args()

    global INVOCATION, DISCARDED
    INVOCATION = f"{int(time.time())}-{os.getpid()}"
    DISCARDED = json.load(open(DISCARD_PATH, encoding="utf-8")) if os.path.exists(DISCARD_PATH) else []
    steps = json.loads(args.steps) if args.steps else []
    if args.steps_file:
        with open(args.steps_file, encoding="utf-8") as fh:
            steps = json.load(fh)

    engine = _engine()
    if args.new:
        for path in (os.path.join(SESSION_DIR, f"save_{args.slot}.json"), TRACE_PATH):
            if os.path.exists(path):
                os.remove(path)
    if os.path.exists(os.path.join(SESSION_DIR, f"save_{args.slot}.json")):
        loaded = engine.load_game(args.slot)
        if not loaded.get("success"):
            print(f"读档失败：{loaded.get('error')}")
            return
        print(f"（读档 {args.slot}）")
    else:
        print("（新开局）")

    if not steps:
        print("没有步骤。")
        print("\n".join(_state_block(engine)))
        return

    try:
      for i, step in enumerate(steps, 1):
        if step.get("_skip"):
            continue
        action = step.get("action", "")
        params = step.get("params", {})
        if action == "__state__":
            print(f"[{i}] ── 状态快照 ──")
            print("\n".join(_state_block(engine)))
            continue
        if action == "__auto_attack__":
            # 便捷步：AI 已给出逐击完整选择，本步只做 prepare→resolve 的 token 管道连接，
            # 两个真实动作分别按原样记入 trace（决策仍在 AI 手里，驱动器不替代选择）。
            print(f"[{i}] ▶ __auto_attack__")
            _auto_attack(engine, params)
            continue
        if action == "__monster_phase__":
            print(f"[{i}] ▶ __monster_phase__")
            pol = params.get("policy") if isinstance(params.get("policy"), dict) else (
                params if "policy" not in params else None)
            _monster_phase(engine, pol or {}, token=params.get("token"),
                           prep_only=bool(params.get("prep_only")))
            continue
        if action == "__round__":
            print(f"[{i}] ▶ __round__ {json.dumps(params, ensure_ascii=False)}")
            _round(engine, params)
            continue
        before_round = (engine.state.current_battle, engine.state.current_round)
        result = engine.execute_action(action, params)
        try:
            lines = _render(action, result, engine)
        except Exception as exc:  # 渲染失败不得吞掉已结算的真实动作
            lines = [f"（渲染异常 {type(exc).__name__}: {exc}）",
                     "原始返回：" + json.dumps(result, ensure_ascii=False)[:3000]]
        print(f"[{i}] ▶ {action} {json.dumps(params, ensure_ascii=False)}")
        for line in lines:
            print("    " + line)
        _record(action, params, result, engine, retry=bool(step.get("retry")))
        engine.save_game(args.slot)
        if not result.get("success"):
            print("    （本步被拒，状态已回滚；修正后重交）")
    finally:
        # 崩溃也要落盘：存档与 trace 永远指向同一个状态
        try:
            engine.save_game(args.slot)
        except Exception as exc:  # pragma: no cover
            print(f"（收尾存档失败：{exc}）")

    print("── 步骤执行完毕，当前状态 ──")
    print("\n".join(_state_block(engine)))
    saved = engine.save_game(args.slot)
    print(f"存档：{saved.get('filepath')}")


if __name__ == "__main__":
    main()
