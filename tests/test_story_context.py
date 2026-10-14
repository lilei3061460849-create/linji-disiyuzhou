"""剧情存档自动整理：只读 故事文档 生成索引／条目／角色知识视图，与角色个人记忆分开。

本文件守住：
- 只解析「剧情存档 → 存档正文」，格式模板与其它章节（收录规则等）不参与；
- 角色知识视图＝在场条目 ∪ 记忆档引用条目，缺记忆档时如实提示；
- CLI 返回码：找不到文档＝2，show 缺条目＝1。
"""
from pathlib import Path

from sim.story_context import (
    main,
    memory_refs,
    parse_entries,
    render_character,
    render_list,
    render_show,
)

STORY = """# 故事文档

## 世界观与底层因果法则

（略）

## 剧情存档

### 写入规则

1. 原始过程由用户提供。

### 存档格式

```
#### E001
- 时间：｜地点：｜在场：
- 白描：（发生了什么）
```

### 存档正文

#### E001
- 时间：第一天·醒时｜地点：空屋｜在场：林晚
- 白描：林晚醒来。她数了数碎片，又数了一遍。

#### D001
- 时间：第一天·醒时｜地点：空屋｜在场：林晚
- 林晚：「三枚。」

#### E002
- 时间：第一天·午后｜地点：城外｜在场：沈昼
- 白描：沈昼在荒径上遇到一只手。

## 收录规则

1. 战报另走一条管线。

## 正文区

（暂无。）
"""

MEMORY_LINWAN = """# 林晚的记忆

- 身份：微光者

## 记得

- [E001·在场] 醒来数了碎片。
- [E002·听说·自沈昼] 荒径上有一只手。
"""


def _story(tmp_path: Path) -> Path:
    path = tmp_path / "故事文档.md"
    path.write_text(STORY, encoding="utf-8")
    return path


def _memory_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "角色记忆"
    directory.mkdir()
    (directory / "林晚.md").write_text(MEMORY_LINWAN, encoding="utf-8")
    return directory


def test_parses_only_archive_body_entries():
    """格式模板里的 E001 不得混入；条目按顺序解析并带字段。"""
    entries = parse_entries(STORY)
    assert [entry.eid for entry in entries] == ["E001", "D001", "E002"]
    assert entries[0].fields["白描"].startswith("林晚醒来")
    assert entries[0].present == ["林晚"]
    assert entries[2].present == ["沈昼"]


def test_list_view_has_identity_time_place_present_and_summary():
    text = render_list(parse_entries(STORY))
    assert "E001｜第一天·醒时｜空屋｜林晚｜林晚醒来" in text
    assert "D001｜第一天·醒时｜空屋｜林晚｜台词 1 句" in text
    assert "（剧情存档为空。）" == render_list([])


def test_show_returns_full_entries_and_reports_missing():
    entries = parse_entries(STORY)
    text, missing = render_show(entries, ["D001", "E999"])
    assert "#### D001" in text and "「三枚。」" in text
    assert missing == ["E999"]


def test_character_view_unions_presence_and_memory_refs(tmp_path):
    """林晚：在场（E001/D001）＋听说引用（E002）都在；沈昼：只有 E002。"""
    entries = parse_entries(STORY)
    memory = _memory_dir(tmp_path)
    linwan = render_character(entries, memory, "林晚")
    assert "#### E001" in linwan and "#### D001" in linwan and "#### E002" in linwan
    shenzhou = render_character(entries, memory, "沈昼")
    assert "#### E002" in shenzhou and "#### E001" not in shenzhou
    assert "记忆档" in shenzhou and "不存在" in shenzhou


def test_memory_refs_parse_entry_numbers(tmp_path):
    memory = _memory_dir(tmp_path)
    assert memory_refs(memory, "林晚") == ["E001", "E002"]
    assert memory_refs(memory, "无名") == []


def test_cli_list_show_and_error_codes(tmp_path, capsys):
    story = _story(tmp_path)
    memory = _memory_dir(tmp_path)
    base = ["--story", str(story), "--memory-dir", str(memory)]

    assert main(["list", *base]) == 0
    assert "E002" in capsys.readouterr().out

    assert main(["show", "e002", *base]) == 0
    assert "#### E002" in capsys.readouterr().out

    assert main(["show", "E404", *base]) == 1
    assert "未找到条目：E404" in capsys.readouterr().err

    assert main(["character", "林晚", *base]) == 0
    assert "#### E002" in capsys.readouterr().out

    missing = tmp_path / "没有这个文件.md"
    assert main(["list", "--story", str(missing)]) == 2
