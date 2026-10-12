#!/usr/bin/env python3
"""白刃战双操推演驱动器：用户操作轮回者，AI 操作敌方。

设计目的（知识库同步条目见 AI_EXPERIENCE.md「白刃战双操推演模式」）：
剧情推演时经常需要「我当角色、AI 当敌方」逐步交手。本脚本不新增任何规则，
只把仓库现行口径接成一条可复现的会话：

- 每一步都走引擎真实动作（prepare_attack/resolve_attack 两阶段、
  prepare_monster_phase/resolve_monster_phase 两阶段、rest 蓄锐），不做手算；
- 会话状态＝一份动作流水，任何时刻都能按流水重放出同一战场，跨轮次不漂移；
- 敌方的闪避/招架逐击声明由 AI 侧提交，轮回者的分刀由用户提交，
  引擎负责校验（击数、总伤上限、防御次数 ≤ 当前速度）。

用法：
  python3 sim/blade_session.py open [--hp 54 --speed 4 --mana 4]   # 开局
  python3 sim/blade_session.py act '<JSON>'                       # 轮回者行动
  python3 sim/blade_session.py enemy <闻泽闪避击数>                # 敌方阶段＋回合推进
  python3 sim/blade_session.py status                             # 查看战场
  python3 sim/blade_session.py grant-relic '<名>' '<效果文本>'      # 现场新增遗物（仅牌子）
  python3 sim/blade_session.py define-spell '<JSON>'               # 现场自创法术（真结算）

动作 JSON：
  {"type":"attack","damages":[4,4,4,4],"defense":["承受","承受","招架","承受"]}
      damages 长度必须等于当前击数；defense 逐击写 承受/闪避/招架
  {"type":"rest"}                                                  # 蓄锐
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.api import GameEngine  # noqa: E402
from engine.daowen import CostType, DaoWenEngine  # noqa: E402
from engine.models import DaoWen, DaoWenInstance, Entity, Relic  # noqa: E402

_FRESH_DAOWEN: dict[str, dict] = {}


def register_fresh_daowen(specs: list[dict]) -> None:
    """现场注册新道纹（幂等）。

    效果口径固定为「消耗X法力，对[目标]造成 mult*X 点伤害」——这是能不改引擎就
    真结算的最小可用形态。要更复杂的效果必须写正式实现，不要在这里扩。

    关键点：`_action_use_daowen` 每次都调用 `DaoWenEngine.register_all()`，而它会
    整体重建 `_registry`。只往 `_registry` 里塞名字会被冲掉（报错就是一个光秃秃的
    道纹名），因此必须把注册动作挂在 `register_all` 之后。
    """
    for spec in specs or []:
        _FRESH_DAOWEN[spec["name"]] = spec
    if not _FRESH_DAOWEN:
        return
    if getattr(DaoWenEngine.register_all, "_fresh_patched", False):
        DaoWenEngine.register_all()
        return
    original = DaoWenEngine.register_all.__func__

    def register_all_with_fresh(cls):
        original(cls)
        for name, spec in _FRESH_DAOWEN.items():
            cls._registry[name] = _make_calculator(name, spec["mult"])

    register_all_with_fresh._fresh_patched = True
    DaoWenEngine.register_all = classmethod(register_all_with_fresh)
    DaoWenEngine.register_all()


def _make_calculator(name: str, mult: int):
    def calculate(x: int, target: Entity = None) -> dict:
        target_name = target.name if target is not None else "未选定目标"
        return {"dao_wen": name, "x": x, "cost_type": CostType.MANA.value, "cost": x,
                "target_damage": mult * x, "damage_type": "普通",
                "summary": f"消耗{x}法力，对{target_name}造成{mult * x}点伤害"}
    return calculate

SESSION = os.environ.get("BLADE_SESSION", "/tmp/linji_blade/session.json")
DEFENSE_MAP = {"承受": (False, False), "闪避": (True, False), "招架": (False, True)}


def _load() -> dict:
    if not os.path.exists(SESSION):
        return {"setup": {}, "actions": []}
    with open(SESSION, encoding="utf-8") as f:
        return json.load(f)


def _save(session: dict) -> None:
    os.makedirs(os.path.dirname(SESSION), exist_ok=True)
    with open(SESSION, "w", encoding="utf-8") as f:
        json.dump(session, f, ensure_ascii=False, indent=2)


def _decline(option: dict) -> dict:
    """按接口要求显式拒绝全部反应法术（不代玩家/敌方决定是否用法术）。"""
    return {timing: {spell["spell_name"]: {"use": False}
                     for spell in option.get("spell_options", {}).get(timing, [])}
            for timing in ("before", "after")}


def new_engine(setup: dict) -> GameEngine:
    """按 1属性点=6血限、2属性点=1速限=1法限 折算面板，并清成白板战斗。"""
    hp, speed, mana = setup["hp"], setup["speed"], setup["mana"]
    assert hp % 6 == 0, "血限必须是 6 的倍数（1属性点=6血限）"
    register_fresh_daowen(setup.get("fresh_daowen") or [])
    tag = f"{hp}_{speed}_{mana}"
    os.makedirs("/tmp/linji_blade", exist_ok=True)
    engine = GameEngine(db_path=f"/tmp/linji_blade/{tag}.db", rng_seed=20261011,
                        sealed_candidate_path=f"/tmp/linji_blade/sealed_{tag}.json",
                        death_book_path="/tmp/linji_blade/death_book.md")
    result = engine.execute_action("setup_attributes", {
        "name": setup.get("name", "闻泽"),
        "blood_points": hp // 6, "speed_points": speed * 2, "mana_points": mana * 2})
    assert result.get("success"), result
    # 开局发现：显式选 1 件遗物与 1 种初始道纹（只走公开 action，不塞未发现的道纹）
    choices = list(engine.state.pending_relic_choices)
    if choices:
        engine.execute_action("choose_discovered_relic", {"relic_name": choices[0]})
    daowen = list(engine.state.pending_initial_daowen_choices)
    if daowen:
        wanted = setup.get("daowen")
        pick = wanted if wanted in daowen else daowen[0]
        engine.execute_action("setup_choose_initial_daowen", {"daowen_name": pick})
        if setup.get("only_picked"):
            for name in list(engine.state.player.dao_wen):
                if name != pick:
                    del engine.state.player.dao_wen[name]
    engine.execute_action("setup_choose_resonance", {"resonance_type": "曲解"})
    engine.execute_action("setup_choose_region", {"region": setup.get("region", "乱葬岗")})
    # 现场新道纹在开局发现之后、进入战斗之前授予角色（战前给予→重来才会保留）
    for spec in setup.get("fresh_daowen") or []:
        engine.state.player.dao_wen[spec["name"]] = DaoWenInstance(DaoWen(
            name=spec["name"], formula=f"消耗X，对[目标]造成{spec['mult']}X点伤害",
            cost_type="消耗", cost_formula="X", effect_formula=f"{spec['mult']}X伤害"),
            x_value=0)
    # 走真实 battle_start：战始快照由此建立（restart_battle 全靠它）。
    # 出怪池清空 → 快照照建但不抽随机怪，敌方由下面按指定面板注入。
    engine.state.enemies[:] = []
    engine.state.phase = "pre_battle"
    engine.monster_pool[engine.state.current_region] = []
    engine.state.energy = 0
    started = engine.execute_action("battle_start", {"relic_choices": {}})
    assert started.get("success"), started
    inject_enemy(engine, setup.get("enemy") or DEFAULT_ENEMY)
    first_round = engine.execute_action("round_start", {"relic_choices": {}})
    assert first_round.get("success"), first_round
    return engine


DEFAULT_ENEMY = {"name": "寒空壳", "hp": 96, "speed": 2, "mana": 1}


def inject_enemy(engine: GameEngine, spec: dict) -> Entity:
    """把指定面板的白板敌方塞进战场（[攻击次数]=速度、[攻击力]=法力，与面板计价同口径）。"""
    enemy = Entity(name=spec["name"], entity_type="怪物",
                   blood_limit=spec["hp"], current_hp=spec["hp"],
                   attack_count=spec["speed"], attack_power=spec["mana"],
                   speed_limit=spec["speed"], current_speed=spec["speed"],
                   mana_limit=spec["mana"], current_mana=spec["mana"])
    engine.state.enemies[:] = [enemy]
    return enemy


def _apply_attack(engine: GameEngine, act: dict) -> str:
    prepared = engine.execute_action("prepare_attack", {"actor_ref": "player:0"})
    if not prepared.get("success"):
        raise SystemExit(f"准备攻击失败：{prepared.get('error')}")
    options = prepared["result"]
    target = options["target_options"][0]
    damages = act["damages"]
    if len(damages) != options["hit_count"]:
        raise SystemExit(f"击数不符：本次{options['hit_count']}击，收到{len(damages)}个伤害值")
    defense = act.get("defense") or ["承受"] * options["hit_count"]
    hits = []
    for index in range(options["hit_count"]):
        dodge, parry = DEFENSE_MAP.get(defense[index], (False, False))
        hits.append({"target_ref": target["ref"], "damage": damages[index], "dodge": dodge,
                     "parry": parry, "blood_shadow": False, "spell_choices": _decline(target)})
    resolved = engine.execute_action("resolve_attack", {"token": options["token"], "hits": hits})
    if not resolved.get("success"):
        raise SystemExit(f"攻击被引擎拒绝：{resolved.get('error')}")
    dealt = [hit.get("damage_dealt") for hit in resolved["result"]["hits"]]
    notes = [hit["note"] for hit in resolved["result"]["hits"] if hit.get("note")]
    attacker = engine.state.player
    return (f"轮回者攻击：击数{options['hit_count']}／总伤上限{options['damage_budget']}，"
            f"分刀{damages}，敌方逐击{defense} → 落地{dealt}"
            + (f"（{'；'.join(notes)}）" if notes else "")
            + f"｜余出手{engine._action_budget_of(attacker) - attacker.actions_used_this_round}")


def _apply_enemy(engine: GameEngine, dodge_hits: int) -> str:
    prepared = engine.execute_action("prepare_monster_phase", {})
    if not prepared.get("success"):
        raise SystemExit(f"准备敌方阶段失败：{prepared.get('error')}")
    choices, detail = [], []
    for actor in prepared["result"]["actors"]:
        target = actor["attack_target_options"][0]
        enemy = engine.state.enemies[int(actor["actor_ref"].split(":")[1])]
        hits = [{"target_ref": target["ref"], "dodge": index < dodge_hits,
                 "blood_shadow": False, "spell_choices": _decline(target)}
                for index in range(actor["base_hits_per_attack"])]
        attacks = [{"hits": hits} for _ in range(actor["base_attack_actions"])]
        choices.append({"actor_ref": actor["actor_ref"],
                        "daowen": None if not actor["daowen_options"] else "__first__",
                        "attack_actions": attacks})
        detail.append(f"{actor['base_attack_actions']}次攻击×{actor['base_hits_per_attack']}击"
                      f"×攻击力{enemy.attack_power}")
    resolved = engine.execute_action("resolve_monster_phase",
                                     {"token": prepared["result"]["token"], "choices": choices})
    if not resolved.get("success"):
        raise SystemExit(f"敌方阶段被拒：{resolved.get('error')}")
    line = f"敌方攻击：{'；'.join(detail)}，轮回者闪避{dodge_hits}击"
    ended = engine.execute_action("round_end", {})
    if not ended.get("success"):
        raise SystemExit(f"round_end 失败：{ended.get('error')}")
    if engine.state.combat_subphase == "await_round_start":
        started = engine.execute_action("round_start", {"relic_choices": {}})
        if not started.get("success"):
            raise SystemExit(f"round_start 失败：{started.get('error')}")
        line += f"｜第{engine.state.current_round}回合开始"
    return line


def _apply(engine: GameEngine, act: dict) -> str:
    kind = act["type"]
    if kind == "attack":
        return _apply_attack(engine, act)
    if kind == "daowen":
        player = engine.state.player
        target = engine.state.enemies[0] if act.get("target_ref") in (None, "enemy:0") else None
        hp_before = target.current_hp if target is not None else None
        mana_before = player.current_mana
        result = engine.execute_action("use_daowen", {
            "daowen_name": act["name"], "x": act["x"],
            "target_ref": act.get("target_ref", "enemy:0")})
        if not result.get("success"):
            raise SystemExit(f"发动道纹被拒：{result.get('error')}")
        budget = engine._action_budget_of(player)
        summary = result.get("calculation", {}).get("summary", "")
        return (f"发动【{act['name']}{act['x']}】：{summary}"
                f"｜敌方生命{hp_before}→{target.current_hp}"
                f"｜法力{mana_before}→{player.current_mana}"
                f"｜余出手{budget - player.actions_used_this_round}")
    if kind == "rest":
        result = engine.execute_action("rest", {})
        if not result.get("success"):
            raise SystemExit(f"蓄锐被拒：{result.get('error')}")
        return "轮回者蓄锐：消耗1次出手，下[回始]出手+1（本回合内不加，实测）"
    if kind == "enemy":
        return _apply_enemy(engine, act["dodge"])
    if kind == "restart":
        # restart_battle 恢复的是战始快照：战前给予的新道纹/新遗物会保留，
        # 战中才加的东西会被抹掉（这是引擎语义，不是驱动器的选择）。
        engine.monster_pool[engine.state.current_region] = []
        result = engine.execute_action("restart_battle", {})
        if not result.get("success"):
            raise SystemExit(f"重来被拒：{result.get('error')}")
        inject_enemy(engine, act["enemy"])
        started = engine.execute_action("round_start", {"relic_choices": {}})
        if not started.get("success"):
            raise SystemExit(f"重开后 round_start 失败：{started.get('error')}")
        player = engine.state.player
        return (f"重来本场：敌方{engine.state.enemies[0].name}复位"
                f"{engine.state.enemies[0].current_hp}/{engine.state.enemies[0].blood_limit}，"
                f"轮回者生命{player.current_hp}、法力{player.current_mana}，"
                f"保留道纹{'、'.join(sorted(player.dao_wen)) or '无'}，"
                f"保留遗物{'、'.join(relic.name for relic in engine.state.relics) or '无'}")
    if kind == "grant_relic":
        engine.state.relics.append(Relic(name=act["name"], effect=act["effect"],
                                         tags=["现场新增·未接线"]))
        return (f"现场新增遗物【{act['name']}】：{act['effect']}"
                f"（已入遗物列表，效果未接线＝不结算，仅叙事）")
    if kind == "define_spell":
        result = engine.execute_action("define_spell", {"spell": act["spell"]})
        if not result.get("success"):
            raise SystemExit(f"自创法术被拒：{result.get('error')}")
        info = result["result"]
        return (f"现场自创法术【{info['spell']['name']}】：{info['tier']}阶，"
                f"已接线={info['wired']}，代价={info['cost']}"
                + (f"｜警告：{info['warning']}" if info.get("warning") else ""))
    raise SystemExit(f"未知动作类型：{kind}")


def build() -> tuple[GameEngine, dict, list[str]]:
    session = _load()
    engine = new_engine(session.get("setup") or {})
    lines = [_apply(engine, act) for act in session["actions"]]
    return engine, session, lines


def panel(engine: GameEngine) -> str:
    player, enemy = engine.state.player, engine.state.enemies[0]
    budget = engine._action_budget_of(player)
    return (f"{player.name} 生命{player.current_hp}/{player.blood_limit}"
            f"｜速度{player.current_speed}/{player.speed_limit}"
            f"｜法力{player.current_mana}/{player.mana_limit}"
            f"｜出手{budget - player.actions_used_this_round}/{budget}"
            f"｜击数{player.effective_attack_count()}"
            f"｜总伤上限{player.effective_attack_count() * player.effective_attack_power()}\n"
            f"{enemy.name} 生命{enemy.current_hp}/{enemy.blood_limit}"
            f"｜速度{enemy.current_speed}/{enemy.speed_limit}"
            f"｜法力{enemy.current_mana}/{enemy.mana_limit}"
            f"｜道纹{len(enemy.dao_wen)}条\n"
            f"轮回者持有道纹：{'、'.join(sorted(player.dao_wen)) or '无'}"
            f"｜遗物：{'、'.join(relic.name for relic in engine.state.relics) or '无'}"
            f"｜法术：{'、'.join(spell.name for spell in player.spells) or '无'}\n"
            f"引擎道纹注册表：{len(DaoWenEngine.list_all())}条"
            + (f"（含现场新增：{'、'.join(sorted(_FRESH_DAOWEN))}）" if _FRESH_DAOWEN else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="白刃战双操推演驱动器")
    parser.add_argument("command", choices=["open", "act", "enemy", "status", "restart",
                                            "grant-relic", "define-spell", "log"])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--hp", type=int, default=54)
    parser.add_argument("--speed", type=int, default=4)
    parser.add_argument("--mana", type=int, default=4)
    parser.add_argument("--name", default="闻泽")
    parser.add_argument("--daowen", default=None, help="开局发现候选里指定要哪一种初始道纹")
    parser.add_argument("--only-picked", action="store_true", help="只保留指定的那一种初始道纹")
    parser.add_argument("--fresh-daowen", action="append", default=[], metavar="名字:倍数",
                        help="现场新增道纹（消耗X，对目标造成 倍数×X 伤害），可重复")
    parser.add_argument("--enemy-name", default="寒空壳")
    parser.add_argument("--enemy-hp", type=int, default=96)
    parser.add_argument("--enemy-speed", type=int, default=2)
    parser.add_argument("--enemy-mana", type=int, default=1)
    parsed = parser.parse_args()

    if parsed.command == "open":
        _save({"setup": {
            "hp": parsed.hp, "speed": parsed.speed, "mana": parsed.mana, "name": parsed.name,
            "daowen": parsed.daowen, "only_picked": parsed.only_picked,
            "fresh_daowen": [{"name": item.split(":")[0], "mult": int(item.split(":")[1])}
                             for item in parsed.fresh_daowen],
            "enemy": {"name": parsed.enemy_name, "hp": parsed.enemy_hp,
                      "speed": parsed.enemy_speed, "mana": parsed.enemy_mana}},
            "actions": []})
        engine, _, _ = build()
        print(f"【白刃战开始】第{engine.state.current_round}回合｜子阶段 {engine.state.combat_subphase}")
        print(panel(engine))
        return

    session = _load()
    new_action = None
    if parsed.command == "act":
        new_action = json.loads(parsed.args[0])
    elif parsed.command == "enemy":
        new_action = {"type": "enemy", "dodge": int(parsed.args[0]) if parsed.args else 0}
    elif parsed.command == "restart":
        new_action = {"type": "restart",
                      "enemy": (session.get("setup") or {}).get("enemy") or DEFAULT_ENEMY}
    elif parsed.command == "grant-relic":
        new_action = {"type": "grant_relic", "name": parsed.args[0], "effect": parsed.args[1]}
    elif parsed.command == "define-spell":
        new_action = {"type": "define_spell", "spell": json.loads(parsed.args[0])}

    if new_action is not None:
        # 先试算：被引擎驳回的动作不落盘，否则会把坏动作永久写进重放流水
        trial = {"setup": session.get("setup") or {},
                 "actions": session["actions"] + [new_action]}
        engine = new_engine(trial["setup"])
        lines = [_apply(engine, act) for act in trial["actions"]]
        session = trial
        _save(session)
    else:
        engine, _, lines = build()
    if parsed.command == "log":
        for index, line in enumerate(lines, 1):
            print(f"{index}. {line}")
        return
    if lines:
        print(lines[-1])
    print(panel(engine))
    if not engine.state.enemies[0].is_alive:
        print(f"→ {engine.state.enemies[0].name} [命零]")
    if not engine.state.player.is_alive:
        print(f"→ {engine.state.player.name} [命零]")


if __name__ == "__main__":
    main()
