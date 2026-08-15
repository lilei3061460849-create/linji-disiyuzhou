"""命令朋友/员工替你扛伤（护卫命令）测试。

引擎机制：背负X（龙心谷道纹）「选择目标，其下X次受到伤害由自身承担」；
伤害重定向在 combat._apply_hostile_damage 实装（龙心谷 F2）。
command_ally 支持「发动背负 打 轮回者/我」——道纹指令目标允许指向我方单位。

覆盖：正常路径 / 别名 / 边界 / 错误输入
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity
from sim.build_learner import round_start_relic_choices


def _engine(suffix: str, region: str = "乱葬岗") -> GameEngine:
    e = GameEngine(db_path=f"data/test_guard_{suffix}.db", rng_seed=1,
                   sealed_candidate_path="/tmp/guard_test.json")
    e.execute_action("setup_attributes", {"name": "贾凡", "blood_points": 10,
                                          "speed_points": 8, "mana_points": 7})
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": region})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    return e


def _friend_beifu(name="岩行者", x=1, hp=54, atk_count=2, atk_power=4) -> Entity:
    fr = Entity(name, "朋友", blood_limit=hp, current_hp=hp,
                attack_count=atk_count, attack_power=atk_power)
    fr.dao_wen["背负"] = DaoWenInstance(
        DaoWen(name="背负", formula="", cost_type="", cost_formula="", effect_formula=""), x_value=x)
    return fr


def _start_battle_with(engine, monster):
    engine.state.energy = 0
    engine.state.enemies.append(monster)
    active = {r.name for r in engine.state.relics if engine.state.sealed_relics.get(r.name, 0) <= 0}
    bs = engine.execute_action("battle_start", {"relic_choices": {
        n: {"use": False} for n in ("三相残韵盘", "折速法印", "猩红果实", "苍白之花") if n in active}})
    assert bs["success"], bs
    engine.execute_action("round_start", {"relic_choices": round_start_relic_choices(engine)})


def _resolve_monster_phase(engine):
    from sim.build_learner import _resolve_monster_turn
    return _resolve_monster_turn(engine)


def test_command_guard_with_beifu_redirects_damage():
    """正常路径：命令岩行者「发动背负 打 轮回者」，怪物打玩家的伤害转给岩行者。"""
    e = _engine("guard_ok")
    fr = _friend_beifu()
    e.state.friends.append(fr)
    m = Entity("血僵", "怪物", blood_limit=270, current_hp=270,
               attack_count=4, attack_power=19)
    _start_battle_with(e, m)
    p = e.state.player
    hp_before = p.current_hp
    fr_hp_before = fr.current_hp

    r = e.execute_action("command_ally", {
        "ally_ref": "friend:0", "instruction": "发动背负 打 轮回者"})
    assert r["success"], r
    # 背负注册：岩行者._beifu_left >= 1 且目标=玩家
    assert getattr(fr, "_beifu_left", 0) >= 1
    assert getattr(fr, "_beifu_target", None) is p

    # 怪物阶段：血僵打玩家，伤害应转给岩行者
    mp = _resolve_monster_phase(e)
    assert mp["success"], mp
    # 玩家伤害承受显著小于怪物总输出（被转伤）
    player_lost = hp_before - p.current_hp
    fr_lost = fr_hp_before - fr.current_hp
    assert fr_lost > 0, "岩行者应替玩家承受伤害"
    assert player_lost < 4 * 19, "玩家承受应小于怪物全部输出（有转伤）"
    assert not p.is_alive or p.current_hp > 0


def test_command_guard_alias_me():
    """正常路径：指令用「我」别名也能指向玩家。"""
    e = _engine("guard_alias")
    fr = _friend_beifu()
    e.state.friends.append(fr)
    m = Entity("蛆冢", "怪物", blood_limit=270, current_hp=270,
               attack_count=6, attack_power=9)
    _start_battle_with(e, m)
    r = e.execute_action("command_ally", {
        "ally_ref": "friend:0", "instruction": "发动背负 打 我"})
    assert r["success"], r
    assert getattr(fr, "_beifu_target", None) is e.state.player


def test_command_guard_unknown_target_rejected():
    """错误输入：目标名不存在时拒绝且不改状态。"""
    e = _engine("guard_bad")
    fr = _friend_beifu()
    e.state.friends.append(fr)
    m = Entity("血僵", "怪物", blood_limit=270, current_hp=270,
               attack_count=4, attack_power=19)
    _start_battle_with(e, m)
    r = e.execute_action("command_ally", {
        "ally_ref": "friend:0", "instruction": "发动背负 打 不存在的人"})
    assert not r["success"]
    assert getattr(fr, "_beifu_left", 0) == 0


def test_command_guard_attack_still_enemy_only():
    """边界：攻击指令仍只能指向敌方（不能攻击自己人）。"""
    e = _engine("guard_atk")
    fr = _friend_beifu()
    e.state.friends.append(fr)
    m = Entity("血僵", "怪物", blood_limit=270, current_hp=270,
               attack_count=4, attack_power=19)
    _start_battle_with(e, m)
    r = e.execute_action("command_ally", {
        "ally_ref": "friend:0", "instruction": "攻击 轮回者"})
    assert not r["success"], "攻击指令不得指向我方"


# ========================================================================
# 护卫指令（无消耗强制挡伤）
# ========================================================================

def test_guard_command_forces_redirect_without_cost():
    """正常路径：命令盟友「护卫 X」→ 无消耗强制背负，怪物打玩家的伤害转给盟友。"""
    e = _engine("guard_cmd_ok")
    fr = _friend_beifu()
    fr.dao_wen.clear()  # 盟友无道纹也要能护卫（强制）
    e.state.friends.append(fr)
    m = Entity("血僵", "怪物", blood_limit=270, current_hp=270,
               attack_count=4, attack_power=19)
    _start_battle_with(e, m)
    p = e.state.player
    actions_before = fr.actions_used_this_round
    r = e.execute_action("command_ally", {
        "ally_ref": "friend:0", "instruction": "护卫 4"})
    assert r["success"], r
    assert getattr(fr, "_beifu_left", 0) == 4
    assert getattr(fr, "_beifu_target", None) is p
    # 无消耗：盟友出手次数不变
    assert fr.actions_used_this_round == actions_before

    hp0, frhp0 = p.current_hp, fr.current_hp
    mp = _resolve_monster_phase(e)
    assert mp["success"], mp
    player_lost = hp0 - p.current_hp
    fr_lost = frhp0 - fr.current_hp
    assert fr_lost > 0, "盟友应替玩家承受伤害"
    assert player_lost == 0, "护卫生效时玩家本回合应无伤"
    assert getattr(fr, "_beifu_left", 0) < 4, "每命中消耗1次护卫次数"


def test_guard_command_default_one():
    """正常路径：护卫缺省X=1，挡1次。"""
    e = _engine("guard_cmd_one")
    fr = _friend_beifu()
    e.state.friends.append(fr)
    m = Entity("血僵", "怪物", blood_limit=270, current_hp=270,
               attack_count=4, attack_power=19)
    _start_battle_with(e, m)
    r = e.execute_action("command_ally", {
        "ally_ref": "friend:0", "instruction": "护卫"})
    assert r["success"], r
    assert getattr(fr, "_beifu_left", 0) == 1


def test_guard_command_bad_x_rejected():
    """错误输入：护卫次数非1~9整数时拒绝且不施加。"""
    e = _engine("guard_cmd_bad")
    fr = _friend_beifu()
    e.state.friends.append(fr)
    m = Entity("血僵", "怪物", blood_limit=270, current_hp=270,
               attack_count=4, attack_power=19)
    _start_battle_with(e, m)
    for bad in ("护卫 0", "护卫 10", "护卫 三", "护卫 abc"):
        r = e.execute_action("command_ally", {"ally_ref": "friend:0", "instruction": bad})
        assert not r["success"], bad
    assert getattr(fr, "_beifu_left", 0) == 0
