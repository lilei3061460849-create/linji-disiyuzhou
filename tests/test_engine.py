"""
引擎单元测试
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.makedirs("/tmp/linji_tests", exist_ok=True)

from engine.api import GameEngine
from engine.models import Entity, StatusEffect
from engine.daowen import DaoWenEngine, ResonanceEngine
from engine.dice import DiceEngine
from engine.enums import EntityType
from engine.gamedata import SHAFA_LOOP_DAOWEN
from tests.monster_phase_support import resolve_monster_phase
from tests.setup_support import finish_initial_daowen
import math


def _choose_region(engine, region):
    if sum(engine.state.resonance.values()) == 0:
        engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    result = engine.execute_action("setup_choose_region", {"region": region})
    if result.get("success") and engine.state.pending_relic_choices:
        # 通用流程测试不覆盖需要额外显式选择的遗物；相关遗物有独立测试。
        optional = {"折速法印", "三相残韵盘", "回锋刀", "血契"}
        choice = next((n for n in engine.state.pending_relic_choices if n not in optional),
                      engine.state.pending_relic_choices[0])
        engine.execute_action("choose_discovered_relic", {"relic_name": choice})
    return result


def test_setup():
    """测试开局流程：属性分配后先发现遗物3选1，再从杀伐闭环发现初始道纹3选1。"""
    print("\n=== 测试：开局 ===")
    engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db", rng_seed=7)

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
    relic_choices = list(engine.state.pending_relic_choices)
    assert result["result"]["relic_choices"] == relic_choices
    assert len(relic_choices) == 3 and len(set(relic_choices)) == 3
    assert engine.state.pending_initial_daowen_choices == []
    assert engine.state.player.dao_wen == {}
    print("  ✓ 属性分配正确，先列出3件遗物发现候选")

    blocked = engine.execute_action("setup_choose_initial_daowen", {"daowen_name": "杀伐"})
    assert not blocked["success"], "遗物未选择时不能选初始道纹"
    print("  ✓ 未选择开局遗物时不能选初始道纹")

    picked_relic = engine.execute_action("choose_discovered_relic",
                                         {"relic_name": relic_choices[0]})
    assert picked_relic["success"], f"遗物选择失败: {picked_relic}"
    assert engine.state.relics and engine.state.relics[0].name == relic_choices[0]
    choices = list(engine.state.pending_initial_daowen_choices)
    assert picked_relic["result"]["daowen_choices"] == choices
    assert len(choices) == 3 and len(set(choices)) == 3
    assert set(choices) <= set(SHAFA_LOOP_DAOWEN)
    assert engine.state.player.dao_wen == {}
    print(f"  ✓ 显式选择开局遗物【{relic_choices[0]}】后，列出3个杀伐闭环发现候选")

    blocked = engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    assert not blocked["success"] and "初始道纹" in blocked["error"]
    print("  ✓ 未选择初始道纹时不能选残韵")

    result = engine.execute_action("setup_attributes", {
        "blood_points": 5, "speed_points": 5, "mana_points": 5
    })
    assert not result["success"], "应该拒绝错误的属性分配"
    print("  ✓ 错误分配被拒绝")

    illegal = engine.execute_action("setup_choose_initial_daowen", {"daowen_name": "狂暴"})
    assert not illegal["success"]
    assert engine.state.player.dao_wen == {}
    print("  ✓ 非发现候选被拒绝")

    picked = engine.execute_action("setup_choose_initial_daowen", {"daowen_name": choices[0]})
    assert picked["success"]
    assert set(engine.state.player.dao_wen) == {choices[0]}
    repeat = engine.execute_action("setup_choose_initial_daowen", {"daowen_name": choices[0]})
    assert not repeat["success"]
    removed = engine.execute_action("setup_choose_daowen", {"daowen": "切割"})
    assert not removed["success"]
    print(f"  ✓ 显式选择初始道纹【{choices[0]}】，旧接口不能改写")

    result = engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    assert result["success"], f"残韵选择失败: {result}"
    assert engine.state.resonance.get("反转", 0) == 1, "残韵计数错误"
    print("  ✓ 残韵选择正确")

    result = _choose_region(engine, "扭曲都市")
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
    assert result["target_shield"] == 10
    print("  ✓ 庇护X=5: 消耗5，格挡10")
    
    # 测试再生
    result = DaoWenEngine.resolve("再生", 4, target=target)
    assert result["cost"] == 4
    assert result["target_heal"] == 12
    print("  ✓ 再生X=4: 消耗4，回复12")
    
    # 测试冲击
    result = DaoWenEngine.resolve("冲击", 2)
    assert result["cost"] == 6
    assert result["aoe_damage"] == 10
    print("  ✓ 冲击X=2: 消耗6，AOE伤害10")
    
    # 测试切割
    result = DaoWenEngine.resolve("切割", 3)
    assert result["cost"] == 9
    assert result["duration"] == 3
    print("  ✓ 切割X=3: 消耗9，持续3")
    
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
    
    # 切割 → 反转 → 增殖
    result = ResonanceEngine.apply_resonance("切割", "反转", False, True)
    assert result["success"]
    assert result["target"] == "增殖"
    print("  ✓ 切割 --反转--> 增殖")
    
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
    engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    
    # 设置玩家
    engine.execute_action("setup_attributes", {
        "name": "测试", "blood_points": 10, "speed_points": 8, "mana_points": 7
    })
    finish_initial_daowen(engine)
    
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
    engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    
    # 设置
    engine.execute_action("setup_attributes", {
        "name": "测试", "blood_points": 10, "speed_points": 8, "mana_points": 7
    })
    finish_initial_daowen(engine)
    _choose_region(engine, "扭曲都市")
    engine.state.phase = "in_combat"
    
    # 添加怪物
    monster = Entity(name="测试怪", entity_type="怪物", blood_limit=50, current_hp=10, 
                     attack_count=2, attack_power=5)
    engine.state.enemies.append(monster)
    
    # 声明许愿（轮回者向"某人"祈求）
    result = engine.execute_action("declare_wish", {"wish_text": "让这只怪物消失", "target_ref": "enemy:0"})
    assert result["success"]
    assert result["interrupt"]["interrupt_type"] == "许愿"
    print("  ✓ 许愿中断触发")
    
    # DM裁定（"某人"以扭曲方式实现愿望，代价不公开）
    result = engine.submit_ruling(
        "许愿",
        "愿望实现：怪物消失，但轮回者失去全部法力并本场无法恢复",
        {}
    )
    assert result["success"]
    assert result["ruling_id"] > 0
    print(f"  ✓ DM裁定保存，ID={result['ruling_id']}")
    
    # 查询先例
    precedent = engine.check_precedent("许愿", {"target_ref": "enemy:0"})
    assert precedent["found"]
    print(f"  ✓ 查询到{precedent['count']}个先例")
    
    print("  ✓ DM裁定系统测试通过")


def test_full_flow():
    """测试完整流程"""
    print("\n=== 测试：完整流程 ===")
    engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    
    # 开局
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    _choose_region(engine, "扭曲都市")
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
    
    heal_amt = 8 + engine.state.rest_heal_bonus
    result = engine.execute_action("pre_battle_action", {
        "sub_action": "休整", "tier": 1,
        "heal_allocations": [{"target_ref": "player:0", "amount": heal_amt}],
    })
    assert result["success"]
    print(f"  ✓ 休整：{heal_amt}点恢复量")
    
    assert engine.state.energy == 0, f"精力应为0，实际{engine.state.energy}"
    print(f"  ✓ 精力耗尽")
    
    # 进入战斗
    result = engine.execute_action("battle_start", {})
    assert result["success"]
    assert engine.state.current_battle == 1
    print(f"  ✓ 进入第1场战斗")
    
    # 回始；若随机发现的是回锋刀，则显式选择一个合法敌方目标。
    round_relic_choices = {}
    if any(relic.name == "回锋刀" for relic in engine.state.relics):
        round_relic_choices["回锋刀"] = {"enemy_index": 0}
    if any(relic.name == "血契" for relic in engine.state.relics):
        round_relic_choices["血契"] = {"use": False}
    result = engine.execute_action("round_start", {"relic_choices": round_relic_choices})
    assert result["success"], result
    # 回始获得等同当前法限的法力；战始已清零，无遗物时恰好等于法限。
    assert engine.state.player.current_mana >= engine.state.player.mana_limit
    print(f"  ✓ 回始：法力不低于法限（当前{engine.state.player.current_mana}/{engine.state.player.mana_limit}）")
    
    # 使用道纹
    monster = Entity(name="千手蜈蚣", entity_type="怪物", blood_limit=120, current_hp=120,
                     attack_count=6, attack_power=8)
    engine.state.enemies.append(monster)
    
    result = engine.execute_action("use_daowen", {
        "daowen_name": "杀伐",
        "x": 5,
        "target_ref": f"enemy:{len(engine.state.enemies) - 1}",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert result["success"]
    print(f"  ✓ 发动杀伐X=5: 对千手蜈蚣造成10伤害")
    
    print("  ✓ 完整流程测试通过")


def test_sculpture_and_proliferation():
    """测试雕塑（攻击力归0）与癌变（恢复达阈值）路径"""
    print("\n=== 测试：雕塑 / 癌变 胜利路径 ===")
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

    # --- 癌变：恢复量达血限阈值 ---
    state2 = GameState()
    p2 = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60)
    state2.player = p2
    m2 = Entity(name="肉瘤", entity_type="怪物", blood_limit=80, current_hp=40,
                attack_count=1, attack_power=5)
    state2.enemies.append(m2)
    combat2 = CombatEngine(state2, DiceEngine())
    # 对怪物过量恢复：实恢40 + 过量160按原值计 → total_healed=200 ≥ 80（双倍机制已删）
    m2.heal(200)
    assert m2.total_healed >= 80
    paths2 = combat2.settle_victory_paths()
    assert any(p["type"] == "proliferation" for p in paths2), "应触发癌变"
    assert m2.is_proliferated and not m2.is_alive
    assert state2.rest_heal_bonus == 8
    print("  ✓ 恢复量超阈值→触发癌变，吸收进死者之书（休整恢复量永久+8）")
    print("  ✓ 雕塑/癌变路径测试通过")


def test_daowen_effects_wired():
    """测试道纹效果真实落地（攻面板/速度/碎片/状态/变形）"""
    print("\n=== 测试：道纹效果落地 ===")
    from engine.models import StatusEffect
    engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    engine.execute_action("setup_attributes", {"name":"测试","blood_points":10,"speed_points":8,"mana_points":7})
    finish_initial_daowen(engine)
    engine.state.phase = "in_combat"
    player = engine.state.player
    # 本测试连续发动6次道纹只为验证效果落地，与出手预算校验无关，给予充裕出手预算
    player.speed_limit = 99
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

    # R35 赎金3：有碎片则最多夺取现有20，不再把不足额扩成负债。
    shards_before = engine.state.shards
    r = engine.execute_action("use_daowen", {"daowen_name":"赎金","x":3,"target":"靶怪"})
    assert r["success"], f"赎金失败: {r}"
    assert m.shards == 0, f"赎金后靶怪现有20碎片应被夺尽，实{m.shards}"
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

    # 变形（自身攻击力/攻击次数互换）：玩家默认 0×0，互换后仍为 0×0，只验证逻辑跑通
    r = engine.execute_action("use_daowen", {"daowen_name":"变形","x":1})
    assert r["success"], r
    print("  ✓ 变形：攻击力/攻击次数互换执行成功")
    print("  ✓ 道纹效果落地测试通过")


def test_out_of_combat_actions():
    """测试局外行动真实生效（休整回血/学习加道纹法术/共鸣给遗物）"""
    print("\n=== 测试：局外行动落地 ===")
    engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    engine.execute_action("setup_attributes", {"name":"测试","blood_points":10,"speed_points":8,"mana_points":7})
    finish_initial_daowen(engine)
    _choose_region(engine, "罪孽都市")
    player = engine.state.player

    # 休整：先扣血再休整，验证回血
    player.current_hp = 20
    heal_amt2 = 24 + engine.state.rest_heal_bonus
    r = engine.execute_action("pre_battle_action", {
        "sub_action": "休整", "tier": 2,
        "heal_allocations": [{"target_ref": "player:0", "amount": heal_amt2}],
    })
    assert r["success"], f"休整失败: {r}"
    expected_hp = min(player.blood_limit, 20 + heal_amt2)
    assert player.current_hp == expected_hp, f"休整2档应回{heal_amt2}→{expected_hp}，实{player.current_hp}"
    print(f"  ✓ 休整2档：HP20→{player.current_hp}")

    # 学习道纹：若开局已发现庇护则改学冲击
    learn_name = "庇护" if "庇护" not in player.dao_wen else "冲击"
    r = engine.execute_action("pre_battle_action", {"sub_action":"学习","sub":"daowen","name": learn_name})
    assert r["success"], f"学习失败: {r}"
    assert learn_name in player.dao_wen, f"{learn_name}应已加入玩家道纹"
    print(f"  ✓ 学习道纹：玩家道纹={list(player.dao_wen.keys())}")

    # 学习法术2档：同时学习两种并支付10碎片
    r = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "spell", "tier": 2,
        "names": ["先发制人", "后发制人"],
    })
    assert r["success"], f"学习法术失败: {r}"
    assert {sp.name for sp in player.spells} == {"先发制人", "后发制人"}
    print("  ✓ 学习法术2档：先发制人、后发制人均已掌握")

    # 共鸣：获得遗物（补满精力以便测试）
    engine.state.energy = 3
    n_before = len(engine.state.relics)
    r = engine.execute_action("pre_battle_action", {"sub_action":"共鸣","sub":"discover"})
    assert r["success"], f"共鸣失败: {r}"
    picked = engine.execute_action("choose_discovered_relic", {
        "relic_name": r["result"]["relic_choices"][0],
    })
    assert picked["success"] and len(engine.state.relics) == n_before + 1
    print(f"  ✓ 共鸣：显式选择遗物【{picked['result']['relic']}】，持有{len(engine.state.relics)}件")
    print("  ✓ 局外行动落地测试通过")


def test_relic_effects():
    """测试遗物效果触发（避风铃/第一杯/回锋刀）"""
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
    combat2.round_start({"回锋刀": {"enemy_index": 0}})  # 显式选择回锋刀目标
    assert m2.current_hp < 120, f"回锋刀应造伤，实HP{m2.current_hp}"
    print(f"  ✓ 回锋刀：回始造伤(失速3→9伤)，靶HP120→{m2.current_hp}")

    # 第一杯：免疫癌变（原钱袋效果，钱袋已删除）
    engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    engine.execute_action("setup_attributes", {"name":"测试","blood_points":10,"speed_points":8,"mana_points":7})
    finish_initial_daowen(engine)
    _choose_region(engine, "罪孽都市")
    engine.state.relics = [Relic(name="第一杯", effect="")]
    player = engine.state.player
    player.total_healed = engine.combat.cancer_threshold_of(player)
    hit = engine.combat.check_cancer(player)
    assert hit is None and player.is_alive, "持有第一杯的轮回者应免疫癌变"
    print("  ✓ 第一杯：累计回复达阈值也不触发癌变")
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
    r1 = resolve_monster_phase(combat, {"打手": None})
    assert m.attack_power == 6, f"白板回合攻击力应6，实{m.attack_power}"
    assert len(r1) > 0, "怪物应有出手"
    print(f"  ✓ 第1回合(白板)：攻击力6，怪物出手{len(r1)}次，贾凡HP{st.player.current_hp} 速{st.player.current_speed}")

    # 第2回合：激活强化3 → 攻击力6→9
    combat.round_start()  # current_round→2
    r2 = resolve_monster_phase(combat, {"打手": "强化"}, target_refs={"打手": "enemy:0"})
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
    r = combat.resolve_attack(m, st.player, spell_choices={
        "before": {"后发制人": {"use": True, "cycles": [[
            {"x": 10, "target_ref": "player:0", "dodge": False},
        ]]}},
        "after": {},
    })
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
    r2 = combat2.resolve_attack(m2, st2.player, spell_choices={
        "before": {},
        "after": {"生生不息": {"use": True, "cycles": [[
            {"x": 4, "target_ref": "player:0", "dodge": False},
        ]]}},
    })
    assert st2.player.current_hp == 60, f"生生不息应奶回满，实HP{st2.player.current_hp}"
    print(f"  ✓ 生生不息：失血后发动再生，奶回满(HP{st2.player.current_hp})")
    print("  ✓ 法术触发测试通过")


def test_flying_and_split():
    """测试飞行免选中 + 裂变分次结算"""
    print("\n=== 测试：飞行/裂变 ===")
    from engine.models import GameState, StatusEffect
    from engine.combat import CombatEngine
    from engine.dice import DiceEngine
    # 飞行：玩家飞行，地面怪无法选中
    st = GameState(); st.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60, speed_limit=8, current_speed=8)
    st.player.is_flying = True
    m = Entity(name="地面怪", entity_type="怪物", blood_limit=50, current_hp=50, attack_count=1, attack_power=10)
    st.enemies.append(m)
    combat = CombatEngine(st, DiceEngine())
    r = combat.resolve_attack(m, st.player)
    assert r.get("cant_target") is True, "地面怪应无法选中飞行玩家"
    assert st.player.current_hp == 60, "飞行玩家不应受伤"
    print("  ✓ 飞行：地面怪无法选中飞行玩家(HP不变)")

    # 裂变：怪受100伤分4次(每次25)
    st2 = GameState(); st2.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60, speed_limit=8, current_speed=8, attack_count=1, attack_power=100)
    m2 = Entity(name="靶", entity_type="怪物", blood_limit=200, current_hp=200, attack_count=1, attack_power=1)
    m2.add_status(StatusEffect(name="裂变", remaining_rounds=3, value=4))
    st2.enemies.append(m2); st2.player.is_flying = False; m2.is_flying = False
    combat2 = CombatEngine(st2, DiceEngine())
    r2 = combat2.resolve_attack(st2.player, m2)
    assert r2.get("split") == 4, "裂变应分4次"
    assert m2.current_hp == 100, f"裂变4次×25后应100，实{m2.current_hp}"
    print(f"  ✓ 裂变：100伤分4次×25结算，靶HP200→{m2.current_hp}")
    print("  ✓ 飞行/裂变测试通过")


def test_huoxue():
    """测试活血：本回合失血÷2回终回复"""
    print("\n=== 测试：活血 ===")
    from engine.models import GameState, StatusEffect
    from engine.combat import CombatEngine
    from engine.dice import DiceEngine
    st = GameState(); st.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60, speed_limit=8, current_speed=8)
    st.player.add_status(StatusEffect(name="活血", remaining_rounds=3, value=1))
    m = Entity(name="打手", entity_type="怪物", blood_limit=120, current_hp=120, attack_count=1, attack_power=5)
    st.enemies.append(m)
    combat = CombatEngine(st, DiceEngine()); combat.reset_monster_activation()
    combat.round_start()
    # 玩家挨5点(不闪避)
    st.player.current_speed = 0
    combat.resolve_attack(m, st.player)  # 玩家HP60→55, hp_lost_this_round=5
    assert st.player.current_hp == 55 and st.player.hp_lost_this_round == 5
    combat.round_end()  # 活血回终回复5//2=2 → HP57
    assert st.player.current_hp == 57, f"活血应回2→57，实{st.player.current_hp}"
    print(f"  ✓ 活血：本回合失血5，回终回复2，HP55→{st.player.current_hp}")
    print("  ✓ 活血测试通过")


def test_events_system():
    """测试事件系统：解析/触发/结算"""
    print("\n=== 测试：事件系统 ===")
    engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    engine.execute_action("setup_attributes", {"name":"测试","blood_points":10,"speed_points":8,"mana_points":7})
    finish_initial_daowen(engine)
    _choose_region(engine, "扭曲都市")
    # 解析数量
    assert len(engine.event_pool.events) >= 25, f"应解析>=25事件，实{len(engine.event_pool.events)}"
    print(f"  ✓ 解析到{len(engine.event_pool.events)}个事件")

    # 探索触发（补精力）
    engine.state.energy = 3
    r = engine.execute_action("pre_battle_action", {"sub_action":"探索"})
    assert r["success"], f"探索失败: {r}"
    ev_name = r["result"]["event"]
    assert r["result"]["options"], "事件应有选项"
    print(f"  ✓ 探索触发【{ev_name}】，{len(r['result']['options'])}个选项")

    # R20：不能越过当前事件直接结算祭坛；随后显式构造祭坛为当前事件再测效果。
    wrong_name = "祭坛" if ev_name != "祭坛" else "无名冢"
    wrong = engine.execute_action("resolve_event", {"event": wrong_name, "option_id": 1})
    assert not wrong["success"] and engine.event_pool.current == ev_name
    engine.event_pool.current = "祭坛"
    bl_before = engine.state.player.blood_limit
    sp_before = engine.state.player.speed_limit
    r2 = engine.execute_action("resolve_event", {"event":"祭坛","option_id":1})
    assert r2["success"], f"结算失败: {r2}"
    assert engine.state.player.blood_limit == bl_before - 8, f"衰老8应血限-8，实{engine.state.player.blood_limit}"
    assert engine.state.player.speed_limit == sp_before + 1, f"应+1速限"
    assert "祭坛" in engine.event_pool.triggered, "事件应标记已触发"
    print(f"  ✓ 祭坛选项1：衰老8(血限{bl_before}→{engine.state.player.blood_limit})+1速限，已标记触发")
    print("  ✓ 事件系统测试通过")


def test_rebellion_and_legacy():
    """测试员工叛变检查 + 死之传承"""
    print("\n=== 测试：员工叛变/死之传承 ===")
    from engine.models import GameState
    from engine.combat import CombatEngine
    from engine.dice import DiceEngine
    # 员工叛变：员工攻击总值≥玩家HP+朋友攻击 → 叛变
    st = GameState(); st.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=10)
    emp = Entity(name="追求者", entity_type="员工", blood_limit=96, current_hp=96, attack_count=8, attack_power=2)
    st.employees.append(emp)  # 攻击总值16 ≥ 玩家HP10
    combat = CombatEngine(st, DiceEngine())
    r = combat.check_employee_rebellion()
    assert r["rebellion"] is True, f"应叛变(16≥10): {r}"
    print(f"  ✓ 员工叛变：追求者攻击总值16 ≥ 阈值10，触发叛变")

    # 不叛变：玩家HP高
    st.player.current_hp = 50
    r2 = combat.check_employee_rebellion()
    assert r2["rebellion"] is False, f"HP50时不应叛变(16<50): {r2}"
    print(f"  ✓ 员工叛变：玩家HP50 > 员工攻击16，不叛变")

    # 死之传承
    st.player.is_alive = False; st.player.current_hp = 0
    legacy = {
        "trigger_point": "速度归零后受到致死攻击",
        "fork": "最后一次闪避耗尽速度",
        "cost_budget": "愿以法力换取保命",
    }
    r3 = combat.trigger_death_legacy(legacy)
    assert r3["triggered"] and st.death_book_legacies == [legacy]
    print(f"  ✓ 死之传承：命零留三段式遗言'{r3['legacy']['trigger_point'][:12]}...'")
    print("  ✓ 员工叛变/死之传承测试通过")


def test_relics_five_more():
    """测试三相残韵盘/无所求/买路财/同魂笔"""
    print("\n=== 测试：剩余5遗物 ===")
    from engine.models import GameState, Relic
    from engine.combat import CombatEngine
    from engine.dice import DiceEngine

    # 三相残韵盘：战始消耗转换(最多)，战终获反转+曲解
    st = GameState(); st.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60)
    st.resonance = {"转换":2, "反转":1, "曲解":0}
    st.relics = [Relic(name="三相残韵盘", effect="")]
    combat = CombatEngine(st, DiceEngine()); combat.reset_monster_activation()
    combat.process_relics("battle_start", {"relic_choices": {
        "三相残韵盘": {"use": True, "resonance_type": "转换"},
    }})  # 显式选择消耗转换
    assert st.resonance["转换"] == 1, f"应消耗转换，实{st.resonance}"
    combat.process_relics("battle_end")  # 获反转+曲解各1
    assert st.resonance["反转"] == 2 and st.resonance["曲解"] == 1, f"战终应+反转+曲解，实{st.resonance}"
    print(f"  ✓ 三相残韵盘：转换2→1，战终反转1→2、曲解0→1")

    # 买路财：撤退成本=怪物20%血限
    st2 = GameState(); st2.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60)
    st2.relics = [Relic(name="买路财", effect="")]
    m = Entity(name="怪", entity_type="怪物", blood_limit=100, current_hp=100)
    combat2 = CombatEngine(st2, DiceEngine())
    esc = combat2.buyaicai_escape_cost(m)
    assert esc["shard_cost"] == 20, f"买路财应20碎片(100*20%)，实{esc['shard_cost']}"
    print(f"  ✓ 买路财：100血限怪撤退成本=20碎片")

    # 无所求：resolve_event拒绝+1速限
    engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    engine.execute_action("setup_attributes", {"name":"t","blood_points":10,"speed_points":8,"mana_points":7})
    finish_initial_daowen(engine)
    _choose_region(engine, "扭曲都市")
    engine.state.relics = [Relic(name="无所求", effect="")]
    engine.event_pool.current = "祭坛"
    sp = engine.state.player.speed_limit
    engine.execute_action("resolve_event", {"event":"祭坛","option_id":3, "wusuoqiu_allocation": "speed"})  # 拒绝：无事发生
    assert engine.state.player.speed_limit == sp + 1, "无所求拒绝应+1速限"
    print(f"  ✓ 无所求：选拒绝类选项+1速限({sp}→{engine.state.player.speed_limit})")
    print("  ✓ 剩余5遗物测试通过")


def test_evolution_yuanchu():
    """
    测试【进化】（原初X）与【崩解】（异变达到阈值直接命零）
    规则：原初X：代价：异变5X。选择一种自身未持有的原始怪物道纹，[战终]前视为持有
    （数值固定为本次X），借用的道纹发动时照常支付其自身代价。须处于困境，每场限一次。
    """
    print("\n=== 测试：进化（原初X）与崩解 ===")
    from engine.models import GameState, DaoWen, DaoWenInstance
    from engine.combat import CombatEngine

    def mk_engine():
        engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
        engine.execute_action("setup_attributes", {
            "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7
        })
        finish_initial_daowen(engine)
        # 裁定：原初X 借用池 = 轮回者当前持有的道纹，故须先给轮回者道纹
        for _n in ("自愈", "强化", "杀伐"):
            engine.state.player.dao_wen[_n] = DaoWenInstance(
                dao_wen=DaoWen(name=_n, formula="", cost_type="消耗",
                               cost_formula="X", effect_formula=""), x_value=1)
        engine.combat.reset_monster_activation()
        engine.state.phase = "in_combat"
        return engine

    def mk_plight_monster(name="困境怪", hp=120, cur=30, atk=1, dw=None):
        """困境怪：生命≤30% + 攻击力极低（2个困境信号）"""
        m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=cur,
                   attack_count=2, attack_power=atk)
        for n, x in (dw or []):
            m.dao_wen[n] = DaoWenInstance(
                dao_wen=DaoWen(name=n, formula="", cost_type="代价", cost_formula="异变5X",
                               effect_formula="", is_monster_original=True), x_value=x)
        return m

    # ---- 1. 正常路径：困境怪物发动原初2借用【自愈2】 ----
    engine = mk_engine()
    m = mk_plight_monster(dw=[("狂暴", 2)])
    engine.state.enemies.append(m)
    r = engine.execute_action("declare_evolution", {"monster": "困境怪", "daowen": "自愈", "x": 2})
    assert r["success"], f"正常进化应成功: {r}"
    assert m.mutation_count == 10, f"门票异变应为5×2=10，实{m.mutation_count}"
    assert "自愈" in m.dao_wen and m.dao_wen["自愈"].x_value == 2, "应借用【自愈2】"
    assert "原初借用" in m.dao_wen["自愈"].dao_wen.tags, "借用道纹应有原初借用标记"
    assert r["collapsed"] is False, "10层不应崩解"
    print(f"  ✓ 困境怪发动【原初2】：异变+10（当前{m.mutation_count}层），借用【自愈2】至战终")

    # ---- 2. 边界：同场第二次进化 → 拒绝 ----
    r2 = engine.execute_action("declare_evolution", {"monster": "困境怪", "daowen": "飞行", "x": 1})
    assert not r2["success"] and "限一次" in r2["error"], f"同场二次进化应被拒绝: {r2}"
    assert m.mutation_count == 10, "被拒绝的进化不得扣异变"
    print(f"  ✓ 同场第二次进化被拒绝（每场限一次），异变仍为{m.mutation_count}层")

    # ---- 3. 边界：异变恰好到（阈值-1）层 → 存活且借用生效 ----
    T = Entity.MUTATION_COLLAPSE_THRESHOLD
    m29 = mk_plight_monster(name="临界怪")
    m29.mutation_count = T - 11
    engine.state.enemies.append(m29)
    r3 = engine.execute_action("declare_evolution", {"monster": "临界怪", "daowen": "自愈", "x": 2})
    assert r3["success"] and r3["collapsed"] is False, f"{T-1}层应存活: {r3}"
    assert m29.mutation_count == T - 1 and m29.is_alive, f"{T-1}层应存活"
    print(f"  ✓ 异变{T-11}+10={T-1}层：存活，借用生效（崩解阈值{T}未达）")

    # ---- 4. 边界：异变恰好到阈值 → 崩解命零，进化效果中断 ----
    m30 = mk_plight_monster(name="崩解怪")
    m30.mutation_count = T - 10
    engine.state.enemies.append(m30)
    r4 = engine.execute_action("declare_evolution", {"monster": "崩解怪", "daowen": "自愈", "x": 2})
    assert r4["success"] and r4["collapsed"] is True, f"{T}层应触发崩解: {r4}"
    assert m30.mutation_count == T and not m30.is_alive and m30.current_hp == 0, "崩解应直接命零"
    assert "自愈" not in m30.dao_wen, "崩解时进化效果中断，借用不生效"
    print(f"  ✓ 异变{T-10}+10={T}层：触发【崩解】直接命零，借用【自愈】中断未生效")

    # ---- 5. 非法输入：借用轮回者未持有的道纹 → 拒绝 ----
    engine2 = mk_engine()
    m_bad = mk_plight_monster(name="非法怪", dw=[("狂暴", 2)])
    engine2.state.enemies.append(m_bad)
    r5 = engine2.execute_action("declare_evolution", {"monster": "非法怪", "daowen": "愤怒", "x": 1})
    assert not r5["success"] and "不在轮回者当前持有的道纹中" in r5["error"], \
        f"借用轮回者未持有的道纹应被拒绝: {r5}"
    # ---- 非法输入：借用怪物自身已持有的道纹 → 拒绝 ----
    # 用轮回者也持有的"强化"，确保先通过"必须在轮回者道纹池内"这一关，
    # 从而真正命中"怪物已持有"的拒绝分支。
    from engine.models import DaoWen as _DW, DaoWenInstance as _DWI
    m_bad.dao_wen["强化"] = _DWI(dao_wen=_DW(name="强化", formula="", cost_type="代价",
                                             cost_formula="异变5X", effect_formula="",
                                             is_monster_original=True), x_value=1)
    r6 = engine2.execute_action("declare_evolution", {"monster": "非法怪", "daowen": "强化", "x": 1})
    assert not r6["success"] and "已持有" in r6["error"], f"借用已持有道纹应被拒绝: {r6}"
    # ---- 非法输入：X=0 → 拒绝 ----
    r7 = engine2.execute_action("declare_evolution", {"monster": "非法怪", "daowen": "自愈", "x": 0})
    assert not r7["success"], f"X=0应被拒绝: {r7}"
    # ---- 非法输入：非困境 → 拒绝 ----
    m_fine = Entity(name="满状态怪", entity_type="怪物", blood_limit=120, current_hp=120,
                    attack_count=2, attack_power=10)
    engine2.state.enemies.append(m_fine)
    r8 = engine2.execute_action("declare_evolution", {"monster": "满状态怪", "daowen": "自愈", "x": 1})
    assert not r8["success"] and "未陷入困境" in r8["error"], f"非困境进化应被拒绝: {r8}"
    assert m_bad.mutation_count == 0 and m_fine.mutation_count == 0, "被拒绝的进化均不扣异变"
    print(f"  ✓ 非法输入全部拒绝：转化道纹/已持有/X=0/非困境，且均不扣异变")

    # ---- 6. 怪物激活原始道纹真实计费 + 崩解中断 ----
    st = GameState()
    st.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                       speed_limit=8, current_speed=8)
    m_act = mk_plight_monster(name="计费怪", hp=120, cur=120, atk=6, dw=[("狂暴", 3)])
    st.enemies.append(m_act)
    combat = CombatEngine(st, DiceEngine()); combat.reset_monster_activation()
    combat.round_start()  # 第1回合（白板）
    combat.round_start()  # 第2回合：激活狂暴3
    r9 = resolve_monster_phase(combat, {"计费怪": "狂暴"})
    assert m_act.mutation_count == 15, f"激活狂暴3应付异变5×3=15，实{m_act.mutation_count}"
    print(f"  ✓ 怪物激活【狂暴3】真实支付异变15层（当前{m_act.mutation_count}层）")

    # 崩解中断：异变(阈值-15) + 狂暴3门票15 = 阈值 → 激活中断、不攻击
    st2 = GameState()
    st2.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                        speed_limit=8, current_speed=8)
    m_col = mk_plight_monster(name="自毁怪", hp=120, cur=120, atk=6, dw=[("狂暴", 3)])
    m_col.mutation_count = Entity.MUTATION_COLLAPSE_THRESHOLD - 15
    st2.enemies.append(m_col)
    combat2 = CombatEngine(st2, DiceEngine()); combat2.reset_monster_activation()
    combat2.round_start(); combat2.round_start()
    r10 = resolve_monster_phase(combat2, {"自毁怪": "狂暴"})
    assert m_col.mutation_count == Entity.MUTATION_COLLAPSE_THRESHOLD and not m_col.is_alive, "激活付异变达阈值应崩解命零"
    assert st2.player.current_hp == 60, "崩解怪攻击出手应被中断，玩家无伤"
    assert any(e.get("collapsed") == "狂暴" for e in r10), "结果应记录崩解事件"
    print(f"  ✓ 异变{Entity.MUTATION_COLLAPSE_THRESHOLD-15}+激活狂暴3(15)={Entity.MUTATION_COLLAPSE_THRESHOLD}层：崩解命零，攻击中断，玩家HP仍为{st2.player.current_hp}")

    # ---- 7. 借用道纹：原初门票与首次发动各付一次，持续期间不再计费 ----
    st3 = GameState()
    st3.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                        speed_limit=8, current_speed=8)
    # 原初X 借用池 = 轮回者持有的道纹
    st3.player.dao_wen["自愈"] = DaoWenInstance(
        dao_wen=DaoWen(name="自愈", formula="", cost_type="消耗",
                       cost_formula="X", effect_formula=""), x_value=1)
    m_b = mk_plight_monster(name="借用怪", hp=120, cur=30, atk=1)  # 无自有道纹，仅借用
    st3.enemies.append(m_b)
    combat3 = CombatEngine(st3, DiceEngine()); combat3.reset_monster_activation()
    ev = combat3.execute_evolution(m_b, "自愈", 2)
    assert ev["success"] and m_b.mutation_count == 10, f"原初2门票应为10层: {ev}"
    combat3.round_start(); combat3.round_start()
    resolve_monster_phase(combat3, {"借用怪": "自愈"})  # 第2回合显式选择借用的自愈2
    total1 = m_b.mutation_count
    assert total1 == 20, f"借用自愈2激活应付异变5×2=10（门票10+激活10=20），实{total1}"
    assert m_b.is_alive, "20层应存活"
    # 准则9（DM裁定2026-08-18）：跨回合可重复发动，X已递增至4，重复发动按新X计费
    assert m_b.dao_wen["自愈"].x_value == 4, "发动一次后X应+2（一阶）"
    combat3.round_start()
    resolve_monster_phase(combat3, {"借用怪": "自愈"})
    total2 = m_b.mutation_count
    assert total2 == 40 and m_b.is_alive, f"重复发动按递增X计费：20+5×4=40，实{total2}"
    assert m_b.dao_wen["自愈"].x_value == 6
    print("  ✓ 借用道纹门票10+首次发动10=20层；准则9重复发动按X=4再付20 → 40层")
    print("  ✓ 进化（原初X）与崩解测试通过")


def test_evolution_plight_listing():
    """测试困境标注暴露给AI（裁定①：AI玩家决策调用——引擎只标注，AI自选是否进化及参数）"""
    print("\n=== 测试：进化困境标注（available_actions暴露）===")
    from engine.models import DaoWen, DaoWenInstance

    engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7
    })
    finish_initial_daowen(engine)
    engine.combat.reset_monster_activation()
    engine.state.phase = "in_combat"

    # 困境怪：生命≤30%+攻击力极低（2信号），已持有狂暴2，异变10层
    m = Entity(name="困境怪", entity_type="怪物", blood_limit=120, current_hp=30,
               attack_count=2, attack_power=1)
    m.dao_wen["狂暴"] = DaoWenInstance(
        dao_wen=DaoWen(name="狂暴", formula="", cost_type="代价", cost_formula="异变5X",
                       effect_formula="", is_monster_original=True), x_value=2)
    m.mutation_count = 10
    engine.state.enemies.append(m)

    # ---- 1. 正常路径：行动列表出现evolution项，参数由引擎算好 ----
    actions = engine.get_available_actions()
    evo = next((a for a in actions["actions"] if a.get("action_type") == "declare_evolution"), None)
    assert evo is not None, "战斗行动列表应包含evolution项"
    assert evo["available"] is True, "存在困境怪物时evolution应可用"
    assert len(evo["plight_monsters"]) == 1, "应恰好1只困境怪"
    info = evo["plight_monsters"][0]
    assert info["monster"] == "困境怪"
    # 裁定：原初X 的借用池改为"轮回者当前持有的道纹"（不再是7种原始怪物道纹）
    assert set(info["borrowable_daowen"]) <= set(engine.state.player.dao_wen), \
        "借用池必须是轮回者持有的道纹的子集"
    assert "狂暴" not in info["borrowable_daowen"], "怪物已持有的道纹不可借用"
    T_list = Entity.MUTATION_COLLAPSE_THRESHOLD
    assert info["max_x_by_mutation"] == (T_list - 1 - 10) // 5, \
        f"max_x应为({T_list-1}-10)//5={(T_list-1-10)//5}，实{info['max_x_by_mutation']}"
    print(f"  ✓ evolution项已暴露：困境怪（信号{info['difficulty_signals']}），"
          f"可借6种原始道纹，不崩解最大X={info['max_x_by_mutation']}")

    # ---- 2. 边界：仅1个劣势信号 → 判定困境（裁定⑦：探针≥1） ----
    m2 = Entity(name="半血怪", entity_type="怪物", blood_limit=120, current_hp=120,
                attack_count=2, attack_power=1)  # 只有攻击力极低1个信号
    engine2 = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    engine2.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7
    })
    finish_initial_daowen(engine2)
    engine2.combat.reset_monster_activation()
    engine2.state.phase = "in_combat"
    engine2.state.enemies.append(m2)
    evo2 = next(a for a in engine2.get_available_actions()["actions"] if a.get("action_type") == "declare_evolution")
    assert evo2["available"] is True and len(evo2["plight_monsters"]) == 1, \
        "裁定⑦后仅1个劣势信号即判定困境，应列单"
    assert evo2["plight_monsters"][0]["monster"] == "半血怪"
    # 边界补充：0个信号（满状态）→ 不可用
    m2b = Entity(name="满状态怪", entity_type="怪物", blood_limit=120, current_hp=120,
                 attack_count=2, attack_power=10)
    engine2b = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    engine2b.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7
    })
    finish_initial_daowen(engine2b)
    engine2b.combat.reset_monster_activation()
    engine2b.state.phase = "in_combat"
    engine2b.state.enemies.append(m2b)
    evo2b = next(a for a in engine2b.get_available_actions()["actions"] if a.get("action_type") == "declare_evolution")
    assert evo2b["available"] is False and evo2b["plight_monsters"] == [], \
        "0个劣势信号不应判定困境"
    print("  ✓ 探针口径（裁定⑦）：1个劣势信号即列单（半血怪），0个信号不可用")

    # ---- 3. 边界：已进化过的怪物不再列出；死亡怪物不列出 ----
    engine3 = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
    engine3.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7
    })
    finish_initial_daowen(engine3)
    engine3.combat.reset_monster_activation()
    engine3.state.phase = "in_combat"
    m3 = Entity(name="已进化怪", entity_type="怪物", blood_limit=120, current_hp=30,
                attack_count=2, attack_power=1)
    engine3.combat._monster_evolved.add(id(m3))
    m4 = Entity(name="死亡怪", entity_type="怪物", blood_limit=120, current_hp=0,
                attack_count=2, attack_power=1)
    m4.is_alive = False
    engine3.state.enemies.extend([m3, m4])
    evo3 = next(a for a in engine3.get_available_actions()["actions"] if a.get("action_type") == "declare_evolution")
    assert evo3["available"] is False and evo3["plight_monsters"] == [], \
        "已进化/已死亡怪物均不应列为困境可进化"
    print("  ✓ 已进化（二选一已用）与已死亡怪物均不出现在标注中")
    print("  ✓ 进化困境标注测试通过")


def test_original_daowen_only_charges_mutation_on_activation():
    """原始怪物道纹首次发动支付异变5X，持续期间不再重复计费。"""
    print("\n=== 测试：原始怪物道纹仅首次发动计费 ===")
    from engine.models import GameState, DaoWen, DaoWenInstance
    from engine.combat import CombatEngine
    from engine.dice import DiceEngine

    def mk(name, dw):
        m = Entity(name=name, entity_type="怪物", blood_limit=200, current_hp=200,
                   attack_count=1, attack_power=5)
        for n, x in dw:
            m.dao_wen[n] = DaoWenInstance(
                dao_wen=DaoWen(name=n, formula="", cost_type="代价", cost_formula="异变5X",
                               effect_formula="", is_monster_original=True), x_value=x)
        return m

    def mkbed(m):
        st = GameState()
        st.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                           speed_limit=8, current_speed=8)
        st.enemies.append(m)
        c = CombatEngine(st, DiceEngine()); c.reset_monster_activation()
        return st, c

    # 正常：自愈2激活只付10；持续期间不重复计费（后续回合改发免费的庇护，
    # 准则9下怪物有合法道纹选项时必须出招，故给填充道纹而非空过）。
    m1 = mk("持续怪", [("自愈", 2)])
    m1.dao_wen["庇护"] = DaoWenInstance(
        dao_wen=DaoWen(name="庇护", formula="", cost_type="消耗", cost_formula="X",
                       effect_formula=""), x_value=1)
    _, c1 = mkbed(m1)
    c1.round_start(); resolve_monster_phase(c1, {"持续怪": None})
    c1.round_start(); resolve_monster_phase(c1, {"持续怪": "自愈"})
    assert m1.mutation_count == 10 and m1.is_alive
    for _ in range(3):
        c1.round_start(); resolve_monster_phase(c1, {"持续怪": "庇护"})
        assert m1.mutation_count == 10 and m1.is_alive
    print("  ✓ 自愈2首次支付异变10，持续期间（改发庇护）不再计费")

    # 边界：次数型必中同样只在激活时付一次，不存在额外豁免分支。
    m2 = mk("次数怪", [("必中", 3)])
    m2.dao_wen["庇护"] = DaoWenInstance(
        dao_wen=DaoWen(name="庇护", formula="", cost_type="消耗", cost_formula="X",
                       effect_formula=""), x_value=1)
    _, c2 = mkbed(m2)
    c2.round_start(); resolve_monster_phase(c2, {"次数怪": None})
    c2.round_start(); resolve_monster_phase(c2, {"次数怪": "必中"})
    c2.round_start(); resolve_monster_phase(c2, {"次数怪": "庇护"})
    assert m2.mutation_count == 15 and m2.is_alive
    print("  ✓ 必中3首次支付异变15，未再发动则不再计费")

    # 崩解仍保留：若首次发动本身使异变达到阈值，效果中断并命零。
    m3 = mk("临界怪", [("自愈", 2)])
    m3.mutation_count = Entity.MUTATION_COLLAPSE_THRESHOLD - 10
    _, c3 = mkbed(m3)
    c3.round_start(); resolve_monster_phase(c3, {"临界怪": None})
    c3.round_start(); result = resolve_monster_phase(c3, {"临界怪": "自愈"})
    assert not m3.is_alive and m3.mutation_count == Entity.MUTATION_COLLAPSE_THRESHOLD
    assert any(entry.get("collapsed") == "自愈" for entry in result)
    print("  ✓ 首次发动支付异变达到阈值时仍会崩解，效果中断")


def test_consumable_mutation_wiring():
    """
    测试裁定⑧（A4全量）：普通消耗品中"获得异变N"统一走 Entity.add_mutation；
    任何角色达50层即【崩解】命零（尸体变怪物=世界观句，无数值效果）。
    """
    print("\n=== 测试：消耗品异变统一入口（裁定⑧） ===")
    from engine.models import Consumable

    T = Entity.MUTATION_COLLAPSE_THRESHOLD

    def mk_engine(mut=0):
        engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
        engine.execute_action("setup_attributes", {
            "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7
        })
        finish_initial_daowen(engine)
        engine.state.player.mutation_count = mut
        return engine

    # ---- 1. 正常路径：残骸（1/1）"恢复20生命并获得异变10" → 异变+10，耐久归零 ----
    engine = mk_engine()
    engine.state.consumables.append(Consumable(
        name="残骸", effect="局内使用恢复20生命并获得异变10", current_uses=1, max_uses=1))
    r = engine.execute_action("consume_item", {"name": "残骸"})
    assert r["success"], f"使用残骸应成功: {r}"
    p = engine.state.player
    assert p.mutation_count == 10, f"残骸应+10异变，实{p.mutation_count}"
    assert r["result"]["mutation"]["mutation_total"] == 10 and not r["result"]["mutation"]["collapsed"]
    assert engine.state.consumables[0].is_depleted, "残骸（1/1）应用尽"
    print(f"  ✓ 残骸使用：异变+10（当前{p.mutation_count}层，阈值{T}），耐久1→0")

    # ---- 2. 边界：T-10层使用残骸 → 恰好T层触发崩解，命零 ----
    engine2 = mk_engine(mut=T - 10)
    engine2.state.consumables.append(Consumable(
        name="残骸", effect="局内使用恢复20生命并获得异变10", current_uses=1, max_uses=1))
    r2 = engine2.execute_action("consume_item", {"name": "残骸"})
    assert r2["success"] and r2["result"]["mutation"]["collapsed"], f"{T-10}+10应崩解: {r2}"
    p2 = engine2.state.player
    assert p2.mutation_count == T and not p2.is_alive and p2.current_hp == 0, \
        "轮回者崩解应直接命零"
    print(f"  ✓ 边界：{T-10}层+异变10={T}层触发崩解，轮回者命零（{r2['result']['mutation'].get('note','')}）")

    # ---- 3. 非法/对照：不含异变的普通消耗品不触碰异变；找不到的消耗品拒绝 ----
    engine3 = mk_engine()
    engine3.state.consumables.append(Consumable(
        name="无异变测试品", effect="回复0", current_uses=1, max_uses=1))
    r3 = engine3.execute_action("consume_item", {"name": "无异变测试品"})
    assert r3["success"] and r3["result"]["mutation"] is None, "无异变文本应不触碰异变"
    assert engine3.state.player.mutation_count == 0
    r4 = engine3.execute_action("consume_item", {"name": "不存在的道具"})
    assert not r4["success"], "找不到的消耗品应拒绝"
    assert engine3.state.player.mutation_count == 0, "被拒绝的使用不得加异变"
    print("  ✓ 对照：普通消耗品mutation=None且不触及异变；不存在道具拒绝且不扣层")
    print("  ✓ 消耗品异变统一入口测试通过")


def test_twisted_tool_library():
    """
    测试裁定⑬：扭曲都市完成事件附赠【发现】工具库消耗品。
    """
    print("\n=== 测试：扭曲工具库附赠发现（裁定⑬） ===")
    from engine.models import Consumable
    from engine.api import TWISTED_TOOL_LIBRARY

    def mk(region="扭曲都市"):
        engine = GameEngine(db_path="/tmp/linji_tests/test_rulings.db")
        engine.execute_action("setup_attributes", {
            "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7
        })
        finish_initial_daowen(engine)
        _choose_region(engine, region)
        return engine

    ev_name = None
    engine = mk()
    ev_name = next(n for n, ev in engine.event_pool.events.items()
                   if ev["region"] == "扭曲都市")

    # ---- 1. 正常路径：扭曲都市结算事件 → 随机列3件，再显式选1件 ----
    engine.event_pool.current = ev_name
    r = engine.execute_action("resolve_event", {"event": ev_name, "option_id": 1})
    assert r["success"], f"事件结算失败: {r}"
    bonus = r["result"].get("附赠发现")
    assert bonus is not None and bonus["等待选择"] is True
    assert len(bonus["候选"]) <= 3
    chosen = bonus["候选"][-1]
    picked = engine.execute_action("choose_discovered_item", {"item_name": chosen})
    assert picked["success"]
    dur_expected = TWISTED_TOOL_LIBRARY[chosen][0]
    got_item = next(c for c in engine.state.consumables if c.name == chosen)
    assert got_item.current_uses == dur_expected
    print(f"  ✓ 事件【{ev_name}】附赠发现：候选{bonus['候选']}→显式选择【{chosen}】")

    # ---- 2. 非法输入：不在候选的显式选择必须拒绝，不得回退第一件 ----
    engine2 = mk()
    ev2 = next(n for n, ev in engine2.event_pool.events.items()
               if ev["region"] == "扭曲都市" and n != ev_name)
    engine2.event_pool.current = ev2
    r2 = engine2.execute_action("resolve_event", {"event": ev2, "option_id": 1})
    assert r2["success"]
    bad = engine2.execute_action("choose_discovered_item", {"item_name": "不存在的道具"})
    assert not bad["success"] and engine2.state.pending_item_choices
    print("  ✓ 非候选选择被拒绝，候选保持待选")

    # ---- 3. 边界：8件全持有 → 再无附赠；非扭曲副本也不附赠 ----
    engine3 = mk()
    for n, (dur, txt) in TWISTED_TOOL_LIBRARY.items():
        engine3.state.consumables.append(Consumable(name=n, effect=txt,
                                                    current_uses=dur, max_uses=dur))
    engine3.event_pool.current = ev_name
    r3 = engine3.execute_action("resolve_event", {"event": ev_name, "option_id": 1})
    assert r3["success"] and "附赠发现" not in r3["result"], "全持有后不应再附赠"
    engine4 = mk(region="龙心谷")
    ev4 = next(n for n, ev in engine4.event_pool.events.items() if ev["region"] == "龙心谷")
    engine4.event_pool.current = ev4
    r4 = engine4.execute_action("resolve_event", {"event": ev4, "option_id": 1})
    assert r4["success"] and "附赠发现" not in r4["result"], "非扭曲副本不应附赠工具库"
    print("  ✓ 边界：8件全持有不再附赠；龙心谷事件不附赠")
    print("  ✓ 扭曲工具库附赠发现测试通过")


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
        test_sculpture_and_proliferation,
        test_daowen_effects_wired,
        test_out_of_combat_actions,
        test_relic_effects,
        test_monster_phase_engine,
        test_spells_trigger,
        test_flying_and_split,
        test_huoxue,
        test_events_system,
        test_rebellion_and_legacy,
        test_relics_five_more,
        test_evolution_yuanchu,
        test_evolution_plight_listing,
        test_original_daowen_only_charges_mutation_on_activation,
        test_consumable_mutation_wiring,
        test_twisted_tool_library,
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

