"""《行动接口口径》整节＝手写正文，但**逐条对引擎核**（①-B-3，用户裁定 2026-09-19＝选案 B）。

为什么这一节不像道纹三节那样直接从引擎生成：四要素（①代价 ②目标要求 ③能否战斗中用
④前置与产出）散在 `engine/api.py` 约 80 处 `action_type == "…"` 的命令式分支里，引擎侧唯一
声明式的事实是 `GameEngine._COMBAT_ONLY_ACTIONS`（31 个名字）与分派表本身。要「从引擎生成」
就得先新建一张 64 条 × 四要素的声明表——那张表与命令式代码是**两处真源**，除非把 api.py
改成读表驱动（大改、风险高），否则只是把「文档漂移」换成「表与代码漂移」。所以选案 B：
正文仍手写，但加一把逐条对引擎的尺，让这一节不能静默漂移。

钉五件事：

1. **覆盖**：引擎分派的 64 个行动接口 ↔ 文档 60 条条文 ＋ 4 条指路（`use_spell`／`define_spell`／
   `prepare_monster_phase`／`resolve_monster_phase` 的四要素写在专题正文里，本节按设计只指路），
   双向不漏不多。
2. **能否战斗中用**：每条条文的声明（自身行优先，其次组标题总声明）必须与 `_COMBAT_ONLY_ACTIONS`
   一致。负向对照（本条存在的理由）：2026-09-19 实测查出 5 条**相反**——震岳龙躯／吞骸龙胃／
   鲜血之翼／血族尖牙／血食 都写「不受战斗限定，不消耗出手」，而引擎把它们列在战斗限定名单里，
   局外调用一律被 `api.py::_phase_error` 拒（实测报文「只能在战斗中执行」）；另有断尾求生
   完全没表态。六处已按引擎更正（用户裁定选案 A）。
3. **四要素齐备**：把本节开头「接口总则」（正文里每条必须说清四件事）机器化；正当缺项
   （纯查询接口没有目标、停用入口恒失败）登记进 `ELEMENT_EXEMPT` 并写明理由。
4. **停用入口真的停用**：`attack`／`monster_phase` 必须恒返回失败并指路新入口——文档说停用
   不算数，行为上停用才算。
5. **没有幽灵条文**：文档立条的名字引擎必须真的分派（防改名后正文留下死条目，AI 照着调必失败）。
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.api import GameEngine  # noqa: E402

DOC = ROOT / "AI_EXPERIENCE.md"
API = ROOT / "engine" / "api.py"
SECTION_HEAD = "### 行动接口口径"

# 四要素写在专题正文、本节按设计只指路的 4 个（见本节末组标题）
POINTER_ONLY = {
    "use_spell": "《局外系统》「装配一种法术」＋《法术索引》",
    "define_spell": "《局外系统》「自创一种法术」＋《法术索引》的法术设计原则",
    "prepare_monster_phase": "《怪物准则》第 11~12 条（出手预算与道纹预留）",
    "resolve_monster_phase": "《怪物准则》第 11~12 条（两段式快照契约）",
}
# 已停用／已移除的旧入口：恒返回失败，四要素与相位门禁对它无意义（但仍要指路新入口）
RETIRED = {"attack", "monster_phase"}

# 正当缺项（必须写理由；理由空着即红）
ELEMENT_EXEMPT: dict[str, dict[str, str]] = {
    "read_death_book": {"②目标要求": "纯查询接口：翻书返回全部〖遗言〗，不选目标、不改数值"},
    "attack": {"①代价": "已停用入口：恒返回失败并指路 prepare_attack/resolve_attack",
               "②目标要求": "同上，没有目标可选",
               "③能否战斗中用": "同上，相位门禁走不到（先被恒失败拦下）"},
    "monster_phase": {"③能否战斗中用": "已停用入口：恒返回失败并指路两段式怪物阶段"},
}

TOP = re.compile(r"^-\s+\*\*[^（(]*[（(]`([a-z_0-9]+)`[）)]\*\*[：:]")   # - **标题（`name`）**：…
SUB = re.compile(r"^\s+-\s+`([a-z_0-9]+)`(?:[（(][^）)]*[）)])?[：:]")     #   - `name`（别名）：…
# 「能否战斗中用」的两种说法；FREE 必须先判——「不受战斗限定」里就含着「战斗限定」四个字
FREE = ("不受战斗限定", "不作战斗限定", "战斗内外均可", "限局外", "局外也能", "局外唯一入口")
COMBAT = ("战斗限定", "只能在战斗中", "_COMBAT_ONLY_ACTIONS` 名单内", "战斗内主动行动")
# ②目标要求：目标类词，或任何一个参数约束（`param` 只能是/必须/非空/缺省/…）
TARGET_WORDS = ("target_ref", "monster_ref", "ally_ref", "actor_ref", "employee_ref", "目标", "自身",
                "无目标", "[员工]", "[朋友]", "敌方", "敌人", "怪物", "候选", "玩家", "守擂", "挑战者")
PARAM_RULE = re.compile(r"`[a-z_0-9]+`\s*(?:只能是|必须|非空|缺省|用|形如|按|记为|相同即拒绝)")
ELEMENTS = {
    "①代价": lambda t: any(k in t for k in (
        "代价", "消耗", "不消耗出手", "无代价", "不花", "限一次", "每场限", "预算", "耐久",
        "点精力", "工资", "负债", "碎片", "龙性", "流血", "衰老", "枯竭", "萎缩", "精力")),
    "②目标要求": lambda t: any(k in t for k in TARGET_WORDS) or bool(PARAM_RULE.search(t)),
    "④前置与产出": lambda t: any(k in t for k in (
        "前置", "产出", "后置", "登记", "获得", "拒绝", "恒返回", "结算", "授予", "清空", "返回")),
}


def _section() -> list[str]:
    lines = DOC.read_text(encoding="utf-8").splitlines()
    head = next(i for i, l in enumerate(lines) if l.startswith(SECTION_HEAD))
    end = next(i for i in range(head + 1, len(lines)) if lines[i].startswith("## "))
    return lines[head + 1:end]


def _entries() -> list[tuple[str, str, str]]:
    """(接口名, 条文行, 所属组标题)；组标题的总声明由调用方按继承规则用。"""
    out: list[tuple[str, str, str]] = []
    group = ""
    for line in _section():
        if line.startswith("- "):
            group = line.strip()
            m = TOP.match(line)
            if m:
                out.append((m.group(1), line.strip(), group))
            continue
        m = SUB.match(line)
        if m:
            out.append((m.group(1), line.strip(), group))
    return out


def _claim(text: str) -> str | None:
    for k in FREE:
        if k in text:
            return "free"
    for k in COMBAT:
        if k in text:
            return "combat"
    return None


def _engine_interfaces() -> set[str]:
    """引擎分派的行动接口＝api.py 里所有与 `action_type` 比较的字面量（实测 64 个）。

    不用 `action == "…"`：那是 handler 内部的子动作值（`approve`/`reject` 属事件/雇佣的
    二级选择），不是对外接口，算进来会把覆盖守卫的基数搞错。
    """
    src = API.read_text(encoding="utf-8")
    names = set(re.findall(r'action_type\s*==\s*"([a-z_0-9]+)"', src))
    names |= set(GameEngine._COMBAT_ONLY_ACTIONS)     # 名单里的名字也必须被分派
    return names


# ------------------------------------------------------------------ 1) 覆盖

def test_every_engine_interface_is_documented():
    """引擎的 64 个接口，文档要么立条、要么在末组指路；一个都不能凭空消失。"""
    documented = {n for n, _, _ in _entries()}
    engine = _engine_interfaces()
    missing = sorted(engine - documented - set(POINTER_ONLY))
    assert not missing, (
        f"这些行动接口在引擎里分派、但《行动接口口径》没写条文（AI 只能靠猜）：{missing}\n"
        "修：给每条补四要素条文；若四要素写在专题正文里，就加进本文件的 POINTER_ONLY 并写明去处。")
    assert len(engine) == 64, f"引擎接口数变了（{len(engine)}）：新增/删除接口要同步更新本节与本守卫"


def test_pointer_only_interfaces_are_actually_pointed_at():
    """指路名单不能变成「哪儿都没写」的垃圾桶：这四个名字必须真的出现在末组指路句里。"""
    text = "\n".join(_section())
    for name, where in POINTER_ONLY.items():
        assert f"`{name}`" in text, f"{name} 声称写在专题正文，但本节连指路都没有"
        assert name not in {n for n, _, _ in _entries()}, f"{name} 已在本节立条，别再挂在 POINTER_ONLY"
        assert where.split("》")[0].lstrip("《") in (ROOT / "AI_EXPERIENCE.md").read_text(encoding="utf-8"), (
            f"{name} 的指路去处《{where}》在正文里找不到")


def test_no_ghost_entries():
    """反向：文档立条的名字引擎必须真的分派（改名后留下死条目，AI 照着调必失败）。"""
    engine = _engine_interfaces()
    ghosts = sorted(n for n, _, _ in _entries() if n not in engine)
    assert not ghosts, f"这些条文对应的接口引擎已经不分派了：{ghosts}"


def test_entries_are_unique():
    """一个接口只立一条：两处条文迟早互相矛盾（本节的存在理由就是消灭两处口径）。"""
    names = [n for n, _, _ in _entries()]
    dup = sorted({n for n in names if names.count(n) > 1})
    assert not dup, f"重复立条：{dup}"


# ------------------------------------------------------------------ 2) 能否战斗中用

def test_combat_only_claims_match_engine():
    """每条条文的「能否战斗中用」必须与 `_COMBAT_ONLY_ACTIONS` 一致（自身行优先，其次组标题总声明）。

    负向对照见模块 docstring 第 2 条：5 条相反＋1 条没表态，都是 2026-09-19 用这把尺查出来的。
    """
    combat = set(GameEngine._COMBAT_ONLY_ACTIONS)
    wrong, silent = [], []
    for name, line, group in _entries():
        said = _claim(line) or _claim(group)
        actual = "combat" if name in combat else "free"
        if said is None:
            if name not in RETIRED:
                silent.append((name, group[:40]))
        elif said != actual:
            wrong.append((name, said, actual, line[:70]))
    assert not wrong, (
        "文档的「能否战斗中用」与引擎 `_COMBAT_ONLY_ACTIONS` 相反（AI 会在错误的时机调用而被拒）：\n"
        + "\n".join(f"  {n}: 文档={s} 引擎={a}\n      {l}" for n, s, a, l in wrong))
    assert not silent, (
        "这些条文没写「能否战斗中用」（四要素③缺项，组标题也没有总声明）：\n"
        + "\n".join(f"  {n}（组：{g}）" for n, g in silent))


def test_combat_only_names_are_all_real_interfaces():
    """名单本身也不能腐坏：`_COMBAT_ONLY_ACTIONS` 里的名字必须都是真接口（否则门禁永不生效）。"""
    dispatched = set(re.findall(r'action_type\s*==\s*"([a-z_0-9]+)"', API.read_text(encoding="utf-8")))
    stray = sorted(set(GameEngine._COMBAT_ONLY_ACTIONS) - dispatched)
    assert not stray, f"战斗限定名单里有引擎不分派的名字（写错了或接口已删）：{stray}"


# ------------------------------------------------------------------ 3) 四要素齐备

def test_every_entry_states_the_four_elements():
    """接口总则机器化：每条条文都要能看出①代价②目标要求③能否战斗中用④前置与产出。

    正当缺项登记在 `ELEMENT_EXEMPT`（必须写理由）。判据是关键词/参数约束式的粗筛——
    它抓的是「整条什么都没提」（例如新接口只写了一句效果），不追求语义精确。
    """
    lacking: dict[str, list[str]] = {}
    for name, line, group in _entries():
        text = f"{line} {group}"
        miss = [k for k, f in ELEMENTS.items() if not f(text)]
        if _claim(text) is None and name not in RETIRED:
            miss.append("③能否战斗中用")
        exempt = ELEMENT_EXEMPT.get(name, {})
        miss = [m for m in miss if m not in exempt]
        if miss:
            lacking[name] = miss
    assert not lacking, (
        "这些条文缺四要素（缺项会让 AI 少算代价或选错目标）：\n"
        + "\n".join(f"  {n}: 缺 {'/'.join(v)}" for n, v in sorted(lacking.items()))
        + "\n修：补齐条文；确实不适用的（纯查询/停用入口）登记进 ELEMENT_EXEMPT 并写理由。")
    assert all(v.strip() for e in ELEMENT_EXEMPT.values() for v in e.values()), "豁免必须写理由"
    # 豁免不能白挂着：条文若已补齐，就从表里删掉（与措辞守卫的 stale 检查同款）
    detectors = dict(ELEMENTS)
    detectors["③能否战斗中用"] = lambda t: _claim(t) is not None
    stale = []
    for name, exempt in ELEMENT_EXEMPT.items():
        hit = next(((line, group) for n, line, group in _entries() if n == name), None)
        if hit is None:
            stale.append(f"{name}（本节已无此条文）")
            continue
        text = f"{hit[0]} {hit[1]}"
        stale += [f"{name}·{element}" for element in exempt if detectors[element](text)]
    assert not stale, f"这些豁免已经用不上了（条文已补齐），请从 ELEMENT_EXEMPT 删掉：{stale}"


# ------------------------------------------------------------------ 4) 停用入口真的停用

@pytest.mark.parametrize("name", sorted(RETIRED))
def test_retired_entries_fail_and_point_the_way(name):
    """文档说「恒返回失败」不算数：两个相位都要失败，且战斗中的报文要指路新入口（AI 靠报文自纠）。

    分两个相位是因为它们同时在 `_COMBAT_ONLY_ACTIONS` 里：局外调用先被 `api.py::_phase_error`
    拦下（报文是「只能在战斗中执行」，指路信息根本出不来）。实测 2026-09-19：
      局外    → 【attack】只能在战斗中执行
      战斗中  → 旧attack已移除；请使用prepare_attack/resolve_attack
    """
    outside = GameEngine().execute_action(name, {})
    assert outside.get("success") is False, f"{name} 在局外竟然成功了：正文说它已停用"

    engine = GameEngine()
    engine.state.phase = "in_combat"      # 只为越过相位门禁，让停用入口自己的报文露出来
    inside = engine.execute_action(name, {})
    assert inside.get("success") is False, f"{name} 在战斗中竟然成功了：正文说它已停用"
    text = str(inside.get("message") or inside.get("error") or inside.get("note") or "")
    assert "已移除" in text or "已停用" in text, f"{name} 的报文没说自己停用了：{text[:120]}"
    assert "prepare_" in text or "依次调用" in text, (
        f"{name} 的失败报文没有指路新入口（AI 会不知道该改调什么）：{text[:120]}")
    entry = next((l for n, l, _ in _entries() if n == name), None)
    assert entry and ("已移除" in entry or "恒返回" in entry), f"{name} 的条文没说它恒失败"
