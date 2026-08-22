#!/usr/bin/env python3
"""死斗对称交替驱动（修复守擂方机制性必胜 bug 后的公平驱动）。

背景：原死斗循环（build_learner 同款）是
  round_start → 挑战者 take_turn（受 duel_turn 严格交替限制，实际每回合只能出1手）
           → resolve_ally_phases → 守擂怪物阶段（原无 duel_turn 校验，每回合全量输出）
→ 挑战者1手 vs 守擂全量 = 守擂方机制性必胜（镜像12/12全胜暴露）。

修复（README「每轮双方交替消耗出手次数，一方耗尽后另一方余下继续」的对称实现）：
  - 挑战者每次出手后 _advance_duel_turn 换边（守擂若有余手）
  - 守擂每步只结算1个 actor（resolve_monster_phase 死斗部分提交），结算后换回挑战者
  - 双方逐出手交替；任一侧余手耗尽则另一方连动
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.build_learner import _decline_spells, round_start_relic_choices



from sim.monster_targets import (  # noqa: E402
    MONSTER_HOSTILE_DAOWEN,
    MONSTER_SELF_DAOWEN,
    pick_monster_daowen_target,
)


def _pick_monster_daowen(engine, actor):
    """怪物按当前情形择优选道纹——委托 sim.monster_targets 统一入口（2026-08-22）。

    优先级分组表不再在本文件复制（此前多处各抄一份、版本变更后三处仍引用
    已删除的切割/冲击/缓慢）；统一口径见 sim.monster_targets.MONSTER_DAOWEN_*
    （DM裁定2026-08-18：自保→输出→控制→机制，含半血/收割/连续压制修正）。
    本文件仅保留候选过滤与空候选回退的原有语义。
    """
    from sim.monster_targets import pick_monster_daowen_option
    opts = actor["daowen_options"]
    if not opts:
        return None
    m_idx = int(actor["actor_ref"].split(":", 1)[1]) if ":" in actor["actor_ref"] else 0
    enemies = engine.state.enemies
    monster = None
    if 0 <= m_idx < len(enemies):
        monster = enemies[m_idx]
    activated = engine.combat._monster_activated.get(id(monster), set()) if monster is not None else set()
    cands = [o for o in opts if o["name"] not in activated]
    if not cands:
        return opts[0]
    p = engine.state.player
    player_low = p is not None and p.is_alive and p.current_hp <= p.blood_limit * 0.5
    monster_low = monster is not None and monster.current_hp <= monster.blood_limit * 0.5
    
    return pick_monster_daowen_option(cands, player_low=player_low,
                                      monster_low=monster_low)

def _resolve_monster_turn_one(e, skip_refs: set):
    """守擂一步：prepare 后只结算1个尚未行动过的 actor，其余本步不动。
    返回 (result, acted_ref)；acted_ref=None 表示本回合守擂已全部行动。"""
    prepared = e.execute_action("prepare_monster_phase", {})
    if not prepared.get("success"):
        return prepared, None
    actors = prepared["result"]["actors"]
    todo = [a for a in actors if a["actor_ref"] not in skip_refs]
    if not todo:
        r = e.execute_action("resolve_monster_phase", {
            "token": prepared["result"]["token"], "choices": []})
        return r, None
    actor = todo[0]
    dao = None
    if actor["daowen_options"]:
        option = _pick_monster_daowen(e, actor)
        dao = {"name": option["name"], "dodge": False, "blood_shadow": False,
               "trigger_spell_choices": {holder: {sp["spell_name"]: {"use": False}
                                                  for sp in spells}
                                         for holder, spells in option.get("trigger_spell_options", {}).items()}}
        if option["requires_target"]:
            dao["target_ref"] = pick_monster_daowen_target(e, actor["actor_ref"], option)
        if option["dodge_submission"] == "per_target":
            from sim.monster_targets import pick_wave_dodge_targets
            dao["dodge_targets"] = pick_wave_dodge_targets(option)
    from engine.ai_tactics import choose_attack_target
    refs = e.combat._combat_entity_refs()
    target_ref = choose_attack_target(actor["attack_target_options"], refs)
    target_option = next((o for o in actor["attack_target_options"] if o["ref"] == target_ref), None)   # 无合法攻击目标时为None（引擎prepare已置base_attack_actions=0）
    attacks = []
    for _ in range(actor["base_attack_actions"]):
        hits = [{"target_ref": target_ref, "dodge": False, "blood_shadow": False,
                 "spell_choices": _decline_spells(target_option)}
                for _ in range(actor["base_hits_per_attack"])]
        attacks.append({"hits": hits})
    choice = {"actor_ref": actor["actor_ref"], "daowen": dao, "attack_actions": attacks}
    r = e.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": [choice]})
    return r, actor["actor_ref"]


def run_duel_alternating(e, player_act, max_rounds=60, max_steps=400):
    """逐出手对称交替死斗。

    player_act(): 挑战者侧行动1次（成功返回 True，duel_turn 已按 _advance_duel_turn
                   换边；无行动返回 False）。
    返回 dict: {'winner': 'challenger'|'defender', 'rounds': n, 'reason': str}
    """
    def _opponent_lord_alive():
        """守擂方是否还有存活轮回者（死斗只允许一名轮回者离开：主将死即败）。"""
        return any(x.is_alive for x in e.state.enemies if x.entity_type == "轮回者")

    def _challenger_alive():
        return bool(e.state.player and e.state.player.is_alive)

    for rnd in range(1, max_rounds + 1):
        if not _challenger_alive():
            return {"winner": "defender", "rounds": rnd, "reason": "挑战者阵亡"}
        if not _opponent_lord_alive():
            return {"winner": "challenger", "rounds": rnd, "reason": "守擂主将阵亡"}
        from sim.optional_actions import start_round
        rs, _rsart = start_round(e)
        acted_refs: set = set()  # 本回合已结算的守擂 actor
        for _ in range(max_steps):
            if not _challenger_alive():
                return {"winner": "defender", "rounds": rnd, "reason": "挑战者阵亡"}
            if not _opponent_lord_alive():
                return {"winner": "challenger", "rounds": rnd, "reason": "守擂主将阵亡"}
            if e.state.duel_turn == "player_side":
                acted = player_act()
                if not acted:
                    ra = e.execute_action("resolve_ally_phases", {})
                    if not (ra.get("result", {}) or {}).get("acted_count", 0):
                        e.state.duel_turn = "opponent_side"  # 挑战者无行动 → 让守擂
            else:  # opponent_side
                mp, acted_ref = _resolve_monster_turn_one(e, acted_refs)
                if not mp.get("success"):
                    return {"winner": "challenger", "rounds": rnd,
                            "reason": f"守擂侧失败:{mp.get('error')}"}
                if mp["result"].get("player_dead"):
                    return {"winner": "defender", "rounds": rnd, "reason": "挑战者阵亡于守擂回合"}
                if acted_ref is None:
                    e.state.duel_turn = "player_side"  # 守擂全部已行动 → 让回挑战者
                else:
                    acted_refs.add(acted_ref)
                    e.state.duel_turn = "player_side"
        e.execute_action("round_end", {})
    return {"winner": "defender", "rounds": max_rounds, "reason": "回合上限"}
