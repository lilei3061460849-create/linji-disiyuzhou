"""AI 风险分类器回归测试（2026-08-19，非特判体系）。

覆盖：risk_classify 的 LETHAL / CRITICAL / HIGH / MEDIUM / LOW / SAFE 等级，
try_consumable 接入预演后的残骸崩解拒绝，_cast 风险记录。

**所有风险判断不依赖道纹/消耗品具体名称，全靠 diff 数值。**
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.ai_preview import ActionPreview
from engine.ai_tactics import TacticalAI
from engine.api import GameEngine
from engine.models import Consumable, Entity
from tests.setup_support import finish_initial_daowen


def _engine():
    e = GameEngine(db_path=os.path.join(tempfile.mkdtemp(prefix="risk"), "g.db"),
                   rng_seed=1, save_dir=tempfile.mkdtemp(prefix="risk2"))
    e.execute_action("setup_attributes", {"name": "T", "blood_points": 10,
                                          "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.energy = 0
    return e


def test_risk_classify_lethal_on_player_dead():
    """LETHAL：diff 玩家命零。"""
    diff = {"player_dead": True, "player": {"hp_after": 0}}
    level, reasons = ActionPreview.risk_classify(diff, None)
    assert level == "LETHAL"


def test_risk_classify_critical_on_mutation_threshold():
    """CRITICAL：异变离阈值 ≤5。"""
    e = _engine()
    p = e.state.player
    p.mutation_count = 46
    diff = {"player": {"mutation_delta": 3, "mutation_after": 49,
                       "hp_after": 60, "hp_before": 60},
            "player_dead": False}
    level, reasons = ActionPreview.risk_classify(diff, p)
    assert level == "CRITICAL", f"expected CRITICAL got {level}: {reasons}"
    assert any("异变" in r for r in reasons)


def test_risk_classify_critical_on_low_hp():
    """CRITICAL：HP 极低且动作继续扣血。"""
    e = _engine()
    p = e.state.player
    p.current_hp = 5
    p.blood_limit = 60
    diff = {"player": {"hp_after": 2, "hp_before": 5,
                       "mutation_delta": 0, "mutation_after": 0},
            "player_dead": False}
    level, reasons = ActionPreview.risk_classify(diff, p)
    assert level == "CRITICAL", f"expected CRITICAL got {level}: {reasons}"


def test_risk_classify_high_on_big_mutation():
    """HIGH：异变大幅增加（≥10）。"""
    e = _engine()
    p = e.state.player
    diff = {"player": {"mutation_delta": 10, "mutation_after": 10,
                       "hp_after": 60, "hp_before": 60},
            "player_dead": False}
    level, reasons = ActionPreview.risk_classify(diff, p)
    assert level == "HIGH", f"expected HIGH got {level}: {reasons}"


def test_risk_classify_high_on_mana_depletion():
    """HIGH：法力耗尽。"""
    e = _engine()
    p = e.state.player
    p.current_mana = 20
    diff = {"player": {"mana_after": 0, "mana_before": 20,
                       "hp_after": 60, "hp_before": 60,
                       "speed_after": 8, "speed_before": 8,
                       "mutation_delta": 0, "mutation_after": 0},
            "player_dead": False}
    level, reasons = ActionPreview.risk_classify(diff, p)
    assert level == "HIGH"


def test_risk_classify_low_on_safe_action():
    """LOW：无明显风险的动作。"""
    e = _engine()
    p = e.state.player
    p.current_hp = 50
    p.current_mana = 30
    p.mutation_count = 5
    diff = {"player": {"hp_after": 48, "hp_before": 50,
                       "mana_after": 25, "mana_before": 30,
                       "speed_after": 8, "speed_before": 8,
                       "mutation_delta": 0, "mutation_after": 5},
            "player_dead": False, "events": []}
    level, reasons = ActionPreview.risk_classify(diff, p)
    assert level in ("LOW", "MEDIUM"), f"expected LOW/MEDIUM got {level}"


def test_try_consumable_residual_rejected_by_risk():
    """残骸崩解对抗：try_consumable 经预演拒绝 LETHAL/CRITICAL 级消耗品。

    构造玩家异变 46 + 残骸（恢复20 + 异变10 → 56 ≥ 50 崩解）。
    不写残骸名称，全靠风险分类器。
    """
    from engine.models import StatusEffect
    e = _engine()
    p = e.state.player
    p.current_hp = 10
    p.blood_limit = 60
    p.mutation_count = 46
    e.state.consumables = [
        Consumable(name="残骸", effect="恢复20生命并获得异变10",
                   current_uses=1, max_uses=1)]
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    e.state.current_round = 2
    ai = TacticalAI(e)

    # try_consumable 应拒绝残骸（预演显示异变 46+10=56 → 崩解 → LETHAL）
    r = ai.try_consumable()
    assert r is None, "致死消耗品必须被安全过滤拒绝"
    assert any("残骸" in entry for entry in ai.preview_rejected), \
        "安全过滤应记录残骸被拒"
    # 玩家状态未改动
    assert p.mutation_count == 46, "残留消耗品使用次数不变"


def test_try_consumable_safe_item_allowed():
    """安全消耗品（残骸但异变低）仍可使用。"""
    from engine.models import StatusEffect
    e = _engine()
    p = e.state.player
    p.current_hp = 10
    p.blood_limit = 60
    p.mutation_count = 5  # 安全异变
    e.state.consumables = [
        Consumable(name="残骸", effect="恢复20生命并获得异变10",
                   current_uses=1, max_uses=1)]
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    e.state.current_round = 2
    ai = TacticalAI(e)

    r = ai.try_consumable()
    assert r is not None and r.get("success"), "安全消耗品应正常使用"
    assert p.current_hp > 10, "消耗品应恢复生命"