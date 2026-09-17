"""装配法术必须校验前置道纹（2026-08-21 修复；2026-09-16 改口径）。

背景：_pre_battle_xuexi 只检查 SPELL_REGISTRY 名称，不校验 required_daowen，
导致玩家没有 庇护 却能学习 借力打力、没有 再生 却能学习 千刀万剐——
消耗局外资源却获得整局无法使用的死条目（实战3次确认）。
修复：校验 required_daowen ⊆ 当前持有道纹；缺失则拒绝并明确列出缺失道纹。

2026-09-16 用户裁定：法术不再需要【学习】，改为持有所需道纹即可装配
（use_spell）。前置道纹的校验本身不变，只是从局外【学习】迁移到装配口，
因此本文件继续钉住同一条规则：缺道纹就装不上，且不产生任何副作用。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from tests.setup_support import finish_initial_daowen


def _engine(tmp_path):
    e = GameEngine(db_path=str(tmp_path / "t.db"), rng_seed=7,
                   sealed_candidate_path=str(tmp_path / "s.json"))
    e.execute_action("setup_attributes", {
        "name": "学者", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)  # 开局：仅持【杀伐】
    e.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.state.shards = 100
    e.state.energy = 3
    # 装配合法发生己方行动阶段
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    return e


def _snapshot(e):
    return (e.state.energy, e.state.shards, set(e.state.player.dao_wen),
            list(e.state.player.armed_spells), [s.name for s in e.state.player.spells])


def test_arm_rejected_when_prerequisite_missing(tmp_path):
    """缺少前置道纹 → 装配失败、不扣资源、不写入装配槽。"""
    e = _engine(tmp_path)
    before = _snapshot(e)
    r = e.execute_action("use_spell", {"spell_name": "借力打力"})
    assert not r.get("success"), "缺少前置道纹（庇护）应拒绝装配"
    assert "庇护" in r.get("error", ""), f"错误应列出缺失道纹：{r.get('error')}"
    assert _snapshot(e) == before, "装配失败不得扣资源/写入装配槽"


def test_arm_rejected_when_multiple_prerequisites_missing(tmp_path):
    """多个前置道纹缺失 → 正确列出全部缺失项。"""
    e = _engine(tmp_path)
    before = _snapshot(e)
    r = e.execute_action("use_spell", {"spell_name": "千刀万剐"})
    assert not r.get("success")
    assert "再生" in r.get("error", "") and "血债" in r.get("error", ""), \
        f"应列出再生与血债两个缺失道纹：{r.get('error')}"
    assert _snapshot(e) == before


def test_arm_succeeds_when_all_prerequisites_owned(tmp_path):
    """拥有全部前置道纹 → 正常装配，且不消耗任何资源。"""
    e = _engine(tmp_path)
    p = e.state.player
    from engine.models import DaoWen, DaoWenInstance
    p.dao_wen["庇护"] = DaoWenInstance(
        DaoWen(name="庇护", formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
        x_value=1)
    r = e.execute_action("use_spell", {"spell_name": "借力打力"})
    assert r.get("success"), f"拥有杀伐+庇护应可装配借力打力：{r.get('error')}"
    assert "借力打力" in p.armed_spells
    assert e.state.shards == 100, "装配不扣碎片"
    assert e.state.energy == 3, "装配不扣精力"


def test_armed_spells_persist_and_can_be_disarmed(tmp_path):
    """已装配法术不受后续道纹变动之外的因素影响；可显式卸下。"""
    e = _engine(tmp_path)
    p = e.state.player
    r = e.execute_action("use_spell", {"spell_name": "先发制人"})  # 只需杀伐，开局即持有
    assert r.get("success"), r.get("error")
    assert "先发制人" in p.armed_spells
    # 重复装配无副作用
    e.execute_action("use_spell", {"spell_name": "先发制人"})
    assert p.armed_spells.count("先发制人") == 1
    # 卸下
    r = e.execute_action("use_spell", {"spell_name": "先发制人", "disarm": True})
    assert r.get("success"), r.get("error")
    assert "先发制人" not in p.armed_spells
    # 未装配时卸下应报错
    r = e.execute_action("use_spell", {"spell_name": "先发制人", "disarm": True})
    assert not r.get("success")


def test_learning_spell_sub_is_retired(tmp_path):
    """局外【学习】的 sub=spell 已随免学习裁定作废，并指向新入口。"""
    e = _engine(tmp_path)
    e.state.phase = "pre_battle"
    e.state.energy = 3
    before = _snapshot(e)
    r = e.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "spell", "tier": 1, "names": ["借力打力"]})
    assert not r.get("success")
    assert "define_spell" in r.get("error", "") or "无需学习" in r.get("error", ""), \
        f"错误应指向新入口：{r.get('error')}"
    assert _snapshot(e) == before, "作废入口不得产生任何效果"
