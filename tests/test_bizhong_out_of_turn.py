"""【必中】X 增强守护（2026-10-12 用户令）。

裁定原文：「增强必中道纹，效果和描述为：你选中的目标无法进行回合外行动，持续X
（这意味着对方无法招架/闪避/未来可能新增的回合外行动）」。

由此固化三条口径，任何回退都会被这里拦住：
  1. 效果挂在**目标**身上（旧口径是给自己上姿态）；
  2. 封死的不只是闪避，还包括招架与反应型法术——即「回合外行动」这一整类；
  3. 判定统一走 `CombatEngine.out_of_turn_blocked()`，新增回合外行动必须复用这道闸。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine                                    # noqa: E402
from engine.daowen import DaoWenEngine                               # noqa: E402
from engine.models import Entity, Spell, StatusEffect                  # noqa: E402
from tests.setup_support import finish_initial_daowen                # noqa: E402


def _engine(region="罪孽都市"):
    engine = GameEngine(rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.state.current_region = region
    engine.state.phase = "in_combat"
    engine.state.player.current_mana = 30
    engine.state.player.attack_power = 5
    return engine


def _monster(engine, *, name="靶怪", hp=500, atk=8, hits=1):
    m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
               speed_limit=6, current_speed=6,
               attack_count=hits, attack_power=atk, shards=20, fake_shards=0)
    m.relics = []
    engine.state.enemies.append(m)
    return m


def _bizhong(engine, target, x=2):
    """玩家对 target 发动必中X（正式口径：必须指定目标）。"""
    calc = DaoWenEngine.resolve("必中", x)
    return engine.combat.apply_daowen_effect("必中", calc, engine.state.player,
                                             target=target)


# ---------- 正常路径 ----------

def test_bizhong_now_debuffs_the_selected_target_not_the_caster():
    """增强后【必中】落在目标身上，施法者不再获得姿态。"""
    engine = _engine()
    player = engine.state.player
    m = _monster(engine)

    _bizhong(engine, m, x=2)

    assert m.has_status("必中"), "必中必须挂在被选中的目标身上"
    assert not player.has_status("必中"), "增强后不再给施法者挂姿态"
    st = next(s for s in m.status_effects if s.name == "必中")
    assert st.remaining_rounds == 2, "持续X回合"


def test_bizhong_blocks_dodge():
    """回合外行动之一：闪避被封死。"""
    engine = _engine()
    player = engine.state.player
    m = _monster(engine, atk=8)
    _bizhong(engine, m, x=2)

    hp0 = m.current_hp
    r = engine.combat.resolve_attack(player, m, hit_index=0,
                                     is_must_hit=False, dodge=True)
    assert r.get("dodge_success") is False
    assert "必中" in r.get("dodge_fail_reason", ""), r
    assert m.current_hp < hp0, "闪避被封 → 伤害照常落地"


def test_bizhong_blocks_parry():
    """回合外行动之二：招架被封死，且不消耗目标速度（它没能支付）。"""
    engine = _engine()
    player = engine.state.player
    m = _monster(engine, atk=8)
    _bizhong(engine, m, x=2)

    speed0 = m.current_speed
    r = engine.combat.resolve_attack(player, m, hit_index=0,
                                     is_must_hit=False, parry=True)
    assert r.get("parry_success") is False
    assert "必中" in r.get("parry_fail_reason", ""), r
    assert m.current_speed == speed0, "招架被拒时不应扣速度"


def test_bizhong_blocks_reaction_spells():
    """回合外行动之三：反应型法术连提案都不该出现。"""
    engine = _engine()
    player = engine.state.player
    m = _monster(engine, atk=8)
    # 给目标一张「受到伤害前」反应法术（后发制人：为自己上庇护）
    m.spells.append(Spell(name="后发制人", required_daowen=[],
                                  trigger_condition="受到伤害前", effect_flow=""))

    assert engine.combat._eligible_spell_flows(
        m, "受到伤害前"), "前置条件：未受必中时该反应法术可用"

    _bizhong(engine, m, x=2)

    assert engine.combat._eligible_spell_flows(m, "受到伤害前") == {}, \
        "被【必中】封锁的角色不得再获得任何回合外行动提案"


# ---------- 边界条件 ----------

def test_gate_only_reads_the_duration_form_of_bizhong():
    """次数型【必中】(remaining_rounds=-1，grant_bizhong 旧口径) 不算封锁回合外行动。

    那条老路径的必中与否由 _bizhong_left 次数决定，若一并纳入会让遗物/怪物旧口径
    顺带失去招架与反应法术，超出本次裁定的范围。
    """
    engine = _engine()
    m = _monster(engine)
    m.add_status(StatusEffect(name="必中", remaining_rounds=-1, value=3,
                              source="旧口径"))

    assert engine.combat.out_of_turn_blocked(m) is False
    assert engine.combat._eligible_spell_flows(m, "受到伤害前") == {}  # 本来就没法术
    # 老口径的必中仍由次数余数负责
    engine.combat.grant_bizhong(m, 1)
    assert engine.combat.consume_bizhong(m) is True


def test_block_ends_with_the_status():
    """状态到期即解封：回合外行动恢复。"""
    engine = _engine()
    m = _monster(engine)
    m.spells.append(Spell(name="后发制人", required_daowen=[],
                                  trigger_condition="受到伤害前", effect_flow=""))
    _bizhong(engine, m, x=1)
    assert engine.combat.out_of_turn_blocked(m) is True

    st = next(s for s in m.status_effects if s.name == "必中")
    st.tick()
    m.status_effects = [s for s in m.status_effects
                        if s.name != "必中" or (s.remaining_rounds or 0) > 0]

    assert engine.combat.out_of_turn_blocked(m) is False
    assert "后发制人" in engine.combat._eligible_spell_flows(m, "受到伤害前")


def test_prepare_attack_reports_the_block():
    """prepare 侧如实回报，免得推演端提交注定失败的回合外行动。"""
    engine = _engine()
    m = _monster(engine)
    before = engine.combat.calculate_attack_damage(engine.state.player, m, 0)
    assert before["out_of_turn_blocked"] is False and before["can_dodge"] is True

    _bizhong(engine, m, x=2)

    after = engine.combat.calculate_attack_damage(engine.state.player, m, 0)
    assert after["out_of_turn_blocked"] is True
    assert after["can_dodge"] is False


# ---------- 错误输入 ----------

def test_gate_tolerates_none_entity():
    """闸门对 None 安全（多处调用点可能拿不到实体）。"""
    engine = _engine()
    assert engine.combat.out_of_turn_blocked(None) is False
