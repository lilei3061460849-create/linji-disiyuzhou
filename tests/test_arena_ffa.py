"""养蛊场（怪物互斗训练模式）的引擎闸门与失败品判据。

覆盖三件事：
  1. 默认关闭：怪物阶段的合法目标仍然只有玩家侧，怪物之间不能互相攻击——正式玩法不受影响；
  2. 打开 `state.arena_ffa`：怪物之间可以互相攻击，并且允许逐个 actor 提交（严格交替）；
  3. 「自己行动导致自己死亡＝失败品」：训练器把它记为 self_kill 且适应度 0。
"""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sim"))

import monster_arena as arena  # noqa: E402
from engine.models import StatusEffect  # noqa: E402


def _lineup(n: int = 2) -> list[dict]:
    return [dict(d) for d in arena.monster_pool()[:n]]


def _prepare(e):
    prep = e.execute_action("prepare_monster_phase", {})
    assert prep["success"], prep.get("error")
    return prep["result"]


def test_default_battle_attack_targets_are_player_side_only():
    """默认（闸门关闭）：怪物的**攻击**合法目标只有玩家侧，且真的打不到别的怪物。

    道纹的 target_options 不在此断言：闸门关闭时该分支走的是改动前逐字节相同的旧代码，
    旧行为本来就允许怪物道纹把其它怪物列为可选目标（属既有语义，本次未动）。
    """
    e = arena.build_arena(_lineup(2))
    e.state.arena_ffa = False
    res = _prepare(e)
    assert res["actors"], "应当有可行动怪物"
    for actor in res["actors"]:
        refs = {t["ref"] for t in actor["attack_target_options"]}
        assert "player:0" in refs
        assert not any(r.startswith("enemy:") for r in refs), \
            "闸门关闭时怪物不得把其它怪物列为攻击目标"

    # 行为面复核：硬把攻击目标顶成另一只怪物，必须被拒
    actor = res["actors"][0]
    hit = {"dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}
    daowen = None
    if actor["daowen_options"] and not actor["daowen_options"][0].get("requires_target"):
        opt = actor["daowen_options"][0]
        daowen = {"name": opt["name"], "x": opt.get("x") or 1, "dodge": False, "blood_shadow": False}
    rogue = {"actor_ref": actor["actor_ref"], "daowen": daowen,
             "attack_actions": [{"hits": [dict(hit, target_ref="enemy:1")
                                          for _ in range(max(1, actor["base_hits_per_attack"]))]}
                                for _ in range(max(1, actor["base_attack_actions"]))]}
    out = e.execute_action("resolve_monster_phase", {"token": res["token"], "choices": [rogue]})
    assert not out["success"], "闸门关闭时怪物攻击另一只怪物必须被引擎拒绝"


def test_arena_ffa_lets_monsters_fight_each_other():
    """打开闸门：怪物可以把其它怪物列为目标，并且真的能打掉对方的生命。"""
    e = arena.build_arena(_lineup(2))
    assert e.state.arena_ffa is True
    res = _prepare(e)
    actor = next(a for a in res["actors"] if a["actor_ref"] == "enemy:0")
    rival_refs = {t["ref"] for t in actor["attack_target_options"]}
    assert "enemy:1" in rival_refs, "养蛊场里 0 号怪必须能把 1 号怪列为攻击目标"
    assert "player:0" not in rival_refs, "观众（玩家侧）不参战，不该出现在目标里"

    rival = e.state.enemies[1]
    hp_before = rival.current_hp
    hit = {"dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}
    choice = {"actor_ref": "enemy:0", "daowen": None,
              "attack_actions": [{"hits": [dict(hit, target_ref="enemy:1")
                                           for _ in range(max(1, actor["base_hits_per_attack"]))]}
                                 for _ in range(max(1, actor["base_attack_actions"]))]}
    if actor["daowen_options"]:      # 有合法道纹时必须提交一个（引擎契约）
        opt = actor["daowen_options"][0]
        choice["daowen"] = {"name": opt["name"], "x": opt.get("x") or 1,
                            "dodge": False, "blood_shadow": False}
        if opt.get("requires_target"):
            choice["daowen"]["target_ref"] = "enemy:1"
    out = e.execute_action("resolve_monster_phase",
                           {"token": res["token"], "choices": [choice]})
    assert out["success"], out.get("error")
    assert e.state.enemies[1].current_hp < hp_before, "养蛊场里怪物应当能打掉对手生命"


def test_arena_ffa_allows_single_actor_submission():
    """养蛊场允许逐个 actor 提交（严格交替）；闸门关闭时仍要求一次交齐全部 actor。"""
    e = arena.build_arena(_lineup(2))
    res = _prepare(e)
    hit = {"dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}
    actor = res["actors"][0]
    daowen = None
    if actor["daowen_options"]:
        opt = actor["daowen_options"][0]
        daowen = {"name": opt["name"], "x": opt.get("x") or 1,
                  "dodge": False, "blood_shadow": False}
        if opt.get("requires_target"):
            daowen["target_ref"] = actor["attack_target_options"][0]["ref"]
    one = {"actor_ref": actor["actor_ref"], "daowen": daowen,
           "attack_actions": [{"hits": [dict(hit, target_ref=actor["attack_target_options"][0]["ref"])
                                        for _ in range(max(1, actor["base_hits_per_attack"]))]}
                              for _ in range(max(1, actor["base_attack_actions"]))]}
    ok = e.execute_action("resolve_monster_phase", {"token": res["token"], "choices": [one]})
    assert ok["success"], f"养蛊场应当接受单 actor 提交: {ok.get('error')}"
    assert e.state.combat_subphase == "player_actions", "还有怪物没出手，子阶段应退回玩家行动阶段"

    # 闸门关闭时：同一种单 actor 提交必须被拒（要求一次交齐）
    e2 = arena.build_arena(_lineup(2))
    e2.state.arena_ffa = False
    res2 = _prepare(e2)
    a2 = res2["actors"][0]
    d2 = None
    if a2["daowen_options"] and not a2["daowen_options"][0].get("requires_target"):
        d2 = {"name": a2["daowen_options"][0]["name"], "x": a2["daowen_options"][0].get("x") or 1,
              "dodge": False, "blood_shadow": False}
    one2 = {"actor_ref": a2["actor_ref"], "daowen": d2,
            "attack_actions": [{"hits": [dict(hit, target_ref="player:0")
                                         for _ in range(max(1, a2["base_hits_per_attack"]))]}
                               for _ in range(max(1, a2["base_attack_actions"]))]}
    out2 = e2.execute_action("resolve_monster_phase", {"token": res2["token"], "choices": [one2]})
    assert not out2["success"], "闸门关闭时必须要求一次提交全部 actor"


def test_ffa_self_kill_is_scored_zero():
    """自己行动导致自己死亡（如【凡庸】空转自爆）＝失败品，适应度 0。"""
    res = arena.run_match(_lineup(2), dict(arena.DEFAULT_WEIGHTS), seed=0, max_rounds=7,
                          setup=lambda e: (
                              setattr(e.state.enemies[0], "mana_limit", 0),
                              setattr(e.state.enemies[0], "current_mana", 0),
                              setattr(e.state.enemies[0], "blood_limit", 5_000),
                              setattr(e.state.enemies[0], "current_hp", 5_000)))
    assert res["stats"][0]["self_kill"] is True, "连续 0 伤空转应当被【凡庸】判定为失败品"
    assert arena.fitness(res, 0, dict(arena.DEFAULT_WEIGHTS)) == 0.0


def test_self_kill_rule_has_teeth():
    """失败品判据「有牙」：同为「杀了两只后自己作死」，判 0 vs 不判 0 的差别可观测。"""
    stat = {"self_kill": True, "deaths": 1, "kills": 2, "zero_damage_rounds": 0,
            "dmg_out": 150, "dmg_taken": 40, "casts": 3, "rounds_acted": 4}
    res = {"stats": {0: dict(stat)}, "survivors": [1], "lineup": ["A", "B"], "rounds": 4}
    assert arena.fitness(res, 0, dict(arena.DEFAULT_WEIGHTS)) == 0.0
    assert arena.fitness(res, 0, dict(arena.DEFAULT_WEIGHTS), cull_self_kill=False) > 0.5


def test_ffa_kill_is_rewarded():
    """击杀对手 = 加分（适应度里含击杀项，且赢家不为 0）。"""
    res = arena.run_match(_lineup(2), dict(arena.DEFAULT_WEIGHTS), seed=0, max_rounds=8,
                          setup=lambda e: setattr(e.state.enemies[1], "current_hp", 1))
    killer = [i for i, s in res["stats"].items() if s["kills"] > 0]
    assert killer, "应当出现击杀"
    assert res["stats"][killer[0]]["self_kill"] is False
    assert arena.fitness(res, killer[0], dict(arena.DEFAULT_WEIGHTS)) > 0
