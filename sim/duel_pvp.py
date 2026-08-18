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
    """守擂方一步：选一个未行动的守擂实体（轮回者主将优先），按轮回者规则行动。
    返回 True=已行动（引擎已换边）；False=守擂方本回合无行动可出。"""
    from sim.build_learner import _decline_spells
    log = log or []
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
    # 优先主将（轮回者），否则按列表顺序
    lord = next(((r, x) for r, x in candidates if x.entity_type == "轮回者"), None)
    ref, ent = lord or candidates[0]
    p = e.state.player
    # 1) 主将有法力 → 杀伐打挑战者主将（与挑战者同策略）
    if ent.entity_type == "轮回者" and ent.current_mana >= 2 and p and p.is_alive:
        x = max(1, ent.current_mana - 3)
        r = e.execute_action("use_daowen", {
            "actor_ref": ref, "daowen_name": "杀伐", "x": x,
            "target_ref": "player:0", "trigger_spell_choices": {}})
        if r.get("success"):
            dmg = sum(ef.get("actual_damage", 0) for ef in (r.get("execution", {}).get("effects") or []))
            log.append(f"  守擂{ent.name} 杀伐X={x} 打挑战者 → {dmg}伤")
            return True
    # 2) 主将血低且有庇护 → 上盾
    if (ent.entity_type == "轮回者" and "庇护" in ent.dao_wen
            and ent.current_hp <= ent.blood_limit * 0.4 and ent.current_mana >= 2):
        r = e.execute_action("use_daowen", {
            "actor_ref": ref, "daowen_name": "庇护", "x": 2,
            "target_ref": ref, "trigger_spell_choices": {}})
        if r.get("success"):
            log.append(f"  守擂{ent.name} 庇护X=2（盾{ent.shield}）")
            return True
    # 3) 普攻（prepare_attack/resolve_attack，走玩家侧攻击接口）
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


def run_duel_pvp(e, player_act, max_rounds=60, max_steps=400, log=None):
    """PvP 对称交替死斗：双方都按轮回者规则行动。

    player_act(): 挑战者侧行动1次（成功返回 True，引擎已换边；无行动返回 False）。
    守擂侧由本驱动用玩家侧接口行动（法力制/出手次数/自由控X）。
    返回 dict: {'winner': 'challenger'|'defender', 'rounds': n, 'reason': str}
    """
    from sim.build_learner import round_start_relic_choices
    log = log or []

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
