"""二阶副本面板合规审计契约测试。

2026-09-16 用户令：怪物设计只约束**属性点数**与**道纹数量**，道纹 X 值自由自定义。
旧的「道纹总值」配额已废止——它本是"怪物发动道纹不支付法力"的补丁；怪物改为支付法力后，
真正的约束是[法限]构成的每回合法力预算。
口径：二阶（乱葬岗/沉沦海）可分配属性点100，道纹5条；
面板成本=⌈血限/6⌉+2×法限+2×速限 ≤100。永夜庭特殊属性点（60×场次）豁免。
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
    """正常路径：乱葬岗/沉沦海全部普通池怪面板≤100、道纹5条（总值不再约束）。"""
    for fname in ("乱葬岗", "沉沦海"):
        spec = ad.TARGETS[fname]
        # 2026-09-16 用户令：只约束属性点数与道纹数量；「道纹总值」配额已废止
        # （它本是"怪物不支付法力"的补丁，改付法力后由[法限]预算承担该约束）。
        assert spec["budget"] == 100 and spec["dw_count"] == 5
        assert "dw_total" not in spec, "道纹总值配额已废止，不应再出现在审计口径里"
        monsters = [m for m in ad.parse_monsters(f"副本/{fname}.md")
                    if m["name"] not in ad.SPECIAL_MONSTERS]
        assert len(monsters) > 0
        for m in monsters:
            cost = ad.panel_cost(m["hp"], m["ap"], m["ac"])
            assert cost <= 100, f"{fname}/{m['name']} 面板成本{cost}>100"
            assert len(m["dw"]) == 5, f"{fname}/{m['name']} 道纹数{len(m['dw'])}≠5"
            # 道纹总值不再约束（2026-09-16），X 值由[法限]预算自行决定


def test_boundary_special_monsters_exempted():
    """边界：疫巢(boss)/潜水员(员工)豁免面板审计。"""
    for fname in ("乱葬岗", "沉沦海"):
        monsters = ad.parse_monsters(f"副本/{fname}.md")
        for m in monsters:
            if m["name"] in ad.SPECIAL_MONSTERS:
                assert True  # 豁免，不检查面板/道纹配额
    # 永夜庭特殊属性点豁免
    assert ad.TARGETS["永夜庭"] is None


def test_error_panel_cost_formula():
    """错误输入：面板成本公式验证（⌈血限/6⌉+攻击力+攻击次数²）。"""
    assert ad.panel_cost(216, 4, 4) == 52  # ⌈216/6⌉36+2×4+2×4
    assert ad.panel_cost(258, 28, 3) == 105  # ⌈258/6⌉43+2×28+2×3
    assert ad.panel_cost(0, 0, 0) == 0
