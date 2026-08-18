"""【凡庸】特殊事件 与 [战终]清除回复 的回归测试

对应 README：
  第500-501行 【凡庸】：任一角色连续五回合未出手／五回合未能使敌对角色生命减少，
               即触发；该角色全身炸裂[命零]，若为怪物则轮回者获得消耗品【残骸】(1/1)。
  第304行     [战终]：清除局内增益（包括回复、格挡、持续∞等）与减益（不包括代价）。
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.models import Entity  # noqa: E402
from engine.api import GameEngine  # noqa: E402

DB_DIR = "/tmp/linji_tests"
os.makedirs(DB_DIR, exist_ok=True)


def _monster(name="木桩", hp=200):
    return Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
                  speed_limit=0, current_speed=0, mana_limit=0, current_mana=0,
                  attack_count=1, attack_power=5)


def _engine(seed=7):
    e = GameEngine(db_path=f"{DB_DIR}/mediocrity.db", rng_seed=seed)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": "罪孽都市"})
    return e


# ---------- 正常路径 ----------

def test_mediocrity_triggers_after_five_idle_rounds():
    """正常路径：连续五回合既未出手也未造成伤害 -> 触发【凡庸】命零"""
    e = _engine()
    cm = e.combat
    cm.state.enemies = [_monster()]
    m = cm.state.enemies[0]
    p = cm.state.player

    fired = None
    for r in range(1, 6):
        m.damage_dealt_this_round = 0
        m.actions_used_this_round = 0
        p.damage_dealt_this_round = 1      # 玩家有作为，不应被凡庸波及
        p.actions_used_this_round = 1
        res = cm.round_end()
        for ef in res.get("effects", []):
            if ef.get("type") == "mediocrity" and ef.get("entity") == m.name:
                fired = r

    assert fired == 5, f"【凡庸】应在第5个空转回合触发，实际={fired}"
    assert m.current_hp == 0 and not m.is_alive, "触发【凡庸】的怪物应[命零]"
    assert p.is_alive, "有作为的轮回者不应被【凡庸】波及"


def test_mediocrity_on_monster_grants_wreckage():
    """正常路径：怪物因【凡庸】命零时，轮回者获得消耗品【残骸】(1/1)"""
    e = _engine()
    cm = e.combat
    cm.state.enemies = [_monster()]
    m = cm.state.enemies[0]
    before = len([c for c in cm.state.consumables if c.name == "残骸"])

    for _ in range(5):
        m.damage_dealt_this_round = 0
        m.actions_used_this_round = 0
        cm.state.player.damage_dealt_this_round = 1
        cm.state.player.actions_used_this_round = 1
        cm.round_end()

    wrecks = [c for c in cm.state.consumables if c.name == "残骸"]
    assert len(wrecks) == before + 1, "怪物触发【凡庸】应产出一件【残骸】"
    assert wrecks[-1].current_uses == 1 and wrecks[-1].max_uses == 1, "【残骸】应为 1/1"


# ---------- 边界条件 ----------

def test_idle_counter_resets_on_any_action():
    """边界：第5回合前只要出手过一次，计数归零，不触发【凡庸】"""
    e = _engine()
    cm = e.combat
    cm.state.enemies = [_monster()]
    m = cm.state.enemies[0]

    p = cm.state.player
    for r in range(1, 9):
        m.damage_dealt_this_round = 2      # 一直有伤害，隔离出"未出手"这一分支
        # 第4回合出手一次，打断连续空转
        m.actions_used_this_round = 1 if r == 4 else 0
        p.damage_dealt_this_round = 1   # 玩家保持有作为，隔离被测对象
        p.actions_used_this_round = 1
        res = cm.round_end()
        fired = [ef for ef in res.get("effects", [])
                 if ef.get("type") == "mediocrity" and ef.get("entity") == m.name]
        assert not fired, f"第{r}回合不应触发【凡庸】（计数应在第4回合归零）"
    # 第4回合打断后重新计数，到第8回合只积累了4个空转回合，仍不触发
    assert m.is_alive, "打断后重新计数，第8回合仍不该触发"
    assert m.no_action_rounds == 4


def test_no_action_branch_triggers_even_if_damage_dealt():
    """边界：README 用"/"表示或——即便回回造成伤害，连续五回合未出手仍触发"""
    e = _engine()
    cm = e.combat
    cm.state.enemies = [_monster()]
    m = cm.state.enemies[0]

    for _ in range(6):
        m.damage_dealt_this_round = 3
        m.actions_used_this_round = 0
        cm.state.player.damage_dealt_this_round = 1
        cm.state.player.actions_used_this_round = 1
        cm.round_end()

    assert not m.is_alive, "连续五回合未出手，即便造成过伤害也应触发【凡庸】"
    assert m.no_damage_rounds == 0


# ---------- [战终] 清除回复类持续效果 ----------

def test_battle_end_clears_infinite_duration_buffs():
    """正常路径：[战终]清除局内持续效果（含持续∞的回复类增益）"""
    from engine.models import StatusEffect
    e = _engine()
    p = e.state.player
    p.status_effects.append(StatusEffect(name="自愈", remaining_rounds=-1, value=2))
    p.status_effects.append(StatusEffect(name="杀伐", remaining_rounds=3, value=1))
    p.shield = 10

    e._action_battle_end({})

    names = [s.name for s in p.status_effects]
    assert "自愈" not in names, "持续∞的回复类增益应在[战终]清除"
    assert "杀伐" not in names, "局内持续增益应在[战终]清除"
    assert p.shield == 0, "格挡应在[战终]清空"


def test_battle_end_keeps_cost_status():
    """边界：[代价]不随[战终]清除（README 第304行括注）"""
    from engine.models import StatusEffect
    e = _engine()
    p = e.state.player
    p.status_effects.append(StatusEffect(name="流血", remaining_rounds=-1, value=3))
    p.status_effects.append(StatusEffect(name="衰老", remaining_rounds=-1, value=2))
    p.status_effects.append(StatusEffect(name="庇护", remaining_rounds=2, value=1))

    e._action_battle_end({})

    names = [s.name for s in p.status_effects]
    assert "流血" in names and "衰老" in names, "[代价]类不应被[战终]清除"
    assert "庇护" not in names, "非代价的局内增益应被清除"


def test_battle_end_reverts_in_battle_healing():
    """正常路径：战斗中[回复]的生命在[战终]吐出（跨场恢复只能靠局外【休整】）"""
    e = _engine()
    p = e.state.player
    p.blood_limit = 60
    p.current_hp = 30
    p.battle_start_hp = 30
    p.healed_this_battle = 0
    p.heal(20)
    assert p.current_hp == 50, "战斗中回复应即时生效"

    e._action_battle_end({})
    assert p.current_hp == 30, f"[战终]应吐出本场回复的生命，实际={p.current_hp}"
    assert p.healed_this_battle == 0


def test_battle_end_keeps_damage_taken():
    """边界：吐出回复时，战斗中受到的伤害如实保留，不被一并还原"""
    e = _engine()
    p = e.state.player
    p.blood_limit = 60
    p.current_hp = 40
    p.battle_start_hp = 40
    p.healed_this_battle = 0

    p.take_damage(25, "普通")   # 40 -> 15
    p.heal(20)                   # 15 -> 35
    assert p.current_hp == 35

    e._action_battle_end({})
    assert p.current_hp == 15, f"伤害应保留、只吐回复，实际={p.current_hp}"


def test_battle_end_heal_revert_never_kills():
    """错误输入/边界：吐出回复不得把轮回者压到[命零]，保底 1 点"""
    e = _engine()
    p = e.state.player
    p.blood_limit = 60
    p.current_hp = 5
    p.battle_start_hp = 60
    p.healed_this_battle = 0
    p.heal(3)                    # 5 -> 8，本场回复 3
    p.healed_this_battle = 50    # 人为放大，模拟大量回复后被打残

    e._action_battle_end({})
    assert p.current_hp == 1, f"吐出回复应保底 1 点，实际={p.current_hp}"
    assert p.is_alive, "[战终]吐出回复不应直接致死"


# ---------- 锐利：血限与当前生命同时扣减（README 第460行） ----------

def test_ruili_reduces_hp_independently_of_blood_limit():
    """正常路径：锐利对残血目标仍应扣当前生命（不是只做血限压顶）"""
    e = _engine()
    cm = e.combat
    m = _monster(hp=200)
    m.current_hp = 10          # 残血：血限压顶对它毫无作用
    cm.state.enemies = [m]

    calc = {"blood_limit_reduction": 20, "hp_reduction": 20}
    cm.apply_daowen_effect("锐利", calc, cm.state.player, m)

    assert m.blood_limit == 180, f"血限应-20，实际={m.blood_limit}"
    assert m.current_hp == 0, f"当前生命应独立-20并被判定[命零]，实际={m.current_hp}"
    assert not m.is_alive


def test_ruili_hp_cut_counts_as_damage_dealt():
    """边界：锐利造成的生命减少要计入本回合伤害，否则会被【凡庸】误判为无所作为"""
    e = _engine()
    cm = e.combat
    m = _monster(hp=200)
    cm.state.enemies = [m]
    p = cm.state.player
    p.damage_dealt_this_round = 0

    cm.apply_daowen_effect("锐利", {"blood_limit_reduction": 20, "hp_reduction": 20}, p, m)

    assert p.damage_dealt_this_round > 0, "锐利削减的生命必须计入伤害统计"


def test_ruili_hp_cut_never_below_zero():
    """错误输入/边界：扣减不得使生命变为负数，且不重复致死"""
    e = _engine()
    cm = e.combat
    m = _monster(hp=200)
    m.current_hp = 3
    cm.state.enemies = [m]

    cm.apply_daowen_effect("锐利", {"blood_limit_reduction": 40, "hp_reduction": 40}, cm.state.player, m)

    assert m.current_hp <= 0
    assert not m.is_alive
