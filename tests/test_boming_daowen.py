"""【搏命】道纹：代价：疲惫X → 获得X点法力（扭曲都市专属，超频的反转）。

意境（用户裁定 2026-09-13）：放弃闪避、拼死一搏——把速度（既是[攻击次数]
也是闪避资源）榨成法力。

倍率被【超频】(消耗2X法力 → 速度+X) 反向锁死：设搏命倍率为 k（疲惫X→kX法力），
则卖 X 速度得 kX 法力，经超频可买回 kX/2 速度，净变化 X(k/2 - 1)。
k≥2 时净值≥0，两者串成无限循环使速度/法力永动暴涨；故 k 必须 < 2，取 k=1。

（遗物【折速法印】是 6X，之所以安全是因为[战始]一次性、不可反复发动；
搏命作为可反复发动的道纹必须砍到 1X。）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.daowen import DaoWenEngine, ResonanceEngine


def test_boming_trades_speed_for_mana_at_one_to_one():
    """正常路径：疲惫X换X法力，1:1。"""
    DaoWenEngine.register_all()
    calc = DaoWenEngine.resolve("搏命", 3, target=None, caster=None)
    assert calc["cost_type"] == "疲惫"
    assert calc["cost_speed"] == 3
    assert calc["mana_gain"] == 3


def test_boming_cannot_outpace_chaopin():
    """核心约束：搏命+超频的闭环必须净亏速度，不得永动。

    这是【搏命】倍率不能高于1的唯一理由，改倍率前先看这条。
    """
    DaoWenEngine.register_all()
    for x in (1, 2, 4, 8):
        sold = DaoWenEngine.resolve("搏命", x, target=None, caster=None)
        mana = sold["mana_gain"]
        # 用拿到的法力经【超频】尽可能买回速度
        bought = 0
        while DaoWenEngine.resolve("超频", bought + 1, target=None,
                                   caster=None)["cost"] <= mana:
            bought += 1
        net = bought - sold["cost_speed"]
        assert net < 0, (f"搏命{x}卖{sold['cost_speed']}速度换{mana}法力，"
                         f"经超频买回{bought}速度，净{net:+d}——非负即可无限永动")


def test_boming_is_chaopin_reversal_in_closed_loop():
    """残韵链：搏命是超频的【反转】，且扭曲都市闭环仍然合拢。"""
    paths = ResonanceEngine.get_available_resonance("超频")
    assert any(p["resonance_type"] == "反转" and p["target_daowen"] == "搏命"
               for p in paths), paths

    edges = ResonanceEngine.CLOSED_LOOPS["扭曲都市闭环"]
    assert len(edges) == 8
    srcs = sorted(s for s, _, _ in edges)
    dsts = sorted(d for _, _, d in edges)
    assert srcs == dsts, "闭环未合拢"
    assert "搏命" in srcs
    assert "僵化" not in srcs, "【僵化】已被【搏命】取代"


def test_jianghua_is_fully_removed():
    """【僵化】已删除，不得残留在道纹注册表里。"""
    DaoWenEngine.register_all()
    assert "僵化" not in DaoWenEngine.list_all()
    assert "搏命" in DaoWenEngine.list_all()
