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
    """特殊事件一律经 depart_battle 记为【离场】，命零/离场即不再阻塞战终"""
    st = _state_with_player()
    # 命零
    dead = _monster("怪·命零")
    dead.is_alive = False
    st.enemies = [dead]
    assert not st.enemy_combat_active(dead) and st.battle_won()
    # 各特殊事件 → 统一离场
    for reason in ("雕塑", "癌变", "还债", "救赎", "封印", "逃跑"):
        m = _monster(f"怪·{reason}")
        m.depart_battle(reason)
        st.enemies = [m]
        assert not st.enemy_combat_active(m), f"{reason} 离场后仍被判为战斗障碍"
        assert m.departure_reason == reason
        assert st.battle_won() and st.battle_over()


def test_future_special_event_needs_no_predicate_change():
    """扩展性：未来新增特殊事件只需调用 depart_battle，无须改动任何判定"""
    st = _state_with_player()
    m = _monster("怪·未来事件")
    m.depart_battle("某个尚未发明的特殊事件")
    st.enemies = [m]
    assert not st.enemy_combat_active(m)
    assert st.battle_won()
    assert m.removed_without_kill, "离场必须兼容旧字段：不视为击杀、不产碎片"


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
    """边界：还债瞬间（is_alive 仍为 True、以员工身份继续参战）也不阻塞战终"""
    st = _state_with_player()
    m = _monster("过渡怪")
    m.is_debt_bound = True
    m.is_departed = True          # 还债路径只置离场标记，不置 is_alive=False
    m.departure_reason = "还债"
    st.enemies = [m]
    assert m.is_alive, "还债者以员工身份继续参战"
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
