"""普攻作为常驻 AI 候选（DM裁定 2026-09-09，实验旗标 LJ_AI_BASIC_ATTACK=1）。

裁定背景：法力改一池制后不再每回合回填，只靠法力型道纹会在池子花干后无事可做。
实测同批种子（0-99，扭曲都市 7 场）：
  · 一池制、AI 无普攻：通关 0/100，平均经历 0.87 场
  · 一池制、AI 有普攻：通关 0/100，平均经历 1.02 场（1×1 普攻只打 1 点，救不动）
  · 再加攻次/攻力加点 + 修行买攻：普攻武斗通关 1/100，平均经历 4.26 场

普攻是两段动作（prepare_attack → resolve_attack，resolve 需要 prepare 给的 token），
所以预演器扩成能吃动作序列，AI 才能看见普攻的真实伤害并参与打分。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from engine.ai_tactics import TacticalAI  # noqa: E402
from engine.api import GameEngine  # noqa: E402
from tests.setup_support import (  # noqa: E402
    begin_battle, begin_round, finish_initial_daowen,
)

FLAG = "LJ_AI_BASIC_ATTACK"


@pytest.fixture
def engine(tmp_path):
    e = GameEngine(db_path=str(tmp_path / "ai_ba.db"), rng_seed=7,
                   sealed_candidate_path=str(tmp_path / "ai_ba.json"))
    e.execute_action("setup_attributes", {"name": "白某", "blood_points": 6,
                                          "speed_points": 8, "mana_points": 5,
                                          "attack_count_points": 3,
                                          "attack_power_points": 3})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "曲解"})
    e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    begin_battle(e)
    begin_round(e)
    return e


def test_flag_off_yields_no_basic_attack_candidate(engine, monkeypatch):
    """错误输入/对照：旗标关闭时行为与改动前一致——不产生普攻候选。"""
    monkeypatch.delenv(FLAG, raising=False)
    ai = TacticalAI(engine)
    assert ai._basic_attack_candidates() == []
    assert engine.state.player.attack_count == 4   # 1 初始 + 3 加点


def test_flag_on_yields_one_candidate_per_live_enemy(engine, monkeypatch):
    """正常路径：旗标开启后每个存活敌人各一个普攻候选。"""
    monkeypatch.setenv(FLAG, "1")
    ai = TacticalAI(engine)
    cands = ai._basic_attack_candidates()
    assert len(cands) == len(ai.alive_enemies()) >= 1
    c = cands[0]
    assert c["kind"] == "attack" and c["label"].startswith("普攻→")
    assert [step[0] for step in c["steps"]] == ["prepare_attack", "resolve_attack"]


def test_preview_sequence_sees_real_attack_damage(engine, monkeypatch):
    """正常路径：预演整串跑完，打分看见真实伤害（攻次4×攻力4=16，未被闪避时）。"""
    monkeypatch.setenv(FLAG, "1")
    ai = TacticalAI(engine)
    foe = ai.alive_enemies()[0]
    foe.current_speed = 0                 # 速度0 → 不能闪避 → 伤害确定
    pv = ai.previewer.preview_sequence(ai._attack_steps(foe.name))
    assert all(r.get("success") for r in pv["results"]), pv["results"]
    hp = next(e for e in pv["diff"]["enemies"] if e["name"] == foe.name)
    assert hp["hp_before"] - hp["hp_after"] == 16, hp
    # 真实状态分毫不动（预演是副本执行）
    assert foe.current_hp == hp["hp_before"]


def test_ai_falls_back_to_basic_attack_when_pool_is_dry(engine, monkeypatch):
    """正常路径：法力花干后 AI 仍有输出手段——普攻兜住这一手。"""
    monkeypatch.setenv(FLAG, "1")
    ai = TacticalAI(engine, verbose=True)
    foe = ai.alive_enemies()[0]
    foe.current_speed = 0
    engine.state.player.current_mana = 0          # 一池制下花干就是花干
    hp_before = foe.current_hp
    results = ai.take_turn()
    assert results, "法力为0时不应整回合空转"
    assert foe.current_hp < hp_before, f"应打出普攻伤害：{foe.current_hp} vs {hp_before}"
    assert any("普攻" in line for line in ai.log), ai.log


def test_at_one_by_one_daowen_still_wins(engine, monkeypatch):
    """边界：普攻是候选不是覆盖——1×1 面板下（每手 1 伤）杀伐仍然优先。

    实测打分：杀伐X=7 → 17.92，普攻（1伤）→ 1.40。
    """
    monkeypatch.setenv(FLAG, "1")
    p = engine.state.player
    p.attack_count, p.attack_power = 1, 1
    ai = TacticalAI(engine, verbose=True)
    ai.take_turn()
    decisions = [line for line in ai.log if "实时决策" in line]
    assert decisions and all("普攻" not in line for line in decisions), decisions


def test_invested_basic_attack_outscores_shaifa(engine, monkeypatch):
    """记录新规则下的真实取舍（不是断言它「应该」如此）：攻次4×攻力4 的普攻
    每手 16 伤且不耗法力，打分 22.40 压过杀伐（14 伤 / 7 法力），AI 会整回合普攻。

    这是 DM 需要知道的平衡后果：投了攻次/攻力之后，法力池在纯输出上不再是必需品。
    """
    monkeypatch.setenv(FLAG, "1")
    ai = TacticalAI(engine, verbose=True)
    ai.take_turn()
    decisions = [line for line in ai.log if "实时决策" in line]
    assert decisions and all("普攻" in line for line in decisions), decisions
    assert engine.state.player.current_mana == engine.state.player.mana_limit, \
        "普攻不耗法力，池子应分毫未动"
