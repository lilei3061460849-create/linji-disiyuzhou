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

# ---- docstring 首行解析：与两个生成器**共用同一份**（共享层 `sim/daowen_doc.py`）----
# 此前这里自己抄了一份正则（注释写「与 sim/gen_daowen_index.py 同口径」），于是首行格式一放宽
# 就要同步改三处（本守卫／索引生成器／区域生成器）——①-B 抽出共享层就是为消掉这种拷贝。
if str(ROOT / "sim") not in sys.path:
    sys.path.insert(0, str(ROOT / "sim"))
from daowen_doc import (  # noqa: E402
    COST_PREFIX,
    DOC_HEAD,
    DOC_SPLIT,
    REVISION as REVISION_NOTE,
)

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
from engine.document_validation import handwritten_rule_docs

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
    """手写的规则文档；清单的唯一权威在 `engine/document_validation.py`。

    原先这里自己列了一份（只有 AI_EXPERIENCE.md ＋ 副本/*.md），于是 README.md:41
    【封印】异变X、法术索引.md:57-58【庇护X】消耗X／【再生X】消耗X 这些同样写着代价数字的
    硬层文档落在守卫之外（当时数字恰好对——靠运气不靠机器）。生成物不在内：
    `全道纹索引.md` 由 test_index_is_regeneration_byte_identical 逐字节守卫，
    `data/build_knowledge.json` 是 sim 产出的叙述性知识库（其中数字是叙述不是口径）。
    """
    files = handwritten_rule_docs(ROOT)
    assert files and all(f.exists() for f in files), "规则文档缺失"
    return files


def test_rule_docs_cost_numbers_match_engine():
    """`名字X（…消耗NX…）` 形式的代价数字必须等于引擎每X倍率。

    归属规则：代价数字归给同一行内**最近的前置**道纹名（窗口 24 字），
    这样 `狂暴X（代价：异变5X：…）→（转换）愤怒X（消耗2X：…）` 一行里的两个
    数字各归各主。日期化的历史结案记录（如「坏死设计=…（消耗5X」）不带 `名字X`
    形式，不在本条约束内——沿革留在变更记录里，正文只写现行口径。
    """
    ratio, unit, dual_param = {}, {}, set()
    for name in ALL_NAMES:
        r = _resolve(name, 1, y=1)
        ct = r["cost_type"]
        unit[name] = UNIT_OF_TYPE[ct]
        ratio[name] = _expected_tokens(name, r)[0]
        # 双参数道纹（现仅【分裂】：X=数量、Y=规模档）的代价是**两个参数的公式**
        # 「衰老X×10Y」，套不进「每X倍率」模型——按倍率比会要求写成「衰老10X」，
        # 那对 Y≠1 就是错的。这里跳过，由 test_dual_param_cost_formula_matches_engine
        # 用引擎真实计算正向钉住（跳过≠不看守）。
        if "y" in r:
            dual_param.add(name)

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
                if owner in dual_param:
                    continue
                written_unit, n = cm.group(1), int(cm.group(2) or 1)
                if written_unit != unit[owner]:
                    bad.append(f"{path.name}:{lineno} [{owner}] 代价类型写成 {written_unit}，"
                               f"引擎是 {unit[owner]}")
                elif n != ratio[owner]:
                    bad.append(f"{path.name}:{lineno} [{owner}] 写 {written_unit}{n}X，"
                               f"引擎是 {written_unit}{ratio[owner]}X")
    assert not bad, "文档代价数字与引擎不符：\n" + "\n".join(bad)


def test_dual_param_cost_formula_matches_engine():
    """双参数道纹（X/Y）的发布正文必须写公式，且公式与引擎的真实计算一致。

    上面的每X倍率守卫跳过它们（模型不适用），所以在这里正向钉住：
    正文出现「衰老X×10Y」这一公式串，且引擎在两个采样点上算出的代价等于该公式。
    将来再加双参数道纹时，这条会因为没有对应断言而**不会**自动放过——按同样写法补一条。
    """
    dual = [n for n in ALL_NAMES if "y" in _resolve(n, 1, y=1)]
    assert dual == ["分裂"], f"双参数道纹清单变了（{dual}）：请同步本条与每X倍率守卫的跳过理由"
    text = "\n".join(p.read_text(encoding="utf-8") for p in _doc_rule_files())
    assert "衰老X×10Y" in text, "分裂的代价公式没写进任何手写规则文档（正文只写现行口径＝公式本身）"
    for x, y in ((1, 1), (3, 2)):
        r = _resolve("分裂", x, y=y)
        assert r["cost_blood_limit"] == x * 10 * y, (x, y, r["cost_blood_limit"])
        assert r["clone_hp"] == 10 * y and r["split_clones"] == x


# ========================================================================
# 错误输入：注册表里没有的代价类型不得静默通过
# ========================================================================

def test_unknown_cost_type_is_not_silently_ignored():
    known = set(SUMMARY_LEAD)
    used = {_resolve(n, 1)["cost_type"] for n in ALL_NAMES}
    assert used <= known, (
        f"出现未登记的代价类型 {sorted(used - known)}：先在 SUMMARY_LEAD/COST_FIELD "
        f"里登记口径，否则 summary 与文档的一致性无人看守")



# ========================================================================
# 规则正文的单一真源：引擎 docstring（＝发布到《全道纹索引》的正文）↔ resolve summary
# ========================================================================
# 为什么要有这两条：改一条规则的**效果数字**（不是代价）时，原先只有代价有机器守卫。
# 实测（2026-09-19，把【自愈】的 25% 改成 30% 再还原）：代价守卫全绿、索引逐字节守卫全绿
# （索引只印代价与效果正文，效果正文来自 docstring，docstring 没跟着改就一起错），
# 只有 2 条行为用例失败；而规则正文「原始怪物道纹与转化道纹」节里「恢复[目标]25X%已损生命」这类
# **发布给 AI 的口径**没人看守，会静默与引擎相反。这两条把源头（引擎自己两处渲染）钉死。
# （此处按节名引用而非行号：那三节自 2026-09-19 起由 sim/gen_experience_daowen.py 生成，
#   行数会随引擎口径变化漂移，行号锚点必然过期。）

PLAIN_NUMBER = re.compile(r'\d+')
INDEX_DOC = "全道纹索引.md"
# 正文里合法、但引擎 X=1 的 summary 里不会出现的数字（例如正文举 X=3 的例子）。
# 登记名字＋理由；不要直接放宽断言。当前为空。
DOCSTRING_NUMBER_EXEMPT: dict[str, str] = {}


def _rule_line_parts(name: str) -> tuple[str, str]:
    """引擎 docstring 首行 → (代价短语, 效果正文)。与生成器同一套解析口径。"""
    doc = (DaoWenEngine._registry[name].__doc__ or "").strip()
    assert doc, f"{name}: 没有 docstring，索引无从生成"
    m = DOC_HEAD.match(doc.splitlines()[0].strip())
    assert m, f"{name}: docstring 首行无法解析: {doc.splitlines()[0]!r}"
    cm = DOC_SPLIT.match(m.group("rest"))
    return (cm.group("cost"), cm.group("eff")) if cm else (m.group("rest"), "")


def test_published_rule_numbers_are_backed_by_engine():
    """docstring 首行效果正文里的每个数字，都必须是引擎在 X=1 真的算得出来的数字。

    docstring 首行就是《全道纹索引》的效果正文（生成器逐字取用），也是人读的规则口径；
    summary 是引擎运行时自己打印的口径。两处各写各的＝同一条规则有两个真源，
    改一处忘另一处时，发布出去的正文会与引擎相反。
    """
    bad = []
    for name in ALL_NAMES:
        if name in DOCSTRING_NUMBER_EXEMPT:
            continue
        _, eff = _rule_line_parts(name)
        eff = REVISION_NOTE.sub("", eff)          # 沿革注记不是现行口径，不参与比对
        doc_nums = set(PLAIN_NUMBER.findall(eff))
        try:
            summary = _resolve(name, 1).get("summary", "")
        except Exception:                          # 需要目标/上下文的道纹：由行为用例看守
            continue
        extra = doc_nums - set(PLAIN_NUMBER.findall(summary))
        if extra:
            bad.append(f"[{name}] 正文写了 {sorted(extra)}，引擎 X=1 的 summary 里没有\n"
                       f"    正文: {eff.strip()[:76]}\n    summary: {summary[:76]}")
    assert not bad, ("规则正文的数字与引擎不符（改了结算忘了改 docstring，或反之）：\n"
                     + "\n".join(bad))


HISTORY_WATERMARK = re.compile(r'20\d\d-\d\d-\d\d|DM裁定|已废止|旧版|此前口径|原名【')
RULE_TEXT_LINE = re.compile(r'^X(?:/Y)?[：:]')


def test_generated_index_rule_text_carries_no_history():
    """《全道纹索引》是**注入 AI 提示词的发布物**：正文只写现行口径（正文红线）。

    沿革留在引擎 docstring（代码注释不受限）、archive/ 与 check_rule_change.STALE_PHRASES。
    本守卫之前实测到的真例：【逼债】的「（DM裁定D 2026-08-22：旧"否则失去2X点血限"废止）」
    因为注记不以日期开头，生成器的 REVISION 抽不出来，整段沿革被印进了索引正文
    （生成器注释还写着「渲染成 > 修订： 行」，而渲染代码从来不存在）。
    """
    lines = (ROOT / INDEX_DOC).read_text(encoding="utf-8").splitlines()
    rule_lines = [(i, l) for i, l in enumerate(lines, 1) if RULE_TEXT_LINE.match(l)]
    assert len(rule_lines) >= len(ALL_NAMES), (
        f"只认出 {len(rule_lines)} 行规则正文，道纹有 {len(ALL_NAMES)} 条——"
        f"行首格式变了？先修本守卫的 RULE_TEXT_LINE，别让它退化成扫不到任何东西")
    bad = [f"{INDEX_DOC}:{i}: {l.strip()[:88]}"
           for i, l in rule_lines if HISTORY_WATERMARK.search(l)]
    assert not bad, "发布的规则正文里混进了沿革/日期水印：\n" + "\n".join(bad)

# ========================================================================
# 发布正文与引擎「各说各话」的守卫（勾魂那一类）
# ========================================================================
# 数字对得上不代表规则是同一件事：实测【勾魂】的 docstring 首行写「使[目标]无法获得[法力]」，
# 而它自己的实现返回 mana_cost_multiplier=2（combat_parts/daowen_effect.py:761 消费）、
# summary 写「法力消耗翻倍」——数字守卫全绿（首行没有数字），索引却把 2026-08-30 的旧口径
# 发布给了 AI。本条用「措辞重叠度」兜住这一类：docstring 首行效果段与 summary 效果段
# 的字符二元组 Jaccard 低于阈值即报警。阈值分不开"换了说法"与"换了规则"
# （搏命/透支 语义相同也是 0.00），所以靠下面这张**带理由**的豁免表区分：
# 新增低重叠的道纹要么改正、要么登记理由，不许直接调阈值。
WORDING_DRIFT_THRESHOLD = 0.22
WORDING_STOP = set('使未选定目标其持续回合点获得无法所有自身本场每后前时X0123456789，。；：（）[]【】∞')
WORDING_EXEMPT: dict[str, str] = {
    "搏命": "正文「你获得X点法力」/summary「获得1点法力」：差第二人称与 X→1，语义同",
    "透支": "同搏命：正文「你获得X点法力」/summary「获得1点法力」",
    "镇尸": "正文「无法获得[回复]」/summary「无法获得回复」：只差方括号",
    "瓦解": "正文「[血限]减少10X%」/summary「血限-10%」：减少↔减号写法",
    "龙鳞": "正文「每次受到伤害-X，最低为0，持续∞」/summary「每次受伤-1(最低0)，永久」：受伤↔受到伤害、∞↔永久",
    "增殖": "正文「［目标］［血限］+X」/summary「血限+1」：全角括号与 X→1",
    "伤痕": "正文「每次失去生命后血限-X，持续∞」/summary「每次掉血后血限-1，永久」：失去生命↔掉血",
    "畸变": "正文写代数式(攻击力×攻击次数)，summary 在无目标时算成「0血限（0×0）」：同一公式的两种呈现",
    "活血": "正文「每累计失去2点生命…[回复1]；未满2点的余数在[回终]清空」/summary「每失去2HP回终回复1」：点生命↔HP，且正文比 summary 多写余数清空（实现 combat.py:1082 按 hp_lost_this_round//2，回终归零）",
    "裂变": "正文补了「每次结算的伤害＝原伤害÷X（依整数规则向上取整）」，summary 是简写；实现 combat.py:686 math.ceil(damage / xv)",
    "假钞": "正文补了「战斗中失去[碎片]时优先失去[假碎片]」，summary 是简写；实现 combat_parts/cost_payment.py::_shards_of/_lose_shards_of",
    "赎金": "正文「夺取10X碎片；若无碎片则失去X点速度」/summary「夺取 10碎片或1速度」：分号句↔或句",
    # 首行**不能**带嵌套括号：rule_sync 抽原始/转化道纹的内联正则是 `([两汉字])X（([^（）]+)）`，
    # 括号里再套一层括号这条就抽不到，通用道纹会从 38 掉到 37（test_rule_sources.py 钉死条数）。
    # 所以「攻击与道纹通用、共用层数」这层口径发布在 sim/daowen_doc.py::NOTES["必中"]（渲染成
    # 「注：」行进索引与规则正文），summary 保留它给 AI 决策用；首行只留净口径。差的就是这个括号。
    "必中": "正文「自身下X次选择[目标]时其无法闪避」/summary 多一个「（攻击/道纹共用层数）」括号："
            "首行带嵌套括号会被 rule_sync 的内联正则漏抽，该口径改由 NOTES[\"必中\"] 发布（2026-09-19）",
}


def _bigrams(s: str) -> set:
    s = "".join(ch for ch in s if ch not in WORDING_STOP)
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def test_published_rule_wording_matches_engine():
    """docstring 首行效果段（＝发布到索引的正文）与引擎 summary 效果段必须说的是同一件事。

    低重叠＝要么换了说法（登记进 WORDING_EXEMPT 并写理由），要么换了规则（改正）。
    """
    flagged = {}
    for name in ALL_NAMES:
        _, eff = _rule_line_parts(name)
        eff = REVISION_NOTE.sub("", eff).strip()
        try:
            summary = _resolve(name, 1).get("summary", "")
        except Exception:
            continue
        se = summary.split("，", 1)[1] if "，" in summary else summary
        if not eff or not se:
            continue
        a, b = _bigrams(eff), _bigrams(se)
        j = len(a & b) / max(1, len(a | b))
        if j < WORDING_DRIFT_THRESHOLD:
            flagged[name] = (j, eff, se)
    unexpected = sorted(set(flagged) - set(WORDING_EXEMPT))
    detail = "\n".join(
        f"  [{n}] 重叠 {flagged[n][0]:.2f}\n    正文: {flagged[n][1][:72]}\n    summary: {flagged[n][2][:72]}"
        for n in unexpected)
    assert not unexpected, (
        "发布正文与引擎 summary 说的不像同一件事（勾魂那一类：数字对得上、规则却是旧的）：\n"
        + detail + "\n要么按实现改正 docstring 首行，要么登记进 WORDING_EXEMPT 并写明理由。")
    assert all(v.strip() for v in WORDING_EXEMPT.values()), "豁免必须写理由"
    stale = sorted(set(WORDING_EXEMPT) - set(flagged))
    assert not stale, (
        f"这些豁免已经用不上了（重叠度已达标），请从 WORDING_EXEMPT 删掉：{stale}")



if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
