"""选区决策与残韵打分的构筑协同（2026-09-13）。

背景：两处决策此前都只看单边信息，漏掉了"对我的构筑有没有用"这一半。

1) 选区硬编码"罪孽都市"，不读遗物——实测开局拿到【回锋刀】/【避风铃】
   （都吃"失去速度"）29 个样本，选扭曲都市 0 次，而【搏命】(代价:疲惫X)
   正在扭曲都市。遗物在选区之前就已确定，时序上完全可判。

2) 残韵打分只算"敌人失去了什么"，不算"我得到了什么"。但
   api._action_use_resonance 明确会把转化结果白送给施法者
   （_grant_transformed_daowen），所以那是半个算式。残韵极稀缺
   （开局仅1个、之后只靠事件补）且是副本专属道纹的唯一入口。

两处实现都不写死副本名/道纹名：遗物按效果文本关心的资源归类，副本按其
专属道纹实际 cost_type 归类，求交集即协同分。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.ai_player import pick_region_by_synergy


# ---------- 选区协同 ----------

def test_speed_relics_steer_to_twisted_city():
    """正常路径：吃'失速'的遗物应把 AI 引向有【搏命】(疲惫X)的扭曲都市。"""
    for name, effect in (
        ("回锋刀", "每失去1点速度后对[目标]造成3伤害；[回始]对[目标]造成3×([速限]-当前速度)伤害"),
        ("避风铃", "每次闪避后获得3格挡；当前速度归零时获得15格挡"),
    ):
        region, why = pick_region_by_synergy({"relics": [{"name": name, "effect": effect}]})
        assert region == "扭曲都市", f"{name} 应导向扭曲都市，实际{region}（{why}）"


def test_shard_relic_steers_to_sin_city():
    """正常路径：同一套通用规则下，吃'碎片'的遗物导向罪孽都市。

    证明打分是数据驱动的，不是给扭曲都市开的后门。
    """
    region, why = pick_region_by_synergy({"relics": [
        {"name": "买路财", "effect": "战斗中可失去等同于怪物20%[血限]的[碎片]安全撤退"},
    ]})
    assert region == "罪孽都市", why


def test_no_synergy_falls_back_to_default():
    """边界：无协同信息时回落默认，不做没依据的随机漂移。"""
    for relics in ([], [{"name": "忘忧香", "effect": "局外行动你可以选择忘忧"}]):
        region, _ = pick_region_by_synergy({"relics": relics})
        assert region == "罪孽都市"


# ---------- 残韵收益项 ----------

def _ai_in_region(region):
    from engine.api import GameEngine
    from engine.ai_tactics import TacticalAI
    from tests.setup_support import finish_initial_daowen
    engine = GameEngine(db_path=f"data/test_syn_{region}.db", rng_seed=1)
    engine.execute_action("setup_attributes",
                          {"blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": region})
    engine.execute_action("choose_discovered_relic",
                          {"relic_name": setup["result"]["relic_choices"][0]})
    return TacticalAI(engine)


def test_region_exclusive_daowen_scores_highest():
    """正常路径：副本专属道纹（残韵是唯一入口）收益权重必须最高。"""
    ai = _ai_in_region("扭曲都市")
    exclusive = ai._resonance_gain_bonus("搏命")     # 扭曲都市专属
    ordinary = ai._resonance_gain_bonus("蒙蔽")      # 非专属
    assert exclusive > ordinary, (exclusive, ordinary)


def test_already_owned_daowen_scores_zero():
    """边界：已持有的道纹重复获得无增量，收益必须为0。"""
    ai = _ai_in_region("扭曲都市")
    owned = next(iter(ai.player.dao_wen))
    assert ai._resonance_gain_bonus(owned) == 0.0


def test_unknown_or_empty_target_is_safe():
    """错误输入：目标为空或不存在时不得抛异常。"""
    ai = _ai_in_region("扭曲都市")
    assert ai._resonance_gain_bonus(None) == 0.0
    assert ai._resonance_gain_bonus("") == 0.0
    assert ai._resonance_gain_bonus("根本不存在的道纹") >= 0.0


def test_gain_bonus_actually_reaches_candidate_score():
    """集成：收益项必须真的进入 _resonance_candidates 的打分，而不是算完不用。"""
    from unittest.mock import patch
    from engine.models import Entity, DaoWen, DaoWenInstance
    ai = _ai_in_region("扭曲都市")
    # 放一只带【畸变】的怪：畸变 --(曲解)--> 超频，是真实存在的转化路径
    foe = Entity(name="靶怪", entity_type="怪物", blood_limit=200, current_hp=200,
                 attack_count=2, attack_power=5)
    foe.dao_wen["畸变"] = DaoWenInstance(DaoWen(
        name="畸变", formula="", cost_type="冷却", cost_formula="X", effect_formula=""))
    ai.engine.state.enemies.append(foe)
    ai.engine.state.phase = "in_combat"

    def score_map():
        ai.engine.state.resonance = {"转换": 1, "反转": 1, "曲解": 1}
        return {c[1]["label"]: c[0] for c in ai._resonance_candidates()}

    with patch.object(type(ai), "_resonance_gain_bonus", return_value=0.0):
        base = score_map()
    with patch.object(type(ai), "_resonance_gain_bonus", return_value=99.0):
        boosted = score_map()
    assert base, "应至少枚举出一个残韵候选（畸变--曲解-->超频）"
    shared = set(base) & set(boosted)
    assert shared, "两次枚举应有共同候选"
    for label in shared:
        assert boosted[label] == pytest.approx(base[label] + 99.0), label
