"""
pytest 风格测试 - 里程碑8：修复5件坏掉/缺失的遗物（血誓戒/买路财/同魂笔/钱袋/忘忧香）

背景（用户直接点名要求先修再继续做初拥之夜/终音法器）：审计发现遗物池13件里
只有7件真正在战斗中生效，血誓戒完全没做、买路财只有计算没有执行动作、同魂笔只生成
一条虚假日志不改任何状态、钱袋的触发点从未被调用过是死代码、忘忧香(第13件)压根没注册。

覆盖范围：
1. 血誓戒：[回始]玩家首次主动支付流血代价获得等量格挡/低血量时改为等量生命，每回合限一次
2. 买路财：新增retreat_via_toll真正执行撤退(扣碎片/生命、清空战场)，不再只是算个数字
3. 同魂笔：真正让施法者永久获得"第二目标"道纹残韵变化后的新道纹，第二目标自身不受影响
4. 钱袋：改为在battle_end随标准击杀奖励一并结算(用battle_start_blood_limit快照)，不再是死代码
5. 忘忧香：补齐为遗物池第13件，并实现对应的"忘忧"局外行动

运行方式：
    python -m pytest tests/test_relic_fixes.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.api import GameEngine
from engine.models import Relic, DaoWen, DaoWenInstance, Entity


def _new_engine(db_suffix: str, daowen="杀伐") -> GameEngine:
    engine = GameEngine(db_path=f"data/test_relicfix_{db_suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {"blood_points": 10, "speed_points": 8, "mana_points": 7})
    engine.execute_action("setup_choose_daowen", {"daowen": daowen})
    return engine


def _give_daowen(entity, name, cost_type="消耗"):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type=cost_type, cost_formula="X", effect_formula=""))


def _start_with_enemy(engine, enemy):
    """battle_start会自动出怪，这里先start再替换为受控的测试怪物"""
    engine.execute_action("battle_start", {})
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
    """同魂笔正常路径：施法者真正永久获得第二目标道纹残韵变化后的新道纹，第二目标自身不变"""
    engine = _new_engine("tongtonbi_ok")
    engine.state.relics.append(Relic(name="同魂笔", effect=""))
    engine.state.resonance["反转"] = 1
    _give_daowen(engine.state.player, "杀伐")
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    enemy = Entity(name="怪甲", entity_type="怪物", blood_limit=100, current_hp=100)
    _give_daowen(enemy, "固执", cost_type="冷却")
    engine.state.enemies.append(enemy)
    engine.execute_action("round_start", {})

    r = engine.execute_action("use_resonance", {
        "source_daowen": "杀伐", "resonance_type": "反转",
        "second_target": "怪甲", "second_source_daowen": "固执",
    })
    assert r["success"] is True
    assert "血债" in engine.state.player.dao_wen, "施法者应永久获得固执反转后的血债"
    assert list(enemy.dao_wen.keys()) == ["固执"], "第二目标自身的道纹不应被改变"


def test_moneybag_adds_bonus_shards_at_battle_end():
    """钱袋正常路径：战终结算击杀奖励时应包含钱袋的额外2%[战始][血限]碎片"""
    engine = _new_engine("moneybag_ok")
    engine.state.relics.append(Relic(name="钱袋", effect=""))
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    monster = Entity(name="待宰怪", entity_type="怪物", blood_limit=100, current_hp=100)
    engine.state.enemies.append(monster)
    monster.current_hp = 0
    monster.is_alive = False
    engine.state.energy = 3
    r = engine.execute_action("battle_end", {})
    assert r["result"]["shard_reward"] == 4, "标准2%(=2) + 钱袋额外2%(=2) = 4"


def test_moneybag_uses_battle_start_snapshot_not_current_blood_limit():
    """钱袋边界：即使战斗中血限发生变化(如增殖)，奖励也应按[战始]快照计算，不受影响"""
    engine = _new_engine("moneybag_snapshot")
    engine.state.relics.append(Relic(name="钱袋", effect=""))
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    monster = Entity(name="膨胀怪", entity_type="怪物", blood_limit=100, current_hp=100)
    engine.state.enemies.append(monster)
    monster.blood_limit = 500  # 战斗中被动态改变(如增殖)，但战始快照应保持100
    monster.current_hp = 0
    monster.is_alive = False
    engine.state.energy = 3
    r = engine.execute_action("battle_end", {})
    assert r["result"]["shard_reward"] == 4, "应按战始快照100算(2%+2%=4)，不是当前500(会算出20)"


def test_wangyouxiang_registered_as_thirteenth_relic():
    """忘忧香正常路径：应作为遗物池第13件被注册"""
    engine = _new_engine("wangyou_registered")
    engine._init_relic_pool()
    names = {r.name for r in engine.state.relics_pool}
    assert "忘忧香" in names
    assert len(engine.RELIC_DEFS) == 13


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


def test_tonghunbi_rejected_when_second_target_lacks_daowen():
    """错误输入：第二目标不持有指定道纹时，不应产生任何状态变化"""
    engine = _new_engine("tongtonbi_missing")
    engine.state.relics.append(Relic(name="同魂笔", effect=""))
    engine.state.resonance["反转"] = 1
    _give_daowen(engine.state.player, "杀伐")
    engine.execute_action("battle_start", {})
    engine.state.enemies.clear()
    enemy = Entity(name="怪乙", entity_type="怪物", blood_limit=100, current_hp=100)
    engine.state.enemies.append(enemy)
    engine.execute_action("round_start", {})

    r = engine.execute_action("use_resonance", {
        "source_daowen": "杀伐", "resonance_type": "反转",
        "second_target": "怪乙", "second_source_daowen": "固执",
    })
    assert r["success"] is True  # 主残韵仍然成功，只是second分支未生效
    assert "未生效" in r["second_target_log"]
    assert "血债" not in engine.state.player.dao_wen


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
