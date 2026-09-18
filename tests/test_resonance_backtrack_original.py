"""
pytest - 转化道纹→原始怪物道纹的**同种残韵回溯**（2026-09-18 用户裁定A）

裁定B「人类只能从怪物身上获得原始怪物道纹」的落地路径是两步残韵：

  第一步：对持有原始道纹的怪物发动残韵 → 该怪物的原始道纹永久变为转化道纹，
          施法者同时永久获得该转化道纹（怪物 `_had_monster_daowen` 被标记，救赎判定不受影响）；
  第二步：施法者对**自身持有的**该转化道纹发动同种残韵 → 它永久变回原始怪物道纹。

改动前 `CLOSED_LOOPS` 里 19 条转化道纹全是末端节点（0 条出边），整张表没有任何一条边
指向原始怪物道纹，且 `_grant_transformed_daowen` 对 `ORIGINAL_MONSTER_DAOWEN` 一律拒发，
所以第二步在规则上根本不存在。本文件把两步路钉住，同时钉住裁定B 不被绕过
（转化道纹自身依旧不可【学习】）。

覆盖：正常路径 / 边界条件 / 错误输入
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.daowen import ResonanceEngine as R
from engine.gamedata import MONSTER_TRANSFORM_DAOWEN, ORIGINAL_MONSTER_DAOWEN
from engine.models import DaoWen, DaoWenInstance, Entity

RESONANCE_TYPES = ("转换", "反转", "曲解")


def _forward_edges():
    """原始怪物道纹 → 转化道纹 的 19 条正向边。"""
    return [(s, t, d) for edges in R.CLOSED_LOOPS.values()
            for s, t, d in edges if s in ORIGINAL_MONSTER_DAOWEN]


def _engine(suffix: str) -> GameEngine:
    engine = GameEngine(db_path=f"/tmp/linji_tests/backtrack_{suffix}.db", rng_seed=1)
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


def _give_daowen(entity: Entity, name: str, x: int = 1) -> None:
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""),
        x_value=x,
    )


def _battle_with(engine: GameEngine, monster: Entity) -> None:
    """battle_start 会按副本怪物池重建 enemies，所以自定义怪物必须**在战始之后**塞进去。"""
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    engine.state.enemies.append(monster)
    engine.execute_action("round_start", {})


def _monster(name: str, *daowen) -> Entity:
    m = Entity(name=name, entity_type="怪物", blood_limit=80, current_hp=80,
               attack_count=1, attack_power=5, mana_limit=11, current_mana=11)
    for dw, x in daowen:
        _give_daowen(m, dw, x=x)
    return m


def _learn(engine, name):
    engine.state.energy = 3
    return engine.execute_action("pre_battle_action",
                                 {"sub_action": "学习", "sub": "daowen", "name": name})


# ---------- 正常路径 ----------

def test_every_forward_edge_has_same_type_mirror():
    """正常路径：19 条正向边逐条有同种残韵的回溯边（原始↔转化双向）。"""
    fwd = _forward_edges()
    assert len(fwd) == 19, f"正向边应为19条，实际{len(fwd)}"
    for src, rtype, dst in fwd:
        assert R.find_transformation(dst, rtype) == src, \
            f"缺回溯边：{dst} --（{rtype}）--> {src}"


def test_backtrack_roundtrip_returns_the_original():
    """正常路径：原始 →(t) 转化 →(t) 原始，往返落回同一条原始怪物道纹。"""
    for src, rtype, dst in _forward_edges():
        assert R.find_transformation(R.find_transformation(src, rtype), rtype) == src


def test_two_step_resonance_gives_player_the_original():
    """正常路径（裁定B 的唯一取得路径）：两步同种残韵，玩家从怪物身上拿到原始怪物道纹。"""
    e = _engine("two_step")
    e.state.resonance["转换"] = 2
    monster = _monster("哀嚎者", ("减速", 2))
    _battle_with(e, monster)
    player = e.state.player

    # 第一步：打在怪物的【减速】上
    r1 = e.execute_action("use_resonance",
                          {"source_daowen": "减速", "resonance_type": "转换"})
    assert r1["success"], r1
    assert "减速" not in monster.dao_wen and "急速" in monster.dao_wen
    assert "急速" in player.dao_wen
    assert monster._had_monster_daowen is True, "怪物永久失去原始道纹须被救赎判定记住"
    assert e.state.resonance["转换"] == 1

    # 第二步：打在自己持有的【急速】上（holder is actor）
    r2 = e.execute_action("use_resonance",
                          {"source_daowen": "急速", "resonance_type": "转换"})
    assert r2["success"], r2
    assert "减速" in player.dao_wen, "回溯后玩家应持有原始怪物道纹【减速】"
    assert "急速" not in player.dao_wen, "转化道纹已就地变回原始道纹，不应残留"
    assert e.state.resonance["转换"] == 0


def test_holder_is_caster_does_not_duplicate_the_daowen():
    """边界：第二步 holder is actor，就地改写 + 同名不重复，面板上只有一份。"""
    e = _engine("no_dup")
    e.state.resonance["转换"] = 2
    _battle_with(e, _monster("狙击手", ("减速", 1)))
    player = e.state.player
    for src in ("减速", "急速"):
        assert e.execute_action("use_resonance",
                                {"source_daowen": src, "resonance_type": "转换"})["success"]
    assert list(player.dao_wen).count("减速") == 1
    assert len(player.dao_wen) == len(set(player.dao_wen))


def test_backtrack_off_another_monster_grants_the_original():
    """正常路径：转化道纹由别的怪物天生承载时，回溯边同样把原始道纹发给施法者。

    这一条走的是 `_grant_transformed_daowen`（holder ≠ actor），即裁定A 撤掉
    「dest 是原始怪物道纹就拒发」旧闸后新打通的路径。
    """
    e = _engine("grant")
    e.state.resonance["转换"] = 1
    monster = _monster("腐疫鼠", ("急速", 2))   # 索引里急速的天生承载者之一
    _battle_with(e, monster)
    player = e.state.player

    r = e.execute_action("use_resonance",
                         {"source_daowen": "急速", "resonance_type": "转换"})
    assert r["success"], r
    assert "急速" not in monster.dao_wen and "减速" in monster.dao_wen
    assert "减速" in player.dao_wen, "施法者应同时永久获得回溯出的原始怪物道纹"
    assert e.state.resonance["转换"] == 0


# ---------- 边界条件 ----------

def test_backtrack_only_works_with_the_same_resonance_type():
    """边界：回溯严格同种——转化道纹对其它两种残韵仍然无路可走。"""
    for src, rtype, dst in _forward_edges():
        for other in RESONANCE_TYPES:
            if other == rtype:
                continue
            assert R.find_transformation(dst, other) is None, \
                f"{dst} 不该有（{other}）出边（正向边是（{rtype}））"


def test_transformed_daowen_have_exactly_one_out_edge():
    """边界：19 条转化道纹各自只有 1 条出边（就是回溯边），没有额外分叉。"""
    for dst in MONSTER_TRANSFORM_DAOWEN:
        outs = [(t, d) for edges in R.CLOSED_LOOPS.values()
                for s, t, d in edges if s == dst]
        assert len(outs) == 1, f"{dst} 出边应为1条，实际{outs}"
        assert outs[0][1] in ORIGINAL_MONSTER_DAOWEN


def test_backtrack_never_creates_a_new_original_edge_target():
    """边界：回溯边的目标必须是七条原始怪物道纹之一，不引入第八种。"""
    targets = {d for edges in R.CLOSED_LOOPS.values() for s, t, d in edges
               if d in ORIGINAL_MONSTER_DAOWEN}
    assert targets <= set(ORIGINAL_MONSTER_DAOWEN)
    assert len(ORIGINAL_MONSTER_DAOWEN) == 7


# ---------- 错误输入 ----------

def test_second_step_fails_without_stock_and_changes_nothing():
    """错误输入：第二步没有同种残韵 → 失败、不消耗、不改写面板（规则1：未生效不消耗）。"""
    e = _engine("no_stock")
    e.state.resonance["转换"] = 1          # 只够第一步
    _battle_with(e, _monster("杀手", ("减速", 1)))
    player = e.state.player
    assert e.execute_action("use_resonance",
                            {"source_daowen": "减速", "resonance_type": "转换"})["success"]
    before = dict(player.dao_wen)

    r = e.execute_action("use_resonance",
                         {"source_daowen": "急速", "resonance_type": "转换"})
    assert not r["success"]
    assert "残韵" in r["error"]
    assert dict(player.dao_wen) == before
    assert e.state.resonance["转换"] == 0, "第一步已把唯一一枚转换残韵用掉，第二步不得再扣"


def test_wrong_resonance_type_on_transform_is_refused():
    """错误输入：拿另一种残韵打转化道纹 → 路径不存在，失败且不消耗。"""
    e = _engine("wrong_type")
    e.state.resonance["转换"] = 1
    e.state.resonance["反转"] = 1
    _battle_with(e, _monster("碎岩鸮", ("减速", 1)))
    player = e.state.player
    assert e.execute_action("use_resonance",
                            {"source_daowen": "减速", "resonance_type": "转换"})["success"]
    assert "急速" in player.dao_wen

    r = e.execute_action("use_resonance",
                         {"source_daowen": "急速", "resonance_type": "反转"})
    assert not r["success"]
    assert "急速" in r["error"]
    assert "急速" in player.dao_wen
    assert e.state.resonance["反转"] == 1


def test_ruling_b_still_holds_no_learning_shortcut():
    """错误输入（裁定B 未被绕过）：原始与转化道纹都不可【学习】，回溯起点只能来自怪物。"""
    e = _engine("gate")
    r_original = _learn(e, "减速")
    assert not r_original["success"]
    assert "只能从怪物身上获得" in r_original["error"]

    r_transform = _learn(e, "急速")
    assert not r_transform["success"]
    assert "怪物转化道纹" in r_transform["error"]
    assert "急速" not in e.state.player.dao_wen
