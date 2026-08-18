"""《死者之书》三段式遗言：校验、命零中断、审核落盘、回音长廊改文件。"""
import os
import sys
from pathlib import Path

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.death_book import (
    CAUSE_DRAFTS,
    DeathBookStore,
    draft_legacy,
    parse_legacies,
    validate_legacy,
)
from engine.dice import DiceEngine
from engine.models import Consumable, Entity, GameState


def _empty_book(path: Path) -> Path:
    path.write_text("# 死者之书\n\n## 可学法术\n\n### 先发制人\n\n"
                    "所需道纹：杀伐\n\n触发条件：受到伤害前\n\n"
                    "生效流程：受到伤害前→发动杀伐 X\n\n## 遗言\n\n当前没有遗言。\n",
                    encoding="utf-8")
    return path


def _combat():
    return CombatEngine(GameState(), DiceEngine(seed=1))


def finish_round(engine):
    engine.state.combat_subphase = "await_round_end"
    return engine.execute_action("round_end", {})


def _engine(tmp_path, suffix="legacy"):
    book = _empty_book(tmp_path / f"死者之书_{suffix}.md")
    engine = GameEngine(
        db_path=str(tmp_path / f"{suffix}.db"),
        rng_seed=1,
        sealed_candidate_path=str(tmp_path / f"{suffix}_sealed.json"),
        death_book_path=str(book),
    )
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    engine.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    engine.state.phase = "in_combat"
    return engine, book


def _current_event(engine, name="回音长廊"):
    engine.state.phase = "pre_battle"
    engine.event_pool.current = name


def test_three_part_legacy_normal_path_is_recorded_without_mutation():
    """正常路径：三个字段完整保存，不混入系统记录列表。"""
    combat = _combat()
    legacy = {
        "trigger_point": "法力归零后受到致死攻击",
        "fork": "放弃格挡并继续输出",
        "cost_budget": "愿以碎片换取法力",
    }
    result = combat.trigger_death_legacy(legacy)
    assert result["legacy"] == legacy
    assert combat.state.death_book_legacies == [legacy]
    assert combat.state.death_book_wisdom == []


def test_each_legacy_field_accepts_exactly_twenty_characters():
    """边界：每段恰好20字合法，且不被静默截断。"""
    combat = _combat()
    legacy = {
        "trigger_point": "触" * 20,
        "fork": "岔" * 20,
        "cost_budget": "代" * 20,
    }
    result = combat.trigger_death_legacy(legacy)
    assert result["legacy"] == legacy
    assert all(len(value) == 20 for value in result["legacy"].values())


def test_legacy_rejects_old_string_missing_fields_and_overlong_values():
    """错误输入：旧字符串、缺字段和超长字段均由校验器拒绝。"""
    combat = _combat()
    invalid_values = [
        "旧格式遗言",
        {"trigger_point": "触发", "fork": "岔路"},
        {"trigger_point": "触" * 21, "fork": "岔路", "cost_budget": "代价"},
    ]
    for invalid in invalid_values:
        try:
            combat.trigger_death_legacy(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"非法遗言未被拒绝：{invalid!r}")
    assert combat.state.death_book_legacies == []


def test_player_mingling_queues_interrupt_and_approve_writes_file(tmp_path):
    """正常路径：轮回者命零 → 中断带草稿 → 审核通过 → 只改遗言节。"""
    engine, book = _engine(tmp_path, "approve")
    engine.state.player.current_hp = 1
    engine.state.enemies.clear()
    engine.state.enemies.append(Entity(
        name="打手", entity_type="怪物", blood_limit=80, current_hp=80,
        attack_count=1, attack_power=20))
    prepared = engine.execute_action("prepare_monster_phase", {})
    r = engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"],
        "choices": [{"actor_ref": "enemy:0", "daowen": None,
                     "attack_actions": [{"hits": [{"target_ref": "player:0", "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}]}]}],
    })
    assert r["result"]["player_dead"] is True
    assert r.get("interrupt", {}).get("interrupt_type") == "死之传承"
    draft = r["interrupt"]["context"]["draft"]
    assert set(draft) >= {"trigger_point", "fork", "cost_budget"}
    assert all(1 <= len(draft[k]) <= 20 for k in ("trigger_point", "fork", "cost_budget"))

    spells_before = book.read_text(encoding="utf-8").split("## 遗言")[0]
    approved = engine.submit_ruling(
        "死之传承", "通过", {"action": "approve"})
    assert approved["success"] is True
    assert approved["death_book"]["written"] is True
    assert engine.state.player is None
    text = book.read_text(encoding="utf-8")
    assert text.split("## 遗言")[0] == spells_before
    assert "所需道纹：杀伐" in text
    assert draft["trigger_point"] in text
    assert "当前没有遗言" not in text
    assert engine.state.death_book_legacies[0]["trigger_point"] == draft["trigger_point"]


def test_two_config_examples_append_without_code_change(tmp_path):
    """可自定义：只改文件就能追加两种完全不同的遗言页。"""
    book = _empty_book(tmp_path / "死者之书_examples.md")
    store = DeathBookStore(book)
    first = store.append(CAUSE_DRAFTS["attack"])
    second = store.append(CAUSE_DRAFTS["collapse"])
    loaded = store.load()
    assert loaded[0]["trigger_point"] == first["trigger_point"] == "受到致死攻击命零"
    assert loaded[1]["trigger_point"] == second["trigger_point"] == "异变叠满崩解命零"
    assert loaded[0]["cost_budget"] != loaded[1]["cost_budget"]
    parsed = parse_legacies(book.read_text(encoding="utf-8"))
    assert [item["fork"] for item in parsed] == [first["fork"], second["fork"]]


def test_boundary_twenty_char_edit_is_written_verbatim(tmp_path):
    """边界：审核修改恰好20字，文件原文不被截断。"""
    engine, book = _engine(tmp_path, "edit20")
    engine.state.player.current_hp = 0
    engine.state.player.is_alive = False
    engine.state.last_death_cause = "attack"
    blocked = engine.execute_action("round_start", {})
    assert blocked["success"] is False
    assert blocked.get("interrupt", {}).get("interrupt_type") == "死之传承"
    legacy = {"trigger_point": "触" * 20, "fork": "岔" * 20, "cost_budget": "代" * 20}
    r = engine.submit_ruling("死之传承", "改", {"action": "edit", **legacy})
    assert r["success"] is True
    written = r["death_book"]["legacy"]
    assert written == legacy
    text = book.read_text(encoding="utf-8")
    assert "触" * 20 in text and "代" * 20 in text


def test_reject_does_not_write_and_invalid_edit_keeps_interrupt(tmp_path):
    """错误输入：超长修改被拒绝且中断仍在；驳回则不写文件。"""
    engine, book = _engine(tmp_path, "reject")
    engine.state.player.current_hp = 0
    engine.state.player.is_alive = False
    engine.state.last_death_cause = "collapse"
    finish_round(engine)
    assert engine._pending_interrupts

    bad = engine.submit_ruling("死之传承", "改", {
        "action": "edit",
        "trigger_point": "触" * 21,
        "fork": "岔路",
        "cost_budget": "代价",
    })
    assert bad["success"] is False
    assert "超过" in bad["error"]
    assert engine._pending_interrupts, "非法修改不得吞掉中断"

    empty_before = book.read_text(encoding="utf-8")
    rejected = engine.submit_ruling("死之传承", "驳回", {"action": "reject"})
    assert rejected["success"] is True
    assert rejected["death_book"]["written"] is False
    assert engine.state.player is None
    assert book.read_text(encoding="utf-8") == empty_before
    assert "当前没有遗言" in empty_before


def test_collapse_and_mediocrity_both_trigger_inheritance(tmp_path):
    """正常路径：崩解与凡庸导致的轮回者命零都触发死之传承。"""
    engine, _ = _engine(tmp_path, "causes")
    T = Entity.MUTATION_COLLAPSE_THRESHOLD
    engine.state.player.mutation_count = T - 10
    engine.state.consumables.append(Consumable(
        name="残骸", effect="局内使用恢复20生命并获得异变10", current_uses=1, max_uses=1))
    r = engine.execute_action("consume_item", {"name": "残骸"})
    assert r["result"]["mutation"]["collapsed"] is True
    assert r["interrupt"]["interrupt_type"] == "死之传承"
    assert r["interrupt"]["context"]["cause"] == "collapse"
    engine.submit_ruling("死之传承", "驳回", {"action": "reject"})

    engine2, _ = _engine(tmp_path, "fan")
    p = engine2.state.player
    for _ in range(5):
        p.actions_used_this_round = 0
        p.damage_dealt_this_round = 0
        end = finish_round(engine2)
    assert not p.is_alive
    assert end.get("interrupt", {}).get("interrupt_type") == "死之传承"
    assert end["interrupt"]["context"]["cause"] == "mediocrity"


def test_echo_corridor_writes_and_clears_chosen_page(tmp_path):
    """正常路径：打碎镜子必须自选一页，清的是指定页不是最后一页。"""
    engine, book = _engine(tmp_path, "echo")
    _current_event(engine)
    store = DeathBookStore(book)
    store.append(CAUSE_DRAFTS["attack"])
    store.append(CAUSE_DRAFTS["collapse"])
    engine.state.death_book_legacies = store.load()
    hp_before = engine.state.player.current_hp

    r2 = engine.execute_action("resolve_event", {
        "event": "回音长廊", "option_id": 2, "legacy_index": 1,
    })
    assert r2["success"] is True
    remaining = store.load()
    assert len(remaining) == 1
    assert remaining[0]["trigger_point"] == CAUSE_DRAFTS["collapse"]["trigger_point"]
    assert engine.state.player.current_hp == hp_before - 5


def test_echo_corridor_clear_without_choice_is_rejected(tmp_path):
    """错误输入：有遗言却不指定要清哪一页，必须拒绝且不流血、不改文件。"""
    engine, book = _engine(tmp_path, "echo_err")
    _current_event(engine)
    DeathBookStore(book).append(CAUSE_DRAFTS["attack"])
    engine.state.death_book_legacies = DeathBookStore(book).load()
    hp_before = engine.state.player.current_hp
    text_before = book.read_text(encoding="utf-8")
    r = engine.execute_action("resolve_event", {"event": "回音长廊", "option_id": 2})
    assert r["success"] is False
    assert "自选" in r["error"]
    assert r.get("pages")
    assert engine.state.player.current_hp == hp_before
    assert book.read_text(encoding="utf-8") == text_before


def test_echo_corridor_invalid_index_is_rejected(tmp_path):
    """错误输入：越界页码拒绝。"""
    engine, book = _engine(tmp_path, "echo_oob")
    _current_event(engine)
    DeathBookStore(book).append(CAUSE_DRAFTS["attack"])
    r = engine.execute_action("resolve_event", {
        "event": "回音长廊", "option_id": 2, "legacy_index": 9,
    })
    assert r["success"] is False
    assert "不存在" in r["error"]


def test_echo_corridor_empty_book_bleeds_without_clearing(tmp_path):
    """边界：书空时打碎镜子只流血5，文件仍是空遗言。"""
    engine, book = _engine(tmp_path, "echo_empty")
    _current_event(engine)
    hp_before = engine.state.player.current_hp
    text_before = book.read_text(encoding="utf-8")
    r = engine.execute_action("resolve_event", {
        "event": "回音长廊", "option_id": 2, "legacy_index": 1,
    })
    assert r["success"] is True
    assert "无遗言可清除" in r["result"]["applied"]
    assert engine.state.player.current_hp == hp_before - 5
    assert book.read_text(encoding="utf-8") == text_before
    assert "当前没有遗言" in text_before


def test_echo_corridor_clears_by_title(tmp_path):
    """正常路径：也可用遗言标题自选要打碎的那一页。"""
    engine, book = _engine(tmp_path, "echo_title")
    _current_event(engine)
    store = DeathBookStore(book)
    store.append({**CAUSE_DRAFTS["attack"], "title": "第一页"})
    store.append({**CAUSE_DRAFTS["collapse"], "title": "第二页"})
    engine.state.death_book_legacies = store.load()
    r = engine.execute_action("resolve_event", {
        "event": "回音长廊", "option_id": 2, "legacy_title": "第一页",
    })
    assert r["success"] is True
    remaining = store.load()
    assert len(remaining) == 1
    assert remaining[0]["title"] == "第二页"


def test_player_cancer_mingling_triggers_inheritance(tmp_path):
    """正常路径：轮回者累计恢复达血限×2，癌变命零并触发死之传承。"""
    engine, _ = _engine(tmp_path, "cancer")
    p = engine.state.player
    need = engine.combat.cancer_threshold_of(p)
    p.total_healed = need
    end = finish_round(engine)
    assert not p.is_alive
    assert p.is_proliferated
    assert end.get("interrupt", {}).get("interrupt_type") == "死之传承"
    assert end["interrupt"]["context"]["cause"] == "cancer"


def test_player_cancer_boundary_just_below_threshold(tmp_path):
    """边界：差 1 点不到阈值不癌变。"""
    engine, _ = _engine(tmp_path, "cancer_edge")
    p = engine.state.player
    need = engine.combat.cancer_threshold_of(p)
    p.total_healed = need - 1
    finish_round(engine)
    assert p.is_alive
    assert not p.is_proliferated


def test_ally_cancer_kills_ally_not_player(tmp_path):
    """正常路径：朋友癌变只命零该朋友，不触发轮回者死之传承。"""
    engine, _ = _engine(tmp_path, "cancer_ally")
    friend = Entity(name="岩行者", entity_type="朋友", blood_limit=20, current_hp=20,
                    attack_count=1, attack_power=1)
    engine.state.friends.append(friend)
    friend.heal(engine.combat.cancer_threshold_of(friend))
    end = finish_round(engine)
    assert not friend.is_alive
    assert friend.is_proliferated
    assert engine.state.player.is_alive
    assert end.get("interrupt") is None


def test_new_engine_reloads_file_as_source_of_truth(tmp_path):
    """边界：新引擎实例只从文件装回遗言，不读上一局内存。"""
    book = _empty_book(tmp_path / "死者之书_reload.md")
    store = DeathBookStore(book)
    store.append(CAUSE_DRAFTS["attack"])
    engine = GameEngine(
        db_path=str(tmp_path / "reload.db"),
        death_book_path=str(book),
        sealed_candidate_path=str(tmp_path / "reload_sealed.json"),
    )
    assert len(engine.state.death_book_legacies) == 1
    assert engine.state.death_book_legacies[0]["fork"] == CAUSE_DRAFTS["attack"]["fork"]


def test_draft_legacy_never_exceeds_capacity():
    """错误/边界：草稿生成器在极端局号下仍不超过20字。"""
    state = GameState()
    state.current_battle = 999
    state.player = Entity(name="轮回者", entity_type="轮回者", current_speed=0, current_mana=0)
    draft = draft_legacy(state, "attack", {"action": "use_daowen", "params": {"daowen_name": "杀伐"}})
    assert all(len(draft[k]) <= 20 for k in ("trigger_point", "fork", "cost_budget"))
    validate_legacy(draft)
