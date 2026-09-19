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

# ---------- 闭环（顺序＝残韵闭环顺序，起点＝第一条边的 src） ----------
# 只有**引擎已实现**的四个区域有闭环数据；其余四区（永夜庭/沉沦海/荒疫古城/巴别塔）
# 的道纹尚未接入引擎，正文仍是手写设计稿，不在生成范围内。
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
