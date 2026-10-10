"""回归测试：残韵必须尊重显式目标，并将同魂笔结果归给真正施法者。"""
from types import SimpleNamespace

from engine.api import GameEngine


def _stub_engine(player, refs):
    engine = object.__new__(GameEngine)
    engine.state = SimpleNamespace(player=player, relics=[])
    engine.combat = SimpleNamespace(_combat_entity_refs=lambda: refs)
    return engine


def test_find_resonance_holder_respects_explicit_target_when_player_also_has_source():
    player = SimpleNamespace(name="玩家", dao_wen={"杀伐": object()})
    enemy = SimpleNamespace(name="敌人", dao_wen={"杀伐": object()})
    engine = _stub_engine(player, {"player:0": player, "enemy:0": enemy})

    holder, error = engine._find_resonance_holder("杀伐", "enemy:0")

    assert error is None
    assert holder is enemy


def test_find_resonance_holder_requires_explicit_target_when_source_is_ambiguous():
    player = SimpleNamespace(name="玩家", dao_wen={"杀伐": object()})
    enemy = SimpleNamespace(name="敌人", dao_wen={"杀伐": object()})
    engine = _stub_engine(player, {"player:0": player, "enemy:0": enemy})

    holder, error = engine._find_resonance_holder("杀伐", "")

    assert holder is None
    assert "多名角色持有杀伐" in error


def test_same_soul_pen_uses_actual_actor_for_second_conversion(monkeypatch):
    # 非玩家施法者自身持有源道纹，玩家不持有；同魂笔第二次转换仍应识别并奖励施法者。
    player = SimpleNamespace(name="玩家", dao_wen={})
    actor = SimpleNamespace(
        name="守擂者",
        dao_wen={"杀伐": object()},
        resonance={"反转": 1},
        entity_type="轮回者",
    )
    first_target = SimpleNamespace(
        name="第一目标", dao_wen={"杀伐": object()}, entity_type="角色"
    )
    second_target = SimpleNamespace(
        name="第二目标", dao_wen={"杀伐": object()}, entity_type="角色"
    )
    refs = {
        "player:0": player,
        "actor:0": actor,
        "enemy:0": first_target,
        "enemy:1": second_target,
    }
    engine = _stub_engine(player, refs)
    engine.state.relics = [SimpleNamespace(name="同魂笔")]

    conversions = []
    grants = []

    monkeypatch.setattr(
        engine, "_permanently_convert_daowen",
        lambda holder, source, dest: conversions.append((holder, source, dest)) or True,
    )
    monkeypatch.setattr(
        engine, "_grant_transformed_daowen",
        lambda recipient, dest: grants.append((recipient, dest)) or True,
    )

    result = engine._action_use_resonance({
        "actor_ref": "actor:0",
        "source_daowen": "杀伐",
        "resonance_type": "反转",
        "target_ref": "enemy:0",
        "target_daowen": "再生",
        "second_target_ref": "enemy:1",
        "second_source_daowen": "杀伐",
        "second_target_daowen": "再生",
    })

    assert result["success"] is True
    assert conversions == [
        (first_target, "杀伐", "再生"),
        (second_target, "杀伐", "再生"),
    ]
    assert grants[0] == (actor, "再生")
    assert grants[1] == (actor, "再生")
    assert player.dao_wen == {}
