"""全副本索引、独立副本文档与运行时加载器的回归测试。"""
from pathlib import Path
import re
import pytest

from engine.dungeons import load_dungeon_documents
from engine.events import parse_events
from engine.monsters import parse_monster_pool

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "副本索引.md"
README = ROOT / "README.md"
ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*\d+\s*\|\s*\d+\s*\|\s*\d+\s*\|\s*\[查看副本\]\(([^)]+)\)\s*\|\s*$")
LINK = re.compile(r"\[[^]]+\]\(([^)#]+)(?:#[^)]+)?\)")


def test_index_normal_path_loads_three_independent_dungeons():
    """正常路径：索引加载三个副本，且每个副本均有独立标题与12只怪物。"""
    documents = load_dungeon_documents()
    assert list(documents) == ["扭曲都市", "罪孽都市", "龙心谷"]
    pools = parse_monster_pool(INDEX)
    assert {name: len(pool) for name, pool in pools.items()} == {
        "扭曲都市": 12, "罪孽都市": 12, "龙心谷": 12,
    }
    events = parse_events(INDEX)
    assert events["医生"]["region"] == "扭曲都市"
    assert events["追求者"]["region"] == "龙心谷"


def test_nested_dungeon_links_and_readme_entry_resolve():
    """边界：嵌套文档可解析根目录物品索引，README 仅保留全副本入口。"""
    assert "[全副本索引](副本索引.md)" in README.read_text(encoding="utf-8")
    assert "扭曲都市（一阶副本" not in README.read_text(encoding="utf-8")
    for relative in ["扭曲都市.md", "罪孽都市.md", "龙心谷.md"]:
        document_path = ROOT / "副本" / relative
        for target in LINK.findall(document_path.read_text(encoding="utf-8")):
            assert (document_path.parent / target).is_file(), f"副本文档存在失效链接：{target}"


def test_invalid_index_configuration_is_rejected(tmp_path):
    """错误输入：重复名称的索引配置必须被加载器拒绝。"""
    invalid = tmp_path / "副本索引.md"
    invalid.write_text(
        "| 重复副本 | 60 | 3 | 8 | [查看副本](a.md) |\n"
        "| 重复副本 | 60 | 3 | 8 | [查看副本](b.md) |\n", encoding="utf-8")
    with pytest.raises(ValueError, match="重复副本名称"):
        load_dungeon_documents(invalid)
