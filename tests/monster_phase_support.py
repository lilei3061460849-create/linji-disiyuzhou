"""测试专用：为两阶段怪物API构造完整显式选择；不属于生产决策层。"""
from __future__ import annotations


def resolve_monster_phase(combat, daowen_choices=None, *, dodge=False, target_refs=None):
    """直接调用CombatEngine的prepare/resolve，供计算单元测试复用。

    daowen_choices: {actor_ref或怪物名: 道纹名/None}；未提供时选第一个合法项。
    target_refs: {actor_ref或怪物名: 目标ref}，默认取prepare列出的第一个合法目标。
    """
    daowen_choices = daowen_choices or {}
    target_refs = target_refs or {}
    prepared = combat.prepare_monster_phase()
    choices = []
    for actor in prepared["actors"]:
        key = actor["actor_ref"]
        monster = actor["monster"]
        requested = daowen_choices.get(key, daowen_choices.get(monster, "__first__"))
        dao = None
        action_count = actor["base_attack_actions"]
        hit_count = actor["base_hits_per_attack"]
        if requested == "__first__" and actor["daowen_options"]:
            requested = actor["daowen_options"][0]["name"]
        if requested is not None and requested != "__first__":
            option = next(o for o in actor["daowen_options"] if o["name"] == requested)
            dao = {"name": requested, "dodge": False}
            if option["requires_target"]:
                dao["target_ref"] = target_refs.get(
                    key, target_refs.get(monster, option["target_options"][0]["ref"]))
            if option["dodge_submission"] == "per_target":
                dao["dodge_targets"] = [
                    {"target_ref": target["ref"], "dodge": False}
                    for target in option["dodge_target_options"]
                ]
            if option["resolves_as"] == "活力":
                action_count += option["x"]
            elif option["resolves_as"] == "狂暴":
                action_count += 1
            elif option["resolves_as"] == "变形":
                enemy_index = int(key.split(":", 1)[1])
                hit_count = combat.state.enemies[enemy_index].attack_power
        attack_target = actor["attack_target_options"][0]["ref"]
        attacks = [{"hits": [{"target_ref": attack_target, "dodge": bool(dodge)}
                              for _ in range(hit_count)]}
                   for _ in range(action_count)]
        choices.append({"actor_ref": key, "daowen": dao, "attack_actions": attacks})
    return combat.resolve_monster_phase(choices, prepared=prepared)
