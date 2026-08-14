"""AI_EXPERIENCE.md 工程准则与知识库清废契约。"""
from pathlib import Path
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.document_validation import validate_ai_knowledge_base

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "AI_EXPERIENCE.md"


def test_ai_knowledge_base_contains_complete_engineering_policy():
    """正常路径：现行知识库包含12条准则、7段交付结构，且无废案标题。"""
    result = validate_ai_knowledge_base(KNOWLEDGE)
    assert result == {"rules": 12, "delivery_sections": 7, "stale_headings": 0}


def test_delivery_sections_with_all_items_but_wrong_order_are_rejected(tmp_path):
    """边界条件：7段一个不少但顺序互换，校验器仍必须拒绝。"""
    text = KNOWLEDGE.read_text(encoding="utf-8")
    original = "   - (5) 交付\n   - (6) 测试"
    assert original in text
    invalid = text.replace(original, "   - (6) 测试\n   - (5) 交付", 1)
    path = tmp_path / "AI_EXPERIENCE.md"
    path.write_text(invalid, encoding="utf-8")

    with pytest.raises(ValueError, match="固定交付结构必须完整且顺序"):
        validate_ai_knowledge_base(path)


def test_missing_mapping_and_resolved_history_heading_are_rejected(tmp_path):
    """错误输入/非法配置：缺少文件映射并重新加入已解决流水账时必须拒绝。"""
    text = KNOWLEDGE.read_text(encoding="utf-8")
    text = text.replace(
        "> 用户提到的“AI 知识库”指仓库根目录的 `AI_EXPERIENCE.md`。\n",
        "",
        1,
    )
    text += "\n## 已解决修复追记\n\n这里不应重新归档。\n"
    path = tmp_path / "AI_EXPERIENCE.md"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError) as exc:
        validate_ai_knowledge_base(path)
    message = str(exc.value)
    assert "缺少 AI 知识库到 AI_EXPERIENCE.md 的明确映射" in message
    assert "知识库仍含过期/已解决流水账标题" in message
