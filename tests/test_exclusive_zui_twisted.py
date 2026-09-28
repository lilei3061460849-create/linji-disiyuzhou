"""
F2 全量验证：罪孽都市（点金/逼债/抵扣/清算/赌命/消灾/假钞）与扭曲都市（爆裂/退化）的引擎侧实装。
- 正常：按《副本/罪孽都市.md》《副本/扭曲都市.md》定义结算
- 边界：碎片不足/无遗物/无碎片/反噬致死/退化归零等
- 错误：假碎片不足/碎片不足被拒绝
"""
import os
import sys
import pytest

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance, Relic, StatusEffect
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.daowen import DaoWenEngine
from engine.models import GameState
from tests.monster_phase_support import resolve_monster_phase


def _setup(region="罪孽都市", mana=100, speed_limit=99):
    engine = GameEngine(rng_seed=42)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 6, "mana_points": 8})
    finish_initial_daowen(engine)
    engine.state.current_region = region
    engine.state.phase = "in_combat"
    player = engine.state.player
    player.current_mana = mana
    player.speed_limit = speed_limit
    player.current_speed = speed_limit
    player.attack_power = 5  # 默认面板攻击力1，测试固定为5以便断言伤害
    engine.state.phase = "in_combat"
    return engine


def _grant(engine, names):
    for n in names:
        engine.state.player.dao_wen[n] = DaoWenInstance(
            DaoWen(name=n, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


def _add_monster(engine, name="靶怪", hp=100, atk=5, shards=0, **kw):
    m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
               attack_count=1, attack_power=atk, **kw)
    m.shards = shards
    engine.state.enemies.append(m)
    return m


def _apply_monster_daowen(engine, caster, name, x, target=None):
    target = target or caster
    calc = DaoWenEngine.resolve(name, x, target=target, caster=caster)
    return engine.combat.apply_daowen_effect(name, calc, caster, target)


# ==================== 失忆（2026-09-28 由【点金】改名改值；前身【洗劫】已废弃）====================
# 道纹【失忆】：消耗8X法力 → 直接获得80X真碎片，与伤害彻底脱钩。
# 旧"血限扣减/印记/清算联动"本版本不存在；状态【洗劫】及其"造成伤害时夺取等量碎片"
# 机制**保留**，但已不由道纹发放，只剩【帮派令】在[战始]发放。


def test_normal_shiyi_converts_mana_to_shards():
    """正常路径：失忆X 消耗8X法力，直接换80X真碎片，不碰目标、不挂状态。"""
    engine = _setup(); _grant(engine, ["失忆"])
    player = engine.state.player
    m = _add_monster(engine, shards=20)
    mana0, shard0 = player.current_mana, engine.state.shards
    r = engine.execute_action("use_daowen", {"daowen_name": "失忆", "x": 2, "target": m.name})
    assert r["success"], r
    assert player.current_mana == mana0 - 16           # 2026-09-28 用户令：消耗8X（X=2）= 16 法力
    assert engine.state.shards == shard0 + 160        # 换到 80*2=160 真碎片
    assert m.shards == 20, "失忆不再从目标身上夺取"
    assert not player.has_status("失忆"), "失忆是即时结算，不得挂状态"


def test_boundary_shiyi_insufficient_mana_rejected():
    """边界：法力不够付 8X 时应当被拒绝，而不是半结算。"""
    engine = _setup(mana=15); _grant(engine, ["失忆"])  # 失忆X=2 需 8X=16，15 点不够
    player = engine.state.player
    _add_monster(engine, shards=20)
    shard0 = engine.state.shards
    r = engine.execute_action("use_daowen", {"daowen_name": "失忆", "x": 2, "target": None})
    assert r["success"] is False
    assert engine.state.shards == shard0 and player.current_mana == 15


# 2026-09-28：【点金】已正式改名为【失忆】，旧名不再接受（避免出现双注册与
# diff_project_daowen 报警）。如果需要旧存档兼容，可在 use_daowen 层做 alias。
def test_legacy_dianjin_name_rejected():
    engine = _setup(); _grant(engine, ["失忆"])
    m = _add_monster(engine, shards=20)
    r = engine.execute_action("use_daowen", {"daowen_name": "点金", "x": 1, "target": m.name})
    assert r["success"] is False, "旧名【点金】已废弃，必须用新名【失忆】"


def test_xijie_status_still_steals_shards_on_damage():
    """状态【洗劫】机制保留：直接挂状态（帮派令口径）后，造成伤害仍夺等量碎片。"""
    # 攻力=当前法力（2026-09-10），所以把法力压到 5 才能得到可断言的每手 5 点伤害
    engine = _setup(mana=5)
    player = engine.state.player
    player.add_status(StatusEffect(name="洗劫", value=2, remaining_rounds=2, source=player.name))
    m = _add_monster(engine, shards=20)
    res = engine.combat.resolve_attack(player, m, hit_index=0, is_must_hit=True, dodge=False)
    assert res["damage_dealt"] == 5
    assert m.shards == 15, f"应夺5碎片，实{m.shards}"
    assert engine.state.shards == 20 + 5


def test_boundary_xijie_status_no_shards_no_steal():
    engine = _setup(mana=5)   # 同上：法力即攻力
    player = engine.state.player
    player.add_status(StatusEffect(name="洗劫", value=1, remaining_rounds=2, source=player.name))
    m = _add_monster(engine, shards=0)
    res = engine.combat.resolve_attack(player, m, hit_index=0, is_must_hit=True, dodge=False)
    assert res["damage_dealt"] == 5
    assert m.shards == 0 and engine.state.shards == 20  # 无碎片则夺取无效


def test_monster_xijie_steals_player_shards_fake_first():
    engine = _setup()
    player = engine.state.player
    engine.state.fake_shards = 10
    m = _add_monster(engine, shards=0)
    m.add_status(StatusEffect(name="洗劫", value=1, remaining_rounds=2, source=m.name))
    res = engine.combat.resolve_attack(m, player, hit_index=0, is_must_hit=True, dodge=False)
    assert res["damage_dealt"] == 5
    assert engine.state.fake_shards == 5, "玩家被洗劫应先扣假碎片"  # 10-5
    assert engine.state.shards == 20


# ==================== 逼债 ====================

def test_normal_bizhai_drains_shards_each_round_start():
    engine = _setup(); _grant(engine, ["逼债"])
    m = _add_monster(engine, shards=10)
    r = engine.execute_action("use_daowen", {"daowen_name": "逼债", "x": 3, "target": m.name})
    assert r["success"], r
    # 发动瞬间不结算（[回始]语义）
    assert m.shards == 10
    engine.combat.round_start()
    assert m.shards == 7, "回始应扣3碎片"


def test_boundary_bizhai_shortfall_becomes_debt():
    """DM裁定D 2026-08-22：无力支付部分记为负债（旧"否则失去2X血限"废止）。"""
    engine = _setup(); _grant(engine, ["逼债"])
    m = _add_monster(engine, shards=2)  # 碎片 < X=3
    engine.execute_action("use_daowen", {"daowen_name": "逼债", "x": 3, "target": m.name})
    engine.combat.round_start()
    assert m.shards == -1, f"余额2抵3，差额1应计负债（shards=-1），实{m.shards}"
    assert m.blood_limit == 100, "裁定D后逼债不再触碰血限"


def test_monster_bizhai_on_player():
    engine = _setup()
    player = engine.state.player
    m = _add_monster(engine, shards=0)
    _apply_monster_daowen(engine, m, "逼债", 2, player)
    assert getattr(player, "_bizhai", []), "玩家应被挂账"
    engine.combat.round_start()
    assert engine.state.shards == 18, "玩家回始应失2碎片"


def test_boundary_battle_end_clears_ledger():
    """[战终]必须清除逼债/清算挂账与封印遗物，否则跨场残留继续扣减玩家"""
    engine = _setup()
    player = engine.state.player
    m = _add_monster(engine, shards=0)
    _apply_monster_daowen(engine, m, "逼债", 2, player)
    _apply_monster_daowen(engine, m, "清算", 1, player)
    engine.state.sealed_relics = {"避风铃": 3}
    assert player._bizhai and player._qingsuan
    m.current_hp = 0
    m.is_alive = False
    engine.execute_action("battle_end")
    assert player._bizhai == [], "战终应清逼债挂账"
    assert player._qingsuan == [], "战终应清算算挂账"
    assert engine.state.sealed_relics == {}, "战终应清封印遗物"


# ==================== 豪夺（2026-09-28 由【抵扣】改名改制） ====================
# 旧【抵扣】=消耗3X法力封印遗物；新【豪夺】=消耗5X碎片夺取遗物持续X回合。

def test_normal_haoduo_steals_relic_for_x_rounds():
    engine = _setup(); _grant(engine, ["豪夺"])
    player = engine.state.player
    # 目标（敌人）持有1件遗物
    m = _add_monster(engine, shards=0)
    m.relics = [Relic(name="防弹插板", effect="伤害减半"), Relic(name="其他", effect="x")]
    engine.state.shards = 100
    shards_before = engine.state.shards
    r = engine.execute_action("use_daowen", {"daowen_name": "豪夺", "x": 2, "target": m.name})
    assert r["success"], r
    assert engine.state.shards == shards_before - 10, "消耗5X=10碎片"
    # 敌人失去防弹插板
    assert not any(r.name == "防弹插板" for r in m.relics)
    # 玩家持有防弹插板
    assert any(r.name == "防弹插板" for r in engine.state.relics), "豪夺成功后施法者应获得该遗物"
    assert "防弹插板" in engine.state.stolen_relics
    assert engine.state.stolen_relics["防弹插板"]["remaining"] == 2


def test_haoduo_no_relic_no_effect():
    engine = _setup(); _grant(engine, ["豪夺"])
    m = _add_monster(engine, shards=0)
    m.relics = []
    engine.state.shards = 100
    r = engine.execute_action("use_daowen", {"daowen_name": "豪夺", "x": 1, "target": m.name})
    # 无遗物时仍会成功（没东西可夺），但不扣遗物
    assert r["success"]
    assert engine.state.stolen_relics == {}


def test_legacy_dikou_name_rejected():
    """旧名【抵扣】已从注册表移除，use_daowen 不再受理该名字。"""
    engine = _setup(); _grant(engine, ["豪夺"])
    m = _add_monster(engine)
    m.relics = [Relic(name="回锋刀", effect="x")]
    engine.state.shards = 100
    r = engine.execute_action("use_daowen", {"daowen_name": "抵扣", "x": 1, "target": m.name})
    assert r["success"] is False, "旧名【抵扣】已废弃，必须用新名【豪夺】"


# ==================== 清算 ====================

def test_normal_qingsuan_drains_shield_each_round_start():
    engine = _setup(); _grant(engine, ["清算"])
    m = _add_monster(engine, shards=0)
    m.shield = 50
    r = engine.execute_action("use_daowen", {"daowen_name": "清算", "x": 2, "target": m.name})
    assert r["success"], r
    assert m.shield == 50  # 发动瞬间不结算
    engine.combat.round_start()
    assert m.shield == 50 - 20, f"应失[你碎片=20]格挡，实{m.shield}"  # 玩家初始碎片20


def test_boundary_qingsuan_zero_shield():
    engine = _setup(); _grant(engine, ["清算"])
    m = _add_monster(engine, shards=0)
    m.shield = 0
    engine.execute_action("use_daowen", {"daowen_name": "清算", "x": 2, "target": m.name})
    engine.combat.round_start()
    assert m.shield == 0


# ==================== 赌命 ====================

def test_normal_duming_random_target_each_round_start():
    engine = _setup(); _grant(engine, ["赌命"])
    player = engine.state.player
    engine.state.fake_shards = 10
    m = _add_monster(engine, hp=100)
    r = engine.execute_action("use_daowen", {"daowen_name": "赌命", "x": 2})
    assert r["success"], r
    assert engine.state.fake_shards == 8, "赌命2应消耗2假碎片"
    assert player.has_status("赌命")
    before = {e.name: e.current_hp for e in [player] + engine.state.enemies}
    effects = engine.combat.round_start()["effects"]
    duming = [e for e in effects if e["type"] == "duming"]
    assert len(duming) == 1
    tgt_name = duming[0]["target"]
    assert tgt_name in before
    expect_loss = max(0, duming[0]["lost"])
    assert before[tgt_name] - expect_loss == duming[0]["hp_after"]


def test_boundary_duming_single_alive_always_targeted():
    engine = _setup(); _grant(engine, ["赌命"])
    player = engine.state.player
    engine.state.fake_shards = 10
    _add_monster(engine, hp=100)
    # 怪物先死，只剩玩家
    engine.state.enemies[0].is_alive = False
    engine.execute_action("use_daowen", {"daowen_name": "赌命", "x": 1})
    hp_before = player.current_hp
    effects = engine.combat.round_start()["effects"]
    duming = [e for e in effects if e["type"] == "duming"]
    assert duming and duming[0]["target"] == player.name
    assert player.current_hp == hp_before - max(0, duming[0]["lost"])


def test_error_duming_insufficient_fake_shards_rejected():
    engine = _setup(); _grant(engine, ["赌命"])
    engine.state.fake_shards = 1
    r = engine.execute_action("use_daowen", {"daowen_name": "赌命", "x": 5})
    assert not r["success"], "假碎片不足应被拒绝"
    assert "假碎片不足" in r.get("error", "")


# ==================== 消灾 ====================

def test_normal_xiaozai_rerolls_next_auto_roll():
    engine = _setup(); _grant(engine, ["消灾"])
    engine.state.fake_shards = 60
    r = engine.execute_action("use_daowen", {"daowen_name": "消灾", "x": 1})
    assert r["success"], r
    assert engine.state.fake_shards == 60 - 50, "应优先扣50X假碎片"
    assert engine.dice.rerolls_pending == 1
    roll = engine.dice.auto_roll("t", ["a", "b", "c"])
    assert roll["rerolled"] is True, "下一次自动随机应被重投"
    assert engine.dice.rerolls_pending == 0


def test_boundary_xiaozai_real_payment_fallback():
    engine = _setup(); _grant(engine, ["消灾"])
    engine.state.shards = 10
    engine.state.fake_shards = 0
    r = engine.execute_action("use_daowen", {"daowen_name": "消灾", "x": 1})
    assert r["success"], r
    assert engine.state.shards == 10 - 5, "无假碎片时按5X真碎片付费"
    assert engine.dice.rerolls_pending == 1


def test_boundary_xiaozai_out_of_combat_double_cost():
    engine = _setup(); _grant(engine, ["消灾"])
    engine.state.phase = "pre_battle"  # 局外
    engine.state.shards = 10
    engine.state.fake_shards = 0
    r = engine.execute_action("use_daowen", {"daowen_name": "消灾", "x": 1})
    assert r["success"], r
    assert engine.state.shards == 10 - 10, "局外发动消耗×2：5X→10X"


def test_error_xiaozai_insufficient_funds_rejected():
    engine = _setup(); _grant(engine, ["消灾"])
    engine.state.shards = 4
    engine.state.fake_shards = 0
    r = engine.execute_action("use_daowen", {"daowen_name": "消灾", "x": 1})
    assert not r["success"], "碎片不足应被拒绝"
    assert "碎片不足" in r.get("error", "")


# ==================== 假钞 ====================

def test_normal_jiachao_gains_fake_shards():
    engine = _setup(); _grant(engine, ["假钞"])
    r = engine.execute_action("use_daowen", {"daowen_name": "假钞", "x": 2})
    assert r["success"], r
    assert engine.state.fake_shards == 20, "假钞2应获得20假碎片"
    assert engine.state.shards == 20, "真碎片不受影响"


def test_boundary_jiachao_fake_lost_first():
    engine = _setup()
    engine.state.fake_shards = 20
    engine.state.shards = 10
    engine.state.lose_shards(15)
    assert engine.state.fake_shards == 5, "应优先扣假碎片"
    assert engine.state.shards == 10


def test_monster_jiachao_and_duming_flow():
    """怪物侧联动：假钞→赌命（假碎片余额准入）→回始随机"""
    engine = _setup()
    m = _add_monster(engine, shards=0)
    m.dao_wen = {"假钞": DaoWenInstance(DaoWen(name="假钞", formula="", cost_type="消耗",
                                              cost_formula="X", effect_formula=""), x_value=2),
                 "赌命": DaoWenInstance(DaoWen(name="赌命", formula="", cost_type="假碎片",
                                              cost_formula="X", effect_formula=""), x_value=2)}
    engine.state.current_round = 2
    for expected in ("假钞", "赌命"):
        prepared = engine.execute_action("prepare_monster_phase", {})
        actor = prepared["result"]["actors"][0]
        resolved = engine.execute_action("resolve_monster_phase", {
            "token": prepared["result"]["token"],
            "choices": [{"actor_ref": actor["actor_ref"],
                         "daowen": {"name": expected, "dodge": False, "blood_shadow": False, "trigger_spell_choices": {}},
                         "attack_actions": [{"hits": [{"target_ref": "player:0", "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}]}]}],
        })
        assert resolved["success"]
        if expected == "假钞":
            assert getattr(m, "fake_shards", 0) == 20
            engine.execute_action("round_end", {})
            engine.execute_action("round_start", {})
    assert m.fake_shards == 20 - 2, "赌命2应扣2假碎片"
    assert m.has_status("赌命")
    engine.combat.round_start()  # 回始赌命结算（玩家+怪物都在场）
    assert True  # 不抛异常即通过


# ==================== 爆裂（扭曲都市；裁定口径：受到伤害前反噬） ====================

def test_normal_baolie_reflects_before_damage():
    engine = _setup(region="扭曲都市"); _grant(engine, ["杀伐"])
    player = engine.state.player
    m = _add_monster(engine, hp=100)
    m.add_status(StatusEffect(name="爆裂", value=1, remaining_rounds=2, source=m.name))
    hp_before = player.current_hp
    r = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 2, "target": m.name})
    assert r["success"], r
    # 杀伐2 造成4伤害（X²）：玩家先被反噬4，怪物仍受4伤害
    assert player.current_hp == hp_before - 4, f"攻击者应先失去等量生命，实差{hp_before - player.current_hp}"
    assert m.current_hp == 100 - 4


def test_boundary_baolie_attacker_dies_damage_cancelled():
    engine = _setup(region="扭曲都市"); _grant(engine, ["杀伐"])
    player = engine.state.player
    player.current_hp = 3
    m = _add_monster(engine, hp=100)
    m.add_status(StatusEffect(name="爆裂", value=1, remaining_rounds=2, source=m.name))
    r = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 2, "target": m.name})
    assert r["success"], r
    assert not player.is_alive, "攻击者被反噬致死"
    assert m.current_hp == 100, "攻击者先死，本次伤害不落地"


def test_boundary_baolie_attack_path():
    """物理攻击路径的反噬（怪物持爆裂，玩家攻击）"""
    engine = _setup(region="扭曲都市", mana=5)   # 攻力=当前法力：给5才有可断言的每手5点
    player = engine.state.player
    m = _add_monster(engine, hp=100)
    m.add_status(StatusEffect(name="爆裂", value=1, remaining_rounds=2, source=m.name))
    hp_before = player.current_hp
    res = engine.combat.resolve_attack(player, m, hit_index=0, is_must_hit=True, dodge=False)
    assert res["damage_dealt"] == 5
    assert player.current_hp == hp_before - 5, "攻击者先被反噬等量生命"


def test_normal_monster_baolie1_survives_same_round_end():
    """正常：怪挂爆裂1，同回终不掉，下一手玩家打仍反噬。"""
    engine = _setup(region="扭曲都市")
    player = engine.state.player
    m = _add_monster(engine, hp=100)
    _apply_monster_daowen(engine, m, "爆裂", 1)
    assert m.has_status("爆裂")
    engine.combat.round_end()
    assert m.has_status("爆裂"), "敌方爆裂1不应在同回终清掉"
    player.current_mana = 5   # 攻力=当前法力：压到5才有可断言的每手5点
    hp_before = player.current_hp
    res = engine.combat.resolve_attack(player, m, is_must_hit=True, dodge=False)
    assert res["damage_dealt"] == 5
    assert player.current_hp == hp_before - 5


def test_boundary_monster_baolie1_expires_at_next_enemy_round_end():
    """边界：怪挂爆裂1，下一次怪物回合开始（它们的敌回终）才到期。"""
    engine = _setup(region="扭曲都市")
    m = _add_monster(engine, hp=100)
    _apply_monster_daowen(engine, m, "爆裂", 1)
    engine.combat.round_end()
    assert m.has_status("爆裂")
    engine.state.current_round = 2  # 跳过白板，让怪物回合能跑
    resolve_monster_phase(engine.combat, {m.name: None})
    assert not m.has_status("爆裂"), "下一敌回终应到期"


def test_boundary_player_baolie1_expires_after_monster_phase():
    """边界：自己挂爆裂1，撑过本轮怪物出手，回终到期。"""
    engine = _setup(region="扭曲都市")
    _grant(engine, ["爆裂"])
    player = engine.state.player
    _add_monster(engine, hp=100)
    r = engine.execute_action("use_daowen", {"daowen_name": "爆裂", "x": 1})
    assert r["success"], r
    assert player.has_status("爆裂")
    engine.combat.round_end()
    assert not player.has_status("爆裂"), "己方爆裂1在回终（敌回终）到期"


# ==================== 退化（扭曲都市） ====================

def test_normal_tuihua_reduces_daowen_x():
    engine = _setup(region="扭曲都市"); _grant(engine, ["杀伐"])
    player = engine.state.player
    m = _add_monster(engine, hp=100)
    player.add_status(StatusEffect(name="退化", value=2, remaining_rounds=-1, source="测试"))
    hp_before = m.current_hp
    r = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 3, "target": m.name})
    assert r["success"], r
    # 杀伐3 退化2 → 实际 X=1 → 伤害1（X²）
    assert m.current_hp == hp_before - 1, f"退化2应使X=1(伤害1)，实减{hp_before - m.current_hp}"


def test_boundary_tuihua_zero_floor():
    engine = _setup(region="扭曲都市"); _grant(engine, ["杀伐"])
    player = engine.state.player
    m = _add_monster(engine, hp=100)
    player.add_status(StatusEffect(name="退化", value=10, remaining_rounds=-1, source="测试"))
    hp_before = m.current_hp
    r = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 3, "target": m.name})
    assert r["success"], r
    assert m.current_hp == hp_before, "退化≥X 时数值最低为0（伤害0）"
