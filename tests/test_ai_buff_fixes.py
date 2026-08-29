"""
pytest - AI 战术表死码修复（2026-08-18）

修复背景：
1. try_buff 旧版只硬编码固执，导致全部 role=buff 道纹（切割/贯穿/增殖/活血/
   爆裂/超频/龙鳞/滑翔/分裂/招魂）授予后 0 发动（死码）。
2. try_debuff 的 X≥2 门槛对代价型道纹（cost≤0，_x_for 恒为1，如畸变/逆鳞）
   永不满足；衰败被错标为 nuke 且按默认 dmg_per_x=2 错价，从未被选中。
3. 尸爆被错标为即时 aoe（无 dmg_per_x → 总伤恒 0 被跳过），实为[命零]死亡触发。

覆盖：正常路径 / 边界条件 / 错误输入
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine  # noqa: E402
from engine.ai_tactics import TacticalAI  # noqa: E402
from engine.models import DaoWen, DaoWenInstance, Entity  # noqa: E402
from tests.setup_support import finish_initial_daowen  # noqa: E402


def _engine(tmp_path, seed=4):
    e = GameEngine(db_path=str(tmp_path / "buff_fix.db"), rng_seed=seed)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 7, "speed_points": 8, "mana_points": 10})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.state.energy = 0
    choices = {}
    if e.state.relics and e.state.relics[0].name in ("折速法印", "三相残韵盘"):
        choices[e.state.relics[0].name] = {"use": False}
    e.execute_action("battle_start", {"relic_choices": choices})
    e.execute_action("round_start", {})
    return e


def _give(entity, name, cost_type="消耗"):
    entity.dao_wen[name] = DaoWenInstance(DaoWen(
        name=name, formula="", cost_type=cost_type, cost_formula="X", effect_formula=""))


# ---------- 正常路径 ----------

def test_guanchuan_cast_when_enemy_shielded(tmp_path):
    """贯穿（buff）：敌方有格挡时应被发动——旧版死码0发动"""
    e = _engine(tmp_path)
    _give(e.state.player, "贯穿")
    e.state.enemies[0].shield = 10
    ai = TacticalAI(e)
    r = ai.try_buff()
    assert r is not None, "敌方有格挡时贯穿应被发动"
    assert ai.used.get(f"buff:贯穿:{e.state.current_battle}") == 1


def test_longlin_cast_early_for_permanent_dr(tmp_path):
    """龙鳞（buff）：法力足够时应尽早挂上永久减伤"""
    e = _engine(tmp_path)
    _give(e.state.player, "龙鳞")
    e.state.player.current_mana = 20
    ai = TacticalAI(e)
    r = ai.try_buff()
    assert r is not None, "法力充足时龙鳞应被发动"


def test_jibian_pay_type_debuff_casts_at_x1(tmp_path):
    """畸变（代价型debuff）：X≥2门槛应对代价型豁免，X=1即可发动"""
    e = _engine(tmp_path)
    _give(e.state.player, "畸变", cost_type="冷却")
    ai = TacticalAI(e)
    r = ai.try_debuff()
    assert r is not None, "代价型削弱应以X=1发动，不得被X≥2门槛卡死"


def test_shuaibai_reclassified_as_debuff_and_casts(tmp_path):
    """衰败：非伤害战术牌（预演归纳），法力≥15时应被发动"""
    e = _engine(tmp_path)
    _give(e.state.player, "衰败")
    e.state.player.current_mana = 20
    e.state.player.current_speed = 3  # 压低出手预算仍应够X=1
    ai = TacticalAI(e)
    r = ai.try_debuff()
    assert r is not None, "衰败X=1（回始扣20%当前生命）应被发动"


def test_shibao_classified_by_preview_not_by_label(tmp_path):
    """尸爆：类别由预演事实归纳（引擎结算即真理），不得靠人工标签查表。

    实测：该场面下尸爆X=1 触发 damage_applied 链（引擎事实），故归 damage；
    换场面后类别随真实效果自动变化——这正是"实时决策"的意义。
    """
    e = _engine(tmp_path)
    _give(e.state.player, "尸爆")
    ai = TacticalAI(e)
    probe = ai._probe("尸爆")
    assert probe is not None, "尸爆应可被预演归纳"
    assert probe["kind"] in ("damage", "tactician", "buff"), "分类须来自预演事件流"
    assert probe["cost_per_x"] == 10           # 法力单价同样来自预演（10/X）


# ---------- 边界条件 ----------

def test_guzhi_recastable_across_battles(tmp_path):
    """固执标记按场次隔离：上一场用过不封锁下一场"""
    e = _engine(tmp_path)
    _give(e.state.player, "固执", cost_type="冷却")
    ai = TacticalAI(e)
    ai.used[f"buff:固执:{e.state.current_battle - 1}"] = 1  # 模拟上一场已用
    r = ai.try_buff()
    assert r is not None, "固执的每场一次标记不得跨场封锁"


def test_buff_not_cast_without_enemies(tmp_path):
    """无敌人时不挂增益（不浪费出手）"""
    e = _engine(tmp_path)
    _give(e.state.player, "龙鳞")
    for m in e.state.enemies:
        m.is_alive = False
    ai = TacticalAI(e)
    assert ai.try_buff() is None


def test_buff_once_per_battle(tmp_path):
    """每张增益每场至多一次"""
    e = _engine(tmp_path)
    _give(e.state.player, "贯穿")
    e.state.enemies[0].shield = 10
    ai = TacticalAI(e)
    assert ai.try_buff() is not None
    e.state.enemies[0].shield = 10
    again = ai.try_buff()
    assert again is None or "贯穿" not in str(again), "同场不得重复挂贯穿"


# ---------- 错误输入 ----------

def test_unknown_daowen_probe_returns_none(tmp_path):
    """未持有/不存在的道纹预演归纳返回None，不抛异常"""
    e = _engine(tmp_path)
    ai = TacticalAI(e)
    assert ai._probe("不存在的道纹") is None
