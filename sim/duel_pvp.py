#!/usr/bin/env python3
"""真正的 PvP 死斗驱动（守擂方按轮回者规则行动）。

背景（用户指出，已核实）：此前死斗把守擂者整体塞进 state.enemies、用怪物阶段
（prepare_monster_phase/resolve_monster_phase）驱动，守擂者被迫遵守怪物规则：
  - 出手次数 = 1+疯狂X+狂暴1（与速限无关），而非轮回者的 速限/3
  - 不持有法力，发动道纹不付蓝
  - 道纹 X = 实例 x_value（封存快照 x=0 → 道纹全废）
  - 首回合白板（怪不出道纹）
根本没有专门的 PvP 程序执行 PvP 规则。

本驱动让守擂方走与挑战者相同的玩家侧接口（use_daowen / prepare_attack /
resolve_attack），引擎对 in_final_duel 的 opponent 侧已放行，且自动走：
  - 法力制（发动消耗道纹扣守擂主将法力）
  - 出手次数 = 速限/3（朋友/员工 = 攻击次数/3）
  - 道纹 X 自由控（由本驱动决策，与挑战者同策略）
  - 每次行动后 _advance_duel_turn 对称换边
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 注意：不在模块顶层导入 build_learner（循环导入——build_learner 在死斗时局部
# import 本模块，若本模块顶部又导入 build_learner 会拿到半加载的模块，
# round_start_relic_choices 尚未定义 → NameError）。改为函数内延迟导入。


def _resolve_opponent_one(e, log=None):
    """守擂方一步：通用预演决策（2026-08-26 修复"木桩守擂"）。

    旧版硬编码 杀伐/低血庇护/普攻 三板斧——不持有杀伐的封存构筑（如
    畸变/自残/血债流）会整场一动不动,死斗沦为打木桩(用户判定为无效数据)。
    现改为与挑战者 TacticalAI 同一哲学:枚举守擂主将**实际持有**的可用道纹
    × X 档 × 目标,经 ActionPreview 在引擎副本上真实预演,按后果打分:
    挑战者掉血/阵亡 > 自身回血 > 压制挑战者(状态事件) > 法力节约;
    全部被引擎拒绝才退回普攻,普攻也不行才让行。
    每次仍只行动一步,保持"逐出手交替"的 PvP 语义。
    """
    from sim.build_learner import _decline_spells
    if log is None:            # 不得用 `log = log or []`:空列表是 falsy,会静默丢弃调用方的日志缓冲(2026-08-26 修复守擂日志丢失)
        log = []
    refs = e.combat._combat_entity_refs()
    candidates = []
    for ref, ent in refs.items():
        if not ref.startswith("enemy:"):
            continue
        if not ent.is_alive or ent.has_retreated:
            continue
        if ent.actions_used_this_round >= ent.action_count:
            continue
        candidates.append((ref, ent))
    if not candidates:
        return False
    lord = next(((r, x) for r, x in candidates if x.entity_type == "轮回者"), None)
    ref, ent = lord or candidates[0]
    p = e.state.player

    best = None   # (score, params, desc)
    if ent.entity_type == "轮回者" and p and p.is_alive:
        from engine.ai_preview import ActionPreview
        previewer = ActionPreview(e)
        actions_left = max(1, ent.action_count - ent.actions_used_this_round)
        mana_budget = max(1, ent.current_mana // actions_left)
        cache = getattr(e, "_duel_probe_cache", None)
        if cache is None:
            cache = {}
            e._duel_probe_cache = cache
        for name, inst in sorted(ent.dao_wen.items()):
            if inst is None or not inst.can_use():
                continue
            probe = cache.get(name)
            if probe is None:   # X=1 探针:法力单价 + 方向(自身/敌向)
                pv = previewer.preview("use_daowen", {
                    "actor_ref": ref, "daowen_name": name, "x": 1,
                    "target_ref": "player:0", "dodge": False, "blood_shadow": False})
                cost = ((pv.get("result") or {}).get("calculation") or {}).get("cost")
                ok = bool((pv.get("result") or {}).get("success"))
                probe = {"cost": cost if isinstance(cost, int) else 1, "ok_on_player": ok}
                cache[name] = probe
            cost = probe["cost"]
            cap = max(1, ent.current_mana // cost) if cost > 0 else 2
            xs = sorted({1, min(cap, max(1, mana_budget // cost)) if cost > 0 else 1, cap})[-3:]
            for target_ref in ("player:0", ref):
                for x in xs:
                    params = {"actor_ref": ref, "daowen_name": name, "x": x,
                              "target_ref": target_ref, "dodge": False,
                              "blood_shadow": False, "trigger_spell_choices": {}}
                    pv = previewer.preview("use_daowen", params)
                    res = pv.get("result") or {}
                    if not res.get("success"):
                        continue
                    diff = pv.get("diff", {})
                    dp = diff.get("player", {})
                    score = 0.0
                    dmg = max(0, dp.get("hp_before", 0) - dp.get("hp_after", 0))
                    score += 2.2 * dmg
                    if dp.get("dead"):
                        score += 100.0
                    score -= 0.5 * max(0, dp.get("shield_after", 0) - dp.get("shield_before", 0))
                    lord_name = ent.name
                    for ev_row in diff.get("enemies", []):
                        if ev_row.get("name") == lord_name:
                            score += 1.4 * max(0, ev_row.get("hp_after", 0) - ev_row.get("hp_before", 0))
                    for ev in diff.get("events", []):
                        if ev.get("type") == "status_applied" and ev.get("target") == (p.name if p else ""):
                            score += 2.5   # 压制挑战者
                    score -= 0.12 * (res.get("calculation", {}).get("cost") or 0)
                    desc = f"守擂{ent.name} {name}X={x}" + ("(自身)" if target_ref == ref else "→挑战者")
                    if best is None or score > best[0]:
                        best = (score, params, desc)
        if best is not None and best[0] > 0:
            r = e.execute_action("use_daowen", best[1])
            if r.get("success"):
                log.append(f"  {best[2]}")
                return True
    # 普攻兜底（prepare_attack/resolve_attack，走玩家侧攻击接口）
    prep = e.execute_action("prepare_attack", {"actor_ref": ref})
    if not prep.get("success"):
        return False
    target_ref = "player:0"
    option = next((o for o in prep["result"]["target_options"] if o["ref"] == target_ref),
                  prep["result"]["target_options"][0])
    hits = [{"target_ref": option["ref"], "dodge": False, "blood_shadow": False,
             "spell_choices": _decline_spells(option)}
            for _ in range(prep["result"]["hit_count"])]
    res = e.execute_action("resolve_attack", {"token": prep["result"]["token"], "hits": hits})
    if res.get("success"):
        log.append(f"  守擂{ent.name} 普攻（{prep['result']['hit_count']}击）")
        return True
    return False


def _duel_state_sizes(e, top=6):
    """诊断：按字段统计 deepcopy 成本（ms），用于超时样本归因。"""
    import copy, dataclasses, time
    rows = []
    st = e.state
    for f in dataclasses.fields(st):
        try:
            t = time.perf_counter()
            copy.deepcopy(getattr(st, f.name))
            rows.append(((time.perf_counter() - t) * 1000, f.name))
        except Exception:
            pass
    rows.sort(reverse=True)
    return [(nm, round(ms, 2)) for ms, nm in rows[:top]]


def run_duel_pvp(e, player_act, max_rounds=60, max_steps=400, log=None,
                 max_wall_seconds=30.0):
    """PvP 对称交替死斗：双方都按轮回者规则行动。

    player_act(): 挑战者侧行动1次（成功返回 True，引擎已换边；无行动返回 False）。
    守擂侧由本驱动用玩家侧接口行动（法力制/出手次数/自由控X）。
    max_wall_seconds: 墙钟守护（2026-08-22）。死斗在战斗7之后进行，实体/遗物/
        事件累积使 ai_preview 的整状态 deepcopy 预演成本暴涨，实测单场死斗
        100% CPU 空转 >5 分钟（批次13 gen1619 卡死事件）。超时判擂主卫冕
        —— 与“回合上限=攻擂失败”语义一致（挑战者未在限时内完成击杀）。
    返回 dict: {'winner': 'challenger'|'defender', 'rounds': n, 'reason': str}
    """
    import time as _time
    from sim.build_learner import round_start_relic_choices
    if log is None:   # 空列表是 falsy,`log or []` 会静默丢弃调用方缓冲(2026-08-26 同源修复)
        log = []
    deadline = _time.monotonic() + max_wall_seconds

    def _over_time():
        return _time.monotonic() > deadline

    def _lord_alive():
        return any(x.is_alive for x in e.state.enemies if x.entity_type == "轮回者")

    def _challenger_alive():
        return bool(e.state.player and e.state.player.is_alive)

    for rnd in range(1, max_rounds + 1):
        if not _challenger_alive():
            return {"winner": "defender", "rounds": rnd, "reason": "挑战者阵亡"}
        if not _lord_alive():
            return {"winner": "challenger", "rounds": rnd, "reason": "守擂主将阵亡"}
        from sim.optional_actions import start_round
        rs, _rsart = start_round(e)
        for _ in range(max_steps):
            if _over_time():
                return {"winner": "defender", "rounds": rnd,
                        "reason": f"超时卫冕(>{max_wall_seconds:g}s)",
                        "timeout": True,
                        "diag_state_sizes": _duel_state_sizes(e)}
            if not _challenger_alive():
                return {"winner": "defender", "rounds": rnd, "reason": "挑战者阵亡"}
            if not _lord_alive():
                return {"winner": "challenger", "rounds": rnd, "reason": "守擂主将阵亡"}
            if e.state.duel_turn == "player_side":
                acted = player_act()
                if not acted:
                    ra = e.execute_action("resolve_ally_phases", {})
                    if not (ra.get("result", {}) or {}).get("acted_count", 0):
                        e.state.duel_turn = "opponent_side"  # 挑战者无行动 → 让守擂
            else:
                acted = _resolve_opponent_one(e, log)
                if not acted:
                    e.state.duel_turn = "player_side"  # 守擂无行动 → 让回挑战者
        e.execute_action("round_end", {})
    return {"winner": "defender", "rounds": max_rounds, "reason": "回合上限"}
