"""副本文档清单加载器。

``副本索引.md`` 同时登记已实现副本和规则草案；运行时只加载已实现副本，
规则草案仅参与文档完整性校验。
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = ROOT / "副本索引.md"

# 已实现副本保留数值元数据，供现有运行时和面板审计使用。
IMPLEMENTED_ROW = re.compile(
    r"^\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|"
    r"\s*\[查看副本\]\(([^)]+)\)\s*\|\s*$"
)
# 草案的预算和机制尚未定稿，只登记阶级、状态和文档位置。
DRAFT_ROW = re.compile(
    r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(未实现)\s*\|"
    r"\s*\[查看草案\]\(([^)]+)\)\s*\|\s*$"
)
# 向后兼容原调用方引用的正则名称。
ROW = IMPLEMENTED_ROW


@dataclass(frozen=True)
class DungeonDocument:
    """索引中的一个副本文档条目。"""

    name: str
    tier: str
    status: str
    path: Path
    mana_budget: int | None = None
    daowen_count: int | None = None
    total_value: int | None = None


def load_dungeon_manifest(index_path: str | Path = DEFAULT_INDEX) -> list[DungeonDocument]:
    """读取并校验全部副本文档，包括未实现草案。"""
    index = Path(index_path)
    entries: list[DungeonDocument] = []

    for line in index.read_text(encoding="utf-8").splitlines():
        implemented = IMPLEMENTED_ROW.match(line)
        if implemented:
            name, budget, count, total, target = implemented.groups()
            entries.append(DungeonDocument(
                name=name,
                tier="一阶",
                status="已实现",
                path=index.parent / target,
                mana_budget=int(budget),
                daowen_count=int(count),
                total_value=int(total),
            ))
            continue

        draft = DRAFT_ROW.match(line)
        if draft:
            name, tier, status, target = draft.groups()
            entries.append(DungeonDocument(
                name=name,
                tier=tier,
                status=status,
                path=index.parent / target,
            ))

    if not entries:
        raise ValueError("副本索引未包含有效副本表格条目")

    names = [entry.name for entry in entries]
    paths = [entry.path.resolve() for entry in entries]
    if len(names) != len(set(names)):
        raise ValueError("副本索引包含重复副本名称")
    if len(paths) != len(set(paths)):
        raise ValueError("副本索引包含重复副本文件链接")

    for entry in entries:
        if not entry.path.is_file():
            raise ValueError(f"副本索引链接的文件不存在：{entry.path}")
        text = entry.path.read_text(encoding="utf-8")
        if not text.startswith(f"# {entry.name}\n"):
            raise ValueError(f"副本文档标题与索引不一致：{entry.path}")

    return entries


def load_dungeon_documents(index_path: str | Path = DEFAULT_INDEX) -> dict[str, str]:
    """返回运行时可用的 ``{副本名: 正文}``；未实现草案不会进入运行时。"""
    entries = [entry for entry in load_dungeon_manifest(index_path) if entry.status == "已实现"]
    if not entries:
        raise ValueError("副本索引未包含已实现副本")
    return {entry.name: entry.path.read_text(encoding="utf-8") for entry in entries}
