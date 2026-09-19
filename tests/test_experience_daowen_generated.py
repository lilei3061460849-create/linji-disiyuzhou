"""AI_EXPERIENCE.md 的三节道纹正文＝**派生产物**（①-B 第二刀：文档从引擎生成，单一真源）。

生成器 `sim/gen_experience_daowen.py` ＋共享解析层 `sim/daowen_doc.py`，事实源是
`engine/gamedata.py` 的归属集合、`engine/daowen.py` 的 docstring 首行、
`ResonanceEngine.CLOSED_LOOPS` 与 `daowen_doc.NOTES`。

这三节比副本区域那四块多一层约束：`engine/rule_sync.py` **运行时就读 AI_EXPERIENCE.md**
（`extract_daowen_from_file` 从 `### 道纹体系` 抽到 `### 特殊事件`，`events.py` 从同一文件抽
通用事件），所以「生成格式」直接决定引擎抽到几条道纹。抽取器只认两种行：

    标准行  `名X[（说明）]：代价。效果`      → 道纹体系 11 条 ＋ 净化 1 条
    内联行  `名X（含「消耗」/「代价」的说明）` → 原始/转化树 26 条（正则 `([^（）]+)`，**不容忍嵌套括号**）

合计 38 条通用道纹（`tests/test_rule_sources.py` 钉死条数与双向 diff 为空）。副本专属那 32 条
用项目符号 `- 【名】X：…`，抽取器不认——这是刻意的：区域专属道纹的事实源是《全道纹索引》与
副本正文，通用道纹才是这 38 条。

这里钉六件事：

1. **逐字节**：重跑生成器必须还原仓库里的 AI_EXPERIENCE.md（手改生成块、或改了引擎忘记重跑即红）。
2. **覆盖与顺序**：三节各自含且只含引擎归属集合里的道纹；顺序＝`SHAFA_LOOP_DAOWEN` 元组序／
   区域名字典序（与《全道纹索引》同序）／`CLOSED_LOOPS` 边序（残韵顺序有意义，不能字典序）。
3. **正文＝引擎首行**：定义行与项目符号行的正文逐字来自 docstring 首行（剥掉修订注记后）。
4. **内联行不得嵌套括号**：一层括号就会让 `rule_sync` 漏抽这条（38→37），是静默失败。
5. **正文红线**：生成块里不得出现沿革（日期水印／DM裁定／废止／旧版／原名）——沿革留在引擎
   docstring 正文段、`archive/` 与 `sim/check_rule_change.py::STALE_PHRASES`。
6. **兼容**：`rule_sync` 仍抽出那 38 条、双向 diff 为空，且区域专属 32 条**不在**其中。
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "sim") not in sys.path:
    sys.path.insert(0, str(ROOT / "sim"))

import gen_experience_daowen as gen  # noqa: E402
from daowen_doc import (  # noqa: E402
    MONSTER_BACKWARD_LOOP,
    MONSTER_FORWARD_LOOP,
    NOTES,
    REGION_ORDER,
    RULES,
    monster_groups,
)
from engine.daowen import ResonanceEngine  # noqa: E402
from engine.gamedata import (  # noqa: E402
    MONSTER_TRANSFORM_DAOWEN,
    ORIGINAL_MONSTER_DAOWEN,
    REGION_EXCLUSIVE_DAOWEN,
    SHAFA_LOOP_DAOWEN,
    UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN,
)
from engine.rule_sync import RuleSync  # noqa: E402

DOC = ROOT / "AI_EXPERIENCE.md"
TAGS = tuple(tag for tag, _ in gen.BLOCKS)
# 沿革特征：日期水印、裁定落款、废止/旧版/改名陈述
HISTORY = re.compile(r"20\d\d-\d\d-\d\d|DM\s*裁定|用户令|废止|旧版|旧口径|原名【|已删除|已并入")
STD_LINE = re.compile(r"^([\u4e00-\u9fff]{2})X(?:/Y)?(?:（[^）]*）)?：(?P<body>.+)$")
BULLET = re.compile(r"^- 【([\u4e00-\u9fff]{2})】(X(?:/Y)?)：(?P<body>.+)$")
REGION_HEAD = re.compile(r"^\*\*(.+?)\*\*$")
NOTE = re.compile(r"^\s*> 注：")


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def _block(tag: str) -> str:
    pat = re.compile(
        rf"<!-- BEGIN GENERATED {re.escape(tag)}.*?-->\n(?P<body>.*?)\n<!-- END GENERATED {re.escape(tag)} -->",
        re.S)
    m = pat.search(_text())
    assert m, f"AI_EXPERIENCE.md 里没有生成块标记（BEGIN/END GENERATED {tag}）"
    return m.group("body")


def _common_38() -> set:
    return (set(SHAFA_LOOP_DAOWEN) | ORIGINAL_MONSTER_DAOWEN | MONSTER_TRANSFORM_DAOWEN
            | set(UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN))


# ---------------------------------------------------------------- 1) 逐字节同源

def test_experience_blocks_are_regeneration_byte_identical():
    """三节与引擎同源：重跑生成器必须逐字节还原（`--check` 退出码 0）。"""
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.run([sys.executable, str(ROOT / "sim" / "gen_experience_daowen.py"), "--check"],
                          cwd=str(ROOT), env=env, capture_output=True, text=True)
    assert proc.returncode == 0, (
        "AI_EXPERIENCE.md 的三节道纹正文与引擎不同源。改了道纹口径/归属/闭环请重跑 "
        "`PYTHONPATH=. python sim/gen_experience_daowen.py`，不要手改生成块：\n"
        + proc.stdout + proc.stderr)


def test_render_is_idempotent_and_overwrites_hand_edits():
    """负向对照（本文件其余守卫不是空跑的证据）：手改生成块会被检出并被还原。"""
    text = _text()
    assert gen.render(text) == text, "render 不幂等：仓库里的三节不是生成器的输出"
    mangled = text.replace("杀伐X：消耗X。", "杀伐X：消耗2X。", 1)
    assert mangled != text, "对照失败：没改到生成块里的定义行"
    assert gen.render(mangled) == text, "生成器没能冲掉手改（守卫会失效）"


def test_block_markers_appear_exactly_once():
    """标记唯一：多处标记会让生成器只重写第一块，剩下的静默漂移。"""
    text = _text()
    for tag in TAGS:
        assert text.count(f"<!-- BEGIN GENERATED {tag}") == 1, f"{tag}: BEGIN 标记不唯一"
        assert text.count(f"<!-- END GENERATED {tag} -->") == 1, f"{tag}: END 标记不唯一"


# ---------------------------------------------------------------- 2) 覆盖与顺序

def test_shafa_block_follows_engine_tuple_order():
    """道纹体系＝杀伐闭环 11 条，顺序＝`SHAFA_LOOP_DAOWEN`（元组序是权威，不字典序）。"""
    body = _block("道纹体系")
    got = [m.group(1) for l in body.splitlines() if (m := STD_LINE.match(l))]
    assert got == list(SHAFA_LOOP_DAOWEN), f"定义行 {got} ≠ 引擎顺序 {list(SHAFA_LOOP_DAOWEN)}"
    chain = body.splitlines()[0]
    start = ResonanceEngine.CLOSED_LOOPS["杀伐闭环"][0][0]
    assert f"{start}X（起点/终点）" in chain, f"链行没标出起点 {start}"
    assert chain.rstrip().endswith(f"{start}X"), "链行没有回到起点（不成环）"
    for src, rtype, dst in ResonanceEngine.CLOSED_LOOPS["杀伐闭环"]:
        assert f"⇄（{rtype}）{dst}X" in chain, f"链行缺 {src}→({rtype})→{dst}"


def test_region_block_covers_every_region_exclusive_daowen():
    """副本专属＝四区 32 条，一条不少一条不多；区域内按名字排序（与《全道纹索引》同序）。"""
    body = _block("副本专属道纹")
    heads = [m.group(1) for l in body.splitlines() if (m := REGION_HEAD.match(l))]
    assert heads == list(REGION_ORDER), f"区域标题 {heads} ≠ {list(REGION_ORDER)}"
    per_region: dict[str, list[str]] = {r: [] for r in REGION_ORDER}
    cur = None
    for line in body.splitlines():
        if (m := REGION_HEAD.match(line)):
            cur = m.group(1)
        elif (m := BULLET.match(line)):
            assert cur, f"项目符号行出现在任何区域标题之前：{line}"
            per_region[cur].append(m.group(1))
    for region in REGION_ORDER:
        want = sorted(REGION_EXCLUSIVE_DAOWEN[region])
        assert per_region[region] == want, f"{region}: {per_region[region]} ≠ {want}"
    flat = [n for ns in per_region.values() for n in ns]
    assert len(flat) == len(set(flat)) == 32, f"区域专属应 32 条不重复，实得 {len(flat)}"
    # 降级名单已废：这 8 条此前只在「效果正文见《全道纹索引》」里挂名，现在都有正文
    assert "下列道纹为副本闭环的中间节点" not in _text(), "旧的降级名单还在（应删：32 条都已生成正文）"


def test_monster_tree_covers_every_forward_edge_in_engine_order():
    """怪物树正向＝7 组 19 条边；组序与组内边序都＝`CLOSED_LOOPS['怪物原始道纹']`。"""
    body = _block("怪物道纹树·正向")
    lines = body.splitlines()
    groups = monster_groups()
    assert list(groups) == sorted(ORIGINAL_MONSTER_DAOWEN, key=list(groups).index), "组集合与原始道纹不符"
    assert set(groups) == ORIGINAL_MONSTER_DAOWEN, f"组 {set(groups)} ≠ 原始 {ORIGINAL_MONSTER_DAOWEN}"
    # 每条边都印出来了，且带残韵类型与括号里的代价/效果
    for src, rtype, dst in ResonanceEngine.CLOSED_LOOPS[MONSTER_FORWARD_LOOP]:
        assert f"→（{rtype}）{dst}X（" in body, f"缺边 {src}→({rtype})→{dst}"
    # 组序＝引擎边序（第一次出现的行号递增）
    first = {src: next(i for i, l in enumerate(lines) if l.startswith(f"{src}X")) for src in groups}
    order = sorted(first, key=lambda s: first[s])
    assert order == list(groups), f"组序 {order} ≠ 引擎边序 {list(groups)}"
    # 净化：所属副本未接入运行时，不挂树，单独一行标准格式（rule_sync 靠它凑齐 38）
    for name, region in UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN.items():
        line = next(l for l in lines if l.startswith(f"{name}X："))
        assert region in line and "不可经局外学习直接获得" in line, f"{name} 行缺归属/学习门禁：{line}"


def test_monster_back_block_mirrors_the_backward_loop():
    """回溯块＝`CLOSED_LOOPS['怪物原始道纹回溯']` 的 19 条，逐行同序。"""
    body = _block("怪物道纹树·回溯")
    got = [l for l in body.splitlines() if l.strip()]
    want = [f"{s}X→（{r}）{d}X" for s, r, d in ResonanceEngine.CLOSED_LOOPS[MONSTER_BACKWARD_LOOP]]
    assert got == want, f"回溯边不符：\n  文档 {got[:3]}\n  引擎 {want[:3]}"
    fwd = ResonanceEngine.CLOSED_LOOPS[MONSTER_FORWARD_LOOP]
    back = ResonanceEngine.CLOSED_LOOPS[MONSTER_BACKWARD_LOOP]
    assert len(fwd) == len(back) == 19, f"正向/回溯应各 19 条，实得 {len(fwd)}/{len(back)}"
    assert {(d, r, s) for s, r, d in fwd} == {(s, r, d) for s, r, d in back}, "回溯边不是正向边的镜像"


# ---------------------------------------------------------------- 3) 正文＝引擎首行

def test_blocks_carry_engine_rule_text_verbatim():
    """标准行与项目符号行的正文＝引擎 docstring 首行（剥修订注记后），一个字都不差。"""
    for tag, pattern in (("道纹体系", STD_LINE), ("副本专属道纹", BULLET)):
        for line in _block(tag).splitlines():
            m = pattern.match(line)
            if not m or NOTE.match(line):
                continue
            name = m.group(1)
            rule = RULES[name]
            assert m.group("body") == rule.line, (
                f"{tag}·{name}: 正文与引擎首行不符\n  文档: {m.group('body')}\n  应为: {rule.line}")


def test_notes_are_published_next_to_their_daowen():
    """NOTES 里三节涉及的每条注记都真的渲染出来了（漏渲染＝口径只留在代码里，读者看不见）。"""
    published = "\n".join(_block(t) for t in TAGS)
    scope = (set(SHAFA_LOOP_DAOWEN) | ORIGINAL_MONSTER_DAOWEN | MONSTER_TRANSFORM_DAOWEN
             | {n for s in REGION_EXCLUSIVE_DAOWEN.values() for n in s}
             | set(UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN))
    for name, note in NOTES.items():
        if name not in scope:
            continue
        assert note in published, f"【{name}】的注记没进正文（NOTES 有、生成块没有）"


# ---------------------------------------------------------------- 4) 内联行不得嵌套括号

def test_tree_lines_have_no_nested_parens():
    """原始/转化用内联格式发布，`rule_sync` 的正则是 `([^（）]+)`：括号里再套括号＝这条抽不到。

    负向对照（本条存在的理由）：【眩晕】首行曾写「…立刻苏醒（伤害被格挡吃满、没实际掉血则不解除）」、
    【必中】曾写「选择[目标]（攻击与道纹通用，共用层数）时…」，生成进树行后内联正则匹配失败，
    通用道纹会从 38 掉到 36，而 `test_rule_sources.py` 只报条数不对、看不出是哪条。
    两条的机制口径已改由 `daowen_doc.NOTES` 发布（渲染成「注：」行）。
    """
    for line in _block("怪物道纹树·正向").splitlines():
        if NOTE.match(line) or not line.strip():
            continue
        assert not re.search(r"（[^）]*（", line), (
            f"内联行出现嵌套括号，rule_sync 会漏抽这条：{line}\n"
            "修：把括号里的说明挪进 sim/daowen_doc.py::NOTES，别写在 docstring 首行")


# ---------------------------------------------------------------- 5) 正文红线

def test_blocks_have_no_revision_history():
    """生成块里不得有沿革（日期水印／裁定落款／废止・旧版・改名陈述）。"""
    for tag in TAGS:
        hits = [(i + 1, l.strip()[:80]) for i, l in enumerate(_block(tag).splitlines()) if HISTORY.search(l)]
        assert not hits, f"{tag} 的生成块里出现沿革（应留在引擎 docstring/archive/废案清单）：\n" + "\n".join(
            f"  行{i}: {l}" for i, l in hits)


# ---------------------------------------------------------------- 6) rule_sync 兼容

def test_rule_sync_still_extracts_the_38_common_daowen(tmp_path):
    """引擎运行时按这份文档抽通用道纹：必须仍是 38 条、名字与引擎归属集合同一，双向 diff 为空。

    区域专属 32 条**不在**其中（项目符号格式抽取器不认，这是刻意的分工，见模块 docstring）——
    谁要把它们改成标准行，通用道纹会变成 70 条，学习门禁与 AI 候选集都会被污染。
    """
    sync = RuleSync(rule_files=[], rules_dir=str(ROOT), db_path=str(tmp_path / "sync.db"))
    names = [d["name"] for d in sync.extract_daowen_from_file(str(DOC))]
    assert len(names) == 38, f"通用道纹应抽出 38 条，实得 {len(names)}：{names}"
    assert set(names) == _common_38(), (
        f"抽取集与引擎归属集合不齐\n  多出: {sorted(set(names) - _common_38())}"
        f"\n  缺少: {sorted(_common_38() - set(names))}")
    leaked = sorted(set(names) & {n for s in REGION_EXCLUSIVE_DAOWEN.values() for n in s})
    assert not leaked, f"区域专属道纹混进了通用道纹（格式被改了？）：{leaked}"
    diff = sync.diff_project_daowen()
    assert diff["in_engine_only"] == [] and diff["in_file_only"] == [], (
        f"文档与引擎注册表不齐：引擎独有 {diff['in_engine_only']}，文档独有 {diff['in_file_only']}")


def test_three_sections_contribute_nothing_to_event_extraction(tmp_path):
    """同一个文件还被 `rule_sync.extract_events_from_file` 抽事件：三节必须对它**零贡献**。

    不钉条数（别处新增通用事件不该让本条红），钉的是「把三节整段挖掉，抽出的事件名一模一样」。
    生成块里的行都是 `- 【名】X：…`／`名X：…`／`> 注：…`，事件抽取器的三个模式都匹配不上
    （项目符号与引用号开头、名字后紧跟 X 而非冒号）。

    负向对照（本条存在的理由）：手写正文里「两者组合即满血复活：滋养（×2）＋自愈2…」这一行
    曾被抽取器第三个模式 `^([汉字·]{2,12})[：:]` 当成事件名收进去（42 条）；改成生成块后它成了
    `> 注：` 行，假事件消失（41 条）。真事件池不受影响——`engine/events.py::parse_events` 走
    `EVENT_NAMES` 白名单，从来就没收过它。
    """
    sync = RuleSync(rule_files=[], rules_dir=str(ROOT), db_path=str(tmp_path / "sync.db"))
    lines = _text().splitlines()
    head = next(i for i, l in enumerate(lines) if l.strip() == gen.SPAN_HEAD)
    tail = next(i for i, l in enumerate(lines) if l.strip() == gen.SPAN_TAIL)
    stripped = tmp_path / "stripped.md"
    stripped.write_text("\n".join(lines[:head] + [gen.SPAN_HEAD, "", gen.SPAN_TAIL] + lines[tail + 1:]),
                        encoding="utf-8")
    with_span = [e["name"] for e in sync.extract_events_from_file(str(DOC))]
    without = [e["name"] for e in sync.extract_events_from_file(str(stripped))]
    assert with_span == without, (
        f"三节正文被事件抽取器误收了：\n  多出 {[n for n in with_span if n not in without]}"
        f"\n  少了 {[n for n in without if n not in with_span]}")
    assert "两者组合即满血复活" not in with_span, "规则正文的句子又被当成事件名抽走了"
    # 健全性：抽取器本身没坏（这三个是 `### 通用事件池` 里的真事件，也在 events.py 的白名单里）
    assert {"无名冢", "过路商人", "猩红暴雨"} <= set(with_span), f"通用事件抽取本身坏了：{with_span[:8]}…"
