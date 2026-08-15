#!/usr/bin/env python3
"""副本草案面板合规审计（只读）。

一阶已实现：预算60，道纹3/总值8（复用 audit_monsters.py 口径）。
二阶草案：乱葬岗/沉沦海 预算110，道纹5/总值15（2026-08-14 裁定：面板成本反推上调声明）。
永夜庭：固定场次预算=60×N（血族机制），特殊豁免面板审计。

面板成本 = ⌈血限/6⌉ + 2×攻击力 + 攻击次数²。
用法: python sim/audit_dungeons.py
"""
import math
import re
import sys

TARGETS = {
    "乱葬岗": {"budget": 100, "dw_count": 5, "dw_total": 15},
    "沉沦海": {"budget": 100, "dw_count": 5, "dw_total": 15},
    "永夜庭": None,  # 特殊预算机制，豁免
}
# 非普通池怪：事件boss/员工面板等，豁免面板与道纹配额审计
SPECIAL_MONSTERS = {"疫巢", "潜水员"}


def parse_monsters(path: str) -> list[dict]:
    text = open(path, encoding="utf-8").read()
    out = []
    for line in text.splitlines():
        m = re.match(r"^([\u4e00-\u9fff]+)（(\d+)×(\d+)/(\d+)，(.+)）", line)
        if not m:
            continue
        name, ac, ap, hp, dw_raw = m.groups()
        dw = {}
        for dm in re.finditer(r"([\u4e00-\u9fff]{2})(\d+)", dw_raw):
            dw[dm.group(1)] = int(dm.group(2))
        out.append({"name": name, "ac": int(ac), "ap": int(ap), "hp": int(hp), "dw": dw})
    return out


def panel_cost(hp, ap, ac):
    return math.ceil(hp / 6) + 2 * ap + ac * ac


def audit():
    total_viol = 0
    for fname, spec in TARGETS.items():
        path = f"副本/{fname}.md"
        monsters = parse_monsters(path)
        print(f"=== {fname}（{len(monsters)}只） ===")
        if spec is None:
            print("  特殊预算机制（60×场次），豁免面板审计\n")
            continue
        viol = 0
        for m in monsters:
            if m["name"] in SPECIAL_MONSTERS:
                print(f"  {m['name']:<8} （特殊面板，豁免审计）")
                continue
            cost = panel_cost(m["hp"], m["ap"], m["ac"])
            issues = []
            if cost > spec["budget"]:
                issues.append(f"面板成本{cost}>{spec['budget']}")
            if len(m["dw"]) != spec["dw_count"]:
                issues.append(f"道纹数{len(m['dw'])}≠{spec['dw_count']}")
            total = sum(m["dw"].values())
            if total != spec["dw_total"]:
                issues.append(f"道纹总值{total}≠{spec['dw_total']}")
            status = "合规" if not issues else "❌" + "；".join(issues)
            if issues:
                viol += 1
            dw_s = "+".join(f"{k}{v}" for k, v in m["dw"].items())
            print(f"  {m['name']:<8} {m['ac']}×{m['ap']}/{m['hp']:<5} {dw_s:<35} 成本{cost:<4} {status}")
        print(f"  违规 {viol}/{len(monsters)}\n")
        total_viol += viol
    print(f"总违规: {total_viol}")
    return total_viol


if __name__ == "__main__":
    sys.exit(1 if audit() else 0)
