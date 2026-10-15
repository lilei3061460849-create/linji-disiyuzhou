"""已裁定遗物化道纹的运行时行为边界回归。

这些用例只钉住《物品索引》已有明文，不决定待裁定道纹的去留或新效果。
"""
from engine.combat import CombatEngine
from engine.daowen import DaoWenEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState, Relic, StatusEffect


def _combat():
    state = GameState()
    player = Entity(
        name="持有者", entity_type="轮回者",
        blood_limit=100, current_hp=50,
        mana_limit=20, current_mana=5,
        speed_limit=10, current_speed=3,
    )
    target = Entity(
        name="靶怪", entity_type="怪物",
        blood_limit=100, current_hp=100,
        mana_limit=10, current_mana=3,
        speed_limit=10, current_speed=2,
        attack_count=1, attack_power=5,
    )
    state.player = player
    state.enemies.append(target)
    state.phase = "in_combat"
    state.current_round = 2
    return state, player, target, CombatEngine(state, DiceEngine())


def test_baisu_gear_doubles_actual_speed_gain_and_respects_cap():
    state, player, _, combat = _combat()
    state.relics.append(Relic(name="倍速齿轮", effect="", tags=[]))

    assert combat._gain_speed(player, 2) == 4
    assert player.current_speed == 7

    player.add_status(StatusEffect(name="加速", value=1, remaining_rounds=2, source="test"))
    assert combat._gain_speed(player, 1) == 3
    assert player.current_speed == player.speed_limit == 10


def test_nixue_longlin_adds_actual_life_loss_to_damage_for_this_round():
    state, player, target, combat = _combat()
    state.relics.append(Relic(name="逆血龙鳞", effect="", tags=[]))

    # 直接失血计入本回合实际失血账，随后造成的伤害增加相同数值。
    combat._raw_hp_loss(player, 3)
    result = combat._apply_hostile_damage(target, 5, source=player)

    assert result["actual_damage"] == 8
    assert target.current_hp == 92


def test_shishang_xuenang_heals_holder_from_actual_damage_taken_by_anyone():
    state, player, target, combat = _combat()
    state.relics.append(Relic(name="噬伤血囊", effect="", tags=[]))
    attacker = Entity(
        name="另一只怪物", entity_type="怪物",
        blood_limit=100, current_hp=100,
        mana_limit=10, current_mana=0,
        speed_limit=5, current_speed=1,
        attack_count=1, attack_power=1,
    )
    state.enemies.append(attacker)

    result = combat._apply_hostile_damage(target, 3, source=attacker)

    assert result["actual_damage"] == 3
    assert target.current_hp == 97
    assert player.current_hp == 51  # ceil(3 * 20%) = 1


def test_chongzhong_jiasuo_removes_one_current_speed_from_each_living_role():
    state, player, target, combat = _combat()
    state.relics.append(Relic(name="沉重枷锁", effect="", tags=[]))
    target.current_speed = 1

    combat.round_start()

    assert player.current_speed == 2
    assert target.current_speed == 0


def test_shisu_mingdeng_reduces_speed_limit_only_when_speed_is_actually_lost():
    state, _, target, combat = _combat()
    state.relics.append(Relic(name="失速冥灯", effect="", tags=[]))

    assert combat._lose_current_speed(target, 1) == 1
    assert target.current_speed == 1
    assert target.speed_limit == 8

    assert combat._lose_current_speed(target, 99) == 1
    assert target.current_speed == 0
    assert target.speed_limit == 6

    assert combat._lose_current_speed(target, 1) == 0
    assert target.speed_limit == 6


def test_shijie_zhong_clock_erodes_each_living_roles_own_mana_times_speed():
    state, player, target, combat = _combat()
    state.relics.append(Relic(name="蚀界残钟", effect="", tags=[]))
    player.current_mana, player.current_speed = 5, 2
    target.blood_limit, target.current_hp = 80, 40
    target.current_mana, target.current_speed = 3, 2

    combat.round_end()

    assert player.blood_limit == 90
    assert target.blood_limit == 74
    assert player.current_hp == 50
    assert target.current_hp == 40


def test_shuai_bian_sha_lou_reduces_effect_x_by_one_but_clamps_at_zero():
    state, player, target, combat = _combat()
    state.relics.append(Relic(name="衰变沙漏", effect="", tags=[]))
    DaoWenEngine.register_all()

    calc = DaoWenEngine.resolve("杀伐", 2, target=target, caster=player)
    combat._apply_daowen_effect_impl("杀伐", calc, player, target)
    assert target.current_hp == 99  # X=2 的效果按 X=1 解析

    target.current_hp = target.blood_limit = 100
    calc = DaoWenEngine.resolve("杀伐", 1, target=target, caster=player)
    combat._apply_daowen_effect_impl("杀伐", calc, player, target)
    assert target.current_hp == 100  # X=1 降到 0，不得仍造成原伤害
