"""癌变怪物吸收与《死者之书》永久休整+8的契约测试。"""
import json

from engine.api import GameEngine
from engine.enums import CombatSubphase
from engine.models import Entity


def _engine(tmp_path) -> GameEngine:
    return GameEngine(
        db_path=str(tmp_path / "rulings.db"),
        save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        death_book_path=str(tmp_path / "death.md"),
        rng_seed=19,
    )


def _setup_player(engine: GameEngine) -> Entity:
    result = engine.execute_action("setup_attributes", {
        "name": "测试者", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    assert result["success"]
    return engine.state.player


def _cancer_monster(engine: GameEngine, name: str = "癌变怪") -> tuple[Entity, dict]:
    monster = Entity(name, "怪物", blood_limit=100, current_hp=1,
                     attack_count=1, attack_power=1)
    engine.state.enemies.append(monster)
    monster.heal(250)
    result = engine.combat.check_cancer(monster)
    assert result is not None
    return monster, result


def test_monster_cancer_adds_real_stackable_rest_bonus(tmp_path):
    """正常路径：每吸收一只怪物永久+8，两只叠加为+16并真实进入休整公式。"""
    engine = _engine(tmp_path)
    player = _setup_player(engine)
    first, first_result = _cancer_monster(engine, "甲")
    second, second_result = _cancer_monster(engine, "乙")

    assert not first.is_alive and not second.is_alive
    assert engine.state.rest_heal_bonus == 16
    assert first_result["rest_heal_bonus_total"] == 8
    assert second_result["rest_heal_bonus_total"] == 16

    engine.state.phase = "pre_battle"
    player.current_hp = 1
    before_cancer = player.total_healed
    rest = engine.execute_action("pre_battle_action", {"sub_action": "休整", "tier": 1})
    assert rest["success"]
    assert rest["result"]["base_heal_amount"] == 8
    assert rest["result"]["rest_heal_bonus"] == 16
    assert rest["result"]["heal_amount"] == 24
    assert player.current_hp == 25
    assert player.total_healed == before_cancer, "局外休整不得计入本场癌变累计"


def test_cancer_rest_bonus_applies_to_every_rest_tier(tmp_path):
    """边界：同一永久加成加到全部档位，不按档位倍增。"""
    engine = _engine(tmp_path)
    player = _setup_player(engine)
    engine.state.phase = "pre_battle"
    engine.state.rest_heal_bonus = 8
    engine.state.shards = 100

    expected = {1: 16, 2: 32, 3: 56}
    for tier, total in expected.items():
        player.current_hp = 1
        engine.state.energy = 3
        result = engine.execute_action("pre_battle_action", {"sub_action": "休整", "tier": tier})
        assert result["success"] and result["result"]["heal_amount"] == total


def test_character_cancer_does_not_strengthen_rest(tmp_path):
    """边界：轮回者/同伴癌变直接命零，不进入怪物吸收奖励。"""
    engine = _engine(tmp_path)
    player = _setup_player(engine)
    player.total_healed = engine.combat.cancer_threshold_of(player)

    result = engine.combat.check_cancer(player)

    assert result["type"] == "cancer" and not player.is_alive
    assert engine.state.rest_heal_bonus == 0
    assert engine.state.death_book_wisdom == []


def test_cancer_monster_gives_no_shards_and_bonus_survives_new_cycle(tmp_path):
    """正常路径：癌变怪物不产碎片；永久加成跨轮回状态重置保留。"""
    engine = _engine(tmp_path)
    _setup_player(engine)
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
    engine.state.current_battle = 1
    monster, _ = _cancer_monster(engine)
    shards_before = engine.state.shards

    end = engine.execute_action("battle_end", {})

    assert end["success"]
    assert engine.state.shards == shards_before
    assert {entry["name"] for entry in end["result"]["removed_via_alt_path"]} == {monster.name}
    assert engine.state.rest_heal_bonus == 8

    engine._reset_after_death()
    assert engine.state.rest_heal_bonus == 8
    assert any("癌变·癌变怪" in entry for entry in engine.state.death_book_wisdom)


def test_new_engine_restores_bonus_from_sealed_cycle(tmp_path):
    """跨进程边界：封存候选文件携带的《死者之书》强化会装入下一引擎实例。"""
    sealed_path = tmp_path / "sealed.json"
    sealed_path.write_text(json.dumps({
        "rest_heal_bonus": 16,
        "death_book_wisdom": ["癌变·甲：休整恢复量+8", "癌变·乙：休整恢复量+8"],
    }, ensure_ascii=False), encoding="utf-8")

    engine = GameEngine(
        db_path=str(tmp_path / "new_rulings.db"),
        save_dir=str(tmp_path / "new_saves"),
        sealed_candidate_path=str(sealed_path),
        death_book_path=str(tmp_path / "new_death.md"),
    )

    assert engine.state.rest_heal_bonus == 16
    assert len(engine.state.death_book_wisdom) == 2


def test_rest_bonus_survives_versioned_save_round_trip(tmp_path):
    """存档边界：永久休整加成包含在版本4完整存档中。"""
    engine = _engine(tmp_path)
    _setup_player(engine)
    engine.state.rest_heal_bonus = 24
    saved = engine.save_game("cancer_bonus")
    assert saved["version"] == 4
    engine.state.rest_heal_bonus = 0

    loaded = engine.load_game("cancer_bonus")

    assert loaded["success"] and engine.state.rest_heal_bonus == 24


def test_invalid_rest_tier_keeps_permanent_bonus_and_resources(tmp_path):
    """非法输入：错误档位不能消耗碎片、永久加成或精力。"""
    engine = _engine(tmp_path)
    _setup_player(engine)
    engine.state.phase = "pre_battle"
    engine.state.rest_heal_bonus = 8
    before = (engine.state.shards, engine.state.energy, engine.state.rest_heal_bonus)

    result = engine.execute_action("pre_battle_action", {"sub_action": "休整", "tier": 99})

    assert not result["success"]
    assert (engine.state.shards, engine.state.energy, engine.state.rest_heal_bonus) == before
