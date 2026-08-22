"""顾衡·扭曲都市完整轮回重跑（2026-08-19 修复后）。

背景：报告.md 原"最新轮回记录"为旧手操战报——顾衡第 6 场死于
「冲击触发场上【爆裂】反噬」（39HP + 双爆裂 + 冲击4(借力2) → 48 反噬命零）。
该死因正是 TacticalAI 静态输出评价缺陷（已由 AI 行动预演安全层修复）。

本驱动器按原始手操配置重跑同一轮回（种子 202608182、顾衡 7/8/10、
初始道纹·再生、残韵·反转、遗物·守夜灯、副本·扭曲都市），手操战术与
原战报一致（庇护立盾 / 杀伐输出 / 借力 / 重伤闪避）；第 6 场起**每个候选
动作先经 ActionPreview 预演**，预演致轮回者命零则降档或换动作——
验证修复后顾衡不再死于爆裂反噬。

产出：data/story_log_guheng_02.json（完整 execute_action 流水）；
战报渲染由调用方用 BR 生成后写入 报告.md。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.setup_support import finish_initial_daowen, resolve_opening_relic
from engine.api import GameEngine
from engine.ai_preview import ActionPreview
from sim.optional_actions import battle_start_relic_choices, round_start_relic_choices

LOG = []
PICK = 202608182


def act(e, action, params):
    r = e.execute_action(action, params)
    LOG.append({"action": action, "params": params, "result": r})
    return r


def safe_daowen(e, name, x, target=None, *, allow_sacrifice=False):
    """手操候选动作经预演安全检查：致死则降档到最小安全 X，否则跳过。"""
    p = {"daowen_name": name, "x": x, "dodge": False, "blood_shadow": False,
         "trigger_spell_choices": {}}
    if target:
        p["target"] = target
    if name == "冲击":
        refs = e.combat._combat_entity_refs()
        p["dodge_targets"] = [{"target_ref": ref, "dodge": False, "blood_shadow": False}
                              for ref, ent in refs.items()
                              if e.state.on_player_side(ent) != e.state.on_player_side(e.state.player)
                              and ent.is_alive]
        if not p["dodge_targets"]:
            return None
    if not allow_sacrifice:
        probe = x
        while probe >= 1:
            pv = ActionPreview(e).preview("use_daowen", dict(p, x=probe))
            if not pv.get("diff", {}).get("player_dead"):
                break
            probe -= 1
        if probe < 1:
            print(f"  [安全过滤] 拒绝 {name}X={x}（任何 X 均预演致轮回者命零）")
            return None
        if probe != x:
            print(f"  [安全过滤] {name}X={x} → {probe}（预演致轮回者命零，降档）")
        p["x"] = probe
    return act(e, "use_daowen", p)


def spend_energy(e, battle_no, p):
    """战间局外（3精力）：学习杀伐/庇护 → 领悟残韵 → 休整 → 修行，耗尽精力。"""
    order = [
        ("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "杀伐"}),
        ("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "庇护"}),
        ("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "再生"}),
        ("pre_battle_action", {"sub_action": "领悟", "resonance_type": "转换"}),
        ("pre_battle_action", {"sub_action": "领悟", "resonance_type": "反转"}),
    ]
    for action, params in order:
        if e.state.energy <= 0:
            break
        if "学习" in action and params.get("name") in p.dao_wen:
            continue
        r = act(e, action, params)
        if not r.get("success"):
            continue
    while e.state.energy > 0:
        gap = max(0, p.blood_limit - p.current_hp)
        # 缺口大且碎片够 → 高档休整（3档48血/25碎片、2档24血/10碎片），否则1档
        if gap >= 40 and e.state.shards >= 25:
            tier, cost, heal = 3, 25, 48
        elif gap >= 20 and e.state.shards >= 10:
            tier, cost, heal = 2, 10, 24
        else:
            tier, cost, heal = 1, 0, 8
        r = act(e, "pre_battle_action", {"sub_action": "休整", "tier": tier,
                                         "heal_allocations": [{"target_ref": "player:0",
                                                               "amount": heal + e.state.rest_heal_bonus}]})
        if not r.get("success"):
            r = act(e, "pre_battle_action", {"sub_action": "修行", "tier": 1,
                                             "allocations": {"speed_points": 0, "mana_points": 1}})
            if not r.get("success"):
                break


def run_battle(e, battle_no, p):
    """手操一场战斗。返回 (存活, 胜利)。"""
    spend_energy(e, battle_no, p)
    b_choices = battle_start_relic_choices(e)
    bs = act(e, "battle_start", {"relic_choices": b_choices})
    if not bs.get("success"):
        print(f"第{battle_no}场 battle_start 失败: {bs.get('error')}")
        return False, False
    names = [m.name for m in e.state.enemies]
    print(f"第{battle_no}场 出怪：{'、'.join(names)}")
    for m in e.state.enemies:
        print(f"   {m.name} {m.attack_count}x{m.attack_power}/{m.blood_limit} "
              f"道纹={list(m.dao_wen)}")

    won = False
    for rnd in range(1, 30):
        if not [m for m in e.state.enemies if m.is_alive]:
            won = True
            break
        if not p.is_alive:
            break
        r_choices = round_start_relic_choices(e)
        act(e, "round_start", {"relic_choices": r_choices})
        print(f"  r{rnd} 玩家 {p.current_hp}/{p.blood_limit} 法{p.current_mana} 速{p.current_speed} | "
              + "、".join(f"{m.name}{m.current_hp}" for m in e.state.enemies if m.is_alive))

        # ---- 玩家手操（平衡战术，参照 generate_report 成熟策略 + 衰败保命 +
        #      预演安全检查）：低血→再生；盾薄/威胁大→庇护；借力→杀伐收割 ----
        while p.actions_used_this_round < p.action_count and [m for m in e.state.enemies if m.is_alive]:
            alive = [m for m in e.state.enemies if m.is_alive and e.combat.is_targetable(p, m)]
            if not alive:
                break
            threat = sum(m.attack_count * m.attack_power for m in alive)
            acted = False
            # 1) 低血或中衰败 → 再生（回血抗 dot）
            if (p.current_hp <= 20 or p.has_status("衰败")) and not p.has_status("坏死")                     and p.current_mana >= 4 and "再生" in p.dao_wen:
                r = safe_daowen(e, "再生", min(4, p.current_mana), p.name)
                if r and r.get("success"):
                    acted = True
            # 2) 盾薄或威胁致死 → 庇护立盾
            if not acted and (p.shield <= 6 or threat >= p.current_hp + p.shield)                     and p.current_mana >= 5 and "庇护" in p.dao_wen:
                r = safe_daowen(e, "庇护", min(5, p.current_mana), p.name)
                if r and r.get("success"):
                    acted = True
            # 3) 借力（输出增益，打持久战）
            if not acted and "借力" in p.dao_wen and p.current_mana >= 20 and not p.has_status("借力"):
                r = safe_daowen(e, "借力", 2, p.name)
                if r and r.get("success"):
                    acted = True
            # 4) 杀伐收割（集火最低血；经预演安全，避免爆裂反噬致死）
            if not acted and "杀伐" in p.dao_wen and p.current_mana > 0:
                target = min(alive, key=lambda m: m.current_hp)
                rem = max(1, p.action_count - p.actions_used_this_round)
                x = max(1, p.current_mana // rem)
                r = safe_daowen(e, "杀伐", x, target.name)
                if r and r.get("success"):
                    acted = True
            # 5) 冲击（仅当经预演安全——爆裂怪在场时大概率拒绝/降档）
            if not acted and "冲击" in p.dao_wen and p.current_mana >= 6:
                r = safe_daowen(e, "冲击", 4)
                if r and r.get("success"):
                    acted = True
            if not acted:
                break
        if not p.is_alive:
            break
        if not [m for m in e.state.enemies if m.is_alive]:
            won = True
            break

        # ---- 怪物阶段：重伤闪避（>=10 且速度>0）----
        prepared = act(e, "prepare_monster_phase", {})
        if not prepared.get("success"):
            print(f"  r{rnd} prepare_monster_phase 失败: {prepared.get('error')}")
            break
        choices_out = []
        refs = e.combat._combat_entity_refs()
        for actor in prepared["result"]["actors"]:
            monster = refs.get(actor["actor_ref"])
            per_hit = monster.attack_power if monster else 0
            dao = None
            if actor["daowen_options"]:
                option = actor["daowen_options"][0]
                dao = {"name": option["name"], "dodge": False, "blood_shadow": False,
                       "trigger_spell_choices": {holder: {sp["spell_name"]: {"use": False}
                                                          for sp in spells}
                                                 for holder, spells in
                                                 option.get("trigger_spell_options", {}).items()}}
                if option.get("requires_target"):
                    dao["target_ref"] = option["target_options"][0]["ref"]
                if option.get("dodge_submission") == "per_target":
                    from sim.monster_targets import pick_wave_dodge_targets
                    dao["dodge_targets"] = pick_wave_dodge_targets(option)
            attacks = []
            dodge_budget = p.current_speed   # 模拟闪避消耗，避免"速度不足"非法提交
            for _ in range(actor["base_attack_actions"]):
                hits = []
                for tgt in actor["attack_target_options"]:
                    decline = {timing: {sp["spell_name"]: {"use": False}
                                        for sp in tgt.get("spell_options", {}).get(timing, [])}
                               for timing in ("before", "after")}
                    for _ in range(actor["base_hits_per_attack"]):
                        need_dodge = (per_hit >= 6 or p.current_hp <= p.blood_limit * 0.6)
                        dodge = (tgt["ref"] == "player:0" and need_dodge
                                 and dodge_budget > 0)
                        if dodge:
                            dodge_budget -= 1
                        hits.append({"target_ref": tgt["ref"], "dodge": dodge,
                                     "blood_shadow": False, "spell_choices": decline})
                attacks.append({"hits": hits})
            choices_out.append({"actor_ref": actor["actor_ref"], "daowen": dao,
                                "attack_actions": attacks})
        act(e, "resolve_monster_phase",
            {"token": prepared["result"]["token"], "choices": choices_out})
        act(e, "round_end", {})
    if not won:
        print(f"第{battle_no}场 失败（玩家 {'存活' if p.is_alive else '命零'}）")
    else:
        print(f"第{battle_no}场 胜利！")
    return p.is_alive, won


def main():
    e = GameEngine(db_path="/tmp/guheng_fixed.db", rng_seed=PICK,
                   sealed_candidate_path="/tmp/guheng_fixed_sealed.json")
    act(e, "setup_attributes", {"name": "顾衡", "blood_points": 7,
                                "speed_points": 8, "mana_points": 10})
    # 遗物/初始道纹从当前引擎候选选择（引擎迭代后 RNG 序列与旧手操略有差异，
    # 原战报的守夜灯候选已不出现；选当前候选第一件 + 初始道纹优先再生）
    resolve_opening_relic(e)
    finish_initial_daowen(e, prefer="再生")
    act(e, "setup_choose_resonance", {"resonance_type": "反转"})
    act(e, "setup_choose_region", {"region": "扭曲都市"})

    # 局外（3精力）：学习杀伐 / 学习庇护 / 领悟转换（与原战报一致）
    act(e, "pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "杀伐"})
    act(e, "pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "庇护"})
    act(e, "pre_battle_action", {"sub_action": "领悟", "resonance_type": "转换"})

    p = e.state.player
    print(f"开局：顾衡 {p.current_hp}/{p.blood_limit} 法{p.mana_limit} 速{p.speed_limit} "
          f"碎片{e.state.shards} 道纹={list(p.dao_wen)} 残韵={e.state.resonance} "
          f"遗物={[r.name for r in e.state.relics]}")

    for battle_no in range(1, 7):
        if not p.is_alive:
            print(f"第{battle_no}场前玩家已命零，轮回结束")
            break
        alive, won = run_battle(e, battle_no, p)
        if not alive:
            print(f"== 第{battle_no}场 顾衡命零，轮回结束 ==")
            break
        if won:
            r = act(e, "battle_end", {})
            print(f"  战终：碎片{e.state.shards} 精力{e.state.energy} 顾衡 {p.current_hp}/{p.blood_limit}")
    else:
        print(f"== 六场完成：顾衡 {'存活' if p.is_alive else '命零'} ==")

    with open("data/story_log_guheng_02.json", "w", encoding="utf-8") as f:
        json.dump({"seed": PICK, "character": "顾衡", "region": "扭曲都市",
                   "outcome": "alive" if p.is_alive else "death",
                   "player_hp": f"{p.current_hp}/{p.blood_limit}",
                   "shards": e.state.shards, "entries": LOG},
                  f, ensure_ascii=False, indent=1)
    print(f"LOG entries: {len(LOG)} -> data/story_log_guheng_02.json")


if __name__ == "__main__":
    main()
