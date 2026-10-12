"""外部 AI 单文件入口：规范源 → AI_CONTEXT.md 的可复现、剧情优先聚合。

外部 AI 常只会读取一个 GitHub raw URL，不能保证继续抓 README 的相对链接。AI_CONTEXT.md
优先保留世界观、角色记忆、人物关系、主线目标与角色扮演约束；完整机制按需查询，不成为新的规则正本。

本文件守住：
- 生成物逐字等于生成器输出；
- 只嵌入故事正典与 AI 角色扮演约束；
- 完整规则、道纹、物品、法术与副本机制不批量嵌入；
- README 保留外部 raw 入口，且说明生成物不是事实源。
"""
from pathlib import Path

from engine.document_sources import AI_CONTEXT_FILE, README_FILE
from sim.gen_ai_context import OUTPUT, context_sources, distill_source, render_context

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / README_FILE

REQUIRED_SOURCES = {"故事文档.md", "AI_EXPERIENCE.md"}
BULK_MECHANICS_SOURCES = {
    "规则正文.md", "推演规范.md", "死者之书.md", "全道纹索引.md", "物品索引.md",
    "法术索引.md", "副本索引.md", "副本/扭曲都市.md", "副本/罪孽都市.md",
    "副本/龙心谷.md", "副本/乱葬岗.md",
}

FORBIDDEN_IN_BODY = (
    "```toml", "data/rules", "gen_rules", "gen_ai_context", "sim/", "engine/", "tests/",
    "action_count", "define_spell", "use_daowen",
    "战斗推演格式", "报告书写口径", "遗言格式", "自创法术标准语法", "触发时机接线状态",
    "收录规则", "待核查区", "正文区", "未实现副本草案遗物", "鲛族遗物沉沦海草案",
    "数值规则迁移状态", "新增副本规范", "未实现规则草案",
    "草案", "未实现", "占位", "待补", "尚未接入", "未接入",
)


def _bodies() -> dict[str, str]:
    included = frozenset(source.relative_path for source in context_sources())
    return {source.relative_path: distill_source(source, included) for source in context_sources()}


def test_ai_context_is_exactly_up_to_date_with_its_sources():
    """生成物逐字等于当前 render；防止忘记重生成。"""
    assert OUTPUT == ROOT / AI_CONTEXT_FILE
    assert OUTPUT.is_file(), "缺少外部 AI 单文件入口：AI_CONTEXT.md"
    assert OUTPUT.read_text(encoding="utf-8") == render_context(), (
        "AI_CONTEXT.md 与当前来源不一致，请跑 python3 sim/gen_ai_context.py")


def test_context_covers_story_sources_and_excludes_bulk_mechanics():
    """只嵌入故事正典与角色扮演约束，完整机制文档改为按需查询。"""
    sources = context_sources()
    paths = {source.relative_path for source in sources}
    assert paths == REQUIRED_SOURCES
    assert not (paths & BULK_MECHANICS_SOURCES)
    assert not any(path.endswith(".toml") for path in paths)
    assert README_FILE not in paths
    assert "报告.md" not in paths

    text = OUTPUT.read_text(encoding="utf-8")
    bodies = _bodies()
    for source in sources:
        assert f"- 路径：`{source.relative_path}`" in text
        assert bodies[source.relative_path] in text, (
            f"AI_CONTEXT 缺少过滤后的来源正文：{source.relative_path}")


def test_context_body_contains_no_code_interface_format_or_future_plans():
    """收录正文不含代码、接口、机械格式与未来规划。"""
    for path, body in _bodies().items():
        assert "`" not in body, f"正文残留代码反引号：{path}"
        for marker in FORBIDDEN_IN_BODY:
            assert marker not in body, f"正文残留「{marker}」：{path}"


def test_readme_exposes_the_one_url_external_ai_entry_and_context_contract():
    """README 暴露 raw 入口；上下文说明剧情优先、事实源边界与按需查询。"""
    readme = README.read_text(encoding="utf-8")
    assert "[AI_CONTEXT.md](AI_CONTEXT.md)" in readme
    assert ("https://raw.githubusercontent.com/lilei3061460849-create/"
            "linji-disiyuzhou/main/AI_CONTEXT.md") in readme

    text = OUTPUT.read_text(encoding="utf-8")
    for marker in (
        "## 使用契约（先读）", "## 来源目录与校验指纹", "## 打包正文",
        "死者之书的遗言不是规则", "副本状态要区分", "禁止手改",
        "剧情优先的混合上下文", "按需查询",
    ):
        assert marker in text
    assert len(text) < 25000, "AI_CONTEXT 应明显小于原先包含完整机制索引的版本"
