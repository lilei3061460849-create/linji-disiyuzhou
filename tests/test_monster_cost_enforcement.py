"""
怪物代价类道纹支付与防复读规则测试。
规则：怪物不持有法力，但发动代价类道纹时必须照常支付对应代价：
- 发动【冷却X】道纹（如固执/束缚/迟滞/缓慢）后立即进入冷却，同场内禁止复读；
- 发动【流血X】（如血债/慈悲）扣除自身生命，生命不足无法发动；
- 发动【衰老X】扣除血限，发动【疲惫X】扣除速度，发动【异变5X】累加异变。
"""
import pytest
from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance


def _setup_engine(tmp_path):
    e = GameEngine(db_path=str(tmp_path / "test.db"), rng_seed=4)
    e.execute_action("setup_attributes", {
        "name": "贾希希", "blood_points": 5, "speed_points": 8, "mana_points": 12
    })
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    s = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.execute_action("choose_discovered_relic", {"relic_name": s["result"]["relic_choices"][0]})
    return e


def test_monster_cooldown_daowen_sets_cooldown_and_cannot_repeat(tmp_path):
    """正常路径：怪物发动固执后，道纹进入冷却，后续回合不再列出，禁止复读。"""
    e = _setup_engine(tmp_path)
    p = e.state.player
    while e.state.energy > 0:
        e.execute_action("pre_battle_action", {
            "sub_action": "修行", "tier": 1,
            "allocations": {"speed_points": 0, "mana_points": 1}
        })
    e.execute_action("battle_start", {"relic_choices": {}})
    
    # 构造带固执3的怪物
    m = e.state.enemies[0]
    m.dao_wen["固执"] = DaoWenInstance(
        DaoWen(name="固执", formula="", cost_type="冷却", cost_formula="X", effect_formula=""),
        x_value=3
    )

    # 第1回合（白板回合）
    e.execute_action("round_start", {"relic_choices": {}})
    e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": m.name})
    pmp1 = e.execute_action("prepare_monster_phase", {})
    actor1 = pmp1["result"]["actors"][0]
    e.execute_action("resolve_monster_phase", {
        "token": pmp1["result"]["token"],
        "choices": [{
            "actor_ref": actor1["actor_ref"],
            "daowen": None,
            "attack_actions": [{"hits": [{"target_ref": "player:0", "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}] * actor1["base_hits_per_attack"]}] * actor1["base_attack_actions"]
        }]
    })
    e.execute_action("round_end", {})

    # 第2回合：怪物可发动固执
    e.execute_action("round_start", {"relic_choices": {}})
    e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": m.name})
    pmp2 = e.execute_action("prepare_monster_phase", {})
    actor2 = pmp2["result"]["actors"][0]
    daowen_names = [opt["name"] for opt in actor2["daowen_options"]]
    assert "固执" in daowen_names

    # 提交发动固执
    e.execute_action("resolve_monster_phase", {
        "token": pmp2["result"]["token"],
        "choices": [{
            "actor_ref": actor2["actor_ref"],
            "daowen": {"name": "固执", "dodge": False, "blood_shadow": False},
            "attack_actions": [{"hits": [{"target_ref": "player:0", "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}] * actor2["base_hits_per_attack"]}] * actor2["base_attack_actions"]
        }]
    })
    e.execute_action("round_end", {})

    # 验证：固执冷却已设为3，can_use 为 False
    assert m.dao_wen["固执"].cooldown_remaining >= 3
    assert m.dao_wen["固执"].can_use() is False

    # 下一回合（第3回合）：prepare 不应再列出固执（防复读）
    e.execute_action("round_start", {"relic_choices": {}})
    e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": m.name})
    pmp3 = e.execute_action("prepare_monster_phase", {})
    actor3 = pmp3["result"]["actors"][0]
    daowen_names3 = [opt["name"] for opt in actor3["daowen_options"]]
    assert "固执" not in daowen_names3, "冷却中的道纹不得再次列为可用选项，严禁复读"


def test_monster_bleed_cost_daowen_deducts_monster_hp(tmp_path):
    """正常路径：怪物发动血债必须扣除自身生命（支付流血代价）。"""
    e = _setup_engine(tmp_path)
    while e.state.energy > 0:
        e.execute_action("pre_battle_action", {
            "sub_action": "修行", "tier": 1,
            "allocations": {"speed_points": 0, "mana_points": 1}
        })
    e.execute_action("battle_start", {"relic_choices": {}})
    m = e.state.enemies[0]
    m.dao_wen["血债"] = DaoWenInstance(
        DaoWen(name="血债", formula="", cost_type="流血", cost_formula="X", effect_formula=""),
        x_value=5
    )

    # 第1回合（白板）
    e.execute_action("round_start", {"relic_choices": {}})
    e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": m.name})
    pmp1 = e.execute_action("prepare_monster_phase", {})
    actor1 = pmp1["result"]["actors"][0]
    e.execute_action("resolve_monster_phase", {
        "token": pmp1["result"]["token"],
        "choices": [{
            "actor_ref": actor1["actor_ref"],
            "daowen": None,
            "attack_actions": [{"hits": [{"target_ref": "player:0", "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}] * actor1["base_hits_per_attack"]}] * actor1["base_attack_actions"]
        }]
    })
    e.execute_action("round_end", {})

    # 第2回合发动血债
    e.execute_action("round_start", {"relic_choices": {}})
    e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": m.name})
    hp_after_player_hit = m.current_hp

    pmp2 = e.execute_action("prepare_monster_phase", {})
    actor2 = pmp2["result"]["actors"][0]
    assert "血债" in [opt["name"] for opt in actor2["daowen_options"]]

    e.execute_action("resolve_monster_phase", {
        "token": pmp2["result"]["token"],
        "choices": [{
            "actor_ref": actor2["actor_ref"],
            "daowen": {"name": "血债", "target_ref": "player:0", "dodge": False, "blood_shadow": False},
            "attack_actions": [{"hits": [{"target_ref": "player:0", "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}] * actor2["base_hits_per_attack"]}] * actor2["base_attack_actions"]
        }]
    })

    # 验证：怪物生命真实扣除了血债5的代价
    assert m.current_hp == hp_after_player_hit - 5, f"怪物发动血债5应扣5生命，实扣{hp_after_player_hit - m.current_hp}"


def test_monster_cannot_cast_when_hp_insufficient_for_cost(tmp_path):
    """边界条件/错误输入：怪物当前生命不足以支付代价时，无法发动代价道纹。"""
    e = _setup_engine(tmp_path)
    while e.state.energy > 0:
        e.execute_action("pre_battle_action", {
            "sub_action": "修行", "tier": 1,
            "allocations": {"speed_points": 0, "mana_points": 1}
        })
    e.execute_action("battle_start", {"relic_choices": {}})
    m = e.state.enemies[0]
    m.dao_wen["血债"] = DaoWenInstance(
        DaoWen(name="血债", formula="", cost_type="流血", cost_formula="X", effect_formula=""),
        x_value=50
    )
    # 怪物只剩10血，无法支付50流血
    m.current_hp = 10

    e.execute_action("round_start", {"relic_choices": {}})
    pmp = e.execute_action("prepare_monster_phase", {})
    actor = pmp["result"]["actors"][0]
    
    # 尝试非法强制提交付不起代价的道纹，必须被拒绝或跳过
    res = e.execute_action("resolve_monster_phase", {
        "token": pmp["result"]["token"],
        "choices": [{
            "actor_ref": actor["actor_ref"],
            "daowen": {"name": "血债", "target_ref": "player:0", "dodge": False, "blood_shadow": False},
            "attack_actions": [{"hits": [{"target_ref": "player:0", "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}] * actor["base_hits_per_attack"]}] * actor["base_attack_actions"]
        }]
    })
    # 应当被拒绝或结算为跳过，怪物不得自杀
    assert res["success"] is False or any(d.get("daowen_skipped") for d in res.get("result", {}).get("details", []))
    assert m.is_alive is True
