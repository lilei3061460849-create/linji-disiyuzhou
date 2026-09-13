"""探针：残韵去向 + 各副本通关率基线/对照。

用法：python3 sim/probe_resonance_targeting.py [每区seed数]
输出：各副本通关率、最终道纹分布、残韵转化得到的道纹 TOP。
"""
import sys, random, collections
sys.path.insert(0, '.')
from sim import build_learner as bl

def run(n=12):
    regions = ["扭曲都市", "罪孽都市", "龙心谷", "乱葬岗"]
    rows = []
    allgot = collections.Counter()
    for reg in regions:
        cleared = won = games = 0
        got = collections.Counter()
        for seed in range(900, 900 + n):
            r = bl.play("杀伐", ["再生", "庇护"], reg, seed=seed, rng=random.Random(seed))
            if r.get("invalid"):
                continue
            games += 1
            cleared += r.get("cleared", 0)
            won += 1 if r.get("won") else 0
            for d in (r.get("final_daowen") or []):
                got[d] += 1
                allgot[d] += 1
        rows.append((reg, games, cleared, won, got))
    print(f"{'副本':<8}{'局数':>5}{'累计通关':>9}{'通关/局':>9}{'胜':>4}")
    for reg, games, cleared, won, got in rows:
        avg = cleared / games if games else 0
        print(f"{reg:<8}{games:>5}{cleared:>9}{avg:>9.2f}{won:>4}")
    print("\n各副本专属道纹获取情况（残韵唯一入口）：")
    from engine.gamedata import REGION_EXCLUSIVE_DAOWEN as REX
    for reg, games, cleared, won, got in rows:
        ex = {d: c for d, c in got.items() if d in REX.get(reg, set())}
        print(f"  {reg}: {ex if ex else '（无）'}")
    return rows

if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 12)
