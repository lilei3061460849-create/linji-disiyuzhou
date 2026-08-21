"""怪物道纹递增机制（README 怪物准则9，DM裁定 2026-08-18）

- 怪物可跨回合重复发动同一道纹（每回合每道纹至多一次；冷却类仍受 can_use 约束）
- 每次实际发动后该道纹 X 累加 +2×副本阶级（一阶+2、二阶+4）
- 无法支付代价 / 被控跳过的回合不计；玩家道纹不受影响
覆盖：正常路径 / 边界条件 / 错误输入
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine  # noqa: E402
from engine.models import DaoWen, DaoWenInstance, Entity  # noqa: E402

DB = "/tmp/linji_tests"
os.makedirs(DB, exist_ok=True)


def _engine(region="罪孽都市", seed=5):
    e = GameEngine(db_path=f"{DB}/escalation.db", rng_seed=seed)
    st = e.state
    st.current_region = region
    st.phase = "in_combat"
    st.current_round = 2  # 跳过白板首回合
    st.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60,
                       mana_limit=20, current_mana=20, speed_limit=8, current_speed=8)
    m = Entity(name="打手", entity_type="怪物", blood_limit=120, current_hp=120,
               attack_count=1, attack_power=4)
    m.dao_wen["强化"] = DaoWenInstance(DaoWen("强化", "", "异变", "5X", ""), x_value=2)
    m.dao_wen["庇护"] = DaoWenInstance(DaoWen("庇护", "", "消耗", "X", ""), x_value=3)
    st.enemies = [m]
    e.combat.reset_monster_activation()
    return e, m


def _cast(e, name, target_ref=None):
    prepared = e.combat.prepare_monster_phase()
    actor = prepared["actors"][0]
    opts = {o["name"]: o for o in actor["daowen_options"]}
    if name not in opts:
        return None
    dao = {"name": name, "dodge": False, "blood_shadow": False, "trigger_spell_choices": {}}
    if opts[name]["requires_target"]:
        dao["target_ref"] = target_ref or opts[name]["target_options"][0]["ref"]
    target_option = next(o for o in actor["attack_target_options"] if o["ref"] == "player:0")
    spell_choices = {timing: {sp["spell_name"]: {"use": False}
                              for sp in target_option.get("spell_options", {}).get(timing, [])}
                     for timing in ("before", "after")}
    choice = {"actor_ref": actor["actor_ref"], "daowen": dao,
              "attack_actions": [{"hits": [{"target_ref": "player:0", "dodge": False,
                                            "blood_shadow": False,
                                            "spell_choices": spell_choices}
                                           for _ in range(actor["base_hits_per_attack"])]}
                                 for _ in range(actor["base_attack_actions"])]}
    return e.combat.resolve_monster_phase([choice], prepared)


# ---------- 正常路径 ----------

def test_escalation_tier1_plus2_per_cast():
    """一阶：庇护3发动后变5，再发动变7（跨回合可重复）"""
    e, m = _engine("罪孽都市")
    _cast(e, "庇护")
    assert m.dao_wen["庇护"].x_value == 5, "一阶发动一次后X应+2"
    e.state.current_round += 1
    m.actions_used_this_round = 0
    _cast(e, "庇护")
    assert m.dao_wen["庇护"].x_value == 7, "跨回合重复发动应再+2"


def test_escalation_tier2_plus4():
    """二阶（乱葬岗）：+4/次"""
    e, m = _engine("乱葬岗")
    _cast(e, "庇护")
    assert m.dao_wen["庇护"].x_value == 3 + 4


def test_original_daowen_escalates_and_pays_scaled_mutation():
    """原始道纹（强化）递增后，下次发动按新X支付异变5X。

    异变支付口径（README 怪物准则9，2026-08-21 统一）：原始怪物道纹
    每次【实际发动】都支付异变5X，且按该次发动时递增后的 X 计算——
    首次按原X、重复发动按递增后X；"持续期间不再重复支付"仅指效果
    持续而未被再次发动的回合。
    """
    e, m = _engine("罪孽都市")
    _cast(e, "强化")
    assert m.dao_wen["强化"].x_value == 4
    assert m.mutation_count == 10  # 本次按 X=2 支付 5X
    e.state.current_round += 1
    m.actions_used_this_round = 0
    _cast(e, "强化")
    assert m.mutation_count == 10 + 20, "第二次应按 X=4 支付异变20"


# ---------- 边界条件 ----------

def test_same_round_repeat_rejected():
    """同一回合内同道纹第二次发动必须被拒绝"""
    e, m = _engine("罪孽都市")
    _cast(e, "庇护")
    m.actions_used_this_round = 0  # 只重置出手，不换回合
    prepared = e.combat.prepare_monster_phase()
    names = {o["name"] for o in prepared["actors"][0]["daowen_options"]}
    assert "庇护" not in names, "同回合不得重复列出已发动道纹"
    with pytest.raises(ValueError):
        dao = {"name": "庇护", "dodge": False, "blood_shadow": False,
               "trigger_spell_choices": {}, "target_ref": "enemy:0"}
        choice = {"actor_ref": "enemy:0", "daowen": dao,
                  "attack_actions": [{"hits": [{"target_ref": "player:0",
                                                "dodge": False, "blood_shadow": False,
                                                "spell_choices": {}}]}]}
        e.combat.resolve_monster_phase([choice], prepared)


def test_skipped_round_no_escalation():
    """被控/未行动的回合不递增"""
    e, m = _engine("罪孽都市")
    before = m.dao_wen["庇护"].x_value
    e.state.current_round += 1  # 空过一回合，无发动
    prepared = e.combat.prepare_monster_phase()
    assert m.dao_wen["庇护"].x_value == before, "未实际发动不得递增"


def test_unknown_region_defaults_tier1():
    """未登记副本按一阶+2兜底"""
    e, m = _engine("未知副本")
    _cast(e, "庇护")
    assert m.dao_wen["庇护"].x_value == 5


# ---------- 错误输入 / 作用域 ----------

def test_player_daowen_never_escalates():
    """玩家道纹发动不受递增机制影响"""
    e, m = _engine("罪孽都市")
    p = e.state.player
    p.dao_wen["杀伐"] = DaoWenInstance(DaoWen("杀伐", "", "消耗", "X", ""))
    before = p.dao_wen["杀伐"].x_value
    r = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 2, "target_ref": "enemy:0"})
    assert r["success"]
    assert p.dao_wen["杀伐"].x_value == before, "递增只作用于怪物"
