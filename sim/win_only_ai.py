"""胜负唯一计分 AI（用户裁定 2026-09-10：胜 +1 / 败 −1，无论什么手段什么战术；
二审：**一切打分机制都用这个**，不只死斗——PvE 战斗、死斗、养蛊链全默认）。

与 TacticalAI 的区别只在**选择**：启发式效用分不再做最终裁决，只用来把
候选裁剪到前 N 个提案（省算力）；每个提案在引擎世界的深拷贝上真实打出：
  · 死斗（in_final_duel）：run_duel_pvp(resume=True) 推演到底 → ±1；
  · PvE 战斗：推演本场打到终局（清场 +1 / 阵亡 −1 / 上限未决 0）。
平局/未决=0；全部同分时按提案器首选出（胜负看不出差别时，听提案器的）。

世界切换镜像 engine/ai_preview.py::preview_sequence 的口径（state+dice 换入
engine 与 combat 双侧，外加 combat 的全部可变记账字典），推演完原样换回——
原世界的实体对象一个字节都不动。

用法：`run_duel_pvp(...)` 默认即本类；PvE 经 `build_learner._play(ai_cls=默认)`。
`win_only_cls(base)` 可以给任意提案器（如遗言桥 LegacyAwareAI）配上 ±1 裁决——
提案口径（含遗言偏见）留在裁剪阶段，胜负裁决权归推演。
代价：每手 ≤N 候选×整局推演，秒级/手；千局级扫描可 LJ_WIN_ONLY=0 或显式传基类。
"""
from __future__ import annotations

import contextlib
import copy
import os

from engine.ai_tactics import TacticalAI, MAX_CANDIDATE_PREVIEWS


def _enabled() -> bool:
    """总开关（默认开）。LJ_WIN_ONLY=0 可整体退回启发式口径（千局级扫描用）。"""
    return os.environ.get("LJ_WIN_ONLY", "1") != "0"


class WinOnlyAI(TacticalAI):
    """提案=基类候选生成；裁决=整局推演的胜负 ±1。"""

    PLAYOUT_TOP_N = 4          # 每手推演的候选上限（启发式排序取前 N）
    PLAYOUT_MAX_ROUNDS = 30    # 死斗推演回合上限（与死斗扫描同口径）
    PLAYOUT_MAX_ROUNDS_PVE = 6 # PvE 推演回合上限（磨血局推不出胜负=0，省算力）
    PLAYOUT_ACTION_CAP = 40    # 推演内单轮玩家出手上限（防拒绝循环）

    # ---------- 世界切换（镜像 ai_preview 的换世界口径，整场推演后丢弃副本） ----------

    def _swap_world(self):
        """context manager：把引擎换到深拷贝世界；退出时原样换回。"""

        @contextlib.contextmanager
        def _cm():
            eng = self.engine
            combat = eng.combat
            real = {
                "state": eng.state, "dice": eng.dice,
                "combat_state": combat.state, "combat_dice": combat.dice,
                "pending": eng._pending_interrupts,
                "hist_len": len(eng._action_history),
                "last": eng._last_result,
                "activated": combat._monster_activated,
                "round_used": combat._monster_daowen_round_used,
                "rewrites": combat._resonance_rewrites,
                "sanxiang": combat._sanxiang_consumed,
                "split": getattr(combat, "_split_clones_spawned", 0),
                "evolved": combat._monster_evolved,
                "depth": combat._effect_chain_depth,
            }
            eng.state = copy.deepcopy(real["state"])
            combat.state = eng.state
            eng.dice = copy.deepcopy(real["dice"])
            combat.dice = eng.dice
            combat._monster_activated = copy.deepcopy(real["activated"])
            combat._monster_daowen_round_used = copy.deepcopy(real["round_used"])
            combat._resonance_rewrites = copy.deepcopy(real["rewrites"])
            combat._sanxiang_consumed = copy.deepcopy(real["sanxiang"])
            combat._split_clones_spawned = copy.deepcopy(real["split"])
            combat._monster_evolved = copy.deepcopy(real["evolved"])
            combat._effect_chain_depth = copy.deepcopy(real["depth"])
            eng._pending_interrupts = copy.deepcopy(real["pending"])
            try:
                yield
            finally:
                eng.state = real["state"]
                combat.state = real["combat_state"]
                eng.dice = real["dice"]
                combat.dice = real["combat_dice"]
                combat._monster_activated = real["activated"]
                combat._monster_daowen_round_used = real["round_used"]
                combat._resonance_rewrites = real["rewrites"]
                combat._sanxiang_consumed = real["sanxiang"]
                combat._split_clones_spawned = real["split"]
                combat._monster_evolved = real["evolved"]
                combat._effect_chain_depth = real["depth"]
                eng._pending_interrupts = real["pending"]
                del eng._action_history[real["hist_len"]:]
                eng._last_result = real["last"]
        return _cm()

    # ---------- 推演计分 ----------

    def _my_sign(self, winner: str) -> int:
        """按我坐哪席把死斗结局换算成 ±1。actor=None=挑战席（驱动 state.player）。"""
        i_am_challenger = self._actor is None
        if winner == "challenger":
            return 1 if i_am_challenger else -1
        if winner == "defender":
            return -1 if i_am_challenger else 1
        return 0

    def _playout_score(self, cand: dict) -> int:
        """在世界深拷贝上真实打出本手，再推演到底，返回 ±1/0（异常=0 未决）。"""
        try:
            with self._swap_world():
                if cand.get("steps"):
                    # steps=(action, params|callable(prev))；_run_steps 走
                    # self.engine.execute_action——此刻世界已是副本，整串（含闪避
                    # 中继的 callable 串联）都打在推演世界里。
                    r = self._run_steps(cand["steps"])
                else:
                    r = self.engine.execute_action(cand["action"], cand["params"])
                if not r.get("success"):
                    return -1   # 非法提案=白给一手（提案器已预演过滤，理论少见）
                if getattr(self.engine.state, "in_final_duel", False):
                    return self._playout_duel()
                return self._playout_pve()
        except Exception:
            return 0            # 推演世界出意外=未决；不得波及真实世界

    def _playout_duel(self) -> int:
        from sim.duel_pvp import run_duel_pvp
        from engine.ai_tactics import TacticalAI as _Base
        verdict = run_duel_pvp(self.engine, None,
                               max_rounds=self.PLAYOUT_MAX_ROUNDS,
                               max_steps=400, log=[], use_tactical=True,
                               ai_cls=_Base,      # 推演内环=基类：防递归推演
                               resume=True)
        return self._my_sign(verdict.get("winner") or "")

    def _playout_pve(self) -> int:
        """PvE：把本场从当前时点推演到终局。清场 +1 / 阵亡 −1 / 上限 0。

        驱动=build_learner 同款（基类出手 → 朋友员工 → 怪物阶段 → 回终/回始），
        例外/门禁按「未决」处理——推演是裁决器，不是被测对象。
        """
        from engine.ai_tactics import TacticalAI as _Base
        from sim.build_learner import (_resolve_monster_turn,
                                       _resolve_pending_choices,
                                       _drive_plight_monsters)
        from sim.build_learner import start_round_with_artifacts
        e = self.engine
        base = _Base(e)
        rounds = self.PLAYOUT_MAX_ROUNDS_PVE
        for _ in range(rounds):
            # 本回合剩余玩家出手
            for _a in range(self.PLAYOUT_ACTION_CAP):
                if not [x for x in e.state.enemies if x.is_alive]:
                    return 1
                if not e.state.player or not e.state.player.is_alive:
                    return -1
                if not base.take_action():
                    break
            if not [x for x in e.state.enemies if x.is_alive]:
                return 1
            if not e.state.player or not e.state.player.is_alive:
                return -1
            e.execute_action("resolve_ally_phases", {})
            if not [x for x in e.state.enemies if x.is_alive]:
                return 1
            _drive_plight_monsters(e)
            if not [x for x in e.state.enemies if x.is_alive]:
                return 1
            mp = _resolve_monster_turn(e)
            if not mp.get("success") or (mp.get("result") or {}).get("player_dead"):
                return -1
            _resolve_pending_choices(e)
            re_ = e.execute_action("round_end", {})
            if not re_.get("success"):
                _resolve_pending_choices(e)
                re_ = e.execute_action("round_end", {})
                if not re_.get("success"):
                    return 0
            if not e.state.player or not e.state.player.is_alive:
                return -1     # 回终结算（癌变/凡庸）命零
            rs, _logs = start_round_with_artifacts(e)
            if not rs.get("success"):
                return 0
            base.new_round()
        return 0               # 上限内推不出胜负（磨血局常态）

    # ---------- 选择：提案裁剪 + ±1 裁决 ----------

    def _dynamic_action(self):
        candidates = self._daowen_candidates()
        candidates.extend(self._basic_attack_candidates())
        for _bonus, cand in self._resonance_candidates():
            candidates.append(cand)   # 残韵候选已按威胁预筛
        # 提案阶段：预演只用来过滤非法与必死候选；启发式分只用于裁剪排序，
        # 不做最终裁决（最终裁决=整局结局 ±1）。
        scored = []
        for cand in candidates[:MAX_CANDIDATE_PREVIEWS]:
            pv = (self.previewer.preview_sequence(cand["steps"]) if cand.get("steps")
                  else self.previewer.preview(cand["action"], cand["params"]))
            res = pv.get("result") or {}
            if not res.get("success"):
                continue
            s = self._score_candidate(pv.get("diff", {}), cand["label"],
                                      cand.get("kind"), cand.get("target"))
            if s is None:
                continue
            scored.append((s, cand))
        scored.sort(key=lambda t: (-t[0], t[1].get("label", "")))
        proposals = [c for _, c in scored[:self.PLAYOUT_TOP_N]]
        if not proposals:
            return None
        best, best_s = None, -2
        for cand in proposals:
            s = self._playout_score(cand)
            if s > best_s:
                best_s, best = s, cand
        # 全部同分（含全 −1）：结果对胜负无差别，照提案器首选——等规则钟的变数。
        r = (self._run_steps(best["steps"]) if best.get("steps")
             else self.engine.execute_action(best["action"], best["params"]))
        if r.get("success"):
            base = best["label"].split("X=")[0].split("→")[0]
            self.used[base] = self.used.get(base, 0) + 1
            return r
        return None


def win_only_cls(proposer: type = TacticalAI) -> type:
    """给任意提案器配上 ±1 裁决：候选/裁剪口径用 proposer，胜负裁决用推演。

    例：`win_only_cls(LegacyAwareAI)`=遗言桥仍影响「试什么」，但「选什么」只认结局。
    """
    return type("WinOnly" + proposer.__name__, (WinOnlyAI, proposer), {})
