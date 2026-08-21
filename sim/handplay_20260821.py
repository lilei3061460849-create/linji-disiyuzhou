#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第四宇宙 · 2026-08-21 实战测试手操驱动（测试者手写决策，非 TacticalAI/批量通关）。

原则（对应《测试原则》）：
- 玩家侧每一个决策点（开局/局外/每回合出手/闪避/法术/残韵/消耗品/队友指令）
  由本驱动的显式策略函数给出，且每步打印决策理由 —— 策略本身是"测试者按当前
  信息与合理策略"编写，不为了证明某个道纹有用而刻意制造场景。
- 怪物侧按引擎 prepare 列出的合法选项行动；道纹选择沿用仓库既有的标准手操
  怪物解析逻辑（_pick_monster_daowen / _resolve_monster_turn_hand），并完整
  记录怪物每回合实际发动的道纹、异变支付与递增后的 X。
- 每场战斗结束后 driver 停止，由测试者阅读战报再决定下一场策略（真实手操节奏）。

输出：
- 逐回合推演日志（stdout，按 README《六、战斗推演格式》的紧凑版）
- data/handplay_20260821_<副本>_<角色>.jsonl 全量事件轨迹
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Entity

TRACE_PATH = os.environ.get("HP_TRACE", "data/handplay_20260821_trace.jsonl")


from sim.monster_targets import (  # noqa: E402
    MONSTER_HOSTILE_DAOWEN,
    MONSTER_SELF_DAOWEN,
    pick_monster_daowen_target,
)

# ---------------------------------------------------------------------------
# 工具：事件轨迹
# ---------------------------------------------------------------------------
class Trace:
    def __init__(self, path: str):
        self.path = path
        self.entries = []
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def log(self, **kw):
        entry = dict(kw)
        self.entries.append(entry)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def text(self, msg: str, **kw):
        print(msg)
        self.log(kind="text", msg=msg, **kw)


TRACE = Trace(TRACE_PATH)


# ---------------------------------------------------------------------------
# 策略状态：每场战斗之间由测试者调整（模拟真实玩家逐场总结经验）
# ---------------------------------------------------------------------------
class Strategy:
    """测试者策略配置。每场战斗可覆盖字段。"""

    def __init__(self, **kw):
        # 出手X选择：'max'（尽可能多打）| 'reserve_kill'（留足下回合斩杀余量）
        self.offense_mode = "max"
        # 残韵：允许对哪些源道纹使用反转（对怪物），源道纹→想获得/达成的效果
        self.resonance_targets = {}   # {源道纹: 说明}
        # 庇护阈值：威胁-格挡-闪避预算 > 当前生命×该系数 时优先上盾
        self.shield_factor = 0.0
        # 每回合最多闪避次数
        self.max_dodges = 2
        # 低伤不闪避的阈值（单次命中伤害）
        self.dodge_min_damage = 6
        # 学习法术（局外）名单
        self.learn_spells = []
        self.learn_daowen = []
        for k, v in kw.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------
# 玩家侧决策函数（手操策略 = 测试者决策）
# ---------------------------------------------------------------------------
def enemy_list(state) -> list:
    return [e for e in state.enemies if e.is_alive and not e.removed_without_kill]


def threat_of(enemies) -> int:
    return sum((e.attack_count or 0) * (e.attack_power or 0) for e in enemies)


def choose_attack_target(engine, enemies):
    """输出优先：先清能杀死的、其次清威胁高的；血最少优先。"""
    p = engine.state.player
    if p is None:
        return enemies[0]
    # 每回合可造成的总伤害估计：剩余法力全部输出（每点法力=2伤害）
    mana_left = max(0, p.current_mana - 2)  # 保守：留2点法力
    actions_left = max(0, p.action_count - p.actions_used_this_round)
    # 斩杀判定：杀伐X 造成 2X，总输出 ≈ mana_left*2
    killable = [e for e in enemies if mana_left * 2 >= e.current_hp]
    if killable:
        return min(killable, key=lambda e: e.current_hp)
    # 威胁分 = 攻击次数×攻击力 + 输出型道纹权重
    def threat(e):
        t = (e.attack_count or 0) * (e.attack_power or 0)
        for name in e.dao_wen:
            if name in ("狂暴", "强化", "血债", "杀伐", "波及", "加害", "洗劫", "赎金", "爆裂"):
                t += 6
            elif name in ("自愈", "再生", "活血", "滋养"):
                t += 4
        return t
    return max(enemies, key=lambda e: (threat(e), -e.current_hp))


def try_resonance(engine, source: str, target_ref: str, note: str) -> Optional[dict]:
    """尝试残韵插队；成功则返回已执行标记。"""
    state = engine.state
    if state.resonance.get("反转", 0) <= 0:
        return None
    r = engine.execute_action("use_resonance", {
        "source_daowen": source, "resonance_type": "反转",
        "target_ref": target_ref})
    if r.get("success"):
        TRACE.text(f"  决策[残韵] 反转{source}→{r.get('action', '').split('→')[-1].strip()}（{note}）"
                   f" 玩家获得【{r.get('granted_daowen')}】")
        return {"action_type": "__resonance_done__", "params": {}}
    return None


def decide_player_action(engine, strat: Strategy) -> Optional[dict]:
    """测试者决策（纯决策，不执行）：返回 (action_type, params) 或 None。"""
    state = engine.state
    p = state.player
    enemies = enemy_list(state)
    if p is None or not p.is_alive or not enemies:
        return None
    enemy_ref = lambda e: f"enemy:{state.enemies.index(e)}"
    current_mana = p.current_mana
    threat = threat_of(enemies)

    # ---- 1. 残韵：战术性概念篡改（反转怪物原始道纹 / 飞行对策）----
    if state.resonance.get("反转", 0) > 0:
        # 1a. 飞行对策优先：怪物持【飞行】（无论是否已飞行）→ 反转飞行→坠落。
        #     先手转换可永久拆掉其飞行；怪物若再发动【坠落】还会自减伤。
        for e in enemies:
            if "飞行" in e.dao_wen:
                TRACE.text(f"  决策[残韵] 反转飞行→坠落（{e.name}，永久拆飞行+玩家获得坠落）")
                return {"action_type": "use_resonance",
                        "params": {"source_daowen": "飞行", "resonance_type": "反转",
                                   "target_ref": enemy_ref(e)}}
        # 1b. 策略指定目标（如 强化→弱化 / 必中→蒙蔽 / 自愈→衰败）
        for src, note in strat.resonance_targets.items():
            for e in enemies:
                if src in e.dao_wen:
                    TRACE.text(f"  决策[残韵] 反转{src}→{note}（{e.name}）")
                    return {"action_type": "use_resonance",
                            "params": {"source_daowen": src, "resonance_type": "反转",
                                       "target_ref": enemy_ref(e)}}
        # 1c. 玩家已持【坠落】且敌方有飞行者 → 主动发动坠落（全场禁飞+减伤）
        if "坠落" in p.dao_wen and current_mana >= 1:
            for e in enemies:
                if engine.combat._is_flying(e):
                    TRACE.text(f"  决策[坠落] 发动坠落X=1（全场禁飞，{e.name}伤害减半）")
                    return {"action_type": "use_daowen",
                            "params": {"daowen_name": "坠落", "x": 1,
                                       "target_ref": enemy_ref(e),
                                       "trigger_spell_choices": {}}}

    # ---- 1d. 买路财撤退：濒死且付得起 20%怪物血限 时安全撤退 ----
    if any(r.name == "买路财" for r in state.relics) and p.current_hp <= p.blood_limit * 0.25:
        m0 = enemies[0]
        cost = math.ceil(m0.blood_limit * 0.2)
        if state.shards >= cost:
            TRACE.text(f"  决策[买路财] 生命{p.current_hp}濒死，花费{cost}碎片安全撤退")
            return {"action_type": "retreat_via_toll", "params": {}}

    # ---- 2. 蒙蔽防御：敌方总命中次数高且威胁大 → 蒙蔽覆盖 ----
    if "蒙蔽" in p.dao_wen and current_mana >= 5:
        target = max(enemies, key=lambda e: (e.attack_count or 0) * (e.attack_power or 0))
        total_hits = sum((e.attack_count or 0) for e in enemies)
        covered = target.get_status_value("蒙蔽") if target.has_status("蒙蔽") else 0
        need = max(0, total_hits - covered)
        x = min(need, current_mana // 5)
        if x >= 1 and threat > p.current_hp + p.shield:
            TRACE.text(f"  决策[蒙蔽] X={x} 覆盖{target.name}本回合命中")
            return {"action_type": "use_daowen",
                    "params": {"daowen_name": "蒙蔽", "x": x, "target_ref": enemy_ref(target),
                               "trigger_spell_choices": {}}}

    # ---- 3. 保命判断：威胁-可闪避部分 > 生命+格挡 → 庇护 ----
    dodge_cap = min(p.current_speed, strat.max_dodges)
    incoming = max(0, threat - dodge_cap * 6)
    if incoming > p.current_hp + p.shield and "庇护" in p.dao_wen and current_mana >= 4:
        x = min(4, current_mana // 2)
        TRACE.text(f"  决策[庇护] 威胁{incoming}＞生命{p.current_hp}+盾{p.shield}，庇护X={x}")
        return {"action_type": "use_daowen",
                "params": {"daowen_name": "庇护", "x": x, "target_ref": "player:0",
                           "trigger_spell_choices": {}}}

    # ---- 3b. 弱化：持有且敌方攻击力高 → 压制攻击（消耗3X，持续∞） ----
    if "弱化" in p.dao_wen and current_mana >= 6:
        target = max(enemies, key=lambda e: (e.attack_power or 0))
        if target.attack_power >= 6:
            x = min(target.attack_power - 2, current_mana // 3)
            if x >= 2:
                TRACE.text(f"  决策[弱化] X={x}（{target.name}攻击{target.attack_power}→{max(1, target.attack_power - x)}）")
                return {"action_type": "use_daowen",
                        "params": {"daowen_name": "弱化", "x": x, "target_ref": enemy_ref(target),
                                   "trigger_spell_choices": {}}}

    # ---- 3c. 封印：应急移除威胁怪物（代价异变8X——代价闭环：敢不敢付） ----
    if "封印" in p.dao_wen and len(enemies) >= 1:
        threat_now = threat_of(enemies)
        no_offense = not any(n in p.dao_wen for n in ("杀伐", "血债", "波及", "贯穿", "蒙蔽"))
        lethal = threat_now > p.current_hp + p.shield + 10 and p.current_hp <= p.blood_limit * 0.6
        if no_offense or lethal:
            target = max(enemies, key=lambda e: (e.attack_count or 0) * (e.attack_power or 0))
            TRACE.text(f"  决策[封印] X=1 移出{target.name}（代价异变8，当前异变{p.mutation_count}，"
                       f"累计{p.mutation_count + 8}层）")
            return {"action_type": "use_daowen",
                    "params": {"daowen_name": "封印", "x": 1, "target_ref": enemy_ref(target),
                               "trigger_spell_choices": {}}}

    # ---- 3d. 固执对策：敌方固执（单次伤害≤1）→ 血债多段1伤才是正解 ----
    if "血债" in p.dao_wen and p.current_hp >= 15:
        # 敌方持有固执（迟早发动）或已激活 → 多段1伤才是正解
        guzhi_target = next((e for e in enemies
                             if e.has_status("固执") or "固执" in e.dao_wen), None)
        if guzhi_target is not None:
            x = min(4, (p.current_hp - 4) // 2)
            if x >= 1:
                TRACE.text(f"  决策[血债] X={x} 打{guzhi_target.name}（固执：单段伤害≤1，多段1伤正解）")
                return {"action_type": "use_daowen",
                        "params": {"daowen_name": "血债", "x": x, "target_ref": enemy_ref(guzhi_target),
                                   "trigger_spell_choices": {}}}

    # ---- 4. 输出：杀伐打选定目标 ----
    target = choose_attack_target(engine, enemies)
    if "杀伐" in p.dao_wen and current_mana >= 1:
        # 输出上限 = _offense_budget（出手循环按已花费递减），保留法力给反应法术
        budget = getattr(p, "_offense_budget", None)
        x = min(current_mana, budget) if budget is not None else current_mana
        if x < 1:
            return None
        TRACE.text(f"  决策[杀伐] X={x} 打{target.name}（{target.current_hp}血）")
        return {"action_type": "use_daowen",
                "params": {"daowen_name": "杀伐", "x": x, "target_ref": enemy_ref(target),
                           "trigger_spell_choices": {}}}

    # ---- 5. 透支：法力电池（代价衰老X→4X法力）。法力缺口大且血限富余时用 ----
    if "透支" in p.dao_wen and current_mana < 6:
        spender = any(n in p.dao_wen for n in ("杀伐", "波及", "血债", "贯穿", "蒙蔽", "庇护"))
        if spender and p.blood_limit - p.current_hp < 30:
            x = min(2, max(1, (6 - current_mana) // 1))
            TRACE.text(f"  决策[透支] X={x} 衰老{x}（血限{p.blood_limit}→{p.blood_limit - x}）→ 法力+{4 * x}")
            return {"action_type": "use_daowen",
                    "params": {"daowen_name": "透支", "x": x, "target_ref": "player:0",
                               "trigger_spell_choices": {}}}

    # ---- 6. 血债：自身流血换稳定伤害（代价类道纹不耗法力；血线安全时用） ----
    if "血债" in p.dao_wen and p.current_hp >= 12:
        x = min(2, (p.current_hp - 6) // 2)
        if x >= 1:
            TRACE.text(f"  决策[血债] X={x}（流血{x}，打{target.name} {x}次1伤）")
            return {"action_type": "use_daowen",
                    "params": {"daowen_name": "血债", "x": x, "target_ref": enemy_ref(target),
                               "trigger_spell_choices": {}}}

    # ---- 7. 兜底输出：波及/贯穿 ----
    for name in ("波及", "贯穿"):
        if name in p.dao_wen and current_mana >= 1:
            TRACE.text(f"  决策[{name}] X=1 打{target.name}")
            return {"action_type": "use_daowen",
                    "params": {"daowen_name": name, "x": 1, "target_ref": enemy_ref(target),
                               "trigger_spell_choices": {}}}
    return None


def decide_ally_actions(engine, strat: Strategy):
    """队友：威胁大时全体护卫（无消耗挡伤），输出型队友补刀。"""
    state = engine.state
    p = state.player
    enemies = enemy_list(state)
    if not enemies or p is None:
        return
    threat = threat_of(enemies)
    need_guard = threat > p.current_hp + p.shield
    all_allies = ([(f"friend:{i}", f) for i, f in enumerate(state.friends)]
                  + [(f"employee:{i}", e) for i, e in enumerate(state.employees) if e.is_deployed])
    for ref, ally in all_allies:
        if not ally.is_alive or ally.has_retreated:
            continue
        if need_guard:
            r = engine.execute_action("command_ally", {"ally_ref": ref, "instruction": "护卫 9"})
            if r.get("success"):
                TRACE.text(f"  决策[护卫] {ally.name} 护卫轮回者（威胁{threat}）")
            continue
        # 输出型队友补刀
        if ally.actions_used_this_round < ally.action_count and ally.attack_count * ally.attack_power >= 6:
            target = max(enemies, key=lambda e: (e.attack_count or 0) * (e.attack_power or 0) + len(e.dao_wen))
            r = engine.execute_action("command_ally", {"ally_ref": ref, "instruction": f"攻击 {target.name}"})
            if r.get("success"):
                TRACE.text(f"  决策[队友] {ally.name} 攻击 {target.name}")




def decide_dodge(engine, per_hit: int, budget_used: int, strat: Strategy,
                 hits_left: int = 1, incoming_total: int = 0) -> bool:
    """闪避决策：单发大伤害必闪；多段小伤害若本回合总入伤威胁大也闪（保留速度）。"""
    p = engine.state.player
    if p is None or not p.is_alive:
        return False
    if p.current_speed <= budget_used:
        return False
    if budget_used >= strat.max_dodges:
        return False
    if p.has_status("固执"):
        return False  # 固执：单次失去生命≤1，无需闪避
    threshold = max(strat.dodge_min_damage, math.ceil(p.blood_limit * 0.05))
    if per_hit >= threshold:
        return True
    # 多段小伤害：本回合剩余命中累计入伤 > 当前生命 25% → 值得闪避
    if per_hit >= 3 and incoming_total > 0:
        remaining = max(0, incoming_total - budget_used * per_hit)
        if remaining > math.ceil(p.current_hp * 0.25):
            return True
    return False


def build_spell_choices(engine, target_option, player_ref: str, mana_budget: int) -> dict:
    """玩家侧反应法术决策：受到攻击时是否触发先发制人/后发制人/生生不息等。

    X选择（自由控X）：攻击步骤取剩余法力的安全值，自保步骤取1（最小消耗）。
    """
    spell_options = target_option.get("spell_options", {}) or {}
    out = {}
    # 预算跨法术共享递减：每个法术的 cycle 都从同一 remaining_budget 扣减，
    # 否则多法术（先发制人+后发制人+生生不息）各自从 mana_budget 全额计算，
    # 会提交超出实际法力的组合 → 执行阶段「法力不足」整段回滚（2026-08-21 实测）。
    remaining_budget = mana_budget
    for timing in ("before", "after"):
        out[timing] = {}
        for spell in spell_options.get(timing, []) or []:
            name = spell["spell_name"]
            steps = spell.get("steps", [])
            use = False
            cycles = []
            if timing == "before":
                # 有敌对步骤才用（先发制人/借力打力）；庇护型（后发制人）在威胁大时用
                has_hostile = any(s.get("target_ref") != player_ref for s in steps)
                if has_hostile:
                    use = True
                elif steps and steps[0].get("daowen") == "庇护":
                    p = engine.state.player
                    threat = threat_of(enemy_list(engine.state))
                    if p is not None and threat > p.current_hp + p.shield:
                        use = True
            else:
                # 掉血后回血总是值得（法力够才触发）
                use = True
            if use and remaining_budget >= 1:
                remaining = remaining_budget
                cycle = []
                ok = True
                for st in steps:
                    target_ref = st.get("target_ref", player_ref)
                    hostile = target_ref != player_ref
                    # 攻击步的 X 受全回合预算约束：必须为同 timing 的其余法术
                    # 与 after timing 的法术保留最小法力（每步至少1），否则先发制人
                    # 一次抽干预算会导致后发制人/生生不息执行时法力不足被拒
                    # （2026-08-21 实测驱动 bug：先发制人X=25吃光26法力）。
                    if st.get("daowen") == "血债":
                        x = 2
                    elif hostile:
                        reserve = 0
                        if timing == "before":
                            reserve = sum(
                                len(s2.get("steps") or [])
                                for s2 in spell_options.get("before", []) or []
                                if s2["spell_name"] != name)
                            reserve += sum(
                                len(s2.get("steps") or [])
                                for s2 in spell_options.get("after", []) or [])
                        x = max(1, remaining - reserve - 1)
                    else:
                        x = 1
                    entry = {"x": x, "target_ref": target_ref}
                    if hostile:
                        entry["dodge"] = False  # 反打步骤：对手不闪避（测试者代怪物决策）
                    if st.get("daowen") != "血债":
                        remaining -= x
                        if remaining < 0:
                            ok = False
                            break
                    cycle.append(entry)
                if ok and cycle:
                    cycles = [cycle]
                    use = True
                    remaining_budget = max(0, remaining_budget - sum(
                        e.get("x", 1) for cy in cycles for e in cy))
                else:
                    use = False
            else:
                use = False  # 法力不足 → 显式拒绝，不能留空cycles
            out[timing][name] = {"use": use, "cycles": cycles} if use else {"use": False}
    return out


def resolve_monster_turn_hand(engine, strat: Strategy):
    """怪物阶段：道纹选择沿用标准手操逻辑（_pick_monster_daowen），但目标选择
    按最优策略修正（SELF→自身 / HOSTILE→玩家），闪避/法术由本驱动决策。"""
    prepared = engine.execute_action("prepare_monster_phase", {})
    if not prepared.get("success"):
        return prepared
    # 记录异变前后，用于观察原始怪物道纹的代价支付
    mut_before = {id(m): m.mutation_count for m in engine.state.enemies if m.is_alive}
    choices = []
    dodge_budget = 0
    refs = engine.combat._combat_entity_refs()
    player_ref = "player:0"
    from sim.duel_common import _pick_monster_daowen
    for actor in prepared["result"]["actors"]:
        dao = None
        option = None
        action_count = actor["base_attack_actions"]
        hit_count = actor["base_hits_per_attack"]
        if actor["daowen_options"]:
            option = _pick_monster_daowen(engine, actor)
            if option is not None:
                dao = {"name": option["name"], "dodge": False, "blood_shadow": False,
                       "trigger_spell_choices": {holder: {sp["spell_name"]: {"use": False}
                                                          for sp in spells}
                                                 for holder, spells in option.get("trigger_spell_options", {}).items()}}
                if option["requires_target"]:
                    dao["target_ref"] = pick_monster_daowen_target(
                        engine, actor["actor_ref"], option)
                if option["dodge_submission"] == "per_target":
                    dao["dodge_targets"] = [
                        {"target_ref": t["ref"], "dodge": False, "blood_shadow": False}
                        for t in option["dodge_target_options"]]
                if option["resolves_as"] == "变形":
                    enemy_index = int(actor["actor_ref"].split(":", 1)[1])
                    hit_count = engine.state.enemies[enemy_index].attack_power
        monster = refs.get(actor["actor_ref"])
        per_hit = monster.attack_power if monster is not None else 0
        if option is not None and option.get("resolves_as") == "变形" and monster is not None:
            per_hit = monster.attack_count  # 变形后攻击力=原攻击次数
        from engine.ai_tactics import choose_attack_target
        target_ref = choose_attack_target(actor["attack_target_options"], refs)
        target_option = next(o for o in actor["attack_target_options"] if o["ref"] == target_ref)
        if engine.state.player is not None:
            # 2026-08-21 修复后：怪物阶段静态校验已计入[敌回始]守夜灯法力，
            # 因此反应法术预算 = 当前法力 + 本回合守夜灯将授予的法力。
            spell_mana_left = (engine.state.player.current_mana
                               + engine.combat._shouyedeng_pending_grant(
                                   engine.state.player))
        else:
            spell_mana_left = 0
        attacks = []
        for _ in range(action_count):
            hits = []
            for _ in range(hit_count):
                # 闪避决策只对轮回者受击生效：[朋友]/[员工]（微光者）速度=0，
                # 提交 dodge=True 会被引擎拒绝（速度不足）→ 整段回退为全不闪避。
                want_dodge = False
                if target_ref == player_ref:
                    incoming_total = max(0, (hit_count - len(hits)) * per_hit)
                    want_dodge = decide_dodge(engine, per_hit, budget_used=dodge_budget, strat=strat,
                                              hits_left=hit_count - len(hits),
                                              incoming_total=incoming_total)
                    if want_dodge:
                        dodge_budget += 1
                        TRACE.text(f"  决策[闪避] 闪避{per_hit}伤（速度-1，剩余预算{dodge_budget}）")
                # 只有命中且受击者是玩家才会触发反应法术（dodge 时 damage=0 不触发）。
                # 预算必须在每击后统一递减（无论攻击目标是谁），否则打[朋友]/[员工]
                # 的命中不会消耗预算，后续命中会重复提交满额法术 → 执行时法力不足
                # 被拒 → 整段回退为全不闪避（2026-08-21 实测驱动 bug）。
                sc = {timing: {sp["spell_name"]: {"use": False}
                               for sp in target_option.get("spell_options", {}).get(timing, [])}
                      for timing in ("before", "after")}
                if not want_dodge and target_ref == player_ref:
                    sc = build_spell_choices(engine, target_option, player_ref,
                                             mana_budget=spell_mana_left)
                for timing in ("before", "after"):
                    for sp in target_option.get("spell_options", {}).get(timing, []) or []:
                        dec = sc.get(timing, {}).get(sp["spell_name"], {})
                        if dec.get("use"):
                            cost = sum(e.get("x", 1) for cy in dec.get("cycles", []) for e in cy)
                            spell_mana_left = max(0, spell_mana_left - cost)
                hit = {"target_ref": target_ref, "dodge": want_dodge,
                       "blood_shadow": False, "spell_choices": sc}
                if want_dodge and target_option.get("dodge_relic_target_options"):
                    hit["dodge_relic_target_ref"] = target_option["dodge_relic_target_options"][0]["ref"]
                hits.append(hit)
            attacks.append({"hits": hits})
        choices.append({"actor_ref": actor["actor_ref"], "daowen": dao,
                        "attack_actions": attacks})
    result = engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": choices})
    if not result.get("success"):
        TRACE.text(f"    [怪物阶段提交被拒] {result.get('error')}（进入兜底）")
        fallback = []
        for actor in prepared["result"]["actors"]:
            dao = None
            hit_count_fb = actor["base_hits_per_attack"]
            if actor["daowen_options"]:
                option = _pick_monster_daowen(engine, actor)
                if option is not None:
                    dao = {"name": option["name"], "dodge": False, "blood_shadow": False,
                           "trigger_spell_choices": {}}
                    if option["requires_target"]:
                        dao["target_ref"] = pick_monster_daowen_target(
                            engine, actor["actor_ref"], option)
                    if option["dodge_submission"] == "per_target":
                        dao["dodge_targets"] = [
                            {"target_ref": t["ref"], "dodge": False, "blood_shadow": False}
                            for t in option["dodge_target_options"]]
                    if option["resolves_as"] == "变形":
                        enemy_index = int(actor["actor_ref"].split(":", 1)[1])
                        hit_count_fb = engine.state.enemies[enemy_index].attack_power
            target_ref = choose_attack_target(actor["attack_target_options"], refs)
            target_option = next(o for o in actor["attack_target_options"] if o["ref"] == target_ref)
            attacks = [{"hits": [{
                "target_ref": target_ref, "dodge": False, "blood_shadow": False,
                "spell_choices": {timing: {sp["spell_name"]: {"use": False}
                                           for sp in target_option.get("spell_options", {}).get(timing, [])}
                                   for timing in ("before", "after")},
            } for _ in range(hit_count_fb)]} for _ in range(actor["base_attack_actions"])]
            fallback.append({"actor_ref": actor["actor_ref"], "daowen": dao,
                             "attack_actions": attacks})
        result = engine.execute_action("resolve_monster_phase", {
            "token": prepared["result"]["token"], "choices": fallback})
    # 记录怪物道纹使用与异变支付
    mut_after = {id(m): m.mutation_count for m in engine.state.enemies if m.is_alive}
    for d in (result.get("result", {}).get("details") or []):
        if d.get("daowen_activated"):
            name = d.get("daowen_activated")
            m = next((m for m in engine.state.enemies if m.name == d.get("monster")), None)
            paid = ""
            if m is not None:
                delta = mut_after.get(id(m), 0) - mut_before.get(id(m), 0)
                if delta:
                    paid = f"，支付异变{delta}（→{mut_after.get(id(m))}层）"
            tgt = d.get("target")
            extra = f"（{d.get('resolves_as')}）" if d.get("resolves_as") != name else ""
            TRACE.text(f"  [怪] {d.get('monster')} 发动【{name}】X={d.get('x')}{extra} 目标={tgt}{paid}")
    for d in (result.get("result", {}).get("details") or []):
        if "damage_dealt" in d and "attacker" in d and d.get("attacker") != "玩家":
            dealt = d.get("damage_dealt", 0)
            note = ""
            if d.get("dodge_success"):
                note = "（闪避成功）"
            elif d.get("cant_target"):
                note = "（飞行不可选中）"
            if dealt > 0 or note:
                TRACE.text(f"  [怪攻击] {d.get('attacker')}→{d.get('target')} 伤{dealt}{note} "
                           f"（{d.get('target')} 生命→{d.get('target_hp_after')}）")
            for lg in d.get("spell_logs") or []:
                if lg.get("execution") or lg.get("dodged"):
                    TRACE.text(f"    法术【{lg.get('spell')}】触发：{lg.get('daowen')}X={lg.get('x')}"
                               + ("（被闪避）" if lg.get("dodged") else ""))
    return result


# ---------------------------------------------------------------------------
# 开局 / 局外 / 整场驱动
# ---------------------------------------------------------------------------
def log_panels(engine, title):
    state = engine.state
    p = state.player
    line = f"  {title}: 玩家 {p.name}({p.blood_limit}血/{p.mana_limit}法/{p.speed_limit}速 出手{p.action_count})"
    TRACE.text(line)
    for idx, m in enumerate(state.enemies):
        if m.is_alive:
            dw = "、".join(f"{n}{i.x_value}" for n, i in m.dao_wen.items())
            TRACE.text(f"    敌{idx} {m.name}：{m.attack_count}×{m.attack_power}/{m.blood_limit} 血{m.current_hp} 道纹[{dw}] 异变{m.mutation_count}")


def run_setup(engine, cfg: dict) -> bool:
    """开局：属性→遗物发现→初始道纹发现→残韵→副本。全部显式决策并记录候选。"""
    name = cfg["name"]
    r = engine.execute_action("setup_attributes", {
        "name": name,
        "blood_points": cfg.get("blood_points", 10),
        "speed_points": cfg.get("speed_points", 8),
        "mana_points": cfg.get("mana_points", 7)})
    if not r.get("success"):
        TRACE.text(f"  [开局] 属性分配失败: {r.get('error')}")
        return False
    # 遗物发现
    choices = list(engine.state.pending_relic_choices)
    prefer = cfg.get("prefer_relic", "")
    pick = prefer if prefer in choices else choices[0] if choices else None
    TRACE.text(f"  [开局] 遗物发现候选：{choices} → 选择【{pick}】")
    if pick:
        engine.execute_action("choose_discovered_relic", {"relic_name": pick})
    # 初始道纹发现（测试者按实际价值选择：优先能立刻输出的道纹）
    dw_choices = list(engine.state.pending_initial_daowen_choices)
    prefer_dw = cfg.get("prefer_daowen", "")
    if prefer_dw in dw_choices:
        pick_dw = prefer_dw
    else:
        # 价值排序：输出>防御>成长>控制>封印（封印异变8X对玩家代价过重）
        order = ["杀伐", "血债", "再生", "庇护", "波及", "贯穿", "增殖", "透支", "束缚", "固执", "封印"]
        pick_dw = next((d for d in order if d in dw_choices), dw_choices[0])
    TRACE.text(f"  [开局] 初始道纹发现候选：{dw_choices} → 选择【{pick_dw}】")
    if pick_dw:
        engine.execute_action("setup_choose_initial_daowen", {"daowen_name": pick_dw})
    # 残韵
    res = cfg.get("resonance", "反转")
    TRACE.text(f"  [开局] 残韵：{res}")
    engine.execute_action("setup_choose_resonance", {"resonance_type": res})
    # 副本
    region = cfg.get("region", "扭曲都市")
    s = engine.execute_action("setup_choose_region", {"region": region})
    if not s.get("success"):
        TRACE.text(f"  [开局] 副本选择失败: {s.get('error')}")
        return False
    TRACE.text(f"  [开局] 副本：{region}")
    # 继承真实一阶胜者快照（二阶入口）
    if cfg.get("winner_snapshot"):
        import json as _json
        with open(cfg["winner_snapshot"], encoding="utf-8") as f:
            snap = _json.load(f)
        from sim.handplay_dungeon_with_winner import load_winner
        load_winner(engine, snap)
        # 测试工具侧降强度：压低法力，模拟真实二阶压力（否则继承胜者两回合秒怪，
        # 怪物控制道纹（勾魂/镇尸/冥气）在实战中永远没有发动机会——2026-08-21 实测）。
        nerf = cfg.get("nerf_mana")
        if nerf:
            p0 = engine.state.player
            p0.mana_limit = nerf
            p0.current_mana = nerf
            # 仅保留先发制人（需杀伐）与生生不息（需再生），删掉依赖未持道纹的死法术
            keep = [s for s in p0.spells if set(s.required_daowen) <= set(p0.dao_wen)]
            p0.spells[:] = keep
        TRACE.text(f"  [继承] 胜者快照：{cfg['winner_snapshot']}（道纹{sorted(engine.state.player.dao_wen)}，"
                   f"碎片{engine.state.shards}，法力{engine.state.player.mana_limit}）")
    TRACE.log(kind="setup", name=name, blood_points=cfg.get("blood_points", 10),
              speed_points=cfg.get("speed_points", 8), mana_points=cfg.get("mana_points", 7),
              relic=pick, daowen=pick_dw, resonance=res, region=region)
    return True


def resolve_pending_event(engine, strat: Strategy) -> Optional[dict]:
    """事件待结算时：按策略选择项显式结算（选项与代价记录在轨迹）。"""
    state = engine.state
    if state.event_pool.current is None:
        return None
    event = engine.event_pool.events[engine.event_pool.current]
    opts = event["options"]
    TRACE.text(f"  事件【{engine.event_pool.current}】：{event.get('desc', '')[:60]}")
    for o in opts:
        TRACE.text(f"    选项：{o['text']}")
    ev_map = getattr(strat, "event_choices", {}) or {}
    pick = ev_map.get(engine.event_pool.current)
    if pick is None:
        # 默认：拒绝类选项（最保守）；无拒绝则第一项
        pick = next((o["id"] for o in opts if "拒绝" in o["text"] or "离开" in o["text"] or "无视" in o["text"]),
                    opts[0]["id"])
    TRACE.text(f"  决策[事件] {engine.event_pool.current} → 选择 {pick}")
    return engine.execute_action("resolve_event", {"event": engine.event_pool.current, "option_id": pick})


def decide_pre_battle(engine, strat: Strategy, shards_budget: int):
    """局外行动（3点精力）：按策略计划逐次决策（测试者每场可调），兜底修行。"""
    state = engine.state
    plan = list(getattr(strat, "pre_battle_plan", []) or [])
    plan_idx = 0
    while state.energy > 0:
        if engine.event_pool.current is not None:
            res = resolve_pending_event(engine, strat)
            if engine.event_pool.current is not None:
                TRACE.text(f"  事件未能结算（{res.get('error') if res else '未知'}），跳过")
                break
            continue
        p = state.player
        if p is None:
            break
        if plan_idx < len(plan):
            spec = plan[plan_idx]
            plan_idx += 1
            act = spec[0]
            if act == "休整":
                tier = spec[1]
                base = {1: 8, 2: 24, 3: 48}[tier]
                cost = {1: 0, 2: 10, 3: 25}[tier]
                if state.shards < cost:
                    continue
                alloc = [{"target_ref": "player:0", "amount": base + state.rest_heal_bonus}]
                r = engine.execute_action("pre_battle_action", {
                    "sub_action": "休整", "tier": tier, "heal_allocations": alloc})
                if r.get("success"):
                    TRACE.text(f"  局外[休整{tier}] 恢复{base + state.rest_heal_bonus}（碎片{state.shards}→{state.shards - cost}）")
                continue
            if act == "学习法术":
                names = spec[1]
                r = engine.execute_action("pre_battle_action", {
                    "sub_action": "学习", "sub": "spell", "tier": len(names), "names": names})
                if r.get("success"):
                    TRACE.text(f"  局外[学习] 学会法术：{names}")
                continue
            if act == "学习道纹":
                name = spec[1]
                r = engine.execute_action("pre_battle_action", {
                    "sub_action": "学习", "sub": "daowen", "tier": 1, "names": [name]})
                if r.get("success"):
                    TRACE.text(f"  局外[学习道纹] {name}")
                continue
            if act == "修行":
                mana = spec[1] if len(spec) > 1 else 1
                r = engine.execute_action("pre_battle_action", {
                    "sub_action": "修行", "tier": 1,
                    "allocations": {"speed_points": 0, "mana_points": mana}})
                if r.get("success"):
                    TRACE.text(f"  局外[修行1] 法力+{2 * mana}限（当前法限{p.mana_limit}）")
                continue
            if act == "领悟":
                r = engine.execute_action("pre_battle_action", {
                    "sub_action": "领悟", "resonance_type": spec[1]})
                if r.get("success"):
                    TRACE.text(f"  局外[领悟] 残韵+{spec[1]}")
                continue
            if act == "共鸣":
                r = engine.execute_action("pre_battle_action", {"sub_action": "共鸣", "tier": 1})
                if r.get("success"):
                    TRACE.text(f"  局外[共鸣] 遗物发现待选：{list(state.pending_relic_choices)}")
                continue
            if act == "探索":
                r = engine.execute_action("pre_battle_action", {"sub_action": "探索", "tier": 1})
                if r.get("success"):
                    TRACE.text(f"  局外[探索] 触发事件：{engine.event_pool.current}")
                continue
            if act == "附煞":
                mode = spec[1] if len(spec) > 1 else "选择"
                sha = spec[2] if len(spec) > 2 else "冥煞"
                dw = spec[3] if len(spec) > 3 else "杀伐"
                r = engine.execute_action("pre_battle_action", {
                    "sub_action": "附煞", "mode": mode, "sha_qi": sha, "daowen_name": dw})
                if r.get("success"):
                    TRACE.text(f"  局外[附煞] {sha}·{dw}")
                continue
            continue
        # 兜底：休整回满 → 修行
        if p.current_hp < p.blood_limit and state.shards >= 8:
            tier = 3 if state.shards >= 25 else (2 if state.shards >= 10 else 1)
            cost = {1: 0, 2: 10, 3: 25}[tier]
            if state.shards >= cost:
                base = {1: 8, 2: 24, 3: 48}[tier]
                alloc = [{"target_ref": "player:0", "amount": base + state.rest_heal_bonus}]
                r = engine.execute_action("pre_battle_action", {
                    "sub_action": "休整", "tier": tier, "heal_allocations": alloc})
                if r.get("success"):
                    TRACE.text(f"  局外[休整{tier}] 恢复{base + state.rest_heal_bonus}")
                    continue
        r = engine.execute_action("pre_battle_action", {
            "sub_action": "修行", "tier": 1,
            "allocations": {"speed_points": 0, "mana_points": 1}})
        if r.get("success"):
            TRACE.text(f"  局外[修行1] 法力+2限（当前法限{p.mana_limit}）")
            continue
        break
    return state.energy <= 0


def battle_start_choices(engine, strat: Strategy) -> dict:
    """战始可选遗物决策：猩红果实/苍白之花/折速法印/三相残韵盘。"""
    active = {r.name for r in engine.state.relics
              if engine.state.sealed_relics.get(r.name, 0) <= 0}
    p = engine.state.player
    out = {}
    if "猩红果实" in active and p is not None:
        # 流血10换[战终]血限+2（永久成长）。血量健康且本场能承受时用。
        out["猩红果实"] = {"use": p.current_hp >= 30}
        if out["猩红果实"]["use"]:
            TRACE.text("  决策[遗物] 猩红果实：流血10 → 战终血限+2")
    if "苍白之花" in active and p is not None:
        # 疲惫5换[战终]精力+1。速度≥7且法力缺口不大时用。
        out["苍白之花"] = {"use": p.current_speed >= 8 and p.mana_limit >= 10}
        if out["苍白之花"]["use"]:
            TRACE.text("  决策[遗物] 苍白之花：疲惫5 → 战终精力+1")
    if "折速法印" in active and p is not None:
        # 疲惫X换6X法力。速度富余时用（保留≥2速度闪避）。
        x = min(max(0, p.current_speed - 2), 2)
        out["折速法印"] = {"use": x >= 1, "x": max(1, x)}
        if x >= 1:
            TRACE.text(f"  决策[遗物] 折速法印：疲惫{x} → 法力+{6 * x}")
    if "三相残韵盘" in active:
        stock = {k: v for k, v in engine.state.resonance.items() if v >= 1}
        if stock:
            consume = max(stock, key=stock.get)
            out["三相残韵盘"] = {"use": True, "resonance_type": consume}
            TRACE.text(f"  决策[遗物] 三相残韵盘：消耗{consume} → 战终获另两种各1")
        else:
            out["三相残韵盘"] = {"use": False}
    return out


def round_start_choices(engine, strat: Strategy) -> dict:
    """回始可选遗物决策：回锋刀/血契/余火印。"""
    active = {r.name for r in engine.state.relics
              if engine.state.sealed_relics.get(r.name, 0) <= 0}
    p = engine.state.player
    out = {}
    if "回锋刀" in active and p is not None:
        damage = 3 * max(0, p.speed_limit - p.current_speed)
        alive = [i for i, e in enumerate(engine.state.enemies) if e.is_alive]
        if damage > 0 and alive:
            out["回锋刀"] = {"enemy_index": alive[0]}
            TRACE.text(f"  决策[遗物] 回锋刀：回始对敌{alive[0]}造成{damage}伤（速限-当前速）")
    if "血契" in active and p is not None:
        # 流血4X换X法力。血契是"用血换法力"的代价闭环：只在血量非常健康、
        # 且法力缺口真实存在时才用，且只用最小档（X=1，流血4换1法力）。
        x = 0
        if p.current_hp >= 45 and p.current_mana < 8:
            x = 1
        out["血契"] = {"use": x >= 1, "x": max(1, x)}
        if x >= 1:
            TRACE.text(f"  决策[遗物] 血契：流血{4 * x} → 法力+{x}（血量健康且法力缺口真实）")
    if "余火印" in active and p is not None:
        heart = next((item for item in engine.state.consumables
                      if item.kind == "dragon_heart" and item.current_uses >= 1), None)
        x = 0
        if heart is not None and p.current_mana <= p.mana_limit - 2:
            x = min(2, heart.current_uses)
        out["余火印"] = {"use": x >= 1, "x": max(1, x),
                         "heart_name": heart.name if heart is not None else ""}
        if x >= 1:
            TRACE.text(f"  决策[遗物] 余火印：消耗{heart.name}耐久{x} → 法力+{2 * x}")
    return out


def resolve_redemption_if_pending(engine, strat: Strategy) -> None:
    """救赎待结算：怪物≤10%血且无原始道纹 → 接纳（有用）或无视。"""
    state = engine.state
    guard = 0
    while state.pending_redemption and guard < 5:
        pend = state.pending_redemption
        name = pend.get("name", "怪物")
        # 面板=原怪物面板/2；攻击力×次数≥2 才值得接纳（否则白养一个负担）
        atk = pend.get("attack_count", 0) * pend.get("attack_power", 0)
        option = "接纳" if atk >= 2 else "无视"
        TRACE.text(f"  决策[救赎] {name}（{pend.get('attack_count')}×{pend.get('attack_power')}→/2）→ {option}")
        r = engine.execute_action("resolve_redemption", {"option": option, "name": f"微光{name[:4]}"})
        guard += 1
        if not r.get("success"):
            TRACE.text(f"    救赎结算失败: {r.get('error')}")
            break


def play_battle(engine, strat: Strategy, battle_no: int) -> dict:
    """打一场完整战斗。返回 {'victory': bool, 'rounds': int, 'death_reason': str}"""
    state = engine.state
    p = state.player
    resolve_redemption_if_pending(engine, strat)
    bs = engine.execute_action("battle_start", {"relic_choices": battle_start_choices(engine, strat)})
    if not bs.get("success"):
        TRACE.text(f"  第{battle_no}场 战始失败: {bs.get('error')}")
        return {"victory": False, "rounds": 0, "death_reason": f"战始失败 {bs.get('error')}"}
    TRACE.text(f"第{battle_no}场 [战始] 出怪：{bs.get('enemies')}")
    log_panels(engine, "[战始]")
    won = False
    death_reason = ""
    for rnd in range(1, 31):
        p = state.player
        if p is None or not p.is_alive:
            death_reason = death_reason or "轮回者命零"
            break
        if not enemy_list(state):
            won = True
            break
        rs = engine.execute_action("round_start", {"relic_choices": round_start_choices(engine, strat)})
        if not rs.get("success"):
            TRACE.text(f"  第{rnd}回合 回始失败: {rs.get('error')}")
            return {"victory": False, "rounds": rnd, "death_reason": f"回始失败 {rs.get('error')}"}
        TRACE.text(f"第{rnd}回合 [回始] 玩家{state.player.current_hp}/{state.player.blood_limit}血 "
                   f"法{state.player.current_mana} 盾{state.player.shield} 速{state.player.current_speed} | "
                   f"敌{[f'{m.name}:{m.current_hp}' for m in enemy_list(state)]}")
        # ---- 玩家出手 ----
        # 反应法术保留池：本回合留给"受到伤害前/失去生命后"法术的法力。
        # 杀伐X 受 _offense_budget 上限约束，其余 HP 型道纹（血债等）不受限。
        reserve_pool = 0
        if any(s.name in ("先发制人", "后发制人", "借力打力", "生生不息", "以牙还牙", "千刀万剐") for s in p.spells):
            reserve_pool = min(10, p.current_mana)
        p._offense_budget = max(0, p.current_mana - reserve_pool)
        for _ in range(max(1, p.action_count)):
            p = state.player
            if p is None or not p.is_alive:
                break
            if not enemy_list(state):
                break
            act = decide_player_action(engine, strat)
            if act is None:
                break
            if act["action_type"] == "__resonance_done__":
                continue  # 残韵已插队执行，不占出手
            before = p.current_mana
            r = engine.execute_action(act["action_type"], act["params"])
            if not r.get("success"):
                TRACE.text(f"    出手失败: {r.get('error')}")
                break
            if hasattr(p, "_offense_budget"):
                p._offense_budget = max(0, p._offense_budget - (before - p.current_mana))
        p._offense_budget = None
        if not enemy_list(state):
            won = True
            break
        p = state.player
        if p is None or not p.is_alive:
            break
        # ---- 队友：护卫优先（无消耗），输出队友补刀 ----
        decide_ally_actions(engine, strat)
        engine.execute_action("resolve_ally_phases", {})
        # ---- 怪物阶段 ----
        mr = resolve_monster_turn_hand(engine, strat)
        if mr.get("result", {}).get("player_dead"):
            TRACE.text(f"  第{rnd}回合 怪物阶段后玩家阵亡")
            death_reason = "怪物阶段命零"
            break
        # 凡庸/癌变/雕塑等特殊结算由 round_end 处理
        re = engine.execute_action("round_end", {})
        if not re.get("success"):
            TRACE.text(f"  第{rnd}回合 回终失败: {re.get('error')}")
            break
        for ef in (re.get("result", {}).get("effects") or []):
            if isinstance(ef, dict) and ef.get("type") in (
                    "sculpture", "cancer", "proliferation", "debt_bind", "redemption",
                    "凡庸", "mediocrity", "collapse", "rescue"):
                TRACE.text(f"  回终特殊结算：{ef}")
        if re.get("victory_paths"):
            TRACE.text(f"  回终胜利路径：{re.get('victory_paths')}")
            won = True
            break
        p = state.player
        if p is None or not p.is_alive:
            death_reason = death_reason or "回终命零"
            break
    if won and state.player and state.player.is_alive:
        resolve_redemption_if_pending(engine, strat)
        be = engine.execute_action("battle_end", {})
        if not be.get("success"):
            TRACE.text(f"  第{battle_no}场 战终失败: {be.get('error')}")
            return {"victory": False, "rounds": rnd, "death_reason": f"战终失败 {be.get('error')}"}
        guard = 0
        while (be.get("success") and be.get("completed") is False
               and be.get("pending_wage_decisions")):
            pending = {k: v for k, v in state.pending_wage_decisions.items() if v is not None}
            if not pending:
                break
            name = next(iter(pending))
            wage = pending[name]
            if state.shards >= wage:
                engine.execute_action("pay_employee_wage", {"name": name, "decision": "pay"})
            else:
                engine.execute_action("pay_employee_wage", {"name": name, "decision": "refuse"})
            be = engine.execute_action("battle_end", {})
            guard += 1
            if guard > 5:
                break
        reward = be.get("result", {}).get("shard_reward", 0) if be.get("success") else 0
        TRACE.text(f"第{battle_no}场 ✅ 胜利（{rnd}回合） 碎片+{reward} → {state.shards}")
        return {"victory": True, "rounds": rnd, "death_reason": ""}
    TRACE.text(f"第{battle_no}场 ❌ 失败（{rnd}回合） {death_reason}")
    return {"victory": False, "rounds": rnd, "death_reason": death_reason}


def run_playthrough(cfg: dict, max_battles: int = 7) -> dict:
    """完整一次轮回（开局→至多7场）。"""
    global TRACE
    if cfg.get("trace"):
        TRACE = Trace(cfg["trace"])
    seed = cfg.get("seed", 20260821)
    seal = cfg.get("seal_path", f"/tmp/seal_{cfg['name']}.json")
    engine = GameEngine(db_path=tempfile.mktemp(suffix=".db"), rng_seed=seed,
                        sealed_candidate_path=seal)
    TRACE.log(kind="run_start", name=cfg["name"], region=cfg.get("region"), seed=seed,
              strat={k: v for k, v in cfg.get("strategy", {}).items()})
    if not run_setup(engine, cfg):
        return {"victory": False, "battles_won": 0}
    strat = Strategy(**cfg.get("strategy", {}))
    result = {"victory": False, "battles_won": 0, "battles": []}
    for b in range(1, max_battles + 1):
        # 局外
        TRACE.text(f"═ 第{b}场 局外 ═（精力{engine.state.energy}，碎片{engine.state.shards}）")
        decide_pre_battle(engine, strat, shards_budget=engine.state.shards)
        # 战斗
        br = play_battle(engine, strat, b)
        result["battles"].append(br)
        if br["victory"]:
            result["battles_won"] += 1
        else:
            TRACE.text(f"轮回结束：第{b}场失败（{br['death_reason']}）")
            break
    p = engine.state.player
    if result["battles_won"] >= max_battles and p and p.is_alive:
        result["victory"] = True
        TRACE.text("第7场胜利 → 触发【最终的冠冕】")
    TRACE.log(kind="run_end", victory=result["victory"],
              battles_won=result["battles_won"],
              shards=engine.state.shards if engine.state.player else -1,
              daowen=sorted(p.dao_wen) if p else [],
              relics=[r.name for r in engine.state.relics],
              spells=[s.name for s in p.spells] if p else [],
              hp=f"{p.current_hp}/{p.blood_limit}" if p else "-")
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="测试者甲")
    parser.add_argument("--region", default="扭曲都市", choices=["扭曲都市", "罪孽都市", "龙心谷", "乱葬岗"])
    parser.add_argument("--seed", type=int, default=20260821)
    parser.add_argument("--battles", type=int, default=7)
    parser.add_argument("--config", default="", help="策略JSON文件路径（可省）")
    args = parser.parse_args()
    cfg = {"name": args.name, "region": args.region, "seed": args.seed}
    if args.config and os.path.exists(args.config):
        with open(args.config, encoding="utf-8") as f:
            cfg.update(json.load(f))
    res = run_playthrough(cfg, max_battles=args.battles)
    print(json.dumps(res, ensure_ascii=False, indent=1))
