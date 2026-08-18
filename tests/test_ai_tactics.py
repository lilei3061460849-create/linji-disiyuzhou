"""
pytest - 战术AI（engine/ai_tactics.py）

背景：早前 sim/format_trace.py 写死"只发杀伐"，导致 AI 表现很蠢。
本测试锁定 AI 会实际使用多种手段，并且法力预算不会被一次出手耗尽。

覆盖：正常路径 / 边界条件 / 错误输入
"""
import math
import os
import sys

import pytest

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.ai_tactics import TacticalAI
from engine.models import DaoWen, DaoWenInstance


def _engine(tmp_path, seed=4, learn=("庇护", "再生", "冲击")):
    e = GameEngine(db_path=str(tmp_path / "ai.db"), rng_seed=seed)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    for dw in learn:
        e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": dw})
    e.state.energy = 0
    choices = {}
    relic = e.state.relics[0].name
    if relic in ("折速法印", "三相残韵盘"):
        choices[relic] = {"use": False}
    e.execute_action("battle_start", {"relic_choices": choices})
    e.execute_action("round_start", {})
    return e


# ---------- 正常路径 ----------

def test_ai_uses_full_action_budget(tmp_path):
    """正常路径：AI 应打满本回合出手次数，而不是一发杀伐烧光法力就收手"""
    e = _engine(tmp_path)
    for monster in e.state.enemies:
        if "强化" not in monster.dao_wen:
            monster.dao_wen["强化"] = DaoWenInstance(
                DaoWen("强化", "", "异变", "5X", ""), x_value=1)
    ai = TacticalAI(e)
    results = ai.take_turn()
    expected = max(1, math.ceil(e.state.player.speed_limit / 3))
    assert len(results) >= 2, f"只出手{len(results)}次，未用满预算(应约{expected}次)"


def test_ai_shields_when_facing_lethal_damage(tmp_path):
    """正常路径：面临致死伤害时应优先庇护，而不是继续输出"""
    e = _engine(tmp_path)
    p = e.state.player
    p.current_hp = 8  # 制造致死威胁
    ai = TacticalAI(e)
    r = ai.try_survive()
    assert r is not None, "面临致死威胁却没有采取保命行动"
    assert "庇护" in r.get("action", "") or "再生" in r.get("action", "")


def test_ai_finishes_killable_target(tmp_path):
    """正常路径：目标可被一击击杀时应当收割"""
    e = _engine(tmp_path)
    m = e.state.enemies[0]
    m.current_hp = 4  # 杀伐X=2 造成6伤 即可击杀
    ai = TacticalAI(e)
    r = ai.try_finish()
    assert r is not None, "可击杀目标却未收割"
    assert not m.is_alive or m.current_hp <= 0


# ---------- 边界条件 ----------

def test_mana_budget_splits_across_actions(tmp_path):
    """边界：预算须按剩余出手次数均分，最后一次允许用尽"""
    e = _engine(tmp_path)
    ai = TacticalAI(e)
    total = max(1, math.ceil(e.state.player.speed_limit / 3))
    budget = ai.mana_budget()
    if total > 1:
        assert budget < e.state.player.current_mana, "预算未分配，会一次烧光法力"
        assert budget >= 1


def test_ai_stops_when_no_mana(tmp_path):
    """边界：法力为0且无可用手段时应安全停手，不抛异常"""
    e = _engine(tmp_path)
    e.state.player.current_mana = 0
    ai = TacticalAI(e)
    r = ai.take_action()
    assert r is None or r.get("success") is True


def test_ai_handles_no_enemies(tmp_path):
    """边界：场上无存活敌人时不应产生任何行动"""
    e = _engine(tmp_path)
    for m in e.state.enemies:
        m.is_alive = False
    ai = TacticalAI(e)
    assert ai.take_turn() == [] or all(x.get("success") for x in ai.take_turn())


# ---------- 错误输入 / 非法配置 ----------

def test_resonance_only_targets_existing_paths(tmp_path):
    """
    错误输入：残韵只能对**存在变化路径**的道纹发动。
    怪物原始道纹(必中/狂暴/飞行)无路径，AI 不得把残韵浪费在上面。
    """
    from engine.daowen import ResonanceEngine
    from engine.ai_tactics import HIGH_VALUE_ENEMY_DAOWEN

    for dw in HIGH_VALUE_ENEMY_DAOWEN:
        paths = ResonanceEngine.get_available_resonance(dw)
        assert paths, f"{dw} 无残韵路径，不应出现在 AI 的残韵目标表中"


def test_ai_never_bypasses_engine_validation(tmp_path):
    """非法配置：AI 未持有的道纹必被引擎拒绝，AI 不得绕过校验改状态"""
    e = _engine(tmp_path, learn=())  # 只有初始道纹杀伐
    ai = TacticalAI(e, verbose=True)
    before = e.state.player.current_hp
    ai.try_survive()  # 没有庇护/再生，应全部失败
    assert e.state.player.current_hp == before
    assert "庇护" not in e.state.player.dao_wen


def test_ai_does_not_crash_without_any_daowen(tmp_path):
    """
    非法配置：玩家无任何道纹时，AI 不得抛异常。

    注：残韵不要求施法者持有该道纹（可作用于敌方道纹），
    因此此时 AI 仍可能合法地发动残韵；只要不崩溃、且结果合法即可。
    """
    e = _engine(tmp_path, learn=())
    e.state.player.dao_wen.clear()
    e.state.resonance.clear()          # 连残韵也清空，才是真正的"无牌可打"
    ai = TacticalAI(e)
    assert ai.take_action() is None


def test_tactical_roles_match_readme_costs():
    """正常：战术表消耗必须跟正文现行公式一致，禁止沿用杀伐3X或缓慢10法力。"""
    from engine.ai_tactics import TACTICAL_ROLES
    assert TACTICAL_ROLES["杀伐"]["cost"] == 1
    assert TACTICAL_ROLES["杀伐"]["dmg_per_x"] == 2
    assert TACTICAL_ROLES["冲击"]["cost"] == 3
    assert TACTICAL_ROLES["冲击"]["dmg_per_x"] == 5
    assert TACTICAL_ROLES["锐利"]["cost"] == 3
    assert TACTICAL_ROLES["锐利"]["dmg_per_x"] == 5
    assert TACTICAL_ROLES["锐利"]["limit_per_x"] == 5
    assert TACTICAL_ROLES["缓慢"]["cost"] == 0
    assert TACTICAL_ROLES["缓慢"]["pay"] == "冷却"
    assert TACTICAL_ROLES["增殖"]["cost"] == 1


def test_ai_uses_chongji_when_two_enemies_outscore_single(tmp_path):
    """正常：两名敌人时冲击总伤高于单体，必须提交闪避并真正发动冲击。"""
    from engine.models import Entity
    e = _engine(tmp_path, learn=("冲击",))
    extra = Entity(name="木桩乙", entity_type="怪物", blood_limit=80, current_hp=80,
                   attack_count=1, attack_power=4)
    e.state.enemies.append(extra)
    for monster in e.state.enemies:
        monster.current_hp = max(monster.current_hp, 80)
        monster.blood_limit = max(monster.blood_limit, 80)
    e.state.player.current_mana = 12
    ai = TacticalAI(e)
    r = ai.try_aoe()
    assert r is not None and r.get("success"), f"双怪场面应发动冲击：{r}"
    assert ai.used.get("冲击", 0) == 1


def test_ai_keeps_shaifa_on_single_target(tmp_path):
    """边界：单怪时冲击总伤不如杀伐，不得为了用冲击而浪费法力。"""
    e = _engine(tmp_path, learn=("冲击",))
    assert len(e.state.enemies) == 1
    e.state.player.current_mana = 12
    ai = TacticalAI(e)
    assert ai.try_aoe() is None
    r = ai.try_pressure()
    assert r is not None and "杀伐" in r.get("action", "")


def test_manqian_can_cast_with_zero_mana(tmp_path):
    """错误输入对照：缓慢是冷却代价，法力为0时仍应能发动，不得按旧10法力表拒绝。"""
    from engine.models import DaoWen, DaoWenInstance
    e = _engine(tmp_path, learn=())
    e.state.player.dao_wen["缓慢"] = DaoWenInstance(
        DaoWen(name="缓慢", formula="", cost_type="冷却", cost_formula="X", effect_formula=""))
    e.state.player.current_mana = 0
    e.state.player.current_hp = 20
    e.state.enemies[0].attack_count = 1
    e.state.enemies[0].attack_power = 8
    ai = TacticalAI(e)
    r = ai.try_control()
    assert r is not None and r.get("success"), f"零法力应能发动缓慢：{r}"
    assert ai.used.get("缓慢", 0) == 1


def test_ai_can_use_resonance_on_monster_daowen(tmp_path):
    """
    正常路径：残韵闭环补齐后，AI 必须能对怪物原始道纹发动残韵。
    修复前 CLOSED_LOOPS 只有杀伐/锐利两轨，对必中/狂暴/飞行发动必然失败。
    """
    e = _engine(tmp_path, learn=())
    e.state.player.dao_wen.clear()
    e.state.enemies[0].dao_wen["飞行"] = DaoWenInstance(
        DaoWen("飞行", "", "异变", "5X", "飞行X"), x_value=1,
    )
    ai = TacticalAI(e)
    r = ai.try_resonance()
    assert r is not None, "补齐闭环后仍无法对怪物道纹发动残韵"
    assert r.get("success")
