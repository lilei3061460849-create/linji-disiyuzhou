"""Markdown 标题、嵌套链接和非法链接的自动校验。"""
from pathlib import Path
import pytest

from engine.document_validation import validate_markdown_documents

ROOT = Path(__file__).resolve().parents[1]


def test_all_repository_markdown_has_h1_and_resolving_links():
    """正常路径：全仓文档都有H1，全部本地链接和锚点可解析。"""
    result = validate_markdown_documents(ROOT)
    assert result["documents"] >= 21
    assert result["links"] >= 64


def test_nested_unicode_document_and_anchor_are_supported(tmp_path):
    """边界：嵌套目录、中文文件名和中文锚点均可解析。"""
    nested = tmp_path / "副本"
    nested.mkdir()
    (tmp_path / "README.md").write_text(
        "# 入口\n\n[条目](副本/草案.md#中文条目)\n", encoding="utf-8")
    (nested / "草案.md").write_text("# 草案\n\n## 中文条目\n\n正文\n", encoding="utf-8")
    result = validate_markdown_documents(tmp_path)
    assert result == {"documents": 2, "links": 1}


def test_missing_h1_file_and_anchor_are_rejected(tmp_path):
    """错误输入：无H1、缺文件和缺锚点必须被校验器拒绝。"""
    (tmp_path / "README.md").write_text(
        "没有标题\n\n[缺文件](missing.md)\n[缺锚点](target.md#不存在)\n",
        encoding="utf-8")
    (tmp_path / "target.md").write_text("# 目标\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        validate_markdown_documents(tmp_path)
    message = str(exc.value)
    assert "缺少一级标题" in message
    assert "链接文件不存在" in message
    assert "链接锚点不存在" in message
