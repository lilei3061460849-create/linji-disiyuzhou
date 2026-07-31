#!/usr/bin/env python3
"""
第四宇宙引擎 · 真实压力模拟器
=====================================
与此前 AI_EXPERIENCE.md 中无法复现的"1920局压力测试"不同：
本脚本的每一局都完整经过 engine/api.py 的公开行动接口结算，
数字可用 `python3 simulate.py` 原样复现（种子固定）。

用法:
    python3 simulate.py                 # 默认 100 局/副本/策略，并追踪走得最远的一局
    python3 simulate.py --runs 500      # 自定义局数
    python3 simulate.py --seed 42       # 自定义种子
    python3 simulate.py --verbose 1     # 打印单局详情
    python3 simulate.py --no-track      # 关闭最远局追踪（节省时间）

追踪说明：
    跑完批量后，找出"走得最远"的一局（胜场最多→到达场次最深→存活回合最多），
    以同种子原样重跑并全程记录：每次局外行动、每回合敌我双方出手、战终结算，
    输出到 data/furthest_run.json 并打印人类可读战报。
    策略与随机源均为确定性实现，追踪局与批量局结果完全一致（可复现）。

诚实声明：
- 怪物AI与本模拟器中的轮回者策略均为确定性启发式，非最优解；
- 事件系统/遗物(除5件)/员工/副本专属行动未实装，不参与模拟；
- 随机数规则：本模拟器以种子化随机源扮演"提供数字的玩家/DM"角色。
"""
import argparse
import json
import os
import random
import shutil
import sys
from typing import Optional

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.gamedata import MONSTER_POOLS, monster_spawn_count, REGION_BATTLE_COUNT


# ============================================================
# 战报渲染（仅用于追踪输出，不影响任何结算）
# ============================================================

def snap_player(engine: GameEngine) -> str:
    p = engine.state.player
    return (f"HP{p.current_hp}/{p.blood_limit} 法{p.current_mana}/{p.mana_limit} "
            f"速{p.current_speed}/{p.speed_limit} 格挡{p.shield} 碎片{engine.state.shards}")


def snap_enemies(engine: GameEngine) -> str:
    return "、".join(f"{m.name}(HP{m.current_hp})" for m in engine.state.enemies) or "（无）"


def _hit_line(hit: dict) -> str:
    """渲染一次命中判定（resolve_attack / take_damage 的返回，两套键名都兼容）"""
    t = hit.get("target", "?")
    if hit.get("dodge_attempted") and hit.get("dodge_success"):
        return f"{t}闪避成功(余速{hit.get('speed_after_dodge', '?')})"
    if hit.get("blocked_by"):
        return f"{t}被[{hit['blocked_by']}]挡下"
    if hit.get("dodge_attempted"):
        reason = hit.get("dodge_fail_reason", "")
        dod = f"闪避失败({reason})，" if reason else "闪避失败，"
    else:
        dod = ""
    dmg = hit.get("damage_dealt", hit.get("actual_damage", 0))
    absorbed = hit.get("shield_absorbed", 0)
    s = f"{t}{dod}受{dmg}伤害"
    if absorbed:
        s += f"(格挡抵{absorbed})"
    hp_after = hit.get("target_hp_after", hit.get("hp_after"))
    if hp_after is not None:
        s += f"→HP{hp_after}"
    if hit.get("target_died", hit.get("died")):
        s += "[命零]"
    return s


def describe_effect(e: dict) -> str:
    """渲染 execution.effects 中的一条效果"""
    t = e.get("type")
    if t == "damage":
        return _hit_line(e)
    if t == "multi_hit_damage":
        s = f"{e.get('target')}受{e.get('hits')}次×1点连续伤害"
        if e.get("target_hp_after") is not None:
            s += f"→HP{e['target_hp_after']}"
        if e.get("target_died"):
            s += "[命零]"
        return s
    if t == "aoe_damage":
        return _hit_line(e)
    if t == "heal":
        return f"{e.get('target')}回复{e.get('actual_heal')}(HP{e.get('hp_after')})"
    if t == "shield":
        return f"{e.get('target')}格挡+{e.get('amount')}"
    if t == "status_added":
        dur = e.get("duration")
        dtxt = "∞" if dur in (-1, None) else f"{dur}回合"
        return f"{e.get('target')}获得[{e.get('status')}{e.get('value')}]({dtxt})"
    if t == "speed_boost":
        return f"{e.get('target')}速度+{e.get('amount')}"
    if t == "speed_halved":
        return f"{e.get('target')}速度减半→{e.get('speed_after')}"
    if t == "mana_gain":
        return f"{e.get('source')}法力+{e.get('amount')}"
    if t == "blood_limit_reduction":
        return f"{e.get('target')}血限-{e.get('amount')}"
    if t == "blood_limit_increase":
        return f"{e.get('target')}血限+{e.get('increase')}"
    if t == "fake_shards":
        return f"假碎片+{e.get('amount')}"
    if t == "shard_steal":
        return f"夺取碎片{e.get('amount')}"
    if t == "gain_flying":
        return f"{e.get('target')}升空"
    if t == "ground_all":
        return f"落地:{e.get('grounded')}"
    if t == "seal":
        return f"封印移出:{e.get('sealed')}"
    if t == "self_attacks":
        return "自残:" + "|".join(_hit_line(h) for h in e.get("hits", []))
    if t == "shield_clear":
        return f"{e.get('entity')}格挡清空(-{e.get('cleared')})"
    if t == "mana_clear":
        return f"{e.get('entity')}法力清空(-{e.get('cleared')})"
    if t == "衰败伤害":
        s = f"{e.get('entity')}受{e.get('damage')}衰败伤害→HP{e.get('hp_after')}"
        return s + ("[命零]" if e.get("died") else "")
    if t == "清算格挡流失":
        return f"{e.get('entity')}清算流失格挡{e.get('shield_lost')}"
    if t == "逼债碎片":
        return f"逼债：-{e.get('amount')}碎片(余{e.get('shards_after')})"
    if t == "逼债血限":
        return f"逼债：血限-{e.get('amount')}(余{e.get('blood_limit_after')})"
    if t == "活血回复":
        return f"{e.get('entity')}活血回复{e.get('heal')}"
    if t == "self_heal":
        return f"{e.get('entity')}自愈回复{e.get('actual')}"
    if t == "deform_blood_loss":
        s = f"{e.get('entity')}畸变血限-{e.get('blood_loss')}(余{e.get('blood_limit_after')})"
        return s + ("[命零]" if e.get("died") else "")
    if t == "洞察法力":
        return f"洞察：{e.get('entity')}法力+{e.get('amount')}"
    if t == "status_expired":
        return f"{e.get('entity')}状态到期:{e.get('expired_effects', e.get('status'))}"
    # 兜底：不丢失任何信息地扁平化
    kv = "，".join(f"{k}={v}" for k, v in e.items()
                  if k != "type" and not isinstance(v, (dict, list)))
    return f"{t}({kv})" if kv else str(t)


def summarize_result(result: dict) -> str:
    """从 use_daowen / use_spell 的成功返回中提取一行效果摘要"""
    if not result or not result.get("success"):
        return f"失败:{(result or {}).get('error', '?')}"
    parts = []
    calc = result.get("calculation") or {}
    if calc.get("cost_type") == "消耗" and calc.get("cost"):
        parts.append(f"耗法{calc['cost']}")
    for c in result.get("cost_applied") or []:
        amt = c.get("amount", c.get("note", c.get("cooldown_battles", "")))
        parts.append(f"代价[{c.get('type')}{amt}]")
    if result.get("dodge") and result["dodge"].get("success"):
        parts.append("被闪避，完全失效")
    ex = result.get("execution") or {}
    if ex.get("monster_triple"):
        parts.append("非专属×3")
    for e in ex.get("effects", []):
        parts.append(describe_effect(e))
    # 法术的逐步骤结算（use_spell 返回 steps_executed）
    for step in result.get("steps_executed", []) or []:
        if step.get("skipped"):
            parts.append(f"步骤[{step.get('step')}]跳过:{step['skipped']}")
            continue
        sub = []
        for c in step.get("cost_applied") or []:
            amt = c.get("amount", c.get("note", c.get("cooldown_battles", "")))
            sub.append(f"代价[{c.get('type')}{amt}]")
        for e in step.get("execution") or []:
            sub.append(describe_effect(e))
        parts.append(f"步骤[{step.get('step')}X={step.get('x', '?')}]：" + ("；".join(sub) or "无效果"))
    if result.get("interrupted"):
        parts.append(f"中断:{result.get('interrupt_reason')}")
    if result.get("player_mana_after") is not None:
        parts.append(f"余法{result['player_mana_after']}")
    return "；".join(parts) if parts else "（无可见效果）"


def render_pre_battle(params: dict, result: dict) -> str:
    sub = params.get("sub_action", "?")
    if not result.get("success"):
        return f"{sub} 失败:{result.get('error', '?')}"
    r = result.get("result") or {}
    if sub == "休整":
        alloc = "、".join(f"{a['target']}{a['hp']}" for a in r.get("allocated", []))
        cost = f"，-{r['shard_cost']}碎片" if r.get("shard_cost") else ""
        return f"休整(档{params.get('tier', 1)})：恢复{r.get('heal_pool')}→{alloc}{cost}"
    if sub == "修行":
        return (f"修行(档{params.get('tier', 1)})：+{r.get('points_gained')}属性点"
                + (f"，-{r['shard_cost']}碎片" if r.get("shard_cost") else ""))
    if sub == "学习":
        return f"学习：{(result.get('action') or '')} {r}"
    if sub == "领悟":
        return f"领悟：残韵[{r.get('gained_resonance')}]×{r.get('total')}"
    if sub == "忘忧":
        return f"忘忧(档{params.get('tier', 1)})：失忆{r.get('forgot_daowen')}→+{r.get('shards_gained')}碎片(余{r.get('shards_total')})"
    if sub == "共鸣":
        return f"共鸣:{result.get('action', '')}"
    return f"{sub}:{r}"


def render_monster_turn(monster_name: str, result: dict) -> list[str]:
    lines = []
    if not result.get("success"):
        return [f"  ✗ {monster_name}出手失败:{result.get('error')}"]
    if result.get("skipped"):
        return [f"  · {monster_name}：{result['skipped']}"]
    used = result.get("acts_used", "?")
    allowed = result.get("acts_allowed", "?")
    entries = []
    for entry in result.get("turn_log", []):
        if entry.get("error"):
            entries.append(f"错误:{entry['error']}")
        elif entry.get("type") == "attack_round":
            hits = entry.get("hits", [])
            entries.append(f"攻击→{entry.get('target')}：" +
                           (" | ".join(_hit_line(h) for h in hits) if hits else "未命中"))
        elif entry.get("type") == "use_daowen":
            # 核心结算自带 action 描述："XX发动道纹【名X=n】"，calculation 里也有 dao_wen/x
            act_txt = entry.get("action")
            if not act_txt:
                calc = entry.get("calculation") or {}
                act_txt = f"发动道纹【{calc.get('dao_wen', entry.get('daowen', '?'))}X={calc.get('x', '?')}】"
            entries.append(f"{act_txt}：" + summarize_result(entry))
        else:
            entries.append(str(entry))
    lines.append(f"  ◀ {monster_name}出手{used}/{allowed}：" + " ‖ ".join(entries))
    php = result.get("player_hp")
    if php is not None:
        lines[-1] += f"（轮回者HP余{php}）"
    return lines


# ============================================================
# 策略层（扮演 AI 玩家与怪物，只调用公开行动接口）
# ============================================================

class Policy:
    """轮回者策略：局外成长序列 + 战斗行为"""

    def __init__(self, name: str):
        self.name = name

    # ---- 局外 ----
    def pre_battle_plan(self, engine: GameEngine, battle_no: int) -> list[dict]:
        """按战斗场次返回局外行动序列（每项为 pre_battle_action 的参数）"""
        if self.name == "naive_dps":
            # 朴素输出：只修行+休整，不学转化道纹
            return [
                {"sub_action": "修行", "tier": 1},
                {"sub_action": "修行", "tier": 1},
                {"sub_action": "休整", "tier": 1},
            ]
        if self.name == "balanced":
            plans = {
                1: [
                    {"sub_action": "学习", "learn_type": "transform_daowen", "names": ["再生"], "tier": 1},
                    {"sub_action": "学习", "learn_type": "transform_daowen", "names": ["庇护"], "tier": 1},
                    {"sub_action": "休整", "tier": 3},
                ],
            }
            default = [
                {"sub_action": "修行", "tier": 1},
                {"sub_action": "修行", "tier": 1},
                {"sub_action": "休整", "tier": 3},
            ]
            return plans.get(battle_no, default)
        if self.name == "combo":
            plans = {
                1: [
                    {"sub_action": "学习", "learn_type": "transform_daowen", "names": ["再生"], "tier": 1},
                    {"sub_action": "学习", "learn_type": "transform_daowen", "names": ["庇护"], "tier": 1},
                    {"sub_action": "休整", "tier": 3},
                ],
                2: [
                    {"sub_action": "学习", "learn_type": "spell", "names": ["借力打力"], "tier": 1},
                    {"sub_action": "学习", "learn_type": "spell", "names": ["以牙还牙"], "tier": 1},
                    {"sub_action": "休整", "tier": 3},
                ],
            }
            default = [
                {"sub_action": "修行", "tier": 1},
                {"sub_action": "修行", "tier": 1},
                {"sub_action": "休整", "tier": 3},
            ]
            return plans.get(battle_no, default)
        raise ValueError(self.name)

    # ---- 战斗：轮回者回合 ----
    def player_acts(self, engine: GameEngine, trace: Optional[list] = None) -> None:
        player = engine.state.player
        enemies = engine.state.get_all_enemy_side()
        while enemies and player.is_alive:
            budget = engine._player_action_budget()
            if engine.state.actions_used >= budget:
                break
            if player.current_mana < 1:
                break
            target = min(enemies, key=lambda m: m.current_hp)
            mana = player.current_mana

            use = None
            # 防御判断：预测敌方下轮总火力，超过当前生命+格挡的60%则先庇护
            threat = sum(m.attack_count * m.attack_power for m in enemies)
            need_shield = threat > player.current_hp + player.shield * 2 and "庇护" in player.dao_wen
            if need_shield and player.shield < threat // 2:
                use = ("庇护", max(1, mana - 2))
            elif "杀伐" in player.dao_wen:
                use = ("杀伐", mana)
            elif "再生" in player.dao_wen and player.current_hp < player.blood_limit * 0.5:
                use = ("再生", max(1, mana // 2))
            if use is None:
                break
            r = engine.execute_action("use_daowen", {
                "daowen_name": use[0], "x": use[1], "target": target.name,
            })
            if trace is not None:
                trace.append(f"  ▶ 【{use[0]}X={use[1]}】→{target.name}：" + summarize_result(r))
            if not r.get("success"):
                break
            enemies = engine.state.get_all_enemy_side()
        if trace is not None and engine.state.actions_used == 0 and player.is_alive:
            trace.append("  ▶ （无可用道纹或法力，跳过行动）")

    def dodge_decisions(self, engine: GameEngine, monster, hit_total: int) -> list[bool]:
        """作为防御方为每次命中给出闪避决策"""
        player = engine.state.player
        dodges = []
        for _ in range(hit_total):
            if player is None or player.current_speed < 1:
                dodges.append(False)
                continue
            incoming = monster.attack_power
            lethal_soon = player.current_hp <= incoming * 2
            low_shield = player.shield < incoming
            dodges.append(bool(low_shield and (lethal_soon or player.current_speed > 4)))
        return dodges

    # ---- 战斗：怪物回合 ----
    def monster_acts(self, engine: GameEngine, monster) -> list[dict]:
        """扮演怪物做最优决策：在出手次数预算内先上关键面板道纹，其余全部输出。

        预算必须与引擎结算口径一致（出手=当前回合÷3向上取整，±活力/无力），
        否则整轮行动会被引擎合法拒绝。
        """
        base = CombatEngine.monster_act_count(engine.state.current_round)
        allowed = max(0, base + monster.get_status_value("活力") - monster.get_status_value("无力"))
        has_kuangbao = monster.has_status("狂暴")
        if allowed <= 0 and not has_kuangbao:
            return []  # 引擎将如实跳过（无出手次数）

        defender = engine.state.player
        hit_total = 1 if monster.has_status("迟滞") else monster.attack_count
        attack = {
            "type": "attack_round",
            "target": defender.name if defender else "",
            "dodges": self.dodge_decisions(engine, monster, hit_total),
        }

        # 一次性增益道纹占用手数：预算内优先上 buff，最后留至少1次输出
        buffs = []
        for dw in ["飞行", "活力", "必中", "自愈", "庇护", "强化"]:
            if dw in monster.dao_wen and not monster.has_status(dw):
                buffs.append(dw)
        buff_budget = max(0, allowed - 1) if not has_kuangbao else allowed  # 狂暴时输出可靠狂暴补
        acts = []
        for dw in buffs[:buff_budget]:
            panel_x = 1
            for m in MONSTER_POOLS[engine.state.current_region]:
                if m["name"].startswith(monster.name.rstrip("0123456789")):
                    panel_x = m["daowen"].get(dw, 1)
                    break
            acts.append({"type": "use_daowen", "daowen": dw, "x": max(1, panel_x),
                         "target": monster.name})
        # 其余手数全部输出（含狂暴额外1次攻击）
        while len(acts) < allowed + (1 if has_kuangbao else 0):
            acts.append(dict(attack, dodges=self.dodge_decisions(engine, monster, hit_total)))
        return acts


# ============================================================
# 单局轮回模拟
# ============================================================

def handle_pending(engine: GameEngine, rng: random.Random, verbose: bool) -> dict:
    """处理所有挂起的中断与随机请求（种子化随机源扮演提供数字的玩家）"""
    guard = 0
    while guard < 50:
        guard += 1
        if engine._pending_interrupts:
            intr = engine._pending_interrupts[0]
            itype = intr.interrupt_type.value
            if itype == "死之传承":
                engine.submit_ruling("死之传承", "")
                return {"ended": "death"}
            engine.submit_ruling(itype, "", {"choice": "concede"})
            continue
        if engine._pending_random:
            meta = engine._pending_random["meta"]
            pool = meta.get("pool", [])
            if pool:
                engine.execute_action("random_number", {
                    "pool_name": engine._pending_random["pool_name"],
                    "number": rng.randint(1, len(pool)),
                })
                continue
        break
    return {"ended": None}


def run_campaign(policy_name: str, region: str, seed: int, verbose: bool = False,
                 trace: Optional[list] = None) -> dict:
    """完整跑完一局轮回（7场常规战斗，胜则封存冠冕）。

    trace 不为 None 时，向其中逐条追加战报行（不影响结算，同种子结果仍然一致）。
    """
    rng = random.Random(seed)
    db_dir = f"data/sim/{seed}_{policy_name}_{region}"
    shutil.rmtree(db_dir, ignore_errors=True)
    os.makedirs(db_dir, exist_ok=True)

    engine = GameEngine(db_path=f"{db_dir}/rulings.db")
    policy = Policy(policy_name)

    def T(msg: str):
        if trace is not None:
            trace.append(msg)

    # 开局（沿用经验库中的标准分配 10/8/7）
    engine.execute_action("setup_attributes", {
        "name": "模拟者", "blood_points": 10, "speed_points": 8, "mana_points": 7,
    })
    engine.execute_action("setup_choose_daowen", {"daowen": "杀伐"})
    engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    engine.execute_action("setup_choose_region", {"region": region})

    # 开局遗物发现（抽3选1：优先已实装遗物）
    engine.execute_action("discover_relic_setup", {})
    handle_pending(engine, rng, verbose)
    last = engine._last_result or {}
    chosen_relic = None
    if last.get("candidates"):
        engine.execute_action("discover_relic_setup", {"chosen": last["candidates"][0]})
        chosen_relic = engine._last_result.get("result", {}).get("relic")

    log = {"policy": policy_name, "region": region, "seed": seed,
           "battles_won": 0, "furthest_battle": 0, "death_battle": None,
           "rounds_survived": 0,
           "crown_sealed": False, "duel": None, "relics": []}
    log["relics"] = [r.name for r in engine.state.relics]

    p0 = engine.state.player
    T(f"═══ 轮回开始 ═══  策略[{policy_name}] 副本[{region}] 种子{seed}")
    T(f"开局：血限{p0.blood_limit} 速限{p0.speed_limit} 法限{p0.mana_limit}"
      f"（10/8/7分配） 初始道纹【杀伐】 残韵[反转] 碎片{engine.state.shards}")
    if last.get("candidates"):
        T(f"发现遗物：候选{last['candidates']} → 选择【{chosen_relic}】")

    for battle_no in range(1, REGION_BATTLE_COUNT + 1):
        log["furthest_battle"] = battle_no
        if verbose:
            print(f"\n--- 第{battle_no}场 | HP {engine.state.player.current_hp}/{engine.state.player.blood_limit}"
                  f" 法限{engine.state.player.mana_limit} 速限{engine.state.player.speed_limit}"
                  f" 碎片{engine.state.shards} 道纹{list(engine.state.player.dao_wen.keys())}")
        T("")
        T(f"━━ 第{battle_no}场 ━━  战前：{snap_player(engine)} 道纹{list(engine.state.player.dao_wen.keys())}")

        # ---- 局外 ----
        T(f"【局外】精力{engine.state.energy}：")
        for act in policy.pre_battle_plan(engine, battle_no):
            if engine.state.energy <= 0:
                break
            r = engine.execute_action("pre_battle_action", act)
            handle_pending(engine, rng, verbose)
            T(f"  · {render_pre_battle(act, r)}")
            if act.get("sub_action") == "修行" and r.get("success"):
                pts = engine.state.attribute_points
                if pts > 0:
                    # 交替加速度与法力
                    if engine.state.player.speed_limit <= engine.state.player.mana_limit:
                        r2 = engine.execute_action("spend_attribute_points", {"to": "速限", "points": pts})
                    else:
                        r2 = engine.execute_action("spend_attribute_points", {"to": "法限", "points": pts})
                    rr = r2.get("result", {})
                    T(f"    分配属性点：{pts}点→{rr.get('to')}"
                      f"（速限{rr.get('speed_limit')} 法限{rr.get('mana_limit')} 出手{rr.get('action_count')}）")
        # 未花完的精力用免费休整兜底（休整档位失败自动降档）
        while engine.state.energy > 0:
            spent = False
            for tier in (3, 2, 1):
                r = engine.execute_action("pre_battle_action", {"sub_action": "休整", "tier": tier})
                if r.get("success"):
                    spent = True
                    T(f"  · {render_pre_battle({'sub_action': '休整', 'tier': tier}, r)}")
                    break
            if not spent:  # 碎片连t1都付不起时只能领悟残韵烧精力
                r = engine.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "反转"})
                T(f"  · {render_pre_battle({'sub_action': '领悟'}, r)}")
                if not r.get("success"):
                    break

        # ---- 战始 ----
        engine.execute_action("battle_start", {"battle_background": "模拟背景"})
        handle_pending(engine, rng, verbose)
        if trace is not None:
            spawn = engine._last_result or {}
            if spawn.get("enemies"):
                T("【战始】出怪：")
                for m in spawn["enemies"]:
                    dwtxt = "、".join(f"{k}{v}" for k, v in (m.get("daowen") or {}).items())
                    carry = f"，携带碎片{m['carry_shards']}" if m.get("carry_shards") else ""
                    T(f"    {m['name']} 面板{m['panel']} 道纹[{dwtxt}]{carry}")
        if engine.state.phase != "in_combat":
            log["death_battle"] = battle_no
            T("  ☠ 战始结算后未能进入战斗，轮回终止")
            break

        # ---- 回合循环 ----
        max_rounds = 60
        finished = False
        for round_i in range(1, max_rounds + 1):
            engine.execute_action("round_start", {})
            log["rounds_survived"] += 1
            T(f"[回合{engine.state.current_round}] 我方：{snap_player(engine)} | 敌方：{snap_enemies(engine)}")

            # 反应型防御：学会后发制人后，怪物出手前插队开启
            player = engine.state.player
            if any(s.name == "借力打力" for s in player.spells) and player.current_mana >= 4:
                enemies = engine.state.get_all_enemy_side()
                if enemies:
                    r = engine.execute_action("use_spell", {
                        "spell_name": "借力打力",
                        "trigger_timing": "受到伤害前",
                        "target": enemies[0].name,
                        "x": max(1, player.current_mana // 2),
                        "y": 1,
                    })
                    T(f"  ▶ 法术【借力打力】：" + summarize_result(r))

            policy.player_acts(engine, trace)

            for monster in list(engine.state.enemies):
                if not monster.is_alive or not engine.state.player.is_alive:
                    continue
                acts = policy.monster_acts(engine, monster)
                r = engine.execute_action("monster_turn", {"monster": monster.name, "acts": acts})
                if trace is not None:
                    trace.extend(render_monster_turn(monster.name, r))

            r = engine.execute_action("round_end", {})
            ended = handle_pending(engine, rng, verbose)
            if trace is not None:
                hooks = ((r.get("result") or {}).get("effects")) or []
                hook_lines = [describe_effect(e) if isinstance(e, dict) and e.get("type") else str(e)
                              for e in hooks]
                diff = r.get("monster_difficulties") or []
                tail = ("；".join(hook_lines) + (" " if hook_lines else "")
                        + (f"怪物困境:{[d.get('monster') for d in diff]}" if diff else "")).strip()
                if tail:
                    T(f"  （回终）{tail}")
            if ended.get("ended") == "death":
                log["death_battle"] = battle_no
                finished = True
                break
            if r.get("battle_finished") or not engine.state.get_all_enemy_side():
                rb = engine.execute_action("battle_end", {})
                log["battles_won"] += 1
                finished = True
                if trace is not None:
                    body = rb.get("result") or {}
                    det = "、".join(f"{d['monster']}+{d['total']}"
                                    for d in body.get("reward_detail", []))
                    T(f"【战终】击倒全部敌人：碎片奖励+{body.get('shard_reward', 0)}"
                      f"（{det}）→总碎片{body.get('total_shards')}，精力恢复3")
                    crown = body.get("crown")
                    if crown:
                        T(f"  👑 最终的冠冕：{crown}")
                break

        if not finished or log["death_battle"]:
            if not log["death_battle"]:
                log["death_battle"] = battle_no
            if trace is not None:
                p = engine.state.player
                T(f"  ☠ 第{battle_no}场战败：轮回者[命零]（最终HP{p.current_hp}/{p.blood_limit}）")
                T(f"  ☠ 死之传承中断触发 → DM裁定 → 轮回结束。本场战绩：胜{log['battles_won']}场")
            break

        if engine.state.phase == "game_over":
            log["crown_sealed"] = True
            T("  👑 冠冕已封存，轮回完整结束")
            break
        if engine.state.phase == "dead_duel":
            log["duel"] = "triggered"
            T("  ⚔ 触发最终死斗（存在封存候选）")
            break

    if engine.state.phase == "game_over" and engine.state.player.is_alive:
        log["crown_sealed"] = True

    return log


def pick_furthest(all_logs: list[dict]) -> Optional[dict]:
    """走得最远：胜场最多 → 冠冕/死斗 → 到达场次最深 → 存活回合最多（确定性）"""
    def key(l):
        return (l["battles_won"], bool(l.get("crown_sealed")), bool(l.get("duel")),
                l["furthest_battle"], l.get("rounds_survived", 0))
    best = None
    for l in all_logs:
        if best is None or key(l) > key(best):
            best = l
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=100, help="每个 副本×策略 的局数")
    ap.add_argument("--seed", type=int, default=20250731, help="基础种子")
    ap.add_argument("--verbose", type=int, default=0)
    ap.add_argument("--no-track", action="store_true", help="关闭最远局追踪")
    args = ap.parse_args()

    regions = ["扭曲都市", "罪孽都市", "龙心谷"]
    policies = ["naive_dps", "balanced", "combo"]

    all_logs = []
    print("=" * 70)
    print(f"真实模拟：{args.runs} 局 × {len(regions)}副本 × {len(policies)}策略  "
          f"（种子{args.seed}，可复现）")
    print("=" * 70)

    for policy in policies:
        for region in regions:
            wins, deaths, furthest_sum, battle_dist = 0, 0, 0, {}
            for i in range(args.runs):
                seed = args.seed + i * 7919
                log = run_campaign(policy, region, seed, verbose=bool(args.verbose))
                all_logs.append(log)
                if log["crown_sealed"]:
                    wins += 1
                if log["death_battle"]:
                    deaths += 1
                    battle_dist[log["death_battle"]] = battle_dist.get(log["death_battle"], 0) + 1
                furthest_sum += log["furthest_battle"]
            rate = wins / args.runs * 100
            print(f"\n[{policy} / {region}] 局数:{args.runs}")
            print(f"  通关(封存冠冕): {wins} ({rate:.1f}%)  死亡: {deaths}  "
                  f"平均推进: {furthest_sum/args.runs:.2f}场")
            print(f"  死亡分布(按场次): {dict(sorted(battle_dist.items()))}")

    out = "data/sim_results.json"
    os.makedirs("data", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "config": {"runs": args.runs, "seed": args.seed,
                       "regions": regions, "policies": policies},
            "logs": all_logs,
        }, f, ensure_ascii=False)
    print(f"\n明细已写入 {out}（共{len(all_logs)}局）")
    print("复现命令: python3 simulate.py --runs %d --seed %d" % (args.runs, args.seed))

    # ---- 走得最远的一局：同种子原样重跑并全程追踪 ----
    if not args.no_track and all_logs:
        best = pick_furthest(all_logs)
        print("\n" + "=" * 70)
        print(f"走得最远的一局：[{best['policy']}/{best['region']}] 种子{best['seed']}"
              f" 胜{best['battles_won']}场 到达第{best['furthest_battle']}场"
              f" 存活{best.get('rounds_survived', 0)}回合"
              + (" 冠冕封存" if best.get("crown_sealed") else "")
              + (" 触发死斗" if best.get("duel") else "")
              + (f" 死于第{best['death_battle']}场" if best.get("death_battle") else ""))
        print("（以下战报由同种子原样重跑产生，与批量结果完全一致）")
        print("=" * 70)
        trace = []
        traced_log = run_campaign(best["policy"], best["region"], best["seed"], trace=trace)
        # 一致性自检：追踪局必须与批量局结果相同，否则追踪不可信
        same = all(traced_log[k] == best[k] for k in
                   ("battles_won", "furthest_battle", "death_battle", "crown_sealed", "duel"))
        consistency = "一致" if same else f"不一致！批量:{best} 追踪:{traced_log}"
        trace.append("")
        trace.append(f"═══ 轨迹一致性自检：{consistency} ═══")

        jout = "data/furthest_run.json"
        with open(jout, "w", encoding="utf-8") as f:
            json.dump({"campaign": traced_log, "consistent_with_batch": same,
                       "transcript": trace}, f, ensure_ascii=False, indent=1)
        mout = "data/furthest_run.md"
        with open(mout, "w", encoding="utf-8") as f:
            f.write(f"# 走得最远的一次轮回（可复现：python3 simulate.py --runs {args.runs} --seed {args.seed}）\n\n")
            f.write(f"策略[{best['policy']}] 副本[{best['region']}] 种子{best['seed']}\n\n```\n")
            f.write("\n".join(trace))
            f.write("\n```\n")
        print("\n".join(trace))
        print(f"\n战报已写入 {mout} / {jout}")


if __name__ == "__main__":
    main()
