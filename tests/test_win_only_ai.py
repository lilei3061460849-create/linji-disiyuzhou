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
from sim.win_only_ai import WinOnlyAI

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
