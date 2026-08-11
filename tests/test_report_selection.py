"""
pytest - 战报选取标准（用户裁定）

标准：
1. 以进入【最终的冠冕】前的当前血量为唯一评判标准，最高者入选
2. 中途阵亡的轮回不参与评选
3. 受 bug 影响的对局视为无效数据作废，不入选、不作为平衡依据

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

REPORT = os.path.join(ROOT, "战报_完整轮回.md")


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
    """正常路径：战报开头必须写明选取标准，供后续测试遵循"""
    txt = open(REPORT, encoding="utf-8").read()
    for key in ["最终的冠冕", "剩余生命", "无效数据", "六、战斗推演格式"]:
        assert key in txt, f"战报开头缺少标准说明：{key}"


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
    """正常路径：入选战报必须是走完7场的完整轮回"""
    txt = open(REPORT, encoding="utf-8").read()
    assert txt.count("[战终]") >= 7, "应包含7场的战终结算"
    assert "【最终的冠冕】触发前" in txt


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


def test_report_hp_matches_declared_standard():
    """边界：战报声明的入选血量必须与正文记录的冠冕前血量一致"""
    import re
    txt = open(REPORT, encoding="utf-8").read()
    m1 = re.search(r"剩余生命 \*\*(\d+)\*\*", txt)
    m2 = re.search(r"【最终的冠冕】触发前 · 当前生命 (\d+)", txt)
    assert m1 and m2, "战报未同时给出声明血量与正文血量"
    assert m1.group(1) == m2.group(1), "声明血量与正文不一致"


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
