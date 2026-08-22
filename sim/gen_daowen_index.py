#!/usr/bin/env python3
"""生成 全道纹索引.md（一次性工具：道纹数据源变更后重跑即可）。

数据源（全部取当前版本引擎/文档事实，不抄旧数据）：
- 效果正文：engine/daowen.py DaoWenEngine.calculate_* 的 docstring（引擎结算口径）
- 归属分类：engine/gamedata.py（SHAFA_LOOP_DAOWEN / ORIGINAL / TRANSFORM / REGION_EXCLUSIVE / UNIMPLEMENTED）
- 残韵闭环：engine/daowen.py DaoWenEngine.CLOSED_LOOPS
- 承载怪物：副本/*.md 全部怪物面板行（怪物池 + 事件/雇佣面板）
"""
import inspect
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.daowen import DaoWenEngine, ResonanceEngine  # noqa: E402
from engine.gamedata import (  # noqa: E402
    MONSTER_TRANSFORM_DAOWEN,
    ORIGINAL_MONSTER_DAOWEN,
    REGION_EXCLUSIVE_DAOWEN,
    REGION_TIERS,
    SHAFA_LOOP_DAOWEN,
    UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN,
)
from engine.dungeons import load_dungeon_documents  # noqa: E402

DaoWenEngine.register_all()

PANEL = re.compile(r'^([\u4e00-\u9fff\w·]+)[（(](\d+)[×x](\d+)/(\d+)(?:[，,]([^)）\n]*))?[）)]')

# ---------- 采集 ----------
effects, costs, params_of = {}, {}, {}
for name, fn in DaoWenEngine._registry.items():
    doc = (fn.__doc__ or "").strip().splitlines()
    first = doc[0].strip()
    m = re.match(r'^\S+?X：(.+?)。(.*)$', first)
    assert m, f"{name}: docstring 格式无法解析: {first!r}"
    costs[name] = m.group(1)
    eff = m.group(2)
    effects[name] = (eff + "。") if eff and not eff.endswith("。") else eff
    params_of[name] = list(inspect.signature(fn).parameters)

# 承载怪物：扫描全部已实现副本文档的面板行（怪物池+事件/雇佣）
carriers = defaultdict(list)
for region, text in sorted(load_dungeon_documents().items()):
    for line in text.splitlines():
        m = PANEL.match(line.strip())
        if m and m.group(5):
            for n, v in re.findall(r'([\u4e00-\u9fff]{2})(\d+)', m.group(5)):
                if n in DaoWenEngine._registry:
                    entry = f"{m.group(1)}{v}"
                    if entry not in carriers[n]:
                        carriers[n].append(entry)

# 残韵边（出/入）
out_edges, in_edges = defaultdict(list), defaultdict(list)
for loop, edges in ResonanceEngine.CLOSED_LOOPS.items():
    for src, rtype, dst in edges:
        out_edges[src].append(f"（{rtype}）→{dst}")
        in_edges[dst].append(f"←{src}（{rtype}）")

# 目标需求
def target_label(name):
    p = params_of[name]
    if "target" in p:
        return "需显式选定"
    if name == "波及":
        return "X个（两阶段显式提交）"
    if name == "封印":
        return "X个怪物（引擎自动选定）"
    doc = effects[name]
    if "所有敌方" in doc:
        return "敌方全体"
    if re.search(r"所有|全场|场上所有", doc):
        return "全局"
    return "自身"

# 分类
CATEGORY = {}
for n in SHAFA_LOOP_DAOWEN:
    CATEGORY[n] = "shaifa"
for n in ORIGINAL_MONSTER_DAOWEN:
    CATEGORY[n] = "original"
for n in MONSTER_TRANSFORM_DAOWEN:
    CATEGORY[n] = "transform"
for region, ns in REGION_EXCLUSIVE_DAOWEN.items():
    for n in ns:
        CATEGORY[n] = region
for n, region in UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN.items():
    CATEGORY[n] = "unimpl"

assert set(CATEGORY) == set(DaoWenEngine._registry), (
    set(CATEGORY) ^ set(DaoWenEngine._registry))

# 特殊注记（引擎机制，非数值）
NOTES = {
    "波及": "目标由两阶段决策显式提交恰好X个（怪物侧prepare枚举候选，候选不足X时prepare不给出该道纹；玩家侧use_daowen的dodge_targets）。你发动的道纹对已标记目标同时生效，数值平分。",
    "消灾": "唯一允许局外发动的道纹（局外消耗×2）；重置随机数。",
    "封印": "怪物支付【异变8X】后若触发【崩解】，效果中断（统一死亡管线）。目标由引擎从存活怪物中自动选定（【波及】名额时优先标记者，余位按敌人列表补足）。",
    "分裂": "【命零】时触发；复制体无【分裂】道纹、无[碎片]奖励。",
    "尸爆": "【命零】时触发。",
    "招魂": "唤回者为[临时朋友]，[战终]消失。",
    "原初": "怪物困境时发动【原初X】可临时借用一种自身未持有的原始怪物道纹（仅借用，不获得）。",
}

def loop_chain(loop_name):
    edges = ResonanceEngine.CLOSED_LOOPS[loop_name]
    # 环：找起点（第一条边的src）并按边串联
    first = edges[0][0]
    chain = [first]
    nxt = {s: (r, d) for s, r, d in edges}
    cur = first
    for _ in range(len(edges)):
        r, d = nxt[cur]
        chain.append(f"⇄（{r}）{d}")
        cur = d
    return " ".join(chain)

# ---------- 渲染 ----------
L = []
A = L.append
A("# 全道纹索引")
A("")
A(f"本文件是**当前版本全部道纹（{len(DaoWenEngine._registry)}种）的完整索引**。道纹条目按归属分类：")
A("杀伐闭环（通用核心）11 ｜ 原始怪物道纹 7 ｜ 怪物转化道纹 19 ｜ 副本专属 4×8 ｜ 未实现 1。")
A("")
A("- **效果正文**抄自引擎 `engine/daowen.py`（`DaoWenEngine.calculate_*` 的规范文本）——**公式以引擎结算为准**。")
A("- **归属与残韵闭环**：`engine/gamedata.py` + `engine/daowen.py`（`CLOSED_LOOPS`）；与README正文的闭环图一致。")
A("- **承载怪物**：解析自 `副本/*.md` 全部面板行（12只怪物池 + 事件/雇佣面板，如「追求者」）；格式 `怪物名X`。")
A("- 冲突时：数值/结算以引擎为准，规则叙述以 README 正文为准，本索引为派生索引（与两者冲突时应重新生成本文件）。")
A("- 通用规则（自由控X、[目标]与闪避、代价结算、平分、声明、怪物道纹递增等）见 README，本文件不重复。")
A("")
A("## 目录")
A("")
A("- [总览表](#总览表)")
A("- [杀伐闭环（通用核心·11）](#杀伐闭环通用核心11)")
A("- [原始怪物道纹（7）](#原始怪物道纹7)")
A("- [怪物转化道纹（19）](#怪物转化道纹19)")
A("- [扭曲都市专属（8）](#扭曲都市专属8)")
A("- [罪孽都市专属（8）](#罪孽都市专属8)")
A("- [龙心谷专属（8）](#龙心谷专属8)")
A("- [乱葬岗专属（8）](#乱葬岗专属8)")
A("- [未实现（荒疫古城·1）](#未实现荒疫古城1)")
A("- [道纹归属与学习规则](#道纹归属与学习规则)")
A("")
A("---")
A("")

# 总览表
A("## 总览表")
A("")
A("| 道纹 | 分类 | 代价 | [目标] | 残韵变化（出） | 承载怪物 |")
A("| --- | --- | --- | --- | --- | --- |")
CAT_LABEL = {
    "shaifa": "通用核心", "original": "原始", "transform": "转化",
    "扭曲都市": "扭曲专属", "罪孽都市": "罪孽专属", "龙心谷": "龙心专属",
    "乱葬岗": "乱葬专属", "unimpl": "未实现",
}
for name in sorted(DaoWenEngine._registry, key=lambda n: (list(CATEGORY).index(n), n)):
    pass
order = (list(SHAFA_LOOP_DAOWEN) + sorted(ORIGINAL_MONSTER_DAOWEN)
         + sorted(MONSTER_TRANSFORM_DAOWEN)
         + [n for r in ("扭曲都市", "罪孽都市", "龙心谷", "乱葬岗") for n in REGION_EXCLUSIVE_DAOWEN[r]]
         + list(UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN))
for name in order:
    out = "、".join(out_edges[name]) if out_edges[name] else "—"
    c = carriers.get(name, [])
    cstr = "、".join(c[:6]) + (f"…+{len(c)-6}" if len(c) > 6 else "") or "—"
    A(f"| {name} | {CAT_LABEL[CATEGORY[name]]} | {costs[name]} | {target_label(name)} | {out} | {cstr} |")
A("")
A("> 承载怪物列按副本文档面板解析；「…」表示超过6种，逐条见下方分类小节。")
A("")
A("---")
A("")

def section(title, names, note_lines=(), loop=None, loop_title=None):
    A(f"## {title}")
    A("")
    for n in note_lines:
        A(n)
        A("")
    if loop:
        A(f"闭环路径：{loop_chain(loop)}")
        A("")
    for name in names:
        A(f"### {name}")
        A(f"X：{costs[name]}。{effects[name]}" if effects[name] else f"X：{costs[name]}。")
        meta = [f"[目标]：{target_label(name)}"]
        res = []
        if out_edges[name]:
            res.append("出：" + "、".join(out_edges[name]))
        if in_edges[name]:
            res.append("入：" + "、".join(in_edges[name]))
        if res:
            meta.append("残韵：" + "；".join(res))
        c = carriers.get(name, [])
        meta.append("承载：" + ("、".join(c) if c else "—（无怪物承载）"))
        A(f"> {' ｜ '.join(meta)}")
        if name in NOTES:
            A(f"> 注：{NOTES[name]}")
        A("")

section("杀伐闭环（通用核心·11）", list(SHAFA_LOOP_DAOWEN),
        note_lines=["开局【发现】初始道纹只从本闭环抽（随机列出3种未持有候选，显式选1；杀伐不是默认起手）。",
                    "通用核心道纹为人类侧基础概念，局外可经学习/残韵获得。"],
        loop="杀伐闭环")

section("原始怪物道纹（7）", sorted(ORIGINAL_MONSTER_DAOWEN),
        note_lines=[
            "原始怪物道纹是各转化分支的起点：**不消耗法力**，每次实际发动支付【异变5X】"
            "（X按该次发动时递增后的数值计算）；只能单向变化为转化道纹；**无法被永久获得**，"
            "只能经【原初X】临时借用（怪物困境时，借一种自身未持有的原始道纹）。",
            "怪物道纹递增（README怪物准则9）：怪物每实际发动一次某道纹，该道纹X本场累加+2×副本阶级"
            "（一阶+2、二阶+4…），递增同步放大效果与真实代价。",
        ])
A("分支结构（原始→转化，残韵单向）：")
A("")
for src in sorted(ORIGINAL_MONSTER_DAOWEN):
    ds = out_edges.get(src, [])
    A(f"- {src}：{'、'.join(ds) if ds else '—'}")
A("")

section("怪物转化道纹（19）", sorted(MONSTER_TRANSFORM_DAOWEN),
        note_lines=["转化道纹由原始怪物道纹经残韵单向变化而来：对持有原始道纹的角色发动残韵，"
                    "该道纹永久变为转化道纹，施法者同时永久获得。人类无法直接学习怪物道纹，只能经此路径。"])

for region in ("扭曲都市", "罪孽都市", "龙心谷", "乱葬岗"):
    ns = REGION_EXCLUSIVE_DAOWEN[region]
    tier = REGION_TIERS[region]
    section(f"{region}专属（8）", list(ns),
            note_lines=[f"副本阶级：{'一二三四'[tier-1]}阶（道纹递增+{2*tier}/次）。学习门禁：先经残韵从本副本怪物处"
                        f"转化获得至少一种本副本道纹，此后才可学习本副本其它专属道纹；其它副本专属道纹不可学习。"],
            loop=f"{region}闭环", loop_title=region)

section("未实现（荒疫古城·1）", list(UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN),
        note_lines=["所属副本**未接入运行时**（规则草案，见 `副本/荒疫古城.md`）：计算已实现并注册，"
                    "但局外学习按专属道纹拒绝，不进入当前事件池/怪物池。荒疫古城草案闭环中还含"
                    "感染/出土/篡改/尘封/催化/重演/原初等草案道纹，均不在当前版本引擎内。"])

A("---")
A("")
A("## 道纹归属与学习规则")
A("")
A("1. **杀伐闭环（通用核心）**：开局发现初始道纹的来源；人类侧基础概念。")
A("2. **副本专属**：学习门槛=先经残韵从本副本怪物转化获得至少一种；其它副本专属不可学。")
A("3. **怪物转化**：只能由自身已持有道纹经残韵变化获得（施法者同时获得）；原始→转化单向。")
A("4. **原始怪物**：人类不可学习；怪物发动支付异变5X；可经【原初X】临时借用。")
A("5. **角色道纹唯一**：同名道纹不重复存在；通过残韵获得的道纹X按自由控X规则自定义。")
A("6. **道纹只在战斗中发动**，唯一局外例外为【消灾】。")
A("7. **自由控X**：发动时可自由指定 1 ≤ X ≤ 当前可用法力/代价上限；【波及】的X还受合法目标数封顶"
  "（目标不足X时：怪物prepare不给出该道纹；玩家schema的X上限按目标数封顶）。")
A("")

out = ROOT / "全道纹索引.md"
out.write_text("\n".join(L) + "\n", encoding="utf-8")
print(f"written {out}: {len(L)} lines, {len(DaoWenEngine._registry)} daowen")
