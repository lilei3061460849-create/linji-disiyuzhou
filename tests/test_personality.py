"""角色性格特征（Personality Traits）系统测试。

覆盖：不预设人格 / 行为成性格 / 置信度升降 / 实例隔离 / 单次异常不贴死标签 /
死亡自动清除 / 死后不可读 / 事务回滚 / 存档往返 / 汇总格式与入参校验。
"""
import copy
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Entity, GameState
from engine.personality import (
    TRAIT_DIMENSIONS,
    format_personality_for_ai,
    record_behavior,
    remove_personality,
)


def _engine(tmp_path, suffix="personality"):
    engine = GameEngine(
        db_path=str(tmp_path / f"{suffix}.db"),
        rng_seed=7,
        save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / f"{suffix}_sealed.json"),
        death_book_path=str(tmp_path / f"{suffix}_book.md"),
    )
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    finish_initial_daowen(engine)
    return engine


def _friend(name="顾衡"):
    return Entity(name=name, entity_type="朋友", blood_limit=40,
                  current_hp=40, attack_count=2, attack_power=4)


# ---------------------------------------------------------------------------
# 1. 不预设人格
# ---------------------------------------------------------------------------
def test_new_character_has_no_preset_personality(tmp_path):
    engine = _engine(tmp_path)
    # 创建后没有任何预设性格：状态为空，接口返回空 traits（而不是报错或编造）
    assert engine.state.personality_traits == {}
    result = engine.get_personality(engine.state.player)
    assert result["success"] is True
    assert result["traits"] == {}
    # AI 状态导出也不含编造数据
    exported = engine.state.to_dict()["personality_traits"]
    assert exported == {}
    summary = engine.format_personality_for_ai(engine.state.player)
    assert summary["success"] is True
    assert "尚未从行为中形成" in summary["summary"] or "尚无维度形成判断" in summary["summary"]


# ---------------------------------------------------------------------------
# 2. 行为形成性格
# ---------------------------------------------------------------------------
def test_behavior_forms_trait(tmp_path):
    engine = _engine(tmp_path)
    result = engine.update_personality(
        engine.state.player, "risk_preference", +1, evidence="第1场主动选择以血限支付代价的死斗")
    assert result["success"] is True
    entry = result["updated"]
    assert entry["trait"] == "risk_preference"
    assert entry["evidence_count"] == 1
    assert entry["last_evidence"] == "第1场主动选择以血限支付代价的死斗"
    assert entry["confidence"] <= 0.30  # 单条证据置信度封顶
    # 结构化数据落在实例键上
    rid = engine.state.player.runtime_id
    assert engine.state.personality_traits[rid]["traits"]["risk_preference"] == entry
    # AI 汇总标注"初步"
    summary = engine.format_personality_for_ai(engine.state.player)["summary"]
    assert "初步" in summary and "置信度" in summary


def test_trait_only_from_nine_dimensions(tmp_path):
    engine = _engine(tmp_path)
    for dim in TRAIT_DIMENSIONS:
        res = engine.update_personality(engine.state.player, dim, +1, evidence=f"{dim}行为")
        assert res["success"] is True, dim
    bad = engine.update_personality(engine.state.player, "mbti_extraversion", +1, evidence="x")
    assert bad["success"] is False


def test_update_personality_validates_input(tmp_path):
    engine = _engine(tmp_path)
    assert engine.update_personality(engine.state.player, "risk_preference", 0, evidence="x")["success"] is False
    assert engine.update_personality(engine.state.player, "risk_preference", +1, evidence="  ")["success"] is False
    assert engine.update_personality(engine.state.player, "risk_preference", +1, evidence="x",
                                     weight=1.5)["success"] is False
    assert engine.update_personality("不存在的角色", "risk_preference", +1, evidence="x")["success"] is False


# ---------------------------------------------------------------------------
# 3. 多次行为升降置信度
# ---------------------------------------------------------------------------
def test_repeated_evidence_raises_confidence(tmp_path):
    engine = _engine(tmp_path)
    confidences = []
    for i in range(5):
        entry = engine.update_personality(
            engine.state.player, "exploration_desire", +1,
            evidence=f"第{i + 1}次主动选择【探索】而非跳过")["updated"]
        confidences.append(entry["confidence"])
    assert confidences == sorted(confidences), "同向证据应单调提升置信度"
    assert confidences[-1] == 0.82   # 0.30 + 4*0.13
    assert confidences[-1] <= 0.95   # 有上限
    # 强度也要多次一致行为才能到"明显"档
    traits = engine.state.personality_traits[engine.state.player.runtime_id]["traits"]
    assert traits["exploration_desire"]["score"] >= 0.60


def test_contrary_evidence_lowers_confidence(tmp_path):
    engine = _engine(tmp_path)
    for i in range(3):
        engine.update_personality(engine.state.player, "resource_view", +1,
                                  evidence=f"第{i + 1}次把碎片留给队友")
    before = engine.get_personality(engine.state.player)["traits"]["resource_view"]
    after = engine.update_personality(
        engine.state.player, "resource_view", -1,
        evidence="为凑齐法器独吞了全队碎片")["updated"]
    assert after["confidence"] < before["confidence"]
    assert after["evidence_count"] == before["evidence_count"] + 1


# ---------------------------------------------------------------------------
# 4. 实例隔离（同名不共享）
# ---------------------------------------------------------------------------
def test_same_name_instances_do_not_share(tmp_path):
    engine = _engine(tmp_path)
    a, b = _friend("独眼"), _friend("独眼")   # 同名不同实例
    engine.state.friends.extend([a, b])
    assert a.runtime_id != b.runtime_id
    engine.update_personality(a, "moral_baseline", +1, evidence="拒绝了牺牲队友换碎石的提议")
    pa = engine.get_personality(a)
    pb = engine.get_personality(b)
    assert "moral_baseline" in pa["traits"]
    assert pb["traits"] == {}, "同名另一实例不得被污染"
    assert set(engine.state.personality_traits) == {a.runtime_id}
    # 用实例引用读，不按名字混读
    assert engine.get_personality(a)["runtime_id"] == a.runtime_id


# ---------------------------------------------------------------------------
# 5. 单次异常行为不贴死标签
# ---------------------------------------------------------------------------
def test_single_outlier_does_not_lock_personality(tmp_path):
    engine = _engine(tmp_path)
    for i in range(3):
        engine.update_personality(engine.state.player, "moral_baseline", +1,
                                  evidence=f"第{i + 1}次优先掩护陌生人撤退")
    settled = engine.get_personality(engine.state.player)["traits"]["moral_baseline"]
    assert settled["value"] == "守义" and settled["confidence"] >= 0.5

    outlier = engine.update_personality(
        engine.state.player, "moral_baseline", -1,
        evidence="唯一一次为脱身谎报了队友位置")["updated"]
    # 一次反向行为：倾向只是部分移动，不被翻转成"利己"，也不被锁死
    assert 0 < outlier["score"] < settled["score"]
    assert "利己" not in outlier["value"], "单次异常行为不得把已形成的判断翻转到反向标签"
    # 继续正向行为可恢复：性格是活的，不是一次定终身
    restored = engine.update_personality(
        engine.state.player, "moral_baseline", +1, evidence="再次冒险送药给受伤的陌生旅人")["updated"]
    assert restored["score"] > outlier["score"]
    assert restored["confidence"] > outlier["confidence"]


def test_single_first_action_never_strong_label(tmp_path):
    engine = _engine(tmp_path)
    entry = engine.update_personality(
        engine.state.player, "risk_preference", -1, evidence="开局第一次交战就回避")["updated"]
    assert abs(entry["score"]) < 0.60  # 单次行为永远到不了"明显"档
    assert entry["confidence"] == 0.30


# ---------------------------------------------------------------------------
# 6. 死亡自动清除（接入统一死亡管线）
# ---------------------------------------------------------------------------
def test_death_removes_traits_via_unified_pipeline(tmp_path):
    state = GameState()
    combat = CombatEngine(state, DiceEngine(seed=1))
    monster = Entity(name="血瞳", entity_type="怪物", blood_limit=30, current_hp=30,
                     attack_count=1, attack_power=5)
    state.enemies.append(monster)
    record_behavior(state, monster, "risk_preference", +1, evidence="开场即压上全部速度抢攻")

    rid = monster.runtime_id
    assert rid in state.personality_traits

    detail = monster.take_damage(30, "普通")
    assert detail["died"] is True
    assert combat._check_hp_zero_death(monster) is True   # 统一死亡入口
    assert rid not in state.personality_traits, "命零必须自动清除该实例的性格数据"
    # 幂等：重复通知不炸、不误删别人
    assert combat._check_hp_zero_death(monster) is False
    assert state.personality_traits == {}


def test_gameengine_death_flow_clears_personality(tmp_path):
    engine = _engine(tmp_path, suffix="p_death")
    friend = _friend("梁九")
    engine.state.friends.append(friend)
    engine.update_personality(friend, "interpersonal_tendency", +1, evidence="把最后的干粮分给陌生人")
    # 存活的其他角色不受影响
    engine.update_personality(engine.state.player, "resource_view", +1, evidence="全程只捡必需品")

    friend.take_damage(99, "代价")
    assert engine.combat._check_hp_zero_death(friend) is True
    assert friend.runtime_id not in engine.state.personality_traits
    assert engine.state.player.runtime_id in engine.state.personality_traits, "不得波及其他角色"


# ---------------------------------------------------------------------------
# 7. 死亡角色对 AI 不可读
# ---------------------------------------------------------------------------
def test_dead_character_unreachable_by_ai(tmp_path):
    engine = _engine(tmp_path, suffix="p_ai")
    friend = _friend("白原")
    engine.state.friends.append(friend)
    engine.update_personality(friend, "expression_style", +1, evidence="当众直言顶撞了执法者")
    engine.format_personality_for_ai(friend)  # 活着时可读

    friend.take_damage(99, "代价")
    engine.combat._check_hp_zero_death(friend)

    for ref in (friend, friend.runtime_id, "白原"):
        assert engine.get_personality(ref) == {"success": False,
                                               "error": "角色不存在或已命零，无性格数据"}
        assert engine.format_personality_for_ai(ref)["success"] is False
        assert engine.update_personality(ref, "risk_preference", +1, evidence="x")["success"] is False
    # 状态导出（AI 上下文 state_snapshot）里也没有死者
    assert friend.runtime_id not in engine.state.to_dict()["personality_traits"]


# ---------------------------------------------------------------------------
# 8. 事务快照 / 回滚
# ---------------------------------------------------------------------------
def test_transaction_rollback_restores_personality(tmp_path):
    engine = _engine(tmp_path, suffix="p_rollback")
    engine.update_personality(engine.state.player, "decision_habit", +1, evidence="遭遇伏击先观察一轮")
    # 事务内又写入两条，形成快照；快照后再写入一条，随后回滚到快照
    engine.update_personality(engine.state.player, "decision_habit", +1, evidence="第二次仍先观察")
    engine.update_personality(engine.state.player, "emotional_stability", -1, evidence="同伴倒下时失态")
    snapshot = copy.deepcopy(engine.state)
    at_snapshot = copy.deepcopy(engine.state.personality_traits)
    engine.update_personality(engine.state.player, "risk_preference", +1, evidence="回滚前最后一条")
    assert engine.state.personality_traits != at_snapshot
    engine._restore_state_in_place(snapshot)
    assert engine.state.personality_traits == at_snapshot, "回滚必须恢复对应时刻的性格状态"
    # 回滚后状态自洽：实体引用仍有效，可继续正常累积
    again = engine.update_personality(engine.state.player, "risk_preference", -1, evidence="回滚后趋于求稳")
    assert again["success"] is True and again["updated"]["evidence_count"] == 1


def test_failed_action_leaves_personality_untouched(tmp_path):
    engine = _engine(tmp_path, suffix="p_tx")
    engine.update_personality(engine.state.player, "resource_view", +1, evidence="只捡必需品")
    before = copy.deepcopy(engine.state.personality_traits)
    # 非法行动：校验失败不改变游戏状态（原子契约）
    result = engine.execute_action("use_daowen", {"daowen_name": "不存在的道纹", "target": "谁", "x_value": 1})
    assert result["success"] is False
    assert engine.state.personality_traits == before


# ---------------------------------------------------------------------------
# 9. 存档往返
# ---------------------------------------------------------------------------
def test_save_load_preserves_personality(tmp_path):
    engine = _engine(tmp_path, suffix="p_save")
    player = engine.state.player
    friend = _friend("宋昭")
    engine.state.friends.append(friend)
    for i in range(3):
        engine.update_personality(player, "risk_preference", +1, evidence=f"第{i + 1}次主动接下死斗")
    engine.update_personality(friend, "moral_baseline", +1, evidence="替陌生人挡下了攻击")
    before = copy.deepcopy(engine.state.personality_traits)

    assert engine.save_game(slot="p1")["success"] is True
    # 存档后破坏现场，再读档
    engine.state.personality_traits.clear()
    engine.state.friends.remove(friend)
    loaded = engine.load_game(slot="p1")
    assert loaded["success"] is True
    assert engine.state.personality_traits == before, "读档必须完整恢复存活角色性格"
    assert engine.get_personality(player)["traits"]["risk_preference"] == before[player.runtime_id]["traits"]["risk_preference"]
    # 引擎战斗对象仍共享同一 state
    assert engine.combat.state is engine.state


# ---------------------------------------------------------------------------
# 10. AI 汇总格式 + 结构化数据并存
# ---------------------------------------------------------------------------
def test_ai_summary_format_and_structured_data(tmp_path):
    engine = _engine(tmp_path, suffix="p_fmt")
    player = engine.state.player
    for i in range(5):
        engine.update_personality(player, "decision_habit", +1, evidence=f"第{i + 1}次遇袭先观察再出手")
    summary = engine.format_personality_for_ai(player)["summary"]
    assert summary.startswith(f"角色：{player.name}")
    assert "决策习惯：先观察后行动" in summary
    assert "置信度 0.82" in summary and "依据 5 次" in summary
    assert "不是强制规则" in summary, "必须提醒 AI：性格是倾向不是强制"
    assert "证据不足" in summary, "未形成判断的维度要显式列出，防止 AI 脑补"
    # 自然语言之外，结构化数据仍在
    stored = engine.state.personality_traits[player.runtime_id]["traits"]["decision_habit"]
    assert set(stored) >= {"trait", "value", "score", "strength", "confidence",
                           "evidence_count", "last_evidence"}


def test_remove_personality_manual_and_idempotent(tmp_path):
    engine = _engine(tmp_path, suffix="p_rm")
    friend = _friend("阿羽")
    engine.state.friends.append(friend)
    engine.update_personality(friend, "exploration_desire", +1, evidence="主动进未探过的巷子")
    assert engine.remove_personality(friend) == {"success": True, "removed": True}
    assert engine.get_personality(friend)["traits"] == {}
    assert engine.remove_personality(friend)["removed"] is False  # 幂等
    assert remove_personality(engine.state, "阿羽") is False
