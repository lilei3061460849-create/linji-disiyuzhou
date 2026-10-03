#!/usr/bin/env python3
"""把《报告.md》的轮回记录压成两个可直接粘贴的版本（给外部模型分析用）。

问题：完整《报告.md》太大，聊天窗口粘贴会提示「文本消息太长」。
做法：**逐回合一行**（精简版）与**逐场一行 + 速览/复盘**（极简版）。

口径（重要）：
* 数字全部从《报告.md》原文机械提取，原样搬运：不四舍五入、不估算、不新增事实。
* 省略的只有**冗余表述**：逐击明细展开、每回合重复的满值面板、样板句
  （「逐击显式提交，未声明招架」等）、解释性括注的复述。
* 保留：每回合动作/目标/伤害/回复/法力、状态层数、[结算] 行、⚠ 标注、
  战始/战终、局外行动、速览表、复盘。
* 自检：解析到的场数/回合数必须与原文一致，否则抛错（宁可不产出，也不产出残缺版）。

用法：
    python sim/compact_cycle_report.py --in 报告.md            # 打印三种字数
    python sim/compact_cycle_report.py --write                 # 写 reports/轮回记录_{精简,极简}版.md
    python sim/compact_cycle_report.py --dump                  # 打到 stdout
"""
from __future__ import annotations
import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BATTLE_RE = re.compile(r"^## 第 (\d+) 场\s*$")
ROUND_RE = re.compile(r"^### 第 (\d+) 回合\s*$")
OUTSIDE_RE = re.compile(r"^### 局外（(.+)）\s*$")
PANEL_RE = re.compile(r"^([^（]+)（生命 (\d+)/(\d+)、法力 (\d+)/(\d+)、速度 (\d+)/(\d+)、格挡 (\d+)、出手 (\d+)）")
ACTION_RE = re.compile(r"^\*\*出手(\d+)\*\*：\[动作声明\] (.*)$")
STATUS_RE = re.compile(r"^（状态：(.*)）$")
SETTLE_RE = re.compile(r"^\[(回始|回终)\] 结算：(.*)$")
BOLD_RE = re.compile(r"^\*\*\[(战终|救赎|死之传承|终局|战始)\]\*\* ?(.*)$")


def _short_status(text: str) -> str:
    """状态串压短：坏死（X=66，剩 54 回合）、衰败（X=5，持续 ∞） → 坏死X66(剩54)、衰败X5(∞)。"""
    t = re.sub(r"（X=(\d+)[，,]剩 (\d+) 回合）", r"X\1(剩\2)", text)
    t = re.sub(r"（X=(\d+)[，,]持续 ∞）", r"X\1(∞)", t)
    t = re.sub(r"（X=(\d+)）", r"X\1", t)
    t = re.sub(r"获得状态【(.+?)】\(?(\d*)\)?", r"\1\2", t)
    return t.strip("；; ")


def _panels(line: str) -> dict:
    """一行面板 → {名称: 「生命/血限(法x)」}；我方键统一为「我」。"""
    out = {}
    for part in line.split("｜"):
        m = PANEL_RE.match(part.strip())
        if not m:
            continue
        name = m.group(1).strip()
        out["我" if name == "林寂" else name] = f"{m.group(2)}/{m.group(3)}"
    return out


def _split_cycle(md: str) -> dict:
    """拆成：开局 / 各场（含局外段）/ 速览与复盘。"""
    lines = md.splitlines()
    try:
        i_summary = next(i for i, ln in enumerate(lines) if ln.startswith("## 二、本局速览"))
    except StopIteration:
        raise ValueError("找不到「## 二、本局速览」：输入不是本仓库的《报告.md》？")

    setup: list[str] = []
    pre_outside: list[str] = []
    battles: list[dict] = []
    cur = None
    where = None          # 'battle' | 'round' | 'outside'
    for ln in lines[:i_summary]:
        m_b, m_r, m_o = BATTLE_RE.match(ln), ROUND_RE.match(ln), OUTSIDE_RE.match(ln)
        if m_b:
            cur = {"no": int(m_b.group(1)), "lines": [], "rounds": [], "outside": []}
            battles.append(cur)
            where = "battle"
            continue
        if m_o:
            if cur is not None:
                cur["outside"].append(m_o.group(1))
            where = "outside"
            continue
        if m_r:
            cur["rounds"].append({"no": int(m_r.group(1)), "lines": []})
            where = "round"
            continue
        if ln.startswith("# 战报") or ln.startswith(">"):
            setup.append(ln)
            continue
        if cur is None:
            (pre_outside if where == "outside" else setup).append(ln)
            continue
        if where == "round":
            cur["rounds"][-1]["lines"].append(ln)
        elif where == "outside":
            (cur["outside"] if cur is not None else pre_outside).append(ln)
        else:
            cur["lines"].append(ln)
    # 速览/复盘只取《二、本局速览》这一节：不能把后面《三、道纹两两协同穷举》的
    # 编号结论也吸进来（它们是另一份独立附录，不属于轮回记录）。
    i_next = next((i for i in range(i_summary + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return {"setup": [x for x in setup if x.strip()],
            "pre_outside": [x for x in pre_outside if x.strip()],
            "battles": battles,
            "summary": lines[i_summary:i_next], "n_battles": len(battles),
            "n_rounds": sum(len(b["rounds"]) for b in battles)}


def _parse_round(lines: list[str]) -> dict:
    f = {"start": {}, "acts": [], "bullets": [], "settle": [], "status": "", "note0": False,
         "end": {}, "warn": [], "end_line": "", "status_line": "", "p_heal": 0}
    act = None
    for raw in lines:
        s = raw.rstrip()
        st = s.strip()
        if not st:
            continue
        if st.startswith("**[回始]**"):
            f["start"] = _panels(st[len("**[回始]** "):])
            f["start_line"] = st
            continue
        m = ACTION_RE.match(st)
        if m:
            act = {"no": int(m.group(1)), "decl": m.group(2), "land": "", "react": "", "reflect": ""}
            f["acts"].append(act)
            continue
        if st.startswith("**[怪物阶段]**"):
            continue
        body = st[2:].strip() if st.startswith("- ") else st
        if body.startswith("[数值落地]"):
            txt = body[len("[数值落地] "):]
            if act is not None and not act["land"]:
                act["land"] = txt
            else:                        # 道纹的落地以子弹形式出现
                f["bullets"].append(("land", txt))
            m2 = re.search(r"回复 (\d+)", txt)
            if m2:
                f["p_heal"] += int(m2.group(1))
            continue
        if body.startswith("[目标反应]"):
            if act is not None:
                act["react"] = body[len("[目标反应] "):]
            continue
        if body.startswith("[反噬]"):
            if act is not None:
                act["reflect"] = body[len("[反噬] "):]
            continue
        if st.startswith("**[回终]**"):
            f["end_line"] = st
            f["end"] = _panels(st[len("**[回终]** "):])
            continue
        if STATUS_RE.match(st):
            f["status"] = STATUS_RE.match(st).group(1)
            continue
        if st.startswith("（本回合"):
            if "0 伤" in st:
                f["note0"] = True
            continue
        if st.startswith("⚠"):
            f["warn"].append(st)
            continue
        if SETTLE_RE.match(st):
            mm = SETTLE_RE.match(st)
            f["settle"].append(f"{mm.group(1)}:{mm.group(2)}")
            continue
        mb = BOLD_RE.match(st)
        if mb and mb.group(1) != "战始":
            f["settle"].append(f"{mb.group(1)}:{mb.group(2)}".rstrip(":"))
            continue
        if body and not body.startswith("["):
            f["bullets"].append(("monster", body))
    return f


def _decl_short(decl: str) -> str:
    d = decl.strip()
    m = re.search(r"对 (.+?) 发动普攻（(\d+) 击）", d)
    if m:
        return f"普攻→{m.group(1)},{m.group(2)}击"
    m = re.search(r"发动道纹【(.+?)】(?:（(.+?)）)?", d)
    if m:
        extra = (m.group(2) or "").replace("消耗", "耗").replace("为林寂", "").replace("目标 player；", "")
        return f"道纹{m.group(1)}" + (f"({extra})" if extra else "")
    return d


def _land_delta(land: str) -> str:
    m = re.search(r"生命 \d+→(\d+)（合计 (\d+) 点", land)
    if m:
        return f"-{m.group(2)}"
    m = re.search(r"生命 (\d+)→(\d+)（回复 (\d+)）", land)
    if m:
        return f"+{m.group(3)}"
    return "?"


def _monster_short(items: list[tuple]) -> list[str]:
    """怪物阶段：道纹按次、普攻按「次数+总伤」聚合。"""
    out, attacks = [], {}
    for kind, body in items:
        m = re.match(r"(.+?) 发动道纹【(.+?)】(?:，目标 (.+?))?[：:]?(.*)$", body)
        if m:
            actor, dw, _tgt, eff = m.group(1), m.group(2), m.group(3), (m.group(4) or "")
            eff_s = _short_status(eff.split("：")[-1]) if eff else ""
            out.append(f"{dw}" + (f"→{eff_s}" if eff_s else ""))
            continue
        m = re.match(r"(.+?) 普攻 (.+?) 第(\d+)击：(\d+) 点伤害", body)
        if m:
            attacks.setdefault(m.group(1), []).append(int(m.group(4)))
            continue
        if kind == "land":
            out.append(f"[落地]{_land_delta(body)}" if _land_delta(body) != "?" else "[落地]")
            continue
        out.append(body[:60])
    for actor, dmg in attacks.items():
        out.append(f"{actor}普攻{len(dmg)}击{sum(dmg)}伤")
    return out


def _setup_brief(setup_lines: list[str]) -> list[str]:
    """开局段只留事实与数字，去掉格式声明/归档说明/协同附录说明（与轮回分析无关）。"""
    keep = []
    for x in setup_lines:
        s = x.strip()
        if not s or s.startswith("# ") or s == "## 一、开局":
            continue
        if s.startswith(">"):
            body = s.lstrip("> ").strip()
            if body.startswith(("记录日期", "本局结果")):
                keep.append(body.replace("**", ""))
            continue
        body = s.strip("- ").strip().replace("**", "")
        if body.startswith("引擎口径"):
            body = ("引擎口径：攻击次数=当前速度、攻击力=当前法力；"
                    "怪物把法力花在道纹上后普攻按剩余法力结算；【爆裂】反噬攻击者")
        keep.append(body)
    return [k for k in keep if k]


def _summary_brief(summary_lines: list[str]) -> list[str]:
    """速览表压成 1 行关键数字 + 复盘原样（复盘本身已是结论层）。"""
    rows = {}
    for ln in summary_lines:
        m = re.match(r"^\| (.+?) \| (.+?) \|$", ln.strip())
        if m:
            rows[m.group(1)] = m.group(2)
    pick = ["场次", "回合", "道纹发动", "输出", "承伤", "有害结算", "法力支出", "局外", "消耗品", "碎片", "结局"]
    line = "；".join(f"{k} {rows[k]}" for k in pick if k in rows)
    out = ["[速览] " + line] if line else []
    out += [x for x in summary_lines if x.strip().startswith(("### 复盘", "1.", "2.", "3.", "4.", "5."))]
    return out


def _keep_status(status: str) -> str:
    keep = []
    for x in re.split(r"[；;]", status):
        x = x.strip()
        if not x or re.fullmatch(r".+ 无", x):     # 「林寂 无」「肠水母 无」这类空状态
            continue
        keep.append(x)
    return "；".join(keep)


def compact_lines(cycle: dict) -> list[str]:
    L: list[str] = []
    A = L.append
    A("# 轮回记录 · 精简版（逐回合一行）")
    A("")
    A("> 由 `python sim/compact_cycle_report.py --write` 从《报告.md》机械提取；数字与完整报告一致，未做估算，仅省略冗余表述。")
    A("")
    A("```")
    brief = _setup_brief(cycle["setup"])
    rec = next((b for b in brief if b.startswith("记录日期")), "")
    facts = [b for b in brief if not b.startswith("记录日期")]
    A("[记录] " + rec.replace("｜", "｜"))
    A("[开局] " + "；".join(facts))
    if cycle["pre_outside"]:
        A("[开局的局外] " + "；".join(x.strip("- ").strip().replace("**", "") for x in cycle["pre_outside"]))
    for b in cycle["battles"]:
        A(f"== 第 {b['no']} 场 ==")
        for ln in b["lines"]:
            s = ln.strip()
            if s.startswith("**[战始]**"):
                A("[战始] " + s[len("**[战始]** "):])
        hp_start = hp_end = None
        dealt = taken = reflect = healed = 0
        for r in b["rounds"]:
            f = _parse_round(r["lines"])
            seg = [f"R{r['no']}"]
            if f["start"]:
                mine = f["start"].get("我", "?")
                foes = ",".join(f"{k}{v}" for k, v in f["start"].items() if k != "我")
                seg.append(f"我{mine}" + (f" 敌{foes}" if foes else ""))
                if hp_start is None and mine != "?":
                    hp_start = mine.split("/")[0]
            acts = []
            for a in f["acts"]:
                token = _decl_short(a["decl"])
                tail = []
                if a["land"]:
                    d = _land_delta(a["land"])
                    m = re.search(r"生命 (\d+)→(\d+)（合计 (\d+) 点", a["land"])
                    if m and m.group(3) and a["decl"].find("普攻") >= 0:
                        dealt += int(m.group(3))
                    tail.append(d)
                if a["react"] and "不闪避" not in a["react"]:
                    tail.append(a["react"].split("（")[0])
                if a["reflect"]:
                    m2 = re.search(r"失去 (\d+) 点", a["reflect"])
                    if m2:
                        reflect += int(m2.group(1))
                    tail.append(f"反噬-{m2.group(1)}" if m2 else "反噬")
                acts.append(token + ("[" + ",".join(t for t in tail if t != "?") + "]" if any(t != "?" for t in tail) else ""))
            # 连续相同动作折叠成 ×N
            folded = []
            for x in acts:
                if folded and folded[-1][0] == x:
                    folded[-1][1] += 1
                else:
                    folded.append([x, 1])
            if folded:
                seg.append("＋".join(x if n == 1 else f"{x}×{n}" for x, n in folded))
            mon = _monster_short(f["bullets"] + [("monster", m) for m in []])
            if mon:
                seg.append("怪:" + "; ".join(mon))
            for kind, body in f["bullets"]:
                if kind == "monster" and body.startswith(("", )):
                    pass
            m_taken = re.findall(r"第\d+击：(\d+) 点伤害", " ".join(
                body for kind, body in f["bullets"] if kind == "monster"))
            taken += sum(int(x) for x in m_taken)
            healed += f["p_heal"]
            if f["end"]:
                seg.append("终 我" + f["end"].get("我", "?")
                           + (" 敌" + ",".join(f"{k}{v}" for k, v in f["end"].items() if k != "我")
                              if any(k != "我" for k in f["end"]) else ""))
                if f["end"].get("我"):
                    hp_end = f["end"]["我"].split("/")[0]
            st = _keep_status(_short_status(f["status"]))
            if st:
                seg.append("状态[" + st + "]")
            for s in f["settle"]:
                seg.append("§" + s)
            for w in f["warn"]:
                seg.append(w)
            if f["note0"]:
                seg.append("[0伤:攻力=法力]")
            A("  " + " ｜ ".join(seg))
        if b["outside"]:
            o = []
            for ln in b["outside"]:
                s = ln.strip()
                if not s or s.startswith(("（", "[回")):
                    continue
                if s.startswith("§") or s.startswith("结算："):
                    if "血限" in s or "流血" in s or "碎片" in s:
                        o.append(s.strip("§"))
                    continue
                s = s.replace("局外·", "").replace("**", "").strip("- ").strip()
                if s and not s.startswith("结算"):
                    o.append(s)
            if o:
                A("[局外] " + "；".join(o))
        A(f"[小计] 回合{len(b['rounds'])} 我血{hp_start}→{hp_end} 我输出{dealt} 承伤{taken}"
          + (f" 反噬{reflect}" if reflect else "") + (f" 回复{healed}" if healed else ""))
    A("```")
    A("")
    L.extend(_summary_brief(cycle["summary"]))
    A("")
    return L


def mini_lines(cycle: dict) -> list[str]:
    L: list[str] = []
    A = L.append
    A("# 轮回记录 · 极简版（逐场一行）")
    A("")
    A("> 由 `python sim/compact_cycle_report.py --write` 从《报告.md》机械提取；数字与完整报告一致，未做估算。要细节用 `reports/轮回记录_精简版.md`。")
    A("")
    A("```")
    A("[开局] " + "；".join(_setup_brief(cycle["setup"])))
    for b in cycle["battles"]:
        hp_track, foes, dealt, taken, reflect, healed, keys = [], {}, 0, 0, 0, 0, []
        for r in b["rounds"]:
            f = _parse_round(r["lines"])
            if f["start"].get("我"):
                hp_track.append(int(f["start"]["我"].split("/")[0]))
            end_hp = f["end"].get("我", "").split("/")[0]
            if end_hp.isdigit():
                hp_track.append(int(end_hp))
            for k, v in f["start"].items():
                if k != "我":
                    foes.setdefault(k, v.split("/")[1])
            for a in f["acts"]:
                if a["land"]:
                    m = re.search(r"(.+?) 生命 \d+→\d+（合计 (\d+) 点", a["land"])
                    if m and m.group(1).strip() != "林寂":
                        dealt += int(m.group(2))
                if a["reflect"]:
                    m2 = re.search(r"失去 (\d+) 点", a["reflect"])
                    if m2:
                        reflect += int(m2.group(1))
            for kind, body in f["bullets"]:
                m = re.match(r".+? 普攻 .+? 第\d+击：(\d+) 点伤害", body)
                if m:
                    taken += int(m.group(1))
            healed += f["p_heal"]
            for s in f["settle"]:
                if any(k in s for k in ("凡庸", "命零", "增援", "救赎", "残骸", "衰败", "畸变")):
                    keys.append(s)
            keys += f["warn"]
        res = "败（林寂[命零]）" if any("林寂在本阶段[命零]" in w for w in keys) or \
            any("终局" in s for s in keys) else "胜"
        seen, uniq = set(), []
        for k in keys:
            kk = k[:70]
            if kk not in seen:
                seen.add(kk); uniq.append(kk)
        A(f"第{b['no']}场 {len(b['rounds'])}回合 {res}｜敌 " +
          ",".join(f"{k}(血限{v})" for k, v in foes.items()) +
          f"｜我血 {hp_track[0] if hp_track else '?'}→{hp_track[-1] if hp_track else '?'}"
          f"｜我输出{dealt} 承伤{taken}"
          + (f" 反噬{reflect}" if reflect else "") + (f" 回复{healed}" if healed else ""))
        for k in uniq[:2]:
            A("    · " + k)
    A("```")
    A("")
    L.extend("```\n" + x for x in _summary_brief(cycle["summary"])[:1])
    L.append("")
    return L


def strip_embedded_compaction(md: str) -> str:
    """去掉《报告.md》里已内嵌的《〇、压缩记录》章节。

    否则在本报告上重跑压缩器会把上一次的压缩结果当成原始记录再压一遍（数字会膨胀），
    只应压缩完整正文。独立报告没有该章节时原样返回。
    """
    start = md.find("## 〇、压缩记录")
    if start < 0:
        return md
    end = md.find("## 一、开局", start)
    if end < 0:
        return md
    return md[:start] + md[end:]


def compress(md: str) -> dict:
    md = strip_embedded_compaction(md)
    cycle = _split_cycle(md)
    comp, mini = compact_lines(cycle), mini_lines(cycle)
    n_rounds_md = len(re.findall(r"^### 第 \d+ 回合\s*$", md, re.M))
    n_battles_md = len(re.findall(r"^## 第 \d+ 场\s*$", md, re.M))
    assert cycle["n_rounds"] == n_rounds_md, (cycle["n_rounds"], n_rounds_md)
    assert cycle["n_battles"] == n_battles_md, (cycle["n_battles"], n_battles_md)
    ctext, mtext = "\n".join(comp).rstrip() + "\n", "\n".join(mini).rstrip() + "\n"
    # 自检：每个回合都要落到精简版里（R 行数 == 回合数）
    assert len(re.findall(r"^  R\d+ ", ctext, re.M)) == cycle["n_rounds"], "精简版回合数不符"
    assert len(re.findall(r"^第\d+场 ", mtext, re.M)) == cycle["n_battles"], "极简版场数不符"
    return {"compact": ctext, "mini": mtext,
            "stats": {"battles": cycle["n_battles"], "rounds": cycle["n_rounds"],
                      "compact_chars": len(ctext), "mini_chars": len(mtext)}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", default=str(ROOT / "报告.md"))
    ap.add_argument("--write", action="store_true", help="写 reports/轮回记录_{精简,极简}版.md")
    ap.add_argument("--dump", choices=("compact", "mini", "both"), default=None)
    args = ap.parse_args()

    src = Path(args.src)
    raw = src.read_text(encoding="utf-8")
    out = compress(raw)
    s = out["stats"]
    print(f"场次 {s['battles']}｜回合 {s['rounds']}｜精简版 {s['compact_chars']} 字｜"
          f"极简版 {s['mini_chars']} 字（原文 {len(raw)} 字）")
    if args.write:
        d = ROOT / "reports"
        d.mkdir(parents=True, exist_ok=True)
        (d / "轮回记录_精简版.md").write_text(out["compact"], encoding="utf-8")
        (d / "轮回记录_极简版.md").write_text(out["mini"], encoding="utf-8")
        print("written:", d / "轮回记录_精简版.md", "/", d / "轮回记录_极简版.md")
    if args.dump in ("compact", "both"):
        print(out["compact"])
    if args.dump in ("mini", "both"):
        print(out["mini"])


if __name__ == "__main__":
    main()
