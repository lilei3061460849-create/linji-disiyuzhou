"""致死进度（【崩解】/【癌变】/【凡庸】）——用户令 2026-09-15：

「给致死的特殊事件标明进度，类似于崩解（10/50），让 AI 不要自爆」。

覆盖：
- 正常路径：进度串（崩解（10/50））与结构化计数同时可见；
- 边界条件：凡庸两条线取更接近触发的一条、血限为 0 时不产生癌变线、阈值恰好触发崩解；
- 错误输入：阈值唯一事实源——CombatEngine 的同名量必须引用 Entity，不得各写一份。
"""
import math
import os
import sys

from tests.setup_support import finish_initial_daowen

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine import battle_report as BR
from engine.combat import CombatEngine, MEDIOCRITY_ROUNDS
from engine.models import Entity


def _player(**kw):
    data = dict(name="测试者", entity_type="轮回者", blood_limit=42, current_hp=42)
    data.update(kw)
    return Entity(**data)


def test_progress_rendering_matches_rule_numbers():
    e = _player()
    e.mutation_count = 10
    e.total_healed = 30
    counters = e.lethal_counters()
    assert counters["崩解"] == (10, Entity.MUTATION_COLLAPSE_THRESHOLD) == (10, 50)
    assert counters["癌变"] == (30, math.ceil(42 * 2))
    assert e.lethal_progress() == ["崩解（10/50）", "癌变（30/84）"]


def test_mediocrity_shows_the_closer_line():
    e = _player()
    e.no_action_rounds, e.no_damage_rounds = 1, 4
    assert e.lethal_counters()["凡庸·未致敌掉血"] == (4, Entity.MEDIOCRITY_ROUNDS)
    assert "凡庸·未致敌掉血（4/5）" in e.lethal_progress()
    other = _player()
    other.no_action_rounds = 3
    assert "凡庸·未出手（3/5）" in other.lethal_progress()


def test_zero_blood_limit_has_no_cancer_line():
    e = _player(blood_limit=0, current_hp=0)
    assert "癌变" not in e.lethal_counters()


def test_to_dict_and_resource_line_carry_progress():
    e = _player()
    e.mutation_count = 45
    data = e.to_dict()
    assert data["lethal_progress"] == ["崩解（45/50）", "癌变（0/84）"]
    assert data["lethal_counters"]["崩解"] == [45, 50]
    assert data["mutation_count"] == 45
    assert "致死进度[崩解（45/50）" in BR.resource_line(e)


def test_thresholds_have_a_single_source():
    assert CombatEngine.PROLIFERATION_THRESHOLD == Entity.CANCER_HEAL_MULTIPLIER
    assert CombatEngine.CANCER_THRESHOLD == Entity.CANCER_HEAL_MULTIPLIER
    assert MEDIOCRITY_ROUNDS == Entity.MEDIOCRITY_ROUNDS == 5
    assert Entity.MUTATION_COLLAPSE_THRESHOLD == 50


def test_collapse_triggers_exactly_at_threshold():
    e = _player()
    e.add_mutation(49)
    assert e.is_alive is True
    assert e.add_mutation(1)["collapsed"] is True
    assert e.is_alive is False and e.current_hp == 0


def _new_engine(tmp_path, region="龙心谷"):
    e = GameEngine(db_path=str(tmp_path / "lethal.db"), rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": region})
    optional = {"三相残韵盘"}
    choice = next((name for name in setup["result"]["relic_choices"] if name not in optional),
                  setup["result"]["relic_choices"][0])
    e.execute_action("choose_discovered_relic", {"relic_name": choice})
    e.state.energy = 0
    return e


def test_monster_phase_payload_carries_monster_progress(tmp_path):
    """怪物同样会【崩解】：prepare_monster_phase 的每个 actor 必须带致死进度。"""
    e = _new_engine(tmp_path)
    e.execute_action("battle_start")
    e.execute_action("round_start", {})
    prepared = e.execute_action("prepare_monster_phase", {})
    assert prepared["success"], prepared
    actors = prepared["result"]["actors"]
    assert actors, "本回合应有可行动的怪物 actor"
    for actor in actors:
        assert actor["lethal_counters"]["崩解"] == [0, Entity.MUTATION_COLLAPSE_THRESHOLD]
        assert "崩解（0/50）" in actor["lethal_progress"]
