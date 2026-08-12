"""项目 Markdown 结构、文件链接与标题锚点校验。"""
from __future__ import annotations
from pathlib import Path
import re
from urllib.parse import unquote

LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")


def github_anchor(heading: str) -> str:
    """生成项目当前标题所需的 GitHub 风格锚点。"""
    heading = re.sub(r"<[^>]+>", "", heading)
    heading = re.sub(r"[`*_~]", "", heading).strip().lower()
    heading = "".join(ch for ch in heading if ch.isalnum() or ch in " -_")
    return re.sub(r"\s+", "-", heading)


def _document_anchors(path: Path) -> set[str]:
    anchors = set()
    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = HEADING.match(line)
        if not match:
            continue
        base = github_anchor(match.group(2))
        suffix = counts.get(base, 0)
        counts[base] = suffix + 1
        anchors.add(base if suffix == 0 else f"{base}-{suffix}")
    return anchors


def validate_markdown_documents(root: str | Path) -> dict:
    """校验全部 Markdown；发现无H1、失效文件链接或失效锚点时抛出 ValueError。"""
    root = Path(root).resolve()
    ignored_parts = {".git", ".venv", ".pytest_cache", "__pycache__", "node_modules"}
    documents = sorted(
        path for path in root.rglob("*.md")
        if not ignored_parts.intersection(path.parts)
    )
    anchors = {path.resolve(): _document_anchors(path) for path in documents}
    errors = []
    link_count = 0

    for path in documents:
        text = path.read_text(encoding="utf-8")
        first_nonempty = next((line for line in text.splitlines() if line.strip()), "")
        if not first_nonempty.startswith("# "):
            errors.append(f"{path.relative_to(root)}: 缺少一级标题")

        in_fence = False
        for line_number, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for match in LINK.finditer(line):
                target = match.group(2).strip()
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                link_count += 1
                file_part, separator, fragment = target.partition("#")
                destination = (path.parent / unquote(file_part)).resolve() if file_part else path.resolve()
                if not destination.is_file():
                    errors.append(
                        f"{path.relative_to(root)}:{line_number}: 链接文件不存在：{target}")
                    continue
                if separator and destination.suffix.lower() == ".md":
                    wanted = github_anchor(unquote(fragment))
                    if wanted not in anchors.get(destination, set()):
                        errors.append(
                            f"{path.relative_to(root)}:{line_number}: 链接锚点不存在：{target}")

    if errors:
        raise ValueError("Markdown文档校验失败：\n" + "\n".join(errors))
    return {"documents": len(documents), "links": link_count}
