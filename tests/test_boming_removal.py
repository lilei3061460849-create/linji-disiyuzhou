"""【搏命】删除护栏（2026-10-14 用户令）。

删除理由：疲惫换法力的【搏命】并入【透支】（四通道：流血4X/衰老2X/枯竭X/
疲惫X 四选一 → X法力，`touzhi_cost` 指定，缺省流血），搏命道纹本身删除。

沿用【嫁祸】/【活血】/【尸爆】删除的先例（`tests/test_jiahuo_removal.py`）：
删干净 + 静态护栏，防止旧口径被下一次改动复活。

闭环补位沿用既有先例（删【嫁祸】→裂变直连背负）：**超频 —（反转）→ 坏死**
（保超频的出边类型），扭曲都市闭环仍合拢，边数 7 → 6。
"""
from __future__ import annotations

import pathlib

from engine.daowen import DaoWenEngine, ResonanceEngine

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 生产代码与 sim 工具都不许再出现【搏命】的运行态标识。
# 允许出现在「已删除」说明性注释里，故按标识符而非中文名校验。
FORBIDDEN = ("calculate_boming",)
SCAN = [
    "engine/daowen.py", "engine/combat.py", "engine/combat_hooks.py", "engine/api.py",
    "engine/models.py", "engine/battle_report.py", "engine/gamedata.py",
    "engine/combat_parts/daowen_effect.py", "engine/combat_parts/damage_death.py",
    "sim/balance_sim.py", "sim/audit_monsters.py", "sim/monster_targets.py",
]


def test_boming_is_not_registered():
    """正常路径：注册表里没有【搏命】，且扭曲都市专属名单同步收窄。"""
    DaoWenEngine.register_all()
    assert "搏命" not in DaoWenEngine._registry
    from engine.gamedata import REGION_EXCLUSIVE_DAOWEN

    assert "搏命" not in REGION_EXCLUSIVE_DAOWEN["扭曲都市"]
    assert REGION_EXCLUSIVE_DAOWEN["扭曲都市"] == {
        "变形", "定型", "畸变", "超频", "坏死", "退化"}


def test_twisted_loop_still_closes_with_six_edges():
    """边界：删【搏命】后由【超频】直连【坏死】，闭环仍合拢（入度=出度=1）。"""
    loop = ResonanceEngine.CLOSED_LOOPS["扭曲都市闭环"]
    assert ("超频", "反转", "坏死") in loop
    assert len(loop) == 6
    outs = {a for a, _, _ in loop}
    ins = {c for _, _, c in loop}
    assert outs == ins, "闭环节点入出必须一一对应"
    assert len(outs) == len(loop), "每个节点只能出现一次，否则不是单环"


def test_no_runtime_trace_left_in_production_or_sim():
    """静态：生产代码与 sim 工具不再含搏命的运行态标识（注释里的沿革说明不算）。"""
    for rel in SCAN:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for token in FORBIDDEN:
            assert token not in text, f"{rel} 仍含 {token}"


def test_jianghua_is_still_gone():
    """【僵化】此前已被【搏命】取代，删除搏命不得把它复活。"""
    DaoWenEngine.register_all()
    assert "僵化" not in DaoWenEngine.list_all()
    assert "搏命" not in DaoWenEngine.list_all()
