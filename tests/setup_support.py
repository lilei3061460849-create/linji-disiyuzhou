"""测试共用：走完开局属性分配后的初始道纹发现，并安全进入战始。"""
from engine.models import DaoWen, DaoWenInstance

OPTIONAL_BATTLE_START = ("折速法印", "三相残韵盘", "猩红果实", "苍白之花")
OPTIONAL_ROUND_START = ("血契", "余火印")


def finish_initial_daowen(engine, prefer: str = "杀伐", only_prefer: bool = True):
    """属性分配后必须显式选择发现候选。优先选 prefer，否则选第一项。

    默认 only_prefer=True：选择完成后只保留 prefer，避免发现到的额外闭环道纹
    干扰“学习庇护”或进化借用池等只关心杀伐的回归。
    """
    player = engine.state.player
    if player is None:
        return {"success": False, "error": "没有玩家"}
    choices = list(engine.state.pending_initial_daowen_choices)
    if choices:
        pick = prefer if prefer in choices else choices[0]
        result = engine.execute_action("setup_choose_initial_daowen", {"daowen_name": pick})
        if not result.get("success"):
            return result
    if prefer and prefer not in player.dao_wen:
        player.dao_wen[prefer] = DaoWenInstance(DaoWen(
            name=prefer, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    if only_prefer and prefer:
        for name in list(player.dao_wen):
            if name != prefer:
                del player.dao_wen[name]
    return {"success": True, "daowen": list(player.dao_wen)}


def begin_battle(engine, relic_choices=None):
    """提交可选战始遗物后进入战斗。未列出的可选遗物默认 use=False。"""
    engine.state.energy = 0
    active = {relic.name for relic in engine.state.relics}
    choices = dict(relic_choices or {})
    for name in OPTIONAL_BATTLE_START:
        if name in active and name not in choices:
            choices[name] = {"use": False}
    return engine.execute_action("battle_start", {"relic_choices": choices})


def begin_round(engine, relic_choices=None):
    """提交可选回始遗物。未列出的可选遗物默认 use=False。"""
    active = {relic.name for relic in engine.state.relics}
    choices = dict(relic_choices or {})
    for name in OPTIONAL_ROUND_START:
        if name in active and name not in choices:
            choices[name] = {"use": False}
    return engine.execute_action("round_start", {"relic_choices": choices})
