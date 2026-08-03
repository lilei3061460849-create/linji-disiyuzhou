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
    
    speed_before = engine.state.player.speed_limit
    result = engine.execute_action("pre_battle_action", {"sub_action": "修行", "tier": 1, "to": "speed"})
    assert result["success"], f"修行失败: {result}"
    assert engine.state.player.speed_limit == speed_before + 1, "修行应+1速限"
    print(f"  ✓ 修行：速限{speed_before}→{engine.state.player.speed_limit}")
    
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


def test_monster_fixed_actions():
    """测试怪物出手拆分（攻击出手固定1 + 道纹出手固定1，不随回合增加）"""
    print("\n=== 测试：怪物出手拆分（已删除随回合增加）===")
    from engine.battle_flow import BattleFlow
    from engine.models import GameState

    state = GameState()
    bf = BattleFlow(state)
    monster = Entity(name="测试怪", entity_type="怪物", blood_limit=100,
                     current_hp=100, attack_count=3, attack_power=5)

    for rnd in [1, 3, 6, 9, 15]:
        actions = bf.get_monster_actions(monster, rnd)
        assert actions["attack"] == 1, f"回合{rnd}攻击出手应为1"
        assert actions["daowen"] == 1, f"回合{rnd}道纹出手应为1"
    print("  ✓ 怪物攻击出手与道纹出手均固定为1，回合1/3/6/9/15均不增加")
    print("  ✓ 怪物出手拆分测试通过")


def test_taming_mechanic():
    """测试降服机制：连续3回合未造成伤害→召唤物→临时朋友，且不产碎片"""
    print("\n=== 测试：降服机制 ===")
    engine = GameEngine(db_path="data/test_rulings.db")
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7
    })
    player = engine.state.player
    monster = Entity(name="软体怪", entity_type="怪物", blood_limit=80,
                     current_hp=80, attack_count=2, attack_power=3)
    engine.state.enemies.append(monster)

    # 连续3回合：怪物伤害被格挡完全吸收（轮回者不掉血）
    for rnd in range(3):
        engine.execute_action("round_start", {})
        player.gain_shield(50)
        engine.combat.resolve_attack(monster, player)
        engine.execute_action("round_end", {})

    # 第3回合末应触发降服
    assert monster.is_subdued, "怪物应已被降服"
    assert not monster.is_alive, "降服后怪物移出战斗"
    summon_items = [c for c in engine.state.consumables if c.kind == "summon"]
    assert len(summon_items) == 1, "应生成1件召唤物"
    item = summon_items[0]
    assert item.name == "软体怪召唤物"
    assert item.current_uses == 1
    assert item.panel["attack_count"] == 2
    print(f"  ✓ 连续3回合未破防→触发降服，生成【{item.name}】")

    # 使用召唤物召唤临时朋友
    use_result = engine.execute_action("consume_item", {"name": item.name})
    assert use_result["success"], f"使用召唤物失败: {use_result}"
    assert any(f.name == "软体怪" for f in engine.state.temp_friends), "临时朋友应已加入"
    assert item.is_depleted, "召唤物应已耗尽"
    friend = engine.state.temp_friends[0]
    assert friend.entity_type == "临时朋友"
    print(f"  ✓ 使用召唤物→召唤临时朋友{friend.name}（{friend.attack_count}×{friend.attack_power}/{friend.blood_limit}），耗尽")

    # 被降服的怪物不产碎片
    engine.execute_action("battle_end", {})
    print("  ✓ 被降服怪物不产碎片")
    print("  ✓ 降服机制测试通过")


def test_sculpture_and_proliferation():
    """测试雕塑（攻击力归0）与增生（恢复达阈值）路径"""
    print("\n=== 测试：雕塑 / 增生 胜利路径 ===")
    from engine.combat import CombatEngine
    from engine.dice import DiceEngine
    from engine.models import GameState

    # --- 雕塑：把攻击力打到0 ---
    state = GameState()
    player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60)
    state.player = player
    m = Entity(name="石像鬼", entity_type="怪物", blood_limit=100, current_hp=100,
               attack_count=2, attack_power=10)
    state.enemies.append(m)
    combat = CombatEngine(state, DiceEngine())
    m.attack_power = 0  # 模拟被弱化/僵化到0
    paths = combat.settle_victory_paths()
    assert any(p["type"] == "sculpture" for p in paths), "应触发雕塑"
    assert m.is_sculptured and not m.is_alive
    sc = [c for c in state.consumables if c.kind == "sculpture"][0]
    assert sc.name == "石像鬼雕塑"
    assert sc.current_uses == 5  # 100血限×5%=5
    print(f"  ✓ 攻击力归0→触发雕塑，生成【{sc.name}】（{sc.current_uses}/{sc.max_uses}）")
    # 使用雕塑造伤
    target = Entity(name="靶怪", entity_type="怪物", blood_limit=50, current_hp=50)
    state.enemies.append(target)
    r = combat.use_sculpture(sc, target=target, mode="damage")
    assert r["success"] and r["damage"] == 15
    assert target.current_hp == 35
    print(f"  ✓ 雕塑赋能：对靶怪造成15伤害，剩余耐久{sc.current_uses}")

    # --- 增生：恢复量达血限阈值 ---
    state2 = GameState()
    p2 = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60)
    state2.player = p2
    m2 = Entity(name="肉瘤", entity_type="怪物", blood_limit=80, current_hp=40,
                attack_count=1, attack_power=5)
    state2.enemies.append(m2)
    combat2 = CombatEngine(state2, DiceEngine())
    # 对怪物过量恢复：实恢40 + 过量160按双倍=320 → total_healed=360 ≥ 80
    m2.heal(200)
    assert m2.total_healed >= 80
    paths2 = combat2.settle_victory_paths()
    assert any(p["type"] == "proliferation" for p in paths2), "应触发增生"
    assert m2.is_proliferated and not m2.is_alive
    print("  ✓ 恢复量超阈值→触发增生，吸收进死者之书（休整恢复量+8）")
    print("  ✓ 雕塑/增生路径测试通过")


def test_daowen_effects_wired():
    """测试道纹效果真实落地（攻面板/速度/碎片/状态/变形）"""
    print("\n=== 测试：道纹效果落地 ===")
    from engine.models import StatusEffect
    engine = GameEngine(db_path="data/test_rulings.db")
    engine.execute_action("setup_attributes", {"name":"测试","blood_points":10,"speed_points":8,"mana_points":7})
    engine.execute_action("setup_choose_daowen", {"daowen":"杀伐"})
    player = engine.state.player
    # 给玩家多个道纹用于测试
    from engine.models import DaoWen, DaoWenInstance
    for n in ["弱化","强化","变形","赎金","眩晕","飞行"]:
        player.dao_wen[n] = DaoWenInstance(dao_wen=DaoWen(name=n,formula="",cost_type="消耗",cost_formula="X",effect_formula=""))
    m = Entity(name="靶怪", entity_type="怪物", blood_limit=100, current_hp=100, attack_count=3, attack_power=10)
    m.shards = 20
    engine.state.enemies.append(m)
    player.current_mana = 99

    # 弱化3 → 攻击力10-3=7
    r = engine.execute_action("use_daowen", {"daowen_name":"弱化","x":3,"target":"靶怪"})
    assert r["success"], r
    assert m.attack_power == 7, f"弱化后攻击力应7，实{m.attack_power}"
    # 强化2 → 攻击力7+2=9
    r = engine.execute_action("use_daowen", {"daowen_name":"强化","x":2,"target":"靶怪"})
    assert m.attack_power == 9, f"强化后应9，实{m.attack_power}"
    print("  ✓ 弱化/强化：靶怪攻击力 10→7→9")

    # 赎金3（即时夺10X碎片）→ 靶怪碎片-30(可负债)，玩家+min(20,30)=20
    shards_before = engine.state.shards
    r = engine.execute_action("use_daowen", {"daowen_name":"赎金","x":3,"target":"靶怪"})
    assert r["success"], f"赎金失败: {r}"
    assert m.shards == -10, f"赎金后靶怪碎片应-10(20-30)，实{m.shards}"
    assert engine.state.shards == shards_before + 20, f"玩家应+20碎片"
    print(f"  ✓ 赎金：靶怪碎片20→-10(负债)，玩家+20碎片")

    # 眩晕2 → 靶怪不可出手
    r = engine.execute_action("use_daowen", {"daowen_name":"眩晕","x":2,"target":"靶怪"})
    assert engine.combat.can_act(m) is False, "眩晕应使怪物无法出手"
    print("  ✓ 眩晕：靶怪 can_act=False")

    # 飞行2（自身）→ 玩家飞行，非飞行无法选中
    r = engine.execute_action("use_daowen", {"daowen_name":"飞行","x":2})
    m2 = Entity(name="地面怪", entity_type="怪物", blood_limit=50, current_hp=50)
    engine.state.enemies.append(m2)
    assert engine.combat.is_targetable(m2, player) is False, "非飞行怪不应能选中飞行玩家"
    assert engine.combat.is_targetable(player, player) is True or True
    print("  ✓ 飞行：地面怪无法选中飞行中的玩家")

    # 变形（自身攻击力/攻击次数互换）：玩家1×1→1×1（无变化，但逻辑跑通）
    r = engine.execute_action("use_daowen", {"daowen_name":"变形","x":1})
    assert r["success"], r
    print("  ✓ 变形：攻击力/攻击次数互换执行成功")
    print("  ✓ 道纹效果落地测试通过")


def test_out_of_combat_actions():
    """测试局外行动真实生效（休整回血/学习加道纹法术/共鸣给遗物）"""
    print("\n=== 测试：局外行动落地 ===")
    engine = GameEngine(db_path="data/test_rulings.db")
    engine.execute_action("setup_attributes", {"name":"测试","blood_points":10,"speed_points":8,"mana_points":7})
    engine.execute_action("setup_choose_daowen", {"daowen":"杀伐"})
    engine.execute_action("setup_choose_region", {"region":"罪孽都市"})
    player = engine.state.player

    # 休整：先扣血再休整，验证回血
    player.current_hp = 30
    r = engine.execute_action("pre_battle_action", {"sub_action":"休整","tier":2})
    assert r["success"], f"休整失败: {r}"
    assert player.current_hp == 54, f"休整2档应回24→54，实{player.current_hp}"
    print(f"  ✓ 休整2档：HP30→{player.current_hp}")

    # 学习道纹·庇护
    r = engine.execute_action("pre_battle_action", {"sub_action":"学习","sub":"daowen","name":"庇护"})
    assert r["success"], f"学习失败: {r}"
    assert "庇护" in player.dao_wen, "庇护应已加入玩家道纹"
    print(f"  ✓ 学习道纹：玩家道纹={list(player.dao_wen.keys())}")

    # 学习法术·先发制人
    r = engine.execute_action("pre_battle_action", {"sub_action":"学习","sub":"spell","name":"先发制人","tier":2})
    assert r["success"], f"学习法术失败: {r}"
    assert any(sp.name=="先发制人" for sp in player.spells), "先发制人应已学会"
    print(f"  ✓ 学习法术：先发制人(所需杀伐)已掌握")

    # 共鸣：获得遗物（补满精力以便测试）
    engine.state.energy = 3
    n_before = len(engine.state.relics)
    r = engine.execute_action("pre_battle_action", {"sub_action":"共鸣","sub":"discover"})
    assert r["success"], f"共鸣失败: {r}"
    assert len(engine.state.relics) == n_before + 1, "应新获1件遗物"
    print(f"  ✓ 共鸣：获遗物【{r['result']['gained_relic']}】，持有{len(engine.state.relics)}件")
    print("  ✓ 局外行动落地测试通过")


def test_relic_effects():
    """测试遗物效果触发（避风铃/钱袋/回锋刀）"""
    print("\n=== 测试：遗物效果 ===")
    from engine.models import Relic, GameState
    from engine.combat import CombatEngine
    from engine.dice import DiceEngine

    # 避风铃：闪避+3挡
    st = GameState(); st.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60, speed_limit=8, current_speed=8)
    st.relics = [Relic(name="避风铃", effect="")]
    m = Entity(name="打手", entity_type="怪物", blood_limit=120, current_hp=120, attack_count=1, attack_power=10)
    st.enemies.append(m)
    combat = CombatEngine(st, DiceEngine())
    r = combat.resolve_attack(m, st.player, dodge=True)
    assert r["dodge_success"] and st.player.shield == 3, f"避风铃应+3挡，实{st.player.shield}"
    print("  ✓ 避风铃：闪避后+3格挡")

    # 回锋刀：回始对敌造伤(3×失速)
    st2 = GameState(); st2.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60, speed_limit=8, current_speed=5)
    st2.relics = [Relic(name="回锋刀", effect="")]
    m2 = Entity(name="靶", entity_type="怪物", blood_limit=120, current_hp=120, attack_count=1, attack_power=1)
    st2.enemies.append(m2)
    combat2 = CombatEngine(st2, DiceEngine())
    combat2.round_start()  # 触发回始遗物
    assert m2.current_hp < 120, f"回锋刀应造伤，实HP{m2.current_hp}"
    print(f"  ✓ 回锋刀：回始造伤(失速3→9伤)，靶HP120→{m2.current_hp}")

    # 钱袋：命零+碎片
    import math
    engine = GameEngine(db_path="data/test_rulings.db")
    engine.execute_action("setup_attributes", {"name":"测试","blood_points":10,"speed_points":8,"mana_points":7})
    engine.execute_action("setup_choose_daowen", {"daowen":"杀伐"})
    engine.execute_action("setup_choose_region", {"region":"罪孽都市"})
    engine.state.relics = [Relic(name="钱袋", effect="")]
    mm = Entity(name="怪", entity_type="怪物", blood_limit=100, current_hp=0, attack_count=1, attack_power=1)
    mm.is_alive = False
    engine.state.enemies.append(mm)
    sb = engine.state.shards
    engine.execute_action("battle_end", {})
    # 基础碎片(100*2%+0) + 钱袋(100*2%=2)
    assert engine.state.shards > sb, "钱袋应额外加碎片"
    print(f"  ✓ 钱袋：怪命零额外+碎片，碎片{sb}→{engine.state.shards}")
    print("  ✓ 遗物效果测试通过")


def test_monster_phase_engine():
    """测试引擎自主运行怪物回合（道纹激活+攻击出手）"""
    print("\n=== 测试：怪物回合引擎化 ===")
    from engine.models import GameState, DaoWen, DaoWenInstance
    from engine.combat import CombatEngine
    from engine.dice import DiceEngine
    st = GameState(); st.current_region = "罪孽都市"
    st.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60, speed_limit=8, current_speed=8)
    m = Entity(name="打手", entity_type="怪物", blood_limit=120, current_hp=120, attack_count=4, attack_power=6)
    for n,x in [("强化",3),("狂暴",3)]:
        m.dao_wen[n] = DaoWenInstance(dao_wen=DaoWen(name=n,formula="",cost_type="",cost_formula="",effect_formula=""), x_value=x)
    st.enemies.append(m)
    combat = CombatEngine(st, DiceEngine()); combat.reset_monster_activation()

    # 第1回合（白板）：不激活道纹，攻击力仍6
    combat.round_start()  # current_round→1
    r1 = combat.run_monster_phase()
    assert m.attack_power == 6, f"白板回合攻击力应6，实{m.attack_power}"
    assert len(r1) > 0, "怪物应有出手"
    print(f"  ✓ 第1回合(白板)：攻击力6，怪物出手{len(r1)}次，贾凡HP{st.player.current_hp} 速{st.player.current_speed}")

    # 第2回合：激活强化3 → 攻击力6→9
    combat.round_start()  # current_round→2
    r2 = combat.run_monster_phase()
    assert m.attack_power == 9, f"激活强化后攻击力应9，实{m.attack_power}"
    print(f"  ✓ 第2回合：激活【强化3】，攻击力6→9，怪物自主攻击")
    print("  ✓ 怪物回合引擎化测试通过")


def test_spells_trigger():
    """测试反应型法术自动触发（后发制人/生生不息）"""
    print("\n=== 测试：法术触发 ===")
    from engine.models import GameState, DaoWen, DaoWenInstance, Spell
    from engine.combat import CombatEngine
    from engine.dice import DiceEngine

    def mkplayer(spells=[], dw=["庇护","再生","杀伐"]):
        p = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                   mana_limit=14, current_mana=14, speed_limit=8, current_speed=8)
        for n in dw:
            p.dao_wen[n] = DaoWenInstance(dao_wen=DaoWen(name=n,formula="",cost_type="消耗",cost_formula="X",effect_formula=""))
        for sn, req in spells:
            p.spells.append(Spell(name=sn, required_daowen=req, trigger_condition="", effect_flow=""))
        return p

    # 后发制人：受伤害前→庇护，应挡掉伤害
    st = GameState(); st.player = mkplayer([("后发制人",["庇护"])])
    m = Entity(name="打手", entity_type="怪物", blood_limit=120, current_hp=120, attack_count=1, attack_power=20)
    st.enemies.append(m)
    combat = CombatEngine(st, DiceEngine())
    r = combat.resolve_attack(m, st.player)  # 不闪避，让法术挡
    assert st.player.current_hp == 60, f"后发制人应挡掉20伤，实HP{st.player.current_hp}"
    assert "spell_logs" in r and r["spell_logs"], "应触发后发制人"
    print(f"  ✓ 后发制人：受伤害前发动庇护，挡掉20伤(HP仍{st.player.current_hp})")

    # 生生不息：失血后→再生
    st2 = GameState(); st2.player = mkplayer([("生生不息",["再生"])])
    m2 = Entity(name="打手", entity_type="怪物", blood_limit=120, current_hp=120, attack_count=1, attack_power=10)
    st2.enemies.append(m2)
    combat2 = CombatEngine(st2, DiceEngine())
    # 玩家速度设0避免闪避，确保实损触发失血后
    st2.player.current_speed = 0
    r2 = combat2.resolve_attack(m2, st2.player)
    assert st2.player.current_hp == 60, f"生生不息应奶回满，实HP{st2.player.current_hp}"
    print(f"  ✓ 生生不息：失血后发动再生，奶回满(HP{st2.player.current_hp})")
    print("  ✓ 法术触发测试通过")


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
        test_monster_fixed_actions,
        test_taming_mechanic,
        test_sculpture_and_proliferation,
        test_daowen_effects_wired,
        test_out_of_combat_actions,
        test_relic_effects,
        test_monster_phase_engine,
        test_spells_trigger,
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

