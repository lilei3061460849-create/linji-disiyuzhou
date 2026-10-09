"""外部 AI 单文件入口：规范源 → AI_CONTEXT.md 的可复现聚合。

外部 AI 常只会读取一个 GitHub raw URL，不能保证继续抓 README 的相对链接。2026-10-09
用户授权新增 AI_CONTEXT.md：它打包当前有效的游戏知识，但绝不成为新的规则正本。

本文件守住：
- 生成物逐字等于生成器输出（来源一改、未重生成就失败）；
- 规则、流程、死者之书、道纹、物品、法术、副本清单与全部副本文档、故事、AI 约束、
  三份 TOML 都确实被收录；
- README/报告不被误嵌入（前者只是入口，后者只是待办）；
- README 给外部用户一条 main/raw 入口，并标明生成物不是事实源。
"""
from pathlib import Path

from engine.document_sources import AI_CONTEXT_FILE, README_FILE
from sim.gen_ai_context import OUTPUT, context_sources, render_context

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / README_FILE


def test_ai_context_is_exactly_up_to_date_with_its_sources():
    """生成物逐字等于当前 render；防止有人手改或忘了在改源后重生成。"""
    assert OUTPUT == ROOT / AI_CONTEXT_FILE
    assert OUTPUT.is_file(), "缺少外部 AI 单文件入口：AI_CONTEXT.md"
    assert OUTPUT.read_text(encoding="utf-8") == render_context(), (
        "AI_CONTEXT.md 与当前来源不一致，请跑 python3 sim/gen_ai_context.py")


def test_context_covers_all_canonical_game_sources_and_excludes_non_truth_inputs():
    """收录面完整：全部副本文档/TOML 都在，README 与报告不作为正文被嵌入。"""
    sources = context_sources()
    paths = {source.relative_path for source in sources}
    required = {
        "规则正文.md", "推演规范.md", "死者之书.md", "全道纹索引.md", "物品索引.md",
        "法术索引.md", "副本索引.md", "故事文档.md", "AI_EXPERIENCE.md",
        "data/rules/lethal_events.toml", "data/rules/special_events.toml",
        "data/rules/game_rules.toml",
        "副本/扭曲都市.md", "副本/罪孽都市.md", "副本/龙心谷.md", "副本/乱葬岗.md",
        "副本/永夜庭.md", "副本/沉沦海.md", "副本/荒疫古城.md", "副本/巴别塔.md",
    }
    assert paths == required
    assert README_FILE not in paths
    assert "报告.md" not in paths

    text = OUTPUT.read_text(encoding="utf-8")
    for source in sources:
        body = source.path.read_text(encoding="utf-8").rstrip()
        assert f"- 路径：`{source.relative_path}`" in text
        assert body in text, f"AI_CONTEXT 缺少来源正文：{source.relative_path}"


def test_readme_exposes_the_one_url_external_ai_entry_and_context_contract():
    """外部调用者从 README 能找到 raw 入口；上下文先说明事实源与遗言边界。"""
    readme = README.read_text(encoding="utf-8")
    assert "[AI_CONTEXT.md](AI_CONTEXT.md)" in readme
    assert ("https://raw.githubusercontent.com/lilei3061460849-create/"
            "linji-disiyuzhou/main/AI_CONTEXT.md") in readme

    text = OUTPUT.read_text(encoding="utf-8")
    for marker in ("## 使用契约（先读）", "## 来源目录与校验指纹", "## 打包正文",
                   "死者之书的遗言不是规则", "副本状态要区分", "禁止手改"):
        assert marker in text
