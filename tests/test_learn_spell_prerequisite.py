"""修复验证（2026-08-21）：学习法术必须校验前置道纹。

背景：_pre_battle_xuexi 只检查 SPELL_REGISTRY 名称，不校验 required_daowen，
导致玩家没有 庇护 却能学习 借力打力、没有 再生 却能学习 千刀万剐——
消耗局外资源却获得整局无法使用的死条目（实战3次确认）。
修复：学习法术时要求 required_daowen ⊆ 当前持有道纹；缺失则拒绝、
不扣碎片/精力，并明确列出缺失道纹。
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
        "name": "学者", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)  # 开局：仅持【杀伐】
    e.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.state.shards = 100
    e.state.energy = 3
    return e


def _snapshot(e):
    return (e.state.energy, e.state.shards, set(e.state.player.dao_wen),
            [s.name for s in e.state.player.spells])


def test_learn_rejected_when_prerequisite_missing(tmp_path):
    """缺少前置道纹 → 学习失败、不扣碎片/精力、不产生法术。"""
    e = _engine(tmp_path)
    before = _snapshot(e)
    r = e.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "spell", "tier": 1, "names": ["借力打力"]})
    assert not r.get("success"), "缺少前置道纹（庇护）应拒绝学习"
    assert "庇护" in r.get("error", ""), f"错误应列出缺失道纹：{r.get('error')}"
    assert _snapshot(e) == before, "学习失败不得扣碎片/精力/写入法术"


def test_learn_rejected_when_multiple_prerequisites_missing(tmp_path):
    """多个前置道纹缺失 → 正确列出全部缺失项。"""
    e = _engine(tmp_path)
    before = _snapshot(e)
    r = e.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "spell", "tier": 1, "names": ["千刀万剐"]})
    assert not r.get("success")
    assert "再生" in r.get("error", "") and "血债" in r.get("error", ""), \
        f"应列出再生与血债两个缺失道纹：{r.get('error')}"
    assert _snapshot(e) == before


def test_learn_succeeds_when_all_prerequisites_owned(tmp_path):
    """拥有全部前置道纹 → 正常学习。"""
    e = _engine(tmp_path)
    p = e.state.player
    # 先学习庇护（通用核心道纹，可直接学习）
    r = e.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "daowen", "tier": 1, "names": ["庇护"]})
    assert r.get("success"), r.get("error")
    e.state.energy = 3
    r = e.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "spell", "tier": 1, "names": ["借力打力"]})
    assert r.get("success"), f"拥有杀伐+庇护应可学习借力打力：{r.get('error')}"
    assert any(s.name == "借力打力" for s in p.spells)
    assert e.state.shards == 100, "一档法术学习不扣碎片"


def test_already_learned_spells_unaffected(tmp_path):
    """已学法术不受影响：先学后补前置，法术仍在且可用判定正确。"""
    e = _engine(tmp_path)
    p = e.state.player
    # 先学先发制人（只需杀伐，开局即持有）
    r = e.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "spell", "tier": 1, "names": ["先发制人"]})
    assert r.get("success"), r.get("error")
    assert any(s.name == "先发制人" for s in p.spells)
    # 补学庇护后，借力打力（杀伐+庇护）可正常学习
    e.state.energy = 3
    r = e.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "daowen", "tier": 1, "names": ["庇护"]})
    assert r.get("success")
    e.state.energy = 3
    r = e.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "spell", "tier": 1, "names": ["借力打力"]})
    assert r.get("success")
    names = [s.name for s in p.spells]
    assert "先发制人" in names and "借力打力" in names
