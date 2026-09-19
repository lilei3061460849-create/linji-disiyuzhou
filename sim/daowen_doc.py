#!/usr/bin/env python3
"""引擎 → 发布正文 的共享解析层（①-B：文档从引擎生成，单一真源）。

两个生成器共用这一层，免得「改一条口径要同步两处」：

- `sim/gen_daowen_index.py`  → `全道纹索引.md`（注入 AI 提示词的全量索引）
- `sim/gen_region_daowen.py` → `副本/<区域>.md` 的「道纹网络」整节（玩家读的区域正文）

事实源（全部取当前版本引擎事实，不抄旧数据）：

- 正文＝`engine/daowen.py` 的 `calculate_*` docstring **首行**（代价＋效果）；
- 闭环顺序与残韵类型＝`ResonanceEngine.CLOSED_LOOPS`（起点＝第一条边的 src）；
- 引擎机制注记＝本模块 `NOTES`（非数值口径，渲染成「注：」行）。

沿革不进正文：首行句尾带日期的修订注记一律**剥掉且不发布**（正文红线：规则正文只写
现行口径）。沿革留在三处——引擎 docstring 正文段（代码注释不受限）、`archive/`、
`sim/check_rule_change.py::STALE_PHRASES`（怕改回去的机器守卫）。

docstring 首行格式（2026-09-17 放宽，此前只认 `名字X：代价。效果`，导致
【全速X（原名【迟滞】）：…】【分裂X/Y：…】【点金X：消耗8X法力，获得X个碎片】
三条解析失败、索引无法重生成而长期漂移）：

    名字X[/Y][（原名…）]：<代价短语>[。|，]<效果>[（2026-xx-xx 修订注记）]

代价短语与效果的分隔符取「第一个出现的 。 或 ，」——所有代价短语本身都不含逗号。
"""
from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.daowen import DaoWenEngine, ResonanceEngine  # noqa: E402
from engine.gamedata import (  # noqa: E402
    MONSTER_TRANSFORM_DAOWEN,
    ORIGINAL_MONSTER_DAOWEN,
    REGION_EXCLUSIVE_DAOWEN,
    SHAFA_LOOP_DAOWEN,
    UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN,
)

DaoWenEngine.register_all()

DOC_HEAD = re.compile(r'^\S+?(?P<head>X(?:/Y)?)(?:（(?P<alias>[^）]*)）)?[：:]\s*(?P<rest>.+)$')
DOC_SPLIT = re.compile(r'^(?P<cost>.+?)(?:。|，)\s*(?P<eff>.*)$')
COST_PREFIX = ("代价：", "消耗", "冷却", "流血", "疲惫", "异变", "衰老", "枯竭", "萎缩", "失忆")
# 注记不一定以日期开头：实测【逼债】写的是「（DM裁定D 2026-08-22：旧…废止）」，
# 旧正则要求括号紧跟日期 → 抽不出来，沿革就留在了**发布出去的规则正文**里。
# 改成「句尾括号里含日期即算修订注记」，与注记的书写顺序解耦。
REVISION = re.compile(r'（(?P<note>[^）]*20\d\d-[^）]*)）\s*。?\s*$')


class Rule(NamedTuple):
    """一条道纹发布到正文的最小事实：X 头（分裂是 X/Y）、代价短语、效果句。"""

    head: str
    cost: str
    effect: str
    params: list

    @property
    def line(self) -> str:
        """`名字X：代价。效果` —— 不含名字前缀的部分（索引与副本正文都用它）。"""
        return f"{self.cost}。{self.effect}" if self.effect else f"{self.cost}。"


def _parse_rules() -> dict[str, Rule]:
    out: dict[str, Rule] = {}
    for name, fn in DaoWenEngine._registry.items():
        first = (fn.__doc__ or "").strip().splitlines()[0].strip()
        m = DOC_HEAD.match(first)
        assert m, f"{name}: docstring 首行格式无法解析: {first!r}"
        rest = m.group("rest")
        cm = DOC_SPLIT.match(rest)
        cost, eff = (cm.group("cost"), cm.group("eff")) if cm else (rest, "")
        cost = cost.strip()
        assert cost.startswith(COST_PREFIX), f"{name}: 代价短语无法识别: {cost!r}"
        rm = REVISION.search(eff)
        while rm:                      # 可能叠了多条修订注记，逐条剥
            eff = REVISION.sub("", eff, count=1)
            rm = REVISION.search(eff)
        eff = eff.strip()
        out[name] = Rule(
            head=m.group("head"),
            cost=cost,
            effect=(eff + "。") if eff and not eff.endswith("。") else eff,
            params=list(inspect.signature(fn).parameters),
        )
    return out


RULES: dict[str, Rule] = _parse_rules()

# ---------- 特殊注记（引擎机制，非数值口径） ----------
NOTES = {
    "波及": "目标由两阶段决策显式提交恰好X个（怪物侧prepare枚举候选dodge_target_options，玩家侧use_daowen的dodge_targets）；X上限＝场上当前角色总数，波及不能选自己，可标记目标不足X时由发动方自己把X选小；一个可标记目标都没有时怪物prepare不给出该道纹。你发动的道纹对**由你挂上**标记的目标同时生效（别人挂的标记不影响你自己的道纹），数值平分；[目标]选自己时你自己也保留一份、波及目标各额外得一份（自我增益类同口径，含 自食/飞行/滑翔/狂暴/必中/固执/贯穿 这组永远挂施法者的道纹）。",
    "消灾": "唯一允许局外发动的道纹（局外消耗×2）；重置随机数。",
    "封印": "玩家支付异变X，使一个显式选定的目标怪物延后X回合再入场；暂离不是死亡也不是永久离场，"
            "但仍阻塞战终；回场当回合即可发动道纹，最终命零按正常死亡与碎片流程结算。",
    "分裂": "即时结算，不挂[命零]；复制体继承本体除【分裂】外的全部道纹、无[碎片]奖励；"
            "本体是怪物→复制体进敌方，否则进[临时朋友]。双参数：X=复制体数量、Y=单个规模档"
            "（每个血限/生命=10Y，代价衰老=X×10Y 恰等于造出的总血限）。"
            "发动＝`use_daowen` 额外传 `y`（缺省1），如 {\"daowen_name\":\"分裂\",\"x\":3,\"y\":2}"
            " = 衰老60、创造3个20血限复制体。",
    "尸爆": "【命零】时触发。",
    "招魂": "唤回者为[临时朋友]，[战终]消失。",
    "缄默": "封禁面＝**由[命零]触发的效果**：尸爆的AoE与自毁（施法者因此留在场上，法力已付不退）、"
            "招魂的尸体入账（封禁期内命零的怪物不入账，此后也无尸可唤）、吞骸龙胃的吞噬窗口、"
            "焦黑发丝一类命零反应。判定口径＝全场不看阵营，且**正在命零的那名自身也计入**"
            "（否则缄默持有者自毁时封禁会在它死的那一刻失效）。"
            "不封禁：死亡本身（命零就是死亡，不是它触发的效果）、[战终]击杀奖励与[碎片]（战斗结算）、"
            "死之传承（轮回流程本身，封了会死锁）、濒死保护（撤退／负岳碑／断尾求生在命零之前结算）。",
    # 下列三条是「属性统一（攻击力=当前法力、攻击次数=当前速度）」后的语义要点：
    # 面板不再写战术，发动方必须从这里推导——尤其【变形】的蒸发规则会让自用变成自残。
    "变形": "互换的是**当前**法力与**当前**速度，超出各自上限的部分**蒸发**（总量不守恒）："
            "对「法力>速度」的目标是净损失（喝汤），对「攻力>攻次」的角色自用即自残。目标可选，不填则自身。"
            "例：敌方 20法力/3速度（速限3）→互换→速度被钳回3、法力3，即凭空失去7点法力。",
    "全力": "锁定＝[法限]，花法力不再掉攻击力；与【龙族利爪】同时在身时全力最后结算，压过×2倍率。",
    "全速": "锁定＝[速限]；因「当前速度≤[速限]」恒成立，本效果是**增益**（补满被削的速度并免疫后续减速），不是减益。",
    # 状态名是【洗劫】（`has_status("洗劫")`、帮派令[战始]发放【洗劫3】）；改名的是**道纹**
    # （洗劫→点金）。此处曾写成「状态【点金】」＝手抄注记的漂移，2026-09-19 按引擎更正。
    "点金": "状态【洗劫】及其「造成伤害时夺取等量碎片」机制**保留**，但已不由本道纹发放，"
            "只剩【帮派令】在[战始]发放（事件收益在禁区清单内，不动）。",
    "原初": "怪物困境时发动【原初X】可临时借用一种自身未持有的原始怪物道纹（仅借用，不获得）。",
}

# 以下口径 2026-09-19 从 AI_EXPERIENCE.md 的手写正文逐字搬来（①-B 第二刀：那三节要改成
# 引擎生成，文档独有的现行口径必须先在引擎侧有位置，否则生成＝裁剪式删口径）。
# 注意：搬的是「现行规则」，日期水印（如「2026-09-16 起无需学习」）按正文红线一律剥掉。
NOTES["封印"] += (
    "【镇魔印】以【封印】为步骤，声明“自身回合结束后→发动封印”；在己方主动出手阶段结束、"
    "怪物阶段开始前自动选定当前怪物（默认X=1），不占主动出手。持有【封印】道纹即具备装配"
    "【镇魔印】的资格，但须显式装配后才会自动发动；装配期间【封印】不再作为主动道纹候选——"
    "这正是该法术把一次主动出手换成回合结束免费暂离的意义。暂离期间仍计入未完成战斗，"
    "回场后按普通怪物正常结算并可产生[碎片]。"
)
NOTES["点金"] = (
    "不再与伤害挂钩：想要钱就得花法力，而[攻击力]=当前法力，花法力直接压低普攻输出。"
) + NOTES["点金"]
# 净化：归属与学习门禁直接由引擎数据派生（UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN），不手抄。
for _name, _region in UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN.items():
    NOTES[_name] = f"{_region}专属；计算已实现，不可经局外学习直接获得"
# 眩晕/必中：首行不能带嵌套括号（rule_sync 内联正则 `([^（）]+)` 抽不到），机制口径落在这里。
NOTES["眩晕"] = (
    "解除条件＝**失去生命**，不是「受到伤害」：格挡吸收与【固执】压帽之后仍有实际掉血才苏醒，"
    "被格挡吃满的一击不掉血、眩晕照旧挂着（实现在 models.py 的扣血入口）。"
)
NOTES["必中"] = "层数对**攻击与道纹通用**：普攻与道纹选目标都消耗同一叠层数，用完即失效。"
NOTES["龙鳞"] = (
    "与【加害】是一对**对称道纹**：同为消耗类同档代价、同「每次受到伤害」触发、同持续∞，"
    "只差符号——加害使受伤增加、龙鳞使受伤减少且最低为0。二者刻意保留为两条不合并："
    "加害是龙心谷闭环起点、龙鳞是它的反转位，闭环拓扑与残韵映射都按两个独立节点接线。"
)
NOTES["自愈"] = (
    "按**已损生命**计价（满血目标回复0），代价【冷却X】＝X 场战斗内不能再次发动；"
    "【坏死】【镇尸】的禁疗照常拦住它。怪物与轮回者同口径只付自身的【冷却X】，"
    "发动它不产生异变层数。"
)
NOTES["滋养"] = (
    "本身不回复任何生命，它让[目标]在持续期间**受到的每一笔恢复量翻倍**：结算点在统一回复入口，"
    "覆盖战斗内的一切来源（道纹／消耗品／寄生…），过量部分同样翻倍计入本场累计回复。"
    "它是局内状态，[战终]清除，因此不加成局外行动（【休整】的恢复量与它无关）。"
    "与【自愈】组合即满血复活：滋养（×2）＋自愈2（已损生命50%）＝已损生命100%。"
    "翻倍同样把【癌变】进度翻倍（对怪＝更快被吸收进《死者之书》且不给[碎片]，"
    "对轮回者/[朋友]/[员工]＝更快直接[命零]），能免疫癌变的有且只有遗物【第一杯】。"
)

# ---------- 闭环（顺序＝残韵闭环顺序，起点＝第一条边的 src） ----------
# 只有**引擎已实现**的四个区域有闭环数据；其余四区（永夜庭/沉沦海/荒疫古城/巴别塔）
# 的道纹尚未接入引擎，正文仍是手写设计稿，不在生成范围内。
# 区域顺序＝文档与索引的既有呈现顺序（阶级升序内的历史次序），生成时按它排。
REGION_ORDER = ("扭曲都市", "罪孽都市", "龙心谷", "乱葬岗")

REGION_LOOPS = {
    "扭曲都市": "扭曲都市闭环",
    "罪孽都市": "罪孽都市闭环",
    "龙心谷": "龙心谷闭环",
    "乱葬岗": "乱葬岗闭环",
}


def loop_nodes(loop_name: str) -> list[str]:
    """闭环上的节点顺序（去重，不含回到起点的那一段）。"""
    edges = ResonanceEngine.CLOSED_LOOPS[loop_name]
    order: list[str] = []
    for src, _rtype, _dst in edges:
        if src not in order:
            order.append(src)
    return order


def loop_start(loop_name: str) -> str:
    return ResonanceEngine.CLOSED_LOOPS[loop_name][0][0]


def loop_chain(loop_name: str, suffix: str = "") -> str:
    """渲染闭环链：`起点 ⇄（残韵类型）下一节点 …`（末段回到起点，成环）。

    `suffix` 给节点名加尾巴：索引用 `""`（只印名字），副本区域正文用 `"X"`
    （与文档既有写法一致：`点金X ⇄（转换）逼债X`）。
    """
    edges = ResonanceEngine.CLOSED_LOOPS[loop_name]
    nxt = {s: (r, d) for s, r, d in edges}
    cur = loop_start(loop_name)
    chain = [cur + suffix]
    for _ in range(len(edges)):
        r, d = nxt[cur]
        chain.append(f"⇄（{r}）{d}{suffix}")
        cur = d
    return " ".join(chain)


def def_line(name: str, index: int | None = None, start: bool = False) -> str:
    """一条道纹的定义行（副本区域正文用）：`1.点金X（起点/终点）：消耗8X法力，获得X个碎片。`"""
    rule = RULES[name]
    head = f"{name}{rule.head}"
    if start:
        head += "（起点/终点）"
    prefix = f"{index}." if index is not None else ""
    return f"{prefix}{head}：{rule.line}"


def note_line(name: str, indent: str = "   ") -> str | None:
    """该道纹的「注：」行（没有注记就返回 None）。"""
    note = NOTES.get(name)
    return f"{indent}> 注：{note}" if note else None


def region_block(region: str) -> str:
    """一个已实现区域的「道纹网络」整节（不含区域标题与 BEGIN/END 标记）。

    格式对齐 `engine/rule_sync.py::extract_daowen_from_file` 的解析口径：
    - 保留「环形闭环主轨：」「道纹定义：」两个标签（后者是它的首选切分点，
      且两者都在事件抽取的 skip_words 里，不会被误认成事件）；
    - 定义行匹配它的标准格式 `^(?:\\d+\\.)?([两汉字])X(?:（[^）]*）)?[：:](.+)$`；
    - 「注：」行缩进＋引用号开头，不会被误抽成道纹或事件。
    """
    loop = REGION_LOOPS[region]
    nodes = loop_nodes(loop)
    start = loop_start(loop)
    out = [f"环形闭环主轨：{loop_chain(loop, suffix='X').replace(start + 'X', start + 'X（起点/终点）', 1)}",
           "",
           "道纹定义："]
    for i, name in enumerate(nodes, 1):
        out.append(def_line(name, index=i, start=False))
        note = note_line(name)
        if note:
            out.append(note)
    return "\n".join(out)


# ==================== AI_EXPERIENCE.md 三节的渲染（①-B 第二刀） ====================
# 这三节（道纹体系／副本专属道纹／原始怪物道纹与转化道纹）此前是手写的，2026-09-19 改成
# 引擎生成。渲染格式**沿用文档既有写法**，唯一硬约束是：
# `engine/rule_sync.py::extract_daowen_from_file` 从这三节抽出的通用道纹必须仍是
# 那 38 条、名字与口径一条不少一条不多（tests/test_rule_sources.py 钉死 38 与双向 diff 为空）。
# 抽取器认两种行：
#   标准行 `名X[（说明）]：代价。效果`（`^(?:\d+\.)?([两汉字])X(?:/Y)?(?:（[^）]*）)?[：:](.+)$`）
#   内联行 `名X（含「消耗」或「代价」的括号说明）`（`([两汉字])X（([^（）]+)）`）
# 于是三节各自的格式是被抽取器倒推出来的，不能随手统一：
#   道纹体系     → 标准行（11 条，进 38）
#   原始/转化树  → 内联行（26 条，进 38）；**首行不得带嵌套括号**，一层括号抽取器就看不见这条
#                  （【眩晕】【必中】的机制说明因此发布在 NOTES，不写进 docstring 首行）
#   副本专属     → 项目符号 `- 【名】X：…`（32 条，**不进 38**：抽取器不认项目符号行，
#                  这是刻意的——区域专属道纹的事实源是《全道纹索引》与副本正文）
MONSTER_FORWARD_LOOP = "怪物原始道纹"
MONSTER_BACKWARD_LOOP = "怪物原始道纹回溯"


def _body(name: str) -> str:
    """`Rule.line` 去掉尾句点（内联进括号、或后面还要接括号说明时用）。"""
    body = RULES[name].line
    return body[:-1] if body.endswith("。") else body


def _inline(name: str) -> str:
    """内联行的括号内容：`代价：异变5X。效果` → `代价：异变5X：效果`（首个句点转冒号）。"""
    return _body(name).replace("。", "：", 1)


def shafa_block() -> str:
    """`### 道纹体系` 正文：杀伐闭环主轨 ＋ 11 条定义行（顺序＝SHAFA_LOOP_DAOWEN）。"""
    loop = "杀伐闭环"
    start = loop_start(loop)
    chain = loop_chain(loop, suffix="X").replace(start + "X", start + "X（起点/终点）", 1)
    out = [chain, ""]
    for name in SHAFA_LOOP_DAOWEN:
        out.append(def_line(name))
        note = note_line(name)
        if note:
            out.append(note)
    return "\n".join(out)


def region_exclusive_block() -> str:
    """`### 副本专属道纹` 正文：四个已实现区域各自的专属道纹（项目符号行，不进 38 条抽取）。

    区域内按名字排序，与《全道纹索引》的区域段同序（`gen_daowen_index.py` 用 `sorted()`），
    两份发布文档同一个顺序，读者/AI 对照时不会错位。
    """
    out: list[str] = []
    for region in REGION_ORDER:
        out += [f"**{region}**", ""]
        for name in sorted(REGION_EXCLUSIVE_DAOWEN[region]):
            rule = RULES[name]
            out.append(f"- 【{name}】{rule.head}：{rule.line}")
            note = note_line(name)
            if note:
                out.append(note)
        out.append("")
    return "\n".join(out).rstrip("\n")


def monster_groups() -> dict:
    """原始道纹 → [(残韵类型, 转化道纹)]；组序＝引擎闭环边序（这是权威顺序，不手写）。"""
    groups: dict = {}
    for src, rtype, dst in ResonanceEngine.CLOSED_LOOPS[MONSTER_FORWARD_LOOP]:
        groups.setdefault(src, []).append((rtype, dst))
    return groups


def monster_tree_block() -> str:
    """`### 原始怪物道纹与转化道纹` 的正向部分：7 组树 ＋ 组后注 ＋ 净化行。

    每组第一行把原始道纹的代价与效果也内联出来（`狂暴X（代价：异变5X：效果）→（转换）愤怒X（…）`），
    其余行只写 `原始X→（残韵）转化X（…）`——与文档既有写法逐字对齐，也保住代价数字守卫的
    24 字窗口行为（改成逐行定义会让窗口里出现的数字换位置，守卫要跟着重调，没必要）。
    """
    out: list[str] = []
    for i, (src, edges) in enumerate(monster_groups().items()):
        if i:
            out.append("")
        for j, (rtype, dst) in enumerate(edges):
            head = f"{src}X（{_inline(src)}）→" if j == 0 else f"{src}X→"
            out.append(f"{head}（{rtype}）{dst}X（{_inline(dst)}）")
        # 组后注：先原始道纹自己，再按边序给转化道纹；这里不是逐行挂载，所以带上【名】前缀。
        for name in [src] + [d for _r, d in edges]:
            if name in NOTES:
                out.append(f"> 注：【{name}】{NOTES[name]}")
    # 净化：所属副本尚未接入运行时，不挂任何树，单独一行标准格式；
    # 归属与学习门禁内联在括号里（NOTES 由 UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN 派生）。
    for name in UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN:
        out += ["", f"{name}X：{_body(name)}（{NOTES[name]}）"]
    return "\n".join(out)


def monster_back_block() -> str:
    """回溯边：19 条 `转化X→（残韵）原始X`（源＝CLOSED_LOOPS['怪物原始道纹回溯']，不反推）。"""
    edges = ResonanceEngine.CLOSED_LOOPS[MONSTER_BACKWARD_LOOP]
    return "\n".join(f"{s}X→（{r}）{d}X" for s, r, d in edges)


def monster_coverage() -> dict:
    """自检用：三节该覆盖的道纹集合（生成器与守卫都拿它核对引擎，防漏防多）。"""
    return {
        "杀伐": set(SHAFA_LOOP_DAOWEN),
        "原始": set(ORIGINAL_MONSTER_DAOWEN),
        "转化": set(MONSTER_TRANSFORM_DAOWEN),
        "区域专属": {n for s in REGION_EXCLUSIVE_DAOWEN.values() for n in s},
        "未实现专属": set(UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN),
    }
