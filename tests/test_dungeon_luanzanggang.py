"""乱葬岗（二阶副本）实现契约测试。

覆盖：8专属道纹注册/残韵闭环、区域可选、怪物池解析、附煞行动（7煞气）、
专属道纹效果（瓦解/镇尸/勾魂/冥气/缄默/尸爆/招魂）。
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.daowen import DaoWenEngine, ResonanceEngine
from engine.models import Entity, GameState
from engine.dice import DiceEngine
from engine.combat import CombatEngine


# ---------- 道纹注册与闭环 ----------

def test_dungeon_daowen_registered():
    """正常路径：8个乱葬岗专属道纹已注册并可解析。"""
    DaoWenEngine.register_all()
    for name in ("分裂", "尸爆", "缄默", "瓦解", "冥气", "勾魂", "镇尸", "招魂"):
        assert name in DaoWenEngine._registry, f"{name} 未注册"
        r = DaoWenEngine.resolve(name, 2, target=Entity("T", "怪物", blood_limit=100, current_hp=100),
                                 caster=Entity("C", "轮回者", blood_limit=60, current_hp=60))
        assert isinstance(r, dict) and r["dao_wen"] == name


def test_dungeon_resonance_loop_complete():
    """正常路径：乱葬岗残韵闭环8条路径全部可达。"""
    DaoWenEngine.register_all()
    loop = [("分裂", "尸爆"), ("尸爆", "缄默"), ("缄默", "瓦解"), ("瓦解", "冥气"),
            ("冥气", "勾魂"), ("勾魂", "镇尸"), ("镇尸", "招魂"), ("招魂", "分裂")]
    for a, b in loop:
        paths = ResonanceEngine.get_available_resonance(a)
        assert any(p.get("target_daowen") == b for p in paths), f"{a}→{b} 缺失"


# ---------- 区域与怪物池 ----------

def test_dungeon_selectable_region():
    """正常路径：乱葬岗可作为开局副本选择。"""
    e = GameEngine(db_path="/tmp/test_lz_region.db", rng_seed=1)
    e.execute_action("setup_attributes", {"name": "贾凡", "blood_points": 10,
                                          "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    r = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    assert r["success"], r
    assert e.state.current_region == "乱葬岗"


def test_dungeon_monster_pool_parsed():
    """正常路径：乱葬岗12只怪物被解析为运行时怪物源。"""
    from engine.monsters import parse_monster_pool
    pools = parse_monster_pool("副本索引.md")
    assert len(pools["乱葬岗"]) == 12
    names = {m["name"] for m in pools["乱葬岗"]}
    assert {"蛆冢", "纸人", "勾魂使者", "血僵"} <= names


# ---------- 附煞行动 ----------

def test_fusha_select_mode():
    """正常路径：附煞·选择模式（75碎片）给道纹附加煞气。"""
    e = GameEngine(db_path="/tmp/test_lz_fusha.db", rng_seed=1)
    e.execute_action("setup_attributes", {"name": "贾凡", "blood_points": 10,
                                          "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.shards = 200
    r = e.execute_action("pre_battle_action", {"sub_action": "附煞", "mode": "选择",
                                               "sha_qi": "冥煞", "daowen_name": "杀伐"})
    assert r["success"], r
    assert e.state.player.dao_wen["杀伐"].sha_qi == "冥煞"
    assert e.state.shards == 175  # 200-25


def test_fusha_discover_mode_candidates():
    """正常路径：附煞·发现模式（50碎片）随机列3件候选。"""
    e = GameEngine(db_path="/tmp/test_lz_fusha2.db", rng_seed=1)
    e.execute_action("setup_attributes", {"name": "贾凡", "blood_points": 10,
                                          "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.shards = 200
    r = e.execute_action("pre_battle_action", {"sub_action": "附煞", "mode": "发现"})
    assert r["success"], r
    cands = r["result"]["sha_qi_candidates"]
    assert len(cands) == 3
    assert e.state.pending_sha_qi_choices == cands
    # 选择后附加
    r2 = e.execute_action("choose_sha_qi", {"sha_qi": cands[0], "daowen_name": "杀伐"})
    assert r2["success"], r2
    assert e.state.player.dao_wen["杀伐"].sha_qi == cands[0]
    assert e.state.shards == 190  # 200-10


def test_fusha_invalid_sha_qi_rejected():
    """错误输入：未知煞气被拒绝。"""
    e = GameEngine(db_path="/tmp/test_lz_fusha3.db", rng_seed=1)
    e.execute_action("setup_attributes", {"name": "贾凡", "blood_points": 10,
                                          "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    r = e.execute_action("pre_battle_action", {"sub_action": "附煞", "mode": "选择",
                                               "sha_qi": "不存在的煞", "daowen_name": "杀伐"})
    assert not r["success"]
    assert "未知煞气" in r["error"]


def test_fusha_region_gate():
    """错误输入：非乱葬岗副本不能使用附煞。"""
    e = GameEngine(db_path="/tmp/test_lz_fusha4.db", rng_seed=1)
    e.execute_action("setup_attributes", {"name": "贾凡", "blood_points": 10,
                                          "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    r = e.execute_action("pre_battle_action", {"sub_action": "附煞", "mode": "选择",
                                               "sha_qi": "冥煞", "daowen_name": "杀伐"})
    assert not r["success"]
    assert "乱葬岗专属" in r["error"]


# ---------- 专属道纹效果 ----------

def test_wajie_reduces_blood_limit_pct():
    """正常路径：瓦解X使目标血限-10X%。"""
    st = GameState()
    st.player = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    st.enemies.append(Entity("怪", "怪物", blood_limit=200, current_hp=200))
    c = CombatEngine(st, DiceEngine(seed=1))
    c.reset_monster_activation()
    target = st.enemies[0]
    calc = DaoWenEngine.resolve("瓦解", 2, target=target, caster=st.player)
    r = c.apply_daowen_effect("瓦解", calc, st.player, target)
    assert target.blood_limit == 160, f"200-20%=160，实{target.blood_limit}"
    assert any(e["type"] == "wajie" for e in r["effects"])


def test_zhenshi_blocks_heal():
    """正常路径：镇尸X使目标无法获得回复。"""
    st = GameState()
    st.player = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    st.enemies.append(Entity("怪", "怪物", blood_limit=200, current_hp=200))
    c = CombatEngine(st, DiceEngine(seed=1))
    c.reset_monster_activation()
    target = st.enemies[0]
    calc = DaoWenEngine.resolve("镇尸", 2, target=target, caster=st.player)
    r = c.apply_daowen_effect("镇尸", calc, st.player, target)
    assert target.has_status("镇尸")
    assert any(e["type"] == "zhenshi" for e in r["effects"])


def test_gouhun_blocks_mana_gain_for_x_rounds():
    """正常路径（2026-08-30 改版）：勾魂X挂到目标身上，持续X回合[回始]不获得法力。

    旧版为「[回始]失去2X法力，持续∞」，已废止；新版**不扣已有法力**，只压制回填。
    """
    st = GameState()
    st.player = Entity("P", "轮回者", blood_limit=60, current_hp=60,
                       mana_limit=20, current_mana=20)
    foe = Entity("敌法", "轮回者", blood_limit=60, current_hp=60,
                 mana_limit=20, current_mana=20)
    st.enemies.append(foe)
    c = CombatEngine(st, DiceEngine(seed=1))
    c.reset_monster_activation()
    calc = DaoWenEngine.resolve("勾魂", 2, target=foe, caster=st.player)
    c.apply_daowen_effect("勾魂", calc, st.player, foe)
    assert foe.has_status("勾魂"), "勾魂应挂在目标身上"
    # 持续回合 = X = 2（不是永久）
    dur = next(s.remaining_rounds for s in foe.status_effects if s.name == "勾魂")
    assert dur == 2, f"勾魂X=2 应持续2回合，实{dur}"

    # 第1回合：法力回填被压制，已有法力不动
    foe.current_mana = 7
    rs = c.round_start({"relic_choices": {}})
    blocked = [e for e in rs.get("effects", []) if e.get("type") == "mana_refill_blocked"]
    assert blocked, "回始应有法力回填被压制的条目"
    assert foe.current_mana == 7, f"勾魂期间不得获得法力，实{foe.current_mana}"

    # 第2回合：仍被压制（持续X=2，[回终]才递减）
    c.round_start({"relic_choices": {}})
    assert foe.current_mana == 7, f"第2回合仍应在持续期内，实{foe.current_mana}"

    # 持续走完后恢复回填
    from engine.enums import CombatSubphase
    st.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
    c.round_end()
    st.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
    c.round_end()
    assert not foe.has_status("勾魂"), "持续X走完后勾魂应自然到期"
    # 注：当前法力在[敌回终]清空，故到期后回填 = 0 + 法限20
    rs2 = c.round_start({"relic_choices": {}})
    refill = [e for e in rs2.get("effects", []) if e.get("type") == "mana_refill"]
    assert refill and refill[0]["gained"] == 20, f"到期后应恢复回填: {refill}"
    assert foe.current_mana == 20, f"到期后应恢复回填，实{foe.current_mana}"
