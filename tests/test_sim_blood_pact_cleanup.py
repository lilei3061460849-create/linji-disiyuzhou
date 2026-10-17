"""A25：血契不再作为回始主动遗物；合法遗物与平分功能仍保留。"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from engine.ai_rules import round_start_relic_choices
from engine.models import Relic

ROOT = Path(__file__).resolve().parents[1]
FILTERS = [
    ("sim/alt_win_paths_probe.py", "INTERACTIVE_RELICS"),
    ("sim/combo_loop_audit.py", "INTERACTIVE"),
]


def _literal_constant(path: str, name: str):
    """只读取声明，避免为了验证发现过滤器而启动长时间模拟。"""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path} 缺少常量 {name}")


def test_discovery_filters_do_not_skip_blood_pact_as_an_active_relic():
    for path, name in FILTERS:
        interactive = _literal_constant(path, name)
        assert "血契" not in interactive
        assert "对手血契" not in interactive
        assert interactive == {"回锋刀", "无所求"}  # 2026-10-13：【三相残韵盘】删除
        choices = ["血契", "回锋刀"]
        assert next(candidate for candidate in choices if candidate not in interactive) == "血契"


def test_blood_pact_stays_in_the_valid_relic_pool():
    assert "血契" in _literal_constant("sim/combo_loop_audit.py", "RELIC_POOL")


def _engine(relics, *, speed=4, speed_limit=4, alive_enemies=0, sealed=None):
    player = SimpleNamespace(current_hp=60, current_mana=1, mana_limit=10,
                             current_speed=speed, speed_limit=speed_limit)
    enemies = [SimpleNamespace(is_alive=True) for _ in range(alive_enemies)]
    state = SimpleNamespace(
        player=player,
        relics=[Relic(name, "") for name in relics],
        sealed_relics=sealed or {},
        enemies=enemies,
        consumables=[],
    )
    return SimpleNamespace(state=state)


def test_round_start_does_not_propose_blood_pact():
    engine = _engine(["血契"], speed=1, speed_limit=4, alive_enemies=1)
    assert round_start_relic_choices(engine) == {}


def test_round_start_preserves_huifeng_choice_and_ignores_blood_pact():
    engine = _engine(["血契", "回锋刀"], speed=1, speed_limit=4, alive_enemies=2)
    assert round_start_relic_choices(engine) == {"回锋刀": {"enemy_index": 0}}


def test_round_start_respects_relic_sealing():
    engine = _engine(["血契", "回锋刀"], speed=1, speed_limit=4,
                     alive_enemies=1, sealed={"回锋刀": 1})
    assert round_start_relic_choices(engine) == {}
