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
import re
import sys
from collections import defaultdict, Counter

# sys.path 必须先于 tests/engine 导入设置：直接 `python3 sim/build_learner.py`
# 运行时 CWD 未必含仓库根，否则 ModuleNotFoundError（2026-08-22 入口地雷治理）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.setup_support import choose_discovered_initial_daowen
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



from sim.monster_targets import pick_monster_daowen_target  # noqa: E402
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
    blocked = monster is not None and getattr(monster, "no_damage_rounds", 0) >= 2
    return pick_monster_daowen_option(cands, player_low=player_low,
                                      monster_low=monster_low,
                                      blocked=blocked)


def _pick_monster_daowen_avoiding_wave(engine, actor):
    """波及重试专用（2026-08-22）：波及的X个闪避目标冻结在 prepare 快照
    （combat.py:4081），阶段内任一目标死亡则该道纹同 token 下永远无法结算；
    报错文案"请重新prepare_monster_phase"具误导性（pending 有效期内 prepare
    会被拒，api.py:854）——正确路径是按 api.py:3598 契约注释用同 token 换选
    非波及备选道纹重交（dodge_submission=="per_target" 的才排除）。"""
    opts = actor["daowen_options"]
    kept = actor["daowen_options"]
    actor["daowen_options"] = [o for o in opts if o.get("dodge_submission") != "per_target"]
    try:
        picked = _pick_monster_daowen(engine, actor)
    finally:
        actor["daowen_options"] = kept
    return picked


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
                # 自由控X：攻击步骤尽量大X（杀伐/血债等），自保步骤留1
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


def _spell_step_mana_cost(engine, daowen_name: str, x: int, holder, target) -> int:
    """单步法术在某X下的真实法力消耗（与 combat.py:3396 校验口径一致；
    非"消耗"类代价记0）。异常时按0处理（交给引擎校验终裁）。"""
    from engine.daowen import DaoWenEngine
    try:
        calc = DaoWenEngine.resolve(daowen_name, x, target=target, caster=holder)
    except Exception:
        return 0
    return int(calc.get("cost", 0) or 0) if calc.get("cost_type") == "消耗" else 0


def _live_spell_choices(engine, actor_ref, target_ref, use, banned=()):
    """现场重算受击方反应法术提交（2026-08-22 防漂移主路径）。

    combat.py:3362 校验的是结算时**实时**资格集，而 prepare 快照可能已被
    同阶段早前命中消耗——不再按快照提交，改为 prepare_spell_reactions 现场重算。
    use=True 只在持有者本阶段**最后一击**传入（早击一律弃权），于是同阶段永不
    发生中途消耗，资格集与提交时刻恒一致（此前的"镜像引擎消耗"方案对闪避
    概率分支无解，已放弃）。
    banned：重试链判定的"提交法力不足"法术名集合——该法术直接弃权。

    步骤X记账（2026-08-23 修复"提交的法力不足"无效局）：X不再按1法力/步的
    朴素模型分配，而是按引擎同口径逐步精确核算——给后续步骤预留其x=1成本，
    本步X取不超支的最大值（杀伐类 消耗2X 的旧模型必超支，冒烟批2/60踩中）。
    """
    refs = engine.combat._combat_entity_refs()
    holder = refs.get(target_ref)
    attacker = refs.get(actor_ref)
    if holder is None or attacker is None:
        return {"before": {}, "after": {}}
    live = engine.combat.prepare_spell_reactions(holder, attacker)
    out = {}
    # 钱包制（2026-08-23 second fix）：同一击可同时声明多个法术（before/after
    # 各自资格集），每个法术若都按满额法力预算会合计超支——提交/结算校验按
    # 共享法力池逐步扣减（combat.py:3379+/3515）。此处与引擎同口径：法术按
    # 声明顺序共用一只钱包，扣完即止，付不起基线(x=1)的法术直接弃权。
    wallet = holder.current_mana
    for timing in ("before", "after"):
        out[timing] = {}
        for spell in live.get(timing, []) or []:
            name = spell["spell_name"]
            steps = spell.get("steps", []) or []
            if use and wallet >= 1 and name not in banned and steps:
                # 各步骤x=1的基线成本（用于给后续步骤预留）
                base_costs = [
                    _spell_step_mana_cost(engine, st.get("daowen", ""), 1, holder,
                                          refs.get(st.get("target_ref", "")) or holder)
                    for st in steps]
                if sum(base_costs) > wallet:
                    # 全部基线(x=1)都付不起的法术直接弃权，不给提交校验留死路
                    out[timing][name] = {"use": False}
                    continue
                cycle = []
                remaining = wallet
                for index, st in enumerate(steps):
                    reserve = sum(base_costs[index + 1:])       # 后续步骤x=1的预留
                    budget = max(0, remaining - reserve)
                    is_self = st.get("target_ref") == target_ref
                    target_ent = refs.get(st.get("target_ref", "")) or holder
                    x = 1
                    if not is_self:
                        # 取不透支预算的最大X（成本随X单调不减，超支即停）
                        for cand in range(2, budget + 1):
                            if _spell_step_mana_cost(engine, st.get("daowen", ""),
                                                     cand, holder, target_ent) <= budget:
                                x = cand
                            else:
                                break
                    step_cost = _spell_step_mana_cost(engine, st.get("daowen", ""),
                                                      x, holder, target_ent)
                    entry = {"x": x, "target_ref": st.get("target_ref")}
                    if not is_self:
                        entry["dodge"] = False
                    cycle.append(entry)
                    remaining -= step_cost
                out[timing][name] = {"use": True, "cycles": [cycle]}
                wallet = remaining   # 钱包流转给下一个法术（与引擎共享池同口径）
            else:
                out[timing][name] = {"use": False}
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
# 2026-08-22 学习顺序实验裁定：第4~5学习位在平均2.8场通关的局里永远学不到
# （同种子配对全系列0/200），UCB曾在该幻影维度空耗约1.1万局。且学习顺序本身
# 显著影响生存（杀伐优先 vs 再生优先 配对38/86）。故提案压缩为3纹，
# 并按UCB强度排序学习（强者先学），探索只在候选池内做选择。
BUILD_SIZE = 3          # 每套 build 学习的道纹数量（有效维度；第4位以后学不到）

# 卡死哨兵（2026-08-22 假死两小时根因治理）：局外 while 精力循环在"一切行动均被
# 门禁拒绝且兜底也被拒"时精力不退=原地死循环（实测20万步）。连续 STALL_LIMIT 步
# 精力不退即回收为无效局，绝不允许挂死进程。
STALL_LIMIT = 5


def _resolve_pending_choices(e) -> None:
    """清一切等待结算池（2026-08-22 门禁治理）。

    各 pending 池相互门禁：非空时绝大多数行动被 api.py:824-845 拒绝并退还精力，
    驱动不清理=原地死循环/战斗无法结算。处理：遗物/道具/雇员道纹/煞气取首项；
    【救赎】固定选【无视】（确定性口径同 combo_loop_audit.py:819——接纳会产生
    朋友盟友，污染适应度横向比较）。事件链单趟上限20，防无限嵌套。
    """
    for _ in range(20):
        acted = False
        st = e.state
        if st.pending_redemption:
            e.execute_action("resolve_redemption", {"option": "无视"})
            acted = True
        if st.pending_relic_choices:
            e.execute_action("choose_discovered_relic",
                             {"relic_name": st.pending_relic_choices[0]})
            acted = True
        if st.pending_item_choices:
            e.execute_action("choose_discovered_item",
                             {"item_name": st.pending_item_choices[0]})
            acted = True
        if st.pending_daowen_choices:
            for emp_name, choices in list(st.pending_daowen_choices.items()):
                e.execute_action("choose_hired_daowen",
                                 {"name": emp_name, "daowen": choices[0]})
            acted = True
        if getattr(st, "pending_sha_qi_choices", None):
            held = next(iter(st.player.dao_wen), None) if st.player else None
            if held:
                e.execute_action("choose_sha_qi", {
                    "sha_qi": st.pending_sha_qi_choices[0],
                    "daowen_name": held})
            acted = True
        if not acted:
            return


# --------------------------------------------------------------------------
# 一局轮回
# --------------------------------------------------------------------------

def _drive_plight_monsters(engine, telemetry: dict = None) -> None:
    """困境驱动（DM裁定2026-08-23③：模拟器怪物执行准则#3强制二选一）。

    每回合怪物阶段开始前：对所有处于困境的**怪物**——
      1. 进化优先：有借用票（轮回者持有且自身未持有的道纹）且门票异变5X不
         必触发崩解（max_x_by_mutation≥1）→ declare_evolution 发动【原初X】，
         借轮回者X值最高的道纹，X=min(异变预算, 3)；
      2. 否则逃跑（无票/必崩解）：统一【离场】depart_battle("逃跑")，不视为
         击杀、不产生碎片奖励，alt_victory 扫描自动记为「逃跑」类离场。
    死斗不驱动（道规禁逃）；决斗对手是轮回者不是怪物（entity_type 门禁）。
    """
    from engine.combat import CombatEngine  # noqa: F401  (类型提示用)
    combat = engine.combat
    if getattr(engine.state, "in_final_duel", False):
        return
    stats = telemetry.setdefault("plight", {}) if telemetry is not None else None
    for opt in combat.get_plight_evolution_options():
        monster = next((e for e in engine.state.enemies
                        if e.name == opt.get("monster") and e.is_alive), None)
        if monster is None or monster.entity_type != "怪物":
            continue
        if id(monster) in combat._monster_evolved:
            continue
        borrowable = list(opt.get("borrowable_daowen") or [])
        max_x = int(opt.get("max_x_by_mutation") or 0)
        if borrowable and max_x >= 1:
            player = engine.state.player
            pick = max(borrowable,
                       key=lambda d: (player.dao_wen[d].x_value
                                      if player is not None and d in player.dao_wen else 0, d))
            res = engine.execute_action("declare_evolution", {
                "monster": monster.name, "daowen": pick,
                "x": max(1, min(max_x, 3))})
            key = "evolve" if res.get("success") else "evolve_failed"
            if stats is not None:
                stats[key] = stats.get(key, 0) + 1
        else:
            combat._monster_evolved.add(id(monster))
            combat._remove_from_combat(monster, "逃跑")
            if stats is not None:
                stats["escape"] = stats.get("escape", 0) + 1


def _resolve_monster_turn(engine):
    """怪物阶段驱动（2026-08-22 重建：现场资格集 + 有界重试链）。

      1. 反应法术按 prepare_spell_reactions 现场重算提交，且只在持有者本阶段
         最后一击声明 use=true（早击弃权 → 阶段内零消耗零漂移）；
      2. 命中数漂移（combat.py:4543 按实时 attack_count 校验；同阶段怪道纹全局
         加盖可改命中数——2026-08-17疯狂裁定）：解析报错 `(名)每个攻击出手必须
         提交N次命中选择` 按 actor 定向覆盖重试（重试必须保留道纹选择，
         combat.py:4353/4508 有候选时道纹提交是强制的）；
      3. 波及目标阶段内失效：同 token 换非波及道纹重交一次；
      4. 全部重试有界（len(actors)+2），不收敛才把失败交上层（判无效局）。
    """
    import re as _re
    prepared = engine.execute_action("prepare_monster_phase", {})
    if not prepared.get("success"):
        return prepared
    from engine.ai_tactics import choose_dodge, choose_attack_target

    refs_all = engine.combat._combat_entity_refs()
    hit_overrides = {}     # actor_ref → 强制命中数/出手
    spell_overrides = {}   # 阶段内资格漂移：报错给出的精确资格名表（timing → [spell]）
    no_dodge_refs = set()  # 报错"速度不足不能闪避"的受击 ref：重交一律不闪避
    banned_spells = set()  # 报错"提交的法力不足"的法术：后续一律弃权
    wave_retry = False
    attempts = 0
    max_attempts = len(prepared["result"]["actors"]) + 2

    while True:
        actors = prepared["result"]["actors"]
        # 第一遍：决定每 actor 的目标与命中数，并统计每名受击持有者的总命中数
        per_actor = []
        hold_counts = Counter()
        for actor in actors:
            hits_n = hit_overrides.get(actor["actor_ref"], actor["base_hits_per_attack"])
            target_ref = choose_attack_target(actor["attack_target_options"], refs_all)
            per_actor.append((actor, hits_n, target_ref))
            hold_counts[target_ref] += actor["base_attack_actions"] * hits_n

        choices = []
        dodge_budget = 0
        for actor, hits_n, target_ref in per_actor:
            dao = None
            if actor["daowen_options"]:
                option = (_pick_monster_daowen_avoiding_wave(engine, actor)
                          if wave_retry else _pick_monster_daowen(engine, actor))
                dao = {"name": option["name"], "dodge": False, "blood_shadow": False,
                       "trigger_spell_choices": {holder: {sp["spell_name"]: {"use": False} for sp in spells}
                                                 for holder, spells in option.get("trigger_spell_options", {}).items()}}
                if option["requires_target"]:
                    dao["target_ref"] = pick_monster_daowen_target(engine, actor["actor_ref"], option)
                if option["dodge_submission"] == "per_target":
                    from sim.monster_targets import pick_wave_dodge_targets
                    dao["dodge_targets"] = pick_wave_dodge_targets(option)
                if option["resolves_as"] == "变形":
                    enemy_index = int(actor["actor_ref"].split(":", 1)[1])
                    hits_n = hit_overrides.get(actor["actor_ref"],
                                               engine.state.enemies[enemy_index].attack_power)
            monster = refs_all.get(actor["actor_ref"])
            per_hit = monster.attack_power if monster is not None else 0
            target_option = next((o for o in actor["attack_target_options"]
                                  if o["ref"] == target_ref), None)   # 无合法攻击目标时为None
            attacks = []
            for _ in range(actor["base_attack_actions"]):
                hits = []
                for _ in range(hits_n):
                    want_dodge = (target_ref not in no_dodge_refs
                                  and choose_dodge(engine, per_hit, budget_used=dodge_budget))
                    if want_dodge:
                        dodge_budget += 1
                    # 持有者本阶段最后一击才声明发动；其余现场弃权
                    hold_counts[target_ref] -= 1
                    is_last_hit = hold_counts[target_ref] == 0
                    if target_option is None:
                        spell_choices = {"before": {}, "after": {}}
                    elif hold_counts[target_ref] >= 0:
                        spell_choices = _live_spell_choices(
                            engine, actor["actor_ref"], target_ref, use=is_last_hit,
                            banned=banned_spells)
                        # 资格漂移重试：按报错给出的精确名表整体覆盖对应 timing
                        # （早击弃权/末击声明策略与漂移解耦——override 一律弃权）
                        if spell_overrides and refs_all.get(target_ref) is engine.state.player:
                            for _tk, _names in spell_overrides.items():
                                spell_choices[_tk] = {nm: {"use": False} for nm in _names}
                    else:
                        spell_choices = _decline_spells(target_option)
                    hit = {"target_ref": target_ref, "dodge": want_dodge,
                           "blood_shadow": False, "spell_choices": spell_choices}
                    # 回锋刀：闪避触发反击必须显式提交合法敌方目标
                    if want_dodge and target_option and target_option.get("dodge_relic_target_options"):
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
        attempts += 1
        if attempts > max_attempts:
            return result
        err = str(result.get("error", ""))
        # 命中数/出手数漂移：按报错给出的实时 N 定向覆盖该 actor 后重试
        m = _re.search(r"(.+?)每个攻击出手必须提交(\d+)次命中选择", err)
        if m:
            name, n = m.group(1), int(m.group(2))
            ref = next((a["actor_ref"] for a in actors
                        if refs_all.get(a["actor_ref"]) is not None
                        and refs_all[a["actor_ref"]].name == name), None)
            if ref:
                hit_overrides[ref] = n
                continue
        # 波及目标阶段内失效：同 token 换非波及道纹重交一次（re-prepare 会被门禁拒）
        if "波及" in err and not wave_retry:
            wave_retry = True
            continue
        # 反应法术资格集阶段内漂移（同阶段早前命中消耗了持有者法力/冷却道纹）：
        # 报错自带结算时刻的精确资格名表，按名表整体覆盖该 timing 后重试。
        m = _re.search(r"spell_choices\.(before|after)必须逐一覆盖\[(.*?)\]", err)
        if m:
            key = m.group(1)
            names = [s.strip().strip("'\"「」【】") for s in m.group(2).split(",") if s.strip()]
            if spell_overrides.get(key) != names:
                spell_overrides[key] = names
                continue
        # 法术提交/结算法力不足（阶段内法力被同阶段早前结算抽干/多法术共享池
        # 超支）：该法术本场后续一律弃权（use=False 是合法提交），立即重试。
        m = _re.search(r"法术(?:【)?([^】提交结算]+?)(?:】)?(?:提交|结算)时?的?法力不足", err)
        if m and m.group(1) not in banned_spells:
            banned_spells.add(m.group(1))
            continue
        # 闪避预算漂移：结算时刻速度被同阶段早前结算压到 0，该目标重交一律不闪避
        m = _re.search(r"(.+?)速度不足，不能选择闪避", err)
        if m:
            tref = next((r for r, ent in refs_all.items()
                         if getattr(ent, "name", None) == m.group(1)), None)
            if tref and tref not in no_dodge_refs:
                no_dodge_refs.add(tref)
                continue
        # spell_choices 资格集漂移等其余失败：主路径已是现场重算，重试意义有限，
        # 再走一轮（live 重算会带上结算后的最新资格集）
        if attempts < max_attempts:
            continue
        return result


def _resolve_pending_event(engine):
    """平衡模拟器显式选择事件选项；不替正式玩家作选择。

    优先级：拒绝/离开类选项 → 其余选项按原顺序逐项尝试，取第一个能通过
    引擎代价校验的（失败选项不改状态，可安全重试）；全部失败才报错，
    避免"只有付不起代价的选项"被误记为引擎异常。事件链单趟上限20。
    """
    chain = 0
    while engine.event_pool.current is not None:
        chain += 1
        if chain > 20:
            return {"success": False, "error": "事件链超过20层，按异常回收防挂死"}
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


# --------------------------------------------------------------------------
# 行为探针（2026-08-22 DM诉求「提高胜率的行为记录进知识库进步」）
# --------------------------------------------------------------------------

# 行为标签 → 可反哺的局外行动（备齐反应法术等不对应权重表行动）
BEHAVIOR_TO_POLICY = {
    "修行提战力": "修行", "修行提战力·高档": "修行", "先学后打": "学习",
    "休整保血": "休整", "残血休整": "休整", "共鸣强化": "共鸣",
    "附煞强化": "附煞", "探索寻机": "探索", "领悟残韵": "领悟",
    "雇佣支援": "雇佣", "炼心固本": "炼心", "维修续用": "维修",
}
BEHAVIOR_RULES = {
    "修行提战力": "局外修行把精力换属性点", "修行提战力·高档": "高档修行(tier≥2)加速成长",
    "先学后打": "第1~2场前学道纹早成型", "休整保血": "休整回血防暴毙",
    "残血休整": "血线≤30%时休整保命", "共鸣强化": "共鸣提升道纹配合",
    "附煞强化": "乱葬岗附煞加效", "探索寻机": "探索事件换资源",
    "领悟残韵": "领悟新残韵", "雇佣支援": "雇佣帮手分压", "炼心固本": "龙心谷炼心",
    "备齐反应法术": "学习先发制人/生生不息/后发制人", "维修续用": "维修回复消耗品耐久",
}


def _tag_behavior(behaviors, act, params, e, battle_no):
    """局外行动成功时打行为标签（统一由 play() 包装层收口进知识库）。"""
    if behaviors is None:
        return
    if act == "修行":
        behaviors.append("修行提战力")
        if (params or {}).get("tier", 1) >= 2:
            behaviors.append("修行提战力·高档")
    elif act == "学习":
        if battle_no <= 2:
            behaviors.append("先学后打")
    elif act == "休整":
        behaviors.append("休整保血")
        p = e.state.player
        if p and p.current_hp <= p.blood_limit * 0.3:
            behaviors.append("残血休整")
    elif act == "共鸣":
        behaviors.append("共鸣强化")
    elif act == "附煞":
        behaviors.append("附煞强化")
    elif act == "探索":
        behaviors.append("探索寻机")
    elif act == "领悟":
        behaviors.append("领悟残韵")
    elif act == "雇佣":
        behaviors.append("雇佣支援")
    elif act == "炼心":
        behaviors.append("炼心固本")
    elif act == "维修":
        behaviors.append("维修续用")


def _record_behaviors(telemetry: dict, behaviors: list, r: dict) -> None:
    """把本局用上的行为按"使用该行为的局的胜负/通关"累计（幸存者偏差注意：
    走得远的局才有机会休整/共鸣——只作温和权重纠偏，不作因果结论）。"""
    stats = telemetry.setdefault("behavior_stats", {})
    for name in sorted(set(behaviors)):
        s = stats.setdefault(name, {"n": 0, "wins": 0, "cleared_sum": 0})
        s["n"] += 1
        s["wins"] += 1 if r.get("won") else 0
        s["cleared_sum"] += r.get("cleared", 0)


def learned_policy(k: dict) -> dict:
    """用 behavior_stats 反哺局外行动权重（闭环：记录→胜率相关→改变后续采样）。

    乘数 = clamp(0.7, 1.4, 1 + 0.25×(行为局均通关 − 全体基线均通关))，min_n=40；
    同一行动被多个行为映射时取样本量最大者。结果存 k["policy_learn"]。
    数据不足返回 None（上层退回 DEFAULT_POLICY）。
    """
    t = k.get("telemetry") or {}
    oc = t.get("outcomes") or {}
    bs = t.get("behavior_stats") or {}
    total = oc.get("win", 0) + oc.get("loss", 0)
    if not total or not bs:
        return None
    base = oc.get("cleared_sum", 0) / total
    best_for_act = {}
    for name, s in bs.items():
        act = BEHAVIOR_TO_POLICY.get(name)
        n = s.get("n", 0)
        if not act or n < 40:
            continue
        avg = s.get("cleared_sum", 0) / n
        m = max(0.7, min(1.4, 1 + 0.25 * (avg - base)))
        if act not in best_for_act or n > best_for_act[act][1]:
            best_for_act[act] = (m, n)
    if not best_for_act:
        return None
    multipliers = {act: round(m, 3) for act, (m, _) in best_for_act.items()}
    k["policy_learn"] = {"base_cleared": round(base, 3), "multipliers": multipliers}
    policy = dict(DEFAULT_POLICY)
    for act, m in multipliers.items():
        if act in policy:
            policy[act] = max(1, round(policy[act] * m))
    return policy


def _postmortem(e, cleared: int, todo: list, killer_hint: str = "") -> dict:
    """战败复盘（2026-08-22 应DM要求新增）：败局不再只留 cleared/won 两个数，
    从引擎事件流（combat_events）里还原"死在谁手里、手里还剩什么"。
    产出：{杀手, 带药, 剩余碎片, 未学纹, 满蓝} —— 全部喂回知识库。"""
    st = e.state
    p = st.player
    pname = p.name if p else ""
    killer = killer_hint
    if not killer:
        # 从事件流反向找玩家死亡前最近一次对其造成伤害的来源
        for ev in reversed(st.combat_events):
            if ev.target_name == pname and getattr(ev, "event_type", None) is not None \
                    and "damage" in str(getattr(ev, "event_type", "")).lower():
                killer = ev.actor_name
                break
        if not killer:
            alive = [x.name for x in st.enemies if getattr(x, "is_alive", False)]
            killer = alive[0] if alive else ""
    unused_items = [c.name for c in st.consumables if c.current_uses > 0]
    max_mana = getattr(p, "max_mana", 0) if p else 0
    return {
        "battle": cleared + 1,                       # 阵亡场次（第几战）
        "killer": killer or "未知",
        "unused_items": unused_items,                # 带药阵亡：消耗品一次没用
        "shards": st.shards,                         # 碎片未花
        "todo_left": list(todo),                     # 没学完的死前欠账
        "full_mana": bool(p and max_mana and p.current_mana >= max_mana * 0.8),
    }


def _record_postmortem(telemetry: dict, pm: dict) -> None:
    pm_all = telemetry.setdefault("postmortem", {
        "deaths": 0, "death_battle": {}, "killers": {},
        "带药阵亡": 0, "碎片未花": 0, "满蓝阵亡": 0})
    pm_all["deaths"] += 1
    b = str(pm["battle"])
    pm_all["death_battle"][b] = pm_all["death_battle"].get(b, 0) + 1
    k = pm["killer"]
    pm_all["killers"][k] = pm_all["killers"].get(k, 0) + 1
    if pm["unused_items"]:
        pm_all["带药阵亡"] += 1
    if pm["shards"] >= 15:
        pm_all["碎片未花"] += 1
    if pm["full_mana"]:
        pm_all["满蓝阵亡"] += 1


# 非伤害清场路径（凡庸/癌变/雕塑/封印/还债/救赎/逃跑）——应DM 2026-08-22质疑
# "为什么只学杀伐"新增的全局统计：胜利≠打死，离场与被动命零同样算赢。
# 离场不发 CombatEvent（models.depart_battle 只打标）——必须由"实体旗标差集"统计；
# 命零类（凡庸/癌变）才有 ENTITY_DIED 事件（subtype 分别为 mediocrity/heal_threshold）。
_DEATH_SUBTYPES = {"mediocrity": "凡庸", "heal_threshold": "癌变"}


def _alt_victory_scan(e, ev_mark: int, prev_gone: set, telemetry: dict) -> set:
    """扫描本场战斗：命零类走事件流；离场类走实体旗标差集。返回新的 prev_gone。"""
    if telemetry is None:
        return prev_gone
    from engine.combat_events import CombatEventType
    av = telemetry.setdefault("alt_victory", {})
    pname = e.state.player.name if e.state.player else ""
    # 1) 命零类（凡庸/癌变在事件流里有 ctx.subtype；其余死亡=伤害击杀）
    for ev in e.state.combat_events[ev_mark:]:
        if ev.event_type != CombatEventType.ENTITY_DIED or ev.target_name == pname:
            continue
        sub = (ev.ctx or {}).get("subtype", "")
        cause = _DEATH_SUBTYPES.get(sub, "伤害击杀")
        av[cause] = av.get(cause, 0) + 1
    # 2) 离场类（depart_battle 只打标不发事件）：差集统计本战新离场
    monsters = list(e.state.enemies) + list(getattr(e.state, "dead_monsters", []))
    now_gone = set()
    for m in monsters:
        if getattr(m, "is_alive", True):
            continue
        key = id(m)
        if getattr(m, "is_proliferated", False):
            now_gone.add((key, "癌变"))
        elif getattr(m, "is_sculptured", False):
            now_gone.add((key, "雕塑"))
        elif getattr(m, "is_debt_bound", False):
            now_gone.add((key, "还债"))
        elif getattr(m, "is_departed", False):
            now_gone.add((key, getattr(m, "departure_reason", "") or "离场"))
    for _key, cause in now_gone - prev_gone:
        if cause and cause != "凡庸":  # 凡庸命零已在事件流计数
            av[cause] = av.get(cause, 0) + 1
    return now_gone


def play(starter: str, learn: list, region: str, seed=None, battles: int = 7,
         rng: random.Random = None, policy: dict = None, telemetry: dict = None,
         spend_shards: bool = False, spell_plan: list = None,
         behaviors: list = None, attrs: dict = None,
         resonance: str = "反转", relic_policy: str = "skip_optional",
         ai_cls=None) -> dict:
    """跑一局轮回（行为探针包装：本局用到了哪些行为，统一收口进知识库）。"""
    behaviors = behaviors if behaviors is not None else []
    r = _play(starter, learn, region, seed, battles=battles, rng=rng, policy=policy,
              telemetry=telemetry, spend_shards=spend_shards, spell_plan=spell_plan,
              behaviors=behaviors, attrs=attrs, resonance=resonance,
              relic_policy=relic_policy, ai_cls=ai_cls)
    if telemetry is not None and not r.get("invalid"):
        _record_behaviors(telemetry, behaviors, r)
        if r.get("pm"):
            _record_postmortem(telemetry, r["pm"])
    return r


def _play(starter: str, learn: list, region: str, seed=None, battles: int = 7,
          rng: random.Random = None, policy: dict = None, telemetry: dict = None,
          spend_shards: bool = False, spell_plan: list = None,
          behaviors: list = None, attrs: dict = None,
          resonance: str = "反转", relic_policy: str = "skip_optional",
          ai_cls=None) -> dict:
    """跑一局轮回。seed=None 时引擎使用真随机源。

    policy: 局外行动权重 {行动名: 权重}，AI 按权重随机挑选可用行动。
            这样"选择率"是 AI 自己选出来的，而不是脚本写死的。
    telemetry: 传入则累计真实统计（行动选择/成功/失败原因/异常）。
    spend_shards: 七场局外把碎片尽量花成战力（高阶修行/附煞/共鸣）——用户裁定
            "不花碎片怎么快速提高战力"。False=旧行为，True=新玩法。
    behaviors: 传入 list 则把本局用到的行为标签追加进去（上层统一记 knowledge）。
    attrs: 初始属性分配 {blood_points, speed_points, mana_points}（2026-08-22 加点
            扫描后默认 6/8/11；此前 10/8/7 硬编码、1.7万局从未被探索）。
    resonance/relic_policy/ai_cls: 开局维度清扫与战术实验的注入点（默认=历史行为）。

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
    # 默认初始加点（2026-08-22 加点扫描实验结论，三副本同种子配对验证：
    # 扭曲都市 优87/差21、罪孽都市 92/14、龙心谷 62/5；均通关×1.6~1.9、
    # 第1战死亡率减半）。机制：自由控X下法限=每轮道纹出手次数上限，蓝是稀缺
    # 资源；血限对早期生存几乎无贡献（血牛15/5/5 反降38%）。4/8/13 不更优。
    attrs = attrs or {"blood_points": 6, "speed_points": 8, "mana_points": 11}
    e.execute_action("setup_attributes", {"name": "贾凡", **attrs})
    chosen = choose_discovered_initial_daowen(e, prefer=starter)
    if not chosen.get("success"):
        raise ValueError(chosen.get("error", "开局发现选择失败"))
    actual_starter = chosen["picked"]
    e.execute_action("setup_choose_resonance", {"resonance_type": resonance})
    setup = e.execute_action("setup_choose_region", {"region": region})
    optional_relics = {"折速法印", "三相残韵盘"}
    relic_choices = setup["result"]["relic_choices"]
    if relic_policy == "prefer_optional":  # 扫描实验：主动选可选遗物
        starter_relic = next((n for n in relic_choices if n in optional_relics),
                             relic_choices[0])
    else:
        starter_relic = next((n for n in relic_choices if n not in optional_relics),
                             relic_choices[0])
    e.execute_action("choose_discovered_relic", {"relic_name": starter_relic})

    ai_cls = ai_cls or TacticalAI
    ai = ai_cls(e)
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

    prev_gone = set()  # 离场旗标差集：跨战斗滚动（dead_monsters 跨战累积）
    for b in range(1, battles + 1):
        stalls = 0
        ev_mark = 0  # 战斗事件水位线（用于战后统计非伤害胜利路径）
        while e.state.energy > 0:
            _resolve_pending_choices(e)   # 先清门禁，否则一切行动被拒=原地死循环
            before = e.state.energy
            # 花碎片→战力（spend_shards）：与学习并行——每场优先修行1次保证法限成长，
            # 其余精力学道纹。
            if spend_shards:
                p = e.state.player
                if p and e.state.shards >= 35:
                    r = e.execute_action("pre_battle_action", {
                        "sub_action": "修行", "tier": 3,
                        "allocations": {"speed_points": 0, "mana_points": 3}})
                    if r.get("success"):
                        _tag_behavior(behaviors, "修行", {"tier": 3}, e, b)
                        continue
                if p and e.state.shards >= 25 and e.state.current_region == "乱葬岗":
                    held = next(iter(p.dao_wen), actual_starter)
                    r = e.execute_action("pre_battle_action", {
                        "sub_action": "附煞", "mode": "选择", "sha_qi": "冥煞", "daowen_name": held})
                    if r.get("success"):
                        _tag_behavior(behaviors, "附煞", {}, e, b)
                        continue
                if p and e.state.shards >= 15 and todo:
                    r = e.execute_action("pre_battle_action", {
                        "sub_action": "修行", "tier": 2,
                        "allocations": {"speed_points": 0, "mana_points": 2}})
                    if r.get("success"):
                        _tag_behavior(behaviors, "修行", {"tier": 2}, e, b)
                        continue
            # 学法术：已有对应道纹且没学过的先学（免费1精力）。
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
                    if behaviors is not None:
                        behaviors.append("备齐反应法术")
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
                _tag_behavior(behaviors, act, params, e, b)
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
                    return {"cleared": cleared, "won": False, "invalid": False,
                            "pm": _postmortem(e, cleared, todo)}
            else:
                record("failed", act, str(r.get("error"))[:60])
                # 失败必须退还精力，否则会死循环；引擎已退还，这里兜底防死锁
                if e.state.energy >= before:
                    e.execute_action("pre_battle_action",
                                     {"sub_action": "修行", "tier": 1, "to": "mana"})
            # 卡死哨兵：连续 STALL_LIMIT 步精力不退（门禁未清/兜底被拒），说明
            # 存在驱动解不开的语义门禁——回收为无效局，绝不挂死进程。
            if e.state.energy >= before:
                stalls += 1
                if stalls >= STALL_LIMIT:
                    return {"cleared": cleared, "won": False, "invalid": True,
                            "reason": f"pre_battle死锁：连续{stalls}步精力不退"
                                      f"（能量{e.state.energy}，按无效局回收）"}
            else:
                stalls = 0

        # 共鸣/事件可能在开局后继续获得可选战始遗物；按当前持有列表逐件显式决策
        # （可以不用但不能不让用：折速法印换法力/三相残韵盘/猩红果实/苍白之花按情形发动）。
        _resolve_pending_choices(e)   # 上一场遗留门禁（含战后救赎）先清，再开战
        ev_mark = len(e.state.combat_events)
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
            # 困境驱动（DM裁定2026-08-23③）：强制困境怪进化/逃跑二选一
            _drive_plight_monsters(e, telemetry)
            if not [x for x in e.state.enemies if x.is_alive]:
                break   # 困境怪全逃跑=清场（不视为击杀）
            mp = _resolve_monster_turn(e)
            if not mp.get("success"):
                return {"cleared": cleared, "won": False, "invalid": True,
                        "reason": f"monster_phase: {mp.get('error')}"}
            if mp["result"].get("player_dead"):
                break
            e.execute_action("round_end", {})
            # [回终]结算可能把残血怪压入救赎等待队列——不清理会门禁下一回合
            # round_start（"必须先结算【救赎】"按无效局回收，2026-08-23 冒烟）。
            _resolve_pending_choices(e)

        # 先扫描再判负：阵亡/败北的战斗同样有移除产出（2026-08-23 审计发现
        # 救赎触发→离场有2倍差距，根因=败场提前 return 跳过扫描、把突围战绩丢了）。
        prev_gone = _alt_victory_scan(e, ev_mark, prev_gone, telemetry)
        if not e.state.player or not e.state.player.is_alive:
            return {"cleared": cleared, "won": False, "invalid": False,
                    "pm": _postmortem(e, cleared, todo)}
        if [x for x in e.state.enemies if x.is_alive]:
            return {"cleared": cleared, "won": False, "invalid": False,
                    "pm": _postmortem(e, cleared, todo)}
        _resolve_pending_choices(e)   # 战后【救赎】等待结算门禁（api.py:824）会挡住 battle_end
        ended = e.execute_action("battle_end", {})
        if not ended.get("success") and "救赎" in str(ended.get("error", "")):
            # 救赎可能由 battle_end 自身的前置结算队列（战终凡庸结算把残血无原
            # 始道纹怪压入救赎队列）在清场之后才出现——清一次再交。
            _resolve_pending_choices(e)
            ended = e.execute_action("battle_end", {})
        if not ended.get("success"):
            return {"cleared": cleared, "won": False, "invalid": True,
                    "reason": f"battle_end: {ended.get('error')}"}
        # 战终结算可能触发癌变/凡庸等命零（回复过量/未造成伤害），死之传承中断
        # 会入队阻塞后续行动；此时本场仍算通关（cleared+1），但轮回到此结束。
        if not e.state.player or not e.state.player.is_alive:
            return {"cleared": cleared + 1, "won": False, "invalid": False,
                    "pm": _postmortem(e, cleared, todo)}
        cleared += 1

        # 第7场战终触发【最终的冠冕】：第一名封存；第二名进入第8场死斗。
        # 死斗胜利后须领取终音法器（choose_terminal_artifact）才算完整通关并封存。
        crown = ended.get("result", {}).get("final_crown", {})
        outcome = crown.get("outcome")
        if outcome == "sealed":
            # DM裁定（2026-08-22）：第7场封存≠完整轮回——只有打过死斗（PvP）
            # 才算完成一次完整轮回；封存只是"进队当擂主"，won 只认死斗胜利。
            return {"cleared": cleared, "won": False, "invalid": False,
                    "duel_fought": False, "sealed": True,
                    "sealed_name": crown.get("sealed_name")}
        if outcome == "duel_start" or e.state.in_final_duel:
            # 第8场死斗：用新的对称PvP驱动（守擂方走玩家侧接口，逐出手交替）。
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
            dr = run_duel_pvp(e, _act, max_rounds=60, max_steps=400, log=log_buf,
                              max_wall_seconds=30)
            duel_won = dr.get("winner") == "challenger"
            duel_rounds = dr.get("rounds")
            if dr.get("timeout"):
                # 卡死守护命中（2026-08-22 批次13教训）：打印证据，便于归因而非无声挂起
                print(f"    [死斗超时] 回合{dr.get('rounds')} 判擂主卫冕｜state字段deepcopy成本(ms): "
                      f"{dr.get('diag_state_sizes')}", flush=True)
            opp = next((x.name for x in e.state.enemies
                        if x.entity_type == "轮回者"), "")  # 守擂擂主名（供PvP经验记录）
            if not duel_won or not e.state.player or not e.state.player.is_alive:
                # 打过死斗但落败：轮回不完整（DM裁定2026-08-22），继续只留下 PV数据。
                out = {"cleared": cleared, "won": False, "invalid": False,
                       "duel_fought": True, "duel_won": False,
                       "duel_opponent": opp,
                       "duel_rounds": duel_rounds,
                       "pm": _postmortem(e, cleared, todo, killer_hint=opp)}
                if dr.get("timeout"):
                    out["duel_timeout"] = True
                return out
            # 死斗胜利：领取终音法器
            dr = e.execute_action("resolve_final_duel", {"outcome": "victory"})
            if not dr.get("success"):
                return {"cleared": cleared, "won": False, "invalid": True,
                        "reason": f"resolve_final_duel: {dr.get('error')}"}
            if dr.get("result", {}).get("pending_terminal_choice"):
                region2 = dr["result"]["pending_terminal_choice"]
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
                    "duel_fought": True, "duel_won": True, "duel_opponent": opp,
                    "duel_rounds": duel_rounds, "terminal_artifact": True}

    # 循环走满 7 场却未见冠冕（理论上不应到达）：按裁定同样不算完整轮回。
    return {"cleared": cleared, "won": False, "invalid": False,
            "duel_fought": False, "note": "未见最终的冠冕"}


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
        # 休整分级（2026-08-19 P2）：按 HP 缺口 + 可支付碎片 + 下一场关键资源选择档位。
        # 档位：tier1=8血/0碎片，tier2=24血/10碎片，tier3=48血/25碎片（engine/api.py）。
        # - 缺口足够大且碎片足够 → 高档位（不机械用 1 级）；
        # - 付不起高档位才退回低级；
        # - 保留关键资源：每名已部署员工的战终工资上限(12) + 应急缓冲(5)，
        #   后期战斗（第 5 场起出怪增多）再额外保留 5，避免为回血耗尽下一场必需碎片。
        p = e.state.player
        gap = max(0, p.blood_limit - p.current_hp) if p else 0
        bonus = e.state.rest_heal_bonus
        shards = e.state.shards
        deployed = sum(1 for emp in e.state.employees
                       if emp.is_alive and emp.is_deployed and not emp.is_debt_bound)
        reserve = 12 * deployed + 5 + (5 if battle_no >= 5 else 0)
        tier = 1
        if gap >= 40 and shards >= 25 + reserve:
            tier = 3
        elif gap >= 24 and shards >= 10 + reserve:
            tier = 2
        heal = {1: 8, 2: 24, 3: 48}[tier] + bonus
        return act, {"tier": tier, "heal_allocations": [
            {"target_ref": "player:0", "amount": heal}]}
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
            telemetry: dict = None, spend_shards: bool = False,
            region: str = None, policy: dict = None) -> tuple:
    """
    适应度 = 平均通关场数 + 3×胜率（0~10）。

    random_seeds=False（默认）：种子由代数推导，同一代可复现，便于排查。
    random_seeds=True：每局用真随机种子与随机副本，样本不重复，
      能避免"只在某几局上表现好"的过拟合，代价是结果不可逐局复现。
    region 指定时：全部局都打该副本（learn 列表是该副本实际可学的组合，
      2026-08-22 起按代轮换避免采样退化到固定子池）。
    policy: learned_policy 反哺后的局外行动权重（None=DEFAULT_POLICY）。

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
        if region is not None:
            run_region = region
            seed = rng.randrange(1, 2 ** 31 - 1) if random_seeds else gen * 1000 + i * 7 + 1
        elif random_seeds:
            seed = rng.randrange(1, 2 ** 31 - 1)
            run_region = rng.choice(REGIONS)
        else:
            seed = gen * 1000 + i * 7 + 1
            run_region = REGIONS[i % len(REGIONS)]
        r = play(starter, learn, run_region, seed, rng=rng, telemetry=telemetry,
                spend_shards=spend_shards, policy=policy)
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
            # PvP 死斗经验（含未达死斗的封存记录，DM裁定2026-08-22）
            du = telemetry.setdefault("duels", {"fought": 0, "won": 0,
                                                "sealed_no_duel": 0, "by_build": {}})
            bkey = f"{starter}|{'+'.join(learn)}"
            bk = du["by_build"].setdefault(bkey, {"fought": 0, "won": 0})
            if r.get("duel_fought"):
                du["fought"] += 1
                bk["fought"] += 1
                if r["won"]:
                    du["won"] += 1
                    bk["won"] += 1
                if r.get("duel_timeout"):
                    du["timeouts"] = du.get("timeouts", 0) + 1
                    bk["timeouts"] = bk.get("timeouts", 0) + 1
            elif r["cleared"] >= 7:
                du["sealed_no_duel"] += 1
            telemetry.setdefault("region_runs", {})
            telemetry["region_runs"][run_region] = telemetry["region_runs"].get(run_region, 0) + 1
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
    region 给定时只从该副本实际可学的道纹中取（门禁修复后必需）。
    2026-08-22：build 压缩为 BUILD_SIZE=3 有效位，学习顺序=UCB强度序。"""
    total_n = sum(t["n"] for t in k["trials"].values()) or 1
    CAND = learnable_candidates(region)
    best = k.get("best")
    if best and rng.random() < 0.5:
        learn = list(best["learn"])[:BUILD_SIZE]  # 旧精英5纹遗产截断为有效3纹
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
    picked = head[:BUILD_SIZE]
    # 学习顺序=UCB强度序（杀伐类输出优先于生存/功能纹——2026-08-22 实验证明
    # "再生优先"配对 38/86 显著更差：出手先要能杀，防/奶其次）。
    picked.sort(key=lambda c: -ucb(k, c, total_n))
    return starter, picked


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
                  f"｜总胜率 {oc['win']/tot*100:.1f}%｜平均通关 {oc['cleared_sum']/tot:.2f} 场"
                  f"（胜率口径：仅死斗胜利，DM2026-08-22）")
    dz = t.get("duels")
    if dz and (dz.get("fought") or dz.get("sealed_no_duel")):
        fought, won = dz.get("fought", 0), dz.get("won", 0)
        timeouts = dz.get("timeouts", 0)
        # 超时=我方算力打不完（死斗AI预演成本随尸体库线性增长），不是"打不过"——
        # 真实 PvP 胜率分母剔除超时场；但 won 口径不变（未完成死斗≠完整轮回）。
        real = fought - timeouts
        print(f"\n【最终死斗 PvP 经验】死斗 {fought} 场｜胜 {won}"
              f"｜真实胜率 {won/real*100 if real else 0:.0f}%（剔除超时 {timeouts} 场）"
              f"｜第7场封存(未达成完整轮回) {dz.get('sealed_no_duel', 0)} 次")
        rows = sorted((b for b in dz.get("by_build", {}).items() if b[1]["fought"]),
                      key=lambda kv: (-kv[1]["won"], -kv[1]["fought"]))[:6]
        if rows:
            print("  按build（死斗实战记录）：")
            for key, rec in rows:
                print(f"    {key}  死斗{rec['fought']}场 胜{rec['won']}")
    rr = t.get("region_runs")
    if rr:
        print("  副本分布：" + "、".join(f"{a}{b}局" for a, b in sorted(rr.items())))

    av = t.get("alt_victory")
    if av:
        tot2 = sum(av.values()) or 1
        parts = "、".join(f"{k2}×{v}({v/tot2*100:.0f}%)" for k2, v in
                          sorted(av.items(), key=lambda kv: -kv[1]))
        print(f"\n【清场路径构成】（胜利≠打死：凡庸=被动命零有碎片；雕塑/封印=离场无碎片）\n  {parts}")
    pm = t.get("postmortem")
    if pm and pm.get("deaths"):
        d = pm["deaths"]
        db = pm.get("death_battle", {})
        top_die = sorted(db.items(), key=lambda kv: -kv[1])[:3]
        top_killers = sorted(pm.get("killers", {}).items(), key=lambda kv: -kv[1])[:5]
        print(f"\n【战败复盘】{d} 次阵亡解剖（从引擎事件流还原死因，非统计猜测）")
        print("  阵亡场次分布：" + "、".join(f"第{k2}战×{v}" for k2, v in top_die))
        print(f"  资源课：带药阵亡 {pm['带药阵亡']}（{pm['带药阵亡']/d*100:.0f}%）"
              f"｜碎片≥15未花 {pm['碎片未花']}（{pm['碎片未花']/d*100:.0f}%）"
              f"｜满蓝阵亡 {pm['满蓝阵亡']}（{pm['满蓝阵亡']/d*100:.0f}%）")
        print("  杀手Top5：" + "、".join(f"{k2}×{v}" for k2, v in top_killers))

    # 行为→胜率相关 + 策略反哺（2026-08-22 DM诉求）
    bs = t.get("behavior_stats")
    if bs and oc and (oc["win"] + oc["loss"]):
        tot3 = oc["win"] + oc["loss"]
        base = oc["cleared_sum"] / tot3
        rows = []
        for name, s in bs.items():
            if not s.get("n"):
                continue
            avg = s["cleared_sum"] / s["n"]
            rows.append((avg - base, name, s, avg))
        rows.sort(reverse=True)
        print(f"\n【行为胜率相关】基线均通关 {base:.2f} 场/局（提高胜率的行为已按此反哺策略权重）")
        for delta, name, s, avg in rows[:8]:
            print(f"  {name:<12} {s['n']:>5}局  胜率 {s['wins']/s['n']*100:>4.1f}%  "
                  f"均通关 {avg:>5.2f}  增益{delta:+.2f}｜{BEHAVIOR_RULES.get(name, '')}")
        if len(rows) > 8:
            print("  …拖累最大2个：" + "、".join(
                f"{name}{delta:+.2f}" for delta, name, s, avg in rows[-2:]))
    pl = k.get("policy_learn")
    if pl:
        print(f"  策略反哺（权重乘数）：{pl['multipliers']}")

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
    if k.get("lessons"):
        print("\n【心得与陷阱】（规则定义不入库，只记经验级结论；详见 README）")
        for l in k["lessons"][-8:]:
            print(f"  [{l['kind']}] {l['text']}（{l['date']}）")

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
        # 按代轮换副本（2026-08-22 采样退化治理：不带 region 的 propose 会把候选池
        # 过滤到固定11/62的常数子集——propose 与 fitness 必须同 region）。
        region = REGIONS[k["generation"] % len(REGIONS)]
        pol = learned_policy(k)   # 行为反哺闭环：提高胜率的行为改变后续采样
        starter, learn = propose(k, rng, region)
        score, valid, invalid = fitness(starter, learn, a.runs, k["generation"],
                                        random_seeds=a.random_seeds, rng=rng,
                                        telemetry=tele, spend_shards=a.spend,
                                        region=region, policy=pol)
        k["total_games"] = k.get("total_games", 0) + valid
        k["invalid_games"] = k.get("invalid_games", 0) + invalid
        if valid:                      # 全部无效的代不计入学习，避免污染权重
            update(k, starter, learn, score)
        star = " ★新最优" if k.get("best") and k["best"]["score"] == score else ""
        bad = f"  [无效{invalid}]" if invalid else ""
        # flush=True：管道下 Python 块缓冲会吞掉进度（曾致"两小时零输出"假象）
        print(f"第{k['generation']:>4}代｜{region}  【{starter}】{'+'.join(learn):<28} "
              f"→ {score:5.2f}{star}{bad}", flush=True)
        save(k)

    print()
    report(k)
    save(k)


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

if __name__ == "__main__":
    main()
