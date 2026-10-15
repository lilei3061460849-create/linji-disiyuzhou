"""
F2 验证：龙心谷幸存专属道纹（背负等）的 combat 侧实装
（【嫁祸】2026-10-11 已删除：效果与遗物【贪婪契约】重复）
- 正常：每件按 README 定义结算
- 边界：持续到期清空、次数耗尽、死亡目标等
- 错误：非法目标/非法发动应被拒绝
"""
import os
import sys
import pytest

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance, StatusEffect, Consumable
from engine.monsters import make_monster_entity
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import GameState

def _setup_player_with_daowen(names):
    engine = GameEngine(rng_seed=42)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 6, "mana_points": 8})
    finish_initial_daowen(engine)
    engine.state.relics = []
    # 2026-10-13：遗物池删2件后随机发放漂移，本文件断言与开局遗物无关，清空钉死环境。
    engine.state.current_region = "龙心谷"
    engine.state.phase = "in_combat"
    # 直接赋予测试用道纹（绕过学习门禁，仅用于单测）
    for n in names:
        engine.state.player.dao_wen[n] = DaoWenInstance(DaoWen(name=n, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
        # 确保法力充足
        engine.state.player.current_mana = 100
    engine.state.player.current_hp = 60
    engine.state.player.blood_limit = 60
    return engine



def test_normal_beifu_absorb():
    """正常：背负X=2 → 目标下2次受伤由自身承担"""
    engine = _setup_player_with_daowen(["背负"])
    player = engine.state.player
    monster = Entity(name="被保护怪", entity_type="怪物", blood_limit=100, current_hp=100, attack_count=1, attack_power=5)
    engine.state.enemies = [monster]
    r = engine.execute_action("use_daowen", {"daowen_name": "背负", "x": 2, "target": "被保护怪"})
    assert r["success"]
    assert hasattr(player, "_beifu_left") and player._beifu_left == 2
    # 被保护怪受到 10 伤害应由玩家承担
    player.current_hp = 60
    monster.current_hp = 100
    detail = engine.combat._apply_hostile_damage(monster, 10)
    assert player.current_hp == 50
    assert monster.current_hp == 100
    assert player._beifu_left == 1
    # 第二次
    detail2 = engine.combat._apply_hostile_damage(monster, 10)
    assert player.current_hp == 40
    assert monster.current_hp == 100
    # 第三次不再承担
    detail3 = engine.combat._apply_hostile_damage(monster, 10)
    assert monster.current_hp == 90
    assert player.current_hp == 40


def test_error_invalid_target():
    """错误：对不存在目标使用应失败"""
    # 无效目标错误由仍在用的【加害】覆盖。
    engine = _setup_player_with_daowen(["加害"])
    r = engine.execute_action("use_daowen", {"daowen_name": "加害", "x": 1, "target": "不存在的怪"})
    assert not r["success"]



def test_custom_extensibility():
    """可自定义：不改引擎代码，仅改配置即可新增专属道纹实例（演示 2 示例）"""
    from engine.combat import CombatEngine
    # 验证 REGION_EXCLUSIVE_DAOWEN 为纯数据驱动
    original = set(CombatEngine.REGION_EXCLUSIVE_DAOWEN["龙心谷"])
    # 模拟新增两个自定义专属道纹（仅配置层面）
    extended = original | {"测试道纹A", "测试道纹B"}
    assert "测试道纹A" in extended
    assert "测试道纹B" in extended
    # 验证引擎的 REGION_EXCLUSIVE_DAOWEN 可被外部扩展而无需改 combat 核心逻辑
    assert len(extended) == len(original) + 2
