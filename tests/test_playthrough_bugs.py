"""手操通关(seed 2026081202)暴露的四条引擎回归锁。

1. 残韵打在敌人道纹上：不改敌人持有，改写下一次发动，施法者获得变化后道纹
2. 回始获得等同当前法限的法力（加法）；战始先清零再结算遗物
3. 原初X借到的【杀伐】等非优先队列道纹必须在怪物回合发动（按原版2X，无×3）
4. 残骸：恢复20生命并获得异变10

每条均覆盖正常路径 / 边界 / 错误输入。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Consumable, DaoWen, DaoWenInstance, Entity, Relic


def finish_round(engine):
    engine.state.combat_subphase = "await_round_end"
    return engine.execute_action("round_end", {})


def _engine(suffix: str) -> GameEngine:
    engine = GameEngine(db_path=f"data/test_playthrough_{suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    optional = {"折速法印", "三相残韵盘"}
    choice = next((n for n in setup["result"]["relic_choices"] if n not in optional),
                  setup["result"]["relic_choices"][0])
    engine.execute_action("choose_discovered_relic", {"relic_name": choice})
    engine.state.energy = 0
    return engine


def _give_daowen(entity: Entity, name: str, x: int = 1, tags=None) -> None:
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X",
               effect_formula="", tags=list(tags or [])),
        x_value=x,
    )


def _put_enemy(engine: GameEngine, monster: Entity) -> None:
    engine.state.enemies.clear()
    engine.state.enemies.append(monster)


def _advance_to_active_round(engine: GameEngine) -> None:
    """计算单元测试直接构造第2回合玩家行动阶段，避免攻击力0的夹具触发雕塑。"""
    engine.state.current_round = 2
    engine.state.combat_subphase = "player_actions"


def _resolve_prepared_monsters(engine: GameEngine, daowen_name: str | None = None):
    prepared = engine.execute_action("prepare_monster_phase", {})
    choices = []
    for actor in prepared["result"]["actors"]:
        dao = None
        if daowen_name is not None:
            option = next(o for o in actor["daowen_options"] if o["name"] == daowen_name)
            dao = {"name": daowen_name, "dodge": False, "blood_shadow": False, "trigger_spell_choices": {}}
            if option["requires_target"]:
                dao["target_ref"] = "player:0"
            if option["dodge_submission"] == "per_target":
                dao["dodge_targets"] = [
                    {"target_ref": target["ref"], "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}
                    for target in option["dodge_target_options"]
                ]
        attacks = [{"hits": [{"target_ref": "player:0", "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}
                              for _ in range(actor["base_hits_per_attack"])]}
                   for _ in range(actor["base_attack_actions"])]
        choices.append({"actor_ref": actor["actor_ref"], "daowen": dao,
                        "attack_actions": attacks})
    return engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": choices,
    })


# ========================================================================
# 1. 残韵打在敌人道纹上
# ========================================================================

def test_resonance_on_enemy_grants_dest_and_rewrites_next_activation():
    """正常路径：反转敌方狂暴 → 贾凡获得自残，敌人仍持有狂暴，下次发动按自残打自己。"""
    engine = _engine("res_happy")
    engine.state.resonance["反转"] = 1
    engine.execute_action("battle_start", {})
    monster = Entity(name="通缉犯", entity_type="怪物", blood_limit=80, current_hp=80,
                     attack_count=1, attack_power=5)
    _give_daowen(monster, "狂暴", x=2)
    _put_enemy(engine, monster)
    engine.execute_action("round_start", {})

    r = engine.execute_action("use_resonance", {
        "source_daowen": "狂暴", "resonance_type": "反转", "target_ref": "enemy:0",
    })
    assert r["success"], r
    assert r["granted_daowen"] == "自残"
    assert "自残" in engine.state.player.dao_wen
    assert "狂暴" in monster.dao_wen
    assert "自残" not in monster.dao_wen
    assert engine.state.resonance["反转"] == 0

    finish_round(engine)
    engine.execute_action("round_start", {})
    prepared = engine.execute_action("prepare_monster_phase", {})
    actor = prepared["result"]["actors"][0]
    phase = engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"],
        "choices": [{
            "actor_ref": actor["actor_ref"],
            "daowen": {"name": "狂暴", "target_ref": "enemy:0", "dodge": False, "blood_shadow": False, "trigger_spell_choices": {}},
            "attack_actions": [{"hits": [{"target_ref": "player:0", "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}]}],
        }],
    })
    details = phase["result"]["details"]
    rewritten = [d for d in details if d.get("resonance_rewrite")]
    assert rewritten, f"应兑现残韵改写: {details}"
    assert rewritten[0]["daowen_activated"] == "狂暴"
    assert rewritten[0]["resolves_as"] == "自残"
    assert monster.current_hp == 70, f"自残2次×攻击力5，HP应80→70，实{monster.current_hp}"
    assert "狂暴" in monster.dao_wen


def test_resonance_no_duplicate_when_caster_already_owns_dest():
    """边界：施法者已持有变化后道纹则不重复获得；无 target 时场上唯一持有者也可命中。"""
    engine = _engine("res_bound")
    engine.state.resonance["反转"] = 1
    _give_daowen(engine.state.player, "自残")
    engine.execute_action("battle_start", {})
    monster = Entity(name="唯一狂暴", entity_type="怪物", blood_limit=80, current_hp=80,
                     attack_count=1, attack_power=4)
    _give_daowen(monster, "狂暴", x=1)
    _put_enemy(engine, monster)
    engine.execute_action("round_start", {})

    r = engine.execute_action("use_resonance", {
        "source_daowen": "狂暴", "resonance_type": "反转",
    })
    assert r["success"], r
    assert r["granted_daowen"] is None
    assert list(engine.state.player.dao_wen).count("自残") == 1
    assert list(monster.dao_wen.keys()) == ["狂暴"]


def test_resonance_fails_without_holder_or_stock():
    """错误输入：目标没有源道纹 / 没有残韵库存 → 失败且不消耗。"""
    engine = _engine("res_invalid")
    engine.state.resonance["反转"] = 1
    engine.execute_action("battle_start", {})
    monster = Entity(name="白板怪", entity_type="怪物", blood_limit=50, current_hp=50,
                     attack_count=1, attack_power=1)
    _put_enemy(engine, monster)
    engine.execute_action("round_start", {})

    r = engine.execute_action("use_resonance", {
        "source_daowen": "狂暴", "resonance_type": "反转", "target_ref": "enemy:0",
    })
    assert r["success"] is False
    assert engine.state.resonance["反转"] == 1
    assert "自残" not in engine.state.player.dao_wen

    engine.state.resonance["反转"] = 0
    r2 = engine.execute_action("use_resonance", {
        "source_daowen": "杀伐", "resonance_type": "反转",
    })
    assert r2["success"] is False
    assert "杀伐" in engine.state.player.dao_wen


# ========================================================================
# 2. 回始获得等同当前法限的法力（加法）；战始先清零再结算遗物
# ========================================================================

def test_zhesu_and_blood_pact_overflow_survives_first_round_start():
    """正常路径：折速在战始加法力；血契在回始流血4X并叠加X法力。"""
    engine = _engine("mana_happy")
    p = engine.state.player
    assert p.mana_limit == 14 and p.speed_limit == 8
    engine.state.relics.append(Relic(name="折速法印", effect="[战始]可疲惫X获得6X法力"))
    engine.execute_action("battle_start", {"relic_choices": {
        "折速法印": {"use": True, "x": 4},
    }})
    assert p.current_mana == 24, f"战始清零后显式折速4，+24法力，应24，实{p.current_mana}"
    assert p.current_speed == 4
    engine.execute_action("round_start", {})
    assert p.current_mana == 38, f"回始获得法限14：24+14=38，实{p.current_mana}"

    engine2 = _engine("mana_pact")
    p2 = engine2.state.player
    engine2.state.relics.append(Relic(name="血契", effect="[回始]可流血4X获得X法力"))
    hp_before = p2.current_hp
    engine2.execute_action("battle_start", {"relic_choices": {}})
    assert p2.current_mana == 0
    engine2.execute_action("round_start", {"relic_choices": {
        "血契": {"use": True, "x": 3},
    }})
    assert p2.current_mana == 17, f"回始先+法限14，再由血契+3，应17，实{p2.current_mana}"
    assert p2.current_hp == hp_before - 12


def test_round_start_adds_mana_limit_even_with_leftover():
    """边界：回始始终 += 法限，残量不被赋值冲掉，也不被压到法限。"""
    engine = _engine("mana_bound")
    p = engine.state.player
    engine.execute_action("battle_start", {})
    assert p.current_mana == 0
    p.current_mana = 3
    engine.execute_action("round_start", {})
    assert p.current_mana == 17, f"残量3+法限14应17，实{p.current_mana}"

    finish_round(engine)
    assert p.current_mana == 0
    engine.execute_action("round_start", {})
    assert p.current_mana == p.mana_limit == 14


def test_no_zhesu_means_no_bonus_mana():
    """错误输入/对照：未持有折速法印时战始清零、不扣速度。"""
    engine = _engine("mana_invalid")
    p = engine.state.player
    engine.execute_action("battle_start", {})
    assert p.current_mana == 0
    assert p.current_speed == 8


def test_shouyedeng_bonus_survives_round_start_after_refill():
    """正常路径：守夜灯在回始获得法限之后再加法限50%。"""
    engine = _engine("lamp_happy")
    p = engine.state.player
    engine.state.relics.append(Relic(name="守夜灯", effect="[敌回始]获得等同于[法限]50%的法力"))
    engine.execute_action("battle_start", {})
    engine.execute_action("round_start", {})
    assert p.current_mana == 21, f"回始应0+14+7=21，实{p.current_mana}"
    finish_round(engine)
    assert p.current_mana == 0
    engine.execute_action("round_start", {})
    assert p.current_mana == 21, f"回终清空后再回始，守夜灯仍应叠到21，实{p.current_mana}"


def test_shouyedeng_stacks_on_zhesu_overflow():
    """边界：折速战始+24，回始再+法限+守夜灯。"""
    engine = _engine("lamp_bound")
    p = engine.state.player
    engine.state.relics.append(Relic(name="折速法印", effect="[战始]可疲惫X获得6X法力"))
    engine.state.relics.append(Relic(name="守夜灯", effect="[敌回始]获得等同于[法限]50%的法力"))
    engine.execute_action("battle_start", {"relic_choices": {
        "折速法印": {"use": True, "x": 4},
    }})
    assert p.current_mana == 24
    engine.execute_action("round_start", {})
    assert p.current_mana == 45, f"24+14+7应45，实{p.current_mana}"


def test_no_shouyedeng_means_no_round_start_bonus():
    """错误输入/对照：未持有守夜灯时回始只获得法限，回终仍清空。"""
    engine = _engine("lamp_invalid")
    p = engine.state.player
    engine.execute_action("battle_start", {})
    engine.execute_action("round_start", {})
    assert p.current_mana == 14
    finish_round(engine)
    assert p.current_mana == 0


# ========================================================================
# 3. 原初X 借用的杀伐必须在怪物回合发动
# ========================================================================

def test_borrowed_shaifa_fires_in_monster_phase():
    """正常路径：困境怪借杀伐2后，怪物回合按原版2X=4打向轮回者。"""
    engine = _engine("evo_happy")
    engine.execute_action("battle_start", {})
    monster = Entity(name="困境怪", entity_type="怪物", blood_limit=120, current_hp=30,
                     attack_count=1, attack_power=0)
    _put_enemy(engine, monster)
    _advance_to_active_round(engine)
    ev = engine.execute_action("declare_evolution", {
        "monster": "困境怪", "daowen": "杀伐", "x": 2,
    })
    assert ev["success"], ev
    assert "原初借用" in monster.dao_wen["杀伐"].dao_wen.tags

    _advance_to_active_round(engine)
    hp_before = engine.state.player.current_hp
    phase = _resolve_prepared_monsters(engine, "杀伐")
    details = phase["result"]["details"]
    borrowed = [d for d in details if d.get("resolves_as") == "杀伐"]
    assert borrowed, f"应发动借用杀伐: {details}"
    assert borrowed[0]["daowen_activated"] == "杀伐"
    assert engine.state.player.current_hp == hp_before - 6, (
        f"杀伐2→3X=6，HP应{hp_before}→{hp_before - 6}，实{engine.state.player.current_hp}")


def test_borrowed_shaifa_x1_deals_two():
    """边界：借用杀伐X=1，伤害3X=3。"""
    engine = _engine("evo_bound")
    engine.execute_action("battle_start", {})
    monster = Entity(name="困境怪", entity_type="怪物", blood_limit=120, current_hp=30,
                     attack_count=1, attack_power=0)
    _put_enemy(engine, monster)
    _advance_to_active_round(engine)
    assert engine.execute_action("declare_evolution", {
        "monster": "困境怪", "daowen": "杀伐", "x": 1,
    })["success"]
    _advance_to_active_round(engine)
    hp_before = engine.state.player.current_hp
    _resolve_prepared_monsters(engine, "杀伐")
    assert engine.state.player.current_hp == hp_before - 3


def test_unevolved_monster_does_not_cast_shaifa():
    """错误输入/对照：未进化的怪物即使玩家持有杀伐，也不会发动杀伐。"""
    engine = _engine("evo_invalid")
    engine.execute_action("battle_start", {})
    monster = Entity(name="普通怪", entity_type="怪物", blood_limit=80, current_hp=80,
                     attack_count=1, attack_power=0)
    _put_enemy(engine, monster)
    _advance_to_active_round(engine)
    hp_before = engine.state.player.current_hp
    phase = _resolve_prepared_monsters(engine)
    details = phase["result"]["details"]
    assert not any("杀伐" in str(d.get("daowen_activated", "")) for d in details)
    assert engine.state.player.current_hp == hp_before


# ========================================================================
# 4. 残骸：回 20 生命 + 异变 10
# ========================================================================

def test_canhai_heals_twenty_and_adds_mutation_ten():
    """正常路径：残血使用残骸，回复20并获得异变10，耐久归零。"""
    engine = _engine("canhai_happy")
    p = engine.state.player
    p.current_hp = 25
    engine.state.consumables.append(Consumable(
        name="残骸", effect="局内使用恢复20生命并获得异变10", current_uses=1, max_uses=1))
    r = engine.execute_action("consume_item", {"name": "残骸"})
    assert r["success"], r
    assert p.current_hp == 45
    assert r["result"]["heal"]["actual_heal"] == 20
    assert p.mutation_count == 10
    assert engine.state.consumables[0].is_depleted


def test_canhai_at_full_hp_tracks_overheal():
    """边界：满血残骸实回复0，过量20按双倍计入累计恢复量。"""
    engine = _engine("canhai_bound")
    p = engine.state.player
    assert p.current_hp == p.blood_limit == 60
    engine.state.consumables.append(Consumable(
        name="残骸", effect="局内使用恢复20生命并获得异变10", current_uses=1, max_uses=1))
    r = engine.execute_action("consume_item", {"name": "残骸"})
    assert r["success"]
    assert p.current_hp == 60
    assert r["result"]["heal"]["actual_heal"] == 0
    assert r["result"]["heal"]["overheal"] == 20
    assert p.total_healed == 40
    assert p.mutation_count == 10
    assert p.is_alive


def test_canhai_missing_item_fails():
    """错误输入：没有残骸时拒绝，不改生命、不加异变。"""
    engine = _engine("canhai_invalid")
    p = engine.state.player
    p.current_hp = 25
    r = engine.execute_action("consume_item", {"name": "残骸"})
    assert r["success"] is False
    assert p.current_hp == 25
    assert p.mutation_count == 0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
