#!/usr/bin/env python3
"""用「真实的一阶胜者」真手操乱葬岗（二阶）。

用户要求：测试二阶必须用真实的一阶胜者（从一阶一个个跑出来的胜者快照），
不能用初始角色。本脚本：
1. 加载 data/real_winners/winner_XX.json（真实一阶胜者快照）
2. 以该快照的 player+friends+employees+遗物+碎片 作为起点进入乱葬岗
3. 真手操：每回合显式决策（满法输出+理智闪避+庇护保命+反应法术），
   与 TacticalAI 无关，决策全部来自本脚本的显式 execute_action 调用
4. 输出逐回合战报（数据全部取自引擎返回值）

用法：
    python3 sim/handplay_dungeon_with_winner.py --winner data/real_winners/winner_01.json --battles 1 --seed 7
"""
import argparse
import json
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.ai_tactics import choose_dodge, choose_attack_target
from sim.build_learner import _decline_spells, round_start_relic_choices
from sim.produce_real_winners import build_spell_choices

# 玩家自己回合的手操策略（不用TacticalAI，用最简显式规则）
# 满法输出：杀伐X尽量大；庇护保命：仅在受到致命威胁且盾不足时上盾

def _pick_monster_daowen(engine, actor):
    """怪物按当前情形择优选道纹（README：怪物为胜利和生存作最优决策）。
    输出优先，血低自保，玩家血低收割，机制型按需。"""
    opts = actor["daowen_options"]
    if not opts:
        return None
    m_idx = int(actor["actor_ref"].split(":", 1)[1]) if ":" in actor["actor_ref"] else 0
    activated = set()
    enemies = engine.state.enemies
    monster = None
    if 0 <= m_idx < len(enemies):
        monster = enemies[m_idx]
        activated = engine.combat._monster_activated.get(id(monster), set())
    cands = [o for o in opts if o["name"] not in activated]
    if not cands:
        return opts[0]
    OUTPUT = {"狂暴", "强化", "杀伐", "血债", "切割", "冲击", "加害", "活血", "裂变", "洗劫", "赎金", "逼债", "清算", "赌命"}
    SELF = {"自愈", "庇护", "再生", "固执", "疯狂", "龙鳞"}
    CONTROL = {"减速", "束缚", "衰败", "勾魂", "镇尸", "僵化", "眩晕", "蒙蔽", "弱化", "退化", "冥气", "缄默", "瓦解", "招魂", "无力", "迟滞", "定型", "封印", "缓慢"}
    p = engine.state.player
    player_low = p is not None and p.is_alive and p.current_hp <= p.blood_limit * 0.5
    monster_low = monster is not None and monster.current_hp <= monster.blood_limit * 0.5
    def group(o):
        n = o["name"]
        if n in OUTPUT: return 0
        if n in SELF: return 1
        if n in CONTROL: return 2
        return 3
    if monster_low:
        self_cands = [o for o in cands if o["name"] in SELF]
        if self_cands:
            return self_cands[0]
    if player_low:
        kill_cands = [o for o in cands if o["name"] in OUTPUT or o["name"] in CONTROL]
        if kill_cands:
            return kill_cands[0]
    return min(cands, key=group)

def manual_player_turn(e, log):
    """满法输出+理智闪避+庇护保命（玩家回合）。返回出手列表。"""
    p = e.state.player
    if p is None or not p.is_alive:
        return []
    out = []
    shielded_this_round = False
    for _ in range(max(1, (p.speed_limit + 2) // 3)):
        if not p.is_alive:
            break
        enemies = [x for x in e.state.enemies if x.is_alive]
        if not enemies:
            break
        threat = sum(x.attack_count * x.attack_power for x in enemies)
        lethal = threat > p.current_hp + p.shield
        # 1) 保命：仅在真的会被打死、且本回合还没上过盾时上盾
        if (lethal and not shielded_this_round and "庇护" in p.dao_wen
                and p.current_mana >= 2):
            r = e.execute_action("use_daowen", {"daowen_name": "庇护", "x": 2,
                                                "target": p.name,
                                                "trigger_spell_choices": {}})
            if r.get("success"):
                shield = sum(ef.get("amount", 0) for ef in
                             (r.get("execution", {}).get("effects") or []))
                log.append(f"  保命：庇护X=2 → 格挡+{shield}（盾{p.shield}）")
                out.append(r)
                shielded_this_round = True
                continue
        # 2) 输出：杀伐打血最少，尽量大X（满法输出）
        target = min(enemies, key=lambda x: x.current_hp)
        x = max(1, p.current_mana - 2)  # 留2法力给反应法术
        if x < 1:
            break
        r = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": x,
                                            "target": target.name,
                                            "trigger_spell_choices": {}})
        if r.get("success"):
            dmg = sum(ef.get("actual_damage", 0) for ef in
                      (r.get("execution", {}).get("effects") or []))
            log.append(f"  输出：杀伐X={x} 打{target.name} → {dmg}伤")
            out.append(r)
            continue
        break
    return out


def _resolve_monster_turn_hand(e, log):
    """怪物阶段：理智闪避 + 反应法术触发（先发制人/后发制人/生生不息）。"""
    prepared = e.execute_action("prepare_monster_phase", {})
    if not prepared.get("success"):
        return prepared
    choices = []
    dodge_budget = 0
    refs = e.combat._combat_entity_refs()
    player_ref = "player:0"
    for actor in prepared["result"]["actors"]:
        dao = None
        action_count = actor["base_attack_actions"]
        hit_count = actor["base_hits_per_attack"]
        if actor["daowen_options"]:
            option = _pick_monster_daowen(e, actor)
            dao = {"name": option["name"], "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {holder: {sp["spell_name"]: {"use": False}
                                                      for sp in spells}
                                             for holder, spells in option.get("trigger_spell_options", {}).items()}}
            if option["requires_target"]:
                dao["target_ref"] = option["target_options"][0]["ref"]
            if option["dodge_submission"] == "per_target":
                dao["dodge_targets"] = [
                    {"target_ref": t["ref"], "dodge": False, "blood_shadow": False}
                    for t in option["dodge_target_options"]]
            if option["resolves_as"] == "变形":
                enemy_index = int(actor["actor_ref"].split(":", 1)[1])
                hit_count = e.state.enemies[enemy_index].attack_power
        monster = refs.get(actor["actor_ref"])
        per_hit = monster.attack_power if monster is not None else 0
        target_ref = choose_attack_target(actor["attack_target_options"], refs)
        target_option = next(o for o in actor["attack_target_options"] if o["ref"] == target_ref)
        spell_mana_left = e.state.player.current_mana if e.state.player else 0
        attacks = []
        for _ in range(action_count):
            hits = []
            for _ in range(hit_count):
                want_dodge = choose_dodge(e, per_hit, budget_used=dodge_budget)
                if want_dodge:
                    dodge_budget += 1
                    log.append(f"    闪避{per_hit}伤命中（速度-1）")
                hit = {"target_ref": target_ref, "dodge": want_dodge,
                       "blood_shadow": False,
                       "spell_choices": build_spell_choices(target_option, player_ref,
                                                            mana_budget=spell_mana_left)}
                if target_ref == player_ref:
                    eligible = target_option.get("spell_options", {}) or {}
                    for timing in ("before", "after"):
                        for sp in eligible.get(timing, []) or []:
                            if hit["spell_choices"].get(timing, {}).get(sp["spell_name"], {}).get("use"):
                                spell_mana_left = max(0, spell_mana_left - 2)
                if want_dodge and target_option.get("dodge_relic_target_options"):
                    hit["dodge_relic_target_ref"] = target_option["dodge_relic_target_options"][0]["ref"]
                hits.append(hit)
            attacks.append({"hits": hits})
        choices.append({"actor_ref": actor["actor_ref"], "daowen": dao,
                        "attack_actions": attacks})
    result = e.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": choices})
    if not result.get("success"):
        # 兜底：拒绝全部法术
        fallback = []
        for actor in prepared["result"]["actors"]:
            dao = None
            hit_count_fb = actor["base_hits_per_attack"]
            if actor["daowen_options"]:
                option = _pick_monster_daowen(e, actor)
                dao = {"name": option["name"], "dodge": False, "blood_shadow": False,
                       "trigger_spell_choices": {holder: {sp["spell_name"]: {"use": False}
                                                          for sp in spells}
                                                 for holder, spells in option.get("trigger_spell_options", {}).items()}}
                if option["requires_target"]:
                    dao["target_ref"] = option["target_options"][0]["ref"]
                if option["resolves_as"] == "变形":
                    enemy_index = int(actor["actor_ref"].split(":", 1)[1])
                    hit_count_fb = e.state.enemies[enemy_index].attack_power
            target_ref = choose_attack_target(actor["attack_target_options"], refs)
            target_option = next(o for o in actor["attack_target_options"] if o["ref"] == target_ref)
            attacks = [{"hits": [{
                "target_ref": target_ref, "dodge": False, "blood_shadow": False,
                "spell_choices": _decline_spells(target_option),
            } for _ in range(hit_count_fb)]}
                for _ in range(actor["base_attack_actions"])]
            fallback.append({"actor_ref": actor["actor_ref"], "daowen": dao,
                             "attack_actions": attacks})
        result = e.execute_action("resolve_monster_phase", {
            "token": prepared["result"]["token"], "choices": fallback})
        if result.get("success"):
            log.append("  （法术提交被拒，已退化为纯物理格挡）")
    # 记录法术触发
    for hit in (result.get("result", {}).get("details") or []):
        for lg in (hit.get("spell_logs") or []):
            if lg.get("used"):
                log.append(f"    法术【{lg['spell']}】→ {lg.get('daowen')}X={lg.get('x')} 对{lg.get('target')}")
    return result


def load_winner(e, snapshot):
    """把真实一阶胜者快照注入乱葬岗引擎（与 tests/test_sealed_candidate_in_dungeon.py 同法）。"""
    player = e._deserialize_entity_full(snapshot["player"])
    e.state.player = player
    for f in snapshot.get("friends", []):
        e.state.friends.append(e._deserialize_entity_full(f))
    for emp in snapshot.get("employees", []):
        e.state.employees.append(e._deserialize_entity_full(emp))
    e.state.shards = snapshot.get("shards", 20)
    e.state.resonance = dict(snapshot.get("resonance") or {})
    for r in snapshot.get("relics", []):
        from engine.models import Relic
        e.state.relics.append(Relic(name=r["name"], effect=r.get("effect", ""),
                                    tags=r.get("tags") or []))
    # 属性点继承
    e.state.attribute_points = snapshot.get("attribute_points", 0)
    e.state.artifacts_owned = list(snapshot.get("artifacts_owned") or [])
    # dragon_traits / first_embrace_traits 是对 relics 的只读视图（按 龙族/血族 tag），
    # 上面复制 relics 时已随 tag 一并带入，无需额外赋值。


def play_dungeon(winner_path: str, battles: int, seed: int):
    with open(winner_path, encoding="utf-8") as f:
        snapshot = json.load(f)
    p0 = snapshot["player"]
    print("═" * 66)
    print(f"真实一阶胜者进入乱葬岗：{p0['name']} "
          f"血{p0['blood_limit']} 法{p0['mana_limit']} 速{p0['speed_limit']} "
          f"碎片{snapshot.get('shards')}")
    print(f"  道纹：{sorted(p0['dao_wen'])}")
    print(f"  法术：{[s['name'] for s in p0['spells']]}")
    print(f"  朋友：{len(snapshot.get('friends', []))}  遗物：{[r['name'] for r in snapshot.get('relics', [])]}")
    print("═" * 66)

    e = GameEngine(db_path=f"/tmp/hp_{seed}_{os.getpid()}.db", rng_seed=seed,
                   sealed_candidate_path="/tmp/hp_seal.json")
    e.execute_action("setup_attributes", {"name": p0["name"],
                                          "blood_points": 10, "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    load_winner(e, snapshot)

    cleared = 0
    for b in range(1, battles + 1):
        if not e.state.player or not e.state.player.is_alive:
            print(f"\n❌ 第{b}场：轮回者已阵亡，本次乱葬岗挑战结束")
            break
        # 每场战斗前都做局外（休整回血 + 附煞），与真实游戏流程一致
        fusha_done = False
        while e.state.energy > 0:
            p = e.state.player
            if p and p.current_hp < p.blood_limit:
                r = e.execute_action("pre_battle_action", {
                    "sub_action": "休整", "tier": 3,
                    "heal_allocations": [{"target_ref": "player:0", "amount": 48 + e.state.rest_heal_bonus}]})
                print(f"局外：休整3档 恢复{48 + e.state.rest_heal_bonus} 血 → {p.current_hp}/{p.blood_limit}"
                      if r.get("success") else f"局外：休整失败 {r.get('error')}")
                continue
            if not fusha_done and e.state.shards >= 25:
                r = e.execute_action("pre_battle_action", {
                    "sub_action": "附煞", "mode": "选择", "sha_qi": "冥煞", "daowen_name": "杀伐"})
                if r.get("success"):
                    print("局外：附煞·选择 冥煞 附于【杀伐】（伤害+100%）")
                    fusha_done = True
                    continue
            e.execute_action("pre_battle_action", {
                "sub_action": "修行", "tier": 1,
                "allocations": {"speed_points": 0, "mana_points": 1}})
            print("局外：修行1档 → 法限+2")
        print(f"\n──── 第{b}场 · 战始 ────")
        from sim.optional_actions import start_battle
        bs, _art = start_battle(e)
        if not bs.get("success"):
            print("battle_start失败:", bs.get("error"))
            break
        names = list(bs.get("enemies") or [])
        print("出怪：", names)

        won = False
        for rnd in range(1, 30):
            p = e.state.player
            if not p or not p.is_alive:
                break
            if not [x for x in e.state.enemies if x.is_alive]:
                won = True
                break
            from sim.optional_actions import start_round
            rs, _rsart = start_round(e)
            print(f"\nR{rnd} 回始：玩家 hp={p.current_hp}/{p.blood_limit} 法={p.current_mana} "
                  f"速={p.current_speed} | 敌={[(x.name, x.current_hp) for x in e.state.enemies if x.is_alive]}")
            log = []
            manual_player_turn(e, log)
            for line in log:
                print(line)
            if not [x for x in e.state.enemies if x.is_alive]:
                won = True
                break
            if not p.is_alive:
                break
            e.execute_action("resolve_ally_phases", {})
            if not [x for x in e.state.enemies if x.is_alive]:
                won = True
                break
            mp = _resolve_monster_turn_hand(e, log)
            if not mp.get("success"):
                print("怪物阶段失败:", mp.get("error"))
                break
            print(f"  ← 怪物阶段后：玩家 hp={p.current_hp} 法={p.current_mana} "
                  f"敌={[(x.name, x.current_hp) for x in e.state.enemies if x.is_alive]}")
            e.execute_action("round_end", {})
            if not p.is_alive:
                break
        if won and e.state.player and e.state.player.is_alive:
            cleared += 1
            be = e.execute_action("battle_end", {})
            print(f"\n✅ 第{b}场通关！碎片+{be.get('result', {}).get('shard_reward')} "
                  f"→ 共{e.state.shards}")
        else:
            print(f"\n❌ 第{b}场失败（玩家{'阵亡' if not e.state.player or not e.state.player.is_alive else '未清场'}）")
            break
    print(f"\n结果：通关 {cleared}/{battles} 场")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--winner", default="data/real_winners/winner_01.json")
    ap.add_argument("--battles", type=int, default=1)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    play_dungeon(a.winner, a.battles, a.seed)
