#!/usr/bin/env python3
"""
数值扫描：用真实对局确定"削强补弱"的修改范围。

原则：不靠推理拍数值。对每个候选参数逐档实测胜率与道纹使用率，
找出使该道纹**回归均衡带**（既不垫底也不支配）的取值区间。

被测对象（来自 3000 局实测强度排行）：
  补弱：锐利 1.79（垫底，却是起手道纹）、冲击 2.19（唯一AOE）
  削强：僵化 3.79、庇护 3.59（公式化三件套中的两件）

方法：monkeypatch 对应的 calculate_* 函数，不改引擎源码即可扫描，
      每档跑 N 局随机种子对局，统计
        - 携带该道纹的 build 胜率
        - 与对照组（原始数值）的差值
      取"使其胜率落在中位数附近"的档位作为建议范围。

用法：
    python3 sim/balance_sweep.py --games 60
    python3 sim/balance_sweep.py --games 60 --only 锐利
"""
import argparse
import importlib.util
import os
import random
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_s = importlib.util.spec_from_file_location("bl", os.path.join(ROOT, "sim", "build_learner.py"))
bl = importlib.util.module_from_spec(_s)
_s.loader.exec_module(bl)
_s2 = importlib.util.spec_from_file_location("dt", os.path.join(ROOT, "sim", "dominance_test.py"))
dt = importlib.util.module_from_spec(_s2)
_s2.loader.exec_module(dt)

from engine.daowen import DaoWenEngine as D
from engine.models import Entity
from engine.ai_tactics import TACTICAL_ROLES


def sync_ai_table(name: str, lv) -> None:
    """把被扫描的数值同步进 AI 的战术表，使 AI 按新数值权衡性价比。"""
    if name == "锐利":
        cost_k, dmg_k = lv
        TACTICAL_ROLES["锐利"]["cost"] = cost_k
        TACTICAL_ROLES["锐利"]["dmg_per_x"] = dmg_k
    elif name == "冲击":
        TACTICAL_ROLES["冲击"]["dmg_per_x"] = lv
    elif name == "僵化":
        TACTICAL_ROLES["僵化"]["cost"] = lv
    elif name == "庇护":
        TACTICAL_ROLES["庇护"]["shield_per_x"] = lv


# ---- 各道纹的参数化实现（cost 系数 / 效果系数）----

def make_ruili(cost_k: int, dmg_k: int):
    def f(x: int, target: Entity) -> dict:
        return {"dao_wen": "锐利", "x": x, "cost_type": "消耗", "cost": cost_k * x,
                "blood_limit_reduction": dmg_k * x, "hp_reduction": dmg_k * x,
                "summary": f"消耗{cost_k*x}法力，{target.name}血限与当前生命各-{dmg_k*x}"}
    return f


def make_chongji(dmg_k: int):
    def f(x: int) -> dict:
        return {"dao_wen": "冲击", "x": x, "cost_type": "消耗", "cost": x,
                "aoe_damage": dmg_k * x, "target": "all_enemies",
                "summary": f"消耗{x}法力，对所有敌方造成{dmg_k*x}点伤害"}
    return f


def make_jianghua(cost_k: int):
    def f(x: int, target: Entity) -> dict:
        return {"dao_wen": "僵化", "x": x, "cost_type": "消耗", "cost": cost_k * x,
                "attack_power_fixed": 1, "duration": x,
                "status": {"name": "僵化", "value": 1, "duration": x},
                "summary": f"消耗{cost_k*x}法力，{target.name}攻击力固定为1，持续{x}"}
    return f


def make_bihu(shield_k: int):
    def f(x: int, target: Entity) -> dict:
        return {"dao_wen": "庇护", "x": x, "cost_type": "消耗", "cost": x,
                "target_shield": shield_k * x,
                "summary": f"消耗{x}法力，{target.name}获得{shield_k*x}点格挡"}
    return f


# 扫描计划：(道纹, 参数标签, 构造器, 档位列表, 原始档)
PLANS = {
    "锐利": ("消耗系数×伤害系数", lambda p: make_ruili(*p),
             [(3, 4), (2, 4), (2, 5), (1, 4), (3, 6), (2, 6)], (3, 4)),
    "冲击": ("每点X伤害", lambda p: make_chongji(p), [1, 2, 3], 1),
    "僵化": ("消耗系数", lambda p: make_jianghua(p), [5, 8, 12, 15], 5),
    "庇护": ("每点X格挡", lambda p: make_bihu(p), [4, 3, 2], 4),
}

# 每个被测道纹配一套"必然会用到它"的 build
PROBE_BUILDS = {
    "锐利": ("杀伐", ["锐利", "庇护", "再生", "贯穿"]),
    "冲击": ("杀伐", ["冲击", "庇护", "再生", "加害"]),
    "僵化": ("杀伐", ["僵化", "庇护", "再生", "加害"]),
    "庇护": ("杀伐", ["庇护", "再生", "僵化", "加害"]),
}


def run(games: int, seed: int, only=None):
    print("=" * 84)
    print("数值扫描：每档实测胜率（随机种子、随机副本，bug局自动剔除）")
    print("=" * 84)

    summary = {}
    for name, (label, maker, levels, original) in PLANS.items():
        if only and name != only:
            continue
        st, lr = PROBE_BUILDS[name]
        orig_fn = D._registry[name]
        orig_role = dict(TACTICAL_ROLES[name])
        print(f"\n【{name}】测试 build：{st} + {'+'.join(lr)}   参数={label}")
        print(f"  {'档位':<16}{'胜率':>8}{'95%CI':>14}{'平均通关':>10}   备注")
        rows = []
        for lv in levels:
            D._registry[name] = maker(lv)
            # 关键：AI 依据 TACTICAL_ROLES 里的 cost/dmg_per_x 估算性价比。
            # 只改引擎公式而不同步这张表，AI 仍按旧认知决策，扫描结果将失真。
            sync_ai_table(name, lv)
            r = dt.evaluate(st, lr, games, random.Random(seed))
            D._registry[name] = orig_fn
            TACTICAL_ROLES[name] = dict(orig_role)
            if not r:
                continue
            tag = "  ← 现行" if lv == original else ""
            rows.append((lv, r["winrate"], r))
            print(f"  {str(lv):<16}{r['winrate']*100:>7.1f}%"
                  f"   [{r['ci'][0]*100:.0f}-{r['ci'][1]*100:.0f}%]"
                  f"{r['avg_cleared']:>10.2f}{tag}")
        summary[name] = (rows, original)

    print("\n" + "=" * 84)
    print("建议修改范围（依据：与现行档位的胜率差，取显著改善且不过头者）")
    print("=" * 84)
    for name, (rows, original) in summary.items():
        base = next((w for lv, w, _ in rows if lv == original), None)
        if base is None:
            continue
        print(f"\n【{name}】现行 {original} → 胜率 {base*100:.1f}%")
        for lv, w, r in rows:
            if lv == original:
                continue
            d = (w - base) * 100
            lo, hi = r["ci"]
            # 置信区间不重叠才算显著
            sig = "显著" if (lo > base or hi < base) else "不显著"
            arrow = "↑" if d > 0 else "↓"
            print(f"    {str(lv):<14}{w*100:>6.1f}%  ({arrow}{abs(d):.1f}pp, {sig})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=60)
    ap.add_argument("--seed", type=int, default=20260811)
    ap.add_argument("--only", default=None)
    a = ap.parse_args()
    run(a.games, a.seed, a.only)


if __name__ == "__main__":
    main()
