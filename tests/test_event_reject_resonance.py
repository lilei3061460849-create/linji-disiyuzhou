"""事件拒绝选项 → 随机残韵（DM 裁定 2026-09-10，用户：「用其他道纹解决」）。

口径：真拒绝（_is_reject_option_text：含 REJECT_OPTION_KEYWORDS 或「拒绝：」前缀）
且结算成功 → 引擎 dice 随机授予 转换/反转/曲解 之一入 State.resonance。
带代价的「拒绝改造」类选项不算拒绝。
"""
import tempfile

import pytest

from engine.api import GameEngine
from tests.setup_support import finish_initial_daowen


def _engine(seed=5):
    e = GameEngine(db_path=tempfile.mktemp(suffix=".db"), rng_seed=seed)
    e.execute_action("setup_attributes", {
        "name": "甲", "blood_points": 7, "speed_points": 8, "mana_points": 10})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    return e


def _stage_event(e, options):
    e.event_pool.events["测试事件"] = {
        "name": "测试事件", "region": "扭曲都市", "desc": "测试",
        "options": [{"id": i + 1, "text": t} for i, t in enumerate(options)],
    }
    e.event_pool.current = "测试事件"


def test_reject_grants_random_resonance():
    e = _engine()
    before = dict(e.state.resonance)
    _stage_event(e, ["拒绝：什么都不要"])
    r = e.execute_action("resolve_event", {"event": "测试事件", "option_id": 1})
    assert r.get("success")
    after = e.state.resonance
    gained = {k: after.get(k, 0) - before.get(k, 0) for k in ("转换", "反转", "曲解")}
    assert sum(gained.values()) == 1, f"应恰获得一种残韵，实际 {gained}"
    assert any("拒绝奖励" in a for a in r["result"]["applied"])


def test_reject_grant_deterministic_per_seed():
    """同 seed 同选项 → 同一种残韵（随机走引擎 dice，可复现）。"""
    kinds = set()
    for _ in range(2):
        e = _engine(seed=77)
        _stage_event(e, ["拒绝：什么都不要"])
        r = e.execute_action("resolve_event", {"event": "测试事件", "option_id": 1})
        kinds.add(next(a for a in r["result"]["applied"] if "拒绝奖励" in a))
    assert len(kinds) == 1, f"同 seed 应抽到同一种：{kinds}"


def test_costly_decline_is_not_reject():
    """「拒绝改造」等带代价选项不算拒绝 → 不授予残韵。"""
    e = _engine()
    before = dict(e.state.resonance)
    _stage_event(e, ["拒绝改造自己：血限+6，失去1精力"])
    r = e.execute_action("resolve_event", {"event": "测试事件", "option_id": 1})
    assert r.get("success")
    after = e.state.resonance
    gained = sum(after.get(k, 0) - before.get(k, 0) for k in ("转换", "反转", "曲解"))
    assert gained == 0, "带代价选项不得触发拒绝奖励"
    assert not any("拒绝奖励" in a for a in r["result"]["applied"])
