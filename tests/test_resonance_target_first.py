"""
pytest - 残韵的**目标优先**定位（2026-09-18 用户裁定：自由选目标，再选它身上的道纹）

旧口径 `engine/api.py::_find_resonance_holder` 是「玩家优先且无视 target_ref」：只要
施法者自己持有同名道纹，残韵就一定打在自己身上。在回溯边（转化道纹 --同种残韵-->
原始怪物道纹，见 tests/test_resonance_backtrack_original.py）打通、玩家可以**永久持有**
原始怪物道纹之后，这条捷径会让收割流当场空转：场上另有一只怪持有同一条【减速】时，
玩家想收割它却永远改写成自己的那份（减速→急速→减速 来回烧残韵）。

现行口径：先按 `target_ref` 定位持有者；未给 `target_ref` 才回落
「施法者本人 → 场上唯一持有者 → 多名持有者报错要求指定」。

覆盖：正常路径 / 边界条件 / 错误输入
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity


def _engine(suffix: str) -> GameEngine:
    engine = GameEngine(db_path=f"/tmp/linji_tests/res_target_{suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    engine.execute_action("choose_discovered_relic",
                          {"relic_name": setup["result"]["relic_choices"][0]})
    engine.state.energy = 0
    return engine


def _give(entity: Entity, name: str, x: int = 1) -> None:
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""),
        x_value=x,
    )


def _monster(name: str, *daowen) -> Entity:
    m = Entity(name=name, entity_type="怪物", blood_limit=80, current_hp=80,
               attack_count=1, attack_power=5, mana_limit=11, current_mana=11)
    for dw, x in daowen:
        _give(m, dw, x=x)
    return m


def _battle_with(engine: GameEngine, *monsters: Entity) -> None:
    """battle_start 会按副本怪物池重建 enemies，自定义怪物必须在战始之后塞进去。"""
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    for m in monsters:
        engine.state.enemies.append(m)
    engine.execute_action("round_start", {})


# ---------- 正常路径 ----------

def test_target_ref_overrides_caster_holding_the_same_daowen():
    """正常：玩家与怪物同持【减速】时，给了 target_ref 就打在怪物身上，自己那份不动。"""
    e = _engine("override")
    e.state.resonance["转换"] = 1
    player = e.state.player
    _give(player, "减速", 2)
    foe = _monster("狙击手", ("减速", 2))
    _battle_with(e, foe)

    r = e.execute_action("use_resonance", {
        "source_daowen": "减速", "resonance_type": "转换", "target_ref": "enemy:0"})
    assert r["success"], r
    # 怪物那份被就地改写
    assert "减速" not in foe.dao_wen and "急速" in foe.dao_wen
    assert foe._had_monster_daowen is True, "怪物永久失去原始道纹须被救赎判定记住"
    # 玩家自己那份原封不动，另按 holder≠actor 规则获得转化道纹
    assert "减速" in player.dao_wen, "施法者自持的同名道纹不得被误改写"
    assert "急速" in player.dao_wen
    assert e.state.resonance["转换"] == 0, "生效即消耗1枚"


def test_harvest_flow_survives_own_permanent_original():
    """正常（回归钉死）：走完两步残韵永久持有【减速】后，仍能收割另一只持【减速】的怪。

    旧口径下这一步必然打在自己身上：玩家持有的【减速】被改成【急速】，怪物毫发无伤，
    残韵白烧——收割流（残韵是全局最稀缺资源）被自己的持有卡死。
    """
    e = _engine("harvest")
    e.state.resonance["转换"] = 3
    player = e.state.player
    first = _monster("哀嚎者", ("减速", 2))
    _battle_with(e, first)

    # 两步残韵：怪物的原始道纹 → 转化道纹（施法者同获）→ 同种残韵回溯成自己永久持有
    assert e.execute_action("use_resonance", {
        "source_daowen": "减速", "resonance_type": "转换", "target_ref": "enemy:0"})["success"]
    assert e.execute_action("use_resonance", {
        "source_daowen": "急速", "resonance_type": "转换"})["success"]
    assert "减速" in player.dao_wen and "急速" not in player.dao_wen
    left = e.state.resonance["转换"]
    assert left == 1

    # 第二只怪同样持有【减速】：显式指定目标即可收割，不再被玩家自持的那份截胡
    second = _monster("腐疫鼠", ("减速", 1))
    e.state.enemies.append(second)
    r = e.execute_action("use_resonance", {
        "source_daowen": "减速", "resonance_type": "转换", "target_ref": "enemy:1"})
    assert r["success"], r
    assert "减速" not in second.dao_wen and "急速" in second.dao_wen
    assert "减速" in player.dao_wen, "自己永久持有的原始道纹必须还在"
    assert e.state.resonance["转换"] == left - 1


# ---------- 边界条件 ----------

def test_without_target_ref_falls_back_to_caster_then_unique_holder():
    """边界：不给 target_ref 时保留旧回落链——先施法者本人，再场上唯一持有者。"""
    # (a) 只有施法者本人持有 → 打在自己身上（回溯边正是靠这条回落才不必自指）
    e = _engine("fallback_self")
    e.state.resonance["转换"] = 1
    player = e.state.player
    _give(player, "急速", 1)
    _battle_with(e, _monster("空手怪"))
    r = e.execute_action("use_resonance", {"source_daowen": "急速", "resonance_type": "转换"})
    assert r["success"], r
    assert "减速" in player.dao_wen and "急速" not in player.dao_wen

    # (b) 施法者不持有、场上恰好一名持有者 → 打在那名持有者身上
    e2 = _engine("fallback_unique")
    e2.state.resonance["转换"] = 1
    foe = _monster("狙击手", ("减速", 1))
    _battle_with(e2, foe)
    r2 = e2.execute_action("use_resonance", {"source_daowen": "减速", "resonance_type": "转换"})
    assert r2["success"], r2
    assert "急速" in foe.dao_wen and "急速" in e2.state.player.dao_wen


def test_ambiguous_holders_require_target_ref_and_then_obey_it():
    """边界：施法者不持有、多名角色持有同一条道纹 → 报错要求指定；指定后按指定目标结算。"""
    e = _engine("ambiguous")
    e.state.resonance["转换"] = 1
    a = _monster("哀嚎者", ("减速", 1))
    b = _monster("腐疫鼠", ("减速", 1))
    _battle_with(e, a, b)

    ambiguous = e.execute_action("use_resonance", {"source_daowen": "减速", "resonance_type": "转换"})
    assert ambiguous["success"] is False
    assert "多名角色持有" in ambiguous["error"] and "target_ref" in ambiguous["error"]
    assert e.state.resonance["转换"] == 1, "未生效不得消耗残韵"
    assert "减速" in a.dao_wen and "减速" in b.dao_wen

    picked = e.execute_action("use_resonance", {
        "source_daowen": "减速", "resonance_type": "转换", "target_ref": "enemy:1"})
    assert picked["success"], picked
    assert "减速" in a.dao_wen, "未被指定的持有者不受影响"
    assert "急速" in b.dao_wen and "减速" not in b.dao_wen
    assert e.state.resonance["转换"] == 0


# ---------- 错误输入 ----------

def test_target_ref_without_the_source_daowen_is_refused_and_free():
    """错误输入：target_ref 指向未持有该道纹的角色 → 拒绝、不消耗、不改动任何面板。"""
    e = _engine("wrong_holder")
    e.state.resonance["转换"] = 1
    player = e.state.player
    _give(player, "减速", 1)
    bare = _monster("空手怪")
    _battle_with(e, bare)

    r = e.execute_action("use_resonance", {
        "source_daowen": "减速", "resonance_type": "转换", "target_ref": "enemy:0"})
    assert r["success"] is False
    assert r["error"] == "空手怪未持有道纹: 减速"
    assert e.state.resonance["转换"] == 1, "被拒绝的残韵不得消耗"
    assert "减速" in player.dao_wen and "减速" not in bare.dao_wen


def test_unknown_target_ref_is_refused():
    """错误输入：target_ref 不在场上引用表里 → 拒绝并点名找不到的引用。"""
    e = _engine("bad_ref")
    e.state.resonance["转换"] = 1
    _battle_with(e, _monster("狙击手", ("减速", 1)))

    r = e.execute_action("use_resonance", {
        "source_daowen": "减速", "resonance_type": "转换", "target_ref": "enemy:7"})
    assert r["success"] is False
    assert r["error"] == "找不到target_ref: enemy:7"
    assert e.state.resonance["转换"] == 1


def test_nobody_holds_the_source_daowen():
    """错误输入：全场无人持有该道纹 → 拒绝（target_ref 有无都一样）。"""
    e = _engine("nobody")
    e.state.resonance["转换"] = 1
    _battle_with(e, _monster("空手怪"))

    r = e.execute_action("use_resonance", {"source_daowen": "减速", "resonance_type": "转换"})
    assert r["success"] is False
    assert r["error"] == "场上无人持有道纹: 减速"
    assert e.state.resonance["转换"] == 1
