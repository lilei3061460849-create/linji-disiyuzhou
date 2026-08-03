"""
回归测试：本轮修复项
1. 怪物×3废案已彻底移除
2. use_spell / consume_item 不再崩溃
3. 法术系统（积木/中断/循环/阶级规则）
4. 代价系统真实生效（冷却/唯一/异变/多段伤害）
5. 内容库（遗物池/事件池/怪物池/出怪公式）
"""
import sys
import os
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Entity, DaoWenInstance, Consumable
from engine.daowen import DaoWenEngine
from engine import content


def new_engine():
    d = tempfile.mkdtemp()
    return GameEngine(db_path=os.path.join(d, "t.db"), save_dir=d)


def setup_player(e, daowen=("杀伐",)):
    e.execute_action("setup_attributes",
                     {"blood_points": 10, "speed_points": 8, "mana_points": 7})
    for n in daowen:
        e.state.player.dao_wen[n] = DaoWenInstance(dao_wen=DaoWenEngine.get_definition(n))
    return e.state.player


def add_monster(e, name="怪", hp=200):
    m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp)
    e.state.enemies.append(m)
    return m


def test_no_triple_rule():
    """怪物×3废案彻底移除"""
    from engine.combat import CombatEngine
    assert not hasattr(CombatEngine, "is_monster_triple"), "is_monster_triple 应已删除"
    assert not hasattr(CombatEngine, "MONSTER_OWN_DAOWEN"), "MONSTER_OWN_DAOWEN 应已删除"

    # 怪物发动庇护4 应为 16 格挡（4X），而非 48
    e = new_engine()
    setup_player(e)
    m = add_monster(e)
    m.dao_wen["庇护"] = DaoWenInstance(dao_wen=DaoWenEngine.get_definition("庇护"))
    calc = DaoWenEngine.resolve("庇护", 4, target=m, caster=m)
    e._execute_daowen_effect("庇护", calc, m, m)
    assert m.shield == 16, f"庇护4应给16格挡（4X），实际{m.shield}"
    print("  ✓ ×3规则已移除，庇护4=16格挡")


def test_actions_do_not_crash():
    """use_spell / consume_item 不再抛 AttributeError"""
    e = new_engine()
    setup_player(e)
    r = e.execute_action("use_spell", {"spell_name": "先发制人", "x": 1})
    assert "object has no attribute" not in str(r.get("error", "")), r
    assert r["success"] is False and "未掌握" in r["error"]

    r = e.execute_action("consume_item", {"item_name": "不存在"})
    assert "object has no attribute" not in str(r.get("error", "")), r
    assert r["success"] is False
    print("  ✓ use_spell / consume_item 不再崩溃")


def test_spell_learn_and_cast():
    """学习法术 → 发动法术"""
    e = new_engine()
    p = setup_player(e)
    r = e.execute_action("pre_battle_action",
                         {"sub_action": "学习", "sub": "spell", "spell_name": "先发制人"})
    assert r["success"], r
    assert r["result"]["learned_spells"][0]["rank"] == 1

    m = add_monster(e)
    e.execute_action("round_start")
    r = e.execute_action("use_spell", {"spell_name": "先发制人", "x": 3, "target": "怪"})
    assert r["success"], r
    assert r["total_damage_dealt"] == 6, r["total_damage_dealt"]
    assert m.current_hp == 194
    print("  ✓ 学习+发动法术：杀伐3 造成6伤害")


def test_spell_building_block_rule():
    """积木规则：缺少所需道纹无法发动"""
    e = new_engine()
    p = setup_player(e, daowen=("杀伐",))
    e.execute_action("pre_battle_action",
                     {"sub_action": "学习", "sub": "spell", "spell_name": "借力打力"})
    add_monster(e)
    e.execute_action("round_start")
    r = e.execute_action("use_spell",
                         {"spell_name": "借力打力", "variables": {"X": 1, "Y": 1}, "target": "怪"})
    assert r["success"] is False and "积木规则" in r["error"], r
    print("  ✓ 积木规则：缺【庇护】无法发动借力打力")


def test_spell_loop_and_interrupt():
    """循环规则 + 中断规则"""
    e = new_engine()
    p = setup_player(e, daowen=("血债", "再生"))
    e.execute_action("pre_battle_action",
                     {"sub_action": "学习", "sub": "spell", "spell_name": "千刀万剐"})
    m = add_monster(e, hp=300)
    e.execute_action("round_start")
    r = e.execute_action("use_spell",
                         {"spell_name": "千刀万剐", "variables": {"X": 2}, "target": "怪"})
    assert r["success"], r
    assert r["loops_run"] > 1, "循环规则应产生多次循环"
    assert r["total_damage_dealt"] > 0, "多段伤害应被计入"
    assert r["interrupted"] and "法力" in r["interrupted"], "法力耗尽应中断"
    assert m.current_hp < 300
    print(f"  ✓ 循环规则：{r['loops_run']}次循环/{r['total_damage_dealt']}伤害，法力耗尽中断")


def test_multi_hit_damage():
    """血债多段伤害逐段结算"""
    e = new_engine()
    p = setup_player(e, daowen=("血债",))
    m = add_monster(e)
    m.shield = 3          # 3点格挡应逐段吸收3段
    e.execute_action("round_start")
    r = e.execute_action("use_daowen", {"daowen_name": "血债", "x": 3, "target": "怪"})
    hit = next(x for x in r["execution"]["effects"] if x["type"] == "multi_hit_damage")
    assert hit["hits"] == 6, hit
    assert hit["shield_absorbed"] == 3, hit
    assert hit["actual_damage"] == 3, hit
    print("  ✓ 血债3：6段×1伤害，格挡逐段吸收3点")


def test_cooldown_cost():
    """冷却代价真实生效并在[战终]递减"""
    e = new_engine()
    p = setup_player(e, daowen=("固执",))
    add_monster(e)
    e.execute_action("round_start")
    r = e.execute_action("use_daowen", {"daowen_name": "固执", "x": 2})
    assert r["cost_applied"]["cooldown"] == 2, r

    r2 = e.execute_action("use_daowen", {"daowen_name": "固执", "x": 2})
    assert r2["success"] is False and "冷却" in r2["error"]

    e.execute_action("battle_end")
    assert p.dao_wen["固执"].cooldown_remaining == 1
    e.execute_action("battle_end")
    assert p.dao_wen["固执"].cooldown_remaining == 0
    assert p.dao_wen["固执"].can_use()
    print("  ✓ 冷却2：发动后锁定，两次[战终]后恢复可用")


def test_mutation_cost():
    """异变代价累积，达50层变为怪物"""
    e = new_engine()
    p = setup_player(e)
    m = add_monster(e)
    m.dao_wen["狂暴"] = DaoWenInstance(dao_wen=DaoWenEngine.get_definition("狂暴"))
    calc = DaoWenEngine.resolve("狂暴", 2, target=m, caster=m)
    r = e._execute_daowen_effect("狂暴", calc, m, m)
    eff = next(x for x in r["effects"] if x["type"] == "mutation_cost")
    assert eff["mutation_gained"] == 10, eff
    assert m.mutation_stacks == 10

    m.mutation_stacks = 45
    r = e._execute_daowen_effect("狂暴", calc, m, m)
    eff = next(x for x in r["effects"] if x["type"] == "mutation_cost")
    assert eff.get("became_monster") is True, eff
    print("  ✓ 异变代价累积，达50层触发怪物化")


def test_consumable():
    """消耗品使用与耗尽"""
    e = new_engine()
    setup_player(e)
    e.state.consumables.append(
        Consumable(name="绝息淤泥", effect="逃脱", current_uses=2, max_uses=2))
    r = e.execute_action("consume_item", {"item_name": "绝息淤泥"})
    assert r["success"] and r["result"]["uses_remaining"] == 1
    assert r["result"]["costs_action"] is False
    r = e.execute_action("consume_item", {"item_name": "绝息淤泥"})
    assert r["result"]["depleted"] is True
    r = e.execute_action("consume_item", {"item_name": "绝息淤泥"})
    assert r["success"] is False
    print("  ✓ 消耗品：2次用尽后移除，使用不消耗出手")


def test_content_library():
    """内容库完整性"""
    assert len(content.RELIC_POOL) == 13, len(content.RELIC_POOL)
    assert len(content.GENERAL_EVENTS) == 10
    for region in ("扭曲都市", "罪孽都市", "龙心谷"):
        assert len(content.MONSTER_POOLS[region]) == 12, region
    m = content.get_monster("扭曲都市", "千手蜈蚣")
    assert m.panel() == "千手蜈蚣（6×8/120，畸变2，狂暴2，活力4）"
    ent = m.to_entity()
    assert ent.current_hp == 120 and ent.dao_wen["活力"].x_value == 4
    assert ent.mana_limit == 0, "怪物不持有法力"
    print("  ✓ 内容库：13遗物/10事件/36怪物面板")


def test_monster_draw_formula():
    """出怪公式：数量=场数，一阶-2，最低1"""
    assert content.monster_count_for_battle(1) == 1
    assert content.monster_count_for_battle(3) == 1
    assert content.monster_count_for_battle(4) == 2
    assert content.monster_count_for_battle(7) == 5

    e = new_engine()
    setup_player(e)
    e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.state.current_battle = 4          # 下一场为第5场 → 3只
    r = e.execute_action("battle_start")
    assert r["monster_count"] == 3, r
    for n in (1, 1, 3):
        r = e.execute_action("random_number", {"pool_name": "monster_draw", "number": n})
    assert len(e.state.enemies) == 3
    names = [x.name for x in e.state.enemies]
    assert names[0] == "千手蜈蚣·1" and names[1] == "千手蜈蚣·2", names
    print("  ✓ 出怪公式：第5场3只，允许重复抽选")


def test_relic_and_event_pools():
    """共鸣发现遗物 / 探索发现事件"""
    e = new_engine()
    setup_player(e)
    r = e.execute_action("pre_battle_action", {"sub_action": "共鸣"})
    assert r["random_required"] and r["energy_remaining"] == 1, r
    r = e.execute_action("random_number", {"pool_name": "relic_discover", "number": 1})
    assert r["result"]["gained_relic"]["name"] == "血誓戒", r
    assert len(e.state.relics_pool) == 12

    r = e.execute_action("pre_battle_action", {"sub_action": "探索"})
    assert r["pool_range"] == "1~10", r
    r = e.execute_action("random_number", {"pool_name": "event_discover", "number": 1})
    assert r["result"]["event"]["name"] == "无名冢"
    print("  ✓ 共鸣发现遗物 / 探索发现事件")


def test_learn_transform_daowen():
    """局外学习只能习得转化道纹，不能习得原始怪物道纹"""
    e = new_engine()
    p = setup_player(e)
    r = e.execute_action("pre_battle_action",
                         {"sub_action": "学习", "sub": "daowen", "daowen_name": "狂暴"})
    assert r["success"] is False and "转化道纹" in r["error"], r

    r = e.execute_action("pre_battle_action",
                         {"sub_action": "学习", "sub": "daowen", "daowen_name": "兴奋"})
    assert r["success"] and "兴奋" in p.dao_wen
    print("  ✓ 学习：可习得转化道纹【兴奋】，拒绝原始道纹【狂暴】")


def test_custom_spell():
    """自创法术：积木规则 + 阶级规则"""
    e = new_engine()
    p = setup_player(e, daowen=("杀伐", "庇护"))
    r = e.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "create_spell",
        "name": "铁壁反击", "trigger_condition": "受到伤害前",
        "steps": [{"daowen": "庇护", "var": "X"}, {"daowen": "杀伐", "var": "Y"}],
    })
    assert r["success"], r
    assert r["result"]["rank"] == 2, r
    assert any(s.name == "铁壁反击" for s in p.spells)

    # 使用未拥有的道纹应被拒绝
    r = e.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "create_spell",
        "name": "非法术", "trigger_condition": "受到伤害前",
        "steps": [{"daowen": "血债", "var": "X"}],
    })
    assert r["success"] is False and "积木规则" in r["error"], r
    print("  ✓ 自创法术：2阶法术创建成功，未持有道纹被拒绝")


ALL_TESTS = [
    test_no_triple_rule,
    test_actions_do_not_crash,
    test_spell_learn_and_cast,
    test_spell_building_block_rule,
    test_spell_loop_and_interrupt,
    test_multi_hit_damage,
    test_cooldown_cost,
    test_mutation_cost,
    test_consumable,
    test_content_library,
    test_monster_draw_formula,
    test_relic_and_event_pools,
    test_learn_transform_daowen,
    test_custom_spell,
]


def main():
    print("=" * 60)
    print("回归测试：本轮修复项")
    print("=" * 60)
    passed = failed = 0
    for t in ALL_TESTS:
        print(f"\n=== {t.__name__} ===")
        try:
            t()
            passed += 1
        except Exception as ex:
            import traceback
            traceback.print_exc()
            print(f"  ✗ 失败: {ex}")
            failed += 1
    print("\n" + "=" * 60)
    print(f"测试结果: {passed} 通过, {failed} 失败")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
