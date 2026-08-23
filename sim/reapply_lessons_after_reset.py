"""批量重学完成后执行：恢复/修订 lessons（批次进程在内存里持有旧知识副本，
其回写会覆盖磁盘上的 lesson 修订——故固化为脚本在批后幂等重放）。"""
import json

p = "data/build_knowledge.json"
k = json.load(open(p, encoding="utf-8"))
ls = k.setdefault("lessons", [])

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

json.dump(k, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("lessons now:", len(k["lessons"]))
