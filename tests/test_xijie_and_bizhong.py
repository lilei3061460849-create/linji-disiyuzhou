"""洗劫只在持有状态下夺碎片；必中只覆盖下X次选择[目标]。"""
from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.daowen import DaoWenEngine
from engine.dice import DiceEngine
from engine.models import DaoWen, DaoWenInstance, Entity, GameState, StatusEffect


def _engine(region="罪孽都市"):
    engine = GameEngine(rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    engine.execute_action("setup_choose_daowen", {"daowen": "杀伐"})
    engine.execute_action("setup_choose_region", {"region": region})
    engine.state.player.current_mana = 40
    engine.state.player.attack_power = 5
    return engine


def _monster(engine, *, name="靶怪", hp=80, atk=8, hits=1, shards=20, daowen=None):
    m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
               attack_count=hits, attack_power=atk)
    m.shards = shards
    for dw_name, x in (daowen or {}).items():
        m.dao_wen[dw_name] = DaoWenInstance(
            DaoWen(name=dw_name, formula="", cost_type="异变", cost_formula="5X", effect_formula=""),
            x_value=x)
    engine.state.enemies.append(m)
    return m


# ---------- 洗劫门闩 ----------

def test_shaifa_without_xijie_does_not_steal():
    """正常/非法：没洗劫状态时，杀伐造成伤害不得夺碎片。"""
    engine = _engine()
    m = _monster(engine, shards=20)
    before = engine.state.shards
    r = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 3, "target": m.name})
    assert r["success"] is True
    assert m.current_hp == 80 - 6
    assert m.shards == 20
    assert engine.state.shards == before


def test_shaifa_with_xijie_status_steals():
    """正常路径：先挂洗劫，再杀伐，按实伤夺碎片。"""
    engine = _engine()
    player = engine.state.player
    m = _monster(engine, shards=20)
    player.add_status(StatusEffect(name="洗劫", value=2, remaining_rounds=2, source=player.name))
    before = engine.state.shards
    r = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 3, "target": m.name})
    assert r["success"] is True
    assert m.shards == 14
    assert engine.state.shards == before + 6


def test_xijie_expired_no_longer_steals():
    """边界：洗劫持续走完后，再造成伤害不再夺。"""
    engine = _engine()
    player = engine.state.player
    m = _monster(engine, shards=20)
    player.add_status(StatusEffect(name="洗劫", value=1, remaining_rounds=1, source=player.name))
    engine.combat.round_end()
    assert not player.has_status("洗劫")
    before = engine.state.shards
    engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 2, "target": m.name})
    assert m.shards == 20
    assert engine.state.shards == before


# ---------- 必中余数 ----------

def test_bizhong_only_next_x_target_selections():
    """正常路径：必中2只让接下来2次选目标无法闪避，第3次可闪。"""
    engine = _engine()
    player = engine.state.player
    player.current_speed = 5
    player.shield = 0
    m = _monster(engine, hits=1, atk=8, daowen={"必中": 2})
    engine.combat.reset_monster_activation()
    engine.state.current_round = 0
    engine.combat.round_start()  # -> 1 白板
    engine.combat.run_monster_phase(dodge_policy="auto")
    assert engine.combat.bizhong_remaining(m) == 0
    engine.combat.round_start()  # -> 2 激活必中2，打1击
    r2 = engine.combat.run_monster_phase(dodge_policy="auto")
    hits2 = [d for d in r2 if d.get("attacker") == m.name]
    assert hits2 and hits2[0].get("dodge_success") is False
    assert engine.combat.bizhong_remaining(m) == 1
    engine.combat.round_start()  # -> 3 再打1击，用尽
    r3 = engine.combat.run_monster_phase(dodge_policy="auto")
    hits3 = [d for d in r3 if d.get("attacker") == m.name]
    assert hits3 and hits3[0].get("dodge_success") is False
    assert engine.combat.bizhong_remaining(m) == 0
    assert not m.has_status("必中")
    engine.combat.round_start()  # -> 4 可闪
    r4 = engine.combat.run_monster_phase(dodge_policy="auto")
    hits4 = [d for d in r4 if d.get("attacker") == m.name]
    assert hits4 and hits4[0].get("dodge_success") is True


def test_bizhong_two_hits_in_one_round_consume_two_charges():
    """边界：一轮攻击2击 = 2次选择[目标]，必中2恰好用完。"""
    engine = _engine()
    player = engine.state.player
    player.current_speed = 6
    player.shield = 0
    m = _monster(engine, hits=2, atk=8, daowen={"必中": 2})
    engine.combat.reset_monster_activation()
    engine.state.current_round = 0
    engine.combat.round_start()
    engine.combat.run_monster_phase(dodge_policy="auto")
    engine.combat.round_start()
    results = engine.combat.run_monster_phase(dodge_policy="auto")
    hits = [d for d in results if d.get("attacker") == m.name]
    assert len(hits) == 2
    assert all(h.get("dodge_success") is False for h in hits)
    assert engine.combat.bizhong_remaining(m) == 0


def test_no_bizhong_auto_dodge_still_works():
    """错误/对照：没激活必中时，auto 闪避照常成功。"""
    engine = _engine()
    player = engine.state.player
    player.current_speed = 4
    player.shield = 0
    m = _monster(engine, hits=1, atk=8, daowen={"狂暴": 1})
    engine.combat.reset_monster_activation()
    engine.state.current_round = 0
    engine.combat.round_start()
    results = engine.combat.run_monster_phase(dodge_policy="auto")
    hits = [d for d in results if d.get("attacker") == m.name]
    assert hits and hits[0].get("dodge_success") is True


def test_player_cast_bizhong_sets_charges():
    """正常路径：玩家发动必中X，余数写入自身。"""
    combat = CombatEngine(GameState(), DiceEngine(seed=1))
    player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60)
    calc = DaoWenEngine.resolve("必中", 3, caster=player)
    combat.apply_daowen_effect("必中", calc, player, player)
    assert combat.bizhong_remaining(player) == 3
    assert player.has_status("必中")
