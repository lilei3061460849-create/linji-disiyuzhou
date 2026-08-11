"""
pytest - AI 道纹多样性（数据驱动，不写死道纹名）

背景：早前 AI 把"杀伐/庇护/再生/冲击/锐利"硬编码在 if 分支里，
无法测试锐利系与副本专属道纹。现改为 TACTICAL_ROLES 数据驱动。

覆盖：正常路径 / 边界条件 / 错误输入
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.ai_tactics import TacticalAI, TACTICAL_ROLES


def _engine(starter="杀伐", learn=(), region="龙心谷", seed=1, tmp="/tmp/bv.db"):
    e = GameEngine(db_path=tmp, rng_seed=seed)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    e.execute_action("setup_choose_daowen", {"daowen": starter})
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": region})
    for dw in learn:
        e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": dw})
    e.execute_action("battle_start")
    e.execute_action("round_start", {})
    return e


# ---------- 正常路径 ----------

def test_ai_uses_ruili_when_it_is_the_only_nuke():
    """正常路径：只有锐利时，AI 必须用锐利（而不是因为找不到杀伐就罢工）"""
    e = _engine(starter="锐利")
    ai = TacticalAI(e, verbose=True)
    ai.take_turn()
    assert ai.used.get("锐利", 0) > 0, f"未使用锐利，log={ai.log}"


@pytest.mark.parametrize("dw", ["加害", "裂变", "伤痕", "僵化", "坏死", "逼债", "洗劫"])
def test_ai_uses_region_specific_daowen(dw):
    """正常路径：各副本专属道纹只要持有就应被实际发动"""
    e = _engine(starter="杀伐", learn=[dw])
    e.state.player.current_mana = 99
    ai = TacticalAI(e)
    for _ in range(3):
        ai.new_round()
        ai.take_turn()
    assert ai.used.get(dw, 0) > 0, f"{dw} 从未被使用"


def test_every_tactical_role_is_reachable():
    """正常路径：表中每个 role 都有对应的 owned() 查询路径"""
    roles = {v["role"] for v in TACTICAL_ROLES.values()}
    e = _engine()
    ai = TacticalAI(e)
    for r in roles:
        ai.owned(r)  # 不应抛异常


# ---------- 边界条件 ----------

def test_nuke_ranked_prefers_higher_damage_per_budget():
    """边界：小预算下应选性价比更高者（杀伐2伤/法 > 锐利1.33伤/法）"""
    e = _engine(starter="锐利", learn=["杀伐"])
    ai = TacticalAI(e)
    ranked = ai._nuke_ranked(4)
    assert ranked[0][0] == "杀伐", f"预算4时应优先杀伐，实际 {ranked}"


def test_control_not_repeated_on_same_target_in_one_round():
    """边界：同一回合不得对同一目标重复施加控制（控制不叠加，重复即浪费）"""
    e = _engine(starter="锐利", learn=["束缚"])
    e.state.player.current_mana = 99
    ai = TacticalAI(e)
    ai.new_round()
    first = ai.try_control()
    second = ai.try_control()
    if first is not None:
        assert second is None, "同回合对同一目标重复上控"


def test_new_round_resets_control_bookkeeping():
    """边界：new_round 后应可再次控制同一目标"""
    e = _engine()
    ai = TacticalAI(e)
    ai._controlled_this_round.add("某怪")
    ai.new_round()
    assert ai._controlled_this_round == set()


# ---------- 错误输入 ----------

def test_all_tactical_roles_reference_registered_daowen():
    """错误输入检出：战术表里不得出现引擎未注册的道纹（否则AI必然空转）"""
    from engine.daowen import DaoWenEngine
    DaoWenEngine.register_all()
    unknown = [n for n in TACTICAL_ROLES if n not in DaoWenEngine._registry]
    assert not unknown, f"战术表引用了未注册的道纹：{unknown}"


def test_tactical_table_entries_wellformed():
    """非法配置：每条战术表项必须有合法 role 与非负 cost"""
    valid = {"nuke", "aoe", "shield", "heal", "control", "debuff", "buff", "ramp", "remove"}
    for name, info in TACTICAL_ROLES.items():
        assert info.get("role") in valid, f"{name} 的 role 非法：{info.get('role')}"
        assert info.get("cost", 0) >= 0, f"{name} 的 cost 为负"


def test_ai_skips_daowen_it_does_not_own():
    """错误输入：未持有的道纹不得被发动"""
    e = _engine(starter="杀伐")
    ai = TacticalAI(e, verbose=True)
    for _ in range(3):
        ai.new_round()
        ai.take_turn()
    assert "封印" not in ai.used
    assert "僵化" not in ai.used


# ---------- 贯穿（无视格挡）回归 ----------

def test_pierce_bypasses_shield():
    """
    正常路径：贯穿/无视格挡 的伤害不得被格挡吸收。
    此前 take_damage 只豁免"代价"，导致【贯穿】完全失效，
    格挡因此成为无解的万能防御，是公式化的成因之一。
    """
    from engine.models import Entity
    t = Entity(name="靶", entity_type="怪物", blood_limit=100, current_hp=100)
    t.shield = 50
    d = t.take_damage(20, "无视格挡")
    assert d["shield_absorbed"] == 0, "无视格挡的伤害不该被格挡吸收"
    assert d["actual_damage"] == 20
    assert t.shield == 50, "格挡不应被消耗"


def test_normal_damage_still_blocked():
    """边界：普通伤害必须仍然被格挡正常吸收（不能改坏原有规则）"""
    from engine.models import Entity
    t = Entity(name="靶", entity_type="怪物", blood_limit=100, current_hp=100)
    t.shield = 50
    d = t.take_damage(20, "普通")
    assert d["shield_absorbed"] == 20
    assert d["actual_damage"] == 0
    assert t.shield == 30


def test_cost_damage_still_ignores_shield():
    """边界：代价类伤害绝对不被格挡吸收（README 明确规定）"""
    from engine.models import Entity
    t = Entity(name="靶", entity_type="怪物", blood_limit=100, current_hp=100)
    t.shield = 50
    d = t.take_damage(15, "代价")
    assert d["shield_absorbed"] == 0
    assert d["actual_damage"] == 15
    assert t.shield == 50


def test_pierce_status_drives_attack_resolution():
    """正常路径：持有【贯穿】状态的攻击者，其攻击应无视目标格挡"""
    from engine.api import GameEngine
    from engine.models import StatusEffect
    e = GameEngine(db_path="/tmp/pierce.db", rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    e.execute_action("setup_choose_daowen", {"daowen": "杀伐"})
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.execute_action("battle_start")
    e.execute_action("round_start", {})
    p, m = e.state.player, e.state.enemies[0]
    p.shield = 60
    m.add_status(StatusEffect(name="贯穿", value=1, remaining_rounds=-1, source="test"))
    r = e.combat.resolve_attack(m, p, dodge=False)
    assert r["hp_lost"] > 0, "贯穿攻击应造成实际生命损失"
    assert r["shield_absorbed"] == 0
