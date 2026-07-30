"""
AI玩家模块
职责：
1. AI作为决策者调用游戏引擎
2. 决策前必须调用引擎获取状态，禁止自行编造数值
3. 每次行动后由校验器检查合规性
4. 遇到Interrupt时暂停，等待DM裁定

支持的AI后端：
- OpenAI API (GPT-4等)
- 本地模型 (通过Ollama等)
- 占位符（开发测试用）
"""
from __future__ import annotations
import json
import os
import time
from typing import Optional, Any, Callable
from .api import GameEngine
from .validator import RuleValidator
from .rule_sync import RuleSync
from .dm_rulings import Interrupt


class AIDecision:
    """AI决策记录"""
    def __init__(self, action_type: str, params: dict, reasoning: str = ""):
        self.action_type = action_type
        self.params = params
        self.reasoning = reasoning
        self.timestamp = time.time()
    
    def to_dict(self) -> dict:
        return {
            "action_type": self.action_type,
            "params": self.params,
            "reasoning": self.reasoning,
            "timestamp": self.timestamp
        }


class AIBackend:
    """AI后端接口（抽象基类）"""
    
    def decide(self, state: dict, available_actions: dict, context: str = "") -> AIDecision:
        """
        根据当前状态和可用行动，做出决策
        返回AIDecision
        """
        raise NotImplementedError
    
    def validate_result(self, action: dict, result: dict) -> dict:
        """
        AI自我检查结果是否合理
        返回：{valid, concerns, suggestions}
        """
        return {"valid": True, "concerns": [], "suggestions": []}


class PlaceholderBackend(AIBackend):
    """
    占位符后端（开发测试用）
    返回默认决策，不实际调用AI
    """
    
    def decide(self, state: dict, available_actions: dict, context: str = "") -> AIDecision:
        phase = state.get("state", {}).get("phase", "unknown")
        
        if phase == "setup":
            return AIDecision("setup_attributes", {
                "name": "AI轮回者",
                "blood_points": 10,
                "speed_points": 8,
                "mana_points": 7
            }, "默认开局分配")
        
        elif phase == "pre_battle":
            return AIDecision("pre_battle_action", {
                "sub_action": "修行",
                "tier": 1
            }, "默认修行获取属性点")
        
        elif phase == "in_combat":
            # 默认使用第一个可用道纹
            actions = available_actions.get("actions", [])
            for a in actions:
                if a.get("type") == "daowen" and a.get("available", True):
                    return AIDecision("use_daowen", {
                        "daowen_name": a["id"],
                        "x": min(5, a.get("max_x", 1)),
                        "target": state.get("state", {}).get("enemies", [{}])[0].get("name", "")
                    }, "使用道纹")
            
            return AIDecision("attack", {
                "target_selections": [0]
            }, "普通攻击")
        
        return AIDecision("noop", {}, "无可用行动")


class OpenAIBackend(AIBackend):
    """
    OpenAI API后端
    需要设置环境变量 OPENAI_API_KEY
    """
    
    def __init__(self, model: str = "gpt-4", api_key: str = None):
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("需要设置 OPENAI_API_KEY 环境变量或传入 api_key 参数")
    
    def decide(self, state: dict, available_actions: dict, context: str = "") -> AIDecision:
        # 构造prompt
        prompt = self._build_prompt(state, available_actions, context)
        
        # 调用OpenAI API
        try:
            import openai
            client = openai.OpenAI(api_key=self.api_key)
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                response_format={"type": "json_object"}
            )
            
            content = response.choices[0].message.content
            decision_data = json.loads(content)
            
            return AIDecision(
                action_type=decision_data.get("action_type", "noop"),
                params=decision_data.get("params", {}),
                reasoning=decision_data.get("reasoning", "")
            )
        
        except Exception as e:
            # AI调用失败，回退到占位符
            fallback = PlaceholderBackend()
            decision = fallback.decide(state, available_actions, context)
            decision.reasoning = f"[AI调用失败: {str(e)}] {decision.reasoning}"
            return decision
    
    def _system_prompt(self) -> str:
        return """你是第四宇宙游戏的AI玩家。你的任务是根据游戏状态做出最优决策。

核心规则：
1. 你只能从"可用行动"列表中选择，不能编造新的行动
2. 所有数值计算由游戏引擎完成，你不要自己计算
3. 每次决策必须返回JSON格式：{"action_type": "...", "params": {...}, "reasoning": "..."}
4. reasoning字段解释你的决策逻辑

决策原则：
- 轮回者需要强烈的生存意志，不能消极等死
- 怪物陷入困境时会尝试逃跑或进化
- 资源（法力、速度、碎片）是稀缺的，要精打细算
- 优先使用能改变局势的道纹，不要无脑输出"""
    
    def _build_prompt(self, state: dict, available_actions: dict, context: str) -> str:
        return f"""当前游戏状态：
{json.dumps(state.get('state', {}), ensure_ascii=False, indent=2)}

可用行动：
{json.dumps(available_actions, ensure_ascii=False, indent=2)}

{f'上下文：{context}' if context else ''}

请做出决策，返回JSON格式。"""


class AIPlayer:
    """
    AI玩家控制器
    将AI决策引擎与游戏引擎连接，加入规则校验
    """
    
    def __init__(
        self,
        game_engine: GameEngine,
        backend: AIBackend = None,
        validator: RuleValidator = None,
        rule_sync: RuleSync = None,
        auto_validate: bool = True,
        max_retries: int = 3
    ):
        self.engine = game_engine
        self.backend = backend or PlaceholderBackend()
        self.validator = validator or RuleValidator()
        self.rule_sync = rule_sync
        self.auto_validate = auto_validate
        self.max_retries = max_retries
        
        self._decision_history: list[dict] = []
        self._violation_callbacks: list[Callable] = []
    
    def on_violation(self, callback: Callable):
        """注册违规回调（违规发现时通知外部）"""
        self._violation_callbacks.append(callback)
    
    # ========== 主循环 ==========
    
    def play_turn(self, context: str = "") -> dict:
        """
        执行一个回合的AI决策
        返回完整的结果报告
        """
        # 1. 获取状态
        state = self.engine.get_state()
        
        # 2. 检查是否有待处理中断
        if state.get("pending_interrupts"):
            return {
                "action": "等待DM裁定",
                "interrupts": state["pending_interrupts"],
                "instruction": "有中断等待DM裁定，AI无法继续决策"
            }
        
        # 3. AI决策
        available_actions = self.engine.get_available_actions()
        decision = self.backend.decide(state, available_actions, context)
        
        # 4. 执行行动
        result = self.engine.execute_action(decision.action_type, decision.params)
        
        # 5. 校验结果
        validation = {"valid": True, "violations": [], "warnings": []}
        if self.auto_validate:
            validation = self.validator.validate(self.engine.state, {
                "action": decision.action_type,
                "params": decision.params
            }, result)
            
            # 通知违规
            if not validation["valid"]:
                for callback in self._violation_callbacks:
                    try:
                        callback(validation)
                    except Exception:
                        pass
        
        # 6. 检查规则文件同步（如果启用了）
        sync_report = None
        if self.rule_sync:
            changes = self.rule_sync.check_for_changes()
            if changes:
                sync_report = self.rule_sync.generate_sync_report()
        
        # 7. 记录决策历史
        record = {
            "decision": decision.to_dict(),
            "result": result,
            "validation": validation,
            "sync_report": sync_report,
            "timestamp": time.time()
        }
        self._decision_history.append(record)
        
        return {
            "action": decision.action_type,
            "params": decision.params,
            "reasoning": decision.reasoning,
            "result": result,
            "validation": validation,
            "sync_report": sync_report,
            "interrupt": result.get("interrupt"),
        }
    
    def play_until_interrupt(self, max_turns: int = 100, context: str = "") -> dict:
        """
        持续执行直到遇到中断或游戏结束
        """
        turns_played = 0
        all_results = []
        
        while turns_played < max_turns:
            result = self.play_turn(context)
            all_results.append(result)
            turns_played += 1
            
            # 遇到中断停止
            if result.get("interrupt"):
                return {
                    "status": "interrupted",
                    "turns_played": turns_played,
                    "results": all_results,
                    "interrupt": result["interrupt"],
                    "instruction": "遇到中断，需要DM裁定"
                }
            
            # 游戏结束
            if result.get("result", {}).get("state", {}).get("phase") == "game_over":
                return {
                    "status": "game_over",
                    "turns_played": turns_played,
                    "results": all_results
                }
            
            # 行动失败且无法继续
            if not result.get("result", {}).get("success"):
                error = result.get("result", {}).get("error", "")
                if "无法" in error or "不足" in error:
                    # AI可能需要换策略，再试一次
                    continue
        
        return {
            "status": "max_turns_reached",
            "turns_played": turns_played,
            "results": all_results
        }
    
    # ========== 历史与统计 ==========
    
    def get_history(self) -> list[dict]:
        return self._decision_history
    
    def get_stats(self) -> dict:
        total = len(self._decision_history)
        violations = sum(1 for d in self._decision_history 
                        if not d.get("validation", {}).get("valid", True))
        interrupts = sum(1 for d in self._decision_history 
                        if d.get("result", {}).get("interrupt"))
        
        return {
            "total_decisions": total,
            "violations_found": violations,
            "interrupts_triggered": interrupts,
            "compliance_rate": f"{(total - violations) / total * 100:.1f}%" if total > 0 else "N/A"
        }


class AIWithRetry(AIPlayer):
    """
    带重试的AI玩家
    当校验发现违规时，自动要求AI重新决策
    """
    
    def play_turn(self, context: str = "") -> dict:
        for attempt in range(self.max_retries):
            result = super().play_turn(context)
            
            if result.get("validation", {}).get("valid", True):
                return result
            
            # 违规了，加到上下文让AI知道
            violations = result["validation"].get("violations", [])
            violation_desc = "; ".join(
                v.get("violation_description", "") for v in violations
            )
            context = f"{context}\n注意：上次决策违规了：{violation_desc}。请重新决策。"
        
        # 重试用完，返回最后一次结果
        result["retry_exhausted"] = True
        return result
