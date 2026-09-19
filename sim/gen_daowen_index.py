#!/usr/bin/env python3
"""生成 全道纹索引.md（一次性工具：道纹数据源变更后重跑即可）。

数据源（全部取当前版本引擎/文档事实，不抄旧数据）：
- 效果正文：engine/daowen.py DaoWenEngine.calculate_* 的 docstring（引擎结算口径）
- 归属分类：engine/gamedata.py（SHAFA_LOOP_DAOWEN / ORIGINAL / TRANSFORM / REGION_EXCLUSIVE / UNIMPLEMENTED）
- 残韵闭环：engine/daowen.py DaoWenEngine.CLOSED_LOOPS
- 承载怪物：副本/*.md 全部怪物面板行（怪物池 + 事件/雇佣面板）
"""
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
from engine.monsters import _parse_daowen_field  # noqa: E402

DaoWenEngine.register_all()

# 面板格式（2026-09-16 用户令）：「名字（[血限]/[法限]/[速限]，道纹…）」——
# 与 engine/monsters.py::parse_monster_pool 同格式；道纹段复用其解析器，
# 兼容带X与不带X两种写法（不带X＝发动时自选）。
PANEL = re.compile(r'^([\u4e00-\u9fff\w·]+)[（(](\d+)/(\d+)/(\d+)(?:[，,]([^)）\n]*))?[）)]')

# ---------- 采集 ----------
# 首行格式解析、修订注记剥离、闭环链渲染、NOTES 全在共享层 `sim/daowen_doc.py`：
# 索引与副本区域正文（`sim/gen_region_daowen.py`）必须用**同一套**解析与注记，
# 否则改一条口径要同步两处——①-B 要消灭的正是这个。
from daowen_doc import NOTES, RULES, loop_chain  # noqa: E402

effects = {n: r.effect for n, r in RULES.items()}
costs = {n: r.cost for n, r in RULES.items()}
params_of = {n: r.params for n, r in RULES.items()}
x_head = {n: r.head for n, r in RULES.items()}   # 【分裂】为 "X/Y"，其余为 "X"

# 承载怪物：扫描全部已实现副本文档的面板行（怪物池+事件/雇佣）
carriers = defaultdict(list)
for region, text in sorted(load_dungeon_documents().items()):
    for line in text.splitlines():
        m = PANEL.match(line.strip())
        if m and m.group(5):
            for n, v in _parse_daowen_field(m.group(5)).items():
                if n in DaoWenEngine._registry:
                    entry = f"{m.group(1)}{v}" if v else m.group(1)
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

# ---------- 渲染 ----------
L = []
A = L.append
A("# 全道纹索引")
A("")
A(f"本文件是**当前版本全部道纹（{len(DaoWenEngine._registry)}种）的完整索引**。道纹条目按归属分类：")
A("杀伐闭环（通用核心）11 ｜ 原始怪物道纹 7 ｜ 怪物转化道纹 19 ｜ 副本专属 4×8 ｜ 未实现 1。")
A("")
A("- **效果正文**抄自引擎 `engine/daowen.py`（`DaoWenEngine.calculate_*` 的规范文本）——**公式以引擎结算为准**。")
A("- **归属与残韵闭环**：`engine/gamedata.py` + `engine/daowen.py`（`CLOSED_LOOPS`）；与规则正文的闭环图一致。")
A("- **承载怪物**：解析自 `副本/*.md` 全部面板行（12只怪物池 + 事件/雇佣面板，如「追求者」）；格式 `怪物名`（面板写死X时附X，2026-09-16 起面板不写X＝发动时自选）。")
A("- 冲突时：数值/结算以引擎为准，规则叙述以 [规则正文](AI_EXPERIENCE.md#第四宇宙规则正文) 为准，本索引为派生索引（与两者冲突时应重新生成本文件）。")
A("- 通用规则（自由控X、[目标]与闪避、代价结算、平分、声明等）见 [规则正文](AI_EXPERIENCE.md#第四宇宙规则正文)，本文件不重复。")
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
# 区域专属在 gamedata 里是 set：必须 sorted() 固定顺序，否则索引每次重跑都整段位移
order = (list(SHAFA_LOOP_DAOWEN) + sorted(ORIGINAL_MONSTER_DAOWEN)
         + sorted(MONSTER_TRANSFORM_DAOWEN)
         + [n for r in ("扭曲都市", "罪孽都市", "龙心谷", "乱葬岗") for n in sorted(REGION_EXCLUSIVE_DAOWEN[r])]
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
        head = x_head.get(name, "X")
        A(f"{head}：{costs[name]}。{effects[name]}" if effects[name] else f"{head}：{costs[name]}。")
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
            "原始怪物道纹是各转化分支的起点：**不消耗法力**，各自按正文写明的代价支付"
            "（狂暴/全力/疯狂/减速/飞行＝【异变5X】，必中＝【异变X】，自愈＝【冷却X】），怪物与轮回者同口径；"
            "与转化道纹之间是**双向**的（同种残韵可回溯）。"
            "人类无法【学习】它，永久取得的唯一路径是**两步残韵**：先对持有它的怪物发动残韵取得转化道纹，"
            "再对自身持有的该转化道纹发动同种残韵回溯（两步共2枚同种残韵；第一步后该怪物永久失去这条原始道纹）；"
            "另有【原初X】临时借用（怪物困境时，借一种自身未持有的原始道纹，仅借用不获得）。",
            "怪物面板不写X：X 由发动方按道纹语义当场自选，上限只受[法限]或代价限制"
            "（规则正文·怪物准则9）。",
        ])
A("分支结构（原始↔转化，同种残韵可回溯）：")
A("")
for src in sorted(ORIGINAL_MONSTER_DAOWEN):
    ds = out_edges.get(src, [])
    A(f"- {src}：{'、'.join(ds) if ds else '—'}")
A("")
A("每条箭头都能原样反走：对转化道纹发动**同种**残韵，它就变回原始怪物道纹（19条正向边对应19条回溯边；"
  "必中/飞行没有【曲解】产物，故也没有对应的曲解回溯边）。")
A("")

section("怪物转化道纹（19）", sorted(MONSTER_TRANSFORM_DAOWEN),
        note_lines=["转化道纹由原始怪物道纹经残韵变化而来：对持有原始道纹的角色发动残韵，"
                    "该道纹永久变为转化道纹，施法者同时永久获得。人类无法直接学习怪物道纹，只能经此路径。",
                    "转化道纹可用**同种残韵**回溯为它的原始怪物道纹（施法者对自身持有的那条发动）——"
                    "这是人类永久取得原始怪物道纹的唯一路径：两步残韵，共2枚同种残韵。"])

for region in ("扭曲都市", "罪孽都市", "龙心谷", "乱葬岗"):
    ns = REGION_EXCLUSIVE_DAOWEN[region]
    tier = REGION_TIERS[region]
    section(f"{region}专属（8）", sorted(ns),
            note_lines=[f"副本阶级：{'一二三四'[tier-1]}阶（死斗按阶级分槽封存）。学习门禁：先经残韵从本副本怪物处"
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
A("3. **怪物转化**：只能由自身已持有道纹经残韵变化获得（施法者同时获得）；原始↔转化双向，同种残韵可回溯。")
A("4. **原始怪物**：人类无法通过【学习】获得，只能从怪物身上获得——两步残韵（怪物的原始道纹→转化道纹→同种回溯），"
  "共2枚同种残韵；怪物发动只付该道纹自身代价（异变类＝异变5X，自愈＝冷却X）；另有【原初X】临时借用（门票异变5X）。")
A("5. **角色道纹唯一**：同名道纹不重复存在；通过残韵获得的道纹X按自由控X规则自定义。")
A("6. **道纹只在战斗中发动**，唯一局外例外为【消灾】。")
A("7. **自由控X**：发动时可自由指定 1 ≤ X ≤ 当前可用法力/代价上限；【波及】的X还受"
  "**场上当前角色总数**封顶（波及不能选自己，可标记目标不足X时由发动方自己把X选小；"
  "一个可标记目标都没有时怪物prepare不给出该道纹）。")
A("")

# 输出路径可用环境变量重定向：tests/test_daowen_cost_consistency.py 靠它把重生成
# 结果写进 tmp_path 再与仓库里的文件逐字节比对，从而钉住「索引＝派生产物、不得手改」。
import os
out = Path(os.environ.get("DAOWEN_INDEX_OUT") or (ROOT / "全道纹索引.md"))
out.write_text("\n".join(L) + "\n", encoding="utf-8")
print(f"written {out}: {len(L)} lines, {len(DaoWenEngine._registry)} daowen")
