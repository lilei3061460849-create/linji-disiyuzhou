"""
战术AI：为轮回者在战斗中选择行动。

背景：早前的 sim/format_trace.py 里写死了"只发杀伐"，导致战报里 AI 显得很蠢
（不闪避决策、不用残韵、不学法术、不吃消耗品、不控场）。本模块把决策逻辑
独立出来，按优先级评估**引擎已实装**的全部手段。

决策优先级（每次出手依次判断，命中即执行）：
  1. 保命：致死威胁 → 庇护(4X格挡) / 再生(3X生命)
  2. 残韵插队：敌方关键道纹(必中/狂暴/强化/飞行等) → 反转/曲解/转换 改写它
  3. 控场：敌方高攻 → 束缚/眩晕/减速/僵化 等已实装控制
  4. 收割：能一击打死的目标 → 杀伐
  5. AOE：敌数≥3 → 冲击
  6. 续航：血量健康时 → 锐利(削血限) / 焦点杀伐

设计原则：
- 只调用 GameEngine.execute_action，不直接改 state，保证一切走引擎校验。
- 任何一步失败(法力不足/未持有/冷却)都安全跳过，不中断整局。
- 不产生数值：X 值由当前法力与目标血量推导，交由引擎结算。
"""
from __future__ import annotations

import math
from typing import Any, Optional

# 已在 engine/daowen.py 注册、且适合玩家主动发动的控制类道纹
CONTROL_DAOWEN = ["束缚", "眩晕", "减速", "僵化", "蒙蔽", "坏死", "定型", "退化"]

# 敌方身上值得用残韵改写的高价值道纹，按威胁度排序。
# 残韵闭环已补齐 6 组 57 条路径（两条主轨 + 三副本 + 怪物原始道纹树），
# 因此怪物面板上的道纹现在同样可被残韵改写。
HIGH_VALUE_ENEMY_DAOWEN = [
    # 怪物原始道纹：直接削弱其输出/生存/机动
    "必中",   # →蒙蔽：使其下X次伤害无效
    "狂暴",   # →无神/自残：强制其自伤或改打自己
    "自愈",   # →衰败：回复变成掉血
    "强化",   # →弱化：攻击力反向
    "活力",   # →无力：出手次数反向
    "飞行",   # →坠落：破飞行
    "减速",   # →眩晕：反过来控它
    # 核心主轨
    "贯穿", "血债", "冲击", "杀伐", "增殖", "透支",
    "固执", "庇护", "再生", "锐利", "缓慢", "束缚",
]


class TacticalAI:
    """轮回者战斗AI。engine 为 GameEngine 实例。"""

    def __init__(self, engine: Any, verbose: bool = False):
        self.engine = engine
        self.verbose = verbose
        self.log: list[str] = []

    # ---------- 工具 ----------

    @property
    def player(self):
        return self.engine.state.player

    def alive_enemies(self) -> list:
        return [e for e in self.engine.state.enemies if e.is_alive]

    def has(self, daowen: str) -> bool:
        return daowen in self.player.dao_wen

    def mana(self) -> int:
        return self.player.current_mana

    def incoming_damage(self) -> int:
        """敌方本回合理论最大输出，用于判断是否致死。"""
        return sum(e.attack_count * e.attack_power for e in self.alive_enemies())

    def _do(self, action: str, params: dict) -> Optional[dict]:
        r = self.engine.execute_action(action, params)
        if r.get("success"):
            return r
        if self.verbose:
            self.log.append(f"[跳过] {action}{params}: {r.get('error')}")
        return None

    # ---------- 各优先级策略 ----------

    def try_survive(self) -> Optional[dict]:
        """1. 保命：面临致死威胁时优先庇护/再生。"""
        p = self.player
        threat = self.incoming_damage()
        if threat <= 0:
            return None
        lethal = threat >= p.current_hp
        heavy = threat >= p.current_hp * 0.5

        if (lethal or heavy) and self.has("庇护") and self.mana() >= 1:
            need = max(0, threat - p.shield)
            x = min(self.mana(), math.ceil(need / 4))
            if x >= 1:
                r = self._do("use_daowen", {"daowen_name": "庇护", "x": x})
                if r:
                    return r
        if p.current_hp <= p.blood_limit * 0.35 and self.has("再生") and self.mana() >= 1:
            if not p.has_status("坏死"):
                deficit = p.blood_limit - p.current_hp
                x = min(self.mana(), max(1, math.ceil(deficit / 3)))
                r = self._do("use_daowen", {"daowen_name": "再生", "x": x})
                if r:
                    return r
        return None

    def try_resonance(self) -> Optional[dict]:
        """
        2. 残韵插队：改写敌方关键道纹（残韵可任意时刻插队）。

        先用 ResonanceEngine.get_available_resonance 查询该道纹**实际存在**的变化路径，
        只对存在的路径发动，避免盲目穷举三种残韵而全部失败。
        """
        from engine.daowen import ResonanceEngine

        stock = {k: v for k, v in self.engine.state.resonance.items() if v > 0}
        if not stock:
            return None
        for enemy in self.alive_enemies():
            for dw in HIGH_VALUE_ENEMY_DAOWEN:
                if dw not in enemy.dao_wen:
                    continue
                for path in ResonanceEngine.get_available_resonance(dw):
                    rtype = path.get("resonance_type") or path.get("type")
                    if not rtype or stock.get(rtype, 0) <= 0:
                        continue
                    r = self._do("use_resonance",
                                 {"source_daowen": dw, "resonance_type": rtype,
                                  "target": enemy.name})
                    if r:
                        return r
        return None

    def try_control(self) -> Optional[dict]:
        """3. 控场：对威胁最大的敌人施加已实装的控制道纹。"""
        enemies = self.alive_enemies()
        if not enemies or self.mana() < 1:
            return None
        if self.incoming_damage() < self.player.current_hp * 0.25:
            return None
        top = max(enemies, key=lambda e: e.attack_count * e.attack_power)
        for dw in CONTROL_DAOWEN:
            if not self.has(dw):
                continue
            x = min(self.mana(), 3)
            r = self._do("use_daowen", {"daowen_name": dw, "x": x, "target": top.name})
            if r:
                return r
        return None

    def try_finish(self) -> Optional[dict]:
        """4. 收割：优先打死能一击击杀的目标（杀伐造成2X伤害）。"""
        if not self.has("杀伐") or self.mana() < 1:
            return None
        for e in sorted(self.alive_enemies(), key=lambda e: e.current_hp):
            need = math.ceil(e.current_hp / 2)
            if 1 <= need <= self.mana():
                r = self._do("use_daowen",
                             {"daowen_name": "杀伐", "x": need, "target": e.name})
                if r:
                    return r
        return None

    def try_aoe(self) -> Optional[dict]:
        """5. AOE：敌数≥3 时用冲击群伤。"""
        enemies = self.alive_enemies()
        if len(enemies) < 3 or not self.has("冲击") or self.mana() < 1:
            return None
        x = min(self.mana(), max(e.current_hp for e in enemies))
        return self._do("use_daowen", {"daowen_name": "冲击", "x": x})

    def remaining_actions(self) -> int:
        """本回合还剩几次出手（出手次数 = [速限]/3 向上取整）。"""
        total = max(1, math.ceil(self.player.speed_limit / 3))
        return max(0, total - getattr(self.player, "actions_used_this_round", 0))

    def mana_budget(self) -> int:
        """
        单次出手可用的法力预算。

        法力[回始]补满、[敌回终]清空，因此本回合不用完就是浪费；
        但一次性梭哈会让后续出手无法力可用（这正是早前"只会发杀伐"的成因）。
        故按剩余出手次数均分，最后一次出手允许用尽。
        """
        left = self.remaining_actions()
        if left <= 1:
            return self.mana()
        return max(1, self.mana() // left)

    def try_pressure(self) -> Optional[dict]:
        """6. 续航输出：锐利削血限，否则焦点杀伐（按预算分配法力）。"""
        enemies = self.alive_enemies()
        if not enemies or self.mana() < 1:
            return None
        target = min(enemies, key=lambda e: e.current_hp)
        budget = self.mana_budget()
        if self.has("锐利") and budget >= 3:
            x = max(1, budget // 3)
            r = self._do("use_daowen",
                         {"daowen_name": "锐利", "x": x, "target": target.name})
            if r:
                return r
        if self.has("杀伐"):
            return self._do("use_daowen",
                            {"daowen_name": "杀伐", "x": budget, "target": target.name})
        return None

    def try_consumable(self) -> Optional[dict]:
        """消耗品：使用不消耗出手，故在出手循环外单独尝试。"""
        p = self.player
        for item in list(self.engine.state.consumables):
            if getattr(item, "current_uses", 0) <= 0:
                continue
            if p.current_hp <= p.blood_limit * 0.4:
                r = self._do("consume_item", {"name": item.name})
                if r:
                    return r
        return None

    # ---------- 主入口 ----------

    def take_action(self) -> Optional[dict]:
        """执行一次出手。返回引擎结果；无可行动作时返回 None。"""
        for strategy in (self.try_survive, self.try_resonance, self.try_control,
                         self.try_finish, self.try_aoe, self.try_pressure):
            r = strategy()
            if r:
                return r
        return None

    def take_turn(self) -> list[dict]:
        """执行本回合全部出手（出手次数 = [速限]/3，向上取整）。"""
        results = []
        # 消耗品不消耗出手，先结算
        c = self.try_consumable()
        if c:
            results.append(c)
        actions = max(1, math.ceil(self.player.speed_limit / 3))
        for _ in range(actions):
            if not self.alive_enemies() or not self.player.is_alive:
                break
            r = self.take_action()
            if not r:
                break
            results.append(r)
        return results
