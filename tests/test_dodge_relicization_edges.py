"""回避触发型道纹遗物的边界回归。

覆盖【窥隙法匣】的延迟法力与失去遗物后的待领取清理，以及【逐影胫骨】
和原道纹【急速】同时存在时各自独立的闪避计数器。测试直接走 CombatEngine
的成功闪避后处理与回始入口，不通过 Hook 单测替代正式分发路径。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Relic, StatusEffect
from tests.setup_support import finish_initial_daowen


def _engine(tmp_path):
    e = GameEngine(db_path=str(tmp_path / "t.db"), rng_seed=7,
                   sealed_candidate_path=str(tmp_path / "s.json"))
    e.execute_action("setup_attributes", {
        "name": "回避测试者", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    # 清掉开局随机遗物，避免无关被动影响回始和速度结算。
    e.state.relics.clear()
    p = e.state.player
    p.dao_wen["杀伐"] = DaoWenInstance(
        DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""), x_value=0)
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    e.state.current_round = 2
    return e


def test_kuixi_mana_is_delayed_until_next_round_start(tmp_path):
    e = _engine(tmp_path)
    p = e.state.player
    e.state.relics.append(Relic(name="窥隙法匣", effect="", tags=[]))
    p.current_mana = 0
    p.mana_limit = 30

    result = e.combat._note_dodge(p)

    assert result["kuixi_pending_mana"] == 10
    assert p.current_mana == 0, "成功闪避当下不应立即获得法力"
    assert p._kuixi_pending_mana == 10

    e.combat.round_start()

    assert p.current_mana == 10, "下一回始应领取10点法力"
    assert p._kuixi_pending_mana == 0


def test_kuixi_stacks_successful_dodges_and_clears_when_relic_removed(tmp_path):
    e = _engine(tmp_path)
    p = e.state.player
    relic = Relic(name="窥隙法匣", effect="", tags=[])
    e.state.relics.append(relic)
    p.current_mana = 0
    p.mana_limit = 40

    e.combat._note_dodge(p)
    e.combat._note_dodge(p)
    assert p._kuixi_pending_mana == 20
    assert p.current_mana == 0

    e.state.relics.remove(relic)
    e.combat.round_start()

    assert p.current_mana == 0, "移除遗物后旧待领取法力不能生效"
    assert p._kuixi_pending_mana == 0, "失效的待领取额度应被清除"


def test_zhuying_and_jisu_keep_independent_dodge_counters(tmp_path):
    e = _engine(tmp_path)
    p = e.state.player
    p.current_speed = 1
    p.speed_limit = 20
    p.add_status(StatusEffect(name="急速", value=1, remaining_rounds=5, source="test"))
    e.state.relics.append(Relic(name="逐影胫骨", effect="", tags=[]))

    first = e.combat._note_dodge(p)
    assert first.get("jisu_speed") is None
    assert first.get("zhuying_speed") is None
    assert p._jisu_dodges == 1
    assert p._zhuying_dodges == 1
    assert p.current_speed == 1

    second = e.combat._note_dodge(p)
    assert second["jisu_speed"] == 1
    assert second["zhuying_speed"] == 1
    assert p._jisu_dodges == 0
    assert p._zhuying_dodges == 0
    assert p.current_speed == 3, "两种效果分别触发，不能互相吞掉计数"

    third = e.combat._note_dodge(p)
    assert third.get("jisu_speed") is None
    assert third.get("zhuying_speed") is None
    assert p._jisu_dodges == 1
    assert p._zhuying_dodges == 1
    assert p.current_speed == 3

    fourth = e.combat._note_dodge(p)
    assert fourth["jisu_speed"] == 1
    assert fourth["zhuying_speed"] == 1
    assert p._jisu_dodges == 0
    assert p._zhuying_dodges == 0
    assert p.current_speed == 5
