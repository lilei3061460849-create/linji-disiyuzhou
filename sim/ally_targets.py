"""微光者（[朋友]/[员工]）发动道纹时自选 X 的决策器。

2026-09-17 用户令：所有角色（含微光者）面板与怪物同格式，**不再写死 X**，
X 由发动时自选，上限只受[法限]或代价限制。

**为什么不直接套用怪物侧的 pick_monster_daowen_x**（两个独立阻断）：

1. 资源模型不同。怪物出厂自带遗物【某人的偏爱】，[回始]法力回满，因此怪物侧
   的问题是"*这回合*花多少最值"——下回合池子又满了，可以放心花。
   微光者按 AI_EXPERIENCE.md:1254「轮回者与微光者不持有，仍是一池制，[回始]
   不回填」，是**整场预算**：直接套用每回合最大化的逻辑，第一回合就会把整池
   砸光，之后整场再无道纹可用。

2. 视角错位。怪物侧靠 TacticalAI._split_diff 把引擎 diff 翻到行动者视角，而它
   只在 diff["player"] 与 diff["enemies"] 里按名字找自己，**从不看
   diff["friends"]**。朋友 actor 会静默退化成"挑战者视角"，把玩家当成自己打分。

因此这里不用预演评分，改用**整场预算分配**：对每种代价类型按"不回填"的口径
留出余量，再在可负担区间内取档位。判据不是"微光者变强了"，而是**它不会在第
一回合把池子花光**。
"""
from engine.daowen import DaoWenEngine
from engine.models import Entity

# 一池制：单次发动最多动用当前法力池的比例，余量留给后面的回合。
# 取 1/2 形成几何衰减（4→2→1→1…），池子永不归零。
MANA_POOL_SPEND_RATIO = 0.5

# 流血：单次发动最多动用当前生命的比例，且绝不把自己流死。
HP_SPEND_RATIO = 0.25

# 异变：累加到 MUTATION_COLLAPSE_THRESHOLD 就【崩解】命零，且跨战斗不回退，
# 是最贵的一种"预算"。留 20% 安全边距（与 TacticalAI.SELF_PRESERVE_MARGIN 同口径）。
MUTATION_SAFE_RATIO = 0.8

# 【冷却】类代价（如【固执】：冷却X场）不计入任何"池子"，X 越大锁的**场次**
# 越多，且是跨战斗的。微光者的道纹本就稀缺，一律取 1——绝不为了一回合效果
# 把自己锁上好几场。
COOLDOWN_COST_MAX_X = 1

# 持续类效果（duration == X）的 X 上限。duration 随 X 一起涨，而战斗很少超过
# 这个回合数，超出部分的持续回合等于白付代价。与怪物侧
# _persistent_duration_value 按"预期剩余回合"折价同源，只是这里取一个保守常数
# 而非逐局估算（微光者侧没有预演评分可用，见模块文档串）。调用方可覆盖。
DEFAULT_DURATION_HORIZON = 5

# 试探上限：与 Combat._DAOWEN_X_PROBE_CAP 一致，避免长循环。
X_PROBE_CAP = 30


def _can_pay(ally: Entity, calc: dict) -> bool:
    """硬性可负担性：付不起就是非法选项（与 Combat._monster_can_pay_calc_cost 同口径）。

    不复用后者的原因：它对 `entity_type != "怪物"` 一律 return True，
    对微光者等价于**没有代价上限**，X 可以开到天上去。
    """
    for key, capacity in (
            ("cost_hp", getattr(ally, "current_hp", 0)),
            ("cost_blood_limit", getattr(ally, "blood_limit", 0)),
            ("cost_speed", getattr(ally, "current_speed", 0)),
    ):
        amount = calc.get(key, 0)
        if amount and capacity < amount:
            return False
    # 发动【消耗】类道纹必须付法力（AI_EXPERIENCE.md:278「怪物与轮回者、微光者
    # 共用同一套面板数据，同样持有[血限]/[法限]/[速限]」；:1254 微光者一池制）。
    if calc.get("cost_type") == "消耗" and calc.get("cost", 0) > 0:
        if getattr(ally, "current_mana", 0) < calc["cost"]:
            return False
    # 【异变】是累加计数而非可花费预算：叠满崩解线当场命零，不提供自杀档。
    if calc.get("cost_type") == "异变":
        headroom = Entity.MUTATION_COLLAPSE_THRESHOLD - getattr(ally, "mutation_count", 0)
        if calc.get("cost_mutation", 0) >= headroom:
            return False
    return True


def _within_budget(ally: Entity, calc: dict, x: int,
                   horizon: int = DEFAULT_DURATION_HORIZON) -> bool:
    """整场预算余量：付得起不等于该付。只约束**不回填**的资源。"""
    import math

    if calc.get("cost_type") == "冷却":
        return x <= COOLDOWN_COST_MAX_X

    # duration 随 X 一起涨的道纹：持续回合超出战斗视野就是白付的代价。
    if calc.get("duration") == x and x > horizon:
        return False

    if calc.get("cost_type") == "消耗" and calc.get("cost", 0) > 0:
        pool = getattr(ally, "current_mana", 0)
        if calc["cost"] > max(1, math.ceil(pool * MANA_POOL_SPEND_RATIO)):
            return False

    hp_cost = calc.get("cost_hp", 0) or 0
    if hp_cost:
        hp = getattr(ally, "current_hp", 0)
        if hp_cost > max(1, math.floor(hp * HP_SPEND_RATIO)):
            return False
        if hp - hp_cost <= 0:      # 绝不把自己流死
            return False

    if calc.get("cost_type") == "异变":
        after = getattr(ally, "mutation_count", 0) + (calc.get("cost_mutation", 0) or 0)
        if after > Entity.MUTATION_COLLAPSE_THRESHOLD * MUTATION_SAFE_RATIO:
            return False
    return True


def pick_ally_daowen_x(ally: Entity, name: str, target: Entity | None = None,
                       *, cap: int = X_PROBE_CAP,
                       horizon: int = DEFAULT_DURATION_HORIZON) -> int:
    """为微光者挑一个 X：在「可负担」∩「留足整场余量」内取最大值。

    返回 0 表示连 X=1 都不该发动（付不起，或会把自己玩死），调用方应跳过该道纹
    而不是硬报错。面板写死 X（x_value>0）时由调用方优先用固定值，不走这里。
    """
    DaoWenEngine.register_all()
    best = 0
    affordable_x1 = False
    for x in range(1, max(0, cap) + 1):
        try:
            calc = DaoWenEngine.resolve(name, x, target=target, caster=ally)
        except ValueError:
            # X_LIMITS 之类的硬性上限（如【失忆】X≤当前道纹数量）会在此抛错
            break
        if not _can_pay(ally, calc):
            break
        if x == 1:
            affordable_x1 = True
        if not _within_budget(ally, calc, x, horizon):
            break
        best = x
    # 余量规则只负责**压上限**，不能把微光者压到彻底失声：只要 X=1 付得起且不会
    # 玩死自己，就至少开 X=1。否则池子小的时候（如岩行者 4 点、背负需 2X）预算
    # 上限会低于最小代价，剩下的法力将永远闲置。
    if best == 0 and affordable_x1:
        return 1
    return best
