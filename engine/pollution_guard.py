"""沙盒污染检测：把「预演不留痕迹」从口头约定变成可执行断言。

背景（两次真实事故）：

* 2026-09-19：预演在 combat 的 ``_monster_daowen_round_used`` 等 **id 键字典**里
  留下沙盒实体的「老键」，被下一次沙盒复用的地址读走 → AI 实盘轨迹漂移。
* 2026-09-20：预演【结算事件】写脏 ``event_pool.triggered/current`` → 事件在真实
  流程里被永久标记为「已触发」。

两次都不是「某段代码写错了」，而是**「新增了一处引擎侧可变状态，沙盒没跟着加」**。
本模块负责让这类错误自动暴露，而不是等下一次事故：

1. :func:`snapshot_runtime_state` —— 遍历引擎对象图，产出**通用结构快照**。
   不写字段白名单、不比较单个 ``GameState``：凡是可达状态都在快照里，
   新增字段自动纳入（这正是「不依赖程序员记忆」的落点）。
2. :func:`assert_runtime_unchanged` —— 比较两份快照，把差异按**具体路径**报出来。
3. :class:`PollutionGuard` —— 上下文管理器 / 装饰器形式的便捷封装。

启用方式（**默认全程关闭，不进正式路径**）：

* 测试里显式调用（推荐）；
* 调试时设环境变量 ``LJ_POLLUTION_GUARD=1``：``ActionPreview`` 每次预演结束自动
  自检，并打印/抛出污染报告。该开关只在模块导入时读一次，正式运行是纯布尔判断。

设计边界：

* 只做**检测**，不做修复；检测到即抛 :class:`RuntimePollutionError`。
* 快照是结构化的**值 + 结构**，不保存对象引用（避免把引擎钉在内存里）。
* 已知的**良性例外**只有一处，见 ``_IGNORED_PATHS``（全局事件 id 计数器）——
  它不属于引擎状态，且不影响任何规则判定；豁免写在这里而不是散在测试里。
"""
from __future__ import annotations

import os
import types
import weakref
from typing import Any, Optional

from .sandbox import _SHARED_RECORD_FIELDS


class RuntimePollutionError(AssertionError):
    """预演/沙盒执行污染了真实引擎状态。"""


# ---------------------------------------------------------------------------
# 通用对象图快照
# ---------------------------------------------------------------------------

#: 不进入快照的外部对象类型（名字匹配）：IO / 随机源实现 / 线程原语 / 代码对象。
#: 这些都是「引擎使用的工具」，不是「引擎的状态」；它们的内部表示不可比也不该比。
_SKIP_TYPE_NAMES = frozenset({
    "module", "function", "builtin_function_or_method", "method", "type",
    "weakref", "WeakMethod", "_StateEventObserver",
    "Connection", "Cursor", "SQLite3Connection",
    "TextIOWrapper", "BufferedReader", "BufferedWriter",
    "lock", "_thread.lock", "RLock", "Condition", "Event",
    "generator", "coroutine", "Random", "RandomState",
})

#: 明确豁免的路径（前缀匹配）。每条都必须写清理由，避免变成"加一行就绕过"。
_IGNORED_PATHS = (
    # 全局事件 id 计数器（engine.effect_context._event_counter）不在引擎对象图内，
    # 天然不会被遍历到；列在这里是为了防止将来有人把它接进 state。
    "engine.state._event_counter",
)

#: 值类型：直接放进快照。
_SCALARS = (int, float, str, bool, bytes, type(None))

_MAX_DEPTH = 14
_MAX_ENTRIES = 400_000


def _is_skipped(obj: Any) -> bool:
    if isinstance(obj, (types.ModuleType, types.FunctionType, types.MethodType,
                        types.BuiltinFunctionType, type, weakref.ref)):
        return True
    return type(obj).__name__ in _SKIP_TYPE_NAMES


#: 只追加的不可变事实记录字段（名单唯一事实源在 engine/sandbox.py）。
#: 这些字段的元素按契约不可变，所以：
#:   check_record_contents=True  —— 逐元素比对内容（默认，最能发现问题）
#:   check_record_contents=False —— 只比「容器长度 + 元素身份序列」（快 10 倍以上，
#:                                  足以发现追加/删除/替换/换容器，抓不到"原地改某条
#:                                  中间记录"——那属于违反不可变契约，由深模式覆盖）
_RECORD_FIELDS = frozenset(_SHARED_RECORD_FIELDS)


def _walk(obj: Any, path: str, out: dict, seen: set, depth: int,
          *, record_contents: bool = True) -> None:
    # 热路径：每个节点都会走这里，所以只做常数最小的几个判断。
    # （_IGNORED_PATHS 只在根层判一次，见 snapshot_runtime_state——早期版本把它放在
    #   本函数里，每个节点一次 startswith + 一次 any()，长局扫描实测多花 18s/97s。）
    if isinstance(obj, _SCALARS):
        out[path] = obj
        return
    if depth > _MAX_DEPTH or len(out) >= _MAX_ENTRIES:
        return
    if _is_skipped(obj):
        # 记录类型名：对象被换成另一类东西时仍能看出差异。
        out[path] = f"<{type(obj).__name__}>"
        return
    if id(obj) in seen:
        # 循环引用：只记类型名。**不能记 id**——两份快照的 id 必然不同，会假阳性。
        out[path] = f"<cycle:{type(obj).__name__}>"
        return
    seen.add(id(obj))
    if isinstance(obj, dict):
        out[path + ":__type__"] = "dict"
        for key, value in obj.items():
            _walk(value, f"{path}[{key!r}]", out, seen, depth + 1,
                  record_contents=record_contents)
        return
    if isinstance(obj, (list, tuple)):
        out[path + ":__type__"] = type(obj).__name__
        if not record_contents and path.rsplit(".", 1)[-1] in _RECORD_FIELDS:
            # 只追加事实记录：长度 + 元素身份序列（元素身份稳定 ⇒ 内容未被换掉）
            out[path + ":__len__"] = len(obj)
            out[path + ":__ids__"] = tuple(id(item) for item in obj)
            return
        for index, value in enumerate(obj):
            _walk(value, f"{path}[{index}]", out, seen, depth + 1,
                  record_contents=record_contents)
        return
    if isinstance(obj, (set, frozenset)):
        out[path + ":__type__"] = type(obj).__name__
        # 集合无序：按 repr 排序保证两次快照遍历顺序一致（否则报告会抖动）。
        for value in sorted(obj, key=repr):
            _walk(value, f"{path}{{{value!r}}}", out, seen, depth + 1,
                  record_contents=record_contents)
        return
    if hasattr(obj, "__dict__"):
        out[path + ":__type__"] = type(obj).__name__
        for key, value in sorted(vars(obj).items()):
            _walk(value, f"{path}.{key}", out, seen, depth + 1,
                  record_contents=record_contents)
        return
    out[path] = f"<{type(obj).__name__}>"


#: 快照根：引擎对象图的入口。**根对象的身份**单独记录——把根换成另一个内容
#: 相同的对象也是污染（外部持有的引用会看见旧内容），这是内容比较抓不到的。
_ROOT_ATTRS = ("state", "dice", "combat", "event_pool")


class RuntimeSnapshot:
    """一次引擎运行时状态的通用结构快照。"""

    __slots__ = ("entries", "roots", "label")

    def __init__(self, entries: dict, roots: dict, label: str = ""):
        self.entries = entries
        self.roots = roots
        self.label = label

    def diff(self, other: "RuntimeSnapshot") -> list[str]:
        """返回人类可读的差异列表（含路径、前后值）。"""
        if self.roots == other.roots and self.entries == other.entries:
            return []          # 快路径：绝大多数情况（未污染）在这里返回
        lines: list[str] = []
        for key in sorted(set(self.roots) | set(other.roots)):
            if self.roots.get(key) != other.roots.get(key):
                lines.append(f"根对象被替换：{key} 身份改变"
                             f"（{self.roots.get(key)} -> {other.roots.get(key)}）")
        for key in sorted(set(self.entries) | set(other.entries)):
            before = self.entries.get(key, "<不存在>")
            after = other.entries.get(key, "<不存在>")
            if before != after:
                lines.append(f"{key}: {before!r} -> {after!r}")
        return lines

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, RuntimeSnapshot)
                and self.entries == other.entries and self.roots == other.roots)


def snapshot_runtime_state(engine: Any, label: str = "", *,
                           check_record_contents: bool = True) -> RuntimeSnapshot:
    """对引擎对象图做一次通用结构快照（测试/debug 专用，非正式路径）。

    check_record_contents=False 用于长局扫描：只追加事实记录（战斗事件流）不逐元素
    比对内容，只比长度与元素身份——快一个数量级，仍能发现追加/删除/换容器。
    """
    entries: dict[str, Any] = {}
    _walk(vars(engine), "engine", entries, set(), 0,
          record_contents=check_record_contents)
    for ignored in _IGNORED_PATHS:
        for key in [k for k in entries if k.startswith(ignored)]:
            entries.pop(key, None)
    roots = {name: id(getattr(engine, name, None)) for name in _ROOT_ATTRS}
    return RuntimeSnapshot(entries, roots, label)


def assert_runtime_unchanged(before: RuntimeSnapshot, after: RuntimeSnapshot,
                             *, context: str = "") -> None:
    """断言两份快照完全一致；不一致时抛出带路径明细的 RuntimePollutionError。"""
    lines = before.diff(after)
    if not lines:
        return
    head = f"沙盒污染：{context}" if context else "沙盒污染"
    detail = "\n".join(f"  - {line}" for line in lines[:40])
    more = f"\n  …另有 {len(lines) - 40} 处" if len(lines) > 40 else ""
    raise RuntimePollutionError(
        f"{head}（共 {len(lines)} 处差异）\n{detail}{more}\n"
        f"提示：新增的引擎侧可变状态需要在 engine/sandbox.py 的隔离清单里登记。")


class PollutionGuard:
    """``with PollutionGuard(engine, "预演"):`` —— 退出时自动比对。

    也可以当作断言前后来用：``guard = PollutionGuard(engine); ...; guard.check()``。
    """

    def __init__(self, engine: Any, context: str = "", *,
                 check_record_contents: bool = True):
        self.engine = engine
        self.context = context
        self.check_record_contents = check_record_contents
        self.before: Optional[RuntimeSnapshot] = None

    def __enter__(self) -> "PollutionGuard":
        self.before = snapshot_runtime_state(
            self.engine, check_record_contents=self.check_record_contents)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # 沙盒内抛异常同样必须零污染——这正是最容易漏的一条路径，所以异常时也检查。
        if exc_type is not None and isinstance(exc, RuntimePollutionError):
            return False
        self.check()
        return False

    def check(self) -> None:
        after = snapshot_runtime_state(
            self.engine, check_record_contents=self.check_record_contents)
        assert_runtime_unchanged(self.before, after, context=self.context)


def guard_enabled() -> bool:
    """环境变量开关（模块级缓存：正式路径只有一次布尔判断）。"""
    return _ENABLED


_ENABLED = os.environ.get("LJ_POLLUTION_GUARD", "") not in ("", "0", "false", "False")
