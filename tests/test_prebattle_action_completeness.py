"""局外多档学习、共鸣与维修必须真实结算，不返回空指令。"""
from engine.api import GameEngine
from engine.models import Consumable, Entity


from tests.setup_support import finish_initial_daowen
def _engine(tmp_path, region="扭曲都市", seed=31):
    engine = GameEngine(
        db_path=str(tmp_path / "rulings.db"), save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        death_book_path=str(tmp_path / "death.md"), rng_seed=seed,
    )
    engine.execute_action("setup_attributes", {
        "name": "局外测试", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": region})
    engine.execute_action("choose_discovered_relic", {
        "relic_name": setup["result"]["relic_choices"][0],
    })
    engine.state.shards = 100
    return engine


def test_available_prebattle_schemas_use_real_executable_parameter_names(tmp_path):
    engine = _engine(tmp_path)
    schemas = [action["params_schema"] for action in engine.get_available_actions()["actions"]
               if action["action_type"] == "pre_battle_action"]
    by_sub_action = {schema["sub_action"]: schema for schema in schemas}

    learning = by_sub_action["学习"]
    assert "mode" not in learning
    assert {"sub", "tier", "names", "spell", "dm_approved"} <= set(learning)
    assert {"heal_allocations", "tier"} <= set(by_sub_action["休整"])
    assert {"allocations", "tier"} <= set(by_sub_action["修行"])
    assert "tier" in by_sub_action["探索"]
    assert "allocations" in by_sub_action["维修"]
    assert "additional" not in by_sub_action["维修"]


def test_learning_three_spells_and_two_daowen_applies_all_names(tmp_path):
    engine = _engine(tmp_path)
    spell_names = list(engine.SPELL_REGISTRY)[:3]

    spells = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "spell", "tier": 3, "names": spell_names,
    })
    assert spells["success"]
    assert {spell.name for spell in engine.state.player.spells} == set(spell_names)
    assert engine.state.shards == 75

    engine.state.energy = 3
    daowen = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "daowen", "tier": 2,
        "names": ["束缚", "庇护"],
    })
    assert daowen["success"]
    assert {"杀伐", "束缚", "庇护"} <= set(engine.state.player.dao_wen)
    assert engine.state.shards == 65


def test_learning_wrong_count_duplicate_and_daowen_tier_three_are_atomic(tmp_path):
    engine = _engine(tmp_path)
    before = (engine.state.energy, engine.state.shards, set(engine.state.player.dao_wen))

    wrong_count = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "spell", "tier": 2,
        "names": [list(engine.SPELL_REGISTRY)[0]],
    })
    assert not wrong_count["success"]
    assert (engine.state.energy, engine.state.shards, set(engine.state.player.dao_wen)) == before

    invalid_tier = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "daowen", "tier": 3,
        "names": ["切割", "庇护", "再生"],
    })
    assert not invalid_tier["success"]
    assert (engine.state.energy, engine.state.shards, set(engine.state.player.dao_wen)) == before


def test_custom_spell_interrupt_then_approved_creation(tmp_path):
    engine = _engine(tmp_path)
    definition = {
        "name": "杀意回响",
        "required_daowen": ["杀伐"],
        "trigger_condition": "受到伤害前",
        "effect_flow": "发动杀伐X",
    }
    before_energy = engine.state.energy
    pending = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "custom_spell", "spell": definition,
    })
    assert pending["success"] and pending["completed"] is False
    assert engine.state.energy == before_energy
    assert not any(spell.name == "杀意回响" for spell in engine.state.player.spells)

    ruled = engine.submit_ruling("未见场景", "批准自创法术", {})
    assert ruled["success"]
    created = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "custom_spell", "spell": definition,
        "dm_approved": True,
    })
    assert created["success"]
    assert any(spell.name == "杀意回响" for spell in engine.state.player.spells)
    assert engine.state.energy == before_energy - 1


def test_resonance_choose_requires_second_energy_and_applies_cost(tmp_path):
    engine = _engine(tmp_path)
    engine._init_relic_pool()
    choice = engine.state.relics_pool[0].name
    engine.state.energy = 1
    before = (engine.state.energy, engine.state.shards, len(engine.state.relics))

    insufficient = engine.execute_action("pre_battle_action", {
        "sub_action": "共鸣", "tier": 2, "name": choice,
    })
    assert not insufficient["success"]
    assert (engine.state.energy, engine.state.shards, len(engine.state.relics)) == before

    engine.state.energy = 3
    success = engine.execute_action("pre_battle_action", {
        "sub_action": "共鸣", "tier": 2, "name": choice,
    })
    assert success["success"]
    assert engine.state.energy == 1 and engine.state.shards == 85
    assert any(relic.name == choice for relic in engine.state.relics)


def test_repair_distributes_durability_and_rejects_overflow_atomically(tmp_path):
    engine = _engine(tmp_path)
    first = Consumable("甲", "", current_uses=1, max_uses=3)
    second = Consumable("乙", "", current_uses=2, max_uses=3)
    engine.state.consumables = [first, second]
    engine.state.energy = 3

    repaired = engine.execute_action("pre_battle_action", {
        "sub_action": "维修", "tier": 2,
        "allocations": [
            {"item_ref": "consumable:0", "amount": 1},
            {"item_ref": "consumable:1", "amount": 1},
        ],
    })
    assert repaired["success"]
    assert (first.current_uses, second.current_uses) == (2, 3)
    assert engine.state.shards == 95

    engine.state.energy = 3
    before = (first.current_uses, second.current_uses, engine.state.shards, engine.state.energy)
    overflow = engine.execute_action("pre_battle_action", {
        "sub_action": "维修", "tier": 1,
        "allocations": [{"item_ref": "consumable:1", "amount": 1}],
    })
    assert not overflow["success"]
    assert (first.current_uses, second.current_uses, engine.state.shards, engine.state.energy) == before


def test_rest_freely_splits_full_amount_without_combat_heal_tracking(tmp_path):
    engine = _engine(tmp_path)
    player = engine.state.player
    player.current_hp = 10
    player.total_healed = 4
    player.healed_this_battle = 3
    friend = Entity("朋友甲", "friend", blood_limit=30, current_hp=1,
                    total_healed=5, healed_this_battle=2)
    employee = Entity("员工甲", "employee", blood_limit=30, current_hp=2,
                      total_healed=6, healed_this_battle=1)
    engine.state.friends.append(friend)
    engine.state.employees.append(employee)
    before_tracking = [(entity.total_healed, entity.healed_this_battle)
                       for entity in (player, friend, employee)]

    rested = engine.execute_action("pre_battle_action", {
        "sub_action": "休整", "tier": 2,
        "heal_allocations": [
            {"target_ref": "player:0", "amount": 5},
            {"target_ref": "friend:0", "amount": 7},
            {"target_ref": "employee:0", "amount": 12},
        ],
    })

    assert rested["success"]
    assert [player.current_hp, friend.current_hp, employee.current_hp] == [15, 8, 14]
    assert [(entity.total_healed, entity.healed_this_battle)
            for entity in (player, friend, employee)] == before_tracking
    assert engine.state.shards == 90


def test_rest_requires_exact_valid_allocation_and_is_atomic(tmp_path):
    engine = _engine(tmp_path)
    engine.state.player.current_hp = 10
    before = (engine.state.player.current_hp, engine.state.energy, engine.state.shards)

    incomplete = engine.execute_action("pre_battle_action", {
        "sub_action": "休整", "tier": 1,
        "heal_allocations": [{"target_ref": "player:0", "amount": 7}],
    })
    assert not incomplete["success"]
    assert (engine.state.player.current_hp, engine.state.energy, engine.state.shards) == before

    unknown_target = engine.execute_action("pre_battle_action", {
        "sub_action": "休整", "tier": 1,
        "heal_allocations": [{"target_ref": "friend:99", "amount": 8}],
    })
    assert not unknown_target["success"]
    assert (engine.state.player.current_hp, engine.state.energy, engine.state.shards) == before


def test_training_splits_tier_points_between_speed_and_mana(tmp_path):
    engine = _engine(tmp_path)
    player = engine.state.player
    before = (player.speed_limit, player.mana_limit)

    trained = engine.execute_action("pre_battle_action", {
        "sub_action": "修行", "tier": 4,
        "allocations": {"speed_points": 1, "mana_points": 3},
    })

    assert trained["success"]
    assert (player.speed_limit, player.mana_limit) == (before[0] + 1, before[1] + 6)
    assert (player.current_speed, player.current_mana) == (player.speed_limit, player.mana_limit)
    assert trained["result"]["allocations"] == {"speed_points": 1, "mana_points": 3}
    assert engine.state.shards == 35


def test_training_rejects_invalid_split_atomically(tmp_path):
    engine = _engine(tmp_path)
    player = engine.state.player
    before = (player.speed_limit, player.mana_limit, player.current_speed,
              player.current_mana, engine.state.energy, engine.state.shards)

    mismatch = engine.execute_action("pre_battle_action", {
        "sub_action": "修行", "tier": 3,
        "allocations": {"speed_points": 1, "mana_points": 1},
    })
    assert not mismatch["success"]
    assert (player.speed_limit, player.mana_limit, player.current_speed,
            player.current_mana, engine.state.energy, engine.state.shards) == before

    boolean = engine.execute_action("pre_battle_action", {
        "sub_action": "修行", "tier": 1,
        "allocations": {"speed_points": True, "mana_points": 0},
    })
    assert not boolean["success"]
    assert (player.speed_limit, player.mana_limit, player.current_speed,
            player.current_mana, engine.state.energy, engine.state.shards) == before
