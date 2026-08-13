"""
战术AI：为轮回者在战斗中选择行动。

设计目标：**数据驱动，不写死道纹名**。
早前版本把"杀伐/庇护/再生/冲击/锐利"硬编码在 if 分支里，导致无法测试
锐利系与各副本专属道纹。现在改为：每个道纹在 TACTICAL_ROLES 里声明它的
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

# ---------------------------------------------------------------------------
# 道纹战术表：AI 的唯一知识来源。
#   role     战术角色
#   cost     每点X的法力消耗（用于预算推导；代价型道纹填0）
#   pay      代价类型（非"消耗"者，AI 会额外权衡）
#   pri      同角色内的优先级，数字越小越先用
# 未列出的道纹 AI 不会主动发动（例如需要复杂声明的），但引擎仍支持手动调用。
# ---------------------------------------------------------------------------
TACTICAL_ROLES: dict[str, dict] = {
    # ---- 杀伐闭环 ----
    "杀伐": {"role": "nuke", "cost": 1, "pri": 1, "dmg_per_x": 2},
    "冲击": {"role": "aoe", "cost": 1, "pri": 1, "dmg_per_x": 1},
    "血债": {"role": "nuke", "cost": 0, "pay": "流血", "pri": 3, "dmg_per_x": 2},
    "庇护": {"role": "shield", "cost": 1, "pri": 1, "shield_per_x": 4},
    "再生": {"role": "heal", "cost": 1, "pri": 1, "heal_per_x": 3},
    "慈悲": {"role": "heal", "cost": 0, "pay": "流血", "pri": 3, "heal_per_x": 1},
    "固执": {"role": "buff", "cost": 0, "pay": "冷却", "pri": 2},
    # ---- 锐利闭环 ----
    "锐利": {"role": "nuke", "cost": 3, "pri": 2, "dmg_per_x": 4},   # 血限与生命同时-4X
    "增殖": {"role": "buff", "cost": 5, "pri": 3},
    "透支": {"role": "ramp", "cost": 0, "pay": "衰老", "pri": 1, "mana_per_x": 4},
    "贯穿": {"role": "buff", "cost": 5, "pri": 1},                    # 伤害无视格挡
    "封印": {"role": "remove", "cost": 10, "pri": 1},                 # 直接移出战斗
    "缓慢": {"role": "control", "cost": 10, "pri": 2},
    "束缚": {"role": "control", "cost": 0, "pay": "冷却", "pri": 1},
    # ---- 龙心谷 ----
    "加害": {"role": "debuff", "cost": 3, "pri": 1},
    "龙鳞": {"role": "buff", "cost": 5, "pri": 2},
    "逆鳞": {"role": "debuff", "cost": 0, "pay": "流血", "pri": 3},
    "活血": {"role": "buff", "cost": 2, "pri": 3},
    "裂变": {"role": "debuff", "cost": 3, "pri": 2},
    "伤痕": {"role": "debuff", "cost": 5, "pri": 2},
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
    "衰败": {"role": "nuke", "cost": 15, "pri": 4},
    "滋养": {"role": "heal", "cost": 5, "pri": 2},
    "坠落": {"role": "debuff", "cost": 1, "pri": 1},
    "滑翔": {"role": "buff", "cost": 5, "pri": 3},
}

# 敌方身上值得用残韵改写的高价值道纹，按威胁度排序。
HIGH_VALUE_ENEMY_DAOWEN = [
    "必中", "狂暴", "自愈", "强化", "活力", "飞行", "减速",
    "贯穿", "血债", "冲击", "杀伐", "增殖", "透支",
    "固执", "庇护", "再生", "锐利", "缓慢", "束缚",
]


class TacticalAI:
    """轮回者战斗AI。engine 为 GameEngine 实例。"""

    def __init__(self, engine: Any, verbose: bool = False):
        self.engine = engine
        self.verbose = verbose
        self.log: list[str] = []
        self.used: dict[str, int] = {}   # 统计各道纹发动次数，便于流派对比
        self._controlled_this_round: set = set()   # 本回合已被控制的目标，避免重复浪费法力

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

    def _cast(self, name: str, x: int, target: Optional[str] = None) -> Optional[dict]:
        if x < 1:
            return None
        p = {"daowen_name": name, "x": x}
        if target:
            p["target"] = target
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
        """6. AOE：敌数≥3 时群伤。"""
        enemies = self.alive_enemies()
        if len(enemies) < 3:
            return None
        for name in self.owned("aoe"):
            x = self._x_for(name, self.mana_budget())
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
        # 只有当这一击的债务能换来足够长的收益期时才值得占用出手：
        # 战斗预计还会持续多回合，且 X 至少为2（X=1 的削弱幅度通常不值一次出手）。
        for name in self.owned("debuff"):
            if self.used.get(f"debuff:{name}:{tank.name}"):
                continue
            x = min(self._x_for(name, self.mana_budget()), 3)
            if x < 2:
                continue
            r = self._cast(name, x, tank.name)
            if r:
                self.used[f"debuff:{name}:{tank.name}"] = 1
                return r
        return None

    def _nuke_ranked(self, budget: int) -> list[tuple[str, int, int]]:
        """
        按"本次出手实际能打出的伤害"排序候选输出道纹。

        不能只看静态优先级：锐利每点X伤害更高(4)但每点X耗法也更高(3)，
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
            out.append((name, x, dmg))
        out.sort(key=lambda t: (-t[2], TACTICAL_ROLES[t[0]].get("cost", 1)))
        return out

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
        """消耗品：使用不消耗出手，故在出手循环外单独尝试。"""
        p = self.player
        if p.current_hp > p.blood_limit * 0.4:
            return None
        for item in list(self.engine.state.consumables):
            if getattr(item, "current_uses", 0) <= 0:
                continue
            r = self.engine.execute_action("consume_item", {"name": item.name})
            if r.get("success"):
                return r
        return None

    # ---------- 主入口 ----------

    STRATEGIES = ("try_survive", "try_resonance", "try_finish", "try_remove",
                  "try_control", "try_aoe", "try_debuff", "try_pressure", "try_ramp")

    def take_action(self) -> Optional[dict]:
        """执行一次出手。返回引擎结果；无可行动作时返回 None。"""
        for fn in self.STRATEGIES:
            r = getattr(self, fn)()
            if r:
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
