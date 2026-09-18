"""项目 Markdown 结构、文件链接与标题锚点校验。"""
from __future__ import annotations
from pathlib import Path
import re
from urllib.parse import unquote

# ===== 活语料清单的唯一权威 =====
# 任何「扫文档」的工具与守卫都从这里取清单，别各自再抄一份：清单分叉＝有的文档没人看守。
# 单一化之前实测：代价数字守卫只扫 AI_EXPERIENCE.md 与 副本/*.md，而 README.md:41【封印】异变X、
# 法术索引.md:57-58【庇护X】消耗X／【再生X】消耗X 同样写着代价数字，落在守卫之外
# （当时数字恰好是对的——靠运气，不靠机器）。
# archive/** 是历史档案，一律不扫（那里允许记沿革）。
CORPUS_HARD = (
    "AI_EXPERIENCE.md", "全道纹索引.md", "README.md", "副本索引.md",
    "物品索引.md", "法术索引.md", "死者之书.md", "data/build_knowledge.json",
)
CORPUS_SOFT = ("报告.md", "机制迁移台账.md")   # 工作日志：本来就允许写旧口径，命中只警告
CORPUS_GLOBS = ("副本/*.md",)
# 生成物不手写，守卫方式是「重跑生成器逐字节比对」而不是文本扫描：
#   全道纹索引.md            ← sim/gen_daowen_index.py（引擎 docstring 是唯一来源）
#   data/build_knowledge.json ← sim 工具产出的知识库（含历史判决叙述，其中的数字是叙述不是口径）
GENERATED_DOCS = frozenset({"全道纹索引.md", "data/build_knowledge.json"})
_REPO_ROOT = Path(__file__).resolve().parent.parent


def corpus_files(root: str | Path | None = None, soft: bool = False) -> list[Path]:
    """硬层（规则事实源＋注入 AI 提示词的派生文档）或软层（工作日志）的现存文件。"""
    root = Path(root) if root is not None else _REPO_ROOT
    names = CORPUS_SOFT if soft else CORPUS_HARD
    out = [root / f for f in names if (root / f).exists()]
    if not soft:
        for g in CORPUS_GLOBS:
            out.extend(sorted(root.glob(g)))
    return out


def handwritten_rule_docs(root: str | Path | None = None) -> list[Path]:
    """硬层里**手写**的规则文档（生成物除外）：口径数字守卫该扫的就是这些。"""
    root = Path(root) if root is not None else _REPO_ROOT
    return [p for p in corpus_files(root)
            if p.relative_to(root).as_posix() not in GENERATED_DOCS]


LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")

AI_KNOWLEDGE_MAPPING = "用户提到的“AI 知识库”指仓库根目录的 `AI_EXPERIENCE.md`"
AI_POLICY_HEADING = "软件工程实现与验证准则"
AI_POLICY_RULE_MARKERS = (
    "不得编造",
    "我无法验证/无法运行",
    "完成声明必须有证据",
    "有歧义先停止并提问",
    "禁止用简化实现冒充完成",
    "输出使用固定结构",
    "新增功能必须有三类测试",
    "结论必须由验证支撑",
    "可自定义能力必须由数据驱动",
    "这不是自定义系统，只是固定实现",
    "发现规则或架构冲突时停止",
    "及时维护知识库",
)
AI_DELIVERY_SECTIONS = (
    "(1) 需求复述",
    "(2) 不确定点清单 + 必问问题",
    "(3) 设计方案",
    "(4) 实现计划",
    "(5) 交付",
    "(6) 测试",
    "(7) 进度与限制",
)
AI_DEDUCTION_HEADING = "推演铁律"
AI_DEDUCTION_RULE_MARKERS = (
    "只能取自引擎真实返回值",
    "AI 无权书写、改写或预写任何结局",
    "禁止结论先行",
    "禁止掺入叙事与文学描写",
    "后台发生的事件不得遗漏，后台没有的事件不得新增",
    "禁止复制同一段模板叙事",
    "只保留后台数据",
)
AI_WRITING_HEADING = "行文十诫"
AI_WRITING_RULE_MARKERS = (
    "严禁杜撰",
    "先否定后肯定",
    "三词并列",
    "高频词不得置于句首",
    "句尾不得拔高",
    "长短句混用",
    "一篇至多一两处",
    "毫无疑问",
    "堵死反驳",
    "假精确",
)
STALE_KNOWLEDGE_HEADING = re.compile(
    r"^#{2,6}\s+.*(?:过时|作废|已删除|已修复|已解决|补全进度|追记|"
    r"实测数据|测试通过|问题\s*/\s*方法\s*/\s*结果).*$",
    re.MULTILINE,
)


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


def validate_ai_knowledge_base(path: str | Path) -> dict:
    """校验 AI 知识库定位、12 条工程准则、交付顺序、推演铁律、行文十诫和废案标题。"""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"AI知识库校验失败：文件不存在：{path}")

    text = path.read_text(encoding="utf-8")
    errors = []
    first_nonempty = next((line for line in text.splitlines() if line.strip()), "")
    if first_nonempty != "# AI经验库":
        errors.append("一级标题必须是“# AI经验库”")
    if AI_KNOWLEDGE_MAPPING not in text:
        errors.append("缺少 AI 知识库到 AI_EXPERIENCE.md 的明确映射")

    section = re.search(
        rf"^##\s+{re.escape(AI_POLICY_HEADING)}\s*$\n(?P<body>.*?)(?=^##\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    policy_body = section.group("body") if section else ""
    if section is None:
        errors.append(f"缺少“## {AI_POLICY_HEADING}”章节")
    else:
        rule_numbers = re.findall(r"^(\d+)\.\s", policy_body, re.MULTILINE)
        expected_numbers = [str(number) for number in range(1, 13)]
        if rule_numbers != expected_numbers:
            errors.append(f"工程准则编号必须严格为1~12，实际为：{rule_numbers}")
        for marker in AI_POLICY_RULE_MARKERS:
            if marker not in policy_body:
                errors.append(f"工程准则缺少关键要求：{marker}")

        delivery_sections = [
            f"({number}) {label.strip()}"
            for number, label in re.findall(
                r"^\s+-\s+\((\d)\)\s+(.+?)\s*$", policy_body, re.MULTILINE
            )
        ]
        if tuple(delivery_sections) != AI_DELIVERY_SECTIONS:
            errors.append(
                "固定交付结构必须完整且顺序为：" + " -> ".join(AI_DELIVERY_SECTIONS)
            )

    deduction = re.search(
        rf"^###\s+{re.escape(AI_DEDUCTION_HEADING)}\s*$\n(?P<body>.*?)(?=^#{2,3}\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if deduction is None:
        errors.append(f"缺少“### {AI_DEDUCTION_HEADING}”章节")
    else:
        for marker in AI_DEDUCTION_RULE_MARKERS:
            if marker not in deduction.group("body"):
                errors.append(f"推演铁律缺少关键要求：{marker}")

    writing = re.search(
        rf"^###\s+{re.escape(AI_WRITING_HEADING)}\s*$\n(?P<body>.*?)(?=^#{2,3}\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if writing is None:
        errors.append(f"缺少“### {AI_WRITING_HEADING}”章节")
    else:
        for marker in AI_WRITING_RULE_MARKERS:
            if marker not in writing.group("body"):
                errors.append(f"行文十诫缺少关键要求：{marker}")

    stale_headings = STALE_KNOWLEDGE_HEADING.findall(text)
    if stale_headings:
        errors.append("知识库仍含过期/已解决流水账标题：" + "；".join(stale_headings))

    if errors:
        raise ValueError("AI知识库校验失败：\n" + "\n".join(errors))
    return {
        "rules": 12,
        "delivery_sections": len(AI_DELIVERY_SECTIONS),
        "stale_headings": 0,
    }


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

    knowledge_path = root / "AI_EXPERIENCE.md"
    if knowledge_path.is_file():
        validate_ai_knowledge_base(knowledge_path)
    return {"documents": len(documents), "links": link_count}
