#!/usr/bin/env python3
"""提示词瘦身审计（2026-08-24 DM任务第一阶段：只审计，不删除）。

盘点仓库内全部"提示词/规则"面：AI_EXPERIENCE.md 逐条、KB lessons 32 条、
其余 Markdown 文档级条目；按 DM 七分类（A底层定义/B当前有效/C历史经验/
D已证伪/E重复/F代码强制/G实验专用）打标，产出机读 JSON。
条目逐一枚举（禁止遗漏）；类别为主要标签，复合身份记入 flags。
重新生成: PYTHONPATH=. python3 sim/prompt_audit.py
输出: data/experiments/prompt_audit_2026-08-24.json
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AE = (ROOT / "AI_EXPERIENCE.md").read_text(encoding="utf-8")
KB = json.loads((ROOT / "data/build_knowledge.json").read_text(encoding="utf-8"))

items = []


def add(iid, file, loc, title, gist, cls, status="current", valid=True,
        evidence="", dup="", code="", hist=False, risk="无", flags=None, note=""):
    items.append({
        "id": iid, "file": file, "loc": loc, "title": title, "gist": gist,
        "class": cls, "status": status, "valid": valid, "evidence": evidence,
        "dup": dup, "code_enforced": code, "historical": hist,
        "delete_risk": risk, "flags": flags or [], "note": note})


F = "AI_EXPERIENCE.md"

# ---- 文件头 ----
add("AIE-000", F, "L1-4", "文件头与知识库映射", "H1《AI经验库》+映射句", "B",
    evidence="document_validation 契约", code="validate_ai_knowledge_base",
    flags=["CONTRACT"], note="H1与映射句为硬契约，不得改动")

# ---- 开发维护规则 1-22 ----
_rules_meta = [
    ("1. 游戏规则优先于架构统一", "架构服从规则的元规则", "B", "", "", ""),
    ("2. 不能把代码现状直接当成游戏规则", "区分代码现状与设计规则", "B", "E:R02+R11+R19+R22 同源('勿臆断/勿自造规则')", "", "合并簇#1"),
    ("3. 禁止为了统一强行修改特殊规则", "特殊机制可不走普通流程", "B", "E:R03+R07+R08 同源('特殊规则允许绕过统一入口')", "", "合并簇#2"),
    ("4. EffectContext 使用规则", "因果链变化才需要ctx", "B", "", "combat/effect_context.py 实现", ""),
    ("5. CombatEvent 与 EffectContext 职责分离", "发生了什么vs为什么发生", "B", "", "", ""),
    ("6. 核心战斗效果必须优先使用统一入口", "9类效果的统一入口表", "B", "", "combat.py 各入口+测试", ""),
    ("7. 特殊机制允许绕过统一入口但须说明原因", "绕过三要件+禁止混淆不统一≠错误", "B", "E:同R03", "", "合并簇#2"),
    ("8. 死亡规则", "普通死亡路径+特殊死亡五分类", "B", "E:同R03族", "", "合并簇#2"),
    ("9. Hook 顺序属于游戏规则", "顺序敏感约定清单", "B", "", "combat_hooks priority+测试", ""),
    ("10. 一个效果只能有一个正式执行入口", "防双入口重复触发", "B", "", "", ""),
    ("11. 任何直接修改核心状态的代码都必须谨慎", "改x+=前须回答五问", "B", "E:同R02族", "", "合并簇#1"),
    ("12. 测试失败时禁止直接修改测试答案", "先判实现错还是测试错", "B", "", "", ""),
    ("13. 核心战斗代码修改后必须进行语义验证", "差分验证五件套", "B", "", "", ""),
    ("14. 修改后必须保护为什么这样写", "特殊规则注释不可删", "B", "", "", ""),
    ("15. 不要扩大修改范围", "最小必要修改", "B", "E:R15+R16 同源(范围纪律)", "", "合并簇#3"),
    ("16. 禁止看到大文件就主动拆分", "长度不构成重构理由", "B", "E:同R15", "", "合并簇#3"),
    ("17. 每次新增游戏机制都必须先判断它属于哪一层", "十层归属判定", "B", "", "", ""),
    ("18. 修改已有机制时必须保留旧行为证据", "用证据判断而非新写法合理", "B", "", "", ""),
    ("19. 对特殊数值规则不要擅自推理", "未明确列两种解释等裁定", "B", "E:同R02族", "", "合并簇#1"),
    ("20. 长期维护时优先保护以下四件事", "因果来源/链/死亡管线/单入口", "B", "E:与R04/R10 原则部分重叠", "", ""),
    ("21. 提交修改后的标准汇报", "五点汇报结构", "B", "", "", ""),
    ("22. 最终总原则", "修Bug还是觉得应该这样写", "B", "E:同R02族", "", "合并簇#1"),
]
for i, (t, gist, cls, dup, code, note) in enumerate(_rules_meta, 1):
    add(f"AIE-R{i:02d}", F, f"开发维护规则{i}", t, gist, cls, dup=dup,
        code=code, note=note)

# ---- 工程准则 1-12 + 交付结构 ----
for i in range(1, 13):
    add(f"AIE-P{i:02d}", F, f"软件工程准则{i}", f"准则第{i}条",
        "工程实现与验证准则（编号1~12为测试硬契约）", "B",
        evidence="validate_ai_knowledge_base+test_ai_experience_policy",
        code="编号与关键marker被校验器强制", flags=["CONTRACT"],
        note="内容可压缩但不得改编号/缺marker")
add("AIE-P13", F, "准则6 交付结构", "固定交付(1)~(7)", "输出使用固定结构",
    "B", evidence="同上契约", code="顺序被校验器强制", flags=["CONTRACT"])

# ---- 角色扮演区块 ----
for i in range(1, 5):
    add(f"AIE-RD{i}", F, f"职责划分{i}", f"职责划分第{i}条",
        "AI/DM角色职责与中断边界", "B")
add("AIE-RP1", F, "角色性格塑造", "先射箭后画靶", "性格从实际行为归纳", "B")
add("AIE-RP2", F, "角色扮演要求", "对话/活人感/信息限制/关系+画像模板",
    "扮演五原则+9字段画像模板", "B", note="画像模板可压缩格式")

# ---- 协作规范 1-9 ----
_collab_note = {1: "中断机制", 2: "事实源", 9: "测试默认：E→与AE测试含义/推演铁律6 三处重复('脚本轮回数据一律虚假')"}
for i in range(1, 10):
    add(f"AIE-C{i:02d}", F, f"协作规范{i}", f"协作规范第{i}条",
        "AI协作规范(9小节标题为测试锚点)", "B",
        evidence="tests/test_ai_policy.py", flags=["CONTRACT"],
        dup=_collab_note.get(i, ""), note="小节标题不得改")

# ---- 行文十诫 / 推演铁律 ----
for i in range(1, 11):
    add(f"AIE-W{i:02d}", F, f"行文十诫{i}", f"十诫第{i}条",
        "写作风格硬约束", "B", evidence="校验器marker契约", flags=["CONTRACT"])
_dedup = {6: "E:'凡用脚本得到的轮回数据一律视为虚假数据'在协作规范9/测试含义/本条出现3次→合并为1处+他处引用",
          7: "含'禁止结论先行'（契约marker）"}
for i in range(1, 11):
    add(f"AIE-I{i:02d}", F, f"推演铁律{i}", f"铁律第{i}条",
        "战斗推演真实性硬约束", "B", evidence="校验器marker契约",
        flags=["CONTRACT"], dup=_dedup.get(i, ""))

# ---- 原子流水线 ----
add("AIE-PL1", F, "七步原子时序", "七步流水线", "声明→插队→预响应→闪避→落地→后响应→基准检查",
    "B", code="引擎结算顺序镜像(规则9)", note="手操推演格式主协议")
add("AIE-PL2", F, "堆栈与残韵插队", "堆栈/残韵插队规则", "反应压栈+残韵永久改写+施法者同获", "B",
    dup="README残韵正文部分重叠", note="以README为事实源，此处为推演口径")
add("AIE-PL3", F, "循环法则", "循环自驱动停机", "循环四轮中断条件", "B")
for i, t in enumerate(["基础攻防", "闪避交互", "残韵插队逆转", "防御型反应法术",
                       "交错反击型法术", "循环自伤法术"], 1):
    add(f"AIE-S{i}", F, f"场景{i}", f"场景 {i}：{t}", "推演格式训练样例", "B",
        evidence="tests/test_ai_policy.py 场景名锚定", flags=["CONTRACT"],
        code="", note="标题为硬契约；场景6循环演示内容可压缩")

# ---- 知识库维护/文档分工/文档管理 ----
for i, t in enumerate(["保留内容", "删除内容", "更新时机", "追溯方式", "真实性要求"], 1):
    add(f"AIE-KM{i}", F, f"知识库维护{i}", t, "知识库清废元规则", "B",
        dup="CF-3:与本文含17段批次流水自相矛盾" if i == 2 else "",
        note="CF-3冲突当事方" if i == 2 else "")
add("AIE-DOC1", F, "文档分工与事实源", "各md事实源表", "文档主权地图", "B",
    dup="CF-5:声称'不承担实现历史归档'但本文存批次日志", note="与CF-3并案")
_dm = {2: "CF-4:与协作规范9/测试含义'只保留最新'措辞冲突", 8: "CF-4同上"}
for i in range(1, 11):
    add(f"AIE-DM{i}", F, f"文档管理规则{i}", f"管理规则第{i}条",
        "报告.md统一文档纪律", "B", dup=_dm.get(i, ""))

# ---- 当前执行与验证口径 ----
for i in range(1, 6):
    add(f"AIE-EX{i}", F, f"实现与问题报告{i}", f"实现报告{i}", "引擎事实源/Interrupt/三段式/修复删叙述", "B")
_ts = {1: "E:与协作规范9'测试默认'重复→合并", 4: "战报只留最新：CF-4当事句"}
for i in range(1, 6):
    add(f"AIE-TS{i}", F, f"测试含义{i}", f"测试含义{i}", "手操测试口径/回归命令", "B",
        dup=_ts.get(i, ""))
_mg = {8: "F: DiceEngine唯一随机源有测试锚定", 4: "F: prepare/resolve两阶段有测试锚定",
       6: "F: 显式选择无默认回退有测试锚定", 16: "F: 触发听字段有测试锚定(避风铃双测)"}
for i in range(1, 17):
    add(f"AIE-MG{i}", F, f"迁移口径{i}", f"状态机/API迁移口径{i}",
        "工程迁移防错约束(16条,措辞可压缩)", "B", code=_mg.get(i, ""),
        flags=["PROMPT_REDUNDANT_CODE_ENFORCED"] if i in _mg else [])

# ---- 当前有效的工程约束 19 ----
_ec = [
    ("七场序列1/1/1/1/2/3/4", "F", "compute_draw_count+audit+test_monster_draw"),
    ("怪物面板预算60+audit公式", "F", "audit_monsters.py 哨兵"),
    ("困境信号≥1+进化借用", "F", "_drive_plight_monsters+test_plight_drive"),
    ("崩解50+add_mutation调用方义务", "F", "models.py+测试"),
    ("活血不区分掉血来源(DM08-19)", "F", "hp_lost_this_round口径+测试"),
    ("死斗封存队列+进阶", "F", "handlers/duel.py+test_final_duel"),
    ("每轮回结束平衡观测写入报告.md", "B", "", ["遗留口头承诺，当前批次流水线已不产出此口径→候选修订"]),
    ("死之传承遗言DM审核", "B", "", []),
    ("回音长廊停下询问", "B", "", []),
    ("负岳碑/断尾预声明接口", "B", "", []),
    ("阶段门禁GameState.phase", "F", "api.py门禁+测试"),
    ("发现候选DiceEngine+显式选择", "F", "api.py+测试"),
    ("探索档位与事件队列", "F", "events.py+测试"),
    ("休整/修行/维修稳定引用", "F", "api.py+测试"),
    ("还债员工独立轨道", "F", "economy/debt+测试"),
    ("储能电池12法力口径", "F", "正文+运行时同步"),
    ("爆裂敌回终递减", "F", "combat+测试"),
    ("死斗首手四平随机", "F", "duel+测试"),
    ("每轮工作推送到 arena/01a01970 分支", "D", "", ["STALE_VALUE", "CF-1", "现会话分支01a028dc；与灾备协议§2-9构成新旧冲突"]),
]
for i, row in enumerate(_ec, 1):
    t, cls, code = row[0], row[1], row[2]
    extra = row[3] if len(row) > 3 else []
    fl = [x for x in extra if isinstance(x, str) and x[:1] in "SC" and "：" not in x]
    notes = [x for x in extra if x not in fl]
    add(f"AIE-EC{i:02d}", F, f"工程约束{i}", t, "工程约束条目", cls,
        code=code, flags=(fl + (["PROMPT_REDUNDANT_CODE_ENFORCED"] if cls == "F" else [])),
        note="；".join(notes), status=("stale" if cls == "D" else "current"))

# ---- 现行手操经验 31 ----
_hp = [
    ("血债每点伤害均被格挡吸收(核心纠正)", "B", ""),
    ("爆裂护城河combo", "B", "OVER_SPEC"),
    ("退化降维锁combo", "B", "OVER_SPEC"),
    ("避风铃闪避叠甲链", "B", "OVER_SPEC"),
    ("逆鳞蓄势反打combo", "B", "OVER_SPEC"),
    ("逼债清算破盾斩combo", "B", "OVER_SPEC"),
    ("怪物代价类道纹真实支付+冷却不复读", "B", ""),
    ("残韵对策战术(反转飞行/自愈狂暴)", "B", "与KB-L06/L15互补"),
    ("开局属性分配6~7血10~11法", "B", "E:与KB-L00/L01方向一致(手操口径)"),
    ("多怪围攻分级承伤/卖血保速", "B", "OVER_SPEC"),
    ("无神期间转守为攻", "B", "OVER_SPEC"),
    ("死斗双雄博弈准则(回血时机/破盾闪避/交替斩杀)", "B", "OVER_SPEC 3子条"),
    ("许愿裁定标准", "B", ""),
    ("怪物选招优先级(自保>输出>控制)", "B", ""),
    ("怪物道纹递增+2X/次", "B", ""),
    ("每回合至多一次庇护防凡庸", "B", "与KB-L07呼应"),
    ("轮回者初始面板0x0不普攻", "B", "OVER_SPEC"),
    ("多怪封印X=1移除威胁(无碎片)", "B", "与KB-L10呼应"),
    ("死斗低X逼闪高X终结", "B", "OVER_SPEC"),
    ("残血死斗防守蒙蔽/再生/庇护+癌变停止", "B", "OVER_SPEC"),
    ("爆裂忌满额杀伐/退化后确认X", "B", "OVER_SPEC"),
    ("折速法印收益比较/血契分担指定", "B", "OVER_SPEC"),
    ("开局流程:先遗物3选1再初始道纹", "B", "F: api门禁强制", ),
    ("开局战报必须写全候选", "B", "F: format_setup_discovery拒绝"),
    ("杀伐公式2X+旧模拟数据作废", "B", "F: 引擎公式;作废声明并入批史"),
    ("残韵永久改写+人类不得原始纹", "B", "与KB-L11呼应"),
    ("第一杯免疫癌变/癌变阈值2x血限", "B", "F: 物品索引+引擎"),
    ("净化X降异变/救赎10%血限触发", "B", "F: 引擎+测试"),
    ("残韵小纹转大纹救急技巧", "B", "OVER_SPEC"),
    ("原始怪物纹仅首次支付异变5X", "B", "F: 引擎语义"),
    ("癌变分流角色类型结算", "B", "F: 引擎+测试"),
    ("强化怪池占比静态vs样本口径", "B", "方法论"),
]
for i, (t, cls, note) in enumerate(_hp, 1):
    fl = ["OVER_SPECIFIED"] if "OVER_SPEC" in note else []
    ev = ""
    if "F:" in note:
        fl.append("PROMPT_REDUNDANT_CODE_ENFORCED")
        ev = note
        note = ""
    add(f"AIE-HP{i:02d}", F, f"手操经验{i}", t, "DM裁定/实战战术", cls,
        evidence=ev, flags=fl, note=note.replace("OVER_SPEC", "").strip())

# ---- 待裁定 ----
for i, t in enumerate(["二阶副本未接入", "爆裂反噬death_ctx口径待裁定",
                       "避风铃/守夜灯双实现禁止接线", "死斗主动法器仅玩家侧",
                       "平衡观测记录于报告.md"], 1):
    add(f"AIE-TD{i}", F, f"待裁定{i}", t, "仍有效的待办/禁区", "B",
        dup="机制迁移台账也登记避风铃双实现" if i == 3 else "")

# ---- 完整后果优先 ----
_fc = [("核心思想+ActionPreview事实源", "B", ""),
       ("风险分层表LETHAL~LOW", "F", "engine/ai_preview.py:risk_classify+ai_tactics gate"),
       ("阈值意识", "F", "ai_preview 阈值常量(_MUT_THRESHOLD)"),
       ("禁止的短视操作5条", "B", ""),
       ("允许冒险=知情风险", "B", "")]
for i, (t, cls, code) in enumerate(_fc, 1):
    add(f"AIE-FC{i}", F, f"完整后果{i}", t, "AI决策原则(2026-08-19)", cls,
        code=code, flags=["PROMPT_REDUNDANT_CODE_ENFORCED"] if cls == "F" else [])

# ---- 2026-08-21 规则变更 ----
_v = ["平分规则", "冲击改名波及", "血契描述修改", "封印改代价异变8X", "删除道纹",
      "道纹归属删除两条", "死斗规则及程序", "测试证据", "尚未迁移清单"]
for i, t in enumerate(_v, 1):
    add(f"AIE-V{i}", F, f"08-21变更{i}", t, "已落实的规则变更记录", "C",
        hist=True, evidence=" README/引擎/索引已同步",
        code="引擎+测试为新事实源", note="候选迁出至 archive（实验/变更档案）")

# ---- 残韵解锁闭环 ----
add("AIE-RZ1", F, "残韵闭环·核心规则", "学习门槛(残韵先获一种)", "副本专属道纹学习门禁", "F",
    code="README L237+api _pre_battle_xuexi+test_region_daowen_gate",
    flags=["PROMPT_REDUNDANT_CODE_ENFORCED"], note="保留一句指针即可")
add("AIE-RZ2", F, "残韵闭环·专属清单", "四副本专属道纹清单", "专属集合枚举", "F",
    code="engine/gamedata.py REGION_EXCLUSIVE_DAOWEN",
    flags=["PROMPT_REDUNDANT_CODE_ENFORCED"])
add("AIE-RZ3", F, "残韵闭环·实战策略6条", "先解锁再学闭环等", "测试期战术", "G",
    hist=True, dup="E:与KB-L06/L11/L15重复", note="阶段G+重复E→压缩为指针")

# ---- 2026-08-22 版本变更 ----
add("AIE-W1", F, "08-22变更1", "波及X不足即过滤", "波及目标数过滤", "D",
    status="stale", valid=False, hist=True,
    evidence="第十四批裁定①降X替代; tests/test_wave_target_limit 已改降X语义; README:467注记",
    note="CF-2冲突旧方;→archive/hypotheses 留痕", flags=["CF-2", "HISTORICAL_POLLUTION"])
add("AIE-W2", F, "08-22变更2", "怪物阶段失败可恢复", "token回滚可恢复", "C",
    hist=True, code="引擎契约+测试", note="修复叙述→archive")
add("AIE-W3", F, "08-22变更3", "运行时属性禁存活实体引用", "RecursionError根因治理", "C",
    hist=True, code="runtime_id口径+测试", note="修复叙述→archive")
add("AIE-W4", F, "08-22变更4", "KB三处同步更新协议", "TACTICAL_ROLES/KB重置/monster_targets 三同步", "B",
    note="持续有效的版本变更后协议，保留并压缩")
for i, t in enumerate(["卡死两小时根因治理", "同批治理三缺口", "第三批无神/漂移治理",
                       "第四批贯穿定位/胜率口径/擂主卫冕"], 5):
    add(f"AIE-W{i}", F, f"08-22补充{i-4}", t, "历史修复叙述", "C", hist=True,
        note="→archive/experiment_log")

# ---- 批次日志 17 段 ----
_batches = [
    ("B03", "第三批(08-22深夜)", "法术覆盖顽固残余根因"),
    ("B04", "第四批", "贯穿定位/胜率口径/经验入库/擂主卫冕"),
    ("B05", "第五批", "行为级经验闭环+幸存者偏差注意"),
    ("B06", "第六批", "死斗墙钟守护+duel_rounds统计修复"),
    ("B07", "第七批", "复盘接线/87%碎片(已被KB-L02证伪)/运气vs理解"),
    ("B08", "第八批", "入库边界裁定+加点扫描6/8/11+占比≠因果"),
    ("B09", "第九批", "第4-5学习位幻影/BUILD_SIZE=3/学习顺序"),
    ("B10", "第十批", "胜利路径审计/凡庸63-76%主路径"),
    ("B11", "第十一批", "胜利路径锦标赛终表+封印手术三件套+工程禁忌"),
    ("B12", "第十二批", "还债不可达根因+裁定D+KB重置"),
    ("B13", "第十三批", "设计符合性审计三零触发"),
    ("B14", "第十四批", "波及降X裁定(替代08-22过滤)/困境驱动/KB80代重学"),
    ("B15", "第十五批", "负债0费误拒族根治"),
    ("B16", "第十六批", "困境难度≈0+排名噪声best_confirmed+两伪效证伪"),
    ("B17", "第十七批", "精英扩散产线接管gen490"),
    ("B18", "第十八批", "知识产量:存量精英承载/迁移门4/4/拓扑结晶"),
    ("B19", "第十九批", "b7归因/消耗品影子/死斗不通约"),
]
for bid, t, gist in _batches:
    add(f"AIE-{bid}", F, t, t, gist, "C", hist=True,
        note="活体结论已抽取进KB lessons;正文→archive/experiment_log.md(RETRIEVABLE)",
        dup="CF-3冲突当事内容")

# ---- 灾备协议 ----
for i in range(1, 15):
    add(f"AIE-DR{i:02d}", F, f"灾备协议{i}", f"灾备协议第{i}条",
        "持久化/推送/验证/重建检测/恢复流程", "B", evidence="2026-08-24 DM强制执行",
        dup="CF-1:第2-9条与工程约束EC19旧push条款新旧关系" if i in (2, 4, 5) else "")

# ---- KB lessons ----
_kb = [
    ("B", "", "加点正反两面的正面;与手操HP09重叠(E)"),
    ("B", "", "与KB-L00合并簇#K1"),
    ("B", "", "方法论判据(占比须A/B)"),
    ("B", "E:与KB-L29互补;贯穿PvP价值在死斗台首批未证实(1/8≈池均值)", "陷阱防回潮"),
    ("B", "", "幻影维度防探索回流"),
    ("B", "", "与KB-L00合并簇#K1"),
    ("B", "", "残韵默认;与KB-L15互补"),
    ("B", "", "凡庸主路径=当前正解"),
    ("B", "", "纯憋陷阱防回潮"),
    ("C", "", "历史审计结论,已被后续批次部分推进(癌变/雕塑仍禁区)"),
    ("B", "", "当前最强清场路径+异变守门"),
    ("B", "E:与AIE-RZ3重复", "收割入口唯一性"),
    ("B", "", "裁定D后还债现口径"),
    ("B", "", "一阶禁区防回潮"),
    ("B", "", "第二优路径+难度不对称"),
    ("B", "E:与AIE-RZ3重复", "收割定向投资"),
    ("B", "", "推债节奏阈值"),
    ("F", "", "代码已实现(逐步记账共享池)+测试;陷阱级防回归", ["PROMPT_REDUNDANT_CODE_ENFORCED"]),
    ("F", "", "代码已实现(_drive_plight_monsters)+test_plight_drive", ["PROMPT_REDUNDANT_CODE_ENFORCED"]),
    ("B", "E:合并簇#K2(库成熟判据)", "复测置信"),
    ("B", "", "行为相关≠因果法条"),
    ("B", "E:合并簇#K2", "冻结实验定位"),
    ("B", "E:合并簇#K3(回注机制)", "回注须配对深挖"),
    ("B", "E:合并簇#K3", "深挖稳定器"),
    ("B", "E:合并簇#K3", "75/25最优"),
    ("B", "", "复现性陷阱(工程侧)"),
    ("B", "E:合并簇#K2", "存量精英承载"),
    ("B", "E:合并簇#K2", "迁移门口径"),
    ("B", "E:合并簇#K2", "KO失效测试:采样服从真实价值"),
    ("B", "E:与KB-L03互补", "死斗不通约"),
    ("B", "", "带药死亡误诊防范"),
    ("B", "", "影子实验判据(过程≠结局)"),
]
for i, l in enumerate(KB["lessons"]):
    cls, dup, note = _kb[i][0], _kb[i][1], _kb[i][2]
    fl = _kb[i][3] if len(_kb[i]) > 3 else []
    add(f"KB-{i:02d}", "data/build_knowledge.json:lessons", f"[{l['kind']}]{l['date']}",
        l["text"][:40], l["text"], cls, evidence=l.get("evidence", ""),
        dup=dup, note=note, flags=fl,
        risk="KB协议:技巧/心得/陷阱须带同种子配对证据;删改走lesson治理流程")

# ---- 文档级 ----
_docs = [
    ("DOC-01", "README.md", "世界观/规则正文事实源(627行)", "A", "第四宇宙底层定义唯一权威源"),
    ("DOC-02", "死者之书.md", "法术与遗言事实源", "A", "含历史遗言数据,属事实源设计"),
    ("DOC-03", "全道纹索引.md", "70道纹完整索引", "A", ""),
    ("DOC-04", "副本索引.md", "副本清单", "A", ""),
    ("DOC-05", "物品索引.md", "遗物/消耗品/法器", "A", ""),
    ("DOC-06", "故事文档.md", "叙事存档(27行)", "C", "外部改编素材,非生产提示词"),
    ("DOC-07", "报告.md", "统一过程信息文档(441行)", "C", "按设计即RETRIEVABLE;含'当前结论/暂不修改'活性段(B成分)"),
    ("DOC-08", "机制迁移台账.md", "Hook迁移工程台账(110行)", "B", "含未决项(避风铃双实现/TriggerBus保险丝),工程台账非Agent提示词"),
    ("DOC-09", "engine/README.md", "架构与API参考", "B", "代码文档"),
]
for did, f, gist, cls, note in _docs:
    add(did, f, "-", f, gist, cls, note=note)

out = {
    "audit": "prompt_slim_2026-08-24_phase1", "phase": "盘点+分类(禁止删除)",
    "total_items": len(items),
    "by_class": {c: sum(1 for it in items if it["class"] == c) for c in "ABCDEFG"},
    "items": items,
}
dst = ROOT / "data/experiments/prompt_audit_2026-08-24.json"
dst.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print("total", out["total_items"], out["by_class"])
