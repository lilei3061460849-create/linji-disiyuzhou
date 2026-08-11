"""
pytest - 物品索引与去重（遗物／消耗品／法器）

对应三项裁定中的第③项：
- 各事件/副本处不再重复抄写物品效果，只写名称
- 名称可点击跳转到 物品索引.md 的对应条目
- 旧的能力术语已按裁定改写为 血脉／遗物／准则（不再出现该词）

覆盖：正常路径 / 边界条件 / 错误输入（非法配置须被检出）
"""
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.md")
DUNGEON_DIR = os.path.join(ROOT, "副本")
INDEX = os.path.join(ROOT, "物品索引.md")


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def gh_anchor(heading: str) -> str:
    """GitHub 的锚点生成规则：小写、去标点、空格转连字符。"""
    h = heading.strip().lower()
    h = re.sub(r"[^\w\u4e00-\u9fff\- ]", "", h)
    return h.replace(" ", "-")


def index_anchors() -> set:
    return {gh_anchor(l.lstrip("#").strip())
            for l in read(INDEX).split("\n") if l.startswith("#")}


def item_links() -> list:
    documents = [README] + [os.path.join(DUNGEON_DIR, name) for name in os.listdir(DUNGEON_DIR)
                            if name.endswith(".md")]
    return [anchor for document in documents
            for anchor in re.findall(r"\]\((?:\.\./)?物品索引\.md#([^)]+)\)", read(document))]


# ---------- 正常路径 ----------

def test_index_file_exists_and_has_four_sections():
    """正常路径：索引存在且包含四大分类"""
    txt = read(INDEX)
    for sec in ["遗物池", "事件遗物", "消耗品", "法器"]:
        assert sec in txt, f"索引缺少分类：{sec}"


def test_readme_links_to_index():
    """正常路径：规则文档确实产生了指向索引的跳转链接"""
    links = item_links()
    assert len(links) >= 30, f"跳转链接过少：{len(links)}"


def test_all_relic_pool_items_documented():
    """正常路径：13件遗物池物品每件都在索引中有条目"""
    pool = ["血誓戒", "买路财", "同魂笔", "回锋刀", "折速法印", "三相残韵盘",
            "鲜血契约", "避风铃", "守夜灯", "钱袋", "卖身契", "无所求", "忘忧香"]
    anchors = index_anchors()
    missing = [n for n in pool if gh_anchor(n) not in anchors]
    assert not missing, f"索引缺少遗物条目：{missing}"


# ---------- 边界条件 ----------

def test_every_link_resolves_to_existing_anchor():
    """边界：每一个跳转链接都必须命中真实存在的标题，不得有死链"""
    anchors = index_anchors()
    dead = [l for l in item_links() if gh_anchor(l) not in anchors]
    assert not dead, f"存在失效锚点（死链）：{dead}"


def test_no_duplicate_headings_in_index():
    """边界：索引内不得有重名条目（README§六·命名2 名称全宇宙唯一）"""
    heads = [l.lstrip("#").strip() for l in read(INDEX).split("\n") if l.startswith("###")]
    dupes = {h for h in heads if heads.count(h) > 1}
    assert not dupes, f"索引存在重复条目：{dupes}"


def test_effect_text_not_duplicated_in_readme():
    """边界：被收录物品的完整效果文本不应再重复出现在 README 事件处"""
    readme = read(README)
    # 抽样若干条：这些效果文本应只存在于索引，不再出现在 README
    moved = [
        "消耗品（耐久2）：对[目标]打出15点忽略【格挡】与【闪避】的伤害",
        "每场[战始]可选择是否流血10；若选择，则[战终][血限]+2",
        "[回始]，可消耗 X 点[碎片]，获得 2X 点格挡",
    ]
    still = [m for m in moved if m in readme]
    assert not still, f"README 仍重复抄写了索引中的效果文本：{still}"


# ---------- 错误输入 / 非法配置 ----------

def test_trait_word_fully_removed_repo_wide():
    """错误输入检出：全仓库不得再出现旧术语（已按裁定改写为血脉/遗物/准则）。

    该词以转义构造，避免本文件自身成为命中项。
    """
    banned = "\u7279\u6027"  # 旧术语
    offenders = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", ".pytest_cache"}]
        for fn in filenames:
            if not fn.endswith((".md", ".py")):
                continue
            p = os.path.join(dirpath, fn)
            try:
                txt = read(p)
            except (UnicodeDecodeError, OSError):
                continue
            if banned in txt:
                offenders.append(os.path.relpath(p, ROOT))
    assert not offenders, f"以下文件仍含旧术语{banned}：{offenders}"


def test_detects_broken_anchor_when_injected():
    """错误输入：校验逻辑本身必须能发现死链（防止测试永真）"""
    anchors = index_anchors()
    assert gh_anchor("根本不存在的物品名") not in anchors


def test_index_entries_have_nonempty_description():
    """非法配置：索引中不得存在只有标题、没有说明正文的空条目"""
    lines = read(INDEX).split("\n")
    empty = []
    for i, l in enumerate(lines):
        if l.startswith("### "):
            body = [x for x in lines[i + 1:i + 4] if x.strip() and not x.startswith("#")]
            if not body:
                empty.append(l.strip())
    assert not empty, f"以下条目缺少说明正文：{empty}"
