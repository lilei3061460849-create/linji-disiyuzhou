"""微光者安全区日常模拟（骨架 v2，2026-10-15）。

依据：设计草案/角色与任务系统.md（五点五、五点八~五点十四）。
每日结算顺序：
  1. 饥饿：每人付 1 碎片（不足即死亡，用户裁定：饥饿一天即死）；
  2. 工作：保底任务（6 小时 = 1 碎片，法力网发放）按需求选择 0~3 单位；
  3. 自由任务：部分角色发布，报酬由发布者付给接单者（转移，守恒）；
  4. 赏金赛：自愿报名，入场 5 碎片，胜者得 10，败者不退；
  5. 每 30 天房租：按合租人数平摊，付不起的人被驱逐离开安全区。
守恒：initial + 法力网发放 = 存活者钱包 + 赏金池 + 消耗（食物/房租）+ 消散（死亡）。

占位（待替换）：
- 对战 fight()：占位公式，待接入引擎 1v1 驱动；
- 室友配对、招新人、免费任务的报酬与接单条件均为占位。

运行：python3 sim/linji_daily_sim.py [--seed 1] [--n 100] [--days 90]
"""
from __future__ import annotations

import argparse
import random
from dataclasses import dataclass, field

DIMENSIONS = (
    "risk_preference", "interpersonal_tendency", "moral_baseline",
    "resource_view", "exploration_desire", "emotional_stability",
    "decision_habit", "expression_style", "reaction_pattern",
)
START_SHARDS = 5          # 占位
FOOD_PER_DAY = 1          # 1 碎片 = 100 货币（草案五点五）
WORK_UNIT_HOURS = 6       # 保底任务单位
WORK_PAY = 3              # 每单位 3 碎片（用户裁定：福利调整，原为 1）
MAX_WORK_UNITS = 3        # 每日最多 18 小时
RENT_PER_MONTH = 9        # 900 货币 = 9 碎片（草案五点五）
MONTH_DAYS = 30
ENTRY_FEE = 5
WINNER_PRIZE = 10
LETHAL_CHANCE = 0.01      # 用户裁定：致死率 1%（原占位 5%）
FREE_TASK_POSTERS = 10    # 每日发布自由任务的人数（占位）
TOP_SHARE = 0.10
OUTSIDE_SURVIVAL = 0.001   # 副本完整流程存活率（用户确认：千分之一）


@dataclass
class Gwang:
    cid: int
    traits: dict[str, float]
    stats: dict[str, int]
    shards: int = START_SHARDS
    alive: bool = True
    left_zone: bool = False
    death_cause: str = ""
    wins: int = 0
    matches: int = 0
    house: int = -1
    survived_outside: bool = False
    income: int = 0           # 累计收入（含保底、自由任务、奖金）

    def trait(self, k: str) -> float:
        return self.traits[k]


@dataclass
class Ledger:
    """封闭经济：主办方库（treasury）是唯一的银行。

    收入：食物、房租、驱逐者与死者身上的碎片（均归主办方）。
    支出：保底工资（只能从主办方库支出，不凭空发放）。
    """
    pool: int = 0             # 赏金池（报名费暂存）
    treasury: int = 0         # 主办方库
    welfare_paid: int = 0     # 累计已付保底工资
    welfare_owed: int = 0     # 累计应付但库里不够的保底工资
    minted_kill: int = 0      # 击杀怪物产出（规则公式：⌈战始血限×2%⌉+道纹数×5），唯一合法的新增来源
    expelled: int = 0         # 被丢出安全区（变怪）人数

    def check(self, people: list[Gwang], initial: int) -> None:
        wallets = sum(g.shards for g in people if g.alive)
        assert initial + self.minted_kill == wallets + self.pool + self.treasury, (
            f"碎片不守恒：初始 {initial}，击杀产出 {self.minted_kill}，"
            f"钱包 {wallets}，池 {self.pool}，主办方库 {self.treasury}")


def make_population(n: int, seed: int) -> list[Gwang]:
    rng = random.Random(seed)
    people = []
    for cid in range(n):
        traits = {d: round(rng.uniform(-1.0, 1.0), 3) for d in DIMENSIONS}
        pts = [0, 0, 0]
        for _ in range(25):
            pts[rng.randrange(3)] += 1
        stats = {"血限": pts[0], "速限": pts[1], "法限": pts[2]}
        people.append(Gwang(cid=cid, traits=traits, stats=stats))
    return people


def assign_houses(people: list[Gwang], rng: random.Random) -> dict[int, list[Gwang]]:
    """合租配对（占位）：按人际倾向排序，相邻者同住，每间 1~3 人。"""
    order = sorted(people, key=lambda g: g.trait("interpersonal_tendency"))
    houses: dict[int, list[Gwang]] = {}
    idx = 0
    hid = 0
    while idx < len(order):
        size = rng.choice([1, 2, 2, 3])
        group = order[idx: idx + size]
        houses[hid] = group
        for g in group:
            g.house = hid
        idx += size
        hid += 1
    return houses


def work_units_wanted(g: Gwang, rent_share_month: float) -> int:
    """节约的人留更多余量；需求超过余量时多做保底任务（占位）。"""
    buffer = 2 if g.trait("resource_view") > 0 else 1
    need = FOOD_PER_DAY + buffer
    units = 1
    while units < MAX_WORK_UNITS and g.shards < need + rent_share_month / MONTH_DAYS + units:
        units += 1
    return units


def fight(a: Gwang, b: Gwang, rng: random.Random) -> tuple[Gwang, Gwang]:
    """占位对战（待替换为引擎 1v1 驱动）。"""
    hp = {a.cid: a.stats["血限"] * 6, b.cid: b.stats["血限"] * 6}
    order = [a, b] if a.stats["速限"] >= b.stats["速限"] else [b, a]
    while True:
        for att in order:
            dfn = b if att is a else a
            hp[dfn.cid] -= 1 + rng.randint(0, att.stats["法限"] + 2)
            if hp[dfn.cid] <= 0:
                return att, dfn


def run(n: int, seed: int, days: int, work_pay: int = WORK_PAY,
        expel_fraction: float = 0.0, daowen_count: int = 1,
        inflow_per_day: float = 0.0) -> dict:
    rng = random.Random(seed + 1)
    people = make_population(n, seed)
    initial = sum(g.shards for g in people)
    ledger = Ledger()
    houses = assign_houses(people, rng)
    evicted = 0
    deaths = {"饥饿": 0, "对战": 0}
    inflow_rng = random.Random(seed + 2)
    inflow_acc = 0.0
    inflow_total = 0

    for day in range(1, days + 1):
        # 外部流入：新微光者开局无碎片，进入人口较少的房间（无房间则新开一间）
        inflow_acc += inflow_per_day
        while inflow_acc >= 1.0:
            inflow_acc -= 1.0
            newcomer = make_population(1, seed=inflow_rng.randrange(10**9))[0]
            newcomer.cid = len(people)
            newcomer.shards = 0
            people.append(newcomer)
            inflow_total += 1
            open_houses = [h for h, mem in houses.items() if 0 < len(mem) < 3]
            if open_houses:
                hid = min(open_houses, key=lambda h: len(houses[h]))
            else:
                hid = max(houses.keys(), default=-1) + 1
                houses[hid] = []
            houses[hid].append(newcomer)
            newcomer.house = hid
        alive = [g for g in people if g.alive]
        rng.shuffle(alive)

        # 0. 丢人循环：主办方把一部分人丢出安全区，变成怪物后被击杀，按规则公式产出碎片归主办方库
        k = round(expel_fraction * len(alive))
        for g in alive[:k]:
            hp = g.stats["血限"] * 6                       # 1 属性点 = 6 血限
            yield_ = -(-hp * 2 // 100) + 5 * daowen_count  # ⌈hp×2%⌉ + 道纹数×5
            ledger.treasury += g.shards + yield_
            ledger.minted_kill += yield_
            ledger.expelled += 1
            g.shards = 0
            g.alive = False
            g.left_zone = True
            g.death_cause = "丢出变怪"
        alive = [g for g in people if g.alive]
        # 顺序：工作收入 → 自由任务 → 赏金赛 → 房租（月末）→ 饥饿（当日结算）


        # 2. 保底任务：按需求选择工作单位，法力网发放
        for g in alive:
            rent_share = RENT_PER_MONTH / max(1, len(houses[g.house]))
            units = work_units_wanted(g, rent_share)
            due = units * work_pay
            pay = min(due, ledger.treasury)   # 只能从主办方库支付
            ledger.treasury -= pay
            ledger.welfare_paid += pay
            ledger.welfare_owed += due - pay
            g.shards += pay
            g.income += pay

        # 3. 自由任务：部分角色发布，接单者由冒险倾向决定（占位）
        posters = rng.sample(alive, min(FREE_TASK_POSTERS, len(alive)))
        for p in posters:
            reward = rng.randint(2, 4)
            if p.shards - reward < FOOD_PER_DAY:  # 必须留出当日口粮
                continue
            takers = [g for g in alive if g is not p and g.alive
                      and rng.random() < 0.3 + 0.4 * (g.trait("risk_preference") + 1) / 2]
            if not takers:
                continue
            worker = rng.choice(takers)
            p.shards -= reward
            worker.shards += reward
            worker.income += reward

        # 4. 赏金赛：自愿报名（需有 5 碎片且冒险倾向决定）
        entrants = [g for g in alive if g.alive and g.shards - ENTRY_FEE >= FOOD_PER_DAY
                    and rng.random() < 0.2 + 0.6 * (g.trait("risk_preference") + 1) / 2]
        rng.shuffle(entrants)
        for g in entrants:
            g.shards -= ENTRY_FEE
            ledger.pool += ENTRY_FEE
        if len(entrants) % 2 == 1:
            bye = entrants.pop()
            bye.shards += ENTRY_FEE
            ledger.pool -= ENTRY_FEE
        for i in range(0, len(entrants), 2):
            a, b = entrants[i], entrants[i + 1]
            a.matches += 1
            b.matches += 1
            w, l = fight(a, b, rng)
            w.wins += 1
            ledger.pool -= ENTRY_FEE * 2
            w.shards += WINNER_PRIZE
            w.income += WINNER_PRIZE
            if w.trait("moral_baseline") < 0 and rng.random() < LETHAL_CHANCE:
                l.alive = False
                l.death_cause = "对战"
                deaths["对战"] += 1
                ledger.treasury += l.shards
                l.shards = 0

        # 6. 饥饿（当日结算）：不足 1 碎片即死亡
        for g in alive:
            if not g.alive:  # 对战中已命零者不再结算饥饿
                continue
            if g.shards >= FOOD_PER_DAY:
                g.shards -= FOOD_PER_DAY
                ledger.treasury += FOOD_PER_DAY
            else:
                g.alive = False
                g.death_cause = "饥饿"
                ledger.treasury += g.shards
                g.shards = 0
                deaths["饥饿"] += 1
        alive = [g for g in people if g.alive]

        # 5. 每月房租：按人数平摊；付不起者被驱逐离开；其余室友按新人数重新平摊（同月补缴，直到稳定）
        if day % MONTH_DAYS == 0:
            for hid in list(houses.keys()):
                members = [m for m in houses[hid] if m.alive]
                while members:
                    share = RENT_PER_MONTH / len(members)
                    broke = [m for m in members if m.shards < share]
                    if not broke:
                        break
                    for m in broke:
                        # 被驱逐者进入副本，完整流程存活率 0.1%（千分之一）
                        ledger.treasury += m.shards
                        m.shards = 0
                        evicted += 1
                        if rng.random() < OUTSIDE_SURVIVAL:
                            m.survived_outside = True   # 极少数人活着回到安全区，身无分文
                            m.shards = 0
                        else:
                            m.alive = False
                            m.left_zone = True
                            m.death_cause = "驱逐（副本）"
                    members = [m for m in members if m.alive]
                for m in members:
                    m.shards -= share
                    ledger.treasury += share
                houses[hid] = members

        ledger.check(people, initial)

    alive = [g for g in people if g.alive]
    ranked = sorted(alive, key=lambda g: (g.shards, g.wins), reverse=True)
    top_n = max(1, int(len(alive) * TOP_SHARE)) if alive else 0
    return {
        "days": days,
        "alive": len(alive),
        "deaths": deaths,
        "evicted": evicted,
        "ledger": ledger,
        "top_wealth": ranked[:top_n],
        "ranked": ranked,
        "people": people,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()
    r = run(args.n, args.seed, args.days)
    print(f"天数 {r['days']}，存活 {r['alive']}，饥饿死亡 {r['deaths']['饥饿']}，"
          f"对战死亡 {r['deaths']['对战']}，驱逐 {r['evicted']}")
    print(f"保底累计已付 {r['ledger'].welfare_paid}，欠付 {r['ledger'].welfare_owed}，主办方库余额 {r['ledger'].treasury}")
    print("钱包前五：")
    for g in r["top_wealth"][:5]:
        print(f"  #{g.cid:03d} 碎片 {g.shards}，累计收入 {g.income}，胜 {g.wins} 场")
    if r["alive"]:
        bottom = sorted(
            (g for g in r["people"] if g.alive), key=lambda g: g.shards)[:3]
        print("钱包后三：", [f"#{g.cid:03d}:{g.shards}" for g in bottom])
    print(f"高层（前 {int(TOP_SHARE*100)}% 钱包）人数：{len(r['top_wealth'])}")


if __name__ == "__main__":
    main()
