"""副本区域的「道纹网络」整节＝**派生产物**（①-B：文档从引擎生成，单一真源）。

生成器 `sim/gen_region_daowen.py` ＋共享解析层 `sim/daowen_doc.py`，事实源是
`engine/daowen.py` 的 docstring 首行、`ResonanceEngine.CLOSED_LOOPS` 与 `daowen_doc.NOTES`。

这里钉四件事：

1. **逐字节**：重跑生成器必须还原仓库里的四个区域文档——手改生成块、或改了引擎忘记重跑，
   都会立刻红（与 `全道纹索引.md` 的同款守卫，见 test_daowen_cost_consistency.py）。
2. **结构与顺序**：每块含且只含该区域闭环的 8 条道纹，顺序＝`CLOSED_LOOPS`（残韵闭环顺序
   有意义，不能按字典序排；起点＝第一条边的 src）。
3. **正文红线**：生成块里不得出现沿革（日期水印／DM裁定／废止／旧版／原名）——沿革留在
   引擎 docstring 正文段、`archive/` 与 `sim/check_rule_change.py::STALE_PHRASES`。
4. **兼容**：`engine/rule_sync.py` 仍能从四个区域各抽出同样 8 条道纹（引擎运行时读这些文档，
   实测 2026-09-19：生成格式一度让【分裂】的 `X/Y` 头抽不出来，`dungeon_daowen` 从 64 掉到 63）。
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "sim") not in sys.path:
    sys.path.insert(0, str(ROOT / "sim"))

from daowen_doc import REGION_LOOPS, RULES, loop_nodes, loop_start  # noqa: E402
from engine.daowen import ResonanceEngine  # noqa: E402
from engine.rule_sync import RuleSync  # noqa: E402

REGIONS = list(REGION_LOOPS)
BLOCK = re.compile(
    r"<!-- BEGIN GENERATED 道纹网络.*?-->\n(?P<body>.*?)\n<!-- END GENERATED 道纹网络 -->",
    re.S)
DEF_LINE = re.compile(r"^\d+\.([\u4e00-\u9fff]{2})X(?:/Y)?", re.M)
# 沿革特征：日期水印、裁定落款、废止/旧版/改名陈述
HISTORY = re.compile(r"20\d\d-\d\d-\d\d|DM\s*裁定|废止|旧版|旧口径|原名【|已删除|已并入")


def _text(region: str) -> str:
    return (ROOT / "副本" / f"{region}.md").read_text(encoding="utf-8")


def _block(region: str) -> str:
    m = BLOCK.search(_text(region))
    assert m, f"副本/{region}.md 里没有生成块标记（BEGIN/END GENERATED 道纹网络）"
    return m.group("body")


def test_region_blocks_are_regeneration_byte_identical():
    """四个区域文档的生成块与引擎同源：重跑生成器必须逐字节还原。"""
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.run([sys.executable, str(ROOT / "sim" / "gen_region_daowen.py"), "--check"],
                          cwd=str(ROOT), env=env, capture_output=True, text=True)
    assert proc.returncode == 0, (
        "副本区域道纹网络整节与引擎不同源。改了道纹口径/闭环请重跑 "
        "`PYTHONPATH=. python sim/gen_region_daowen.py`，不要手改生成块：\n"
        + proc.stdout + proc.stderr)


def test_block_markers_appear_exactly_once():
    """标记唯一：多处标记会让生成器只重写第一块，剩下的静默漂移。"""
    for r in REGIONS:
        t = _text(r)
        assert t.count("<!-- BEGIN GENERATED 道纹网络") == 1, f"{r}: BEGIN 标记 {t.count('BEGIN GENERATED')} 处"
        assert t.count("<!-- END GENERATED 道纹网络 -->") == 1, f"{r}: END 标记不唯一"


def test_block_lists_region_loop_in_engine_order():
    """生成块＝该区域闭环的全部道纹，且顺序与 `CLOSED_LOOPS` 一致（起点标注也在）。"""
    for region in REGIONS:
        body = _block(region)
        got = DEF_LINE.findall(body)
        want = loop_nodes(REGION_LOOPS[region])
        assert got == want, f"{region}: 定义行 {got} ≠ 引擎闭环顺序 {want}"
        chain = next(l for l in body.splitlines() if l.startswith("环形闭环主轨："))
        start = loop_start(REGION_LOOPS[region])
        assert f"{start}X（起点/终点）" in chain, f"{region}: 链行没标出起点 {start}"
        # 闭环要成环：末段回到起点
        assert chain.rstrip().endswith(f"{start}X"), f"{region}: 链行没有回到起点（不成环）"
        # 每条边的残韵类型都印出来了
        for src, rtype, dst in ResonanceEngine.CLOSED_LOOPS[REGION_LOOPS[region]]:
            assert f"⇄（{rtype}）{dst}X" in chain, f"{region}: 链行缺 {src}→({rtype})→{dst}"


def test_block_carries_engine_rule_text_verbatim():
    """定义行的正文＝引擎 docstring 首行（剥掉修订注记后），一个字都不差。"""
    for region in REGIONS:
        body = _block(region)
        for line in body.splitlines():
            m = DEF_LINE.match(line)
            if not m:
                continue
            name = m.group(1)
            rule = RULES[name]
            expect = f"{name}{rule.head}：{rule.line}"
            assert line.split(".", 1)[1] == expect, (
                f"{region}·{name}: 正文与引擎首行不符\n  文档: {line}\n  应为: {expect}")


def test_block_has_no_revision_history():
    """正文红线：生成块里不得有沿革（日期水印／裁定落款／废止・旧版陈述）。"""
    for region in REGIONS:
        body = _block(region)
        hits = [(i + 1, l.strip()[:80]) for i, l in enumerate(body.splitlines()) if HISTORY.search(l)]
        assert not hits, f"{region} 的生成块里出现沿革（应留在引擎 docstring/archive/废案清单）：\n" + "\n".join(
            f"  行{i}: {l}" for i, l in hits)


def test_rule_sync_still_extracts_every_region_daowen(tmp_path):
    """兼容守卫：引擎运行时按这些文档抽道纹，格式变了会静默少抽。

    负向对照（本条存在的理由）：生成器最初把【分裂】印成 `1.分裂X/Y：…`，
    `rule_sync` 的标准格式正则不认 `/Y` 头 → 乱葬岗只抽出 7 条、
    `diff_project_daowen()` 报 `in_engine_only=['分裂']`。已把正则放宽为 `X(?:/Y)?`。
    """
    # RuleSync 会 makedirs(dirname(db_path))，所以不能用 ":memory:"
    sync = RuleSync(rule_files=[], rules_dir=str(ROOT), db_path=str(tmp_path / "sync.db"))
    for region in REGIONS:
        names = [d["name"] for d in sync.extract_daowen_from_file(str(ROOT / "副本" / f"{region}.md"))]
        want = loop_nodes(REGION_LOOPS[region])
        assert names == want, f"{region}: rule_sync 抽出 {names}，应为 {want}"
    all_dungeon = sync.extract_dungeon_daowen(include_drafts=True)
    assert len(all_dungeon) == 64, f"八个区域各 8 条＝64，实得 {len(all_dungeon)}"
    diff = sync.diff_project_daowen()
    assert diff["in_engine_only"] == [] and diff["in_file_only"] == [], (
        f"文档与引擎注册表不齐：引擎独有 {diff['in_engine_only']}，文档独有 {diff['in_file_only']}")


def test_unimplemented_regions_are_not_generated():
    """范围守卫：引擎没实现的四个区域**不得**被生成（没有事实源＝凭空造）。"""
    for region in ("永夜庭", "沉沦海", "荒疫古城", "巴别塔"):
        assert region not in REGION_LOOPS, f"{region} 进了生成范围，但引擎里没有它的闭环"
        assert "BEGIN GENERATED 道纹网络" not in _text(region), (
            f"副本/{region}.md 出现了生成块：该区域道纹尚未接入引擎，正文仍是手写设计稿")
