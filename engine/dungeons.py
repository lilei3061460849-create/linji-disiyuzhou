"""副本文档索引加载器：副本规则的唯一文档数据源。"""
from __future__ import annotations
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = ROOT / "副本索引.md"
ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*\d+\s*\|\s*\d+\s*\|\s*\d+\s*\|\s*\[查看副本\]\(([^)]+)\)\s*\|\s*$")


def load_dungeon_documents(index_path: str | Path = DEFAULT_INDEX) -> dict[str, str]:
    """读取索引表，返回 ``{副本名: 完整文档正文}``，并拒绝无效索引。"""
    index = Path(index_path)
    entries: list[tuple[str, Path]] = []
    for line in index.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line)
        if match:
            entries.append((match.group(1), index.parent / match.group(2)))
    if not entries:
        raise ValueError("副本索引未包含有效副本表格条目")
    names = [name for name, _ in entries]
    paths = [path.resolve() for _, path in entries]
    if len(names) != len(set(names)):
        raise ValueError("副本索引包含重复副本名称")
    if len(paths) != len(set(paths)):
        raise ValueError("副本索引包含重复副本文件链接")
    documents = {}
    for name, path in entries:
        if not path.is_file():
            raise ValueError(f"副本索引链接的文件不存在：{path}")
        text = path.read_text(encoding="utf-8")
        if not text.startswith(f"# {name}\n"):
            raise ValueError(f"副本文档标题与索引不一致：{path}")
        documents[name] = text
    return documents
