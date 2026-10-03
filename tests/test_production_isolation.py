"""正式 LLM 决策路径与实验规则 AI 的隔离回归（架构契约，不测提示词措辞）。

2026-10-02 架构收口后的四条硬契约：

1. **方向**：tests/probes/sim → production。生产模块（engine/ 下除 ai_tactics.py
   与 ai_preview.py 之外的全部文件）不得 import 实验规则 AI 或模拟层
   （ai_tactics / ai_preview / sim.*）。
2. **无继承、无兜底**：AIPlayer 不继承 TacticalAI，也没有 tactical_combat 分支；
   LLM 提交失败/非法时不会静默改走规则 AI。
3. **缺失即证明**：在 engine.ai_tactics 与 engine.ai_preview 被屏蔽（import 直接
   失败）时，正式路径仍能导入、决策、并调用引擎内的闪避启发式。
4. **实验工具保留**：TacticalAI / sim 侧入口仍可用（它们只是被隔离，不是被删除）。
5. **运行期提示词不含过时知识**：`SYSTEM_PROMPT` 不得把"每回合固定 2 次"当成 AI 契约，
   必须按"动作槽位"描述、并且明确要求 AI 不要自己展开循环/预演引擎。
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine"

# 这两个文件**就是**实验规则 AI 本体：只允许它们互相 import，以及向下依赖生产。
EXPERIMENTAL_MODULES = {"ai_tactics.py", "ai_preview.py"}

FORBIDDEN_TOKENS = ("ai_tactics", "ai_preview")


def _import_targets(path: Path) -> set[str]:
    """AST 级提取 import 目标（含函数内惰性 import）；相对 import 记模块名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
    return out


def _production_engine_files() -> list[Path]:
    return sorted(p for p in ENGINE.rglob("*.py")
                  if p.name not in EXPERIMENTAL_MODULES and "__pycache__" not in p.parts)


def test_production_engine_does_not_import_experimental_ai_or_sim():
    """契约1：生产模块不得 import engine.ai_tactics / engine.ai_preview / sim.*。"""
    offenders: list[str] = []
    for path in _production_engine_files():
        for target in _import_targets(path):
            head = target.split(".")[0]
            if head == "sim":
                offenders.append(f"{path.relative_to(ROOT)} -> {target}")
            elif any(target.split(".")[-1] == token or target == token
                     for token in FORBIDDEN_TOKENS):
                offenders.append(f"{path.relative_to(ROOT)} -> {target}")
    assert not offenders, "生产模块反向依赖实验 AI/模拟层：\n" + "\n".join(offenders)


def test_engine_local_rules_module_has_no_experimental_dependency():
    """契约1（细粒度）：engine/ai_rules.py 是生产侧叶子模块。"""
    import engine.ai_rules as rules

    assert rules.__file__.endswith("engine/ai_rules.py")
    targets = _import_targets(Path(rules.__file__))
    assert not any(t.split(".")[0] == "sim" for t in targets)
    assert not any(t.split(".")[-1] in FORBIDDEN_TOKENS for t in targets)


def test_ai_player_does_not_inherit_tactical_ai():
    """契约2：无继承、无 tactical_combat 分支、无规则打分入口。"""
    from engine.ai_player import AIPlayer
    from engine.ai_tactics import TacticalAI

    assert not issubclass(AIPlayer, TacticalAI)
    assert not hasattr(AIPlayer, "tactical_combat")
    for removed in ("_is_tactical_combat_step", "_run_tactical_step"):
        assert not hasattr(AIPlayer, removed), f"AIPlayer 仍留有旧战术分支 {removed}"


def _combat_engine(tmp_path):
    from engine.api import GameEngine
    from tests.setup_support import begin_battle, begin_round, finish_initial_daowen

    engine = GameEngine(
        db_path=str(tmp_path / "isolation.db"),
        save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        rng_seed=4,
    )
    assert engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })["success"]
    assert finish_initial_daowen(engine)["success"]
    assert engine.execute_action("setup_choose_resonance",
                                 {"resonance_type": "反转"})["success"]
    assert engine.execute_action("setup_choose_region", {"region": "龙心谷"})["success"]
    assert begin_battle(engine)["success"]
    assert begin_round(engine)["success"]
    return engine


def test_llm_failure_does_not_fall_back_to_rule_ai(tmp_path, monkeypatch):
    """契约2：LLM 提交非法动作 → 结果是失败；规则 AI 不得被调用。"""
    from engine.ai_player import AIBackend, AIDecision, AIPlayer
    from engine.ai_tactics import TacticalAI

    engine = _combat_engine(tmp_path)
    engine.state.player.dao_wen.clear()

    def _forbidden(self, *a, **k):
        raise AssertionError("正式路径不得回退到 TacticalAI")

    monkeypatch.setattr(TacticalAI, "take_action", _forbidden)
    monkeypatch.setattr(TacticalAI, "take_turn", _forbidden)

    class _IllegalLLM(AIBackend):
        def decide(self, state, available_actions, context=""):
            return AIDecision("use_daowen", {"daowen_name": "并不存在的道纹", "x": 1},
                              "故意非法提交")

    ai = AIPlayer(engine, backend=_IllegalLLM())
    result = ai.play_turn("非法提交测试")
    assert result["result"]["success"] is False, result
    assert result["action"] == "use_daowen"
    assert len(ai.get_history()) == 1


def test_production_path_imports_and_decides_with_experimental_ai_blocked(tmp_path):
    """契约3：屏蔽 engine.ai_tactics / engine.ai_preview 后，正式路径仍可用。

    在子进程里安装 import 拦截器再导入引擎——若生产链路上还有任何一处
    import 实验 AI，这一步会直接 ImportError。
    """
    script = textwrap.dedent(
        """
        import importlib.abc
        import importlib.machinery
        import sys
        import tempfile
        from pathlib import Path

        BLOCKED = {"engine.ai_tactics", "engine.ai_preview"}

        class _Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname in BLOCKED:
                    raise ImportError("blocked by isolation test: " + fullname)
                return None

        sys.meta_path.insert(0, _Blocker())

        from engine.api import GameEngine
        from engine.ai_player import AIBackend, AIDecision, AIPlayer
        from engine.ai_rules import choose_dodge
        from tests.setup_support import begin_battle, begin_round, finish_initial_daowen

        root = Path(sys.argv[1])
        tmp = Path(tempfile.mkdtemp())
        engine = GameEngine(db_path=str(tmp / "iso.db"), save_dir=str(tmp / "saves"),
                            sealed_candidate_path=str(tmp / "sealed.json"), rng_seed=4)
        assert engine.execute_action("setup_attributes", {
            "name": "贾凡", "blood_points": 11, "speed_points": 8,
            "mana_points": 6})["success"]
        assert finish_initial_daowen(engine)["success"]
        assert engine.execute_action("setup_choose_resonance",
                                    {"resonance_type": "反转"})["success"]
        assert engine.execute_action("setup_choose_region", {"region": "龙心谷"})["success"]
        assert begin_battle(engine)["success"]
        assert begin_round(engine)["success"]

        class _LLM(AIBackend):
            def decide(self, state, available_actions, context=""):
                return AIDecision("focus", {}, "在屏蔽实验 AI 的环境里决策")

        ai = AIPlayer(engine, backend=_LLM())
        result = ai.play_turn("隔离验证")
        assert result["result"]["success"], result
        assert result["action"] == "focus"

        class _Target:
            is_alive = True
            current_speed = 5
            blood_limit = 40
            def has_status(self, name):
                return False

        assert choose_dodge(None, 30, entity=_Target()) is True
        for name in BLOCKED:
            assert name not in sys.modules, name
        print("ISOLATED_OK")
        """
    )
    proc = subprocess.run([sys.executable, "-c", script, str(ROOT)],
                          cwd=str(ROOT), capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ISOLATED_OK" in proc.stdout


def test_experimental_rule_ai_is_kept_but_separated():
    """契约4：实验工具保留——TacticalAI 与 choose_dodge 再导出仍可用。"""
    from engine.ai_tactics import TacticalAI, choose_dodge
    from engine.ai_rules import choose_dodge as production_choose_dodge
    from sim import optional_actions, monster_targets, ally_targets

    assert choose_dodge is production_choose_dodge, "闪避启发式应只有一份实现"
    assert callable(TacticalAI)
    assert callable(monster_targets.pick_wave_dodge_targets)
    assert callable(optional_actions.battle_start_relic_choices)
    assert callable(ally_targets.pick_ally_daowen_x)
    assert optional_actions.battle_start_relic_choices is \
        __import__("engine.ai_rules", fromlist=["x"]).battle_start_relic_choices


def test_active_prompt_does_not_hardcode_fixed_two_action_contract():
    """契约5：正式提示词按「动作槽位」描述，不把固定 2 次写成 AI 契约。"""
    from engine.ai_player import SYSTEM_PROMPT

    compact = SYSTEM_PROMPT.replace(" ", "")
    assert "每回合固定2次" not in compact, "提示词仍把固定 2 次当成 AI 契约"
    assert "固定2次" not in compact
    assert "动作槽位" in SYSTEM_PROMPT
    # 槽位数由引擎给出（基础 2 次 + 修正），不是提示词写死的常数
    assert "基础2次" in compact
    assert "槽位数量由引擎" in compact or "槽位数由引擎" in compact


def test_active_prompt_tells_llm_not_to_unroll_or_preplay():
    """契约5：提示词必须明确"LLM 提交、执行器执行期求值"，禁止 AI 自行展开。"""
    from engine.ai_player import SYSTEM_PROMPT

    assert "不要自己展开循环" in SYSTEM_PROMPT
    assert "预演" in SYSTEM_PROMPT and "不要替引擎" in SYSTEM_PROMPT
    # 生命周期三档必须在运行期契约里说明（AI 才知道 battle/permanent 的差别）
    for word in ("instant", "battle", "permanent"):
        assert word in SYSTEM_PROMPT


@pytest.mark.parametrize("name", sorted(os.listdir(ENGINE)))
def test_engine_dir_snapshot_is_importable_after_warmup(name):
    """冒烟：engine/ 下每个模块都能被 import（屏蔽测试之外的兜底）。"""
    if not name.endswith(".py") or name == "__init__.py":
        pytest.skip("非模块")
    module = "engine." + name[:-3]
    __import__(module)
