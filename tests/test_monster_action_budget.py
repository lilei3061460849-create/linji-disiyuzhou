"""怪物出手预算在怪物阶段真正生效（【无力】的洞，2026-09-17 修复）。

正文口径：怪物每回合 **1 次攻击 + 1 种道纹**；【疯狂】+X、【狂暴】+1、
【无力】-X（【高爆手雷】给的也是【无力】）。这条预算由 `single_round_action_count` 算出，
但修复前只有 `api._action_budget_of` / `_duel_side_can_act` 读它——
**怪物阶段自己从不校验**：【无力】挂满 2 层、预算算出 0，怪物照样打出
1 次攻击（3 命中、18 伤害）+ 1 种道纹，等于【无力】对怪物完全无效。

修复后（本文件逐条钉住）：
- `prepare_monster_phase` 先算 `remaining = 预算 - 已用出手`，≤0 直接把该怪物列入
  `skipped`（理由写明 已用/预算），不再给出任何可提交动作；
- 还有余量时按「道纹优先预留 1 次」分配：
  `base_attack_actions = min(攻击出手数, remaining - (1 if 有可发动道纹 else 0))`，
  保证任何合法提交（道纹至多 1 + 攻击出手）都不超预算；
- actor 载荷暴露 `action_budget` / `actions_used` / `actions_remaining`，
  发动方不必自己按状态推算净效果（与「prepare 带引擎口径」同一原则）；
- `skipped` 条目随 resolve 结果一并返回，AI/DM 能看到"这只为什么没动"。

覆盖：常态 / 无力 / 叠加归零 / 疯狂·狂暴不被预留吃掉 / 无道纹时的分配 /
已用出手 / skipped 契约。
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
from tests.monster_phase_support import resolve_monster_phase
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity, StatusEffect


def _engine(suffix: str) -> GameEngine:
    engine = GameEngine(db_path=f"data/test_budget_{suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "沈昼", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    engine.execute_action("setup_choose_region", {"region": "扭曲都市"})
    return engine


def _monster(engine: GameEngine, *, name="缝合鱼", speed=3, mana=6, hp=234, daowen="爆裂"):
    m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
               attack_count=speed, attack_power=mana, speed_limit=speed, mana_limit=mana)
    m.current_speed = speed
    m.current_mana = mana
    if daowen:
        m.dao_wen[daowen] = DaoWenInstance(
            DaoWen(name=daowen, formula="", cost_type="消耗", cost_formula="2X",
                   effect_formula=""), x_value=1)
    engine.state.enemies = [m]
    engine.state.phase = "in_combat"
    engine.state.current_round = 2
    engine.combat.reset_monster_activation()
    return m


def _actor(engine, monster):
    prepared = engine.combat.prepare_monster_phase()
    return prepared, next((a for a in prepared["actors"] if a["monster"] == monster.name), None)


def _hits(details):
    return [d for d in details if "damage_dealt" in d or "dodge_success" in d]


# ---------------------------------------------------------------- 常态

def test_baseline_budget_is_one_attack_plus_one_daowen():
    engine = _engine("baseline")
    m = _monster(engine)

    prepared, actor = _actor(engine, m)

    assert actor["action_budget"] == 2 and actor["actions_used"] == 0
    assert actor["actions_remaining"] == 2
    assert actor["base_attack_actions"] == 1, "1 次攻击出手"
    assert actor["daowen_options"], "1 种道纹仍可选"

    details = resolve_monster_phase(engine.combat)   # 自动选第一个合法道纹
    assert len(_hits(details)) == 3, "攻次3 → 一次攻击出手打3下"
    assert m.actions_used_this_round == 2, "1 种道纹 + 1 次攻击出手 ＝ 预算 2，刚好用满"


# ---------------------------------------------------------------- 无力

def test_wuli_two_stacks_skips_the_monster_entirely():
    """修复前：预算算出 0，怪物照样打出 3 命中 / 18 伤害。"""
    engine = _engine("wuli2")
    m = _monster(engine)
    hp_before = engine.state.player.current_hp
    m.add_status(StatusEffect(name="无力", value=2, remaining_rounds=2, source="测试"))

    prepared, actor = _actor(engine, m)

    assert actor is None, "预算 0：不得再给出任何可提交动作"
    skip = next(s for s in prepared["skipped"] if s["monster"] == m.name)
    assert "出手预算已用尽(0/0)" in skip["reason"]

    details = resolve_monster_phase(engine.combat, {m.name: None})
    assert _hits(details) == []
    assert engine.state.player.current_hp == hp_before, "无力2：这一刀本来会掉18血"
    assert any("出手预算已用尽" in str(d.get("reason", "")) for d in details), \
        "skipped 条目要随 resolve 结果返回，AI/DM 才看得到它为什么没动"


def test_wuli_one_stack_leaves_only_the_daowen():
    engine = _engine("wuli1")
    m = _monster(engine)
    m.add_status(StatusEffect(name="无力", value=1, remaining_rounds=2, source="测试"))

    prepared, actor = _actor(engine, m)

    assert actor["action_budget"] == 1
    assert actor["base_attack_actions"] == 0, "仅剩的 1 次出手预留给道纹"
    assert actor["daowen_options"], "道纹仍发动得出去——无力不是束缚"

    details = resolve_monster_phase(engine.combat)   # 自动选第一个合法道纹
    assert _hits(details) == [], "攻击出手已被无力吃掉"
    assert m.actions_used_this_round == 1, "只剩道纹那 1 次出手：道纹照发、攻击没了"


def test_wuli_one_stack_without_daowen_keeps_the_attack():
    """没有可发动道纹时不预留：仅剩的 1 次出手给攻击。"""
    engine = _engine("wuli1_nodw")
    m = _monster(engine, daowen=None)
    m.add_status(StatusEffect(name="无力", value=1, remaining_rounds=2, source="测试"))

    prepared, actor = _actor(engine, m)

    assert actor["action_budget"] == 1
    assert actor["daowen_options"] == []
    assert actor["base_attack_actions"] == 1
    assert len(_hits(resolve_monster_phase(engine.combat, {m.name: None}))) == 3


# ---------------------------------------------------------------- 叠加

def test_wuli_from_two_sources_stacks_to_zero_budget():
    """道纹给的【无力】与【高爆手雷】给的【无力】是同一笔账：层数相加，预算归零。"""
    engine = _engine("stack")
    m = _monster(engine)
    m.add_status(StatusEffect(name="无力", value=1, remaining_rounds=2, source="测试"))
    m.add_status(StatusEffect(name="无力", value=1, remaining_rounds=-1, source="高爆手雷"))

    prepared, actor = _actor(engine, m)

    assert m.get_status_value("无力") == 2, "同名状态数值相加"
    assert engine._action_budget_of(m) == 0
    assert actor is None
    assert any(s["monster"] == m.name for s in prepared["skipped"])


def test_used_actions_shrink_the_remaining_budget():
    engine = _engine("used")
    m = _monster(engine)
    m.actions_used_this_round = 1

    prepared, actor = _actor(engine, m)

    assert actor["actions_used"] == 1 and actor["actions_remaining"] == 1
    assert actor["base_attack_actions"] == 0


# ---------------------------------------------------------------- 增益不被吃掉

def test_fengkuang_expands_budget_and_attacks_together():
    """【疯狂】2：预算 2+2=4、攻击出手 1+2=3 —— 预留 1 次给道纹后仍是 3，不被吃掉。"""
    engine = _engine("fengkuang")
    m = _monster(engine)
    m.add_status(StatusEffect(name="疯狂", value=2, remaining_rounds=3, source="测试"))

    prepared, actor = _actor(engine, m)

    assert actor["action_budget"] == 4
    assert actor["base_attack_actions"] == 3
    assert engine.combat._monster_attack_actions(m, set()) == 3


def test_kuangbao_expands_budget_and_attacks_together():
    engine = _engine("kuangbao")
    m = _monster(engine)
    m.add_status(StatusEffect(name="狂暴", value=1, remaining_rounds=3, source="测试"))

    prepared, actor = _actor(engine, m)

    assert actor["action_budget"] == 3
    assert actor["base_attack_actions"] == 2
