"""
pytest - 残韵闭环完整性（引擎 CLOSED_LOOPS 必须与规则正文声明一致）

背景：现行将原杀伐/切割两轨首尾接成一个14节点【杀伐闭环】；
规则正文声明的三条副本闭环与怪物原始道纹转化也必须完整登记，
导致对怪物面板道纹发动残韵必然失败。

覆盖：正常路径 / 边界条件 / 错误输入
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.daowen import ResonanceEngine as R

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rules_text() -> str:
    """规则正文事实源：规则正文.md#第四宇宙规则正文（见 AI_EXPERIENCE.md《文档分工与事实源》）。"""
    with open(os.path.join(ROOT, "规则正文.md"), encoding="utf-8") as f:
        return f.read()


def engine_edges() -> set:
    return {(s, t, d) for edges in R.CLOSED_LOOPS.values() for s, t, d in edges}


def engine_undirected_edges() -> set:
    """双向口径下的无向边：登记 (a,类型,b) 等价于 (b,类型,a)。"""
    return {tuple(sorted([(s, t), (d, t)])) for s, t, d in engine_edges()}


# ---------- 正常路径 ----------

def test_all_readme_monster_transforms_registered():
    """正常路径：规则正文声明的每一条怪物原始道纹转化都必须在引擎中登记。

    2026-10-07 用户令：转化途径改为双向，正文用 ⇄（历史写法 → 同样接受）。
    """
    txt = rules_text()
    spec = set()
    # 双向写法：全力X⇄（转换）借力X；历史单向写法：全力X→（转换）借力X
    for arrow in ("⇄", "→"):
        for m in re.finditer(r"(\w+?)X" + arrow + r"（(转换|反转|曲解)）(\w+?)X", txt):
            spec.add((m.group(1), m.group(2), m.group(3)))
        for m in re.finditer(
                r"(\w+?)X（代价[^）]*）" + arrow + r"（(转换|反转|曲解)）(\w+?)X", txt):
            spec.add((m.group(1), m.group(2), m.group(3)))
    assert spec, "未能从规则正文解析出怪物转化关系"
    missing = [e for e in spec
               if tuple(sorted([(e[0], e[1]), (e[2], e[1])])) not in engine_undirected_edges()]
    assert not missing, f"引擎缺失规则正文声明的转化：{sorted(missing)}"


def test_transform_paths_are_bidirectional():
    """正常路径：登记的每条边都双向可走（同一种残韵、同样消耗一次）。"""
    for s, t, d in engine_edges():
        assert R.find_transformation(s, t, d) == d, f"{s}--{t}-->{d} 正向不可达"
        assert R.find_transformation(d, t, s) == s, f"{d}--{t}-->{s} 反向不可达"
        forward = R.get_available_resonance(s)
        backward = R.get_available_resonance(d)
        assert any(p["target_daowen"] == d and p["resonance_type"] == t for p in forward)
        assert any(p["target_daowen"] == s and p["resonance_type"] == t for p in backward)


def test_monster_transform_branch_is_reversible():
    """正常路径：原始怪物道纹 ⇄ 转化道纹 可反向（如 借力→(转换)→全力）。"""
    assert R.find_transformation("借力", "转换") == "全力"
    assert R.find_transformation("弱化", "反转") == "全力"
    assert R.find_transformation("自食", "曲解") == "全力"
    assert R.find_transformation("寄生", "曲解") == "自愈"
    assert R.get_available_resonance("全力") == [
        {"resonance_type": "转换", "target_daowen": "借力", "direction": "正向", "loop": "怪物原始道纹"},
        {"resonance_type": "反转", "target_daowen": "弱化", "direction": "正向", "loop": "怪物原始道纹"},
        {"resonance_type": "曲解", "target_daowen": "自食", "direction": "正向", "loop": "怪物原始道纹"},
    ]


def test_single_fourteen_node_core_loop():
    """正常路径：杀伐闭环为唯一11节点核心闭环（2026-08-21：冲击改名波及；删除缓慢、慈悲、切割）。"""
    assert "杀伐闭环" in R.CLOSED_LOOPS
    assert "切割闭环" not in R.CLOSED_LOOPS
    edges = R.CLOSED_LOOPS["杀伐闭环"]
    assert len(edges) == 11
    assert sorted(source for source, _, _ in edges) == sorted(target for _, _, target in edges)
    assert R.find_transformation("血债", "转换") == "波及"
    assert R.find_transformation("波及", "反转") == "增殖"
    assert R.find_transformation("封印", "反转") == "杀伐"
    for removed in ("冲击", "慈悲", "切割", "缓慢"):
        assert not any(src == removed or tgt == removed for src, _, tgt in edges)


def test_three_region_loops_present():
    """正常路径：三条副本闭环必须存在"""
    # 2026-10-03：扭曲都市删【爆裂】、龙心谷删【活血】，这两条闭环 8 → 7 条边；罪孽都市不变。
    expected = {"扭曲都市闭环": 7, "罪孽都市闭环": 8, "龙心谷闭环": 7}
    for loop, n in expected.items():
        assert loop in R.CLOSED_LOOPS, f"缺少 {loop}"
        assert len(R.CLOSED_LOOPS[loop]) == n, f"{loop} 应有{n}条边"


def test_monster_daowen_now_transformable():
    """正常路径：怪物面板常见道纹必须有可用残韵路径"""
    # 2026-10-03：狂暴删除；飞行暂无残韵路径（原转换→滑翔、反转→坠落都随之删除，
    # 是否给飞行补新路径待用户裁定，见 报告.md）。
    for dw in ("必中", "自愈", "全力", "疯狂", "减速"):
        paths = R.get_available_resonance(dw)
        assert paths, f"{dw} 仍无残韵路径"


# ---------- 边界条件 ----------

def test_region_loops_are_closed():
    """边界：三条副本闭环必须真正合拢（每个节点入度=出度=1）"""
    for loop in ("扭曲都市闭环", "罪孽都市闭环", "龙心谷闭环"):
        edges = R.CLOSED_LOOPS[loop]
        srcs = [s for s, _, _ in edges]
        dsts = [d for _, _, d in edges]
        assert sorted(srcs) == sorted(dsts), f"{loop} 未合拢：{sorted(set(srcs) ^ set(dsts))}"


def test_no_duplicate_declared_edge():
    """边界：登记的**正向**边里，同一 (源道纹, 残韵类型) 仍不得重复指向两个结果"""
    seen = {}
    for s, t, d in engine_edges():
        key = (s, t)
        assert key not in seen or seen[key] == d, f"{s}+{t} 同时指向 {seen[key]} 与 {d}"
        seen[key] = d


def test_ambiguous_path_requires_explicit_target():
    """边界：双向后同一 (源道纹, 残韵类型) 可能通向两个相邻节点。

    此时引擎不得替发动者挑一个（禁止静默取第一项），必须由发动者显式指定；
    指定后两个方向都成立。已知歧义节点（闭环上两条相邻边同类型）：
    杀伐(反转) / 庇护(曲解) / 束缚(曲解) / 变形(转换) / 裂变(转换) /
    分裂(转换)。
    （2026-10-08 删【镇尸】：勾魂那一格改留空占位符后，镇尸(曲解) 不再是歧义节点。）
    """
    ambiguous = {(s, t) for s, t in
                 ((s, t) for s, t, _ in engine_edges())
                 if R.is_ambiguous_path(s, t)}
    assert ambiguous, "未检出任何歧义节点，本用例与闭环现状脱节"
    for s, t in sorted(ambiguous):
        candidates = [c["target_daowen"] for c in R.find_transformations(s, t)]
        assert len(candidates) == 2, f"{s}+{t} 歧义候选应为2个，实际{candidates}"
        # 不指定 → 拒绝，且不消耗残韵
        assert R.find_transformation(s, t) is None
        res = R.apply_resonance(s, t, True, True, resonance_stock={t: 1})
        assert res["success"] is False and res.get("ambiguous") is True, (s, t, res)
        # 显式指定 → 两个方向都成立
        for dst in candidates:
            assert R.find_transformation(s, t, dst) == dst
            ok = R.apply_resonance(s, t, True, True, resonance_stock={t: 1}, target_daowen=dst)
            assert ok["success"] is True and ok["target"] == dst, (s, t, dst, ok)
        # 指定到不存在的目标 → 拒绝
        bad = R.apply_resonance(s, t, True, True, resonance_stock={t: 1}, target_daowen="不存在的道纹")
        assert bad["success"] is False


def test_find_transformation_roundtrip():
    """边界：登记的每条边都应能被 find_transformation 双向查到"""
    for s, t, d in engine_edges():
        assert R.find_transformation(s, t, d) == d
        assert R.find_transformation(d, t, s) == s


# ---------- 错误输入 ----------

def test_unknown_daowen_has_no_path():
    """错误输入：不存在的道纹不得返回任何路径"""
    assert R.get_available_resonance("不存在的道纹") == []
    assert R.find_transformation("不存在的道纹", "反转") is None


def test_invalid_resonance_type_returns_none():
    """错误输入：非法残韵类型必须返回 None，不得静默命中"""
    assert R.find_transformation("杀伐", "乱写") is None
    assert R.find_transformation("杀伐", "") is None
