#!/usr/bin/env python3
"""
自学习流派优化器：让 AI 通过多次轮回自己发现哪些道纹组合 1+1>2。

与 sim/build_winrate.py 的区别：
  build_winrate 是"固定套路"——我事先写死几个流派，只是测它们的胜率。
  build_learner 不预设任何流派：它自己组合道纹、跑轮回、根据胜负更新权重，
  并把学到的结果**写回 JSON**，下次启动继续在此基础上进化。

方法（多臂老虎机 + 协同增益挖掘）：
  1. 每轮从候选道纹池按 UCB1 采样一套 build（初始道纹 + 学习序列）
  2. 跑 N 局，得到 fitness（通关场数 + 胜负加权）
  3. 用 fitness 更新：
       - 单道纹价值   value[A]
       - 配对协同     synergy[A,B] = 含AB的平均分 - (含A平均 + 含B平均)/2
     synergy > 0 即 1+1>2
  4. 精英组合交叉变异产生下一代，持续迭代
  5. 全部状态存入 data/build_knowledge.json，可反复续跑累积经验

用法：
    python3 sim/build_learner.py --generations 20 --runs 6
    python3 sim/build_learner.py --report          # 只看已学到的知识
    python3 sim/build_learner.py --reset           # 清空重学
"""
import argparse
import json
import math
import os
import random
import sys
from collections import defaultdict

from tests.setup_support import choose_discovered_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.ai_tactics import TacticalAI, TACTICAL_ROLES

KNOWLEDGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "build_knowledge.json")

# 候选池：所有 AI 会主动使用的道纹（数据驱动，跟着 TACTICAL_ROLES 走）
CANDIDATES = sorted(TACTICAL_ROLES.keys())

# 门禁修复后，并非所有道纹都能通过局外【学习】获得：
#   - 怪物转化道纹：须以自身已持有的道纹为起点经残韵变化获得（README 211/248）
#   - 副本专属道纹：须先经残韵从本副本怪物身上转化获得一种，才能学其余（README 156）
# 若仍按全池组 build，绝大多数 build 会因"学不上"而退化成同一套，数据失真。
# 故按副本给出"实际可通过学习获得"的候选池。
from engine.gamedata import (REGION_EXCLUSIVE_DAOWEN, ORIGINAL_MONSTER_DAOWEN,
                             MONSTER_TRANSFORM_DAOWEN, SHAFA_LOOP_DAOWEN)

_ALL_EXCLUSIVE = {d for v in REGION_EXCLUSIVE_DAOWEN.values() for d in v}



def _pick_monster_daowen(engine, actor):
    """怪物按当前情形择优选道纹（README：怪物为胜利和生存作最优决策）。

    DM裁定（2026-08-18）优先级固定为：
      1. 自保型（自愈/庇护/再生/固执等）——先保住自己
      2. 输出型（狂暴/强化/杀伐/血债/切割等）
      3. 控制/削弱型（减速/束缚/衰败/勾魂/镇尸等）
      4. 机制型（飞行/必中/贯穿/蒙蔽等）——只有在完全无法对轮回者造成
         任何影响（没有任何自保/输出/控制候选可选）时才允许选择。
    """
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

    # 自保/输出/控制/机制 优先级分组
    OUTPUT = {"狂暴", "强化", "杀伐", "血债", "切割", "冲击", "加害", "活血", "裂变", "洗劫", "赎金", "逼债", "清算", "假钞", "赌命"}
    SELF = {"自愈", "庇护", "再生", "固执", "疯狂", "兴奋", "坚韧", "龙鳞"}
    CONTROL = {"减速", "束缚", "衰败", "勾魂", "镇尸", "僵化", "眩晕", "蒙蔽", "弱化", "退化", "冥气", "缄默", "瓦解", "招魂", "堕落", "坠落", "无力", "迟滞", "定型", "封印", "缓慢"}

    def group(o):
        n = o["name"]
        if n in SELF: return 0
        if n in OUTPUT: return 1
        if n in CONTROL: return 2
        return 3

    # 机制组是最后手段：存在任何自保/输出/控制候选时一律不选机制。
    # 例外（裁定原文"完全无法对轮回者造成任何影响"的字面情形）：怪物已连续
    # ≥2回合未能使敌方生命减少（如伤害被格挡完全吸收），说明常规手段已失效，
    # 允许动用机制组（必中破盾/飞行脱离等）。
    blocked = monster is not None and getattr(monster, "no_damage_rounds", 0) >= 2
    if blocked:
        mech_cands = [o for o in cands if group(o) == 3]
        if mech_cands:
            return mech_cands[0]
    effective = [o for o in cands if group(o) < 3]
    return min(effective or cands, key=group)

def _decline_spells(option):
    return {timing: {spell["spell_name"]: {"use": False}
                     for spell in option.get("spell_options", {}).get(timing, [])}
            for timing in ("before", "after")}


def _trigger_spells(option, player_mana):
    """玩家受击时触发反应法术（此前 _decline_spells 全部拒绝，先发制人/生生不息
    从不触发=玩家法术白学）。有法力就触发：先发制人反击、后发制人上盾、生生不息回血。"""
    out = {}
    for timing in ("before", "after"):
        out[timing] = {}
        for spell in option.get("spell_options", {}).get(timing, []) or []:
            name = spell["spell_name"]
            steps = spell.get("steps", [])
            use = False
            cycles = []
            if player_mana >= 1:
                use = True
                # 自由控X：攻击步骤尽量大X（杀伐/切割/血债等），自保步骤留1
                cycle = []
                remaining = player_mana
                for st in steps:
                    is_self = st.get("target_ref") == "player:0"
                    x = max(1, remaining - 1) if not is_self else 1
                    entry = {"x": x, "target_ref": st.get("target_ref")}
                    if st.get("target_ref") != "player:0":
                        entry["dodge"] = False
                    cycle.append(entry)
                    remaining -= x
                cycles = [cycle]
            out[timing][name] = {"use": use, "cycles": cycles} if use else {"use": False}
    return out

def learnable_candidates(region: str = None) -> list:
    """当前副本下可通过局外【学习】直接获得的道纹（不含需残韵转化的）。"""
    out = []
    for c in CANDIDATES:
        if c in ORIGINAL_MONSTER_DAOWEN or c in MONSTER_TRANSFORM_DAOWEN:
            continue
        if c in _ALL_EXCLUSIVE and c not in set(REGION_EXCLUSIVE_DAOWEN.get(region, ())):
            continue
        out.append(c)
    return out
# 开局从杀伐闭环【发现】3选1；STARTERS 只表示可被偏好的闭环节点，不能绕过发现。
STARTERS = list(SHAFA_LOOP_DAOWEN)
REGIONS = ["罪孽都市", "扭曲都市", "龙心谷"]
BUILD_SIZE = 5          # 每套 build 学习的道纹数量


# --------------------------------------------------------------------------
# 一局轮回
# --------------------------------------------------------------------------

def _resolve_monster_turn(engine):
    prepared = engine.execute_action("prepare_monster_phase", {})
    if not prepared.get("success"):
        return prepared
    from engine.ai_tactics import choose_dodge, choose_attack_target
    choices = []
    # 闪避预算跨整个怪物阶段共享（多怪时每只各算会超支当前速度）
    dodge_budget = 0
    for actor in prepared["result"]["actors"]:
        dao = None
        action_count = actor["base_attack_actions"]
        hit_count = actor["base_hits_per_attack"]
        if actor["daowen_options"]:
            option = _pick_monster_daowen(engine, actor)
            dao = {"name": option["name"], "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {holder: {sp["spell_name"]: {"use": False} for sp in spells}
                                               for holder, spells in option.get("trigger_spell_options", {}).items()}}
            if option["requires_target"]:
                dao["target_ref"] = option["target_options"][0]["ref"]
            if option["dodge_submission"] == "per_target":
                dao["dodge_targets"] = [
                    {"target_ref": target["ref"], "dodge": False, "blood_shadow": False}
                    for target in option["dodge_target_options"]
                ]
            if option["resolves_as"] == "变形":
                enemy_index = int(actor["actor_ref"].split(":", 1)[1])
                hit_count = engine.state.enemies[enemy_index].attack_power
        refs = engine.combat._combat_entity_refs()
        monster = refs.get(actor["actor_ref"])
        per_hit = monster.attack_power if monster is not None else 0
        target_ref = choose_attack_target(actor["attack_target_options"], refs)
        target_option = next(option for option in actor["attack_target_options"] if option["ref"] == target_ref)
        attacks = []
        for _ in range(action_count):
            hits = []
            for _ in range(hit_count):
                want_dodge = choose_dodge(engine, per_hit,
                                          budget_used=dodge_budget)
                if want_dodge:
                    dodge_budget += 1
                spell_choices = (_trigger_spells(target_option, engine.state.player.current_mana if engine.state.player else 0)
                                 if target_ref == "player:0" else _decline_spells(target_option))
                hit = {"target_ref": target_ref, "dodge": want_dodge,
                       "blood_shadow": False, "spell_choices": spell_choices}
                # 回锋刀：闪避触发反击必须显式提交合法敌方目标
                if want_dodge and target_option.get("dodge_relic_target_options"):
                    hit["dodge_relic_target_ref"] = target_option["dodge_relic_target_options"][0]["ref"]
                hits.append(hit)
            attacks.append({"hits": hits})
        choices.append({"actor_ref": actor["actor_ref"], "daowen": dao,
                        "attack_actions": attacks})
    result = engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": choices,
    })
    if result.get("success"):
        return result

    # prepare 的首个候选可能在结算时因动态支付能力失效；原子失败后以同一快照
    # 显式提交合法决策（保留首个道纹选项），避免把可继续的模拟误记为引擎异常。
    fallback = []
    refs_fb = engine.combat._combat_entity_refs()
    for actor in prepared["result"]["actors"]:
        dao = None
        if actor["daowen_options"]:
            option = _pick_monster_daowen(engine, actor)
            dao = {"name": option["name"], "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {holder: {sp["spell_name"]: {"use": False} for sp in spells}
                                               for holder, spells in option.get("trigger_spell_options", {}).items()}}
            if option["requires_target"]:
                dao["target_ref"] = option["target_options"][0]["ref"]
            if option["dodge_submission"] == "per_target":
                dao["dodge_targets"] = [
                    {"target_ref": target["ref"], "dodge": False, "blood_shadow": False}
                    for target in option["dodge_target_options"]
                ]
        target_ref = choose_attack_target(actor["attack_target_options"], refs_fb)
        target_option = next(option for option in actor["attack_target_options"]
                             if option["ref"] == target_ref)
        attacks = [{"hits": [{
            "target_ref": target_ref, "dodge": False, "blood_shadow": False,
            "spell_choices": _decline_spells(target_option),
        } for _ in range(actor["base_hits_per_attack"])]}
                   for _ in range(actor["base_attack_actions"])]
        fallback.append({"actor_ref": actor["actor_ref"], "daowen": dao,
                         "attack_actions": attacks})
    return engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": fallback,
    })


def _resolve_pending_event(engine):
    """平衡模拟器显式选择事件选项；不替正式玩家作选择。

    优先级：拒绝/离开类选项 → 其余选项按原顺序逐项尝试，取第一个能通过
    引擎代价校验的（失败选项不改状态，可安全重试）；全部失败才报错，
    避免"只有付不起代价的选项"被误记为引擎异常。
    """
    while engine.event_pool.current is not None:
        name = engine.event_pool.current
        event = engine.event_pool.events[name]
        reject_words = ("无事发生", "拒绝", "离开", "观棋", "视而不见", "绕桥")
        ordered = sorted(event["options"],
                         key=lambda entry: 0 if any(w in entry["text"] for w in reject_words) else 1)
        result = None
        for option in ordered:
            result = engine.execute_action("resolve_event", {
                "event": name, "option_id": option["id"], "x": 1,
                "resonance_type": "转换", "daowen_names": ["杀伐"],
                "wusuoqiu_allocation": "speed",  # 模拟器确定性默认：持无所求时属性点加速限
            })
            if result.get("success"):
                break
        if not result or not result.get("success"):
            return result or {"success": False, "error": f"事件【{name}】无可支付选项"}
        if result.get("completed") is False:
            return {"success": False,
                    "error": f"事件【{name}】需要DM裁定，平衡模拟器不能代替裁定"}
        # 事件代价可能恰好致死（死之传承中断入队），之后的附赠发现会被门禁挡住；
        # 交由 play() 按阵亡结束，不误标 invalid。
        if not engine.state.player or not engine.state.player.is_alive:
            return {"success": True, "player_dead": True}
        if engine.state.pending_item_choices:
            chosen = engine.execute_action("choose_discovered_item", {
                "item_name": engine.state.pending_item_choices[0],
            })
            if not chosen.get("success"):
                return chosen
        if engine.state.pending_relic_choices:
            chosen = engine.execute_action("choose_discovered_relic", {
                "relic_name": engine.state.pending_relic_choices[0],
            })
            if not chosen.get("success"):
                return chosen
    return {"success": True}


def play(starter: str, learn: list, region: str, seed=None, battles: int = 7,
         rng: random.Random = None, policy: dict = None, telemetry: dict = None,
         spend_shards: bool = False, spell_plan: list = None) -> dict:
    """
    跑一局轮回。seed=None 时引擎使用真随机源。

    policy: 局外行动权重 {行动名: 权重}，AI 按权重随机挑选可用行动。
            这样"选择率"是 AI 自己选出来的，而不是脚本写死的。
    telemetry: 传入则累计真实统计（行动选择/成功/失败原因/异常）。
    spend_shards: 七场局外把碎片尽量花成战力（高阶修行/附煞/共鸣）——用户裁定
            "不花碎片怎么快速提高战力"。False=旧行为，True=新玩法。

    返回含 invalid 标记：本局若出现引擎异常，视为无效数据（不计入统计）。
    """
    rng = rng or (random.Random(seed) if seed is not None else random)
    policy = policy or DEFAULT_POLICY
    # 脚本以 __main__ 运行时会产生双模块（__main__ 与 sim.build_learner），
    # 模块级 round_start_relic_choices 需在此显式引用，否则 NameError。
    from sim.build_learner import round_start_relic_choices as _rsrc
    global round_start_relic_choices
    round_start_relic_choices = _rsrc
    e = GameEngine(db_path="/tmp/learner.db", rng_seed=seed)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    chosen = choose_discovered_initial_daowen(e, prefer=starter)
    if not chosen.get("success"):
        raise ValueError(chosen.get("error", "开局发现选择失败"))
    actual_starter = chosen["picked"]
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": region})
    optional_relics = {"折速法印", "三相残韵盘"}
    starter_relic = next((n for n in setup["result"]["relic_choices"] if n not in optional_relics),
                         setup["result"]["relic_choices"][0])
    e.execute_action("choose_discovered_relic", {"relic_name": starter_relic})

    ai = TacticalAI(e)
    todo = [name for name in learn if name != actual_starter]
    # 法术按当前持有道纹解锁，不再默认杀伐起手。
    SPELL_PLAN = spell_plan or [("先发制人", ["杀伐"]), ("生生不息", ["再生"]), ("后发制人", ["庇护"])]
    learned_spells = set()
    cleared = 0

    def record(kind, name, detail=""):
        if telemetry is None:
            return
        telemetry.setdefault(kind, {})
        key = name if not detail else f"{name}｜{detail}"
        telemetry[kind][key] = telemetry[kind].get(key, 0) + 1

    for b in range(1, battles + 1):
        while e.state.energy > 0:
            before = e.state.energy
            # 花碎片→战力（spend_shards）：与学习并行——每场优先修行1次保证法限成长，
            # 其余精力学道纹（此前"学完todo才花碎片"导致5道纹build学5场碎片花晚了，
            # 法限没提上去，更强build反而更弱）。
            if spend_shards:
                p = e.state.player
                if p and e.state.shards >= 35:
                    r = e.execute_action("pre_battle_action", {
                        "sub_action": "修行", "tier": 3,
                        "allocations": {"speed_points": 0, "mana_points": 3}})
                    if r.get("success"):
                        continue
                if p and e.state.shards >= 25 and e.state.current_region == "乱葬岗":
                    held = next(iter(p.dao_wen), actual_starter)
                    r = e.execute_action("pre_battle_action", {
                        "sub_action": "附煞", "mode": "选择", "sha_qi": "冥煞", "daowen_name": held})
                    if r.get("success"):
                        continue
                if p and e.state.shards >= 15 and todo:
                    r = e.execute_action("pre_battle_action", {
                        "sub_action": "修行", "tier": 2,
                        "allocations": {"speed_points": 0, "mana_points": 2}})
                    if r.get("success"):
                        continue
            # 学法术：已有对应道纹且没学过的先学（免费1精力）。
            # spell_plan 元素可为 ("名", [道纹]) 或 {"name","required_daowen","trigger_condition",
            # "effect_flow"}（自创法术，走 dm_approved 自动通过）。
            spell_next = None
            spell_definition = None
            for item in SPELL_PLAN:
                if isinstance(item, dict):
                    sname = item["name"]
                    req = item["required_daowen"]
                    if sname in learned_spells:
                        continue
                    if all(r in e.state.player.dao_wen for r in req):
                        spell_next = sname
                        spell_definition = item
                        break
                else:
                    sname, req = item
                    if sname in learned_spells:
                        continue
                    if all(r in e.state.player.dao_wen for r in req):
                        spell_next = sname
                        break
            if spell_next:
                if spell_definition:
                    # 自创法术：提交→dm_approved
                    e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "custom_spell",
                                                           "spell": spell_definition})
                    r = e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "custom_spell",
                                                               "spell": spell_definition, "dm_approved": True})
                else:
                    r = e.execute_action("pre_battle_action", {
                        "sub_action": "学习", "sub": "spell", "tier": 1, "names": [spell_next]})
                if r.get("success"):
                    learned_spells.add(spell_next)
                    continue
            act, params = choose_pre_battle(e, todo, b, rng, policy)
            record("attempted", act)
            try:
                r = e.execute_action("pre_battle_action", {"sub_action": act, **params})
            except Exception as ex:                      # 引擎抛异常 = bug，本局作废
                record("engine_error", act, f"{type(ex).__name__}: {ex}")
                return {"cleared": cleared, "won": False, "invalid": True,
                        "reason": f"pre_battle {act}: {ex}"}
            if r.get("success"):
                record("succeeded", act)
                if act == "学习" and params.get("name") in todo:
                    todo.remove(params["name"])
                # 附煞·发现模式：显式选择候选煞气
                if act == "附煞" and e.state.pending_sha_qi_choices:
                    held = params.get("daowen_name") or next(iter(e.state.player.dao_wen), actual_starter)
                    e.execute_action("choose_sha_qi", {
                        "sha_qi": e.state.pending_sha_qi_choices[0],
                        "daowen_name": held,
                    })
                if e.state.pending_relic_choices:
                    e.execute_action("choose_discovered_relic", {
                        "relic_name": e.state.pending_relic_choices[0],
                    })
                if e.state.pending_item_choices:
                    e.execute_action("choose_discovered_item", {
                        "item_name": e.state.pending_item_choices[0],
                    })
                for employee_name, choices in list(e.state.pending_daowen_choices.items()):
                    e.execute_action("choose_hired_daowen", {
                        "name": employee_name, "daowen": choices[0],
                    })
                if e.event_pool.current is not None:
                    event_result = _resolve_pending_event(e)
                    if not event_result.get("success"):
                        return {"cleared": cleared, "won": False, "invalid": True,
                                "reason": f"event: {event_result.get('error')}"}
                # 事件选项的代价可能恰好致死（如流血6且仅剩6血）——死之传承中断入队，
                # 之后的任何行动都会被门禁挡住；直接按阵亡结束本局，不误标 invalid。
                if not e.state.player or not e.state.player.is_alive:
                    return {"cleared": cleared, "won": False, "invalid": False}
            else:
                record("failed", act, str(r.get("error"))[:60])
                # 失败必须退还精力，否则会死循环；引擎已退还，这里兜底防死锁
                if e.state.energy >= before:
                    e.execute_action("pre_battle_action",
                                     {"sub_action": "修行", "tier": 1, "to": "mana"})

        # 共鸣/事件可能在开局后继续获得可选战始遗物；按当前持有列表逐件显式决策
        # （可以不用但不能不让用：折速法印换法力/三相残韵盘/猩红果实/苍白之花按情形发动）。
        bs, bs_artifact_logs = start_battle_with_artifacts(e)
        if not bs.get("success"):
            return {"cleared": cleared, "won": False, "invalid": True,
                    "reason": f"battle_start: {bs.get('error')}"}
        if bs_artifact_logs:
            for _r in bs_artifact_logs:
                record("artifact", _r.get("action", ""))
        for _ in range(40):
            if not e.state.player or not e.state.player.is_alive:
                break
            if not [x for x in e.state.enemies if x.is_alive]:
                break
            rs, _rs_logs = start_round_with_artifacts(e)
            if not rs.get("success"):
                return {"cleared": cleared, "won": False, "invalid": True,
                        "reason": f"round_start: {rs.get('error')}"}
            ai.new_round()
            try:
                ai.take_turn()
            except Exception as ex:
                record("engine_error", "combat", f"{type(ex).__name__}: {ex}")
                return {"cleared": cleared, "won": False, "invalid": True,
                        "reason": f"combat: {ex}"}
            if not [x for x in e.state.enemies if x.is_alive]:
                break
            # 玩家可能在自己回合内命零（癌变/崩解/代价反噬）——此时死之传承中断已入队，
            # 不能再进怪物阶段（会被中断门禁挡成 invalid），直接按阵亡结算。
            if not e.state.player or not e.state.player.is_alive:
                break
            # [朋友]/[员工]自主出手（无语言命令时，README：微光者会根据情况对敌方出手）
            e.execute_action("resolve_ally_phases", {})
            if not [x for x in e.state.enemies if x.is_alive]:
                break
            mp = _resolve_monster_turn(e)
            if not mp.get("success"):
                return {"cleared": cleared, "won": False, "invalid": True,
                        "reason": f"monster_phase: {mp.get('error')}"}
            if mp["result"].get("player_dead"):
                break
            e.execute_action("round_end", {})

        if not e.state.player or not e.state.player.is_alive:
            return {"cleared": cleared, "won": False, "invalid": False}
        if [x for x in e.state.enemies if x.is_alive]:
            return {"cleared": cleared, "won": False, "invalid": False}
        ended = e.execute_action("battle_end", {})
        if not ended.get("success"):
            return {"cleared": cleared, "won": False, "invalid": True,
                    "reason": f"battle_end: {ended.get('error')}"}
        # 战终结算可能触发癌变/凡庸等命零（回复过量/未造成伤害），死之传承中断
        # 会入队阻塞后续行动；此时本场仍算通关（cleared+1），但轮回到此结束。
        if not e.state.player or not e.state.player.is_alive:
            return {"cleared": cleared + 1, "won": False, "invalid": False}
        cleared += 1

        # 第7场战终触发【最终的冠冕】：第一名封存；第二名进入第8场死斗。
        # 死斗胜利后须领取终音法器（choose_terminal_artifact）才算完整通关并封存。
        crown = ended.get("result", {}).get("final_crown", {})
        outcome = crown.get("outcome")
        if outcome == "sealed":
            return {"cleared": cleared, "won": True, "invalid": False,
                    "sealed": True, "sealed_name": crown.get("sealed_name")}
        if outcome == "duel_start" or e.state.in_final_duel:
            # 第8场死斗：用新的对称PvP驱动（守擂方走玩家侧接口，逐出手交替）。
            # 旧循环用 _resolve_monster_turn 每回合全量驱动守擂方，与死斗交替门禁
            # （player_side回合守擂不行动）冲突，且守擂被当怪物处理（无PvP规则）。
            from sim.duel_pvp import run_duel_pvp
            log_buf = []
            def _act():
                if not e.state.player or not e.state.player.is_alive:
                    return False
                ai.new_round()
                try:
                    ai.take_turn()
                except Exception:
                    return False
                return True
            dr = run_duel_pvp(e, _act, max_rounds=60, max_steps=400, log=log_buf)
            duel_won = dr.get("winner") == "challenger"
            if not duel_won or not e.state.player or not e.state.player.is_alive:
                return {"cleared": cleared, "won": False, "invalid": False}
            # 死斗胜利：领取终音法器
            dr = e.execute_action("resolve_final_duel", {"outcome": "victory"})
            if not dr.get("success"):
                return {"cleared": cleared, "won": False, "invalid": True,
                        "reason": f"resolve_final_duel: {dr.get('error')}"}
            if dr.get("result", {}).get("pending_terminal_choice"):
                region = dr["result"]["pending_terminal_choice"]
                options = dr["result"].get("options") or []
                if options:
                    ca = e.execute_action("choose_terminal_artifact", {"choice": 1})
                    if not ca.get("success"):
                        return {"cleared": cleared, "won": False, "invalid": True,
                                "reason": f"choose_terminal_artifact: {ca.get('error')}"}
                    # 若选了猩红尖牙会触发初拥之夜，需继续选择
                    if e.state.pending_first_embrace:
                        e.execute_action("choose_first_embrace", {"choice": 1})
            return {"cleared": cleared, "won": True, "invalid": False,
                    "duel_won": True, "terminal_artifact": True}

    return {"cleared": cleared, "won": True, "invalid": False}


# 局外行动权重：AI 按此概率挑选。7项为引擎当前可用行动
# （忘忧/献祭需道具，雇佣仅罪孽都市，维修仅扭曲都市，炼心仅龙心谷）
DEFAULT_POLICY = {
    "修行": 30, "学习": 25, "休整": 15, "共鸣": 10,
    "探索": 8, "领悟": 6, "炼心": 2, "维修": 2, "雇佣": 2, "附煞": 18,
}

REGION_ACTION = {"炼心": "龙心谷", "维修": "扭曲都市", "雇佣": "罪孽都市", "附煞": "乱葬岗"}


def choose_pre_battle(e, todo, battle_no, rng, policy):
    """AI 自主挑选一个局外行动（按权重），返回 (行动名, 参数)。"""
    p = e.state.player
    cands = []
    for act, w in policy.items():
        need = REGION_ACTION.get(act)
        if need and e.state.current_region != need:
            continue
        if act == "维修" and not any(0 < item.current_uses < item.max_uses
                                      for item in e.state.consumables):
            continue
        if act == "学习" and not todo:
            continue
        if act == "休整" and p and p.current_hp >= p.blood_limit:
            continue          # 满血不休整（无效行动，不该计入选择率）
        cands.append((act, w))
    if not cands:
        return "修行", {"tier": 1, "to": "mana"}

    total = sum(w for _, w in cands)
    pick = rng.uniform(0, total)
    acc = 0
    act = cands[-1][0]
    for a, w in cands:
        acc += w
        if pick <= acc:
            act = a
            break

    if act == "学习":
        return act, {"sub": "daowen", "name": todo[0]}
    if act == "附煞":
        held = next(iter(p.dao_wen), None) if p else None
        if not held:
            return "修行", {"tier": 1, "to": "mana"}
        # 确定性：碎片≥25用选择（冥煞附当前持有道纹），≥10用发现，否则跳过
        if e.state.shards >= 25:
            return act, {"mode": "选择", "sha_qi": "冥煞", "daowen_name": held}
        if e.state.shards >= 10:
            return act, {"mode": "发现", "daowen_name": held}
        return "修行", {"tier": 1, "to": "mana"}
    if act == "修行":
        return act, {"tier": 1, "to": "mana" if battle_no % 2 else "speed"}
    if act == "休整":
        return act, {"tier": 1, "heal_allocations": [
            {"target_ref": "player:0", "amount": 8 + e.state.rest_heal_bonus},
        ]}
    if act == "领悟":
        return act, {"resonance_type": rng.choice(["转换", "反转", "曲解"])}
    if act == "维修":
        index = next(index for index, item in enumerate(e.state.consumables)
                     if 0 < item.current_uses < item.max_uses)
        return act, {"tier": 1, "allocations": [
            {"item_ref": f"consumable:{index}", "amount": 1},
        ]}
    if act == "雇佣":
        return act, {"name": f"雇员{rng.randrange(1000)}", "blood_alloc": 8, "atk_bundles": 4}
    return act, {}


def fitness(starter: str, learn: list, runs: int, gen: int,
            random_seeds: bool = False, rng: random.Random = None,
            telemetry: dict = None, spend_shards: bool = False) -> tuple:
    """
    适应度 = 平均通关场数 + 3×胜率（0~10）。

    random_seeds=False（默认）：种子由代数推导，同一代可复现，便于排查。
    random_seeds=True：每局用真随机种子与随机副本，样本不重复，
      能避免"只在某几局上表现好"的过拟合，代价是结果不可逐局复现。

    返回 (score, valid_runs, invalid_runs)。
    出现引擎异常的对局视为**无效数据**，不计入分数与统计。
    """
    # 非随机模式必须完全可复现：局外行动的挑选也要用确定性 rng，
    # 否则同参数两次评估会因决策不同而给出不同分数。
    if rng is None:
        rng = random if random_seeds else random.Random(gen * 7919 + 13)
    total = 0.0
    valid = 0
    invalid = 0
    for i in range(runs):
        if random_seeds:
            seed = rng.randrange(1, 2 ** 31 - 1)
            region = rng.choice(REGIONS)
        else:
            seed = gen * 1000 + i * 7 + 1
            region = REGIONS[i % len(REGIONS)]
        r = play(starter, learn, region, seed, rng=rng, telemetry=telemetry,
                spend_shards=spend_shards)
        if r.get("invalid"):
            invalid += 1
            if telemetry is not None:
                telemetry.setdefault("invalid_reasons", {})
                key = str(r.get("reason"))[:80]
                telemetry["invalid_reasons"][key] = telemetry["invalid_reasons"].get(key, 0) + 1
            continue
        valid += 1
        total += r["cleared"] + (3.0 if r["won"] else 0.0)
        if telemetry is not None:
            telemetry.setdefault("outcomes", {"win": 0, "loss": 0, "cleared_sum": 0})
            telemetry["outcomes"]["win" if r["won"] else "loss"] += 1
            telemetry["outcomes"]["cleared_sum"] += r["cleared"]
            telemetry.setdefault("region_runs", {})
            telemetry["region_runs"][region] = telemetry["region_runs"].get(region, 0) + 1
    return (total / valid if valid else 0.0), valid, invalid


# --------------------------------------------------------------------------
# 知识库
# --------------------------------------------------------------------------

def load() -> dict:
    if os.path.exists(KNOWLEDGE):
        with open(KNOWLEDGE, encoding="utf-8") as f:
            k = json.load(f)
        # 战术知识区（战报驱动：根据对局结果持续更新优化，淘汰过时打法）
        if "tactics" not in k:
            k["tactics"] = {}
        return k
    return {"generation": 0, "trials": {}, "pair_scores": {}, "history": [], "best": None,
            "tactics": {}}


def save(k: dict) -> None:
    os.makedirs(os.path.dirname(KNOWLEDGE), exist_ok=True)
    with open(KNOWLEDGE, "w", encoding="utf-8") as f:
        json.dump(k, f, ensure_ascii=False, indent=1)


def ucb(k: dict, name: str, total_n: int) -> float:
    """UCB1：平衡"已知高分"与"尝试次数少"。"""
    t = k["trials"].get(name)
    if not t or t["n"] == 0:
        return 1e9                      # 没试过的优先试
    mean = t["sum"] / t["n"]
    return mean + 1.4 * math.sqrt(math.log(max(total_n, 2)) / t["n"])


def propose(k: dict, rng: random.Random, region: str = None) -> tuple:
    """生成下一套待测 build：50% 探索，50% 在精英基础上变异。
    region 给定时只从该副本实际可学的道纹中取（门禁修复后必需）。"""
    total_n = sum(t["n"] for t in k["trials"].values()) or 1
    CAND = learnable_candidates(region)
    best = k.get("best")
    if best and rng.random() < 0.5:
        learn = list(best["learn"])
        starter = best["starter"]
        # 变异：替换1~2个位置
        for _ in range(rng.randint(1, 2)):
            if learn:
                i = rng.randrange(len(learn))
                pool = [c for c in CAND if c not in learn and c != starter]
                if pool:
                    ranked = sorted(pool, key=lambda c: -ucb(k, c, total_n))
                    learn[i] = rng.choice(ranked[:8])
        if rng.random() < 0.25:
            starter = rng.choice(STARTERS)
        return starter, learn

    starter = rng.choice(STARTERS)
    pool = [c for c in CAND if c != starter]
    ranked = sorted(pool, key=lambda c: -ucb(k, c, total_n))
    head = ranked[:12]
    rng.shuffle(head)
    return starter, head[:BUILD_SIZE]


def update(k: dict, starter: str, learn: list, score: float) -> None:
    members = [starter] + list(learn)
    for m in members:
        t = k["trials"].setdefault(m, {"n": 0, "sum": 0.0})
        t["n"] += 1
        t["sum"] += score
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            key = "|".join(sorted((members[i], members[j])))
            p = k["pair_scores"].setdefault(key, {"n": 0, "sum": 0.0})
            p["n"] += 1
            p["sum"] += score
    if not k.get("best") or score > k["best"]["score"]:
        k["best"] = {"starter": starter, "learn": list(learn), "score": score}
    k["history"].append({"gen": k["generation"], "starter": starter,
                         "learn": list(learn), "score": round(score, 3)})
    k["total_games"] = k.get("total_games", 0) + k.get("_last_runs", 0)


# ============ 战术知识区（战报驱动） ============
# 用户要求：根据战报不断更新优化战术并淘汰过时战术打法。
# 每条战术 = {status: active/retired, n: 对局数, wins: 胜场, rule: 规则说明}
# 记录时机：play() 每局结束后调用 _record_tactic_perf，把"本局采用的关键战术"计入。
# 淘汰时机：对局数 ≥ RETIRE_MIN_N 且 胜率 < RETIRE_WINRATE → 标记 retired（过时打法）。
RETIRE_MIN_N = 30
RETIRE_WINRATE = 0.20


def _record_tactic_perf(k: dict, tactic_names: list, won: bool) -> None:
    """把一局结果记入指定战术（战报驱动的持续更新）。"""
    tactics = k.setdefault("tactics", {})
    for name in tactic_names:
        t = tactics.setdefault(name, {"status": "active", "n": 0, "wins": 0, "rule": ""})
        if t.get("status") == "retired":
            continue  # 已淘汰的战术不再累计（保留淘汰记录）
        t["n"] = t.get("n", 0) + 1
        if won:
            t["wins"] = t.get("wins", 0) + 1


def retire_stale_tactics(k: dict) -> list:
    """淘汰过时战术：对局数达标且胜率低于阈值 → retired。
    返回本轮新淘汰的战术名列表（供报告展示）。"""
    retired_now = []
    tactics = k.get("tactics", {})
    for name, t in tactics.items():
        if t.get("status") != "active":
            continue
        n = t.get("n", 0)
        wins = t.get("wins", 0)
        if n >= RETIRE_MIN_N and (wins / n) < RETIRE_WINRATE:
            t["status"] = "retired"
            t["retired_at_gen"] = k.get("generation", 0)
            t["retire_reason"] = f"胜率{wins / n:.0%} < 阈值{RETIRE_WINRATE:.0%}（{n}局）"
            retired_now.append(name)
    return retired_now


def report_tactics(k: dict) -> None:
    """战报战术区：当前生效战术与已淘汰战术。"""
    tactics = k.get("tactics", {})
    if not tactics:
        print("\n【战术知识库】(空：尚无战报驱动的战术记录)")
        return
    active = [(n, t) for n, t in tactics.items() if t.get("status") == "active"]
    retired = [(n, t) for n, t in tactics.items() if t.get("status") == "retired"]
    print(f"\n【战术知识库】生效 {len(active)} 条 / 已淘汰 {len(retired)} 条"
          f"（战报驱动：持续更新优化，胜率<{RETIRE_WINRATE:.0%}且≥{RETIRE_MIN_N}局淘汰）")
    for name, t in sorted(active, key=lambda kv: -kv[1].get("wins", 0) / max(1, kv[1].get("n", 1))):
        n, wins = t.get("n", 0), t.get("wins", 0)
        rate = f"{wins / n:.0%}" if n else "-"
        print(f"  ✅ {name:<12} 胜率{rate:>5} ({wins}/{n}局)")
        if t.get("rule"):
            print(f"      规则：{t['rule']}")
    for name, t in retired:
        print(f"  ⛔ {name:<12} 已淘汰：{t.get('retire_reason', '')}")


def synergies(k: dict, min_n: int = 2) -> list:
    """协同增益：pair 均分 − 两个单体均分的平均。>0 即 1+1>2。"""
    out = []
    for key, p in k["pair_scores"].items():
        if p["n"] < min_n:
            continue
        a, b = key.split("|")
        ta, tb = k["trials"].get(a), k["trials"].get(b)
        if not ta or not tb or ta["n"] == 0 or tb["n"] == 0:
            continue
        solo = (ta["sum"] / ta["n"] + tb["sum"] / tb["n"]) / 2
        out.append((p["sum"] / p["n"] - solo, a, b, p["n"], p["sum"] / p["n"]))
    out.sort(reverse=True)
    return out


def report_telemetry(k: dict) -> None:
    """真实运行数据：局外行动选择率、成功率、失败原因、无效数据。"""
    t = k.get("telemetry") or {}
    att = t.get("attempted", {})
    suc = t.get("succeeded", {})
    if not att:
        return
    total = sum(att.values())
    print(f"\n【局外行动真实选择率】(共 {total} 次决策，由AI按权重自主选择)")
    print(f"  {'行动':<6}{'选择次数':>8}{'选择率':>9}{'成功率':>9}")
    for act, n in sorted(att.items(), key=lambda kv: -kv[1]):
        ok = suc.get(act, 0)
        print(f"  {act:<6}{n:>8}{n/total*100:>8.1f}%{ok/n*100:>8.1f}%")

    fails = t.get("failed", {})
    if fails:
        print("\n【行动失败原因 Top8】(合法拒绝，非bug)")
        for k2, n in sorted(fails.items(), key=lambda kv: -kv[1])[:8]:
            print(f"  {n:>5}× {k2}")

    oc = t.get("outcomes")
    if oc:
        tot = oc["win"] + oc["loss"]
        if tot:
            print(f"\n【对局结果】有效 {tot} 局｜通关 {oc['win']}｜阵亡 {oc['loss']}"
                  f"｜总胜率 {oc['win']/tot*100:.1f}%｜平均通关 {oc['cleared_sum']/tot:.2f} 场")
    rr = t.get("region_runs")
    if rr:
        print("  副本分布：" + "、".join(f"{a}{b}局" for a, b in sorted(rr.items())))

    err = t.get("engine_error", {})
    inv = t.get("invalid_reasons", {})
    print(f"\n【数据有效性】无效对局 {k.get('invalid_games', 0)} 局"
          f"（引擎异常，已从统计中剔除）")
    if err:
        print("  引擎异常明细（这些是bug，需修复）：")
        for k2, n in sorted(err.items(), key=lambda kv: -kv[1])[:10]:
            print(f"    {n:>4}× {k2}")
    if inv and not err:
        for k2, n in sorted(inv.items(), key=lambda kv: -kv[1])[:5]:
            print(f"    {n:>4}× {k2}")
    if not err and not inv:
        print("  ✅ 本批次未出现任何引擎异常，全部数据有效")


def report(k: dict) -> None:
    print(f"已学习代数：{k['generation']}｜累计试验：{len(k['history'])} 套"
          f"｜有效对局：{k.get('total_games', 0)} 局"
          f"｜无效(bug) {k.get('invalid_games', 0)} 局")
    if k.get("best"):
        b = k["best"]
        print(f"\n★ 目前最优：初始【{b['starter']}】+ {b['learn']}   适应度 {b['score']:.2f}/10")

    ranked = sorted(((t["sum"] / t["n"], n, t["n"])
                     for n, t in k["trials"].items() if t["n"] > 0), reverse=True)
    print("\n【单道纹价值 Top12】(平均适应度 × 试验次数)")
    for v, n, cnt in ranked[:12]:
        print(f"  {n:<6}{v:6.2f}  ({cnt}次)")
    if len(ranked) > 12:
        print("  ...最低3个：", "、".join(f"{n}{v:.2f}" for v, n, _ in ranked[-3:]))

    report_telemetry(k)

    syn = synergies(k)
    print("\n【协同增益 Top10  —— 1+1>2 的组合】")
    if not syn:
        print("  （数据不足，需要更多代数）")
    for d, a, b, n, avg in syn[:10]:
        print(f"  {a}+{b:<6} 增益{d:+.2f}  组合均分{avg:.2f} ({n}次)")
    neg = [s for s in syn if s[0] < 0]
    if neg:
        print("\n【负协同 —— 互相拖累，应避免同时携带】")
        for d, a, b, n, avg in neg[-5:]:
            print(f"  {a}+{b:<6} 增益{d:+.2f} ({n}次)")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--generations", type=int, default=10)
    ap.add_argument("--runs", type=int, default=6, help="每套build评估局数")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--seed", type=int, default=0,
                    help="控制'提出哪套build'的采样随机性；0=每次运行都不同")
    ap.add_argument("--random-seeds", action="store_true",
                    help="每局用真随机种子与随机副本（推荐，避免过拟合到固定局面）")
    ap.add_argument("--spend", action="store_true",
                    help="七场局外花碎片提升战力（修行/附煞），用户裁定为正常玩法")
    a = ap.parse_args()

    if a.reset and os.path.exists(KNOWLEDGE):
        os.remove(KNOWLEDGE)
        print("已清空知识库")

    k = load()
    if a.report:
        report(k)
        return

    rng = random.Random(a.seed or None)
    tele = k.setdefault("telemetry", {})
    for g in range(a.generations):
        k["generation"] += 1
        starter, learn = propose(k, rng)
        score, valid, invalid = fitness(starter, learn, a.runs, k["generation"],
                                        random_seeds=a.random_seeds, rng=rng,
                                        telemetry=tele, spend_shards=a.spend)
        k["total_games"] = k.get("total_games", 0) + valid
        k["invalid_games"] = k.get("invalid_games", 0) + invalid
        if valid:                      # 全部无效的代不计入学习，避免污染权重
            update(k, starter, learn, score)
        star = " ★新最优" if k.get("best") and k["best"]["score"] == score else ""
        bad = f"  [无效{invalid}]" if invalid else ""
        print(f"第{k['generation']:>3}代  【{starter}】{'+'.join(learn):<28} → {score:5.2f}{star}{bad}")
        save(k)

    print()
    report(k)
    save(k)


if __name__ == "__main__":
    main()


def round_start_relic_choices(e) -> dict:
    """构建 [回始] 显式遗物选择：回锋刀需显式敌方目标；血契/余火印按情形主动使用。

    委托 sim/optional_actions（可以不用但不能不让用：血厚且法力有缺口时就换法力）。
    """
    from sim.optional_actions import round_start_relic_choices as _impl
    return _impl(e)


def start_battle_with_artifacts(e):
    """战始 + 战始窗口法器（共心环/黑金名片）。返回 (battle_start结果, 法器结果列表)。"""
    from sim.optional_actions import start_battle
    return start_battle(e)


def start_round_with_artifacts(e):
    """回始窗口法器（罪业金库/烬翼）+ round_start。返回 (round_start结果, 法器结果列表)。"""
    from sim.optional_actions import start_round
    return start_round(e)
