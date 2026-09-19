#!/usr/bin/env python3
"""生成 `副本/<区域>.md` 的「道纹网络」整节（①-B：文档从引擎生成，单一真源）。

**范围**＝引擎已实现闭环的四个区域（扭曲都市／罪孽都市／龙心谷／乱葬岗）：它们的道纹在
`engine/daowen.py` 里有 `calculate_*`，正文可从 docstring 首行派生。其余四区（永夜庭／沉沦海／
荒疫古城／巴别塔）的道纹**尚未接入引擎**，正文仍是手写设计稿，不在生成范围内——引擎里没有
事实源，生成等于凭空造。

标记块（HTML 注释，渲染时不可见）之间的内容由本工具重写，**不要手改**：
改 `engine/daowen.py` 的 docstring 首行／`ResonanceEngine.CLOSED_LOOPS`／`sim/daowen_doc.py::NOTES`，
然后重跑本工具（`sim/check_rule_change.py` 第 1 步与 `tests/test_region_daowen_generated.py`
会逐字节守卫它，手改立刻被测出来）。

事实源与「沿革不进正文」的口径见 `sim/daowen_doc.py` 的模块 docstring。

跑法：

    PYTHONPATH=. .venv/bin/python sim/gen_region_daowen.py           # 就地重写四个区域文档
    PYTHONPATH=. .venv/bin/python sim/gen_region_daowen.py --check   # 只校验（漂移即非零退出）
    PYTHONPATH=. .venv/bin/python sim/gen_region_daowen.py --print 罪孽都市   # 看一个区域的生成结果
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "sim") not in sys.path:
    sys.path.insert(0, str(ROOT / "sim"))

from daowen_doc import REGION_LOOPS, loop_nodes, region_block  # noqa: E402

BEGIN = ("<!-- BEGIN GENERATED 道纹网络｜源：engine/daowen.py 的 calculate_* docstring 首行"
         " ＋ ResonanceEngine.CLOSED_LOOPS ＋ sim/daowen_doc.py::NOTES"
         "｜生成器：sim/gen_region_daowen.py｜勿手改，改引擎后重跑 -->")
END = "<!-- END GENERATED 道纹网络 -->"

# 生成块结束于下一个小节：这些标签是各区域文档现有的收尾边界（rule_sync 也按「专属行动」切分）。
NEXT_SECTION = ("专属行动", "专属机制", "怪物池", "专属事件")


def _section_bounds(lines: list[str]) -> tuple[int, int]:
    """返回（道纹网络标题行号, 下一小节起始行号）。"""
    head = next(i for i, l in enumerate(lines) if "道纹网络】" in l)
    end = len(lines)
    for i in range(head + 1, len(lines)):
        if any(tag in lines[i] for tag in NEXT_SECTION):
            end = i
            break
    return head, end


def render(path: Path) -> str:
    """把该区域文档的「道纹网络」整节换成生成块，返回新全文。"""
    region = path.stem
    assert region in REGION_LOOPS, f"{region} 不在生成范围内（引擎未实现其闭环）"
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    head, end = _section_bounds(lines)

    block = [lines[head], "", BEGIN, region_block(region), END, ""]
    return "\n".join(lines[:head] + block + lines[end:]).rstrip("\n") + "\n"


def main(argv: list[str]) -> int:
    files = [ROOT / "副本" / f"{r}.md" for r in REGION_LOOPS]
    if "--print" in argv:
        region = argv[argv.index("--print") + 1]
        print(region_block(region))
        return 0
    drift = []
    for f in files:
        new = render(f)
        old = f.read_text(encoding="utf-8")
        if new == old:
            continue
        drift.append(f)
        if "--check" not in argv:
            f.write_text(new, encoding="utf-8")
    names = "、".join(f.stem for f in files)
    if "--check" in argv:
        if drift:
            print("副本区域道纹网络整节与引擎不同源（手改了生成块，或改了引擎忘记重跑）：")
            for f in drift:
                print(f"  - {f.relative_to(ROOT)}")
            print("修：PYTHONPATH=. python sim/gen_region_daowen.py")
            return 1
        print(f"✓ {names}：道纹网络整节与引擎同源（逐字节一致）")
        return 0
    for f in drift:
        nodes = loop_nodes(REGION_LOOPS[f.stem])
        print(f"written {f.relative_to(ROOT)}: {len(nodes)} 条道纹（{'、'.join(nodes)}）")
    if not drift:
        print(f"unchanged {names}（已是最新）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
