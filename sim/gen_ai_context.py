#!/usr/bin/env python3
"""从第四宇宙的规范文档生成单文件 AI 全量上下文 ``AI_CONTEXT.md``。

## 为什么需要它

外部 AI 若只抓取 GitHub raw 的一个 URL，不会自动继续抓 README 里的相对链接；而
README 又刻意不重复会变动的精确中文规则。这个生成物把当前有效的游戏知识打包为
一个可直接抓取的文件：规则、流程、道纹、物品、法术、副本（含草案）、故事正典、
AI 操作约束与结构化规则事实源全部在内。

它**不是新的事实源**：每个区块都保留来源路径和 SHA-256；修改任意来源后都必须重跑
本脚本。正文被置于 fenced code block，以保留原文，又避免嵌入文档内的相对链接在
AI_CONTEXT.md 里被错误地当成从仓库根目录出发的链接。

## 用法

    python3 sim/gen_ai_context.py          # 写回 AI_CONTEXT.md
    python3 sim/gen_ai_context.py --check  # 只比对，不写（供一致性测试使用）

外部单文件入口（合并到 main 后）：

    https://raw.githubusercontent.com/lilei3061460849-create/linji-disiyuzhou/main/AI_CONTEXT.md
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.document_sources import (  # noqa: E402
    AI_CONTEXT_FILE,
    COMMON_RULES_FILE,
    DEATH_BOOK_FILE,
    DUNGEON_INDEX_FILE,
    ITEM_INDEX_FILE,
    PLAYBOOK_FILE,
    README_FILE,
)
from engine.dungeons import load_dungeon_manifest  # noqa: E402

OUTPUT = ROOT / AI_CONTEXT_FILE


@dataclass(frozen=True)
class ContextSource:
    """一个需要嵌入 AI_CONTEXT 的规范源。"""

    role: str
    path: Path
    kind: str = "markdown"

    @property
    def relative_path(self) -> str:
        return self.path.relative_to(ROOT).as_posix()


def context_sources() -> list[ContextSource]:
    """返回完整且稳定有序的上下文来源列表（不含 README 与报告）。

    README 是入口而非精确规则源，嵌入它只会重复导航；报告是当前待办台账而非游戏
    正典，两者都不应被外部 AI 当成游戏真相。副本通过 manifest 读取，因此新副本被
    登记后会自动进入上下文，已实现/未实现状态也写入角色标签。
    """
    sources = [
        ContextSource("精确通用规则（唯一正文事实源）", ROOT / COMMON_RULES_FILE),
        ContextSource("操作与记录规范（开局/战斗/死斗/战报）", ROOT / PLAYBOOK_FILE),
        ContextSource("死者之书（法术、遗言、压缩规则与特殊事件页）", ROOT / DEATH_BOOK_FILE),
        ContextSource("全道纹索引（引擎派生效果索引）", ROOT / "全道纹索引.md"),
        ContextSource("物品索引（遗物/消耗品/法器事实源）", ROOT / ITEM_INDEX_FILE),
        ContextSource("法术索引（法术语法与完整说明）", ROOT / "法术索引.md"),
        ContextSource("副本清单与状态", ROOT / DUNGEON_INDEX_FILE),
    ]

    for entry in load_dungeon_manifest(ROOT / DUNGEON_INDEX_FILE):
        sources.append(ContextSource(
            f"副本正文：{entry.name}（{entry.status}，{entry.tier}）", entry.path,
        ))

    sources.extend([
        ContextSource("世界观与战报正典", ROOT / "故事文档.md"),
        ContextSource("AI 开发/维护/推演约束", ROOT / "AI_EXPERIENCE.md"),
        ContextSource("结构化规则源：致死特殊事件", ROOT / "data/rules/lethal_events.toml", "toml"),
        ContextSource("结构化规则源：非致死特殊事件", ROOT / "data/rules/special_events.toml", "toml"),
        ContextSource("结构化规则源：稳定底层逻辑压缩稿", ROOT / "data/rules/game_rules.toml", "toml"),
    ])
    return sources


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fence_language(source: ContextSource) -> str:
    return "toml" if source.kind == "toml" else "markdown"


def _safe_fence(text: str) -> str:
    """返回比正文任何连续反引号都长的 fenced-code 分隔符。

    被打包的 Markdown 自身有 ``` 代码块（推演模板、AI 知识库尤甚）。固定三反引号
    会让它们提早关闭外层代码块，继而让校验器把嵌入的相对链接当作 AI_CONTEXT 根目录
    链接。动态加一根反引号可保持区块完整、可复现。
    """
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def render_context() -> str:
    """渲染全文；纯函数、无时间戳，保证 --check 可以逐字比较。"""
    sources = context_sources()
    loaded: list[tuple[ContextSource, str, str]] = []
    for source in sources:
        if not source.path.is_file():
            raise FileNotFoundError(f"AI_CONTEXT 来源不存在：{source.relative_path}")
        text = source.path.read_text(encoding="utf-8").rstrip()
        loaded.append((source, text, _digest(text)))

    lines = [
        "# 第四宇宙 · AI 全量上下文",
        "",
        "> **生成文件，禁止手改。** 源文件变更后运行 `python3 sim/gen_ai_context.py`；",
        "> 只检查一致性则运行 `python3 sim/gen_ai_context.py --check`。",
        ">",
        "> 此文件给只会抓取一个 raw URL 的外部 AI 使用。它汇总当前有效的游戏文档，",
        "> 但不改变任何来源的事实源地位：规则冲突仍按下列优先级判断。",
        "",
        "## 使用契约（先读）",
        "",
        "1. **结算优先级**：用户最新裁定 → 引擎实际结算 → 结构化规则 TOML → 《规则正文》 → 索引/速览/叙事。",
        "2. **死者之书的遗言不是规则**：遗言署真人姓名，可能有真假；其中「规则」节署 `？？？`，才是所有人一致可见的压缩讲述。",
        "3. **压缩不等于穷尽**：六条底层逻辑只保留稳定原则。数值、目标限制、时序、可用对象等必须在《规则正文》、索引或引擎口径里核对。",
        "4. **副本状态要区分**：标为「已实现」的副本进入当前运行时；「未实现」是规则草案，不进入事件池、怪物池或常规流程。",
        "5. **AI 行为约束**：推演只能使用引擎真实返回值；遇到规则与实现冲突，停止并请求 DM/用户裁定，不得自行编造。",
        "",
        "## 单文件范围",
        "",
        f"- 已打包 {len(loaded)} 份来源：通用规则、推演规范、死者之书、道纹、物品、法术、副本清单与全部副本文档、故事正典、AI 约束、结构化规则 TOML。",
        f"- 未打包 `{README_FILE}`（它只是入口/导航）和 `报告.md`（它是项目待办，不是游戏正典）。",
        "- 每个区块的 SHA-256 用于判断外部副本是否过期；不要从本文件编辑后反写来源。",
        "",
        "## 来源目录与校验指纹",
        "",
        "| # | 角色 | 原始文件 | SHA-256 |",
        "| --- | --- | --- | --- |",
    ]
    for index, (source, _, digest) in enumerate(loaded, 1):
        lines.append(
            f"| {index} | {source.role} | [{source.relative_path}]({source.relative_path}) | `{digest}` |")

    lines.extend([
        "",
        "## 打包正文",
        "",
        "> 每一块保持原始文件文字；链接、标题与代码仍应以来源文件为准。为防止嵌套相对",
        "> 链接在此聚合文件内被错误解析，正文采用代码块呈现。",
    ])

    for index, (source, text, digest) in enumerate(loaded, 1):
        fence = _safe_fence(text)
        lines.extend([
            "",
            f"### 来源 {index}：{source.role}",
            "",
            f"- 路径：`{source.relative_path}`",
            f"- SHA-256：`{digest}`",
            "",
            f"{fence}{_fence_language(source)}",
            text,
            fence,
        ])

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只比对 AI_CONTEXT.md，不写文件")
    args = parser.parse_args()

    wanted = render_context()
    current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else None
    if args.check:
        if current != wanted:
            print("DRIFT: AI_CONTEXT.md 与当前规范源不一致，请跑 python3 sim/gen_ai_context.py")
            return 1
        print(f"OK: AI_CONTEXT.md 与 {len(context_sources())} 份来源一致")
        return 0

    OUTPUT.write_text(wanted, encoding="utf-8")
    print(f"written {OUTPUT}: {len(context_sources())} 份来源，{len(wanted.encode('utf-8'))} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
