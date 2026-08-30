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

from engine.enums import CombatSubphase
from engine.ai_tactics import daowen_text_kind
# 对白渲染（角色=轮回者，共用 personality）：让 PvP 有台词、有性格差异
try:
    from sim.duel_dialogue import render_line, peek_personality
except Exception:  # 兜底：对白渲染失败不阻塞死斗
    render_line = lambda actor, event, personality=None, rng=None: f"{getattr(actor,'name','??')}: …"
    peek_personality = lambda engine, entity: None

# 注意：不在模块顶层导入 build_learner（循环导入——build_learner 在死斗时局部
# import 本模块，若本模块顶部又导入 build_learner 会拿到半加载的模块，
# round_start_relic_choices 尚未定义 → NameError）。改为函数内延迟导入。


def _defender_resonance_candidates(e, ref, ent, previewer, cache):
    """守擂者（同为轮回者）的残韵候选：用自己的残韵库存转化**挑战者**的道纹，
    并让自己获得转化后的道纹。与挑战者侧 TacticalAI._resonance_candidates 同一
    哲学（对存在变化路径的敌方道纹打分，无固定表）。返回 [(score, params, desc)]。"""
    from engine.daowen import ResonanceEngine
    stock = getattr(ent, "resonance", None) or {}
    if not any(v > 0 for v in stock.values()):
        return []
    p = e.state.player
    if not p or not p.is_alive:
        return []
    out = []
    # 挑战者威胁构成（可见信息）：越强的道纹越值得转化
    for dw, inst in sorted(p.dao_wen.items()):
        if inst is None:
            continue
        text_kind = daowen_text_kind(inst)
        weight = {"damage": 1.0, "control": 0.7, "debuff": 0.6}.get(text_kind, 0.4)
        for path in ResonanceEngine.get_available_resonance(dw):
            rtype = path.get("resonance_type")
            if not rtype or stock.get(rtype, 0) <= 0:
                continue
            params = {"actor_ref": ref, "source_daowen": dw, "resonance_type": rtype,
                      "target_ref": "player:0"}
            pv = previewer.preview("use_resonance", params)
            res = pv.get("result") or {}
            if not res.get("success"):
                continue
            diff = pv.get("diff", {})
            dp = diff.get("player", {})
            # 转化挑战者强道纹即可观收益：夺其威胁 + 施法者获得新道纹的期权
            score = 4.0 * (0.5 + weight)
            score -= 0.5 * max(0, dp.get("hp_before", 0) - dp.get("hp_after", 0))  # 不产生直接伤害，避免与输出竞争时被压制
            out.append((score, params,
                        f"残韵·{rtype}→{dw}@{p.name}（守擂{ent.name}转化）"))
    out.sort(key=lambda t: -t[0])
    return out[:3]


def _resolve_opponent_one(e, log=None, 对话=None):
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
        # 守擂残韵：与挑战者共用一套残韵机制（本步骤不消耗出手，可任意时刻插队）
        for score, rparams, rdesc in _defender_resonance_candidates(e, ref, ent, previewer, cache):
            if best is None or score > best[0]:
                best = (score, rparams, rdesc)
        if best is not None and best[0] > 0:
            params = dict(best[1])
            # 闪避中继(2026-08-26):攻方提交时代目标声明闪避(与怪物阶段解析器同构)。
            # 挑战者是否闪避由 choose_dodge 按伤害阈值/速度预算决定——修复 PvP 双方
            # 从不闪避的问题(用户指出:把把死斗第一回合结束)。
            from engine.ai_tactics import choose_dodge
            pv_check = previewer.preview("use_daowen", params)
            exp_dmg = max(0, ((pv_check.get("diff", {}).get("player", {}) or {})
                              .get("hp_before", 0))
                          - ((pv_check.get("diff", {}).get("player", {}) or {})
                             .get("hp_after", 0)))
            params["dodge"] = choose_dodge(e, exp_dmg) if params.get("target_ref") == "player:0" else False
            r = e.execute_action("use_daowen" if best[1].get("daowen_name") else "use_resonance", params)
            if r.get("success"):
                dodge_note = "(挑战者闪避)" if params.get("dodge") else ""
                log.append(f"  {best[2]}{dodge_note}")
                _defender_line(e, 对话, ent)
                return True
    # 普攻兜底（prepare_attack/resolve_attack，走玩家侧攻击接口；逐击闪避由
    # choose_dodge 按每击伤害与速度预算决定——挑战者侧的闪避终于存在）
    prep = e.execute_action("prepare_attack", {"actor_ref": ref})
    if not prep.get("success"):
        return False
    target_ref = "player:0"
    option = next((o for o in prep["result"]["target_options"] if o["ref"] == target_ref),
                  prep["result"]["target_options"][0])
    from engine.ai_tactics import choose_dodge
    hits = [{"target_ref": option["ref"],
             "dodge": choose_dodge(e, ent.attack_power or 1),
             "blood_shadow": False,
             "spell_choices": _decline_spells(option)}
            for _ in range(prep["result"]["hit_count"])]
    res = e.execute_action("resolve_attack", {"token": prep["result"]["token"], "hits": hits})
    if res.get("success"):
        n_dodge = sum(1 for h in hits if h["dodge"])
        log.append(f"  守擂{ent.name} 普攻（{prep['result']['hit_count']}击"
                   + (f",{n_dodge}击被闪避" if n_dodge else "") + "）")
        _defender_line(e, 对话, ent)
        return True
    return False


# 性格维度：给死斗双方各seed一个**确定、彼此不同**的人格画像（由名字哈希导出）。
# 这使「挑战者 vs 守擂者」的性格差异真实可观察（TacticalAI 性格调制 + 对白渲染），
# 而不是两人都退化为无性格的纯局势效用。人格由 engine.personality 权威记录，
# _refresh_personality/_w 与 render_line 均读同一份数据。
_TRAIT_DIMS = [
    "risk_preference",     # +冒险 / -求稳
    "exploration_desire",  # +探索 / -守成
    "expression_style",    # +直言 / -内敛
    "reaction_pattern",    # +从容 / -慌乱
    "emotional_stability", # +沉稳 / -易波动
    "decision_habit",      # +先观察后行动 / -冲动
    "moral_baseline",      # +守义 / -利己
    "resource_view",       # +节约 / -挥霍
]


def _seed_duelist_personality(e, entity) -> None:
    """给一名轮回者写入一套确定性人格（名字哈希决定各维方向与强度）。

    每条维度记 4 次同向证据（weight=1），使 EMA 收敛到方向、置信度累积到可读阈值，
    score×confidence 足够让 _w() 产生真实调制，也让 render_line 能挑到性格台词。
    """
    if entity is None or not getattr(entity, "is_alive", False):
        return
    import hashlib
    digest = hashlib.sha256(entity.name.encode("utf-8")).hexdigest()
    for i, dim in enumerate(_TRAIT_DIMS):
        byte = int(digest[i % len(digest):i % len(digest) + 2], 16)
        direction = 1 if byte % 2 == 0 else -1
        strength = 0.55 + (byte % 5) / 10.0  # 0.55~0.95，避免所有角色一致
        for _ in range(4):
            try:
                e.update_personality(
                    entity, dim, direction,
                    evidence=f"死斗性格映射：{entity.name}在{_TRAIT_DIMS[i]}维度"
                             f"表现出{'积极' if direction>0 else '消极'}倾向",
                    weight=strength,
                )
            except Exception:
                break  # 实体中途离场等偶发情况：跳过即可，不阻塞死斗


def _clean_ai_label(line: str) -> str:
    """把 TacticalAI 日志行压缩为可读动作标签，如
    '[实时决策] 残韵·曲解→再生@贾凡（对手）（得分 1.83）' → '残韵·曲解→再生@贾凡（对手）'。"""
    label = line.strip()
    label = label.split("[实时决策]", 1)[-1].strip()
    label = label.split("（得分", 1)[0].strip()
    return label


def _defender_line(e, 对话, ent) -> None:
    """守擂者动作后触发一句性格对白（不阻塞死斗）。"""
    if 对话 is None:
        return
    对话.events += 1
    if 对话.events in (3, 10, 18, 26):
        pers = peek_personality(e, ent)
        line = render_line(ent, "damage_out" if 对话.events % 2 else "damage_in", pers)
        对话.buf.append(line)


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


def run_duel_pvp(e, player_act=None, max_rounds=60, max_steps=400, log=None,
                 max_wall_seconds=30.0, use_tactical=True, 对话=None):
    """PvP 对称交替死斗：双方都按轮回者规则行动。

    player_act(): 挑战者侧行动1次（成功返回 True，引擎已换边；无行动返回 False）。
        当 use_tactical=True 时忽略 player_act，改由 TacticalAI（含残韵候选 +
        性格调制 + 变数决策）驱动挑战者；player_act 保留为向后兼容的落后路径。
    守擂侧由本驱动用玩家侧接口行动（法力制/出手次数/自由控X + 残韵候选 + 对白）。
    对话: 可选 SimpleNamespace(buf, events, next_line_round)，用于收集死斗对白。

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
    # 双方都是轮回者：各自 seed 一套确定、可区分的性格画像 → 性格调制 + 对白差异。
    _seed_duelist_personality(e, e.state.player)
    for foe in e.state.enemies:
        if foe.entity_type == "轮回者":
            _seed_duelist_personality(e, foe)
    # 残韵：挑战者沿用 State.resonance（load_winner 已从快照还原其真实准备量）；
    # 守擂者在 _trigger_final_crown 也已从快照还原其真实准备量（无则 0）。
    # 一律**只使用真实准备的残韵**，绝不凭空充能——没有就是没有。
    # 挑战者用 TacticalAI 驱动（残韵+性格+变数）
    _def_tai = None
    if use_tactical:
        from engine.ai_tactics import TacticalAI
        _tai = TacticalAI(e, verbose=True)
        def player_act():
            acted = _tai.take_action()
            if acted:
                # 动作实录：挑战者侧执行的动作（含残韵/道纹）→ 报告可见
                if _tai.log:
                    log.append(f"  挑战者 {_clean_ai_label(_tai.log[-1])}")
                # 对白：挑战者侧动作事件（首句开场后按节奏插台词）
                if 对话 is not None:
                    对话.events += 1
                    pers = peek_personality(e, e.state.player)
                    line = render_line(e.state.player, "opening", pers)
                    if 对话.events in (1, 2, 8, 15):
                        对话.buf.append(line)
                return True
            return False
        # 守擂者用同一套 TacticalAI，视角重定向为守擂（双方共享机制）
        lord = next((x for x in e.state.enemies
                     if x.entity_type == "轮回者" and x.is_alive), None)
        if lord is not None:
            refs = e.combat._combat_entity_refs()
            lord_ref = next((r for r, ent in refs.items() if ent is lord), "player:0")
            foes = [e.state.player] + [f for f in e.state.friends if f.is_alive] \
                   + [emp for emp in e.state.employees if emp.is_alive and emp.is_deployed]
            foes = [f for f in foes if f is not None and f.is_alive]
            # verbose=True：让守擂者动作（含残韵）写入 _def_tai.log，报告才能如实呈现。
            # 否则守擂者一切行动（含残韵）都静默执行、对客席不可见，报告会误判"守擂无残韵"。
            _def_tai = TacticalAI(e, verbose=True, actor=lord, enemies=foes, actor_ref=lord_ref)
    deadline = _time.monotonic() + max_wall_seconds

    def _over_time():
        return _time.monotonic() > deadline

    def _lord_alive():
        return any(x.is_alive for x in e.state.enemies if x.entity_type == "轮回者")

    def _challenger_alive():
        return bool(e.state.player and e.state.player.is_alive)

    # 「连续 N 回合双方面板零净变化」= 真死锁(互瞪):法力每回合已回填,仍无任何
    # 可造成伤害/回血的动作(如 1 血 0 牌对峙)。这与「法力枯竭、回填后还能打」
    # 的 PvP 对耗区分开——后者该继续,而非判死锁。
    hp_frozen_rounds = 0
    for rnd in range(1, max_rounds + 1):
        if not _challenger_alive():
            return {"winner": "defender", "rounds": rnd, "reason": "挑战者阵亡"}
        if not _lord_alive():
            return {"winner": "challenger", "rounds": rnd, "reason": "守擂主将阵亡"}
        from sim.optional_actions import start_round
        # 记录本回合开局双方 hp，用于回合末判「真死锁」。
        hp_before = (e.state.player.current_hp if e.state.player else None,
                     tuple(x.current_hp for x in e.state.enemies if x.entity_type == "轮回者"))
        rs, _rsart = start_round(e)
        if not rs.get("success"):
            # 回始失败不允许吞掉:法力不会回填,双方将永久空转(2026-08-26 死斗三
            # "对手血契"校验事故)。显式判卫冕并留因,绝不无声挂死。
            return {"winner": "defender", "rounds": rnd,
                    "reason": f"回始失败判卫冕: {str(rs.get('error', ''))[:80]}"}
        # 死锁防护:连续 STALL_GUARD_STEPS 步双方面板零变化 → 判卫冕(双有效行动
        # 枯竭的残局,如 1 血 0 法互瞪;不做 400 步 × 60 回合的无意义空转)。
        last_panel, panel_stall = None, 0
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
            panel = (
                (e.state.player.current_hp, e.state.player.shield, e.state.player.current_mana,
                 e.state.player.current_speed, e.state.current_round) if e.state.player else None,
                tuple((x.current_hp, x.shield, x.current_mana, x.current_speed)
                      for x in e.state.enemies if x.entity_type == "轮回者"),
            )
            panel_stall = panel_stall + 1 if panel == last_panel else 0
            last_panel = panel
            if panel_stall >= 50:
                # 本回合双方已无有效动作（法力/出手/可打出的牌耗尽）。这通常是
                # 「法力枯竭」而非死斗终局——先结束本回合让回始回填法力，下一回合
                # 继续对耗。真正的死锁（回填后仍无净变化）由回合末 hp_frozen_rounds 判定。
                break
            if e.state.duel_turn == "player_side":
                acted = player_act()
                if not acted:
                    ra = e.execute_action("resolve_ally_phases", {})
                    if not (ra.get("result", {}) or {}).get("acted_count", 0):
                        e.state.duel_turn = "opponent_side"  # 挑战者无行动 → 让守擂
            else:
                if _def_tai is not None:
                    acted = _def_tai.take_action()
                    if acted:
                        if _def_tai.log:
                            log.append(f"  守擂 {_clean_ai_label(_def_tai.log[-1])}")
                        if 对话 is not None:
                            对话.events += 1
                            pers = peek_personality(e, _def_tai.player)
                            line = render_line(_def_tai.player, "damage_out", pers)
                            if 对话.events in (4, 12, 20, 28):
                                对话.buf.append(line)
                else:
                    acted = _resolve_opponent_one(e, log, 对话)
                if not acted:
                    e.state.duel_turn = "player_side"  # 守擂无行动 → 让回挑战者
        # 本回合双方在死斗里都经"玩家侧接口"驱动（use_daowen/resolve_attack/普攻），
        # 这些 action 只在双方都不能行动时才把 subphase 推到 await_round_end；但守擂侧
        # 由 _resolve_opponent_one 驱动、不触发该收尾，subphase 会停在 player_actions，
        # 使次回合 round_start 撞 guard 失败 → 法力永不回填 → 死锁卫冕。
        # 这里显式收尾到 await_round_end，再调 round_end。
        e.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
        re = e.execute_action("round_end", {})
        if not re.get("success"):
            return {"winner": "defender", "rounds": rnd,
                    "reason": f"round_end判卫冕: {str(re.get('error',''))[:80]}"}
        # 回始：重置双方 AI 的回合记账（凡庸压力/本回合伤害标记/已控制目标）
        if use_tactical:
            _tai.new_round()
            if _def_tai is not None:
                _def_tai.new_round()
        # 回合进度记录（供诊断/报告展示战局推进）
        log.append(f"  ─ 第{rnd}回合结束：挑战者hp={e.state.player.current_hp if e.state.player else 0}"
                   f" 守擂hp={[(x.name, x.current_hp) for x in e.state.enemies if x.entity_type=='轮回者']}")
        # 真死锁判定:回始已回满法力、双方仍无任何净变化(伤害/回血),连续两回合零
        # 净变化即互瞪死锁——法力回填都救不了,判卫冕而非空转满 max_rounds。
        hp_after = (e.state.player.current_hp if e.state.player else None,
                    tuple(x.current_hp for x in e.state.enemies if x.entity_type == "轮回者"))
        frozen = hp_before == hp_after
        hp_frozen_rounds = hp_frozen_rounds + 1 if frozen else 0
        if hp_frozen_rounds >= 2:
            return {"winner": "defender", "rounds": rnd,
                    "reason": "死斗死锁判卫冕(回始回填后双方面板仍连续零净变化)"}
    return {"winner": "defender", "rounds": max_rounds, "reason": "回合上限"}
