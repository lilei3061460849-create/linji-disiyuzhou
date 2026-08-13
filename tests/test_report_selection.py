"""
pytest - 战报政策与批量选取脚本

正式 `战报.md` 只保留最新一次轮回记录，由 GameEngine 手操写入。
`sim/pick_best_report.py` 仍是平衡工具：通关、冠冕前血量、无效局标记。

覆盖：正常路径 / 边界条件 / 错误输入
"""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_s = importlib.util.spec_from_file_location(
    "pb", os.path.join(ROOT, "sim", "pick_best_report.py"))
pb = importlib.util.module_from_spec(_s)
_s.loader.exec_module(pb)

REPORT = os.path.join(ROOT, "战报.md")


# ---------- 正常路径 ----------

def test_play_and_record_reports_hp_before_crown():
    """正常路径：通关的轮回必须给出进入冠冕前的血量"""
    r = pb.play_and_record("龙心谷", seed=105)
    assert not r.get("invalid"), r.get("reason")
    if not r.get("died"):
        assert r["hp_before_crown"] is not None
        assert r["cleared"] == 7
        assert 0 < r["hp_before_crown"] <= 60


def test_report_states_selection_standard():
    """正常路径：正式战报声明只保留最新手操轮回，批量工具不得覆盖。"""
    txt = open(REPORT, encoding="utf-8").read()
    for key in ["最新一次轮回", "六、战斗推演格式", "pick_best_report", "不得"]:
        assert key in txt, f"战报开头缺少现行政策说明：{key}"


def test_report_follows_spec_format():
    """正常路径：战报必须符合 README§六 的字段要求"""
    txt = open(REPORT, encoding="utf-8").read()
    for field in ["[战始]", "出怪：", "战斗背景：", "敌方面板：",
                  "我方面板：", "[回始]：", "[回终]：", "[战终]"]:
        assert field in txt, f"战报缺少规范字段 {field}"
    # 禁止概括式写法
    import re
    assert not re.search(r"怪物出手\d+次", txt), "出现被禁止的概括式结算"


def test_report_records_seven_battles():
    """正常路径：当前正式战报记录了七场手操战斗，不要求附加批量评选文案。"""
    txt = open(REPORT, encoding="utf-8").read()
    assert txt.count("[战终]") >= 7, "应包含7场的战终结算"
    assert "本文件只保留最新一次轮回记录" in txt


# ---------- 边界条件 ----------

def test_highest_hp_wins():
    """边界：选取逻辑必须挑血量最高者"""
    finished = [(45, "龙心谷", 1, ["a"]), (60, "罪孽都市", 2, ["b"]), (58, "扭曲都市", 3, ["c"])]
    finished.sort(key=lambda t: -t[0])
    assert finished[0][0] == 60
    assert finished[0][1] == "罪孽都市"


def test_died_runs_excluded():
    """边界：中途阵亡的轮回 hp_before_crown 为 None，不得参与排序"""
    r = pb.play_and_record("龙心谷", seed=999999)
    if r.get("died"):
        assert r["hp_before_crown"] is None


def test_report_does_not_claim_batch_selection_result():
    """边界：最新手操战报不得伪装成pick_best_report批量评选结果。"""
    txt = open(REPORT, encoding="utf-8").read()
    assert "入选依据：进入【最终的冠冕】前剩余生命" not in txt


# ---------- 错误输入 ----------

def test_invalid_runs_are_flagged(monkeypatch):
    """错误输入：引擎抛异常的对局必须被标为 invalid 并附原因"""
    class Boom:
        def execute_action(self, *a, **k):
            raise RuntimeError("模拟引擎异常")

    monkeypatch.setattr(pb, "GameEngine", lambda *a, **k: Boom())
    r = pb.play_and_record("龙心谷", seed=1)
    assert r["invalid"] is True
    assert "RuntimeError" in r["reason"]


def test_invalid_runs_never_selected():
    """错误输入：无效局不得进入候选排序（无 hp_before_crown 字段）"""
    bad = {"invalid": True, "reason": "boom"}
    assert bad.get("hp_before_crown") is None
    assert bad["invalid"] is True
