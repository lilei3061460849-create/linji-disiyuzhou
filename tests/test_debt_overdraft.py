"""怪物碎片代价透支成负债（DM裁定2026-08-22 方案A）+ 还债触发阈值20 的回归锚点。

背景：引擎曾对所有碎片类代价做余额门禁——怪物碎片守恒≥0，【还债】
（README 519：负债≥20 触发，转为我方[员工]参战）在 20万+ 局实跑中零触发。
修复：仅怪物侧，真碎片轨道余额不足时不再拒绝发动，shards 扣成负数（负债）。
玩家/朋友/员工/api 侧仍维持"余额不足拒绝发动"。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import DaoWen, DaoWenInstance, Entity, GameState


def _make_combat(monster: Entity) -> CombatEngine:
    state = GameState()
    state.current_region = "罪孽都市"
    state.player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60)
    state.enemies.append(monster)
    return CombatEngine(state, DiceEngine())


def _xiaozai_monster(x: int = 1, shards: int = 0, fake: int = 0) -> Entity:
    m = Entity(name="刷卡怪", entity_type="怪物", blood_limit=100, current_hp=100,
               attack_count=1, attack_power=5, shards=shards, fake_shards=fake)
    m.dao_wen["消灾"] = DaoWenInstance(DaoWen(
        name="消灾", formula="消灾X", cost_type="碎片",
        cost_formula="50X假/5X真", effect_formula=""), x_value=x)
    return m


def test_monster_xiaozai_overdraws_real_shards_into_debt():
    """正常路径：怪物无碎片发动消灾——不再拒绝，真碎片扣成负债。"""
    combat = _make_combat(_xiaozai_monster(x=1, shards=0, fake=0))
    monster = combat.state.enemies[0]
    result = combat._resolve_monster_daowen_choice(
        monster, {"name": "消灾", "dodge": False, "blood_shadow": False}, {}, set(), {})
    assert result.get("daowen_activated") == "消灾" or "monster" in result
    assert monster.shards == -5, f"消灾X1 真碎片代价 5，0 余额透支应为 -5，实际 {monster.shards}"


def test_xiaozai_chain_reaches_debt_threshold_and_binds_at_settle():
    """链路：连续透支→负债≥20→回终结算触发还债→转为员工参战。"""
    combat = _make_combat(_xiaozai_monster(x=1, shards=0, fake=0))
    monster = combat.state.enemies[0]
    for _ in range(3):   # 透支序列：X1→-5，递增X3→-15(累-20)，X5→-25(累-45)
        combat._resolve_monster_daowen_choice(
            monster, {"name": "消灾", "dodge": False, "blood_shadow": False},
            {}, set(), {})
        combat._monster_round_used(monster).discard("消灾")  # 跨回合重置使用记录
    assert monster.shards <= -combat.DEBT_THRESHOLD
    results = combat.settle_victory_paths()
    assert any(r.get("type") == "debt_bind" for r in results), f"负债≥20应触发还债: {results}"
    assert monster.is_debt_bound is True
    assert monster.entity_type == "员工" and monster in combat.state.get_all_player_side()


def test_debt_trigger_boundary_19_not_bound_20_bound():
    """边界条件：负债19不触发、20正好触发（阈值含端点）。"""
    for shards, expect in ((-19, False), (-20, True)):
        combat = _make_combat(_xiaozai_monster(shards=shards))
        monster = combat.state.enemies[0]
        results = combat.settle_victory_paths()
        bound = any(r.get("type") == "debt_bind" for r in results)
        assert bound is expect, f"负债{-shards}触发期望{expect}，实际{bound}"


def test_fake_shards_still_preferred_no_debt_when_fake_sufficient():
    """反向：假碎片充足时优先扣假，不产生负债、不触发还债。"""
    combat = _make_combat(_xiaozai_monster(x=1, shards=3, fake=100))
    monster = combat.state.enemies[0]
    combat._resolve_monster_daowen_choice(
        monster, {"name": "消灾", "dodge": False, "blood_shadow": False}, {}, set(), {})
    assert monster.fake_shards == 50 and monster.shards == 3
    assert all(r.get("type") != "debt_bind" for r in combat.settle_victory_paths())
