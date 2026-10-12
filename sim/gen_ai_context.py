#!/usr/bin/env python3
"""从第四宇宙的规范文档生成单文件 AI 上下文 ``AI_CONTEXT.md``。

## 为什么需要它

外部 AI 若只抓取 GitHub raw 的一个 URL，不会自动继续抓 README 里的相对链接。这个
生成物把「文字描述、规则逻辑与世界观」打包为一个可直接抓取的文件：通用规则、开局与
流程、死者之书的法术与规则节、道纹与物品索引、法术规则、已实现副本、故事正典、AI 角色
扮演约束。

## 只收什么、剔除什么

生成物以**剧情优先的混合上下文**为目标：保留世界观、角色记忆、人物关系、主线目标和角色扮演约束，只保留少量稳定的剧情理解原则。完整规则与索引不再批量收录（源文件不动）：

- 完整规则与机制目录：规则正文、推演规范、道纹/物品/法术索引、副本索引与完整副本机制；\n- 代码与结构化数据源：全部 TOML（`data/rules/*.toml`）、代码块、行内代码；
- 系统接口规范：DSL 语法、引擎接线状态、命令/API 写法、测试与生成器说明；
- 机械化格式要求：战斗推演格式、报告书写口径、遗言格式、故事收录流程；
- 未实用的未来规划：未实现副本草案、草案遗物、占位道纹/物品、迁移台账与开发规范。

剔除按下列**可复现规则**执行（见 ``distill_source``），不做人工挑字：

1. 删除所有代码块（``` / ~~~ 围栏）。
2. 按标题删除整节（``ContextSource.drop_headings``），或只保留指定节（``keep_headings``）。
3. 删除不以「## 」开始的导言（``drop_preamble``）。
4. 删除叶子标题（三级及以下）下含未实现/草案/占位字样的整条目。
5. 按「段落单元」（空行、列表项、引述、表格行、标题各为一单元）过滤：
   - 含结构/接口性行内代码（路径、标识符、命令、公式）的单元整体删除；
   - 仅为纯中文的行内代码（如 ``持续∞``）去掉反引号保留文字；
   - 含未实现/草案/占位字样，或含全局与来源专属禁用短语的单元整体删除。
6. 指向未收录文件的链接降为纯文字。

## 不是新的事实源

每个区块的 SHA-256 是**原始来源文件**的指纹（用于判断外部副本是否过期）；区块内正文
是过滤后的文字。修改任意来源后都必须重跑本脚本，且不得从生成物反写来源。

## 用法

    python3 sim/gen_ai_context.py          # 写回 AI_CONTEXT.md
    python3 sim/gen_ai_context.py --check  # 只比对，不写（供一致性测试使用）

外部单文件入口（合并到 main 后）：

    https://raw.githubusercontent.com/lilei3061460849-create/linji-disiyuzhou/main/AI_CONTEXT.md
"""
from __future__ import annotations

import argparse
import hashlib
import posixpath
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

# 未实现 / 草案 / 占位字样：命中即视为未来规划，整条（标题或段落单元）剔除。
FUTURE_WORDS: tuple[str, ...] = ("草案", "未实现", "未接入", "尚未接入", "占位", "待补")

# 所有来源共用的禁用短语：生成链路、一致性测试与数据表字段说明，不属于文字内容。
GLOBAL_DROP_PHRASES: tuple[str, ...] = (
    "AI_CONTEXT",
    "gen_rules",
    "gen_ai_context",
    "gen_daowen_index",
    "data/rules",
    "一致性测试",
    "引擎解析",
    "阈值算法",
    "进度口径",
    "死因文案",
    "重新生成本文件",
    "可读性镜像",
)

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
NUMERAL_RE = re.compile(r"^[一二三四五六七八九十]+、.+$")
UNIT_START_RE = re.compile(
    r"^\s*(?:#{1,6}\s|[-*+]\s|\d+[.、)]|>|\||[一二三四五六七八九十]+、)"
)
# 未加反引号的代码标识符（如 use_daowen、dodge_targets）。单字母数学变量（R_i、T_i）属于规则公式，不算。
CODE_IDENTIFIER_RE = r"[a-z]{2,}_[a-z_]+"
LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\(([^()\s\n]+)\)")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


@dataclass(frozen=True)
class ContextSource:
    """一个需要嵌入 AI_CONTEXT 的规范源（及其剔除规则）。"""

    role: str
    path: Path
    drop_headings: tuple[str, ...] = ()
    keep_headings: tuple[str, ...] | None = None
    drop_preamble: bool = False
    drop_phrases: tuple[str, ...] = ()

    @property
    def relative_path(self) -> str:
        return self.path.relative_to(ROOT).as_posix()


def context_sources() -> list[ContextSource]:
    """剧情优先：只嵌入世界观/角色记忆与角色扮演约束。

    完整规则、道纹、物品、法术与副本机制不批量打包；需要精确裁定时按需查询原始文档。
    """
    return [
        ContextSource(
            "世界观与角色档案（故事正典）",
            ROOT / "故事文档.md",
            keep_headings=("世界观与底层因果法则", "角色记忆档案"),
            drop_headings=("收录规则", "待核查区", "正文区"),
            drop_phrases=("战报正文区", "待核查区", "独立性声明", "战报.md", "收录规则", "不随引擎数值调整"),
        ),
        ContextSource(
            "AI 角色扮演约束（节选：职责、性格、扮演要求）",
            ROOT / "AI_EXPERIENCE.md",
            keep_headings=("职责划分", "角色性格塑造", "角色扮演要求"),
            drop_phrases=("引擎", "行为画像"),
        ),
    ]

def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_fence(text: str) -> str:
    """返回比正文任何连续反引号都长的 fenced-code 分隔符（保持区块完整）。"""
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _level(line: str) -> int | None:
    """标题层级：Markdown 标题为 # 数；中文序号行（「六、…」）视作二级边界。"""
    match = HEADING_RE.match(line)
    if match:
        return len(match.group(1))
    if NUMERAL_RE.match(line) and len(line) <= 40:
        return 2
    return None


def _title(line: str) -> str:
    match = HEADING_RE.match(line)
    return match.group(2).strip() if match else line.strip()


def _strip_fences(lines: list[str]) -> list[str]:
    out: list[str] = []
    fence: str | None = None
    for line in lines:
        match = FENCE_RE.match(line)
        if fence is None:
            if match:
                fence = match.group(1)
                continue
            out.append(line)
        elif match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
            fence = None
    return out


def _keep_sections(lines: list[str], titles: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        level = _level(lines[i])
        if level is not None and _title(lines[i]) in titles:
            j = i + 1
            while j < n and not (_level(lines[j]) is not None and _level(lines[j]) <= level):
                j += 1
            out.extend(lines[i:j])
            i = j
        else:
            i += 1
    return out


def _drop_sections(lines: list[str], titles: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        level = _level(lines[i])
        if level is not None and _title(lines[i]) in titles:
            j = i + 1
            while j < n and not (_level(lines[j]) is not None and _level(lines[j]) <= level):
                j += 1
            i = j
        else:
            out.append(lines[i])
            i += 1
    return out


def _drop_preamble(lines: list[str]) -> list[str]:
    first = next((i for i, line in enumerate(lines) if line.startswith("## ")), len(lines))
    head = [line for line in lines[:first] if line.startswith("# ")]
    return head + [""] + lines[first:] if head else lines[first:]


def _drop_future_leaf_blocks(lines: list[str]) -> list[str]:
    """三级及以下标题的条目若含未实现/草案/占位字样，整条（含标题）剔除。"""
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        match = HEADING_RE.match(lines[i])
        if match and len(match.group(1)) >= 3:
            j = i + 1
            while j < n and _level(lines[j]) is None:
                j += 1
            block = "\n".join(lines[i:j])
            if any(word in block for word in FUTURE_WORDS):
                i = j
                continue
        out.append(lines[i])
        i += 1
    return out


SENTENCE_END_RE = re.compile(r"[。！？；]\s*$")


def _group_units(lines: list[str]) -> list[list[str] | None]:
    """把行切成段落单元；空行记为 None（段落分隔）。

    单元在空行、列表/标题/表格起始行、以及句末标点（。！？；）之后断开，使一句含代码时
    只剔除该句，而不连带同段的其他规则句。
    """
    units: list[list[str] | None] = []
    current: list[str] | None = None
    for line in lines:
        if not line.strip():
            units.append(None)
            current = None
        elif current is None or UNIT_START_RE.match(line) or SENTENCE_END_RE.search(current[-1]):
            current = [line]
            units.append(current)
        else:
            current.append(line)
    return units


def _is_prose_span(span: str) -> bool:
    """纯中文/符号文字（无 ASCII 字母数字与代码符号）才算可保留的正文。"""
    return bool(span.strip()) and not re.search(r"[A-Za-z0-9_=<>{}\"'/\\|.+*#%@`]", span)


def _clean_unit(text: str, drop_phrases: tuple[str, ...]) -> str | None:
    """处理一个段落单元：含代码则整体剔除；纯文字行内代码去掉反引号；命中禁用词则剔除。"""

    def paren(match: re.Match[str]) -> str:
        return match.group(0) if _is_prose_span(match.group(1)) else ""

    if re.fullmatch(r"\s*>?\s*", text):
        return None
    text = re.sub(r"[（(]\s*`([^`\n]+)`\s*[）)]", paren, text)
    spans = re.findall(r"`([^`\n]+)`", text)
    if any(not _is_prose_span(span) for span in spans):
        return None
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    if re.search(CODE_IDENTIFIER_RE, text):
        return None
    if any(word in text for word in FUTURE_WORDS):
        return None
    if any(phrase in text for phrase in GLOBAL_DROP_PHRASES + drop_phrases):
        return None
    return text


def _resolve_links(text: str, source_rel: str, included: frozenset[str]) -> str:
    """指向已收录文件的链接保留；指向未收录文件（README、报告、被剔除的源）的降为文字。"""

    def repl(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        if re.match(r"^[a-z]+://", target):
            return match.group(0)
        path_part = target.split("#", 1)[0]
        if not path_part:
            return label
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_rel), path_part))
        return match.group(0) if resolved in included else label

    return LINK_RE.sub(repl, text)


def distill_source(source: ContextSource, included: frozenset[str]) -> str:
    """按 ContextSource 的规则生成过滤后的正文（纯函数，供生成与测试共用）。"""
    raw = source.path.read_text(encoding="utf-8").rstrip()
    lines = _strip_fences(raw.splitlines())
    if source.keep_headings is not None:
        lines = _keep_sections(lines, source.keep_headings)
    if source.drop_preamble:
        lines = _drop_preamble(lines)
    lines = _drop_sections(lines, source.drop_headings)
    lines = _drop_future_leaf_blocks(lines)

    out: list[str] = []
    for unit in _group_units(lines):
        if unit is None:
            if out and out[-1] != "":
                out.append("")
            continue
        cleaned = _clean_unit("\n".join(unit), source.drop_phrases)
        if cleaned is not None:
            out.extend(cleaned.split("\n"))

    text = "\n".join(out).strip()
    text = _resolve_links(text, source.relative_path, included)
    return re.sub(r"\n{3,}", "\n\n", text)


def render_context() -> str:
    """渲染全文；纯函数、无时间戳，保证 --check 可以逐字比较。"""
    sources = context_sources()
    included = frozenset(source.relative_path for source in sources)
    loaded: list[tuple[ContextSource, str, str]] = []
    for source in sources:
        if not source.path.is_file():
            raise FileNotFoundError(f"AI_CONTEXT 来源不存在：{source.relative_path}")
        raw = source.path.read_text(encoding="utf-8").rstrip()
        loaded.append((source, distill_source(source, included), _digest(raw)))

    lines = [
        "# 第四宇宙 · AI 上下文",
        "",
        "> **生成文件，禁止手改。** 源文件变更后运行 `python3 sim/gen_ai_context.py`；",
        "> 只检查一致性则运行 `python3 sim/gen_ai_context.py --check`。",
        ">",
        "> 此文件是剧情优先的混合上下文，供只抓取一个 raw URL 的外部 AI 使用。",
        "> 它优先提供世界观、角色记忆、人物关系、主线目标与角色扮演约束，只附带少量稳定的剧情理解原则。",
        "> 完整规则、道纹、物品、法术与副本机制不再整批嵌入，需要精确裁定时按需查询对应原文。",
        "> 本文件不是新的规则事实源，也不改变角色记忆与规则正本之间的优先级。",
        "",
        "## 使用契约（先读）",
        "",
        "1. **裁定优先级**：用户最新裁定高于一切；涉及精确结算时，必须按需查询《规则正文》及对应机制原文，不得把本文件当作完整规则。",
        "2. **剧情优先**：优先维持世界观因果、人物性格、既有羁绊、当前认知、主线目标与未解悬念；不要用无关机制资料淹没剧情。",
        "3. **角色记忆不等于客观规则**：角色可能误解、遗漏或持有过时认知；记忆中的说法可作为叙事事实，但不能覆盖规则正本。",
        "4. **最小必要规则**：只把理解剧情所需的稳定原则放在这里。世界遵循因果而非宿命，选择会带来后果，力量与解法可能伴随代价；具体数值、触发条件、目标限制和时序按需查原文。",
        "5. **死者之书的遗言不是规则**：遗言可能属于具体角色且可能有真假；不能把角色遗言自动当成客观规则。",
        "6. **副本状态要区分**：故事中提到的副本、能力或线索不自动证明其机制已实现；涉及副本规则时，按需查询对应副本文档。",
        "",
        "## 单文件范围",
        "",
        f"- 已收录 {len(loaded)} 份来源：世界观与角色记忆正典、AI 角色扮演约束（职责、性格与扮演要求节选）。",
        "- 不再整批收录：通用规则正文、完整推演规范、道纹/物品/法术索引、副本索引与副本机制、战报、代码和开发资料。",
        "- 来源区块附原始文件 SHA-256 指纹，便于判断内容是否过期；区块正文经过筛选，**不得**反写来源。",
        "",
        "## 来源目录与校验指纹",
        "",
        "| # | 角色 | 原始文件 | SHA-256 |",
        "| --- | --- | --- | --- |",
    for index, (source, _, digest) in enumerate(loaded, 1):
        lines.append(
            f"| {index} | {source.role} | [{source.relative_path}]({source.relative_path}) | `{digest}` |")

    lines.extend([
        "",
        "## 打包正文",
        "",
        "> 每一块为来源文件经过滤后的文字（已去除代码、接口与格式段落、未实现内容）；标题与链接以来源文件为准。",
        "> 正文采用代码块呈现，以防嵌套相对链接在此聚合文件内被错误解析。",
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
            f"{fence}markdown",
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
