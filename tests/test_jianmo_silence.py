"""【缄默】实装回归（报告 P4·5.3 元机制类；DM 裁定：按正文定义实装）。

正文口径（`engine/daowen.py::calculate_qianmo`）：
    缄默X：消耗X。使**场上所有**由[命零]触发的效果无法触发，持续X。

实装前它是一条死状态：状态只盖在 `wave_status_targets`（无目标道纹的兜底＝施法者
自己），而引擎里没有任何消费点——尸爆的[命零]AoE、招魂的尸体入账、焦黑发丝的
命零反应全部照跑。本文件钉住实装后的施加面、四个消费面、到期恢复与数据不变量：

1. 施加面：状态盖满全场（敌我都带），不再只盖施法者；怪物施放也能封到轮回者；
2. 命零反应：焦黑发丝（怪物命零→玩家速度+2）被封禁，死亡上下文带 `silenced_death`；
3. 尸体入账：封禁期内命零的怪物不进 `dead_monsters`（招魂无尸可唤）；
4. 尸爆：整条都是「由[命零]触发的效果」→ 不产 AoE、不自毁（法力已付不退）；
5. 到期：持续X回合走完 `tick_status_effects` 后，上述效果全部恢复；
6. 死者自身：缄默持有者自己命零时其[命零]效果同样被封（否则它一死封禁就当场失效）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.daowen import DaoWenEngine
from engine.dice import DiceEngine
from engine.enums import CostType
from engine.models import Entity, GameState, Relic

DaoWenEngine.register_all()


def _battle(*, player_speed=5, player_speed_limit=7, monster_blood=40, relics=()):
    """最小战斗：轮回者 沈昼 vs 怪物 骨天使（速度留 2 点余量，否则焦黑发丝 +2 被速限吃掉）。"""
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("沈昼", "轮回者", blood_limit=60, current_hp=60,
                    mana_limit=20, current_mana=20,
                    speed_limit=player_speed_limit, current_speed=player_speed)
    monster = Entity("骨天使", "怪物", blood_limit=monster_blood, current_hp=monster_blood,
                     mana_limit=7, current_mana=7, speed_limit=3, current_speed=3)
    state.player = player
    state.enemies = [monster]
    state.relics = list(relics)
    return state, CombatEngine(state, DiceEngine()), player, monster


def _cast_silence(combat, caster, x=2):
    return combat.apply_daowen_effect("缄默", DaoWenEngine.calculate_qianmo(x), caster, caster)


def _kill_monster(combat, player, monster, amount=99):
    """走统一伤害管线击灭怪物（命零判定与死亡通知都在里面）。"""
    return combat._apply_hostile_damage(monster, amount, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": monster, "mechanic": "damage", "subtype": "daowen",
        "amount": amount, "tags": {"daowen"}, "event_id": "damage-kill-jianmo",
    })


def _cast_shibao(combat, caster, x=1):
    return combat.apply_daowen_effect("尸爆", DaoWenEngine.calculate_shibao(x), caster, caster)


# ---------------------------------------------------------------- 1. 施加面

def test_silence_stamps_every_entity_on_the_field():
    state, combat, player, monster = _battle()

    result = _cast_silence(combat, player, x=2)

    assert player.has_status("缄默")
    assert monster.has_status("缄默"), "「场上所有」必须包含敌方，不能只盖施法者"
    effect = next(e for e in result["effects"] if e["type"] == "qianmo")
    assert effect["scope"] == "field"
    assert set(effect["targets"]) == {"沈昼", "骨天使"}
    assert effect["duration"] == 2


def test_monster_cast_silence_also_seals_the_reincarnator():
    state, combat, player, monster = _battle()

    _cast_silence(combat, monster, x=1)

    assert player.has_status("缄默")
    assert combat._death_triggers_silenced(player) is True


# ---------------------------------------------------------------- 2. 命零反应

def test_silenced_death_grants_no_charred_hair_speed():
    state, combat, player, monster = _battle(relics=[Relic("焦黑发丝", "")])
    _cast_silence(combat, player, x=2)

    detail = _kill_monster(combat, player, monster)

    assert detail["died"] is True and monster.is_alive is False
    assert player.current_speed == 5, "【缄默】生效：焦黑发丝的命零反应不得触发"
    assert all(e["source"] != "焦黑发丝"
               for e in getattr(player, "_speed_change_events", []))
    assert "silenced_death" in monster._death_ctx["tags"]


def test_control_charred_hair_still_triggers_without_silence():
    state, combat, player, monster = _battle(relics=[Relic("焦黑发丝", "")])

    _kill_monster(combat, player, monster)

    assert player.current_speed == 7
    assert player._speed_change_events[-1]["source"] == "焦黑发丝"
    assert "silenced_death" not in monster._death_ctx["tags"]


# ---------------------------------------------------------------- 3. 尸体入账

def test_silenced_corpse_is_not_recorded_for_zhaohun():
    state, combat, player, monster = _battle()
    _cast_silence(combat, player, x=2)

    _kill_monster(combat, player, monster)

    assert not state.dead_monsters, "封禁期内命零的怪物不入尸体账，招魂无尸可唤"


def test_control_corpse_is_recorded_without_silence():
    state, combat, player, monster = _battle()

    _kill_monster(combat, player, monster)

    assert monster in state.dead_monsters


# ---------------------------------------------------------------- 4. 尸爆

def test_silence_blocks_shibao_aoe_and_self_destruct():
    state, combat, player, monster = _battle()
    _cast_silence(combat, player, x=2)
    hp_before = monster.current_hp

    result = _cast_shibao(combat, player, x=1)

    assert result.get("silenced") is True
    assert monster.current_hp == hp_before, "【缄默】生效：尸爆的[命零]AoE 不得落地"
    assert player.is_alive is True, "尸爆的自毁式命零同属[命零]触发效果，一并封禁"
    assert any(e["type"] == "qianmo_blocked" for e in result["effects"])


def test_control_shibao_still_explodes_without_silence():
    state, combat, player, monster = _battle(monster_blood=40)

    result = _cast_shibao(combat, player, x=1)

    assert result.get("self_destructed") is True
    assert player.is_alive is False
    assert monster.current_hp == 40 - 6, "血限60×10%＝6 点 AoE"


# ---------------------------------------------------------------- 5. 到期恢复

def test_silence_expires_after_its_duration():
    state, combat, player, monster = _battle(relics=[Relic("焦黑发丝", "")])
    _cast_silence(combat, player, x=1)

    for entity in (player, monster):
        entity.tick_status_effects()

    assert not player.has_status("缄默")
    assert combat._death_triggers_silenced(player) is False
    _kill_monster(combat, player, monster)
    assert player.current_speed == 7, "到期后命零反应恢复"
    assert monster in state.dead_monsters


def test_silence_still_holds_before_duration_runs_out():
    state, combat, player, monster = _battle()
    _cast_silence(combat, player, x=2)

    player.tick_status_effects()
    monster.tick_status_effects()

    assert player.has_status("缄默"), "持续2回合：只走过1回合时封禁仍在"
    assert combat._death_triggers_silenced(monster) is True


# ---------------------------------------------------------------- 6. 死者自身

def test_silence_holder_own_death_is_also_silenced():
    state, combat, player, monster = _battle(relics=[Relic("焦黑发丝", "")])
    # 只剩怪物自己带【缄默】：它命零时已 is_alive=False，不在全场存活名单里，
    # 若判定不含死者自身，封禁会在它死亡的那一刻失效。
    _cast_silence(combat, monster, x=2)
    player.tick_status_effects()   # 持续2回合：走完两拍，轮回者身上那份到期
    player.tick_status_effects()
    assert not player.has_status("缄默")
    assert monster.has_status("缄默")

    _kill_monster(combat, player, monster)

    assert player.current_speed == 5
    assert not state.dead_monsters


# ---------------------------------------------------------------- 数据不变量

def test_qianmo_calc_matches_its_rule_text():
    calc = DaoWenEngine.calculate_qianmo(3)
    doc = DaoWenEngine.calculate_qianmo.__doc__

    assert calc["cost_type"] == CostType.MANA.value and calc["cost"] == 3, "消耗X（1X 档）"
    assert calc["duration"] == 3
    assert calc["silence_death_triggers"] is True, "消费点认的就是这个键"
    assert "场上所有" in doc and "持续X" in doc
    assert "全场" in calc["summary"] and "持续3回合" in calc["summary"]
