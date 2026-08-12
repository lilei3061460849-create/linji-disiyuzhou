"""《死者之书》三段式遗言的数据结构与输入校验。"""
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import GameState


def _combat():
    return CombatEngine(GameState(), DiceEngine(seed=1))


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
