#!/usr/bin/env python3
"""故事文档「剧情存档」的自动整理：索引、条目调阅与角色知识视图。

剧情存档是全知真相层（E 叙述／D 台词，只追加不改写）；角色个人记忆是压缩层
（`角色记忆/<名>.md`，可能记错、可能遗漏）。本脚本只读 故事文档，输出三种视图，
不改任何来源文件，用来把「故事上下文」与「角色个人记忆」明确分开：

  python3 sim/story_context.py list               # 全部条目一行索引（编号/时间/地点/在场/摘要）
  python3 sim/story_context.py show E001 D002     # 指定条目全文
  python3 sim/story_context.py character 林晚      # 该角色可知条目全文（在场 ∪ 记忆档引用）

推演时按需取料：扮演某个角色的 AI 只拿本人的记忆档，加上本脚本为该角色生成的
「可知条目」原文；全局真相不整段外发。

--story / --memory-dir 可指向别的路径（测试用）；默认读取仓库根的故事文档与 角色记忆/。
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STORY = ROOT / "故事文档.md"
DEFAULT_MEMORY_DIR = ROOT / "角色记忆"

ARCHIVE_CHAPTER = "## 剧情存档"
ARCHIVE_BODY = "### 存档正文"
ENTRY_RE = re.compile(r"^####\s+([ED]\d{3,})\s*$")
MEMORY_REF_RE = re.compile(r"\[([ED]\d{3,})[·\s]")
SEP = "｜"


@dataclass
class Entry:
    """剧情存档的一条 E（白描）或 D（台词）条目。"""

    eid: str
    kind: str
    lines: list[str]
    fields: dict[str, str]

    @property
    def present(self) -> list[str]:
        raw = self.fields.get("在场", "")
        return [name.strip() for name in re.split(r"[、，,]", raw) if name.strip()]

    def summary(self, limit: int = 36) -> str:
        if self.kind == "D":
            spoken = sum(1 for line in self.lines if "：「" in line)
            return f"台词 {spoken} 句"
        first = re.split(r"[。！？]", self.fields.get("白描", ""))[0]
        return first[:limit] + ("…" if len(first) > limit else "")


def _parse_fields(lines: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in lines:
        text = line.lstrip("-").strip()
        for part in text.split(SEP):
            if "：" not in part:
                continue
            key, value = part.split("：", 1)
            key = key.strip()
            if key and len(key) <= 12:
                fields.setdefault(key, value.strip())
    return fields


def parse_entries(story_text: str) -> list[Entry]:
    """只解析「## 剧情存档 → ### 存档正文」内的条目；格式模板与其它章节不参与。"""
    entries: list[Entry] = []
    in_chapter = in_body = False
    current_id: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_id, current_lines
        if current_id is not None:
            entries.append(Entry(current_id, current_id[0], list(current_lines),
                                 _parse_fields(current_lines)))
        current_id, current_lines = None, []

    for line in story_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("### "):
            flush()
            in_body = stripped == ARCHIVE_BODY
            continue
        if stripped.startswith("## "):
            flush()
            in_chapter = stripped == ARCHIVE_CHAPTER
            in_body = False
            continue
        if not (in_chapter and in_body):
            continue
        match = ENTRY_RE.match(stripped)
        if match:
            flush()
            current_id = match.group(1)
            continue
        if current_id is not None:
            current_lines.append(line)
    flush()
    return entries


def render_list(entries: list[Entry]) -> str:
    if not entries:
        return "（剧情存档为空。）"
    out = ["编号｜时间｜地点｜在场｜摘要"]
    for entry in entries:
        out.append(SEP.join([
            entry.eid,
            entry.fields.get("时间", "—"),
            entry.fields.get("地点", "—"),
            "、".join(entry.present) or "—",
            entry.summary(),
        ]))
    return "\n".join(out)


def render_entry(entry: Entry) -> str:
    return "\n".join([f"#### {entry.eid}", *entry.lines]).rstrip()


def render_show(entries: list[Entry], ids: list[str]) -> tuple[str, list[str]]:
    by_id = {entry.eid: entry for entry in entries}
    missing = [eid for eid in ids if eid not in by_id]
    text = "\n\n".join(render_entry(by_id[eid]) for eid in ids if eid in by_id)
    return text, missing


def memory_refs(memory_dir: Path, name: str) -> list[str]:
    path = memory_dir / f"{name}.md"
    if not path.is_file():
        return []
    return MEMORY_REF_RE.findall(path.read_text(encoding="utf-8"))


def render_character(entries: list[Entry], memory_dir: Path, name: str) -> str:
    """角色知识视图＝在场条目 ∪ 记忆档引用条目的原文（真相层）。"""
    path = memory_dir / f"{name}.md"
    ref_ids = set(memory_refs(memory_dir, name))
    known = [entry for entry in entries
             if name in entry.present or entry.eid in ref_ids]
    header = [
        f"# 角色知识视图：{name}",
        "",
        "本视图是该角色可知条目的原文（真相层），推演时按需取料；",
        f"其个人记忆是压缩层（可能记错），见 角色记忆/{name}.md（{'已找到' if path.is_file() else '不存在'}）。",
        "在场条目与记忆引用并集；两者不一致时以本视图为准，并回头补记忆档。",
        "",
    ]
    if not known:
        header.append("（暂无：该角色不在场，记忆档也没有引用。）")
        return "\n".join(header)
    return "\n".join(header) + "\n" + "\n\n".join(render_entry(entry) for entry in known)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="故事文档剧情存档的自动整理视图")
    parser.add_argument("command", nargs="?", default="list",
                        choices=("list", "show", "character"))
    parser.add_argument("args", nargs="*", help="show 的编号们 / character 的角色名")
    parser.add_argument("--story", type=Path, default=DEFAULT_STORY)
    parser.add_argument("--memory-dir", type=Path, default=DEFAULT_MEMORY_DIR)
    ns = parser.parse_args(argv)

    if not ns.story.is_file():
        print(f"找不到故事文档：{ns.story}", file=sys.stderr)
        return 2
    entries = parse_entries(ns.story.read_text(encoding="utf-8"))

    if ns.command == "list":
        print(render_list(entries))
        return 0
    if ns.command == "show":
        if not ns.args:
            print("用法：story_context.py show <编号…>", file=sys.stderr)
            return 2
        text, missing = render_show(entries, [arg.upper() for arg in ns.args])
        if text:
            print(text)
        if missing:
            print("未找到条目：" + "、".join(missing), file=sys.stderr)
            return 1
        return 0
    if not ns.args:
        print("用法：story_context.py character <角色名>", file=sys.stderr)
        return 2
    print(render_character(entries, ns.memory_dir, ns.args[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
