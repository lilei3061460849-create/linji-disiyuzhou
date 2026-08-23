# 全胜利路径锦标赛（同种子配对，200局/变体）
# 每条路径：构筑/区域/战术类 不同变量就位，统计 均通关/1战死/进7场/死斗 + 清场构成
import math, random, sys, time
from collections import Counter
sys.path.insert(0, ".")
from sim import build_learner as bl
from engine.ai_tactics import TacticalAI

k = bl.load()
pol = bl.learned_policy(k) or bl.DEFAULT_POLICY
N = 200


class FeedAI(TacticalAI):
    """癌变供养战术（2026-08-22 实验）：自身安全后，把再生灌向血限最低的活敌，
    使其 total_healed 累计≥2×血限触发癌变（吸收进书，无碎片，永久+8休整）。"""
    STRATEGIES = ("try_artifact", "try_survive", "try_feed", "try_buff", "try_resonance",
                  "try_finish", "try_remove", "try_control", "try_aoe", "try_debuff",
                  "try_pressure", "try_ramp", "try_reroll")

    def try_feed(self):
        p = self.player
        if not p or not p.is_alive or "再生" not in p.dao_wen:
            return None
        if p.current_hp < p.blood_limit * 0.6:   # 自己血线不安全不喂
            return None
        enemies = self.alive_enemies()
        if not enemies:
            return None
        t = min(enemies, key=lambda en: en.blood_limit)
        need = 2 * t.blood_limit - t.total_healed
        if need <= 0:
            return None
        x = min(self._x_for("再生", self.mana()), math.ceil(need / 3))
        if x < 1:
            return None
        return self._cast("再生", x, t.name)


hire_pol = dict(pol); hire_pol["雇佣"] = 50  # 雕塑探针：雇佣拉满

variants = [
    # (标签, starter, learn, region, kwargs)
    ("伤害基准@扭曲", "庇护", ["杀伐", "定型", "再生"], "扭曲都市", {}),
    ("凡庸混合@扭曲", "庇护", ["再生", "束缚", "杀伐"], "扭曲都市", {}),
    ("纯憋零输出@扭曲", "庇护", ["再生", "束缚", "固执"], "扭曲都市", {}),
    ("封印流@扭曲", "庇护", ["封印", "杀伐", "再生"], "扭曲都市", {}),
    ("癌变供养@扭曲", "庇护", ["再生", "杀伐", "定型"], "扭曲都市", {"ai_cls": FeedAI}),
    ("还债流@罪孽", "庇护", ["逼债", "赎金", "杀伐"], "罪孽都市", {}),
    ("伤害基准@罪孽", "庇护", ["杀伐", "定型", "再生"], "罪孽都市", {}),
    ("雕塑探针(雇佣50)@罪孽", "庇护", ["杀伐", "定型", "再生"], "罪孽都市", {"policy": hire_pol}),
]
for tag, starter, learn, region, kw in variants:
    t0 = time.time()
    tele = {}
    c = Counter()
    reach7 = fought = won = 0
    n = 0
    for i in range(N):
        r = bl.play(starter, learn, region, seed=10000 + i,
                    rng=random.Random(10000 + i), telemetry=tele, **kw)
        if r.get("invalid"):
            continue
        n += 1
        c[r["cleared"]] += 1
        reach7 += 1 if r["cleared"] >= 7 else 0
        fought += 1 if r.get("duel_fought") else 0
        won += 1 if r.get("duel_won") else 0
    avg = sum(kk * vv for kk, vv in c.items()) / n
    av = dict(sorted(tele.get("alt_victory", {}).items(), key=lambda kv: -kv[1]))
    print(f"\n{tag}: 均通关{avg:.2f}｜1战死{c[0]/n*100:.0f}%｜进7场{reach7}｜死斗{fought}胜{won}｜{time.time()-t0:.0f}s", flush=True)
    print(f"   清场构成: {av}", flush=True)
