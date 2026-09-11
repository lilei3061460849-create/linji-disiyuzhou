#!/usr/bin/env python3
"""遗言桥实证实验（2026-09-10，DM 裁定：角色可参考遗言但不百分百照做）。

三组证据：
  A. 死斗 A/B：同一对真实胜者（普攻武斗_69 挑战 普攻武斗_429）× 多种子 ×
     {普通 TacticalAI + 空书} vs {LegacyAwareAI + 遗言实验室副本}，
     看挑战席胜率是否被"参考遗言"翻转（上一轮实测 0%，挑战者凡庸命零）。
  B. 养蛊 A/B：同一批种子 × {普通管线} vs {遗言桥管线}，看 cleared 分布与
     采纳统计——预期天花板仍在（遗言改变的是打法分配，不是 25 点预算）。
  C. 采纳统计：信从度（0.35–0.85）与逐回合"听/不听"——"参考不照做"的量化。

遗言实验室副本只存在于临时文件，**不写正典 死者之书.md**（遗言须经 DM 审核后才能入书）。
"""
from __future__ import annotations

import collections
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 普攻候选开关（2026-09-09 一池制补丁）：遗言"蓝就是拳"要有普攻可选才成立。
# A/B 两组同开，对称。
os.environ["LJ_AI_BASIC_ATTACK"] = "1"

from engine.api import GameEngine                     # noqa: E402
from sim.duel_pvp import run_duel_pvp                 # noqa: E402
from sim.legacy_mentor import LegacyAwareAI, LegacyMentor, match_hints  # noqa: E402
from tests.setup_support import finish_initial_daowen  # noqa: E402
from sim.handplay_dungeon_with_winner import load_winner  # noqa: E402
from sim.optional_actions import start_battle         # noqa: E402
from sim.guard_full_run import settle_wages           # noqa: E402
from sim.build_learner import _play                   # noqa: E402
from sim.breed_and_duel import BUILDS                 # noqa: E402

WINNER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "breed_winners")

# 遗言草稿（≤20字单句，DM 裁定 2026-08-31 格式）——待 DM 审核后方可入正典。
DRAFT_LEGACIES = """# 死者之书

## 遗言

### 某人·扭曲都市·留训一

- 遗言：法力一池不回填，蓝就是拳，别乱花

### 某人·扭曲都市·留训二

- 遗言：五回合打不掉敌人血，凡庸会收你命

### 某人·扭曲都市·留训三

- 遗言：回复过量会癌变，再生别贪杯

### 某人·扭曲都市·留训四

- 遗言：封印攒异变，五十层崩解命零

### 某人·扭曲都市·留训五

- 遗言：第7战四怪围攻，血与速都要堆
"""

_INSTANCES: list = []


class RecordingAI(LegacyAwareAI):
    """记录 mentor 实例，便于赛后统计采纳率。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _INSTANCES.append(self)


def _mk_lab_book(tmpdir: str, content: str) -> str:
    # 每本书唯一文件名：同目录重名会互相覆盖（首批实验就栽在这上面）
    path = os.path.join(tmpdir, f"死者之书_实验_{len(os.listdir(tmpdir))}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def run_duel(challenger: str, defender: str, seed: int, book_path: str, ai_cls) -> dict:
    """与 traced_duel 同一搭台流程（lab 死者之书路径注入），返回判定+mentor 统计。"""
    _INSTANCES.clear()
    tmp_seal = tempfile.mktemp(suffix=".json")
    shutil.copy(defender, tmp_seal)
    e = GameEngine(db_path=tempfile.mktemp(suffix=".db"), rng_seed=seed,
                   sealed_candidate_path=tmp_seal, death_book_path=book_path)
    with open(challenger, encoding="utf-8") as f:
        snap = json.load(f)
    p0 = snap["player"]
    e.execute_action("setup_attributes", {"name": p0["name"], "blood_points": 10,
                                          "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    load_winner(e, snap)
    e.state.energy = 0
    e.state.current_battle = 6
    bs, _ = start_battle(e)
    if not bs.get("success"):
        return {"error": f"开始第7战失败: {str(bs.get('error',''))[:60]}"}
    for m in e.state.enemies:
        m.is_alive = False
    be = e.execute_action("battle_end", {})
    guard = 0
    while be.get("success") and be.get("completed") is False and be.get("pending_wage_decisions"):
        settle_wages(e, [])
        be = e.execute_action("battle_end", {})
        guard += 1
        if guard > 5:
            break
    crown = (be.get("result") or {}).get("final_crown", {})
    if crown.get("outcome") != "duel_start":
        return {"error": f"未进入死斗: {crown.get('outcome')}"}
    buf: list[str] = []
    res = run_duel_pvp(e, None, max_rounds=30, max_steps=400, log=buf,
                       use_tactical=True, ai_cls=ai_cls)
    mentors = [ai.mentor for ai in _INSTANCES]
    return {**res, "log": buf,
            "采纳": [(m.adherence and round(m.adherence, 2), m.hints, m.follows, m.requests)
                    for m in mentors]}


def duel_ab(n_seeds: int = 16) -> None:
    ch = os.path.join(WINNER_DIR, "普攻武斗_69.json")
    de = os.path.join(WINNER_DIR, "普攻武斗_429.json")
    with tempfile.TemporaryDirectory() as td:
        book = _mk_lab_book(td, DRAFT_LEGACIES)
        for label, ai_cls, bp in (("基线(普通AI+空书)", None, _mk_lab_book(td, "# 死者之书\n\n## 遗言\n")),
                                  ("遗言桥(LegacyAware+草稿书)", RecordingAI, book)):
            wins = collections.Counter()
            for seed in range(1, n_seeds + 1):
                r = run_duel(ch, de, seed, bp, ai_cls)
                if r.get("error"):
                    wins[f"错误:{r['error'][:20]}"] += 1
                    continue
                wins[r.get("winner")] += 1
                if seed == 1:
                    print(f"    [seed1样例] winner={r.get('winner')} R{r.get('rounds')} "
                          f"reason={str(r.get('reason'))[:36]} 采纳={r.get('采纳')}")
            print(f"  {label}: {dict(wins)}")


def breed_ab(n_seeds: int = 40) -> None:
    cfg = BUILDS["杀伐法攻"]
    with tempfile.TemporaryDirectory() as td:
        for label, ai_cls, content in (
                ("基线(普通AI+空书)", None, "# 死者之书\n\n## 遗言\n"),
                ("遗言桥(LegacyAware+草稿书)", RecordingAI, DRAFT_LEGACIES)):
            bp = _mk_lab_book(td, content)
            dist: collections.Counter = collections.Counter()
            req = fol = 0
            for seed in range(1, n_seeds + 1):
                _INSTANCES.clear()
                r = _play(cfg["starter"], cfg["learn"], "扭曲都市", seed=seed, battles=7,
                          attrs=cfg["attrs"], spend_shards=True, xiuxing=cfg.get("xiuxing"),
                          ai_cls=ai_cls,
                          lab_paths={"sealed_path": tempfile.mktemp(suffix=".json"),
                                     "db_path": tempfile.mktemp(suffix=".db"),
                                     "death_book_path": bp})
                if r.get("invalid"):
                    dist["invalid"] += 1
                else:
                    dist[f"c{r.get('cleared')}"] += 1
                for ai in _INSTANCES:
                    req += ai.mentor.requests
                    fol += ai.mentor.follows
            rate = f"{fol}/{req}={fol / req:.0%}" if req else "0/0"
            print(f"  {label}: {dict(sorted(dist.items()))} 遗言采纳={rate}")


def main() -> None:
    print("== A. 死斗 A/B（普攻武斗_69 挑战 普攻武斗_429，双方同班 AI）==")
    duel_ab(16)
    print("\n== B. 养蛊 A/B（杀伐法攻 × 40 种子）==")
    breed_ab(40)
    print("\n== C. 信从度抽样（同一书、不同角色/种子）==")
    texts = [{"text": t} for t in (
        "法力一池不回填，蓝就是拳，别乱花", "五回合打不掉敌人血，凡庸会收你命")]
    for sk in ("1:林渊:legacy", "1:阮烟:legacy", "2:林渊:legacy", "7:贾凡:legacy"):
        m = LegacyMentor(texts, sk)
        print(f"  {sk}: 匹配建议={m.hints} 信从度={m.adherence:.2f}")


if __name__ == "__main__":
    main()
