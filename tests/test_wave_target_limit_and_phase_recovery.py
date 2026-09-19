"""BUG-01/BUG-02 回归（2026-08-22）：怪物【波及】目标数限制 + 怪物阶段失败恢复。

波及X（用户裁定 2026-09-19）：X 上限＝**场上当前角色总数**（含发动者自己），
        引擎不再替发动方"目标不足就降X结算"，也不再因目标不足把指令判成不可发动。
        波及不能选自己（`api.py::_resolve_daowen_dodge`），所以真正可标记的只有
        prepare 枚举的 dodge_target_options；把 X 收到可标记目标数以内是**发动方**
        （AI/操作者）自己的事——AI 侧统一走 `sim/monster_targets.apply_wave_submission`。
        仅"可标记目标数为0"时怪物 prepare 才不给出该道纹（一个都标记不了）。
BUG-02：resolve_monster_phase 提交失败后战斗不得卡在怪物阶段——
        失败时状态整体回滚（零副作用），pending保持有效且同token可修正重交；
        所有失败/拦截路径都返回 recoverable/token/instruction，恢复路径明确可执行。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity
from sim.monster_targets import apply_wave_submission, clamp_wave_x, pick_wave_dodge_targets
from tests.setup_support import finish_initial_daowen


def _engine(tmp_path, seed: int = 20260822) -> GameEngine:
    return GameEngine(
        db_path=str(tmp_path / "w.db"),
        save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        death_book_path=str(tmp_path / "death.md"),
        rng_seed=seed,
    )


def _full_setup(engine: GameEngine, region: str = "龙心谷") -> None:
    assert engine.execute_action("setup_attributes", {
        "name": "测试", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })["success"]
    assert finish_initial_daowen(engine)["success"]
    assert engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})["success"]
    assert engine.execute_action("setup_choose_region", {"region": region})["success"]


def _dw(entity: Entity, name: str, x: int = 1, x_free: bool = False) -> None:
    """挂一条道纹。x_free=True＝面板不写 X（2026-09-16 用户令后现行怪物面板的常态）。"""
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
        x_value=(0 if x_free else x), x_free=x_free)


def _controlled_combat(engine: GameEngine, monsters: list[Entity]) -> Entity:
    player = Entity("轮回者", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=100, current_mana=100, speed_limit=9, current_speed=9,
                    attack_count=1, attack_power=2)
    engine.state.player = player
    engine.state.friends = []
    engine.state.employees = []
    engine.state.enemies = monsters
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.pending_monster_phase = {}
    engine.state.current_round = 2  # 跳过白板回合，怪物可发动道纹
    return player


def _magma_lizard() -> Entity:
    """龙心谷怪物池：熔岩蜥（血限234/法限14/速限3，加害，狂暴，波及）。

    现行面板（副本/龙心谷.md:67）三条道纹都不写 X → x_free，X 由发动方在
    [1, min(代价上限, 场上角色总数)] 内自选。法限 14 够付到波及 X=7（代价 2X）。
    本文件只断言波及的目标数与阶段恢复，不断言怪物伤害。
    """
    m = Entity("熔岩蜥", "怪物", blood_limit=234, current_hp=234,
               attack_count=3, attack_power=14)
    _dw(m, "加害", x_free=True)
    _dw(m, "狂暴", x_free=True)
    _dw(m, "波及", x_free=True)
    return m


def _friend(name: str = "友军") -> Entity:
    return Entity(name, "朋友", blood_limit=40, current_hp=40,
                  speed_limit=2, current_speed=2, attack_count=1, attack_power=1)


def _prepared_monster_option(engine: GameEngine, actor_ref: str = "enemy:0") -> tuple:
    prepared = engine.execute_action("prepare_monster_phase", {})
    assert prepared["success"], prepared
    actor = next(a for a in prepared["result"]["actors"] if a["actor_ref"] == actor_ref)
    return prepared, actor


def _combat_prepared_actor(engine: GameEngine, actor_ref: str = "enemy:0") -> dict:
    """纯枚举（CombatEngine层，无pending副作用）——只用于检查prepare给出的选项。"""
    prepared = engine.combat.prepare_monster_phase()
    return next(a for a in prepared["actors"] if a["actor_ref"] == actor_ref)


def _decline(option) -> dict:
    return {timing: {sp["spell_name"]: {"use": False} for sp in option.get("spell_options", {}).get(timing, [])}
            for timing in ("before", "after")}


def _attack_block(actor: dict) -> list[dict]:
    tgt = actor["attack_target_options"][0]["ref"]
    to = next(t for t in actor["attack_target_options"] if t["ref"] == tgt)
    hits = [{"target_ref": tgt, "dodge": False, "blood_shadow": False,
             "spell_choices": _decline(to)} for _ in range(actor["base_hits_per_attack"])]
    return [{"hits": hits} for _ in range(actor["base_attack_actions"])]


def _legal_daowen(actor: dict) -> dict | None:
    if not actor["daowen_options"]:
        return None
    chosen = actor["daowen_options"][0]
    dao = {"name": chosen["name"], "dodge": False, "blood_shadow": False,
           "trigger_spell_choices": {h: {sp["spell_name"]: {"use": False} for sp in ss}
                                     for h, ss in chosen.get("trigger_spell_options", {}).items()}}
    if chosen["requires_target"]:
        dao["target_ref"] = chosen["target_options"][0]["ref"]
    if chosen["dodge_submission"] == "per_target":
        apply_wave_submission(dao, chosen)      # X 与目标提交数成对
    return dao


def _snapshot(e: GameEngine) -> dict:
    """怪物阶段副作用快照：HP/速度/状态/碎片/round_used/事件流。"""
    m = e.state.enemies[0]
    return {
        "player": (e.state.player.current_hp, e.state.player.current_speed,
                   sorted(s.name for s in e.state.player.status_effects)),
        "friends": [(f.current_hp, sorted(s.name for s in f.status_effects))
                    for f in e.state.friends],
        "monster_hp": m.current_hp,
        "round_used": sorted(e.combat._monster_round_used(m)),
        "events": len(e.state.combat_events),
    }


# ============ 波及：X 上限＝场上当前角色总数（发动方自己收 X） ============

def test_wave_x_cap_is_field_character_count_solo(tmp_path):
    """solo 场（轮回者＋熔岩蜥＝2 个角色）：X 上限＝场上当前角色总数＝2，
    而不是"可标记目标数"1；prepare 也不再输出降X快照。发动方自己把 X 收到 1 后可结算。"""
    e = _engine(tmp_path)
    _full_setup(e)
    _controlled_combat(e, [_magma_lizard()])
    prepared, actor = _prepared_monster_option(e)
    option = next((o for o in actor["daowen_options"] if o["name"] == "波及"), None)
    assert option is not None, f"solo场上波及必须给出: {[o['name'] for o in actor['daowen_options']]}"
    assert option["x_free"] is True
    assert option["max_x"] == 2, option          # 上限＝场上角色总数（法限够付到7）
    assert "wave_effective_x" not in option, "降X快照已删除，不得再出现在 prepare 输出里"
    assert len(option["dodge_target_options"]) == 1   # 波及不能选自己
    # 发动方自己收 X：clamp 到可标记目标数，提交数与 X 成对
    assert clamp_wave_x(option, option["max_x"]) == 1
    dao = {"name": "波及", "dodge": False, "blood_shadow": False,
           "trigger_spell_choices": {h: {sp["spell_name"]: {"use": False} for sp in ss}
                                     for h, ss in option.get("trigger_spell_options", {}).items()}}
    assert apply_wave_submission(dao, option) == 1
    assert dao["x"] == 1 and len(dao["dodge_targets"]) == 1
    ok = e.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"],
        "choices": [{"actor_ref": actor["actor_ref"], "daowen": dao,
                     "attack_actions": _attack_block(actor)}]})
    assert ok["success"], ok
    assert e.state.player.has_status("波及")


def test_wave_x_cap_scales_with_field_size(tmp_path):
    """X 上限随**场上角色总数**走（含发动者自己），可标记目标数＝总数-1。"""
    e = _engine(tmp_path)
    _full_setup(e)
    _controlled_combat(e, [_magma_lizard()])
    e.state.friends.append(_friend("友军A"))
    actor = _combat_prepared_actor(e)
    option = next(o for o in actor["daowen_options"] if o["name"] == "波及")
    assert option["max_x"] == 3 and len(option["dodge_target_options"]) == 2

    e.state.friends.append(_friend("友军B"))
    actor = _combat_prepared_actor(e)
    option = next(o for o in actor["daowen_options"] if o["name"] == "波及")
    assert option["max_x"] == 4 and len(option["dodge_target_options"]) == 3
    assert "wave_effective_x" not in option
    refs = {t["ref"] for t in option["dodge_target_options"]}
    assert refs == {"player:0", "friend:0", "friend:1"}
    assert option["dodge_submission"] == "per_target"
    # 发动方按可标记目标数收 X：3；照上限 4 提交则凑不出 4 个合法目标
    assert clamp_wave_x(option, option["max_x"]) == 3
    assert len(pick_wave_dodge_targets(option)) == 3


def test_wave_marks_exactly_x_targets(tmp_path):
    """候选数>X（双怪+玩家+2友军，波及2）：恰好提交2个，仅被提交者被标记。"""
    e = _engine(tmp_path)
    _full_setup(e)
    other = Entity("石背熊", "怪物", blood_limit=100, current_hp=100,
                   attack_count=1, attack_power=3)
    lizard = _magma_lizard()
    _dw(lizard, "波及", 2)  # 构造 候选4 > X=2
    _controlled_combat(e, [lizard, other])
    e.state.friends.append(_friend("友军A"))
    e.state.friends.append(_friend("友军B"))
    prepared, actor = _prepared_monster_option(e)
    option = next(o for o in actor["daowen_options"] if o["name"] == "波及")
    assert option["x"] == 2
    assert len(option["dodge_target_options"]) == 4  # 玩家+2友军+另一只怪

    dao = {"name": "波及", "dodge": False, "blood_shadow": False,
           "trigger_spell_choices": {h: {sp["spell_name"]: {"use": False} for sp in ss}
                                     for h, ss in option.get("trigger_spell_options", {}).items()},
           "dodge_targets": [
               {"target_ref": "player:0", "dodge": False, "blood_shadow": False},
               {"target_ref": "friend:0", "dodge": False, "blood_shadow": False}]}
    other_actor = next(a for a in prepared["result"]["actors"] if a["actor_ref"] == "enemy:1")
    ok = e.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"],
        "choices": [
            {"actor_ref": "enemy:0", "daowen": dao, "attack_actions": _attack_block(actor)},
            {"actor_ref": "enemy:1", "daowen": None, "attack_actions": _attack_block(other_actor)},
        ]})
    assert ok["success"], ok
    assert e.state.player.has_status("波及")
    assert e.state.friends[0].has_status("波及")
    assert not e.state.friends[1].has_status("波及")
    assert not other.has_status("波及")


def test_wave_over_submit_rejected_and_rolled_back(tmp_path):
    """提交多于X个目标（候选全量提交）必须被拒且零副作用。"""
    e = _engine(tmp_path)
    _full_setup(e)
    other = Entity("石背熊", "怪物", blood_limit=100, current_hp=100,
                   attack_count=1, attack_power=3)
    lizard = _magma_lizard()
    _dw(lizard, "波及", 2)  # 候选4 > X=2，全量提交必然超X
    _controlled_combat(e, [lizard, other])
    e.state.friends.append(_friend("友军A"))
    e.state.friends.append(_friend("友军B"))
    prepared, actor = _prepared_monster_option(e)
    option = next(o for o in actor["daowen_options"] if o["name"] == "波及")
    other_actor = next(a for a in prepared["result"]["actors"] if a["actor_ref"] == "enemy:1")
    before = _snapshot(e)

    # 全量提交4个候选（>X=2）→ 必须拒绝
    dao = {"name": "波及", "dodge": False, "blood_shadow": False,
           "trigger_spell_choices": {},
           "dodge_targets": [{"target_ref": t["ref"], "dodge": False, "blood_shadow": False}
                             for t in option["dodge_target_options"]]}
    bad = e.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"],
        "choices": [
            {"actor_ref": "enemy:0", "daowen": dao, "attack_actions": _attack_block(actor)},
            {"actor_ref": "enemy:1", "daowen": None, "attack_actions": _attack_block(other_actor)},
        ]})
    assert not bad["success"], bad
    assert "2个目标" in bad["error"], bad
    assert _snapshot(e) == before, "超提交被拒后必须零副作用"


# ============ BUG-02：失败提交后战斗不得卡死 ============

def test_failed_resolve_stays_resubmittable_with_same_token(tmp_path):
    """非法提交（少交命中）→ 失败带token/instruction；同token修正重交成功，回终畅通。"""
    e = _engine(tmp_path)
    _full_setup(e)
    _controlled_combat(e, [_magma_lizard()])
    prepared, actor = _prepared_monster_option(e)
    token = prepared["result"]["token"]
    before = _snapshot(e)

    bad = _legal_daowen(actor)
    bad_choice = {"actor_ref": actor["actor_ref"], "daowen": bad,
                  "attack_actions": _attack_block(actor)}
    bad_choice["attack_actions"][0]["hits"] = bad_choice["attack_actions"][0]["hits"][:-1]
    failed = e.execute_action("resolve_monster_phase",
                              {"token": token, "choices": [bad_choice]})
    assert failed["success"] is False
    assert failed.get("recoverable") is True
    assert failed.get("token") == token
    assert failed.get("instruction"), failed
    assert _snapshot(e) == before, "失败提交必须零副作用"

    # 期间其它行动被拦，但必须给出可执行的恢复指引
    locked = e.execute_action("round_end", {})
    assert locked["success"] is False
    assert locked.get("recoverable") is True
    assert locked.get("token") == token
    assert "resolve_monster_phase" in locked.get("instruction", "")

    # 关键：同一token修正后重交成功，回合推进
    good = {"actor_ref": actor["actor_ref"], "daowen": _legal_daowen(actor),
            "attack_actions": _attack_block(actor)}
    ok = e.execute_action("resolve_monster_phase",
                          {"token": token, "choices": [good]})
    assert ok["success"], ok
    assert e.execute_action("round_end", {})["success"]


def test_stale_token_replay_returns_valid_token(tmp_path):
    """旧token重放：失败且带回当前有效token；按该token重交成功。"""
    e = _engine(tmp_path)
    _full_setup(e)
    _controlled_combat(e, [_magma_lizard()])
    prepared, actor = _prepared_monster_option(e)
    token = prepared["result"]["token"]

    stale = e.execute_action("resolve_monster_phase",
                             {"token": "stale-token", "choices": []})
    assert stale["success"] is False
    assert stale.get("recoverable") is True
    assert stale.get("token") == token

    ok = e.execute_action("resolve_monster_phase", {
        "token": stale["token"],
        "choices": [{"actor_ref": actor["actor_ref"], "daowen": _legal_daowen(actor),
                     "attack_actions": _attack_block(actor)}]})
    assert ok["success"], ok


def test_user_original_playthrough_no_longer_stucks(tmp_path):
    """用户原流程回归：龙心谷 seed=20260822 熔岩蜥 solo，连续3回合不卡死。
    现行口径下波及每回合都仍被 prepare 给出（可标记目标数＝1≠0），
    发动方把 X 收到 1 后即可结算——不需要引擎代为降X。"""
    e = _engine(tmp_path, seed=20260822)
    _full_setup(e)
    _controlled_combat(e, [_magma_lizard()])
    e.state.combat_subphase = "await_round_start"  # 从完整回合循环开始
    for _ in range(3):
        e.state.player.current_hp = e.state.player.blood_limit  # 夹具：避免死之传承中断干扰
        assert e.execute_action("round_start", {"relic_choices": {}})["success"]
        prepared, actor = _prepared_monster_option(e)
        option = next((o for o in actor["daowen_options"] if o["name"] == "波及"), None)
        assert option is not None and option["max_x"] == 2   # 场上2个角色
        dao = {"name": "波及", "dodge": False, "blood_shadow": False,
               "trigger_spell_choices": {h: {sp["spell_name"]: {"use": False} for sp in ss}
                                         for h, ss in option.get("trigger_spell_options", {}).items()}}
        assert apply_wave_submission(dao, option) == 1       # 发动方自己收到可标记目标数
        ok = e.execute_action("resolve_monster_phase", {
            "token": prepared["result"]["token"],
            "choices": [{"actor_ref": actor["actor_ref"], "daowen": dao,
                         "attack_actions": _attack_block(actor)}]})
        assert ok["success"], ok
        assert e.execute_action("round_end", {})["success"]


# ============ 玩家/指令侧：use_daowen 的波及X上限＝场上角色总数 ============

def _wave_use_daowen_schema(engine: GameEngine, actor_ref: str = "") -> dict:
    """从可用行动中取 波及 的 use_daowen schema。"""
    actions = engine.get_available_actions().get("actions", [])
    for action in actions:
        schema = action.get("params_schema", {})
        if action.get("action_type") == "use_daowen" \
                and schema.get("daowen_name") == "波及" \
                and schema.get("actor_ref", "") == actor_ref:
            return action
    return {}


def test_use_daowen_schema_caps_wave_x_by_field_character_count(tmp_path):
    """玩家侧：schema 的X上限＝场上当前角色总数（含自己），不再按可标记目标数封顶。"""
    e = _engine(tmp_path)
    _full_setup(e)
    player = _controlled_combat(e, [_magma_lizard()])
    _dw(player, "波及", 0)
    player.current_mana = 100  # 法力充足：X上限只能被目标数压住

    # solo：场上2个角色（轮回者＋怪物）→ X上限=2，而不是法力允许的大X
    action = _wave_use_daowen_schema(e)
    assert action, "use_daowen schema 缺失"
    assert action["available"] is True
    assert action["params_schema"]["x"]["maximum"] == 2, action["params_schema"]

    # 2怪+1友军：场上4个角色 → X上限=4（其中可标记的只有3个：波及不能选自己）
    e.state.enemies.append(Entity("石背熊", "怪物", blood_limit=100, current_hp=100,
                                  attack_count=1, attack_power=3))
    e.state.friends.append(_friend("友军A"))
    action = _wave_use_daowen_schema(e)
    assert action["params_schema"]["x"]["maximum"] == 4, action["params_schema"]


def test_player_wave_cast_at_markable_count_succeeds(tmp_path):
    """按**可标记目标数**发动 波及（恰好提交X个目标）：成功且标记正确。
    照 schema 的字面上限（场上角色总数，含自己）发动则凑不出那么多合法目标 → 被拒。"""
    e = _engine(tmp_path)
    _full_setup(e)
    player = _controlled_combat(e, [_magma_lizard()])
    _dw(player, "波及", 0)
    e.state.enemies.append(Entity("石背熊", "怪物", blood_limit=100, current_hp=100,
                                  attack_count=1, attack_power=3))
    player.current_mana = 100

    action = _wave_use_daowen_schema(e)
    assert action["params_schema"]["x"]["maximum"] == 3   # 场上3个角色（含自己）
    x = 2                                                 # 可标记的只有两只怪物
    # 先验：照字面上限 X=3 发动必被拒（第三个目标只能是自己，而波及不能选自己）
    bad = e.execute_action("use_daowen", {"daowen_name": "波及", "x": 3,
                                          "dodge": False, "blood_shadow": False,
                                          "trigger_spell_choices": {},
                                          "dodge_targets": [
                                              {"target_ref": "enemy:0", "dodge": False,
                                               "blood_shadow": False},
                                              {"target_ref": "enemy:1", "dodge": False,
                                               "blood_shadow": False}]})
    assert not bad["success"] and "必须为3个目标" in str(bad.get("error", "")), bad
    ok = e.execute_action("use_daowen", {"daowen_name": "波及", "x": x,
                                         "dodge": False, "blood_shadow": False,
                                         "trigger_spell_choices": {},
                                         "dodge_targets": [
                                             {"target_ref": "enemy:0", "dodge": False,
                                              "blood_shadow": False},
                                             {"target_ref": "enemy:1", "dodge": False,
                                              "blood_shadow": False}]})
    assert ok["success"], ok
    assert e.state.enemies[0].has_status("波及")
    assert e.state.enemies[1].has_status("波及")
    assert not player.has_status("波及")


def test_commanded_wave_not_gated_by_target_count(tmp_path):
    """指令侧：目标不足也**不再**把【波及】判成 available=False（用户裁定 2026-09-19 删除门禁）。

    固定X面板（这里 X=3）是历史形态：现行副本面板的波及一律不写 X。目标真不够时
    由结算阶段照实报错，不在指令列表里预先判死。
    """
    e = _engine(tmp_path)
    _full_setup(e)
    _controlled_combat(e, [_magma_lizard()])
    friend = _friend("友军A")
    _dw(friend, "波及", 3)
    e.state.friends.append(friend)

    action = _wave_use_daowen_schema(e, actor_ref="friend:0")
    assert action, "指令 use_daowen schema 缺失"
    assert action["available"] is True, action      # 门禁已删：不再预判不可发动
    assert "reason" not in action, action

    # 补足目标后同样是 available=True（口径不再随目标数翻转）
    e.state.enemies.append(Entity("石背熊", "怪物", blood_limit=100, current_hp=100,
                                  attack_count=1, attack_power=3))
    action = _wave_use_daowen_schema(e, actor_ref="friend:0")
    assert action["available"] is True, action


def test_placeholder_ai_casts_wave_without_rejection(tmp_path):
    """AI对战路径（PlaceholderBackend）：玩家持【波及】时占位AI能合法发动（恰好X个目标）。"""
    from engine.ai_player import AIPlayer, PlaceholderBackend
    e = _engine(tmp_path)
    _full_setup(e)
    player = _controlled_combat(e, [_magma_lizard(),
                                    Entity("石背熊", "怪物", blood_limit=100, current_hp=100,
                                           attack_count=1, attack_power=3)])
    _dw(player, "波及", 0)  # 玩家唯一道纹：占位AI必然轮到它
    player.current_mana = 100
    ai = AIPlayer(e, backend=PlaceholderBackend(), auto_validate=False,
                  tactical_combat=False)

    r = ai.play_turn()
    assert r["action"] == "use_daowen" and r["params"].get("daowen_name") == "波及", r["action"]
    assert r["result"]["success"], r["result"]
    marked = [m.name for m in e.state.enemies if m.has_status("波及")]
    assert len(marked) == 2, f"波及必须标记恰好X=2个目标: {marked}"


# ============ 怪物阶段义务可满足性：无攻击目标不卡死 ============

def test_monster_without_attack_targets_phase_still_settles(tmp_path):
    """solo对手飞行而怪物不飞：怪物无任何合法攻击目标。

    prepare必须把base_attack_actions置0（否则每击都需引用合法目标，
    提交永远失败——与【波及】目标数限制同族的"无法满足的义务"）。
    """
    e = _engine(tmp_path)
    _full_setup(e)
    player = _controlled_combat(e, [_magma_lizard()])
    player.is_flying = True  # 怪物不飞行 → 选不中玩家 → 无合法攻击目标

    prep = e.execute_action("prepare_monster_phase", {})
    assert prep["success"], prep
    actor = next(a for a in prep["result"]["actors"] if a["actor_ref"] == "enemy:0")
    assert actor["attack_target_options"] == [], actor["attack_target_options"]
    assert actor["base_attack_actions"] == 0, actor["base_attack_actions"]
    # 不变量：给出的每个需[目标]道纹都必须有合法目标（飞行solo时只剩自身，
    # 自指向合法——与强化/自愈等自用道纹同一口径，只是无战术意义）
    for o in actor["daowen_options"]:
        if o["requires_target"]:
            assert o["target_options"], f"{o['name']} 无合法目标却仍被给出: {o}"

    choice = {"actor_ref": "enemy:0", "daowen": _legal_daowen(actor),
              "attack_actions": []}
    r = e.execute_action("resolve_monster_phase",
                         {"token": prep["result"]["token"], "choices": [choice]})
    assert r["success"], r
    assert e.execute_action("round_end", {})["success"]


# ============ pick_wave_dodge_targets：恰好X个、对侧优先 ============

def test_pick_wave_dodge_targets_exactly_x_hostiles_first():
    option = {
        "x": 2,
        "dodge_target_options": [
            {"ref": "player:0", "name": "轮回者"},
            {"ref": "enemy:1", "name": "另一只怪"},
            {"ref": "friend:0", "name": "友军"},
            {"ref": "enemy:2", "name": "第三只怪"},
        ],
    }
    picked = pick_wave_dodge_targets(option)
    assert [p["target_ref"] for p in picked] == ["player:0", "friend:0"]
    assert all(p["dodge"] is False and p["blood_shadow"] is False for p in picked)

    # 对侧不足X时以其余合法目标补齐
    option["x"] = 3
    picked = pick_wave_dodge_targets(option)
    assert [p["target_ref"] for p in picked] == ["player:0", "friend:0", "enemy:1"]
