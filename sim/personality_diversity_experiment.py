#!/usr/bin/env python3
"""千人千面·长期分化验证实验(2026-08-26)。

目标:验证"相同初始条件下,角色因实际行为形成不同性格,不同性格影响后续决策,
后续决策继续塑造性格"的闭环是否自然成立——**不是**验证"某局面不同性格选不同牌"。

方法:
  - N 个角色,初始条件完全相同(属性/道纹/装备/资源/副本/敌人构成逻辑/可见信息),
    唯一区别=随机种子(影响发现候选/出怪/局外选择/骰点);
  - 不预设任何性格;人格全部由 engine.personality 系统根据**实际行为证据**推断
    (证据抽取规则表见本文件 EVIDENCE_RULES,全部透明、写进报告);
  - 每手决策完整记录(前后局势/资源/行为类别/证据),按场次快照性格;
  - 五项核心验证:人格分化 / 行为分化 / 同局面决策分布 / 长期稳定性 /
    行为→性格→行为闭环;外加"新公式化源"扫描。

不修改任何正式运行逻辑:实验通过 ai_cls 注入 TrackedTacticalAI(纯观察者),
复用 sim.build_learner.play 驱动,结论按预注册阈值(VERDICT_THRESHOLDS)判定,
不为通过验收调整任何结果。

用法:
  PYTHONPATH=. python3 -m sim.personality_diversity_experiment --characters 50
  PYTHONPATH=. python3 -m sim.personality_diversity_experiment --characters 5 --quick
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.ai_tactics import TacticalAI
from engine.personality import TRAIT_DIMENSIONS
import sim.build_learner as bl

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS_DIR = ROOT / "data" / "experiments"
DATE_TAG = "2026-08-26"

TRAIT_KEYS = list(TRAIT_DIMENSIONS.keys())

# ---------------------------------------------------------------------------
# 预注册验收标准(实验前写死;不得为通过验收事后调整)
# ---------------------------------------------------------------------------
VERDICT_THRESHOLDS = {
    "entropy_min_A": 0.60,        # 同局面决策分布归一熵
    "max_share_max_A": 0.50,      # 同局面最大群体占比
    "trait_std_min_A": 0.15,      # 至少 N 个维度的人格 std 下限
    "trait_std_dims_A": 5,
    "behavior_dist_min_A": 0.10,  # 角色间平均行为距离
    "closure_rate_min_A": 0.15,   # 性格改变同局面选择的对照差异率
    "entropy_min_B": 0.35,
    "max_share_max_B": 0.75,
    "trait_std_min_B": 0.10,
    "trait_std_dims_B": 3,
    "max_share_C": 0.75,          # 超过此占比=整体仍公式化
    "trait_std_C": 0.10,
}

# ---------------------------------------------------------------------------
# 行为 → 性格证据规则表(透明;方向/维度/触发条件全部公开)
# 每场每维度证据上限 EVIDENCE_CAP 条,防止单一维度被灌满。
# ---------------------------------------------------------------------------
EVIDENCE_CAP = 3

EVIDENCE_RULES = {
    "risk_preference": [
        "自伤≥3 或 血限损失>0 的行动 → +1(承担风险)",
        "威胁≥50%血限的窗口中选择防御/回复(无损) → -1(选择安全)",
    ],
    "exploration_desire": [
        "首次使用某道纹 → +1(探索新手段)",
        "同一场内第4次起重复同一张已用≥3次的牌 → -1(路径依赖)",
    ],
    "resource_view": [
        "单手消耗≥70%法限 或 ≥5碎片 → -1(大额支出)",
        "法力<30%时选择 X≤2 的低费行动 → +1(紧缩用度)",
    ],
    "decision_habit": [
        "高压窗口首手选择 X≤2 试探性输出 → +1(先观察)",
        "高压窗口满预算(≥80%预算)输出 → -1(果断全押)",
    ],
    "emotional_stability": [
        "血<35%仍执行自伤行动 → -1(低血不稳)",
        "血<35%选择防御/回复 → +1(低血自稳)",
    ],
    "expression_style": [
        "直接输出(敌方掉血) → +1(直球)",
        "战术/增益牌(敌方无损、自身无损) → -1(迂回)",
    ],
    "reaction_pattern": [
        "威胁较上回合跳升≥50%后的首手为无损防御 → +1(从容应对)",
        "威胁跳升≥50%后的首手为自伤强攻 → -1(应激硬拼)",
    ],
    "interpersonal_tendency": [
        "接纳救赎朋友 → +1(信任陌生人);本实验单人无友军互动,证据稀疏属环境限制",
    ],
    "moral_baseline": [
        "喂养/利用敌方道纹(残韵反转强化自身) → -1(功利);无友军牺牲场景,证据稀疏属环境限制",
    ],
}


def _alive_enemy_hp(state) -> int:
    return sum(e.current_hp for e in state.enemies if e.is_alive)


def _threat(state) -> int:
    return sum(e.attack_count * e.attack_power for e in state.enemies if e.is_alive)


def _action_facts(result):
    """从引擎结果提取 (类别, 道纹名, X, 目标)。"""
    if not result or not result.get("success"):
        return ("skip", "", 0, "")
    action = result.get("action", "")
    calc = result.get("calculation", {}) or {}
    name = calc.get("dao_wen", "") or calc.get("daowen", "")
    x = calc.get("x", 0) or 0
    target = calc.get("target", "") or ""
    if result.get("action_type") == "use_resonance" or action.startswith("残韵"):
        return ("resonance", name or action, x, target)
    if action.startswith("发动道纹") or name:
        return ("daowen", name, x, target)
    if "消耗品" in action:
        return ("consumable", action, 0, "")
    return ("other", action, x, target)


class BehaviorObserver:
    """行为证据抽取器:每手决策 → (维度, 方向, 依据) → engine.update_personality。

    推断算法(EMA/置信度/证据计数)全部在 engine.personality 系统内;
    本类只判定"这一手行为构成哪一维的什么方向的证据"(先射箭后画靶的"箭")。
    """

    def __init__(self, engine):
        self.engine = engine
        self.cap: dict[tuple, int] = {}          # (battle, dim) -> 已记条数
        self.recorded: list[dict] = []           # 本手记录的证据(进轨迹)

    def _note(self, dim, direction, evidence, battle):
        if self.cap.get((battle, dim), 0) >= EVIDENCE_CAP:
            return
        r = self.engine.update_personality(
            self.engine.state.player, dim, direction, evidence=evidence)
        if r.get("success"):
            self.cap[(battle, dim)] = self.cap.get((battle, dim), 0) + 1
            self.recorded.append({"dim": dim, "dir": direction, "evidence": evidence})

    def consume(self, prev, post, result, used_before, battle, threat_prev_round):
        self.recorded = []
        category, name, x, _target = _action_facts(result)
        p = self.engine.state.player
        if p is None or not p.is_alive:
            return
        hp_loss = prev["hp"] - post["hp"]
        bl_loss = prev["bl"] - post["bl"]
        enemy_dmg = prev["enemy_hp"] - post["enemy_hp"]
        shield_gain = post["shield"] - prev["shield"]
        mana_spent = prev["mana"] - post["mana"]
        threat = _threat(self.engine.state)
        hp_ratio = post["hp"] / max(1, post["bl"])
        high_pressure = threat >= post["hp"] * 0.5
        desc = f"{name or category}X={x}" if name else category

        # risk_preference
        if hp_loss >= 3 or bl_loss > 0:
            self._note("risk_preference", +1, f"{desc} 自伤{hp_loss}/血限-{bl_loss}", battle)
        elif high_pressure and category == "daowen" and (shield_gain > 0 or hp_loss < 0):
            self._note("risk_preference", -1, f"高压窗口以{desc}无损设防", battle)
        # exploration_desire
        if category == "daowen" and name and name not in used_before:
            self._note("exploration_desire", +1, f"首次使用【{name}】", battle)
        elif (category == "daowen" and name
              and used_before.get(name, 0) >= 3):
            self._note("exploration_desire", -1, f"第{used_before[name] + 1}次重复【{name}】", battle)
        # resource_view
        if mana_spent >= 0.7 * max(1, p.mana_limit):
            self._note("resource_view", -1, f"{desc} 单手耗{mana_spent}法力", battle)
        elif prev["mana"] < 0.3 * max(1, p.mana_limit) and 0 < x <= 2:
            self._note("resource_view", +1, f"法力{prev['mana']}时低费{desc}", battle)
        # decision_habit
        if high_pressure and category == "daowen" and enemy_dmg > 0:
            if 0 < x <= 2:
                self._note("decision_habit", +1, f"高压窗口小X试探({desc})", battle)
            elif mana_spent >= 0.8 * max(1, prev.get("budget", mana_spent)):
                self._note("decision_habit", -1, f"高压窗口满预算输出({desc})", battle)
        # emotional_stability
        if hp_ratio < 0.35:
            if hp_loss >= 3:
                self._note("emotional_stability", -1, f"低血仍自伤({desc})", battle)
            elif shield_gain > 0 or hp_loss < 0:
                self._note("emotional_stability", +1, f"低血自稳({desc})", battle)
        # expression_style
        if category == "daowen":
            if enemy_dmg > 0:
                self._note("expression_style", +1, f"直球输出({desc})", battle)
            elif shield_gain == 0 and hp_loss == 0 and enemy_dmg == 0:
                self._note("expression_style", -1, f"迂回战术({desc})", battle)
        # reaction_pattern: 威胁跳升后的首手
        if (threat_prev_round and threat >= 1.5 * threat_prev_round
                and not getattr(self, "_reacted_this_jump", False)):
            if shield_gain > 0 and hp_loss == 0:
                self._note("reaction_pattern", +1, f"威胁跳升后从容设防({desc})", battle)
                self._reacted_this_jump = True
            elif hp_loss >= 3:
                self._note("reaction_pattern", -1, f"威胁跳升后应激硬拼({desc})", battle)
                self._reacted_this_jump = True
        # interpersonal_tendency: 救赎
        if category != "skip" and result and "救赎" in str(result.get("action", "")):
            if "接纳" in str(result.get("action", "")):
                self._note("interpersonal_tendency", +1, "接纳救赎者入队", battle)


class TrackedTacticalAI(TacticalAI):
    """观察者包装:逐手记录轨迹并喂给 personality 系统;不改任何决策逻辑。"""

    def __init__(self, engine, verbose=False):
        super().__init__(engine, verbose)
        self.observer = BehaviorObserver(engine)
        self.decisions: list[dict] = []
        self.trait_snapshots: list[dict] = []
        self._snap_battle = None
        self._threat_prev_round = 0
        self._decision_index = 0

    # --- 快照工具 ---
    def _facts(self):
        st = self.engine.state
        p = st.player
        return {
            "hp": p.current_hp, "bl": p.blood_limit, "shield": p.shield,
            "mana": p.current_mana, "speed": p.current_speed,
            "enemy_hp": _alive_enemy_hp(st), "threat": _threat(st),
            "budget": self.mana_budget(),
            "daowen_usable": sorted(n for n, inst in p.dao_wen.items()
                                    if inst is not None and inst.can_use()),
            "shards": st.shards,
        }

    def _snapshot_traits(self, battle):
        st = self.engine.state
        p = st.player
        per = st.personality_traits.get(p.runtime_id) if p else None
        self.trait_snapshots.append({
            "battle": battle,
            "traits": copy.deepcopy((per or {}).get("traits", {})),
        })

    def new_round(self):
        current_battle = self.engine.state.current_battle
        if current_battle != self._snap_battle:
            self._snapshot_traits(current_battle)
            self._snap_battle = current_battle
            self.observer.cap = {}            # 每场重置证据上限
            self._reacted_this_jump = False
        self._threat_prev_round = _threat(self.engine.state)
        super().new_round()

    def take_action(self):
        prev = self._facts()
        used_before = dict(self.used)
        result = super().take_action()
        post = self._facts()
        category, name, x, target = _action_facts(result)
        self.observer.consume(prev, post, result, used_before,
                              self.engine.state.current_battle,
                              self._threat_prev_round)
        enemy_dmg = prev["enemy_hp"] - post["enemy_hp"]
        self._decision_index += 1
        self.decisions.append({
            "i": self._decision_index,
            "battle": self.engine.state.current_battle,
            "round": self.engine.state.current_round,
            "action": (result or {}).get("action", "跳过/无"),
            "category": category, "daowen": name, "x": x, "target": target,
            "risk": {
                "hp": prev["hp"], "hp_after": post["hp"],
                "shield": prev["shield"], "shield_after": post["shield"],
                "mana": prev["mana"], "mana_after": post["mana"],
                "enemy_hp": prev["enemy_hp"], "enemy_hp_after": post["enemy_hp"],
                "threat": prev["threat"], "shards": prev["shards"],
                "shards_after": post["shards"],
                "daowen_usable": prev["daowen_usable"],
            },
            "behavior": {
                "attack": enemy_dmg > 0,
                "defense": post["shield"] > prev["shield"] or post["hp"] > prev["hp"],
                "finish": post["enemy_hp"] < prev["enemy_hp"]
                          and post["enemy_hp"] == 0,
                "risky": (prev["hp"] - post["hp"]) >= 3
                         or (prev["bl"] - post["bl"]) > 0,
                "exploratory": bool(name) and name not in used_before,
                "mana_spent": prev["mana"] - post["mana"],
                "shards_spent": prev["shards"] - post["shards"],
                "high_pressure": prev["threat"] >= prev["hp"] * 0.5,
            },
            "evidence": list(self.observer.recorded),
        })
        return result


# ---------------------------------------------------------------------------
# 单角色运行(完全相同初始条件;唯一区别=seed)
# ---------------------------------------------------------------------------
CONFIG = {
    "starter": "杀伐",
    "learn": ["庇护", "再生"],
    "region": "扭曲都市",
    "attrs": {"blood_points": 6, "speed_points": 8, "mana_points": 11},
    "resonance": "反转",
    "battles": 7,
    "policy": "DEFAULT_POLICY(局外按权重随机,随机源=角色种子)",
}


def run_character(cid: int, seed: int, battles: int = 7) -> dict:
    for attempt in range(3):
        ai_holder = {}

        def make_ai(engine, verbose=False, _h=ai_holder):
            ai = TrackedTacticalAI(engine, verbose)
            _h["ai"] = ai
            return ai

        result = bl.play(
            starter=CONFIG["starter"], learn=list(CONFIG["learn"]),
            region=CONFIG["region"], seed=seed, battles=battles,
            policy=None, ai_cls=make_ai,
            attrs=dict(CONFIG["attrs"]), resonance=CONFIG["resonance"])
        ai = ai_holder.get("ai")
        if ai is None or result.get("invalid"):
            seed = seed + 1_000_000      # 无效局换种子重试(如实记录)
            continue
        ai._snapshot_traits(battles + 1)  # 终局快照(阵亡者为空——命零即清除是系统规则)
        # 最终人格 = 最后一个非空快照(阵亡者取死亡前人格;通关者取终局人格)
        final_traits = {}
        for snap in ai.trait_snapshots:
            if snap["traits"]:
                final_traits = snap["traits"]
        return {
            "cid": cid, "seed": seed - attempt * 1_000_000, "retries": attempt,
            "cleared": result.get("cleared", 0), "won": bool(result.get("won")),
            "survived": bool(result.get("won")),
            "final_daowen": result.get("final_daowen", []),
            "decisions": ai.decisions,
            "trait_snapshots": ai.trait_snapshots,
            "used_final": {k: v for k, v in ai.used.items()
                           if not str(k).startswith(("buff:", "debuff:"))},
            "final_traits": copy.deepcopy(final_traits),
        }
    return {"cid": cid, "seed": seed, "retries": 3, "invalid": True,
            "cleared": 0, "won": False, "survived": False, "final_daowen": [],
            "decisions": [], "trait_snapshots": [], "used_final": {},
            "final_traits": {}}


# ---------------------------------------------------------------------------
# 统计:验证1 人格分化
# ---------------------------------------------------------------------------
def trait_stats(chars):
    out = {}
    for dim in TRAIT_KEYS:
        scores, confs, counts = [], [], []
        for c in chars:
            entry = c["final_traits"].get(dim)
            if entry:
                scores.append(entry["score"])
                confs.append(entry["confidence"])
                counts.append(entry["evidence_count"])
        n = len(chars)
        formed = len(scores)
        if scores:
            out[dim] = {
                "formed": formed, "unformed": n - formed,
                "mean": round(statistics.fmean(scores), 4),
                "std": round(statistics.pstdev(scores), 4) if formed > 1 else 0.0,
                "min": round(min(scores), 4), "max": round(max(scores), 4),
                "histogram": _histogram(scores),
                "avg_confidence": round(statistics.fmean(confs), 4) if confs else 0,
                "avg_evidence": round(statistics.fmean(counts), 2) if counts else 0,
            }
        else:
            out[dim] = {"formed": 0, "unformed": n, "note": "无角色形成该维度"}
    return out


def _histogram(scores, bins=5):
    lo, hi = -1.0, 1.0
    step = (hi - lo) / bins
    labels = ["[-1,-0.6)", "[-0.6,-0.2)", "[-0.2,0.2]", "(0.2,0.6]", "(0.6,1]"]
    counts = [0] * bins
    for s in scores:
        idx = min(bins - 1, int((s - lo) / step))
        if s > 0.2:
            idx = 3 if s <= 0.6 else 4
        elif s > -0.2:
            idx = 2
        elif s > -0.6:
            idx = 1
        else:
            idx = 0
        counts[idx] += 1
    return {label: c for label, c in zip(labels, counts)}


# ---------------------------------------------------------------------------
# 统计:验证2 行为分化 + 距离
# ---------------------------------------------------------------------------
def behavior_features(char) -> dict:
    ds = [d for d in char["decisions"] if d["category"] != "skip"]
    n = max(1, len(ds))
    high = [d for d in ds if d["behavior"]["high_pressure"]]
    defense_high = [d for d in high if d["behavior"]["defense"]]
    xs = [d["x"] for d in ds if d["x"]]
    return {
        "decisions": len(char["decisions"]),
        "attack_rate": round(sum(1 for d in ds if d["behavior"]["attack"]) / n, 4),
        "defense_rate": round(sum(1 for d in ds if d["behavior"]["defense"]) / n, 4),
        "finish_rate": round(sum(1 for d in ds if d["behavior"]["finish"]) / n, 4),
        "explore_rate": round(sum(1 for d in ds if d["behavior"]["exploratory"]) / n, 4),
        "risky_rate": round(sum(1 for d in ds if d["behavior"]["risky"]) / n, 4),
        "resonance_rate": round(sum(1 for d in ds if d["category"] == "resonance") / n, 4),
        "skip_rate": round(
            sum(1 for d in char["decisions"] if d["category"] == "skip")
            / max(1, len(char["decisions"])), 4),
        "avg_mana_spend": round(
            statistics.fmean([d["behavior"]["mana_spent"] for d in ds]) if ds else 0, 3),
        "high_pressure_defense_rate": round(
            len(defense_high) / max(1, len(high)), 4),
        "avg_x": round(statistics.fmean(xs), 3) if xs else 0,
        "cleared": char["cleared"],
    }


FEATURE_KEYS = ["attack_rate", "defense_rate", "finish_rate", "explore_rate",
                "risky_rate", "resonance_rate", "skip_rate", "avg_mana_spend",
                "high_pressure_defense_rate", "avg_x", "cleared"]


def pairwise_distances(features: list[dict]):
    vecs = [[f[k] for k in FEATURE_KEYS] for f in features]
    n = len(vecs)
    dists = []
    for i in range(n):
        for j in range(i + 1, n):
            dists.append(math.dist(vecs[i], vecs[j]))
    if not dists:
        return {"mean": 0.0, "max": 0.0, "min": 0.0, "pairs": 0}
    return {"mean": round(statistics.fmean(dists), 4),
            "max": round(max(dists), 4), "min": round(min(dists), 4),
            "pairs": len(dists)}


# ---------------------------------------------------------------------------
# 验证3:同局面反复测试
# ---------------------------------------------------------------------------
def _standard_situation_engine():
    """构造完全相同的标准局面(固定面板/道纹/敌人),供所有角色注入性格后决策。"""
    from tests.setup_support import finish_initial_daowen
    from engine.api import GameEngine
    from engine.models import DaoWen, DaoWenInstance, Entity
    e = GameEngine(db_path="/tmp/pde_standard.db", rng_seed=999,
                   sealed_candidate_path="/tmp/pde_sealed.json",
                   death_book_path="/tmp/pde_book.md")
    e.execute_action("setup_attributes", {
        "name": "受试者", "blood_points": 6, "speed_points": 8, "mana_points": 11})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.energy = 0
    e.execute_action("battle_start", {"relic_choices": {}})
    e.execute_action("round_start", {})
    p = e.state.player
    # 血限=6血点→36;HP 必须≤血限(否则被引擎钳位污染 diff)。24=2/3血限,
    # 威胁23≈96%HP,构成明确高压窗口,防御/输出/试探都是合法争胜选项。
    p.current_hp, p.current_mana = min(24, p.blood_limit), 20
    for n in ("庇护", "再生"):
        p.dao_wen[n] = DaoWenInstance(DaoWen(
            name=n, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    e.state.enemies.clear()
    e.state.enemies.extend([
        Entity(name="石背熊", entity_type="怪物", blood_limit=252, current_hp=252,
               attack_count=4, attack_power=5),
        Entity(name="木桩乙", entity_type="怪物", blood_limit=40, current_hp=40,
               attack_count=1, attack_power=3),
    ])
    return e


def same_situation_test(chars, with_traits=True, steps=3):
    """所有角色在同一局面决策;保留各自的性格与 used 经历。返回动作序列分布。"""
    from collections import Counter
    sequences = []
    for char in chars:
        e = _standard_situation_engine()
        p = e.state.player
        if with_traits and char["final_traits"]:
            e.state.personality_traits[p.runtime_id] = {
                "name": p.name, "traits": copy.deepcopy(char["final_traits"])}
        ai = TacticalAI(e)
        # 注入该角色的真实使用经历(消除首试配额干扰,让"性格+经历"主导)
        ai.used = {k: v for k, v in char["used_final"].items()}
        seq = []
        for _ in range(steps):
            r = ai.take_action()
            if not r:
                break
            calc = r.get("calculation", {}) or {}
            seq.append(calc.get("dao_wen", "") or r.get("action", "?")[:12])
        sequences.append(tuple(seq) if seq else ("(无行动)",))
    counter = Counter(sequences)
    total = len(sequences)
    dist = {" > ".join(k): v for k, v in counter.most_common()}
    probs = [v / total for v in counter.values()]
    entropy = -sum(q * math.log2(q) for q in probs if q > 0) if probs else 0.0
    max_ent = math.log2(total) if total > 1 else 1.0
    return {
        "distribution": dist,
        "distinct": len(counter),
        "max_share": round(max(probs), 4) if probs else 1.0,
        "normalized_entropy": round(entropy / max(max_ent, 1e-9), 4),
    }


# ---------------------------------------------------------------------------
# 验证4:长期稳定性(高压窗口防御倾向 按风险人格分桶)
# ---------------------------------------------------------------------------
def stability_check(chars):
    buckets = {"求稳(risk<-0.3)": [], "中间": [], "冒险(risk>0.3)": []}
    for c in chars:
        entry = c["final_traits"].get("risk_preference")
        if not entry:
            continue
        feats = behavior_features(c)
        rate = feats["high_pressure_defense_rate"]
        if entry["score"] < -0.3:
            buckets["求稳(risk<-0.3)"].append(rate)
        elif entry["score"] > 0.3:
            buckets["冒险(risk>0.3)"].append(rate)
        else:
            buckets["中间"].append(rate)
    out = {}
    for name, rates in buckets.items():
        if rates:
            out[name] = {"n": len(rates),
                         "avg_high_pressure_defense_rate": round(statistics.fmean(rates), 4),
                         "all_defensive": all(r >= 0.999 for r in rates),
                         "all_offensive": all(r <= 0.001 for r in rates)}
        else:
            out[name] = {"n": 0}
    ordered = [out.get(k, {}).get("avg_high_pressure_defense_rate")
               for k in ("求稳(risk<-0.3)", "中间", "冒险(risk>0.3)")]
    vals = [v for v in ordered if v is not None]
    monotonic = len(vals) >= 2 and all(
        vals[i] >= vals[i + 1] for i in range(len(vals) - 1))
    out["_monotonic_defense_by_risk"] = bool(monotonic)
    out["_no_absolute_lock"] = not any(
        v.get("all_defensive") or v.get("all_offensive")
        for v in out.values() if isinstance(v, dict))
    return out


# ---------------------------------------------------------------------------
# 验证5:行为→性格→行为闭环
# ---------------------------------------------------------------------------
def loop_closure(chars, same_with, same_without):
    # (a) 滞后相关:第k场风险人格 vs 第k+1场自伤率(跨角色)
    pairs = []
    for c in chars:
        snaps = c["trait_snapshots"]
        for i in range(len(snaps) - 1):
            risk = (snaps[i]["traits"].get("risk_preference") or {}).get("score")
            nxt = [d for d in c["decisions"]
                   if d["battle"] == snaps[i + 1]["battle"]
                   and d["category"] != "skip"]
            if risk is None or not nxt:
                continue
            risky = sum(1 for d in nxt if d["behavior"]["risky"]) / len(nxt)
            pairs.append((risk, risky))
    lag_corr = _pearson([p[0] for p in pairs], [p[1] for p in pairs]) if len(pairs) >= 3 else None
    # (b) 对照差异率:同局面 有性格 vs 无性格 选择不同 的角色占比
    with_seq = same_with["distribution"]
    without_seq = same_without["distribution"]
    # 逐角色比较需要序列;重新计算(轻量):直接用分布差估算
    common = set(with_seq) | set(without_seq)
    total_w = sum(with_seq.values())
    diff = sum(abs(with_seq.get(k, 0) - without_seq.get(k, 0)) for k in common) / 2
    closure_rate = round(diff / max(1, total_w), 4)
    # (c) 人格强度与差异的相关:用 std 代理(有分化的维度越多,差异应越大)
    return {
        "lagged_risk_vs_next_battle_risky_rate": (
            round(lag_corr, 4) if lag_corr is not None else None),
        "lag_pairs": len(pairs),
        "same_situation_shift_rate_vs_no_personality": closure_rate,
        "note": "shift_rate>0 且滞后相关同号 → 行为塑造性格、性格改变后续行为",
    }


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    vy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy)


# ---------------------------------------------------------------------------
# 验证9:新公式化源扫描
# ---------------------------------------------------------------------------
def formulaic_scan(chars, same_without):
    from collections import Counter
    first_actions = Counter()
    for c in chars:
        for d in c["decisions"]:
            if d["category"] != "skip":
                first_actions[d["daowen"] or d["action"][:10]] += 1
                break
    trait_std = {dim: st["std"] for dim, st in trait_stats(chars).items()
                 if "std" in st}
    dims_with_evidence = [d for d, st in trait_stats(chars).items()
                          if st.get("formed", 0) > 0]
    return {
        "first_action_distribution": dict(first_actions.most_common()),
        "first_action_distinct": len(first_actions),
        "no_personality_baseline_same_situation": same_without,
        "dims_with_any_evidence": dims_with_evidence,
        "dims_never_formed": [d for d in TRAIT_KEYS if d not in dims_with_evidence],
        "trait_std_summary": trait_std,
    }


# ---------------------------------------------------------------------------
# 验收判定(预注册阈值)
# ---------------------------------------------------------------------------
def verdict(trait, beh_dist, same_with, closure):
    t = VERDICT_THRESHOLDS
    dims_A = sum(1 for v in trait.values()
                 if isinstance(v, dict) and v.get("std", 0) >= t["trait_std_min_A"])
    dims_B = sum(1 for v in trait.values()
                 if isinstance(v, dict) and v.get("std", 0) >= t["trait_std_min_B"])
    entropy = same_with["normalized_entropy"]
    share = same_with["max_share"]
    checks = {
        "A_entropy": entropy >= t["entropy_min_A"],
        "A_max_share": share <= t["max_share_max_A"],
        "A_trait_dims": dims_A >= t["trait_std_dims_A"],
        "A_behavior_dist": beh_dist["mean"] >= t["behavior_dist_min_A"],
        "A_closure": closure["same_situation_shift_rate_vs_no_personality"]
        >= t["closure_rate_min_A"],
        "B_entropy_or_share": entropy >= t["entropy_min_B"]
                              or share <= t["max_share_max_B"],
        "B_trait_dims": dims_B >= t["trait_std_dims_B"],
    }
    if all(checks[k] for k in ("A_entropy", "A_max_share", "A_trait_dims",
                               "A_behavior_dist", "A_closure")):
        grade = "A"
    elif checks["B_entropy_or_share"] and checks["B_trait_dims"]:
        grade = "B"
    elif share > t["max_share_C"] and dims_B >= 1:
        grade = "C"
    else:
        grade = "D"
    return grade, checks


def diagnose_convergence(same_with, trait, scan):
    """若趋同,定位机制(不修改任何结果,只做归因)。"""
    reasons = []
    if same_with["max_share"] > VERDICT_THRESHOLDS["max_share_max_B"]:
        top = list(same_with["distribution"].items())[:3]
        reasons.append(f"同局面最大群体占比 {same_with['max_share']:.0%}"
                       f"(头部序列:{top})——评分确定性+局势主导,性格权重不足以翻转")
    weak_dims = [d for d, v in trait.items()
                 if isinstance(v, dict) and v.get("std", 0) < 0.10]
    if weak_dims:
        reasons.append("人格 std<0.10 的维度:" + "、".join(weak_dims)
                       + " —— 证据源稀疏或观察规则触发率低")
    never = scan["dims_never_formed"]
    if never:
        reasons.append("从未形成证据的维度:" + "、".join(never)
                       + " —— 单人副本无社交/牺牲场景,环境限制")
    if scan["first_action_distinct"] <= 2:
        reasons.append("首手行动分布高度集中(首试配额+确定性评分的趋同源)")
    return reasons


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_experiment(n_chars: int, battles: int = 7, quick: bool = False):
    t0 = time.time()
    print(f"[实验] {n_chars} 角色 × {battles} 场战斗,相同初始条件,仅种子不同")
    chars = []
    for cid in range(n_chars):
        seed = 20260826 + cid * 7919
        c = run_character(cid, seed, battles=battles)
        chars.append(c)
        if cid % 10 == 9 or cid == n_chars - 1:
            done = cid + 1
            print(f"  完成 {done}/{n_chars}"
                  f"(均决策 {statistics.fmean([len(x['decisions']) for x in chars if x['decisions']] or [0]):.0f} 手,"
                  f"耗时 {time.time() - t0:.0f}s)")
    valid = [c for c in chars if not c.get("invalid")]
    print(f"[实验] 有效角色 {len(valid)}/{n_chars}")

    print("[验证1] 人格分化统计…")
    trait = trait_stats(valid)
    print("[验证2] 行为特征与距离…")
    feats = [behavior_features(c) for c in valid]
    beh_dist = pairwise_distances(feats)
    print("[验证3] 同局面决策分布(保留各自性格+经历)…")
    same_with = same_situation_test(valid, with_traits=True)
    print("[验证3-对照] 同局面(清除性格基线)…")
    same_without = same_situation_test(valid, with_traits=False)
    print("[验证4] 长期稳定性…")
    stability = stability_check(valid)
    print("[验证5] 行为→性格→行为闭环…")
    closure = loop_closure(valid, same_with, same_without)
    print("[验证9] 新公式化源扫描…")
    scan = formulaic_scan(valid, same_without)
    grade, checks = verdict(trait, beh_dist, same_with, closure)
    reasons = diagnose_convergence(same_with, trait, scan) if grade in ("B", "C", "D") else []

    elapsed = round(time.time() - t0, 1)
    report = {
        "date": DATE_TAG,
        "config": {**CONFIG, "battles": battles,
                   "n_characters": n_chars, "valid_characters": len(valid),
                   "evidence_rules": EVIDENCE_RULES,
                   "evidence_cap_per_dim_per_battle": EVIDENCE_CAP,
                   "verdict_thresholds": VERDICT_THRESHOLDS},
        "elapsed_seconds": elapsed,
        "total_decisions": sum(len(c["decisions"]) for c in valid),
        "trait_stats": trait,
        "behavior_features": [
            {"cid": c["cid"], **f} for c, f in zip(valid, feats)],
        "behavior_distance": beh_dist,
        "same_situation_with_personality": same_with,
        "same_situation_no_personality_baseline": same_without,
        "stability": stability,
        "loop_closure": closure,
        "formulaic_scan": scan,
        "verdict": {"grade": grade, "checks": checks,
                    "convergence_reasons": reasons},
        "characters_summary": [
            {"cid": c["cid"], "seed": c["seed"], "cleared": c["cleared"],
             "survived": c["survived"], "decisions": len(c["decisions"]),
             "retries": c.get("retries", 0)} for c in valid],
    }
    trajectories = [
        {"cid": c["cid"], "seed": c["seed"], "cleared": c["cleared"],
         "won": c["won"], "final_daowen": c["final_daowen"],
         "final_traits": c["final_traits"],
         "trait_snapshots": c["trait_snapshots"],
         "used_final": c["used_final"],
         "decisions": c["decisions"]} for c in valid]
    return report, trajectories, elapsed


def write_reports(report, trajectories, tag=None):
    EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    tag = tag or DATE_TAG
    j_path = EXPERIMENTS_DIR / f"personality_diversity_report_{tag}.json"
    t_path = EXPERIMENTS_DIR / f"personality_diversity_trajectories_{tag}.json"
    m_path = EXPERIMENTS_DIR / f"personality_diversity_report_{tag}.md"
    j_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    t_path.write_text(json.dumps(trajectories, ensure_ascii=False), encoding="utf-8")
    m_path.write_text(render_markdown(report), encoding="utf-8")
    return j_path, t_path, m_path


def render_markdown(r) -> str:
    v = r["verdict"]
    lines = [
        f"# 千人千面·长期分化验证报告({r['date']})",
        "",
        f"**验收结论:{v['grade']}**(A=明显千人千面 B=有分化仍趋同 C=局部差异整体公式化 D=基本无分化)",
        "",
        f"- 角色:{r['config']['n_characters']}(有效 {r['config']['valid_characters']}),"
        f"决策总数 {r['total_decisions']},耗时 {r['elapsed_seconds']}s",
        f"- 验收检查项:{json.dumps(v['checks'], ensure_ascii=False)}",
        "",
        "## 一、最终人格分布(九维)",
        "",
        "| 维度 | 成型数 | 均值 | 标准差 | 最小 | 最大 | 直方图 |",
        "|---|---|---|---|---|---|---|",
    ]
    for dim, st in r["trait_stats"].items():
        if "std" in st:
            hist = " ".join(f"{k}:{x}" for k, x in st["histogram"].items())
            lines.append(f"| {dim} | {st['formed']} | {st['mean']} | {st['std']} "
                         f"| {st['min']} | {st['max']} | {hist} |")
        else:
            lines.append(f"| {dim} | 0 | - | - | - | - | {st.get('note', '')} |")
    lines += [
        "",
        "## 二、行为分布与角色间距离",
        "",
        f"- 平均行为距离:{r['behavior_distance']['mean']}"
        f"(最大 {r['behavior_distance']['max']},{r['behavior_distance']['pairs']} 对)",
        f"- 首手行动分布:{json.dumps(r['formulaic_scan']['first_action_distribution'], ensure_ascii=False)}",
        "",
        "## 三、同局面决策分布(相同局面,保留各自性格+经历)",
        "",
        f"- 不同行动序列数:{r['same_situation_with_personality']['distinct']},"
        f"最大群体占比 {r['same_situation_with_personality']['max_share']:.0%},"
        f"归一熵 {r['same_situation_with_personality']['normalized_entropy']}",
        f"- 分布:{json.dumps(r['same_situation_with_personality']['distribution'], ensure_ascii=False)}",
        f"- 无性格基线(对照):最大占比 {r['same_situation_no_personality_baseline']['max_share']:.0%},"
        f"熵 {r['same_situation_no_personality_baseline']['normalized_entropy']}",
        "",
        "## 四、长期稳定性(高压窗口防御率,按风险人格分桶)",
        "",
        f"{json.dumps(r['stability'], ensure_ascii=False, indent=1)}",
        "",
        "## 五、行为→性格→行为闭环",
        "",
        f"{json.dumps(r['loop_closure'], ensure_ascii=False, indent=1)}",
        "",
        "## 六、新公式化源扫描",
        "",
        f"- 从未形成证据的维度:{r['formulaic_scan']['dims_never_formed']}",
        f"- 无性格基线同局面:{json.dumps(r['formulaic_scan']['no_personality_baseline_same_situation']['distribution'], ensure_ascii=False)}",
        "",
        "## 七、趋同归因(如未达 A)",
        "",
    ]
    lines += [f"- {x}" for x in v["convergence_reasons"]] or ["- (达到 A 级,无重大趋同归因)"]
    lines += [
        "",
        "## 附:行为→性格证据规则表",
        "",
    ]
    for dim, rules in EVIDENCE_RULES.items():
        lines.append(f"**{dim}**")
        lines += [f"- {x}" for x in rules]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--characters", type=int, default=50)
    ap.add_argument("--battles", type=int, default=7)
    ap.add_argument("--quick", action="store_true", help="快速模式(供测试)")
    ap.add_argument("--tag", default=None)
    args = ap.parse_args()
    battles = 2 if args.quick else args.battles
    report, trajectories, elapsed = run_experiment(args.characters, battles=battles,
                                                   quick=args.quick)
    j_path, t_path, m_path = write_reports(report, trajectories, tag=args.tag)
    print(f"\n[输出] {j_path}\n[输出] {t_path}\n[输出] {m_path}")
    print(f"[结论] {report['verdict']['grade']} | 耗时 {elapsed}s "
          f"| 角色 {report['config']['valid_characters']} "
          f"| 决策 {report['total_decisions']}")


if __name__ == "__main__":
    main()
