"""
pytest - 自学习流派优化器（sim/build_learner.py）

它不预设流派，而是自己组合道纹、跑轮回、按胜负更新权重，
并挖掘"1+1>2"的协同增益，知识写入 data/build_knowledge.json 可续跑。

覆盖：正常路径 / 边界条件 / 错误输入
"""
import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load():
    spec = importlib.util.spec_from_file_location(
        "bl", os.path.join(ROOT, "sim", "build_learner.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bl = _load()


# ---------- 正常路径 ----------

def test_play_returns_wellformed_result():
    """正常路径：跑一局应返回通关场数与胜负"""
    r = bl.play("杀伐", ["庇护", "再生"], "龙心谷", seed=1)
    assert set(r) == {"cleared", "won"}
    assert 0 <= r["cleared"] <= 7
    assert isinstance(r["won"], bool)


def test_synergy_detects_positive_pair():
    """
    正常路径：协同挖掘必须能识别出 1+1>2。
    构造：A、B 单独都低分，一起出现时高分 → 增益应为正。
    """
    k = {"generation": 0, "trials": {}, "pair_scores": {}, "history": [], "best": None}
    bl.update(k, "A", ["X"], 1.0)        # A 单独 低分
    bl.update(k, "B", ["Y"], 1.0)        # B 单独 低分
    bl.update(k, "A", ["B"], 9.0)        # A+B   高分
    bl.update(k, "A", ["B"], 9.0)
    syn = bl.synergies(k, min_n=2)
    pair = [s for s in syn if {s[1], s[2]} == {"A", "B"}]
    assert pair, "未能挖掘出 A+B 组合"
    assert pair[0][0] > 0, f"A+B 应为正协同，实际 {pair[0][0]}"


def test_update_accumulates_knowledge():
    """正常路径：多次 update 后单体价值与配对分均被累计"""
    k = {"generation": 0, "trials": {}, "pair_scores": {}, "history": [], "best": None}
    bl.update(k, "杀伐", ["庇护", "再生"], 8.0)
    assert k["trials"]["杀伐"]["n"] == 1
    assert k["trials"]["庇护"]["sum"] == 8.0
    assert "庇护|杀伐" in k["pair_scores"] or "杀伐|庇护" in k["pair_scores"]
    assert k["best"]["score"] == 8.0


# ---------- 边界条件 ----------

def test_best_only_improves():
    """边界：best 只应在更高分时被替换"""
    k = {"generation": 0, "trials": {}, "pair_scores": {}, "history": [], "best": None}
    bl.update(k, "杀伐", ["庇护"], 7.0)
    bl.update(k, "锐利", ["束缚"], 3.0)
    assert k["best"]["score"] == 7.0
    bl.update(k, "锐利", ["封印"], 9.0)
    assert k["best"]["score"] == 9.0


def test_ucb_prefers_untried():
    """边界：未尝试过的道纹必须获得最高探索优先级"""
    k = {"generation": 0, "trials": {"A": {"n": 5, "sum": 50.0}},
         "pair_scores": {}, "history": [], "best": None}
    assert bl.ucb(k, "从未试过", 5) > bl.ucb(k, "A", 5)


def test_propose_respects_build_size():
    """边界：生成的 build 不得超过设定长度，且不含初始道纹自身"""
    import random
    k = {"generation": 0, "trials": {}, "pair_scores": {}, "history": [], "best": None}
    for i in range(10):
        starter, learn = bl.propose(k, random.Random(i))
        assert starter in bl.STARTERS
        assert len(learn) <= bl.BUILD_SIZE
        assert starter not in learn


def test_synergy_ignores_low_sample_pairs():
    """边界：样本不足的配对不得进入结论（避免噪声当规律）"""
    k = {"generation": 0, "trials": {}, "pair_scores": {}, "history": [], "best": None}
    bl.update(k, "A", ["B"], 10.0)       # 只出现1次
    assert bl.synergies(k, min_n=5) == []


# ---------- 错误输入 ----------

def test_candidates_are_all_registered():
    """错误输入检出：候选池中不得含引擎未注册的道纹"""
    from engine.daowen import DaoWenEngine
    DaoWenEngine.register_all()
    unknown = [c for c in bl.CANDIDATES if c not in DaoWenEngine._registry]
    assert not unknown, f"候选池含未注册道纹：{unknown}"


def test_load_handles_missing_file(tmp_path, monkeypatch):
    """错误输入：知识库不存在时应返回初始结构而非崩溃"""
    monkeypatch.setattr(bl, "KNOWLEDGE", str(tmp_path / "nope.json"))
    k = bl.load()
    assert k["generation"] == 0 and k["trials"] == {}


def test_save_load_roundtrip(tmp_path, monkeypatch):
    """边界：知识库存盘后应能原样读回（支持续跑累积经验）"""
    monkeypatch.setattr(bl, "KNOWLEDGE", str(tmp_path / "k.json"))
    k = bl.load()
    bl.update(k, "杀伐", ["束缚", "封印"], 9.5)
    k["generation"] = 3
    bl.save(k)
    k2 = bl.load()
    assert k2["generation"] == 3
    assert k2["best"]["score"] == 9.5
    assert k2["trials"]["束缚"]["n"] == 1


# ---------- 随机种子模式 ----------

def test_play_accepts_none_seed():
    """正常路径：seed=None 时引擎使用真随机源，仍应返回合法结果"""
    r = bl.play("杀伐", ["庇护"], "龙心谷", None)
    assert 0 <= r["cleared"] <= 7
    assert isinstance(r["won"], bool)


def test_fixed_seed_is_reproducible():
    """边界：固定种子必须完全可复现（否则无法排查问题）"""
    a = bl.play("杀伐", ["庇护", "再生"], "龙心谷", 42)
    b = bl.play("杀伐", ["庇护", "再生"], "龙心谷", 42)
    assert a == b, "同一固定种子两次结果不一致"


def test_fitness_fixed_mode_is_deterministic():
    """边界：非随机模式下同参数 fitness 必须一致"""
    f1 = bl.fitness("杀伐", ["庇护", "再生"], 3, gen=1)
    f2 = bl.fitness("杀伐", ["庇护", "再生"], 3, gen=1)
    assert f1 == f2


def test_random_seed_mode_varies_samples():
    """
    正常路径：随机模式下不同 rng 应产生不同的评估样本。
    （比较 fitness 数值可能偶然相等，故直接校验取样过程会用到 rng）
    """
    import random as _r
    calls = []

    class SpyRandom(_r.Random):
        def randrange(self, *a, **k):
            v = super().randrange(*a, **k)
            calls.append(v)
            return v

    bl.fitness("杀伐", ["庇护"], 3, gen=1, random_seeds=True, rng=SpyRandom(5))
    assert len(calls) == 3, "随机模式应为每局取一个新种子"
    assert len(set(calls)) > 1, "取到的种子全部相同，未真正随机"


def test_random_mode_uses_random_regions():
    """边界：随机模式应在三个副本间取样，而非固定轮换"""
    import random as _r
    picked = []
    orig = bl.play

    def spy(starter, learn, region, seed=None, battles=7):
        picked.append(region)
        return {"cleared": 0, "won": False}

    bl.play = spy
    try:
        bl.fitness("杀伐", ["庇护"], 30, gen=1, random_seeds=True, rng=_r.Random(3))
    finally:
        bl.play = orig
    assert set(picked).issubset(set(bl.REGIONS))
    assert len(set(picked)) > 1, "随机模式下副本没有变化"
