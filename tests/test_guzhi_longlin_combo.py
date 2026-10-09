"""【固执】+【龙鳞】组合：结算顺序与「无限伤害免疫」边界。

用户问题（2026-10-09）：固执加龙鳞能否免疫无限伤害？

真实管线是：

    incoming_adjust：龙鳞 max(0, 原伤害 - X)
        → Entity.take_damage：格挡 → 固执 min(剩余伤害, 1)
        → 扣生命

因此两者并不是「固执先把大伤害压成 1，再由龙鳞减 1」：龙鳞先运行。结果为：
- 每一笔普通/无视格挡伤害 ≤ 龙鳞 X：减为 0；
- 每一笔普通/无视格挡伤害 > 龙鳞 X：固执仍让其**只掉 1 点**，而非 0；
- 任意多笔独立大伤害会各掉 1 点；【固执】持续 X 回合且付冷却 X 场，不是永久；
- 【代价】既不走龙鳞，也不受固执压帽。

这组断言钉住顺序，防止将来重构时误把组合变成无限免伤，或误把龙鳞挪到固执之后。
"""
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, StatusEffect


def _arena(hp: int = 10_000) -> tuple[Entity, Entity, CombatEngine]:
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    target = Entity("受测者", "轮回者", blood_limit=hp, current_hp=hp,
                    mana_limit=10, current_mana=10, speed_limit=1, current_speed=1)
    source = Entity("攻击者", "怪物", blood_limit=hp, current_hp=hp,
                    attack_count=1, attack_power=1)
    target.add_status(StatusEffect("固执", value=1, remaining_rounds=1, source="test"))
    target.add_status(StatusEffect("龙鳞", value=1, remaining_rounds=-1, source="test"))
    state.player = target
    state.enemies = [source]
    return target, source, CombatEngine(state, DiceEngine(seed=20261009))


def test_guzhi_and_longlin_do_not_make_arbitrarily_large_damage_zero():
    """龙鳞先减、固执后封顶：10^12 普通伤害最终是 1，不是 0。"""
    target, source, combat = _arena()
    detail = combat._apply_hostile_damage(target, 10**12, source=source)

    # 龙鳞=1 先把伤害从 10^12 调成 10^12-1；固执才把剩余压到 1。
    assert detail["raw_damage"] == 10**12 - 1
    assert detail["capped_by"] == "固执"
    assert detail["actual_damage"] == 1
    assert target.current_hp == 9_999


def test_guzhi_longlin_zeroes_only_hits_within_longlin_threshold_and_each_large_hit_still_costs_one():
    """≤龙鳞值的单击为0；超过后每一笔独立伤害仍掉1，故不能免任意多击。"""
    target, source, combat = _arena()

    small = combat._apply_hostile_damage(target, 1, source=source)
    assert small["actual_damage"] == 0
    assert target.current_hp == 10_000

    for _ in range(7):
        detail = combat._apply_hostile_damage(target, 999_999, source=source)
        assert detail["actual_damage"] == 1
        assert detail["capped_by"] == "固执"
    assert target.current_hp == 9_993, "七笔独立大伤害应各穿过 1 点"


def test_guzhi_longlin_still_applies_to_piercing_but_neither_blocks_cost():
    """无视格挡不绕过组合；代价伤害则同时绕过龙鳞与固执。"""
    target, source, combat = _arena()

    piercing = combat._apply_hostile_damage(target, 500, damage_type="无视格挡", source=source)
    assert piercing["actual_damage"] == 1
    assert piercing["capped_by"] == "固执"

    # 用新的满血靶，避免上一击干扰。代价完整落地，不经龙鳞或固执。
    cost_target, cost_source, cost_combat = _arena(hp=2_000)
    cost = cost_combat._apply_hostile_damage(cost_target, 999, damage_type="代价", source=cost_source)
    assert cost["raw_damage"] == 999
    assert cost["actual_damage"] == 999
    assert "capped_by" not in cost
    assert cost_target.current_hp == 1_001
