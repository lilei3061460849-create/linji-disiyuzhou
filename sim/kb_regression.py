#!/usr/bin/env python3
"""KB 行为回归（pre-slim-2026-08-24 vs HEAD）：可用性格子 + 历史污染 + 机器契约。

方法论（检索落地模型，作为 Agent 行为代理）：
  Agent 回答一条规则问题 = 其可读到的语料是否提供该规则的正文。
  语料分层：
    PRE_ACTIVE   = tag 快照 AI_EXPERIENCE.md + 报告.md（瘦身前 Agent 常驻注入）
    POST_ACTIVE  = HEAD AI_EXPERIENCE.md + 报告.md（瘦身后常驻注入）
    SHARED       = README.md + 规则附件（两版共用、未变更，计入两边）
    ARCHIVE      = archive/**（瘦身新增：可检索、不常驻注入；仅 POST 侧存在）
    POST_FULL    = POST_ACTIVE + ARCHIVE
  每条探针带 gold 锚点（事实必须文本可达）与 stale 锚点（不得以"当前规则"形式出现，
  否定语境：已删除/已废止/作废/已并入/改名/原【/留痕/过期/替代 视为历史注明，不算污染）。
判定：
  AVAILABLE-ACTIVE    gold 全命中常驻注入层
  AVAILABLE-SHARED    gold 靠 SHARED 命中（两版等价，规则单一事实源=README）
  AVAILABLE-ARCHIVE   gold 只在 archive 命中，且 POST_ACTIVE 有该档案指针
  MISSING             gold 在 POST 全集（含 archive+shared）不可达 → 真退化
  POLLUTED            stale 锚点以当前规则语境出现
  CLEANED             stale 在 PRE_ACTIVE 以当前规则语境出现、POST_ACTIVE 已隔离
verdict 汇总映射 DM 第三阶段指标：正确率/遗漏/规则冲突/旧知识误用/行为策略变化。

用法：PYTHONPATH=. python -m sim.kb_regression
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TAG = "pre-slim-2026-08-24"
NEG = ("已删除", "已废止", "作废", "已并入", "改名", "原【", "留痕", "过期", "替代", "已删")


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, cwd=ROOT).stdout


def load_corpora():
    pre = (sh("git", "show", f"{TAG}:AI_EXPERIENCE.md")
           + "\n" + sh("git", "show", f"{TAG}:报告.md"))
    post = ((ROOT / "AI_EXPERIENCE.md").read_text(encoding="utf-8")
            + "\n" + (ROOT / "报告.md").read_text(encoding="utf-8"))
    shared_files = ["README.md", "死者之书.md", "全道纹索引.md", "副本索引.md", "物品索引.md"]
    shared = "\n".join((ROOT / f).read_text(encoding="utf-8") for f in shared_files
                       if (ROOT / f).exists())
    arch = "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted((ROOT / "archive").rglob("*.md")))
    pre_kb = sh("git", "show", f"{TAG}:data/build_knowledge.json")
    post_kb = (ROOT / "data" / "build_knowledge.json").read_text(encoding="utf-8")
    pre_kb_txt = "\n".join(l.get("text", "") for l in json.loads(pre_kb).get("lessons", []))
    post_kb_txt = "\n".join(l.get("text", "") for l in json.loads(post_kb).get("lessons", []))
    return {"PRE_ACTIVE": pre, "POST_ACTIVE": post, "SHARED": shared,
            "ARCHIVE": arch, "PRE_KB": pre_kb_txt, "POST_KB": post_kb_txt,
            "POST_FULL": post + "\n" + arch + "\n" + shared + "\n" + post_kb_txt,
            "PRE_FULL": pre + "\n" + shared + "\n" + pre_kb_txt}


def _hit(corpus: str, anchor) -> bool:
    if isinstance(anchor, (list, tuple)):      # any-of
        return any(a in corpus for a in anchor)
    return anchor in corpus


def _stale_active_lines(corpus: str, anchor) -> list[str]:
    hits = [ln.strip() for ln in corpus.splitlines() if anchor in ln]
    return [ln for ln in hits if not any(n in ln for n in NEG)]


# ---------------- 探针库（锚点全部来自仓库事实源：README/引擎字符串/DM裁定原文） ----------------
PROBES = [
    # 一、基础规则理解（事实源=SHARED README，KB 不得矛盾；两版共用 → 预期 EQUIV）
    dict(id="C1-01", cat=1, q="第四宇宙/轮回的本质", gold=["轮回是为了填补过去的不甘心"], layer="SHARED"),
    dict(id="C1-02", cat=1, q="死者之书=灵魂契约交易承载体", gold=["局外行动本质上均由《死者之书》承担"], layer="SHARED"),
    dict(id="C1-03", cat=1, q="灵魂碎片与微光者", gold=["灵魂碎片是字面意思"], layer="SHARED"),
    dict(id="C1-04", cat=1, q="死者之书不可交接/偷走", gold=["无法作为物品被交接或偷走"], layer="SHARED"),
    dict(id="C1-05", cat=1, q="残韵定义存在性", gold=["残韵"], layer="SHARED"),
    dict(id="C1-06", cat=1, q="道纹定义存在性（全道纹索引）", gold=["道纹"], layer="SHARED"),
    dict(id="C1-07", cat=1, q="能量/精力资源锚点", gold=["精力"], layer="SHARED"),
    dict(id="C1-08", cat=1, q="三层世界结构", gold=["三层世界"], layer="SHARED", note="全仓无此术语——诚实记为 LACUNA，不编造"),
    # 二、道纹与残韵
    dict(id="C2-01", cat=2, q="残韵转化规则（永久改写+施法者同获+人类不得原始怪物道纹）",
         gold=["永久改写", ["同获", "同时永久获得"], ["人类仍无法永久获得原始怪物道纹", "人类仍不得原始怪物道纹"]]),
    dict(id="C2-02", cat=2, q="道纹学习=开局发现流程",
         gold=["format_setup_discovery", ["杀伐不是默认起手", "杀伐非默认起手"], "【发现】"]),
    dict(id="C2-03", cat=2, q="怪物专属道纹首次发动代价",
         gold=[["原始怪物道纹只在首次发动时支付异变5X", "原始怪物道纹仅首次发动支付异变5X"]]),
    dict(id="C2-04", cat=2, q="怪物道纹递增（+2×阶级，README准则9 单一事实源）",
         gold=["累加+2×副本阶级"], layer="SHARED+PRE_ACTIVE"),
    dict(id="C2-05", cat=2, q="道纹变化（退化降X实战注意）", gold=["退化"]),
    dict(id="C2-06", cat=2, q="资源限制：法力支付/冷却锚点", gold=["冷却"]),
    dict(id="C2-07", cat=2, q="波及自适应降X（CF-2 现行裁定，README:467）",
         gold=["自适应降X"], layer="SHARED"),
    # 三、副本规则
    dict(id="C3-01", cat=3, q="救赎触发条件", gold=["救赎", ["血限10%", "[血限]10%"], ["七种原始怪物道纹", "七种原始怪物道纹"]]),
    dict(id="C3-02", cat=3, q="癌变阈值=本场累计回复2×血限", gold=["癌变阈值", ["2×血限", "2×[血限]"]]),
    dict(id="C3-03", cat=3, q="凡庸（5回合未掉血自炸）", gold=["凡庸", "5 回合"]),
    dict(id="C3-04", cat=3, q="怪物困境强制二选一", gold=[["怪物困境信号≥1", "困境"], ["强制二选一", "进化借"]]),
    dict(id="C3-05", cat=3, q="强化怪池占比报告纪律（2/12 条目）",
         gold=["罪孽2/12", "扭曲2/12", "龙心1/12"]),
    dict(id="C3-06", cat=3, q="死斗封存规则存在性", gold=["死斗", ["擂主", "封存"]]),
    dict(id="C3-07", cat=3, q="净化X 机制", gold=["【净化X】消耗5X"]),
    # 四、当前有效经验（KB lessons + 生产配置摘要）
    dict(id="C4-01", cat=4, q="确认精英回注剂量 75/25", gold=["75%", "25%", "UCB"]),
    dict(id="C4-02", cat=4, q="双种子同序最优 2.62>2.02>1.78", gold=["2.62>2.02>1.78"]),
    dict(id="C4-03", cat=4, q="每15代 top3 深挖2评", gold=[["每 15 代", "每15代"], ["top3", "top 3"]]),
    dict(id="C4-04", cat=4, q="best_confirmed ≥2 次评估均值确认", gold=["best_confirmed", ["≥2 次", "≥2次"]]),
    dict(id="C4-05", cat=4, q="凡庸才是主输出（清场路径实测）", gold=[["凡庸才是主输出", "非伤害清场"]]),
    dict(id="C4-06", cat=4, q="学习位≥4 幻影维度", gold=[["幻影维度", "幻影"]]),
    # 五、历史污染（stale 不得以当前规则语境常驻注入）+ 冲突检测
    dict(id="S5-01", cat=5, q="波及过滤旧语义", stale=["不进入 daowen_options"], gold=["自适应降X"]),
    dict(id="S5-02", cat=5, q="过期会话分支名", stale=["arena/01a01970-linji-disiyuzhou"],
         gold=["每轮工作结束必须提交并推送"]),
    dict(id="S5-03", cat=5, q="已删遗物钱袋（仅允许历史注明）", stale=["钱袋"]),
    dict(id="S5-04", cat=5, q="癌变双倍计入（已删除机制）", stale=["双倍计入"]),
    dict(id="S5-05", cat=5, q="KB-L03 贯穿结论=未证实假设", gold=["【未证实假设"]),
    dict(id="S5-06", cat=5, q="报告.md 已瘦身（历史进archive）", gold=["archive/report_history_2026-08-24.md"]),
    # 六、机器契约（文档校验器/锚点标题）
    dict(id="M6-01", cat=6, q="H1 与映射句（校验器硬契约）", gold=["# AI经验库", "用户提到的“AI 知识库”指仓库根目录的 `AI_EXPERIENCE.md`"]),
    dict(id="M6-02", cat=6, q="职责划分锚点标题", gold=["### 职责划分"]),
    dict(id="M6-03", cat=6, q="推演铁律锚点标题", gold=["### 推演铁律"]),
    dict(id="M6-04", cat=6, q="战斗推演原子流水线 + 六场景", gold=["## 战斗推演原子流水线与典型实战示例", "#### 场景 6"]),
    # 七、工程操作
    dict(id="E7-01", cat=7, q="每轮工作必须提交并推送（现行口径）", gold=["每轮工作结束必须提交并推送"]),
    dict(id="E7-02", cat=7, q="实验灾备协议在场（REMOTE VERIFIED）", gold=["实验灾备协议", "REMOTE VERIFIED"]),
    dict(id="E7-03", cat=7, q="实验库/生产KB分离+幂等重放", gold=["--reset", "reapply_lessons_after_reset", "幂等"]),
    dict(id="E7-04", cat=7, q="实验档案指针（experiment_log）", gold=["archive/experiment_log.md"]),
    dict(id="E7-05", cat=7, q="旧知识只进 archive/hypotheses（D 类流程）", gold=["archive/hypotheses/"]),
]


def run() -> dict:
    C = load_corpora()
    assert len(C["PRE_ACTIVE"]) > 1000, "tag 快照读取失败"
    results = []
    for p in PROBES:
        r = {"id": p["id"], "cat": p["cat"], "q": p["q"]}
        # --- stale 检查（历史污染）---
        for s in p.get("stale", []):
            pre_stale = _stale_active_lines(C["PRE_ACTIVE"], s)
            post_stale = _stale_active_lines(C["POST_ACTIVE"], s)
            r.setdefault("stale", {})[s] = {
                "pre_active_ctx": len(pre_stale), "post_active_ctx": len(post_stale),
                "post_sample": post_stale[:1], "pre_sample": pre_stale[:1]}
        # --- gold 可用性 ---
        g = p.get("gold", [])
        layers = {}
        for name in ("PRE_FULL", "PRE_ACTIVE", "POST_ACTIVE", "SHARED", "ARCHIVE",
                     "PRE_KB", "POST_KB", "POST_FULL"):
            layers[name] = all(_hit(C[name], a) for a in g) if g else None
        r["layers"] = layers
        post_active_ok = layers["POST_ACTIVE"]
        shared_ok = layers["SHARED"]
        arch_ok = layers["ARCHIVE"]
        kb_ok = layers["POST_KB"]
        pointer_ok = arch_ok and "archive/" in C["POST_ACTIVE"]
        stales = r.get("stale", {})
        any_post_pollution = any(sd["post_active_ctx"] > 0 for sd in stales.values())
        any_cleaned = any(sd["pre_active_ctx"] > 0 and sd["post_active_ctx"] == 0
                          for sd in stales.values())
        # --- verdict ---
        if p["id"] == "C1-08":
            verdict = "LACUNA" if not layers["POST_FULL"] else "EQUIV"
        elif not g:   # 纯污染探针：无 gold，不参与可用性判定
            if any_post_pollution:
                verdict = "POLLUTED"
            elif any_cleaned:
                verdict = "CLEANED"
            else:
                verdict = "CLEAN-BOTH"
        elif layers["POST_FULL"] is False:
            verdict = "DEGRADED-MISSING"
        elif post_active_ok:
            verdict = "EQUIV" if layers["PRE_ACTIVE"] else "IMPROVED"
        elif shared_ok:
            verdict = "EQUIV-SHARED"
        elif kb_ok:
            verdict = "EQUIV-KB" if layers["PRE_KB"] else "IMPROVED-KB"
        elif arch_ok and pointer_ok:
            verdict = "EQUIV-RETRIEVABLE" if layers["PRE_ACTIVE"] else "IMPROVED-RETRIEVABLE"
        else:
            verdict = "DEGRADED-NO-POINTER" if arch_ok else "DEGRADED-MISSING"
        # 污染覆写
        if any_post_pollution:
            verdict = "POLLUTED:" + next(s for s, sd in stales.items() if sd["post_active_ctx"] > 0)
        elif any_cleaned and verdict.startswith(("EQUIV", "IMPROVED")):
            verdict = "CLEANED(stale已隔离)"
        r["verdict"] = verdict
        results.append(r)

    # --- S5-05 KB-L03 直接 JSON 断言（kind 必须降级为假设）---
    pre_kb_obj = json.loads(sh("git", "show", f"{TAG}:data/build_knowledge.json"))
    post_kb_obj = json.loads((ROOT / "data" / "build_knowledge.json").read_text(encoding="utf-8"))
    pre_l03 = [l for l in pre_kb_obj.get("lessons", []) if "贯穿" in l.get("text", "")[:30]]
    post_l03 = [l for l in post_kb_obj.get("lessons", []) if "未证实假设" in l.get("text", "")[:30]]
    for r in results:
        if r["id"] == "S5-05":
            r["l03_pre_kind"] = pre_l03[0].get("kind") if pre_l03 else "N/A"
            r["l03_post_kind"] = post_l03[0].get("kind") if post_l03 else "MISSING"
            r["verdict"] = ("CLEANED(假设降级)" if post_l03 and post_l03[0].get("kind") == "假设"
                            and (not pre_l03 or pre_l03[0].get("kind") != "假设") else "CHECK!")

    # --- 机器契约：文档校验器对两版文本 ---
    sys.path.insert(0, str(ROOT))
    from engine.document_validation import validate_ai_knowledge_base
    val = {}
    for name in ("POST_ACTIVE", "PRE_ACTIVE"):
        try:
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
                f.write(C[name].split("\n\n# 第四宇宙 · 实战测试")[0])  # 只喂 AI_EXPERIENCE 部分
                tmp = f.name
            val[name] = validate_ai_knowledge_base(pathlib.Path(tmp))
        except Exception as e:  # 校验器签名不符时记录异常而非崩溃
            val[name] = {"error": repr(e)}
    # 策略测试快跑（机器消费回归的一部分）
    tp = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                         "tests/test_ai_policy.py", "tests/test_ai_experience_policy.py"],
                        capture_output=True, text=True, cwd=ROOT)
    r = {"id": "M6-00", "cat": 6, "q": "document_validation 校验器机器消费 + 策略测试",
         "validator_post": val.get("POST_ACTIVE"), "validator_pre": val.get("PRE_ACTIVE"),
         "policy_tests_tail": tp.stdout.strip().splitlines()[-1] if tp.stdout.strip() else tp.stderr[-200:],
         "verdict": "MACHINE-CONTRACT"}
    results.append(r)

    agg = {}
    for r in results:
        v = r["verdict"].split("(")[0].split(":")[0]
        agg[v] = agg.get(v, 0) + 1
    out = {"tag": TAG, "head": sh("git", "rev-parse", "--short", "HEAD").strip(),
           "probes": len(PROBES), "aggregate": agg, "results": results}
    return out


def main():
    out = run()
    dst = ROOT / "data" / "experiments" / "kb_regression_2026-08-24.json"
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in out["results"]:
        print(f'{r["id"]:7s} cat{r["cat"]} {r["verdict"]:26s} {r["q"][:48]}')
    print("\nAGG:", json.dumps(out["aggregate"], ensure_ascii=False))
    print("->", dst)


if __name__ == "__main__":
    main()
