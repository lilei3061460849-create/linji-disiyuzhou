"""困境驱动回归（DM裁定2026-08-23③：模拟器怪物驱动执行准则#3强制二选一）。

裁定：怪物陷入困境（check_monster_difficulty ≥1 劣势信号）时——
  1. 进化优先：有借用票（轮回者持有、自身未持有的道纹）且原初X门票异变5X
     不必然崩解 → declare_evolution 借纹（X≤异变预算）；
  2. 无票或必崩解 → 逃跑（统一【离场】，不视为击杀）。
门禁：死斗不驱动；决斗敌方（轮回者，非怪物）永不驱动；每场战斗每怪限一次。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity
from sim.build_learner import _drive_plight_monsters
from tests.setup_support import finish_initial_daowen


def _engine(tmp_path, seed: int = 20260823) -> GameEngine:
    return GameEngine(
        db_path=str(tmp_path / "p.db"),
        save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        death_book_path=str(tmp_path / "death.md"),
        rng_seed=seed,
    )


def _dw(entity: Entity, name: str, x: int = 1) -> None:
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
        x_value=x)


def _combat(engine: GameEngine, monsters: list[Entity], player_daowen: dict) -> Entity:
    player = Entity("轮回者", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=100, current_mana=100, speed_limit=9, current_speed=9,
                    attack_count=1, attack_power=2)
    for name, x in player_daowen.items():
        _dw(player, name, x)
    engine.state.player = player
    engine.state.friends = []
    engine.state.employees = []
    engine.state.enemies = monsters
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.pending_monster_phase = {}
    engine.state.current_round = 2
    engine.combat.reset_monster_activation()   # 等价战始重置（绕过 battle_start 的夹具）
    return player


def _plight_monster(name: str = "困境兽", hp_ratio: float = 0.29) -> Entity:
    m = Entity(name, "怪物", blood_limit=100, current_hp=int(100 * hp_ratio),
               attack_count=1, attack_power=6)
    _dw(m, "狂暴", 2)   # 与玩家道纹错开，保证借用池非空
    return m


def test_plight_evolution_first_borrows_player_daowen(tmp_path):
    """困境+有票+预算足 → 进化：借玩家X最高的道纹，付异变5X，本场锁定。"""
    e = _engine(tmp_path)
    _full_ok(e)
    m = _plight_monster()
    _combat(e, [m], {"加害": 4, "自愈": 2})
    telemetry: dict = {}
    _drive_plight_monsters(e, telemetry)
    assert m.is_alive, "进化的怪物不应离场"
    assert "加害" in m.dao_wen, f"应借X最高的【加害】: {list(m.dao_wen)}"
    assert m.dao_wen["加害"].x_value == 3, "X=min(预算9, 3)=3"
    assert m.mutation_count == 15, f"门票异变=5X=15，实际{m.mutation_count}"
    assert telemetry["plight"]["evolve"] == 1
    assert id(m) in e.combat._monster_evolved
    # 每场限一次：再次驱动不得重复触发
    _drive_plight_monsters(e, telemetry)
    assert telemetry["plight"]["evolve"] == 1


def test_plight_escape_when_no_ticket(tmp_path):
    """玩家道纹怪物全持有（无借用票）→ 逃跑：离场不视为击杀。"""
    e = _engine(tmp_path)
    _full_ok(e)
    m = _plight_monster()
    _combat(e, [m], {"狂暴": 5})   # 怪物已持有狂暴 → borrowable 为空
    telemetry: dict = {}
    _drive_plight_monsters(e, telemetry)
    assert not m.is_alive and m.is_departed
    assert m.departure_reason == "逃跑"
    assert m.removed_without_kill is True
    assert telemetry["plight"]["escape"] == 1


def test_plight_escape_when_certain_collapse(tmp_path):
    """门票异变必崩解（max_x=0）→ 即使有票也逃跑。"""
    e = _engine(tmp_path)
    _full_ok(e)
    m = _plight_monster()
    m.mutation_count = 46   # (50-1-46)//5 = 0
    _combat(e, [m], {"加害": 4})
    telemetry: dict = {}
    _drive_plight_monsters(e, telemetry)
    assert not m.is_alive and m.departure_reason == "逃跑"
    assert m.mutation_count == 46, "逃跑不付异变"
    assert telemetry["plight"]["escape"] == 1


def test_plight_never_drives_non_monster(tmp_path):
    """决斗敌方是轮回者（entity_type≠怪物）：即使困境信号齐全也永不驱动。"""
    e = _engine(tmp_path)
    _full_ok(e)
    champ = Entity("卫冕冠军", "轮回者", blood_limit=100, current_hp=20,
                   attack_count=1, attack_power=0)
    _combat(e, [champ], {"加害": 4})
    telemetry: dict = {}
    _drive_plight_monsters(e, telemetry)
    assert champ.is_alive and not champ.dao_wen.get("加害")
    assert telemetry.get("plight", {}) == {}


def test_plight_no_drive_without_signals(tmp_path):
    """无困境信号的怪物不驱动（idle循环不得每回合清空怪物）。"""
    e = _engine(tmp_path)
    _full_ok(e)
    m = Entity("强壮兽", "怪物", blood_limit=100, current_hp=100,
               attack_count=2, attack_power=6)
    _dw(m, "狂暴", 2)
    _combat(e, [m], {"加害": 4})
    telemetry: dict = {}
    _drive_plight_monsters(e, telemetry)
    assert m.is_alive and not m.dao_wen.get("加害")
    assert telemetry.get("plight", {}) == {}


def _full_ok(engine: GameEngine) -> None:
    assert engine.execute_action("setup_attributes", {
        "name": "测试", "blood_points": 10, "speed_points": 8, "mana_points": 7})["success"]
    assert finish_initial_daowen(engine)["success"]
    assert engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})["success"]
    assert engine.execute_action("setup_choose_region", {"region": "龙心谷"})["success"]
