"""
F2 全量验证：罪孽都市（洗劫/逼债/抵扣/清算/赌命/消灾/假钞）与扭曲都市（爆裂/退化）的引擎侧实装。
- 正常：按《副本/罪孽都市.md》《副本/扭曲都市.md》定义结算
- 边界：碎片不足/无遗物/无碎片/反噬致死/退化归零等
- 错误：假碎片不足/碎片不足被拒绝
"""
import os
import sys
import pytest

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
    engine.execute_action("setup_attributes", {"blood_points": 10, "speed_points": 7, "mana_points": 8})
    engine.execute_action("setup_choose_daowen", {"daowen": "杀伐"})
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


# ==================== 洗劫 ====================

def test_normal_xijie_steals_shards_on_damage():
    engine = _setup(); _grant(engine, ["洗劫"])
    player = engine.state.player
    m = _add_monster(engine, shards=20)
    r = engine.execute_action("use_daowen", {"daowen_name": "洗劫", "x": 2, "target": m.name})
    assert r["success"], r
    assert player.has_status("洗劫")  # 状态挂施法者
    # 玩家攻击怪物造成 5 伤害 → 夺取 min(20,5)=5
    res = engine.combat.resolve_attack(player, m, hit_index=0, is_must_hit=True, dodge=False)
    assert res["damage_dealt"] == 5
    assert m.shards == 15, f"应夺5碎片，实{m.shards}"
    assert engine.state.shards == 20 + 5


def test_boundary_xijie_no_shards_no_steal():
    engine = _setup(); _grant(engine, ["洗劫"])
    player = engine.state.player
    m = _add_monster(engine, shards=0)
    engine.execute_action("use_daowen", {"daowen_name": "洗劫", "x": 1, "target": m.name})
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


def test_boundary_bizhai_blood_limit_fallback():
    engine = _setup(); _grant(engine, ["逼债"])
    m = _add_monster(engine, shards=2)  # 碎片 < X=3
    engine.execute_action("use_daowen", {"daowen_name": "逼债", "x": 3, "target": m.name})
    engine.combat.round_start()
    assert m.shards == 2, "二选一：碎片不足则不动碎片"
    assert m.blood_limit == 100 - 6, f"应失2X=6血限，实{m.blood_limit}"


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


# ==================== 抵扣 ====================

def test_normal_dikou_seals_relic():
    engine = _setup(); _grant(engine, ["抵扣"])
    player = engine.state.player
    engine.state.relics = [Relic(name="回锋刀", effect="回始造成伤害"), Relic(name="避风铃", effect="闪避+格挡")]
    r = engine.execute_action("use_daowen", {"daowen_name": "抵扣", "x": 2, "target": player.name})
    assert r["success"], r
    assert engine.state.sealed_relics.get("回锋刀", 0) == 2, "应封印第一件遗物"
    # 封印期间 process_relics 不触发被封印遗物
    logs = engine.combat.process_relics("round_start")
    assert not any("回锋刀" in lg for lg in logs), f"封印遗物不应触发: {logs}"


def test_boundary_dikou_unseals_after_rounds():
    engine = _setup(); _grant(engine, ["抵扣"])
    player = engine.state.player
    engine.state.relics = [Relic(name="回锋刀", effect="回始造成伤害")]
    engine.execute_action("use_daowen", {"daowen_name": "抵扣", "x": 1, "target": player.name})
    engine.combat.round_end()
    assert engine.state.sealed_relics.get("回锋刀", 0) == 0, "1回合后应解封"
    assert "回锋刀" not in engine.state.sealed_relics


def test_boundary_dikou_no_relic_no_effect():
    engine = _setup(); _grant(engine, ["抵扣"])
    player = engine.state.player
    engine.state.relics = []  # 清空（开局自动发现会持有遗物）
    engine.execute_action("use_daowen", {"daowen_name": "抵扣", "x": 1, "target": player.name})
    assert engine.state.sealed_relics == {}, "无遗物则无效果"


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
    # 杀伐2 造成4伤害：玩家先被反噬4，怪物仍受4伤害
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
    engine = _setup(region="扭曲都市")
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
    # 杀伐3 退化2 → 实际 X=1 → 伤害2
    assert m.current_hp == hp_before - 2, f"退化2应使X=1(伤害2)，实减{hp_before - m.current_hp}"


def test_boundary_tuihua_zero_floor():
    engine = _setup(region="扭曲都市"); _grant(engine, ["杀伐"])
    player = engine.state.player
    m = _add_monster(engine, hp=100)
    player.add_status(StatusEffect(name="退化", value=10, remaining_rounds=-1, source="测试"))
    hp_before = m.current_hp
    r = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 3, "target": m.name})
    assert r["success"], r
    assert m.current_hp == hp_before, "退化≥X 时数值最低为0（伤害0）"
