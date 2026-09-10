"""`sim/tournament.py`：赛制推进逻辑（不跑真死斗，桩掉 `_play`）。

这些测试盯的是「谁晋级／谁换主／摘要怎么写」——之前 `summary_gauntlet` 把胜者
当成上台者打印过（7 行全写成同一个名字），这里留回归。
"""
import sim.tournament as tn


def _rec(ch, df, winner, rounds=6, cause="凡庸"):
    """最小可用记录：赛制逻辑只读这几个键。"""
    dead_seat = "守擂" if winner == "challenger" else "挑战"
    book = {}
    for seat in ("挑战", "守擂"):
        book[seat] = {"存活": seat != dead_seat, "死因source": cause if seat == dead_seat else "",
                      "死因subtype": "mediocrity" if seat == dead_seat else ""}
    return {"挑战档名": ch, "守擂档名": df, "seed": 1,
            "判定": {"winner": winner, "rounds": rounds, "reason": "x"},
            "终局账面": book, "开局": {}, "回合": []}


def _stub(monkeypatch, table):
    """table[(challenger, defender)] = winner。"""
    def fake_play(challenger, defender, seed, retries):
        winner = table[(challenger, defender)]
        rec = _rec(challenger, defender, winner)
        return [rec], rec, winner
    monkeypatch.setattr(tn, "_play", fake_play)


def test_bracket_advances_winners_and_gives_bye_to_last(monkeypatch):
    files = ["a.json", "b.json", "c.json", "d.json", "e.json"]
    # 全部判挑战席胜；5 人 → 首轮 2 局 + 末位轮空 → 3 人 → 1 局 + 轮空 → 2 人 → 决赛
    _stub(monkeypatch, {(a, b): "challenger" for a in files for b in files if a != b})

    rounds, champion, abort = tn.bracket(files, seed=1, retries=0)

    assert abort == ""
    assert champion in files
    # 5 人 → 首轮 2 局 + 末位轮空 → 3 人 → 1 局 + 轮空 → 2 人 → 决赛
    assert [len(alive) for _r, _recs, alive, _w in rounds] == [5, 3, 2]
    assert [len(winners) for _r, _recs, _alive, winners in rounds] == [3, 2, 1]
    # 单败淘汰：N 人恰好 N-1 局（轮空那格 rec 为 None，不计入）
    played = [rec for _r, recs, _a, _w in rounds for _at, rec, _x in recs if rec is not None]
    assert len(played) == len(files) - 1
    # 奇数人数那一轮必须有一个轮空位
    assert any(rec is None for _r, recs, _a, _w in rounds for _at, rec, _x in recs)
    # 挑战席恒胜 → 每局晋级的都是挑战方那一档
    assert all(rec["挑战档名"] in winners
               for _r, _recs, _alive, winners in rounds
               for _at, rec, _x in _recs if rec is not None)


def test_gauntlet_lord_changes_when_challenger_wins(monkeypatch):
    files = ["lord.json", "x.json", "y.json"]
    _stub(monkeypatch, {("x.json", "lord.json"): "challenger",   # 换主
                        ("y.json", "x.json"): "defender"})       # x 卫冕

    recs, lord, abort = tn.gauntlet(files, "lord.json", seed=1, retries=0, order="sorted")

    assert abort == ""
    assert lord == "x.json"
    assert len(recs) == 2


def test_summary_gauntlet_names_the_challenger_not_the_winner(monkeypatch):
    """回归：上台的必须是挑战方那一档，卫冕行也要写清擂主是谁。"""
    files = ["lord.json", "x.json"]
    _stub(monkeypatch, {("x.json", "lord.json"): "defender"})
    recs, lord, abort = tn.gauntlet(files, "lord.json", seed=1, retries=0, order="sorted")

    text = tn.summary_gauntlet(recs, lord, abort, "lord.json", seed=1)

    assert "x 上台 → 擂主 lord 卫冕" in text          # 摘要里档位名去掉 .json
    assert "最后站在台上的人：lord" in text


def test_gauntlet_aborts_honestly_on_wall_clock_guard(monkeypatch):
    """墙钟兜底（winner=None）不得替规则判胜负：中止并写明。"""
    monkeypatch.setattr(tn, "_play", lambda c, d, s, r: (
        [_rec(c, d, None)], _rec(c, d, None), None))

    recs, lord, abort = tn.gauntlet(["lord.json", "x.json"], "lord.json",
                                    seed=1, retries=1, order="sorted")

    assert "墙钟兜底" in abort and "中止" in abort
    assert lord == "lord.json"  # 保持原擂主，不宣布任何结果
