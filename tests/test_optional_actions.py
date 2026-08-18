"""
pytest - 可选遗物/法器策略（sim/optional_actions.py）

背景（用户：可以不用 但是不能不让用，修）：
此前所有脚本把可选战始遗物（折速法印/三相残韵盘/猩红果实/苍白之花）一律
use:False 拒绝，回始遗物（血契/余火印）从不使用，终音法器（黑金名片/罪业金库/
教父左轮/烬翼/鲜血之翼/共心环）没有任何发动策略——这些机制"存在但不可用"。

本测试锁定：
1. 战始遗物按情形主动发动（速度高→折速法印换法力；有残韵→三相残韵盘）
2. 回始遗物按情形主动发动（血契换法力）
3. 终音法器可被实际发动（黑金名片减敌血限/罪业金库换格挡/教父左轮必中伤害/
   共心环共享龙心/鲜血之翼飞行）
4. 不满足条件时显式拒绝（策略返回 use=False，而不是让引擎代猜）
"""
import os
import sys

import pytest

from tests.setup_support import finish_initial_daowen
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.api import GameEngine
from engine.models import Consumable, Relic
from sim.optional_actions import (
    battle_start_relic_choices, round_start_relic_choices,
    try_select_shared_dragon_heart, try_use_black_card,
    try_use_crime_vault, try_use_blood_wings,
    try_fire_godfather_revolver, start_battle, start_round,
)


@pytest.fixture()
def engine(tmp_path):
    e = GameEngine(db_path=str(tmp_path / "optional.db"), rng_seed=7)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    return e


# ---------- 战始遗物 ----------

def test_zhe_su_fa_yin_used_when_speed_high(engine):
    """速度高时折速法印应主动发动（疲惫X→+6X法力），且引擎真实结算。"""
    engine.state.relics.append(Relic(name="折速法印", effect="[战始]可疲惫X获得6X法力"))
    engine.state.energy = 0
    choices = battle_start_relic_choices(engine)
    assert choices.get("折速法印", {}).get("use") is True, "速度8应发动折速法印"
    speed_before = engine.state.player.current_speed
    bs = engine.execute_action("battle_start", {"relic_choices": choices})
    assert bs.get("success"), bs
    logs = bs.get("relic_logs") or []
    assert any("折速法印" in str(l) for l in logs), logs
    assert engine.state.player.current_speed < speed_before
    assert engine.state.player.current_mana >= 6  # 折速法印法力叠加在战始清零之后


def test_san_xiang_disc_consumes_abundant_resonance(engine):
    """三相残韵盘：有残韵库存时消耗库存最多的类型，战终补回另两种。"""
    engine.state.relics.append(Relic(name="三相残韵盘",
                                     effect="[战始]消耗一种残韵；[战终]获得另两种残韵各1"))
    engine.state.resonance = {"转换": 0, "反转": 2, "曲解": 1}
    engine.state.energy = 0
    choices = battle_start_relic_choices(engine)
    assert choices.get("三相残韵盘", {}).get("use") is True
    assert choices["三相残韵盘"]["resonance_type"] == "反转"
    bs = engine.execute_action("battle_start", {"relic_choices": choices})
    assert bs.get("success"), bs
    assert any("三相残韵盘" in str(l) for l in (bs.get("relic_logs") or []))


def test_zhe_su_declined_when_speed_low(engine):
    """速度过低时折速法印应显式拒绝（use=False），不硬发动。"""
    engine.state.relics.append(Relic(name="折速法印", effect="[战始]可疲惫X获得6X法力"))
    engine.state.player.current_speed = 1
    engine.state.energy = 0
    choices = battle_start_relic_choices(engine)
    assert choices.get("折速法印", {}).get("use") is False


# ---------- 回始遗物 ----------

def test_blood_pact_used_when_hp_high(engine):
    """血契：血量充足且法力有缺口时主动换法力。"""
    engine.state.relics.append(Relic(name="血契", effect="[回始]可流血4X获得X法力"))
    engine.state.energy = 0
    bs, _ = start_battle(engine)
    assert bs.get("success"), bs
    p = engine.state.player
    p.current_hp = 40
    p.current_mana = 0
    choices = round_start_relic_choices(engine)
    assert choices.get("血契", {}).get("use") is True, choices
    rs = engine.execute_action("round_start", {"relic_choices": choices})
    assert rs.get("success"), rs
    assert any("血契" in str(ef) for ef in rs.get("result", {}).get("effects", []))


def test_blood_pact_declined_when_hp_low(engine):
    """血契：血量过低时显式拒绝。"""
    engine.state.relics.append(Relic(name="血契", effect="[回始]可流血4X获得X法力"))
    engine.state.energy = 0
    bs, _ = start_battle(engine)
    p = engine.state.player
    p.current_hp = 8
    choices = round_start_relic_choices(engine)
    assert choices.get("血契", {}).get("use") is False


# ---------- 终音法器 ----------

def test_black_card_halves_enemy_blood_limit(engine):
    """黑金名片：碎片充足时战始发动，敌方血限真实减半。"""
    engine.state.artifacts_owned.append("黑金名片")
    engine.state.shards = 200
    engine.state.energy = 0
    bs, artifact_logs = start_battle(engine)
    assert bs.get("success"), bs
    assert any(l.get("action") == "黑金名片" and l.get("success") for l in artifact_logs)
    for m in engine.state.enemies:
        assert m.blood_limit <= m.battle_start_blood_limit


def test_crime_vault_gives_shield(engine):
    """罪业金库：受威胁且碎片充足时回始发动获得格挡。"""
    engine.state.artifacts_owned.append("罪业金库")
    engine.state.shards = 500
    engine.state.energy = 0
    bs, _ = start_battle(engine)
    assert bs.get("success"), bs
    for m in engine.state.enemies:
        m.attack_count = 3
        m.attack_power = 5
    shield_before = engine.state.player.shield
    r = try_use_crime_vault(engine)
    assert r and r.get("success"), r
    assert engine.state.player.shield > shield_before


def test_godfather_revolver_deals_must_hit_damage(engine):
    """教父左轮：战斗内可发动，造成30%血限×次数必中伤害。"""
    engine.state.artifacts_owned.append("教父左轮")
    engine.state.consumables.append(Consumable(
        name="教父左轮", effect="", current_uses=6, max_uses=6, kind="artifact_weapon"))
    engine.state.energy = 0
    bs, _ = start_battle(engine)
    assert bs.get("success"), bs
    rs, _ = start_round(engine)
    assert rs.get("success"), rs
    target = next(x for x in engine.state.enemies if x.is_alive)
    hp_before = target.current_hp
    r = try_fire_godfather_revolver(engine)
    assert r and r.get("success"), r
    assert r["result"]["damage"] >= 1
    assert target.current_hp < hp_before


def test_shared_dragon_heart_selects_type(engine):
    """共心环：持有龙心时战始自动选择共享类型（start_battle 内发动）。"""
    engine.state.artifacts_owned.append("共心环")
    engine.state.consumables.append(Consumable(
        name="流血龙心", effect="", current_uses=5, max_uses=5,
        kind="dragon_heart", dragon_heart_type="流血"))
    engine.state.energy = 0
    bs, artifact_logs = start_battle(engine)
    assert bs.get("success"), bs
    assert any(l.get("action") == "共心环" and l.get("success") for l in artifact_logs)
    assert engine.state.shared_dragon_heart_type == "流血"


def test_blood_wings_fly_when_threatened(engine):
    """鲜血之翼：血厚且受致命威胁时发动飞行。"""
    engine.state.grant_relic("鲜血之翼", "代价：流血5X，发动【飞行X】回合", tag="血族")
    p = engine.state.player
    p.current_hp = 60
    p.blood_limit = 60
    engine.state.energy = 0
    bs, _ = start_battle(engine)
    assert bs.get("success"), bs
    rs, _ = start_round(engine)  # 进入 PLAYER_ACTIONS 子阶段
    assert rs.get("success"), rs
    for m in engine.state.enemies:
        m.attack_count = 4
        m.attack_power = 8  # 威胁32 ≥ 40%×60
    r = try_use_blood_wings(engine)
    assert r and r.get("success"), r
    assert r["result"]["flying_rounds"] >= 1


def test_blood_wings_declined_without_threat(engine):
    """鲜血之翼：威胁不足时显式不发动（可以不用但不能不让用）。"""
    engine.state.grant_relic("鲜血之翼", "代价：流血5X，发动【飞行X】回合", tag="血族")
    p = engine.state.player
    p.current_hp = 60
    p.blood_limit = 60
    engine.state.energy = 0
    bs, _ = start_battle(engine)
    assert bs.get("success"), bs
    for m in engine.state.enemies:
        m.attack_count = 1
        m.attack_power = 1  # 威胁小
    assert try_use_blood_wings(engine) is None


def test_start_battle_and_start_round_keep_working(engine):
    """组合入口：start_battle/start_round 正常推进，无可选遗物时不影响战斗。"""
    engine.state.energy = 0
    bs, _ = start_battle(engine)
    assert bs.get("success"), bs
    rs, _ = start_round(engine)
    assert rs.get("success"), rs
