#!/usr/bin/env python3
"""淘汰赛：让胜者池两两死斗，直到只剩最后一人。

赛制（全部走仓库自带的 `sim/duel_diff_trace.py::traced_duel`，不改引擎、不加自造规则）：

  --mode bracket   单败淘汰：N 档 → 1 人。按 --seed 洗牌配对，某轮人数为奇数时
                   末位轮空（轮空位由洗牌顺序决定，不另设种子档）。
  --mode gauntlet  擂台车轮战：--champion 当擂主，其余各档依次上台（--order 决定上台顺序）。
  --mode robust    强度体检：--champion 对每一档 × --seeds 列出的每个种子，**两席各跑一遍**
                   （座位差异大于构筑差异，单席战绩不能当强度）。

**每局都是「干净」死斗**：双方各自从封存快照上场。带伤续战做不了——
`_serialize_entity_full`（engine/api.py:4062）不写 `mutation_count`/`total_healed`，
`_deserialize_entity_full`（:4081）也不读，现成封存格式带不走「崩解／癌变」两条进度条；
扩格式属于规则面改动，未裁定不动（已在报告登记待办）。

超时口径：`sim/duel_pvp.py` 的 30 秒墙钟兜底「不作胜负判定」（:576-582）。
本工具按 --retries 换 seed（+1000 递增）重赛；仍拿不到判定就**如实中止并记录**，
绝不自行裁定胜负（工具无权替规则判卫冕）。

用法：
  python3 sim/tournament.py --mode bracket --seed 1
  python3 sim/tournament.py --mode gauntlet --champion 速战速决_5.json --seed 1
"""
import argparse
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.duel_records import WINNER_DIR, render, run_and_record  # noqa: E402


def _play(challenger: str, defender: str, seed: int, retries: int):
    """跑一局；墙钟兜底则换 seed 重赛。返回 (记录列表, 最终记录, 胜方)。"""
    attempts = []
    for i in range(retries + 1):
        s = seed + 1000 * i
        rec = run_and_record(challenger, defender, s)
        attempts.append(rec)
        if rec["判定"].get("winner") in ("challenger", "defender"):
            return attempts, rec, rec["判定"]["winner"]
    return attempts, attempts[-1], None


def _label(rec) -> str:
    return f"{rec['挑战档名'][:-5]} vs {rec['守擂档名'][:-5]}"


def bracket(files: list, seed: int, retries: int):
    """单败淘汰。返回 (轮次列表, 冠军, 中止说明)。"""
    order = list(files)
    random.Random(seed).shuffle(order)
    rounds, alive, rnd = [], order, 0
    while len(alive) > 1:
        rnd += 1
        recs, winners = [], []
        for i in range(0, len(alive) - 1, 2):
            a, b = alive[i], alive[i + 1]
            attempts, rec, winner = _play(a, b, seed, retries)
            recs.append((attempts, rec, winner))
            if winner is None:
                return rounds, None, (f"第{rnd}轮 {_label(rec)} 连续 {retries + 1} 次"
                                      f"墙钟兜底无判定，赛制中止（工具不代规则裁定）")
            winners.append(a if winner == "challenger" else b)
        if len(alive) % 2 == 1:
            winners.append(alive[-1])  # 轮空
            recs.append(([], None, None))
        rounds.append((rnd, recs, list(alive), winners))
        alive = winners
    return rounds, alive[0], ""


def gauntlet(files: list, champion: str, seed: int, retries: int, order: str):
    """擂台车轮战：champion 守擂，其余依次上台。返回 (对局列表, 最终擂主, 中止说明)。"""
    rest = [f for f in files if f != champion]
    if order == "reverse":
        rest = rest[::-1]
    lord, recs = champion, []
    for ch in rest:
        attempts, rec, winner = _play(ch, lord, seed, retries)
        recs.append((attempts, rec, winner))
        if winner is None:
            return recs, lord, (f"车轮战第{len(recs)}局 {_label(rec)} 连续 {retries + 1} 次"
                                f"墙钟兜底无判定，赛制中止（工具不代规则裁定）")
        if winner == "challenger":
            lord = ch
    return recs, lord, ""


def _cause(rec) -> str:
    """该局死因（取阵亡方的 `_death_ctx.source`，无则用判定文案）。"""
    for _seat, b in rec["终局账面"].items():
        if not b["存活"]:
            return b["死因source"] or b["死因subtype"] or "未注明"
    return "无人阵亡"


def summary_bracket(rounds, champion, abort_msg, files, seed) -> str:
    out = [f"- 赛制：单败淘汰（{len(files)} 档，seed={seed}，每局干净死斗）；"
           f"共 {sum(1 for _r, recs, _a, _w in rounds for a, _rec, _w2 in recs if a)} 局。"]
    if abort_msg:
        out.append(f"- **中止**：{abort_msg}")
    for rnd, recs, alive, winners in rounds:
        bits = []
        for attempts, rec, winner in recs:
            if rec is None:
                bits.append("轮空：" + alive[-1][:-5])
                continue
            loser = rec["守擂档名"] if winner == "challenger" else rec["挑战档名"]
            bits.append(f"{_label(rec)} → {rec['挑战档名'][:-5] if winner == 'challenger' else rec['守擂档名'][:-5]} 晋级"
                        f"（第{rec['判定'].get('rounds')}回合，{loser[:-5]} 死于{_cause(rec)}）")
        out.append(f"- 第{rnd}轮（{len(alive)}人）：" + "；".join(bits))
    if champion:
        out.append(f"- **最后一人：{champion[:-5]}**")
    return "\n".join(out)


def summary_gauntlet(recs, lord, abort_msg, champion, seed) -> str:
    out = [f"- 赛制：擂台车轮战（擂主 {champion[:-5]}，seed={seed}，每局干净死斗）；共 {len(recs)} 局。"]
    if abort_msg:
        out.append(f"- **中止**：{abort_msg}")
    keeps, losses = 0, 0
    for attempts, rec, winner in recs:
        ch, df = rec["挑战档名"][:-5], rec["守擂档名"][:-5]
        rnd, cause = rec["判定"].get("rounds"), _cause(rec)
        if winner == "defender":
            keeps += 1
            out.append(f"- {ch} 上台 → 擂主 {df} 卫冕（第{rnd}回合，{ch} 死于{cause}）")
        else:
            losses += 1
            out.append(f"- {ch} 上台 → **换主**（第{rnd}回合，擂主 {df} 死于{cause}）")
    out.append(f"- 全程 {len(recs)} 局：守擂方获胜 {keeps} 局／挑战方获胜 {losses} 局。")
    out.append(f"- **最后站在台上的人：{lord[:-5]}**")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("bracket", "gauntlet", "robust"), default="bracket")
    ap.add_argument("--seeds", default="1,2,3,4,5", help="robust 模式的种子列表")
    ap.add_argument("--emit-records", action="store_true", help="robust 模式也输出逐局实录")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--champion", help="gauntlet 的初始擂主（档位文件名）")
    ap.add_argument("--order", choices=("sorted", "reverse"), default="sorted")
    ap.add_argument("--retries", type=int, default=1, help="墙钟兜底后换 seed 重赛次数")
    args = ap.parse_args()

    files = sorted(f for f in os.listdir(WINNER_DIR) if f.endswith(".json"))
    out: list = []
    idx = 0

    def emit(rec, tag):
        nonlocal idx
        idx += 1
        out.append(render(rec, idx, tag))
        out.append("")
        print(f"[{idx}] {_label(rec)} seed={rec['seed']} → {rec['判定'].get('winner')}",
              file=sys.stderr)

    if args.mode == "bracket":
        rounds, champion, abort_msg = bracket(files, args.seed, args.retries)
        out.append(f"# 淘汰赛（单败淘汰 · {len(files)} 档 · seed={args.seed}）\n")
        out.append(summary_bracket(rounds, champion, abort_msg, files, args.seed) + "\n")
        for rnd, recs, _alive, _winners in rounds:
            out.append(f"\n## 第{rnd}轮\n")
            for attempts, rec, _winner in recs:
                for a in attempts:
                    emit(a, f"T{rnd}")
    elif args.mode == "robust":
        champ = args.champion or files[0]
        seeds = [int(x) for x in args.seeds.split(",") if x.strip()]
        rows, total, timeouts = [], [0, 0], 0
        out.append(f"# 强度体检（{champ[:-5]} 对全池 · 种子 {seeds} · 两席各跑）\n")
        out.append("| 对手 | 该档坐挑战席（本档守擂） | 该档坐守擂席（本档挑战） | 本档合计 |")
        out.append("|---|---|---|---|")
        for opp in [f for f in files if f != champ]:
            cells = []
            # seat = 对手的席位。opp_seat="challenger" → 本档守擂；"defender" → 本档挑战。
            for opp_seat in ("challenger", "defender"):
                won = 0
                for sd in seeds:
                    ch, df = (opp, champ) if opp_seat == "challenger" else (champ, opp)
                    attempts, rec, winner = _play(ch, df, sd, args.retries)
                    if winner is None:
                        timeouts += 1
                    champ_won = winner == ("defender" if opp_seat == "challenger" else "challenger")
                    won += 1 if champ_won else 0
                    if args.emit_records:
                        for a in attempts:
                            emit(a, "X")
                cells.append(f"{won}/{len(seeds)}")
                # cells[0]=本档守擂席战绩，cells[1]=本档挑战席战绩
                total[0 if opp_seat == "challenger" else 1] += won
            rows.append((opp[:-5], cells))
            out.append(f"| {opp[:-5]} | {cells[0]} | {cells[1]} | {cells[0]}+{cells[1]} |")
        n = len(seeds) * (len(files) - 1)
        out.append(f"\n- 合计：本档 {total[0] + total[1]}/{2 * n} 胜"
                   f"（守擂席 {total[0]}/{n}、挑战席 {total[1]}/{n}）；墙钟兜底 {timeouts} 局。")
        print("\n".join(out))
        return

    else:
        champ = args.champion or files[0]
        recs, lord, abort_msg = gauntlet(files, champ, args.seed, args.retries, args.order)
        out.append(f"# 擂台车轮战（擂主 {champ[:-5]} · seed={args.seed}）\n")
        out.append(summary_gauntlet(recs, lord, abort_msg, champ, args.seed) + "\n")
        out.append("\n## 车轮战逐局\n")
        for attempts, rec, _winner in recs:
            for a in attempts:
                emit(a, "G1")

    print("\n".join(out))


if __name__ == "__main__":
    main()
