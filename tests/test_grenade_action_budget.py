"""高爆手雷重做回归（用户裁定：全场20伤害 + 按伤害后生命线给【无力】）。

现行口径：「对全场所有敌方[目标]造成20点伤害；随后当前生命≥其[血限]50%的[目标]
获得【无力2】，否则获得【无力1】」。

为什么落在这一版：
1. 上一版重做（对一个[目标]出手次数-1、持续1回合）自造了一条"手雷减出手"状态，
   扣的却是与道纹【无力】同一笔出手预算——机制重复，而且只压一只怪一回合；
2. 平衡模拟实测两版差异 0.1 个百分点（2000 局/副本，扭曲都市通关率 7.5% vs 7.6%），
   等于没有影响；
3. 现行口径把伤害与削弱合成一发全场：20 点对一阶池怪物（血限 192~252）约 8~10%，
   真正的价值在【无力】——复用既有状态，不再自造同义状态名。

本文件钉住的口径：
- 伤害走 `_apply_hostile_damage` 统一漏斗（龙鳞减伤／嫁祸转嫁／爆裂反噬都在里面）；
- 生命线判定在**伤害之后**：手雷自己打出的 20 点参与判定；恰好 50% 算高线（给 2）；
- 【无力】与道纹同源同口径：value=层数、remaining_rounds=-1（本场永久）、
  scope=BATTLE（战终清除）、极性由 add_status 按规则表标成减益；
- 同名状态自动叠加（两发手雷打同一只 → 层数相加，且仍只有一条状态记录）；
- 扣减点是出手预算唯一口径（怪物 single_round_action_count／其余角色 Entity.action_count），
  对敌方轮回者同样生效；
- 【定型】拦不住它（DM 裁定：定型只管道纹与面板，消耗品是外部来源）；
- 打死的目标不再挂【无力】；场上没有敌方目标时拒绝使用并退还耐久；
- 全场效果不需要 target_ref（AI 也不必再为它补目标）。

文末保留两条与手雷无关、但同属出手预算修复且依然成立的口径锁：
怪物命中数跟当前速度走（不读遗留 attack_count）、resolve 一律用 prepare 快照。

覆盖：正常 / 对照 / 边界 / 口径叠加 / 文档一致性。
"""
import os
import re
import sys
from pathlib import Path

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import TWISTED_TOOL_LIBRARY, GameEngine
from engine.enums import EffectPolarity, EffectScope
from engine.models import Consumable, DaoWen, DaoWenInstance, Entity, StatusEffect

NADE_EFFECT = TWISTED_TOOL_LIBRARY["高爆手雷"][1]


def _engine(suffix: str) -> GameEngine:
    engine = GameEngine(db_path=f"data/test_nade_{suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "沈昼", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "扭曲都市"})
    engine.execute_action("choose_discovered_relic", {
        "relic_name": setup["result"]["relic_choices"][0]})
    return engine


def _nade(uses: int = 2) -> Consumable:
    """每例新建：Consumable 是可变对象，跨用例共享会带脏耐久。"""
    return Consumable(name="高爆手雷", effect=NADE_EFFECT, current_uses=uses, max_uses=uses)


def _foe_reincarnator(name="陆沉", speed=5, mana=7, hp=200) -> Entity:
    """死斗对手：敌方侧的轮回者（属性统一后同样持有[速限]/[法限]）。"""
    foe = Entity(name=name, entity_type="轮回者", blood_limit=hp, current_hp=hp,
                 attack_count=0, attack_power=0, speed_limit=speed, mana_limit=mana)
    foe.current_speed = speed
    foe.current_mana = mana
    return foe


def _monster(name="畸变行者", speed=3, mana=8, hp=210) -> Entity:
    m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
               attack_count=speed, attack_power=mana,
               speed_limit=speed, mana_limit=mana)
    m.current_speed = speed
    m.current_mana = mana
    return m


def _give_daowen(entity: Entity, name: str = "爆裂", x: int = 1) -> None:
    """给测试怪挂一枚可发动道纹（prepare 会为它预留 1 次出手）。"""
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="2X", effect_formula=""),
        x_value=x)


def _in_combat(engine: GameEngine, *foes: Entity) -> None:
    engine.state.enemies = list(foes)
    engine.state.phase = "in_combat"
    engine.state.current_round = 2
    engine.combat.reset_monster_activation()


def _throw(engine: GameEngine, **params) -> dict:
    engine.state.consumables.append(_nade())
    return engine.execute_action("consume_item", {"name": "高爆手雷", **params})


def _entry(result: dict, name: str) -> dict:
    return next(t for t in result["targets"] if t["target"] == name)


# ========================================================================
# 正常：全场 20 伤害
# ========================================================================

def test_grenade_hits_every_alive_enemy_for_20():
    engine = _engine("aoe")
    a, b, c = _monster("甲怪"), _monster("乙怪", hp=250), _monster("丙怪", hp=192)
    _in_combat(engine, a, b, c)

    r = _throw(engine)
    assert r["success"], r
    res = r["result"]
    assert res["aoe"] is True and res["damage"] == 20
    assert [t["target"] for t in res["targets"]] == ["甲怪", "乙怪", "丙怪"]
    assert (a.current_hp, b.current_hp, c.current_hp) == (190, 230, 172)
    assert res["uses_remaining"] == 1, "耐久 2 → 1"


def test_grenade_needs_no_target_ref():
    """全场效果：不带 target 也能用；带了错的 target 也照样打全场。"""
    engine = _engine("no_target")
    a, b = _monster("甲怪"), _monster("乙怪")
    _in_combat(engine, a, b)

    assert _throw(engine)["success"]
    assert _throw(engine, target="根本不存在的怪")["success"]
    assert a.current_hp == b.current_hp == 170, "两发手雷都打满了全场"


# ========================================================================
# 生命线：≥50%血限→无力2，否则无力1（判定在伤害之后）
# ========================================================================

def test_wuli_stacks_by_hp_line_after_damage():
    engine = _engine("hp_line")
    healthy = _monster("高线怪", hp=210)          # 210-20=190 → 90% ≥50% → 无力2
    hurt = _monster("低线怪", hp=210)
    hurt.current_hp = 100                         # 100-20=80 → 38% <50% → 无力1
    _in_combat(engine, healthy, hurt)

    r = _throw(engine)
    assert r["success"], r
    assert healthy.get_status_value("无力") == 2
    assert hurt.get_status_value("无力") == 1
    assert _entry(r["result"], "高线怪")["wuli"] == 2
    assert _entry(r["result"], "低线怪")["wuli"] == 1


def test_grenade_can_push_a_monster_into_redemption():
    """边界：20 点把怪物压到血限10%以下且它没有原始道纹 → 触发【救赎】离场，
    离场的怪不再挂【无力】（它已经不是战斗单位了）。"""
    engine = _engine("redemption")
    m = _monster("残血怪", hp=210)
    m.current_hp = 30                             # 30-20=10 ≤ 21（血限10%）
    _in_combat(engine, m)

    r = _throw(engine)
    assert r["success"], r
    assert not m.is_alive, "被救赎离场"
    assert not m.has_status("无力")
    assert "wuli" not in _entry(r["result"], "残血怪")


def test_wuli_threshold_boundary_exactly_half_counts_as_high():
    engine = _engine("boundary")
    m = _monster("半血怪", hp=40)                 # 40-20=20 → 恰好 50%
    _in_combat(engine, m)

    r = _throw(engine)
    assert m.current_hp == 20 and m.blood_limit == 40
    assert m.get_status_value("无力") == 2, "恰好 50% 按「≥」算高线"
    assert _entry(r["result"], "半血怪")["wuli"] == 2


def test_wuli_judged_after_the_grenade_damage_not_before():
    """对照：若按伤害前生命线判定，这只怪会拿到 2 层而不是 1 层。"""
    engine = _engine("after_damage")
    m = _monster("临界怪", hp=38)                 # 38（100%）→ 18（47.4%）
    _in_combat(engine, m)

    assert m.current_hp * 2 >= m.blood_limit, "伤害前确实还站在高线上"
    assert _throw(engine)["success"]
    assert m.get_status_value("无力") == 1, "手雷自己打出的 20 点参与判定"


def test_killed_target_gets_no_wuli():
    engine = _engine("killed")
    m = _monster("脆皮怪", hp=20)
    _in_combat(engine, m)

    r = _throw(engine)
    assert r["success"], r
    assert not m.is_alive
    assert not m.has_status("无力"), "打死就不必再压制"
    assert "wuli" not in _entry(r["result"], "脆皮怪")
    assert r["result"]["uses_remaining"] == 1, "耐久照扣"


# ========================================================================
# 出手预算：与【无力】道纹同一笔账
# ========================================================================

def test_wuli_2_zeroes_the_monster_action_budget():
    engine = _engine("budget_zero")
    m = _monster()
    _give_daowen(m)
    _in_combat(engine, m)

    assert engine._action_budget_of(m) == 2, "怪物预算＝1攻+1纹"
    r = _throw(engine)
    assert engine.combat.single_round_action_count(m) == 0
    assert engine._action_budget_of(m) == 0
    assert _entry(r["result"], "畸变行者")["action_budget_after"] == 0

    prepared = engine.combat.prepare_monster_phase()
    assert prepared["actors"] == []
    assert prepared["skipped"][0]["reason"] == "出手预算已用尽(0/0)"


def test_wuli_1_leaves_only_the_daowen_action():
    engine = _engine("budget_one")
    m = _monster(hp=210)
    m.current_hp = 100                            # 挨完 20 → 80（38%），落在低线
    _give_daowen(m)
    _in_combat(engine, m)

    assert _throw(engine)["success"]
    assert m.get_status_value("无力") == 1
    assert engine._action_budget_of(m) == 1

    actor = engine.combat.prepare_monster_phase()["actors"][0]
    assert actor["action_budget"] == 1 and actor["actions_remaining"] == 1
    assert actor["base_attack_actions"] == 0, "预算只剩 1 次出手，已为可发动道纹预留"
    assert actor["base_hits_per_attack"] == 3, "每次出手的命中数不受影响"


def test_grenade_also_works_on_enemy_reincarnators():
    """旧病灶的对应锁：打在敌方轮回者身上同样生效（走 Entity.action_count 属性）。"""
    engine = _engine("foe_budget")
    foe = _foe_reincarnator()
    _in_combat(engine, foe)

    assert foe.action_count == 2
    assert _throw(engine)["success"]
    assert foe.current_hp == 180
    assert foe.get_status_value("无力") == 2
    assert foe.action_count == 0
    assert engine._action_budget_of(foe) == 0


# ========================================================================
# 生命周期：本场永久、战终清除、同名叠加
# ========================================================================

def test_wuli_from_grenade_is_permanent_in_battle_and_is_a_debuff():
    engine = _engine("permanent")
    m = _monster()
    _in_combat(engine, m)

    assert _throw(engine)["success"]
    status = next(s for s in m.status_effects if s.name == "无力")
    assert status.remaining_rounds == -1, "本场战斗内无限（不是只压一回合）"
    assert status.scope == EffectScope.BATTLE.value
    assert status.polarity == EffectPolarity.DEBUFF.value, "按规则表标成减益"
    assert status.source == "高爆手雷"

    m.tick_status_effects()
    assert m.get_status_value("无力") == 2, "永久状态不被回合 tick 清掉"
    assert engine._action_budget_of(m) == 0


def test_wuli_from_grenade_is_cleared_at_battle_end():
    engine = _engine("battle_end")
    m = _monster()
    _in_combat(engine, m)

    assert _throw(engine)["success"]
    assert m.has_status("无力")
    m.current_hp = 0                              # 战斗结束的前置：敌方全灭
    m.is_alive = False
    ended = engine.execute_action("battle_end", {})
    assert ended["success"], ended
    assert not m.has_status("无力"), "scope=BATTLE：只压本场战斗，不跨场"


def test_two_grenades_stack_into_one_wuli_record():
    engine = _engine("stack")
    m = _monster(hp=400)
    _in_combat(engine, m)

    assert _throw(engine)["success"]
    assert _throw(engine)["success"]
    assert m.get_status_value("无力") == 4, "同名状态数值相加"
    assert len([s for s in m.status_effects if s.name == "无力"]) == 1, "仍只有一条记录"
    assert engine._action_budget_of(m) == 0, "下限为0，不出负数"


# ========================================================================
# 不影响的东西：攻次、命中数、面板
# ========================================================================

def test_grenade_leaves_attack_count_and_hits_alone():
    engine = _engine("hits_alone")
    m = _monster(speed=3)
    _give_daowen(m)
    _in_combat(engine, m)

    before = engine.combat.prepare_monster_phase()
    assert before["actors"][0]["base_hits_per_attack"] == 3

    assert _throw(engine)["success"]

    assert m.effective_attack_count() == 3
    assert m.attack_count == 3, "遗留面板字段不被写穿"
    assert m.speed_limit == 3 and m.current_speed == 3
    assert m.blood_limit == 210
    assert m.is_alive and not m.is_sculptured


def test_dingxing_does_not_block_the_grenade():
    """DM 裁定：【定型】锁的是攻次/攻力，拦不住消耗品的伤害与【无力】。"""
    engine = _engine("dingxing")
    foe = _foe_reincarnator(name="白骨祭坛", speed=6)
    _in_combat(engine, foe)
    foe.add_status(StatusEffect(name="定型", value=1, remaining_rounds=3, source="测试"))

    assert foe.effective_attack_count() == 6
    assert _throw(engine)["success"]
    assert foe.current_hp == 180 and foe.get_status_value("无力") == 2
    assert foe.effective_attack_count() == 6, "攻次没被动过"
    assert engine._action_budget_of(foe) == 0, "定型不拦手雷"


# ========================================================================
# 错误输入
# ========================================================================

def test_no_enemies_refuses_and_refunds_durability():
    engine = _engine("no_foes")
    engine.state.phase = "in_combat"
    engine.state.enemies = []
    item = _nade()
    engine.state.consumables.append(item)

    r = engine.execute_action("consume_item", {"name": "高爆手雷"})
    assert r["success"] is False
    assert r["error"] == "场上没有敌方目标"
    assert item.current_uses == 2, "没打出去就不扣耐久"


def test_dead_enemies_are_not_counted_as_targets():
    engine = _engine("all_dead")
    m = _monster()
    m.current_hp = 0
    m.is_alive = False
    _in_combat(engine, m)

    item = _nade()
    engine.state.consumables.append(item)
    r = engine.execute_action("consume_item", {"name": "高爆手雷"})
    assert r["success"] is False
    assert item.current_uses == 2


# ========================================================================
# 文档一致性：引擎效果文本与《物品索引》逐字一致
# ========================================================================

def _item_index_line(name: str) -> str:
    text = (Path(__file__).resolve().parent.parent / "物品索引.md").read_text(encoding="utf-8")
    matched = re.search(rf"^#### {re.escape(name)}\n(.+)$", text, re.M)
    assert matched, f"物品索引.md 缺【{name}】条目"
    return matched.group(1).strip()


def test_tool_text_matches_item_index_verbatim():
    durability, effect = TWISTED_TOOL_LIBRARY["高爆手雷"]
    assert durability == 2
    assert effect == ("对全场所有敌方[目标]造成20点伤害；随后当前生命≥其[血限]50%的[目标]"
                      "获得【无力2】，否则获得【无力1】")
    assert _item_index_line("高爆手雷") == f"消耗品（耐久{durability}）：{effect}"


def test_no_stale_grenade_wording_left_in_engine():
    """旧口径（15点伤害 / 攻击次数-1 / 出手次数-1 / 自造状态）不得在引擎里留尾巴。"""
    root = Path(__file__).resolve().parent.parent
    from tests.source_scan import combat_relative_paths
    for relative in ("engine/api.py", "engine/models.py",
                     "engine/daowen.py", "engine/ai_tactics.py",
                     *combat_relative_paths()):  # combat.py 已拆出 combat_parts/*.py
        source = (root / relative).read_text(encoding="utf-8")
        for stale in ("手雷减攻", "手雷减出手"):
            assert stale not in source, f"{relative} 仍在读写已废状态{stale}"
    text = TWISTED_TOOL_LIBRARY["高爆手雷"][1]
    assert "出手次数-1" not in text and "造成15点伤害" not in text


# ========================================================================
# 保留：与手雷无关、但同属出手预算修复的口径锁
# ========================================================================

def test_monster_hits_follow_current_speed_not_legacy_field():
    engine = _engine("monster_speed")
    m = _monster(speed=3)
    _in_combat(engine, m)

    prepared = engine.combat.prepare_monster_phase()
    assert prepared["actors"][0]["base_hits_per_attack"] == 3

    # 速度被削（闪避/减速同源）：命中数必须跟着掉，遗留字段不再说了算
    m.current_speed = 2
    prepared = engine.combat.prepare_monster_phase()
    assert prepared["actors"][0]["base_hits_per_attack"] == 2
    assert m.attack_count == 3, "遗留字段不动，读的已不是它"


def test_resolve_uses_prepare_snapshot_hits():
    """命中数按 prepare 快照校验：本阶段内速度被改（减速/闪避/前一个actor的道纹）
    不影响本阶段已提交的命中数 —— 与出手数「快照即契约」同一条规则。"""
    engine = _engine("snapshot")
    m = _monster(speed=3)
    _in_combat(engine, m)

    prepared = engine.combat.prepare_monster_phase()
    actor = prepared["actors"][0]
    hits_per_action = actor["base_hits_per_attack"]
    assert hits_per_action == 3

    # 模拟同阶段内的速度变化（例如另一只怪的道纹在 prepare 之后落下）
    m.current_speed = 1

    target_ref = actor["attack_target_options"][0]["ref"]
    target_option = next(t for t in actor["attack_target_options"] if t["ref"] == target_ref)
    spell_choices = {
        timing: {sp["spell_name"]: {"use": False}
                 for sp in target_option.get("spell_options", {}).get(timing, [])}
        for timing in ("before", "after")
    }
    choices = [{
        "actor_ref": actor["actor_ref"],
        "daowen": None,
        "attack_actions": [
            {"hits": [{"target_ref": target_ref, "dodge": False, "blood_shadow": False,
                       "spell_choices": spell_choices}
                      for _ in range(hits_per_action)]}
            for _ in range(actor["base_attack_actions"])
        ],
    }]
    details = engine.combat.resolve_monster_phase(choices, prepared=prepared)
    resolved_hits = [d for d in details if "damage_dealt" in d or "dodge_success" in d]
    assert len(resolved_hits) == hits_per_action, f"应按快照打满{hits_per_action}击：{details}"
    assert all(d.get("hit_total") == hits_per_action for d in resolved_hits)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
