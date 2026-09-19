"""
道纹效果结算 apply_daowen_effect 与内置法术流程表

从 engine/combat.py 原样抽出（方法体一字未改），由 CombatEngine 以 Mixin 方式继承：
对外仍是 `engine.combat.CombatEngine`，调用方与 API 契约均不受影响。
本模块的方法只能通过 CombatEngine 实例使用（依赖 self.state / self._emit 等引擎成员）。
"""
from __future__ import annotations
import math
import weakref
from typing import Optional, Any
from ..models import Entity, StatusEffect, GameState, DaoWenInstance, DaoWen, Spell, Consumable
from ..daowen import DaoWenEngine, ResonanceEngine
from ..dice import DiceEngine
from ..enums import (ActionPhase, TriggerTiming, InterruptType, DamageType,
                    EffectScope, EffectPolarity, CostType)
from ..dm_rulings import Interrupt
from ..combat_events import CombatEvent, CombatEventType, register_combat_event_observer
from ..combat_hooks import CombatHookManager
from ..effect_context import EffectContext, make_context, normalize_context
from ..mechanisms import MECHANISMS, Phase, TriggerBus, TriggerContext
from ..personality import remove_personality
# 常量定义在 models（授予点在 Entity.__post_init__），此处只读取以判定效果。
from ..models import MONSTER_MANA_RELIC


class DaowenEffectMixin:
    """道纹效果结算 apply_daowen_effect 与内置法术流程表（CombatEngine 的组成部分）"""

    # ========== 辅助方法 ==========
    
    def apply_daowen_effect(
        self, name: str, calc: dict, caster: Entity, target: Entity,
        dragon_heart_use: int = 0, *, cost_share_target_ref: str = "",
        aoe_targets_override: Optional[list[Entity]] = None,
    ) -> dict:
        """应用道纹效果；aoe_targets_override用于绑定两阶段决策的目标快照。"""
        result = {"daowen": name, "effects": []}
        x = calc.get("x", 0)
        if name in ("自食", "固执"):
            target = caster
        # 本次道纹结算的根上下文。由它派生的血限/生命/命零变化都以此为父事件，
        # 这样「杀伐 → 伤害 → 血限下降 → 命零」在链上是连续的。
        daowen_ctx = make_context(
            timing=self._current_context_timing(),
            source=name, source_type="daowen",
            actor=caster, target=target, owner=caster,
            mechanic="daowen_resolution", subtype=name, amount=x,
            tags={"daowen"},
        )
        result["daowen_ctx"] = daowen_ctx.to_dict()

        # ---- 波及X（2026-08-21）：你发动的道纹同时作用于所有**由你**挂上波及效果的目标（别人挂在你身上的标记不影响你自己的道纹）；[目标]选自己时你自己也保留一份，波及目标各额外得一份；2026-09-19 用户裁定 D10 选案 A 落地，见 报告.md 6.21 ----
        # 数值型效果的总数值在所有目标（本次[目标]+波及目标，均排除施法者自身）间平分，
        # 余数随机分配；状态类效果对波及目标原样生效。多目标不复制或增加总数值。
        # 目标可选的道纹（如【变形】）未指定目标时兜底为施法者，避免 [None]
        wave_status_targets: list[Entity] = [target if target else caster]
        wave_pieces: dict[str, list[int]] = {}
        wave_targets: list[Entity] = []
        if name != "波及":
            wave_targets = self._wave_targets(caster)
            if wave_targets:
                effective: list[Entity] = []
                # 用户裁定 2026-09-19（选案A）：本次[目标]是施法者自己时，施法者**保留在名单里**。
                # 旧写法把 `target is not caster` 当过滤条件，于是自施时名单里只剩波及目标：
                # 自我增益类道纹的状态整体跑到对方身上、自己一点拿不到（与下方注释「含本次[目标]」
                # 相反，也让 summary 的「使某某……」与事实相反）。现行口径＝「你发动的道纹同时
                # 作用于所有拥有波及效果的目标」：自己那一份不丢，波及目标额外各得一份；
                # 代价是数值型在自施时也会开始平分（用户裁定时已认可：怕波及当内鬼就不用波及）。
                # 去重按同一性（Entity 不可哈希，与下方【缄默】全场名单同一写法）。
                for wt in ([target] if target is not None else [caster]) + wave_targets:
                    if wt.is_alive and not any(wt is seen for seen in effective):
                        effective.append(wt)
                if effective:
                    wave_status_targets = effective
                    numeric_keys = [k for k in self.WAVE_NUMERIC_KEYS if k in calc]
                    if numeric_keys and len(effective) >= 2:
                        wave_pieces = {k: self._divide_flat(calc[k], len(effective))
                                       for k in numeric_keys}
                        result["wave_spread"] = {
                            "targets": [e.name for e in effective],
                            "pieces": {k: list(v) for k, v in wave_pieces.items()},
                        }
                    elif len(effective) >= 2:
                        result["wave_spread"] = {
                            "targets": [e.name for e in effective], "status_only": True}

        # ---- 乱葬岗·附煞（sha_qi）效果修正 ----
        # 施法者持有的道纹实例可能带煞气；对消耗/持续/伤害/回复做代数修正。
        inst = caster.dao_wen.get(name)
        sha = getattr(inst, "sha_qi", "") if inst is not None else ""
        if sha:
            if sha == "法煞" and calc.get("cost", 0) > 0:
                calc["cost"] = max(calc.get("x", 1), calc["cost"] - calc.get("x", 1))
            if sha == "魂煞" and calc.get("duration") not in (None, -1):
                calc["duration"] += calc.get("x", 1)
            if sha == "冥煞":
                for k in ("target_damage", "total_damage", "aoe_damage"):
                    if k in calc:
                        calc[k] = calc[k] * 2
            if sha == "血煞" and "target_heal" in calc:
                calc["target_heal"] *= 2
            if sha == "心煞":
                result["sha_qi_cooldown_boost"] = True

        # 【冷却X】代价：规则正文「冷却X：使用后该道纹记为【X(0)/Y】，[战终]后已完成
        # 战斗场数+1，达到Y时才能再次使用」。此前从未写入 cooldown_remaining，
        # 导致 固执/束缚/畸变/全速 可在同一场里无限重复发动（束缚因此支配全局）。
        if calc.get("cost_type") == "冷却":
            inst = caster.dao_wen.get(name)
            if inst is not None:
                inst.cooldown_remaining = max(inst.cooldown_remaining,
                                              int(calc.get("cost", 0)))
                result["cooldown_set"] = inst.cooldown_remaining

        # 【唯一】代价：规则正文「唯一：使用后，本次轮回中无法再次使用」。
        # 此前该代价种类只有一行名字映射、没有任何结算逻辑，等于空定义；
        # 现在与【冷却X】同处落账——唯一是轮回级一次性，跨战斗场数不恢复。
        if calc.get("cost_type") == "唯一":
            inst = caster.dao_wen.get(name)
            if inst is not None:
                inst.spent_unique = True
                result["unique_spent"] = True

        # 蒙蔽(施法者伤害类道纹归零) / 坏死/镇尸(目标禁疗)
        mengbi_blocked = caster.has_status("蒙蔽") and ("target_damage" in calc or "aoe_damage" in calc)
        if mengbi_blocked:
            for s in caster.status_effects:
                if s.name == "蒙蔽" and s.value > 0:
                    s.value -= 1
                    if s.value <= 0: caster.status_effects.remove(s)
                    break
            result["mengbi_blocked"] = True
        huaisi_block = self._heal_blocked(target) and "target_heal" in calc

        # ---- 逆鳞加成（F2）：施法者若有层数，下次伤害+层数后清空 ----
        nilin_bonus = 0
        if hasattr(caster, "_nilin") and getattr(caster, "_nilin", 0) > 0 and any(k in calc for k in ("target_damage", "total_damage", "aoe_damage", "hp_percent_loss")):
            nilin_bonus = caster._nilin
            caster._nilin = 0
            result["nilin_bonus"] = nilin_bonus
            # 状态层数虽清空，但 status 本身仍按 duration 存在（仅清空计数）

        # ---- 伤害类 ----
        if "target_damage" in calc:
            base = calc["target_damage"] + (nilin_bonus if nilin_bonus else 0)
            base = self._jieli_boost(caster, base)
            if caster.has_status("坠落") and base > 0:
                base = math.ceil(base / 2)
            dmg_amount = 0 if mengbi_blocked else base
            if "target_damage" in wave_pieces:
                # 波及：修正后总数值平分（余数随机分配），逆鳞已计入首段总值。
                pieces = self._divide_flat(dmg_amount, len(wave_status_targets))
                wave_pieces["target_damage"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    piece = 0 if mengbi_blocked else piece
                    dmg = self._apply_hostile_damage(
                        wt, piece, source=caster,
                        ctx={"timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                             "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                             "mechanic": "damage", "subtype": "daowen", "amount": piece,
                             "tags": {"daowen", "wave"}})
                    result["effects"].append({"type": "damage", "target": wt.name, **dmg})
                    if dmg.get("actual_damage", 0) > 0:
                        caster.damage_dealt_this_round += dmg["actual_damage"]
                nilin_bonus = 0
            else:
                dmg = self._apply_hostile_damage(
                    target, dmg_amount, source=caster,
                    ctx={"timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                         "source": name, "source_type": "daowen", "actor": caster, "target": target,
                         "mechanic": "damage", "subtype": "daowen", "amount": dmg_amount,
                         "tags": {"daowen"}})
                result["effects"].append({"type": "damage", "target": target.name, **dmg})
                if dmg.get("actual_damage", 0) > 0:
                    caster.damage_dealt_this_round += dmg["actual_damage"]
        if name == "血债" or ("hits" in calc and calc.get("damage_per_hit") == 1 and "target_damage" not in calc):
            hits = calc.get("hits", 1)
            if "hits" in wave_pieces:
                # 波及：总命中次数平分，每个目标的份额每次1点伤害。
                for wt, piece in zip(wave_status_targets, wave_pieces["hits"]):
                    total_act = 0
                    total_abs = 0
                    for _ in range(piece):
                        if not wt.is_alive:
                            break
                        hit_amount = 0 if mengbi_blocked else 1
                        dmg_i = self._apply_hostile_damage(
                            wt, hit_amount, source=caster,
                            ctx={"timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                                 "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                                 "mechanic": "damage", "subtype": "daowen_hit", "amount": hit_amount,
                                 "tags": {"daowen", "multi_hit", "wave"}})
                        total_act += dmg_i.get("actual_damage", 0)
                        total_abs += dmg_i.get("shield_absorbed", 0)
                    dmg = {"raw_damage": piece, "actual_damage": total_act, "shield_absorbed": total_abs,
                           "hp_after": wt.current_hp, "died": not wt.is_alive}
                    result["effects"].append({"type": "damage", "target": wt.name, **dmg})
                    if total_act > 0:
                        caster.damage_dealt_this_round += total_act
            else:
                total_act = 0
                total_abs = 0
                for _ in range(hits):
                    if not target.is_alive:
                        break
                    hit_amount = 0 if mengbi_blocked else 1
                    dmg_i = self._apply_hostile_damage(
                        target, hit_amount, source=caster,
                        ctx={"timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                             "source": name, "source_type": "daowen", "actor": caster, "target": target,
                             "mechanic": "damage", "subtype": "daowen_hit", "amount": hit_amount,
                             "tags": {"daowen", "multi_hit"}})
                    total_act += dmg_i.get("actual_damage", 0)
                    total_abs += dmg_i.get("shield_absorbed", 0)
                dmg = {"raw_damage": hits, "actual_damage": total_act, "shield_absorbed": total_abs,
                       "hp_after": target.current_hp, "died": not target.is_alive}
                result["effects"].append({"type": "damage", "target": target.name, **dmg})
                if total_act > 0:
                    caster.damage_dealt_this_round += total_act
        elif "total_damage" in calc and "target_damage" not in calc:  # 其他多段
            add = nilin_bonus
            nilin_bonus = 0
            chunk = self._jieli_boost(caster, calc["total_damage"] + add)
            if caster.has_status("坠落") and chunk > 0:
                chunk = math.ceil(chunk / 2)
            dmg_amount = 0 if mengbi_blocked else chunk
            if "total_damage" in wave_pieces:
                pieces = self._divide_flat(dmg_amount, len(wave_status_targets))
                wave_pieces["total_damage"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    piece = 0 if mengbi_blocked else piece
                    dmg = self._apply_hostile_damage(wt, piece, source=caster, ctx={
                        "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                        "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                        "mechanic": "damage", "subtype": "daowen", "amount": piece,
                        "tags": {"daowen", "wave"},
                    })
                    if add:
                        dmg["nilin_bonus"] = add
                        add = 0
                    result["effects"].append({"type": "damage", "target": wt.name, **dmg})
                    if dmg.get("actual_damage", 0) > 0:
                        caster.damage_dealt_this_round += dmg["actual_damage"]
            else:
                dmg = self._apply_hostile_damage(target, dmg_amount, source=caster, ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": target,
                    "mechanic": "damage", "subtype": "daowen", "amount": dmg_amount, "tags": {"daowen"},
                })
                if add:
                    dmg["nilin_bonus"] = add
                result["effects"].append({"type": "damage", "target": target.name, **dmg})
                if dmg.get("actual_damage", 0) > 0:
                    caster.damage_dealt_this_round += dmg["actual_damage"]

        # ---- 乱葬岗·附煞后置：锁煞（造成伤害后触发） ----
        if sha == "锁煞":
            dealt = 0
            for ef in result.get("effects", []):
                if ef.get("type") == "damage":
                    dealt += ef.get("actual_damage", 0) or 0
            if dealt > 0 and target.is_alive:
                if target.entity_type == "轮回者":
                    drain = min(target.current_mana, dealt)
                    target.current_mana -= drain
                    result["sha_qi_lock_mana"] = drain

        if "aoe_damage" in calc:
            a = 0 if mengbi_blocked else self._jieli_boost(caster, calc["aoe_damage"])
            if caster.has_status("坠落") and a > 0:
                a = math.ceil(a / 2)
            # 逆鳞加成仅作用于首个目标的首段伤害
            if nilin_bonus:
                a += nilin_bonus
                result["nilin_bonus"] = nilin_bonus
                nilin_bonus = 0
            if aoe_targets_override is not None:
                # 两阶段怪物决策必须只结算prepare时列出的目标，且已闪避目标已由调用方剔除。
                aoe_targets = [e for e in aoe_targets_override if e.is_alive]
            elif caster in self.state.get_all_player_side():
                aoe_targets = self.state.get_all_enemy_side()
            else:
                aoe_targets = self.state.get_all_player_side()
            for enemy in aoe_targets:
                # 对首个敌人附加剩余加成（若前未消耗）
                dmg_a = a
                if nilin_bonus and enemy is aoe_targets[0]:
                    dmg_a += nilin_bonus
                    nilin_bonus = 0
                dmg = self._apply_hostile_damage(enemy, dmg_a, source=caster, ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": enemy,
                    "mechanic": "damage", "subtype": "aoe_daowen", "amount": dmg_a, "tags": {"daowen", "aoe"},
                })
                result["effects"].append({"type": "aoe_damage", "target": enemy.name, **dmg})
                if dmg.get("actual_damage", 0) > 0:
                    caster.damage_dealt_this_round += dmg["actual_damage"]
        if "hp_percent_loss" in calc and name != "赌命":  # 赌命已改为[回始]随机结算（F2），此处仅保留其他百分比道纹
            if "hp_percent_loss" in wave_pieces:
                # 波及：总数值=各目标按当前生命×百分比之和，再平分。
                total = sum(math.ceil(wt.current_hp * calc["hp_percent_loss"] / 100)
                            for wt in wave_status_targets) + (nilin_bonus if nilin_bonus else 0)
                if nilin_bonus:
                    result["nilin_bonus"] = nilin_bonus
                    nilin_bonus = 0
                pieces = self._divide_flat(total, len(wave_status_targets))
                wave_pieces["hp_percent_loss"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    dmg = self._apply_hostile_damage(wt, piece, source=caster, ctx={
                        "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                        "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                        "mechanic": "damage", "subtype": "percent", "amount": piece,
                        "tags": {"daowen", "percent", "wave"},
                    })
                    result["effects"].append({"type": "pct_damage", "target": wt.name, **dmg})
                    if dmg.get("actual_damage", 0) > 0:
                        caster.damage_dealt_this_round += dmg["actual_damage"]
            else:
                d = math.ceil(target.current_hp * calc["hp_percent_loss"] / 100) + (nilin_bonus if nilin_bonus else 0)
                if nilin_bonus:
                    result["nilin_bonus"] = nilin_bonus
                    nilin_bonus = 0
                dmg = self._apply_hostile_damage(target, d, source=caster, ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": target,
                    "mechanic": "damage", "subtype": "percent", "amount": d, "tags": {"daowen", "percent"},
                })
                result["effects"].append({"type": "pct_damage", "target": target.name, **dmg})
                if dmg.get("actual_damage", 0) > 0:
                    caster.damage_dealt_this_round += dmg["actual_damage"]

        # ---- 回复类 ----
        if "target_heal" in calc:
            if "target_heal" in wave_pieces:
                pieces = self._divide_flat(calc["target_heal"], len(wave_status_targets))
                wave_pieces["target_heal"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    if self._heal_blocked(wt):
                        result["effects"].append(
                            {"type": "heal", "target": wt.name, "blocked_by": "坏死"})
                        continue
                    result["effects"].append({"type": "heal", "target": wt.name, **self.state.apply_heal(wt, piece, ctx={
                        "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                        "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                        "owner": caster, "mechanic": "heal", "subtype": "daowen", "amount": piece,
                        "tags": {"daowen", "wave"},
                    })})
            elif not huaisi_block:
                heal_amount = calc["target_heal"]
                result["effects"].append({"type": "heal", "target": target.name, **self.state.apply_heal(target, heal_amount, ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": target,
                    "owner": caster, "mechanic": "heal", "subtype": "daowen", "amount": heal_amount,
                    "tags": {"daowen"},
                })})
        # 【自愈】（2026-09-18 用户令重做）：按**已损生命**百分比回复，主动单体结算。
        # 旧版自愈走 heal_percent + ROUND_START 机制（持续∞ 自我奶），已随重做删除；
        # 下面的 heal_percent 分支因此不再需要 `name != "自愈"` 例外。
        if "heal_missing_percent" in calc:
            pct = calc["heal_missing_percent"]
            if "heal_missing_percent" in wave_pieces:
                # 波及：总量=各目标按自身已损生命×百分比之和，再平分（对齐 heal_percent）。
                total = sum(math.ceil(max(0, wt.blood_limit - wt.current_hp) * pct / 100)
                            for wt in wave_status_targets)
                pieces = self._divide_flat(total, len(wave_status_targets))
                wave_pieces["heal_missing_percent"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    if self._heal_blocked(wt):
                        result["effects"].append(
                            {"type": "heal_missing_pct", "target": wt.name, "blocked_by": "坏死"})
                        continue
                    result["effects"].append({
                        "type": "heal_missing_pct", "target": wt.name, "pct": pct,
                        **self.state.apply_heal(wt, piece, ctx={
                            "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                            "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                            "owner": caster, "mechanic": "heal", "subtype": "daowen_missing_pct",
                            "amount": piece, "tags": {"daowen", "missing_pct", "wave"},
                        })})
            elif self._heal_blocked(target):
                result["effects"].append(
                    {"type": "heal_missing_pct", "target": target.name, "blocked_by": "坏死"})
            else:
                missing = max(0, target.blood_limit - target.current_hp)
                h = math.ceil(missing * pct / 100)
                result["effects"].append({
                    "type": "heal_missing_pct", "target": target.name,
                    "missing_hp": missing, "pct": pct,
                    **self.state.apply_heal(target, h, ctx={
                        "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                        "source": name, "source_type": "daowen", "actor": caster, "target": target,
                        "owner": caster, "mechanic": "heal", "subtype": "daowen_missing_pct",
                        "amount": h, "tags": {"daowen", "missing_pct"},
                    })})
        if "heal_percent" in calc:
            if "heal_percent" in wave_pieces:
                # 波及：总数值=各目标按血限×百分比之和，再平分。
                total = sum(math.ceil(wt.blood_limit * calc["heal_percent"] / 100)
                            for wt in wave_status_targets)
                pieces = self._divide_flat(total, len(wave_status_targets))
                wave_pieces["heal_percent"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    if self._heal_blocked(wt):
                        result["effects"].append(
                            {"type": "heal_pct", "target": wt.name, "blocked_by": "坏死"})
                        continue
                    result["effects"].append({"type": "heal_pct", "target": wt.name, **self.state.apply_heal(wt, piece, ctx={
                        "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                        "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                        "owner": caster, "mechanic": "heal", "subtype": "daowen_pct", "amount": piece,
                        "tags": {"daowen", "wave"},
                    })})
            elif not self._heal_blocked(target):
                h = math.ceil(target.blood_limit * calc["heal_percent"] / 100)
                result["effects"].append({"type": "heal_pct", "target": target.name, **self.state.apply_heal(target, h, ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": target,
                    "owner": caster, "mechanic": "heal", "subtype": "daowen_pct", "amount": h,
                    "tags": {"daowen"},
                })})
        if "mutation_reduction" in calc:
            if "mutation_reduction" in wave_pieces:
                pieces = self._divide_flat(calc["mutation_reduction"], len(wave_status_targets))
                wave_pieces["mutation_reduction"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    pay = wt.add_mutation(-piece)
                    result["effects"].append({
                        "type": "mutation_reduction", "target": wt.name,
                        "reduced": piece, "mutation_total": pay["mutation_total"],
                    })
                    if wt.entity_type == "怪物":
                        redemption = self.check_redemption(wt)
                        if redemption:
                            result["effects"].append(redemption)
            else:
                reduced = int(calc["mutation_reduction"])
                pay = target.add_mutation(-reduced)
                result["effects"].append({
                    "type": "mutation_reduction", "target": target.name,
                    "reduced": reduced, "mutation_total": pay["mutation_total"],
                })
                if target.entity_type == "怪物":
                    redemption = self.check_redemption(target)
                    if redemption:
                        result["effects"].append(redemption)

        if ("target_heal" in calc or "heal_percent" in calc
                or "heal_missing_percent" in calc):
            for cancer_target in wave_status_targets:
                cancer = self.check_cancer(cancer_target)
                if cancer:
                    result["effects"].append(cancer)

        # ---- 格挡/血限 ----
        if "target_shield" in calc:
            if "target_shield" in wave_pieces:
                pieces = self._divide_flat(calc["target_shield"], len(wave_status_targets))
                wave_pieces["target_shield"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    wt.gain_shield(piece)
                    result["effects"].append({"type": "shield", "target": wt.name, "amount": piece})
            else:
                s = calc["target_shield"]; target.gain_shield(s)
                result["effects"].append({"type": "shield", "target": target.name, "amount": s})
        if "shield_drain" in calc:  # 清算：目标失格挡
            lost = min(target.shield, calc["shield_drain"]); target.shield -= lost
            result["effects"].append({"type": "shield_drain", "target": target.name, "lost": lost})
        if "blood_limit_reduction" in calc:
            _hp_before = target.current_hp
            # clamp_hp/lethal 关掉：本效果的生命封顶要在 hp_reduction 之后统一做一次。
            _blr = self._apply_blood_limit_change(
                target, -calc["blood_limit_reduction"], name, EffectPolarity.DEBUFF.value,
                ctx=daowen_ctx, source_type="daowen", subtype="blood_limit_reduction",
                actor=caster, owner=caster, clamp_hp=False, lethal=False)
            # 规则正文"[血限]及当前生命同时 -4X"：两者是各自独立的扣减。
            # 此前实现只做 current_hp=min(current_hp, blood_limit)（血限压顶），
            # 对残血目标等于毫无效果。合并成一次写入：既保持与两步扣减相同的终值，
            # 又让 Entity.__setattr__ 的「失去生命后」钩子恰好触发一次。
            if "hp_reduction" in calc:
                _target_hp = target.current_hp - calc["hp_reduction"]
            else:
                _target_hp = target.current_hp
            self._hp_loss_ctx = daowen_ctx
            target.current_hp = max(0, min(_target_hp, target.blood_limit))
            self._check_hp_zero_death(target, ctx=_blr["ctx"] or daowen_ctx)
            _hp_cut_tmp = _hp_before - target.current_hp
            # 血限压迫导致的当前生命减少，同样属于"使敌对角色生命减少"，
            # 必须计入本回合伤害统计，否则纯压血限流派会被【凡庸】判定为无所作为而自爆。
            _hp_cut = _hp_before - target.current_hp
            if _hp_cut > 0 and target is not caster:
                caster.damage_dealt_this_round += _hp_cut
            result["effects"].append({"type": "blood_limit_reduction", "target": target.name,
                                      "new_blood_limit": target.blood_limit,
                                      "hp_reduced": _hp_cut})
        if "blood_limit_increase" in calc:
            if "blood_limit_increase" in wave_pieces:
                pieces = self._divide_flat(calc["blood_limit_increase"], len(wave_status_targets))
                wave_pieces["blood_limit_increase"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    # 不朽之躯（初拥之夜遗物）：血限无法增加，对该实体的增殖等血限增长一律归零
                    if self.state.side_has(wt, "不朽之躯"):
                        result["effects"].append({"type": "blood_limit_increase", "target": wt.name,
                                                   "increase": 0, "blocked_by": "不朽之躯"})
                        continue
                    increase = self._apply_blood_limit_change(
                        wt, piece, name, EffectPolarity.BUFF.value,
                        ctx=daowen_ctx, source_type="daowen", subtype="blood_limit_increase",
                        actor=caster, owner=caster, clamp_hp=False, lethal=False)["applied"]
                    result["effects"].append({"type": "blood_limit_increase", "target": wt.name,
                                              "increase": increase})
            else:
                # 不朽之躯（初拥之夜遗物）：血限无法增加，对该实体的增殖等血限增长一律归零
                if self.state.side_has(target, "不朽之躯"):
                    result["effects"].append({"type": "blood_limit_increase", "target": target.name,
                                               "increase": 0, "blocked_by": "不朽之躯"})
                else:
                    increase = self._apply_blood_limit_change(
                        target, calc["blood_limit_increase"], name, EffectPolarity.BUFF.value,
                        ctx=daowen_ctx, source_type="daowen", subtype="blood_limit_increase",
                        actor=caster, owner=caster, clamp_hp=False, lethal=False)["applied"]
                    result["effects"].append({"type": "blood_limit_increase", "target": target.name,
                                              "increase": increase})
        # 【逼债】旧"碎片不足则失血限"路径已按 DM裁定D（2026-08-22）废止并移除：
        # 无力支付统一记为负债（见 round_start 的 F2 结算），唯一血限语义不再存在。

        # ---- 攻击面板修改 ----
        _panel_keys = ("attack_boost", "attack_reduction", "attack_fixed", "attack_count_fixed")
        # 波及扩散：attack_boost/reduction 数值平分；attack_fixed/attack_count_fixed
        # （固定面板为状态类）对波及目标原样生效。
        # 目标可选的道纹（如【变形】）未指定目标时兜底为施法者，避免 [None]
        panel_targets = wave_status_targets if (
            any(k in wave_pieces for k in ("attack_boost", "attack_reduction"))
            or (any(k in calc for k in ("attack_fixed", "attack_count_fixed"))
                and len(wave_status_targets) > 1)) else [target if target else caster]
        for panel_idx, panel_target in enumerate(panel_targets):
            panel_locked = panel_target.has_status("定型") and any(k in calc for k in _panel_keys)
            if panel_locked:
                result["effects"].append({"type": "dingxing_block", "target": panel_target.name})
            if (not panel_locked) and "attack_boost" in calc:
                piece = (wave_pieces.get("attack_boost") or [calc["attack_boost"]])[panel_idx]
                self._battle_delta(
                    panel_target, "attack_power", piece,
                    name, EffectPolarity.BUFF.value)
                result["effects"].append({"type": "attack_boost", "target": panel_target.name,
                                          "attack_power": panel_target.attack_power})
            # 【全力】2026-09-17 用户令重做：攻击力锁定 = [法限]，持续X。
            # 走状态层（models.py::effective_attack_power 读取），不再写遗留字段
            # attack_power——那样对不写穿的轮回者无效。
            if (not panel_locked) and calc.get("attack_power_to_mana_limit"):
                panel_target.add_status(StatusEffect(
                    name="全力", value=1,
                    remaining_rounds=calc.get("duration", x), source=caster.name))
                result["effects"].append({
                    "type": "attack_power_to_mana_limit", "target": panel_target.name,
                    "attack_power": panel_target.effective_attack_power(),
                    "duration": calc.get("duration", x)})
            if (not panel_locked) and "attack_reduction" in calc:
                amount = (wave_pieces.get("attack_reduction") or [calc["attack_reduction"]])[panel_idx]
                delta = max(0, panel_target.attack_power - amount) - panel_target.attack_power
                self._battle_delta(
                    panel_target, "attack_power", delta, name, EffectPolarity.DEBUFF.value)
                result["effects"].append({"type": "attack_reduction", "target": panel_target.name,
                                          "attack_power": panel_target.attack_power})
            if (not panel_locked) and "attack_fixed" in calc:
                self._battle_delta(
                    panel_target, "attack_power", calc["attack_fixed"] - panel_target.attack_power,
                    name, EffectPolarity.NEUTRAL.value)
                result["effects"].append({"type": "attack_fixed", "target": panel_target.name,
                                          "attack_power": panel_target.attack_power})
            # 【全速】2026-09-17 用户令（原名【迟滞】）：攻击次数锁定 = [速限]，持续X。
            # 走状态层（models.py::effective_attack_count 读取），不再写遗留字段
            # attack_count——那样对不写穿的轮回者无效。
            if (not panel_locked) and calc.get("attack_count_to_speed_limit"):
                panel_target.add_status(StatusEffect(
                    name="全速", value=1,
                    remaining_rounds=calc.get("duration", x), source=caster.name))
                result["effects"].append({
                    "type": "attack_count_to_speed_limit", "target": panel_target.name,
                    "attack_count": panel_target.effective_attack_count(),
                    "duration": calc.get("duration", x)})
            if (not panel_locked) and "attack_count_fixed" in calc:
                self._battle_delta(
                    panel_target, "attack_count", calc["attack_count_fixed"] - panel_target.attack_count,
                    name, EffectPolarity.NEUTRAL.value)
                result["effects"].append({"type": "attack_count_fixed", "target": panel_target.name,
                                          "attack_count": panel_target.attack_count})
        bianxing_blocked = False
        if name == "变形":
            # 2026-09-17 用户令：变形改为可选目标，不指定时作用于施法者。
            # 【定型】的判定对象随之改为**被变形者**（旧版固定查施法者，
            # 在"目标是别人"的场景下会误判）。
            _bx_target = target if target else caster
            if _bx_target.has_status("定型"):
                bianxing_blocked = True
                result["effects"].append({"type": "dingxing_block", "target": _bx_target.name})
            else:
                # 2026-09-17 用户令重做：改为**[目标]当前速度 ↔ 当前法力互换**，
                # 互换后各自被上限钳制（clamp_immortal_body：当前速度≤[速限]、
                # 当前法力≤[法限]），被钳掉的部分**凭空消失**，不返还。
                #   例：敌方 20/3/10（血限/速度/法力，速限3）→ 互换得 速度10、法力3
                #       → 速度被速限钳回 3 → 结果 20/3/3：目标凭空失去 7 点法力。
                # 旧版「自身攻击力与攻击次数互换」写遗留字段 attack_power/attack_count，
                # 属性模型统一后（攻击力=当前法力、攻击次数=当前速度）对轮回者无效，
                # 且只能对自己用。新版可指定目标，不指定时默认自身。
                swap_target = target if target else caster
                if not hasattr(swap_target, "_bianxing_original"):
                    swap_target._bianxing_original = (swap_target.current_speed,
                                                       swap_target.current_mana)
                swap_target.current_speed, swap_target.current_mana = (
                    swap_target.current_mana, swap_target.current_speed)
                # 互换后立即钳制：超出上限的部分直接蒸发（全局钳制规则）
                self.clamp_immortal_body(swap_target)
                result["effects"].append({"type": "swap", "target": swap_target.name,
                                          "current_speed": swap_target.current_speed,
                                          "current_mana": swap_target.current_mana})

        # ---- 速度修改 ----
        if "speed_boost" in calc:
            if "speed_boost" in wave_pieces:
                pieces = self._divide_flat(calc["speed_boost"], len(wave_status_targets))
                wave_pieces["speed_boost"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    gained = self._gain_speed(wt, piece, ctx={
                        "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                        "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                        "owner": caster, "mechanic": "speed_change", "subtype": "current_speed",
                        "amount": piece, "tags": {"daowen", "wave"},
                    })
                    result["effects"].append({"type": "speed_boost", "target": wt.name,
                                              "speed": wt.current_speed, "gained": gained})
            else:
                gained = self._gain_speed(target, calc["speed_boost"], ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": target,
                    "owner": caster, "mechanic": "speed_change", "subtype": "current_speed",
                    "amount": calc["speed_boost"], "tags": {"daowen"},
                })
                result["effects"].append({"type": "speed_boost", "target": target.name,
                                          "speed": target.current_speed, "gained": gained})
        if "speed_loss_pct" in calc:
            # 减速X：使[目标]失去其当前速度的10X%（一次性切除整场速度池的一部分——
            # 速度是一池制，[回始]不回填、[战终]复原，故没有"持续X"可言）。
            # 按百分比结算＝状态类效果（非数值平分），对波及目标各按其自身当前速度原样生效。
            # 取整按正文「整数规则：所有计算都向上取整」；只有当前速度为 0 才会削 0 点。
            pct = calc["speed_loss_pct"]
            for wt in wave_status_targets:
                lost = math.ceil(wt.current_speed * pct / 100)
                self._lose_current_speed(wt, lost, ctx={
                    "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                    "source": name, "source_type": "daowen", "actor": caster, "target": wt,
                    "owner": caster, "mechanic": "speed_change", "subtype": "current_speed",
                    "amount": -lost, "tags": {"daowen"},
                })
                result["effects"].append({"type": "speed_loss_pct", "target": wt.name,
                                          "pct": pct, "lost": lost, "speed": wt.current_speed})
        if "speed_penalty" in calc and (name != "赎金" or self._shards_of(target) <= 0):
            self._lose_current_speed(target, calc["speed_penalty"], ctx={
                "timing": "monster_action" if caster.entity_type == "怪物" else "player_action",
                "source": name, "source_type": "daowen", "actor": caster, "target": target,
                "owner": caster, "mechanic": "speed_change", "subtype": "current_speed",
                "amount": -calc["speed_penalty"], "tags": {"daowen"},
            })
            result["effects"].append({"type": "speed_penalty", "target": target.name,
                                      "lost": calc["speed_penalty"], "speed": target.current_speed})

        # ---- 碎片系（罪孽）----
        if "shard_steal" in calc and self._shards_of(target) > 0:
            # 赎金是“有碎片则夺取，若无碎片才失速”的二选一；不得把不足额偷取扩成负债。
            gained = min(self._shards_of(target), calc["shard_steal"])
            self._lose_shards_of(target, gained)
            if caster is self.state.player:
                self.state.shards += gained
            else:
                caster.shards += gained
            result["effects"].append({"type": "shard_steal", "target": target.name, "gained": gained})
        if "shard_gain" in calc:  # 点金：消耗10X法力直接换X真碎片（与伤害无关）
            if caster is self.state.player:
                self.state.shards += calc["shard_gain"]
            else:
                caster.shards += calc["shard_gain"]
            result["effects"].append({"type": "shard_gain", "gained": calc["shard_gain"]})
        if "fake_shards" in calc:  # 假钞：获得10X假碎片（假碎片与真碎片分离存储）
            if caster is self.state.player:
                self.state.fake_shards += calc["fake_shards"]
            else:
                caster.fake_shards = getattr(caster, "fake_shards", 0) + calc["fake_shards"]
            result["effects"].append({"type": "fake_shards", "gained": calc["fake_shards"]})
        if "cost_shards" in calc and name != "消灾":  # 消灾的付费在专属分支统一处理
            spent = self._lose_shards_of(self.state.player, calc["cost_shards"]) if caster is self.state.player else 0
            result["effects"].append({"type": "cost_shards", "spent": spent})

        # ---- 数值代价：统一走代价总线；【血契】可按显式引用共同承担 ----
        cost_spec = None
        if "cost_hp" in calc:
            cost_spec = ("流血", calc["cost_hp"], "bleed_cost")
        elif "cost_blood_limit" in calc:
            cost_spec = ("衰老", calc["cost_blood_limit"], "aging_cost")
        elif "cost_speed" in calc:
            cost_spec = ("疲惫", calc["cost_speed"], "fatigue_cost")
        elif "cost_mutation" in calc:
            # 2026-09-18 用户令：删除怪物侧「家族税」硬编码支付点。异变代价一律按道纹
            # 自身 calc 走统一代价总线，怪物与轮回者/同伴同口径（《怪物准则》第5条
            # 「发动代价类道纹时必须照常支付对应代价」）。此前怪物被本分支排除、改由
            # resolve_monster_phase 里按「原始怪物道纹每次发动付异变5X」硬扣，导致
            # 【自愈】这类自身代价为【冷却X】的道纹在怪物侧被额外扣一份异变。
            cost_spec = ("异变", calc["cost_mutation"], "mutation_cost")
        if cost_spec is not None:
            cost_type, amount, effect_type = cost_spec
            timing = "monster_action" if caster.entity_type == "怪物" else "player_action"
            payment = self.pay_numeric_cost(
                caster, cost_type, amount,
                cost_share_target_ref=cost_share_target_ref,
                dragon_heart_use=dragon_heart_use,
                cost_context={"timing": timing, "source": name, "source_type": "daowen", "tags": {"active_payment"}},
            )
            cost_effect = {"type": effect_type, **payment}
            if cost_type == "流血":
                # 保留既有公开字段，同时以owner/shared_with暴露血契拆分详情。
                cost_effect["actual_damage"] = payment["actual_paid"]
                cost_effect["hp_after"] = caster.current_hp
            result["effects"].append(cost_effect)
        elif cost_share_target_ref:
            raise ValueError("该道纹没有可由【血契】共同承担的数值代价")
        # 代价把自己打死（【异变】达50层→【崩解】命零）时道纹效果中断：这条口径原先
        # 写在怪物阶段的家族税支付点上（支付→崩解→early return），2026-09-18 删除家族税、
        # 异变代价改走统一总线后落到这里，对怪物与轮回者同样成立。
        # 只认「异变」这一种代价：流血/衰老类代价致死仍按既有口径结算完效果。
        if cost_spec is not None and cost_spec[0] == "异变" and not caster.is_alive:
            result["effects"].append({"type": "interrupted_by_cost", "cost_type": "异变",
                                      "collapsed": True, "caster": caster.name})
            return result
        if "mana_gain" in calc:
            caster.current_mana += calc["mana_gain"]
            self.clamp_immortal_body(caster)
            result["effects"].append({"type": "mana_gain", "source": caster.name, "mana_gained": calc["mana_gain"]})

        # ---- 乱葬岗（二阶）专属道纹效果 ----
        if name == "瓦解" and calc.get("blood_limit_pct"):
            pct = calc["blood_limit_pct"]
            if len(wave_status_targets) > 1:
                # 波及：总数值=各目标血限×百分比之和，再平分。
                total = sum(math.ceil(wt.blood_limit * pct / 100) for wt in wave_status_targets)
                pieces = self._divide_flat(total, len(wave_status_targets))
                for wt, piece in zip(wave_status_targets, pieces):
                    self._apply_blood_limit_change(
                        wt, -piece, "瓦解", EffectPolarity.DEBUFF.value,
                        ctx=daowen_ctx, source_type="daowen", subtype="disintegrate",
                        actor=caster, owner=caster)
                    result["effects"].append({"type": "wajie", "target": wt.name,
                                              "blood_limit_pct": pct, "blood_limit_cut": piece,
                                              "blood_limit_after": wt.blood_limit})
            else:
                cut = math.ceil(target.blood_limit * pct / 100)
                self._apply_blood_limit_change(
                    target, -cut, "瓦解", EffectPolarity.DEBUFF.value,
                    ctx=daowen_ctx, source_type="daowen", subtype="disintegrate",
                    actor=caster, owner=caster)
                result["effects"].append({"type": "wajie", "target": target.name,
                                          "blood_limit_pct": pct, "blood_limit_cut": cut,
                                          "blood_limit_after": target.blood_limit})
        if name == "镇尸" and calc.get("no_heal"):
            for st_target in wave_status_targets:
                st_target.add_status(StatusEffect(name="镇尸", value=1,
                                                  remaining_rounds=calc.get("duration", 1),
                                                  source=caster.name))
                result["effects"].append({"type": "zhenshi", "target": st_target.name,
                                          "duration": calc.get("duration", 1)})
        if name == "勾魂" and calc.get("mana_cost_multiplier"):
            # 勾魂X（DM裁定 2026-09-09 再改版）：持续X回合**法力消耗翻倍**
            # （实现在 models.py::spend_mana）。历史：旧版「[回始]失去2X法力，持续∞」
            # 已废止；2026-08-30 版「[回始]无法获得法力」随法力一池制一起失去作用对象。
            for st_target in wave_status_targets:
                st_target.add_status(StatusEffect(name="勾魂", value=1,
                                                  remaining_rounds=calc.get("duration", x),
                                                  source=caster.name))
                result["effects"].append({
                    "type": "gouhun", "target": st_target.name,
                    "mana_cost_multiplier": calc.get("mana_cost_multiplier"),
                    "duration": calc.get("duration", x)})
        if name == "冥气" and calc.get("speed_loss_speed_limit"):
            for st_target in wave_status_targets:
                st_target.add_status(StatusEffect(name="冥气", value=calc["speed_loss_speed_limit"],
                                                  remaining_rounds=calc.get("duration", 1),
                                                  source=caster.name))
                result["effects"].append({"type": "mingqi", "target": st_target.name,
                                          "speed_loss_speed_limit": calc["speed_loss_speed_limit"],
                                          "duration": calc.get("duration", 1)})
        if name == "缄默" and calc.get("silence_death_triggers"):
            # 「场上所有」＝敌我全体。wave_status_targets 对无目标道纹的兜底是施法者自己，
            # 只盖施法者会把一条全场封禁做成单体状态，故这里显式取全场（含施法者）。
            duration = calc.get("duration", 1)
            field: list[Entity] = []
            for candidate in (caster, *self.state.get_all_player_side(),
                              *self.state.get_all_enemy_side()):
                if not any(candidate is seen for seen in field):   # Entity 不可哈希，按同一性去重
                    field.append(candidate)
            for st_target in field:
                st_target.add_status(StatusEffect(name="缄默", value=1,
                                                  remaining_rounds=duration,
                                                  source=caster.name))
            result["effects"].append({"type": "qianmo", "scope": "field", "duration": duration,
                                      "targets": [e.name for e in field],
                                      "note": f"全场由[命零]触发的效果封禁{duration}回合"})
        if name == "尸爆" and calc.get("self_destruct"):
            # [命零]对全体敌方打出自身血限10X%伤害
            if self._death_triggers_silenced(caster):
                # 【缄默】：尸爆整条都是「由[命零]触发的效果」——封禁期内既不产生 AoE，
                # 也不发生自毁式命零（施法者留在场上）。法力已付、效果落空，不退还。
                result["silenced"] = True
                result["effects"].append({"type": "qianmo_blocked", "daowen": "尸爆",
                                          "target": caster.name,
                                          "note": "【缄默】生效：[命零]触发效果被封禁"})
            elif caster.is_alive and caster.current_hp > 0:
                pct = calc["aoe_pct"]
                dmg = math.ceil(caster.blood_limit * pct / 100)
                for enemy in [e for e in self.state.get_all_enemy_side() if e.is_alive]:
                    rd = self._apply_hostile_damage(enemy, dmg, source=caster, ctx={
                        "timing": "player_action" if caster is self.state.player else "monster_action",
                        "source": "尸爆", "source_type": "daowen", "actor": caster, "target": enemy,
                        "mechanic": "damage", "subtype": "self_destruct_aoe", "amount": dmg,
                        "tags": {"daowen", "aoe", "self_destruct"},
                    })
                    result["effects"].append({"type": "aoe_damage", "target": enemy.name, **rd})
                # 尸爆是「自毁式[命零]」，不是生命归零致死：正文未规定清零当前生命，
                # 因此这里刻意不走 _check_hp_zero_death（它会把 current_hp 抹成 0），
                # 而是直接置命零标记 + 统一死亡通知（带完整 ctx）。
                caster.is_alive = False
                self._on_entity_death(caster, ctx={
                    "timing": "player_action" if caster is self.state.player else "monster_action",
                    "source": "尸爆", "source_type": "daowen", "actor": caster, "target": caster,
                    "mechanic": "death", "subtype": "self_destruct", "tags": {"daowen", "self_destruct"},
                })
                result["self_destructed"] = True
        if name == "分裂" and calc.get("split_clones"):
            # 2026-09-17 用户令重做：分裂X/Y 改为**即时**创造 X 个 10Y 血限的
            # 自身复制体（代价衰老＝X×10Y＝造出的总血限）。旧版把创造挂在
            # [命零]上（_pending_split_clones），且血限按本体 20% 浮动——本体
            # 血限越高白赚越多，代价【冷却】又与产出无关，可无限白嫖。
            # 旧版还有 entity_type != "怪物" 的过滤，导致怪物永远不分裂，
            # 与「乱葬岗·分裂」的设计不符；现对任意实体类型一视同仁。
            clones = self._spawn_fenlie_clones(caster, calc["split_clones"],
                                               calc.get("clone_hp", 10))
            result["effects"].append({"type": "fenlie", "clones": len(clones),
                                      "clone_hp": calc.get("clone_hp", 10),
                                      "names": [c.name for c in clones]})
        if name == "招魂" and calc.get("revive_temp_friend"):
            # 唤回1具已击灭的怪物尸体作临时朋友（生命20X）
            dead = [e for e in self.state.dead_monsters if e.entity_type == "怪物"]
            if dead:
                corpse = dead[-1]
                from ..models import Entity
                revived = Entity(name=f"{corpse.name}（魂）", entity_type="临时朋友",
                                 blood_limit=calc["temp_hp"], current_hp=calc["temp_hp"],
                                 attack_count=corpse.attack_count, attack_power=corpse.attack_power)
                for dw_name, dw_inst in corpse.dao_wen.items():
                    revived.dao_wen[dw_name] = dw_inst
                self.state.temp_friends.append(revived)
                result["effects"].append({"type": "zhaohun", "name": revived.name,
                                          "hp": calc["temp_hp"], "corpse": corpse.name})
            else:
                result["effects"].append({"type": "zhaohun", "note": "没有可唤回的怪物尸体"})

        # ---- 特殊 ----
        if "self_attack_count" in calc:  # 自残：目标自打X次
            if "self_attack_count" in wave_pieces:
                pieces = self._divide_flat(calc["self_attack_count"], len(wave_status_targets))
                wave_pieces["self_attack_count"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    for _ in range(piece):
                        result["effects"].append({"type": "self_attack", "target": wt.name,
                            **self._apply_hostile_damage(wt, wt.attack_power, source=wt, ctx={
                                "timing": "player_action" if caster is self.state.player else "monster_action",
                                "source": name, "source_type": "daowen", "actor": wt, "target": wt,
                                "mechanic": "damage", "subtype": "self_attack", "amount": wt.attack_power,
                                "tags": {"daowen", "self_damage", "wave"},
                            })})
            else:
                for _ in range(calc["self_attack_count"]):
                    result["effects"].append({"type": "self_attack", "target": target.name,
                        **self._apply_hostile_damage(target, target.attack_power, source=target, ctx={
                            "timing": "player_action" if caster is self.state.player else "monster_action",
                            "source": name, "source_type": "daowen", "actor": target, "target": target,
                            "mechanic": "damage", "subtype": "self_attack", "amount": target.attack_power,
                            "tags": {"daowen", "self_damage"},
                        })})
        if calc.get("delay_monster_reentry"):
            # 【封印X】：目标由 use_daowen 的显式 target_ref/target 绑定；只允许一只
            # 当前在场怪物进入暂离队列。暂离不写离场/死亡上下文，也不产生碎片分类。
            if target.entity_type != "怪物":
                raise ValueError("【封印】的目标必须是当前在场的怪物")
            if not any(e is target for e in self.state.enemies) or not target.is_alive:
                raise ValueError("【封印】的目标必须是当前存活且在场的怪物")
            reentry = self._delay_monster_reentry(target, calc.get("delay_rounds", x))
            result["effects"].append({
                "type": "seal", "target": target.name,
                "delay_rounds": reentry["delay_rounds"],
                "return_round": reentry["return_round"],
                "note": f"{target.name}延后{reentry['delay_rounds']}回合，于第{reentry['return_round']}回合始再入场",
            })

        # ---- 持续/触发状态（status_added）----
        if "guaranteed_hits" in calc:
            self.grant_bizhong(caster, int(calc["guaranteed_hits"]))
            result["effects"].append({"type": "bizhong", "target": caster.name,
                                      "count": calc["guaranteed_hits"],
                                      "remaining": self.bizhong_remaining(caster)})

        # 蒙蔽X：使[目标]下X次造成的伤害无效。次数型，不走 duration 挂状态。
        # 此前只算出 invalid_damage_hits，apply 不消费，轮回者 use_daowen 等于白扣 5X 法力。
        # 怪物侧走 _apply_control_to_player，探照灯走 add_status，所以旧测试都绿。
        if "invalid_damage_hits" in calc:
            if "invalid_damage_hits" in wave_pieces:
                pieces = self._divide_flat(calc["invalid_damage_hits"], len(wave_status_targets))
                wave_pieces["invalid_damage_hits"] = pieces
                for wt, piece in zip(wave_status_targets, pieces):
                    if piece > 0:
                        wt.add_status(StatusEffect(
                            name="蒙蔽", remaining_rounds=-1, value=piece, source=caster.name))
                        result["effects"].append({
                            "type": "mengbi",
                            "target": wt.name,
                            "count": piece,
                            "remaining": wt.get_status_value("蒙蔽"),
                        })
            else:
                hits = int(calc["invalid_damage_hits"])
                if hits > 0:
                    target.add_status(StatusEffect(
                        name="蒙蔽", remaining_rounds=-1, value=hits, source=caster.name))
                    result["effects"].append({
                        "type": "mengbi",
                        "target": target.name,
                        "count": hits,
                        "remaining": target.get_status_value("蒙蔽"),
                    })

        if name == "坠落":
            duration = x if calc.get("duration") in (None, 0) else calc["duration"]
            grounded = []
            for e in self.state.get_all_player_side() + self.state.get_all_enemy_side():
                if not e.is_alive:
                    continue
                if self._is_flying(e) or e.has_status("坠落"):
                    e.is_flying = False
                    e.status_effects = [s for s in e.status_effects if s.name not in ("飞行", "滑翔")]
                    e.add_status(StatusEffect(name="坠落", remaining_rounds=duration, value=x, source=caster.name))
                    grounded.append(e.name)
            result["effects"].append({"type": "zhuiluo", "targets": grounded, "duration": duration})
        elif ("duration" in calc and calc.get("duration") is not None
              and not (name == "变形" and bianxing_blocked)
              # 波及标记由 use_daowen/怪物结算逐目标处理，不走通用状态块
              # 乱葬岗道纹已在上方乱葬岗段自行 add_status，跳过通用状态处理避免重复叠加
              and name not in ("勾魂", "冥气", "缄默", "镇尸", "瓦解", "波及")):
            duration = calc["duration"] if calc["duration"] != 0 else -1
            effect_target = target if target else caster
            # 自身作用型道纹(变形/超频/自食等)作用于施法者
            # 2026-09-10：道纹【洗劫】已改名【点金】并改为即时结算（不再挂状态），
            # 故从自身作用名单移除；状态【洗劫】本身保留，仍由【帮派令】发放。
            # 2026-09-17 用户令：【超频】改为自由选择目标（选到谁给谁加速），不再属于
            # "自身作用型"。它原本留在本名单里也无效——其 calc 无 duration 键，
            # 进不了本状态块，实际效果一直是下方数值段给 target 加速。
            # 2026-09-17 用户令：【变形】改为可自由选择目标（不指定时默认自身），
            # 故移出"自身作用型"名单，状态随之挂到目标身上（到期还原也落在目标）。
            # 2026-09-18 用户令：【自愈】重做为「代价冷却X，恢复[目标]25X%已损生命」——
            # 主动、需显式选定[目标]、calc 无 duration 键（不进本状态块），故移出自身作用名单。
            self_targeted = name in ("自食", "飞行", "滑翔", "狂暴", "必中", "固执", "贯穿")
            if name == "疯狂":
                # 2026-08-17 用户裁定：疯狂X改为【所有角色出手+X】（全局，变相平衡）。
                # 状态盖到双方全部存活角色；出手口径各自读取自身疯狂状态：
                # 轮回者/朋友/员工走 Entity.action_count，怪物攻击轮数走 _monster_attack_actions。
                for et_all in self.state.get_all_player_side() + self.state.get_all_enemy_side():
                    if not et_all.is_alive:
                        continue
                    et_all.add_status(StatusEffect(name="疯狂", remaining_rounds=duration,
                                                   value=x, source=caster.name))
                    result["effects"].append({"type": "status_added", "target": et_all.name,
                                              "status": name, "duration": duration, "value": x})
            elif self_targeted:
                # 这类道纹的状态永远挂施法者（不受提交目标影响）；用户裁定 2026-09-19（A）后，
                # 波及目标也一并生效——与下面 else 分支同一口径：自己那一份不丢，对方额外得一份。
                # 此前它们完全绕过扩散名单，导致同为自我增益的道纹在波及下行为相反。
                for et in [caster] + [wt for wt in wave_targets
                                      if wt is not caster and wt.is_alive]:
                    if name in ("飞行", "滑翔") and self._field_has_zhuiluo():
                        et.is_flying = False
                        et.add_status(StatusEffect(name="坠落", remaining_rounds=1, value=x, source=caster.name))
                        result["effects"].append({"type": "zhuiluo_block_flight", "target": et.name})
                    else:
                        et.add_status(StatusEffect(name=name, remaining_rounds=duration, value=x, source=caster.name))
                        result["effects"].append({"type": "status_added", "target": et.name,
                                                  "status": name, "duration": duration, "value": x})
            else:
                # 波及：状态类效果对每个拥有波及效果的目标（含本次[目标]）原样生效。
                for et in wave_status_targets:
                    if not et.is_alive:
                        continue
                    et.add_status(StatusEffect(name=name, remaining_rounds=duration, value=x, source=caster.name))
                    result["effects"].append({"type": "status_added", "target": et.name,
                                              "status": name, "duration": duration, "value": x})

        # ---- 龙心谷专属 4 件（F2）：逆鳞/嫁祸/背负/伤痕 的 combat 侧实装 ----
        # 逆鳞X：目标每失去1HP积1层，下次伤害+全部层后清空，持续X（已通过 duration 加状态，此处初始化计数）
        if name == "逆鳞":
            for st_target in wave_status_targets:
                if not hasattr(st_target, "_nilin"):
                    st_target._nilin = 0
                result["effects"].append({"type": "nilin_setup", "target": st_target.name, "x": x})
        # 嫁祸X：自身下X次受伤由目标承担（无持续，仅计数）
        # 存 runtime_id 而非实体引用：自施/互指会形成实体引用环，
        # 事务回滚 _restore_state_in_place 的递归会无限深入（2026-08-22 学习遥测 RecursionError）。
        elif name == "嫁祸":
            caster._jiahuo_left = x
            caster._jiahuo_target = target.runtime_id
            caster.add_status(StatusEffect(name="嫁祸", value=x, remaining_rounds=x, source=caster.name))
            result["effects"].append({"type": "jiahuo", "caster": caster.name, "target": target.name, "count": x})
        # 背负X：目标下X次受伤由自身承担（同样只存 runtime_id）
        elif name == "背负":
            caster._beifu_left = x
            caster._beifu_target = target.runtime_id
            # 在目标侧加标记便于查询
            for st_target in wave_status_targets:
                st_target.add_status(StatusEffect(name="被背负", value=x, remaining_rounds=-1, source=caster.name))
                result["effects"].append({"type": "beifu", "caster": caster.name, "target": st_target.name, "count": x})
        # 伤痕X：目标每次掉血后血限-X，永久（已通过 duration 加伤痕状态，此处仅补日志）
        elif name == "伤痕":
            for st_target in wave_status_targets:
                result["effects"].append({"type": "shanghen", "target": st_target.name, "x": x})

        # ---- F2 全量：罪孽都市（逼债/清算/赌命/消灾/抵扣）的注册与即时结算 ----
        # 逼债X：[回始]使[目标]失去X碎片，否则失去2X血限（二选一）。此处仅挂账，[回始]在 round_start 结算。
        if name == "逼债":
            for st_target in wave_status_targets:
                st_target._bizhai.append({"x": x, "caster": caster})
                st_target.add_status(StatusEffect(name="逼债", value=x, remaining_rounds=-1, source=caster.name))
                result["effects"].append({"type": "bizhai_register", "target": st_target.name, "x": x})
        # 清算X：[回始]使[目标]失去你[碎片]点格挡，持续X。此处仅挂账。
        elif name == "清算":
            for st_target in wave_status_targets:
                st_target._qingsuan.append({"x": x, "caster": caster})
                result["effects"].append({"type": "qingsuan_register", "target": st_target.name, "x": x})
        # 赌命X：玩家侧在_action_use_daowen预检付费；怪物侧由两阶段决策结算器付费。
        # 状态经 duration 挂在施法者上，[回始]在 round_start 按存活角色随机结算。
        elif name == "赌命":
            result["effects"].append({"type": "duming_register", "caster": caster.name, "x": x})
        # 消灾X：玩家侧在_action_use_daowen预检付费；怪物侧由两阶段决策结算器付费；此处登记重投次数。
        elif name == "消灾":
            self.dice.set_rerolls(self.dice.rerolls_pending + x)
            result["effects"].append({"type": "xiaozai_rerolls", "added": x, "total": self.dice.rerolls_pending})
        # 抵扣X：封印[目标]拥有的一件遗物，持续X（目标无遗物则无效果）
        elif name == "抵扣":
            for st_target in wave_status_targets:
                sealed = self._seal_one_relic(st_target, x)
                result["effects"].append({"type": "dikou", "target": st_target.name,
                                          "sealed": sealed or None, "rounds": x})

        # 波及X：标记建立/解除由 use_daowen 与怪物两阶段结算按显式提交逐目标处理，
        # 此处仅登记结算信息（通用状态块已排除波及，避免重复挂状态）。
        if name == "波及":
            result["effects"].append({"type": "boba_register", "target": target.name, "x": x})

        return result



    # 法术流程注册表；计算层只描述步骤，不再替持有者选择X、目标或闪避。
    SPELL_FLOWS = {
        "先发制人": {"trigger": ActionPhase.BEFORE_DAMAGE_TAKEN.value, "steps": [("杀伐", "attacker")]},
        "后发制人": {"trigger": ActionPhase.BEFORE_DAMAGE_TAKEN.value, "steps": [("庇护", "self")]},
        "生生不息": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("再生", "self")]},
        "以牙还牙": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("再生", "self"), ("杀伐", "attacker")]},
        "借力打力": {"trigger": ActionPhase.BEFORE_DAMAGE_TAKEN.value, "steps": [("庇护", "self"), ("杀伐", "attacker")]},
        "不死不休": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("血债", "attacker")], "loop": True},
        "千刀万剐": {"trigger": ActionPhase.AFTER_LIFE_LOST.value, "steps": [("再生", "self"), ("血债", "attacker")], "loop": True},
        "咎由自取": {"trigger": "目标发动道纹前", "steps": [("坠落", "target"), ("杀伐", "target"), ("血债", "target")]},
        "镇魔印": {"trigger": TriggerTiming.SELF_TURN_END.value,
                   "steps": [("封印", "any")],
                   "effect_flow": "自身回合结束后→发动封印X于任意目标",
                   "automatic": True},
        # 血炼周天（2026-09-16 补流程，清单 D1）：
        # 失去生命后→发动再生→发动透支→失去生命后（循环）。
        # 【透支】的流血会再次触发「失去生命后」，由此自驱动循环。
        "血炼周天": {"trigger": ActionPhase.AFTER_LIFE_LOST.value,
                     "steps": [("再生", "self"), ("透支", "self")],
                     "effect_flow": "失去生命后→发动再生→发动透支→失去生命后（循环）",
                     "loop": True},
    }

    # 内置法术的所需道纹（唯一事实源；api.GameEngine.SPELL_REGISTRY 是本表的别名）。
    # 键与 SPELL_FLOWS 一一对应——凡有流程的内置法术都必须在此登记所需道纹，
    # 否则「持道纹即可施法」判定无法知道该法术需要什么。
    BUILTIN_SPELL_DAOWEN = {
        "先发制人": ["杀伐"],
        "后发制人": ["庇护"],
        "生生不息": ["再生"],
        "以牙还牙": ["杀伐", "再生"],
        "借力打力": ["杀伐", "庇护"],
        "不死不休": ["血债"],
        "千刀万剐": ["血债", "再生"],
        "咎由自取": ["坠落", "杀伐", "血债"],
        "镇魔印": ["封印"],
        "血炼周天": ["再生", "透支"],
    }
