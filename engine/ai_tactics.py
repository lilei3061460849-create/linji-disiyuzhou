"""
战术AI：为轮回者在战斗中选择行动。

2026-08-26 防公式化改造（DM裁决）：固定战术表（TACTICAL_ROLES）、固定残韵
目标表（HIGH_VALUE_ENEMY_DAOWEN）与固定策略顺序（STRATEGIES 串）已删除——
所有角色按同一张表出牌必然公式化。

现行决策方式（实时、状态驱动）：
  1. 候选生成：只看玩家**实际持有**的道纹 × 若干X × 合法目标（含残韵候选）；
  2. 逐候选经 ActionPreview 在**真实引擎管线**副本上预演，得到完整后果 diff；
  3. 按「当前局势（威胁/血线/法力/凡庸压力）+ 角色性格（personality_traits，
     实例级、行为推断而来）+ 可见信息（diff 数值）」实时打分；
  4. 最高分执行；预演致轮回者命零（LETHAL）的候选一律拒绝。
AI 不复制任何引擎公式；新增/修改道纹零维护（预演即事实源）。

性格只调制**倾向**（风险代价容忍度/节俭/求新/先观察后行动……），
不是强制规则——同一性格在不同局面可以打出不同牌。

兼容层：旧 try_* 策略名保留为「类别策略」薄封装（类别由预演 diff 归纳，
非历史经验表），供 tests 与 archive 实验（sim/victory_path_tournament 等
子类化 TacticalAI 的脚本）继续运行；基类 STRATEGIES=None，主入口
take_action 走实时评估，不再有固定顺序。
"""
from __future__ import annotations

import math
from typing import Any, Optional
from engine.ai_preview import ActionPreview

# 单次出手最多预演的候选数（性能护栏；候选按新鲜度与威胁优先）
MAX_CANDIDATE_PREVIEWS = 26


# ---------------------------------------------------------------------------
# 道纹效果分类：不查表，从「可见文本」做弱分类（仅用于威胁评估与兼容壳；
# 真正的候选评估一律以预演 diff 数值为准）。
# ---------------------------------------------------------------------------
_KIND_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("remove",   ("移出战斗", "移出", "封印")),
    ("heal",     ("回复", "恢复", "治疗")),
    ("shield",   ("格挡",)),
    ("control",  ("无法行动", "眩晕", "束缚", "蒙蔽", "无法出手", "攻击次数固定",
                  "强制目标", "缄默", "沉默")),
    ("debuff",   ("削弱", "受到伤害+", "失去生命", "衰老", "弱化", "退化", "衰减")),
    ("ramp",     ("重置随机", "获得法力", "法力+")),
    ("buff",     ("获得", "提升", "免疫", "翻倍", "速度+", "血限+")),
    ("damage",   ("伤害",)),
)


def daowen_text_kind(inst) -> str:
    """按道纹可见文本给出弱分类（引擎事实，非经验）。无法判断返回 'other'。"""
    if inst is None:
        return "other"
    dw = getattr(inst, "dao_wen", inst)
    text = " ".join(filter(None, (
        getattr(dw, "formula", ""), getattr(dw, "effect_formula", ""),
        getattr(dw, "trigger_timing", ""), getattr(dw, "cost_type", ""),
    )))
    for kind, keys in _KIND_HINTS:
        if any(k in text for k in keys):
            return kind
    return "other"


# 兼容壳的旧角色名 → 弱分类集合（owned() 用）
_ROLE_KINDS = {
    "nuke": ("damage",), "aoe": ("damage",), "finisher": ("damage",),
    "mark": ("damage",), "shield": ("shield",), "heal": ("heal",),
    "control": ("control", "tactician"), "debuff": ("debuff", "tactician"),
    "buff": ("buff",), "remove": ("remove",), "ramp": ("ramp",),
    "reroll": ("ramp",),
}


class TacticalAI:
    """轮回者战斗AI。engine 为 GameEngine 实例。

    基类无固定策略顺序（STRAGIES=None）：take_action 实时评估候选。
    实验子类可自定义 STRATEGIES 串走旧级联（仅为兼容 archive 实验）。
    """

    # 基类不再有固定顺序；子类（archive 实验脚本）可覆写为方法名元组。
    STRATEGIES: Optional[tuple] = None

    def __init__(self, engine: Any, verbose: bool = False, actor: Any = None,
                 enemies: Optional[list] = None, actor_ref: Optional[str] = None):
        self.engine = engine
        self.verbose = verbose
        # 视角重定向（2026-08-30）：PvP 死斗里挑战者与守擂者都是「轮回者」，共用同一套
        # TacticalAI（残韵候选 + 性格调制 + 变数决策），双方本应走完全相同的决策逻辑。
        # 早期 TacticalAI 硬绑定 player=state.player、enemies=state.enemies，只适用于
        # 挑战者视角；守擂者必须能指定自己的 actor/enemies 才能共享这套机制。
        # actor/enemies 为空时保持原行为（普通战斗/挑战者视角）。
        self._actor = actor
        self._enemies = enemies
        self._actor_ref = actor_ref
        self.log: list[str] = []
        self.used: dict[str, int] = {}   # 统计各道纹发动次数，便于流派对比
        self._controlled_this_round: set = set()   # 本回合已被控制的目标
        self._previewer = None           # 行动后果预演器（惰性创建）
        self.preview_rejected: list[str] = []   # 被安全过滤淘汰的候选
        self._sacrifice_actions: set = set()    # 显式允许的主动牺牲策略（默认空）
        self._last_risk: tuple = ("SAFE", [])   # 最近一次候选的风险等级
        # 第十九批实验钩子（默认 None = 现行≤40%血线门，行为不变）
        self.consumable_gate = None
        self._hp_trace: list = []
        # 实时决策记账
        self._probe_cache: dict = {}     # (name, target, battle) → 预演归纳的分类/单价
        self._ptraits: dict = {}         # 本回合的性格权重缓存（score×confidence）
        self._damage_done_battle = False # 本回合是否已使敌方掉血（凡庸压力）
        self._rounds_since_damage = 0    # 连续未使敌方掉血的回合数
        # 硬伤3（红线 E 已解除 2026-08-30）：对手台词在**我**心里的可信度。
        # 只由我自己的性格算出，不查对手真实状态；None=没听到有实质主张的话。
        self._opponent_read: Optional[dict] = None

    @property
    def previewer(self):
        """行动后果预演器（惰性创建）。"""
        if self._previewer is None:
            from engine.ai_preview import ActionPreview
            self._previewer = ActionPreview(self.engine)
        return self._previewer

    # ---------- 基础工具 ----------

    @property
    def player(self):
        if self._actor is not None:
            return self._actor
        return self.engine.state.player

    def alive_enemies(self) -> list:
        if self._enemies is not None:
            return [e for e in self._enemies if e.is_alive]
        return [e for e in self.engine.state.enemies if e.is_alive]

    def mana(self) -> int:
        return self.player.current_mana

    def owned(self, role: str) -> list[str]:
        """兼容壳：持有道纹中属于该旧角色者（文本弱分类，空文本回退预演归纳）。"""
        kinds = _ROLE_KINDS.get(role, ())
        out = []
        for n, inst in sorted(self.player.dao_wen.items()):
            if inst is None or not inst.can_use():
                continue
            kind = daowen_text_kind(inst)
            if kind == "other":
                kind = (self._probe(n) or {}).get("kind", "other")
            if kind in kinds:
                out.append(n)
        return out

    def incoming_damage(self) -> int:
        return sum(e.attack_count * e.attack_power for e in self.alive_enemies())

    def remaining_actions(self) -> int:
        total = max(1, math.ceil(self.player.speed_limit / 3))
        return max(0, total - getattr(self.player, "actions_used_this_round", 0))

    def mana_budget(self) -> int:
        """按剩余出手次数均分法力，避免一次梭哈导致后续出手闲置。"""
        left = self.remaining_actions()
        if left <= 1:
            return self.mana()
        return max(1, self.mana() // left)

    def _refresh_personality(self) -> None:
        """拉取当前性格权重（实例级；无性格=全 0，退化为纯局势效用）。"""
        from engine.personality import get_personality
        data = get_personality(self.engine.state, self.player) or {}
        self._ptraits = {
            dim: entry["score"] * entry.get("confidence", 0.0)
            for dim, entry in (data.get("traits") or {}).items()
        }

    def _w(self, dim: str) -> float:
        """性格维度权重 ∈ [-1,1]（score×confidence，证据不足自然影响小）。"""
        return self._ptraits.get(dim, 0.0) or 0.0

    # ---------- 硬伤3：读对手的台词（红线 E 已解除） ----------

    def _refresh_opponent_read(self) -> None:
        """读战场公开频道里对手最后一句有实质主张的话，按**我自己的性格**判可信度。

        说话没有数值作用，但**让敌人忌惮**就是最大的作用：A 说自己没法力了，
        我得自己判断那是真的弹尽粮绝，还是在骗我全力压上然后收割。
        信不信只取决于我的性格——引擎绝不代我去查 A 的法力。
        """
        try:
            from engine.dialogue import read_opponent
            self._opponent_read = read_opponent(self.engine.state, self.player,
                                                traits=self._ptraits)
        except Exception:
            self._opponent_read = None

    DIALOGUE_BIAS_CAP = 10.0

    def _dialogue_bias(self, *, enemy_hp_loss: float, shield_useful: float,
                       heal: float, mana_spent: float) -> float:
        """按"我有多信对手那句话"微调本候选的分数（**不产生任何数值效果**）。

        含义（belief>0 = 信以为真；belief<0 = 认为是反话）：
          · 对手喊虚(weak)：信 → 趁势压上收割（进攻加值、龟缩减值）；
                           不信 → 怀疑是引我全力出击再收割（进攻减值、防御加值）。
          · 对手威胁(strong)：信 → 忌惮，加防、少冒进（这就是"让敌人忌惮"）；
                            不信 → 当它是虚张，照打。
          · 对手试探(probe)：不下断言，只是不轻易把法力梭哈在一手上。

        这是倾向而非规则：上限 DIALOGUE_BIAS_CAP=10，压不过 CRITICAL(-30)/
        LETHAL 等安全护栏，只在"压上"与"收手"之间替我把天平拨一点。
        """
        read = self._opponent_read
        if not read:
            return 0.0
        claim = read.get("claim")
        belief = float(read.get("belief") or 0.0)
        if claim not in ("weak", "strong", "probe") or abs(belief) < 0.05:
            return 0.0
        guard = shield_useful + heal
        if claim == "weak":
            bias = 0.9 * belief * enemy_hp_loss - 0.9 * belief * guard
        elif claim == "strong":
            bias = -0.9 * belief * enemy_hp_loss + 1.5 * belief * guard
        else:                                   # probe：不梭哈
            bias = -0.25 * abs(belief) * mana_spent
        return max(-self.DIALOGUE_BIAS_CAP, min(self.DIALOGUE_BIAS_CAP, bias))

    # ---------- 预演与归纳 ----------

    def _probe(self, name: str) -> Optional[dict]:
        """对道纹做多目标 X=1 预演，按实际效果方向归纳其事实特征（缓存至本场）。

        引擎会"默许"部分指向错误的目标（如庇护打向敌人=空效果），因此必须
        对 self / 敌方 等变体各预演一次，取**效果方向最优**的成功变体：
        敌方掉血 → damage；自身获益 → shield/heal/buff；敌方被移除 → remove；
        无面板位移 → tactician（控场/削弱/标记类，目标取该变体目标）。
        返回 {kind, cost_per_x, dmg, shield, heal, target_name}；全拒绝返回 None。
        """
        battle = self.engine.state.current_battle
        cache_key = (name, "*", battle)
        cached = self._probe_cache.get(cache_key)
        if cached is not None:
            return cached if cached.get("target") == "ok" else None

        best: Optional[dict] = None
        best_score = -1.0
        seen_targets = set()
        for target in (self._top_enemy_name(), self.player.name, None,
                       self._ally_name()):
            if target in seen_targets or target is None and None in seen_targets:
                continue
            seen_targets.add(target)
            key = (name, target, battle)
            per_key = self._probe_cache.get(key)
            if per_key is not None and per_key.get("target") == "reject":
                continue
            params: dict = {"daowen_name": name, "x": 1, "dodge": False,
                            "blood_shadow": False}
            if self._actor_ref:
                params["actor_ref"] = self._actor_ref   # 守擂者视角：预演也须指定施法者，否则引擎当玩家动作
            if target:
                params["target"] = target
            pv = self.previewer.preview("use_daowen", params)
            res = pv.get("result") or {}
            if not res.get("success"):
                self._probe_cache[key] = {"target": "reject"}
                continue
            info = self._digest_diff(pv.get("diff", {}))
            info["target"] = "ok"
            info["target_name"] = target
            self._probe_cache[key] = info
            # 效果方向分：敌方掉血 > 自身获益 > 空效果
            orientation = (2.0 * info["dmg"] + info["shield"] + info["heal"]
                           + 0.5 * info.get("mana_gain", 0)
                           + 0.5 * info.get("bl_gain", 0)
                           + (6.0 if info["kind"] == "remove" else 0.0))
            if orientation > best_score:
                best_score = orientation
                best = info
        self._probe_cache[cache_key] = best if best is not None else {"target": "reject"}
        return best

    def _target_ref_for(self, entity) -> str:
        """按实体找其稳定战斗引用（player:0 / enemy:N）。用于 use_resonance 的 target_ref。"""
        refs = self.engine.combat._combat_entity_refs()
        for ref, ent in refs.items():
            if ent is entity:
                return ref
        return getattr(entity, "name", "")

    def _top_enemy_name(self) -> Optional[str]:
        enemies = self.alive_enemies()
        if not enemies:
            return None
        return max(enemies, key=lambda e: e.attack_count * e.attack_power).name

    def _ally_name(self) -> Optional[str]:
        allies = self._allies()
        return allies[0].name if allies else None

    def _digest_diff(self, diff: dict) -> dict:
        """把 X=1 预演 diff 归纳为 {kind, cost_per_x, dmg, shield, heal, ...}。

        kind 判定以**事件流**为准（面板位移会被格挡吸收等遮蔽）：
        damage_applied 命中敌方 = 输出（即使被格挡挡光）；status_applied 落在
        敌方 = 战术牌（控场/削弱），落在自身 = 增益；敌方无伤消失 = 移除。
        """
        p = diff.get("player", {})
        enemies = diff.get("enemies", [])
        enemy_names = {e.get("name") for e in enemies}
        player_name = self.player.name if self.player else ""
        enemy_hp_loss = sum(max(0, e.get("hp_before", 0) - e.get("hp_after", 0))
                            for e in enemies)
        enemy_gone = any(e.get("dead") for e in enemies)
        shield = max(0, p.get("shield_after", 0) - p.get("shield_before", 0))
        heal = max(0, p.get("hp_after", 0) - p.get("hp_before", 0))
        cost = max(0, p.get("mana_before", 0) - p.get("mana_after", 0))
        mana_gain = max(0, p.get("mana_after", 0) - p.get("mana_before", 0))
        bl_gain = max(0, p.get("bl_after", 0) - p.get("bl_before", 0))
        speed_gain = max(0, p.get("speed_after", 0) - p.get("speed_before", 0))

        hit_enemy = False        # 伤害类事件命中敌方（含被格挡吸收）
        status_on_enemy = False  # 状态/控制落在敌方
        status_on_self = False   # 状态落在自身
        for ev in diff.get("events", []):
            etype = ev.get("type", "")
            target = ev.get("target", "")
            if etype == "damage_applied" and target in enemy_names:
                hit_enemy = True
            elif etype == "status_applied":
                if target in enemy_names:
                    status_on_enemy = True
                elif target == player_name:
                    status_on_self = True

        if enemy_gone and enemy_hp_loss <= 0 and not hit_enemy:
            kind = "remove"
        elif enemy_hp_loss > 0 or hit_enemy:
            kind = "damage"
        elif status_on_enemy and not status_on_self:
            kind = "tactician"
        elif shield > 0:
            kind = "shield"
        elif heal > 0:
            kind = "heal"
        elif mana_gain > 0:
            kind = "ramp"
        elif bl_gain > 0 or speed_gain > 0 or status_on_self:
            kind = "buff"
        else:
            kind = "tactician"   # 其余无面板位移的战术牌
        return {"kind": kind, "cost_per_x": cost, "dmg": enemy_hp_loss,
                "shield": shield, "heal": heal, "mana_gain": mana_gain,
                "bl_gain": bl_gain}

    # ---------- 候选生成与实时评分 ----------

    def _daowen_candidates(self) -> list[dict]:
        """玩家实持道纹 → 聚焦候选（X 按预算/需求取 2~3 档）。"""
        out = []
        budget = self.mana_budget()
        for name, inst in sorted(self.player.dao_wen.items()):
            if inst is None or not inst.can_use():
                continue
            probe = self._probe(name)
            if probe is None:
                continue
            cost = probe["cost_per_x"]
            if cost > 0:
                budget_x = max(1, budget // cost)
                # 最后一手允许用尽全部法力；否则按预算档，防一发梭哈
                cap = budget_x if self.remaining_actions() > 1 else max(1, self.mana() // cost)
            else:                        # 代价型：不受法力限制，X 保守取小
                cap = 3
                budget_x = 1
            xs = {1, budget_x, cap}
            target = probe.get("target_name")
            kind = probe["kind"]
            if kind == "damage":         # 输出牌给血最少敌人（收割/推进）
                foes = sorted(self.alive_enemies(), key=lambda e: e.current_hp)
                targets = [foes[0].name] if foes else []
                if foes and probe["dmg"] > 0:   # 收割档：恰好打死血最少者
                    xs.add(math.ceil(foes[0].current_hp / probe["dmg"]))
            elif kind in ("shield", "heal", "buff", "ramp"):
                targets = [self.player.name]
            else:
                targets = ([t for t in (self._top_enemy_name(),) if t]
                           if probe.get("target_name") in (None, self._top_enemy_name())
                           else [probe.get("target_name")])
            xs = sorted((x for x in xs if 1 <= x <= max(cap, 1)), reverse=True)[:4]
            for t in targets or [None]:
                for x in xs:
                    out.append({"action": "use_daowen",
                                "label": f"{name}X={x}",
                                "kind": kind, "target": t,
                                "params": self._params(name, x, t)})
        return out

    def _params(self, name: str, x: int, target: Optional[str]) -> dict:
        p: dict = {"daowen_name": name, "x": x, "dodge": False, "blood_shadow": False}
        if self._actor_ref:
            p["actor_ref"] = self._actor_ref   # 守擂者视角：所有道纹都指定施法者
        if target:
            p["target"] = target
        if name == "波及":
            refs = self.engine.combat._combat_entity_refs()
            # 视角重定向：波及目标是「本 AI 视角的敌人」；普通视角取非玩家侧，守擂视角取挑战者侧。
            enemy_ids = {id(e) for e in self.alive_enemies()}
            candidates = [
                {"target_ref": ref, "dodge": False, "blood_shadow": False}
                for ref, entity in refs.items()
                if entity.is_alive and id(entity) in enemy_ids
            ]
            if len(candidates) >= x:
                p["dodge_targets"] = candidates[:x]
                p["target_ref"] = candidates[0]["target_ref"]
        return p

    def _resonance_candidates(self) -> list[tuple[float, dict]]:
        """残韵候选：对**存在变化路径**的敌方道纹，按威胁动态打分（无固定表）。"""
        from engine.daowen import ResonanceEngine
        # 施法者残韵库存：挑战者经 State.resonance，守擂者经实体级 resonance（共用机制）。
        # 只读施法者**真实准备**的库存，绝不借他人——没有就是没有（不给没准备的塞残韵）。
        if self.player is self.engine.state.player:
            src_stock = self.engine.state.resonance
        else:
            src_stock = getattr(self.player, "resonance", None) or {}
        stock = {k: v for k, v in (src_stock or {}).items() if v > 0}
        if not stock:
            return []
        out = []
        enemies = sorted(self.alive_enemies(),
                         key=lambda e: -e.attack_count * e.attack_power)
        for enemy in enemies:
            threat_share = (enemy.attack_count * enemy.attack_power
                            / max(1, self.incoming_damage()))
            for dw in enemy.dao_wen:
                # 威胁构成可见信息：输出类文本权重高，其余次之
                text_kind = daowen_text_kind(enemy.dao_wen[dw])
                weight = {"damage": 1.0, "control": 0.7, "debuff": 0.6}.get(text_kind, 0.4)
                for path in ResonanceEngine.get_available_resonance(dw):
                    rtype = path.get("resonance_type")
                    if not rtype or stock.get(rtype, 0) <= 0:
                        continue
                    score = 4.0 * (0.5 + threat_share) * weight
                    params = {"source_daowen": dw, "resonance_type": rtype,
                              "target": enemy.name,             # 兼容测试/旧解析：按名字找目标
                              "target_ref": self._target_ref_for(enemy)}  # use_resonance 需要稳定引用
                    if self._actor_ref:
                        params["actor_ref"] = self._actor_ref
                    out.append((score, {
                        "action": "use_resonance",
                        "label": f"残韵·{rtype}→{dw}@{enemy.name}",
                        "params": params}))
        out.sort(key=lambda t: -t[0])
        return out[:3]

    def _score_candidate(self, diff: dict, label: str,
                         kind: Optional[str] = None,
                         target: Optional[str] = None) -> Optional[float]:
        """实时评分：局势效用 + 性格调制。返回 None 表示必须拒绝（LETHAL）。"""
        if ActionPreview.would_kill_player(diff):
            self.preview_rejected.append(f"{label}（预演致轮回者命零）")
            return None
        risk, reasons = ActionPreview.risk_classify(diff, self.player)
        self._last_risk = (risk, reasons)

        p = diff.get("player", {})
        enemies = diff.get("enemies", [])
        hp = p.get("hp_before", 0), p.get("hp_after", 0)
        own_hp_loss = max(0, hp[0] - hp[1])
        heal = max(0, hp[1] - hp[0])
        bl_loss = max(0, p.get("bl_before", 0) - p.get("bl_after", 0))
        speed_loss = max(0, p.get("speed_before", 0) - p.get("speed_after", 0))
        mana_spent = max(0, p.get("mana_before", 0) - p.get("mana_after", 0))
        shield_gain = max(0, p.get("shield_after", 0) - p.get("shield_before", 0))
        mutation = max(0, p.get("mutation_delta", 0))
        shards_spent = max(0, diff.get("shards_before", 0) - diff.get("shards_after", 0))

        enemy_hp_loss = sum(max(0, e.get("hp_before", 0) - e.get("hp_after", 0))
                            for e in enemies)
        enemy_heal = sum(max(0, e.get("hp_after", 0) - e.get("hp_before", 0))
                         for e in enemies)
        kills = sum(1 for e in enemies if e.get("dead"))
        all_gone = bool(enemies) and all(
            e.get("dead") or e.get("hp_after", 1) == 0 or e.get("departed", False)
            for e in enemies)

        # ---- 局势效用（状态驱动，无任何道纹名特判） ----
        score = 0.0
        if all_gone:
            score += 100.0                        # 直接终结战斗
        score += 1.4 * enemy_hp_loss              # 推进敌方血量（与防御对称,倾斜交给局势与性格）
        score += 8.0 * kills                      # 击杀/移除
        # 战术牌价值：对敌方施加压制（状态事件可见，或引擎已受理的敌方指向
        # 战术牌——部分潜在规则型道纹零事件足迹，但引擎 result.calculation 证实生效）。
        # 价值来自可见信息：目标威胁占比（压制谁）、目标血量比（肥怪先上战术
        # 更划算）、本场是否首次使用（每张至少探索一次，防只打伤害的单调收敛）。
        enemy_by_name = {en.name: en for en in self.alive_enemies()}
        pressed: set = set()
        for ev in diff.get("events", []):
            if ev.get("type") == "status_applied" and ev.get("target", "") in enemy_by_name:
                pressed.add(ev.get("target", ""))
        if kind == "tactician" and target in enemy_by_name:
            pressed.add(target)
        for tname in pressed:
            ent = enemy_by_name[tname]
            share = (ent.attack_count * ent.attack_power
                     / max(1, self.incoming_damage()))
            ratio = ent.current_hp / max(1, ent.blood_limit)
            score += 2.0 + 4.0 * min(1.0, ratio) + 2.0 * min(1.0, share)
            score += 0.8 * ent.attack_count * ent.attack_power  # 压制其行动的期权
            if label.split("X=")[0].split("→")[0] not in self.used:
                score += 4.0             # 首次探索加成（每张一次）
        self_status = any(
            ev.get("type") == "status_applied"
            and ev.get("target", "") == (self.player.name if self.player else "")
            for ev in diff.get("events", []))
        if self_status or kind == "buff":
            score += 1.5                 # 自身增益的期权价值（小额，防无脑挂buff）
        score -= 1.2 * enemy_heal                 # 喂养敌方（癌变风险由 CRITICAL 兜底）
        threat = self.incoming_damage()
        shield_useful = min(shield_gain, max(0, threat - self.player.shield))
        score += 1.1 * shield_useful + 0.15 * max(0, shield_gain - shield_useful)
        missing = max(0, self.player.blood_limit - self.player.current_hp)
        score += 1.4 * min(heal, missing)
        # 凡庸压力：连续未使敌方掉血越久，输出候选越紧迫
        if enemy_hp_loss > 0 and self._rounds_since_damage >= 2:
            score += 1.2 * self._rounds_since_damage
        score -= 0.12 * mana_spent
        score -= 0.4 * shards_spent
        score -= 0.35 * mutation
        score -= 0.8 * speed_loss

        # ---- 性格调制（倾向而非规则；权重=score×confidence，无性格=0） ----
        w_risk = self._w("risk_preference")          # +冒险 / -求稳
        w_res = self._w("resource_view")             # +节约 / -挥霍
        w_explore = self._w("exploration_desire")    # +求新
        w_habit = self._w("decision_habit")          # +先观察后行动 / -冲动
        w_stable = self._w("emotional_stability")    # +沉稳 / -易波动
        w_trust = self._w("interpersonal_tendency")  # +信任（护友）
        w_moral = self._w("moral_baseline")          # +守义（不伤友）
        w_expr = self._w("expression_style")         # +直言（进攻） / -内敛（防守）
        w_react = self._w("reaction_pattern")        # +从容

        tol = 1.0 - 0.85 * w_risk                   # 风险容忍 → 自伤/血限惩罚系数
        tol = min(2.0, max(0.3, tol))
        score -= (2.2 * tol) * own_hp_loss
        score -= (3.0 * tol) * bl_loss
        if w_risk < 0:                              # 求稳：受威胁时防回手段显著加值
            score += 0.8 * abs(w_risk) * (shield_useful + min(heal, missing))
        score -= 0.12 * (1.0 + 1.5 * max(0.0, w_res)) * mana_spent   # 节约：更疼法力
        score -= 0.4 * (1.0 + 1.5 * max(0.0, w_res)) * shards_spent
        if w_explore > 0:                           # 求新：没打过的牌加分
            base = label.split("X=")[0]
            if base not in self.used:
                score += 0.5 * w_explore
        if w_habit > 0 and self._first_contact():   # 先观察后行动：开局忌梭哈
            if mana_spent > 3:
                score -= 2.0 * w_habit * mana_spent / 6.0
        if w_habit < 0 and enemy_hp_loss > 0:       # 冲动：立打立伤偏好
            score += 0.3 * abs(w_habit)
        hp_ratio = self.player.current_hp / max(1, self.player.blood_limit)
        if hp_ratio < 0.35:                         # 低血：易波动者更重自伤
            score -= 0.9 * max(0.0, -w_stable) * own_hp_loss
        if threat > 0 and w_react > 0 and enemy_hp_loss == 0 and own_hp_loss == 0:
            score += 0.1 * w_react                  # 从容：高压下偏好稳妥手段
        if w_expr > 0 and enemy_hp_loss > 0:
            score += 0.15 * w_expr                  # 直言：进攻倾向
        if w_expr < 0 and enemy_hp_loss == 0:
            score += 0.15 * abs(w_expr)             # 内敛：防守/布局倾向
        for ally in diff.get("allies", []):         # 友军后果
            a_loss = max(0, (ally.get("hp_before") or 0) - (ally.get("hp_after") or 0))
            a_heal = max(0, (ally.get("hp_after") or 0) - (ally.get("hp_before") or 0))
            score -= (0.8 + 0.8 * w_moral) * a_loss
            score += 0.5 * w_trust * a_heal

        # ---- 台词影响（硬伤3；红线 E 已于 2026-08-30 由 DM 解除）----
        # 语言没有实质数值作用，但"让敌人忌惮"就是最大的作用。信不信由我的性格定。
        if self._opponent_read:
            score += self._dialogue_bias(
                enemy_hp_loss=enemy_hp_loss, shield_useful=shield_useful,
                heal=min(heal, missing), mana_spent=mana_spent)

        # ---- 风险等级惩罚（引擎口径分级） ----
        if risk == "LETHAL":
            return None
        score -= {"CRITICAL": 30.0, "HIGH": 6.0, "MEDIUM": 1.2}.get(risk, 0.0)
        return score

    def _first_contact(self) -> bool:
        """开局首轮/刚遇新敌（先观察型性格用；可见信息：本场出手与回合数）。"""
        return (not self.used and self.engine.state.current_round <= 1)

    def _dynamic_action(self) -> Optional[dict]:
        """实时决策主路径：生成候选 → 预演评分 → 执行最高分。"""
        self._refresh_personality()
        self._refresh_opponent_read()
        scored: list[tuple[float, str, dict]] = []
        candidates = self._daowen_candidates()
        for bonus, cand in self._resonance_candidates():
            candidates.append(cand)   # 残韵候选已按威胁预筛，附带基础分
        for cand in candidates[:MAX_CANDIDATE_PREVIEWS]:
            pv = self.previewer.preview(cand["action"], cand["params"])
            res = pv.get("result") or {}
            if not res.get("success"):
                continue               # 引擎拒绝=非法候选，跳过
            s = self._score_candidate(pv.get("diff", {}), cand["label"],
                                      cand.get("kind"), cand.get("target"))
            if s is None:
                continue
            if cand["action"] == "use_resonance":
                s += bonus
            # 首试配额：安全窗口（当前威胁低于自身生命）下，每张非输出牌每场
            # 至少真实试打一次——预演看不到潜在规则型道纹的延迟价值，
            # 用一次真实发动校准后续估值（先射箭后画靶；也保证持有即会发动）。
            base = cand["label"].split("X=")[0].split("→")[0]
            if (cand.get("kind") in ("tactician", "buff", "ramp")
                    and base not in self.used
                    and self.incoming_damage() < self.player.current_hp):
                s += 40.0
            scored.append((s, cand["label"], cand))
        if not scored:
            return None
        scored.sort(key=lambda t: (-t[0], t[1]))
        best = scored[0][2]
        hp_before = sum(e.current_hp for e in self.alive_enemies())
        r = self.engine.execute_action(best["action"], best["params"])
        if r.get("success"):
            label = best["label"]
            base = label.split("X=")[0].split("→")[0]
            self.used[base] = self.used.get(base, 0) + 1
            # 记账：本回合是否推进了敌方血量（凡庸压力用）
            hp_after = sum(e.current_hp for e in self.alive_enemies())
            if hp_after < hp_before:
                self._damage_done_battle = True
            if self.verbose:
                self.log.append(f"[实时决策] {label}（得分 {scored[0][0]:.2f}）")
            return r
        if self.verbose:
            self.log.append(f"[跳过] {best['label']}: {r.get('error')}")
        return None

    # ---------- 统一执行入口（安全过滤保留） ----------

    def _cast(self, name: str, x: int, target: Optional[str] = None,
              *, allow_sacrifice: bool = False) -> Optional[dict]:
        """候选动作 → 预演 → LETHAL 降X搜索 → 正式执行（兼容壳与测试共用）。"""
        if x < 1:
            return None
        p = self._params(name, x, target)
        if not allow_sacrifice:
            probe_x = x
            reason = None
            self._last_risk = ("SAFE", [])
            while probe_x >= 1:
                pv = self.previewer.preview("use_daowen", dict(p, x=probe_x))
                diff = pv.get("diff", {})
                res = pv.get("result") or {}
                if not res.get("success"):
                    # 大X可能因法力/代价不足非法：降档重试，全部非法才放弃
                    probe_x -= 1
                    continue
                self._last_risk = ActionPreview.risk_classify(diff, self.player)
                if not ActionPreview.would_kill_player(diff):
                    break
                reason = "预演致轮回者命零"
                probe_x -= 1
            if probe_x < 1:
                self.preview_rejected.append(f"{name}X={x}->{target or 'all'}（{reason}）")
                if self.verbose:
                    self.log.append(f"[安全过滤] 拒绝 {name}X={x}: {reason}")
                return None
            if probe_x != x:
                self.preview_rejected.append(
                    f"{name}X={x}->{target or 'all'}（预演致轮回者命零，降档到X={probe_x}）")
                if self.verbose:
                    self.log.append(f"[安全过滤] {name}X={x}→{probe_x}（降低到安全档）")
            x = probe_x
            p["x"] = x
        r = self.engine.execute_action("use_daowen", p)
        if r.get("success"):
            self.used[name] = self.used.get(name, 0) + 1
            return r
        if self.verbose:
            self.log.append(f"[跳过] {name}X={x}: {r.get('error')}")
        return None

    # ---------- 兼容壳：类别策略（供 tests 与 archive 实验子类） ----------

    def _sized_x(self, name: str, need_units: int, unit: str = "dmg") -> int:
        """按需求量与法力推导 X（每X产量来自 X=1 预演，缓存本场）。"""
        probe = self._probe(name)
        if probe is None:
            return 0
        per = probe.get(unit) or 1
        cost = probe["cost_per_x"]
        x = max(1, math.ceil(need_units / max(1, per)))
        if cost > 0:
            x = min(x, max(1, self.mana() // cost))
        return min(x, 9)

    def try_survive(self) -> Optional[dict]:
        """保命：致死威胁 → 格挡；血线过低 → 回复（类别由预演归纳）。"""
        p = self.player
        threat = self.incoming_damage()
        if threat > 0 and threat >= p.current_hp * 0.5:
            names = list(dict.fromkeys(
                self.owned("shield") + [n for n, inst in sorted(self.player.dao_wen.items())
                                        if inst is not None and inst.can_use()
                                        and (self._probe(n) or {}).get("kind") == "shield"]))
            for name in names:
                need = max(1, threat - p.shield)
                r = self._cast(name, self._sized_x(name, need, "shield"), p.name)
                if r:
                    return r
        if p.current_hp <= p.blood_limit * 0.35:
            names = list(dict.fromkeys(
                self.owned("heal") + [n for n, inst in sorted(self.player.dao_wen.items())
                                      if inst is not None and inst.can_use()
                                      and (self._probe(n) or {}).get("kind") == "heal"]))
            for name in names:
                need = p.blood_limit - p.current_hp
                r = self._cast(name, self._sized_x(name, need, "heal"), p.name)
                if r:
                    return r
        return None

    def try_buff(self) -> Optional[dict]:
        """增益：每张每场至多一次（引擎/自身状态自然拒绝重复）。"""
        battle = self.engine.state.current_battle
        for name, inst in sorted(self.player.dao_wen.items()):
            if inst is None or not inst.can_use():
                continue
            if self.used.get(f"buff:{name}:{battle}"):
                continue
            probe = self._probe(name)
            if probe is None or probe["kind"] not in ("buff", "tactician"):
                continue
            target = probe.get("target_name")
            r = self._cast(name, 2 if probe["cost_per_x"] > 0 else 1, target)
            if r:
                self.used[f"buff:{name}:{battle}"] = 1
                return r
        return None

    def _allies(self) -> list:
        out = [e for e in self.engine.state.friends
               if e.is_alive and not e.has_retreated]
        out += [e for e in self.engine.state.employees
                if e.is_alive and not e.has_retreated and e.is_deployed]
        return out

    def _best_attack_ally(self):
        allies = self._allies()
        return max(allies, key=lambda e: e.attack_power or 0) if allies else None

    def _tankiest_ally(self):
        allies = self._allies()
        return max(allies, key=lambda e: e.current_hp) if allies else None

    def _weakest_ally(self):
        allies = self._allies()
        return min(allies, key=lambda e: e.current_hp) if allies else None

    def resolve_pending_redemption(self, option: str = "无视") -> Optional[dict]:
        """救赎是强制待选：未结算前任何其它行动都会被引擎拒绝。"""
        pending = self.engine.state.pending_redemption
        if not pending:
            return None
        params: dict = {"option": option}
        if option in (1, "1", "接纳"):
            base = f"微光·{pending.get('name', '友')}"
            existing = {self.player.name} if self.player else set()
            for group in (self.engine.state.friends, self.engine.state.employees,
                          self.engine.state.temp_friends):
                existing.update(entity.name for entity in group)
            existing.update(entity.name for entity in self.alive_enemies())
            name = base
            suffix = 2
            while name in existing:
                name = f"{base}{suffix}"
                suffix += 1
            params["name"] = name
        r = self.engine.execute_action("resolve_redemption", params)
        if r.get("success"):
            self.used["救赎"] = self.used.get("救赎", 0) + 1
            return r
        if self.verbose:
            self.log.append(f"[跳过] 救赎: {r.get('error')}")
        return None

    def _best_resonance(self, min_score: float) -> Optional[dict]:
        for bonus, cand in self._resonance_candidates():
            if bonus < min_score:
                break
            r = self.engine.execute_action(cand["action"], cand["params"])
            if r.get("success"):
                rtype = cand["params"]["resonance_type"]
                self.used[f"残韵·{rtype}"] = self.used.get(f"残韵·{rtype}", 0) + 1
                self.resolve_pending_redemption()
                return r
        return None

    def try_resonance(self) -> Optional[dict]:
        """残韵：对存在变化路径的敌方道纹按威胁动态发动（无固定目标表）。"""
        return self._best_resonance(min_score=2.0)

    def try_finish(self) -> Optional[dict]:
        """收割：能一击打死就打死（每点X伤害来自预演归纳，非查表）。"""
        for e in sorted(self.alive_enemies(), key=lambda x: x.current_hp):
            for name in sorted(self.player.dao_wen):
                inst = self.player.dao_wen[name]
                if inst is None or not inst.can_use():
                    continue
                probe = self._probe(name)
                if probe is None or probe["kind"] != "damage" or probe["dmg"] <= 0:
                    continue
                need_x = math.ceil(e.current_hp / probe["dmg"])
                cost = probe["cost_per_x"]
                if cost > 0 and need_x * cost > self.mana():
                    continue
                if need_x >= 1:
                    r = self._cast(name, need_x, e.name)
                    if r:
                        return r
        return None

    def try_remove(self) -> Optional[dict]:
        """移除：把怪物移出战斗（X=1；异变代价由安全过滤与风险分级把关）。"""
        enemies = [en for en in self.alive_enemies() if en.entity_type == "怪物"]
        if not enemies:
            return None
        threat = max(enemies, key=monster_threat)
        for name in sorted(self.player.dao_wen):
            inst = self.player.dao_wen[name]
            if inst is None or not inst.can_use():
                continue
            if self._probe(name) and self._probe(name)["kind"] == "remove":
                r = self._cast(name, 1, threat.name)
                if r:
                    return r
        return None

    def try_control(self) -> Optional[dict]:
        """控场：对威胁最大的敌人上控制（目标本回合已控则不重复）。"""
        enemies = self.alive_enemies()
        if not enemies:
            return None
        p = self.player
        top = max(enemies, key=lambda e: e.attack_count * e.attack_power)
        if top.name in self._controlled_this_round:
            return None
        for name in sorted(self.player.dao_wen):
            inst = self.player.dao_wen[name]
            if inst is None or not inst.can_use():
                continue
            probe = self._probe(name)
            if probe is None or probe["kind"] != "tactician":
                continue
            x = 2 if probe["cost_per_x"] > 0 else 1
            r = self._cast(name, min(x, 3), top.name)
            if r:
                self._controlled_this_round.add(top.name)
                return r
        return None

    def try_aoe(self) -> Optional[dict]:
        """群伤：敌数≥2 时输出牌默认已覆盖（预演 diff 天然含全体敌方掉血）。"""
        if len(self.alive_enemies()) < 2:
            return None
        return None   # 实时评分已按敌方总掉血比较单体/群体，无需单独策略

    def try_debuff(self) -> Optional[dict]:
        """削弱：对最肥目标上战术牌（每张每目标每场一次）。"""
        enemies = self.alive_enemies()
        if not enemies:
            return None
        tank = max(enemies, key=lambda e: e.current_hp)
        for name in sorted(self.player.dao_wen):
            inst = self.player.dao_wen[name]
            if inst is None or not inst.can_use():
                continue
            if self.used.get(f"debuff:{name}:{tank.name}"):
                continue
            probe = self._probe(name)
            if probe is None or probe["kind"] != "tactician":
                continue
            x = 2 if probe["cost_per_x"] > 0 else 1
            r = self._cast(name, min(x, 3), tank.name)
            if r:
                self.used[f"debuff:{name}:{tank.name}"] = 1
                return r
        return None

    def try_pressure(self) -> Optional[dict]:
        """输出：预算内性价比最高（单价来自预演），焦点血最少目标。"""
        enemies = self.alive_enemies()
        if not enemies:
            return None
        target = min(enemies, key=lambda e: e.current_hp)
        budget = self.mana_budget()
        ranked = []
        for name in sorted(self.player.dao_wen):
            inst = self.player.dao_wen[name]
            if inst is None or not inst.can_use():
                continue
            probe = self._probe(name)
            if probe is None or probe["kind"] != "damage" or probe["dmg"] <= 0:
                continue
            cost = probe["cost_per_x"]
            x = max(1, budget // cost) if cost > 0 else 2
            ranked.append((-(x * probe["dmg"]), name, min(x, 9)))
        for _, name, x in sorted(ranked):
            r = self._cast(name, x, target.name)
            if r:
                return r
        return None

    def try_ramp(self) -> Optional[dict]:
        """资源：法力见底且有换取手段时补充。"""
        if self.mana() >= 3:
            return None
        for name in self.owned("ramp"):
            r = self._cast(name, 2)
            if r:
                return r
        return None

    def try_reroll(self) -> Optional[dict]:
        """消灾类：血线告急且碎片富余时买一次重掷（文本『重置随机』归纳）。"""
        p = self.player
        if p.current_hp > p.blood_limit * 0.5:
            return None
        state = self.engine.state
        if not (state.shards >= 5 or state.fake_shards >= 50):
            return None
        for name in sorted(p.dao_wen):
            if daowen_text_kind(p.dao_wen[name]) == "ramp" and "重置" in _full_text(p.dao_wen[name]):
                return self._cast(name, 1)
        return None

    def try_consumable(self) -> Optional[dict]:
        """消耗品：不消耗出手；完整后果预演 + 通用风险分级（不查物品名）。"""
        p = self.player
        if self.consumable_gate is not None:
            try:
                if not self.consumable_gate(self):
                    return None
            except Exception:
                return None
        elif p.current_hp > p.blood_limit * 0.4:
            return None
        for item in list(self.engine.state.consumables):
            if getattr(item, "current_uses", 0) <= 0:
                continue
            pv = self.previewer.preview("consume_item", {"name": item.name})
            if not pv.get("result") or not pv["result"].get("success"):
                continue
            risk_level, reasons = ActionPreview.risk_classify(pv.get("diff", {}), p)
            if risk_level in ("LETHAL", "CRITICAL"):
                self.preview_rejected.append(
                    f"消耗品{item.name}（{risk_level}：{'；'.join(reasons) or '通用风险'})")
                if self.verbose:
                    self.log.append(f"[安全过滤] 拒绝 消耗品{item.name}: {risk_level} {' '.join(reasons)}")
                continue
            r = self.engine.execute_action("consume_item", {"name": item.name})
            if r.get("success"):
                self.used[f"消耗品·{item.name}"] = self.used.get(f"消耗品·{item.name}", 0) + 1
                return r
        return None

    def try_artifact(self) -> Optional[dict]:
        """可选法器（不占出手）：由 sim.optional_actions 驱动，引擎校验把关。"""
        from sim.optional_actions import (
            try_fire_godfather_revolver, try_use_blood_wings,
        )
        for fn in (try_fire_godfather_revolver, try_use_blood_wings):
            r = fn(self.engine)
            if r and r.get("success"):
                self.used[f"法器·{r.get('action', '')}"] = self.used.get(
                    f"法器·{r.get('action', '')}", 0) + 1
                return r
        return None

    # ---------- 主入口 ----------

    def take_action(self) -> Optional[dict]:
        """执行一次出手：实时评估候选；子类固定串（archive 实验）走旧级联。"""
        self.resolve_pending_redemption()
        if not self.alive_enemies() or not self.player.is_alive:
            return None
        if self.STRATEGIES:            # 仅实验子类定义了固定顺序
            for fn in self.STRATEGIES:
                r = getattr(self, fn)()
                if r:
                    self.resolve_pending_redemption()
                    return r
            return None
        r = self._dynamic_action()
        if r:
            self.resolve_pending_redemption()
        return r

    def new_round(self) -> None:
        """[回始]调用：清回合记账，更新凡庸压力。"""
        self._controlled_this_round.clear()
        p = self.player
        if p is not None:
            self._hp_trace.append(p.current_hp)
        if self._damage_done_battle:
            self._rounds_since_damage = 0
        else:
            self._rounds_since_damage += 1
        self._damage_done_battle = False

    def take_turn(self) -> list[dict]:
        """执行本回合全部出手（出手次数 = [速限]/3，向上取整）。"""
        results = []
        self._refresh_personality()
        c = self.try_consumable()
        if c:
            results.append(c)
        for _ in range(max(1, math.ceil(self.player.speed_limit / 3))):
            if not self.alive_enemies() or not self.player.is_alive:
                break
            r = self.take_action()
            if not r:
                break
            results.append(r)
        return results


def _full_text(inst) -> str:
    dw = getattr(inst, "dao_wen", inst)
    return " ".join(filter(None, (
        getattr(dw, "formula", ""), getattr(dw, "effect_formula", ""),
        getattr(dw, "trigger_timing", ""), getattr(dw, "cost_type", ""),
    )))


def choose_dodge(engine, per_hit_damage: int, *, budget_used: int = 0,
                 max_dodges: int = 2, min_hit_pct: float = 0.10) -> bool:
    """AI 闪避决策（供 sim 怪物阶段解析器调用，处理轮回者受到的攻击）。

    规则依据（README 基础定义）：被选为[目标]后可消耗 1 点当前速度完全闪避。
    - 速度不足/必中已由引擎拒绝，这里只做预算与收益判断；
    - 每回合最多闪避 max_dodges 次（留速度应对残韵/回锋刀等）；
    - 只闪避会伤 ≥ min_hit_pct×[血限] 的命中，低伤不浪费速度。
    """
    p = engine.state.player
    if p is None or not p.is_alive:
        return False
    if p.current_speed <= budget_used:
        return False
    if p.has_status("固执"):
        return False            # 固执3：单次失去生命≤1，无需闪避
    if per_hit_damage < max(3, math.ceil(p.blood_limit * min_hit_pct)):
        return False
    if budget_used >= max_dodges:
        return False
    return True


def monster_threat(entity) -> int:
    """怪物视角的目标威胁分：物理输出 + 道纹持有加成（引擎可见事实，不查表）。

    施法型道纹（cost_type=消耗，如杀伐/庇护/再生）是可反复发动的主动手段，
    威胁 +10；代价型（异变/冷却/流血等）+4；空 cost_type（纯被动/触发）+0。
    """
    if entity is None:
        return 0
    score = (entity.attack_power or 0) * (entity.attack_count or 0)
    for inst in (entity.dao_wen or {}).values():
        dw = getattr(inst, "dao_wen", inst)
        cost_type = getattr(dw, "cost_type", "") or ""
        if cost_type == "消耗":
            score += 10
        elif cost_type:
            score += 4
    return score


def choose_attack_target(attack_target_options: list[dict], refs: dict) -> str:
    """怪物攻击目标：挑威胁最大者。威胁分相同时取当前生命最低（脆的先倒）。"""
    if not attack_target_options:
        return ""
    scored = []
    for option in attack_target_options:
        entity = refs.get(option["ref"])
        score = monster_threat(entity)
        hp = entity.current_hp if entity is not None else 10 ** 9
        scored.append((score, hp, option["ref"]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored[0][2]
