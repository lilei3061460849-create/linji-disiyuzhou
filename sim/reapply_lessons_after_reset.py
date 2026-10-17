#!/usr/bin/env python3
"""重置构筑学习后恢复方法论条目，不回填旧版构筑成绩或策略数据。

旧游戏版本的 history、胜率、遥测和单次最优构筑不得随 reset 重新灌回。
提交版 data/build_knowledge.json 仅作为通用方法论条目的规范来源。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "data" / "build_knowledge.json"


def committed_lessons() -> list[dict]:
    try:
        raw = subprocess.check_output(
            ["git", "show", "HEAD:data/build_knowledge.json"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        data = json.loads(raw)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return []
    return [
        item for item in data.get("lessons", [])
        if item.get("kind") in {"心得", "陷阱"} and item.get("text")
    ]


def main() -> None:
    data = json.loads(KNOWLEDGE.read_text(encoding="utf-8"))
    lessons = []
    seen = set()
    for item in [*data.get("lessons", []), *committed_lessons()]:
        text = item.get("text", "").strip()
        if not text or text in seen or item.get("kind") not in {"心得", "陷阱"}:
            continue
        seen.add(text)
        lessons.append(item)
    data["lessons"] = lessons
    KNOWLEDGE.write_text(
        json.dumps(data, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    print(f"已恢复 {len(lessons)} 条通用方法论；未恢复任何旧版成绩、遥测或最优构筑。")


if __name__ == "__main__":
    main()
