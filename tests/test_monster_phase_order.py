"""怪物阶段：先攻击还是先发动道纹由**发动方**自选（2026-09-18 用户裁定，报告 Q11）。

用户令原话：不要在引擎硬编码顺序，怪物 AI 自己选先攻击还是先道纹。本文件钉住四条：

1. 缺省＝道纹先、攻击后（现行口径不变，老提交零改动照样合法）；
2. `attack_first=true`＝先普攻、后道纹，且顺序**真的改变结算后果**——同一条「闪避」
   提交，道纹先时被【必中】压掉（`dodge_success=False`／`dodge_fail_reason=必中攻击
   无法闪避`，伤害照吃），先攻击时闪避成功（伤害 0），【必中】层数留到下一回合；
3. `prepare_monster_phase` 面板公示这个选择（`attack_first_allowed`／`order_note`），
   发动方不必猜；
4. `attack_first` 非布尔 → 静态校验拒绝（零副作用，不扣法力不动层数）。

配套口径：出手数与命中数一律按 prepare 快照校验，与先后顺序无关。
"""
import os
import sys

import pytest

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.daowen import DaoWenEngine
from engine.monsters import make_monster_entity

DaoWenEngine.register_all()

TIMINGS = ("before", "after", "damage_after", "life_before")


def _engine(suffix: str) -> GameEngine:
    engine = GameEngine(db_path=f"data/test_morder_{suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "沈昼", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    engine.execute_action("setup_choose_region", {"region": "扭曲都市"})
    return engine


def _monster(name="靶怪"):
    """只带【必中】的怪：x_free，代价 异变X，效果＝下X次选择[目标]无法被闪避。"""
    return make_monster_entity({
        "name": name, "blood_limit": 222, "mana_limit": 7, "speed_limit": 3,
        "attack_count": 3, "attack_power": 7,
        "dao_wen": {"必中": None}, "region": "扭曲都市",
    })


def _enter_combat(engine, monster):
    engine.state.enemies = [monster]
    engine.state.phase = "in_combat"
    engine.state.current_round = 2
    engine.combat.reset_monster_activation()


def _build_choice(actor, *, x=1, dodge=True, attack_first=None):
    option = next(o for o in actor["daowen_options"] if o["name"] == "必中")
    dao = {"name": "必中", "x": x, "dodge": False, "blood_shadow": False,
           "trigger_spell_choices": {
               holder: {sp["spell_name"]: {"use": False} for sp in spells}
               for holder, spells in option.get("trigger_spell_options", {}).items()}}
    tgt = actor["attack_target_options"][0]
    spell_choices = {tm: {sp["spell_name"]: {"use": False}
                          for sp in tgt.get("spell_options", {}).get(tm, [])}
                     for tm in TIMINGS}
    hits = [{"target_ref": tgt["ref"], "dodge": bool(dodge), "blood_shadow": False,
             "spell_choices": spell_choices}
            for _ in range(actor["base_hits_per_attack"])]
    choice = {"actor_ref": actor["actor_ref"], "daowen": dao,
              "attack_actions": [{"hits": hits}
                                 for _ in range(actor["base_attack_actions"])]}
    if attack_first is not None:
        choice["attack_first"] = attack_first
    return choice


def _run(engine, **kw):
    prepared = engine.combat.prepare_monster_phase()
    actor = prepared["actors"][0]
    choice = _build_choice(actor, **kw)
    results = engine.combat.resolve_monster_phase([choice], prepared=prepared)
    return results, prepared, actor, choice


def _attacks(results):
    return [r for r in results if "hit_index" in r]


def _daowen_index(results):
    return next(i for i, r in enumerate(results) if r.get("daowen_activated") == "必中")


# ========================================================================
# 正常：两种顺序都合法，且后果不同
# ========================================================================

def test_default_order_is_daowen_first_and_bizhong_covers_this_round():
    """缺省（不提交 attack_first）＝道纹先、攻击后：本回合刚发动的【必中】压掉闪避。"""
    e = _engine("default")
    m = _monster()
    _enter_combat(e, m)
    hp_before = e.state.player.current_hp

    results, prepared, actor, choice = _run(e, x=1, dodge=True)
    assert "attack_first" not in choice, "缺省路径不得凭空多出字段"

    first_attack = next(i for i, r in enumerate(results) if "hit_index" in r)
    assert _daowen_index(results) < first_attack, "缺省必须道纹先、攻击后"

    hits = _attacks(results)
    assert hits[0]["dodge_success"] is False
    assert hits[0]["dodge_fail_reason"] == "必中攻击无法闪避"
    assert hits[0]["hp_lost"] > 0, "必中的那一击必须真的打到人"
    assert e.state.player.current_hp < hp_before
    # X=1 只有一层：首击吃掉后，其余各击恢复可闪避
    assert all(h["dodge_success"] for h in hits[1:])
    assert e.combat.bizhong_remaining(m) == 0


def test_attack_first_lands_dodges_and_banks_bizhong_for_next_round():
    """attack_first=true＝先普攻、后道纹：本回合的攻击仍可被闪避，【必中】留到下回合。"""
    e = _engine("attack_first")
    m = _monster()
    _enter_combat(e, m)
    hp_before = e.state.player.current_hp

    results, prepared, actor, choice = _run(e, x=1, dodge=True, attack_first=True)
    assert choice["attack_first"] is True

    attack_idx = [i for i, r in enumerate(results) if "hit_index" in r]
    assert max(attack_idx) < _daowen_index(results), "先攻击：所有攻击都排在道纹之前"

    hits = _attacks(results)
    assert all(h["dodge_success"] for h in hits), "必中还没生效，闪避必须成功"
    assert all(h["hp_lost"] == 0 for h in hits)
    assert e.state.player.current_hp == hp_before
    assert e.combat.bizhong_remaining(m) == 1, "层数没被本回合的攻击吃掉"


def test_attack_first_true_then_next_round_daowen_first_is_covered():
    """跨回合：先攻击那一回合存下的【必中】，下一回合格外管用（道纹先/后都压闪避）。"""
    e = _engine("cross_round")
    m = _monster()
    _enter_combat(e, m)
    _run(e, x=1, dodge=True, attack_first=True)
    assert e.combat.bizhong_remaining(m) == 1

    # 真实跨回合：round_end → round_start（预算/阶段/道纹激活集都按引擎口径重置）
    from sim.build_learner import round_start_relic_choices
    e.state.combat_subphase = "await_round_end"   # 直接调 CombatEngine，需手动同步 API 子阶段
    assert e.execute_action("round_end", {})["success"]
    assert e.execute_action(
        "round_start", {"relic_choices": round_start_relic_choices(e)})["success"]
    assert e.combat.bizhong_remaining(m) == 1, "必中是常驻层（remaining_rounds=-1），跨回合不清"
    e.state.player.current_speed = e.state.player.speed_limit   # 让它这回合还闪得动

    results, prepared, actor, choice = _run(e, x=1, dodge=True)   # 缺省＝道纹先
    hits = _attacks(results)
    assert len(hits) == 3, "靶怪速限3 → 每次攻击3击"
    # 层数＝2（上回合存的 1 + 本回合 X=1 新攒的 1），而每一击都要消耗一层
    # （consume_bizhong 在每次攻击判定里调用，不看有没有提交闪避）→ 前两击必中。
    assert hits[0]["dodge_success"] is False
    assert hits[1]["dodge_success"] is False, "上回合存下的那一层在本回合照样生效"
    assert hits[0]["dodge_fail_reason"] == "必中攻击无法闪避"
    assert hits[1]["dodge_fail_reason"] == "必中攻击无法闪避"
    assert all(h["dodge_success"] for h in hits[2:]), "两层用完后恢复可闪避"
    assert e.combat.bizhong_remaining(m) == 0


# ========================================================================
# 面板公示：发动方不必猜
# ========================================================================

def test_prepare_advertises_order_choice():
    e = _engine("panel")
    m = _monster()
    _enter_combat(e, m)
    prepared = e.combat.prepare_monster_phase()
    actor = prepared["actors"][0]
    assert actor["attack_first_allowed"] is True
    assert "attack_first" in actor["order_note"]
    assert "先发动道纹" in actor["order_note"], "order_note 必须写清缺省是哪一种"


# ========================================================================
# 错误输入：非布尔拒绝，且零副作用
# ========================================================================

def test_non_bool_attack_first_rejected_without_side_effects():
    e = _engine("bad_bool")
    m = _monster()
    _enter_combat(e, m)
    prepared = e.combat.prepare_monster_phase()
    actor = prepared["actors"][0]
    mana_before, mutation_before = m.current_mana, m.mutation_count
    hp_before = e.state.player.current_hp

    choice = _build_choice(actor, x=1, attack_first="yes")
    with pytest.raises(ValueError, match="attack_first必须是布尔值"):
        e.combat.resolve_monster_phase([choice], prepared=prepared)

    assert (m.current_mana, m.mutation_count) == (mana_before, mutation_before)
    assert e.combat.bizhong_remaining(m) == 0
    assert e.state.player.current_hp == hp_before, "静态校验必须零副作用"
