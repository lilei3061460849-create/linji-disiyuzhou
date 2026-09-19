"""【自愈】【滋养】重做回归（2026-09-18 用户令）。

自愈：旧「代价：异变5X。[回始]获得自身血限10X%的回复，持续∞」
      → 新「代价：冷却X。恢复[目标]25X%已损生命」。
      旧版在现行规则下是一台**自杀定时器**：回复经统一 heal 动词把过量部分一并计入
      `total_healed`，而癌变阈值只有 ⌈血限×2⌉ → 承载怪每回合自我奶 ⌈血限×10X%⌉，
      ceil(20/X) 回合后必然自我癌变（X=3→7、X=5→4、X=9→3），永久离场且不给[碎片]，
      只白送局外【休整】+8；叠加异变5X/次与崩解线50，第二次发动还会直接崩解。
      新版是主动、单体、按已损生命计价（满血不浪费）、代价【冷却X】（X 场战斗），
      因此 ROUND_START 上的自愈机制整体删除（见 机制迁移台账.md《已回撤》）。

滋养：旧「消耗2X。使[目标]获得血限10X%的回复」→ 新「消耗2X。使[目标]受到的恢复量翻倍，持续X」。
      本身不回复任何生命，是**放大器**；结算点在 `GameState.apply_heal`（统一回复入口），
      因此覆盖一切来源（道纹／消耗品／寄生／休整…），过量部分同样翻倍计入 total_healed。

combo（用户给的验收算式）：滋养（×2）＋自愈2（已损50%）＝已损100%＝满血复活。
癌变没有被绕开：翻倍同样把癌变进度翻倍，能免疫的有且只有遗物【第一杯】（用户裁定的强度上限）。

覆盖：正常路径 / 边界条件 / 错误输入
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.daowen import DaoWenEngine
from engine.dice import DiceEngine
from engine.enums import CostType
from engine.mechanisms import MECHANISMS, Mechanism, Phase, Trigger
from engine.models import DaoWen, DaoWenInstance, Entity, GameState, Relic, StatusEffect

DaoWenEngine.register_all()
ROOT = Path(__file__).resolve().parents[1]


def _battle(*, m_hp=10, m_bl=100, p_mana=20):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("沈昼", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=p_mana, current_mana=p_mana,
                    speed_limit=10, current_speed=10)
    monster = Entity("白骨祭坛", "怪物", blood_limit=m_bl, current_hp=m_hp,
                     mana_limit=20, current_mana=20,
                     speed_limit=6, current_speed=6)
    state.player = player
    state.enemies = [monster]
    return state, CombatEngine(state, DiceEngine()), player, monster


def _give(entity, name, x=1, cost_type="消耗"):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type=cost_type,
               cost_formula="X", effect_formula=""), x_value=x)


def _cast_ziyu(combat, caster, target, x=1):
    _give(caster, "自愈", x, cost_type="冷却")
    return combat.apply_daowen_effect("自愈", DaoWenEngine.calculate_ziyu(x, target),
                                      caster, target)


def _cast_ziyang(combat, caster, target, x=1):
    _give(caster, "滋养", x)
    return combat.apply_daowen_effect("滋养", DaoWenEngine.calculate_ziyang(x, target),
                                      caster, target)


def _status(entity, name, value=1, rounds=-1):
    entity.add_status(StatusEffect(name=name, remaining_rounds=rounds,
                                   value=value, source="x"))


def _entry(result, entry_type):
    return next((e for e in result["effects"] if e.get("type") == entry_type), None)


# ==================== 自愈：正常路径 ====================


def test_ziyu_heals_25x_percent_of_missing_hp():
    """正常路径：已损40 的目标，X=1/2/4 依次回 10/20/40（按**已损生命**计价）"""
    for x, expected in ((1, 10), (2, 20), (4, 40)):
        state, combat, player, monster = _battle(m_hp=60, m_bl=100)
        r = _cast_ziyu(combat, player, monster, x=x)
        eff = _entry(r, "heal_missing_pct")
        assert eff["pct"] == 25 * x
        assert eff["missing_hp"] == 40
        assert eff["heal_amount"] == expected, f"X={x} 应回{expected}"
        assert monster.current_hp == 60 + expected


def test_ziyu_rounds_up():
    """边界：已损10、X=1 → ceil(10×25%)=3（正文整数规则：所有计算都向上取整）"""
    state, combat, player, monster = _battle(m_hp=90, m_bl=100)
    r = _cast_ziyu(combat, player, monster, x=1)
    assert _entry(r, "heal_missing_pct")["heal_amount"] == 3
    assert monster.current_hp == 93


def test_ziyu_cost_is_cooldown_not_mutation():
    """正常路径：代价＝冷却X 场，写在道纹实例上；不再支付异变、不再消耗法力"""
    state, combat, player, monster = _battle(m_hp=50)
    calc = DaoWenEngine.calculate_ziyu(3, monster)
    assert calc["cost_type"] == CostType.COOLDOWN.value
    assert calc["cost"] == 3
    assert "cost_mutation" not in calc and "heal_percent" not in calc and "duration" not in calc

    _give(player, "自愈", 3, cost_type="冷却")
    combat.apply_daowen_effect("自愈", calc, player, monster)
    assert player.dao_wen["自愈"].cooldown_remaining == 3
    assert player.mutation_count == 0
    assert player.current_mana == 20


def test_ziyu_can_heal_self_and_allies():
    """正常路径：需显式选定[目标]，可以选自己、也可以选友方/敌方任意角色"""
    state, combat, player, monster = _battle(m_hp=50)
    player.current_hp = 40
    r_self = _cast_ziyu(combat, player, player, x=2)
    assert _entry(r_self, "heal_missing_pct")["heal_amount"] == 30   # ceil(60×50%)
    assert player.current_hp == 70

    ally = Entity("同伴", "朋友", blood_limit=50, current_hp=20)
    state.friends.append(ally)
    r_ally = _cast_ziyu(combat, player, ally, x=1)
    assert _entry(r_ally, "heal_missing_pct")["heal_amount"] == 8    # ceil(30×25%)
    assert ally.current_hp == 28


# ==================== 自愈：边界与错误输入 ====================


def test_ziyu_on_full_hp_heals_zero():
    """边界：满血目标回复0——按已损生命计价，不再浪费在满血身上"""
    state, combat, player, monster = _battle(m_hp=100)
    r = _cast_ziyu(combat, player, monster, x=4)
    eff = _entry(r, "heal_missing_pct")
    assert eff["missing_hp"] == 0 and eff["heal_amount"] == 0
    assert monster.current_hp == 100
    assert monster.total_healed == 0


def test_ziyu_blocked_by_huaisi_and_zhenshi():
    """错误输入：【坏死】【镇尸】的禁疗照常拦住新版自愈（口径同 target_heal）"""
    for blocker in ("坏死", "镇尸"):
        state, combat, player, monster = _battle(m_hp=50)
        _status(monster, blocker)
        r = _cast_ziyu(combat, player, monster, x=2)
        eff = _entry(r, "heal_missing_pct")
        assert eff["blocked_by"] == "坏死"
        assert monster.current_hp == 50


def test_ziyu_target_mode_is_required():
    """口径：自愈/滋养 都是「需显式选定[目标]」（由 calc 签名驱动，不硬编码）"""
    state, combat, player, monster = _battle()
    for name in ("自愈", "滋养"):
        assert combat._daowen_requires_target(name) is True
        assert combat._daowen_target_mode(name) == "required"


def test_ziyu_mechanism_removed_and_round_start_no_longer_self_heals():
    """回撤钉死：ROUND_START 上不再有自愈机制，[回始]不会凭空回血"""
    assert MECHANISMS.get("自愈") is None
    state, combat, player, monster = _battle(m_hp=50)
    _status(monster, "自愈", value=5)      # 旧口径的状态：现在应当完全无效
    res = combat.round_start()
    assert monster.current_hp == 50
    assert not [e for e in res["effects"] if e.get("type") == "self_heal"]


def test_round_start_mechanisms_still_ordered_after_removal():
    """回撤后机制系统完好：ROUND_START 可容纳多机制、priority 决定顺序、每实体恰好一次。

    原 test_ziyu_migration.py 用自愈(10)当生产机制验证本条；自愈移除后改用衰败(20)，
    dummy 放在 5 与 30 两侧。priority 一律不重排（顺序即规则）。
    """
    records = []
    dummy_before = Mechanism(
        name="回始测试·前", when=Trigger.phase(Phase.ROUND_START),
        effect=lambda ctx, ts: records.append(("前", ctx.target.name, ctx.target.current_hp)),
        priority=5)
    dummy_after = Mechanism(
        name="回始测试·后", when=Trigger.phase(Phase.ROUND_START),
        effect=lambda ctx, ts: records.append(("后", ctx.target.name, ctx.target.current_hp)),
        priority=30)

    state, combat, player, monster = _battle(m_hp=20)
    _status(monster, "衰败", value=1)      # [回始]失去 ceil(20×10%)=2 → 20→18

    MECHANISMS.register(dummy_before)
    MECHANISMS.register(dummy_after)
    try:
        combat.round_start()
        assert records == [("前", "沈昼", 100), ("后", "沈昼", 100),
                           ("前", "白骨祭坛", 20), ("后", "白骨祭坛", 18)], records
        assert monster.current_hp == 18
    finally:
        MECHANISMS.unregister("回始测试·前")
        MECHANISMS.unregister("回始测试·后")
    assert MECHANISMS.get("回始测试·前") is None


def test_heal_missing_percent_is_a_wave_numeric_key():
    """口径：新键与 target_heal/heal_percent 同列波及数值键（总量平分，不复制不增加）"""
    assert "heal_missing_percent" in CombatEngine.WAVE_NUMERIC_KEYS


# ==================== 滋养：正常路径 ====================


def test_ziyang_applies_status_and_heals_nothing():
    """正常路径：滋养本身不回血，只挂【滋养】状态（持续X回合），代价消耗2X法力"""
    state, combat, player, monster = _battle(m_hp=50)
    calc = DaoWenEngine.calculate_ziyang(3, monster)
    assert calc["cost_type"] == CostType.MANA.value and calc["cost"] == 6
    assert calc["duration"] == 3 and "target_heal" not in calc

    _give(player, "滋养", 3)
    r = combat.apply_daowen_effect("滋养", calc, player, monster)
    eff = _entry(r, "status_added")
    assert eff["status"] == "滋养" and eff["duration"] == 3
    assert monster.current_hp == 50 and monster.total_healed == 0
    st = next(s for s in monster.status_effects if s.name == "滋养")
    assert st.remaining_rounds == 3


def test_ziyang_doubles_healing_from_any_source():
    """正常路径：翻倍落在统一回复入口，因此对一切来源生效（这里用直发 apply_heal 代表消耗品/寄生）"""
    state, combat, player, monster = _battle(m_hp=10)
    _cast_ziyang(combat, player, monster, x=2)

    detail = state.apply_heal(monster, 7, ctx={"source": "残骸", "mechanic": "heal"})
    assert detail["ziyang_doubled"] is True
    assert detail["heal_amount"] == 14
    assert monster.current_hp == 24
    assert monster.total_healed == 14      # 癌变计数按翻倍后的值记


def test_ziyang_plus_ziyu2_is_a_full_revive():
    """combo（用户验收算式）：滋养（×2）＋自愈2（已损50%）＝已损100%＝满血复活"""
    state, combat, player, monster = _battle(m_hp=10, m_bl=100)
    _cast_ziyang(combat, player, monster, x=1)
    r = _cast_ziyu(combat, player, monster, x=2)

    eff = _entry(r, "heal_missing_pct")
    assert eff["missing_hp"] == 90
    assert eff["heal_amount"] == 90            # ceil(90×50%)=45，滋养翻倍→90
    assert monster.current_hp == monster.blood_limit == 100


def test_ziyang_without_ziyang_heal_is_not_doubled():
    """边界：没有【滋养】状态时，同一笔自愈只回 45（对照上一条，证明翻倍来自状态）"""
    state, combat, player, monster = _battle(m_hp=10, m_bl=100)
    r = _cast_ziyu(combat, player, monster, x=2)
    assert _entry(r, "heal_missing_pct")["heal_amount"] == 45
    assert monster.current_hp == 55


def test_ziyang_doubling_accelerates_cancer_and_first_cup_immunizes():
    """口径：翻倍把癌变进度也翻倍（阈值 ⌈血限×2⌉）；唯一免疫仍是遗物【第一杯】"""
    # 怪物：一笔 60 的回复在滋养下变 120，累计越过 200 阈值 → 癌变吸收（不给碎片）
    state, combat, player, monster = _battle(m_hp=10, m_bl=100)
    monster.total_healed = 100
    _cast_ziyang(combat, player, monster, x=1)
    state.apply_heal(monster, 60, ctx={"source": "测试", "mechanic": "heal"})
    assert monster.total_healed == 220
    assert combat.cancer_threshold_of(monster) == 200
    assert combat.check_cancer(monster) is not None
    assert monster.is_proliferated and not monster.is_alive

    # 轮回者侧：持【第一杯】→ 同样翻倍也不癌变（用户裁定：这就是强度上限）
    state2, combat2, player2, monster2 = _battle(m_hp=10)
    state2.relics.append(Relic(name="第一杯", effect=""))
    player2.total_healed = combat2.cancer_threshold_of(player2)
    _cast_ziyang(combat2, player2, player2, x=1)
    state2.apply_heal(player2, 50, ctx={"source": "测试", "mechanic": "heal"})
    assert combat2.check_cancer(player2) is None
    assert player2.is_alive


def test_ziyang_expires_after_x_rounds():
    """边界：持续X 回合，到期后恢复量不再翻倍"""
    state, combat, player, monster = _battle(m_hp=10)
    _cast_ziyang(combat, player, monster, x=1)
    assert monster.has_status("滋养")
    monster.tick_status_effects()
    assert not monster.has_status("滋养")
    detail = state.apply_heal(monster, 10, ctx={"source": "测试", "mechanic": "heal"})
    assert "ziyang_doubled" not in detail
    assert detail["heal_amount"] == 10


def test_ziyang_stack_keeps_multiplier_at_two():
    """边界：同名重复施放不增强倍率（恒×2），只叠加持续时间——正文「状态不增强效果」"""
    state, combat, player, monster = _battle(m_hp=10)
    _cast_ziyang(combat, player, monster, x=1)
    _cast_ziyang(combat, player, monster, x=1)
    layers = [s for s in monster.status_effects if s.name == "滋养"]
    assert len(layers) == 1, "同名状态必须合并，不得叠成两条"
    detail = state.apply_heal(monster, 10, ctx={"source": "测试", "mechanic": "heal"})
    assert detail["heal_amount"] == 20, "两次滋养不得变成 ×4"


# ==================== 正文口径 ====================


def test_readme_declares_the_new_semantics():
    """口径：规则正文的两条声明行已换成新文案，旧文案不得残留"""
    txt = (ROOT / "AI_EXPERIENCE.md").read_text(encoding="utf-8")
    assert "自愈X（代价：冷却X：恢复[目标]25X%已损生命）→（转换）滋养X（消耗2X：使[目标]受到的恢复量翻倍，持续X）" in txt
    assert "代价：异变5X：[回始]获得自身[血限]10X%的[回复]" not in txt
    assert "使[目标]获得[血限]10X%的[回复]" not in txt
    # 组合与癌变口径必须在正文里写明（滋养的强度上限＝只有第一杯能免疫癌变）
    assert "滋养（×2）＋自愈2（已损生命50%）＝已损生命100%" in txt
    assert "能免疫癌变的有且只有遗物【第一杯】" in txt
