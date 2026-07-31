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
    跑完批量后，找出"走得最远"的一局（胜场最多→冠冕/死斗→到达场次最深→存活回合最多），
    以同种子原样重跑并全程记录：每次局外行动、每回合敌我双方每次出手、战终结算，
    输出 data/battle.md（标题 battle，格式遵循 README「推演」模板）与 data/battle.json。
    策略与随机源均为确定性实现，追踪局与批量局结果完全一致（可复现，附一致性自检）。

诚实声明：
- 怪物AI与本模拟器中的轮回者策略均为确定性启发式，非最优解；
- 事件系统/多数遗物/员工/副本专属行动未实装，不参与模拟；
- "受到伤害前"类反应法术由模拟器在回合开始（敌方出手前）主动开启，
  "失去生命后"类反应法术在检测到生命下降后立即开启——两者均为策略层近似时机；
- 随机数规则：本模拟器以种子化随机源扮演"提供数字的玩家/DM"角色。
"""
import argparse
import json
import math
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
# 战报渲染（README「推演」模板：战始配置→第N回合→战终；不影响结算）
# ============================================================

def pool_entry(engine: GameEngine, monster_name: str) -> Optional[dict]:
    """按实体名（可能带重复编号）查怪物池面板"""
    base = monster_name.rstrip("0123456789")
    for m in MONSTER_POOLS[engine.state.current_region]:
        if m["name"] == base:
            return m
    return None


def player_panel(engine: GameEngine) -> str:
    p = engine.state.player
    return (f"模拟者 生命{p.current_hp}/{p.blood_limit} 法力{p.current_mana}/{p.mana_limit}"
            f" 速度{p.current_speed}/{p.speed_limit} 格挡{p.shield}")


def enemy_panel(engine: GameEngine, alive_only: bool = True) -> str:
    ms = [m for m in engine.state.enemies if m.is_alive] if alive_only else engine.state.enemies
    if not ms:
        return "敌方：全体命零"
    return "、".join(f"{m.name} 生命{m.current_hp}/{m.blood_limit}" for m in ms)


def battle_config_lines(engine: GameEngine) -> list[str]:
    """战始配置：我方面板+敌方面板（README推演格式）"""
    p = engine.state.player
    budget = engine._player_action_budget()
    dw = "、".join(p.dao_wen.keys()) or "无"
    spells = "、".join(s.name for s in p.spells) or "无"
    resonance = "、".join(f"{k}×{v}" for k, v in engine.state.resonance.items() if v) or "无"
    relics = "、".join(r.name for r in engine.state.relics) or "无"
    lines = ["战始配置", ""]
    lines.append(
        f"模拟者 生命{p.current_hp}/{p.blood_limit} 法力{p.current_mana}/{p.mana_limit}（回始补满）"
        f" 速度{p.current_speed}/{p.speed_limit}（每回合出手{budget}，闪避每次耗1速，战终复原）"
        f" 道纹：{dw} 法术：{spells} 残韵：{resonance} 遗物：{relics}")
    for m in engine.state.enemies:
        entry = pool_entry(engine, m.name)
        dwtxt = "、".join(f"{k}{v}" for k, v in entry["daowen"].items()) if entry else "、".join(m.dao_wen)
        carry = f" 携带碎片{m.shards}" if m.shards else ""
        lines.append(f"{m.name} 生命{m.current_hp}/{m.blood_limit} 攻击{m.attack_count}×{m.attack_power}"
                     f" 道纹：{dwtxt}{carry} —— 白板：道纹须在其出手轮发动后生效")
    lines.append(f"战斗背景：{engine.state.battle_background}")
    lines.append("先手：对怪物战斗无先后手判定规则，按 回始→我方出手轮→敌方出手轮→回终 推进")
    return lines


def _hit_phrase(hit: dict, defender_hint: str = "") -> str:
    """一次命中的响应短语：闪避（速度a→b）/ 硬吃（生命a→b，格挡抵n）"""
    t = hit.get("target", defender_hint or "?")
    if hit.get("dodge_attempted") and hit.get("dodge_success"):
        return f"{t}闪避（速度{hit.get('speed_after_dodge', '?') + 1 if isinstance(hit.get('speed_after_dodge'), int) else '?'}→{hit.get('speed_after_dodge', '?')}）"
    if hit.get("blocked_by"):
        return f"{t}被[{hit['blocked_by']}]挡下"
    dmg = hit.get("damage_dealt", hit.get("actual_damage", 0))
    absorbed = hit.get("shield_absorbed", 0)
    hp_b = hit.get("hp_before")
    hp_a = hit.get("target_hp_after", hit.get("hp_after"))
    # take_damage语义：actual_damage即生命净损失（格挡已另计），可精确反推命中前生命
    if hp_b is None and isinstance(hp_a, int) and isinstance(dmg, int):
        hp_b = hp_a + dmg
    dod_txt = "（闪避失败）" if hit.get("dodge_attempted") else ""
    s = f"{t}{dod_txt}硬吃"
    if absorbed:
        s += f"（格挡抵{absorbed}）"
    s += f"受{dmg}伤害"
    if hp_b is not None and hp_a is not None:
        s += f" 生命{hp_b}→{hp_a}"
    elif hp_a is not None:
        s += f" 生命→{hp_a}"
    if hit.get("target_died", hit.get("died")):
        s += "【命零】"
    return s


def _cost_phrase(result: dict, caster_is_monster: bool) -> str:
    """消耗/代价短语，例如：耗14法力 / 流血16 生命64→48 / 面板道纹不耗法力；代价：异变15"""
    parts = []
    calc = result.get("calculation") or {}
    if calc.get("cost_type") == "消耗" and calc.get("cost"):
        parts.append(f"耗{calc['cost']}法力")
    applied = result.get("cost_applied") or []
    if applied:
        seg = []
        for c in applied:
            t = c.get("type")
            if t == "流血":
                seg.append(f"流血{c.get('amount')}")
            elif t == "衰老":
                seg.append(f"衰老{c.get('amount')}")
            elif t == "枯竭":
                seg.append(f"枯竭{c.get('amount')}")
            elif t == "萎缩":
                seg.append(f"萎缩{c.get('amount')}")
            elif t == "疲惫":
                seg.append(f"疲惫{c.get('amount')}")
            elif t == "异变":
                seg.append(f"异变{c.get('amount')}")
            elif t == "冷却":
                seg.append(f"冷却{c.get('cooldown_battles')}场")
            elif t == "唯一":
                seg.append("本次轮回唯一")
            else:
                seg.append(str(t))
        parts.append("代价：" + "、".join(seg))
    if not parts and caster_is_monster:
        parts.append("面板道纹不耗法力")
    return "；".join(parts)


def _effects_phrase(result: dict, engine: GameEngine) -> str:
    """效果短语：伤害/治疗/格挡/状态，命中用样本风格（硬吃/闪避+数值变化）"""
    parts = []
    ex = result.get("execution") or {}
    if ex.get("monster_triple"):
        parts.append("非专属×3")
    for e in ex.get("effects", []):
        t = e.get("type")
        if t in ("damage", "aoe_damage"):
            parts.append(_hit_phrase(e))
        elif t == "multi_hit_damage":
            s = f"{e.get('target')}受{e.get('hits')}次×1点连续伤害"
            hp_b, hp_a = e.get("hp_before"), e.get("target_hp_after", e.get("hp_after"))
            if hp_a is not None:
                s += (f" 生命{hp_b}→{hp_a}" if hp_b is not None else f" 生命→{hp_a}")
            parts.append(s + ("【命零】" if e.get("target_died", e.get("died")) else ""))
        elif t == "heal":
            parts.append(f"{e.get('target')}回复{e.get('actual_heal')}（生命{e.get('hp_before')}→{e.get('hp_after')}）")
        elif t == "shield":
            tgt = next((x for x in [engine.state.player] + engine.state.enemies + engine.state.friends
                        if x and x.name == e.get("target")), None)
            parts.append(f"{e.get('target')}获得格挡{e.get('amount')}（格挡→{tgt.shield if tgt else '?' }）")
        elif t == "status_added":
            dur = e.get("duration")
            dtxt = "持续∞" if dur in (-1, None) else f"持续{dur}回合"
            parts.append(f"{e.get('target')}获得[{e.get('status')}{e.get('value')}]（{dtxt}）")
        elif t == "speed_boost":
            parts.append(f"{e.get('target')}速度+{e.get('amount')}")
        elif t == "speed_halved":
            parts.append(f"{e.get('target')}速度减半→{e.get('speed_after')}")
        elif t == "mana_gain":
            parts.append(f"{e.get('source')}法力+{e.get('amount')}")
        elif t == "blood_limit_increase":
            parts.append(f"{e.get('target')}血限+{e.get('increase')}")
        elif t == "blood_limit_reduction":
            parts.append(f"{e.get('target')}血限-{e.get('amount')}")
        elif t == "fake_shards":
            parts.append(f"假碎片+{e.get('amount')}")
        elif t == "shard_steal":
            parts.append(f"夺取碎片{e.get('amount')}")
        elif t == "gain_flying":
            parts.append(f"{e.get('target')}升空")
        elif t == "ground_all":
            parts.append(f"落地:{e.get('grounded')}")
        elif t == "seal":
            parts.append(f"封印移出:{e.get('sealed')}（无碎片收益）")
        elif t == "self_attacks":
            parts.append("自残:" + "|".join(_hit_phrase(h) for h in e.get("hits", [])))
        else:
            kv = "，".join(f"{k}={v}" for k, v in e.items()
                          if k != "type" and not isinstance(v, (dict, list)))
            parts.append(f"{t}({kv})" if kv else str(t))
    return " ‖ ".join(parts)


def player_daowen_line(result: dict, engine: GameEngine, no: int) -> str:
    """我方一次出手：出手N：道纹X（耗法/代价 效果摘要）→ 目标响应"""
    calc = result.get("calculation") or {}
    title = f"{calc.get('dao_wen', '?')}{calc.get('x', '?')}"
    if result.get("dodge") and result["dodge"].get("success"):
        return (f"模拟者出手{no}：{title}（{_cost_phrase(result, False)}）"
                f"→ 目标闪避，判定与结算完全失效，未发生消耗")
    eff = _effects_phrase(result, engine)
    return f"模拟者出手{no}：{title}（{_cost_phrase(result, False)}）→ {eff or '（无可见效果）'}"


def player_spell_line(spell_name: str, result: dict, engine: GameEngine) -> str:
    """反应型法术（插队、不耗出手号）"""
    if not result.get("success"):
        return f"法术【{spell_name}】：发动失败:{result.get('error')}"
    segs = []
    for step in result.get("steps_executed", []) or []:
        if step.get("skipped"):
            segs.append(f"{step.get('step')}(跳过:{step['skipped']})")
            continue
        sub = ""
        for c in step.get("cost_applied") or []:
            amt = c.get("amount", c.get("note", c.get("cooldown_battles", "")))
            sub += f"代价[{c.get('type')}{amt}] "
        effs = []
        for e in step.get("execution") or []:
            effs.append(_effects_phrase({"execution": {"effects": [e]}}, engine))
        segs.append(f"{step.get('step')}{step.get('x', '?')}（{sub}{'；'.join(e for e in effs if e)}）")
    line = f"法术【{spell_name}】（{result.get('trigger_timing')}，插队不耗出手）：" + " → ".join(segs)
    if result.get("interrupted"):
        line += f"（中断：{result.get('interrupt_reason') or result.get('interrupt')}）"
    mana = result.get("player_mana_after")
    if mana is not None:
        line += f"（余法{mana}）"
    return line


def monster_act_lines(monster_name: str, result: dict, engine: GameEngine) -> list[str]:
    """怪物一次出手轮转写：每个act一条出手行"""
    if not result.get("success"):
        return [f"{monster_name}出手：提交被引擎拒绝:{result.get('error')}"]
    if result.get("skipped"):
        return [f"{monster_name}出手：{result['skipped']}"]
    lines = []
    no = 0
    for entry in result.get("turn_log", []):
        no += 1
        if entry.get("error"):
            lines.append(f"{monster_name}出手{no}：错误:{entry['error']}")
        elif entry.get("type") == "attack_round":
            hits = entry.get("hits", [])
            if hits:
                body = " ｜ ".join(_hit_phrase(h) for h in hits)
            else:
                body = "未命中"
            atk_desc = ""
            m = next((x for x in engine.state.enemies if x.name == monster_name), None)
            if m:
                n_hits = len(hits)
                atk_desc = f"（{n_hits}×{m.attack_power}）"
            lines.append(f"{monster_name}出手{no}：攻击{atk_desc} → {body}")
        elif entry.get("type") == "use_daowen":
            act_txt = entry.get("action") or ""
            title = act_txt.replace(f"{monster_name}发动道纹【", "").rstrip("】") or "道纹"
            eff = _effects_phrase(entry, engine)
            cost = _cost_phrase(entry, True)
            tail = f"（{cost}）→ {eff}" if eff else f"（{cost}）"
            lines.append(f"{monster_name}出手{no}：{title}{tail}")
        else:
            lines.append(f"{monster_name}出手{no}：{entry}")
    return lines


def round_hook_line(timing: str, effects: list, engine: GameEngine) -> str:
    """回始/回终效果行；无效果时如实写'无'"""
    parts = []
    for e in effects or []:
        t = e.get("type") if isinstance(e, dict) else None
        if t == "mana_refill":
            parts.append(f"{e.get('entity', '模拟者')}法力补满（{e.get('from')}→{e.get('to')}）")
        elif t == "shield_clear":
            parts.append(f"{e.get('entity')}格挡清空（-{e.get('cleared')}）")
        elif t == "mana_clear":
            parts.append(f"{e.get('entity')}法力清空（-{e.get('cleared')}）")
        elif t == "衰败伤害":
            s = f"{e.get('entity')}受{e.get('damage')}衰败伤害（生命→{e.get('hp_after')}）"
            parts.append(s + ("【命零】" if e.get("died") else ""))
        elif t == "清算格挡流失":
            parts.append(f"{e.get('entity')}清算流失格挡{e.get('shield_lost')}")
        elif t == "逼债碎片":
            parts.append(f"逼债：-{e.get('amount')}碎片（余{e.get('shards_after')}）")
        elif t == "逼债血限":
            parts.append(f"逼债：血限-{e.get('amount')}（余{e.get('blood_limit_after')}）")
        elif t == "活血回复":
            parts.append(f"{e.get('entity')}活血回复{e.get('heal')}")
        elif t == "self_heal":
            parts.append(f"{e.get('entity')}自愈回复{e.get('actual')}")
        elif t == "deform_blood_loss":
            s = f"{e.get('entity')}畸变血限-{e.get('blood_loss')}（余{e.get('blood_limit_after')}）"
            parts.append(s + ("【命零】" if e.get("died") else ""))
        elif t == "洞察法力":
            parts.append(f"洞察：{e.get('entity')}法力+{e.get('amount')}")
        elif t == "status_expired":
            parts.append(f"{e.get('entity')}持续到期：{e.get('expired_effects', e.get('status'))}")
        elif t == "extra_attack_ready":
            continue  # 狂暴标记属内部状态，不影响叙事
        elif isinstance(e, dict):
            kv = "，".join(f"{k}={v}" for k, v in e.items()
                          if k != "type" and not isinstance(v, (dict, list)))
            parts.append(f"{t}({kv})" if kv else str(t))
        else:
            parts.append(str(e))
    return f"{timing}：" + ("；".join(parts) if parts else "无")


# ============================================================
# 理性工具（策略层公共服务：任何行动都必须有实际收益，禁止演傻）
# ============================================================

def shard_reserve(engine: GameEngine) -> int:
    """碎片留存量：持买路财时必留撤退基金，否则留小额应急"""
    return 90 if any(r.name == "买路财" for r in engine.state.relics) else 25


def pick_rest_tier(engine: GameEngine) -> Optional[int]:
    """休整决策：满血/近满血不执行（返回None）；否则选能覆盖缺口的最小可负担档"""
    p = engine.state.player
    missing = p.blood_limit - p.current_hp
    if missing <= 0:
        return None
    tier_map = {1: (8, 0), 2: (24, 10), 3: (48, 25)}
    for tier in (1, 2, 3):
        heal, cost = tier_map[tier]
        if engine.state.shards - shard_reserve(engine) >= cost and heal >= min(missing, 8):
            return tier
    return 1 if missing >= 8 else None  # t1免费，缺口≥8才值得花精力


def pick_train_tier(engine: GameEngine) -> int:
    """修行决策：在留存线以上选最大可负担档（碎片捂到死不如换成数值）"""
    tier_map = {1: 0, 2: 15, 3: 35, 4: 65, 5: 100, 6: 150}
    budget = engine.state.shards - shard_reserve(engine)
    best = 1
    for tier, cost in tier_map.items():
        if budget >= cost:
            best = tier
    return best


def expected_incoming_damage(engine: GameEngine) -> int:
    """保守评估本回合敌方火力：闪避预算内优先闪高攻，剩余命中+必中全额计入"""
    p = engine.state.player
    dodgeable = []       # 可闪避命中的单次伤害列表
    unavoidable = 0      # 必中伤害
    for m in engine.state.enemies:
        if not m.is_alive or m.has_status("束缚") or m.has_status("眩晕"):
            continue
        n = 1 if m.has_status("迟滞") else m.attack_count
        if m.has_status("必中"):
            unavoidable += n * m.attack_power
        else:
            dodgeable.extend([m.attack_power] * n)
    dodgeable.sort(reverse=True)
    budget = p.current_speed if p else 0
    return unavoidable + sum(dodgeable[budget:])


def try_retreat(engine: GameEngine, trace: Optional[list] = None) -> bool:
    """买路财撤退决策：预测本回合必死且付得起撤退费时才跑（不白跑、不送死）"""
    p = engine.state.player
    if not any(r.name == "买路财" for r in engine.state.relics):
        return False
    if engine.state.phase != "in_combat" or not p.is_alive:
        return False
    alive = [m for m in engine.state.enemies if m.is_alive]
    if not alive:
        return False

    incoming = expected_incoming_damage(engine)
    if incoming < p.current_hp + p.shield:
        return False  # 扛得住，不浪费碎片

    cost = sum(math.ceil(m.blood_limit * 0.2) for m in alive)
    shards_part = min(engine.state.shards, cost)
    remainder = cost - shards_part
    hp_pay = 0
    blood_pay = 0
    if remainder > 0:
        hp_pay = min(remainder * 2, p.current_hp - 1)      # 2生命=1碎片，留1滴血
        left = remainder - math.ceil(hp_pay / 2)
        if left > 0:
            # 血限1:1补足，且扣后血限必须≥扣后生命且≥1（禁止为了跑先跑死自己）
            blood_pay = left
            if p.blood_limit - blood_pay < max(1, p.current_hp - hp_pay):
                if trace is not None:
                    trace.append(f"（必死局想买路财撤退，但付不起：需{cost}碎片等价，"
                                 f"碎片{engine.state.shards}/生命{p.current_hp}/血限{p.blood_limit}不足，"
                                 f"只能死战——如实记录这个绝望决策）")
                return False
    r = engine.execute_action("retreat", {"hp_pay": hp_pay, "blood_limit_pay": blood_pay})
    if trace is not None:
        det = (r.get("result") or {}).get("retreat") or {}
        trace.append(f"⚠ 预判本回合承伤{incoming}≥生命{p.current_hp}+格挡{p.shield}，买路财撤退："
                     f"费{det.get('cost_shards_equivalent', cost)}碎片等价"
                     f"（碎片{det.get('shards_paid', shards_part)}"
                     f"{f'＋生命{hp_pay}' if hp_pay else ''}{f'＋血限{blood_pay}' if blood_pay else ''}）"
                     f"→ 本场无碎片收益，命保住进下一场")
    return r.get("success", False)



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
        if self.name == "custom":
            # 自创法术策略：用同一条积木管线组装库外法术
            plans = {
                1: [
                    {"sub_action": "学习", "learn_type": "transform_daowen", "names": ["再生"], "tier": 1},
                    {"sub_action": "学习", "learn_type": "transform_daowen", "names": ["庇护"], "tier": 1},
                    {"sub_action": "学习", "learn_type": "create_spell",
                     "name": "后发先至", "trigger": "受到伤害前",
                     "steps": [{"daowen": "庇护", "x_param": "x", "target": "self"},
                               {"daowen": "杀伐", "x_param": "y", "target": "enemy"}]},
                ],
                2: [
                    {"sub_action": "学习", "learn_type": "transform_daowen", "names": ["固执"], "tier": 1},
                    {"sub_action": "学习", "learn_type": "transform_daowen", "names": ["血债"], "tier": 1},
                    {"sub_action": "学习", "learn_type": "create_spell",
                     "name": "血色狂潮", "trigger": "失去生命后", "loop": True,
                     "steps": [{"daowen": "再生", "x_param": "x", "target": "self"},
                               {"daowen": "血债", "x_param": "y", "target": "enemy"}]},
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
                use = ("庇护", max(1, mana - 2), player.name)   # 增益打自己，不打敌人
            elif "杀伐" in player.dao_wen:
                use = ("杀伐", mana, target.name)
            elif "再生" in player.dao_wen and player.current_hp < player.blood_limit * 0.5:
                use = ("再生", max(1, mana // 2), player.name)
            if use is None:
                break
            r = engine.execute_action("use_daowen", {
                "daowen_name": use[0], "x": use[1], "target": use[2],
            })
            if trace is not None:
                if r.get("success"):
                    trace.append(player_daowen_line(r, engine, engine.state.actions_used))
                else:
                    trace.append(f"模拟者出手{engine.state.actions_used + 1}：【{use[0]}】发动失败:{r.get('error')}")
            if not r.get("success"):
                break
            enemies = engine.state.get_all_enemy_side()
        if trace is not None and engine.state.actions_used == 0 and player.is_alive:
            trace.append("模拟者出手：0法力或无可用道纹，无法出手")

    # ---- 反应型法术（策略层近似触发时点）----
    @staticmethod
    def cast_reaction_spells(engine: GameEngine, timing: str, trace: Optional[list] = None) -> bool:
        """按法术触发时点开启反应法术（自创法术与法术库法术一视同仁，同一条积木管线）"""
        player = engine.state.player
        if not player or not player.is_alive:
            return False
        for s in list(player.spells):
            spec = engine._spell_spec(s.name)
            if not spec or spec.get("trigger") != timing:
                continue
            if player.current_mana < 2:
                continue
            enemies = engine.state.get_all_enemy_side()
            mana = player.current_mana
            r = engine.execute_action("use_spell", {
                "spell_name": s.name,
                "trigger_timing": timing,
                "target": enemies[0].name if enemies else player.name,
                "x": max(1, mana // 2), "y": 1, "z": 1,
            })
            if trace is not None:
                trace.append(player_spell_line(s.name, r, engine))
            return r.get("success", False)
        return False

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
            entry = pool_entry(engine, monster.name)
            if entry:
                panel_x = entry["daowen"].get(dw, 1)
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

    trace 不为 None 时，向其中逐条追加战报行（README推演格式；不影响结算，同种子结果一致）。
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
        # 发现3选1：优先已实装遗物（拿摆设没有意义）
        from engine.gamedata import RELIC_POOL
        impl = {r["name"] for r in RELIC_POOL if r.get("implemented")}
        pick = next((c for c in last["candidates"] if c in impl), last["candidates"][0])
        engine.execute_action("discover_relic_setup", {"chosen": pick})
        chosen_relic = engine._last_result.get("result", {}).get("relic")

    log = {"policy": policy_name, "region": region, "seed": seed,
           "battles_won": 0, "furthest_battle": 0, "death_battle": None,
           "rounds_survived": 0, "escapes": 0,
           "crown_sealed": False, "duel": None, "relics": []}
    log["relics"] = [r.name for r in engine.state.relics]

    p0 = engine.state.player
    T(f"轮回开始：生命60/60 速度8/8 法力14/14（25属性点按10/8/7分配）"
      f" 初始道纹：杀伐 残韵：反转×1 碎片20 副本：{region} 策略：{policy_name}")
    if last.get("candidates"):
        T(f"发现遗物：候选{last['candidates']} → 选择【{chosen_relic}】")

    last_hp = p0.current_hp  # “失去生命后”反应法术的策略层检测基线

    for battle_no in range(1, REGION_BATTLE_COUNT + 1):
        log["furthest_battle"] = battle_no
        if verbose:
            print(f"\n--- 第{battle_no}场 | HP {engine.state.player.current_hp}/{engine.state.player.blood_limit}"
                  f" 法限{engine.state.player.mana_limit} 速限{engine.state.player.speed_limit}"
                  f" 碎片{engine.state.shards} 道纹{list(engine.state.player.dao_wen.keys())}")
        T("")
        title = f"第{battle_no}场战斗"
        if battle_no == 8:
            title += "  最终死斗"
        T(f"━━ {title} ━━━━━━━━━━━━━━━━━━━━")

        # ---- 局外：计划逐一合理化（满血不休整、碎片在留存线以上就买数值），剩余精力兜底 ----
        pre_lines = []
        for act in policy.pre_battle_plan(engine, battle_no):
            if engine.state.energy <= 0:
                break
            sane = dict(act)
            if sane.get("sub_action") == "休整":
                tier = pick_rest_tier(engine)
                if tier is None:
                    pre_lines.append("休整跳过：血量已满/无缺口，不白烧精力")
                    continue
                sane["tier"] = tier
            elif sane.get("sub_action") == "修行":
                sane["tier"] = max(sane.get("tier", 1), pick_train_tier(engine))
            r = engine.execute_action("pre_battle_action", sane)
            handle_pending(engine, rng, verbose)
            if r.get("success"):
                pre_lines.append(_condense_pre_battle(sane, r, engine))
            else:
                pre_lines.append(f"{sane.get('sub_action', '?')}失败:{r.get('error', '?')}")
            if sane.get("sub_action") == "修行" and r.get("success"):
                pts = engine.state.attribute_points
                if pts > 0:
                    # 交替加速度与法力
                    if engine.state.player.speed_limit <= engine.state.player.mana_limit:
                        r2 = engine.execute_action("spend_attribute_points", {"to": "速限", "points": pts})
                    else:
                        r2 = engine.execute_action("spend_attribute_points", {"to": "法限", "points": pts})
                    rr = r2.get("result", {})
                    pre_lines.append(
                        f"→属性点分配：{pts}点→{rr.get('to')}"
                        f"（速限{rr.get('speed_limit')} 法限{rr.get('mana_limit')} 每回合出手{rr.get('action_count')}）")
        # 兜底：精力不能空转——该回血回血，该买数值买数值，最后才领悟烧掉
        while engine.state.energy > 0:
            tier = pick_rest_tier(engine)
            if tier is not None:
                r = engine.execute_action("pre_battle_action", {"sub_action": "休整", "tier": tier})
                pre_lines.append(_condense_pre_battle({"sub_action": "休整", "tier": tier}, r, engine))
                continue
            tier = pick_train_tier(engine)
            r = engine.execute_action("pre_battle_action", {"sub_action": "修行", "tier": tier})
            if r.get("success"):
                pre_lines.append(_condense_pre_battle({"sub_action": "修行", "tier": tier}, r, engine))
                pts = engine.state.attribute_points
                if pts > 0:
                    if engine.state.player.speed_limit <= engine.state.player.mana_limit:
                        r2 = engine.execute_action("spend_attribute_points", {"to": "速限", "points": pts})
                    else:
                        r2 = engine.execute_action("spend_attribute_points", {"to": "法限", "points": pts})
                    rr = r2.get("result", {})
                    pre_lines.append(
                        f"→属性点分配：{pts}点→{rr.get('to')}"
                        f"（速限{rr.get('speed_limit')} 法限{rr.get('mana_limit')} 每回合出手{rr.get('action_count')}）")
                continue
            r = engine.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "反转"})
            if r.get("success"):
                pre_lines.append(_condense_pre_battle({"sub_action": "领悟"}, r, engine))
            else:
                break
        if trace is not None:
            T("局外（精力3→0）：")
            for ln in pre_lines:
                T(f"  {ln}")
            T("")

        # ---- 战始 ----
        engine.execute_action("battle_start", {"battle_background": "模拟背景"})
        handle_pending(engine, rng, verbose)
        if trace is not None and engine.state.phase == "in_combat":
            trace.extend(battle_config_lines(engine))
            T("")
        if engine.state.phase != "in_combat":
            log["death_battle"] = battle_no
            T("战始结算异常，未能进入战斗（如实记录）")
            break

        # ---- 回合循环 ----
        max_rounds = 60
        finished = False
        escaped = False
        for round_i in range(1, max_rounds + 1):
            rs = engine.execute_action("round_start", {})
            log["rounds_survived"] += 1
            T(f"第{engine.state.current_round}回合")
            if trace is not None:
                hook = round_hook_line("回始", ((rs.get("result") or {}).get("effects")), engine)
                T(f"{hook} ｜ {player_panel(engine)} ｜ {enemy_panel(engine)}")

            # 买路财：预判本回合必死且付得起才撤退（不白跑、不揣着钱送死）
            if try_retreat(engine, trace):
                finished = True
                escaped = True
                log["escapes"] += 1
                break

            # 反应型法术："受到伤害前"类仅在威胁实质化时开启（不对挠痒威胁空烧法力）
            p_now = engine.state.player
            threat = expected_incoming_damage(engine)
            if threat > p_now.current_hp + p_now.shield * 2:
                Policy.cast_reaction_spells(engine, "受到伤害前", trace)

            policy.player_acts(engine, trace)

            for monster in list(engine.state.enemies):
                if not monster.is_alive or not engine.state.player.is_alive:
                    continue
                acts = policy.monster_acts(engine, monster)
                r = engine.execute_action("monster_turn", {"monster": monster.name, "acts": acts})
                if trace is not None:
                    trace.extend(monster_act_lines(monster.name, r, engine))
                # "失去生命后"反应：生命每出现新的下降，立即开启（策略层近似）
                p = engine.state.player
                if p.is_alive and p.current_hp < last_hp:
                    hp_mark = p.current_hp
                    Policy.cast_reaction_spells(engine, "失去生命后", trace)
                    last_hp = engine.state.player.current_hp if engine.state.player.is_alive else hp_mark
                elif p.is_alive and p.current_hp > last_hp:
                    last_hp = p.current_hp

            r = engine.execute_action("round_end", {})
            ended = handle_pending(engine, rng, verbose)
            if trace is not None:
                p = engine.state.player
                diff = r.get("monster_difficulties") or []
                diff_txt = f"；怪物困境检查触发：{[d.get('monster') for d in diff]}" if diff else ""
                T(round_hook_line("回终", ((r.get("result") or {}).get("effects")), engine)
                  + f" ｜ {player_panel(engine)} ｜ {enemy_panel(engine)}{diff_txt}")
                if p and p.current_hp > last_hp:
                    last_hp = p.current_hp
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
                    det_parts = []
                    for d in body.get("reward_detail", []):
                        seg = f"{d['monster']}（战始血限2%={d['base_2pct_spawn_blood']}＋道纹×5={d['daowen_bonus']}"
                        if d.get("moneybag_bonus"):
                            seg += f"＋钱袋2%={d['moneybag_bonus']}"
                        seg += f"）→+{d['total']}"
                        det_parts.append(seg)
                    T("")
                    T(f"战终：敌方全体命零 → 死亡结算：{'；'.join(det_parts) or '无'}"
                      f" → 碎片+{body.get('shard_reward', 0)}（总{body.get('total_shards')}）"
                      f" → 增益减益清除/代价保留/精力恢复3")
                    crown = body.get("crown")
                    if crown:
                        T(f"👑 最终的冠冕触发：{crown}")
                break

        if not finished or log["death_battle"]:
            if not log["death_battle"]:
                log["death_battle"] = battle_no
            if trace is not None:
                p = engine.state.player
                T("")
                T(f"模拟者命零（生命{p.current_hp}/{p.blood_limit}）无回复手段 死亡")
                T("☠ 死之传承中断触发 → DM裁定 → 轮回结束（本轮回胜"
                  f"{log['battles_won']}场，死于第{battle_no}场第{engine.state.current_round}回合）")
            break

        if engine.state.phase == "game_over":
            log["crown_sealed"] = True
            T("👑 冠冕已封存：轮回者及其状态完整封存，等待下一名完成者（本模拟器止步于此）")
            break
        if engine.state.phase == "dead_duel":
            log["duel"] = "triggered"
            T("⚔ 存在封存候选 → 第8场战斗 最终死斗触发（死斗按先手规则推演，交由DM接管，本模拟器不结算）")
            break

    if engine.state.phase == "game_over" and engine.state.player.is_alive:
        log["crown_sealed"] = True

    return log


def _condense_pre_battle(act: dict, result: dict, engine: GameEngine) -> str:
    """局外行动一行摘要"""
    sub = act.get("sub_action", "?")
    r = result.get("result") or {}
    if sub == "休整":
        alloc = "、".join(f"{a['target']} 生命{a['hp']}" for a in r.get("allocated", []))
        cost = f"，-{r['shard_cost']}碎片" if r.get("shard_cost") else ""
        return f"休整(档{act.get('tier', 1)})：恢复{r.get('heal_pool')}→{alloc}{cost}"
    if sub == "修行":
        return (f"修行(档{act.get('tier', 1)})：+{r.get('points_gained')}属性点"
                + (f"，-{r['shard_cost']}碎片" if r.get("shard_cost") else ""))
    if sub == "学习":
        lt = r.get("learn_type") or ("create_spell" if "spec" in r else None)
        if lt == "create_spell":
            spec = r.get("spec") or {}
            flow = "→".join(f"{s['daowen']}({s['x_param']}→{s['target']})" for s in spec.get("steps", []))
            loop = "，循环法则" if spec.get("loop") else ""
            return (f"自创法术【{spec.get('name')}】（触发:{spec.get('trigger')}；积木:{flow}{loop}）"
                    f" -0碎片")
        if lt == "spell":
            cost = f"，-{r.get('shard_cost', 0)}碎片" if r.get("shard_cost") else ""
            return f"学习法术：{r.get('learned')}{cost}"
        if lt == "transform_daowen":
            got = "、".join(x["name"] for x in r.get("learned", []) if isinstance(x, dict)) or str(r.get("learned"))
            cost = f"，-{r.get('shard_cost', 0)}碎片" if r.get("shard_cost") else ""
            return f"习得转化道纹：{got}{cost}"
        return f"学习：{r}"
    if sub == "领悟":
        return f"领悟：残韵[{r.get('gained_resonance')}]×{r.get('total')}"
    if sub == "忘忧":
        return f"忘忧(档{act.get('tier', 1)})：失忆{r.get('forgot_daowen')}→+{r.get('shards_gained')}碎片（余{r.get('shards_total')}）"
    if sub == "共鸣":
        return f"共鸣：{result.get('action', '')}"
    return f"{sub}：{r}"


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
    policies = ["naive_dps", "balanced", "combo", "custom"]

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

        jout = "data/battle.json"
        with open(jout, "w", encoding="utf-8") as f:
            json.dump({"campaign": traced_log, "consistent_with_batch": same,
                       "transcript": trace}, f, ensure_ascii=False, indent=1)
        # ---- 附录：自创法术策略走得最远的一局（证明自创管线真实参与模拟）----
        custom_logs = [l for l in all_logs if l["policy"] == "custom"]
        appendix = []
        if custom_logs:
            cbest = pick_furthest(custom_logs)
            appendix.append(f"附：自创法术策略[custom]走得最远的一局 "
                            f"（[{cbest['region']}] 种子{cbest['seed']} 胜{cbest['battles_won']}场"
                            f" 到达第{cbest['furthest_battle']}场）")
            appendix.append("")
            ctrace = []
            clog = run_campaign("custom", cbest["region"], cbest["seed"], trace=ctrace)
            csame = all(clog[k] == cbest[k] for k in
                        ("battles_won", "furthest_battle", "death_battle", "crown_sealed", "duel"))
            appendix.extend(ctrace)
            appendix.append("")
            appendix.append(f"═══ 附录轨迹一致性自检：{'一致' if csame else '不一致！'} ═══")

        mout = "data/battle.md"
        with open(mout, "w", encoding="utf-8") as f:
            f.write("# battle\n\n")
            f.write(f"走得最远的一次轮回（可复现：`python3 simulate.py --runs {args.runs}"
                    f" --seed {args.seed}`）\n\n")
            f.write(f"策略[{best['policy']}] 副本[{best['region']}] 种子{best['seed']}\n\n```\n")
            f.write("\n".join(trace))
            f.write("\n```\n")
            if appendix:
                f.write("\n---\n\n```\n")
                f.write("\n".join(appendix))
                f.write("\n```\n")
        with open(jout, "w", encoding="utf-8") as f:
            json.dump({"campaign": traced_log, "consistent_with_batch": same,
                       "transcript": trace, "custom_policy_appendix": appendix},
                      f, ensure_ascii=False, indent=1)
        print("\n".join(trace))
        print(f"\n战报已写入 {mout} / {jout}")


if __name__ == "__main__":
    main()
