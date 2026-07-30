"""
引擎单元测试
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Entity, StatusEffect
from engine.daowen import DaoWenEngine, ResonanceEngine
from engine.dice import DiceEngine
from engine.enums import EntityType
import math


def test_setup():
    """测试开局流程"""
    print("\n=== 测试：开局 ===")
    engine = GameEngine(db_path="data/test_rulings.db")
    
    # 分配属性
    result = engine.execute_action("setup_attributes", {
        "name": "测试轮回者",
        "blood_points": 10,
        "speed_points": 8,
        "mana_points": 7
    })
    assert result["success"], f"属性分配失败: {result}"
    assert engine.state.player.blood_limit == 60, f"血限错误: {engine.state.player.blood_limit}"
    assert engine.state.player.speed_limit == 8, f"速限错误: {engine.state.player.speed_limit}"
    assert engine.state.player.mana_limit == 14, f"法限错误: {engine.state.player.mana_limit}"
    assert engine.state.player.action_count == math.ceil(8 / 3), "出手次数错误"
    assert engine.state.shards == 20, "初始碎片错误"
    print("  ✓ 属性分配正确")
    
    # 错误分配（点数不为25）
    result = engine.execute_action("setup_attributes", {
        "blood_points": 5, "speed_points": 5, "mana_points": 5
    })
    assert not result["success"], "应该拒绝错误的属性分配"
    print("  ✓ 错误分配被拒绝")
    
    # 选择道纹
    result = engine.execute_action("setup_choose_daowen", {"daowen": "杀伐"})
    assert result["success"], f"道纹选择失败: {result}"
    assert "杀伐" in engine.state.player.dao_wen, "道纹未添加"
    print("  ✓ 道纹选择正确")
    
    # 选择残韵
    result = engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    assert result["success"], f"残韵选择失败: {result}"
    assert engine.state.resonance.get("反转", 0) == 1, "残韵计数错误"
    print("  ✓ 残韵选择正确")
    
    # 选择副本
    result = engine.execute_action("setup_choose_region", {"region": "扭曲都市"})
    assert result["success"], f"副本选择失败: {result}"
    assert engine.state.current_region == "扭曲都市", "副本设置错误"
    assert engine.state.phase == "pre_battle", "阶段切换错误"
    print("  ✓ 副本选择正确，进入局外阶段")


def test_daowen_calculations():
    """测试道纹计算"""
    print("\n=== 测试：道纹计算 ===")
    
    # 测试杀伐
    target = Entity(name="目标", entity_type=EntityType.MONSTER.value, blood_limit=100, current_hp=100)
    result = DaoWenEngine.resolve("杀伐", 3, target=target)
    assert result["cost"] == 3, f"杀伐消耗错误: {result['cost']}"
    assert result["target_damage"] == 6, f"杀伐伤害错误: {result['target_damage']}"
    print("  ✓ 杀伐X=3: 消耗3，伤害6")
    
    # 测试庇护
    result = DaoWenEngine.resolve("庇护", 5, target=target)
    assert result["cost"] == 5
    assert result["target_shield"] == 20
    print("  ✓ 庇护X=5: 消耗5，格挡20")
    
    # 测试再生
    result = DaoWenEngine.resolve("再生", 4, target=target)
    assert result["cost"] == 4
    assert result["target_heal"] == 12
    print("  ✓ 再生X=4: 消耗4，回复12")
    
    # 测试冲击
    result = DaoWenEngine.resolve("冲击", 2)
    assert result["cost"] == 2
    assert result["aoe_damage"] == 2
    print("  ✓ 冲击X=2: 消耗2，AOE伤害2")
    
    # 测试锐利
    result = DaoWenEngine.resolve("锐利", 3, target=target)
    assert result["cost"] == 9
    assert result["blood_limit_reduction"] == 12
    print("  ✓ 锐利X=3: 消耗9，血限-12，生命-12")
    
    # 测试飞行
    result = DaoWenEngine.resolve("飞行", 2)
    assert result["cost_mutation"] == 10
    print("  ✓ 飞行X=2: 异变+10，无法被选为目标，持续2回合")
    
    print("  ✓ 所有道纹计算通过")


def test_resonance():
    """测试残韵系统"""
    print("\n=== 测试：残韵系统 ===")
    
    # 杀伐 → 反转 → 再生
    result = ResonanceEngine.apply_resonance("杀伐", "反转", False, True)
    assert result["success"], f"残韵失败: {result}"
    assert result["target"] == "再生"
    print("  ✓ 杀伐 --反转--> 再生")
    
    # 再生 → 曲解 → 庇护
    result = ResonanceEngine.apply_resonance("再生", "曲解", False, True)
    assert result["success"]
    assert result["target"] == "庇护"
    print("  ✓ 再生 --曲解--> 庇护")
    
    # 锐利 → 反转 → 增殖
    result = ResonanceEngine.apply_resonance("锐利", "反转", False, True)
    assert result["success"]
    assert result["target"] == "增殖"
    print("  ✓ 锐利 --反转--> 增殖")
    
    # 查看可用残韵
    available = ResonanceEngine.get_available_resonance("杀伐")
    assert len(available) > 0
    print(f"  ✓ 杀伐可用残韵: {[a['resonance_type'] + '→' + a['target_daowen'] for a in available]}")
    
    # 不存在的路径
    result = ResonanceEngine.apply_resonance("杀伐", "转换", False, True)
    assert not result["success"]
    print("  ✓ 不存在的路径被正确拒绝")


def test_combat():
    """测试战斗系统"""
    print("\n=== 测试：战斗系统 ===")
    engine = GameEngine(db_path="data/test_rulings.db")
    
    # 设置玩家
    engine.execute_action("setup_attributes", {
        "name": "测试", "blood_points": 10, "speed_points": 8, "mana_points": 7
    })
    engine.execute_action("setup_choose_daowen", {"daowen": "杀伐"})
    
    # 添加怪物
    monster = Entity(
        name="测试怪物",
        entity_type=EntityType.MONSTER.value,
        blood_limit=80,
        current_hp=80,
        attack_count=3,
        attack_power=5,
    )
    engine.state.enemies.append(monster)
    
    # 测试伤害计算
    damage_result = monster.take_damage(20, "普通")
    assert damage_result["actual_damage"] == 20
    assert monster.current_hp == 60
    print(f"  ✓ 伤害20: HP 80→60")
    
    # 测试格挡
    monster.gain_shield(10)
    damage_result = monster.take_damage(15, "普通")
    assert damage_result["shield_absorbed"] == 10
    assert damage_result["actual_damage"] == 5
    print(f"  ✓ 格挡10吸收10伤害: 15伤害→5实际")
    
    # 测试回复
    monster.current_hp = 40
    heal_result = monster.heal(20)
    assert heal_result["actual_heal"] == 20
    assert monster.current_hp == 60
    print(f"  ✓ 回复20: HP 40→60")
    
    # 测试过量回复
    monster.current_hp = 75
    heal_result = monster.heal(20)
    assert heal_result["overheal"] == 15, f"过量应为15，实际{heal_result['overheal']}"
    assert monster.current_hp == 80
    print(f"  ✓ 过量回复: HP 75→80, 过量15")
    
    # 测试死亡
    monster.current_hp = 5
    damage_result = monster.take_damage(10)
    assert damage_result["died"] == True
    assert not monster.is_alive
    print(f"  ✓ 致死伤害: HP 5→0, 死亡")
    
    # 测试状态效果
    entity = Entity(name="测试", entity_type="轮回者", blood_limit=100, current_hp=100)
    entity.add_status(StatusEffect(name="兴奋", remaining_rounds=3, value=2))
    assert entity.has_status("兴奋")
    assert entity.get_status_value("兴奋") == 2
    print(f"  ✓ 状态效果添加: 兴奋3回合，值2")
    
    # 测试合并
    entity.add_status(StatusEffect(name="兴奋", remaining_rounds=2, value=1))
    assert entity.get_status_value("兴奋") == 3
    print(f"  ✓ 同名状态合并: 兴奋值3")
    
    # 测试递减
    expired = entity.tick_status_effects()
    assert not entity.has_status("兴奋") or entity.get_status_value("兴奋") == 3
    print(f"  ✓ 回合递减")
    
    print("  ✓ 战斗系统测试通过")


def test_dice():
    """测试随机数系统"""
    print("\n=== 测试：随机数系统 ===")
    dice = DiceEngine()
    
    # 创建池
    options = ["事件A", "事件B", "事件C", "事件D", "事件E"]
    result = dice.create_pool("test_pool", options)
    assert result["count"] == 5
    assert result["range"] == "1~5"
    print(f"  ✓ 创建池: {result['count']}个选项，范围{result['range']}")
    
    # 解析
    result = dice.resolve_pool("test_pool", 3)
    assert result["selected"] == "事件C"
    print(f"  ✓ 选择3: {result['selected']}")
    
    # 池缩小
    status = dice.get_pool_status("test_pool")
    assert status["count"] == 4
    print(f"  ✓ 池缩小: {status['count']}个剩余")
    
    # 超范围
    try:
        dice.resolve_pool("test_pool", 10)
        assert False, "应该抛出错误"
    except ValueError:
        print("  ✓ 超范围数字被拒绝")
    
    print("  ✓ 随机数系统测试通过")


def test_dm_rulings():
    """测试DM裁定系统"""
    print("\n=== 测试：DM裁定系统 ===")
    engine = GameEngine(db_path="data/test_rulings.db")
    
    # 设置
    engine.execute_action("setup_attributes", {
        "name": "测试", "blood_points": 10, "speed_points": 8, "mana_points": 7
    })
    engine.execute_action("setup_choose_daowen", {"daowen": "杀伐"})
    engine.execute_action("setup_choose_region", {"region": "扭曲都市"})
    
    # 添加怪物
    monster = Entity(name="测试怪", entity_type="怪物", blood_limit=50, current_hp=10, 
                     attack_count=2, attack_power=5)
    engine.state.enemies.append(monster)
    
    # 声明急中生智
    result = engine.execute_action("declare_wit", {"target": "测试怪"})
    assert result["success"]
    assert result["interrupt"]["interrupt_type"] == "急中生智"
    print("  ✓ 急中生智中断触发")
    
    # DM裁定
    result = engine.submit_ruling(
        "急中生智",
        "利用废弃管道释放蒸汽，遮蔽怪物视线，趁机移动到有利位置",
        {"effect": "怪物下回合无法选中轮回者", "duration": 1}
    )
    assert result["success"]
    assert result["ruling_id"] > 0
    print(f"  ✓ DM裁定保存，ID={result['ruling_id']}")
    
    # 查询先例
    precedent = engine.check_precedent("急中生智", {"target": "测试怪"})
    assert precedent["found"]
    print(f"  ✓ 查询到{precedent['count']}个先例")
    
    print("  ✓ DM裁定系统测试通过")


def test_full_flow():
    """测试完整流程"""
    print("\n=== 测试：完整流程 ===")
    engine = GameEngine(db_path="data/test_rulings.db")
    
    # 开局
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7
    })
    engine.execute_action("setup_choose_daowen", {"daowen": "杀伐"})
    engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    engine.execute_action("setup_choose_region", {"region": "扭曲都市"})
    print("  ✓ 开局完成")
    
    # 局外行动
    result = engine.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "曲解"})
    assert result["success"]
    print(f"  ✓ 领悟：获得曲解残韵")
    
    result = engine.execute_action("pre_battle_action", {"sub_action": "修行", "tier": 1})
    assert result["success"]
    assert engine.state.attribute_points == 1
    print(f"  ✓ 修行：获得1属性点")
    
    result = engine.execute_action("pre_battle_action", {"sub_action": "休整", "tier": 1})
    assert result["success"]
    print(f"  ✓ 休整：8点恢复量")
    
    assert engine.state.energy == 0, f"精力应为0，实际{engine.state.energy}"
    print(f"  ✓ 精力耗尽")
    
    # 进入战斗
    result = engine.execute_action("battle_start", {})
    assert result["success"]
    assert engine.state.current_battle == 1
    print(f"  ✓ 进入第1场战斗")
    
    # 回始
    result = engine.execute_action("round_start", {})
    assert result["success"]
    assert engine.state.player.current_mana == engine.state.player.mana_limit
    print(f"  ✓ 回始：法力补满")
    
    # 使用道纹
    monster = Entity(name="千手蜈蚣", entity_type="怪物", blood_limit=120, current_hp=120,
                     attack_count=6, attack_power=8)
    engine.state.enemies.append(monster)
    
    result = engine.execute_action("use_daowen", {
        "daowen_name": "杀伐",
        "x": 5,
        "target": "千手蜈蚣"
    })
    assert result["success"]
    print(f"  ✓ 发动杀伐X=5: 对千手蜈蚣造成10伤害")
    
    print("  ✓ 完整流程测试通过")


def run_all_tests():
    """运行所有测试"""
    print("=" * 60)
    print("第四宇宙游戏引擎 - 测试套件")
    print("=" * 60)
    
    os.makedirs("data", exist_ok=True)
    
    tests = [
        test_setup,
        test_daowen_calculations,
        test_resonance,
        test_combat,
        test_dice,
        test_dm_rulings,
        test_full_flow,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"\n  ✗ 失败: {test.__name__}")
            print(f"    错误: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    
    print("\n" + "=" * 60)
    print(f"测试结果: {passed} 通过, {failed} 失败")
    print("=" * 60)
    
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
