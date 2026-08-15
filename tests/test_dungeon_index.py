"""全副本索引、草案隔离、独立副本文档与运行时加载器的回归测试。"""
from pathlib import Path
import re
import pytest

from engine.dungeons import load_dungeon_documents, load_dungeon_manifest
from engine.events import parse_events
from engine.monsters import parse_monster_pool

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "副本索引.md"
README = ROOT / "README.md"
LINK = re.compile(r"\[[^]]+\]\(([^)#]+)(?:#[^)]+)?\)")

IMPLEMENTED = ["扭曲都市", "罪孽都市", "龙心谷", "乱葬岗"]
DRAFTS = ["永夜庭", "沉沦海", "荒疫古城", "巴别塔"]


def test_index_normal_path_loads_only_four_implemented_dungeons():
    """正常路径：清单登记8个文档，运行时只加载4个已实现副本（含乱葬岗）。"""
    manifest = load_dungeon_manifest()
    assert [entry.name for entry in manifest] == IMPLEMENTED + DRAFTS
    assert {entry.name: entry.status for entry in manifest} == {
        **{name: "已实现" for name in IMPLEMENTED},
        **{name: "未实现" for name in DRAFTS},
    }

    documents = load_dungeon_documents()
    assert list(documents) == IMPLEMENTED
    pools = parse_monster_pool(INDEX)
    assert {name: len(pool) for name, pool in pools.items()} == {
        "扭曲都市": 12, "罪孽都市": 12, "龙心谷": 12, "乱葬岗": 12,
    }
    events = parse_events(INDEX)
    assert events["医生"]["region"] == "扭曲都市"
    assert events["追求者"]["region"] == "龙心谷"


def test_draft_documents_are_indexed_but_excluded_from_runtime():
    """边界：草案可被导航和校验，但不得进入现行事件池或怪物池。"""
    manifest = load_dungeon_manifest()
    draft_entries = [entry for entry in manifest if entry.status == "未实现"]
    assert [entry.name for entry in draft_entries] == DRAFTS
    assert all(entry.mana_budget is None for entry in draft_entries)
    assert set(load_dungeon_documents()).isdisjoint(DRAFTS)
    assert set(parse_monster_pool(INDEX)).isdisjoint(DRAFTS)


def test_all_dungeon_documents_have_index_entries_and_resolving_links():
    """边界：副本目录无孤儿文档，全部使用标准H1，且嵌套链接目标存在。"""
    assert "[全副本索引](副本索引.md)" in README.read_text(encoding="utf-8")
    assert "扭曲都市（一阶副本" not in README.read_text(encoding="utf-8")

    manifest = load_dungeon_manifest()
    indexed = {entry.path.resolve() for entry in manifest}
    actual = {path.resolve() for path in (ROOT / "副本").glob("*.md")}
    assert indexed == actual

    for entry in manifest:
        text = entry.path.read_text(encoding="utf-8")
        assert text.startswith(f"# {entry.name}\n")
        for target in LINK.findall(text):
            assert (entry.path.parent / target).is_file(), f"副本文档存在失效链接：{target}"


def test_invalid_duplicate_index_configuration_is_rejected(tmp_path):
    """错误输入：跨已实现/草案表的重复名称必须被拒绝。"""
    implemented = tmp_path / "a.md"
    draft = tmp_path / "b.md"
    implemented.write_text("# 重复副本\n", encoding="utf-8")
    draft.write_text("# 重复副本\n", encoding="utf-8")
    invalid = tmp_path / "副本索引.md"
    invalid.write_text(
        "| 重复副本 | 60 | 3 | 8 | [查看副本](a.md) |\n"
        "| 重复副本 | 二阶 | 未实现 | [查看草案](b.md) |\n", encoding="utf-8")
    with pytest.raises(ValueError, match="重复副本名称"):
        load_dungeon_manifest(invalid)


def test_invalid_draft_title_is_rejected(tmp_path):
    """错误输入：草案标题与索引名称不一致时必须拒绝。"""
    draft = tmp_path / "draft.md"
    draft.write_text("没有标准标题\n", encoding="utf-8")
    invalid = tmp_path / "副本索引.md"
    invalid.write_text(
        "| 草案副本 | 二阶 | 未实现 | [查看草案](draft.md) |\n", encoding="utf-8")
    with pytest.raises(ValueError, match="标题与索引不一致"):
        load_dungeon_manifest(invalid)
