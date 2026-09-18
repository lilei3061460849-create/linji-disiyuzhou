"""道纹代价一致性守卫（报告 P1，2026-09-17）。

病灶：一条道纹的「代价」在四个地方各写一遍——引擎 `cost` 字段、`calculate_*` 的
docstring、返回的 `summary`、以及注入 AI 提示词的文档（`全道纹索引.md` /
`AI_EXPERIENCE.md` / `副本/*.md`）。上一轮调价只改了 cost 与 docstring：**35 条
summary** 与 **32 处文档**停在旧倍率——战斗日志写「消耗20法力」、AI 读到「消耗20X」，
引擎实际只扣 5。

本文件把这四处钉成一条链，唯一事实源是 cost 字段：

    cost 字段 → docstring → summary → 全道纹索引.md → 规则正文/副本文档

外加一条：`全道纹索引.md` 是 `sim/gen_daowen_index.py` 的**派生产物**，重生成结果
必须与仓库里的文件逐字节一致——手改索引（或改了引擎忘记重跑）会立刻被测出来。
"""
import os
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.daowen import DaoWenEngine  # noqa: E402

DaoWenEngine.register_all()

# ---- docstring 首行解析（与 sim/gen_daowen_index.py 同口径）----
DOC_HEAD = re.compile(r'^\S+?(?P<head>X(?:/Y)?)(?:（[^）]*）)?[：:]\s*(?P<rest>.+)$')
DOC_SPLIT = re.compile(r'^(?P<cost>.+?)(?:。|，)\s*(?P<eff>.*)$')
COST_PREFIX = ("代价：", "消耗", "冷却", "流血", "疲惫", "异变", "衰老", "枯竭", "萎缩", "失忆")

# 代价类型 → (代价字段, summary 开头该怎么写)
COST_FIELD = {"消耗": "cost", "冷却": "cost", "异变": "cost_mutation", "流血": "cost_hp",
              "疲惫": "cost_speed", "衰老": "cost_blood_limit"}
SUMMARY_LEAD = {
    "消耗": lambda r: f"消耗{r['cost']}法力",
    "冷却": lambda r: f"冷却{r['cost']}场",
    "异变": lambda r: f"异变+{r['cost_mutation']}",
    "流血": lambda r: f"流血{r['cost_hp']}",
    "疲惫": lambda r: f"疲惫{r['cost_speed']}",
    "衰老": lambda r: f"衰老{r['cost_blood_limit']}",
    "假碎片": lambda r: f"消耗{r['fake_cost']}假碎片",
    "碎片": lambda r: f"消耗{r['fake_cost']}假碎片或{r['real_cost']}碎片（局外×2）",
}
ALL_NAMES = sorted(DaoWenEngine._registry)


def _resolve(name, x, y=1):
    return DaoWenEngine.resolve(name, x, **({"y": y} if name == "分裂" else {}))


def _docstring_head(name):
    """返回 (参数头, 代价短语)；代价短语已去掉「代价：」前缀。"""
    first = (DaoWenEngine._registry[name].__doc__ or "").strip().splitlines()[0]
    m = DOC_HEAD.match(first)
    assert m, f"{name}: docstring 首行无法解析: {first!r}"
    rest = m.group("rest")
    cm = DOC_SPLIT.match(rest)
    phrase = (cm.group("cost") if cm else rest).strip()
    assert phrase.startswith(COST_PREFIX), f"{name}: 代价短语无法识别: {phrase!r}"
    if phrase.startswith("代价："):
        phrase = phrase[len("代价："):]
    return m.group("head"), phrase


def _expected_tokens(name, r):
    """引擎口径的「每X倍率」数字序列（顺序与 docstring 里出现顺序一致）。"""
    ct = r["cost_type"]
    if ct == "假碎片":
        return [r["fake_cost"]]
    if ct == "碎片":
        return [r["fake_cost"], r["real_cost"]]
    return [r[COST_FIELD[ct]]]


# ========================================================================
# 正常：summary 的代价开头 == 引擎代价字段（P1 的 35 条就在这里漂的）
# ========================================================================

@pytest.mark.parametrize("x", [1, 3])
def test_summary_cost_prefix_matches_engine_fields(x):
    bad = []
    for name in ALL_NAMES:
        r = _resolve(name, x)
        lead = SUMMARY_LEAD[r["cost_type"]](r)
        if not r["summary"].startswith(lead):
            bad.append(f"{name}(X={x}): summary 开头 {r['summary'][:18]!r} ≠ 引擎 {lead!r}")
    assert not bad, "summary 的代价数字与引擎 cost 字段不符：\n" + "\n".join(bad)


def test_summary_present_for_every_daowen():
    empty = [n for n in ALL_NAMES if not _resolve(n, 1).get("summary", "").strip()]
    assert not empty, f"这些道纹没有 summary：{empty}"


# ========================================================================
# 正常：docstring 的代价倍率 == 引擎代价字段
# ========================================================================

def test_docstring_cost_ratio_matches_engine_fields():
    bad = []
    for name in ALL_NAMES:
        head, phrase = _docstring_head(name)
        x = 3
        r = _resolve(name, x, y=2)
        expected = _expected_tokens(name, r)
        if name == "分裂":
            # 双参数道纹：docstring 写「衰老X×10Y」，数值随 x、y 双轴变化，单独钉
            assert head == "X/Y", f"分裂的参数头应为 X/Y，实为 {head}"
            assert phrase == "衰老X×10Y", f"分裂的代价短语漂移：{phrase}"
            assert r["cost_blood_limit"] == x * 10 * 2
            continue
        if head != "X":
            bad.append(f"{name}: 参数头应为 X，实为 {head}")
        written = [int(t or 1) for t in re.findall(r'(\d*)X', phrase)]
        per_x = [v // x for v in expected]
        if written != per_x:
            bad.append(f"{name}: docstring 写 {written}（每X），引擎是 {per_x} → {phrase!r}")
    assert not bad, "docstring 的代价倍率与引擎不符：\n" + "\n".join(bad)


def test_fenlie_cost_scales_on_both_axes():
    for x, y in [(1, 1), (2, 3), (4, 1)]:
        r = _resolve("分裂", x, y=y)
        assert r["cost_blood_limit"] == x * 10 * y
        assert r["split_clones"] == x and r["clone_hp"] == 10 * y
        assert r["summary"].startswith(f"衰老{x * 10 * y}")


# ========================================================================
# 正常：全道纹索引.md 的代价列/小节正文 == docstring
# ========================================================================

def test_index_cost_matches_docstring():
    text = (ROOT / "全道纹索引.md").read_text(encoding="utf-8")
    phrase_of = {n: _docstring_head(n)[1] for n in ALL_NAMES}
    head_of = {n: _docstring_head(n)[0] for n in ALL_NAMES}
    bad = []

    # 总览表：| 道纹 | 分类 | 代价 | [目标] | 残韵变化（出） | 承载怪物 |
    seen_rows = set()
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 3 and cells[0] in phrase_of:
            seen_rows.add(cells[0])
            want = phrase_of[cells[0]]
            if cells[2] != want and cells[2] != "代价：" + want:
                bad.append(f"总览表 {cells[0]}: 代价列 {cells[2]!r} ≠ docstring {want!r}")
    missing = set(phrase_of) - seen_rows
    assert not missing, f"总览表缺这些道纹：{sorted(missing)}"

    # 小节正文：### 名字 的下一行是「X：代价。效果」
    lines = text.splitlines()
    seen_sections = set()
    for i, line in enumerate(lines):
        if not line.startswith("### "):
            continue
        name = line[4:].strip()
        if name not in phrase_of:
            continue
        seen_sections.add(name)
        body = lines[i + 1] if i + 1 < len(lines) else ""
        want_head = head_of[name]
        if not body.startswith(f"{want_head}："):
            bad.append(f"小节 {name}: 正文应以 {want_head}： 开头，实为 {body[:24]!r}")
            continue
        cost_part = body[len(want_head) + 1:].split("。")[0]
        if cost_part != phrase_of[name] and cost_part != "代价：" + phrase_of[name]:
            bad.append(f"小节 {name}: 正文代价 {cost_part!r} ≠ docstring {phrase_of[name]!r}")
    assert seen_sections == set(phrase_of), (
        f"索引小节缺失：{sorted(set(phrase_of) - seen_sections)}")
    assert not bad, "全道纹索引.md 的代价与引擎 docstring 不符：\n" + "\n".join(bad)


def test_index_is_regeneration_byte_identical(tmp_path):
    """索引是派生产物：重跑生成器必须逐字节还原仓库里的文件。"""
    out = tmp_path / "全道纹索引.md"
    env = {**os.environ, "DAOWEN_INDEX_OUT": str(out)}
    proc = subprocess.run([sys.executable, str(ROOT / "sim" / "gen_daowen_index.py")],
                          cwd=str(ROOT), env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert out.read_text(encoding="utf-8") == (ROOT / "全道纹索引.md").read_text(encoding="utf-8"), (
        "全道纹索引.md 与重生成结果不一致：改了道纹数据请重跑 "
        "`python sim/gen_daowen_index.py`，不要手改索引")


# ========================================================================
# 正常：规则正文与副本文档里的代价数字 == 引擎
# ========================================================================

NAME_X = re.compile("(" + "|".join(map(re.escape, ALL_NAMES)) + r")X")
COST_TOKEN = re.compile(r'(消耗|冷却|流血|疲惫|异变|衰老)\s*(\d*)\s*X')
UNIT_OF_TYPE = {"消耗": "消耗", "冷却": "冷却", "异变": "异变", "流血": "流血",
                "疲惫": "疲惫", "衰老": "衰老", "假碎片": "消耗", "碎片": "消耗"}


def _doc_rule_files():
    files = [ROOT / "AI_EXPERIENCE.md"] + sorted((ROOT / "副本").glob("*.md"))
    assert files and all(f.exists() for f in files), "规则文档缺失"
    return files


def test_rule_docs_cost_numbers_match_engine():
    """`名字X（…消耗NX…）` 形式的代价数字必须等于引擎每X倍率。

    归属规则：代价数字归给同一行内**最近的前置**道纹名（窗口 24 字），
    这样 `狂暴X（代价：异变5X：…）→（转换）愤怒X（消耗2X：…）` 一行里的两个
    数字各归各主。日期化的历史结案记录（如「坏死设计=…（消耗5X」）不带 `名字X`
    形式，不在本条约束内——沿革留在变更记录里，正文只写现行口径。
    """
    ratio, unit = {}, {}
    for name in ALL_NAMES:
        r = _resolve(name, 1, y=1)
        ct = r["cost_type"]
        unit[name] = UNIT_OF_TYPE[ct]
        ratio[name] = _expected_tokens(name, r)[0]

    bad = []
    for path in _doc_rule_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            names = [(m.start(), m.group(1)) for m in NAME_X.finditer(line)]
            if not names:
                continue
            for cm in COST_TOKEN.finditer(line):
                owner = next((nm for pos, nm in reversed(names)
                              if pos < cm.start() and cm.start() - pos <= 24), None)
                if owner is None:
                    continue
                written_unit, n = cm.group(1), int(cm.group(2) or 1)
                if written_unit != unit[owner]:
                    bad.append(f"{path.name}:{lineno} [{owner}] 代价类型写成 {written_unit}，"
                               f"引擎是 {unit[owner]}")
                elif n != ratio[owner]:
                    bad.append(f"{path.name}:{lineno} [{owner}] 写 {written_unit}{n}X，"
                               f"引擎是 {written_unit}{ratio[owner]}X")
    assert not bad, "文档代价数字与引擎不符：\n" + "\n".join(bad)


# ========================================================================
# 错误输入：注册表里没有的代价类型不得静默通过
# ========================================================================

def test_unknown_cost_type_is_not_silently_ignored():
    known = set(SUMMARY_LEAD)
    used = {_resolve(n, 1)["cost_type"] for n in ALL_NAMES}
    assert used <= known, (
        f"出现未登记的代价类型 {sorted(used - known)}：先在 SUMMARY_LEAD/COST_FIELD "
        f"里登记口径，否则 summary 与文档的一致性无人看守")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
