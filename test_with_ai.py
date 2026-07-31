#!/usr/bin/env python3
"""
一键测试脚本 - 本地运行
用法: python test_with_ai.py [gemini|groq|openrouter|deepseek]

首次使用：
  1. 去 https://aistudio.google.com/apikey 免费获取Gemini API key
  2. 设置环境变量: export GEMINI_API_KEY="你的key"
  3. 运行: python test_with_ai.py
"""
import sys
import os
import json

# 确保在项目目录运行
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from engine.api import GameEngine
from engine.validator import RuleValidator
from engine.rule_sync import RuleSync
from engine.ai_player import AIPlayer, AIWithRetry, create_ai_backend


def find_available_backend():
    """自动检测可用的AI后端"""
    providers = [
        ("gemini", "GEMINI_API_KEY", "Gemini (Google免费)"),
        ("groq", "GROQ_API_KEY", "Groq (超快)"),
        ("openrouter", "OPENROUTER_API_KEY", "OpenRouter (免费模型)"),
        ("deepseek", "DEEPSEEK_API_KEY", "DeepSeek (中文强)"),
    ]
    
    for provider, env_key, name in providers:
        if os.environ.get(env_key):
            try:
                backend = create_ai_backend(provider)
                print(f"✓ 使用 {name}")
                return backend
            except Exception as e:
                print(f"✗ {name}: {e}")
    
    print("⚠ 没有找到AI API key，使用占位符后端（仅测试管线）")
    print("  推荐: export GEMINI_API_KEY='你的key'")
    print("  免费获取: https://aistudio.google.com/apikey")
    from engine.ai_player import PlaceholderBackend
    return PlaceholderBackend()


def main():
    provider = sys.argv[1] if len(sys.argv) > 1 else None
    
    print("=" * 60)
    print("第四宇宙 · AI游戏测试")
    print("=" * 60)
    
    # 初始化
    engine = GameEngine(db_path="data/game_rulings.db")
    validator = RuleValidator(db_path="data/game_violations.db")
    sync = RuleSync(rule_files=["README.md"], db_path="data/game_sync.db")
    
    # 找AI后端
    if provider:
        backend = create_ai_backend(provider)
        print(f"✓ 使用 {provider}")
    else:
        backend = find_available_backend()
    
    # 创建AI玩家
    ai = AIWithRetry(engine, backend, validator, sync, max_retries=2)
    
    # 违违规回调
    def on_violation(v):
        for x in v.get("violations", []):
            print(f"  🚨 违规: [{x.get('severity')}] {x.get('rule_name')}: {x.get('violation_description')}")
    ai.on_violation(on_violation)
    
    # === 开局 ===
    print("\n[开局]")
    for step in ["分配属性", "选择道纹", "选择残韵", "选择副本"]:
        r = ai.play_turn(step)
        action = r.get("action", "?")
        reasoning = r.get("reasoning", "")[:60]
        success = r.get("result", {}).get("success", False)
        print(f"  {step}: {action} ({'✓' if success else '✗'}) {reasoning}")
    
    # === 局外阶段 ===
    print("\n[局外阶段]")
    for i in range(3):
        r = ai.play_turn(f"局外第{i+1}次行动，精力{engine.state.energy}")
        print(f"  第{i+1}次: {r.get('action','?')}")
    
    # === 战斗 ===
    print("\n[战斗]")
    r = ai.play_turn("进入战斗")
    print(f"  战始: {r.get('action','?')}")
    
    # 回始+行动
    r = ai.play_turn("回始")
    print(f"  回始: {r.get('action','?')}")
    
    r = ai.play_turn("战斗中行动")
    print(f"  行动: {r.get('action','?')}")
    
    # === 统计 ===
    stats = ai.get_stats()
    print("\n" + "=" * 60)
    print(f"测试完成: {stats['total_decisions']}次决策, 合规率{stats['compliance_rate']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
