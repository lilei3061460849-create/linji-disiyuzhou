"""AI守则与战斗推演格式完整性契约测试。"""
from pathlib import Path
import re
import pytest

from engine.document_validation import validate_markdown_documents

ROOT = Path(__file__).resolve().parents[1]
AI_POLICY_FILE = ROOT / "AI守则.md"
README_FILE = ROOT / "README.md"


# ---------- 正常路径 ----------

def test_ai_policy_file_exists_and_contains_all_core_sections():
    """正常路径：AI守则.md 存在，包含一级标题、职责划分、AI协作规范、推演铁律、流水线与六大场景示例。"""
    assert AI_POLICY_FILE.is_file(), "AI守则.md 文件必须存在"
    text = AI_POLICY_FILE.read_text(encoding="utf-8")

    assert text.startswith("# AI守则")
    assert "## 一、职责划分" in text
    assert "## 二、AI协作规范" in text
    assert "## 三、推演铁律" in text
    assert "## 四、战斗推演原子流水线与结算原则" in text
    assert "## 五、推演标准格式与典型示例" in text

    # 协作规范九条必须齐全
    for sub in [
        "1. 越权禁令与工具授权",
        "2. 事实源与同步",
        "3. 设计合规",
        "4. 代价与收益",
        "5. 效果撰写",
        "6. 命名",
        "7. 叙事",
        "8. 工作次序",
        "9. 测试默认",
    ]:
        assert sub in text, f"AI协作规范缺少小节：{sub}"

    # 包含全部六大典型场景
    assert "场景 1：基础攻防" in text
    assert "场景 2：闪避交互" in text
    assert "场景 3：残韵插队逆转" in text
    assert "场景 4：防御型反应法术" in text
    assert "场景 5：交错反击型法术" in text
    assert "场景 6：循环自伤法术" in text


def test_readme_navigates_to_ai_policy_and_has_clean_structure():
    """正常路径：README 导航链接到 AI守则.md，且原 AI 规范段落已完成移出。"""
    readme_text = README_FILE.read_text(encoding="utf-8")
    assert "[AI守则](AI守则.md)" in readme_text
    assert "【十三、AI协作规范】" not in readme_text
    assert "零、职责划分" not in readme_text
    assert "六、战斗推演格式" in readme_text
    assert "七步原子流水线" in readme_text


# ---------- 边界条件 ----------

def test_six_scenarios_contain_exact_mechanic_details():
    """边界条件：验证场景3中残韵作用于轮回者永久转化道纹、场景6千刀万剐完整三轮自驱动循环直到法力耗尽中断。"""
    text = AI_POLICY_FILE.read_text(encoding="utf-8")

    # 场景3边界：作用于轮回者A，A拥有道纹永久转化为再生，B不获得道纹
    assert "作用对象为轮回者 A：A 拥有的道纹【杀伐】永久转化为【再生】" in text
    assert "B 不会因此习得【再生】" in text

    # 场景6边界：千刀万剐 3 轮循环展开，法力从 6 扣减至 0，中断检查法力不足
    assert "【失去生命后】B 触发循环法术【千刀万剐】（第 1 轮循环，X=2）" in text
    assert "【失去生命后】B 触发循环法术【千刀万剐】（第 2 轮循环，X=2）" in text
    assert "【失去生命后】B 触发循环法术【千刀万剐】（第 3 轮循环，X=2）" in text
    assert "【中断检查】：B 当前法力为 0，无法支付【再生2】的 2 点法力，循环自然中断" in text


# ---------- 错误输入 / 非法配置 ----------

def test_missing_h1_or_broken_links_in_ai_policy_are_rejected(tmp_path):
    """错误输入：缺少一级标题或包含失效内部锚点时必须被校验器拒绝。"""
    bad_policy = tmp_path / "AI守则.md"
    bad_policy.write_text("缺少一级标题\n\n[坏链接](README.md#不存在的锚点)\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# 灵记\n\n[守则](AI守则.md)\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc:
        validate_markdown_documents(tmp_path)
    message = str(exc.value)
    assert "缺少一级标题" in message or "链接锚点不存在" in message
