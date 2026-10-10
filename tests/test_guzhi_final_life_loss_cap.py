"""【固执】在所有失血倍率之后封顶实际失血。"""

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, StatusEffect


def test_guzhi_caps_final_life_loss_after_multiplier():
    target = Entity(
        "受测者", "轮回者",
        blood_limit=20, current_hp=20,
        mana_limit=10, current_mana=10,
        speed_limit=1, current_speed=1,
    )
    target.add_status(StatusEffect(
        "固执", value=1, remaining_rounds=-1, source="test",
    ))

    detail = target.take_damage(
        5,
        life_loss_multiplier=2,
        # 模拟受击持续状态阶段已经按固执把中间伤害限制为 1。
        status_adjuster=lambda amount: min(amount, 1),
    )

    assert detail["life_loss_multiplier"] == 2
    assert detail["capped_by"] == "固执"
    assert detail["guzhi_final_cap"] is True
    assert detail["actual_damage"] == 1
    assert detail["hp_before"] - detail["hp_after"] == 1
    assert target.current_hp == 19


def test_guzhi_caps_first_cup_damage_in_real_combat_pipeline():
    target = Entity(
        "受测者", "轮回者",
        blood_limit=20, current_hp=20,
        mana_limit=10, current_mana=10,
        speed_limit=1, current_speed=1,
    )
    target.add_status(StatusEffect(
        "固执", value=1, remaining_rounds=-1, source="test",
    ))
    attacker = Entity(
        "攻击者", "怪物",
        blood_limit=20, current_hp=20,
        attack_count=1, attack_power=1,
    )
    state = GameState(
        phase="in_combat",
        combat_subphase="player_actions",
        player=target,
        enemies=[attacker],
        artifacts_owned=["第一杯"],
    )
    combat = CombatEngine(state, DiceEngine(seed=1))

    detail = combat._apply_hostile_damage(target, 5, source=attacker)

    assert detail["life_loss_multiplier"] == 2
    assert detail["actual_damage"] == 1
    assert detail["actual_life_loss"] == 1
    assert detail["guzhi_final_cap"] is True
    assert target.current_hp == 19
