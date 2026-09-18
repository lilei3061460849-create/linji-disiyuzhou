"""怪物侧「可选目标道纹」与 prepare 语义出口（报告 P2／P5-B，2026-09-17）。

DM 裁定：怪物面板**只写道纹、不写战术**，战术由发动方按道纹语义实时推导。
要让这条裁定跑得起来，怪物侧的决策接口必须补齐两件事，本文件逐条钉住：

1. **语义出口**：`prepare_monster_phase` 的每个道纹候选带上引擎口径的 `summary`
   （含真实代价数字）。此前只给道纹名与合法目标，语义全靠读文档/背面板注释——
   本轮 35 条 summary、32 处文档一漂移，怪物战术就静默失效（骨天使/奇美拉/眼树）。
   改道纹只动 `calculate_*`，怪物侧零维护。

2. **可选目标**：【变形】【超频】的正文口径是「[目标]可选，不填则自身」，但它们的
   `calculate_*` 故意不声明 target 形参（声明了 api.py 就会强制显式目标、堵死
   "不填则自身"）。怪物侧此前因此把它们当成"无目标道纹"：prepare 不给
   target_options、resolve 直接拒绝 target_ref —— 怪物**永远只能自施**。
   对【变形】这是致命的：互换后超出[速限]的部分蒸发，"攻力>攻次"的怪自施即自残，
   而"喝汤"用法（对法力>速度的轮回者施放）在接口层根本不可达。

覆盖：正常 / 边界 / 错误输入 / 架构不变量。
"""
import os
import sys

import pytest

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.daowen import DaoWenEngine
from engine.monsters import make_monster_entity

DaoWenEngine.register_all()

TIMINGS = ("before", "after", "damage_after", "life_before")


def _engine(suffix: str) -> GameEngine:
    engine = GameEngine(db_path=f"data/test_optgt_{suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "沈昼", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    engine.execute_action("setup_choose_region", {"region": "扭曲都市"})
    return engine


def _monster(name: str, daowen: list[str], *, blood=222, mana=7, speed=3):
    """按运行时口径造怪：面板不写死 X（x_free），[法限]/[速限]与轮回者同源。"""
    return make_monster_entity({
        "name": name, "blood_limit": blood, "mana_limit": mana, "speed_limit": speed,
        "attack_count": speed, "attack_power": mana,
        "dao_wen": {d: None for d in daowen}, "region": "扭曲都市",
    })


def _enter_combat(engine: GameEngine, monsters: list) -> None:
    engine.state.enemies = monsters
    engine.state.phase = "in_combat"
    engine.state.current_round = 2
    engine.combat.reset_monster_activation()


def _attack_actions(actor: dict) -> list:
    """按 prepare 快照拼出合法的攻击提交（本文件的用例只关心道纹那一步）。"""
    if not actor["attack_target_options"]:
        return []
    option = actor["attack_target_options"][0]
    spell_choices = {
        timing: {sp["spell_name"]: {"use": False}
                 for sp in option.get("spell_options", {}).get(timing, [])}
        for timing in TIMINGS
    }
    hits = [{"target_ref": option["ref"], "dodge": False, "blood_shadow": False,
             "spell_choices": spell_choices}
            for _ in range(actor["base_hits_per_attack"])]
    return [{"hits": hits} for _ in range(actor["base_attack_actions"])]


def _cast(engine: GameEngine, monster, daowen_choice: dict | None):
    """跑一次完整怪物阶段：monster 发动 daowen_choice（None＝不发动道纹）。"""
    prepared = engine.combat.prepare_monster_phase()
    actor = next(a for a in prepared["actors"] if a["monster"] == monster.name)
    choices = []
    for a in prepared["actors"]:
        choices.append({
            "actor_ref": a["actor_ref"],
            "daowen": daowen_choice if a["actor_ref"] == actor["actor_ref"] else None,
            "attack_actions": _attack_actions(a),
        })
    return engine.combat.resolve_monster_phase(choices, prepared=prepared), prepared, actor


def _option(actor: dict, name: str) -> dict:
    return next(o for o in actor["daowen_options"] if o["name"] == name)


# ========================================================================
# 正常：prepare 给出目标候选 + 引擎口径的效果正文
# ========================================================================

def test_optional_target_daowen_exposes_target_options():
    engine = _engine("expose")
    gu = _monster("骨天使", ["变形", "全力", "飞行"])
    _enter_combat(engine, [gu])

    prepared = engine.combat.prepare_monster_phase()
    opt = _option(prepared["actors"][0], "变形")
    assert opt["target_optional"] is True, "【变形】是可选目标道纹"
    assert opt["requires_target"] is False, "可选 ≠ 必选，旧键语义不得翻转"
    assert opt["dodge_submission"] == "single_if_hostile"
    names = {t["name"] for t in opt["target_options"]}
    assert "沈昼" in names, "必须能选到轮回者——否则「喝汤」在接口层不可达"
    assert "骨天使" in names, "不填则自身，自身也必须是合法候选"


def test_every_option_carries_engine_summary():
    """语义出口：每个候选的 summary 必须逐字等于引擎当前口径（零维护不变量）。"""
    engine = _engine("summary")
    gu = _monster("骨天使", ["变形", "全力", "飞行"])
    chimera = _monster("奇美拉", ["变形", "必中"], blood=228, mana=9, speed=2)
    _enter_combat(engine, [gu, chimera])

    prepared = engine.combat.prepare_monster_phase()
    seen = 0
    for actor in prepared["actors"]:
        monster = next(m for m in engine.state.enemies if m.name == actor["monster"])
        for opt in actor["daowen_options"]:
            seen += 1
            assert opt["summary"].strip(), f"{opt['name']} 没有效果正文"
            want = DaoWenEngine.resolve(opt["resolves_as"], opt["x"],
                                        caster=monster)["summary"]
            assert opt["summary"] == want, (
                f"{opt['name']} 的 summary 不是引擎口径：{opt['summary']!r} ≠ {want!r}")
            # 代价数字必须与引擎一致：这是 P1 那 35 条漂移的正面对照
            assert f"消耗{opt['x']}法力" in opt["summary"] or "异变+" in opt["summary"]
    assert seen >= 4, f"候选过少，用例失去意义：{seen}"


def test_monster_can_soup_the_player_with_bianxing():
    """正常路径：骨天使对「法力>速度」的轮回者施放【变形】＝喝汤。"""
    engine = _engine("soup")
    player = engine.state.player
    player.current_mana, player.current_speed, player.speed_limit = 20, 3, 3
    gu = _monster("骨天使", ["变形", "全力", "飞行"])
    _enter_combat(engine, [gu])
    prepared = engine.combat.prepare_monster_phase()
    ref = next(t["ref"] for t in _option(prepared["actors"][0], "变形")["target_options"]
               if t["name"] == "沈昼")

    assert player.effective_attack_power() == 20
    results, _, _ = _cast(engine, gu, {
        "name": "变形", "x": 1, "target_ref": ref,
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    swap = [r for r in results if r.get("daowen_activated") == "变形"]
    assert swap and swap[0]["target"] == "沈昼", results
    # 速度被[速限]钳回3、法力掉到3：凭空蒸发 17 点法力＝攻击力
    assert (player.current_mana, player.current_speed) == (3, 3)
    assert player.effective_attack_power() == 3
    assert gu.current_mana == 6, "只付了 X=1 的法力代价"


def test_chaopin_can_target_another_monster():
    """可选目标名单里第二条：【超频】给谁加速度由发动方选（含队友）。"""
    engine = _engine("chaopin")
    caster = _monster("人头气球", ["超频"], blood=216, mana=11, speed=1)
    ally = _monster("爬行者", [], blood=204, mana=5, speed=4)
    _enter_combat(engine, [caster, ally])
    ally.current_speed = 2      # 已因闪避掉过速：留出[速限]内的加速余量
    prepared = engine.combat.prepare_monster_phase()
    actor = next(a for a in prepared["actors"] if a["monster"] == "人头气球")
    opt = _option(actor, "超频")
    assert opt["target_optional"] is True
    ref = next(t["ref"] for t in opt["target_options"] if t["name"] == "爬行者")

    assert ally.effective_attack_count() == 2
    _cast(engine, caster, {"name": "超频", "x": 2, "target_ref": ref,
                           "dodge": False, "blood_shadow": False,
                           "trigger_spell_choices": {}})
    assert ally.current_speed == 4, "选到谁给谁加速度"
    assert ally.effective_attack_count() == 4, "攻击次数＝当前速度，同步回升"
    assert caster.current_speed == 1, "没有落到施法者自己头上"


# ========================================================================
# 边界：不提交目标＝自身；无目标道纹仍不给候选
# ========================================================================

def test_optional_target_defaults_to_self():
    engine = _engine("self_default")
    gu = _monster("骨天使", ["变形"], mana=7, speed=3)
    gu.current_mana, gu.current_speed = 7, 3
    engine.state.player.current_mana = 20
    _enter_combat(engine, [gu])

    player_mana_before = engine.state.player.current_mana
    results, _, _ = _cast(engine, gu, {"name": "变形", "x": 1,
                                       "trigger_spell_choices": {}})
    assert [r for r in results if r.get("daowen_activated") == "变形"], results
    assert engine.state.player.current_mana == player_mana_before, "不该动到轮回者"
    # 自身 7法/3速 → 互换后速度被[速限]钳回3、法力3：自施即自残（正是旧战术失效的原因）
    assert (gu.current_mana, gu.current_speed) == (3, 3)
    assert gu.effective_attack_power() == 3


def test_none_mode_daowen_still_has_no_target_options():
    """无目标道纹（【飞行】）口径不变：不给候选，提交 target_ref 直接拒。"""
    engine = _engine("none_mode")
    gu = _monster("骨天使", ["飞行"])
    _enter_combat(engine, [gu])
    prepared = engine.combat.prepare_monster_phase()
    opt = _option(prepared["actors"][0], "飞行")
    assert opt["target_options"] == []
    assert opt["target_optional"] is False and opt["requires_target"] is False

    player_ref = "player:0"
    with pytest.raises(ValueError, match="不接受target_ref"):
        _cast(engine, gu, {"name": "飞行", "x": 1, "target_ref": player_ref,
                           "trigger_spell_choices": {}})


# ========================================================================
# 错误输入：指向敌对目标就得按判定提交闪避；非法目标要拒
# ========================================================================

def test_optional_target_on_hostile_requires_dodge_submission():
    engine = _engine("need_dodge")
    gu = _monster("骨天使", ["变形"])
    _enter_combat(engine, [gu])
    with pytest.raises(ValueError, match="dodge/blood_shadow"):
        _cast(engine, gu, {"name": "变形", "x": 1, "target_ref": "player:0",
                           "trigger_spell_choices": {}})


def test_optional_target_hostile_can_dodge():
    """对方闪避：不结算互换，只扣 1 点当前速度（与必选目标道纹同一口径）。"""
    engine = _engine("dodged")
    player = engine.state.player
    player.current_mana, player.current_speed, player.speed_limit = 20, 3, 3
    gu = _monster("骨天使", ["变形"])
    _enter_combat(engine, [gu])

    results, _, _ = _cast(engine, gu, {"name": "变形", "x": 1, "target_ref": "player:0",
                                       "dodge": True, "blood_shadow": False,
                                       "trigger_spell_choices": {}})
    dodged = [r for r in results if r.get("daowen_activated") == "变形"]
    assert dodged and dodged[0].get("dodged") is True, results
    assert player.current_mana == 20, "闪避掉了就不该被喝汤"
    assert player.current_speed == 2, "闪避付 1 点当前速度"


def test_optional_target_rejects_untargetable_target():
    """错误输入：飞行中的目标不可被选中（与必选目标道纹同一条 is_targetable 口径）。"""
    engine = _engine("flying")
    gu = _monster("骨天使", ["变形"])
    engine.state.player.is_flying = True
    _enter_combat(engine, [gu])
    prepared = engine.combat.prepare_monster_phase()
    refs = {t["name"] for t in _option(prepared["actors"][0], "变形")["target_options"]}
    assert "沈昼" not in refs, "prepare 就不该把飞行目标列为候选"
    with pytest.raises(ValueError):
        _cast(engine, gu, {"name": "变形", "x": 1, "target_ref": "player:0",
                           "dodge": False, "blood_shadow": False,
                           "trigger_spell_choices": {}})


# ========================================================================
# 架构不变量：可选目标由数据声明，且与"不声明 target 形参"的约定配套
# ========================================================================

def test_optional_target_set_is_data_driven_and_matches_signature():
    import inspect
    declared = DaoWenEngine.OPTIONAL_TARGET_DAOWEN
    assert declared == {"变形", "超频"}, (
        f"可选目标名单变化必须是有意的裁定，当前：{sorted(declared)}")
    combat = _engine("mode").combat
    for name in declared:
        params = inspect.signature(DaoWenEngine._registry[name]).parameters
        assert "target" not in params, (
            f"{name} 已声明 target 形参：它会走 required 口径，"
            f"应从 OPTIONAL_TARGET_DAOWEN 移除，否则两份口径打架")
        assert combat._daowen_target_mode(name) == "optional"
    # 反向：声明了 target 的道纹必须走 required，不得混进可选名单
    for name, fn in DaoWenEngine._registry.items():
        if "target" in inspect.signature(fn).parameters:
            assert name not in declared, f"{name} 声明了 target 却仍在可选名单里"
            assert combat._daowen_target_mode(name) == "required"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
