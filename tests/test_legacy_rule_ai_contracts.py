"""显式 legacy 覆盖：实验规则 AI（TacticalAI / WinOnlyAI）的陈旧候选排序断言。

2026-10-02 架构收口（"正式 LLM 决策与 TacticalAI 完全解耦"）时对全仓剩余的
3 条失败测试做了定性：

  * tests/test_ai_tactics.py::test_ai_can_declare_parry_under_lethal_threat
  * tests/test_win_only_ai.py::test_win_only_includes_parry_in_real_candidate_path
  * tests/test_ai_basic_attack_candidate.py::test_at_one_by_one_daowen_still_wins

结论：**它们只锁实验规则 AI 的候选排序/偏好，与正式 LLM 决策路径无关**，
且断言口径在两次规则/机制改动后已失效——

  1. 2026-09-10「攻次/攻力改制」：普攻伤害 = 攻击次数×攻击力，攻力=当前法力。
     旧断言里"杀伐X=7 得 17.92 分"的前提（X=7 可负担）在 1×1 面板下不成立。
  2. 2026-09-28「招架重写」：减免 = floor(10%当前生命)/每击、次数=当前生命。
     10 生命面对 12 伤害时招架只能减 1 点、救不了命，
     ``_parry_candidate()`` 会**故意拒绝**这种无收益招架（threat-reduction > hp）。

按用户令「不得为保住陈旧测试改战斗语义」，这 3 条原断言以 xfail（strict=True）
留档：规则 AI 排序再次漂移时仍可被看见，但不会把实验工具的偏好伪装成生产契约。
下方同时断言**当前真实生效**的契约（这些必须通过），避免本文件只是一座墓园：

  * 引擎层：招架动作出现在运行时 available_actions 里，减免=floor(10%当前生命)，
    已声明的回合不能再声明，且**不读**已废弃的 parry_locked_this_round；
  * 规则 AI 层：招架候选在"能救命"的局面下仍会被生成；
    WinOnlyAI 的候选构建入口没有漏掉基类招架候选；
    ``remaining_actions()`` 读 action_count 而不是写死 2。
"""
from __future__ import annotations

import inspect

import pytest

from tests.test_ai_tactics import _engine


def _lethal_engine(tmp_path, hp=10, attack_power=12):
    e = _engine(tmp_path, learn=())
    e.state.player.dao_wen.clear()
    e.state.resonance.clear()
    e.state.player.current_hp = hp
    e.state.enemies[0].attack_count = 1
    e.state.enemies[0].attack_power = attack_power
    return e


# ---------------------------------------------------------------------------
# 一、留档的陈旧断言（xfail：记录实验规则 AI 排序的历史口径）
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="2026-09-28 招架改为 10%当前生命后，"
                                        "10 生命面 12 伤害时招架救不了命，规则 AI 不再优先它")
def test_legacy_rule_ai_prefers_parry_under_lethal_threat(tmp_path):
    from engine.ai_tactics import TacticalAI

    e = _lethal_engine(tmp_path)
    result = TacticalAI(e).take_action()
    assert result is not None and result.get("success"), result
    assert result.get("action") == "贾凡招架"


@pytest.mark.xfail(strict=True, reason="同上：WinOnlyAI 继承同一套招架候选过滤")
def test_legacy_win_only_keeps_parry_in_real_candidate_path(tmp_path):
    from sim.win_only_ai import WinOnlyAI

    e = _lethal_engine(tmp_path)
    result = WinOnlyAI(e).take_action()
    assert result is not None and result.get("success"), result
    assert result.get("action") == "贾凡招架"


@pytest.mark.xfail(strict=True, reason="2026-09-10 攻次/攻力改制后，1×1 面板下"
                                        "普攻（免费1伤）与杀伐（1法力1伤）的真实取舍已改变")
def test_legacy_one_by_one_daowen_outscores_basic_attack(tmp_path):
    from engine.ai_tactics import TacticalAI

    e = _engine(tmp_path, learn=("杀伐",))
    p = e.state.player
    p.current_speed, p.current_mana = 1, 1
    ai = TacticalAI(e, verbose=True)
    ai.take_turn()
    decisions = [line for line in ai.log if "实时决策" in line]
    assert decisions and all("普攻" not in line for line in decisions), decisions


# ---------------------------------------------------------------------------
# 二、当前生效的契约（必须通过）
# ---------------------------------------------------------------------------

def _declare_parry_action(engine):
    actions = engine.get_available_actions().get("actions", [])
    return next(a for a in actions if a.get("action_type") == "declare_parry")


def test_current_parry_contract_is_exposed_and_uses_ten_percent_current_hp(tmp_path):
    e = _lethal_engine(tmp_path, hp=40, attack_power=20)
    action = _declare_parry_action(e)
    assert action["available"] is True, action

    r = e.execute_action("declare_parry", {})
    assert r["success"], r
    assert r["result"]["reduction_preview"] == 40 // 10
    assert r["result"]["uses"] == 40
    assert e.state.player.parrying_this_round is True
    # 同一回合重复声明被拒（现行规则），错误信息与旧"回合锁"无关
    again = e.execute_action("declare_parry", {})
    assert again["success"] is False
    assert "已处于招架姿态" in again["error"]


def test_current_parry_availability_ignores_deprecated_turn_lock_field(tmp_path):
    e = _lethal_engine(tmp_path, hp=40, attack_power=20)
    e.state.player.parry_locked_this_round = True   # 旧字段：已废弃，不再参与门禁
    e.state.player.parrying_this_round = False
    assert _declare_parry_action(e)["available"] is True
    e.state.player.parrying_this_round = True
    assert _declare_parry_action(e)["available"] is False


def test_current_rule_ai_still_generates_parry_candidate_when_it_can_save_lives(tmp_path):
    from engine.ai_tactics import TacticalAI

    e = _lethal_engine(tmp_path, hp=40, attack_power=20)
    ai = TacticalAI(e)
    cand = ai._parry_candidate()
    assert cand is not None and cand["action"] == "declare_parry"
    assert cand["expected_reduction"] == 40 // 10
    result = ai.take_action()
    assert result is not None and result.get("success"), result


def test_win_only_candidate_builder_keeps_base_parry_candidate():
    """WinOnlyAI 覆写候选入口时不得漏掉基类招架候选（源码级结构检查）。"""
    from sim.win_only_ai import WinOnlyAI

    src = inspect.getsource(WinOnlyAI._dynamic_action)
    assert "_parry_candidate()" in src


def test_rule_ai_budget_reads_action_count_not_hardcoded_two(tmp_path):
    """实验规则 AI 的出手预算读 action_count（可被 蓄锐·增/疯狂/无力 修正）。"""
    from engine.ai_tactics import TacticalAI

    e = _lethal_engine(tmp_path)
    stub = type("_Stub", (), {"action_count": 5, "actions_used_this_round": 1,
                              "current_mana": 0})()
    ai = TacticalAI(e, actor=stub)
    assert ai.remaining_actions() == 4
