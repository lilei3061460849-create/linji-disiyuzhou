"""批量重学完成后执行：恢复/修订 lessons（批次进程在内存里持有旧知识副本，
其回写会覆盖磁盘上的 lesson 修订——故固化为脚本在批后幂等重放）。

2026-08-23 起新增第0步：--reset 会连 lessons 一起删掉，先按文本去重从
git HEAD 版 KB 合并回全部基线 lessons（机制裁定/技巧记录不属于 fitness
样本，按KB协议在重置后保留），再做下方的修订与新增。全程幂等可重复执行。"""
import json
import subprocess

p = "data/build_knowledge.json"
k = json.load(open(p, encoding="utf-8"))
ls = k.setdefault("lessons", [])

# 第0步：从 git HEAD 合并基线 lessons（按 text 前40字去重，幂等）
try:
    _head = json.loads(subprocess.check_output(
        ["git", "show", "HEAD:data/build_knowledge.json"]))
    _have = {l.get("text", "")[:40] for l in ls}
    for _l in _head.get("lessons", []):
        if _l.get("text", "")[:40] not in _have:
            ls.append(_l)
            _have.add(_l.get("text", "")[:40])
except Exception as _ex:  # git不可用时不阻塞后续修订
    print("baseline restore skipped:", _ex)

NEW_DEBT_LESSON = ("还债路径曾'玩家不可驱动'的判决已随DM裁定（2026-08-22）废止并重估："
    "①修复引擎余额门禁——怪物碎片类代价允许透支成负债（此前永不可达、20万+局零触发）；"
    "②裁定D——逼债'无力支付部分记为负债'；③还债触发阈值负债≥20。修复后实测："
    "收割流+逼债X≤12 在罪孽都市120局触发还债37次、转化怪立即成为参战员工。"
    "教训：零触发先审机制可达性再谈构筑；单元测试全绿≠路径存在（旧测试手搓shards=-15直调结算）。")

# 1) 覆盖旧的还债陷阱 lesson（若存在）
done = False
for l in ls:
    if l["text"].startswith("还债路径"):
        l.update({"kind": "心得", "text": NEW_DEBT_LESSON,
                  "evidence": "裁定D实装 + tests/test_debt_overdraft.py + 锦标赛还债行重测(X5:6次/119 vs X12:37次/113) 2026-08-22"})
        done = True
for l in ls:
    if "放弃'还债离场'幻想" in l["text"]:
        l["text"] = l["text"].replace(
            "罪孽经济纹真实用法=赎金抽干+逼债放血（对穷怪转血限DOT），放弃'还债离场'幻想。",
            "逼债旧血限DOT已被裁定D废止，新用法=负债化推还债（见当日心得）。")
if not done:
    ls.append({"date": "2026-08-22", "kind": "心得", "text": NEW_DEBT_LESSON,
               "evidence": "裁定D实装 + tests/test_debt_overdraft.py + 锦标赛还债行重测 2026-08-22"})

def add_once(kind, text_head, text, evidence):
    if any(l["text"].startswith(text_head) for l in ls):
        return
    ls.append({"date": "2026-08-22", "kind": kind, "text": text, "evidence": evidence})

add_once("技巧", "收割流的残韵开局=定向投资",
    "收割流的残韵开局=定向投资：想推逼债必须开局选转换（逼债←洗劫+转换），"
    "想推赎金选反转（赎金←清算+反转）；且收割型AI必须禁用通用try_resonance，否则稀缺的"
    "残韵库存被高价值敌方道纹烧掉、收割永远等不到类型（v1锦标赛两行归零的直接原因）。",
    "收割dbg探针（30局15次成功）+vt v1/v2/v3对照 2026-08-22")
add_once("陷阱", "推债节奏",
    "推债节奏：逼债寄存每回始才欠X，触发线负债20——X≤5需要4-5回始（实测119局仅6次触发，"
    "怪早死了），X≥11两回始即达线（113局37次）。小额逼债=白给；推债要用当前法力上限附近的X。",
    "裁定D实装后配对实测 A(X≤5):6/119局 vs B(X≤12):37/113局 2026-08-22")
add_once("陷阱", "法术提交的成本记账必须与引擎同口径",
    "法术提交的成本记账必须与引擎同口径：①步骤X不能按'1步=1法力'朴素模型分配"
    "（杀伐类消耗2X必超支→'提交的法力不足'无效局），要逐步 costs 精确核算并给后续"
    "步骤预留x=1基线；②同一击可同时声明多个法术，校验/结算按共享法力池逐步扣减——"
    "每个法术都按满额预算会合计超支（before先发制人抽干后after生生不息结算超支），"
    "必须钱包制按声明顺序扣。修复后法力类无效局 60局×2批 = 0新增。",
    "冒烟批配对 /tmp/bl_smoke14.log（2先发制人+1生生不息） vs smoke15（法力类0新增） 2026-08-23")
add_once("心得", "困境驱动落地：进化≈0.5次/局、逃跑为稀有兜底",
    "困境驱动落地：进化≈0.5次/局、逃跑为稀有兜底（DM裁定2026-08-23③）。"
    "怪物困境强制二选一后进化≈0.5次/局（借玩家X最高纹、门票异变X=min(预算,3)），"
    "逃跑≈0（借用池几乎从不空）——'无票/必崩解'是稀有兜底路径，日常主线是进化；"
    "玩家构筑越强怪借得越好，攻略方向=别让单一高X纹成为唯一胜点"
    "（或保证有处理借纹后的plan B）。",
    "telemetry plight 计数：smoke1 24/58局 + smoke2 52/119局 2026-08-23")
add_once("心得", "KB单次6局适应度排名噪声巨大，'最优'须复测置信",
    "KB单次6局适应度排名噪声巨大，'最优'须复测置信：fitness=6局/次的采样方差"
    "让 history 顶部被运气样本占据。2026-08-23 对照复测（每构筑同种子120局）："
    "KB分4.83【封印】背负+透支+增殖复测场均仅1.50；KB分3.67【封印】杀伐+透支+增殖"
    "复测2.21才是真实天花板。选'当前最强'做决策/汇报时，必须用多样本复测后的均值，"
    "不能直接用单次 fitness 分数。",
    "elite_measure 配对复测 4.83→1.50 / 3.67→2.21 / 3.33→1.36 (n=120) 2026-08-23")
add_once("心得", "行为相关≠因果再添两例：休整/闪避",
    "行为相关≠因果再添两例：①行为统计里休整保血avg3.80看着是神行为，"
    "同种子配对强制'缺口≥8必休整'反而 场均1.53→1.49——活下来的人才有空休整"
    "（幸存者偏差），休整不是首战胜率之因；②闪避放开(4次/7%门槛)同种子反而更差"
    "(b1 45.3%→43.0%)——过度闪避烧光速度、挤占反击资源。现行默认即局部最优。",
    "rest_ab/dodge_ab 同种子配对 n=300 2026-08-23")

json.dump(k, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("lessons now:", len(k["lessons"]))
