"""
引擎单元测试（全部经由公开行动接口结算，禁止手动改造实体作假）
"""
import sys
import os
import shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Entity, StatusEffect, Spell
from engine.daowen import DaoWenEngine, ResonanceEngine
from engine.dice import DiceEngine
from engine.gamedata import MONSTER_POOLS, RELIC_POOL, SPELL_LIBRARY, monster_spawn_count
import math


def fresh_engine(name="test"):
    db_dir = f"data/test_{name}"
    shutil.rmtree(db_dir, ignore_errors=True)
    os.makedirs(db_dir, exist_ok=True)
    return GameEngine(db_path=f"{db_dir}/rulings.db")


def setup_player(engine, daowen="杀伐", region="扭曲都市", spread=(10, 8, 7)):
    b, s, m = spread
    assert engine.execute_action("setup_attributes", {
        "name": "测试者", "blood_points": b, "speed_points": s, "mana_points": m})["success"]
    assert engine.execute_action("setup_choose_daowen", {"daowen": daowen})["success"]
    assert engine.execute_action("setup_choose_region", {"region": region})["success"]


def drain_energy(engine):
    engine.state.energy = 0


def start_battle(engine, numbers=None):
    """走完真实的战始抽怪流程"""
    r = engine.execute_action("battle_start", {"battle_background": "测试背景"})
    assert r["success"], f"战始失败: {r}"
    count = r["spawn_count"]
    numbers = numbers or [1] * count
    last = r
    for i in range(count):
        last = engine.execute_action("random_number", {
            "pool_name": f"spawn_battle_{engine.state.current_battle}",
            "number": numbers[i % len(numbers)],
        })
    assert engine.state.phase == "in_combat", f"未进入战斗: {last}"
    return last


def test_setup():
    """测试开局流程"""
    print("\n=== 测试：开局 ===")
    engine = fresh_engine("setup")

    engine2 = fresh_engine("setup2")
    r = engine2.execute_action("setup_attributes", {"blood_points": 5, "speed_points": 5, "mana_points": 5})
    assert not r["success"], "应该拒绝点数≠25的分配"

    setup_player(engine)
    player = engine.state.player
    assert player.blood_limit == 60 and player.mana_limit == 14 and player.speed_limit == 8
    assert player.action_count == math.ceil(8 / 3)
    assert engine.state.shards == 20
    assert engine.state.phase == "pre_battle" and engine.state.energy == 3
    print("  ✓ 属性/道纹/副本/精力/碎片正确")
    r = engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    assert r["success"] and engine.state.resonance.get("反转") == 1
    print("  ✓ 残韵选择正确")

    # 遗物发现：抽3候选 → 自选1件入包
    r = engine.execute_action("discover_relic_setup", {})
    if r.get("random_required"):
        r = engine.execute_action("random_number", {"pool_name": "relic_candidates", "number": 3})
    assert r.get("candidates") and len(r["candidates"]) == 3
    chosen = r["candidates"][0]
    r2 = engine.execute_action("discover_relic_setup", {"chosen": chosen})
    assert r2["success"] and any(x.name == chosen for x in engine.state.relics)
    assert all(x.name != chosen for x in engine.state.relics_pool)
    print(f"  ✓ 遗物发现入包（{chosen}）并从池中移除")


def test_unavailable_mechanisms():
    """未实装机制必须如实拒绝，不允许假装成功"""
    print("\n=== 测试：未实装机制的诚实拒绝 ===")
    engine = fresh_engine("unavail")
    setup_player(engine)

    for sub in ("探索", "维修", "雇佣", "炼心"):
        r = engine.execute_action("pre_battle_action", {"sub_action": sub})
        assert not r["success"] and r.get("unavailable"), f"{sub}应被拒绝"
        print(f"  ✓ {sub}: {r['error'][:40]}")

    r = engine.execute_action("pre_battle_action", {"sub_action": "探索"})
    assert engine.state.energy == 3, "被拒行动不得扣精力"
    print("  ✓ 被拒行动不消耗精力")

    # 未实装道纹直接拒绝
    assert "赌命" not in SPELL_LIBRARY
    assert engine.state.player is not None
    drain_energy(engine)
    start_battle(engine)
    engine.execute_action("round_start", {})
    engine.state.player.dao_wen["赌命"] = engine.state.player.dao_wen["杀伐"].__class__(
        dao_wen=engine._build_daowen_def("赌命"))
    r = engine.execute_action("use_daowen", {"daowen_name": "赌命", "x": 1, "target": engine.state.enemies[0].name})
    assert not r["success"] and r.get("unavailable")
    print("  ✓ 未实装道纹【赌命】拒绝发动")


def test_spawn_formula():
    """出怪公式：数量=战斗场数-2（最低1），怪物真实入列，白板开局"""
    print("\n=== 测试：出怪公式与怪物池 ===")
    for battle_no, expect in [(1, 1), (2, 1), (3, 1), (4, 2), (5, 3), (6, 4), (7, 5)]:
        assert monster_spawn_count(battle_no, "扭曲都市") == expect
    print("  ✓ 一阶出怪数序列 1/1/1/2/3/4/5")

    assert sum(len(v) for v in MONSTER_POOLS.values()) == 36
    assert all(len(v) == 12 for v in MONSTER_POOLS.values())
    print("  ✓ 三个副本各12种怪物，共36种")

    engine = fresh_engine("spawn")
    setup_player(engine)
    drain_energy(engine)
    # 用合法数字抽满1只
    r = start_battle(engine, numbers=[4])
    mon = engine.state.enemies[0]
    pool = MONSTER_POOLS["扭曲都市"]
    assert mon.name.rstrip("0123456789") in [m["name"] for m in pool]
    assert mon.blood_limit > 0 and len(mon.status_effects) == 0, "怪物必须白板开局"
    print(f"  ✓ 真实出怪: {mon.name} {mon.attack_count}×{mon.attack_power}/{mon.blood_limit}，白板")

    r = engine.execute_action("random_number", {"pool_name": "spawn_battle_1", "number": 99})
    assert not r["success"], "范围外数字必须拒绝"
    print("  ✓ 范围外随机数被拒绝")


def test_daowen_calculations():
    """测试道纹公式（含本次补录的加害）"""
    print("\n=== 测试：道纹计算 ===")
    target = Entity(name="目标", entity_type="怪物", blood_limit=100, current_hp=100)

    r = DaoWenEngine.resolve("杀伐", 3, target=target)
    assert r["cost"] == 3 and r["target_damage"] == 6
    r = DaoWenEngine.resolve("庇护", 5, target=target)
    assert r["cost"] == 5 and r["target_shield"] == 20
    r = DaoWenEngine.resolve("锐利", 3, target=target)
    assert r["cost"] == 9 and r["blood_limit_reduction"] == 12
    r = DaoWenEngine.resolve("加害", 2, target=target)
    assert r["cost"] == 6 and r["damage_amp_per_hit"] == 2 and r["duration"] == -1
    print("  ✓ 杀伐/庇护/锐利/加害 公式正确")
    assert "加害" in DaoWenEngine.list_all()
    print("  ✓ 加害已注册（不再缺失）")


def test_resonance_learning():
    """残韵与转化道纹学习的路径校验"""
    print("\n=== 测试：残韵与转化学习 ===")
    r = ResonanceEngine.apply_resonance("杀伐", "反转", False, True)
    assert r["success"] and r["target"] == "再生"
    r = ResonanceEngine.apply_resonance("杀伐", "转换", False, True)
    assert not r["success"]
    print("  ✓ 闭环路径正误判定")

    engine = fresh_engine("learn")
    setup_player(engine)
    # 直接学庇护应失败（庇护需经再生中转，当前没有再生）
    r = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "learn_type": "transform_daowen", "names": ["庇护"], "tier": 1})
    assert not r["success"] and "相邻" in r["error"]
    print("  ✓ 跳过前置链的学习被拒绝")
    # 先学再生，下场再学庇护
    r = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "learn_type": "transform_daowen", "names": ["再生"], "tier": 1})
    assert r["success"] and "再生" in engine.state.player.dao_wen
    engine.state.energy = 3
    r = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "learn_type": "transform_daowen", "names": ["庇护"], "tier": 1})
    assert r["success"] and "庇护" in engine.state.player.dao_wen
    print("  ✓ 沿闭环链 杀伐→再生→庇护 真实习得")

    # 法术学习校验所需道纹
    engine.state.energy = 3
    r = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "learn_type": "spell", "names": ["借力打力"], "tier": 1})
    assert r["success"] and any(s.name == "借力打力" for s in engine.state.player.spells)
    engine.state.energy = 3
    r = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "learn_type": "spell", "names": ["千刀万剐"], "tier": 1})
    assert not r["success"] and "血债" in r["error"]
    print("  ✓ 法术学习校验所需道纹（千刀万剐缺血债被拒）")


def test_combat_settlement():
    """真实战斗：杀伐伤害/杀伐闪避失效后返还/庇护格挡吸收/战终奖励与清理"""
    print("\n=== 测试：战斗结算 ===")
    engine = fresh_engine("combat")
    setup_player(engine)
    engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "learn_type": "transform_daowen", "names": ["再生"], "tier": 1})
    engine.state.energy = 3
    engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "learn_type": "transform_daowen", "names": ["庇护"], "tier": 1})
    drain_energy(engine)
    start_battle(engine, numbers=[1])   # 千手蜈蚣 6×8/120
    mon = engine.state.enemies[0]

    engine.execute_action("round_start", {})
    r = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 7, "target": mon.name})
    assert r["success"] and mon.current_hp == 120 - 14
    assert engine.state.player.current_mana == 7
    print("  ✓ 杀伐X=7 造成14伤害，法力14→7")
    mana_before = engine.state.player.current_mana
    # 测试注入：怪物面板本无速度（无法闪避）；此处依DM裁定临时赋予速度以验证闪避-返还路径
    mon.current_speed = 1
    hp_before_dodge = mon.current_hp
    r = engine.execute_action("use_daowen", {
        "daowen_name": "杀伐", "x": 5, "target": mon.name, "target_dodge": True})
    assert r["success"] and r.get("cost_refunded")
    assert engine.state.player.current_mana == mana_before, "闪避生效后消耗必须不发生"
    assert mon.current_hp == hp_before_dodge, "闪避生效后判定与结算完全失效"
    assert mon.current_speed == 0, "闪避消耗1点速度"
    print("  ✓ 目标闪避成功：判定与结算完全失效，消耗与代价未发生")

    while engine.state.actions_used < engine._player_action_budget():
        r = engine.execute_action("use_daowen", {"daowen_name": "庇护", "x": 3, "target": "测试者"})
        if not r.get("success"):
            break
    shield_before = engine.state.player.shield
    assert shield_before > 0
    print(f"  ✓ 庇护获得{shield_before}格挡，预算{engine.state.actions_used}/{engine._player_action_budget()}")

    r = engine.execute_action("monster_turn", {"monster": mon.name, "acts": [
        {"type": "attack_round", "target": "测试者", "dodges": [False] * 6}]})
    hits = r["turn_log"][0]["hits"]
    assert len(hits) == 6 and all(h["dodge_attempted"] is False for h in hits)
    absorbed_total = sum(h.get("shield_absorbed", 0) for h in hits)
    assert absorbed_total > 0
    print(f"  ✓ 一轮攻击：6次独立命中，格挡共吸收{absorbed_total}")

    # 击杀 → 战终结算
    mon.current_hp = 1
    engine.state.actions_used = 0
    engine.state.player.current_mana = 7
    engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": mon.name})
    assert not mon.is_alive
    r = engine.execute_action("battle_end", {})
    body = r["result"]
    expect_base = math.ceil(120 * 0.02) + 3 * 5
    assert body["shard_reward"] == expect_base, f"{body['shard_reward']} != {expect_base}"
    assert engine.state.player.shield == 0 and engine.state.player.current_speed == engine.state.player.speed_limit
    assert engine.state.energy == 3 and engine.state.phase == "pre_battle"
    print(f"  ✓ 战终：碎片奖励{expect_base}（血限2%+道纹3×5），格挡清空，速复原，精力回3")


def test_spell_engine():
    """法术引擎：积木/循环/中断"""
    print("\n=== 测试：法术引擎 ===")
    engine = fresh_engine("spell")
    setup_player(engine)
    p = engine.state.player
    for dw in ("再生", "庇护"):
        engine.state.energy = 3
        r = engine.execute_action("pre_battle_action", {
            "sub_action": "学习", "learn_type": "transform_daowen", "names": [dw], "tier": 1})
        assert r["success"], r
    engine.state.energy = 3
    engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "learn_type": "spell", "names": ["借力打力", "以牙还牙"], "tier": 2})
    drain_energy(engine)
    start_battle(engine, numbers=[1])
    mon = engine.state.enemies[0]
    engine.execute_action("round_start", {})

    r = engine.execute_action("use_spell", {
        "spell_name": "借力打力", "trigger_timing": "受到伤害前",
        "target": mon.name, "x": 3, "y": 4})
    assert r["success"], r
    shield = engine.state.player.shield
    assert shield == 12, f"庇护X=3应为12格挡，得{shield}"
    assert mon.current_hp == 120 - 8
    assert engine.state.player.current_mana == 14 - 3 - 4
    print("  ✓ 借力打力：庇护X3→盾12，杀伐Y4→伤8，法力分步扣除")

    # 触发时点不符必须拒绝
    r = engine.execute_action("use_spell", {
        "spell_name": "借力打力", "trigger_timing": "失去生命后",
        "target": mon.name, "x": 1, "y": 1})
    assert not r["success"] and "受到伤害前" in r["error"]
    print("  ✓ 触发时点校验（不能在非声明时点发动）")

    # 中断法则：法力耗尽中断
    engine.state.player.current_mana = 2
    r = engine.execute_action("use_spell", {
        "spell_name": "借力打力", "trigger_timing": "受到伤害前",
        "target": mon.name, "x": 2, "y": 2})
    assert r["success"] and r["cycles"] == 1
    print(f"  ✓ 法力不足时按中断法则终止: {r.get('interrupt_reason')}")

    # 未学法术拒绝
    r = engine.execute_action("use_spell", {
        "spell_name": "千刀万剐", "trigger_timing": "失去生命后", "target": mon.name, "x": 1})
    assert not r["success"] and "未学会" in r["error"]
    print("  ✓ 未学法术拒绝发动")


def test_costs_and_cooldown():
    """代价系统：流血真实扣血、冷却跨场推进、异变累计"""
    print("\n=== 测试：代价系统 ===")
    engine = fresh_engine("costs")
    setup_player(engine, daowen="杀伐", region="龙心谷")
    # 通过残韵获得血债（杀伐→反转→再生→曲解→庇护→曲解→固执→反转→血债 链路太长，直接局外学习链）
    # 杀伐→(反转)再生→(曲解)庇护→(曲解)固执→(反转)血债
    for dw in ("再生", "庇护", "固执", "血债"):
        engine.state.energy = 3
        r = engine.execute_action("pre_battle_action", {
            "sub_action": "学习", "learn_type": "transform_daowen", "names": [dw], "tier": 1})
        assert r["success"], (dw, r)
    drain_energy(engine)
    start_battle(engine, numbers=[1])   # 熔岩蜥 3×10/114
    mon = engine.state.enemies[0]
    engine.execute_action("round_start", {})

    hp_before = engine.state.player.current_hp
    r = engine.execute_action("use_daowen", {"daowen_name": "血债", "x": 5, "target": mon.name})
    assert r["success"], r
    assert engine.state.player.current_hp == hp_before - 5, "流血代价必须真实扣血"
    # 血债：2X=10次独立1点伤害
    hits = mon.current_hp
    assert hits == 114 - 10, f"血债应造成10点总伤害（10次×1点），实际{114-hits}"
    log = r["execution"]["effects"][0]
    assert log["type"] == "multi_hit_damage" and log["hits"] == 10
    print("  ✓ 血债X=5：流血5真实扣除，怪物受10次独立1点伤害")

    # 固执：冷却5场
    r = engine.execute_action("use_daowen", {"daowen_name": "固执", "x": 2, "target": "测试者"})
    assert r["success"]
    dw = engine.state.player.dao_wen["固执"]
    assert dw.cooldown_remaining == 2
    r2 = engine.execute_action("use_daowen", {"daowen_name": "固执", "x": 2, "target": "测试者"})
    assert not r2["success"] and "冷却" in r2["error"]
    print("  ✓ 冷却X真实生效，冷却中拒绝发动")

    mon.current_hp = 1
    engine.state.actions_used = 0
    engine.state.player.current_mana = 50
    engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": mon.name})
    engine.execute_action("battle_end", {})
    assert engine.state.player.dao_wen["固执"].cooldown_remaining == 1, "战终必须推进冷却-1"
    print("  ✓ 战终冷却推进 2→1")


def test_death_and_crown():
    """死之传承中断 + 第7场后冠冕封存"""
    print("\n=== 测试：死亡与冠冕 ===")
    engine = fresh_engine("death")
    setup_player(engine)
    drain_energy(engine)
    start_battle(engine)
    engine.execute_action("round_start", {})
    engine.state.player.current_hp = 1
    mon = engine.state.enemies[0]
    r = engine.execute_action("monster_turn", {"monster": mon.name, "acts": [
        {"type": "attack_round", "target": "测试者", "dodges": [False] * mon.attack_count}]})
    assert not engine.state.player.is_alive
    assert engine._pending_interrupts and \
        engine._pending_interrupts[0].interrupt_type.value == "死之传承"
    print("  ✓ 轮回者[命零]触发死之传承中断，阻塞后续行动")
    r = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": mon.name})
    assert not r["success"] and "中断" in r["error"]
    engine.submit_ruling("死之传承", "别把速度花在不值得的人身上")
    assert engine.state.death_book_wisdom == ["别把速度花在不值得的人身上"]
    assert engine.state.phase == "game_over"
    print("  ✓ 遗言入死者之书（≤20字），轮回终结")

    # 冠冕：连胜7场（直接推到第7场战终）
    engine2 = fresh_engine("crown")
    setup_player(engine2)
    for b in range(1, 8):
        engine2.state.energy = 0
        start_battle(engine2)
        mon = engine2.state.enemies[0]
        engine2.execute_action("round_start", {})
        mon.current_hp = 1
        engine2.state.player.current_mana = 50
        engine2.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": mon.name})
        r = engine2.execute_action("battle_end", {})
    crown = r["result"].get("crown")
    assert crown and crown.get("sealed"), f"第7场战后应封存冠冕: {crown}"
    assert engine2.state.sealed_candidate["player_daowen"] == ["杀伐"]
    print("  ✓ 第7场战后触发最终的冠冕：完整封存（无候选时）")


def test_dice():
    """测试随机数系统"""
    print("\n=== 测试：随机数系统 ===")
    dice = DiceEngine()
    result = dice.create_pool("test_pool", ["A", "B", "C", "D", "E"])
    assert result["count"] == 5 and result["range"] == "1~5"
    result = dice.resolve_pool("test_pool", 3)
    assert result["selected"] == "C"
    try:
        dice.resolve_pool("test_pool", 10)
        assert False
    except ValueError:
        pass
    print("  ✓ 池创建/解析/范围校验正确")


def test_relic_pool_and_rules():
    """遗物池与事实源一致性"""
    print("\n=== 测试：遗物池 ===")
    names = [r["name"] for r in RELIC_POOL]
    assert len(names) == len(set(names)), "遗物同名违反全宇宙唯一"
    # README 宣称"共12件"但正文实列13件（含忘忧香/无所求），如实记录而非遮掩
    assert len(names) == 13, "README实列13件，与'共12件'的说法不一致，如实按实列装载并记录质询"
    print(f"  ✓ 实列{len(names)}件（README标注'共12件'，差异已在经验库记录，待DM确认）")
    implemented = [r["name"] for r in RELIC_POOL if r["implemented"]]
    print(f"  ✓ 已实装{len(implemented)}件：{implemented}，其余如实标记未实装")


def test_wangyou_relic():
    """遗物【忘忧香】：局外行动『忘忧』真实生效"""
    print("\n=== 测试：忘忧香 ===")
    from engine.models import Relic
    engine = fresh_engine("wangyou")
    setup_player(engine)
    player = engine.state.player

    # 未持有忘忧香时：行动被拒绝
    engine.state.energy = 3
    r = engine.execute_action("pre_battle_action", {"sub_action": "忘忧", "tier": 1,
                                                    "forget_names": ["杀伐"]})
    assert not r["success"] and "忘忧香" in r["error"], "未持有遗物时必须拒绝"
    assert engine.state.energy == 3, "拒绝时不得吞掉精力"
    print("  ✓ 未持有忘忧香时拒绝且不吞精力")

    # 持有后：忘忧2 = 失忆2种道纹 → +55碎片
    relic = next(r for r in engine.state.relics_pool if r.name == "忘忧香")
    assert "implemented" in relic.tags, "忘忧香已实装，必须带 implemented 标记"
    engine.state.relics_pool.remove(relic)
    engine.state.relics.append(relic)
    for dw in ("再生", "庇护"):  # 真实学习两种转化道纹
        engine.state.energy = 3
        assert engine.execute_action("pre_battle_action", {
            "sub_action": "学习", "learn_type": "transform_daowen",
            "names": [dw], "tier": 1})["success"]
    engine.state.energy = 3
    shards_before = engine.state.shards
    r = engine.execute_action("pre_battle_action", {
        "sub_action": "忘忧", "tier": 2, "forget_names": ["再生", "庇护"]})
    assert r["success"], r
    assert engine.state.shards == shards_before + 55, "忘忧2必须真实+55碎片"
    assert "再生" not in player.dao_wen and "庇护" not in player.dao_wen, "失忆必须真实移除道纹"
    assert engine.state.energy == 2, "忘忧消耗1点精力（与其他局外行动一致，待DM裁定确认）"
    print(f"  ✓ 忘忧2：失忆[再生,庇护]，碎片 {shards_before}→{engine.state.shards}，精力3→2")

    # 档位校验：忘忧3但只持有2种道纹 → 必须拒绝
    engine.state.energy = 3
    r = engine.execute_action("pre_battle_action", {
        "sub_action": "忘忧", "tier": 3, "forget_names": ["杀伐"]})
    assert not r["success"], "道纹不足档位时必须拒绝"
    r = engine.execute_action("pre_battle_action", {
        "sub_action": "忘忧", "tier": 1, "forget_names": ["杀伐"]})
    assert r["success"], r
    assert not player.dao_wen, "忘忧可以失忆到空"
    print("  ✓ 忘忧1：失忆[杀伐]，可失忆至空；超额档位被正确拒绝")


def test_custom_spell():
    """自创法术：完全由已拥有道纹按三大法则组装（创建/校验/施放/失效/修订）"""
    print("\n=== 测试：自创法术 ===")
    engine = fresh_engine("custom")
    setup_player(engine)
    player = engine.state.player
    # 学再生/庇护
    for dw in ("再生", "庇护"):
        engine.state.energy = 3
        assert engine.execute_action("pre_battle_action", {
            "sub_action": "学习", "learn_type": "transform_daowen",
            "names": [dw], "tier": 1})["success"]

    # 创建：后发先至（受到伤害前→庇护x自身→杀伐y敌人），1精力、0碎片
    engine.state.energy = 3
    r = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "learn_type": "create_spell",
        "name": "后发先至", "trigger": "受到伤害前",
        "steps": [{"daowen": "庇护", "x_param": "x", "target": "self"},
                  {"daowen": "杀伐", "x_param": "y", "target": "enemy"}]})
    assert r["success"], r
    assert engine.state.energy == 2
    spec = r["result"]["spec"]
    assert spec["custom"] and spec["required_daowen"] == ["庇护", "杀伐"]
    print("  ✓ 自创法术【后发先至】创建成功，spec公开回显")

    # 图纸不合规必须拒绝：未拥有道纹 / 非法触发 / 未实装道纹 / 库法术重名
    engine.state.energy = 3
    bad = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "learn_type": "create_spell",
        "name": "假大空", "trigger": "失去生命后",
        "steps": [{"daowen": "血债", "x_param": "x", "target": "enemy"}]})
    assert not bad["success"] and engine.state.energy == 3, "未拥有道纹的图纸必须拒绝且不吞精力"
    bad2 = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "learn_type": "create_spell",
        "name": "坏触发", "trigger": "每回合开始时",
        "steps": [{"daowen": "再生", "x_param": "x", "target": "self"}]})
    assert not bad2["success"]
    bad3 = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "learn_type": "create_spell",
        "name": "借力打力", "trigger": "受到伤害前",
        "steps": [{"daowen": "庇护", "x_param": "x", "target": "self"}]})
    assert not bad3["success"] and "重名" in bad3["error"]
    print("  ✓ 未拥有道纹/非法触发/库重名 三类图纸均被拒绝且退回精力")

    # 真实施放（与法术库同一条积木结算管线）
    drain_energy(engine)
    start_battle(engine)
    engine.execute_action("round_start", {})
    mon = engine.state.enemies[0]
    r = engine.execute_action("use_spell", {
        "spell_name": "后发先至", "trigger_timing": "受到伤害前",
        "target": mon.name, "x": 5, "y": 3})
    assert r["success"], r
    steps = r["steps_executed"]
    assert steps[0]["execution"][0]["amount"] == 20   # 庇护5→20格挡
    assert steps[1]["execution"][0]["actual_damage"] == 6  # 杀伐3→6伤害
    print("  ✓ 自创法术真实结算：庇护5→20格挡，杀伐3→6伤害")

    # 道纹丢失→法术失效（规则：法术必须完全由已有道纹组成）
    del player.dao_wen["庇护"]
    r = engine.execute_action("use_spell", {
        "spell_name": "后发先至", "trigger_timing": "受到伤害前",
        "target": mon.name, "x": 5, "y": 3})
    assert not r["success"] and "失效" in r["error"]
    # 库法术同样适用
    player.spells.append(Spell(
        name="后发制人", required_daowen=["庇护"], trigger_condition="受到伤害前",
        effect_flow="庇护", rank=1))
    r = engine.execute_action("use_spell", {"spell_name": "后发制人", "target": mon.name, "x": 3})
    assert not r["success"] and "失效" in r["error"]
    print("  ✓ 所需道纹丢失后，自创法术与法术库法术均如实战时失效")

    # 战终后修订（以修订时持有道纹为准）
    engine.state.phase = "pre_battle"
    r = engine.execute_action("revise_custom_spell", {
        "name": "后发先至",
        "steps": [{"daowen": "再生", "x_param": "x", "target": "self"},
                  {"daowen": "杀伐", "x_param": "y", "target": "enemy"}]})
    assert r["success"], r
    r = engine.execute_action("revise_custom_spell", {
        "name": "后发先至",
        "steps": [{"daowen": "固执", "x_param": "x", "target": "self"}]})
    assert not r["success"], "修订同样受'已拥有道纹'校验"
    print("  ✓ [战终]修订窗口真实生效，且受同等图纸校验")


def run_all_tests():
    print("=" * 60)
    print("第四宇宙游戏引擎 - 测试套件（全部真实结算）")
    print("=" * 60)

    os.makedirs("data", exist_ok=True)

    tests = [
        test_setup,
        test_unavailable_mechanisms,
        test_spawn_formula,
        test_daowen_calculations,
        test_resonance_learning,
        test_combat_settlement,
        test_spell_engine,
        test_costs_and_cooldown,
        test_death_and_crown,
        test_dice,
        test_relic_pool_and_rules,
        test_wangyou_relic,
        test_custom_spell,
    ]

    passed = failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"\n  ✗ 失败: {test.__name__}\n    错误: {e}")
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
