#!/usr/bin/env python3
"""
怪物面板合规审计工具（只读，不修改任何文件）
现行唯一口径（裁定④转正+裁定⑥预算60，README正文）：
  X法力=6X血限，2X法力=X攻击力，X²法力=X攻击次数；一阶可分配法力60（面板三围），道纹单独配额。
= panel_cost = ⌈血限/6⌉ + 2×攻击力 + 攻击次数² ≤ 60。
（历史口径"÷8/预算60"经审计36/36不可能成立且已被裁定④作废；6处削弱目标值随之作废。
  裁定⑥将预算30→60并全量重算36面板 hp=6×(60-次数²-2×攻击)，道纹串不变。）
道纹审查：数量=3 / 数量总值=8 / 同池组合唯一 / 池许可（通用核心+原始+转化+本副本专属）。
事件/雇佣面板（追求者/医生等）经裁定⑤豁免预算约束，仅列参照数值。
用法: python sim/audit_monsters.py
"""
import sys, os, math, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib.util
_spec = importlib.util.spec_from_file_location("bs", os.path.join(os.path.dirname(os.path.abspath(__file__)), "balance_sim.py"))
bs = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(bs)

# 道纹池（正文：道纹体系两闭环=通用核心14；道纹归属规则=原始7+转化19；各副本专属8）
CORE = {"杀伐","再生","庇护","固执","血债","波及","增殖","透支","贯穿","束缚","封印"}
ORIGINAL = {"狂暴","强化","疯狂","减速","必中","自愈","飞行"}
TRANSFORM = {"愤怒","自残","无神","借力","弱化","自食","兴奋","无力","迟滞","急速","加速",
             "眩晕","洞察","蒙蔽","滋养","衰败","寄生","滑翔","坠落"}
REGION_EXCLUSIVE = {
    "扭曲都市": {"变形","定型","畸变","僵化","超频","坏死","爆裂","退化"},
    "罪孽都市": {"洗劫","逼债","抵扣","清算","赎金","假钞","赌命","消灾"},
    "龙心谷":   {"加害","龙鳞","逆鳞","活血","裂变","嫁祸","背负","伤痕"},
}

def panel_cost(hp, ap, ac, hp_div):
    return math.ceil(hp / hp_div) + 2 * ap + ac * ac

def audit():
    monsters = [m for m in bs.parse_monsters() if m.get("region") in REGION_EXCLUSIVE]
    assert len(monsters) == 36, f"应解析36只一阶池怪，实{len(monsters)}"
    BUDGET = 60
    print(f"解析到36只一阶副本池怪，预算BUDGET={BUDGET}（⌈血限/6⌉+2×攻击力+攻击次数²）\n")
    print(f"{'怪物':<8}{'副本':<6}{'面板':<13}{'道纹(数量/总值)':<22}"
          f"{'成本':<6}{'判定':<8}{'道纹审查'}")
    all_viol = []
    for region in ["扭曲都市", "罪孽都市", "龙心谷"]:
        seen = {}
        for m in [x for x in monsters if x["region"] == region]:
            cost = panel_cost(m["hp"], m["ap"], m["ac"], 6)
            verdict = "合规" if cost <= BUDGET else f"超{cost-BUDGET}"
            if cost > BUDGET: all_viol.append(m["name"])
            # 道纹审查
            dws = list(m["dw"].items())
            n, total = len(dws), sum(m["dw"].values())
            issues = []

            if n != 3: issues.append(f"数量{n}≠3")
            if total != 8: issues.append(f"总值{total}≠8")
            key = (m["ac"], m["ap"], m["hp"], tuple(sorted(dws)))
            if key in seen: issues.append(f"与{seen[key]}组合重复")
            seen[key] = m["name"]
            legal = CORE | ORIGINAL | TRANSFORM | REGION_EXCLUSIVE[region]
            for d in m["dw"]:
                if d not in legal:
                    src = next((r for r, s in REGION_EXCLUSIVE.items() if d in s and r != region), None)
                    issues.append(f"【{d}】" + (f"系{src}专属" if src else "不在任何许可池"))
            dw_str = "+".join(f"{d}{v}" for d, v in dws)
            print(f"{m['name']:<8}{region:<6}{m['ac']}×{m['ap']}/{m['hp']:<7}"
                  f"{dw_str+' ('+str(n)+'/'+str(total)+')':<22}"
                  f"{cost:<6}{verdict:<8}{'；'.join(issues) if issues else '合规'}")
        print()
    print("===== 汇总 =====")
    print(f"面板违规 {len(all_viol)}/36：{all_viol if all_viol else '无，全部合规'}")

    # 事件/雇佣面板（裁定⑤：豁免预算约束，仅列参照数值）
    print("\n===== 事件/雇佣面板（裁定⑤豁免预算，仅参照） =====")
    extra = [
        ("追求者(事件怪/员工)", 8, 2, 96, "逆鳞2+活血3+固执3"),
        ("医生(员工)", 1, 1, 50, "无"),
        ("乞丐(朋友)", 2, 3, 50, "狂暴2"),
        ("岩行者(朋友)", 2, 4, 54, "背负1"),
        ("赴火者(朋友)", 3, 3, 60, "逆鳞1"),
    ]
    for name, ac, ap, hp, dw in extra:
        print(f"{name:<14}{ac}×{ap}/{hp:<6}参照成本={panel_cost(hp,ap,ac,6)} 道纹:{dw}")

if __name__ == "__main__":
    audit()
