"""战斗运行态「账本」的**单一权威清单**＋副本世界隔离（2026-09-18 用户裁定：从小到大改，先做这一刀）。

为什么要有这个文件
------------------
`CombatEngine` 上有一批**不在 `GameState` 里**的可变运行态（下称账本）：多以 `id(entity)` 为键，
记「本场/本回合已发动过什么」「残韵改写过谁」「自动闪避用掉几次」。

AI 预演（`engine/ai_preview.py::preview_sequence`）与死斗推演（`sim/win_only_ai.py::_swap_world`）
都复用**同一个** CombatEngine 对象，只把 `state`/`dice` 换成深拷贝副本——副本里的实体是新对象、
`id` 与真实实体不同。若让副本直接写真实账本，就会留下以副本 id 为键的垃圾条目；副本被回收、
地址复用后，后来的真实怪或新副本会**继承**这些条目。典型症状：明明本回合没发动过却报
「不能发动道纹【X】」，且是否命中取决于内存分配顺序 → **同一 seed 两次跑结果不同**
（`tests/test_build_learner.py::test_fixed_seed_is_reproducible` 曾因此失败：战术选 X 的三档预演里，
同一档第一次跑通过、第二次跑被假拒绝）。2026-09-18 已修，修法就是「进副本前 deepcopy 隔离账本」。

问题在于：那批字段当时是**手抄**进 `ai_preview` 的；`win_only_ai` 另有一份手抄（存一遍＋还一遍，
写两次，漏字段不报错、只静默漂移）；`sim/diag_repro.py` 的 L7 审计又抄了第三份（只抄到 3 本）。
三处不同步 ＝「新加一本账忘了登记」这个坑还有三次机会重现。本文件把清单收敛成**一处**：
新增账本只改 `COMBAT_LEDGERS`，预演隔离／死斗推演隔离／复现性审计同时生效，
并由 `tests/test_ledger_isolation.py` 的完备性用例守住（扫真实战斗后的 `vars(combat)`，
凡 id 键容器未登记即失败）。

口径边界（勿混）
----------------
`engine/api.py::_snapshot_combat_runtime` 是**失败事务回滚**快照，不是副本世界隔离：它要把 id 键
翻译成 `(kind, index)` 稳定引用（回滚会重建实体对象），且只覆盖 3 项。那是另一件事、另一套口径，
不由本文件驱动（2026-09-18 只查证、未改动；是否需要扩它的覆盖面属另一裁定）。
"""

from __future__ import annotations

import contextlib
import copy
from typing import Any, Iterator, NamedTuple


class LedgerField(NamedTuple):
    """一本账的登记项。"""

    name: str            # CombatEngine 上的属性名
    default: Any         # 属性尚未创建时的取值（懒初始化的账本，如 _dodge_counts/_dodge_round）
    id_keyed: bool       # 键/元素是否为 id(entity)
    roster_bound: bool   # 键是否**必须始终**是 state 名册里的在场实体——
                         # True：不在名册就是副本执行留下的垃圾键（计入 L7 门禁）
                         # False：键可以合法地比在场时间长（如怪离场/进化后 id 仍留存），
                         #        只作参考计数，不判失败
    note: str            # 这本账记什么


# --------------------------------------------------------------------------
# 权威清单：新增账本只改这里
# --------------------------------------------------------------------------

COMBAT_LEDGERS: tuple[LedgerField, ...] = (
    LedgerField("_monster_activated", {}, True, True,
                "本场已发动过的怪物道纹：id(怪) → 道纹名集合"),
    LedgerField("_monster_daowen_round_used", {}, True, True,
                "本回合已发动：id(怪) → 道纹名（每次发动都占一次）"),
    LedgerField("_resonance_rewrites", {}, True, True,
                "残韵改写记录：id(实体) → 改写明细"),
    LedgerField("_monster_evolved", set(), True, False,
                "已进化/已处理过的怪：id(怪) 集合；怪进化或逃跑后会被移出 state.enemies，"
                "故其 id 合法地不在名册里（roster_bound=False）"),
    LedgerField("_dodge_counts", {}, True, False,
                "自动反应路径的闪避计数：id(目标) → 本回合已闪几次（每回合每目标上限 2 次，"
                "与 choose_dodge 同口径）；换回合才清空，故目标中途离场时键会短暂滞留"
                "（roster_bound=False）。2026-09-18 用户裁定纳入隔离：实测整局脚本轮回 4789 次预演"
                "里预演 0 次写入（自动闪避路径未被预演触达），属潜在漏洞而非活 bug，纳入为结构保险"),
    LedgerField("_dodge_round", None, False, False,
                "_dodge_counts 的回合哨兵（与 _dodge_counts 配套，换回合则清空计数），非 id 键"),
    LedgerField("_sanxiang_consumed", "", False, False, "三响是否已消耗（标量）"),
    LedgerField("_split_clones_spawned", 0, False, False, "分裂已生成的克隆数（标量计数）"),
    LedgerField("_effect_chain_depth", 0, False, False, "效果链递归深度（标量计数）"),
)

LEDGER_NAMES: tuple[str, ...] = tuple(f.name for f in COMBAT_LEDGERS)
ID_KEYED_LEDGERS: tuple[str, ...] = tuple(f.name for f in COMBAT_LEDGERS if f.id_keyed)
ROSTER_BOUND_LEDGERS: tuple[str, ...] = tuple(f.name for f in COMBAT_LEDGERS if f.roster_bound)
DEFAULTS: dict[str, Any] = {f.name: f.default for f in COMBAT_LEDGERS}

# 副本世界还要换掉的引擎级字段（不是账本，但同样会被副本执行写脏）：
#   state / dice（+ combat 侧镜像）、_pending_interrupts、_action_history（按长度截回）、_last_result
_STATE_ROSTER_ATTRS = ("player",)
_LIST_ROSTER_ATTRS = ("friends", "employees", "temp_friends", "enemies")


# --------------------------------------------------------------------------
# 存 → 进副本世界 → 还原
# --------------------------------------------------------------------------

def snapshot(eng) -> dict:
    """记下**真实世界的引用**（不 deepcopy），返回快照字典。

    账本按引用保存，`restore_world` 也按引用归还（identity restore）：外部若持有某本账的
    原对象（如 `_monster_round_used(monster)` 返回的内层集合），还原后它仍是
    `combat.<name>` 本体，不会变成「内容相等但已脱钩」的副本。
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
    （`_monster_round_used(monster).add(name)`、`_monster_activated.setdefault(id, set())`），
    浅拷贝会让副本世界改到真实账本的内层对象。
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

def roster_ids(state) -> set[int]:
    """state 名册里全部实体的 id（含已死/已离场但仍在列表里的）。"""
    ids: set[int] = set()
    if state is None:
        return ids
    for attr in _STATE_ROSTER_ATTRS:
        e = getattr(state, attr, None)
        if e is not None:
            ids.add(id(e))
    for attr in _LIST_ROSTER_ATTRS:
        for e in (getattr(state, attr, []) or []):
            ids.add(id(e))
    return ids


def audit_id_ledgers(combat) -> dict:
    """数每本 id 键账本里「键不在本场名册」的条目 ＝ 副本执行留下的垃圾。

    返回 {"junk_keys": 门禁计数（只数 roster_bound 的账本）,
          "unbound_keys": 参考计数（键可合法滞留的账本，不判失败）,
          "detail": {"<账本名>": 数量}}
    """
    alive = roster_ids(getattr(combat, "state", None))
    junk, unbound, detail = 0, 0, {}
    for field in COMBAT_LEDGERS:
        if not field.id_keyed:
            continue
        led = getattr(combat, field.name, field.default)
        keys: Any
        if isinstance(led, dict):
            keys = list(led.keys())
        elif isinstance(led, (set, frozenset)):
            keys = list(led)
        else:
            continue
        bad = [k for k in keys if isinstance(k, int) and k not in alive]
        if bad:
            detail[field.name] = len(bad)
            if field.roster_bound:
                junk += len(bad)
            else:
                unbound += len(bad)
    return {"junk_keys": junk, "unbound_keys": unbound, "detail": detail}
