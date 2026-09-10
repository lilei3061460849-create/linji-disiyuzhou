"""法力一池制（DM裁定 2026-09-09）：一场战斗一池法力。

裁定背景：原口径是「[回始]获得等同[法限]的法力、[敌回终]清空」，等于每回合把蓝条
重置一次；DM 改为一池制——[战始]直接给满，整场战斗按剩余量支配，[战终]复原
（与当前速度同口径）。连带裁定：【勾魂】原「持续X回合无法获得法力」失去作用对象，
改为**目标消耗法力翻倍**。

本文件钉住整条生命周期，避免旧口径以任何形式回流。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine  # noqa: E402
from engine.models import Entity, Relic, StatusEffect  # noqa: E402
from tests.setup_support import begin_battle, begin_round, finish_initial_daowen  # noqa: E402


def _engine(suffix, **points):
    os.makedirs("/tmp/linji_tests", exist_ok=True)
    e = GameEngine(db_path=f"/tmp/linji_tests/test_mana_pool_{suffix}.db", rng_seed=7,
                   sealed_candidate_path=f"/tmp/linji_tests/test_mana_pool_s_{suffix}.json")
    r = e.execute_action("setup_attributes", {"name": "白某", **points})
    assert r.get("success"), r
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "曲解"})
    e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    assert e.state.phase == "pre_battle", e.state.phase   # 战始只接受局外阶段
    return e


def _finish_round(e):
    e.state.combat_subphase = "await_round_end"
    return e.execute_action("round_end", {})


def _clear_enemies(e):
    """战终结算要求没有存活敌人。"""
    for en in list(e.state.enemies):
        en.current_hp = 0
        en.is_alive = False          # is_alive 是独立字段，不是 current_hp 的派生属性
        assert not en.is_alive


# ==================== 1. [战始]给满一池 ====================

def test_battle_start_grants_full_pool():
    """正常路径：战始当前法力 = 法限（不再是 0）。"""
    e = _engine("start", blood_points=7, speed_points=8, mana_points=10)
    p = e.state.player
    assert p.mana_limit == 5, p.mana_limit      # DM裁定 2026-09-10：2属性点=1法限
    r = begin_battle(e)
    assert r["success"], r
    assert p.current_mana == p.mana_limit, p.current_mana


def test_relic_gain_stacks_over_full_pool():
    """边界：遗物获得叠在满池上，允许超过[法限]（反向禁区：不得加 clamp）。"""
    e = _engine("relic", blood_points=7, speed_points=8, mana_points=10)
    e.state.relics.append(Relic(name="折速法印", effect="[战始]可疲惫X获得6X法力"))
    p = e.state.player
    begin_battle(e, relic_choices={"折速法印": {"use": True, "x": 4}})
    assert p.current_mana == p.mana_limit + 24, p.current_mana
    assert p.current_mana > p.mana_limit, "超过法限是合法面板"


# ==================== 2. [回始]不回填 / [回终]不清空 ====================

def test_round_start_does_not_refill():
    """正常路径：花掉的法力不会在回始补回来，也不再产出 mana_refill 条目。"""
    e = _engine("refill", blood_points=7, speed_points=8, mana_points=10)
    p = e.state.player
    begin_battle(e)
    assert p.spend_mana(p.mana_limit - 1) is True
    assert p.current_mana == 1
    _finish_round(e)
    r = begin_round(e)
    assert r["success"], r
    assert [x for x in r["result"].get("effects", [])
            if x.get("type") == "mana_refill"] == [], "一池制：不得有回填条目"
    assert p.current_mana == 1, f"回始不得回填，实{p.current_mana}"


def test_round_end_keeps_leftover_mana():
    """正常路径：回终与敌回终都不清空，剩余法力跨回合保留。"""
    e = _engine("keep", blood_points=7, speed_points=8, mana_points=10)
    p = e.state.player
    begin_battle(e)
    assert p.spend_mana(p.mana_limit - 1) is True
    r = _finish_round(e)
    assert r["success"], r
    assert [x for x in r["result"].get("effects", [])
            if x.get("type") == "mana_clear"] == [], "一池制：不得有清空条目"
    assert p.current_mana == 1, f"回终不得清空，实{p.current_mana}"
    begin_round(e)
    assert p.current_mana == 1, f"下一回始仍是1，实{p.current_mana}"


def test_pool_never_refills_across_many_rounds():
    """边界：连走三回合，池子只减不增（除非有遗物/道纹显式给）。"""
    e = _engine("many", blood_points=1, speed_points=8, mana_points=16)
    p = e.state.player
    begin_battle(e)
    pool = p.mana_limit
    assert pool == 8, pool
    _finish_round(e)
    for i in range(3):
        assert p.spend_mana(2) is True, f"第{i + 1}回合应还能付 2"
        _finish_round(e)
        begin_round(e)
        assert p.current_mana == pool - 2 * (i + 1), (i, p.current_mana)
    assert p.spend_mana(3) is False, "只剩 2，付不起 3"


# ==================== 3. [战终]复原（与当前速度同口径） ====================

def test_battle_end_restores_mana_like_speed():
    """正常路径：战终把当前法力与当前速度一起复原到上限。"""
    e = _engine("end", blood_points=7, speed_points=8, mana_points=10)
    p = e.state.player
    begin_battle(e)
    assert p.spend_mana(p.mana_limit - 1) is True
    p.current_speed = 1
    assert (p.current_mana, p.current_speed) == (1, 1)
    _clear_enemies(e)
    r = e.execute_action("battle_end", {})
    assert r.get("success"), r
    assert p.current_mana == p.mana_limit, f"战终应复原法力，实{p.current_mana}"
    assert p.current_speed == p.speed_limit, f"战终应复原速度，实{p.current_speed}"


def test_battle_end_does_not_grant_mana_to_non_reincarnator():
    """错误输入/对照：朋友不是轮回者，战终不给它复原法力。"""
    e = _engine("friend", blood_points=7, speed_points=8, mana_points=10)
    friend = Entity(name="旁观朋友", entity_type="朋友", blood_limit=20, current_hp=20,
                    mana_limit=10, current_mana=2)
    e.state.friends.append(friend)
    begin_battle(e)
    _clear_enemies(e)
    r = e.execute_action("battle_end", {})
    assert r.get("success"), r
    assert friend.current_mana == 2, f"非轮回者不享受战终复原，实{friend.current_mana}"


# ==================== 4. 连带裁定：勾魂 = 消耗翻倍 ====================

def test_gouhun_doubles_cost_until_it_expires():
    """正常路径：勾魂期间消耗翻倍；持续X 走完后恢复原值。"""
    e = _engine("gouhun", blood_points=1, speed_points=0, mana_points=24)
    p = e.state.player
    begin_battle(e)
    assert p.mana_limit == 12, p.mana_limit
    p.add_status(StatusEffect(name="勾魂", value=1, remaining_rounds=2, source="寄骨蝇"))
    assert p.spend_mana(3) is True
    assert p.current_mana == 6, f"3 点应翻倍扣 6，实剩 {p.current_mana}"
    assert p.spend_mana(4) is False, "翻倍后需 8，只剩 6 → 付不起"
    assert p.current_mana == 6, "付不起时不得扣费"

    _finish_round(e)
    _finish_round(e)
    assert not p.has_status("勾魂"), "持续X=2 走完应自然到期"
    assert p.spend_mana(3) is True
    assert p.current_mana == 3, f"到期后按原值扣，实剩 {p.current_mana}"


def test_gouhun_does_not_touch_mana_on_apply():
    """边界：勾魂只改消耗倍率，挂上/到期都不动当前法力。"""
    e = _engine("gouhun_apply", blood_points=7, speed_points=8, mana_points=10)
    p = e.state.player
    foe = Entity(name="寄骨蝇", entity_type="怪物", blood_limit=40, current_hp=40,
                 attack_count=1, attack_power=1)
    e.state.enemies.append(foe)
    begin_battle(e)
    from engine.daowen import DaoWenEngine
    calc = DaoWenEngine.resolve("勾魂", 3, target=p, caster=foe)
    assert calc.get("mana_cost_multiplier") == 2
    res = e.combat.apply_daowen_effect("勾魂", calc, foe, p)
    entry = next(x for x in res["effects"] if x.get("type") == "gouhun")
    assert entry["mana_cost_multiplier"] == 2 and entry["duration"] == 3, entry
    assert p.current_mana == p.mana_limit, "挂状态不得动当前法力"
