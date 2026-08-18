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

from tests.setup_support import finish_initial_daowen
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load():
    spec = importlib.util.spec_from_file_location(
        "bl", os.path.join(ROOT, "sim", "build_learner.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bl = _load()


def _finish_region_setup(engine, region):
    result = engine.execute_action("setup_choose_region", {"region": region})
    optional = {"折速法印", "三相残韵盘"}
    choice = next((n for n in result["result"]["relic_choices"] if n not in optional),
                  result["result"]["relic_choices"][0])
    engine.execute_action("choose_discovered_relic", {"relic_name": choice})
    return choice


def _start_battle(engine, relic):
    engine.state.energy = 0
    choices = {relic: {"use": False}} if relic in {
        "折速法印", "三相残韵盘"} else {}
    return engine.execute_action("battle_start", {"relic_choices": choices})


# ---------- 正常路径 ----------

def test_starters_are_shafa_loop_not_forced_shaifa():
    """正常：可偏好的起手是整个杀伐闭环，不再写死只开杀伐。"""
    from engine.gamedata import SHAFA_LOOP_DAOWEN
    assert bl.STARTERS == list(SHAFA_LOOP_DAOWEN)
    assert "冲击" in bl.STARTERS and "再生" in bl.STARTERS


def test_play_does_not_inject_shaifa_when_not_discovered():
    """边界：发现列表没有杀伐时，模拟不得把杀伐塞进玩家。"""
    from engine.api import GameEngine
    from tests.setup_support import choose_discovered_initial_daowen

    found = None
    for seed in range(1, 80):
        engine = GameEngine(db_path=f"/tmp/disc{seed}.db", rng_seed=seed)
        engine.execute_action("setup_attributes", {
            "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7,
        })
        offered = list(engine.state.pending_initial_daowen_choices)
        if "杀伐" not in offered:
            found = (engine, offered)
            break
    assert found, "80 个种子里应能抽到不含杀伐的发现"
    engine, offered = found
    chosen = choose_discovered_initial_daowen(engine, prefer="杀伐")
    assert chosen["success"]
    assert chosen["picked"] in offered
    assert chosen["picked"] != "杀伐"
    assert "杀伐" not in engine.state.player.dao_wen


def test_choose_discovered_honors_prefer_only_when_offered():
    """正常：prefer 在候选中才选它。"""
    from engine.api import GameEngine
    from tests.setup_support import choose_discovered_initial_daowen

    engine = GameEngine(db_path="/tmp/disc_pref.db", rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    offered = list(engine.state.pending_initial_daowen_choices)
    prefer = offered[1]
    chosen = choose_discovered_initial_daowen(engine, prefer=prefer)
    assert chosen["picked"] == prefer
    assert list(engine.state.player.dao_wen) == [prefer]


def test_choose_discovered_rejects_missing_pending():
    """错误输入：没有待选发现时必须拒绝，不能凭空发道纹。"""
    from engine.api import GameEngine
    from tests.setup_support import choose_discovered_initial_daowen

    engine = GameEngine(db_path="/tmp/disc_bad.db", rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    engine.execute_action("setup_choose_initial_daowen", {
        "daowen_name": engine.state.pending_initial_daowen_choices[0],
    })
    bad = choose_discovered_initial_daowen(engine, prefer="杀伐")
    assert not bad["success"]
    assert "没有待选择" in bad["error"]


def test_play_returns_wellformed_result():
    """正常路径：跑一局应返回通关场数与胜负"""
    r = bl.play("杀伐", ["庇护", "再生"], "龙心谷", seed=1)
    assert {"cleared", "won", "invalid"} <= set(r)
    assert 0 <= r["cleared"] <= 7
    assert isinstance(r["won"], bool)


def test_pending_event_requiring_dm_stops_instead_of_looping(tmp_path):
    """边界：模拟器遇到不能代裁的事件时应明确作废，而不是反复结算同一事件。"""
    from engine.api import GameEngine

    engine = GameEngine(
        db_path=str(tmp_path / "r.db"), save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        death_book_path=str(tmp_path / "death.md"), rng_seed=1,
    )
    engine.execute_action("setup_attributes", {
        "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    engine.execute_action("choose_discovered_relic", {
        "relic_name": setup["result"]["relic_choices"][0],
    })
    creative = next(option for option in engine.event_pool.events["过路商人"]["options"]
                    if option["text"].startswith("限制选择权"))
    engine.event_pool.events["过路商人"]["options"] = [creative]
    engine.event_pool.current = "过路商人"

    result = bl._resolve_pending_event(engine)

    assert not result["success"] and "需要DM裁定" in result["error"]
    assert engine.event_pool.current == "过路商人"


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
    import random as _r
    a = bl.play("杀伐", ["庇护", "再生"], "龙心谷", 42, rng=_r.Random(9))
    b = bl.play("杀伐", ["庇护", "再生"], "龙心谷", 42, rng=_r.Random(9))
    assert a == b, "同一固定种子+同一决策rng，两次结果必须一致"


def test_fitness_fixed_mode_is_deterministic():
    """边界：非随机模式下同参数 fitness 必须一致"""
    f1, v1, _ = bl.fitness("杀伐", ["庇护", "再生"], 3, gen=1)
    f2, v2, _ = bl.fitness("杀伐", ["庇护", "再生"], 3, gen=1)
    assert (f1, v1) == (f2, v2)


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
    assert len(calls) >= 3, "随机模式应为每局取一个新种子"
    assert len(set(calls)) > 1, "取到的种子全部相同，未真正随机"


def test_random_mode_uses_random_regions():
    """边界：随机模式应在三个副本间取样，而非固定轮换"""
    import random as _r
    picked = []
    orig = bl.play

    def spy(starter, learn, region, seed=None, battles=7, rng=None, telemetry=None, spend_shards=False):
        picked.append(region)
        return {"cleared": 0, "won": False, "invalid": False}

    bl.play = spy
    try:
        bl.fitness("杀伐", ["庇护"], 30, gen=1, random_seeds=True, rng=_r.Random(3))
    finally:
        bl.play = orig
    assert set(picked).issubset(set(bl.REGIONS))
    assert len(set(picked)) > 1, "随机模式下副本没有变化"


# ---------- 局外行动遥测与无效数据剔除 ----------

def test_choose_pre_battle_respects_region_exclusive():
    """错误输入检出：副本专属行动不得在其他副本被选中"""
    import random as _r
    from engine.api import GameEngine
    e = GameEngine(db_path="/tmp/tele.db", rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": "龙心谷"})
    picked = {bl.choose_pre_battle(e, [], 1, _r.Random(i), bl.DEFAULT_POLICY)[0]
              for i in range(200)}
    assert "维修" not in picked, "扭曲都市专属的【维修】不该在龙心谷被选中"
    assert "雇佣" not in picked, "罪孽都市专属的【雇佣】不该在龙心谷被选中"


def test_region_exclusive_enforced_by_engine():
    """正常路径：引擎必须拒绝跨副本使用专属行动"""
    from engine.api import GameEngine
    e = GameEngine(db_path="/tmp/tele2.db", rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    _finish_region_setup(e, "龙心谷")
    r = e.execute_action("pre_battle_action", {"sub_action": "维修", "tier": 1})
    assert not r["success"]
    assert "扭曲都市" in r["error"]
    # 被拒绝时精力必须退还，否则会白白损失一次行动
    assert e.state.energy == 3


def test_telemetry_records_action_choices():
    """正常路径：遥测必须记录每次局外行动的尝试与成功"""
    tele = {}
    bl.play("杀伐", ["庇护"], "龙心谷", seed=5, telemetry=tele)
    assert tele.get("attempted"), "未记录任何行动选择"
    assert sum(tele["attempted"].values()) > 0
    for act in tele.get("succeeded", {}):
        assert act in tele["attempted"], "成功数不应超出尝试范围"


def test_invalid_runs_excluded_from_fitness(monkeypatch):
    """
    错误输入：出现引擎异常的对局必须被判为无效并剔除，不得污染分数。
    """
    def fake_play(starter, learn, region, seed=None, battles=7, rng=None, telemetry=None, spend_shards=False):
        return {"cleared": 0, "won": False, "invalid": True, "reason": "boom"}

    monkeypatch.setattr(bl, "play", fake_play)
    tele = {}
    score, valid, invalid = bl.fitness("杀伐", ["庇护"], 4, gen=1, telemetry=tele)
    assert valid == 0 and invalid == 4
    assert score == 0.0
    assert tele["invalid_reasons"], "无效原因必须被记录以便定位bug"


def test_valid_and_invalid_are_separated(monkeypatch):
    """边界：有效局与无效局混合时，分数只由有效局决定"""
    calls = {"n": 0}

    def mixed(starter, learn, region, seed=None, battles=7, rng=None, telemetry=None, spend_shards=False):
        calls["n"] += 1
        if calls["n"] % 2:
            return {"cleared": 7, "won": True, "invalid": False}
        return {"cleared": 0, "won": False, "invalid": True, "reason": "bug"}

    monkeypatch.setattr(bl, "play", mixed)
    score, valid, invalid = bl.fitness("杀伐", ["庇护"], 4, gen=1)
    assert valid == 2 and invalid == 2
    assert score == 10.0, "有效局全胜时分数应为满分，不应被无效局拉低"


# ---------- 冷却代价（回归：束缚等曾可无限刷）----------

def test_cooldown_cost_is_applied():
    """
    正常路径：代价为【冷却X】的道纹发动后必须写入 cooldown_remaining。
    此前从未写入，导致 固执/束缚/畸变/迟滞 可在同场无限重复发动。
    """
    from engine.api import GameEngine
    e = GameEngine(db_path="/tmp/cdtest.db", rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    relic = _finish_region_setup(e, "龙心谷")
    e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "束缚"})
    _start_battle(e, relic)
    e.execute_action("round_start", {})
    m = e.state.enemies[0]
    r1 = e.execute_action("use_daowen", {"daowen_name": "束缚", "x": 2, "target": m.name})
    assert r1["success"]
    assert e.state.player.dao_wen["束缚"].cooldown_remaining == 4, "冷却2X未写入"


def test_cooldown_blocks_reuse_in_same_battle():
    """边界：冷却中的道纹不得再次发动（这正是束缚曾经支配全局的原因）"""
    from engine.api import GameEngine
    e = GameEngine(db_path="/tmp/cdtest2.db", rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    relic = _finish_region_setup(e, "龙心谷")
    e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "束缚"})
    _start_battle(e, relic)
    e.execute_action("round_start", {})
    m = e.state.enemies[0]
    e.execute_action("use_daowen", {"daowen_name": "束缚", "x": 2, "target": m.name})
    r2 = e.execute_action("use_daowen", {"daowen_name": "束缚", "x": 2, "target": m.name})
    assert not r2["success"], "冷却中的道纹被重复发动"
    assert "冷却" in r2["error"] or "不可用" in r2["error"]


def test_cooldown_decrements_at_battle_end():
    """边界：README 规定[战终]后冷却-1，否则道纹将永久锁死"""
    from engine.api import GameEngine
    from engine.models import DaoWen, DaoWenInstance
    e = GameEngine(db_path="/tmp/cdtest3.db", rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    _finish_region_setup(e, "龙心谷")
    inst = DaoWenInstance(DaoWen(name="束缚", formula="", cost_type="代价",
                                 cost_formula="", effect_formula=""))
    inst.cooldown_remaining = 3
    e.state.player.dao_wen["束缚"] = inst
    e.state.phase = "in_combat"
    e.execute_action("battle_end", {})
    assert inst.cooldown_remaining == 2, "[战终]后冷却未递减"


def test_all_cooldown_daowen_covered():
    """错误输入检出：所有 cost_type=冷却 的道纹都应受同一机制约束"""
    from engine.daowen import DaoWenEngine
    from engine.models import Entity
    DaoWenEngine.register_all()
    t = Entity(name="x", entity_type="怪物", blood_limit=100, current_hp=100,
               attack_count=2, attack_power=5, speed_limit=6)
    found = []
    for n in DaoWenEngine._registry:
        try:
            r = DaoWenEngine.resolve(n, 2, target=t, caster=t)
        except Exception:
            continue
        if r and r.get("cost_type") == "冷却":
            found.append(n)
            assert r.get("cost", 0) > 0, f"{n} 冷却代价为0，等于没有代价"
    assert found, "未找到任何冷却型道纹，检测逻辑可能失效"
