"""
AI玩家模块
职责：
1. AI作为决策者调用游戏引擎
2. 决策前必须调用引擎获取状态，禁止自行编造数值
3. 每次行动后由校验器检查合规性
4. 遇到Interrupt时暂停，等待DM裁定

支持的AI后端（全部免费可用）：
- Google Gemini API（免费，无需信用卡）
- Groq API（免费，超快推理）
- OpenRouter API（免费模型变体）
- DeepSeek API（注册送额度）
- 占位符（开发测试用）
"""
from __future__ import annotations
import json
import os
import time
import urllib.request
import urllib.error
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
        raise NotImplementedError

# ========== 通用OpenAI兼容调用器 ==========

def _call_openai_compatible(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.7
) -> Optional[str]:
    """
    通用OpenAI兼容API调用
    支持：Groq, DeepSeek, OpenRouter, 任何OpenAI兼容端点
    """
    url = f"{base_url}/chat/completions"
    
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": temperature,
        "max_tokens": 2000
    }
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
    
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[AI API错误] HTTP {e.code}: {body[:200]}")
        return None
    except Exception as e:
        print(f"[AI API错误] {type(e).__name__}: {e}")
        return None


def _call_gemini(
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str
) -> Optional[str]:
    """Google Gemini API调用（免费，无需信用卡）"""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={api_key}"
    )
    
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": f"{system_prompt}\n\n---\n\n{user_prompt}"}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 2000,
            "responseMimeType": "application/json"
        }
    }
    
    headers = {"Content-Type": "application/json"}
    
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["candidates"][0]["content"]["parts"][0]["text"]
    
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[Gemini错误] HTTP {e.code}: {body[:200]}")
        return None
    except Exception as e:
        print(f"[Gemini错误] {type(e).__name__}: {e}")
        return None


# ========== 系统提示词 ==========

SYSTEM_PROMPT = """你是第四宇宙游戏的AI玩家。你的任务是根据游戏状态做出最优决策。

核心规则：
1. 你只能从"可用行动"列表中选择，不能编造新的行动
2. 所有数值计算由游戏引擎完成，你不要自己计算
3. 每次决策必须返回JSON格式：{"action_type": "...", "params": {...}, "reasoning": "..."}
4. reasoning字段解释你的决策逻辑

决策原则：
- 轮回者需要强烈的生存意志，不能消极等死
- 怪物陷入困境时会尝试逃跑或进化
- 资源（法力、速度、碎片）是稀缺的，要精打细算
- 优先使用能改变局势的道纹，不要无脑输出

可选的action_type包括：
- setup_attributes: 开局分配属性（params: name, blood_points, speed_points, mana_points，总和必须25；成功后先随机列出3件开局遗物候选）
- setup_choose_initial_daowen: 从发现候选中显式选1种作为初始道纹（params: daowen_name）
- setup_choose_resonance: 选择残韵（params: resonance_type，可选"转换"/"反转"/"曲解"）
- setup_choose_region: 选择副本（params: region，可选"罪孽都市"/"扭曲都市"/"龙心谷"）
- choose_discovered_relic: 从当前遗物发现候选中显式选1件（params: relic_name；开局遗物选定后从杀伐闭环发现3种初始道纹）
- choose_discovered_item: 从当前消耗品发现候选中显式选1件（params: item_name）
- pre_battle_action: 局外行动（params: sub_action + tier等）
- use_daowen: 发动道纹（params: daowen_name, x, target）
- prepare_attack: 准备一轮攻击并取得逐击合法目标、闪避、血影与法术反应选项
- resolve_attack: 携带prepare返回的一次性token，逐击显式提交完整选择后原子结算；禁止使用旧attack/dodge_decision
- declare_evolution: 怪物进化·发动原初X（params: monster, daowen, x；仅当可用行动中出现evolution项且available=true时可对其中列出的困境怪物使用，x不得超过max_x_by_mutation，否则触发崩解自杀）
- prepare_monster_phase: 只获取本次怪物阶段的合法道纹、目标、攻击与闪避选项
- resolve_monster_phase: 携带prepare返回的一次性token，为全部可行动怪物提交完整选择后统一结算；禁止使用旧monster_phase
- round_start: 回始
- round_end: 回终
- battle_start: 战始（持有可选战始遗物时，params.relic_choices必须逐件显式提交use及所需X/目标/残韵）
- battle_end: 战终"""

def _build_user_prompt(state: dict, available_actions: dict, context: str) -> str:
    return f"""当前游戏状态：
{json.dumps(state.get('state', {}), ensure_ascii=False, indent=2)}

可用行动：
{json.dumps(available_actions, ensure_ascii=False, indent=2)}

{f'上下文：{context}' if context else ''}

请做出决策，返回JSON格式。"""


def _parse_ai_response(content: str) -> Optional[AIDecision]:
    """解析AI返回的JSON"""
    if not content:
        return None
    
    try:
        # 尝试直接解析
        data = json.loads(content)
        return AIDecision(
            action_type=data.get("action_type", "noop"),
            params=data.get("params", {}),
            reasoning=data.get("reasoning", "")
        )
    except json.JSONDecodeError:
        # 尝试从markdown代码块中提取
        import re
        match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                return AIDecision(
                    action_type=data.get("action_type", "noop"),
                    params=data.get("params", {}),
                    reasoning=data.get("reasoning", "")
                )
            except json.JSONDecodeError:
                pass
        
        # 尝试找到第一个{到最后一个}
        start = content.find('{')
        end = content.rfind('}')
        if start != -1 and end != -1:
            try:
                data = json.loads(content[start:end+1])
                return AIDecision(
                    action_type=data.get("action_type", "noop"),
                    params=data.get("params", {}),
                    reasoning=data.get("reasoning", "")
                )
            except json.JSONDecodeError:
                pass
    
    return None


# ========== 免费AI后端 ==========

class GeminiBackend(AIBackend):
    """
    Google Gemini后端（免费，无需信用卡）
    注册地址：https://aistudio.google.com/apikey
    环境变量：GEMINI_API_KEY
    """
    
    def __init__(self, api_key: str = None, model: str = "gemini-2.0-flash"):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = model
        if not self.api_key:
            raise ValueError(
                "需要设置 GEMINI_API_KEY。\n"
                "免费获取：https://aistudio.google.com/apikey"
            )
    
    def decide(self, state: dict, available_actions: dict, context: str = "") -> AIDecision:
        user_prompt = _build_user_prompt(state, available_actions, context)
        content = _call_gemini(self.api_key, self.model, SYSTEM_PROMPT, user_prompt)
        
        decision = _parse_ai_response(content)
        if decision:
            return decision
        
        # 回退
        return AIDecision("noop", {}, f"[Gemini解析失败] 原始回复: {content[:200] if content else 'None'}")


class GroqBackend(AIBackend):
    """
    Groq后端（免费，超快推理）
    注册地址：https://console.groq.com/keys
    环境变量：GROQ_API_KEY
    """
    
    def __init__(self, api_key: str = None, model: str = "llama-3.3-70b-versatile"):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        self.model = model
        self.base_url = "https://api.groq.com/openai/v1"
        if not self.api_key:
            raise ValueError(
                "需要设置 GROQ_API_KEY。\n"
                "免费获取：https://console.groq.com/keys"
            )
    
    def decide(self, state: dict, available_actions: dict, context: str = "") -> AIDecision:
        user_prompt = _build_user_prompt(state, available_actions, context)
        content = _call_openai_compatible(
            self.base_url, self.api_key, self.model, SYSTEM_PROMPT, user_prompt
        )
        
        decision = _parse_ai_response(content)
        if decision:
            return decision
        
        return AIDecision("noop", {}, f"[Groq解析失败] 原始回复: {content[:200] if content else 'None'}")


class OpenRouterBackend(AIBackend):
    """
    OpenRouter后端（免费模型变体）
    注册地址：https://openrouter.ai/keys
    环境变量：OPENROUTER_API_KEY
    
    免费模型（在模型名后加 :free）：
    - meta-llama/llama-4-scout:free
    - google/gemma-3-27b-it:free
    - deepseek/deepseek-r1-0528:free
    - mistralai/mistral-small-3.1-24b-instruct:free
    """
    
    def __init__(self, api_key: str = None, model: str = "meta-llama/llama-4-scout:free"):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.model = model
        self.base_url = "https://openrouter.ai/api/v1"
        if not self.api_key:
            raise ValueError(
                "需要设置 OPENROUTER_API_KEY。\n"
                "免费获取：https://openrouter.ai/keys\n"
                "免费模型列表：在模型名后加 :free"
            )
    
    def decide(self, state: dict, available_actions: dict, context: str = "") -> AIDecision:
        user_prompt = _build_user_prompt(state, available_actions, context)
        content = _call_openai_compatible(
            self.base_url, self.api_key, self.model, SYSTEM_PROMPT, user_prompt
        )
        
        decision = _parse_ai_response(content)
        if decision:
            return decision
        
        return AIDecision("noop", {}, f"[OpenRouter解析失败] 原始回复: {content[:200] if content else 'None'}")


class DeepSeekBackend(AIBackend):
    """
    DeepSeek后端（注册送额度，性价比极高）
    注册地址：https://platform.deepseek.com/api_keys
    环境变量：DEEPSEEK_API_KEY
    """
    
    def __init__(self, api_key: str = None, model: str = "deepseek-chat"):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self.model = model
        self.base_url = "https://api.deepseek.com/v1"
        if not self.api_key:
            raise ValueError(
                "需要设置 DEEPSEEK_API_KEY。\n"
                "免费获取：https://platform.deepseek.com/api_keys"
            )
    
    def decide(self, state: dict, available_actions: dict, context: str = "") -> AIDecision:
        user_prompt = _build_user_prompt(state, available_actions, context)
        content = _call_openai_compatible(
            self.base_url, self.api_key, self.model, SYSTEM_PROMPT, user_prompt
        )
        
        decision = _parse_ai_response(content)
        if decision:
            return decision
        
        return AIDecision("noop", {}, f"[DeepSeek解析失败] 原始回复: {content[:200] if content else 'None'}")


class PlaceholderBackend(AIBackend):
    """
    占位符后端（开发测试用，不需要API key）
    返回默认决策，不实际调用AI
    """
    
    def decide(self, state: dict, available_actions: dict, context: str = "") -> AIDecision:
        phase = state.get("state", {}).get("phase", "unknown")
        
        inner = state.get("state", {})
        if inner.get("pending_redemption"):
            return AIDecision("resolve_redemption", {"option": 2}, "默认无视救赎")
        if phase == "setup":
            relic_choices = inner.get("pending_relic_choices") or []
            if relic_choices:
                return AIDecision("choose_discovered_relic", {
                    "relic_name": relic_choices[0],
                }, "从发现候选中选择开局遗物")
            choices = inner.get("pending_initial_daowen_choices") or []
            if choices:
                return AIDecision("setup_choose_initial_daowen", {
                    "daowen_name": choices[0],
                }, "从发现候选中选择初始道纹")
            if inner.get("player") is None:
                return AIDecision("setup_attributes", {
                    "name": "AI轮回者",
                    "blood_points": 10,
                    "speed_points": 8,
                    "mana_points": 7
                }, "默认开局分配")
            if not inner.get("resonance"):
                return AIDecision("setup_choose_resonance", {
                    "resonance_type": "转换",
                }, "选择初始残韵")
            if not inner.get("current_region"):
                return AIDecision("setup_choose_region", {
                    "region": "罪孽都市",
                }, "选择初始副本")
            return AIDecision("noop", {}, "开局步骤已完成")
        
        elif phase == "pre_battle":
            engine = getattr(self, "engine", None)
            # 精力耗尽时，可进入战始：按共享策略显式提交可选战始遗物（可以不用但不能不让用）。
            for action in available_actions.get("actions", []) or []:
                if action.get("action_type") == "battle_start" and engine is not None:
                    from sim.optional_actions import battle_start_relic_choices
                    return AIDecision("battle_start", {
                        "relic_choices": battle_start_relic_choices(engine),
                    }, "进入战始并提交可选遗物决策")
            return AIDecision("pre_battle_action", {
                "sub_action": "修行",
                "tier": 1
            }, "默认修行获取属性点")
        
        elif phase == "in_combat":
            pending_type = available_actions.get("action_type")
            # 可选法器（黑金名片/罪业金库/烬翼/鲜血之翼/教父左轮/共心环）：
            # 有引擎引用且对应行动在可用列表时，按策略函数尝试发动（可以不用但不能不让用）。
            # commit=False 只返回计划不执行，由 AIPlayer.play_turn 统一 execute_action。
            engine = getattr(self, "engine", None)
            actions = available_actions.get("actions", [])
            if engine is not None:
                from sim.optional_actions import (
                    try_select_shared_dragon_heart, try_use_black_card,
                    try_use_crime_vault, try_use_dragon_wings,
                    try_use_blood_wings, try_fire_godfather_revolver,
                )
                artifact_map = {
                    "select_shared_dragon_heart": try_select_shared_dragon_heart,
                    "use_black_card": try_use_black_card,
                    "use_crime_vault": try_use_crime_vault,
                    "use_dragon_wings": try_use_dragon_wings,
                    "use_blood_wings": try_use_blood_wings,
                    "fire_godfather_revolver": try_fire_godfather_revolver,
                }
                for action in actions:
                    fn = artifact_map.get(action.get("action_type"))
                    if fn is None:
                        continue
                    plan = fn(engine, commit=False)
                    if plan and plan.get("_plan"):
                        return AIDecision(plan["action_type"], plan["params"],
                                          "发动可选法器")
            if pending_type == "resolve_attack":
                options = available_actions.get("target_options", [])
                target = options[0] if options else None
                hits = [] if target is None else [{
                    "target_ref": target["ref"], "dodge": False, "blood_shadow": False,
                    "spell_choices": {timing: {spell["spell_name"]: {"use": False}
                                               for spell in target.get("spell_options", {}).get(timing, [])}
                                      for timing in ("before", "after")},
                } for _ in range(available_actions.get("hit_count", 0))]
                return AIDecision("resolve_attack", {"token": available_actions["token"], "hits": hits},
                                  "提交完整攻击选择")
            if pending_type == "resolve_monster_phase":
                choices = []
                for actor in available_actions.get("actors", []):
                    dao = None
                    action_count = actor["base_attack_actions"]
                    hit_count = actor["base_hits_per_attack"]
                    if actor["daowen_options"]:
                        option = actor["daowen_options"][0]
                        dao = {"name": option["name"], "dodge": False, "blood_shadow": False,
                               "trigger_spell_choices": {
                                   holder: {spell["spell_name"]: {"use": False} for spell in spells}
                                   for holder, spells in option.get("trigger_spell_options", {}).items()}}
                        if option["requires_target"]: dao["target_ref"] = option["target_options"][0]["ref"]
                        if option["dodge_submission"] == "per_target":
                            # 波及X：必须恰好提交X个目标（候选全量提交会在候选>X时被拒）。
                            from sim.monster_targets import pick_wave_dodge_targets
                            dao["dodge_targets"] = pick_wave_dodge_targets(option)
                        if option["resolves_as"] == "疯狂": action_count += option["x"]
                        if option["resolves_as"] == "狂暴": action_count += 1
                    target = (actor["attack_target_options"][0]
                              if actor["attack_target_options"] else None)
                    if target is None:
                        # 无合法攻击目标：本回合不出手（引擎prepare已置base_attack_actions=0）
                        attacks = []
                    else:
                        spell_choices = {timing: {spell["spell_name"]: {"use": False}
                                                  for spell in target.get("spell_options", {}).get(timing, [])}
                                         for timing in ("before", "after")}
                        attacks = [{"hits": [{"target_ref": target["ref"], "dodge": False,
                                               "blood_shadow": False, "spell_choices": spell_choices}
                                              for _ in range(hit_count)]}
                                   for _ in range(action_count)]
                    choices.append({"actor_ref": actor["actor_ref"], "daowen": dao,
                                    "attack_actions": attacks})
                return AIDecision("resolve_monster_phase",
                                  {"token": available_actions["token"], "choices": choices},
                                  "提交完整怪物阶段选择")
            actions = available_actions.get("actions", [])
            for action in actions:
                action_type = action.get("action_type")
                if action_type in ("round_start", "round_end", "prepare_monster_phase"):
                    round_params = {}
                    if action_type == "round_start":
                        engine = getattr(self, "engine", None)
                        if engine is not None:
                            from sim.optional_actions import round_start_relic_choices
                            choices = round_start_relic_choices(engine)
                        else:
                            schema = action.get("params_schema", {}).get("relic_choices", {})
                            choices = {name: {"use": False}
                                       for name in schema if name != "_instruction"}
                        round_params = {"relic_choices": choices}
                    return AIDecision(action_type, round_params,
                                      "推进合法战斗子阶段")
                if action_type == "use_daowen" and action.get("available", True):
                    schema = action["params_schema"]
                    enemies = [target for target in schema.get("target_ref", [])
                               if str(target.get("ref", "")).startswith("enemy:")]
                    x_schema = schema.get("x", {})
                    # 指令道纹的x是固定整数；玩家道纹的x是{minimum,maximum}范围
                    if isinstance(x_schema, dict):
                        x = max(1, min(5, x_schema["maximum"]))
                    else:
                        x = int(x_schema)
                    params = {"daowen_name": schema["daowen_name"], "x": x,
                              "dodge": False, "blood_shadow": False,
                              "trigger_spell_choices": {}}
                    if "actor_ref" in schema: params["actor_ref"] = schema["actor_ref"]
                    if schema["daowen_name"] == "波及" and engine is not None:
                        # 波及X：必须恰好提交X个存活非自身目标（schema上限已按目标数封顶）
                        refs = engine.combat._combat_entity_refs()
                        actor = refs.get(schema.get("actor_ref", "")) or engine.state.player
                        candidates = [r for r, e in refs.items()
                                      if e.is_alive and e is not actor]
                        params["dodge_targets"] = [{"target_ref": r, "dodge": False,
                                                    "blood_shadow": False}
                                                   for r in candidates[:x]]
                    elif enemies:
                        params["target_ref"] = enemies[0]["ref"]
                    return AIDecision("use_daowen", params, "使用合法道纹选项")
                if action_type == "prepare_attack":
                    actor_schema = action["params_schema"]["actor_ref"]
                    actor_ref = actor_schema[0]["ref"] if isinstance(actor_schema, list) else actor_schema
                    return AIDecision("prepare_attack", {"actor_ref": actor_ref}, "准备攻击")

        return AIDecision("noop", {}, "无可用行动")


# ========== 便捷工厂函数 ==========

def create_ai_backend(provider: str = "placeholder", **kwargs) -> AIBackend:
    """
    创建AI后端的便捷函数
    
    用法：
        backend = create_ai_backend("gemini")          # 从环境变量读取key
        backend = create_ai_backend("groq", api_key="gsk_xxx")
        backend = create_ai_backend("openrouter")       # 免费模型
        backend = create_ai_backend("deepseek")
        backend = create_ai_backend("placeholder")      # 测试用
    """
    providers = {
        "gemini": GeminiBackend,
        "google": GeminiBackend,
        "groq": GroqBackend,
        "openrouter": OpenRouterBackend,
        "deepseek": DeepSeekBackend,
        "placeholder": PlaceholderBackend,
        "test": PlaceholderBackend,
    }
    
    provider = provider.lower()
    if provider not in providers:
        raise ValueError(f"未知AI提供商: {provider}。可用: {list(providers.keys())}")
    
    return providers[provider](**kwargs)


# ========== AI玩家控制器 ==========

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
        # 让后端能读到引擎实时状态，用于可选法器/遗物的显式决策（可以不用但不能不让用）。
        if not getattr(self.backend, "engine", None):
            try:
                self.backend.engine = game_engine
            except Exception:
                pass
        self.validator = validator or RuleValidator()
        self.rule_sync = rule_sync
        self.auto_validate = auto_validate
        self.max_retries = max_retries
        
        self._decision_history: list[dict] = []
        self._violation_callbacks: list[Callable] = []
    
    def on_violation(self, callback: Callable):
        """注册违规回调"""
        self._violation_callbacks.append(callback)
    
    def play_turn(self, context: str = "") -> dict:
        """执行一个回合的AI决策"""
        state = self.engine.get_state()
        
        if state.get("pending_interrupts"):
            return {
                "action": "等待DM裁定",
                "interrupts": state["pending_interrupts"],
                "instruction": "有中断等待DM裁定，AI无法继续决策"
            }
        
        available_actions = self.engine.get_available_actions()
        decision = self.backend.decide(state, available_actions, context)
        
        result = self.engine.execute_action(decision.action_type, decision.params)
        
        validation = {"valid": True, "violations": [], "warnings": []}
        if self.auto_validate:
            validation = self.validator.validate(self.engine.state, {
                "action": decision.action_type,
                "params": decision.params
            }, result)
            
            if not validation["valid"]:
                for callback in self._violation_callbacks:
                    try:
                        callback(validation)
                    except Exception:
                        pass
        
        sync_report = None
        if self.rule_sync:
            changes = self.rule_sync.check_for_changes()
            if changes:
                sync_report = self.rule_sync.generate_sync_report()
        
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
    """带重试的AI玩家"""
    
    def play_turn(self, context: str = "") -> dict:
        for attempt in range(self.max_retries):
            result = super().play_turn(context)
            
            if result.get("validation", {}).get("valid", True):
                return result
            
            violations = result["validation"].get("violations", [])
            violation_desc = "; ".join(
                v.get("violation_description", "") for v in violations
            )
            context = f"{context}\n注意：上次决策违规了：{violation_desc}。请重新决策。"
        
        result["retry_exhausted"] = True
        return result
