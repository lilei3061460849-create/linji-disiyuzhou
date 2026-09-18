#!/usr/bin/env python3
"""改一条规则/道纹之后的一键自检（2026-09-18 沉淀，取代「等 4–5 分钟全量回归」）。

四步，任一步失败即以非零码退出（可以当提交前门禁）：

  1. **索引同步**：重跑生成器 `sim/gen_daowen_index.py`，比对 `全道纹索引.md` 前后哈希。
     变了＝你改了引擎口径却忘了重生成派生索引（索引是注入 AI 提示词的文档，必须与引擎同源；
     `tests/test_daowen_cost_consistency.py` 会逐字节守卫它）。工具会替你把索引重生成好，
     记得 `git add`。
  2. **守卫测试子集**（约 3 秒，代替全量）：道纹代价一致性／文档一致性／正文政策／
     裁定回归／道纹接线／引擎口径／怪物阶段顺序／护卫／员工经济。`--full` 换成整个 `tests/`。
  3. **废案扫描**：把一组「已废止口径」的关键句在**活语料**里逐行扫
     （`AI_EXPERIENCE.md`／`全道纹索引.md`／`README.md`／各索引／`死者之书.md`／`副本/*.md`／
     `data/build_knowledge.json`／`报告.md`），命中且该行**不含否定语境词**（沿用
     `sim/kb_regression.py::NEG`：已删除/已废止/作废/已并入/改名/原【/留痕/过期/替代/已删）
     即报警。新废止一条口径就往 `STALE_PHRASES` 加一行——这是「怕改回去」的机器守卫，
     与正文《已删内容》表配套（表给人读，这里给机器跑）。
  4. **口径速览**：打印原始怪物道纹在 X=1 的**引擎真实代价**（`DaoWenEngine.resolve`），
     便于与正文/索引肉眼对齐；`--daowen 必中 --x-to 9` 打一条道纹的整档曲线。

跑法：

    PYTHONPATH=. .venv/bin/python sim/check_rule_change.py
    PYTHONPATH=. .venv/bin/python sim/check_rule_change.py --daowen 必中 --x-to 9
    PYTHONPATH=. .venv/bin/python sim/check_rule_change.py --skip-tests      # 只查文档/口径
    PYTHONPATH=. .venv/bin/python sim/check_rule_change.py --full           # 全量回归（4–5 分钟）
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

INDEX = "全道纹索引.md"
GENERATOR = "sim/gen_daowen_index.py"

GUARD_TESTS = [
    "tests/test_daowen_cost_consistency.py",
    "tests/test_f7_doc_consistency.py",
    "tests/test_ai_experience_policy.py",
    "tests/test_ai_policy.py",
    "tests/test_rulings_2026_09_16.py",
    "tests/test_daowen_wiring.py",
    "tests/test_engine.py",
    "tests/test_monster_phase_order.py",
    "tests/test_monster_daowen_x_free.py",
    "tests/test_guard_command.py",
    "tests/test_employee_economy.py",
    "tests/test_xijie_and_bizhong.py",
    "tests/test_ledger_isolation.py",
]
# 注：「同 seed 可复现」不在子集里（那条用例单跑约 29s，会把 7 秒的自检拖成半分钟）；
# 要验复现性跑 sim/diag_repro.py（--quick 更快），它顺带做 L7 账本垃圾键审计。

# 活语料分两层（archive/** 是历史档案，一律不扫）：
#   硬层＝规则事实源与注入 AI 提示词的派生文档，命中废案即**门禁失败**；
#   软层＝工作日志类（报告.md 记沿革、机制迁移台账.md 记已回撤），命中只**警告**——
#         这两处本来就允许写旧口径（正文红线只约束规则正文），要求每行都带否定语境词不现实。
CORPUS = [
    "AI_EXPERIENCE.md", INDEX, "README.md", "副本索引.md", "物品索引.md", "法术索引.md",
    "死者之书.md", "data/build_knowledge.json",
]
CORPUS_SOFT = ["报告.md", "机制迁移台账.md"]
CORPUS_GLOBS = ["副本/*.md"]

# 2026-09-18 全量实测的 9 条既有失败（AI/mock 类，见 报告.md 第二节）——不是回归，
# 不计入门禁；哪天它们绿了，本工具会提示从名单里删掉。
KNOWN_BASELINE = {
    "tests/test_ai_basic_attack_candidate.py::test_at_one_by_one_daowen_still_wins",
    "tests/test_ai_tactics.py::test_ai_can_declare_parry_under_lethal_threat",
    "tests/test_build_learner.py::test_valid_and_invalid_are_separated",
    "tests/test_monster_daowen_x_free.py::test_heal_x_scales_with_missing_hp",
    "tests/test_unified_ai.py::test_ai_player_is_the_combat_and_high_level_entrypoint",
    "tests/test_win_only_ai.py::test_win_only_includes_parry_in_real_candidate_path",
    "tests/test_win_only_ai.py::test_collapse_line_veto_rejects_mutation_suicide",
    "tests/test_win_only_ai.py::test_collapse_line_veto_allows_safe_cast",
    "tests/test_win_only_ai.py::test_survival_tiebreak_prefers_later_death",
}

# (同一行必须同时出现的子串元组, 这条口径为什么已废)
STALE_PHRASES: list[tuple[tuple[str, ...], str]] = [
    (("累加+2×副本阶级",), "怪物道纹递增 2026-09-16 废止（现行＝面板不写X、发动方自选）"),
    (("X+2×副本阶级",), "同上"),
    (("每实际发动一次",), "同上"),
    (("道纹递增",), "同上"),
    (("家族税",), "怪物家族税 2026-09-18 删除（只按道纹自身正文代价支付）"),
    (("仅首次发动支付异变5X",), "家族税废案陈述"),
    # 正则：同行里「必中」与「异变5X」挨得很近才算命中——现行索引那一行是
    # 「狂暴/全力/疯狂/减速/飞行＝【异变5X】，必中＝【异变X】」，两个子串同行但不相邻，
    # 用纯子串会误报（这就是本工具自己踩的第一个坑，故支持正则）。
    (r"必中.{0,14}异变\s*5X", "【必中】代价 2026-09-18 降为 异变X"),
    (("护卫不占",), "护卫 2026-09-18 起算[朋友]/[员工]当回合的一次出手"),
    (("不占盟友出手",), "同上"),
    (r"净化.{0,12}消耗\s*5X", "【净化X】现行＝消耗2X"),
    (("自愈", "血限10X%"), "【自愈X】2026-09-18 重做＝冷却X＋恢复25X%已损生命"),
    (("减速", "速度减半"), "【减速X】2026-09-18 重做＝失去当前速度的10X%"),
    (("攻击次数/3",), "微光者出手＝攻击次数/3 已废止（全体固定 2 次出手）"),
    (("钱袋",), "已删遗物钱袋"),
    (("双倍计入",), "癌变双倍计入已删除"),
    (("【领悟】",), "局外行动【领悟】2026-09-10 删除"),
]


def _files(soft: bool = False) -> list[Path]:
    names = CORPUS_SOFT if soft else CORPUS
    out = [ROOT / f for f in names if (ROOT / f).exists()]
    if not soft:
        for g in CORPUS_GLOBS:
            out.extend(sorted(ROOT.glob(g)))
    return out


def step_index(args) -> bool:
    print("=" * 78)
    print("1) 索引同步（重跑生成器，比对前后哈希）")
    path = ROOT / INDEX
    before = hashlib.md5(path.read_bytes()).hexdigest() if path.exists() else ""
    r = subprocess.run([sys.executable, GENERATOR], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"   ✗ 生成器失败：{r.stderr.strip()[-400:]}")
        return False
    after = hashlib.md5(path.read_bytes()).hexdigest()
    if before == after:
        print(f"   ✓ {INDEX} 与引擎同源（无变化）")
        return True
    print(f"   ✗ {INDEX} 被重生成改动 → 你改了引擎口径但没重生成索引；已替你生成好，记得 git add")
    d = subprocess.run(["git", "diff", "--stat", "--", INDEX], cwd=ROOT,
                       capture_output=True, text=True).stdout.strip()
    if d:
        print(f"     {d}")
    return args.strict is False


def step_tests(args) -> bool:
    """跑守卫测试；**基线既有失败不算红**（否则这个门禁永远是红的，等于没有门禁）。"""
    print("=" * 78)
    targets = ["tests/"] if args.full else GUARD_TESTS
    print(f"2) 守卫测试{'（全量，4–5 分钟）' if args.full else f'（子集 {len(targets)} 文件）'}")
    r = subprocess.run([sys.executable, "-m", "pytest", *targets, "-q", "--tb=line"],
                       cwd=ROOT, capture_output=True, text=True)
    lines = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
    failed = set()
    for ln in lines:
        if ln.startswith("FAILED "):
            failed.add(ln[len("FAILED "):].split(" - ")[0].strip())
    summary = next((ln for ln in reversed(lines) if " passed" in ln or " failed" in ln
                    or " error" in ln), "")
    print(f"   {summary}")

    scoped = {t for t in KNOWN_BASELINE
              if args.full or t.split("::")[0] in targets}
    new = sorted(failed - KNOWN_BASELINE)
    healed = sorted(scoped - failed)
    for t in sorted(failed & KNOWN_BASELINE):
        print(f"   · 基线既有失败（不计入门禁）：{t}")
    for t in healed:
        print(f"   · 基线用例这回绿了，可从 KNOWN_BASELINE 删掉：{t}")
    if new:
        print(f"   ✗ 基线之外的新失败 {len(new)} 条：")
        for t in new:
            print(f"     {t}")
        for ln in lines[-8:]:
            if not ln.startswith("FAILED "):
                print("     " + ln)
        return False
    if r.returncode != 0 and not failed:
        print(f"   ✗ pytest 非零退出但没有 FAILED 行（收集错误？）：{lines[-3:]}")
        return False
    print("   ✓ 无基线之外的新失败")
    return True


def step_stale(args) -> bool:
    print("=" * 78)
    print("3) 废案扫描（活语料逐行；带否定语境词的行放过）")
    from sim.kb_regression import NEG
    # 比 kb_regression 更宽的否定语境：正文《已删内容》表与派生索引里写「用户令废止／降为／
    # 重做」这类**动词原形**也是在声明旧口径已废，不该被当成污染。
    neg = tuple(NEG) + ("废止", "撤销", "回撤", "降为", "改为", "重做", "旧版", "旧口径",
                        "不再", "取代", "删除")

    def _hit(spec, line: str) -> bool:
        if isinstance(spec, str):
            return re.search(spec, line) is not None
        return all(p in line for p in spec)

    def scan(paths: list[Path]) -> list:
        out = []
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8")
            except Exception as exc:                              # noqa: BLE001
                print(f"   ! 读不了 {path.name}: {exc}")
                continue
            in_deleted_table = False
            for lineno, line in enumerate(text.splitlines(), 1):
                # 正文《已删内容》表**就是**废案档案（左列引旧口径、右列写现行口径），
                # 整表跳过：这是行文红线指定的存放处（「怕改回去的记在这里」），不是漏洞。
                # 该表不在任何 # 标题下，靠表头「| 已删内容 | 现状… |」识别，遇非表格行复位。
                stripped = line.strip()
                if stripped.startswith("|"):
                    if "已删内容" in stripped and "现状" in stripped:
                        in_deleted_table = True
                        continue
                    if in_deleted_table:
                        continue
                else:
                    in_deleted_table = False
                if any(n in line for n in neg):
                    continue
                for spec, why in STALE_PHRASES:
                    if _hit(spec, line):
                        label = spec if isinstance(spec, str) else "＋".join(spec)
                        out.append((path.relative_to(ROOT), lineno, label, why,
                                    line.strip()[:120]))
        return out

    hard = scan(_files())
    soft = scan(_files(soft=True))
    n_files = len(_files()) + len(_files(soft=True))
    for f, ln, phrase, why, text in soft[:3]:
        print(f"   ~ 软层（工作日志，只警告）{f}:{ln}  [{phrase}] → {why}")
    if soft:
        print(f"   ~ 软层共 {len(soft)} 处旧口径引用（报告/台账允许记沿革，不计入门禁；"
              f"--max-hits 只影响硬层展开）")
    if not hard:
        print(f"   ✓ 硬层无废案残留（{len(_files())}/{n_files} 个文件、{len(STALE_PHRASES)} 条口径）")
        return True
    print(f"   ✗ 硬层 {len(hard)} 处命中（这些是注入 AI 的规则文本，必须清）：")
    for f, ln, phrase, why, text in hard[: args.max_hits]:
        print(f"     {f}:{ln}  [{phrase}] → {why}")
        print(f"        {text}")
    if len(hard) > args.max_hits:
        print(f"     …另有 {len(hard) - args.max_hits} 处（--max-hits 提高上限）")
    return False


def step_costs(args) -> bool:
    print("=" * 78)
    print("4) 口径速览（引擎 resolve 的真实数字，非文档抄写）")
    from engine.daowen import DaoWenEngine
    from engine.gamedata import ORIGINAL_MONSTER_DAOWEN
    DaoWenEngine.register_all()
    names = [args.daowen] if args.daowen else sorted(ORIGINAL_MONSTER_DAOWEN)
    x_to = max(1, args.x_to) if args.daowen else 1
    for name in names:
        if name not in DaoWenEngine.list_all():
            print(f"   ✗ 未知道纹【{name}】")
            return False
        for x in range(1, x_to + 1):
            try:
                calc = DaoWenEngine.resolve(name, x)
            except Exception as exc:                              # noqa: BLE001
                print(f"   {name} X={x}: 拒绝（{exc}）")
                continue
            cost = {k: v for k, v in calc.items()
                    if k.startswith("cost") and v not in (None, 0, "")}
            print(f"   {name:<4} X={x:<2} 代价={cost or '无'}  {calc.get('summary', '')[:64]}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="改规则后的一键自检")
    ap.add_argument("--full", action="store_true", help="第 2 步跑整个 tests/（4–5 分钟）")
    ap.add_argument("--skip-tests", action="store_true", help="跳过第 2 步")
    ap.add_argument("--skip-index", action="store_true", help="跳过第 1 步（不重生成索引）")
    ap.add_argument("--daowen", default=None, help="第 4 步只看这条道纹")
    ap.add_argument("--x-to", type=int, default=1, help="配合 --daowen：打到 X=几（默认 1）")
    ap.add_argument("--max-hits", dest="max_hits", type=int, default=20)
    ap.add_argument("--strict", action="store_true",
                    help="索引未重生成也算失败（默认：替你生成好并只警告）")
    args = ap.parse_args()

    results = []
    if not args.skip_index:
        results.append(("索引同步", step_index(args)))
    if not args.skip_tests:
        results.append(("守卫测试", step_tests(args)))
    results.append(("废案扫描", step_stale(args)))
    results.append(("口径速览", step_costs(args)))

    print("=" * 78)
    bad = [n for n, ok in results if not ok]
    for n, ok in results:
        print(f"   {'✓' if ok else '✗'} {n}")
    print("结论：" + ("全部通过" if not bad else f"{len(bad)} 步未通过：{'、'.join(bad)}"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
