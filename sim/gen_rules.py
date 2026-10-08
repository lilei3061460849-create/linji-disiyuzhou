#!/usr/bin/env python3
"""从结构化事实源（data/rules/*.toml）生成《死者之书.md》的「## 规则」节。

样板批（2026-10-08）：只生成「致死类特殊事件」一章。格式定下来后再逐章扩。

## 为什么规则署名 ？？？

遗言署真人姓名，可能有真假；规则不会——所有人看到的规则都一样。所以代表规则
的页面一律署名 `？？？`（条目形如 `### ？？？·特殊事件·迷失`），与署人名的个人
遗言区分开。

## 为什么单独一节、不写进「## 遗言」

`engine/death_book.py` 的遗言有**单句 20 字上限**（DEFAULT_CAPACITY=20），
规则条文塞不进去（光「封印X：代价：异变X，使一个[目标]延后X回合再入场」就 20+ 字）。
规则因此进并列的「## 规则」节，不受 20 字约束。death_book 只读写「## 遗言」节，
新增本节不会影响运行时。

## 用法

    python3 sim/gen_rules.py          # 写回 死者之书.md
    python3 sim/gen_rules.py --check  # 只比对，不写（供一致性测试用）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.rules_source import lethal_events  # noqa: E402

BOOK = ROOT / "死者之书.md"
SECTION_HEADER = "## 规则"
SIGNATURE = lethal_events.signature
CHAPTER = lethal_events.chapter


def render_section() -> str:
    """把致死类特殊事件渲染成「## 规则」节的全文（不含首尾空行）。"""
    lines: list[str] = []
    lines.append(SECTION_HEADER)
    lines.append("")
    lines.append(
        "> 本节由 `data/rules/*.toml` 生成，运行 `python3 sim/gen_rules.py` 重新生成；"
        "**禁止手改**（手改会在生成器下次运行时丢失，且过不了一致性测试）。"
    )
    lines.append(">")
    lines.append(
        f"> 署名统一为 `{SIGNATURE}`：遗言署真人姓名、可能有真假；规则不会，"
        "所有人看到的规则都一样。"
    )
    lines.append("")

    for entry in lethal_events.all():
        name = entry["name"]
        lines.append(f"### {SIGNATURE}·{CHAPTER}·{name}")
        lines.append("")
        # 结构化字段：让人和 AI 都能直接读到数值，不必回散文里数
        bits = []
        if "threshold" in entry:
            bits.append(f"阈值 {entry['threshold']}")
        if "threshold_multiplier" in entry:
            bits.append(f"阈值 ⌈[血限]×{entry['threshold_multiplier']}⌉")
        bits.append(f"进度口径 {entry['counter_label']}")
        lines.append(f"- {'；'.join(bits)}")
        lines.append(f"- 阈值算法：{entry['threshold_expr']}")
        lines.append(f"- 死因文案：{entry['death_cause_text']}")
        lines.append("")
        for text in entry["rule_lines"]:
            lines.append(text)
        lines.append("")

    return "\n".join(lines).rstrip()


def extract_section(book_text: str) -> str | None:
    """从《死者之书.md》里取出「## 规则」节的现有内容（没有则返回 None）。"""
    if SECTION_HEADER not in book_text:
        return None
    head, _, rest = book_text.partition(SECTION_HEADER)
    # 切到下一个同级或更高级标题为止
    body = rest
    for marker in ("\n## ", "\n# "):
        idx = body.find(marker, 1)
        if idx != -1:
            body = body[:idx]
    return (SECTION_HEADER + body).rstrip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="只比对现有「## 规则」节与生成结果，不写文件")
    args = parser.parse_args()

    wanted = render_section()
    book_text = BOOK.read_text(encoding="utf-8") if BOOK.exists() else ""

    if args.check:
        current = extract_section(book_text)
        if current is None:
            print("MISSING: 死者之书.md 里没有「## 规则」节")
            return 1
        if current != wanted:
            print("DRIFT: 「## 规则」节与 data/rules/*.toml 不一致，请跑 python3 sim/gen_rules.py")
            return 1
        print("OK: 「## 规则」节与事实源一致")
        return 0

    if not book_text:
        print(f"找不到 {BOOK}", file=sys.stderr)
        return 1

    current = extract_section(book_text)
    if current is None:
        new_text = book_text.rstrip() + "\n\n" + wanted + "\n"
    else:
        new_text = book_text.replace(current, wanted)
    BOOK.write_text(new_text, encoding="utf-8")
    print(f"written {BOOK}: 「## 规则」节 {len(lethal_events.all())} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
