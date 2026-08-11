"""
pytest - 战报格式化器（README《六、战斗推演格式》合规性）

对应三项裁定中的第②项：战报必须严格符合 README 第318-337行定义的格式。

覆盖：
- 正常路径：完整一场战斗，产出含全部规范字段的战报
- 边界条件：0个[朋友]/[员工]、无[战始]效果、闪避成功（判定完全失效）
- 错误输入/非法配置：引擎拒绝非法发动时，战报如实记录失败而不伪造数值
"""
import math
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine import battle_report as BR


def _new_engine(tmp_path, region="龙心谷"):
    e = GameEngine(db_path=str(tmp_path / "rep.db"))
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    e.execute_action("setup_choose_daowen", {"daowen": "杀伐"})
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": region})
    return e


# ---------- 正常路径 ----------

def test_battle_start_contains_all_required_fields(tmp_path):
    """正常路径：[战始]段必须逐项含规范要求的5个字段"""
    e = _new_engine(tmp_path)
    bs = e.execute_action("battle_start")
    lines = BR.format_battle_start(
        battle_no=1,
        draw_range="战斗场数1，一阶副本-3最低为1，抽取1只",
        draw_result="、".join(x.name for x in e.state.enemies),
        enemies=e.state.enemies,
        player=e.state.player,
        allies=[],
        background="熔岩隘口",
        start_effects=bs.get("relic_logs", []),
    )
    text = "\n".join(lines)
    for field in ["[战始]", "出怪：", "战斗背景：", "敌方面板：", "我方面板：", "[战始]效果结算："]:
        assert field in text, f"缺少规范字段 {field}"


def test_enemy_panel_matches_spec_shape(tmp_path):
    """正常路径：敌方面板格式 = 名称（攻击次数×攻击力/[血限]，道纹）"""
    e = _new_engine(tmp_path)
    e.execute_action("battle_start")
    m = e.state.enemies[0]
    panel = BR.enemy_panel(m)
    assert re.match(rf"^{re.escape(m.name)}（\d+×\d+/\d+，.+）$", panel), panel
    # 数值必须与引擎实体一致，不得杜撰
    assert f"{m.attack_count}×{m.attack_power}/{m.blood_limit}" in panel


def test_monster_hits_listed_one_per_line(tmp_path):
    """正常路径：怪物每一击单独成行，禁止合并结算"""
    e = _new_engine(tmp_path)
    e.execute_action("battle_start")
    e.execute_action("round_start", {})
    mp = e.execute_action("monster_phase", {})
    details = mp["result"]["details"]
    assert details, "monster_phase 必须上抛逐次出手明细"
    lines = BR.format_monster_hits(1, details)
    assert len(lines) == len(details), "出手行数必须等于实际出手次数"
    for i, ln in enumerate(lines, 1):
        assert ln.startswith(f"出手{i}（"), ln


def test_round_end_reports_shield_clear_and_duration(tmp_path):
    """正常路径：[回终]必须写明格挡清空与持续X-1，并给出回合末资源面板"""
    e = _new_engine(tmp_path)
    e.execute_action("battle_start")
    e.execute_action("round_start", {})
    re_ = e.execute_action("round_end", {})
    text = "\n".join(BR.format_round_end(re_["result"], e.state.player, e.state.enemies))
    assert "格挡清空" in text
    assert "持续X剩余回合-1" in text
    assert "回合末资源面板" in text


# ---------- 边界条件 ----------

def test_no_allies_and_no_start_effects(tmp_path):
    """边界：无[朋友]/[员工]、无[战始]效果时，字段仍须存在且明确写「无」"""
    e = _new_engine(tmp_path)
    e.execute_action("battle_start")
    lines = BR.format_battle_start(
        battle_no=1, draw_range="r", draw_result="x",
        enemies=e.state.enemies, player=e.state.player,
        allies=[], background="废墟据点", start_effects=[],
    )
    text = "\n".join(lines)
    assert "无[朋友]与[员工]" in text
    assert "[战始]效果结算：" in text and "无" in text


def test_dodge_success_is_written_explicitly(tmp_path):
    """边界：闪避成功时必须显式写明消耗1点速度且判定完全失效（铁律5）"""
    detail = [{
        "attacker": "石背熊", "target": "贾凡", "hit_index": 0,
        "dodge_attempted": True, "dodge_success": True,
        "damage_dealt": 0, "shield_absorbed": 0, "hp_lost": 0,
        "target_died": False, "speed_after_dodge": 7,
    }]
    line = BR.format_monster_hits(1, detail)[0]
    assert "消耗1点速度闪避" in line
    assert "判定与结算完全失效" in line
    assert "速度→7" in line


def test_empty_details_yields_no_lines():
    """边界：怪物无出手（空明细）时不产出任何出手行，不得凭空补写"""
    assert BR.format_monster_hits(1, []) == []
    assert BR.format_monster_hits(1, None) == []


# ---------- 错误输入 / 非法配置 ----------

def test_illegal_daowen_is_recorded_as_failure_not_fabricated(tmp_path):
    """错误输入：发动未持有的道纹必须被引擎拒绝，战报不得伪造伤害数值"""
    e = _new_engine(tmp_path)
    e.execute_action("battle_start")
    e.execute_action("round_start", {})
    target = e.state.enemies[0].name
    hp_before = e.state.enemies[0].current_hp
    res = e.execute_action("use_daowen", {"daowen_name": "裂变", "x": 1, "target": target})
    assert not res.get("success"), "未持有的道纹必须被拒绝"
    # 目标生命不得发生变化
    assert e.state.enemies[0].current_hp == hp_before
    # 格式化器对失败结果不得产出伤害行
    lines = BR.format_player_action(1, e.state.player.name, res)
    assert not any("原始伤害" in l for l in lines)


def test_formatter_never_invents_numbers_absent_from_engine():
    """非法配置：effects 中出现未知字段时，只做键值透传，不推算数值"""
    fake = {"success": True, "calculation": {}, "execution": {"effects": [{"type": "unknown_x", "foo": 1}]}}
    lines = BR.format_player_action(1, "贾凡", fake)
    text = "\n".join(lines)
    assert "foo=1" in text
    assert "伤害" not in text


# ---------- 端到端：产物合规性 ----------

def test_generated_report_is_spec_compliant(tmp_path):
    """
    正常路径（端到端）：sim/format_trace.py 的产物必须含 README§六 全部规范字段，
    且怪物出手逐条列出（不出现旧版"怪物出手N次"这类概括写法）。
    """
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sim"))
    import format_trace

    lines = format_trace.run(region="龙心谷", seed=4, battles=1)
    text = "\n".join(lines)

    for field in ["[战始]", "出怪：", "战斗背景：", "敌方面板：", "我方面板：",
                  "[战始]效果结算：", "[回始]：", "[回终]："]:
        assert field in text, f"产物缺少规范字段 {field}"

    # 禁止概括式写法（旧 engine_trace 的违规形态）
    assert not re.search(r"怪物出手\d+次", text), "出现被禁止的概括式结算"

    # 每一次出手都必须单独成行并带行动者
    hits = [l for l in lines if l.startswith("出手")]
    assert hits, "没有任何出手行"
    for h in hits:
        assert re.match(r"^出手\d+（.+?）：", h), h


def test_report_is_reproducible_with_same_seed():
    """边界：同一 seed 必须产出完全一致的战报（可复现，便于核查）"""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sim"))
    import format_trace

    a = format_trace.run(region="龙心谷", seed=4, battles=1)
    b = format_trace.run(region="龙心谷", seed=4, battles=1)
    assert a == b, "同一 seed 两次运行结果不一致，战报不可复现"
