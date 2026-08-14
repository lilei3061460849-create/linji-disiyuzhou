"""【血契】、再生6X与显式效果作用域账本的契约测试。"""
from __future__ import annotations

import pytest

from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.daowen import DaoWenEngine
from engine.dice import DiceEngine
from engine.enums import CombatSubphase, EffectPolarity, EffectScope
from engine.events import resolve_option_effect
from engine.models import Consumable, Entity, GameState, Relic, StatusEffect


def _state_with_blood_pact() -> tuple[GameState, CombatEngine, Entity, Entity]:
    state = GameState(phase="in_combat", combat_subphase=CombatSubphase.PLAYER_ACTIONS.value)
    player = Entity(
        "轮回者", "轮回者", blood_limit=100, current_hp=100,
        mana_limit=30, current_mana=30, speed_limit=20, current_speed=20,
        attack_count=2, attack_power=4,
    )
    ally = Entity(
        "同伴", "朋友", blood_limit=80, current_hp=80,
        mana_limit=20, current_mana=20, speed_limit=14, current_speed=14,
        attack_count=2, attack_power=3,
    )
    state.player = player
    state.friends.append(ally)
    state.relics.append(Relic("血契", "数值代价共同承担"))
    return state, CombatEngine(state, DiceEngine()), player, ally


@pytest.mark.parametrize(
    ("cost_type", "player_field", "ally_field"),
    [
        ("流血", "current_hp", "current_hp"),
        ("衰老", "blood_limit", "blood_limit"),
        ("枯竭", "mana_limit", "mana_limit"),
        ("萎缩", "speed_limit", "speed_limit"),
        ("疲惫", "current_speed", "current_speed"),
        ("异变", "mutation_count", "mutation_count"),
    ],
)
def test_blood_pact_shares_every_supported_numeric_cost(
    cost_type: str, player_field: str, ally_field: str,
):
    """正常路径：代价5由玩家承担3、同伴承担2，六种数值代价使用同一总线。"""
    _, combat, player, ally = _state_with_blood_pact()
    player_before = getattr(player, player_field)
    ally_before = getattr(ally, ally_field)

    result = combat.pay_numeric_cost(
        player, cost_type, 5, cost_share_target_ref="friend:0")

    if cost_type == "异变":
        assert getattr(player, player_field) == player_before + 3
        assert getattr(ally, ally_field) == ally_before + 2
    else:
        assert getattr(player, player_field) == player_before - 3
        assert getattr(ally, ally_field) == ally_before - 2
    assert result["owner"]["paid"] == 3
    assert result["shared_with"]["paid"] == 2
    assert result["actual_paid"] == 5


def test_blood_pact_cost_one_and_dragon_heart_order():
    """边界：代价1由玩家承担1/同伴0；龙心先抵消，再拆分剩余后果。"""
    state, combat, player, ally = _state_with_blood_pact()
    one = combat.pay_numeric_cost(
        player, "流血", 1, cost_share_target_ref="friend:0")
    assert player.current_hp == 99 and ally.current_hp == 80
    assert one["owner"]["paid"] == 1 and one["shared_with"]["paid"] == 0

    heart = Consumable(
        "流血龙心", "抵消流血", current_uses=3, max_uses=3,
        kind="dragon_heart", dragon_heart_type="流血")
    state.consumables.append(heart)
    result = combat.pay_numeric_cost(
        player, "流血", 9, cost_share_target_ref="friend:0", dragon_heart_use=3)
    assert result["dragon_heart_offset"] == 3
    assert result["remaining"] == 6
    assert player.current_hp == 96 and ally.current_hp == 77
    assert heart.current_uses == 0


def test_blood_pact_rejects_invalid_or_unpayable_share_atomically():
    """非法输入：无遗物、非法引用或共同承担者资源不足时，任何一方都不扣除。"""
    state, combat, player, ally = _state_with_blood_pact()
    ally.current_mana = ally.mana_limit = 1
    before = (player.mana_limit, ally.mana_limit)
    with pytest.raises(ValueError, match="无法完整承担"):
        combat.pay_numeric_cost(
            player, "枯竭", 5, cost_share_target_ref="friend:0")
    assert (player.mana_limit, ally.mana_limit) == before

    with pytest.raises(ValueError, match="cost_share_target_ref"):
        combat.pay_numeric_cost(
            player, "流血", 5, cost_share_target_ref="friend:99")
    assert player.current_hp == 100

    state.relics.clear()
    with pytest.raises(ValueError, match="血契"):
        combat.pay_numeric_cost(
            player, "流血", 5, cost_share_target_ref="friend:0")
    assert player.current_hp == 100 and ally.current_hp == 80


def test_blood_pact_round_start_can_share_its_own_bleed(tmp_path):
    """正常路径：回始流血4X也可共同承担，回始法限与血契法力按加法结算。"""
    state, _, player, ally = _state_with_blood_pact()
    player.current_mana = 0
    state.combat_subphase = CombatSubphase.AWAIT_ROUND_START.value
    engine = GameEngine(db_path=str(tmp_path / "rulings.db"))
    engine.state = state
    engine.combat.state = state

    result = engine.execute_action("round_start", {"relic_choices": {
        "血契": {"use": True, "x": 2, "cost_share_target_ref": "friend:0"},
    }})

    assert result["success"]
    assert player.current_hp == 96 and ally.current_hp == 76
    assert player.current_mana == 32  # 法限30 + 血契2


def test_opponent_blood_pact_uses_its_own_allies_in_final_duel():
    """双边边界：封存对手的血契使用敌方稳定引用，不能误分担给玩家队友。"""
    state = GameState(phase="in_combat")
    state.player = Entity("玩家", "轮回者", blood_limit=100, current_hp=100,
                          mana_limit=10, current_mana=0)
    opponent = Entity("对手", "轮回者", blood_limit=90, current_hp=90,
                      mana_limit=8, current_mana=0)
    opponent_ally = Entity("对手同伴", "朋友", blood_limit=60, current_hp=60)
    state.enemies = [opponent, opponent_ally]
    state.opponent_relics = [Relic("血契", "")]
    combat = CombatEngine(state, DiceEngine())

    combat.round_start({"对手血契": {
        "use": True, "x": 1, "cost_share_target_ref": "enemy:1",
    }})

    assert opponent.current_hp == 88 and opponent_ally.current_hp == 58
    assert opponent.current_mana == 9  # 法限8 + 血契1
    assert state.player.current_hp == 100


def test_blood_pact_round_start_requires_explicit_valid_decision(tmp_path):
    """边界/非法：持有血契时必须显式提交use；X非法时事务不改变回合与生命。"""
    state, _, player, ally = _state_with_blood_pact()
    state.combat_subphase = CombatSubphase.AWAIT_ROUND_START.value
    engine = GameEngine(db_path=str(tmp_path / "rulings.db"))
    engine.state = state
    engine.combat.state = state

    missing = engine.execute_action("round_start", {"relic_choices": {}})
    assert not missing["success"] and "血契" in missing["error"]
    invalid = engine.execute_action("round_start", {"relic_choices": {
        "血契": {"use": True, "x": 0, "cost_share_target_ref": "friend:0"},
    }})
    assert not invalid["success"]
    assert player.current_hp == 100 and ally.current_hp == 80
    assert state.current_round == 0


def test_event_and_daowen_costs_use_blood_pact_bus(tmp_path):
    """正常路径：事件与道纹代价均接入同一血契总线，不只覆盖遗物自身。"""
    state, combat, player, ally = _state_with_blood_pact()
    engine = GameEngine(db_path=str(tmp_path / "rulings.db"))
    engine.state = state
    engine.combat.state = state

    event = resolve_option_effect(
        "流血15。", engine, params={"cost_share_target_ref": "friend:0"})
    assert "error" not in event
    assert player.current_hp == 92 and ally.current_hp == 73  # 8/7

    target = Entity("敌人", "怪物", blood_limit=100, current_hp=100)
    state.enemies.append(target)
    calc = DaoWenEngine.resolve("血债", 5, target=target)
    combat.apply_daowen_effect(
        "血债", calc, player, target,
        cost_share_target_ref="friend:0")
    assert player.current_hp == 89 and ally.current_hp == 71  # 再分3/2


def test_regeneration_is_six_x_and_old_contracts_are_removed():
    """规则替换：再生为6X；遗物池只保留新血契，不再包含两件旧契约。"""
    target = Entity("目标", "朋友", blood_limit=100, current_hp=10)
    assert DaoWenEngine.resolve("再生", 4, target=target)["target_heal"] == 24
    names = {name for name, _ in GameEngine.RELIC_DEFS}
    assert "血契" in names
    assert "鲜血契约" not in names
    assert "卖身契" not in names
    assert len(names) == 12


def test_scoped_ledger_rolls_back_battle_effects_but_keeps_costs(tmp_path):
    """正常路径：局内面板变化战终逆向清除；同字段上的衰老代价仍然保留。"""
    state, combat, player, _ = _state_with_blood_pact()
    state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
    state.current_battle = 1
    state.apply_scoped_delta(
        player, "blood_limit", 10,
        scope=EffectScope.BATTLE.value, polarity=EffectPolarity.BUFF.value,
        source="增殖")
    state.apply_scoped_delta(
        player, "attack_power", 5,
        scope=EffectScope.BATTLE.value, polarity=EffectPolarity.BUFF.value,
        source="强化")
    combat.pay_numeric_cost(player, "衰老", 3)
    player.total_healed = 17
    player.no_action_rounds = 4
    player.no_damage_rounds = 3
    player._jisu_dodges = 1
    player.is_flying = True
    player.add_status(StatusEffect("强化", -1, 5, "强化"))

    engine = GameEngine(db_path=str(tmp_path / "rulings.db"))
    engine.state = state
    engine.combat.state = state
    result = engine.execute_action("battle_end", {})

    assert result["success"]
    assert player.blood_limit == 97  # 初始100，仅保留衰老3
    assert player.attack_power == 4
    assert player.total_healed == 0
    assert player.no_action_rounds == player.no_damage_rounds == 0
    assert not hasattr(player, "_jisu_dodges")
    assert player.is_flying is False
    assert player.status_effects == []
    assert state.scoped_effect_ledger == []


def test_blood_limit_debuff_rolls_back_without_erasing_life_loss():
    """关键边界：锐利的局内血限减少会清除，但其独立造成的当前生命减少不是增减益。"""
    state = GameState(phase="in_combat")
    player = Entity("轮回者", "轮回者", blood_limit=100, current_hp=100,
                    current_mana=10)
    target = Entity("目标", "怪物", blood_limit=100, current_hp=100)
    state.player = player
    state.enemies = [target]
    combat = CombatEngine(state, DiceEngine())
    calc = DaoWenEngine.resolve("锐利", 1, target=target)

    combat.apply_daowen_effect("锐利", calc, player, target)
    assert target.blood_limit == 96 and target.current_hp == 96
    state.rollback_scoped_effects(EffectScope.BATTLE.value)

    assert target.blood_limit == 100
    assert target.current_hp == 96  # 生命损失不是局内面板减益，不能被回滚顺带治疗


def test_duration_expiry_rolls_back_matching_scoped_delta():
    """边界：持续效果到期时立即按来源回滚，不错误拖到战终。"""
    state = GameState(phase="in_combat")
    player = Entity("轮回者", "轮回者", blood_limit=100, current_hp=100,
                    attack_count=2, attack_power=10)
    state.player = player
    state.apply_scoped_delta(
        player, "attack_power", -7,
        scope=EffectScope.BATTLE.value, polarity=EffectPolarity.DEBUFF.value,
        source="僵化")
    player.add_status(StatusEffect("僵化", 1, 1, "施法者"))
    assert player.attack_power == 3

    CombatEngine(state, DiceEngine()).round_end()

    assert player.attack_power == 10
    assert not player.has_status("僵化")
    assert state.scoped_effect_ledger == []


def test_scoped_ledger_survives_versioned_save_round_trip(tmp_path):
    """存档边界：作用域账本与稳定实体ID完整往返，载入后仍能回滚到当前实体。"""
    state, _, player, _ = _state_with_blood_pact()
    state.apply_scoped_delta(
        player, "attack_power", 6,
        scope=EffectScope.BATTLE.value, polarity=EffectPolarity.BUFF.value,
        source="强化")
    engine = GameEngine(db_path=str(tmp_path / "rulings.db"))
    engine.state = state
    engine.combat.state = state
    assert engine.save_game("scope")["version"] == 5
    player.attack_power = 99

    loaded = engine.load_game("scope")

    assert loaded["success"] and engine.state.player.attack_power == 10
    assert len(engine.state.scoped_effect_ledger) == 1
    engine.state.rollback_scoped_effects(EffectScope.BATTLE.value)
    assert engine.state.player.attack_power == 4


def test_status_scope_and_polarity_are_explicit():
    """状态元数据不再靠数值正负猜测：作用域与增减益极性分别可读。"""
    entity = Entity("角色", "朋友")
    entity.add_status(StatusEffect("强化", -1, 2, "道纹"))
    entity.add_status(StatusEffect("坏死", 2, 1, "道纹"))
    assert entity.get_status_effects("强化")[0].scope == EffectScope.BATTLE.value
    assert entity.get_status_effects("强化")[0].polarity == EffectPolarity.BUFF.value
    assert entity.get_status_effects("坏死")[0].polarity == EffectPolarity.DEBUFF.value
