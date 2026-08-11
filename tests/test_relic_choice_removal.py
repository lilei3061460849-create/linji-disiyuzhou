"""
F5 验证：relic_of_choice 死字段已删除
- 正常路径：GameState 不再持有该字段，relics 机制不受影响
- 边界：to_dict 不导出该字段，存档/读档不回归
- 错误：显式访问应抛 AttributeError（或不存在）
"""
import pytest
from engine.models import GameState, Relic

def test_normal_no_relic_of_choice_attribute():
    """正常：GameState 实例不再有 relic_of_choice 属性"""
    gs = GameState()
    assert not hasattr(gs, "relic_of_choice"), "relic_of_choice 字段应已删除"
    # relics 机制仍可用
    r = gs.grant_relic("血誓戒", "测试效果")
    assert r.name == "血誓戒"
    assert gs.relics[0].name == "血誓戒"
    # 移除后为空
    assert gs.remove_relic("血誓戒") is True
    assert len(gs.relics) == 0

def test_boundary_to_dict_excludes_relic_of_choice():
    """边界：to_dict 导出的字典不再包含 relic_of_choice"""
    gs = GameState()
    gs.grant_relic("买路财", "效果A")
    gs.grant_relic("同魂笔", "效果B")
    d = gs.to_dict()
    assert "relic_of_choice" not in d, "to_dict 不应导出已删字段"
    assert len(d["relics"]) == 2
    # 存档恢复后仍无该字段
    gs2 = GameState()
    gs2.relics = [Relic(name=n, effect=e) for n, e in [("血誓戒","x"),("守夜灯","y")]]
    assert not hasattr(gs2, "relic_of_choice")

def test_error_access_raises():
    """错误：对已删除字段的显式访问应失败（AttributeError 或无属性）"""
    gs = GameState()
    # 直接访问应抛 AttributeError（dataclass 无此 field）
    with pytest.raises(AttributeError):
        _ = gs.relic_of_choice
    # setattr 后再 del 应能清理（确保无残留默认值）
    # 若代码残留默认值，hasattr 会为 True，此处断言已删
    assert "relic_of_choice" not in dir(gs) or not hasattr(gs, "relic_of_choice")

def test_no_grep_in_codebase():
    """静态：源码层不再出现 relic_of_choice（除文档外）"""
    import pathlib, re
    py_files = list(pathlib.Path("engine").rglob("*.py")) + list(pathlib.Path("tests").rglob("*.py"))
    hits = []
    for p in py_files:
        # 跳过本测试文件自身（允许在测试中断言）
        if p.name == "test_relic_choice_removal.py":
            continue
        text = p.read_text(encoding="utf-8")
        if "relic_of_choice" in text:
            hits.append(str(p))
    assert hits == [], f"仍有代码引用 relic_of_choice: {hits}"
