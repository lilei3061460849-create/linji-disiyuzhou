"""测试专用：为两阶段攻击接口构造完整显式提交。"""


def decline_spell_choices(option):
    spell_options = option.get("spell_options", {})
    return {
        timing: {spell["spell_name"]: {"use": False}
                 for spell in spell_options.get(timing, [])}
        for timing in ("before", "after")
    }


def resolve_attack(engine, attacker_name=None, target_selections=None, *, dodge=False,
                   blood_shadow=False):
    if engine.state.in_final_duel and engine.state.combat_subphase == "await_round_start":
        started = engine.execute_action("round_start", {"relic_choices": {}})
        if not started.get("success"):
            return started
    refs = engine.combat._combat_entity_refs()
    if attacker_name is None:
        actor_ref = "player:0"
    else:
        matches = [ref for ref, entity in refs.items() if entity.name == attacker_name]
        if len(matches) != 1:
            return engine.execute_action("prepare_attack", {"actor_ref": "invalid"})
        actor_ref = matches[0]
    prepared = engine.execute_action("prepare_attack", {"actor_ref": actor_ref})
    if not prepared.get("success"):
        return prepared
    result = prepared["result"]
    target_selections = target_selections or []
    hits = []
    for index in range(result["hit_count"]):
        selected = target_selections[index] if index < len(target_selections) else 0
        option = result["target_options"][selected]
        hits.append({
            "target_ref": option["ref"],
            "dodge": bool(dodge),
            "blood_shadow": bool(blood_shadow),
            "spell_choices": decline_spell_choices(option),
        })
    return engine.execute_action("resolve_attack", {"token": result["token"], "hits": hits})
