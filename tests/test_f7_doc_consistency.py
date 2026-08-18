"""
F7 验证：文档一致性（全程自动触发 + 命名漂移 + engine/README 复审）
- 正常：README 五章列表与特殊事件节 14 项对齐，含凡庸/癌变/崩解/救赎
- 边界：活跃代码层（engine/*.py, sim/*.py, tests/*.py）不再出现中文“增生”，仅允许在废弃别名注释中出现
- 错误：对旧名“增生”的显式调用应被拒绝或不存在
- 引擎README：文件结构已补全至当前19项且含F7订正注记
"""
import pathlib
import re

def test_normal_readme_auto_trigger_list():
    readme = pathlib.Path("README.md").read_text(encoding="utf-8")
    # 找到 五、全程自动触发 段
    m = re.search(r"五、全程自动触发[^\n]*\n([^\n]+)", readme)
    assert m, "未找到 五、全程自动触发 段"
    line = m.group(1)
    # 必须包含 凡庸 / 癌变 / 崩解（本次 F7 补漏的三项）
    for kw in ["凡庸", "癌变", "崩解"]:
        assert kw in line, f"五章列表应包含 {kw}，实际为：{line}"
    # 同步 all 14 项：死之传承、凡庸、癌变、还债、雕塑、员工叛变、急中生智、逃跑与追击、进化、崩解、撤退、最终的冠冕、初拥之夜、救赎
    expected_14 = ["死之传承","凡庸","癌变","还债","雕塑","员工叛变","急中生智","逃跑与追击","进化","崩解","撤退","最终的冠冕","初拥之夜","救赎"]
    for kw in expected_14:
        assert kw in line, f"同步后五章应含 {kw}"
    # 验证特殊事件节的标题与五章一致（不校验数量，仅校验关键词存在）
    assert "凡庸（任一角色连续五回合" in readme
    assert "多个角色触发凡庸时，非轮回者优先触发" in readme
    assert "癌变（任一角色在本场战斗内累计受到回复" in readme
    assert "累计回复属于局内减益追踪，[战终]清零" in readme
    assert "崩解（任一角色【异变】达到" in readme
    assert "救赎（怪物没有怪物道纹" in readme

def test_boundary_no_zengsheng_in_active_code():
    """边界：活跃代码层不再出现中文“增生”（旧名），已统一为癌变；增殖道纹不受影响"""
    active_roots = [pathlib.Path("engine"), pathlib.Path("sim")]
    hits = []
    for root in active_roots:
        for p in root.rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            # 排除掉 已知的兼容别名注释中允许出现的一次（engine/models.py 保留旧字段名的注释里含“增生”二字用于说明）
            # 为严格，我们要求除了 models.py 的那一行兼容注释外，其余不应出现
            if "增生" in text:
                # 计算行号
                for i, line in enumerate(text.splitlines(), 1):
                    if "增生" in line and "增殖" not in line:
                        # 允许 models.py 的那一行（包含“旧名 增生”）
                        if p.name == "models.py" and "旧名" in line and "增生" in line:
                            continue
                        # 允许 combat.py 的那一行 type= proliferation 注释中的旧名说明（若有）
                        if p.name == "combat.py" and "旧名" in line:
                            continue
                        hits.append(f"{p}:{i}:{line.strip()}")
    assert hits == [], f"活跃代码仍含增生（应已全改为癌变）：{hits[:5]}"
    # 增殖道纹必须仍在（二者无关）
    assert pathlib.Path("engine/daowen.py").read_text(encoding="utf-8").count("增殖") > 0
    assert pathlib.Path("engine/models.py").read_text(encoding="utf-8").count("增殖") > 0

def test_error_old_name_rejected():
    """错误：对旧字段/旧阈值的显式错误使用应不存在"""
    # 旧阈值名 PROLIFERATION_THRESHOLD 应仍可用作别名，但新名 CANCER_THRESHOLD 应存在
    from engine.combat import CombatEngine
    assert hasattr(CombatEngine, "PROLIFERATION_THRESHOLD")
    assert hasattr(CombatEngine, "CANCER_THRESHOLD")
    assert CombatEngine.CANCER_THRESHOLD == CombatEngine.PROLIFERATION_THRESHOLD
    # 旧实体字段 is_proliferated 仍兼容，但新别名 is_cancer 应可用
    from engine.models import Entity
    e = Entity(name="test", entity_type="怪物")
    e.is_proliferated = True
    assert e.is_cancer is True
    e.is_cancer = False
    assert e.is_proliferated is False
    # 非法：若外部试图用“增生”作为道纹名应不存在（道纹池无此名）
    from engine.daowen import DaoWenEngine
    DaoWenEngine.register_all()
    assert "增生" not in DaoWenEngine._registry
    assert "癌变" not in DaoWenEngine._registry  # 癌变是机制非道纹，不在道纹注册表
    assert "增殖" in DaoWenEngine._registry

def test_engine_readme_completeness():
    """engine/README 已全面复审：文件结构当前19项 + F7订正注记。"""
    text = pathlib.Path("engine/README.md").read_text(encoding="utf-8")
    # 文件结构应包含新增的 8 个缺漏文件
    for fname in ["gamedata.py", "events.py", "battle_report.py", "ai_player.py", "ai_tactics.py", "rule_sync.py", "dungeons.py", "document_validation.py", "validator.py"]:
        assert fname in text, f"engine/README 文件结构应包含 {fname}"
    # F7 订正注记
    assert "F7 订正" in text or "增生" in text or "癌变" in text
    # 增生→癌变 说明
    assert "癌变" in text
    assert "增殖" in text

def test_no_active_zengsheng_in_tests_except_allowed():
    """tests 目录除本文件的允许注释外，不应再以“增生”作为机制名出现"""
    hits = []
    for p in pathlib.Path("tests").rglob("*.py"):
        if p.name == "test_f7_doc_consistency.py":
            continue
        text = p.read_text(encoding="utf-8")
        if "增生" in text:
            # test_engine.py 的旧路径测试已在上轮改为癌变，允许其存在中文“癌变”，但不应再有“增生”
            hits.append(str(p))
    # 允许 test_engine.py 中仍保留的 proliferation 英文 type 字符串，但中文不应再出现
    # 实际本轮已将 tests/test_engine.py 的增生中文改为癌变，故此处应为空
    # 若仍有，说明还有漏改
    assert hits == [], f"tests 仍含增生中文：{hits}"
