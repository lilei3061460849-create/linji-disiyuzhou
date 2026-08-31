"""`sim/duel_records.py`：凡庸活值抓取（结算会清零，必须在入口抓）。

背景：`_apply_mediocrity`（engine/combat.py:5481）在把 hp 置 0 之后，于 `:5494-5495`
把 `no_action_rounds`/`no_damage_rounds` 清零，所以事后读盘面永远是 0——
实录里的「凡庸触发口径」必须来自结算入口的活值快照。
"""
import types

import sim.duel_records as dr


def _entity(**kw):
    base = dict(name="测试", current_hp=0, blood_limit=36, shield=0, mutation_count=0,
                total_healed=0, no_action_rounds=0, no_damage_rounds=0,
                is_alive=False, status_effects=[], _death_ctx=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_mediocrity_hook_records_live_counters_and_delegates(monkeypatch):
    """钩子必须在原方法清零之前记录两个计数与触发口径，并原样委托。"""
    seen = {}

    def fake_orig(self, entity, why):
        seen["args"] = (entity, why)
        return ["结算条目"]

    monkeypatch.setattr(dr, "_ORIG_MEDIOCRITY", fake_orig)
    dr._LIVE.clear()
    ent = _entity(no_action_rounds=4, no_damage_rounds=5)

    out = dr._mediocrity_hook(object(), ent, "连续五回合未能使敌对角色生命减少")

    assert dr._LIVE[id(ent)] == (4, 5, "连续五回合未能使敌对角色生命减少")
    assert seen["args"] == (ent, "连续五回合未能使敌对角色生命减少")
    assert out == ["结算条目"]
    dr._LIVE.clear()


def test_book_reports_live_counters_not_zeroed_ones(monkeypatch):
    """终局账面要报活值（4/5 + 口径），而不是被清零后的 0/0。"""
    ent = _entity(no_action_rounds=0, no_damage_rounds=0,
                  _death_ctx={"subtype": "mediocrity", "source": "凡庸", "actor": "测试"})
    engine = types.SimpleNamespace(
        state=types.SimpleNamespace(player=ent, enemies=[]),
        combat=types.SimpleNamespace(cancer_threshold_of=lambda e: 72))
    monkeypatch.setitem(dr._LIVE, id(ent), (4, 5, "连续五回合未能使敌对角色生命减少"))

    book = dr._book(engine)["挑战"]

    assert book["未出手回合"] == 4
    assert book["未使敌掉血回合"] == 5
    assert book["凡庸口径"] == "连续五回合未能使敌对角色生命减少"
    assert book["癌变线"] == 72
    assert book["死因source"] == "凡庸"
    dr._LIVE.clear()


def test_render_marks_untriggered_mediocrity():
    """没触发凡庸的一方要写明「未触发」，不能留空口径。"""
    rec = {"challenger": "a.json", "defender": "b.json", "seed": 1,
           "开局": {"挑战": {"name": "林渊", "hp": 36, "bl": 36, "shield": 0, "mana": 0,
                             "ml": 30, "speed": 12, "sl": 12, "used": 0, "acts": 4},
                    "守擂": {"name": "阮烟", "hp": 36, "bl": 36, "shield": 0, "mana": 0,
                             "ml": 28, "speed": 11, "sl": 11, "used": 0, "acts": 4}},
           "回合": [],
           "终局账面": {"挑战": {"名字": "林渊", "hp": 36, "血限": 36, "盾": 0, "异变": 0,
                                 "累计回复": 0, "癌变线": 72, "未出手回合": 0,
                                 "未使敌掉血回合": 2, "凡庸口径": "", "死因subtype": "",
                                 "死因source": "", "死因actor": "", "存活": True}},
           "判定": {"winner": "challenger", "rounds": 3, "reason": "守擂主将阵亡"}}
    out = dr.render(rec, 1, "B1")
    assert "未使敌掉血2回合（未触发凡庸；线=5回合）" in out
    assert "命零" not in out
