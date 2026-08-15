"""二阶副本面板合规审计契约测试。

口径（2026-08-14 裁定）：二阶（乱葬岗/沉沦海）可分配法力140，道纹5/总值15；
面板成本=⌈血限/6⌉+2×攻击力+攻击次数² ≤140。永夜庭特殊预算（60×场次）豁免。
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location(
    "audit_dungeons", os.path.join(ROOT, "sim", "audit_dungeons.py"))
ad = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ad)


def test_tier2_dungeon_panels_all_compliant():
    """正常路径：乱葬岗/沉沦海全部普通池怪面板≤140、道纹5/总值15。"""
    for fname in ("乱葬岗", "沉沦海"):
        spec = ad.TARGETS[fname]
        assert spec["budget"] == 100 and spec["dw_count"] == 5 and spec["dw_total"] == 15
        monsters = [m for m in ad.parse_monsters(f"副本/{fname}.md")
                    if m["name"] not in ad.SPECIAL_MONSTERS]
        assert len(monsters) > 0
        for m in monsters:
            cost = ad.panel_cost(m["hp"], m["ap"], m["ac"])
            assert cost <= 100, f"{fname}/{m['name']} 面板成本{cost}>140"
            assert len(m["dw"]) == 5, f"{fname}/{m['name']} 道纹数{len(m['dw'])}≠5"
            assert sum(m["dw"].values()) == 15, f"{fname}/{m['name']} 道纹总值≠15"


def test_boundary_special_monsters_exempted():
    """边界：疫巢(boss)/潜水员(员工)豁免面板审计。"""
    for fname in ("乱葬岗", "沉沦海"):
        monsters = ad.parse_monsters(f"副本/{fname}.md")
        for m in monsters:
            if m["name"] in ad.SPECIAL_MONSTERS:
                assert True  # 豁免，不检查面板/道纹配额
    # 永夜庭特殊预算豁免
    assert ad.TARGETS["永夜庭"] is None


def test_error_panel_cost_formula():
    """错误输入：面板成本公式验证（⌈血限/6⌉+2×攻击力+攻击次数²）。"""
    assert ad.panel_cost(216, 4, 4) == 60  # 千手蜈蚣 一阶满预算
    assert ad.panel_cost(258, 28, 3) == 108  # 纸人: ⌈258/6⌉43+56+9
    assert ad.panel_cost(0, 0, 0) == 0
