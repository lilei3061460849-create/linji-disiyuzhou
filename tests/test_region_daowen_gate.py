"""
pytest - 副本专属道纹门禁（用户裁定）

规则：最初的副本专属道纹仅能通过残韵从**对应副本的怪物**身上转化获得；
获得之后才能学习该副本的其他专属道纹。其他副本的专属道纹一律不可获得。

覆盖：正常路径 / 边界条件 / 错误输入
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance
from engine.gamedata import REGION_EXCLUSIVE_DAOWEN


def _engine(region="龙心谷"):
    e = GameEngine(db_path="/tmp/linji_tests/gate.db", rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    e.execute_action("setup_choose_daowen", {"daowen": "杀伐"})
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": region})
    return e


def _learn(e, name):
    e.state.energy = 3
    return e.execute_action("pre_battle_action",
                            {"sub_action": "学习", "sub": "daowen", "name": name})


# ---------- 正常路径 ----------

def test_common_daowen_always_learnable():
    """正常路径：通用核心道纹不受副本门禁限制"""
    e = _engine("龙心谷")
    for dw in ("庇护", "再生", "冲击", "血债"):
        assert _learn(e, dw)["success"], f"通用道纹{dw}应可学习"


def test_can_learn_same_region_after_owning_one():
    """正常路径：已持有本副本一种专属道纹后，可学习该副本其他专属道纹"""
    e = _engine("龙心谷")
    # 模拟"经残韵从本副本怪物身上转化获得"
    e.state.player.dao_wen["裂变"] = DaoWenInstance(
        DaoWen(name="裂变", formula="", cost_type="消耗",
               cost_formula="X", effect_formula=""))
    for dw in ("加害", "伤痕", "龙鳞"):
        assert _learn(e, dw)["success"], f"同副本专属道纹{dw}应可学习"


def test_transformed_monster_daowen_not_learnable_outside_battle():
    """正常路径：怪物转化道纹须以自身已持有的道纹为起点经残韵获得，
    不可通过局外【学习】直接习得（README 第211/248行）"""
    e = _engine("龙心谷")
    for dw in ("蒙蔽", "坠落", "弱化"):
        r = _learn(e, dw)
        assert not r["success"], f"转化道纹{dw}不应能被局外直接学习"
        assert "怪物转化道纹" in r["error"]


def test_original_monster_daowen_never_learnable():
    """边界：原始怪物道纹人类无法承受并获得（README 第250行）"""
    e = _engine("龙心谷")
    for dw in ("必中", "狂暴", "自愈", "飞行"):
        r = _learn(e, dw)
        assert not r["success"], f"原始怪物道纹{dw}不应能被学习"
        assert "原始怪物道纹" in r["error"]


def test_rejected_monster_daowen_refunds_energy():
    """边界：被拒绝的学习必须退还精力，不能白扣"""
    e = _engine("龙心谷")
    before = e.state.energy
    r = _learn(e, "蒙蔽")
    assert not r["success"]
    assert e.state.energy == before, "学习被拒后精力应原样退还"


# ---------- 边界条件 ----------

def test_first_exclusive_cannot_be_learned_directly():
    """边界：未持有任何本副本专属道纹时，不能直接学习第一个"""
    e = _engine("龙心谷")
    r = _learn(e, "加害")
    assert not r["success"]
    assert "须先通过残韵" in r["error"]


def test_energy_refunded_on_rejection():
    """边界：被门禁拒绝时必须退还精力，否则玩家白白损失一次行动"""
    e = _engine("龙心谷")
    e.state.energy = 3
    e.execute_action("pre_battle_action",
                     {"sub_action": "学习", "sub": "daowen", "name": "僵化"})
    assert e.state.energy == 3, "被拒绝时精力应退还"


def test_gate_applies_to_every_region():
    """边界：三个副本的专属道纹都应受门禁约束"""
    for region, pool in REGION_EXCLUSIVE_DAOWEN.items():
        e = _engine(region)
        other = next(rg for rg in REGION_EXCLUSIVE_DAOWEN if rg != region)
        foreign = sorted(REGION_EXCLUSIVE_DAOWEN[other])[0]
        r = _learn(e, foreign)
        assert not r["success"], f"{region}不应能学{other}的{foreign}"


# ---------- 错误输入 ----------

def test_foreign_region_exclusive_always_rejected():
    """错误输入：其他副本的专属道纹，即使已持有本副本专属道纹也不可学"""
    e = _engine("龙心谷")
    e.state.player.dao_wen["裂变"] = DaoWenInstance(
        DaoWen(name="裂变", formula="", cost_type="消耗",
               cost_formula="X", effect_formula=""))
    for foreign in ("僵化", "洗劫", "坏死", "逼债"):
        r = _learn(e, foreign)
        assert not r["success"], f"{foreign}属于其他副本，不应可学"
        assert "专属道纹" in r["error"]


def test_unknown_daowen_still_rejected():
    """错误输入：不存在的道纹仍应被拒绝"""
    e = _engine("龙心谷")
    r = _learn(e, "根本不存在的道纹")
    assert not r["success"]
    assert "未知道纹" in r["error"]


def test_pools_are_disjoint():
    """非法配置：三个副本的专属道纹池不得有交集，否则归属判定有歧义"""
    regions = list(REGION_EXCLUSIVE_DAOWEN)
    for i in range(len(regions)):
        for j in range(i + 1, len(regions)):
            a, b = REGION_EXCLUSIVE_DAOWEN[regions[i]], REGION_EXCLUSIVE_DAOWEN[regions[j]]
            assert not (a & b), f"{regions[i]}与{regions[j]}专属道纹重叠：{a & b}"
