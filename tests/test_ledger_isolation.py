"""账本清单的**单一权威**＋`runtime_id` 键（`engine/ledger_isolation.py`）守卫测试。

2026-09-18 用户裁定「从小到大改」→ ③-lite（清单单一化）→ ③-full（键从 `id(entity)` 改
`Entity.runtime_id`，用户选 A，并先做 C 量化）。本文件守三件事：

**一、清单只有一处**（③-lite）。此前这份清单被手抄在四处：`engine/ai_preview.py`
（`ledger_defaults`）、`sim/win_only_ai.py`（同一批字段存一遍＋还一遍）、`sim/diag_repro.py`
（L7 只抄到 3 本）、`ai_preview.py` 里已死的回滚式预演（第 4 份，随死代码删除）。
→ 用例：三个调用点都引用权威清单、手抄痕迹不得复活、静态扫 `combat.py` 的容器创建点
（新账本在多数局面里是**空的**，只有静态扫描抓得到「忘了登记」）。

**二、键是 runtime_id，不是内存地址**（③-full）。实测依据（整局脚本轮回 7 场、4789 次预演）：
- 地址复用会让同一 seed 两次跑结果不同（`test_fixed_seed_is_reproducible` 曾因此失败）；
- **怪物阶段回滚后 id 键账本 100% 失配**：`_monster_phase_restore` 把实体换成快照副本
  （`runtime_id` 不变、对象全换），实测回滚后账本 1 条非空条目里能对上在场实体的 **0 条**，
  可观测后果＝**狂暴的额外出手从 2 掉到 1**（怪物静默丢失本场已激活的持续效果）；
- 57%（2779/4879）的副本世界看不见 `_monster_daowen_round_used`（副本实体 id 与账本键对不上），
  改键后实测盲预演 0 次、看得见 4363＋4315 条；
  同一次实测里「预演说能用、真打被拒」为 0 次，所以这条是保真度欠账而非活 bug。
→ 用例：AST 扫 engine/ 与 sim/，凡「同一条语句里既用了账本、又用了 `id()`」即失败；
   加上下面这条回滚回归用例（③-full 的实际收益）。

**三、隔离语义**：进副本世界拿到深拷贝、退出时**引用与内容都原样归还**（identity restore）；
懒初始化的账本不得被还原成清单里的共享默认对象（否则写入会污染全项目默认值）。

口径边界：`engine/api.py::_snapshot_combat_runtime`（失败事务回滚）是另一套口径、只覆盖 3 项
（2026-09-18 查证未扩，见 `报告.md` D7），本文件只钉「它覆盖的账本名必须在权威清单内」。
"""
import ast
import copy
import os
import random
import re
import sys

import pytest

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.ai_preview import ActionPreview
from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.daowen import DaoWenEngine
from engine.ledger_isolation import (COMBAT_LEDGERS, DEFAULTS, ENTITY_KEYED_LEDGERS,
                                     LEDGER_NAMES, ROSTER_BOUND_LEDGERS,
                                     audit_ledgers, copy_world, roster_runtime_ids)
from engine.monsters import make_monster_entity

DaoWenEngine.register_all()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# runtime_id 的形态：uuid4().hex ＝ 32 位十六进制
HEX32 = re.compile(r"^[0-9a-f]{32}$")

# 运行时扫描的豁免表（键是 runtime_id 形态但确认不需隔离）；静态扫描的豁免见 EXEMPT_CONTAINERS。
EXEMPT: dict[str, str] = {}

# 在 CombatEngine 上创建容器型运行态的赋值语句（`self._x = {}` / `set()` / `[]` / defaultdict）
_CONTAINER_ASSIGN = re.compile(
    r"self\.(_[a-z_0-9]+)\s*=\s*(?:\{\}|\[\]|set\(\)|dict\(\)|list\(\)|"
    r"(?:collections\.)?defaultdict\(|OrderedDict\()")

# 若将来出现「容器型运行态但不是需要隔离的账本」（例如按道纹名索引的缓存），
# 在这里登记名字＋理由；不要直接放宽断言。当前为空：combat.py 里创建的容器**全部**是账本。
EXEMPT_CONTAINERS: dict[str, str] = {}


def _src(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _engine(suffix: str) -> GameEngine:
    engine = GameEngine(db_path=f"data/test_ledger_iso_{suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "沈昼", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    engine.execute_action("setup_choose_region", {"region": "扭曲都市"})
    return engine


def _monster(name="靶怪", daowen="必中"):
    return make_monster_entity({
        "name": name, "blood_limit": 222, "mana_limit": 9, "speed_limit": 3,
        "attack_count": 3, "attack_power": 7,
        "dao_wen": {daowen: None}, "region": "扭曲都市",
    })


def _enter_combat(engine, monster):
    engine.state.enemies = [monster]
    engine.state.phase = "in_combat"
    engine.state.current_round = 2
    engine.combat.reset_monster_activation()


def _ledger_objects(combat) -> dict:
    """当前每本账的**对象引用**（属性还没创建的记 None，用来验 identity restore）。"""
    return {name: getattr(combat, name, None) for name in LEDGER_NAMES}


# --------------------------------------------------------------------------
# 一、单一权威：调用点都引用它，手抄痕迹不得复活
# --------------------------------------------------------------------------

def test_all_call_sites_reference_the_single_source():
    for rel in ("engine/ai_preview.py", "sim/win_only_ai.py", "sim/diag_repro.py",
                "engine/api.py"):
        assert "ledger_isolation" in _src(rel), (
            f"{rel} 必须引用 engine/ledger_isolation.py 的权威清单；"
            f"自己再列一遍账本名＝重新分叉，就是本轮要消灭的那个坑")


def test_hand_copied_lists_are_gone():
    ap = _src("engine/ai_preview.py")
    assert "ledger_defaults" not in ap, "ai_preview 里手抄的 ledger_defaults 已删，不要写回来"
    assert "_monster_activated" not in ap, "ai_preview 不该再逐本点名账本（清单只在 ledger_isolation.py）"
    woa = _src("sim/win_only_ai.py")
    assert 'combat._monster_activated = copy.deepcopy' not in woa, (
        "win_only_ai 里「存一遍＋还一遍」的手抄账本清单已删，不要写回来")
    assert "_resonance_rewrites" not in woa, "win_only_ai 不该再逐本点名账本"
    dr = _src("sim/diag_repro.py")
    assert not re.search(r'^LEDGERS = \(', dr, re.M), (
        "diag_repro 里手抄的 LEDGERS 三元组已删，改用 ENTITY_KEYED_LEDGERS")
    assert "ENTITY_KEYED_LEDGERS" in dr


def test_spec_entries_are_self_documenting():
    for field in COMBAT_LEDGERS:
        assert field.name.startswith("_") and field.note.strip(), f"{field.name} 缺说明"
    # 门禁集合（键必须始终在场）至少包含最初那三本；扩它要有意识地改这里
    assert {"_monster_activated", "_monster_daowen_round_used",
            "_resonance_rewrites"} <= set(ROSTER_BOUND_LEDGERS)
    assert set(ROSTER_BOUND_LEDGERS) <= set(ENTITY_KEYED_LEDGERS) <= set(LEDGER_NAMES)


def test_every_container_ledger_created_in_combat_is_registered():
    """**静态**扫 `engine/combat.py`：凡在 CombatEngine 上创建的容器都必须已登记。

    为什么必须静态：新加的账本在多数局面里是**空的**（`_resonance_rewrites` 整局脚本轮回
    都没被写过），运行时扫描看不出键类型、抓不到「忘了登记」。反向对照实测：把
    `_resonance_rewrites` 从清单里摘掉 → 本用例失败并点名该账本（只用运行时扫描时不失败）。
    """
    src = _src("engine/combat.py")
    created = set(_CONTAINER_ASSIGN.findall(src))
    assert created, "没扫到任何容器创建点，正则失效＝断言空转"
    assert {"_monster_activated", "_monster_daowen_round_used", "_resonance_rewrites",
            "_monster_evolved", "_dodge_counts"} <= created, (
        f"正则漏扫了已知账本：{sorted(created)}")
    missing = created - set(LEDGER_NAMES) - set(EXEMPT_CONTAINERS)
    assert not missing, (
        f"combat.py 新建了未登记的容器型运行态：{sorted(missing)}。"
        f"请加进 engine/ledger_isolation.COMBAT_LEDGERS（预演/死斗推演隔离与 L7 审计"
        f"会同时生效），或确认它不需要隔离后登记进 EXEMPT_CONTAINERS 并写明理由。")


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _nodes_in_own_scope(node):
    """node 自身的 AST 节点，**剪掉嵌套作用域**（函数/类/lambda 各自单独检查）。

    不剪枝就会把整个 `class CombatEngine` 当成一条语句，凡类里任何地方同时出现账本名与
    `id()` 就误报（第一版正是这样报了 api.py:83 与 combat.py:32 两个假阳性）。
    """
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        for child in ast.iter_child_nodes(n):
            if isinstance(child, _SCOPES):
                continue
            stack.append(child)


def test_no_id_keyed_ledger_access_left():
    """**AST 扫** engine/ 与 sim/：同一条语句里既用了账本、又用了 `id()` → 就是漏改的旧键。

    用 AST 而不是文本匹配：注释与 docstring 里讨论历史（「当时都按 id(entity) 建索引」）
    不该报警，而 `x[id(m)]`／`.get(id(m))`／`id(m) in x` 必须报。
    """
    ledger_names = set(LEDGER_NAMES)
    offenders = []
    for base in ("engine", "sim"):
        for dirpath, _dirnames, filenames in os.walk(os.path.join(ROOT, base)):
            for fn in sorted(filenames):
                if not fn.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), ROOT)
                tree = ast.parse(_src(rel))
                for stmt in (n for n in ast.walk(tree)
                             if isinstance(n, ast.stmt) and not isinstance(n, _SCOPES)):
                    nodes = list(_nodes_in_own_scope(stmt))
                    touched = {n.attr for n in nodes
                               if isinstance(n, ast.Attribute) and n.attr in ledger_names}
                    if not touched:
                        continue
                    uses_id = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                                  and n.func.id == "id" for n in nodes)
                    if uses_id:
                        offenders.append(f"{rel}:{stmt.lineno} {sorted(touched)}")
    assert not offenders, (
        "这些语句仍在用 id() 索引账本（键必须是 Entity.runtime_id）：\n  " +
        "\n  ".join(offenders))


def test_runtime_id_keyed_containers_are_registered():
    """**运行时**辅门禁：跑一局真实脚本轮回后，扫 `vars(combat)` 里 runtime_id 形态键的容器。"""
    import sim.build_learner as bl

    seen: list = []
    orig_init = CombatEngine.__init__

    def _init(self, *a, **k):
        orig_init(self, *a, **k)
        seen.append(self)

    CombatEngine.__init__ = _init
    try:
        bl.play("杀伐", ["庇护", "再生"], "龙心谷", 42, battles=1, rng=random.Random(9))
    finally:
        CombatEngine.__init__ = orig_init

    assert seen, "没抓到任何 CombatEngine 实例，扫描等于空转"
    offenders, checked = [], 0
    for combat in seen:
        for name, val in vars(combat).items():
            if isinstance(val, dict):
                keys = list(val.keys())
            elif isinstance(val, (set, frozenset)):
                keys = list(val)
            else:
                continue
            if not keys or not all(isinstance(k, str) and HEX32.match(k) for k in keys):
                continue          # 空容器看不出键类型；非 runtime_id 键的容器不是实体账本
            checked += 1
            if name not in LEDGER_NAMES and name not in EXEMPT:
                offenders.append(f"{name}（{len(keys)} 个 runtime_id 键）")
    assert checked, "一局下来没扫到任何 runtime_id 键容器，辅门禁空转（账本没被用上？）"
    assert not offenders, (
        "CombatEngine 上出现了未登记的 runtime_id 键账本：" + "、".join(sorted(set(offenders))))


def test_scalars_in_spec_still_exist_on_combat():
    """清单里的标量账本（非容器）名字必须真实存在，防清单写成愿望清单。"""
    engine = _engine("spec")
    monster = _monster()
    _enter_combat(engine, monster)
    combat = engine.combat
    ActionPreview(engine).preview("prepare_attack", {})   # 走一遍副本世界，触发懒创建
    for name in LEDGER_NAMES:
        if name in ENTITY_KEYED_LEDGERS or name == "_dodge_round":
            continue
        assert hasattr(combat, name), f"{name} 在清单里但引擎上没有这个名字（改名了？）"


# --------------------------------------------------------------------------
# 二、③-full 的实际收益：怪物阶段回滚后账本必须仍然够得着
# --------------------------------------------------------------------------

def test_monster_phase_rollback_keeps_ledgers_reachable():
    """`_monster_phase_restore` 会**换掉实体对象**（runtime_id 不变）→ id 键账本全部失配。

    实测（改键之前）：回滚后账本 1 条非空条目、能对上在场实体的 0 条，
    狂暴的额外出手从 **2 掉到 1** —— 怪物静默丢失本场已激活的持续效果。
    手操时一次非法的怪物阶段提交就会走到这条路径。
    """
    engine = _engine("rollback")
    monster = _monster("狂暴怪", daowen="狂暴")
    _enter_combat(engine, monster)
    combat = engine.combat
    combat._monster_activated[monster.runtime_id] = {"狂暴"}

    def acts(ent):
        return combat._monster_attack_actions(
            ent, combat._monster_activated.get(ent.runtime_id, set()))

    before = acts(monster)
    assert before == 2, f"夹具没触发狂暴加成（出手数={before}），用例会被 1==1 空过"

    snap = combat._monster_phase_snapshot()
    combat._monster_phase_restore(snap)

    live = engine.state.enemies[0]
    assert live is not monster, "回滚没有换掉实体对象？那本用例的前提变了，请重新核对"
    assert live.runtime_id == monster.runtime_id
    assert acts(live) == before, (
        f"回滚后怪物的狂暴额外出手从 {before} 掉到 {acts(live)}：账本键对不上在场角色"
        f"（id 键时代的实测故障）。键必须是 runtime_id。")
    assert audit_ledgers(combat)["junk_keys"] == 0, "回滚后账本里出现了不在名册的键"
    assert monster.runtime_id in roster_runtime_ids(engine.state)


def test_ledger_entries_survive_deepcopy_of_state():
    """账本键跨 deepcopy 稳定：副本世界的角色仍能被同一本账认出来（预演保真的前提）。"""
    engine = _engine("deepcopy")
    monster = _monster()
    _enter_combat(engine, monster)
    combat = engine.combat
    combat._monster_daowen_round_used[monster.runtime_id] = (2, {"必中"})

    with copy_world(engine) as snap:
        copy_monster = engine.state.enemies[0]
        assert copy_monster is not monster
        assert copy_monster.runtime_id == monster.runtime_id
        # 副本世界**看得见**真实世界的回合内状态（id 键时代这里必然查不到）
        assert combat._monster_round_used(copy_monster) == {"必中"}
        assert snap["copy_state"] is engine.state


# --------------------------------------------------------------------------
# 三、隔离语义：副本拿深拷贝、真实世界按引用原样归还
# --------------------------------------------------------------------------

def test_copy_world_gives_duplicates_and_restores_identity_and_content():
    engine = _engine("world")
    monster = _monster()
    _enter_combat(engine, monster)
    combat = engine.combat
    rid = monster.runtime_id
    combat._monster_activated[rid] = {"必中"}
    combat._dodge_counts = {rid: 1}
    before = _ledger_objects(combat)
    real_state = engine.state

    with copy_world(engine) as snap:
        assert snap["eng_state"] is real_state
        assert engine.state is snap["copy_state"] and combat.state is snap["copy_state"]
        for name in LEDGER_NAMES:
            old = before[name]
            if isinstance(old, (dict, set, list)) and old:
                assert getattr(combat, name) is not old, f"{name} 副本世界没拿到深拷贝"
        # 在副本世界里写脏：这些写入必须在退出时全部作废
        combat._monster_activated[rid] = {"副本里的假记录"}
        combat._dodge_counts[rid] = 9
        combat._monster_daowen_round_used[rid] = (2, {"减速"})

    for name, obj in before.items():
        if obj is not None:
            assert getattr(combat, name) is obj, f"{name} 还原后不是原对象（identity 漂移）"
    assert combat._monster_activated == {rid: {"必中"}}
    assert combat._dodge_counts == {rid: 1}
    assert not combat._monster_daowen_round_used
    assert engine.state is real_state and combat.state is real_state


def test_lazy_ledger_is_not_restored_as_the_shared_default_object():
    """懒初始化的账本（属性还不存在）：还原时不得把清单里的**共享默认对象**挂上去。

    否则 `combat._dodge_counts is DEFAULTS["_dodge_counts"]`，之后任何写入都会污染
    全项目的默认值（下一台引擎一出生就带着上一场的闪避计数）。
    """
    engine = _engine("lazy")
    monster = _monster()
    _enter_combat(engine, monster)
    combat = engine.combat
    for name in ("_dodge_counts", "_dodge_round"):
        if hasattr(combat, name):
            delattr(combat, name)

    with copy_world(engine):
        combat._dodge_counts[monster.runtime_id] = 2      # 副本世界里创建并写入

    assert hasattr(combat, "_dodge_counts")
    assert combat._dodge_counts is not DEFAULTS["_dodge_counts"], (
        "还原成了清单里的共享默认对象，后续写入会污染 DEFAULTS")
    assert DEFAULTS["_dodge_counts"] == {}, "DEFAULTS 已被污染"
    assert combat._dodge_counts == {}


def test_preview_leaves_real_ledgers_untouched():
    """走真实入口（ActionPreview.preview）验一遍：预演前后账本引用与内容都不变。"""
    engine = _engine("preview")
    monster = _monster()
    _enter_combat(engine, monster)
    combat = engine.combat
    combat._monster_activated[monster.runtime_id] = {"必中"}
    before = _ledger_objects(combat)
    contents = {n: copy.deepcopy(v) for n, v in before.items() if v is not None}

    pv = ActionPreview(engine).preview("prepare_attack", {})
    assert pv.get("result") is not None

    for name, obj in before.items():
        if obj is not None:
            assert getattr(combat, name) is obj, f"预演后 {name} 不是原对象"
            assert getattr(combat, name) == contents[name], f"预演后 {name} 内容被改"
    assert engine.state.enemies[0] is monster, "真实世界的怪物对象被换掉了"


def test_dodge_ledgers_are_registered_per_ruling():
    """2026-09-18 用户裁定（选 B）：自动闪避计数与其回合哨兵一并纳入隔离。"""
    assert "_dodge_counts" in LEDGER_NAMES and "_dodge_round" in LEDGER_NAMES
    assert "_dodge_counts" in ENTITY_KEYED_LEDGERS
    assert "_dodge_round" not in ENTITY_KEYED_LEDGERS, "回合哨兵不是实体键"
    field = next(f for f in COMBAT_LEDGERS if f.name == "_dodge_counts")
    assert field.roster_bound is False, (
        "目标中途离场时计数会滞留到回合末，属合法，不能进门禁（否则 L7 会误报）")


def test_dodge_counts_are_isolated_across_preview():
    engine = _engine("dodge")
    monster = _monster()
    _enter_combat(engine, monster)
    combat = engine.combat
    combat._dodge_counts = {monster.runtime_id: 2}
    combat._dodge_round = engine.state.current_round
    real_counts = combat._dodge_counts

    ActionPreview(engine).preview("prepare_attack", {})

    assert combat._dodge_counts is real_counts
    assert combat._dodge_counts == {monster.runtime_id: 2}
    assert combat._dodge_round == engine.state.current_round


# --------------------------------------------------------------------------
# 审计分层：门禁 vs 参考；审计只读
# --------------------------------------------------------------------------

def test_audit_separates_gate_from_reference():
    engine = _engine("audit")
    monster = _monster()
    _enter_combat(engine, monster)
    combat = engine.combat

    clean = audit_ledgers(combat)
    assert clean["junk_keys"] == 0, "干净局就该是 0，否则门禁失去意义"

    combat._monster_activated["f" * 32] = {"垃圾"}     # 键不在名册 → 门禁计数
    combat._monster_evolved.add("e" * 32)              # 键可合法滞留 → 只进参考计数
    result = audit_ledgers(combat)

    assert result["junk_keys"] == 1
    assert result["detail"]["_monster_activated"] == 1
    assert result["unbound_keys"] == 1
    assert "_monster_evolved" in result["detail"]
    assert "f" * 32 in combat._monster_activated, "审计只数不删"


def test_audit_is_read_only():
    engine = _engine("audit_ro")
    monster = _monster()
    _enter_combat(engine, monster)
    combat = engine.combat
    combat._monster_activated[monster.runtime_id] = {"必中"}
    before = copy.deepcopy(combat._monster_activated)
    audit_ledgers(combat)
    assert combat._monster_activated == before


# --------------------------------------------------------------------------
# 口径边界：api.py 的失败事务回滚（另一套口径）必须落在权威清单内
# --------------------------------------------------------------------------

def test_api_rollback_ledgers_are_a_subset_of_the_spec():
    src = _src("engine/api.py")
    m = re.search(r"def _snapshot_combat_runtime.*?def _roll_back_monster_phase_pending",
                  src, re.S)
    assert m, "api.py 的回滚快照函数没找到（改名了？请同步更新本用例）"
    names = set(re.findall(r"self\.combat\.(_[a-z_0-9]+)", m.group(0)))
    assert names, "没解析到任何账本名，断言等于空转"
    assert names <= set(LEDGER_NAMES), (
        f"api.py 的失败事务回滚覆盖了权威清单之外的账本：{sorted(names - set(LEDGER_NAMES))}。"
        f"两套口径可以覆盖范围不同（回滚只 3 项，2026-09-18 只查证未改，见 报告.md D7），"
        f"但不能出现清单不认识的账本名。")
