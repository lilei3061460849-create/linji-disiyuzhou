#!/usr/bin/env python3
"""生成 `AI_EXPERIENCE.md` 的三节道纹正文（①-B 第二刀：文档从引擎生成，单一真源）。

**范围**＝规则正文里这三节，它们覆盖的道纹在引擎里都有 `calculate_*`：

- `### 道纹体系`             → 杀伐闭环 11 条（`gamedata.SHAFA_LOOP_DAOWEN`）
- `### 副本专属道纹`         → 四个已实现区域的专属道纹 32 条（`gamedata.REGION_EXCLUSIVE_DAOWEN`）
- `### 原始怪物道纹与转化道纹` → 原始 7 ＋ 转化 19（`ResonanceEngine.CLOSED_LOOPS` 的两个怪物环）
                               ＋ 净化 1（`UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN`）

其余四区（永夜庭／沉沦海／荒疫古城／巴别塔）的专属道纹尚未接入引擎，不在生成范围内。

标记块（HTML 注释，渲染时不可见）之间的内容由本工具重写，**不要手改**：改
`engine/daowen.py` 的 docstring 首行／`engine/gamedata.py` 的归属集合／
`ResonanceEngine.CLOSED_LOOPS`／`sim/daowen_doc.py::NOTES`，然后重跑本工具
（`sim/check_rule_change.py` 与 `tests/test_experience_daowen_generated.py` 会逐字节守卫）。

三节的行格式不是审美选择，是 `engine/rule_sync.py::extract_daowen_from_file` 倒推出来的：
它只认「标准行 `名X：…`」与「内联行 `名X（含消耗/代价的括号说明）`」，且内联正则
`([^（）]+)` 不容忍嵌套括号——细节见 `sim/daowen_doc.py` 三节渲染层的注释。抽取结果必须
仍是 38 条通用道纹（`tests/test_rule_sources.py` 钉死条数与双向 diff 为空）。

两段手写散文（副本专属的引子、原始↔转化的双向路径说明）不是引擎事实而是设计说明，
由本文件持有——与 `sim/gen_daowen_index.py` 的 `note_lines` 同一种做法。

跑法：

    PYTHONPATH=. .venv/bin/python sim/gen_experience_daowen.py           # 就地重写三节
    PYTHONPATH=. .venv/bin/python sim/gen_experience_daowen.py --check   # 只校验（漂移即非零退出）
    PYTHONPATH=. .venv/bin/python sim/gen_experience_daowen.py --print 道纹体系   # 看一节的生成结果
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "sim") not in sys.path:
    sys.path.insert(0, str(ROOT / "sim"))

from daowen_doc import (  # noqa: E402
    monster_back_block,
    monster_coverage,
    monster_tree_block,
    region_exclusive_block,
    shafa_block,
)

DOC = ROOT / "AI_EXPERIENCE.md"
SPAN_HEAD = "### 道纹体系"
SPAN_TAIL = "### 特殊事件"          # rule_sync 也按这两个标签切分道纹正文，别改

_SRC = ("源：engine/gamedata.py 的归属集合 ＋ engine/daowen.py 的 calculate_* docstring 首行"
        " ＋ ResonanceEngine.CLOSED_LOOPS ＋ sim/daowen_doc.py::NOTES")

# 三节各自的渲染器与标记名（顺序＝文档呈现顺序）
BLOCKS = (
    ("道纹体系", shafa_block),
    ("副本专属道纹", region_exclusive_block),
    ("怪物道纹树·正向", monster_tree_block),
    ("怪物道纹树·回溯", monster_back_block),
)

# ---------- 手写散文（设计说明，非引擎事实） ----------
REGION_INTRO = ("下列道纹由各副本自带，只在该副本内出现；效果正文与《全道纹索引》一致。\n"
                "它们同样遵守残韵变化与代价规则。")

MONSTER_PROSE = (
    "原始怪物道纹与转化道纹之间是**双向**的：转化道纹可用**同种残韵**回溯为它的原始怪物道纹。\n"
    "人类取得原始怪物道纹的唯一路径因此是两步残韵：第一步对持有原始道纹的怪物发动残韵，"
    "该怪物的原始道纹永久变为转化道纹、施法者同时永久获得该转化道纹；第二步施法者对自身持有的"
    "该转化道纹发动同种残韵，它永久变回原始怪物道纹。两步各自消耗1枚同种残韵，共2枚。\n"
    "这条路径不违反「人类只能从怪物身上获得原始怪物道纹」：转化道纹自身无法被【学习】，"
    "只能经残韵从怪物处取得，所以回溯的起点必然来自某只怪物。第一步之后该怪物永久失去这条原始道纹。\n"
    "必中/飞行没有【曲解】产物，故洞察/蒙蔽/滑翔/坠落之外不存在对应的曲解回溯边；"
    "19条回溯边与19条正向边一一镜像。"
)


def markers(tag: str) -> tuple[str, str]:
    return (f"<!-- BEGIN GENERATED {tag}｜{_SRC}"
            f"｜生成器：sim/gen_experience_daowen.py｜勿手改，改引擎后重跑 -->",
            f"<!-- END GENERATED {tag} -->")


def build_span() -> str:
    """三节全文（含标题、手写散文与标记块）。"""
    out: list[str] = []
    for tag, render in BLOCKS:
        begin, end = markers(tag)
        if tag == "道纹体系":
            out += [SPAN_HEAD, ""]
        elif tag == "副本专属道纹":
            out += ["### 副本专属道纹", "", REGION_INTRO, ""]
        elif tag == "怪物道纹树·正向":
            out += ["### 原始怪物道纹与转化道纹", ""]
        elif tag == "怪物道纹树·回溯":
            out += [MONSTER_PROSE, ""]
        out += [begin, render(), end, ""]
    return "\n".join(out).rstrip("\n")


def render(text: str) -> str:
    """把 `### 道纹体系` 到 `### 特殊事件` 之间的整段换成生成结果，返回新全文。"""
    lines = text.splitlines()
    head = next(i for i, l in enumerate(lines) if l.strip() == SPAN_HEAD)
    tail = next(i for i, l in enumerate(lines) if l.strip() == SPAN_TAIL)
    assert head < tail, "三节的边界标签顺序不对（道纹体系 应在 特殊事件 之前）"
    return "\n".join(lines[:head] + [build_span(), ""] + lines[tail:]).rstrip("\n") + "\n"


def main(argv: list[str]) -> int:
    if "--print" in argv:
        tag = argv[argv.index("--print") + 1]
        print(dict(BLOCKS)[tag]())
        return 0
    if "--coverage" in argv:
        for group, names in monster_coverage().items():
            print(f"{group}（{len(names)}）：{'、'.join(sorted(names))}")
        return 0

    old = DOC.read_text(encoding="utf-8")
    new = render(old)
    if new == old:
        print(f"unchanged {DOC.relative_to(ROOT)}（三节已是最新）")
        return 0
    if "--check" in argv:
        print("AI_EXPERIENCE.md 的三节道纹正文与引擎不同源（手改了生成块，或改了引擎忘记重跑）：")
        print(f"  - {DOC.relative_to(ROOT)}")
        print("修：PYTHONPATH=. python sim/gen_experience_daowen.py")
        return 1
    DOC.write_text(new, encoding="utf-8")
    cov = monster_coverage()
    total = sum(len(v) for v in cov.values())
    print(f"written {DOC.relative_to(ROOT)}: 三节共 {total} 条道纹"
          f"（杀伐{len(cov['杀伐'])}＋原始{len(cov['原始'])}＋转化{len(cov['转化'])}"
          f"＋区域专属{len(cov['区域专属'])}＋未实现{len(cov['未实现专属'])}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
