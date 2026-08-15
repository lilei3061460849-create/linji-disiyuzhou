"""算出了但没人消费的道纹：统一接线回归。

同类洞：calculate 出了键 / duration 挂了状态，apply 或战斗钩子不读。
缓慢是原型（effective 从未消费）。本文件覆盖同批接线。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.daowen import DaoWenEngine
from engine.models import DaoWen, DaoWenInstance, Entity, StatusEffect


def _engine(suffix):
    os.makedirs("/tmp/linji_tests", exist_ok=True)
    engine = GameEngine(db_path=f"/tmp/linji_tests/test_wiring_{suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "龙心谷"})
    engine.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    # 本文件是道纹单元接线测试，直接构造合法战斗阶段，避免随机出怪干扰。
    engine.state.phase = "in_combat"
    p = engine.state.player
    # 多道纹同回合连发：速限 24 → 出手 8，避免第 5 手被预算挡掉误报“没接线”。
    p.speed_limit = 24
    p.current_speed = 24
    p.current_mana = 80
    p.mana_limit = 80
    return engine


def _give(player, name):
    player.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


def _monster(engine, name="靶怪", hp=100, atk=3, ap=6):
    m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
               attack_count=atk, attack_power=ap)
    engine.state.enemies.append(m)
    return m


def test_manqian_happy_blocks_when_budget_le_x():
    """正常：目标出手 3，缓慢 3 生效，本回合无法出手。"""
    engine = _engine("manqian_ok")
    p = engine.state.player
    _give(p, "缓慢")
    foe = Entity(name="对手", entity_type="轮回者", blood_limit=80, current_hp=80,
                 mana_limit=20, current_mana=20, speed_limit=8, current_speed=8)
    foe.dao_wen["杀伐"] = DaoWenInstance(
        DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    engine.state.enemies.append(foe)
    engine.execute_action("round_start", {})
    assert foe.action_count == 3
    mana = p.current_mana
    r = engine.execute_action("use_daowen", {"daowen_name": "缓慢", "x": 3, "target": foe.name})
    assert r["success"], r
    assert r["calculation"]["effective"] is True
    assert foe.has_status("缓慢")
    assert p.current_mana == mana  # 缓慢改冷却后不消耗法力
    assert engine.combat.can_act(foe) is False
    blocked = engine._consume_action_or_error(foe)
    assert blocked is not None
    assert "无法出手" in blocked["error"]


def test_manqian_boundary_not_effective_and_monster_budget():
    """边界：出手 3 用缓慢 2 不生效；怪物按 2 手不算攻击次数。"""
    engine = _engine("manqian_bound")
    p = engine.state.player
    _give(p, "缓慢")
    foe = Entity(name="对手", entity_type="轮回者", blood_limit=80, current_hp=80,
                 speed_limit=8, current_speed=8)
    engine.state.enemies.append(foe)
    engine.execute_action("round_start", {})
    r = engine.execute_action("use_daowen", {"daowen_name": "缓慢", "x": 2, "target": foe.name})
    assert r["success"]
    assert r["calculation"]["effective"] is False
    assert not foe.has_status("缓慢")
    assert engine.combat.can_act(foe)

    fish = _monster(engine, "缝合鱼", hp=234, atk=3, ap=6)
    assert DaoWenEngine.single_round_action_count(fish) == 2
    calc = DaoWenEngine.resolve("缓慢", 1, target=fish)
    assert calc["target_action_count"] == 2
    assert calc["effective"] is False
    calc2 = DaoWenEngine.resolve("缓慢", 2, target=fish)
    assert calc2["effective"] is True


def test_manqian_invalid_and_ziyang_zishi_shuaibai():
    """错误：法力不足 / X<1；滋养回血；自食打自己；衰败扣当前生命%。"""
    engine = _engine("keys")
    p = engine.state.player
    for n in ("缓慢", "滋养", "自食", "衰败"):
        _give(p, n)
    m = _monster(engine)
    engine.execute_action("round_start", {})

    # 缓慢已改为代价：冷却X，不耗法力；X=0 仍非法
    r0 = engine.execute_action("use_daowen", {"daowen_name": "缓慢", "x": 0, "target": m.name})
    assert r0["success"] is False
    # 怪物出手2，缓慢X=2才生效
    r1 = engine.execute_action("use_daowen", {"daowen_name": "缓慢", "x": 2, "target": m.name})
    assert r1["success"] is True
    assert m.has_status("缓慢")
    # 冷却X=2：本场已用，再次发动被拒
    r2 = engine.execute_action("use_daowen", {"daowen_name": "缓慢", "x": 2, "target": m.name})
    assert r2["success"] is False
    assert "冷却" in r2["error"] or "不可用" in r2["error"]
    assert "X必须≥1" in r0["error"]

    m.current_hp = 50
    r2 = engine.execute_action("use_daowen", {"daowen_name": "滋养", "x": 1, "target": m.name})
    assert r2["success"]
    assert m.current_hp == 60  # 100*10%=10

    p.attack_power = 5
    p.current_hp = 40
    r3 = engine.execute_action("use_daowen", {"daowen_name": "自食", "x": 3, "target": p.name})
    assert r3["success"]
    assert p.attack_power == 2
    assert p.current_hp == 43

    hp = m.current_hp
    r4 = engine.execute_action("use_daowen", {"daowen_name": "衰败", "x": 1, "target": m.name})
    assert r4["success"]
    assert m.current_hp == hp  # R32：发动时不立即触发
    assert m.has_status("衰败")
    engine.state.combat_subphase = "await_round_end"  # 单元测试跳过怪物行动
    engine.execute_action("round_end", {})
    engine.execute_action("round_start", {})
    assert m.current_hp == hp - 6  # [回始]ceil(60*10%)


def test_jiahai_guzhi_fennu_jieli_jisheng():
    """加害挂目标加伤；固执单次掉 1；愤怒法力减半；借力加伤；寄生吸血。"""
    engine = _engine("hooks")
    p = engine.state.player
    for n in ("加害", "固执", "愤怒", "借力", "寄生"):
        _give(p, n)
    m = _monster(engine, ap=10)
    engine.execute_action("round_start", {})

    r = engine.execute_action("use_daowen", {"daowen_name": "加害", "x": 4, "target": m.name})
    assert r["success"]
    assert m.has_status("加害")
    d = engine.combat._apply_hostile_damage(m, 10)
    assert d["actual_damage"] == 14

    p.current_hp = 50
    engine.execute_action("use_daowen", {"daowen_name": "固执", "x": 2})
    assert p.has_status("固执")
    d2 = p.take_damage(20)
    assert d2["actual_damage"] == 1
    assert p.current_hp == 49
    d3 = p.take_damage(8, "代价")
    assert d3["actual_damage"] == 8

    engine.execute_action("use_daowen", {"daowen_name": "愤怒", "x": 2, "target": p.name})
    assert p.has_status("愤怒")
    mana = p.current_mana
    assert p.spend_mana(10) is True
    assert p.current_mana == mana - 5

    engine.execute_action("use_daowen", {"daowen_name": "借力", "x": 2, "target": p.name})
    assert p.has_status("借力")
    assert engine.combat._jieli_boost(p, 10) == 12  # +20%

    host = _monster(engine, name="寄生体", hp=80, atk=1, ap=1)
    r_js = engine.execute_action("use_daowen", {"daowen_name": "寄生", "x": 1, "target": host.name})
    assert r_js["success"], r_js
    assert host.has_status("寄生")
    p.current_hp = 40
    before = p.current_hp
    engine.combat._apply_hostile_damage(host, 10)
    assert p.current_hp == before + 2  # 10 * 20%


def test_huaxiang_zhuiluo_dingxing_wushen_xuanyun():
    """滑翔视同飞行；坠落落地并减半；定型挡弱化；无神打自己；眩晕掉血苏醒。"""
    engine = _engine("ctrl")
    p = engine.state.player
    for n in ("滑翔", "坠落", "定型", "无神", "弱化"):
        _give(p, n)
    m = _monster(engine, ap=8)
    engine.execute_action("round_start", {})

    engine.execute_action("use_daowen", {"daowen_name": "滑翔", "x": 2})
    assert p.has_status("滑翔")
    assert engine.combat.is_targetable(m, p) is False

    engine.execute_action("use_daowen", {"daowen_name": "坠落", "x": 1})
    assert not p.has_status("滑翔")
    assert p.has_status("坠落")
    assert engine.combat.is_targetable(m, p) is True
    m.add_status(StatusEffect(name="坠落", remaining_rounds=1, value=1, source="测"))
    hp = p.current_hp
    engine.combat.resolve_attack(m, p, dodge=False)
    assert p.current_hp == hp - 4  # ceil(8/2)

    m.attack_power = 10
    engine.execute_action("use_daowen", {"daowen_name": "定型", "x": 1, "target": m.name})
    engine.execute_action("use_daowen", {"daowen_name": "弱化", "x": 3, "target": m.name})
    assert m.attack_power == 10

    foe = Entity(name="失神者", entity_type="轮回者", blood_limit=80, current_hp=80,
                 mana_limit=20, current_mana=20, speed_limit=6, current_speed=6)
    foe.dao_wen["杀伐"] = DaoWenInstance(
        DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    engine.state.enemies.append(foe)
    r_ws = engine.execute_action("use_daowen", {"daowen_name": "无神", "x": 1, "target": foe.name})
    assert r_ws["success"], r_ws
    assert foe.has_status("无神")
    # 敌方轮回者只能在死斗里 use_daowen；非死斗会报「不能作为行动者」。
    engine.state.in_final_duel = True
    engine.state.duel_turn = "opponent_side"
    hp_f = foe.current_hp
    r = engine.execute_action("use_daowen", {
        "actor": foe.name, "daowen_name": "杀伐", "x": 2, "target": p.name,
    })
    assert r["success"], r
    assert foe.current_hp == hp_f - 6  # 无神改打自己(杀伐3X)

    m.add_status(StatusEffect(name="眩晕", remaining_rounds=2, value=1, source="测"))
    assert engine.combat.can_act(m) is False
    m.take_damage(3)
    assert not m.has_status("眩晕")
    assert engine.combat.can_act(m)


def test_ziyu_cast_does_not_heal_until_round_start():
    """正常：自愈发动当下不奶，回始才按血限10X%奶一次。"""
    import math
    engine = _engine("ziyu_ok")
    p = engine.state.player
    _give(p, "自愈")
    engine.execute_action("round_start", {})
    p.current_hp = 30
    r = engine.execute_action("use_daowen", {"daowen_name": "自愈", "x": 2})
    assert r["success"], r
    assert p.has_status("自愈")
    assert p.current_hp == 30
    expected = math.ceil(p.blood_limit * 20 / 100)
    engine.state.combat_subphase = "await_round_end"
    engine.execute_action("round_end", {})
    engine.execute_action("round_start", {})
    assert p.current_hp == 30 + expected


def test_ziyu_necrosis_blocks_and_invalid_x():
    """边界：坏死回始不奶；错误：X<1 拒绝。"""
    engine = _engine("ziyu_bound")
    p = engine.state.player
    _give(p, "自愈")
    engine.execute_action("round_start", {})
    bad = engine.execute_action("use_daowen", {"daowen_name": "自愈", "x": 0})
    assert bad["success"] is False
    assert "X必须≥1" in bad["error"]
    engine.execute_action("use_daowen", {"daowen_name": "自愈", "x": 1})
    p.current_hp = 30
    p.add_status(StatusEffect(name="坏死", remaining_rounds=-1, value=0, source="测"))
    engine.execute_action("round_start", {})
    assert p.current_hp == 30


def test_ziyu_monster_activate_heals_next_round_start():
    """正常：怪物激活自愈只挂状态，下个回始才奶。"""
    import math
    engine = _engine("ziyu_mon")
    m = _monster(engine, "自愈鱼", hp=100, atk=1, ap=1)
    m.dao_wen["自愈"] = DaoWenInstance(
        DaoWen(name="自愈", formula="", cost_type="异变", cost_formula="5X", effect_formula=""),
        x_value=1)
    engine.state.current_round = 2
    prepared = engine.execute_action("prepare_monster_phase", {})
    actor = prepared["result"]["actors"][0]
    resolved = engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"],
        "choices": [{"actor_ref": actor["actor_ref"],
                     "daowen": {"name": "自愈", "dodge": False, "blood_shadow": False, "trigger_spell_choices": {}},
                     "attack_actions": [{"hits": [{"target_ref": "player:0", "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}]}]}],
    })
    assert resolved["success"]
    assert m.has_status("自愈")
    m.current_hp = 50
    engine.execute_action("round_end", {})
    engine.execute_action("round_start", {})
    assert m.current_hp == 50 + math.ceil(m.blood_limit * 10 / 100)


def test_jisu_jiasu_dongcha():
    """急速每闪两次+1 速；加速让超频翻倍；洞察闪避后下回始+10 法力。"""
    engine = _engine("speed")
    p = engine.state.player
    for n in ("急速", "加速", "超频", "洞察"):
        _give(p, n)
    engine.execute_action("round_start", {})

    engine.execute_action("use_daowen", {"daowen_name": "急速", "x": 2, "target": p.name})
    spd = p.current_speed
    engine.combat._note_dodge(p)
    assert p.current_speed == spd
    engine.combat._note_dodge(p)
    assert p.current_speed == spd + 1

    engine.execute_action("use_daowen", {"daowen_name": "加速", "x": 1, "target": p.name})
    spd = p.current_speed
    engine.execute_action("use_daowen", {"daowen_name": "超频", "x": 3})
    assert p.current_speed == spd + 6

    engine.execute_action("use_daowen", {"daowen_name": "洞察", "x": 1, "target": p.name})
    engine.combat._note_dodge(p)
    assert getattr(p, "_dongcha_pending", 0) == 10
    mana = p.current_mana
    engine.state.combat_subphase = "await_round_end"
    engine.execute_action("round_end", {})
    engine.execute_action("round_start", {})
    assert p.current_mana == p.mana_limit + 10
