"""
pytest - 残韵闭环完整性（引擎 CLOSED_LOOPS 必须与 README 声明一致）

背景：现行将原杀伐/锐利两轨首尾接成一个14节点【杀伐闭环】；
README 声明的三条副本闭环与怪物原始道纹转化也必须完整登记，
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


def readme() -> str:
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
        return f.read()


def engine_edges() -> set:
    return {(s, t, d) for edges in R.CLOSED_LOOPS.values() for s, t, d in edges}


# ---------- 正常路径 ----------

def test_all_readme_monster_transforms_registered():
    """正常路径：README 声明的每一条怪物原始道纹转化都必须在引擎中登记"""
    txt = readme()
    spec = set()
    for m in re.finditer(r"(\w+?)X→（(转换|反转|曲解)）(\w+?)X", txt):
        spec.add((m.group(1), m.group(2), m.group(3)))
    for m in re.finditer(r"(\w+?)X（代价[^）]*）→（(转换|反转|曲解)）(\w+?)X", txt):
        spec.add((m.group(1), m.group(2), m.group(3)))
    assert spec, "未能从 README 解析出怪物转化关系"
    missing = spec - engine_edges()
    assert not missing, f"引擎缺失 README 声明的转化：{sorted(missing)}"


def test_single_fourteen_node_core_loop():
    """正常路径：杀伐与原锐利闭环必须首尾接成唯一14节点核心闭环。"""
    assert "杀伐闭环" in R.CLOSED_LOOPS
    assert "锐利闭环" not in R.CLOSED_LOOPS
    edges = R.CLOSED_LOOPS["杀伐闭环"]
    assert len(edges) == 14
    assert sorted(source for source, _, _ in edges) == sorted(target for _, _, target in edges)
    assert R.find_transformation("慈悲", "反转") == "锐利"
    assert R.find_transformation("封印", "反转") == "杀伐"


def test_three_region_loops_present():
    """正常路径：三条副本闭环必须存在"""
    for loop in ("扭曲都市闭环", "罪孽都市闭环", "龙心谷闭环"):
        assert loop in R.CLOSED_LOOPS, f"缺少 {loop}"
        assert len(R.CLOSED_LOOPS[loop]) == 8, f"{loop} 应有8条边"


def test_monster_daowen_now_transformable():
    """正常路径：怪物面板常见道纹必须有可用残韵路径"""
    for dw in ("必中", "狂暴", "飞行", "自愈", "强化", "疯狂", "减速"):
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


def test_no_duplicate_source_and_type():
    """边界：同一 (源道纹, 残韵类型) 不得指向两个不同结果（否则结算有歧义）"""
    seen = {}
    for s, t, d in engine_edges():
        key = (s, t)
        assert key not in seen or seen[key] == d, f"{s}+{t} 同时指向 {seen[key]} 与 {d}"
        seen[key] = d


def test_find_transformation_roundtrip():
    """边界：登记的每条边都应能被 find_transformation 查到"""
    for s, t, d in engine_edges():
        assert R.find_transformation(s, t) == d


# ---------- 错误输入 ----------

def test_unknown_daowen_has_no_path():
    """错误输入：不存在的道纹不得返回任何路径"""
    assert R.get_available_resonance("不存在的道纹") == []
    assert R.find_transformation("不存在的道纹", "反转") is None


def test_invalid_resonance_type_returns_none():
    """错误输入：非法残韵类型必须返回 None，不得静默命中"""
    assert R.find_transformation("杀伐", "乱写") is None
    assert R.find_transformation("杀伐", "") is None
