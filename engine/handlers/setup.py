"""
开局流程处理器（Setup Handler）
负责初始属性分配、残韵选择、副本选择与遗物发现初始化。
"""
from __future__ import annotations
from typing import Any, Dict
from ..models import Entity, DaoWen, DaoWenInstance
from ..enums import EntityType


def handle_setup_attributes(engine: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """分配初始属性点：1属性点 = 6血限 = 1速限 = 2法限"""
    if engine.state.player is not None:
        return {"success": False, "error": "初始属性已经分配，不能重复开局"}
    blood_points = params.get("blood_points", 0)
    speed_points = params.get("speed_points", 0)
    mana_points = params.get("mana_points", 0)

    total = blood_points + speed_points + mana_points

    if total != 25:
        return {
            "success": False,
            "error": f"属性点总和必须为25，当前为{total}",
            "instruction": "1属性点=6血限=1速限=2法限，请重新分配"
        }

    blood_limit = blood_points * 6
    speed_limit = speed_points
    mana_limit = mana_points * 2

    player = Entity(
        name=params.get("name", "轮回者"),
        entity_type=EntityType.REINCARNATOR.value,
        blood_limit=blood_limit,
        current_hp=blood_limit,
        mana_limit=mana_limit,
        current_mana=mana_limit,
        speed_limit=speed_limit,
        current_speed=speed_limit,
        attack_count=0,
        attack_power=0,
    )

    # 开局唯一初始道纹为【杀伐】
    player.dao_wen["杀伐"] = DaoWenInstance(dao_wen=DaoWen(
        name="杀伐", formula="杀伐X的公式", cost_type="消耗",
        cost_formula="X", effect_formula="2X伤害"))
    engine.state.player = player
    engine.state.attribute_points = 0
    engine.state.allocated_blood = blood_limit
    engine.state.shards = 20

    return {
        "success": True,
        "action": "分配属性点",
        "result": {
            "name": player.name,
            "blood_limit": blood_limit,
            "mana_limit": mana_limit,
            "speed_limit": speed_limit,
            "attack_count": player.attack_count,
            "attack_power": player.attack_power,
            "action_count": player.action_count,
            "shards": 20,
            "initial_daowen": "杀伐",
        },
        "next_actions": ["setup_choose_resonance", "setup_choose_region"],
        "note": "已自动获得初始道纹【杀伐】；接下来选择残韵与副本。遗物发现需要随机数。"
    }


def handle_setup_choose_region(engine: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """选择副本，并生成开局遗物发现"""
    if engine.state.player is None:
        return {"success": False, "error": "请先分配初始属性"}
    if "杀伐" not in engine.state.player.dao_wen or sum(engine.state.resonance.values()) != 1:
        return {"success": False, "error": "选择副本前必须先获得初始杀伐并选择1种初始残韵"}
    region = params.get("region", "")
    valid = ["罪孽都市", "扭曲都市", "龙心谷", "乱葬岗"]
    if region not in valid:
        return {"success": False, "error": f"只能从{valid}中选择"}
    engine.state.current_region = region
    engine.state.phase = "pre_battle"
    engine._init_relic_pool()
    discovery = engine._offer_relic_discovery("开局发现")
    if not discovery.get("success"):
        return discovery
    return {
        "success": True,
        "action": "选择副本",
        "result": {"region": region, "relic_choices": discovery["choices"]},
        "next_actions": ["choose_discovered_relic"],
        "note": "开局发现已随机列出3件遗物；必须显式选择1件后进入局外行动。",
    }


def handle_setup_choose_resonance(engine: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """选择初始残韵"""
    if engine.state.player is None:
        return {"success": False, "error": "请先分配初始属性"}
    if sum(engine.state.resonance.values()) > 0:
        return {"success": False, "error": "初始残韵已经选择，不能重复选择"}
    rtype = params.get("resonance_type", "")
    valid = ["转换", "反转", "曲解"]

    if rtype not in valid:
        return {"success": False, "error": f"只能从{valid}中选择"}

    engine.state.resonance[rtype] = engine.state.resonance.get(rtype, 0) + 1

    return {
        "success": True,
        "action": "选择残韵",
        "result": {"resonance_type": rtype, "count": engine.state.resonance[rtype]},
        "next_actions": ["setup_choose_region"]
    }
