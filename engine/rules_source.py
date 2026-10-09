"""结构化事实源的加载器（样板批：致死类特殊事件）。

## 方向

旧机制 `engine/rule_sync.py` 是**从 markdown 正则抓取、再和引擎注册表比对**——
它只能数出「文档提到 N 条、引擎注册 N 条」，真冲突时谁对并无断言，是软护栏。

新机制反过来：**`data/rules/*.toml` 是源**，引擎常量、进度渲染、死者之书文案、
《死者之书.md》的「## 规则」节全部由它派生。这才有唯一事实源。

## 引擎怎么读

`Entity.MUTATION_COLLAPSE_THRESHOLD` 这类**类属性在 import 时从本模块取值**，
所以全仓库那几十处 `Entity.MUTATION_COLLAPSE_THRESHOLD` 的写法一个都不用改，
但事实源已经变成了 toml。这是低风险的接线方式：改规则只动 toml。

## 格式选择

用 TOML 而非 YAML：引擎至今零第三方依赖（纯 stdlib），运行环境是 PEP-668
受管的，为一个事实源引入 PyYAML 不值得；`tomllib` 是 3.11 自带，且支持注释。
若日后愿意接受依赖，只需改本文件的 `_load`。
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
RULES_DIR = ROOT / "data" / "rules"

LETHAL_EVENTS_FILE = RULES_DIR / "lethal_events.toml"


def _load(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


class _LethalEvents:
    """致死类特殊事件的事实源访问器（模块级单例 `lethal_events`）。"""

    def __init__(self, path: Path = LETHAL_EVENTS_FILE) -> None:
        self._path = path
        self._doc = _load(path)
        self._by_id: dict[str, dict[str, Any]] = {}
        self._by_name: dict[str, dict[str, Any]] = {}
        for entry in self._doc.get("event", []):
            self._by_id[entry["id"]] = entry
            self._by_name[entry["name"]] = entry

    # ---- 元信息 ----
    @property
    def signature(self) -> str:
        """《死者之书》规则节的署名（遗言署人名、规则署 ？？？）。"""
        return self._doc.get("signature", "？？？")

    @property
    def chapter(self) -> str:
        return self._doc.get("chapter", "特殊事件")

    # ---- 查询 ----
    def all(self) -> list[dict[str, Any]]:
        return sorted(self._doc.get("event", []), key=lambda e: e.get("order", 0))

    def by_id(self, event_id: str) -> dict[str, Any]:
        try:
            return self._by_id[event_id]
        except KeyError:
            raise KeyError(f"致死事件 id 不存在: {event_id}") from None

    def by_name(self, name: str) -> dict[str, Any]:
        """按名称查条目。显示键带 `·` 后缀时按前缀查（`凡庸·未出手` → 凡庸）。"""
        if name in self._by_name:
            return self._by_name[name]
        head = name.split("·", 1)[0]
        if head in self._by_name:
            return self._by_name[head]
        raise KeyError(f"致死事件名称不存在: {name}")

    def progress_keys(self, event_id: str) -> Optional[list[str]]:
        """该事件在进度面板上的显示键（多数事件就等于事件名）。"""
        return self.by_id(event_id).get("progress_keys")

    # ---- 引擎消费的字段 ----
    def threshold(self, event_id: str) -> int:
        """固定阈值（癌变是倍率制，走 threshold_multiplier）。"""
        entry = self.by_id(event_id)
        if "threshold" not in entry:
            raise KeyError(f"致死事件 {event_id} 没有固定阈值，请用 threshold_multiplier")
        return int(entry["threshold"])

    def threshold_multiplier(self, event_id: str) -> float:
        entry = self.by_id(event_id)
        if "threshold_multiplier" not in entry:
            raise KeyError(f"致死事件 {event_id} 没有倍率阈值，请用 threshold")
        return float(entry["threshold_multiplier"])

    def progress_format(self, event_id: str) -> str:
        return self.by_id(event_id)["progress_format"]

    def death_cause_key(self, event_id: str) -> str:
        return self.by_id(event_id)["death_cause_key"]

    def death_cause_text(self, event_id: str) -> str:
        return self.by_id(event_id)["death_cause_text"]

    def rule_lines(self, event_id: str) -> list[str]:
        return list(self.by_id(event_id)["rule_lines"])

    def counter_field(self, event_id: str) -> str:
        return self.by_id(event_id)["counter_field"]

    def alt_counter_field(self, event_id: str) -> Optional[str]:
        return self.by_id(event_id).get("alt_counter_field")


lethal_events = _LethalEvents()
