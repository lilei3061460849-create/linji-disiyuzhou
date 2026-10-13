"""A25：模拟器不再把血契当作回始主动遗物，合法遗物与平分功能仍保留。"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.models import Relic

ROOT = Path(__file__).resolve().parents[1]
FILTERS = [
    ("sim/alt_win_paths_probe.py", "INTERACTIVE_RELICS"),
    ("sim/daowen_combo_tournament.py", "INTERACTIVE_RELICS"),
    ("sim/combo_loop_audit.py", "INTERACTIVE"),
    ("sim/exhaustive_combo_winrate.py", "INTERACTIVE_RELICS"),
]


def _literal_constant(path: str, name: str):
    """只读取声明，避免为了验证发现过滤器而启动长时间模拟。"""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path} 缺少常量 {name}")


@pytest.mark.parametrize("path,name", FILTERS)
def test_discovery_filter_does_not_skip_blood_pact_as_an_active_relic(path, name):
    interactive = _literal_constant(path, name)
    assert "血契" not in interactive
    assert "对手血契" not in interactive
    assert interactive == {"三相残韵盘", "回锋刀", "无所求"}
    choices = ["血契", "回锋刀"]
    assert next(candidate for candidate in choices if candidate not in interactive) == "血契"


def test_blood_pact_stays_in_the_valid_relic_pool():
    assert "血契" in _literal_constant("sim/combo_loop_audit.py", "RELIC_POOL")


@pytest.fixture
def handplay(tmp_path, monkeypatch):
    # 驱动在 import 时初始化 Trace；隔离输出，绝不写仓库里的历史实战数据。
    monkeypatch.setenv("HP_TRACE", str(tmp_path / "handplay.jsonl"))
    module = importlib.import_module("sim.handplay_20260821")
    logs = []
    monkeypatch.setattr(module, "TRACE", SimpleNamespace(text=logs.append))
    return module, logs


def _engine(relics):
    player = SimpleNamespace(current_hp=60, current_mana=1, mana_limit=10,
                             current_speed=4, speed_limit=4)
    state = SimpleNamespace(player=player, relics=[Relic(name, "") for name in relics],
                            sealed_relics={}, enemies=[], consumables=[])
    return SimpleNamespace(state=state)


def test_handplay_does_not_propose_blood_for_mana(handplay):
    module, logs = handplay
    engine = _engine(["血契"])
    before = (engine.state.player.current_hp, engine.state.player.current_mana)
    choices = module.round_start_choices(engine, module.Strategy())
    assert choices == {}
    assert not logs
    assert (engine.state.player.current_hp, engine.state.player.current_mana) == before


def test_handplay_preserves_other_round_start_relic_choices(handplay):
    module, logs = handplay
    engine = _engine(["血契", "回锋刀", "余火印"])
    engine.state.player.current_speed = 1
    engine.state.enemies = [SimpleNamespace(is_alive=False), SimpleNamespace(is_alive=True)]
    engine.state.consumables = [SimpleNamespace(kind="dragon_heart", name="流血龙心", current_uses=3)]
    choices = module.round_start_choices(engine, module.Strategy())
    assert choices == {
        "回锋刀": {"enemy_index": 1},
        "余火印": {"use": True, "x": 2, "heart_name": "流血龙心"},
    }
    assert len(logs) == 2 and all("血契" not in log for log in logs)


def test_handplay_still_respects_relic_sealing(handplay):
    module, logs = handplay
    engine = _engine(["血契", "回锋刀"])
    engine.state.player.current_speed = 1
    engine.state.enemies = [SimpleNamespace(is_alive=True)]
    engine.state.sealed_relics["回锋刀"] = 1
    assert module.round_start_choices(engine, module.Strategy()) == {}
    assert not logs
