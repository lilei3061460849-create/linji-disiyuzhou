"""【自愈】已删除的墓碑护栏（2026-10-11 从 `test_ziyu_migration.py` 摘出）。

背景：【自愈】与【滋养】两条道纹已按用户裁定从道纹体系删除，引擎里不再有
`self_heal` 机制（只剩 `engine/battle_report.py` 的事件文案分支）。原
`tests/test_ziyu_migration.py` 是「round_start 内嵌 if → ROUND_START 相位
Mechanism」的迁移期验证文件，机制删除后 15 条里 12 条已无对象，整文件移除。

本文件只保留**实测有效**的两条：钉死「自愈没有以硬编码形式回到核心管线」，
以及「没有【自愈】状态时回始不会凭空回血」。
要恢复【自愈】必须先有新的用户裁定，届时这三条会立刻失败——这是有意的。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine  # noqa: E402
from engine.dice import DiceEngine  # noqa: E402
from engine.models import Entity, GameState  # noqa: E402
from engine.validator import check_migrated_mechanism_guards  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _arena(enemy_hp: int = 100, enemy_bl: int | None = None):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=50, speed_limit=10, current_speed=5)
    enemy = Entity("M", "怪物", blood_limit=enemy_bl if enemy_bl is not None else enemy_hp,
                   current_hp=enemy_hp)
    state.player = player
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def test_ziyu_hardcode_stays_out_of_the_combat_pipeline():
    """旧 if 不得回到核心管线（迁移护栏 + 源码扫描双重钉死）。"""
    assert check_migrated_mechanism_guards() == []
    source = (ROOT / "engine" / "combat.py").read_text(encoding="utf-8")
    assert 'has_status("自愈")' not in source, "combat.py 不得残留自愈硬编码分支"


def test_no_ziyu_status_means_no_free_heal():
    """没有【自愈】状态时，回始不得凭空回血。"""
    state, combat, player, _ = _arena()
    player.current_hp = 40
    from engine.mechanisms import Phase
    combat._dispatch_phase(Phase.ROUND_START, target=player)
    assert player.current_hp == 40
