"""战斗运行态「账本」的**单一权威清单**＋副本世界隔离（2026-09-18 ③-lite＋③-full）。

账本是什么
----------
`CombatEngine` 上有一批**不在 `GameState` 里**的可变运行态：记「本场/本回合已发动过什么」
「残韵改写过谁」「自动闪避用掉几次」「哪些怪已进化」。

键为什么是 `Entity.runtime_id`（而不是 `id(entity)`、也不是实体对象本身）
----------------------------------------------------------------------
2026-09-18 之前这批账本按 `id(entity)`（内存地址）建索引，实测出三类问题：

1. **地址复用 → 同一 seed 两次跑结果不同**。AI 预演（`engine/ai_preview.py::preview_sequence`）
   与死斗推演（`sim/win_only_ai.py::_swap_world`）都复用**同一个** CombatEngine 对象，只把
   `state`/`dice` 换成深拷贝副本；副本里的实体是新对象、`id` 与真实实体不同。副本一旦写进真实账本，
   就留下以副本 id 为键的垃圾条目；副本被回收、地址复用后，后来的真实怪或新副本会**继承**这些条目。
   典型症状：明明本回合没发动过却报「不能发动道纹【X】」，且是否命中取决于内存分配顺序
   （`tests/test_build_learner.py::test_fixed_seed_is_reproducible` 曾因此失败）。
   ③-lite 用「进副本前隔离、退出按引用归还」堵住了它；改成 runtime_id 键后这类故障**结构上不可能**
   （uuid4 十六进制串不复用）。
2. **回滚后账本整体失配（真 bug，已实测）**。`combat.py::_monster_phase_restore` 在怪物阶段出错时
   把 `state.__dict__` 整体换成快照副本——**实体对象全被换掉**（`runtime_id` 不变）。id 键账本因此
   100% 失配：实测一次回滚后账本里 1 条非空条目、能对上在场实体的 **0 条**，可观测后果是
   **狂暴的额外出手从 2 掉到 1**（怪物静默丢失本场已激活的持续效果）。runtime_id 键不受影响。
3. **预演看不见真实的回合内状态**。同一探针前后各跑一次实测：4879 次进副本世界里 **2779 次（57%）**看不见
   `_monster_daowen_round_used`（2779 条）、2503 条看不见 `_monster_activated`（副本实体 id 与账本键对不上）。
   同一次实测里「预演说能用、真打被拒」是 **0 次**（候选生成直接读真实账本，不经预演），
   所以这条目前是**保真度欠账而非活 bug**；runtime_id 跨 deepcopy 稳定，改完实测：盲预演 **0** 次、
   看得见的条目 4363（round_used）＋4315（activated）。

为什么不用实体对象当键：① `Entity` 是 `@dataclass`（默认 `eq=True`）→ **不可哈希**，要当键就得
违背 hash/eq 契约（`__hash__ = object.__hash__` 但 `__eq__` 按全字段比较）或改 `__eq__` 语义
（影响全仓按值比较实体的用例）；② 账本持有实体引用＝本仓 2026-08-22 已修过的雷
（`api.py`「存 runtime_id 而非实体引用（引用环会炸事务回滚递归）」、`combat.py::_jiahuo_target`
同款注释）；③ 已死/已离场的实体会被账本拽着不能回收。runtime_id 是本仓既有的稳定身份约定
（`state.personality_traits`、`_jiahuo_target`、`_beifu_target`、`GameState.entity_by_runtime_id`），
且可序列化（存档/回放/断点续跑都用得上）。

清单为什么只有一处
------------------
③-lite 之前这份清单被**手抄在四处**：`ai_preview.py`（`ledger_defaults`）、`win_only_ai.py`
（同一批字段存一遍＋还一遍）、`diag_repro.py`（L7 审计只抄到 3 本）、`ai_preview.py` 里已死的
回滚式预演（第 4 份，已随死代码删除）。三处以上不同步＝「新加一本账忘了登记」这个坑随时重现。
现在新增账本只改 `COMBAT_LEDGERS`：预演隔离、死斗推演隔离、复现性审计同时生效，并由
`tests/test_ledger_isolation.py` 守住（静态扫 `combat.py` 的容器创建点＋扫 engine/sim 里
「拿 `id()` 当账本键」的写法）。

口径边界（勿混）
----------------
`combat.py::_monster_phase_snapshot`/`_monster_phase_restore`（怪物阶段事务）与
`engine/api.py::_snapshot_combat_runtime`/`_restore_combat_runtime`（失败事务回滚）是**回滚快照**，
不是副本世界隔离：前者连 state/dice 一起快照、后者要把账本按在场名册过滤。它们都从本清单取字段名，
但覆盖面各自独立（回滚只覆盖 3 项，2026-09-18 只查证未扩，见 `报告.md` D7）。
"""

from __future__ import annotations

import contextlib
import copy
from typing import Any, Iterator, NamedTuple


class LedgerField(NamedTuple):
    """一本账的登记项。"""

    name: str            # CombatEngine 上的属性名
    default: Any         # 属性尚未创建时的取值（懒初始化的账本，如 _dodge_counts/_dodge_round）
    entity_keyed: bool   # 键/元素是否为 Entity.runtime_id
    roster_bound: bool   # 键是否**必须始终**是 state 名册里的在场角色——
                         # True：不在名册就是垃圾键（计入 L7 门禁）
                         # False：键可以合法地比在场时间长（怪离场/进化后仍留存、
                         #        自动闪避计数换回合才清），只作参考计数，不判失败
    note: str            # 这本账记什么


# --------------------------------------------------------------------------
# 权威清单：新增账本只改这里（键一律 Entity.runtime_id）
# --------------------------------------------------------------------------

COMBAT_LEDGERS: tuple[LedgerField, ...] = (
    LedgerField("_monster_activated", {}, True, True,
                "本场已发动过的怪物道纹：runtime_id → 道纹名集合（持续激活口径，狂暴出手加成等）"),
    LedgerField("_monster_daowen_round_used", {}, True, True,
                "本回合已发动：runtime_id → (回合号, 道纹名集合)；换回合自动清空"),
    LedgerField("_resonance_rewrites", {}, True, True,
                "残韵改写记录：runtime_id → {来源: 改写成的道纹名}"),
    LedgerField("_monster_evolved", set(), True, False,
                "已进化/已处理过的怪：runtime_id 集合；怪进化或逃跑后会被移出 state.enemies，"
                "故其键合法地不在名册里（roster_bound=False）"),
    LedgerField("_dodge_counts", {}, True, False,
                "自动反应路径的闪避计数：runtime_id → 本回合已闪几次（每回合每目标上限 2 次，"
                "与 choose_dodge 同口径）；换回合才清空，故目标中途离场时键会短暂滞留"
                "（roster_bound=False）。2026-09-18 用户裁定纳入隔离：实测 4789 次预演里预演 0 次写入"
                "（自动闪避路径未被预演触达），属结构保险"),
    LedgerField("_dodge_round", None, False, False,
                "_dodge_counts 的回合哨兵（换回合则清空计数），非实体键"),
    LedgerField("_sanxiang_consumed", "", False, False, "三响是否已消耗（标量）"),
    LedgerField("_split_clones_spawned", 0, False, False, "分裂已生成的克隆数（标量计数）"),
    LedgerField("_effect_chain_depth", 0, False, False, "效果链递归深度（标量计数）"),
)

LEDGER_NAMES: tuple[str, ...] = tuple(f.name for f in COMBAT_LEDGERS)
ENTITY_KEYED_LEDGERS: tuple[str, ...] = tuple(f.name for f in COMBAT_LEDGERS if f.entity_keyed)
ROSTER_BOUND_LEDGERS: tuple[str, ...] = tuple(f.name for f in COMBAT_LEDGERS if f.roster_bound)
DEFAULTS: dict[str, Any] = {f.name: f.default for f in COMBAT_LEDGERS}

_STATE_ROSTER_ATTRS = ("player",)
_LIST_ROSTER_ATTRS = ("friends", "employees", "temp_friends", "enemies")


# --------------------------------------------------------------------------
# 名册
# --------------------------------------------------------------------------

def roster_entities(state) -> list:
    """state 名册里的全部实体（含已死/已离场但仍在列表里的）。"""
    out: list = []
    if state is None:
        return out
    for attr in _STATE_ROSTER_ATTRS:
        e = getattr(state, attr, None)
        if e is not None:
            out.append(e)
    for attr in _LIST_ROSTER_ATTRS:
        out.extend(getattr(state, attr, []) or [])
    return out


def roster_runtime_ids(state) -> set[str]:
    """state 名册里全部角色的 runtime_id。"""
    return {e.runtime_id for e in roster_entities(state) if getattr(e, "runtime_id", None)}


# --------------------------------------------------------------------------
# 存 → 进副本世界 → 还原
# --------------------------------------------------------------------------

def snapshot(eng) -> dict:
    """记下**真实世界的引用**（不 deepcopy），返回快照字典。

    账本按引用保存、`restore_world` 也按引用归还（identity restore）：外部若持有某本账的原对象
    （如 `_monster_round_used(monster)` 返回的内层集合），还原后它仍是本体而非脱钩副本。
    """
    combat = eng.combat
    return {
        "eng_state": eng.state,
        "eng_dice": eng.dice,
        "combat_state": combat.state,
        "combat_dice": combat.dice,
        "pending_interrupts": eng._pending_interrupts,
        "action_history_len": len(eng._action_history),
        "last_result": eng._last_result,
        # 懒初始化的账本（如 _dodge_counts/_dodge_round 只在自动闪避路径里才创建）：
        # 属性不存在时**必须 deepcopy 一份默认值**，不能直接把 DEFAULTS 里的共享对象挂进快照——
        # 否则 restore_world 会把那个共享对象设成真实账本，之后任何写入都会污染全项目的默认值。
        "ledgers": {name: (getattr(combat, name) if hasattr(combat, name)
                           else copy.deepcopy(DEFAULTS[name])) for name in LEDGER_NAMES},
    }


def enter_copy_world(eng, snap: dict) -> None:
    """把引擎切到**副本世界**：state/dice/账本全换成深拷贝，真实对象留在 `snap` 里。

    账本必须 deepcopy 而非浅拷贝：它们的值会被**原地改写**
    （`_monster_round_used(monster).add(name)`、`_monster_activated.setdefault(rid, set())`），
    浅拷贝会让副本世界改到真实账本的内层对象。

    键是 runtime_id（字符串、跨 deepcopy 稳定），所以副本世界的账本天然对得上副本世界的角色：
    预演能看见「这只怪本回合已经发动过什么」，不再像 id 键时代那样一律查不到（实测 56% 的预演
    曾处于这种盲态）。
    """
    combat = eng.combat
    snap["copy_state"] = copy.deepcopy(snap["eng_state"])
    snap["copy_dice"] = copy.deepcopy(snap["eng_dice"])
    eng.state = snap["copy_state"]
    combat.state = snap["copy_state"]
    eng.dice = snap["copy_dice"]
    combat.dice = snap["copy_dice"]
    for name in LEDGER_NAMES:
        setattr(combat, name, copy.deepcopy(snap["ledgers"][name]))
    eng._pending_interrupts = copy.deepcopy(snap["pending_interrupts"])


def restore_world(eng, snap: dict) -> None:
    """按引用原样切回真实世界；副本世界里的一切写入作废。"""
    combat = eng.combat
    eng.state = snap["eng_state"]
    combat.state = snap["combat_state"]
    eng.dice = snap["eng_dice"]
    combat.dice = snap["combat_dice"]
    for name in LEDGER_NAMES:
        setattr(combat, name, snap["ledgers"][name])
    eng._pending_interrupts = snap["pending_interrupts"]
    del eng._action_history[snap["action_history_len"]:]
    eng._last_result = snap["last_result"]


@contextlib.contextmanager
def copy_world(eng) -> Iterator[dict]:
    """`with copy_world(eng) as snap:` —— 进出副本世界，异常也保证还原。

    `snap["copy_state"]` 是副本世界的 state（预演拿它与 `snap["eng_state"]` 做后果对比）。
    """
    snap = snapshot(eng)
    enter_copy_world(eng, snap)
    try:
        yield snap
    finally:
        restore_world(eng, snap)


# --------------------------------------------------------------------------
# 复现性审计（sim/diag_repro.py 的 L7 用它，不再自己抄清单）
# --------------------------------------------------------------------------

def audit_ledgers(combat) -> dict:
    """数每本实体键账本里「键不在本场名册」的条目 ＝ 垃圾键。

    runtime_id 键下垃圾键只可能来自「实体已离场」，不再可能来自地址复用；因此这里既是
    复现性门禁，也是离场清理是否到位的检查。

    返回 {"junk_keys": 门禁计数（只数 roster_bound 的账本）,
          "unbound_keys": 参考计数（键可合法滞留的账本，不判失败）,
          "detail": {"<账本名>": 数量}}
    """
    live = roster_runtime_ids(getattr(combat, "state", None))
    junk, unbound, detail = 0, 0, {}
    for field in COMBAT_LEDGERS:
        if not field.entity_keyed:
            continue
        led = getattr(combat, field.name, field.default)
        if isinstance(led, dict):
            keys = list(led.keys())
        elif isinstance(led, (set, frozenset)):
            keys = list(led)
        else:
            continue
        bad = [k for k in keys if k not in live]
        if bad:
            detail[field.name] = len(bad)
            if field.roster_bound:
                junk += len(bad)
            else:
                unbound += len(bad)
    return {"junk_keys": junk, "unbound_keys": unbound, "detail": detail}
