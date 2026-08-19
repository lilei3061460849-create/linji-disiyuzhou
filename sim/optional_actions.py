#!/usr/bin/env python3
"""可选遗物/法器策略：可以不用，但不能不让用。

此前所有模拟/手操/生产脚本把可选战始遗物（折速法印/三相残韵盘/猩红果实/
苍白之花）一律 use:False 拒绝，回始遗物（血契/余火印）也从不使用，终音法器
（黑金名片/罪业金库/教父左轮/烬翼/鲜血之翼/共心环）更是没有任何发动策略——
这些机制"存在但不可用"，玩家战力被系统性低估。

本模块给每一件可选遗物/法器实现**显式决策**（数据全部取自引擎实时状态），
供各模拟脚本、TacticalAI 与 ai_player 调用：
- battle_start_relic_choices：折速法印换法力 / 三相残韵盘 / 猩红果实 / 苍白之花
- round_start_relic_choices：回锋刀（原有）/ 血契换法力 / 余火印换法力
- 战始窗口法器：共心环（共享龙心）、黑金名片（敌方血限减半）
- 回始窗口法器：罪业金库（碎片→格挡）、烬翼（龙性→飞行）
- 战斗内法器：教父左轮（免费必中伤害）、鲜血之翼（流血→飞行）

决策原则：能用且划算才用；不划算就显式拒绝（引擎强制逐件显式提交）。
"""
from __future__ import annotations

import math
from typing import Optional

# ---------------------------------------------------------------------------
# 战始遗物
# ---------------------------------------------------------------------------

def battle_start_relic_choices(engine) -> dict:
    """战始可选遗物显式决策。

    - 折速法印：疲惫X → +6X法力。速度富余时换法力（保留至少2点速度用于闪避）。
    - 三相残韵盘：消耗一种残韵，[战终]获得另两种各1（净+1残韵）。有库存就用，
      消耗存量最多的类型。
    - 猩红果实：流血10 → [战终]血限+2（永久成长）。付得起就用。
    - 苍白之花：疲惫5 → [战终]精力+1。速度富余时用（保留至少2点）。
    """
    active = {r.name for r in engine.state.relics
              if engine.state.sealed_relics.get(r.name, 0) <= 0}
    p = engine.state.player
    out: dict = {}
    if "折速法印" in active and p is not None:
        x = min(max(0, p.current_speed - 2), 2)
        out["折速法印"] = {"use": x >= 1, "x": max(1, x)}
    if "三相残韵盘" in active:
        stock = {k: v for k, v in engine.state.resonance.items() if v >= 1}
        if stock:
            consume = max(stock, key=stock.get)
            out["三相残韵盘"] = {"use": True, "resonance_type": consume}
        else:
            out["三相残韵盘"] = {"use": False}
    if "猩红果实" in active and p is not None:
        affordable = p.current_hp >= 10 + max(10, math.ceil(p.blood_limit * 0.15))
        out["猩红果实"] = {"use": affordable}
    if "苍白之花" in active and p is not None:
        out["苍白之花"] = {"use": p.current_speed >= 7}
    using_fatigue = bool(out.get("折速法印", {}).get("use") or out.get("苍白之花", {}).get("use"))
    if using_fatigue and "回锋刀" in active:
        alive = [i for i, enemy in enumerate(engine.state.enemies) if enemy.is_alive]
        out["回锋刀"] = {"enemy_index": alive[0] if alive else 0}
    return out


# ---------------------------------------------------------------------------
# 回始遗物
# ---------------------------------------------------------------------------

def round_start_relic_choices(engine) -> dict:
    """回始遗物显式决策（回锋刀原有；血契/余火印新增主动使用）。

    - 回锋刀：[回始]对目标造成 3×(速限-当前速度) 伤害，需显式目标。
    - 血契：流血4X → +X法力。血量充足且法力有缺口时用（X≤2）。
    - 余火印：消耗龙心耐久X → +2X法力。有可用龙心且法力有缺口时用（X≤2）。
    """
    choices: dict = {}
    active = {r.name for r in engine.state.relics
              if engine.state.sealed_relics.get(r.name, 0) <= 0}
    p = engine.state.player
    if "回锋刀" in active and p is not None and p.speed_limit > p.current_speed:
        alive = [i for i, enemy in enumerate(engine.state.enemies) if enemy.is_alive]
        if alive:
            choices["回锋刀"] = {"enemy_index": alive[0]}
    if "血契" in active and p is not None:
        x = 0
        if p.current_hp >= 25 and p.current_mana <= p.mana_limit:
            x = min(2, (p.current_hp - 15) // 4)
        choices["血契"] = {"use": x >= 1, "x": max(1, x)}
    if "余火印" in active and p is not None:
        heart = next((item for item in engine.state.consumables
                      if item.kind == "dragon_heart" and item.current_uses >= 1), None)
        x = 0
        if heart is not None and p.current_mana <= p.mana_limit:
            x = min(2, heart.current_uses)
        choices["余火印"] = {"use": x >= 1, "x": max(1, x),
                             "heart_name": heart.name if heart is not None else ""}
    return choices


# ---------------------------------------------------------------------------
# 战始窗口法器（AWAIT_ROUND_START，battle_start 之后、round_start 之前）
# ---------------------------------------------------------------------------

def try_select_shared_dragon_heart(engine, commit: bool = True) -> Optional[dict]:
    """共心环：[战始]选择一枚自身拥有的【××龙心】类型，本场全员可共享抵消代价。"""
    if "共心环" not in engine.state.artifacts_owned:
        return None
    if engine.state.shared_dragon_heart_type:
        return None
    heart = next((c for c in engine.state.consumables if c.kind == "dragon_heart"), None)
    if heart is None:
        return None
    params = {"dragon_heart_type": heart.dragon_heart_type}
    if not commit:
        return {"_plan": True, "action_type": "select_shared_dragon_heart", "params": params}
    return engine.execute_action("select_shared_dragon_heart", params)


def try_use_black_card(engine, commit: bool = True) -> Optional[dict]:
    """黑金名片：[战始]所有敌方[血限]减半，付出等量碎片（负债≤50）。

    收益巨大（全体敌方血限减半），代价是碎片。负债上限50；本策略再保守留出
    20碎片缓冲（工资/后续成长也要用），超过就不发动。
    """
    if "黑金名片" not in engine.state.artifacts_owned:
        return None
    enemies = [e for e in engine.state.enemies if e.is_alive]
    if not enemies:
        return None
    total = sum(math.ceil(e.blood_limit / 2) for e in enemies)
    if total <= 0:
        return None
    if total > engine.state.shards + 20:
        return None
    if not commit:
        return {"_plan": True, "action_type": "use_black_card", "params": {}}
    return engine.execute_action("use_black_card", {})


# ---------------------------------------------------------------------------
# 回始窗口法器（AWAIT_ROUND_START，每个 round_start 之前）
# ---------------------------------------------------------------------------

def try_use_crime_vault(engine, commit: bool = True) -> Optional[dict]:
    """罪业金库：[回始]消耗X碎片（X≤2%当前碎片）获得2X格挡。

    本回合受到的威胁超过当前格挡、且碎片充足时发动，补上缺口。
    """
    if "罪业金库" not in engine.state.artifacts_owned:
        return None
    p = engine.state.player
    threat = sum(e.attack_count * e.attack_power for e in engine.state.enemies if e.is_alive)
    if p is None or threat <= p.shield:
        return None
    cap = math.floor(engine.state.shards * 0.02)
    x = min(cap, max(1, math.ceil((threat - p.shield) / 2)))
    if x < 1 or engine.state.shards < 100:
        return None
    params = {"x": x}
    if not commit:
        return {"_plan": True, "action_type": "use_crime_vault", "params": params}
    return engine.execute_action("use_crime_vault", params)


def try_use_dragon_wings(engine, commit: bool = True) -> Optional[dict]:
    """烬翼：[回始]消耗3X龙性，获得【飞行X】。威胁高且龙性够时起飞。"""
    if "烬翼" not in engine.state.dragon_traits:
        return None
    p = engine.state.player
    threat = sum(e.attack_count * e.attack_power for e in engine.state.enemies if e.is_alive)
    if p is None or engine.state.dragon_nature < 6:
        return None
    if threat < p.current_hp * 0.3:
        return None
    x = min(2, engine.state.dragon_nature // 3)
    params = {"x": max(1, x)}
    if not commit:
        return {"_plan": True, "action_type": "use_dragon_wings", "params": params}
    return engine.execute_action("use_dragon_wings", params)


# ---------------------------------------------------------------------------
# 战斗内法器（PLAYER_ACTIONS）
# ---------------------------------------------------------------------------

def try_fire_godfather_revolver(engine, commit: bool = True) -> Optional[dict]:
    """教父左轮：对[目标]打出 30%自身血限×本场使用次数 的【必中】伤害。

    耐久6/6，永不消耗（每场回满），不占用出手。能打死就打血最少，否则打血最多。
    """
    if "教父左轮" not in engine.state.artifacts_owned:
        return None
    gun = next((c for c in engine.state.consumables
                if c.name == "教父左轮" and c.kind == "artifact_weapon"), None)
    if gun is None or gun.current_uses <= 0:
        return None
    enemies = [e for e in engine.state.enemies if e.is_alive]
    if not enemies:
        return None
    uses = engine.state.godfather_revolver_uses + 1
    damage = math.ceil(engine.state.player.blood_limit * 0.3) * uses
    killable = [e for e in enemies if e.current_hp <= damage]
    target = (min(killable, key=lambda e: e.current_hp) if killable
              else max(enemies, key=lambda e: e.blood_limit))
    idx = engine.state.enemies.index(target)
    params = {"target_ref": f"enemy:{idx}"}
    if not commit:
        return {"_plan": True, "action_type": "fire_godfather_revolver", "params": params}
    return engine.execute_action("fire_godfather_revolver", params)


def try_use_blood_wings(engine, commit: bool = True) -> Optional[dict]:
    """鲜血之翼：代价流血5X，发动【飞行X】回合。血厚且受致命威胁时起飞。"""
    if "鲜血之翼" not in engine.state.first_embrace_traits:
        return None
    p = engine.state.player
    threat = sum(e.attack_count * e.attack_power for e in engine.state.enemies if e.is_alive)
    if p is None or p.has_status("飞行"):
        return None
    if p.current_hp < 25 or threat < p.current_hp * 0.4:
        return None
    x = min(2, (p.current_hp - 10) // 5)
    params = {"x": max(1, x)}
    if not commit:
        return {"_plan": True, "action_type": "use_blood_wings", "params": params}
    return engine.execute_action("use_blood_wings", params)


# ---------------------------------------------------------------------------
# 组合入口：战始 / 回始（供各脚本统一替换手写 use:False）
# ---------------------------------------------------------------------------

def start_battle(engine):
    """战始 + 战始窗口法器。返回 (battle_start结果, 法器结果列表)。"""
    logs: list[dict] = []
    bs = engine.execute_action("battle_start",
                               {"relic_choices": battle_start_relic_choices(engine)})
    if not bs.get("success"):
        return bs, logs
    for fn in (try_select_shared_dragon_heart, try_use_black_card):
        r = fn(engine)
        if r and r.get("success"):
            logs.append(r)
    return bs, logs


def start_round(engine):
    """回始窗口法器 + round_start。返回 (round_start结果, 法器结果列表)。"""
    logs: list[dict] = []
    for fn in (try_use_crime_vault, try_use_dragon_wings):
        r = fn(engine)
        if r and r.get("success"):
            logs.append(r)
    rs = engine.execute_action("round_start",
                               {"relic_choices": round_start_relic_choices(engine)})
    return rs, logs
