"""微光者赏金赛模拟（骨架，2026-10-15）。

依据：设计草案/角色与任务系统.md 五点十一、五点十二裁定。
- 赏金赛自愿报名，入场费 5 碎片，胜者得 10 碎片，败者入场费不退；
- 报名者随机配对，奇数时随机一人轮空（轮空不扣费）；
- 对战中的行动与致死由个人性格决定；
- 战力以胜场衡量，前 10% 为高层。

占位说明（待替换）：
- 对战结算 `fight()` 是占位公式，并非引擎的 CombatEngine；
  后续应改为调用轮回者同一套战斗规则（草案五点七）。
- 初始碎片 `START_SHARDS` 与 性格→报名/致死的映射均为占位数值。

运行：python3 sim/linji_bounty_tournament.py [--seed 1] [--n 100] [--rounds 1]
"""
from __future__ import annotations

import argparse
import random
from dataclasses import dataclass, field

ENTRY_FEE = 5
WINNER_PRIZE = 10
START_SHARDS = 20          # 占位
TOP_SHARE = 0.10           # 高层 = 前 10%
LETHAL_CHANCE = 0.05       # 占位：下手狠的胜者致死概率（用户裁定：降低致死率）
DIMENSIONS = (
    "risk_preference", "interpersonal_tendency", "moral_baseline",
    "resource_view", "exploration_desire", "emotional_stability",
    "decision_habit", "expression_style", "reaction_pattern",
)


@dataclass
class Gwang:
    """微光者（轮回者 = 持有死者之书的微光者，本模拟不含死者之书）。"""
    cid: int
    traits: dict[str, float]          # 每维 -1.0 ~ 1.0
    stats: dict[str, int]             # 三维：血限 / 速限 / 法限（点数）
    shards: int = START_SHARDS
    wins: int = 0
    matches: int = 0
    alive: bool = True
    lethal_hits: int = 0              # 致死次数（下手狠）


@dataclass
class Ledger:
    """碎片账本：用于守恒校验。"""
    pool: int = 0                     # 赏金池（系统暂存）
    dissolved: int = 0                # 命零者身上的碎片随之消散（占位规则）


def make_population(n: int, seed: int) -> list[Gwang]:
    """按种子生成 n 个微光者：性格倾向随机，25 点属性随机分配到三维。"""
    rng = random.Random(seed)
    people = []
    for cid in range(n):
        traits = {d: round(rng.uniform(-1.0, 1.0), 3) for d in DIMENSIONS}
        # 25 点属性：1 点 = 6 血限（草案五点九所引规则的占位换算，仅用于战斗）
        pts = [0, 0, 0]
        for _ in range(25):
            pts[rng.randrange(3)] += 1
        stats = {"血限": pts[0], "速限": pts[1], "法限": pts[2]}
        people.append(Gwang(cid=cid, traits=traits, stats=stats))
    return people


def wants_to_enter(g: Gwang, rng: random.Random) -> bool:
    """报名决定：碎片不够不能报名；冒险倾向越高越愿意报名。占位公式。"""
    if g.shards < ENTRY_FEE:
        return False
    p = 0.2 + 0.6 * (g.traits["risk_preference"] + 1) / 2   # 0.2 ~ 0.8
    return rng.random() < p


def is_ruthless(g: Gwang) -> bool:
    """下手狠与否：道德底线偏利己的更可能致死。占位公式。"""
    return g.traits["moral_baseline"] < 0


def fight(a: Gwang, b: Gwang, rng: random.Random) -> tuple[Gwang, Gwang]:
    """占位对战：返回 (胜者, 败者)。速限决定先手，血限为生命，法限决定伤害。"""
    hp = {a.cid: a.stats["血限"] * 6, b.cid: b.stats["血限"] * 6}
    order = [a, b] if a.stats["速限"] >= b.stats["速限"] else [b, a]
    if a.stats["速限"] == b.stats["速限"] and rng.random() < 0.5:
        order.reverse()
    while True:
        for att in order:
            dfn = b if att is a else a
            dmg = 1 + rng.randint(0, att.stats["法限"] + 2)
            hp[dfn.cid] -= dmg
            if hp[dfn.cid] <= 0:
                return att, dfn


def run(n: int, seed: int, rounds: int) -> dict:
    rng = random.Random(seed + 1)
    people = make_population(n, seed)
    ledger = Ledger()
    initial_total = sum(g.shards for g in people)
    lethal_total = 0

    for _ in range(rounds):
        entrants = [g for g in people if g.alive and wants_to_enter(g, rng)]
        rng.shuffle(entrants)
        # 入场费进入奖池
        for g in entrants:
            g.shards -= ENTRY_FEE
            ledger.pool += ENTRY_FEE
        bye = None
        if len(entrants) % 2 == 1:
            bye = entrants.pop()              # 轮空：先退还入场费，不算参赛
            bye.shards += ENTRY_FEE
            ledger.pool -= ENTRY_FEE
        for i in range(0, len(entrants), 2):
            a, b = entrants[i], entrants[i + 1]
            a.matches += 1
            b.matches += 1
            w, l = fight(a, b, rng)
            w.wins += 1
            # 奖池中该场的 10 碎片归胜者，败者入场费留在奖池（即已由胜者领取）
            pot = ENTRY_FEE * 2
            ledger.pool -= pot
            w.shards += WINNER_PRIZE
            if is_ruthless(w) and rng.random() < LETHAL_CHANCE:
                l.alive = False
                ledger.dissolved += l.shards  # 占位：命零者碎片消散
                l.shards = 0
                w.lethal_hits += 1
                lethal_total += 1
        # 守恒：奖池应为 0（每场 10 进 10 出）；奇数轮空已退还
        assert ledger.pool == 0, f"奖池未清零：{ledger.pool}"

    # 真正的守恒校验：初始总量 = 存活者碎片 + 奖池 + 消散量
    alive_total = sum(g.shards for g in people if g.alive)
    assert initial_total == alive_total + ledger.pool + ledger.dissolved, (
        f"碎片不守恒：初始 {initial_total}，存活 {alive_total}，"
        f"奖池 {ledger.pool}，消散 {ledger.dissolved}")

    alive = [g for g in people if g.alive]
    ranked = sorted(alive, key=lambda g: (g.wins, g.shards), reverse=True)
    top_n = max(1, int(len(alive) * TOP_SHARE))
    return {
        "n": n,
        "rounds": rounds,
        "alive": len(alive),
        "lethal_total": lethal_total,
        "top": ranked[:5],
        "high_class": ranked[:top_n],
        "ranked": ranked,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--rounds", type=int, default=1)
    args = ap.parse_args()
    res = run(args.n, args.seed, args.rounds)
    print(f"人数 {res['n']}，赛程 {res['rounds']} 轮，存活 {res['alive']}，致死 {res['lethal_total']}")
    print("胜场前五：")
    for g in res["top"]:
        print(f"  #{g.cid:03d} 胜 {g.wins} 场 / 参赛 {g.matches} 场，碎片 {g.shards}，"
              f"三维 {g.stats}，致死 {g.lethal_hits} 次")
    print(f"高层（前 {int(TOP_SHARE*100)}%）人数：{len(res['high_class'])}")


if __name__ == "__main__":
    main()
