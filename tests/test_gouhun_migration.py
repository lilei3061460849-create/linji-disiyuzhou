"""【勾魂】改版验证（2026-08-30，DM 裁定见 报告.md 硬伤2-C）。

旧语义（已废止）：消耗X，[回始]使[目标]失去 2X 点当前法力，持续∞。
    曾迁移为 ROUND_START 相位 Mechanism（priority 40，经 mana 动词）。
新语义：消耗X，使[目标]**无法获得[法力]**，持续X。
    - 不扣已有法力，只压制[回始]的法力回填（条目 mana_refill_blocked）；
    - 持续 X 回合，到期自然恢复（不再是"永久死刑"）；
    - 因此不再有回始机制条目，ROUND_START 声明层机制 GOULUN 已删除。

另外本文件钉住硬伤2-D 的裁定：**转化（残韵）不清除已生效的 debuff**——
把怪物的【勾魂】转化成别的道纹，玩家身上已挂的勾魂状态按自然规则继续。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine  # noqa: E402
from engine.daowen import DaoWenEngine  # noqa: E402
from engine.dice import DiceEngine  # noqa: E402
from engine.enums import CombatSubphase  # noqa: E402
from engine.mechanisms import MECHANISMS, Phase  # noqa: E402
from engine.models import DaoWen, DaoWenInstance, Entity, GameState, StatusEffect  # noqa: E402
from engine.validator import check_migrated_mechanism_guards  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
COMBAT_SOURCE = (ROOT / "engine" / "combat.py").read_text(encoding="utf-8")


def _arena(mana=20, gouhun_rounds=None, entity_type="轮回者", alive=True):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=0, speed_limit=10, current_speed=5)
    ent = Entity("E", entity_type, blood_limit=50, current_hp=50,
                 mana_limit=30, current_mana=mana)
    if gouhun_rounds is not None:
        ent.add_status(StatusEffect(name="勾魂", remaining_rounds=gouhun_rounds,
                                    value=1, source="x"))
    ent.is_alive = alive
    state.player = player
    state.enemies = [ent]
    return state, CombatEngine(state, DiceEngine()), player, ent


# ==================== 1. 旧机制已拆除 ====================

def test_old_gouhun_mechanism_removed():
    assert MECHANISMS.get("勾魂") is None, "旧【勾魂】回始扣法力机制必须已删除"
    assert "勾魂" not in [m.name for m in MECHANISMS.all()]
    assert check_migrated_mechanism_guards() == []


def test_no_round_start_mana_drain_anywhere():
    assert "round_start_mana_drain" not in COMBAT_SOURCE
    assert "gouhun_mana" not in COMBAT_SOURCE, "不得残留回始扣法力的效果条目"
    calc = DaoWenEngine.resolve("勾魂", 3)
    assert "round_start_mana_drain" not in calc
    assert calc.get("no_mana_gain") is True
    assert calc.get("duration") == 3, "持续 = X"
    assert "无法获得法力" in calc["summary"], calc["summary"]


# ==================== 2. 新语义：回始不获得法力 ====================

def test_gouhun_blocks_mana_refill_but_keeps_current_mana():
    """正常路径：勾魂期间[回始]不获得法力，已有法力分毫不动。"""
    state, combat, _player, ent = _arena(mana=9, gouhun_rounds=2)
    res = combat.round_start({"relic_choices": {}})
    blocked = [e for e in res["effects"] if e.get("type") == "mana_refill_blocked"]
    assert blocked, f"应有回填被压制条目: {res['effects']}"
    assert blocked[0]["entity"] == "E" and blocked[0]["gained"] == 0
    assert ent.current_mana == 9, f"已有法力不得被扣，实{ent.current_mana}"
    assert not any(e.get("type") == "mana_refill" and e.get("entity") == "E"
                   for e in res["effects"])


def test_gouhun_expires_after_x_rounds():
    """边界：持续 X 回合，[回终]递减，走完后恢复回填。"""
    state, combat, _player, ent = _arena(mana=5, gouhun_rounds=2)
    combat.round_start({"relic_choices": {}})
    assert ent.has_status("勾魂")
    state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
    combat.round_end()
    assert ent.has_status("勾魂"), "第1回合末仍在持续期内"
    state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
    combat.round_end()
    assert not ent.has_status("勾魂"), "持续X=2 走完应自然到期"
    res = combat.round_start({"relic_choices": {}})
    assert any(e.get("type") == "mana_refill" and e.get("entity") == "E"
               for e in res["effects"]), "到期后应恢复回填"


def test_gouhun_only_applies_to_targets_that_gain_mana():
    """边界：怪物没有法力概念，勾魂对其无意义（回始不回填法力，条目不出现）。"""
    state, combat, _player, ent = _arena(mana=0, gouhun_rounds=2, entity_type="怪物")
    res = combat.round_start({"relic_choices": {}})
    types = [e.get("type") for e in res["effects"] if e.get("entity") == "E"]
    assert "mana_refill_blocked" not in types, types
    assert "mana_refill" not in types, types


def test_gouhun_dead_entity_no_entry():
    """边界：已命零的实体既无回填也无压制条目。"""
    state, combat, _player, ent = _arena(mana=10, gouhun_rounds=2, alive=False)
    res = combat.round_start({"relic_choices": {}})
    types = [e.get("type") for e in res["effects"] if e.get("entity") == "E"]
    assert "mana_refill_blocked" not in types and "mana_refill" not in types, types


def test_gouhun_cast_sets_duration_equal_x():
    """正常：施放勾魂X 挂在目标身上的状态持续 = X（不再是 ∞）。"""
    state, combat, player, ent = _arena(mana=10)
    calc = DaoWenEngine.resolve("勾魂", 4, target=ent, caster=player)
    res = combat.apply_daowen_effect("勾魂", calc, player, ent)
    assert ent.has_status("勾魂")
    dur = next(s.remaining_rounds for s in ent.status_effects if s.name == "勾魂")
    assert dur == 4, f"持续应为 X=4，实{dur}"
    gouhun_effect = next((e for e in res["effects"] if e.get("type") == "gouhun"), None)
    assert gouhun_effect and gouhun_effect.get("no_mana_gain") is True
    assert gouhun_effect.get("duration") == 4, gouhun_effect


def test_no_gouhun_refills_normally():
    """对照：没有勾魂时，回始照常回填法限（防止压制逻辑误伤）。"""
    state, combat, _player, ent = _arena(mana=10)
    res = combat.round_start({"relic_choices": {}})
    refill = next(e for e in res["effects"]
                  if e.get("type") == "mana_refill" and e.get("entity") == "E")
    assert refill["gained"] == 30 and ent.current_mana == 40, refill


# ==================== 3. 硬伤2-D：转化不清除已生效 debuff ====================

def test_resonance_conversion_does_not_clear_active_gouhun():
    """DM 裁定：残韵转化只改怪物牌面，不解除玩家身上已挂的勾魂。

    转化后玩家仍按剩余持续回合继续被压制；怪物下回合改用新道纹（真实生效）。
    """
    from engine.api import GameEngine
    from tests.setup_support import finish_initial_daowen

    db = f"/tmp/linji_tests/test_gouhun_res_{os.getpid()}.db"
    e = GameEngine(db_path=db, rng_seed=7,
                   sealed_candidate_path=f"/tmp/linji_tests/test_gouhun_res_s_{os.getpid()}.json")
    e.execute_action("setup_attributes", {"name": "白某", "blood_points": 10,
                                          "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "曲解"})
    e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.state.phase = "in_combat"
    e.state.current_round = 2
    p = e.state.player
    m = Entity(name="寄骨蝇", entity_type="怪物", blood_limit=200, current_hp=200,
               attack_count=1, attack_power=1)
    m.dao_wen["勾魂"] = DaoWenInstance(
        DaoWen(name="勾魂", formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""), x_value=4)
    e.state.enemies.append(m)
    # 先让怪物的勾魂落到玩家身上（持续 X=4）
    from engine.daowen import DaoWenEngine as DWE
    calc = DWE.resolve("勾魂", 4, target=p, caster=m)
    e.combat.apply_daowen_effect("勾魂", calc, m, p)
    assert p.has_status("勾魂")

    # 玩家用残韵把怪物的【勾魂】曲解成【镇尸】
    e.state.resonance = {"曲解": 1}
    r = e.execute_action("use_resonance", {"source_daowen": "勾魂",
                                           "resonance_type": "曲解",
                                           "target_ref": "enemy:0"})
    assert r.get("success") is True, r.get("error")
    assert "镇尸" in m.dao_wen and "勾魂" not in m.dao_wen, "怪物牌面应已改写"

    # 裁定要点：玩家身上已生效的勾魂**不**被清除，继续按剩余持续压制
    assert p.has_status("勾魂"), "转化不得清除已生效的 debuff"
    p.current_mana = 11
    rs = e.combat.round_start({"relic_choices": {}})
    assert any(x.get("type") == "mana_refill_blocked" and x.get("entity") == p.name
               for x in rs["effects"]), "转化后勾魂仍应压制回始回填"
    assert p.current_mana == 11

    # 真实生效：怪物下回合用新道纹（镇尸）而不是已被转化的勾魂
    e.state.current_round = 3
    prepared = e.combat.prepare_monster_phase()
    names = [o["name"] for o in prepared["actors"][0]["daowen_options"]]
    assert names == ["镇尸"], f"转化后怪物应改用新道纹，实{names}"
