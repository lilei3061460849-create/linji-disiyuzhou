from tests.test_exclusive_zui_twisted import _setup, _grant, _add_monster


def test_dianjin_pays_amnesia_not_mana_and_gains_shards():
    e = _setup(mana=20)
    _grant(e, ["点金", "杀伐", "庇护"])
    _add_monster(e, shards=0)
    p = e.state.player
    mana = p.current_mana
    shards = e.state.shards
    r = e.execute_action("use_daowen", {"daowen_name": "点金", "x": 2})
    assert r["success"], r
    assert p.current_mana == mana
    assert e.state.shards == shards + 160
    assert len(p.dao_wen) == 1
    assert set(p.dao_wen) == {"点金"}


def test_amnesia_named_cost_cannot_exceed_owned_daowen():
    e = _setup(mana=20)
    _grant(e, ["点金"])
    before = e.state.shards
    x = len(e.state.player.dao_wen) + 1
    r = e.execute_action("use_daowen", {"daowen_name": "点金", "x": x})
    assert not r["success"] and f"失忆{x}无法支付" in r["error"]
    assert e.state.shards == before and "点金" in e.state.player.dao_wen
