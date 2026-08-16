"""
防死循环与战术诊断守门员系统 (Watchdog & Tactical Diagnostic System)

功能：
1. 战斗动作级停滞探针：监控连续出手无血量削减、伤害被完全吸收或循环出招的情况，
   自动中断并输出精准机制归因（如 固执上限/爆裂反噬/自愈回满/飞行阻断/龙鳞减免等）与对应解法。
2. 连续失败熔断与综合归因：多场连续未通关时自动熔断，分析经济卡点、道纹构筑缺陷与机制对策盲区。
"""

from typing import Optional, Any
from engine.models import GameState, Entity


class CombatWatchdog:
    """战斗时序停滞与死循环监控器"""

    def __init__(self, max_stagnant_actions: int = 6, max_rounds: int = 20):
        self.max_stagnant_actions = max_stagnant_actions
        self.max_rounds = max_rounds
        self.history = []
        self.consecutive_stagnant_actions = 0
        self.last_monster_hp_snapshot = {}

    def reset_battle(self, state: Optional[GameState] = None):
        self.history.clear()
        self.consecutive_stagnant_actions = 0
        if state:
            live = [m for m in state.enemies if m.is_alive]
            self.last_monster_hp_snapshot = {m.name: m.current_hp for m in live}
        else:
            self.last_monster_hp_snapshot.clear()

    def record_action(self, action_name: str, params: dict, result: dict, state: GameState) -> Optional[dict]:
        """记录一次出手并检查是否存在无进展循环。若判定陷入死循环则返回诊断中断字典。"""
        if not result.get("success"):
            return None

        # 捕获怪物生命快照
        live_monsters = [m for m in state.enemies if m.is_alive]
        current_snapshot = {m.name: m.current_hp for m in live_monsters}

        # 检查怪物生命是否有净减少
        total_hp_before = sum(self.last_monster_hp_snapshot.values()) if self.last_monster_hp_snapshot else sum(current_snapshot.values())
        total_hp_now = sum(current_snapshot.values())

        is_stagnant = False
        if action_name in ("use_daowen", "attack"):
            if total_hp_now >= total_hp_before and total_hp_now > 0:
                self.consecutive_stagnant_actions += 1
                is_stagnant = True
            else:
                self.consecutive_stagnant_actions = 0

        self.last_monster_hp_snapshot = current_snapshot
        self.history.append({
            "action": action_name,
            "params": params,
            "result_action": result.get("action"),
            "stagnant": is_stagnant,
            "monster_hp": current_snapshot,
            "player_hp": state.player.current_hp if state.player else 0,
            "round": state.current_round,
        })

        # 判定是否触发停滞中断
        if self.consecutive_stagnant_actions >= self.max_stagnant_actions:
            diag = self.diagnose_stagnation(state)
            return {
                "interrupted": True,
                "reason": "stagnant_loop",
                "message": f"检测到连续 {self.consecutive_stagnant_actions} 次出手敌方生命未出现削减，触发自动中断！",
                "diagnosis": diag,
            }

        return None

    def diagnose_stagnation(self, state: GameState) -> dict:
        """分析导致停滞的怪物机制与应对方案"""
        findings = []
        recommendations = []
        player = state.player

        for m in state.enemies:
            if not m.is_alive:
                continue

            # 1. 固执判定
            if m.has_status("固执"):
                val = m.get_status_value("固执")
                findings.append(f"敌方【{m.name}】处于【固执{val}】状态（单次失去生命上限为1），常规大额伤害被完全锁死。")
                recommendations.append(f"对策方案：使用【残韵·反转】将【固执】逆转为【血债】，或使用【血债X】打出X次独立1点伤害绕过上限！")

            # 2. 飞行判定
            if m.has_status("飞行") or m.is_flying:
                findings.append(f"敌方【{m.name}】处于【飞行】状态，地面攻击无法锁定目标。")
                recommendations.append(f"对策方案：使用【残韵·反转】将【飞行】篡改为【坠落】击落，或使用扭曲工具【反怪物电击枪】！")

            # 3. 爆裂反噬判定
            if m.has_status("爆裂"):
                val = m.get_status_value("爆裂")
                findings.append(f"敌方【{m.name}】处于【爆裂{val}】状态，受到伤害前对攻击者进行100%反噬。")
                recommendations.append(f"对策方案：使用【残韵·曲解】将【爆裂】篡改为【退化】或【坏死】瓦解反噬！")

            # 4. 自愈/活血回血抵消判定
            if m.has_status("自愈"):
                findings.append(f"敌方【{m.name}】处于【自愈】状态，回始巨额回复抵消了常规攻击。")
                recommendations.append(f"对策方案：使用【残韵·反转】将【自愈】篡改为【衰败】使其自损生命！")

            # 5. 格挡阻隔判定
            if m.shield >= 20:
                findings.append(f"敌方【{m.name}】持有高额格挡（{m.shield}点），低额攻击无法破防。")
                recommendations.append(f"对策方案：使用【贯穿】（无视格挡）或高代数【冲击】/【杀伐】破盾！")

        if not findings:
            findings.append("施法者法力或代数配置不足，导致未能击穿敌方护盾。")
            recommendations.append("提高局外修行档位，将法限提升至40~60以上，增强单次出手爆发力。")

        return {
            "findings": findings,
            "recommendations": recommendations,
        }


class RunFailureWatchdog:
    """连续失败熔断与综合归因分析器"""

    def __init__(self, max_consecutive_failures: int = 10):
        self.max_consecutive_failures = max_consecutive_failures
        self.consecutive_failures = 0
        self.failure_records = []

    def record_run(self, cleared_battles: int, won: bool, death_cause: str, details: dict = None) -> Optional[dict]:
        if won:
            self.consecutive_failures = 0
            return None

        self.consecutive_failures += 1
        self.failure_records.append({
            "run_index": len(self.failure_records) + 1,
            "cleared_battles": cleared_battles,
            "death_cause": death_cause,
            "details": details or {},
        })

        if self.consecutive_failures >= self.max_consecutive_failures:
            return {
                "interrupted": True,
                "reason": "max_failures_exceeded",
                "message": f"连续 {self.consecutive_failures} 轮轮回未能通关，触发熔断保护！",
                "analysis": self.analyze_failures(),
            }
        return None

    def analyze_failures(self) -> dict:
        """分析连续失败的宏观结构性原因"""
        battle_stops = {}
        for r in self.failure_records[-self.max_consecutive_failures:]:
            b = r["cleared_battles"] + 1
            battle_stops[b] = battle_stops.get(b, 0) + 1

        common_stopper = max(battle_stops.items(), key=lambda x: x[1])[0]
        return {
            "total_failures": self.consecutive_failures,
            "failure_distribution": battle_stops,
            "primary_bottleneck_battle": f"第 {common_stopper} 场",
            "suggestion": "检查该场次怪物的专属道纹机制，并在前置局外阶段准备对应残韵（反转/曲解/转换）与高档位修行法力。",
        }
