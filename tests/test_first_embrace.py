"""
pytest 风格测试 - 里程碑：初拥之夜（9选1特殊事件）

原文（FIRST_EMBRACE_OPTIONS，见 engine/api.py）：
由终音法器"猩红尖牙"强制触发，也可能在其他场景下被再次触发（封存血脉保留触发权）。
1~8每项限选1次，选择后立即回复30%当前[血限]；9号"封存血脉"不消耗触发权、可重复触发。

覆盖范围：choose_first_embrace 的门槛校验 + 9个选项各自的具体机制效果。
运行方式：
    python -m pytest tests/test_first_embrace.py -v
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tests.attack_support import resolve_attack as resolve_player_attack
from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance


def _new_engine(region="龙心谷", name="老张", speed=8, mana=7, dbsuffix="a"):
    engine = GameEngine(db_path=f"data/test_embrace_{dbsuffix}.db", rng_seed=1,
                         sealed_candidate_path=f"data/test_embrace_{dbsuffix}_sealed.json")
    blood = 25 - speed - mana
    engine.execute_action("setup_attributes",
                           {"blood_points": blood, "speed_points": speed, "mana_points": mana, "name": name})
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": region})
    optional = {"折速法印", "三相残韵盘"}
    choice = next((n for n in setup["result"]["relic_choices"] if n not in optional),
                  setup["result"]["relic_choices"][0])
    engine.execute_action("choose_discovered_relic", {"relic_name": choice})
    engine.state.energy = 0
    engine.state.player.dao_wen["杀伐"] = DaoWenInstance(
        DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    return engine


def _grant(engine, choice):
    engine.state.pending_first_embrace = True
    return engine.execute_action("choose_first_embrace", {"choice": choice})


def finish_round(engine):
    engine.state.combat_subphase = "await_round_end"
    return engine.execute_action("round_end", {})


def _start_battle_with_enemy(engine, hp=50):
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    enemy = Entity(name="怪物甲", entity_type="怪物", blood_limit=hp, current_hp=hp)
    engine.state.enemies.append(enemy)
    engine.execute_action("round_start", {})
    return enemy


def _resolve_full_attack(engine, attacker_name, target):
    return resolve_player_attack(engine, attacker_name, [0])


# ========================================================================
# 门槛校验
# ========================================================================

def test_choose_without_pending_rejected():
    """错误输入：没有待处理的初拥之夜时拒绝"""
    engine = _new_engine(dbsuffix="gate1")
    r = engine.execute_action("choose_first_embrace", {"choice": 1})
    assert r["success"] is False


def test_choice_out_of_range_rejected():
    """边界：choice必须在1~9之间"""
    engine = _new_engine(dbsuffix="gate2")
    engine.state.pending_first_embrace = True
    assert engine.execute_action("choose_first_embrace", {"choice": 0})["success"] is False
    assert engine.execute_action("choose_first_embrace", {"choice": 10})["success"] is False


def test_cannot_reselect_same_trait_1_to_8():
    """错误输入：1~8号每项限选1次，重复选择被拒绝；9号不受此限制"""
    engine = _new_engine(dbsuffix="gate3")
    r1 = _grant(engine, 1)
    assert r1["success"] is True
    r_dup = _grant(engine, 1)
    assert r_dup["success"] is False
    assert "已经选过" in r_dup["error"]

    r9a = _grant(engine, 9)
    assert r9a["success"] is True
    r9b = _grant(engine, 9)
    assert r9b["success"] is True, "9号可重复选择"


def test_heal_30_percent_blood_limit_on_choice():
    """正常路径：无论选哪一项，都立即回复30%当前血限"""
    engine = _new_engine(dbsuffix="heal")
    player = engine.state.player
    player.current_hp = 1
    r = _grant(engine, 7)  # 血影
    expected = math.ceil(player.blood_limit * 0.3)
    assert r["result"]["healed"] == min(expected, player.blood_limit - 1)


def test_option9_seal_blood_lineage_retains_trigger_right():
    """正常路径：9号封存血脉不获得血脉，也不清空pending_first_embrace（保留再次触发权）"""
    engine = _new_engine(dbsuffix="opt9")
    r = _grant(engine, 9)
    assert r["success"] is True
    assert r["result"]["trait"] == "封存血脉"
    assert engine.state.first_embrace_traits == []
    assert engine.state.pending_first_embrace is True


# ========================================================================
# 1. 血族血脉
# ========================================================================

def test_option1_blood_lineage_heals_when_damage_dealt_else_bleeds_20():
    """正常路径：本回合造成过伤害则[回终]回复等量，否则流血20"""
    engine = _new_engine(dbsuffix="bloodline")
    player = engine.state.player
    _grant(engine, 1)
    enemy = _start_battle_with_enemy(engine)
    player.attack_count = 1
    player.attack_power = 5

    _resolve_full_attack(engine, player.name, enemy)
    player.current_hp = min(player.current_hp, player.blood_limit - 5)
    hp_before_heal = player.current_hp
    r = finish_round(engine)
    heal_effects = [e for e in r["result"]["effects"] if e["type"] == "blood_lineage_heal"]
    assert heal_effects, "本回合造成过伤害应触发回复"
    assert player.current_hp > hp_before_heal

    # 第二回合不出手，应改为流血
    engine.execute_action("round_start", {})
    hp_before_bleed = player.current_hp
    r2 = finish_round(engine)
    bleed_effects = [e for e in r2["result"]["effects"] if e["type"] == "blood_lineage_bleed"]
    assert bleed_effects
    assert player.current_hp == hp_before_bleed - 20


# ========================================================================
# 2. 不朽之躯
# ========================================================================

def test_option2_immortal_body_halves_blood_limit_and_blocks_growth():
    """正常路径：立即血限减半；免疫衰老代价；血限无法增加；无法修行突破上限"""
    engine = _new_engine(dbsuffix="immortal")
    player = engine.state.player
    base_bl = player.blood_limit
    r = _grant(engine, 2)
    assert r["success"] is True
    assert player.blood_limit == math.ceil(base_bl / 2)

    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    engine.execute_action("round_start", {})
    engine.state.player.dao_wen["透支"] = DaoWenInstance(
        DaoWen(name="透支", formula="", cost_type="代价", cost_formula="X", effect_formula=""))
    bl_before = player.blood_limit
    r_dw = engine.execute_action("use_daowen", {"daowen_name": "透支", "x": 5})
    assert r_dw["success"] is True
    assert player.blood_limit == bl_before, "不朽之躯应免疫衰老代价"

    r_xiuxing = engine.execute_action("pre_battle_action", {"sub_action": "修行"})
    assert r_xiuxing["success"] is False, "不朽之躯持有者无法修行突破上限"


# ========================================================================
# 3. 鲜血之翼
# ========================================================================

def test_option3_blood_wings_costs_bleed_and_grants_flight():
    """正常路径：流血5X换取【飞行X】"""
    engine = _new_engine(dbsuffix="wings")
    player = engine.state.player
    _grant(engine, 3)
    engine.execute_action("battle_start", {})
    engine.execute_action("round_start", {})
    hp_before = player.current_hp
    r = engine.execute_action("use_blood_wings", {"x": 2})
    assert r["success"] is True
    assert player.current_hp == hp_before - 10
    assert player.get_status_value("飞行") == 2


def test_option3_boundary_x_must_be_positive():
    """边界：x<1应拒绝"""
    engine = _new_engine(dbsuffix="wings_err")
    _grant(engine, 3)
    r = engine.execute_action("use_blood_wings", {"x": 0})
    assert r["success"] is False


# ========================================================================
# 4. 血族尖牙 + 8. 血食
# ========================================================================

def test_option4_vampire_fang_enslaves_weaker_target_as_chizu():
    """正常路径：衰老20，使生命低于自身的目标转化为赤族"""
    engine = _new_engine(dbsuffix="fang")
    player = engine.state.player
    _grant(engine, 4)
    enemy = _start_battle_with_enemy(engine, hp=5)
    bl_before = player.blood_limit

    r = engine.execute_action("enslave_as_chizu", {"target_ref": "enemy:0"})
    assert r["success"] is True
    assert player.blood_limit == bl_before - 20
    assert "怪物甲" in engine.state.chizu_names
    chizu = next(f for f in engine.state.friends if f.name == "怪物甲")
    assert chizu.entity_type == "赤族"
    assert chizu.is_chizu_of == player.name
    assert enemy not in engine.state.enemies


def test_option4_error_target_not_weaker_or_missing():
    """错误输入：目标生命不低于自身，或目标不存在/已死亡"""
    engine = _new_engine(dbsuffix="fang_err")
    _grant(engine, 4)
    _start_battle_with_enemy(engine, hp=99999)
    r = engine.execute_action("enslave_as_chizu", {"target_ref": "enemy:0"})
    assert r["success"] is False

    r2 = engine.execute_action("enslave_as_chizu", {"target_ref": "enemy:99"})
    assert r2["success"] is False


def test_option8_blood_feast_kills_chizu_and_heals_equal_amount():
    """正常路径：命零一名赤族，自身获得等同其当前生命的回复"""
    engine = _new_engine(dbsuffix="feast")
    player = engine.state.player
    _grant(engine, 4)
    _grant(engine, 8)
    _start_battle_with_enemy(engine, hp=5)
    engine.execute_action("enslave_as_chizu", {"target_ref": "enemy:0"})
    chizu = next(f for f in engine.state.friends if f.name == "怪物甲")
    chizu.current_hp = 18
    player.current_hp = 10
    r = engine.execute_action("blood_feast", {"target_ref": "friend:0"})
    assert r["success"] is True
    assert chizu.is_alive is False
    assert player.current_hp == 10 + 18


def test_option8_error_without_trait_or_invalid_chizu():
    """错误输入：没有血食血脉时拒绝；指定的赤族不存在时拒绝"""
    engine = _new_engine(dbsuffix="feast_err")
    r = engine.execute_action("blood_feast", {"target_ref": "friend:99"})
    assert r["success"] is False
    _grant(engine, 8)
    r2 = engine.execute_action("blood_feast", {"target_ref": "friend:99"})
    assert r2["success"] is False


def test_chizu_curse_bleeds_20_each_round_end():
    """正常路径：赤族诅咒——存活的赤族[回终]固定流血20"""
    engine = _new_engine(dbsuffix="curse")
    _grant(engine, 4)
    _start_battle_with_enemy(engine, hp=5)
    engine.execute_action("enslave_as_chizu", {"target_ref": "enemy:0"})
    chizu = next(f for f in engine.state.friends if f.name == "怪物甲")
    chizu.current_hp = 30
    finish_round(engine)
    assert chizu.current_hp == 10


# ========================================================================
# 5. 真理眼
# ========================================================================

def test_option5_truth_eye_raises_interrupt_and_sets_cooldown():
    """正常路径：真理眼是中断类能力，需要DM裁定，不由代码自动判定结果"""
    engine = _new_engine(dbsuffix="eye")
    _grant(engine, 5)
    enemy = _start_battle_with_enemy(engine)
    r = engine.execute_action("use_truth_eye", {"target_ref": "enemy:0", "statement": "你藏着钥匙"})
    assert r["success"] is True
    assert "interrupt" in r["result"]
    assert engine.state.truth_eye_cooldown == 2
    assert len(engine._pending_interrupts) == 1


def test_option5_cooldown_blocks_reuse_and_decrements_per_battle_end():
    """边界：冷却期内再次发动应拒绝；冷却随每次[战终]递减，归零后可再次使用"""
    engine = _new_engine(dbsuffix="eye_cd")
    _grant(engine, 5)
    _start_battle_with_enemy(engine)
    engine.execute_action("use_truth_eye", {"target_ref": "enemy:0", "statement": "第一次"})
    engine.submit_ruling(interrupt_type="自定义", ruling_text="属实")

    r_again = engine.execute_action("use_truth_eye", {"target_ref": "enemy:0", "statement": "立刻再来"})
    assert r_again["success"] is False, "冷却期内应拒绝"

    engine.state.enemies.clear()
    engine.execute_action("battle_end", {})
    assert engine.state.truth_eye_cooldown == 1
    engine.state.phase = "in_combat"  # 构造下一场已结束的合法战终状态
    engine.execute_action("battle_end", {})
    assert engine.state.truth_eye_cooldown == 0

    engine.state.phase = "in_combat"
    engine.state.enemies.clear()
    engine.state.enemies.append(Entity(name="怪物乙", entity_type="怪物", blood_limit=10, current_hp=10))
    r_ok = engine.execute_action("use_truth_eye", {"target_ref": "enemy:0", "statement": "冷却结束"})
    assert r_ok["success"] is True


# ========================================================================
# 6. 寒冰法力
# ========================================================================

def test_option6_frost_mana_reduces_target_action_count_per_10_points():
    """正常路径：持有者对任意目标(含自身)累计施加满10点消耗法力，使其本回合出手次数-1"""
    engine = _new_engine(dbsuffix="ice")
    player = engine.state.player
    _grant(engine, 6)
    enemy = _start_battle_with_enemy(engine, hp=1000)
    r = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 10, "target": "怪物甲"})
    assert r["success"] is True
    assert enemy.mana_inflicted_this_round == 10
    assert enemy.get_status_value("无力") == 1


def test_option6_boundary_below_10_does_not_stack():
    """边界：不足10点时不叠加【无力】，跨越多个10的倍数一次性叠加对应层数"""
    # 提高法限分配，使单次施放能一次性跨越多个10点门槛
    engine = _new_engine(dbsuffix="ice_bound", speed=2, mana=21)
    player = engine.state.player
    player.speed_limit = player.current_speed = 6  # 同回合允许两次施放
    _grant(engine, 6)
    enemy = _start_battle_with_enemy(engine, hp=1000)
    engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 5, "target": "怪物甲"})
    assert enemy.get_status_value("无力") == 0, "未满10点不应叠加"
    engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 25, "target": "怪物甲"})
    # 累计5+25=30，跨越3个10 => 3层
    assert enemy.mana_inflicted_this_round == 30
    assert enemy.get_status_value("无力") == 3


# ========================================================================
# 7. 血影
# ========================================================================

def test_option7_blood_shadow_negates_non_must_hit_attack_by_bleeding_10():
    """正常路径：非必中判定下，可流血10取消本次判定（不同于常规闪避）"""
    engine = _new_engine(dbsuffix="shadow")
    player = engine.state.player
    _grant(engine, 7)
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    attacker = Entity(name="攻击者", entity_type="怪物", blood_limit=30, current_hp=30, attack_power=50)
    engine.state.enemies.append(attacker)
    engine.execute_action("round_start", {})
    player.current_hp = 60
    hp_before = player.current_hp
    result = engine.combat.resolve_attack(attacker, player, is_must_hit=False, blood_shadow=True)
    assert result.get("blood_shadow_success") is True
    assert player.current_hp == hp_before - 10
    assert result["damage_dealt"] == 0


def test_option7_boundary_must_hit_cannot_be_negated_by_blood_shadow():
    """边界：必中判定无法用血影取消"""
    engine = _new_engine(dbsuffix="shadow_must")
    player = engine.state.player
    _grant(engine, 7)
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    attacker = Entity(name="攻击者", entity_type="怪物", blood_limit=30, current_hp=30, attack_power=10)
    engine.state.enemies.append(attacker)
    engine.execute_action("round_start", {})
    player.current_hp = 60
    result = engine.combat.resolve_attack(attacker, player, is_must_hit=True, blood_shadow=True)
    assert "blood_shadow_success" not in result
    assert result["damage_dealt"] == 10
