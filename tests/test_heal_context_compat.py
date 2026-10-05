from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Consumable, Entity, GameState, Relic, StatusEffect


def test_apply_heal_legacy_context_warning_and_overheal_storage_ctx():
    state = GameState(phase="in_combat")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=95)
    state.player = player
    bottle = Consumable("龙血瓶", "", current_uses=10, max_uses=10)
    state.consumables = [bottle]

    detail = state.apply_heal(player, 12)

    assert detail["actual_heal"] == 5
    assert detail["overheal"] == 7
    assert detail["heal_ctx"]["mechanic"] == "heal"
    assert detail["heal_ctx"]["tags"] == ["legacy_context"]
    assert "context_warning" in detail
    assert detail["dragon_blood_bottle_stored"] == 7
    assert detail["dragon_blood_bottle_ctx"]["mechanic"] == "heal_storage"
    assert detail["dragon_blood_bottle_ctx"]["parent_event_id"] == detail["heal_ctx"]["event_id"]
    assert bottle.current_uses == 17 and bottle.max_uses == 17


def test_daowen_heal_effect_has_context_without_warning():
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    caster = Entity("P", "轮回者", blood_limit=100, current_hp=50)
    target = caster
    state.player = caster
    combat = CombatEngine(state, DiceEngine())

    result = combat.apply_daowen_effect("再生", {"x": 3, "target_heal": 9}, caster, target)
    heal = result["effects"][0]

    assert heal["actual_heal"] == 9
    assert "context_warning" not in heal
    assert heal["heal_ctx"]["source"] == "再生"
    assert heal["heal_ctx"]["source_type"] == "daowen"
    assert heal["heal_ctx"]["subtype"] == "daowen"


def test_relic_and_blood_lineage_heals_have_contexts():
    """遗物回复与血族血脉回复都必须带 heal_ctx。

    2026-10-03：【活血】道纹删除（效果由通用遗物【活血衣】承接），原「回终活血回复」
    段落改为直接验证【活血衣】的即时回复带上下文。
    """
    state = GameState(phase="in_combat", combat_subphase="await_round_end")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=70)
    state.player = player
    state.relics = [Relic("活血衣", "", tags=[])]
    combat = CombatEngine(state, DiceEngine())
    monster = Entity("M", "怪物", blood_limit=100, current_hp=100, attack_power=8)
    state.enemies = [monster]
    player.current_speed = 0
    detail = combat._apply_hostile_damage(player, 8, "普通", source=monster, ctx={
        "timing": "测试", "source": "怪物攻击", "source_type": "monster",
        "actor": monster, "target": player, "mechanic": "damage", "subtype": "attack"})
    # 受到攻击伤害 → 活血衣即时回复 ceil(实际伤害/2)，且回复必须带动作上下文
    relic_heal = detail["huoxueyi_heal"]
    assert relic_heal["amount"] == 4 and relic_heal["actual"] == 4
    assert relic_heal["ctx"]["source"] == "活血衣"
    assert relic_heal["ctx"]["mechanic"] == "heal"
    assert relic_heal["ctx"]["subtype"] == "relic_heal_huoxueyi"
    assert player.current_hp == 70 - 8 + 4

    # 单独测血族血脉，避免遗物干扰。
    # 单独测血族血脉，避免活血干扰。
    state = GameState(phase="in_combat", combat_subphase="await_round_end")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=70)
    state.player = player
    state.relics = []
    player.relics = []
    state.relics.append(Relic("血族血脉", "", tags=["血族"]))
    player.damage_dealt_this_round = 6
    combat = CombatEngine(state, DiceEngine())
    result = combat.round_end()
    lineage = next(e for e in result["effects"] if e.get("type") == "blood_lineage_heal")
    assert lineage["amount"] == 6
    assert lineage["heal_ctx"]["source"] == "血族血脉"
    assert lineage["heal_ctx"]["subtype"] == "blood_lineage"
