"""精英冻结测试（DM实验协议·第六节）：同种子套件下逐层剥离"精英为何强"。

目的：定位瓶颈到底在 构筑 / 学习到的行为策略(learned policy) / 经验默认值(lesson-
baked defaults: 加点6/8/11、反转残韵) 中的哪一层。四个细胞、同一套360局
（3副本×120 固定种子，主种子90210），唯一变量逐格打开：

  F1  精英构筑(确认最优 封印+杀伐/透支/增殖) + 学到policy + 现默认     = 精英全貌
  F2  精英构筑 + DEFAULT_POLICY(未学的策略权重) + 现默认                − 行为学习
  F3  精英构筑 + DEFAULT_POLICY + 旧默认(10血8速7蓝, 无经验默认值)      − 经验默认值
  F4  幻影best(背负系, 单次4.83已被复测证伪) + 学到policy + 现默认      = 现学习器在扩散什么

判定：
  F1≈F4 ⇒ 幻影精英也能打 ⇒ 构筑差异不重要（传播什么无所谓）
  F1≫F4 ⇒ 学习器把50%预算花在扩散假精英 ⇒ 选择器噪声是瓶颈
  F1≈F2 ⇒ policy贡献≈0，瓶颈纯在构筑传播率
  F1>F2 ≫ F3 ⇒ 行为/经验各占多少，一看便知
"""
import random
import sys

sys.path.insert(0, "/home/user/linji-disiyuzhou")
from sim import build_learner as bl

SEEDS_MASTER = 90210
N_PER_REGION = 120
NEW_DEFAULTS = {"blood_points": 6, "speed_points": 8, "mana_points": 11}
OLD_DEFAULTS = {"blood_points": 10, "speed_points": 8, "mana_points": 7}
ELITE = ("封印", ["杀伐", "透支", "增殖"])
PHANTOM = ("封印", ["背负", "透支", "增殖"])   # KB单次4.83，复测证伪的幻影


def run_cell(starter, learn, policy, attrs):
    master = random.Random(SEEDS_MASTER)
    total = 0; b1 = 0; full = 0; invalid = 0
    per_region = {}
    for region in bl.REGIONS:
        cle = 0; n = 0; f = 0; b = 0; inv = 0
        for _ in range(N_PER_REGION):
            seed = master.randrange(1, 2 ** 31 - 1)
            r = bl.play(starter, list(learn), region, seed, rng=random.Random(seed),
                        spend_shards=True, policy=policy, attrs=attrs)
            if r.get("invalid"):
                inv += 1
                continue
            n += 1; cle += r["cleared"]; b += r["cleared"] >= 1; f += r["cleared"] == 7
        per_region[region] = (cle / max(1, n), b / max(1, n), f, inv)
        total += n; b1 += b; full += f; invalid += inv
    avg = sum(a * (n if isinstance(n, int) else 0) for a, *_ in [])  # placeholder
    gavg = sum(pr[0] * N_PER_REGION for pr in per_region.values()) / 360
    return gavg, b1 / total, full / total, invalid, per_region


def main():
    k = bl.load()
    learned = bl.learned_policy(k) or dict(bl.DEFAULT_POLICY)
    cells = [
        ("F1 精英构筑+学policy+现默认", ELITE, learned, NEW_DEFAULTS),
        ("F2 精英构筑+默认policy+现默认", ELITE, dict(bl.DEFAULT_POLICY), NEW_DEFAULTS),
        ("F3 精英构筑+默认policy+旧默认", ELITE, dict(bl.DEFAULT_POLICY), OLD_DEFAULTS),
        ("F4 幻影best+学policy+现默认", PHANTOM, learned, NEW_DEFAULTS),
    ]
    for label, (starter, learn), pol, attrs in cells:
        gavg, b1, full, invalid, pr = run_cell(starter, learn, pol, attrs)
        print(f"{label}｜场均{gavg:.2f}｜首胜{b1:.0%}｜全清{full:.1%}｜无效{invalid}")
        print("    ", {r: f"均{a:.2f}/胜{b:.0%}/清{f}" for r, (a, b, f, _) in pr.items()})


if __name__ == "__main__":
    main()
