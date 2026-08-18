"""战斗结束与胜利·统一判定（DM裁定 2026-08-18）

对应 README §二「战斗结束与胜利判定」：
  胜利＝敌方全部经由七条路径之一移出战场（命零/救赎/雕塑/癌变/还债/封印/逃跑）；
  失败＝轮回者[命零]（任何死因）。
引擎内所有"能否战终/胜负已定"判断必须走 GameState.battle_won/battle_lost/battle_over，
本文件覆盖：正常路径 / 边界条件 / 错误输入。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine  # noqa: E402
from engine.models import Entity, GameState  # noqa: E402
from tests.setup_support import finish_initial_daowen  # noqa: E402

DB_DIR = "/tmp/linji_tests"
os.makedirs(DB_DIR, exist_ok=True)


def _monster(name="怪", hp=100, **kw):
    return Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
                  attack_count=1, attack_power=5, **kw)


def _state_with_player():
    st = GameState()
    st.player = Entity(name="贾凡", entity_type="轮回者",
                       blood_limit=60, current_hp=60)
    return st


# ---------- 正常路径 ----------

def test_each_removal_path_deactivates_enemy():
    """七条移出路径任一命中，敌人即不再阻塞战终"""
    st = _state_with_player()
    cases = {
        "命零": dict(is_alive=False),
        "雕塑": dict(is_sculptured=True),
        "癌变": dict(is_proliferated=True),
        "还债": dict(is_debt_bound=True),
        "封印/逃跑/离场": dict(removed_without_kill=True),
    }
    for path, flags in cases.items():
        m = _monster(f"怪·{path}")
        for k, v in flags.items():
            setattr(m, k, v)
        st.enemies = [m]
        assert not st.enemy_combat_active(m), f"{path} 后仍被判为战斗障碍"
        assert st.battle_won(), f"{path} 后 battle_won 应为 True"
        assert st.battle_over()


def test_active_enemy_blocks_victory_and_battle_end():
    """存活且未移出的敌人阻塞胜利与战终 action"""
    e = GameEngine(db_path=f"{DB_DIR}/victory_pred.db", rng_seed=3)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": "罪孽都市"})
    e.state.energy = 0
    e.execute_action("battle_start", {"relic_choices": {}})
    assert e.state.active_enemies(), "战始后应有活跃敌人"
    assert not e.state.battle_won()
    blocked = e.execute_action("battle_end", {})
    assert not blocked["success"] and "存活敌人" in blocked["error"]
    # 全部命零后战终放行
    for m in e.state.enemies:
        m.current_hp = 0
        m.is_alive = False
    assert e.state.battle_won()
    assert e.execute_action("battle_end", {})["success"]


def test_battle_lost_on_player_death_any_cause():
    """轮回者命零即失败，与敌方状态无关"""
    st = _state_with_player()
    st.enemies = [_monster()]
    assert not st.battle_over()
    st.player.current_hp = 0
    st.player.is_alive = False
    assert st.battle_lost() and st.battle_over()
    assert not st.battle_won(), "敌人仍活跃时不得同时判胜"


# ---------- 边界条件 ----------

def test_flagged_but_alive_enemy_does_not_block():
    """边界：标志已置但 is_alive 尚为 True（如还债转员工瞬间）也不阻塞战终"""
    st = _state_with_player()
    m = _monster("过渡怪")
    m.is_debt_bound = True  # is_alive 仍为 True
    st.enemies = [m]
    assert not st.enemy_combat_active(m)
    assert st.battle_won()


def test_mixed_enemies_partial_removal_not_victory():
    """边界：多敌人只移出一部分不算胜利"""
    st = _state_with_player()
    a, b = _monster("甲"), _monster("乙")
    a.is_sculptured = True
    a.is_alive = False
    st.enemies = [a, b]
    assert st.active_enemies() == [b]
    assert not st.battle_won() and not st.battle_over()


def test_no_player_counts_as_lost():
    """边界：player 为 None（轮回结束态）视为失败侧，battle_over 为真"""
    st = GameState()
    st.enemies = [_monster()]
    assert st.battle_lost() and st.battle_over()


# ---------- 错误输入 / 口径一致性 ----------

def test_no_stray_victory_predicates_in_engine():
    """静态：engine/ 内不得再出现散落的战终口径（is_alive 与移出标志的手写组合）"""
    import pathlib
    import re
    offenders = []
    pattern = re.compile(r"is_alive and not .*removed_without_kill")
    for p in pathlib.Path("engine").rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line) and "enemy_combat_active" not in text[:1]:
                # 允许唯一出处：models.py 的统一判定本体
                if p.name != "models.py":
                    offenders.append(f"{p}:{i}")
    assert offenders == [], f"发现绕过统一判定的手写口径: {offenders}"
