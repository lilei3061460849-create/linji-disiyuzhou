"""胜负唯一计分 AI（WinOnlyAI）守卫测试。

用户裁定 2026-09-10：胜 +1 / 败 −1，无论什么手段什么战术。
WinOnlyAI 用「整局推演的结局 ±1」做最终裁决（启发式只裁剪提案）。
本文件守住两件事：
1. 推演世界切换不泄漏——同 seed 复跑两次逐字节一致（世界换回不干净会立刻发散）；
2. 结构性结论在 ±1 口径下复现——正席 26 血方胜、换席 4 血方负。
"""
import hashlib
import os

import sim.duel_chronicle as dc
from sim.win_only_ai import WinOnlyAI, win_only_cls

W = dc.WINNER_DIR
CH_FULL = os.path.join(W, "普攻武斗_69.json")    # 26血/速12/法12
DF_FULL = os.path.join(W, "普攻武斗_429.json")   # 4血/速10/法16


def _render_md5(rec: dict) -> str:
    return hashlib.md5(dc.render(rec).encode()).hexdigest()


def test_win_only_world_swap_no_leak_same_seed_identical():
    """同 seed 复跑两次：判定+逐手实录逐字节一致（推演世界换回不净即发散）。"""
    r1 = dc.chronicle(CH_FULL, DF_FULL, 1, ai_cls=WinOnlyAI)
    r2 = dc.chronicle(CH_FULL, DF_FULL, 1, ai_cls=WinOnlyAI)
    assert r1["verdict"] == r2["verdict"]
    assert _render_md5(r1) == _render_md5(r2)


def test_win_only_strutsural_verdicts_both_seats():
    """±1 口径下结构性结论复现：26 血方正席胜，换席 4 血方负。"""
    v = dc.chronicle(CH_FULL, DF_FULL, 1, ai_cls=WinOnlyAI)["verdict"]
    assert v["winner"] == "challenger"
    v2 = dc.chronicle(DF_FULL, CH_FULL, 1, ai_cls=WinOnlyAI)["verdict"]
    assert v2["winner"] == "defender"


def test_win_only_is_run_duel_pvp_default():
    """用户裁定 2026-09-10：胜负唯一计分是死斗默认口径（不许悄悄改回去）。"""
    from sim.duel_pvp import _default_ai_cls
    assert _default_ai_cls() is WinOnlyAI


def test_win_only_is_pve_default_everywhere():
    """用户裁定二审：一切打分都用胜负唯一计分——PvE 默认也是 WinOnlyAI。"""
    import inspect
    from sim import build_learner as bl
    src = inspect.getsource(bl._play)
    assert "WinOnlyAI" in src          # 默认类落点在 _play
    from sim.duel_pvp import _default_ai_cls
    assert _default_ai_cls() is WinOnlyAI
    # 混合类：遗言桥为提案器、±1 为裁决（MRO 保证裁决覆写提案器口径）
    from sim.legacy_mentor import LegacyAwareAI
    hybrid = win_only_cls(LegacyAwareAI)
    assert issubclass(hybrid, WinOnlyAI) and issubclass(hybrid, LegacyAwareAI)


def test_win_only_pve_playout_signs():
    """PvE 推演判分：残局收割=+1（世界换回不净会污染第二段）。"""
    import shutil
    import tempfile
    from engine.api import GameEngine
    from tests.setup_support import finish_initial_daowen
    from sim.optional_actions import start_battle, start_round

    tmp_seal = tempfile.mktemp(suffix=".json")
    shutil.copy(os.path.join(W, "普攻武斗_69.json"), tmp_seal)
    e = GameEngine(db_path=tempfile.mktemp(suffix=".db"), rng_seed=7,
                   sealed_candidate_path=tmp_seal, death_book_path=dc.CANON_BOOK)
    e.execute_action("setup_attributes", {"name": "探针", "blood_points": 3,
                                          "speed_points": 10, "mana_points": 12})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.energy = 0
    start_battle(e)
    start_round(e)
    ai = WinOnlyAI(e)
    cand = ai._basic_attack_candidates()[0]
    for m in e.state.enemies:
        m.current_hp = 1
    assert ai._playout_score(cand) == 1      # 收割残局 → 胜 +1
    for m in e.state.enemies:
        m.current_hp = m.blood_limit          # 满血墙 → 推不出胜负/被磨死 ≠ +1
    assert ai._playout_score(cand) != 1
