"""
pytest 风格测试 - 里程碑8：修复5件坏掉/缺失的遗物（血誓戒/买路财/同魂笔/第一杯(原钱袋)/忘忧香）

历史背景：早期遗物池中只有7件真正在战斗中生效，血誓戒完全没做、买路财只有计算没有执行动作、同魂笔只生成
一条虚假日志不改任何状态、钱袋的触发点从未被调用过是死代码、忘忧香曾未注册。钱袋已删除，其免疫癌变效果并入【第一杯】，当前遗物池为11件。

覆盖范围：
1. 血誓戒：[回始]玩家首次主动支付流血代价获得等量格挡/低血量时改为等量生命，每回合限一次
2. 买路财：新增retreat_via_toll真正执行撤退(扣碎片/生命、清空战场)，不再只是算个数字
3. 同魂笔：第二目标的道纹也永久变为变化后的道纹，施法者同时永久获得
4. 第一杯：持有者不再受到癌变事件影响（承接原钱袋效果）；朋友/员工不继承
5. 忘忧香：保持在当前12件遗物池中，并实现对应的"忘忧"局外行动

运行方式：
    python -m pytest tests/test_relic_fixes.py -v
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.api import GameEngine
from engine.models import Relic, DaoWen, DaoWenInstance, Entity


def _new_engine(db_suffix: str, daowen="杀伐") -> GameEngine:
    engine = GameEngine(db_path=f"data/test_relicfix_{db_suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 10, "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    optional = {"折速法印", "三相残韵盘"}
    choice = next((n for n in setup["result"]["relic_choices"] if n not in optional),
                  setup["result"]["relic_choices"][0])
    engine.execute_action("choose_discovered_relic", {"relic_name": choice})
    if daowen != "杀伐":
        _give_daowen(engine.state.player, daowen)
    engine.state.energy = 3
    return engine


def _give_daowen(entity, name, cost_type="消耗"):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type=cost_type, cost_formula="X", effect_formula=""))


def _start_with_enemy(engine, enemy):
    """battle_start会自动出怪，这里先start再替换为受控的测试怪物"""
    engine.state.energy = 0
    choices = {r.name: {"use": False} for r in engine.state.relics
               if r.name in ("折速法印", "三相残韵盘")}
    engine.execute_action("battle_start", {"relic_choices": choices})
    engine.state.enemies.clear()
    engine.state.enemies.append(enemy)
    engine.execute_action("round_start", {})


# ========================================================================
# 正常路径
# ========================================================================

def test_blood_oath_ring_grants_shield_on_first_bleed_cost():
    """血誓戒正常路径：本回合首次流血代价，获得等量格挡"""
    engine = _new_engine("oath_shield", daowen="血债")
    engine.state.relics.append(Relic(name="血誓戒", effect=""))
    _give_daowen(engine.state.player, "血债", cost_type="流血")
    _start_with_enemy(engine, Entity(name="靶怪", entity_type="怪物", blood_limit=100, current_hp=100))

    hp_before = engine.state.player.current_hp
    r = engine.execute_action("use_daowen", {"daowen_name": "血债", "x": 5, "target": "靶怪"})
    assert r["success"] is True
    assert engine.state.player.shield == 5
    assert engine.state.player.current_hp == hp_before - 5


def test_blood_oath_ring_grants_life_instead_when_low_hp():
    """血誓戒正常路径：支付后生命≤30%时，改为获得等量生命而不是格挡"""
    engine = _new_engine("oath_life", daowen="血债")
    engine.state.relics.append(Relic(name="血誓戒", effect=""))
    _give_daowen(engine.state.player, "血债", cost_type="流血")
    engine.state.player.current_hp = 20  # blood_limit=60, 支付后20-15=5 <= 30%*60=18
    _start_with_enemy(engine, Entity(name="靶怪", entity_type="怪物", blood_limit=100, current_hp=100))

    r = engine.execute_action("use_daowen", {"daowen_name": "血债", "x": 15, "target": "靶怪"})
    assert r["success"] is True
    assert engine.state.player.shield == 0, "低血量时不应给格挡"
    assert engine.state.player.current_hp == 5 + 15, "应改为获得等量生命(15)"


def test_blood_oath_ring_only_once_per_round():
    """血誓戒正常路径：每回合只在首次流血代价时触发，第二次不再触发"""
    engine = _new_engine("oath_once", daowen="血债")
    engine.state.relics.append(Relic(name="血誓戒", effect=""))
    _give_daowen(engine.state.player, "血债", cost_type="流血")
    _start_with_enemy(engine, Entity(name="靶怪", entity_type="怪物", blood_limit=100, current_hp=100))

    engine.execute_action("use_daowen", {"daowen_name": "血债", "x": 3, "target": "靶怪"})
    shield_after_first = engine.state.player.shield
    engine.execute_action("use_daowen", {"daowen_name": "血债", "x": 4, "target": "靶怪"})
    assert engine.state.player.shield == shield_after_first, "同一回合第二次流血代价不应再给格挡"


def test_buyaicai_toll_actually_executes_retreat():
    """买路财正常路径：真正扣碎片并结束战斗，而不只是算出一个数字"""
    engine = _new_engine("toll_exec")
    engine.state.relics.append(Relic(name="买路财", effect=""))
    engine.state.shards = 100
    _start_with_enemy(engine, Entity(name="大怪", entity_type="怪物", blood_limit=100, current_hp=100))

    r = engine.execute_action("retreat_via_toll", {"target": "大怪"})
    assert r["success"] is True
    assert r["result"]["shard_paid"] == 20  # 100*20%
    assert engine.state.shards == 80
    assert engine.state.enemies == [], "撤退后战场应清空"
    assert engine.state.phase == "pre_battle"


def test_buyaicai_toll_uses_life_to_cover_shortfall():
    """买路财正常路径：碎片不足时按1碎片=2生命的比例用生命补足"""
    engine = _new_engine("toll_life")
    engine.state.relics.append(Relic(name="买路财", effect=""))
    engine.state.shards = 5  # 需要20，差15，需额外30点生命
    _start_with_enemy(engine, Entity(name="大怪", entity_type="怪物", blood_limit=100, current_hp=100))
    hp_before = engine.state.player.current_hp

    r = engine.execute_action("retreat_via_toll", {"target": "大怪"})
    assert r["success"] is True
    assert engine.state.shards == 0
    assert r["result"]["life_paid"] == 30
    assert engine.state.player.current_hp == hp_before - 30


def test_tonghunbi_grants_caster_real_daowen_from_second_target():
    """同魂笔正常路径：第二目标道纹永久变化，施法者同时永久获得变化后道纹"""
    engine = _new_engine("tongtonbi_ok")
    engine.state.relics.append(Relic(name="同魂笔", effect=""))
    engine.state.resonance["反转"] = 1
    _give_daowen(engine.state.player, "杀伐")
    engine.state.energy = 0
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    enemy = Entity(name="怪甲", entity_type="怪物", blood_limit=100, current_hp=100)
    _give_daowen(enemy, "固执", cost_type="冷却")
    engine.state.enemies.append(enemy)
    engine.execute_action("round_start", {})

    r = engine.execute_action("use_resonance", {
        "source_daowen": "杀伐", "resonance_type": "反转",
        "second_target_ref": "enemy:0", "second_source_daowen": "固执",
    })
    assert r["success"] is True
    assert "血债" in engine.state.player.dao_wen, "施法者应永久获得固执反转后的血债"
    assert "固执" not in enemy.dao_wen and "血债" in enemy.dao_wen


def test_moneybag_blocks_cancer():
    """第一杯正常路径：持有者累计回复达阈值也不触发癌变（原钱袋效果已并入第一杯）"""
    engine = _new_engine("moneybag_ok")
    engine.state.relics.append(Relic(name="第一杯", effect=""))
    player = engine.state.player
    player.total_healed = engine.combat.cancer_threshold_of(player)
    hit = engine.combat.check_cancer(player)
    assert hit is None
    assert player.is_alive and not player.is_proliferated


def test_moneybag_does_not_protect_allies():
    """第一杯边界：朋友/员工不继承免疫"""
    engine = _new_engine("moneybag_ally")
    engine.state.relics.append(Relic(name="第一杯", effect=""))
    friend = Entity(name="同伴", entity_type="朋友", blood_limit=40, current_hp=40)
    engine.state.friends.append(friend)
    friend.total_healed = engine.combat.cancer_threshold_of(friend)
    hit = engine.combat.check_cancer(friend)
    assert hit is not None
    assert not friend.is_alive and friend.is_proliferated


def test_wangyouxiang_registered_in_revised_relic_pool():
    """删除两件旧契约与钱袋（免疫癌变并入第一杯）后，忘忧香仍在11件遗物池中。"""
    engine = _new_engine("wangyou_registered")
    names = {n for n, _ in engine.RELIC_DEFS}
    assert "忘忧香" in names
    assert len(engine.RELIC_DEFS) == 11
    assert "血契" in names
    assert "钱袋" not in names


def test_wangyouxiang_action_loses_daowen_and_gains_shards():
    """忘忧香正常路径：忘忧行动按档位永久失去指定数量道纹、获得对应碎片"""
    engine = _new_engine("wangyou_action")
    engine.state.relics.append(Relic(name="忘忧香", effect=""))
    _give_daowen(engine.state.player, "再生")
    engine.state.energy = 3
    shards_before = engine.state.shards

    r = engine.execute_action("pre_battle_action", {
        "sub_action": "忘忧", "tier": 2, "daowen_names": ["杀伐", "再生"],
    })
    assert r["success"] is True
    assert engine.state.shards == shards_before + 55
    assert "杀伐" not in engine.state.player.dao_wen
    assert "再生" not in engine.state.player.dao_wen


# ========================================================================
# 错误输入 / 非法配置
# ========================================================================

def test_buyaicai_rejected_without_relic():
    """错误输入：没有买路财遗物不能使用此撤退方式"""
    engine = _new_engine("toll_no_relic")
    engine.state.relics = [r for r in engine.state.relics if r.name != "买路财"]
    engine.state.shards = 100
    _start_with_enemy(engine, Entity(name="大怪", entity_type="怪物", blood_limit=100, current_hp=100))
    r = engine.execute_action("retreat_via_toll", {"target": "大怪"})
    assert r["success"] is False
    assert engine.state.enemies, "被拒绝的撤退不应清空战场"


def test_buyaicai_rejected_when_insufficient_shards_and_life():
    """错误输入：碎片和生命都不足以支付时必须拒绝，不能扣成负数或直接杀死玩家"""
    engine = _new_engine("toll_broke")
    engine.state.relics.append(Relic(name="买路财", effect=""))
    engine.state.shards = 0
    _start_with_enemy(engine, Entity(name="大怪", entity_type="怪物", blood_limit=100, current_hp=100))
    engine.state.player.current_hp = 5  # 需要40点生命补足，但只有5点
    r = engine.execute_action("retreat_via_toll", {"target": "大怪"})
    assert r["success"] is False
    assert engine.state.player.current_hp == 5, "被拒绝的撤退不应扣血"
    assert engine.state.enemies, "被拒绝的撤退不应清空战场"


def test_wangyouxiang_rejected_without_relic():
    """错误输入：没有忘忧香不能执行忘忧"""
    engine = _new_engine("wangyou_no_relic")
    _give_daowen(engine.state.player, "再生")
    engine.state.energy = 3
    r = engine.execute_action("pre_battle_action", {"sub_action": "忘忧", "tier": 1, "daowen_names": ["再生"]})
    assert r["success"] is False
    assert "再生" in engine.state.player.dao_wen


def test_wangyouxiang_rejected_when_daowen_count_mismatches_tier():
    """错误输入：指定的道纹数量必须与档位一致，否则拒绝"""
    engine = _new_engine("wangyou_mismatch")
    engine.state.relics.append(Relic(name="忘忧香", effect=""))
    _give_daowen(engine.state.player, "再生")
    engine.state.energy = 3
    r = engine.execute_action("pre_battle_action", {"sub_action": "忘忧", "tier": 2, "daowen_names": ["再生"]})
    assert r["success"] is False
    assert "再生" in engine.state.player.dao_wen, "拒绝的行动不应扣除道纹"


def test_moneybag_does_not_add_kill_shards():
    """错误对照：第一杯不改写击杀碎片，标准奖励仍按血限2%+道纹×5"""
    engine = _new_engine("moneybag_no_shard")
    engine.state.relics.append(Relic(name="第一杯", effect=""))
    engine.state.energy = 0
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    monster = Entity(name="待宰怪", entity_type="怪物", blood_limit=100, current_hp=0)
    monster.is_alive = False
    engine.state.enemies.append(monster)
    r = engine.execute_action("battle_end", {})
    assert r["result"]["shard_reward"] == 2, "第一杯不得额外加碎片，100血限×2%=2"


def test_tonghunbi_rejected_when_second_target_lacks_daowen():
    """错误输入：第二目标不持有指定道纹时，不应产生任何状态变化"""
    engine = _new_engine("tongtonbi_missing")
    engine.state.relics.append(Relic(name="同魂笔", effect=""))
    engine.state.resonance["反转"] = 1
    _give_daowen(engine.state.player, "杀伐")
    engine.state.energy = 0
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    enemy = Entity(name="怪乙", entity_type="怪物", blood_limit=100, current_hp=100)
    engine.state.enemies.append(enemy)
    engine.execute_action("round_start", {})

    r = engine.execute_action("use_resonance", {
        "source_daowen": "杀伐", "resonance_type": "反转",
        "second_target_ref": "enemy:0", "second_source_daowen": "固执",
    })
    assert r["success"] is True  # 主残韵仍然成功，只是second分支未生效
    assert "未生效" in r["second_target_log"]
    assert "血债" not in engine.state.player.dao_wen


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
