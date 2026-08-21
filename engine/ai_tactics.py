"""
战术AI：为轮回者在战斗中选择行动。

设计目标：**数据驱动，不写死道纹名**。
早前版本把"杀伐/庇护/再生"等硬编码在 if 分支里，导致无法测试
各副本专属道纹。现在改为：每个道纹在 TACTICAL_ROLES 里声明它的
战术角色与取值方式，AI 只按"角色"决策，因此
**新增或更换道纹无需改动决策代码**——这也让不同流派的实战胜率可被对比。

战术角色（role）：
  nuke      单体伤害        finisher 收割（能一击致死时优先）
  aoe       群体伤害        shield   获得格挡
  heal      回复生命        control  使目标无法行动/削弱出手
  debuff    削弱目标        buff     强化自身
  ramp      资源循环（换法力等）
  remove    直接移出战斗

决策优先级：保命 → 收割 → 移除 → 控场 → AOE → 削弱 → 输出 → 资源。
每一步都在**玩家实际持有的道纹**中挑选，持有什么就打什么。
"""
from __future__ import annotations

import math
from typing import Any, Optional
from engine.ai_preview import ActionPreview

# ---------------------------------------------------------------------------
# 道纹战术表：AI 的唯一知识来源。
#   role     战术角色
#   cost     每点X的法力消耗（用于预算推导；代价型道纹填0）
#   pay      代价类型（非"消耗"者，AI 会额外权衡）
#   pri      同角色内的优先级，数字越小越先用
# 未列出的道纹 AI 不会主动发动（例如需要复杂声明的），但引擎仍支持手动调用。
# ---------------------------------------------------------------------------
TACTICAL_ROLES: dict[str, dict] = {
    # ---- 杀伐闭环（cost/dmg 必须跟 README 现行公式一致，禁止沿用旧 3X/自动起手）----
    "杀伐": {"role": "nuke", "cost": 1, "pri": 2, "dmg_per_x": 2},
    "波及": {"role": "mark", "cost": 3, "pri": 1},
    "血债": {"role": "nuke", "cost": 0, "pay": "流血", "pri": 3, "dmg_per_x": 1},
    "庇护": {"role": "shield", "cost": 1, "pri": 1, "shield_per_x": 2},
    "再生": {"role": "heal", "cost": 1, "pri": 1, "heal_per_x": 3},
    "固执": {"role": "buff", "cost": 0, "pay": "冷却", "pri": 2},
    # ---- 杀伐11节点闭环后半（增殖至封印）----
    "增殖": {"role": "buff", "cost": 1, "pri": 3},
    "透支": {"role": "ramp", "cost": 0, "pay": "衰老", "pri": 1, "mana_per_x": 4},
    "贯穿": {"role": "buff", "cost": 5, "pri": 1},                    # 伤害无视格挡
    "封印": {"role": "remove", "cost": 0, "pay": "异变", "pri": 1},   # 移出战斗（异变8X）
    "束缚": {"role": "control", "cost": 0, "pay": "冷却", "pri": 1},
    # ---- 龙心谷 ----
    "加害": {"role": "debuff", "cost": 3, "pri": 1},
    "龙鳞": {"role": "buff", "cost": 5, "pri": 2},
    "逆鳞": {"role": "debuff", "cost": 0, "pay": "流血", "pri": 3},
    "活血": {"role": "buff", "cost": 2, "pri": 3},
    "裂变": {"role": "debuff", "cost": 3, "pri": 2},
    "伤痕": {"role": "debuff", "cost": 5, "pri": 2},
    # ---- 乱葬岗（二阶）----
    "分裂": {"role": "buff", "cost": 0, "pay": "冷却", "pri": 3},
    "尸爆": {"role": "buff", "cost": 10, "pri": 3},   # [命零]死亡触发爆炸，按濒死保险挂
    "缄默": {"role": "control", "cost": 2, "pri": 2},
    "瓦解": {"role": "debuff", "cost": 10, "pri": 2},
    "冥气": {"role": "debuff", "cost": 5, "pri": 2},
    "勾魂": {"role": "debuff", "cost": 1, "pri": 2},
    "镇尸": {"role": "debuff", "cost": 5, "pri": 1},
    "招魂": {"role": "buff", "cost": 10, "pri": 3},
    # ---- 扭曲都市 ----
    "僵化": {"role": "control", "cost": 5, "pri": 1},
    "坏死": {"role": "debuff", "cost": 5, "pri": 1},
    "退化": {"role": "debuff", "cost": 5, "pri": 1},
    "定型": {"role": "debuff", "cost": 3, "pri": 2},
    "畸变": {"role": "debuff", "cost": 0, "pay": "冷却", "pri": 3},
    "爆裂": {"role": "buff", "cost": 3, "pri": 2},
    "超频": {"role": "buff", "cost": 2, "pri": 3},
    # ---- 罪孽都市 ----
    "洗劫": {"role": "debuff", "cost": 3, "pri": 2},
    "逼债": {"role": "debuff", "cost": 1, "pri": 1},
    "清算": {"role": "debuff", "cost": 5, "pri": 2},
    "赎金": {"role": "debuff", "cost": 10, "pri": 3},
    "假钞": {"role": "ramp", "cost": 1, "pri": 2},
    # ---- 怪物转化道纹（玩家可经残韵获得）----
    "蒙蔽": {"role": "control", "cost": 5, "pri": 2},
    "眩晕": {"role": "control", "cost": 20, "pri": 3},
    "弱化": {"role": "debuff", "cost": 3, "pri": 1},
    "无力": {"role": "control", "cost": 10, "pri": 2},
    "衰败": {"role": "debuff", "cost": 15, "pri": 1, "min_x": 1},  # 回始扣20%当前生命(持续∞)，X=1即满效
    "滋养": {"role": "heal", "cost": 5, "pri": 2},
    "坠落": {"role": "debuff", "cost": 1, "pri": 1},
    "滑翔": {"role": "buff", "cost": 5, "pri": 3},
}

# 敌方身上值得用残韵改写的高价值道纹，按威胁度排序。
HIGH_VALUE_ENEMY_DAOWEN = [
    "必中", "狂暴", "自愈", "强化", "疯狂", "飞行", "减速",
    "贯穿", "血债", "波及", "杀伐", "增殖", "透支",
    "固执", "庇护", "再生", "束缚",
]


class TacticalAI:
    """轮回者战斗AI。engine 为 GameEngine 实例。"""

    def __init__(self, engine: Any, verbose: bool = False):
        self.engine = engine
        self.verbose = verbose
        self.log: list[str] = []
        self.used: dict[str, int] = {}   # 统计各道纹发动次数，便于流派对比
        self._controlled_this_round: set = set()   # 本回合已被控制的目标，避免重复浪费法力
        self._previewer = None           # 行动后果预演器（惰性创建）
        self.preview_rejected: list[str] = []   # 被安全过滤淘汰的候选（供测试/报告）
        self._sacrifice_actions: set = set()    # 项目明确允许的主动牺牲策略（默认空）
        self._last_risk: tuple = ("SAFE", [])   # 最近一次 _cast 的风险等级（供策略层参考）

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
        return self.engine.state.player

    def alive_enemies(self) -> list:
        return [e for e in self.engine.state.enemies if e.is_alive]

    def mana(self) -> int:
        return self.player.current_mana

    def owned(self, role: str) -> list[str]:
        """玩家实际持有、且属于该战术角色的道纹，按 pri 排序。"""
        out = [n for n in self.player.dao_wen if TACTICAL_ROLES.get(n, {}).get("role") == role]
        return sorted(out, key=lambda n: TACTICAL_ROLES[n].get("pri", 9))

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

    def _x_for(self, name: str, budget: Optional[int] = None) -> int:
        """在预算内为该道纹推导可用的最大X。代价型道纹不吃法力预算。"""
        info = TACTICAL_ROLES.get(name, {})
        per = info.get("cost", 1)
        if per <= 0:                      # 代价型（流血/衰老/冷却）
            return 1
        b = self.mana() if budget is None else budget
        return max(0, b // per)

    def _cast(self, name: str, x: int, target: Optional[str] = None,
              *, allow_sacrifice: bool = False) -> Optional[dict]:
        """统一候选执行入口：候选动作 → CombatEngine 预演 → 安全过滤 → 正式执行。

        预演由 CombatEngine 真实管线完成数值计算（含爆裂反噬/触发法术/癌变等一切
        监听链），TacticalAI 不复制任何伤害/反伤规则。第一阶段只做安全性过滤：
        预演导致轮回者本人命零的候选直接拒绝（除非 allow_sacrifice 显式放行），
        不允许"收益很高"覆盖必死结果。所有策略（保命/输出/控制）共用本入口。
        """
        if x < 1:
            return None
        p = {"daowen_name": name, "x": x, "dodge": False, "blood_shadow": False}
        if target:
            p["target"] = target
        if name == "波及":
            # 波及X：选择X个[目标]建立/解除波及标记（默认标记敌方存活目标）。
            refs = self.engine.combat._combat_entity_refs()
            actor = self.player
            candidates = [
                {"target_ref": ref, "dodge": False, "blood_shadow": False}
                for ref, entity in refs.items()
                if (self.engine.state.on_player_side(entity)
                    != self.engine.state.on_player_side(actor)
                    and entity.is_alive)
            ]
            if len(candidates) < x:
                return None
            p["dodge_targets"] = candidates[:x]
            if target is None:
                p["target_ref"] = candidates[0]["target_ref"]
        if not allow_sacrifice:
            # 预演安全过滤：候选导致轮回者命零则拒绝。通用降 X 搜索（非道纹特判）：
            # 大 X 可能触发癌变/反噬/嫁祸转伤等完整效果链，小 X 可能安全
            # （如再生 X=14 治愈癌变阈值，X=1 则安全）；自动降 X 找最小安全档。
            # 同时用风险分类器记录非致命风险等级（CRITICAL / HIGH 等，供策略层参考）。
            probe_x = x
            reason = None
            self._last_risk = ("SAFE", [])
            while probe_x >= 1:
                pv = self.previewer.preview("use_daowen", dict(p, x=probe_x))
                diff = pv.get("diff", {})
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
            p["x"] = x   # 正式执行必须用降档后的 X
        r = self.engine.execute_action("use_daowen", p)
        if r.get("success"):
            self.used[name] = self.used.get(name, 0) + 1
            return r
        if self.verbose:
            self.log.append(f"[跳过] {name}X={x}: {r.get('error')}")
        return None

    # ---------- 各优先级策略 ----------

    def try_survive(self) -> Optional[dict]:
        """1. 保命：致死威胁 → 格挡；血线过低 → 回复。"""
        p = self.player
        threat = self.incoming_damage()
        if threat > 0 and (threat >= p.current_hp or threat >= p.current_hp * 0.5):
            for name in self.owned("shield"):
                per = TACTICAL_ROLES[name].get("shield_per_x", 4)
                need = max(0, threat - p.shield)
                x = min(self._x_for(name), math.ceil(need / per))
                r = self._cast(name, x, p.name)
                if r:
                    return r
        if p.current_hp <= p.blood_limit * 0.35 and not p.has_status("坏死"):
            for name in self.owned("heal"):
                per = TACTICAL_ROLES[name].get("heal_per_x", 3)
                x = min(self._x_for(name), max(1, math.ceil((p.blood_limit - p.current_hp) / per)))
                r = self._cast(name, x, p.name)
                if r:
                    return r
        return None

    def try_buff(self) -> Optional[dict]:
        """1.5 增益：固执优先（防爆发），其余 role=buff 按条件表逐张评估。

        修复（2026-08-18）：旧版只硬编码固执，导致贯穿/增殖/活血/爆裂/
        超频/龙鳞/滑翔/分裂/招魂 等 10 张 buff 永不发动（死码）。
        现按每张牌的战术条件评估；每张每场至多一次，一回合至多打出一张增益。
        """
        p = self.player
        enemies = self.alive_enemies()
        if not enemies:
            return None
        battle = self.engine.state.current_battle
        # 固执特判：冷却代价、克制爆发，首回合尽早挂（按场计，跨场可重挂）
        if ("固执" in p.dao_wen and not self.used.get(f"buff:固执:{battle}")
                and self.incoming_damage() > 0):
            inst = p.dao_wen.get("固执")
            if inst is None or inst.can_use():
                r = self._cast("固执", 3, p.name)
                if r:
                    self.used[f"buff:固执:{battle}"] = 1
                    self.used["固执"] = self.used.get("固执", 0) + 1
                    return r
        for name in self.owned("buff"):
            if name == "固执" or self.used.get(f"buff:{name}:{battle}"):
                continue
            plan = self._buff_plan(name)
            if not plan:
                continue
            x, target = plan
            r = self._cast(name, x, target)
            if r:
                self.used[f"buff:{name}:{battle}"] = 1
                return r
        return None

    def _buff_plan(self, name: str) -> Optional[tuple[int, Optional[str]]]:
        """返回 (x, target) 或 None（条件不满足）。条件保守：不透支保命法力。"""
        p = self.player
        enemies = self.alive_enemies()
        info = TACTICAL_ROLES.get(name, {})
        per = max(0, info.get("cost", 1))
        mana = self.mana()
        threat = self.incoming_damage()
        hp_ratio = p.current_hp / max(1, p.blood_limit)
        if name == "贯穿":       # 伤害无视格挡：敌方有格挡才值得
            if any(e.shield > 0 for e in enemies) and mana >= per:
                return (1, p.name)
        elif name == "增殖":     # 廉价加血限：法力将溢出时倾倒
            if mana >= p.mana_limit * 0.8:
                x = min(self._x_for(name, mana - per), 3)
                if x >= 1:
                    return (x, p.name)
        elif name == "活血":     # 掉血回复：血线中低位挂
            if hp_ratio <= 0.7 and mana >= per * 2:
                return (2, p.name)
        elif name == "爆裂":     # 受伤反弹：威胁大时挂
            if threat >= 8 and mana >= per * 2:
                return (2, p.name)
        elif name == "超频":     # 补速度（闪避资源）
            if p.current_speed < p.speed_limit and mana >= per and threat > 0:
                return (1, p.name)
        elif name == "龙鳞":     # 永久减伤：尽早挂高X
            x = min(self._x_for(name, mana), 2)
            if x >= 1 and mana >= per:
                return (x, p.name)
        elif name == "滑翔":     # 获得飞行：致命威胁下脱离目标选择
            if threat >= p.current_hp * 0.4 and mana >= per:
                return (1, p.name)
        elif name == "分裂":     # 命零保险（冷却代价）：血线危险时挂
            if hp_ratio <= 0.35:
                return (1, p.name)
        elif name == "尸爆":     # 命零时对全体敌爆炸：濒死且敌多才值
            if hp_ratio <= 0.35 and len(enemies) >= 2 and mana >= per:
                return (1, p.name)
        elif name == "招魂":     # 唤回尸体作临时朋友：法力充裕时尝试（无尸体会被引擎拒绝）
            if mana >= max(per, p.mana_limit * 0.8):
                return (1, None)
        return None

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
            existing.update(entity.name for entity in self.engine.state.enemies if entity.is_alive)
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

    def try_resonance(self) -> Optional[dict]:
        """2. 残韵插队：只对**存在变化路径**的敌方道纹发动。"""
        from engine.daowen import ResonanceEngine

        stock = {k: v for k, v in self.engine.state.resonance.items() if v > 0}
        if not stock:
            return None
        for enemy in self.alive_enemies():
            for dw in HIGH_VALUE_ENEMY_DAOWEN:
                if dw not in enemy.dao_wen:
                    continue
                for path in ResonanceEngine.get_available_resonance(dw):
                    rtype = path.get("resonance_type")
                    if not rtype or stock.get(rtype, 0) <= 0:
                        continue
                    r = self.engine.execute_action(
                        "use_resonance",
                        {"source_daowen": dw, "resonance_type": rtype, "target": enemy.name})
                    if r.get("success"):
                        self.used[f"残韵·{rtype}"] = self.used.get(f"残韵·{rtype}", 0) + 1
                        self.resolve_pending_redemption()
                        return r
        return None

    def try_finish(self) -> Optional[dict]:
        """3. 收割：能一击打死就打死（按每点X伤害推导所需X）。"""
        for e in sorted(self.alive_enemies(), key=lambda x: x.current_hp):
            cands = []
            for name in self.owned("nuke"):
                info = TACTICAL_ROLES[name]
                per = info.get("dmg_per_x", 2)
                need_x = math.ceil(e.current_hp / per)
                if 1 <= need_x <= self._x_for(name):
                    cands.append((name, need_x, need_x * info.get("cost", 1)))
            # 能击杀的手段里选最省法力的
            for name, need_x, _c in sorted(cands, key=lambda t: t[2]):
                r = self._cast(name, need_x, e.name)
                if r:
                    return r
        return None

    def try_remove(self) -> Optional[dict]:
        """4. 移除：封印等直接把怪物移出战斗（注意不产碎片）。"""
        enemies = self.alive_enemies()
        if len(enemies) < 2:
            return None
        for name in self.owned("remove"):
            x = self._x_for(name, self.mana())
            if x >= 1:
                r = self._cast(name, 1, enemies[0].name)
                if r:
                    return r
        return None

    def try_control(self) -> Optional[dict]:
        """
        5. 控场：对威胁最大的敌人上控制。

        血线越低越该控：没有格挡/回复手段的流派（如纯debuff流派），
        控制是唯一的保命方式，此时应放宽触发阈值。
        """
        enemies = self.alive_enemies()
        if not enemies:
            return None
        p = self.player
        desperate = (not self.owned("shield") and not self.owned("heal")
                     and p.current_hp <= p.blood_limit * 0.5)
        threshold = 0.05 if desperate else 0.25
        if self.incoming_damage() < p.current_hp * threshold:
            return None
        top = max(enemies, key=lambda e: e.attack_count * e.attack_power)
        # 同一目标本回合已被控制过就不再重复施加（控制不叠加，重复施加纯属浪费）
        if top.name in self._controlled_this_round:
            return None
        for name in self.owned("control"):
            x = max(1, min(self._x_for(name, self.mana()), 3))
            r = self._cast(name, x, top.name)
            if r:
                self._controlled_this_round.add(top.name)
                return r
        return None

    def try_aoe(self) -> Optional[dict]:
        """6. AOE：按群体总伤与当次单体比较，敌数≥2 且总伤不低于单体时用群伤。"""
        enemies = self.alive_enemies()
        n = len(enemies)
        if n < 2:
            return None
        budget = self.mana_budget()
        ranked = self._nuke_ranked(budget)
        best_single = ranked[0][2] if ranked else 0
        for name in self.owned("aoe"):
            x = self._x_for(name, budget)
            if x < 1:
                continue
            total = x * TACTICAL_ROLES[name].get("dmg_per_x", 0) * n
            if total < best_single:
                continue
            r = self._cast(name, x)
            if r:
                return r
        return None

    def try_debuff(self) -> Optional[dict]:
        """7. 削弱：对最肥的目标叠加受伤加成/削减。每种每场只上一次。"""
        enemies = self.alive_enemies()
        if not enemies:
            return None
        tank = max(enemies, key=lambda e: e.current_hp)
        # X≥2 门槛只适用于按X线性放大的法力消耗型削弱；
        # 代价型（cost≤0，_x_for恒为1，如畸变/逆鳞）与声明min_x=1的强效
        # 削弱（如衰败：X=1已是回始扣20%当前生命）按min_x=1放行。
        for name in self.owned("debuff"):
            if self.used.get(f"debuff:{name}:{tank.name}"):
                continue
            info = TACTICAL_ROLES.get(name, {})
            min_x = info.get("min_x", 1 if info.get("cost", 1) <= 0 else 2)
            x = min(self._x_for(name, self.mana_budget()), 3)
            if x < min_x:
                # 重锤型削弱（min_x=1，如衰败15法力/X）：均分预算付不起时
                # 允许动用全部法力——一次打残主坦换整场收益是划算的。
                x = min(self._x_for(name, self.mana()), 3)
            if x < min_x:
                continue
            r = self._cast(name, x, tank.name)
            if r:
                self.used[f"debuff:{name}:{tank.name}"] = 1
                return r
        return None

    def _nuke_ranked(self, budget: int) -> list[tuple[str, int, int]]:
        """
        按"本次出手实际能打出的伤害"排序候选输出道纹。

        不能只看静态优先级：高费道纹每点X伤害更高但每点X耗法也更高，
        在小预算下反而不如杀伐(2伤害/1法力)。故按 预算内可达伤害 降序，
        同伤害时取更省法力者。
        """
        out = []
        for name in self.owned("nuke"):
            info = TACTICAL_ROLES[name]
            x = self._x_for(name, budget)
            if x < 1:
                continue
            dmg = x * info.get("dmg_per_x", 2)
            score = dmg + x * info.get("limit_per_x", 0)
            out.append((name, x, dmg, score))
        out.sort(key=lambda t: (-t[3], TACTICAL_ROLES[t[0]].get("cost", 1)))
        return [(name, x, dmg) for name, x, dmg, _score in out]

    def try_pressure(self) -> Optional[dict]:
        """8. 输出：按预算选性价比最高的输出道纹，焦点打击血最少的目标。"""
        enemies = self.alive_enemies()
        if not enemies:
            return None
        target = min(enemies, key=lambda e: e.current_hp)
        budget = self.mana_budget()
        for name, x, _dmg in self._nuke_ranked(budget):
            r = self._cast(name, x, target.name)
            if r:
                return r
        return None

    def try_ramp(self) -> Optional[dict]:
        """9. 资源：法力见底且有换取手段时补充。"""
        if self.mana() >= 3:
            return None
        for name in self.owned("ramp"):
            r = self._cast(name, 2)
            if r:
                return r
        return None

    def try_consumable(self) -> Optional[dict]:
        """消耗品：使用不消耗出手，故在出手循环外单独尝试。

        完整后果评估（非特判）：经 ActionPreview 预演完整效果链（含异变/崩解/
        血限/连锁触发等一切引擎管线效果），由通用风险分类器评估，LETHAL 和
        CRITICAL 等级的消耗品被拒绝。不检查消耗品具体名称，全靠 diff 数值说话。
        """
        p = self.player
        if p.current_hp > p.blood_limit * 0.4:
            return None
        for item in list(self.engine.state.consumables):
            if getattr(item, "current_uses", 0) <= 0:
                continue
            # 预演完整后果（不消耗真实次数、不改真实 state）
            pv = self.previewer.preview("consume_item", {"name": item.name})
            if not pv.get("result") or not pv["result"].get("success"):
                continue   # 引擎拒绝（如条件不满足）
            risk_level, reasons = ActionPreview.risk_classify(pv.get("diff", {}), p)
            if risk_level in ("LETHAL", "CRITICAL"):
                self.preview_rejected.append(
                    f"消耗品{item.name}（{risk_level}：{'；'.join(reasons) or '通用风险'})")
                if self.verbose:
                    self.log.append(f"[安全过滤] 拒绝 消耗品{item.name}: {risk_level} {' '.join(reasons)}")
                continue
            # 正式执行
            r = self.engine.execute_action("consume_item", {"name": item.name})
            if r.get("success"):
                return r
        return None

    def try_artifact(self) -> Optional[dict]:
        """可选法器（教父左轮/鲜血之翼等战斗内行动）：不占出手，能发动就发动。

        此前 AI 从不发动任何可选法器（黑金名片/罪业金库/烬翼/左轮/血翼/共心环），
        玩家战力被系统性低估。战始/回始窗口法器由 sim.optional_actions.start_battle/
        start_round 在对应子阶段驱动；本策略处理 PLAYER_ACTIONS 阶段的法器。
        """
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

    STRATEGIES = ("try_artifact", "try_survive", "try_buff", "try_resonance", "try_finish",
                  "try_remove", "try_control", "try_aoe", "try_debuff", "try_pressure",
                  "try_ramp")

    def take_action(self) -> Optional[dict]:
        """执行一次出手。返回引擎结果；无可行动作时返回 None。"""
        self.resolve_pending_redemption()
        if not self.alive_enemies() or not self.player.is_alive:
            return None
        for fn in self.STRATEGIES:
            r = getattr(self, fn)()
            if r:
                self.resolve_pending_redemption()
                return r
        return None

    def new_round(self) -> None:
        """[回始]调用：清空"本回合已控制"记账。"""
        self._controlled_this_round.clear()

    def take_turn(self) -> list[dict]:
        """执行本回合全部出手（出手次数 = [速限]/3，向上取整）。"""
        results = []
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
    """怪物视角的目标威胁分：物理输出（攻击力×攻击次数）+ 输出类道纹加成。

    玩家面板 0×0 但靠道纹输出，需额外加成；朋友/员工高攻会被优先打。
    """
    if entity is None:
        return 0
    score = (entity.attack_power or 0) * (entity.attack_count or 0)
    for name in entity.dao_wen:
        info = TACTICAL_ROLES.get(name, {})
        if info.get("role") in ("nuke", "aoe", "finisher", "debuff", "remove", "control"):
            score += 10
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
    # 威胁分高优先；同威胁血低优先（hp 升序）
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored[0][2]
