"""循环法术的法力校验必须与结算口径一致：产法力道纹的收益要计入预算。

背景（2026-09-12）：【透支】是"流血3X→获得X法力"，是体系里唯一的法力来源。
把它和【再生】打包成循环法术（透支X于自身→再生X于自身→循环）时，
每个循环法力净零，理应能从0法力自持启动、由【癌变】(累计回复达2×血限)自然终止。

但校验期 `validate_spell_reaction_submission` / 全局时点校验只扣 cost_type=="消耗"
的花费，从不计 mana_gain，于是 N 次循环被要求预付 cost*N 的法力——而这笔法力
恰恰正是循环自己要产出的东西。结果：循环法术这一【循环】法则对
"再生+透支"闭环完全不可用。结算侧(_apply_daowen_result)一直是计入 mana_gain 的，
所以这是校验与结算的口径不一致，而非设计意图。

本测试锁定：零法力起步的透支+再生循环可以提交并结算，且仍被癌变封顶。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.models import DaoWen, DaoWenInstance
from tests.test_dragon_heart import _new_engine, _start_with_enemy

SPELL = {
    "name": "血炼周天",
    "required_daowen": ["透支", "再生"],
    "trigger_condition": "失去生命后",
    "effect_flow": "发动透支X于自身→发动再生X于自身→循环",
}


def _engine_with_loop_spell(suffix, *, hp, mana, spells=(SPELL,)):
    engine = _new_engine(suffix)
    player = engine.state.player
    for name in ("透支", "再生"):
        player.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="", cost_formula="X", effect_formula=""))
    # 自创法术只能在局外阶段学习，故所有法术都要赶在 _start_with_enemy 之前学完。
    for spell in spells:
        engine.state.energy = 3
        engine.execute_action("pre_battle_action",
                              {"sub_action": "学习", "sub": "custom_spell", "spell": spell})
        learned = engine.execute_action(
            "pre_battle_action",
            {"sub_action": "学习", "sub": "custom_spell", "spell": spell, "dm_approved": True})
        assert learned["success"], learned.get("error")
        assert learned["result"]["wired"] is True

    _start_with_enemy(engine)
    foe = engine.state.enemies[0]
    foe.current_hp = 99999
    foe.attack_power = 5
    foe.attack_count = 1
    player.current_hp = hp
    player.current_mana = mana
    return engine


def _fire(engine, cycles, x=3):
    """让怪物普攻一次触发【失去生命后】，并提交 cycles 轮循环。"""
    combat = engine.combat
    prepared = combat.prepare_monster_phase()
    actor = prepared["actors"][0]
    target = actor["attack_target_options"][0]
    options = target["spell_options"]
    assert any(s["spell_name"] == "血炼周天" and s["loop"] for s in options["after"])

    steps = [{"x": x, "target_ref": "player:0"}, {"x": x, "target_ref": "player:0"}]
    spell_choices = {
        "before": {s["spell_name"]: {"use": False} for s in options["before"]},
        "after": {"血炼周天": {"use": True, "cycles": [list(steps) for _ in range(cycles)]}},
        "damage_after": {s["spell_name"]: {"use": False} for s in options["damage_after"]},
        "life_before": {s["spell_name"]: {"use": False} for s in options["life_before"]},
    }
    choices = [{
        "actor_ref": actor["actor_ref"],
        "daowen": None,
        "attack_actions": [{"hits": [{"target_ref": target["ref"], "dodge": False,
                                      "blood_shadow": False,
                                      "spell_choices": spell_choices}]}],
    }]
    return combat.resolve_monster_phase(choices, prepared=prepared)


def test_zero_mana_loop_bootstraps_from_touzhi_output():
    """0法力起步也能跑透支+再生循环——法力由循环内的透支现产现用。"""
    engine = _engine_with_loop_spell("loop_zero", hp=40, mana=0)
    player = engine.state.player

    _fire(engine, cycles=1)

    # 挨打5点后触发：透支3(流血9、产3法力) → 再生3(耗3法力、回12生命)
    assert player.current_hp == 38          # 40-5-9+12
    assert player.current_mana == 0         # 法力净零：产3用3
    assert player.total_healed == 12


def test_loop_is_mana_neutral_across_many_cycles():
    """多轮循环同样零法力自持，每轮净赚3生命。"""
    engine = _engine_with_loop_spell("loop_many", hp=40, mana=0)
    player = engine.state.player

    _fire(engine, cycles=10)

    assert player.current_mana == 0
    assert player.total_healed == 120       # 10轮 × 再生3回12
    assert player.current_hp == 65          # 40-5 + 10×(12-9)
    assert player.is_alive


def test_cancer_still_caps_the_loop():
    """癌变是天然闸门：累计回复达2×血限即命零，提交再多循环也止步于此。"""
    for cycles in (11, 50):
        engine = _engine_with_loop_spell(f"loop_cap_{cycles}", hp=40, mana=0)
        player = engine.state.player
        cap = 2 * player.blood_limit

        _fire(engine, cycles=cycles)

        assert player.total_healed == cap   # 132，不因提交更多循环而继续累加
        assert player.current_hp == 0
        assert not player.is_alive


def test_validation_still_rejects_genuinely_unaffordable_loop():
    """修复不得放水：没有产法力步骤的循环，法力不足照样必须拒绝。"""
    spell = {
        "name": "纯耗周天",
        "required_daowen": ["再生"],
        "trigger_condition": "失去生命后",
        "effect_flow": "发动再生X于自身→循环",
    }
    engine = _engine_with_loop_spell("loop_broke", hp=40, mana=0, spells=(spell,))

    combat = engine.combat
    prepared = combat.prepare_monster_phase()
    actor = prepared["actors"][0]
    target = actor["attack_target_options"][0]
    options = target["spell_options"]
    after = {s["spell_name"]: {"use": False} for s in options["after"]}
    assert "纯耗周天" in after
    after["纯耗周天"] = {"use": True, "cycles": [[{"x": 3, "target_ref": "player:0"}]]}
    spell_choices = {
        "before": {s["spell_name"]: {"use": False} for s in options["before"]},
        "after": after,
        "damage_after": {s["spell_name"]: {"use": False} for s in options["damage_after"]},
        "life_before": {s["spell_name"]: {"use": False} for s in options["life_before"]},
    }
    choices = [{
        "actor_ref": actor["actor_ref"],
        "daowen": None,
        "attack_actions": [{"hits": [{"target_ref": target["ref"], "dodge": False,
                                      "blood_shadow": False,
                                      "spell_choices": spell_choices}]}],
    }]
    with pytest.raises(ValueError, match="法力不足"):
        combat.resolve_monster_phase(choices, prepared=prepared)
