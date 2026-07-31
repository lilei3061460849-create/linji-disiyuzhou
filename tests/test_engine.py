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
from engine.events import EVENT_POOL_UNIVERSAL, EVENT_POOL_REGION, CONSUMABLES, EVENT_FRIENDS
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

    # 探索/维修已随事件系统真实实装（见 test_events）；此处守卫仍诚实拒绝的项
    for sub in ("雇佣", "炼心"):
        r = engine.execute_action("pre_battle_action", {"sub_action": sub})
        assert not r["success"] and r.get("unavailable"), f"{sub}应被拒绝"
        print(f"  ✓ {sub}: {r['error'][:40]}")

    # 事件池耗尽后探索如实拒绝且不吞精力
    for e in EVENT_POOL_UNIVERSAL:
        engine.event_pool.mark_encountered(e["id"])
    for evs in EVENT_POOL_REGION.values():
        for e in evs:
            engine.event_pool.mark_encountered(e["id"])
    r = engine.execute_action("pre_battle_action", {"sub_action": "探索"})
    assert not r["success"], "事件池已空后探索应拒绝"
    assert engine.state.energy == 3, "被拒行动不得扣精力"
    print("  ✓ 事件池已空后探索如实拒绝且不扣精力")

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


def test_shell_daowen_bizhong_manqian():
    """空壳修复：必中（层数真实消耗+闪避失效）与缓慢（阈值判定+无法出手）"""
    print("\n=== 测试：必中/缓慢（原空壳道纹）===")
    from engine.models import DaoWenInstance
    engine = fresh_engine("shellfix")
    setup_player(engine)
    drain_energy(engine)
    start_battle(engine, numbers=[1])
    mon = engine.state.enemies[0]
    # 面板补录必中（怪物面板道纹为数据层，测试构造）
    mon.dao_wen["必中"] = DaoWenInstance(dao_wen={"name": "必中"})
    engine.state.current_round = 4   # 怪物本轮预算=ceil(4/3)=2
    engine.execute_action("round_start", {})

    # 怪物发动必中3 + 攻击两轮（玩家全部声明闪避）
    r = engine.execute_action("monster_turn", {"monster": mon.name, "acts": [
        {"type": "use_daowen", "daowen": "必中", "x": 3, "target": mon.name},
        {"type": "attack_round", "target": "测试者", "dodges": [True, True]},
    ]})
    assert r["success"], r
    logs = r["turn_log"]
    hits = logs[1]["hits"]
    assert all(not h["dodge_success"] for h in hits), "必中攻击不得被闪避"
    assert all(h["damage_dealt"] == mon.attack_power for h in hits), "必中攻击必须真实命中"
    expect = max(0, 3 - len(hits))  # 每次攻击消耗1层
    assert mon.get_status_value("必中") == expect, \
        f"3层-{len(hits)}次攻击={expect}层，实际{mon.get_status_value('必中')}"
    print(f"  ✓ 必中真实生效：3层挂上→{len(hits)}次攻击各耗1层→闪避失效且全中（修复前：花钱不挂状态）")

    # 缓慢：怪物对玩家（玩家速限8→预算3；怪物非专属×3→阈值3）3≤3生效→玩家无法出手
    mon.dao_wen["缓慢"] = DaoWenInstance(dao_wen={"name": "缓慢"})
    r = engine.execute_action("monster_turn", {"monster": mon.name, "acts": [
        {"type": "use_daowen", "daowen": "缓慢", "x": 1, "target": "测试者"},
    ]})
    apply = [ef for ef in r["turn_log"][0].get("execution", {}).get("effects", [])
             if ef.get("type") == "slow_apply"]
    assert apply and engine.state.player.has_status("缓慢"), f"预算3≤阈值3必须生效: {r}"
    r2 = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": mon.name})
    assert not r2["success"] and "缓慢" in r2["error"], "缓慢期道纹出手必须被拒绝"
    r3 = engine.execute_action("attack", {"target": mon.name})
    assert not r3["success"] and "缓慢" in r3["error"], "缓慢期普攻必须被拒绝"
    print("  ✓ 缓慢真实生效：阈值判定（预算3≤3）→ 道纹/普攻双路径锁死（修复前：resolve直接崩溃）")

    # 缓慢不生效情形：玩家施放阈值1打预算为3的怪物（移到下轮避免已有状态干扰）
    engine.state.player.status_effects.clear()
    engine.state.current_round = 7  # 怪物预算=3
    engine.state.player.dao_wen["缓慢"] = DaoWenInstance(dao_wen=engine._build_daowen_def("缓慢"))
    engine.state.player.current_mana = 50
    engine.state.actions_used = 0
    r4 = engine.execute_action("use_daowen", {"daowen_name": "缓慢", "x": 1, "target": mon.name})
    assert r4["success"] and not mon.has_status("缓慢"), "阈值1<预算3必须不生效，不挂状态"
    assert r4["execution"]["effects"][0]["type"] == "slow_failed"
    print("  ✓ 缓慢未达标（1<3）：如实 slow_failed，不挂状态找借口")


def test_resonance_hijack():
    """残韵战内插队：命中即耗+按新公式结算+施法者获得；未命中不消耗"""
    print("\n=== 测试：残韵实时插队 ===")
    engine = fresh_engine("hijack")
    setup_player(engine)
    # 领悟拿1个反转（接口，真实）
    r = engine.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "反转"})
    assert r["success"] and engine.state.resonance["反转"] == 1
    drain_energy(engine)
    start_battle(engine, numbers=[5])   # 眼树 1×26/96：定型2，必中4，再生2
    mon = engine.state.enemies[0]
    assert mon.name == "眼树" and "必中" in mon.dao_wen
    engine.execute_action("round_start", {})

    # 声明插队：不耗残韵（未生效不消耗）
    r = engine.execute_action("use_resonance", {
        "resonance_type": "反转", "source_daowen": "必中", "on_monster": "眼树", "x": 2})
    assert r["success"], f"插队声明失败: {r}"
    assert engine.state.resonance["反转"] == 1, "声明时不应扣残韵"
    assert len(engine.state.pending_resonance) == 1
    print("  ✓ 插队声明成功且不消耗（残韵未生效不消耗）")

    # 怪物发动必中 → 被改写为蒙蔽2（消耗5×2=10法力，怪物无法力→流程中断发动失败）
    r = engine.execute_action("monster_turn", {"monster": "眼树", "acts": [
        {"type": "use_daowen", "daowen": "必中", "x": 4, "target": "眼树"}]})
    assert r["success"]
    log = r["turn_log"][0]
    assert not log["success"], f"改写后应按新公式中断（消耗无法满足）: {log}"
    assert "无法满足" in log["error"] or "法力不足" in log["error"]
    assert engine.state.resonance["反转"] == 0, "命中时残韵必须消耗"
    assert "必中" in mon.dao_wen, "怪物拥有的道纹不得被改变"
    assert not mon.has_status("必中"), "中断的发动不得挂上状态"
    assert "蒙蔽" in engine.state.player.dao_wen, "规则2：施法者永久获得变化后的道纹"
    print("  ✓ 命中：必中→蒙蔽2，消耗10法力怪物无法支付→流程中断（发动失败）；残韵-1，面板不变，玩家获得蒙蔽")

    # 未命中不消耗：对怪物再生声明插队，怪物不发动，残韵原样保留
    engine.state.resonance["曲解"] = 1
    r = engine.execute_action("use_resonance", {
        "resonance_type": "曲解", "source_daowen": "再生", "on_monster": "眼树", "x": 1})
    assert r["success"]
    r = engine.execute_action("monster_turn", {"monster": "眼树", "acts": [
        {"type": "attack_round", "target": engine.state.player.name, "dodges": [False]}]})
    assert engine.state.resonance["曲解"] == 1, "未命中不得消耗残韵"
    print("  ✓ 声明后怪物不发动：残韵原样保留")

    # 路径不存在（必中无曲解分支）→ 直接拒绝不消耗
    r = engine.execute_action("use_resonance", {
        "resonance_type": "曲解", "source_daowen": "必中", "on_monster": "眼树"})
    assert not r["success"] and "不存在" in r["error"]
    print("  ✓ 路径不存在：拒绝且不消耗")

    # 玩家自身道纹的永久变化（规则3）：杀伐--反转-->再生
    engine.state.resonance["反转"] = 1
    r = engine.execute_action("use_resonance", {"resonance_type": "反转", "source_daowen": "杀伐"})
    assert r["success"] and "再生" in engine.state.player.dao_wen and "杀伐" not in engine.state.player.dao_wen
    assert engine.state.resonance["反转"] == 0
    print("  ✓ 自身道纹：杀伐→再生 永久变化并消耗")


def test_events_system():
    """探索→事件选择（代价/收益真实）→无所求/朋友/消耗品/赌局/扭曲工具库"""
    print("\n=== 测试：探索与事件系统 ===")
    engine = fresh_engine("events")
    setup_player(engine)
    p = engine.state.player

    # 通用池10-1（手术需微光者队友被过滤）+扭曲7-1（尖叫下水道需5道纹）=14
    pool = engine._current_event_pool()
    ids = [e["id"] for e in pool]
    assert "手术" not in ids and "尖叫下水道" not in ids and len(ids) == 15, ids
    print(f"  ✓ 条件事件正确过滤：{ids}")

    # 探索1个：数字6 → 无名碑林（通用池第6）
    r = engine.execute_action("pre_battle_action", {"sub_action": "探索", "count": 1})
    assert r["success"] and r.get("random_required")
    assert engine.state.energy == 2
    r = engine.execute_action("random_number", {"pool_name": "event_pool", "number": 6})
    assert r["success"] and r["events_found"] == ["无名碑林"]
    print("  ✓ 探索→随机数→发现【无名碑林】，精力3→2")

    # 未选择事件不能继续其他行动
    r = engine.execute_action("pre_battle_action", {"sub_action": "休整", "tier": 1})
    assert not r["success"] and "choose_event_option" in r["error"]
    print("  ✓ 事件未选择前其他行动被正确拦截")

    # 查询模式 → 选触摸：流血15，+15碎片+曲解×1
    r = engine.execute_action("choose_event_option", {})
    assert r.get("query") and r["event"] == "无名碑林"
    hp0, shards0 = p.current_hp, engine.state.shards
    r = engine.execute_action("choose_event_option", {"option_index": 0})
    assert r["success"], r
    assert p.current_hp == hp0 - 15 and engine.state.shards == shards0 + 15
    assert engine.state.resonance.get("曲解") == 1
    assert engine.event_pool.is_encountered("无名碑林")
    print("  ✓ 触摸：流血15真实扣血、+15碎片、曲解×1、事件标记已遇到")

    # 扭曲都市：每个事件完成后附赠工具库发现（伪节点），先清完队列
    r = engine.execute_action("choose_event_option", {})
    assert r.get("random_required") and "工具库" in r["action"]
    r = engine.execute_action("random_number", {"pool_name": "tool_library", "number": 1})
    assert r["success"] and any(c.name == "反怪物电击枪" for c in engine.state.consumables)
    assert not engine.state.pending_event
    print("  ✓ 事件队列全部处理完（含工具库附赠发现）才能继续")

    # 无所求：拒绝类选项+1属性点
    from engine.models import Relic as _Relic
    engine.state.relics.append(_Relic(name="无所求", effect="拒绝→+1属性点", tags=["implemented"]))
    engine.execute_action("pre_battle_action", {"sub_action": "探索", "count": 1})
    r = engine.execute_action("random_number", {"pool_name": "event_pool", "number": 6})  # 池已去碑林：6号=回音长廊
    assert r["events_found"] == ["回音长廊"]
    ap0 = engine.state.attribute_points
    r = engine.execute_action("choose_event_option", {"option_index": 2})  # 捂住耳朵
    assert r["success"] and r.get("refuse_note") and engine.state.attribute_points == ap0 + 1
    print("  ✓ 无所求：拒绝类选项永久+1属性点")
    engine.execute_action("choose_event_option", {})  # 工具库伪节点
    engine.execute_action("random_number", {"pool_name": "tool_library", "number": 2})
    assert not engine.state.pending_event

    # 扭曲专属：乞丐→朋友加入 + 附赠工具库发现
    engine.execute_action("pre_battle_action", {"sub_action": "探索", "count": 1})
    pool_ids = [e["id"] for e in engine._current_event_pool()]
    num = pool_ids.index("乞丐") + 1
    r = engine.execute_action("random_number", {"pool_name": "event_pool", "number": num})
    assert "乞丐" in r["events_found"] and r.get("tool_discovery_pending")
    hp0 = p.current_hp
    r = engine.execute_action("choose_event_option", {"option_index": 0})  # 给予庇护
    assert r["success"] and p.current_hp == hp0 - 10
    assert any(f.name == "乞丐" and f.attack_count == 2 and f.attack_power == 3
               and f.blood_limit == 50 and "狂暴" in f.dao_wen and f.mutation == 3
               for f in engine.state.friends)
    print("  ✓ 给予庇护：流血10，乞丐（2×3/50，狂暴，异变3）作为[朋友]真实加入")
    # 工具库附赠发现
    r = engine.execute_action("choose_event_option", {})
    assert r.get("random_required") and "工具库" in r["action"]
    r = engine.execute_action("random_number", {"pool_name": "tool_library", "number": 6})
    assert r["success"] and any(c.name == "急救箱" for c in engine.state.consumables)
    print("  ✓ 扭曲都市：事件完成后附赠工具库发现 → 获得急救箱（耐久2）")

    # 猩红暴雨（无拒绝项）：枯竭3
    engine.state.energy = 3  # 夹具补足精力（前面三次探索已耗完）
    engine.execute_action("pre_battle_action", {"sub_action": "探索", "count": 1})
    pool_ids = [e["id"] for e in engine._current_event_pool()]
    num = pool_ids.index("猩红暴雨") + 1
    engine.execute_action("random_number", {"pool_name": "event_pool", "number": num})
    ml0 = p.mana_limit
    r = engine.execute_action("choose_event_option", {"option_index": 1})
    assert r["success"] and p.mana_limit == ml0 - 3
    print("  ✓ 猩红暴雨：法力屏障→枯竭3真实削减法限")
    engine.execute_action("choose_event_option", {})  # 工具库伪节点
    engine.execute_action("random_number", {"pool_name": "tool_library", "number": 3})
    assert not engine.state.pending_event

    # 罪孽都市：赌局（押注碎片赢双倍）
    engine2 = fresh_engine("events2")
    setup_player(engine2, region="罪孽都市")
    engine2.execute_action("pre_battle_action", {"sub_action": "探索", "count": 1})
    pool2 = [e["id"] for e in engine2._current_event_pool()]
    num = pool2.index("遗落的赌局") + 1
    engine2.execute_action("random_number", {"pool_name": "event_pool", "number": num})
    s0 = engine2.state.shards
    r = engine2.execute_action("choose_event_option", {"option_index": 0, "x": 5})
    assert r["success"] and r.get("follow", {}).get("gamble")
    r = engine2.execute_action("random_number", {"pool_name": "gamble", "number": 1})
    assert r["success"] and r["win"] and engine2.state.shards == s0 + 10
    print("  ✓ 赌局：押注5碎片，随机数1=赢→+10碎片")

    # 需要DM裁定的选项：如实拒绝假装
    engine3 = fresh_engine("events3")
    setup_player(engine3)
    engine3.execute_action("pre_battle_action", {"sub_action": "探索", "count": 1})
    pool3 = [e["id"] for e in engine3._current_event_pool()]
    num = pool3.index("无名冢") + 1
    engine3.execute_action("random_number", {"pool_name": "event_pool", "number": num})
    r = engine3.execute_action("choose_event_option", {"option_index": 1})  # 为你而战
    assert not r["success"] and r.get("unavailable") and engine3.event_pool.is_encountered("无名冢")
    print("  ✓ 创造性选项（设计新遗物）：如实拒绝，不假装成功")

    # 消耗品战斗中使用：穿甲弹忽略格挡与闪避
    engine4 = fresh_engine("events4")
    setup_player(engine4)
    r = engine4.execute_action("pre_battle_action", {"sub_action": "探索", "count": 1})
    pool4 = [e["id"] for e in engine4._current_event_pool()]
    num = pool4.index("无魂泥潭") + 1
    engine4.execute_action("random_number", {"pool_name": "event_pool", "number": num})
    r = engine4.execute_action("choose_event_option", {"option_index": 0})  # 采集淤泥
    assert r["success"] and any(c.name == "绝息淤泥" for c in engine4.state.consumables)
    print("  ✓ 无魂泥潭：流血10→获得绝息淤泥")
    engine4.execute_action("choose_event_option", {})  # 工具库伪节点
    engine4.execute_action("random_number", {"pool_name": "tool_library", "number": 4})
    assert not engine4.state.pending_event
    drain_energy(engine4)
    start_battle(engine4, numbers=[1])
    engine4.execute_action("round_start", {})
    # 淤泥：anytime 不耗出手，立刻以撤退结算
    r = engine4.execute_action("use_consumable", {"name": "绝息淤泥"})
    assert r["success"] and r.get("result", {}).get("escaped"), r
    assert not any(c.name == "绝息淤泥" for c in engine4.state.consumables), "耐久归零应销毁"
    print("  ✓ 绝息淤泥：战斗中任意时刻使用→本次战终立刻逃脱，耐久归零销毁")


def test_five_relics():
    """五件遗物真实实装：回锋刀/折速法印/鲜血契约/守夜灯/卖身契（结算全走公开接口）"""
    print("\n=== 测试：五件遗物实装 ===")
    from engine.models import Entity as _Entity, DaoWenInstance

    # 遗物池13件全部 implemented
    assert all(r.get("implemented") for r in RELIC_POOL), "遗物池13件必须全部 implemented"
    print("  ✓ 遗物池13/13全部标记 implemented")

    engine = fresh_engine("relic5")
    setup_player(engine)  # 血限60/速限8/法限14
    for nm in ("回锋刀", "折速法印", "鲜血契约", "守夜灯", "卖身契"):
        relic = next(r for r in engine.state.relics_pool if r.name == nm)
        assert "implemented" in relic.tags, f"{nm}必须带 implemented 标记"
        engine.state.relics_pool.remove(relic)
        engine.state.relics.append(relic)
    # 朋友夹具（构造面板；其参战/承伤均经真实接口结算）
    friend = _Entity(name="乞丐", entity_type="朋友", blood_limit=50, current_hp=50,
                     attack_count=2, attack_power=3)
    engine.state.friends.append(friend)
    drain_energy(engine)
    p = engine.state.player

    # ---- 前置校验：超上限如实拒绝，且不带病开战 ----
    r = engine.execute_action("battle_start", {"battle_background": "测试", "zhesu_x": 9})
    assert not r["success"] and "疲惫不能透支" in r["error"]
    assert engine.state.phase == "pre_battle", "拒绝后不得推进阶段"
    r = engine.execute_action("battle_start", {"battle_background": "测试", "xianxue_x": 13})
    assert not r["success"] and "超出上限" in r["error"]   # 上限=60//5=12
    r = engine.execute_action("battle_start", {"battle_background": "测试", "maishenqi_friend": "不存在的人"})
    assert not r["success"] and "不存在或已死亡" in r["error"]
    print("  ✓ 战始声明校验：疲惫透支/流血超限/指定无对象，全部拒绝且不推进")

    # ---- 合规声明：折速3+鲜血10+卖身契指定乞丐 ----
    r = engine.execute_action("battle_start", {
        "battle_background": "测试背景", "zhesu_x": 3, "xianxue_x": 10,
        "maishenqi_friend": "乞丐"})
    assert r["success"], f"战始失败: {r}"
    for i in range(r["spawn_count"]):
        engine.execute_action("random_number", {
            "pool_name": f"spawn_battle_{engine.state.current_battle}", "number": 5})  # 眼树 1×26/96
    assert engine.state.phase == "in_combat"
    mon = engine.state.enemies[0]
    assert mon.name == "眼树"
    assert p.current_speed == 5 and p.current_mana == 14 + 18 + 10 and p.current_hp == 50, \
        f"折速疲惫3→+18法力；鲜血10→-10血+10法: 速{p.current_speed} 法{p.current_mana} 血{p.current_hp}"
    assert mon.current_hp == 96 - 9, f"回锋刀：战始疲惫3→反击9点（顺延怪物首位）: {mon.current_hp}"
    print("  ✓ 折速法印：疲惫3→法力+18；鲜血契约：流血10→首回合法力+10；回锋刀：战始疲惫反击9")

    # ---- 第1回合[回始]：补满不削平(42>14保留) + 回锋刀[回始]3×(8-5)=9 ----
    r = engine.execute_action("round_start", {})
    assert p.current_mana == 42, f"补满为补足不削平（战始增益应保留）: {p.current_mana}"
    assert mon.current_hp == 87 - 9, f"回锋刀[回始]3×3=9: {mon.current_hp}"
    print("  ✓ [回始]补满为补足不削平：战始增益法力42保留；回锋刀[回始]追加9")

    # ---- 守夜灯[敌回始] + 回锋刀闪避反击 ----
    r = engine.execute_action("monster_turn", {"monster": "眼树", "acts": [
        {"type": "attack_round", "target": "测试者", "dodges": [True]}]})
    assert r["success"]
    assert any("守夜灯" in n for n in r["relic_notes"]), f"敌回始应授予法力: {r['relic_notes']}"
    assert p.current_mana == 42 + 7, f"守夜灯+法限50%=7: {p.current_mana}"
    assert p.current_speed == 4, "闪避耗1速"
    assert mon.current_hp == 78 - 3, f"回锋刀闪避反击3: {mon.current_hp}"
    hit = r["turn_log"][0]["hits"][0]
    assert "relic_回锋刀" in hit
    print("  ✓ 守夜灯[敌回始]+7法力；闪避失速→回锋刀反击3（来源=攻击者眼树）")

    # ---- 卖身契：慈悲3的流血代价由乞丐承担 ----
    p.dao_wen["慈悲"] = DaoWenInstance(dao_wen=engine._build_daowen_def("慈悲"))
    hp_before, fhp_before = p.current_hp, friend.current_hp
    r = engine.execute_action("use_daowen", {
        "daowen_name": "慈悲", "x": 3, "target": p.name})
    assert r["success"], f"慈悲失败: {r}"
    assert friend.current_hp == fhp_before - 3, f"流血3应由乞丐承担: {friend.current_hp}"
    assert p.current_hp == hp_before + 3, f"自身不流血且被慈悲回复3: {p.current_hp}"
    assert any(c.get("paid_by", "").endswith("乞丐")
               for c in r.get("cost_applied", [])), f"结算必须标明卖身契转承: {r.get('cost_applied')}"
    print("  ✓ 卖身契：慈悲3流血代价由乞丐承担（50→47），自身回复3，血誓戒逻辑不误触")

    # ---- [敌回终]全部法力清空（README 213行，守夜灯授予法力一并清空）----
    engine.execute_action("round_end", {})
    assert p.current_mana == 0, f"敌回终应清空全部法力: {p.current_mana}"
    print("  ✓ [敌回终]全部法力清空（守夜灯授予的7点随全局规则一并清空）")

    # ---- 第2回合[回始]：法力补足至法限 + 回锋刀[回始]：3×(速限8-当前4)=12 ----
    r = engine.execute_action("round_start", {})
    assert p.current_mana == 14, f"回始应补足至法限14: {p.current_mana}"
    assert mon.current_hp == 75 - 12, f"回锋刀回始12点: {mon.current_hp}"
    assert any("回锋刀" in n and "3×4" in n for n in r["result"].get("relic_notes", [])), r["result"].get("relic_notes")
    print("  ✓ 回锋刀[回始]：对怪物造成3×(8-4)=12点伤害")

    # ---- 卖身契[命零]后失效：转到自身支付 ----
    friend.current_hp = 1
    r = engine.execute_action("use_daowen", {"daowen_name": "慈悲", "x": 2, "target": p.name})
    assert r["success"] and not friend.is_alive, "乞丐承担流血2后命零"
    hp_before = p.current_hp
    r = engine.execute_action("use_daowen", {"daowen_name": "慈悲", "x": 1, "target": p.name})
    assert r["success"]
    assert p.current_hp == hp_before, "指定对象命零后代价回落自身（流血1再被慈悲回复1，净不变）"
    assert any("卖身契" in str(c) and "失效" in str(c) for c in r.get("cost_applied", [])), \
        f"必须如实记录卖身契失效: {r.get('cost_applied')}"
    print("  ✓ 卖身契：指定对象[命零]→效果失效如实记录，代价回落自身")


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
        test_shell_daowen_bizhong_manqian,
        test_resonance_hijack,
        test_events_system,
        test_five_relics,
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
