"""静态规则解析的进程级缓存（RuleRepository）。

规则正文、副本索引与副本文档在**一次进程里只解析一次**，之后每个 GameEngine
直接取这份解析结果：模拟批量开局时不再反复跑 regex 全文。GameEngine 仍然各有
自己的运行时状态（GameState / dice / 事件池触发记录 / 计数器），缓存里只有
「文档解析出来的静态事实」。

三条不变量：

1. **单一事实源仍是文档**。缓存键取输入正文的摘要（不是 mtime），
   所以改文档、测试替换语料、rule_sync 巡检都会立刻重新解析，不存在陈旧缓存。
2. **返回结构副本**。调用方（含测试）改自己引擎的 ``event_pool.events``
   不会污染别的引擎；缓存对象本身只读。
3. **不做第二套事实**。缓存里不放任何运行时状态；触发记录、当前事件等
   仍然只活在 EventPool 实例上。

用法：

    from .rule_repository import RuleRepository
    events = RuleRepository.events()            # {事件名: {region, desc, options}}
    pools = RuleRepository.monster_pool()       # {副本: [怪物面板定义]}
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .dungeons import DEFAULT_INDEX, load_dungeon_documents
from .events import load_event_sources, parse_events
from .monsters import parse_monster_pool

# 缓存键：("events"|"monsters", 索引路径, 输入正文摘要) → 解析结果（只读）
_CACHE: dict[tuple, Any] = {}
# 语料在测试里会被临时替换，键会随之变化；加个上限避免长进程里无界增长。
_CACHE_LIMIT = 16


def _digest(*chunks: str) -> str:
    """输入正文摘要：内容变了键就变，与文件时间戳无关。"""
    hasher = hashlib.blake2b(digest_size=16)
    for chunk in chunks:
        hasher.update(chunk.encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


def _cache_get(key: tuple, builder):
    cached = _CACHE.get(key)
    if cached is None:
        if len(_CACHE) >= _CACHE_LIMIT:
            _CACHE.clear()
        cached = builder()
        _CACHE[key] = cached
    return cached


def _copy_events(events: dict) -> dict:
    """结构副本：外层 dict、每个事件条目、options 列表与选项 dict 全部新建。"""
    return {
        name: {**entry, "options": [dict(option) for option in entry.get("options", [])]}
        for name, entry in events.items()
    }


def _copy_monster_pool(pools: dict) -> dict:
    """结构副本：副本列表、怪物面板定义与道纹表全部新建。"""
    return {
        region: [{**monster, "dao_wen": dict(monster.get("dao_wen") or {})}
                 for monster in monsters]
        for region, monsters in pools.items()
    }


class RuleRepository:
    """只读静态规则容器：解析一次，多个 GameEngine 复用。"""

    @staticmethod
    def events(index_path: str | Path = DEFAULT_INDEX) -> dict:
        """事件定义（通用事件 + 已实现副本专属事件）。"""
        index = Path(index_path)
        content, documents = load_event_sources(index)
        key = ("events", str(index), _digest(content, *documents.values()))
        parsed = _cache_get(key, lambda: parse_events(index_path))
        return _copy_events(parsed)

    @staticmethod
    def monster_pool(index_path: str | Path = DEFAULT_INDEX) -> dict:
        """出怪池：{副本名: [怪物面板定义, ...]}。"""
        index = Path(index_path)
        documents = load_dungeon_documents(index)
        key = ("monsters", str(index), _digest(*documents.values()))
        parsed = _cache_get(key, lambda: parse_monster_pool(index_path))
        return _copy_monster_pool(parsed)

    @staticmethod
    def clear() -> None:
        """清空缓存（测试/调试用；正常运行不需要）。"""
        _CACHE.clear()
