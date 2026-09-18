"""账本隔离清单的**单一权威**（`engine/ledger_isolation.py`）守卫测试。

2026-09-18 用户裁定「从小到大改，先做最小的改动」→ ③ 的最小切片：把「哪些战斗运行态账本
必须在副本世界（AI 预演／死斗推演）里隔离」从**三处手抄**收敛成一处。此前
`engine/ai_preview.py`（`ledger_defaults`，7 本）、`sim/win_only_ai.py`（同一批字段存一遍＋
还一遍，写两次，漏字段不报错只静默漂移）、`sim/diag_repro.py`（L7 审计只抄到 3 本）各有一份；
三处不同步 ＝「新加一本账忘了登记」这个坑还有三次机会重现——2026-09-18 那次
「同 seed 两次跑不一致」（`test_build_learner::test_fixed_seed_is_reproducible`）正是这么来的。

本文件守「收敛成一处」这件事本身：

1. 三个调用点都引用权威清单，且手抄痕迹不再出现（防重新分叉）；
2. **完备性**：真实战斗后 `CombatEngine` 上任何「id 形态键」的容器都必须已登记（或显式豁免）；
3. 隔离语义：进副本世界拿到的是深拷贝，出来时**引用与内容都原样归还**（identity restore）；
4. 懒初始化的账本不得被还原成清单里的**共享默认对象**（否则之后的写入会污染全项目默认值）；
5. 裁定项落地：`_dodge_counts`/`_dodge_round` 已登记（用户选 B：一并纳入隔离）；
6. 审计分层：门禁只数「键必须始终在场」的账本，键可合法滞留的只进参考计数；
7. 口径边界：`engine/api.py` 的失败事务回滚清单是另一套口径（id → `(kind, index)` 稳定引用、
   只覆盖 3 项；2026-09-18 只查证、未改动），但其覆盖项必须在权威清单内，防两边彻底分叉。
"""
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
from engine.ledger_isolation import (COMBAT_LEDGERS, DEFAULTS, ID_KEYED_LEDGERS,
                                     LEDGER_NAMES, ROSTER_BOUND_LEDGERS,
                                     audit_id_ledgers, copy_world)
from engine.monsters import make_monster_entity

DaoWenEngine.register_all()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 「id 形态」的判定：CPython 的对象地址是很大的整数（~1e14），而道纹 X 值、回合数这类
# 真·整数键都很小。用 10**6 划线，避免把 {1: …} 这类 X 值映射误判成实体账本。
ID_LIKE = 10 ** 6

# 运行时扫描的豁免表（键是 id 形态但确认不需隔离）；静态扫描的豁免见 EXEMPT_CONTAINERS。
EXEMPT: dict[str, str] = {}


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


def _monster(name="靶怪"):
    return make_monster_entity({
        "name": name, "blood_limit": 222, "mana_limit": 7, "speed_limit": 3,
        "attack_count": 3, "attack_power": 7,
        "dao_wen": {"必中": None}, "region": "扭曲都市",
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
# 1) 单一权威：三个调用点都引用它，手抄痕迹不得复活
# --------------------------------------------------------------------------

def test_all_three_call_sites_reference_the_single_source():
    for rel in ("engine/ai_preview.py", "sim/win_only_ai.py", "sim/diag_repro.py"):
        assert "ledger_isolation" in _src(rel), (
            f"{rel} 必须引用 engine/ledger_isolation.py 的权威清单；"
            f"自己再列一遍账本名＝重新分叉，就是本轮要消灭的那个坑")


def test_hand_copied_lists_are_gone():
    ap = _src("engine/ai_preview.py")
    assert "ledger_defaults" not in ap, "ai_preview 里手抄的 ledger_defaults 已删，不要写回来"
    assert "_monster_activated" not in ap.replace("ledger_isolation", ""), (
        "ai_preview 不该再逐本点名账本（清单只在 ledger_isolation.py）")
    woa = _src("sim/win_only_ai.py")
    assert 'combat._monster_activated = copy.deepcopy' not in woa, (
        "win_only_ai 里「存一遍＋还一遍」的手抄账本清单已删，不要写回来")
    assert "_resonance_rewrites" not in woa, "win_only_ai 不该再逐本点名账本"
    dr = _src("sim/diag_repro.py")
    assert not re.search(r'^LEDGERS = \(', dr, re.M), (
        "diag_repro 里手抄的 LEDGERS 三元组已删，改用 ID_KEYED_LEDGERS")
    assert "ID_KEYED_LEDGERS" in dr


def test_spec_entries_are_self_documenting():
    for field in COMBAT_LEDGERS:
        assert field.name.startswith("_") and field.note.strip(), f"{field.name} 缺说明"
    # 门禁集合（键必须始终在场）至少包含最初那三本；扩它要有意识地改这里
    assert {"_monster_activated", "_monster_daowen_round_used",
            "_resonance_rewrites"} <= set(ROSTER_BOUND_LEDGERS)
    assert set(ROSTER_BOUND_LEDGERS) <= set(ID_KEYED_LEDGERS) <= set(LEDGER_NAMES)


# --------------------------------------------------------------------------
# 2) 完备性：任何新建的账本都必须登记（静态扫描为主，运行时扫描为辅）
# --------------------------------------------------------------------------

# 在 CombatEngine 上创建容器型运行态的赋值语句（`self._x = {}` / `set()` / `[]` / defaultdict）
_CONTAINER_ASSIGN = re.compile(
    r"self\.(_[a-z_0-9]+)\s*=\s*(?:\{\}|\[\]|set\(\)|dict\(\)|list\(\)|"
    r"(?:collections\.)?defaultdict\(|OrderedDict\()")

# 若将来出现「容器型运行态但不是需要隔离的账本」（例如按道纹名索引的缓存），
# 在这里登记名字＋理由；不要直接放宽断言。当前为空：combat.py 里创建的容器**全部**是账本。
EXEMPT_CONTAINERS: dict[str, str] = {}


def test_every_container_ledger_created_in_combat_is_registered():
    """**静态**扫 `engine/combat.py`：凡在 CombatEngine 上创建的容器都必须已登记。

    为什么用静态扫描而不是跑一局再看：新加的账本在多数局面里是**空的**（例如
    `_resonance_rewrites` 一整局脚本轮回都没被写过），运行时扫描看不出键类型、
    也就抓不到「忘了登记」——而空账本恰恰是刚新增时的常态。这条用例是本轮
    「三处手抄清单」收敛成一处之后，防止第四处/新账本再次脱管的主门禁。
    """
    src = _src("engine/combat.py")
    created = set(_CONTAINER_ASSIGN.findall(src))
    assert created, "没扫到任何容器创建点，正则失效＝断言空转"
    # 已知的 5 本容器账本必须在扫描结果里（防正则漏扫导致门禁形同虚设）
    assert {"_monster_activated", "_monster_daowen_round_used", "_resonance_rewrites",
            "_monster_evolved", "_dodge_counts"} <= created, (
        f"正则漏扫了已知账本：{sorted(created)}")
    missing = created - set(LEDGER_NAMES) - set(EXEMPT_CONTAINERS)
    assert not missing, (
        f"combat.py 新建了未登记的容器型运行态：{sorted(missing)}。"
        f"请加进 engine/ledger_isolation.COMBAT_LEDGERS（预演/死斗推演隔离与 L7 审计"
        f"会同时生效），或确认它不需要隔离后登记进 EXEMPT_CONTAINERS 并写明理由。")


def test_runtime_id_keyed_containers_are_registered():
    """**运行时**辅门禁：跑一局真实脚本轮回后，扫 `vars(combat)` 里 id 形态键的容器。

    抓的是静态扫描抓不到的情况——账本由别的模块挂上去、或键在运行期才变成 id。
    空容器在这里天然看不见（无法判断键类型），所以它是辅、上面那条是主。
    """
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
    offenders = []
    for combat in seen:
        for name, val in vars(combat).items():
            if isinstance(val, dict):
                keys = list(val.keys())
            elif isinstance(val, (set, frozenset)):
                keys = list(val)
            else:
                continue
            if not keys or not all(isinstance(k, int) and k > ID_LIKE for k in keys):
                continue          # 空容器看不出键类型；小整数键是 X 值/回合数一类，不是实体账本
            if name not in LEDGER_NAMES and name not in EXEMPT:
                offenders.append(f"{name}（{len(keys)} 个 id 形态键）")
    assert not offenders, (
        "CombatEngine 上出现了未登记的 id 键账本：" + "、".join(sorted(set(offenders))) +
        "。登记方式同 test_every_container_ledger_created_in_combat_is_registered。")


def test_scalars_in_spec_still_exist_on_combat():
    """清单里的标量账本（非容器）名字必须真实存在，防清单写成愿望清单。"""
    engine = _engine("spec")
    monster = _monster()
    _enter_combat(engine, monster)
    combat = engine.combat
    ActionPreview(engine).preview("prepare_attack", {})   # 走一遍副本世界，触发懒创建
    for name in LEDGER_NAMES:
        if name in ID_KEYED_LEDGERS or name == "_dodge_round":
            continue
        assert hasattr(combat, name), f"{name} 在清单里但引擎上没有这个名字（改名了？）"


# --------------------------------------------------------------------------
# 3)+4) 隔离语义：副本拿深拷贝、真实世界按引用原样归还
# --------------------------------------------------------------------------

def test_copy_world_gives_duplicates_and_restores_identity_and_content():
    engine = _engine("world")
    monster = _monster()
    _enter_combat(engine, monster)
    combat = engine.combat
    combat._monster_activated[id(monster)] = {"必中"}
    combat._dodge_counts = {id(monster): 1}
    before = _ledger_objects(combat)
    real_state = engine.state

    with copy_world(engine) as snap:
        assert snap["eng_state"] is real_state
        assert engine.state is snap["copy_state"] and combat.state is snap["copy_state"]
        # 容器型账本在副本世界里必须是**另一个对象**，否则副本会写脏真实账本
        for name in LEDGER_NAMES:
            old = before[name]
            if isinstance(old, (dict, set, list)) and old:
                assert getattr(combat, name) is not old, f"{name} 副本世界没拿到深拷贝"
        # 在副本世界里写脏：这些写入必须在退出时全部作废
        combat._monster_activated[id(monster)] = {"副本里的假记录"}
        combat._dodge_counts[id(monster)] = 9
        combat._monster_daowen_round_used[id(monster)] = ("减速",)

    for name, obj in before.items():
        if obj is not None:
            assert getattr(combat, name) is obj, f"{name} 还原后不是原对象（identity 漂移）"
    assert combat._monster_activated == {id(monster): {"必中"}}
    assert combat._dodge_counts == {id(monster): 1}
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
        combat._dodge_counts[id(monster)] = 2      # 副本世界里创建并写入

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
    combat._monster_activated[id(monster)] = {"必中"}
    before = _ledger_objects(combat)
    contents = {n: copy.deepcopy(v) for n, v in before.items() if v is not None}

    pv = ActionPreview(engine).preview("prepare_attack", {})
    assert pv.get("result") is not None

    for name, obj in before.items():
        if obj is not None:
            assert getattr(combat, name) is obj, f"预演后 {name} 不是原对象"
            assert getattr(combat, name) == contents[name], f"预演后 {name} 内容被改"
    assert engine.state.enemies[0] is monster, "真实世界的怪物对象被换掉了"


# --------------------------------------------------------------------------
# 5) 裁定项：_dodge_counts / _dodge_round 已纳入（用户选 B）
# --------------------------------------------------------------------------

def test_dodge_ledgers_are_registered_per_ruling():
    assert "_dodge_counts" in LEDGER_NAMES and "_dodge_round" in LEDGER_NAMES, (
        "2026-09-18 用户裁定（选 B）：自动闪避计数与其回合哨兵一并纳入隔离")
    assert "_dodge_counts" in ID_KEYED_LEDGERS
    assert "_dodge_round" not in ID_KEYED_LEDGERS, "回合哨兵不是 id 键"
    field = next(f for f in COMBAT_LEDGERS if f.name == "_dodge_counts")
    assert field.roster_bound is False, (
        "目标中途离场时计数会滞留到回合末，属合法，不能进门禁（否则 L7 会误报）")


def test_dodge_counts_are_isolated_across_preview():
    engine = _engine("dodge")
    monster = _monster()
    _enter_combat(engine, monster)
    combat = engine.combat
    combat._dodge_counts = {id(monster): 2}
    combat._dodge_round = engine.state.current_round
    real_counts = combat._dodge_counts

    ActionPreview(engine).preview("prepare_attack", {})

    assert combat._dodge_counts is real_counts
    assert combat._dodge_counts == {id(monster): 2}
    assert combat._dodge_round == engine.state.current_round


# --------------------------------------------------------------------------
# 6) 审计分层：门禁 vs 参考
# --------------------------------------------------------------------------

def test_audit_separates_gate_from_reference():
    engine = _engine("audit")
    monster = _monster()
    _enter_combat(engine, monster)
    combat = engine.combat

    assert audit_id_ledgers(combat)["junk_keys"] == 0, "干净局就该是 0，否则门禁失去意义"

    combat._monster_activated[ID_LIKE + 1] = {"垃圾"}     # 键不在名册 → 门禁计数
    combat._monster_evolved.add(ID_LIKE + 2)              # 键可合法滞留 → 只进参考计数
    result = audit_id_ledgers(combat)

    assert result["junk_keys"] == 1
    assert result["detail"]["_monster_activated"] == 1
    assert result["unbound_keys"] == 1
    assert "_monster_evolved" in result["detail"]
    # 在场怪的键不受影响（审计只数不删）
    assert ID_LIKE + 1 in combat._monster_activated


def test_audit_is_read_only():
    engine = _engine("audit_ro")
    monster = _monster()
    _enter_combat(engine, monster)
    combat = engine.combat
    combat._monster_activated[id(monster)] = {"必中"}
    before = copy.deepcopy(combat._monster_activated)
    audit_id_ledgers(combat)
    assert combat._monster_activated == before


# --------------------------------------------------------------------------
# 7) 口径边界：api.py 的失败事务回滚清单（另一套口径）必须落在权威清单内
# --------------------------------------------------------------------------

def test_api_rollback_ledgers_are_a_subset_of_the_spec():
    src = _src("engine/api.py")
    m = re.search(r"def _snapshot_combat_runtime.*?def _roll_back_monster_phase_pending",
                  src, re.S)
    assert m, "api.py 的回滚快照函数没找到（改名了？请同步更新本用例）"
    names = set(re.findall(r"self\.combat\.(_[a-z_]+)", m.group(0)))
    assert names, "没解析到任何账本名，断言等于空转"
    assert names <= set(LEDGER_NAMES), (
        f"api.py 的失败事务回滚覆盖了权威清单之外的账本：{sorted(names - set(LEDGER_NAMES))}。"
        f"两套口径可以覆盖范围不同（回滚只 3 项，2026-09-18 只查证未改），"
        f"但不能出现清单不认识的账本名。")
