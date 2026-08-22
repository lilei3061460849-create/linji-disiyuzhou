"""《死者之书》遗言：文件是唯一事实源。

只读写 `死者之书.md` 的「## 遗言」节，不得改动「## 可学法术」。
新增遗言只需往该节追加三段式数据，不必改引擎代码。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional


LEGACY_FIELDS = ("trigger_point", "fork", "cost_budget")
FIELD_LABELS = {
    "trigger_point": "触发点",
    "fork": "岔路",
    "cost_budget": "代价预算",
}
DEFAULT_CAPACITY = 20
SECTION_HEADER = "## 遗言"
EMPTY_MARK = "当前没有遗言。"

# 可扩展草稿表：新增死因只需加一条，流程代码不用改。
# 两个现成实例：attack（战斗致死）与 collapse（崩解）。
CAUSE_DRAFTS: dict[str, dict[str, str]] = {
    "attack": {
        "trigger_point": "受到致死攻击命零",
        "fork": "未闪避承受攻击",
        "cost_budget": "愿以碎片换保命",
    },
    "collapse": {
        "trigger_point": "异变叠满崩解命零",
        "fork": "继续叠异变未停手",
        "cost_budget": "愿以失忆换清异变",
    },
    "mediocrity": {
        "trigger_point": "连续五回合触发凡庸",
        "fork": "未出手也未破僵局",
        "cost_budget": "愿以法力换一击",
    },
    "duel": {
        "trigger_point": "最终死斗落败",
        "fork": "死斗最后一手选错",
        "cost_budget": "愿以速度换先手",
    },
    "bleed": {
        "trigger_point": "代价流血导致命零",
        "fork": "支付流血未留血",
        "cost_budget": "愿以碎片换血",
    },
    "cancer": {
        "trigger_point": "回复过量触发癌变",
        "fork": "继续堆回复未停手",
        "cost_budget": "愿以失忆换停手",
    },
    "echo_error": {
        "trigger_point": "回音长廊安魂曲",
        "fork": "选择聆听而非打碎",
        "cost_budget": "以碎片换虚假记忆",
        "title": "错误遗言",
    },
}


def clip_text(value: str, limit: int = DEFAULT_CAPACITY) -> str:
    text = (value or "").strip()
    if not text:
        return "未记录"
    return text[:limit]


def validate_legacy(legacy: Any, capacity: int = DEFAULT_CAPACITY) -> dict[str, str]:
    """三段式校验：字段必须恰好为三键，每段非空且不超过字数上限。"""
    if not isinstance(legacy, dict):
        raise ValueError("遗言必须是包含 trigger_point/fork/cost_budget 的对象")
    allowed = set(LEGACY_FIELDS) | {"title", "action", "option"}
    if not set(LEGACY_FIELDS).issubset(legacy):
        raise ValueError("遗言字段必须且只能是 trigger_point/fork/cost_budget")
    extra = set(legacy) - allowed
    if extra:
        raise ValueError("遗言字段必须且只能是 trigger_point/fork/cost_budget")

    normalized: dict[str, str] = {}
    for field_name in LEGACY_FIELDS:
        value = legacy[field_name]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"遗言字段 {field_name} 必须是非空字符串")
        value = value.strip()
        if len(value) > capacity:
            raise ValueError(f"遗言字段 {field_name} 超过{capacity}字上限")
        normalized[field_name] = value
    title = legacy.get("title")
    if isinstance(title, str) and title.strip():
        normalized["title"] = title.strip()
    return normalized


def draft_legacy(
    state: Any,
    cause: str,
    last_action: Optional[dict] = None,
    capacity: int = DEFAULT_CAPACITY,
) -> dict[str, str]:
    """按死因模板生成不超过字数上限的三段式草稿，再用本局事实覆盖岔路。"""
    template = dict(CAUSE_DRAFTS.get(cause) or CAUSE_DRAFTS["attack"])
    battle = int(getattr(state, "current_battle", 0) or 0)
    trigger = template["trigger_point"]
    if battle > 0:
        prefix = f"第{battle}场"
        if len(prefix + trigger) <= capacity:
            trigger = prefix + trigger
    template["trigger_point"] = clip_text(trigger, capacity)

    fork = template["fork"]
    if last_action:
        act = last_action.get("action", "")
        params = last_action.get("params") or {}
        if act == "use_daowen":
            fork = f"最后出手发动{params.get('daowen_name', '道纹')}"
        elif act == "attack":
            fork = "选择攻击而非防御"
        elif act == "monster_phase":
            fork = "未闪避承受攻击"
        elif act == "declare_escape":
            fork = "试图逃跑未能脱身"
        elif act == "consume_item":
            fork = f"使用{params.get('name', '消耗品')}"
        elif act == "resolve_final_duel":
            fork = "死斗最后一手选错"
        elif act == "round_end":
            fork = "回终前未打破僵局"
    template["fork"] = clip_text(fork, capacity)

    player = getattr(state, "player", None)
    shards = int(getattr(state, "shards", 0) or 0)
    if player is not None and getattr(player, "current_speed", 1) <= 0:
        budget = "愿以碎片换速度"
    elif player is not None and getattr(player, "current_mana", 1) <= 0:
        budget = "愿以血换法力"
    elif player is not None and getattr(player, "shield", 1) <= 0:
        budget = "愿以法力换格挡"
    elif shards <= 5:
        budget = "愿以失忆换碎片"
    else:
        budget = template["cost_budget"]
    template["cost_budget"] = clip_text(budget, capacity)

    player_name = getattr(player, "name", "") if player is not None else ""
    region = getattr(state, "current_region", "") or ""
    title_bits = [bit for bit in (player_name, region, f"第{battle}场" if battle else "") if bit]
    if template.get("title"):
        pass
    elif title_bits:
        template["title"] = "·".join(title_bits)
    return validate_legacy(template, capacity)


def render_legacy_section(entries: list[dict[str, str]]) -> str:
    if not entries:
        return f"{SECTION_HEADER}\n\n{EMPTY_MARK}\n"
    lines = [SECTION_HEADER, ""]
    for index, entry in enumerate(entries, 1):
        title = entry.get("title") or f"遗言{index}"
        lines.append(f"### {title}")
        lines.append("")
        for field_name in LEGACY_FIELDS:
            lines.append(f"- {FIELD_LABELS[field_name]}：{entry[field_name]}")
        lines.append("")
    return "\n".join(lines)


def _split_legacy_section(text: str) -> tuple[str, str, str]:
    match = re.search(rf"^## 遗言\s*$", text, re.MULTILINE)
    if not match:
        return text.rstrip() + "\n\n", "", ""
    prefix = text[: match.start()]
    rest = text[match.start() :]
    next_heading = re.search(r"\n## ", rest[1:])
    if next_heading:
        cut = 1 + next_heading.start()
        return prefix, rest[:cut].rstrip() + "\n", rest[cut:]
    return prefix, rest, ""


def parse_legacies(text: str) -> list[dict[str, str]]:
    _, section, _ = _split_legacy_section(text)
    if not section.strip() or (EMPTY_MARK in section and "### " not in section):
        return []
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw in section.splitlines():
        line = raw.strip()
        if line.startswith("### "):
            if all(field in current for field in LEGACY_FIELDS):
                entries.append(current)
            current = {"title": line[4:].strip()}
            continue
        for field_name, label in FIELD_LABELS.items():
            if line.startswith(f"- {label}：") or line.startswith(f"- {label}:"):
                current[field_name] = line.split("：", 1)[-1].split(":", 1)[-1].strip()
    if all(field in current for field in LEGACY_FIELDS):
        entries.append(current)
    return entries


class DeathBookStore:
    """《死者之书.md》遗言节的读写器。path 可换成测试用临时文件。"""

    def __init__(self, path: str | Path = "死者之书.md"):
        self.path = Path(path)

    def load(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        return parse_legacies(self.path.read_text(encoding="utf-8"))

    def write_all(self, entries: list[dict[str, str]]) -> list[dict[str, str]]:
        normalized = [validate_legacy(entry) for entry in entries]
        if self.path.exists():
            text = self.path.read_text(encoding="utf-8")
        else:
            text = "# 死者之书\n\n"
        prefix, _, suffix = _split_legacy_section(text)
        rendered = render_legacy_section(normalized)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(prefix + rendered + (("\n" + suffix.lstrip("\n")) if suffix else ""),
                             encoding="utf-8")
        return self.load()

    def append(self, legacy: dict[str, str]) -> dict[str, str]:
        validated = validate_legacy(legacy)
        entries = self.load()
        entries.append(validated)
        self.write_all(entries)
        return validated

    def remove_at(self, index: int) -> Optional[dict[str, str]]:
        """按 0-based 下标删除一页。越界返回 None，文件不变。"""
        entries = self.load()
        if index < 0 or index >= len(entries):
            return None
        removed = entries.pop(index)
        self.write_all(entries)
        return removed

    def remove_by_title(self, title: str) -> Optional[dict[str, str]]:
        """按标题删除第一页同名遗言。找不到返回 None，文件不变。"""
        want = (title or "").strip()
        if not want:
            return None
        entries = self.load()
        for i, entry in enumerate(entries):
            if (entry.get("title") or "").strip() == want:
                removed = entries.pop(i)
                self.write_all(entries)
                return removed
        return None
