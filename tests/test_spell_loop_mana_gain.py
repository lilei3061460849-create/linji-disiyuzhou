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

from engine.models import DaoWen, DaoWenInstance, Relic
from tests.test_dragon_heart import _new_engine, _start_with_enemy

# 与官方条目 死者之书.md「## 可学法术 → 血炼周天」同构（现已是内置法术，
# 这里刻意用另一个名字自创同一流程，以覆盖"自创循环法术"这条路径）。先【再生】回血、再
# 【透支】把血卖成法力，【透支】的流血同时满足「失去生命后」驱动下一轮，
# 与【千刀万剐】靠代价自驱同构。故需 3 点法力垫付第一轮【再生】。
SPELL = {
    "name": "周天自持",
    "required_daowen": ["再生", "透支"],
    "trigger_condition": "失去生命后",
    "effect_flow": "发动再生X于自身→发动透支X于自身→循环",
}
SEED_MANA = 3


def _engine_with_loop_spell(suffix, *, hp, mana, spells=None):
    engine = _new_engine(suffix)
    player = engine.state.player
    for name in ("透支", "再生"):
        player.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="", cost_formula="X", effect_formula=""))
    # 2026-09-16：自创法术只能在战斗中用 define_spell 完成（消耗1次主动出手），
    # 局外【学习·自创法术】入口已取消，故必须先开战再自创。
    _start_with_enemy(engine)
    _define_spells(engine, spells if spells is not None else (SPELL,))
    foe = engine.state.enemies[0]
    foe.current_hp = 99999
    foe.attack_power = 5
    foe.attack_count = 1
    player.current_hp = hp
    player.current_mana = mana
    return engine


def _define_spells(engine, spells):
    """战斗中自创法术（2026-09-16 新规则入口，每次消耗 1 次主动出手）。"""
    for spell in spells:
        result = engine.execute_action("define_spell", {"spell": spell})
        assert result["success"], result.get("error")
        assert result["result"]["wired"] is True


def _fire(engine, cycles, x=3):
    """让怪物普攻一次触发【失去生命后】，并提交 cycles 轮循环。"""
    combat = engine.combat
    prepared = combat.prepare_monster_phase()
    actor = prepared["actors"][0]
    target = actor["attack_target_options"][0]
    options = target["spell_options"]
    assert any(s["spell_name"] == "周天自持" and s["loop"] for s in options["after"])

    steps = [{"x": x, "target_ref": "player:0"}, {"x": x, "target_ref": "player:0"}]
    spell_choices = {
        "before": {s["spell_name"]: {"use": False} for s in options["before"]},
        "after": {"周天自持": {"use": True, "cycles": [list(steps) for _ in range(cycles)]}},
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


BRANCH_SPELL = {
    "name": "血溅五步",
    "required_daowen": ["再生", "透支", "杀伐"],
    "trigger_condition": "失去生命后",
    "effect_flow": (
        "若自身 法力 大于等于 2 则 "
        "发动再生X于自身；发动杀伐X于攻击者；发动透支X于自身 "
        "否则 发动再生X于自身；发动透支X于自身→循环"
    ),
}


def _engine_with_branch_spell(suffix, *, mana):
    """用真实 GameEngine 装配【血溅五步】并把怪物设为三击靶场。"""
    engine = _new_engine(suffix)
    player = engine.state.player
    for name in ("杀伐", "再生", "透支"):
        player.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="", cost_formula="X", effect_formula=""))

    # 让前一击的真实失血与【承露盏】在同一阶段内把法力从<2推到≥2，
    # 直接覆盖用户要求的“承露盏+血溅五步”引擎路径。
    engine.state.relics.append(Relic(
        name="承露盏", effect="每累计失去10点生命，获得1点法力"))
    _start_with_enemy(engine)
    _define_spells(engine, (BRANCH_SPELL,))
    foe = engine.state.enemies[0]
    foe.current_hp = 99999
    foe.attack_power = 6
    foe.attack_count = 3
    engine.state.player.current_hp = 40
    engine.state.player.current_mana = mana
    return engine


def _branch_choices(option, *, use, cycles):
    """按 prepare 返回的真实资格集生成一份完整反应提交。"""
    spell_choices = {
        timing: {
            spell["spell_name"]: {"use": False}
            for spell in option["spell_options"].get(timing, [])
        }
        for timing in ("before", "after", "damage_after", "life_before")
    }
    if use:
        spell_choices["after"]["血溅五步"] = {"use": True, "cycles": cycles}
    return spell_choices


def _resolve_branch_phase(engine, hits):
    """通过 CombatEngine 的 prepare/resolve 合约结算一次真实怪物阶段。"""
    prepared = engine.combat.prepare_monster_phase()
    actor = prepared["actors"][0]
    target = actor["attack_target_options"][0]
    option = target
    choices = [{
        "actor_ref": actor["actor_ref"],
        "daowen": None,
        "attack_actions": [{"hits": [
            {"target_ref": target["ref"], "dodge": False, "blood_shadow": False,
             "spell_choices": _branch_choices(option, use=use, cycles=cycles)}
            for use, cycles in hits
        ]}],
    }]
    return engine.combat.resolve_monster_phase(choices, prepared)


def test_blood_splash_low_mana_branch_stays_frozen_across_hits():
    """低法力分支在前一击产法力后，后续重复校验仍按同一次触发的两步展开。"""
    engine = _engine_with_branch_spell("branch_low", mana=1)
    # 两个连续命中都在提交时看到法力<2；第一轮【透支】会把法力推到2，
    # 这是此前 prepare/validate/resolve 步数漂移的最小真实引擎复现。
    result = _resolve_branch_phase(engine, [
        (True, [[{"x": 1, "target_ref": "player:0"},
                 {"x": 1, "target_ref": "player:0"}]]),
        (True, [[{"x": 1, "target_ref": "player:0"},
                 {"x": 1, "target_ref": "player:0"}]]),
        (False, []),
    ])
    assert result and engine.state.player.is_alive
    assert engine.state.player.current_mana == 3
    used = [
        log["daowen"]
        for detail in result
        for log in detail.get("spell_logs", [])
        if "daowen" in log
    ]
    assert used == ["再生", "透支", "再生", "透支"]


def test_blood_splash_high_mana_branch_submits_three_steps_and_loops():
    """高法力分支必须实际提交并结算再生→杀伐→透支三步循环。"""
    engine = _engine_with_branch_spell("branch_high", mana=2)
    result = _resolve_branch_phase(engine, [
        (
            True,
            [[{"x": 1, "target_ref": "player:0"},
              {"x": 1, "target_ref": "enemy:0", "dodge": False},
              {"x": 1, "target_ref": "player:0"}]],
        ),
        (False, []),
        (False, []),
    ])
    assert result and engine.state.player.is_alive
    logs = [log for detail in result for log in detail.get("spell_logs", [])]
    assert [log["daowen"] for log in logs if "daowen" in log] == ["再生", "杀伐", "透支"]


def test_single_cycle_is_mana_neutral():
    """一轮循环法力净零：再生耗3、透支产3，法力回到起点，生命净+3。"""
    engine = _engine_with_loop_spell("loop_one", hp=40, mana=SEED_MANA)
    player = engine.state.player

    _fire(engine, cycles=1)

    # 挨打5点后触发：再生3(耗3法力、回12生命) → 透支3(流血12、产3法力)
    # 2026-09-13 透支改 4X 后，与再生4X 构成严格 4:1 对称，每轮净 0 生命。
    assert player.current_hp == 35          # 40-5+12-12
    assert player.current_mana == SEED_MANA  # 用3产3，回到起点
    assert player.total_healed == 12


def test_loop_cannot_start_without_seed_mana():
    """首步是【再生】，零法力起不来——循环自持但不自举。"""
    engine = _engine_with_loop_spell("loop_noseed", hp=40, mana=0)
    with pytest.raises(ValueError, match="法力不足"):
        _fire(engine, cycles=1)


def test_loop_is_mana_neutral_across_many_cycles():
    """多轮循环同样零法力自持，每轮净 0 生命（4:1 对称闭环）。"""
    engine = _engine_with_loop_spell("loop_many", hp=40, mana=SEED_MANA)
    player = engine.state.player

    _fire(engine, cycles=10)

    assert player.current_mana == SEED_MANA  # 10轮之后法力仍回到起点
    assert player.total_healed == 120        # 10轮 × 再生3回12
    assert player.current_hp == 35           # 40-5 + 10×(12-12)：闭环净零
    assert player.is_alive


def test_cancer_still_caps_the_loop():
    """癌变是天然闸门：累计回复达2×血限即命零，提交再多循环也止步于此。"""
    for cycles in (11, 50):
        engine = _engine_with_loop_spell(f"loop_cap_{cycles}", hp=40, mana=SEED_MANA)
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
