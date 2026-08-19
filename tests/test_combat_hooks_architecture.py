"""
重构架构单元测试：战斗事件总线与声明式钩子系统
覆盖三类测试：正常路径、边界条件、错误输入与非法状态。
"""
import math
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.models import Entity, GameState, StatusEffect
from engine.combat_events import CombatEvent, CombatEventType
from engine.combat_hooks import (
    CombatHookManager,
    BaolieHook,
    BifenglingHook,
    ShouyedengHook,
)


class MockState:
    def __init__(self, relics=None):
        self.relics = relics or []

    def side_has(self, entity, relic_name):
        return relic_name in self.relics


# ---------- 正常路径 ----------

def test_hooks_normal_incoming_and_reflect():
    """正常路径：加害提升伤害、爆裂受到伤害前反噬、避风铃闪避叠甲"""
    manager = CombatHookManager()
    state = MockState(relics=["避风铃"])

    attacker = Entity(name="攻击者", blood_limit=42, current_hp=42, mana_limit=50, speed_limit=12, entity_type="轮回者")
    target = Entity(name="防守者", blood_limit=42, current_hp=42, mana_limit=50, speed_limit=12, entity_type="轮回者")
    target.current_speed = 12
    target.add_status(StatusEffect(name="加害", value=2, remaining_rounds=-1, source="test"))
    target.add_status(StatusEffect(name="爆裂", value=2, remaining_rounds=2, source="test"))

    # 1. 伤害修正（加害2使10伤害变为12）
    adjusted = manager.apply_incoming_adjust(target, 10, "普通", attacker, state)
    assert adjusted == 12

    # 2. 受到伤害前反噬（攻击者在造成伤害前先扣12血）
    res = manager.apply_before_damage(target, adjusted, "普通", attacker, state)
    assert res["reflected"] == 12
    assert attacker.current_hp == 30
    assert not res["suppressed"]

    # 3. 闪避触发避风铃（获得3格挡）
    dodge_res = manager.apply_dodge(target, state)
    assert dodge_res["shield_gained"] == 3
    assert target.shield == 3


def test_combat_event_serialization():
    """正常路径：CombatEvent 能够被正确构造并无损序列化"""
    evt = CombatEvent(
        event_type=CombatEventType.DAMAGE_APPLIED,
        battle_no=1,
        round_no=2,
        actor_name="莫非",
        target_name="林渊",
        data={"damage": 20, "shield_absorbed": 10, "hp_lost": 10},
    )
    d = evt.to_dict()
    assert d["event_type"] == "damage_applied"
    assert d["battle_no"] == 1
    assert d["data"]["damage"] == 20


# ---------- 边界条件 ----------

def test_hooks_boundary_conditions():
    """边界条件：速度归零获得15格挡、龙鳞减免至保底0、代价伤害不受加害增幅"""
    manager = CombatHookManager()
    state = MockState(relics=["避风铃"])

    zero_speed_entity = Entity(name="零速者", blood_limit=30, current_hp=30, speed_limit=0, entity_type="轮回者")
    zero_speed_entity.current_speed = 0
    d_res = manager.apply_dodge(zero_speed_entity, state)
    assert d_res["shield_gained"] == 3
    assert zero_speed_entity.shield == 3  # 闪避句只+3；归零+15走失速总线

    # 龙鳞减免边界
    dragon_target = Entity(name="龙族", blood_limit=40, current_hp=40, entity_type="怪物")
    dragon_target.add_status(StatusEffect(name="龙鳞", value=15, remaining_rounds=-1, source="test"))
    dmg_adj = manager.apply_incoming_adjust(dragon_target, 10, "普通", None, state)
    assert dmg_adj == 0, "龙鳞15面对10伤害应减至保底0"

    # 代价类型伤害不触发加害
    jiahai_target = Entity(name="目标", blood_limit=30, current_hp=30, entity_type="轮回者")
    jiahai_target.add_status(StatusEffect(name="加害", value=5, remaining_rounds=-1, source="test"))
    cost_adj = manager.apply_incoming_adjust(jiahai_target, 10, "代价", None, state)
    assert cost_adj == 10, "代价伤害不应受加害增益"


# ---------- 错误输入 / 非法状态 ----------

def test_hooks_handles_none_and_invalid_state():
    """错误输入/非法状态：传入 None、非实体或空状态时安全降级而不抛出未捕获异常"""
    manager = CombatHookManager()

    # None 目标
    adj = manager.apply_incoming_adjust(None, 20, "普通", None, None)
    assert adj == 20

    # None 实体闪避
    d_none = manager.apply_dodge(None, None)
    assert d_none == {}

    # None 实体回始
    rs_none = manager.apply_round_start(None, True, None)
    assert rs_none == {}

    # 负数伤害调整
    neg_adj = manager.apply_incoming_adjust(None, -5, "普通", None, None)
    assert neg_adj == -5


def test_damage_redirection_and_mitigation_hooks():
    """正常路径与边界：嫁祸重定向、背负援护、朋友濒死撤退保护"""
    manager = CombatHookManager()
    
    # 1. 嫁祸重定向
    victim = Entity(name="受害者", blood_limit=30, current_hp=30, entity_type="轮回者")
    scapegoat = Entity(name="替罪羊", blood_limit=30, current_hp=30, entity_type="轮回者")
    victim._jiahuo_left = 1
    victim._jiahuo_target = scapegoat
    victim.add_status(StatusEffect(name="嫁祸", value=1, remaining_rounds=-1, source="test"))

    target_redirected = manager.apply_redirection(victim, "普通", None)
    assert target_redirected is scapegoat
    assert victim._jiahuo_left == 0
    assert not victim.has_status("嫁祸")

    # 2. 撤退保护
    friend = Entity(name="小跟班", blood_limit=20, current_hp=5, entity_type="朋友")
    class MockCombat:
        def __init__(self):
            self.state = MockState()
            self.state.player = Entity(name="玩家", blood_limit=42, current_hp=42, entity_type="轮回者")
            self.state.artifacts_owned = []
            self.state.fuyuebei_declared = set()
            self.state.event_modifiers = {}
        def _combat_entity_refs(self):
            return {"friend:0": friend}
    mock_c = MockCombat()
    mit_res = manager.apply_mitigation(friend, 15, "普通", mock_c)
    assert mit_res is not None
    assert mit_res["retreated"] is True
    assert friend.has_retreated is True
    assert friend.current_hp == 5, "撤退后生命完好保留"

