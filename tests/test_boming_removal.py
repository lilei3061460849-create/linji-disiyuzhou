"""【搏命】删除护栏（2026-10-14 用户令）。

删除理由：疲惫换法力的【搏命】并入【透支】（四通道：流血4X/衰老2X/枯竭X/
疲惫X 四选一 → X法力，`touzhi_cost` 指定，缺省流血），搏命道纹本身删除。

沿用【嫁祸】/【活血】/【尸爆】删除的先例（`tests/test_jiahuo_removal.py`）：
删干净 + 静态护栏，防止旧口径被下一次改动复活。

闭环补位沿用 2026-10-08 删【镇尸】的先例（用户 2026-10-14 拍板占位，
否决直连方案）：**超频 —（反转）→（待补）—（转换）→ 坏死**，
扭曲都市闭环仍为 7 条边。残韵走到占位符=失败（不耗残韵、不改牌面、
不授予），见 `test_gouhun_migration.py::test_gouhun_qujie_lands_on_reserved_slot_and_is_refused`。
"""
from __future__ import annotations

import pathlib

from engine.daowen import DaoWenEngine, ResonanceEngine, RESERVED_SLOT

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


def test_twisted_loop_keeps_seven_edges_with_reserved_slot():
    """边界：删【搏命】后该格留成空占位符，闭环仍 7 条边且合拢。"""
    loop = ResonanceEngine.CLOSED_LOOPS["扭曲都市闭环"]
    assert ("超频", "反转", RESERVED_SLOT) in loop
    assert (RESERVED_SLOT, "转换", "坏死") in loop
    assert len(loop) == 7
    outs = {a for a, _, _ in loop}
    ins = {c for _, _, c in loop}
    assert outs == ins, "闭环节点入出必须一一对应"
    assert len(outs) == len(loop), "每个节点只能出现一次，否则不是单环"
    DaoWenEngine.register_all()
    assert RESERVED_SLOT not in DaoWenEngine._registry, "占位符不是道纹"
    from engine.gamedata import REGION_EXCLUSIVE_DAOWEN

    assert RESERVED_SLOT not in REGION_EXCLUSIVE_DAOWEN["扭曲都市"]


def test_chaopin_fanzhuan_to_slot_is_refused():
    """边界：【超频】的【反转】通向空占位符=失败，不耗残韵、不改牌面。"""
    import os
    from engine.api import GameEngine
    from engine.models import Entity, DaoWen, DaoWenInstance
    from tests.setup_support import finish_initial_daowen

    db = f"/tmp/linji_tests/test_boming_slot_{os.getpid()}.db"
    e = GameEngine(db_path=db, rng_seed=7,
                   sealed_candidate_path=f"/tmp/linji_tests/test_boming_slot_s_{os.getpid()}.json")
    e.execute_action("setup_attributes", {"name": "白某", "blood_points": 11,
                                          "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.state.phase = "in_combat"
    e.state.current_round = 2
    p = e.state.player
    m = Entity(name="人头气球", entity_type="怪物", blood_limit=200, current_hp=200,
               attack_count=1, attack_power=12)
    m.dao_wen["超频"] = DaoWenInstance(
        DaoWen(name="超频", formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""), x_value=4)
    e.state.enemies.append(m)

    e.state.resonance = {"反转": 1}
    r = e.execute_action("use_resonance", {"source_daowen": "超频",
                                           "resonance_type": "反转",
                                           "target_ref": "enemy:0"})
    assert r.get("success") is False, f"通向空占位符的残韵应失败，实：{r}"
    assert e.state.resonance.get("反转") == 1, "失败不应消耗残韵"
    assert "超频" in m.dao_wen, "怪物牌面不应被改写"
    assert RESERVED_SLOT not in m.dao_wen, "空占位符不得进入任何人的道纹列表"
    assert RESERVED_SLOT not in p.dao_wen, "施法者也不得获得空占位符"


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
