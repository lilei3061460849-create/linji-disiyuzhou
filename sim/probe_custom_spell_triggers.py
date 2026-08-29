"""真实引擎冒烟：逐一实测11种触发时机能否在自创法术上真正触发。

不是纸面描述——每个用例都真正调用 GameEngine.execute_action 的公开
两阶段接口（prepare_monster_phase / resolve_monster_phase）跑一次完整
流程，记录：
1. 【学习】阶段能否通过句式校验并学会（wired 字段是否如实标注）；
2. 真实进入战斗后，该法术是否会被列入可发动候选（prepare阶段）；
3. 若可发动，实际提交后资源变化是否符合预期（法力/生命/伤害）。

2026-08-29 修订：初版探针误用 combat.resolve_monster_phase 直接驱动
（绕过了 combat_subphase 状态机）、且在怪物"白板回合"（第1回合怪物
只普攻不出道纹）测试"目标发动道纹前"——这两个都是探针脚本自身的用法
错误，不是引擎的问题。本版改用 execute_action 公开两阶段接口，并把
"目标发动道纹前"的测试放到第2回合（怪物真正发动道纹时）。

运行：python3 sim/probe_custom_spell_triggers.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance
from tests.setup_support import finish_initial_daowen


def _fresh_engine(tag: str, region: str = "罪孽都市") -> GameEngine:
    d = tempfile.mkdtemp(prefix=f"probe_{tag}_")
    e = GameEngine(db_path=os.path.join(d, "g.db"), rng_seed=7,
                   sealed_candidate_path=os.path.join(d, "s.json"))
    e.execute_action("setup_attributes", {
        "name": "测试者", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": region})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    return e


def _give_daowen(entity, name, x=0):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
        x_value=x)


def _learn(e, definition):
    r1 = e.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "custom_spell", "spell": definition})
    if not r1["success"]:
        return r1
    return e.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "custom_spell", "spell": definition, "dm_approved": True})


def _decline_all_spell_choices(target_option):
    return {k: {sp["spell_name"]: {"use": False} for sp in target_option.get("spell_options", {}).get(k, [])}
            for k in ("before", "after")}


def _pure_attack_round(e):
    """驱动一整回合怪物阶段：全体谢绝反应法术，纯普攻。用于凑出第2回合。"""
    res = e.execute_action("prepare_monster_phase", {})
    choices = []
    for a in res["result"]["actors"]:
        hits = []
        for _ in range(a["base_hits_per_attack"]):
            target_opt = a["attack_target_options"][0]
            hits.append({"target_ref": target_opt["ref"], "dodge": False, "blood_shadow": False,
                         "spell_choices": _decline_all_spell_choices(target_opt)})
        choices.append({"actor_ref": a["actor_ref"], "daowen": None,
                        "attack_actions": [{"hits": hits} for _ in range(a["base_attack_actions"])]})
    r = e.execute_action("resolve_monster_phase", {"token": res["result"]["token"], "choices": choices})
    assert r["success"], r
    e.execute_action("round_end", {})
    e.execute_action("round_start", {})


REPORT = []


def record(trigger_label, wired_flag, fired, detail):
    REPORT.append({"trigger": trigger_label, "learn_wired": wired_flag,
                   "actually_fired": fired, "detail": detail})


def probe_before_damage():
    """受到伤害前：怪物普攻玩家，玩家用自创法术反打怪物一下。"""
    e = _fresh_engine("before_damage")
    _give_daowen(e.state.player, "杀伐")
    definition = {"name": "临危反杀", "required_daowen": ["杀伐"],
                  "trigger_condition": "受到伤害前",
                  "effect_flow": "发动杀伐X于攻击者"}
    learned = _learn(e, definition)
    wired = learned["result"].get("wired")
    e.state.energy = 0
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {})
    e.state.player.current_mana = 20
    enemy = e.state.enemies[0]
    enemy.attack_power = 5
    enemy.attack_count = 1
    hp_before = enemy.current_hp

    res = e.execute_action("prepare_monster_phase", {})
    a = res["result"]["actors"][0]
    target_opt = a["attack_target_options"][0]
    spell_choices = _decline_all_spell_choices(target_opt)
    spell_choices["before"]["临危反杀"] = {"use": True, "cycles": [[
        {"x": 6, "target_ref": "enemy:0", "dodge": False}]]}
    choices = [{"actor_ref": a["actor_ref"], "daowen": None,
                "attack_actions": [{"hits": [{"target_ref": target_opt["ref"], "dodge": False,
                                              "blood_shadow": False, "spell_choices": spell_choices}]}]}]
    r = e.execute_action("resolve_monster_phase", {"token": res["result"]["token"], "choices": choices})
    hp_after = e.state.enemies[0].current_hp
    fired = r["success"] and hp_after < hp_before
    record("受到伤害前", wired, fired,
           f"resolve success={r['success']}；敌方hp {hp_before}→{hp_after}（应因【杀伐6】掉12点）")


def probe_after_life_lost():
    """失去生命后：玩家挨打掉血后，自创法术自动回血。"""
    e = _fresh_engine("after_life_lost")
    _give_daowen(e.state.player, "再生")
    definition = {"name": "痛定回春", "required_daowen": ["再生"],
                  "trigger_condition": "失去生命后",
                  "effect_flow": "发动再生X于自身"}
    learned = _learn(e, definition)
    wired = learned["result"].get("wired")
    e.state.energy = 0
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {})
    e.state.player.current_mana = 20
    e.state.player.current_hp = 50
    enemy = e.state.enemies[0]
    enemy.attack_power = 5
    enemy.attack_count = 1
    hp_before = e.state.player.current_hp

    res = e.execute_action("prepare_monster_phase", {})
    a = res["result"]["actors"][0]
    target_opt = a["attack_target_options"][0]
    spell_choices = _decline_all_spell_choices(target_opt)
    spell_choices["after"]["痛定回春"] = {"use": True, "cycles": [[
        {"x": 4, "target_ref": "player:0", "dodge": False}]]}
    choices = [{"actor_ref": a["actor_ref"], "daowen": None,
                "attack_actions": [{"hits": [{"target_ref": target_opt["ref"], "dodge": False,
                                              "blood_shadow": False, "spell_choices": spell_choices}]}]}]
    r = e.execute_action("resolve_monster_phase", {"token": res["result"]["token"], "choices": choices})
    hp_after = e.state.player.current_hp
    # 先掉5点伤害，再靠【再生4】回12点生命，净变化应该是 +7（若净值高于"只掉伤害"说明确实回血了）
    fired = r["success"] and hp_after > hp_before - 5
    record("失去生命后", wired, fired,
           f"resolve success={r['success']}；玩家hp {hp_before}→{hp_after}（先掉5伤，再靠【再生4】回12血）")


def probe_target_before_daowen():
    """目标发动道纹前：第2回合怪物真正发动道纹时，玩家的反应法术能否触发。"""
    e = _fresh_engine("target_before_daowen", region="扭曲都市")
    for n in ("坠落", "杀伐", "血债"):
        _give_daowen(e.state.player, n)
    definition = {"name": "自创咎由自取", "required_daowen": ["坠落", "杀伐", "血债"],
                  "trigger_condition": "目标发动道纹前",
                  "effect_flow": "发动坠落X于目标→发动杀伐X于目标→发动血债X于目标"}
    learned = _learn(e, definition)
    wired = learned["result"].get("wired")
    e.state.energy = 0
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {})
    e.state.player.current_mana = 20
    e.state.player.current_hp = 200
    enemy = e.state.enemies[0]

    _pure_attack_round(e)  # 消耗掉第1回合白板，进入第2回合
    e.state.player.current_mana = 20

    res = e.execute_action("prepare_monster_phase", {})
    a = res["result"]["actors"][0]
    if not a["daowen_options"]:
        record("目标发动道纹前", wired, False, "第2回合怪物仍无可用道纹，本用例未能构造出触发场景")
        return
    opt = a["daowen_options"][0]
    trigger_options = opt.get("trigger_spell_options", {})
    listed = "player:0" in trigger_options and any(
        sp["spell_name"] == "自创咎由自取" for sp in trigger_options["player:0"])

    dao_choice = {"name": opt["name"], "dodge": False, "blood_shadow": False,
                  "trigger_spell_choices": {"player:0": {"自创咎由自取": {"use": True, "steps": [
                      {"x": 3, "target_ref": "enemy:0", "dodge": False},
                      {"x": 4, "target_ref": "enemy:0", "dodge": False},
                      {"x": 2, "target_ref": "enemy:0", "dodge": False},
                  ]}}}}
    if opt["requires_target"]:
        dao_choice["target_ref"] = opt["target_options"][0]["ref"]
    target_opt = a["attack_target_options"][0]
    choices = [{"actor_ref": a["actor_ref"], "daowen": dao_choice,
                "attack_actions": [{"hits": [{"target_ref": target_opt["ref"], "dodge": False,
                                              "blood_shadow": False,
                                              "spell_choices": _decline_all_spell_choices(target_opt)}]}]}]
    hp_before = enemy.current_hp
    r = e.execute_action("resolve_monster_phase", {"token": res["result"]["token"], "choices": choices})
    hp_after = e.state.enemies[0].current_hp
    fired = r["success"] and listed and hp_after < hp_before
    record("目标发动道纹前", wired, fired,
           f"resolve success={r.get('success')} error={r.get('error')}；"
           f"prepare阶段已列出候选={listed}；敌方hp {hp_before}→{hp_after}")


def probe_condition_branch():
    """条件分支：若自身生命小于100则回血否则反击，验证两条分支在真实战斗里都各自生效。"""
    def build(hp):
        e = _fresh_engine(f"cond_{hp}")
        p = e.state.player
        _give_daowen(p, "杀伐")
        _give_daowen(p, "再生")
        definition = {"name": "临机应变", "required_daowen": ["杀伐", "再生"],
                      "trigger_condition": "受到伤害前",
                      "effect_flow": "若自身 生命 小于 100 则 发动再生X于自身 否则 发动杀伐X于攻击者"}
        learned = _learn(e, definition)
        e.state.energy = 0
        e.execute_action("battle_start", {})
        e.execute_action("round_start", {})
        p.current_mana = 20
        p.current_hp = hp
        enemy = e.state.enemies[0]
        enemy.attack_power = 5
        enemy.attack_count = 1
        res = e.execute_action("prepare_monster_phase", {})
        a = res["result"]["actors"][0]
        target_opt = a["attack_target_options"][0]
        spell_choices = _decline_all_spell_choices(target_opt)
        ref = "player:0" if hp < 100 else "enemy:0"
        spell_choices["before"]["临机应变"] = {"use": True, "cycles": [[
            {"x": 4, "target_ref": ref, "dodge": False}]]}
        choices = [{"actor_ref": a["actor_ref"], "daowen": None,
                    "attack_actions": [{"hits": [{"target_ref": target_opt["ref"], "dodge": False,
                                                  "blood_shadow": False, "spell_choices": spell_choices}]}]}]
        enemy_hp_before = enemy.current_hp
        r = e.execute_action("resolve_monster_phase", {"token": res["result"]["token"], "choices": choices})
        return learned, r, hp, e.state.player.current_hp, enemy_hp_before, e.state.enemies[0].current_hp

    learned_low, r_low, hp0_low, hp1_low, ehp0_low, ehp1_low = build(50)
    learned_high, r_high, hp0_high, hp1_high, ehp0_high, ehp1_high = build(200)
    wired = learned_low["result"].get("wired")
    fired = (r_low["success"] and hp1_low > hp0_low - 5   # 低血分支：扣5伤后应因再生4回血，净变化优于纯扣血
             and r_high["success"] and ehp1_high < ehp0_high)  # 高血分支：应改为反击敌方掉血
    record("条件分支(若...则...否则...)", wired, fired,
           f"低血分支(生命50<100)：玩家hp {hp0_low}→{hp1_low}（应回血）；"
           f"高血分支(生命200≥100)：敌方hp {ehp0_high}→{ehp1_high}（应掉血），"
           f"两分支resolve均success={r_low['success']}/{r_high['success']}")


def probe_any_target():
    """任意目标：把自创反应法术打向使用者自选的任意合法单位（不再靠道纹类型推断身份）。"""
    e = _fresh_engine("any_target")
    p = e.state.player
    _give_daowen(p, "杀伐")
    from engine.models import Entity
    friend = Entity(name="队友甲", entity_type="友方", blood_limit=100, current_hp=100,
                    mana_limit=20, current_mana=20, speed_limit=10, current_speed=10)
    e.state.friends.append(friend)
    definition = {"name": "自选制裁", "required_daowen": ["杀伐"],
                  "trigger_condition": "受到伤害前",
                  "effect_flow": "发动杀伐X于任意目标"}
    learned = _learn(e, definition)
    wired = learned["result"].get("wired")
    e.state.energy = 0
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {})
    p.current_mana = 20
    p.current_hp = 200
    enemy = e.state.enemies[0]
    enemy.attack_power = 5
    enemy.attack_count = 1
    res = e.execute_action("prepare_monster_phase", {})
    a = res["result"]["actors"][0]
    target_opt = a["attack_target_options"][0]
    before_opts = target_opt["spell_options"]["before"]
    listed_candidates = next(sp["steps"][0].get("target_options", [])
                             for sp in before_opts if sp["spell_name"] == "自选制裁")
    spell_choices = _decline_all_spell_choices(target_opt)
    # 使用者不是道纹类型推断出的默认身份，而是自己显式选中战场上的"敌方"作为目标
    spell_choices["before"]["自选制裁"] = {"use": True, "cycles": [[
        {"x": 5, "target_ref": "enemy:0", "dodge": False}]]}
    choices = [{"actor_ref": a["actor_ref"], "daowen": None,
                "attack_actions": [{"hits": [{"target_ref": target_opt["ref"], "dodge": False,
                                              "blood_shadow": False, "spell_choices": spell_choices}]}]}]
    hp_before = enemy.current_hp
    r = e.execute_action("resolve_monster_phase", {"token": res["result"]["token"], "choices": choices})
    hp_after = e.state.enemies[0].current_hp
    candidates_ok = set(listed_candidates) == {"player:0", "friend:0", "enemy:0"}
    fired = r["success"] and candidates_ok and hp_after < hp_before
    record("目标(于任意目标，自选提交)", wired, fired,
           f"prepare阶段列出候选={listed_candidates}（应含玩家自身/友方/敌方三个合法单位）；"
           f"提交选中enemy:0后 resolve success={r['success']}；敌方hp {hp_before}→{hp_after}")


def probe_loop():
    """循环：法力充足时可多次循环发动，法力不足以支撑声明的循环次数时应被拒绝，
    非循环法术强行提交多个cycle同样应被拒绝（对照组）。"""
    e = _fresh_engine("loop_ok")
    p = e.state.player
    _give_daowen(p, "杀伐")
    definition = {"name": "连环杀伐", "required_daowen": ["杀伐"], "trigger_condition": "受到伤害前",
                  "effect_flow": "发动杀伐X于攻击者→循环"}
    learned = _learn(e, definition)
    wired = learned["result"].get("wired")
    e.state.energy = 0
    e.execute_action("battle_start", {})
    e.execute_action("round_start", {})
    p.current_mana = 30
    enemy = e.state.enemies[0]
    enemy.attack_power = 5
    enemy.attack_count = 1
    res = e.execute_action("prepare_monster_phase", {})
    a = res["result"]["actors"][0]
    target_opt = a["attack_target_options"][0]
    loop_flag = next(sp["loop"] for sp in target_opt["spell_options"]["before"]
                     if sp["spell_name"] == "连环杀伐")
    spell_choices = _decline_all_spell_choices(target_opt)
    spell_choices["before"]["连环杀伐"] = {"use": True, "cycles": [
        [{"x": 2, "target_ref": "enemy:0", "dodge": False}] for _ in range(3)]}
    choices = [{"actor_ref": a["actor_ref"], "daowen": None,
                "attack_actions": [{"hits": [{"target_ref": target_opt["ref"], "dodge": False,
                                              "blood_shadow": False, "spell_choices": spell_choices}]}]}]
    hp_before, mana_before = enemy.current_hp, p.current_mana
    r_ok = e.execute_action("resolve_monster_phase", {"token": res["result"]["token"], "choices": choices})
    hp_after_ok, mana_after_ok = e.state.enemies[0].current_hp, p.current_mana

    # 对照1：法力不足以支撑声明的循环次数 → 应拒绝，不能"打到哪算哪"
    e2 = _fresh_engine("loop_insufficient_mana")
    p2 = e2.state.player
    _give_daowen(p2, "杀伐")
    _learn(e2, definition)
    e2.state.energy = 0
    e2.execute_action("battle_start", {})
    e2.execute_action("round_start", {})
    p2.current_mana = 5  # 只够1次杀伐2，不够3次
    e2.state.enemies[0].attack_power = 5
    e2.state.enemies[0].attack_count = 1
    res2 = e2.execute_action("prepare_monster_phase", {})
    a2 = res2["result"]["actors"][0]
    target_opt2 = a2["attack_target_options"][0]
    spell_choices2 = _decline_all_spell_choices(target_opt2)
    spell_choices2["before"]["连环杀伐"] = {"use": True, "cycles": [
        [{"x": 2, "target_ref": "enemy:0", "dodge": False}] for _ in range(3)]}
    choices2 = [{"actor_ref": a2["actor_ref"], "daowen": None,
                "attack_actions": [{"hits": [{"target_ref": target_opt2["ref"], "dodge": False,
                                              "blood_shadow": False, "spell_choices": spell_choices2}]}]}]
    r_mana_reject = e2.execute_action("resolve_monster_phase", {"token": res2["result"]["token"], "choices": choices2})

    # 对照2：非循环法术强行提交2个cycle → 应拒绝（证明"只能提交一个cycle"仍对非循环法术生效，
    # 循环能力是显式声明才解锁的新增语义，不是把限制去掉）
    e3 = _fresh_engine("non_loop_reject")
    p3 = e3.state.player
    _give_daowen(p3, "杀伐")
    non_loop_def = {"name": "单发杀伐", "required_daowen": ["杀伐"], "trigger_condition": "受到伤害前",
                    "effect_flow": "发动杀伐X于攻击者"}
    _learn(e3, non_loop_def)
    e3.state.energy = 0
    e3.execute_action("battle_start", {})
    e3.execute_action("round_start", {})
    p3.current_mana = 30
    e3.state.enemies[0].attack_power = 5
    e3.state.enemies[0].attack_count = 1
    res3 = e3.execute_action("prepare_monster_phase", {})
    a3 = res3["result"]["actors"][0]
    target_opt3 = a3["attack_target_options"][0]
    spell_choices3 = _decline_all_spell_choices(target_opt3)
    spell_choices3["before"]["单发杀伐"] = {"use": True, "cycles": [
        [{"x": 2, "target_ref": "enemy:0", "dodge": False}],
        [{"x": 2, "target_ref": "enemy:0", "dodge": False}]]}
    choices3 = [{"actor_ref": a3["actor_ref"], "daowen": None,
                "attack_actions": [{"hits": [{"target_ref": target_opt3["ref"], "dodge": False,
                                              "blood_shadow": False, "spell_choices": spell_choices3}]}]}]
    r_non_loop_reject = e3.execute_action("resolve_monster_phase", {"token": res3["result"]["token"], "choices": choices3})

    fired = (loop_flag is True and r_ok["success"] and hp_after_ok < hp_before
             and mana_after_ok < mana_before
             and r_mana_reject["success"] is False
             and r_non_loop_reject["success"] is False)
    record("循环(多cycle+安全阀)", wired, fired,
           f"loop字段={loop_flag}；3次循环resolve success={r_ok['success']}，"
           f"敌方hp {hp_before}→{hp_after_ok}，玩家法力 {mana_before}→{mana_after_ok}；"
           f"法力不足以支撑声明循环次数时应拒绝：success={r_mana_reject['success']} "
           f"error={r_mana_reject.get('error')!r}；"
           f"非循环法术强行提交2个cycle应拒绝：success={r_non_loop_reject['success']} "
           f"error={r_non_loop_reject.get('error')!r}")


def probe_learn_time_rejection():
    """学习阶段拒绝：句式/字段/触发词错误必须在学习时就报错，不允许静默学会摆设法术。"""
    cases = [
        ("触发时机乱写", "乱七八糟的时机词", "发动杀伐X于攻击者"),
        ("效果步骤缺少目标声明", "受到伤害前", "发动杀伐X"),
        ("目标身份非法", "受到伤害前", "发动杀伐X于路人甲"),
        ("引用道纹格式不合法", "受到伤害前", "发动不存在道纹X于攻击者"),
        ("条件分支缺少“则”", "受到伤害前", "若自身 生命 小于 50 发动杀伐X于攻击者"),
        ("条件表达式字段非法", "受到伤害前", "若自身 血量 小于 50 则 发动杀伐X于攻击者"),
    ]
    all_rejected = True
    details = []
    for label, trig, flow in cases:
        e = _fresh_engine(f"reject_{label}")
        _give_daowen(e.state.player, "杀伐")
        d = {"name": f"坏法术_{label}", "required_daowen": ["杀伐"],
             "trigger_condition": trig, "effect_flow": flow}
        r = e.execute_action("pre_battle_action", {
            "sub_action": "学习", "sub": "custom_spell", "spell": d})
        rejected = not r["success"]
        all_rejected = all_rejected and rejected
        details.append(f"[{label}] 拒绝={rejected} error={r.get('error')!r}")
    record("学习阶段句式校验(拒绝坏法术)", None, all_rejected, "；".join(details))


def probe_unwired(trigger_text, label):
    e = _fresh_engine(f"unwired_{label}")
    _give_daowen(e.state.player, "杀伐")
    definition = {"name": f"未接线测试_{label}", "required_daowen": ["杀伐"],
                  "trigger_condition": trigger_text,
                  "effect_flow": "发动杀伐X于攻击者"}
    learned = _learn(e, definition)
    if not learned["success"]:
        record(label, None, False, f"学习被拒绝：{learned.get('error')}")
        return
    wired = learned["result"].get("wired")
    warning = learned["result"].get("warning", "")
    record(label, wired, "N/A（无对应结算点，见warning）", f"warning={warning!r}")


def main():
    probe_before_damage()
    probe_after_life_lost()
    probe_target_before_daowen()
    probe_condition_branch()
    probe_any_target()
    probe_loop()
    probe_learn_time_rejection()
    for trigger_text, label in [
        ("受到伤害后", "受到伤害后"),
        ("失去生命前", "失去生命前"),
        ("战斗开始时", "战始"),
        ("战斗结束时", "战终"),
        ("回合开始时", "回始"),
        ("回合结束时", "回终"),
        ("敌方回合开始时", "敌回始"),
        ("敌方回合结束时", "敌回终"),
    ]:
        probe_unwired(trigger_text, label)

    print("=" * 78)
    print("自创法术触发时机 —— 真实引擎实测结果（2026-08-29 修订版）")
    print("=" * 78)
    for row in REPORT:
        print(f"[{row['trigger']}] learn阶段wired={row['learn_wired']} "
              f"实际触发={row['actually_fired']}")
        print(f"    {row['detail']}")
    print("=" * 78)
    wired_triggers = [r for r in REPORT if r["learn_wired"] is True]
    fired_count = sum(1 for r in wired_triggers if r["actually_fired"] is True)
    print(f"已接线的{len(wired_triggers)}种时机中，真实触发成功：{fired_count}/{len(wired_triggers)}")
    return REPORT


if __name__ == "__main__":
    main()
