"""CombatEngine 分片：怪物回合两阶段：激活记账/准备枚举/静态校验/选择解析/快照与回滚/执行

方法体自 engine/combat.py 原样搬来（逐字节），行为与对外契约不变；
CombatEngine 仍是唯一入口（门面类继承本 Mixin）。
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
from ..models import MONSTER_MANA_RELIC


class MonsterPhaseMixin:
    # ========== 怪物回合（两阶段显式决策） ==========
    # 怪物已激活的道纹 / 已进化的怪物（均按战斗重置）
    _monster_activated: dict = {}
    _monster_evolved: set = set()  # 进化（原初X）：本场已进化的怪物 id 集合
    _monster_daowen_round_used: dict = {}  # 本回合已发动的道纹（DM裁定2026-08-18：跨回合可重复发动）

    def reset_monster_activation(self):
        """战始重置怪物激活状态与战斗遗物状态"""
        # 招架姿态与承露盏记账都是**每场**口径，战始一并归零，
        # 避免上一场的姿态/余数漏到新战斗（换场时不经 [回始]）。
        for e in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            e.parrying_this_round = False
            e.parry_locked_this_round = False
            e.hp_lost_this_battle = 0
            e.chenglu_paid = 0
        self._monster_activated = {}
        self._monster_evolved = set()  # 进化（原初X）：每场战斗限一次
        self._monster_daowen_round_used = {}
        self._sanxiang_consumed = ""
        self._resonance_rewrites = {}

    def _monster_round_used(self, monster: Entity) -> set:
        """该怪物本回合已发动的道纹集合（换回合自动清空）。

        DM裁定（2026-08-18，规则正文·怪物准则9）：怪物可在不同回合重复发动同一
        道纹（冷却类由 can_use 管辖），每回合每道纹至多一次。
        2026-09-16 用户令：原「重复发动则 X 累加 +2×副本阶级」的递增机制已废止，
        重复发动按同一 X 计费。
        _monster_activated 保留为持续激活口径（狂暴出手加成等），不再作发动门禁。
        """
        rec = self._monster_daowen_round_used.get(id(monster))
        if rec is None or rec[0] != self.state.current_round:
            rec = (self.state.current_round, set())
            self._monster_daowen_round_used[id(monster)] = rec
        return rec[1]
    def consume_resonance_rewrite(self, entity: Entity, source: str) -> Optional[str]:
        bucket = self._resonance_rewrites.get(id(entity)) or {}
        dest = bucket.pop(source, None)
        if dest and not bucket:
            self._resonance_rewrites.pop(id(entity), None)
        return dest

    def _monster_attack_actions(self, m: Entity, activated: set) -> int:
        """怪物攻击出手数 = 1 + 疯狂X(自身状态) + 狂暴1(若激活)。

        疯狂2026-08-17全局裁定：发动方把疯狂状态盖到所有角色，怪物从自身状态读+X；
        激活集合口径仅保留给狂暴。发动当回合的状态在resolve阶段才落下，
        prepare在本回合道纹结算前已快照出手数，因此疯狂自下回合生效的时序不变。
        高爆手雷修改的是每轮攻击中的"攻击次数"，不再同时削减攻击出手数。
        """
        n = 1
        n += m.get_status_value("疯狂")
        if "狂暴" in activated or m.has_status("狂暴"):
            n += 1
        return max(0, n)

    def _combat_entity_refs(self) -> dict[str, Entity]:
        """为两阶段决策提供本场稳定的显式目标引用，避免同名实体歧义。"""
        refs: dict[str, Entity] = {}
        if self.state.player and self.state.player.is_alive:
            refs["player:0"] = self.state.player
        for prefix, entities in (
            ("friend", self.state.friends),
            ("employee", self.state.employees),
            ("temp_friend", self.state.temp_friends),
            ("enemy", self.state.enemies),
        ):
            for i, entity in enumerate(entities):
                if not entity.is_alive or entity.has_retreated:
                    continue
                if prefix == "employee" and not entity.is_deployed:
                    continue
                self._bind_hp_hook(entity)  # 确认战斗实体已绑定「失去生命后」兜底钩子（幂等）
                refs[f"{prefix}:{i}"] = entity
        return refs

    def _daowen_requires_target(self, name: str) -> bool:
        import inspect
        DaoWenEngine.register_all()
        func = DaoWenEngine._registry.get(name)
        return bool(func and "target" in inspect.signature(func).parameters)

    def prepare_monster_phase(self) -> dict:
        """只枚举合法选择，不决定道纹、目标或闪避，也不改变战斗数值。"""
        refs = self._combat_entity_refs()
        player_refs = [
            {"ref": ref, "name": e.name}
            for ref, e in refs.items()
            if self.state.on_player_side(e)
        ]
        all_targets = [{"ref": ref, "name": e.name} for ref, e in refs.items()]
        # 白板回合已于 2026-09-15 用户令删除：怪物（含[战始]首发、波次增援、[封印]回场）
        # 进场当回合即可发动道纹，不再有"登场回合只能普攻"的限制。
        actors = []
        skipped = []
        for index, monster in enumerate(self.state.enemies):
            if not monster.is_alive or monster.removed_without_kill:
                continue
            actor_ref = f"enemy:{index}"
            if not self.can_act(monster):
                skipped.append({"actor_ref": actor_ref, "monster": monster.name, "reason": "无法行动"})
                continue
            activated = self._monster_activated.get(id(monster), set())
            round_used = self._monster_round_used(monster)
            daowen_options = []
            # 2026-09-15 用户令：删除白板限制——增援怪/回场怪进场当回合即可发动道纹
            # （R1 首发怪同理）。spawned_round 仍作为出生回合的记录字段保留，但不再
            # 参与任何"当回合不能发动道纹"的判定。
            if not monster.has_status("干扰"):
                for name, inst in monster.dao_wen.items():
                    if (name in round_used or not inst.can_use()
                            or name not in DaoWenEngine.list_all()):
                        continue
                    rewritten_as = (self._resonance_rewrites.get(id(monster)) or {}).get(name)
                    effective_name = rewritten_as or name
                    requires_target = self._daowen_requires_target(effective_name)
                    legal_targets = ([target for target in all_targets
                                      if self.is_targetable(monster, refs[target["ref"]])]
                                     if requires_target else [])
                    if "龙威" in self.state.dragon_traits and self.state.player and self.state.player.is_alive:
                        legal_targets = [target for target in legal_targets
                                         if (not self.state.on_player_side(refs[target["ref"]])
                                             or target["ref"] == "player:0")]
                    if requires_target and not legal_targets:
                        continue
                    # 波及X：必须显式提交X个互不重复的合法目标。DM裁定2026-08-23：
                    # 面板X超过当前合法目标数时按目标数**自适应降X**（有效X=
                    # min(面板X, 合法目标数)），与玩家侧 _max_legal_daowen_x 的
                    # 目标数封顶口径一致——永不因目标不足而不可结算/死锁；仅当
                    # 合法目标数为0时本道纹才真正无法发动，prepare才过滤。
                    # （取代2026-08-22 BUG-01的"不足X即过滤"方案：过滤让面板波及
                    # 怪在solo场上1/111场才开得出火，属于非符合预期效果。）
                    dodge_target_options: list[dict] = []
                    if effective_name == "波及":
                        dodge_target_options = [target for target in all_targets
                                                if target["ref"] != actor_ref
                                                and self.is_targetable(monster, refs[target["ref"]])]
                        if not dodge_target_options:
                            continue
                    # 过滤当前付不起数值代价的候选（改写后代价可能超出怪物资源）
                    preview_target = (refs[legal_targets[0]["ref"]] if legal_targets else monster)
                    # 2026-09-16 用户令：面板不写死 X 时，X 由发动方自选，
                    # 「上限只受法限或者代价限制」。这里探出可负担上限，
                    # 连 X=1 都付不起 → 本道纹此刻不可发动，prepare 过滤。
                    if getattr(inst, "x_free", False):
                        max_x = self._monster_max_daowen_x(
                            monster, effective_name, preview_target,
                            hard_cap=len(dodge_target_options) if effective_name == "波及" else None)
                        if max_x < 1:
                            continue
                        effective_x = max_x
                    else:
                        if name == "赌命" and getattr(monster, "fake_shards", 0) < inst.x_value:
                            continue
                        if (name == "消灾" and monster.fake_shards < 50 * inst.x_value
                                and monster.shards < 5 * inst.x_value):
                            continue
                        effective_x = inst.x_value
                    preview_calc = DaoWenEngine.resolve(
                        effective_name, effective_x, target=preview_target, caster=monster)
                    if not self._monster_can_pay_calc_cost(monster, preview_calc):
                        continue
                    wave_effective_x = 0
                    if effective_name == "波及":
                        wave_effective_x = min(effective_x, len(dodge_target_options))
                    daowen_options.append({
                        "name": name,
                        "resolves_as": effective_name,
                        # 2026-09-16 用户令：x_free 时面板没有 X，改由发动方在
                        # [1, max_x] 内自选；x 字段保留为上限值以便旧调用方读取。
                        "x": effective_x,
                        "x_free": bool(getattr(inst, "x_free", False)),
                        "max_x": max_x if getattr(inst, "x_free", False) else 0,
                        "wave_effective_x": wave_effective_x,
                        "requires_target": requires_target,
                        "target_options": legal_targets,
                        "dodge_submission": ("per_target" if effective_name == "波及"
                                             else ("single_if_hostile" if requires_target else "none")),
                        "dodge_target_options": dodge_target_options,
                        "trigger_spell_options": self.prepare_daowen_trigger_spells(monster),
                    })
            attack_targets = [
                target for target in player_refs
                if self.is_targetable(monster, refs[target["ref"]])
            ]
            # 【龙威】是规则约束而非策略默认：敌方只能把持有者列为合法攻击目标。
            if "龙威" in self.state.dragon_traits and self.state.player and self.state.player.is_alive:
                attack_targets = [target for target in attack_targets if target["ref"] == "player:0"]
            for target_option in attack_targets:
                entity = refs[target_option["ref"]]
                blood_pact_options = self.blood_shadow_cost_share_options(entity)
                target_option["can_blood_shadow"] = (
                    self.state.side_has(entity, "血影")
                    and (entity.current_hp > 10 or bool(blood_pact_options)))
                target_option["blood_shadow_cost_share_target_options"] = blood_pact_options
                target_option["spell_options"] = self.prepare_spell_reactions(entity, monster)
                target_option["dodge_relic_target_options"] = [
                    candidate for candidate in all_targets
                    if self.state.on_player_side(refs[candidate["ref"]]) != self.state.on_player_side(entity)
                ] if self.state.side_has(entity, "回锋刀") else []
            # 没有任何合法攻击目标（如solo对手飞行而怪物不飞）→ 本回合不出手。
            # 否则怪物阶段无法被满足：每击都必须引用合法目标，提交永远失败
            # （与【波及】目标数限制同族：prepare不得给出无法满足的义务）。
            base_actions = (0 if not attack_targets
                            else self._monster_attack_actions(monster, activated))
            actors.append({
                "actor_ref": actor_ref,
                "monster": monster.name,
                "daowen_required": bool(daowen_options),
                "daowen_options": daowen_options,
                "attack_target_options": attack_targets,
                "base_attack_actions": base_actions,
                "base_hits_per_attack": max(0, monster.attack_count - monster.get_status_value("手雷减攻")),
                "dodge_must_be_explicit": True,
                # 致死进度（用户令 2026-09-15）：怪物同样会【崩解】，攻守双方都要能直接读到
                # 「崩解（30/50）」这种进度，才可能判断"再逼它发动一次道纹它就自爆"。
                "lethal_counters": {k: list(v) for k, v in monster.lethal_counters().items()},
                "lethal_progress": monster.lethal_progress(),
            })
        return {"round": self.state.current_round, "actors": actors, "skipped": skipped}

    def _monster_can_pay_calc_cost(self, caster: Entity, calc: dict) -> bool:
        """怪物发动道纹前校验数值代价是否付得起（不实际支付）。

        残韵改写/状态变化可能让怪物付不起代价（如速度归零后发动
        【洞察】(疲惫3)）——付不起就不该被 prepare 列为合法项，
        也不该在 resolve 时硬报错。
        """
        if caster is None or not caster.entity_type == "怪物":
            return True
        for key, capacity in (
                ("cost_hp", getattr(caster, "current_hp", 0)),
                ("cost_blood_limit", getattr(caster, "blood_limit", 0)),
                ("cost_speed", getattr(caster, "current_speed", 0)),
        ):
            amount = calc.get(key, 0)
            if amount and capacity < amount:
                return False
        # 2026-09-16 用户令：怪物与轮回者同口径持有[法限]，发动【消耗】类道纹必须付法力。
        # 旧条文「怪物不持有法力、发动道纹不支付法力」已废止。
        if calc.get("cost_type") == "消耗" and calc.get("cost", 0) > 0:
            if caster.current_mana < calc["cost"]:
                return False
        return True

    # 2026-09-16 用户令：面板不再写死 X，改由怪物 AI 在发动时自选，
    # 「上限只受法限或者代价限制」。试探上限时逐个 X 递增，取第一个付不起的 X 之前的值。
    # 代价随 X 单调递增（杀伐 X² 之类的超线性亦然），故首次失败即可停止，不必二分。
    _DAOWEN_X_PROBE_CAP = 30

    def _monster_max_daowen_x(self, monster: Entity, effective_name: str, target: Entity,
                              hard_cap: int | None = None) -> int:
        """求该道纹此刻可负担的最大 X。返回 0 表示连 X=1 都付不起（prepare 应过滤掉）。"""
        cap = hard_cap if hard_cap is not None else self._DAOWEN_X_PROBE_CAP
        best = 0
        for x in range(1, max(0, cap) + 1):
            try:
                calc = DaoWenEngine.resolve(effective_name, x, target=target, caster=monster)
            except ValueError:
                # X_LIMITS 之类的硬性上限（如【失忆】X≤当前道纹数量）会在此抛错
                break
            if not self._monster_can_pay_calc_cost(monster, calc):
                break
            # 【异变】是**累加计数**而非可花费的预算：付异变等于给自己叠层，
            # 达到 MUTATION_COLLAPSE_THRESHOLD 就【崩解】命零，所以它没有天然的
            # "付不起"上限，探测会一路撞上试探封顶值。这里按生存线封顶——
            # 允许叠加到崩解线之前，但**不把"当场自爆"的 X 当成合法选项**。
            # （是否值得逼近崩解线由 AI 预演评分自行权衡，引擎只保证不主动提供自杀档。）
            if calc.get("cost_type") == "异变":
                headroom = (Entity.MUTATION_COLLAPSE_THRESHOLD
                            - getattr(monster, "mutation_count", 0))
                if calc.get("cost_mutation", 0) >= headroom:
                    break
            # 碎片/假碎片类道纹不经过 _monster_can_pay_calc_cost，单独封顶
            if effective_name == "赌命" and getattr(monster, "fake_shards", 0) < x:
                break
            if effective_name == "消灾" and (monster.fake_shards < 50 * x
                                             and monster.shards < 5 * x):
                break
            best = x
        return best

    def _resolve_monster_daowen_choice(
        self, monster: Entity, choice: dict, refs: dict[str, Entity], activated: set,
        prepared_option: dict,
    ) -> dict:
        name = choice.get("name", "")
        inst = monster.dao_wen.get(name)
        if (inst is None or name in self._monster_round_used(monster)
                or not inst.can_use()):
            raise ValueError(f"{monster.name}不能发动道纹【{name}】")
        if name not in DaoWenEngine.list_all():
            raise ValueError(f"未知道纹【{name}】")
        rewritten_as = (self._resonance_rewrites.get(id(monster)) or {}).get(name)
        effective_name = rewritten_as or name
        requires_target = self._daowen_requires_target(effective_name)
        target_ref = choice.get("target_ref", "")
        if requires_target:
            target = refs.get(target_ref)
            if target is None:
                raise ValueError(f"道纹【{effective_name}】必须提交合法target_ref")
            if target is not monster and not self.is_targetable(monster, target):
                raise ValueError(f"目标{target.name}当前不可被{monster.name}选中")
        else:
            if target_ref:
                raise ValueError(f"道纹【{effective_name}】不接受target_ref")
            target = monster

        # 2026-09-16 用户令：面板未写死 X（x_free）时，X 由发动方在提交里自选，
        # 「上限只受法限或者代价限制」——这里按 prepare 同一口径重新探一次上限并校验，
        # 防止提交方给出此刻已付不起的 X（资源在 prepare 之后可能已被消耗）。
        if getattr(inst, "x_free", False):
            submitted_x = choice.get("x")
            # 提交方没有给 X 时**回退到可负担上限**而非报错。
            # sim/ 下有几十处怪物阶段驱动各自拼装提交字典，面板去掉 X 后它们
            # 不会凭空多出一个 x 字段；若此处硬报错，迁面板就等于让整个
            # 模拟器与手操流程当场跑不起来。回退保证"改面板不会改坏"，
            # 想要更精细取值的 AI 自行提交 x 即可（见 sim/duel_common.py）。
            # 只把"没有这个字段"当作未提交；负数/0 是明确的非法输入，必须拒绝，
            # 不能拿任何整数值当哨兵（否则 -1 会被静默当成"回退到上限"）。
            missing = submitted_x is None
            if not missing and (not isinstance(submitted_x, int)
                                or isinstance(submitted_x, bool)):
                raise ValueError(f"道纹【{name}】的x必须是整数")
            hard_cap = (len(prepared_option.get("dodge_target_options", []))
                        if effective_name == "波及" else None)
            max_x = self._monster_max_daowen_x(monster, effective_name, target,
                                               hard_cap=hard_cap)
            if missing:                    # 提交方未给 X → 回退到上限
                effective_x = max_x
            elif not 1 <= submitted_x <= max_x:
                raise ValueError(
                    f"道纹【{name}】X={submitted_x}超出可负担范围1~{max_x}")
            else:
                effective_x = submitted_x
            if effective_x < 1:
                raise ValueError(f"道纹【{name}】此刻无可负担的X（上限{max_x}）")
        else:
            effective_x = inst.x_value

        # 先完成完整闪避提交的静态校验，再支付任何代价或改变激活状态。
        calc = DaoWenEngine.resolve(effective_name, effective_x, target=target, caster=monster)
        # 动态代价校验：残韵改写/状态变化后怪物可能付不起代价（如速度归零后
        # 【洞察】(疲惫3)）——本次视为无法发动并跳过，不硬报错、不占出手。
        if not self._monster_can_pay_calc_cost(monster, calc):
            return {"monster": monster.name, "daowen_skipped": name,
                    "resolves_as": effective_name, "reason": "无法支付代价"}
        # 2026-09-16 用户令：怪物支付法力（旧条文"不支付法力"已废止）。
        # 付不起的情况已由上一闸门挡掉，这里只做实际支付。
        # 注意：法力同时就是[攻击力]，此刻支付会削弱本回合**之后**的普攻——
        # 攻击与道纹的先后顺序由提交方（AI/操作者）决定，这正是"先攻后纹还是先纹后攻"的取舍。
        if calc.get("cost_type") == "消耗" and calc.get("cost", 0) > 0:
            monster.spend_mana(calc["cost"])
        hostile = self.state.on_player_side(target) != self.state.on_player_side(monster)
        dodge = choice.get("dodge")
        blood_shadow = choice.get("blood_shadow", False)
        aoe_dodge_choices: list[tuple[Entity, bool, dict]] = []
        must_hit_preview = self.bizhong_remaining(monster) > 0
        if effective_name == "波及":
            # 波及X：选择X个[目标]建立/解除波及效果（持续∞）。每个目标显式提交闪避。
            submitted_dodges = choice.get("dodge_targets")
            # DM裁定2026-08-23自适应降X：以prepare快照的wave_effective_x为准
            # （min(面板X, 合法目标数)），驱动与校验始终同一口径。
            mark_count = int(prepared_option.get("wave_effective_x")
                             or calc.get("mark_targets", effective_x))
            if not isinstance(submitted_dodges, list) or len(submitted_dodges) != mark_count:
                raise ValueError(f"道纹【波及】必须为{mark_count}个目标显式提交dodge_targets")
            expected_ref_list = [
                target.get("ref") for target in prepared_option.get("dodge_target_options", [])
            ]
            expected_refs = set(expected_ref_list)
            received: dict[str, dict] = {}
            for entry in submitted_dodges:
                if (not isinstance(entry, dict) or not isinstance(entry.get("dodge"), bool)
                        or not isinstance(entry.get("blood_shadow"), bool)
                        or not isinstance(entry.get("target_ref"), str)
                        or entry["dodge"] and entry["blood_shadow"]):
                    raise ValueError("dodge_targets每项必须包含target_ref与布尔值dodge/blood_shadow")
                ref = entry["target_ref"]
                if ref in received or ref not in expected_refs:
                    raise ValueError("波及dodge_targets必须覆盖X个不重复的合法目标")
                received[ref] = entry
            for entry in submitted_dodges:
                ref = entry["target_ref"]
                entity = refs.get(ref)
                if entity is None or not entity.is_alive or entity is monster:
                    raise ValueError("prepare中的波及目标已失效，请重新prepare_monster_phase")
                want_dodge = entry["dodge"]
                if want_dodge and not must_hit_preview and entity.current_speed < 1:
                    raise ValueError(f"{entity.name}速度不足，不能选择闪避")
                if entry["blood_shadow"] and (must_hit_preview or not self.state.side_has(entity, "血影")
                                                or entity.current_hp <= 10):
                    raise ValueError(f"{entity.name}不能使用血影")
                if want_dodge and not must_hit_preview and self.state.side_has(entity, "回锋刀"):
                    allowed = {t_opt["ref"] for t_opt in prepared_option["target_options"]
                               if not self.state.on_player_side(refs[t_opt["ref"]])}
                    if entry.get("dodge_relic_target_ref") not in allowed:
                        raise ValueError("回锋刀触发必须显式提交合法目标")
                aoe_dodge_choices.append((entity, want_dodge, entry))
            if dodge not in (None, False):
                raise ValueError("波及使用dodge_targets，不接受dodge=true")
        elif requires_target and hostile:
            if not isinstance(dodge, bool) or not isinstance(blood_shadow, bool):
                raise ValueError(f"道纹【{effective_name}】必须显式提交布尔值dodge/blood_shadow")
            if dodge and blood_shadow:
                raise ValueError("不能同时闪避并使用血影")
            if dodge and not must_hit_preview and target.current_speed < 1:
                raise ValueError(f"{target.name}速度不足，不能选择闪避")
            if blood_shadow and (must_hit_preview or not self.state.side_has(target, "血影")
                                 or target.current_hp <= 10):
                raise ValueError(f"{target.name}不能使用血影")
            if dodge and not must_hit_preview and self.state.side_has(target, "回锋刀"):
                allowed = {entry["ref"] for entry in prepared_option["target_options"]
                           if self.state.on_player_side(refs[entry["ref"]]) != self.state.on_player_side(target)}
                if choice.get("dodge_relic_target_ref") not in allowed:
                    raise ValueError("回锋刀触发必须显式提交合法目标")
            if choice.get("dodge_targets") not in (None, []):
                raise ValueError(f"道纹【{effective_name}】不接受dodge_targets")
        else:
            if (dodge not in (None, False) or blood_shadow not in (None, False)
                    or choice.get("dodge_targets") not in (None, [])):
                raise ValueError(f"道纹【{effective_name}】当前结算不接受闪避提交")

        trigger_choices = choice.get("trigger_spell_choices", {})
        # 执行阶段：守夜灯法力已在[敌回始]实际授予，无需预计算（extra_mana 默认0）。
        self.validate_daowen_trigger_spells(monster, trigger_choices, refs)
        trigger_logs = self.resolve_daowen_trigger_spells(monster, trigger_choices, refs)
        if not monster.is_alive:
            return {"monster": monster.name, "daowen_activated": name,
                    "interrupted_by_spell": True, "trigger_spell_logs": trigger_logs}

        # 残韵改写只替换本次结算：不支付源道纹代价，也不将源道纹记为已激活。
        if rewritten_as:
            consumed = self.consume_resonance_rewrite(monster, name)
            if consumed != rewritten_as:
                raise ValueError("残韵改写已变化，请重新prepare_monster_phase")
        # 原始怪物道纹发动时支付异变5X；选择导致崩解仍是合法结算，效果中断。
        elif name in self.ORIGINAL_MONSTER_DAOWEN:
            paid = monster.add_mutation(self.YUANCHU_COST_RATE * effective_x)
            if paid["collapsed"]:
                # 修复：此前直接 return，崩解死者从不进入统一死亡管线
                # （不产生 _death_ctx、不进 dead_monsters、不触发焦黑发丝/分裂）。
                self._on_entity_death(monster, ctx=self._collapse_context(monster, {
                    "timing": "monster_action", "source": name, "source_type": "daowen",
                    "actor": monster, "target": monster, "mechanic": "cost",
                    "subtype": "mutation", "amount": self.YUANCHU_COST_RATE * effective_x,
                    "tags": {"daowen", "active_payment"}}))
                return {"monster": monster.name, "collapsed": name,
                        "note": "支付异变后触发【崩解】，道纹效果中断"}
        elif name == "封印":
            # 怪物侧若持有【封印】，同样按新版口径支付异变X；玩家【封印】才会
            # 把目标怪物放入延迟回场队列。
            paid = monster.add_mutation(effective_x)
            if paid["collapsed"]:
                self._on_entity_death(monster, ctx=self._collapse_context(monster, {
                    "timing": "monster_action", "source": name, "source_type": "daowen",
                    "actor": monster, "target": monster, "mechanic": "cost",
                    "subtype": "mutation", "amount": effective_x,
                    "tags": {"daowen", "active_payment"}}))
                return {"monster": monster.name, "collapsed": name,
                        "note": "支付异变后触发【崩解】，道纹效果中断"}
        elif name == "赌命":
            if monster.fake_shards < effective_x:
                raise ValueError(f"{monster.name}假碎片不足，不能发动【赌命】")
            monster.fake_shards -= effective_x
        elif name == "消灾":
            fake_cost, real_cost = 50 * effective_x, 5 * effective_x
            if monster.fake_shards >= fake_cost:
                monster.fake_shards -= fake_cost
            else:
                # DM裁定2026-08-22（方案A）：怪物的真碎片类代价允许**透支成负债**——
                # 余额不足不再拒绝发动，而是把 shards 扣成负数。负债是【还债】路径的
                # 唯一产生入口；此前余额门禁让怪物碎片守恒≥0，还债在20万+局中零触发
                # （_shards_of 早已设计"负债不抵消假碎片"语义，缺的就是产生入口）。
                # 仅怪物适用：玩家/朋友/员工/api 侧维持余额不足拒绝发动。
                monster.shards -= real_cost

        if not rewritten_as:
            activated.add(name)
            self._monster_round_used(monster).add(name)
            # 2026-09-16 用户令：道纹递增（升级）机制已废止。X 恒为面板/借用时写定的值，
            # 重复发动按同一 X 计费，不再随发动次数累加。
        monster.actions_used_this_round += 1

        aoe_targets_override = None
        if effective_name == "波及":
            # 波及X：按显式提交逐目标建立/解除波及标记（持续∞，[战终]清除）。
            wave_marked: list[str] = []
            wave_unmarked: list[str] = []
            for entity, want_dodge, entry in aoe_dodge_choices:
                if must_hit_preview:
                    self.consume_bizhong(monster)
                    marked = self._toggle_wave_mark(entity, monster)
                    (wave_marked if marked else wave_unmarked).append(entity.name)
                elif entry["blood_shadow"]:
                    self.pay_numeric_cost(
                        entity, "流血", 10,
                        cost_share_target_ref=entry.get("cost_share_target_ref", ""),
                        cost_context={"timing": "reaction", "source": "血影", "source_type": "relic", "tags": {"active_payment"}})
                elif want_dodge:
                    self._spend_dodge_speed(entity, entry.get("dodge_relic_target_ref"))
                else:
                    marked = self._toggle_wave_mark(entity, monster)
                    (wave_marked if marked else wave_unmarked).append(entity.name)
            execution = self.apply_daowen_effect(effective_name, calc, monster, target)
            execution["wave_marked"] = wave_marked
            execution["wave_unmarked"] = wave_unmarked
            return {"monster": monster.name, "daowen_activated": name, "x": effective_x,
                    "resolves_as": effective_name, "resonance_rewrite": bool(rewritten_as),
                    "target": target.name, "execution": execution,
                    "trigger_spell_logs": trigger_logs}
        elif requires_target and hostile:
            if must_hit_preview:
                self.consume_bizhong(monster)
            elif blood_shadow:
                self.pay_numeric_cost(
                    target, "流血", 10,
                    cost_share_target_ref=choice.get("cost_share_target_ref", ""),
                    cost_context={"timing": "reaction", "source": "血影", "source_type": "relic", "tags": {"active_payment"}})
                return {"monster": monster.name, "daowen_activated": name,
                        "resolves_as": effective_name, "target": target.name, "blood_shadow": True,
                        "trigger_spell_logs": trigger_logs}
            elif dodge:
                self._spend_dodge_speed(target, choice.get("dodge_relic_target_ref"))
                return {"monster": monster.name, "daowen_activated": name,
                        "resolves_as": effective_name, "target": target.name, "dodged": True,
                        "trigger_spell_logs": trigger_logs}

        execution = self.apply_daowen_effect(
            effective_name, calc, monster, target,
            aoe_targets_override=aoe_targets_override,
        )
        return {"monster": monster.name, "daowen_activated": name, "x": effective_x,
                "resolves_as": effective_name, "resonance_rewrite": bool(rewritten_as),
                "target": target.name, "execution": execution,
                "trigger_spell_logs": trigger_logs}

    def _validate_monster_daowen_schema(
        self, monster: Entity, choice: dict, refs: dict[str, Entity],
        prepared_option: dict, pending_shouyedeng: int = 0,
    ) -> None:
        """道纹选择的静态 schema 校验（零副作用）。

        只校验与执行状态无关的结构/引用/布尔提交；依赖执行后状态的数值
        （目标当前速度、血影所需生命、本回合已使用集合）留给执行阶段动态
        校验 + 快照回滚兜底，避免把"道纹执行会改变的状态"提前固化。
        """
        name = choice.get("name", "")
        inst = monster.dao_wen.get(name)
        if (inst is None or name in self._monster_round_used(monster)
                or not inst.can_use()):
            raise ValueError(f"{monster.name}不能发动道纹【{name}】")
        if name not in DaoWenEngine.list_all():
            raise ValueError(f"未知道纹【{name}】")
        rewritten_as = (self._resonance_rewrites.get(id(monster)) or {}).get(name)
        effective_name = rewritten_as or name
        requires_target = self._daowen_requires_target(effective_name)
        target_ref = choice.get("target_ref", "")
        if requires_target:
            target = refs.get(target_ref)
            if target is None:
                raise ValueError(f"道纹【{effective_name}】必须提交合法target_ref")
            if target is not monster and not self.is_targetable(monster, target):
                raise ValueError(f"目标{target.name}当前不可被{monster.name}选中")
        else:
            if target_ref:
                raise ValueError(f"道纹【{effective_name}】不接受target_ref")
            target = monster

        hostile = self.state.on_player_side(target) != self.state.on_player_side(monster)
        dodge = choice.get("dodge")
        blood_shadow = choice.get("blood_shadow", False)
        if effective_name == "波及":
            submitted_dodges = choice.get("dodge_targets")
            # DM裁定2026-08-23自适应降X：与执行阶段同一口径（prepare快照）。
            mark_count = int(prepared_option.get("wave_effective_x") or inst.x_value)
            if not isinstance(submitted_dodges, list) or len(submitted_dodges) != mark_count:
                raise ValueError(f"道纹【波及】必须为{mark_count}个目标显式提交dodge_targets")
            expected_refs = {
                t_opt.get("ref") for t_opt in prepared_option.get("dodge_target_options", [])
            }
            received: dict[str, dict] = {}
            for entry in submitted_dodges:
                if (not isinstance(entry, dict) or not isinstance(entry.get("dodge"), bool)
                        or not isinstance(entry.get("blood_shadow"), bool)
                        or not isinstance(entry.get("target_ref"), str)
                        or entry["dodge"] and entry["blood_shadow"]):
                    raise ValueError("dodge_targets每项必须包含target_ref与布尔值dodge/blood_shadow")
                ref = entry["target_ref"]
                if ref in received or ref not in expected_refs:
                    raise ValueError("波及dodge_targets必须提交X个不重复的合法目标")
                received[ref] = entry
            if dodge not in (None, False):
                raise ValueError("波及使用dodge_targets，不接受dodge=true")
        elif requires_target and hostile:
            if not isinstance(dodge, bool) or not isinstance(blood_shadow, bool):
                raise ValueError(f"道纹【{effective_name}】必须显式提交布尔值dodge/blood_shadow")
            if dodge and blood_shadow:
                raise ValueError("不能同时闪避并使用血影")
            if choice.get("dodge_targets") not in (None, []):
                raise ValueError(f"道纹【{effective_name}】不接受dodge_targets")
        else:
            if (dodge not in (None, False) or blood_shadow not in (None, False)
                    or choice.get("dodge_targets") not in (None, [])):
                raise ValueError(f"道纹【{effective_name}】当前结算不接受闪避提交")

        trigger_choices = choice.get("trigger_spell_choices", {})
        self.validate_daowen_trigger_spells(
            monster, trigger_choices, refs, extra_mana=pending_shouyedeng)

    def _validate_monster_phase_static(
        self, submitted: dict[str, dict], prepared: dict,
    ) -> None:
        """怪物阶段全部输入的静态 schema 校验（零副作用）。

        在任何执行（龙息/守夜灯/道纹/攻击）之前拦截与执行状态无关的非法输入：
          - 道纹选择/目标引用/闪避提交结构（_validate_monster_daowen_schema）
          - attack_actions 数量（vs prepare 快照）
          - 每次命中的 target_ref/dodge/blood_shadow 布尔/血影资格/回锋刀目标/法术提交

        依赖执行后状态的数量校验（hits 命中数、目标当前速度、目标存活性）不在此
        判定——它们必须按执行时的真实状态校验（如【变形】会改变命中数），失败由
        resolve_monster_phase 的快照回滚保证零副作用。
        """
        expected = {actor["actor_ref"]: actor for actor in prepared["actors"]}
        refs = self._combat_entity_refs()
        # 守夜灯自 2026-09-13 改为[回始]授予，进入怪物阶段时法力已经在池里，
        # 静态校验无需再预付（再预付就是重复计算）。
        pending_shouyedeng = 0
        for actor_ref, choice in submitted.items():
            monster = refs.get(actor_ref)
            if monster is None or not monster.is_alive:
                continue  # 与执行循环一致：死斗部分提交/已死者跳过
            expected_actor = expected[actor_ref]

            dao_choice = choice.get("daowen")
            options = {o["name"] for o in expected_actor["daowen_options"]}
            if options and not isinstance(dao_choice, dict):
                raise ValueError(f"{monster.name}必须从合法选项中提交一个daowen对象")
            if not options and dao_choice is not None:
                raise ValueError(f"{monster.name}本次没有合法道纹选项，daowen必须为null")
            if isinstance(dao_choice, dict):
                if dao_choice.get("name") not in options:
                    raise ValueError(f"{monster.name}提交的道纹不在prepare合法选项中")
                prepared_option = next(
                    option for option in expected_actor["daowen_options"]
                    if option["name"] == dao_choice["name"]
                )
                if (prepared_option["requires_target"]
                        and dao_choice.get("target_ref") not in {
                            target["ref"] for target in prepared_option["target_options"]
                        }):
                    raise ValueError(f"{monster.name}提交的道纹目标不在prepare合法选项中")
                self._validate_monster_daowen_schema(
                    monster, dao_choice, refs, prepared_option,
                    pending_shouyedeng=pending_shouyedeng,
                )

            attack_actions = choice.get("attack_actions")
            expected_actions = expected_actor["base_attack_actions"]
            if not isinstance(attack_actions, list) or len(attack_actions) != expected_actions:
                raise ValueError(f"{monster.name}必须提交{expected_actions}个attack_actions")
            legal_attack_options = {
                target["ref"]: target for target in expected_actor["attack_target_options"]
            }
            for attack_action in attack_actions:
                if (not isinstance(attack_action, dict)
                        or not isinstance(attack_action.get("hits"), list)):
                    raise ValueError("每个attack_action必须包含hits列表")
                for hit in attack_action["hits"]:
                    if (not isinstance(hit, dict) or not isinstance(hit.get("dodge"), bool)
                            or not isinstance(hit.get("blood_shadow"), bool)):
                        raise ValueError("每次攻击必须显式提交target_ref、dodge与blood_shadow")
                    if hit["dodge"] and hit["blood_shadow"]:
                        raise ValueError("同一次判定不能同时闪避并使用血影")
                    if hit.get("target_ref") not in legal_attack_options:
                        raise ValueError("怪物攻击目标不在prepare合法选项中")
                    target = refs.get(hit.get("target_ref", ""))
                    if target is None or not self.state.on_player_side(target):
                        raise ValueError("怪物攻击target_ref必须是prepare列出的己方目标")
                    if not self.is_targetable(monster, target):
                        raise ValueError(f"{target.name}当前不可被{monster.name}选中")
                    option = legal_attack_options[hit["target_ref"]]
                    if hit["blood_shadow"] and not option.get("can_blood_shadow"):
                        raise ValueError(f"{target.name}不能使用血影")
                    if hit["dodge"] and self.state.side_has(target, "回锋刀"):
                        allowed = {entry["ref"] for entry in option["dodge_relic_target_options"]}
                        if hit.get("dodge_relic_target_ref") not in allowed:
                            raise ValueError("回锋刀触发必须显式提交合法目标")
                    self.validate_spell_reaction_submission(
                        target, monster, hit.get("spell_choices"), refs,
                        extra_mana=pending_shouyedeng,
                    )

    def _monster_phase_snapshot(self) -> dict:
        """怪物阶段执行前快照：state（含实体/事件流/碎片/消耗品）+ 引擎侧怪物状态。"""
        import copy
        return {
            "state": copy.deepcopy(self.state),
            "activated": copy.deepcopy(self._monster_activated),
            "round_used": copy.deepcopy(self._monster_daowen_round_used),
            "rewrites": copy.deepcopy(self._resonance_rewrites),
            "sanxiang": self._sanxiang_consumed,
            "dice": copy.deepcopy(self.dice),
            "split_spawned": getattr(self, "_split_clones_spawned", 0),
        }

    def _monster_phase_restore(self, snap: dict) -> None:
        """恢复执行前快照：任何非法 resolve 输入/执行中异常都不得留下战斗副作用。

        原地恢复 self.state 的内容（保持 combat.state 与 api 层 engine.state 是
        同一对象引用），并把实体/列表替换为快照副本——外部代码在失败后应重新
        从 state 读取实体，不得继续使用失败前的旧引用。
        """
        state = self.state
        restored = snap["state"]
        state.__dict__.clear()
        state.__dict__.update(restored.__dict__)
        self._monster_activated = snap["activated"]
        self._monster_daowen_round_used = snap["round_used"]
        self._resonance_rewrites = snap["rewrites"]
        self._sanxiang_consumed = snap["sanxiang"]
        self.dice = snap["dice"]
        self._split_clones_spawned = snap["split_spawned"]

    def resolve_monster_phase(self, choices: list[dict], prepared: dict) -> list[dict]:
        """严格按传入的prepare快照验证并结算；任何非法输入由API事务整体回滚。"""
        if not isinstance(choices, list):
            raise ValueError("choices必须是列表")
        if not isinstance(prepared, dict) or not isinstance(prepared.get("actors"), list):
            raise ValueError("prepared必须是prepare_monster_phase返回的合法快照")
        expected = {actor["actor_ref"]: actor for actor in prepared["actors"]}
        submitted: dict[str, dict] = {}
        for choice in choices:
            if not isinstance(choice, dict):
                raise ValueError("每个怪物选择必须是对象")
            ref = choice.get("actor_ref", "")
            if ref in submitted:
                raise ValueError(f"重复提交怪物选择: {ref}")
            submitted[ref] = choice
        # 死斗交替（对称）：守擂侧每步只结算1个actor，其余本步不动
        # （逐出手交替与挑战者侧一致，修复守擂方机制性必胜）。
        if not self.state.in_final_duel and set(submitted) != set(expected):
            raise ValueError(f"必须为全部可行动怪物各提交一次选择；需要{sorted(expected)}，收到{sorted(submitted)}")
        # 事务一致性（2026-08-19）：先完成全部静态 schema 校验（零副作用），
        # 再执行任何龙息/守夜灯/道纹/攻击。依赖执行后状态的动态校验
        # （hits 命中数、目标速度/存活）在执行阶段进行，失败即快照回滚，
        # 保证"任何非法 resolve 输入不得留下任何战斗副作用"。
        self._validate_monster_phase_static(submitted, prepared)
        snapshot = self._monster_phase_snapshot()
        try:
            return self._execute_monster_phase(submitted, prepared, expected)
        except Exception:
            self._monster_phase_restore(snapshot)
            raise

    def _execute_monster_phase(
        self, submitted: dict[str, dict], prepared: dict, expected: dict[str, dict],
    ) -> list[dict]:
        """校验全部通过后的怪物阶段执行（原 resolve_monster_phase 执行体，语义不变）。"""
        refs = self._combat_entity_refs()
        results: list[dict] = []
        # 守夜灯：用户裁定 2026-09-13 改为[回始]授予且不再清空，
        # 故怪物阶段不再有「[敌回始]授予 / [敌回终]清空」这一对动作。
        results.extend(self._tick_baolie(self.state.get_all_enemy_side()))
        results.extend(prepared["skipped"])
        for actor_ref in submitted:  # 死斗部分提交：只结算本步提交的actor
            monster = refs.get(actor_ref)
            if monster is None or not monster.is_alive:
                continue
            choice = submitted[actor_ref]
            breath = self.apply_opposing_longxi(monster)
            if breath:
                results.append({"monster": monster.name, **breath})
                if not monster.is_alive:
                    continue
            activated = self._monster_activated.setdefault(id(monster), set())
            # 攻击出手数以“道纹结算前”的已激活集合为准：狂暴/疯狂是[回始]持续效果，
            # 本回合刚发动时从下回合起生效，prepare列出的 base_attack_actions 也是按
            # 结算前状态给出的——两处必须一致，否则按 prepare 提交必然失败。
            activated_before = set(activated)

            dao_choice = choice.get("daowen")
            options = {o["name"] for o in expected[actor_ref]["daowen_options"]}
            if options and not isinstance(dao_choice, dict):
                raise ValueError(f"{monster.name}必须从合法选项中提交一个daowen对象")
            if not options and dao_choice is not None:
                raise ValueError(f"{monster.name}本次没有合法道纹选项，daowen必须为null")
            if isinstance(dao_choice, dict):
                if dao_choice.get("name") not in options:
                    raise ValueError(f"{monster.name}提交的道纹不在prepare合法选项中")
                prepared_option = next(
                    option for option in expected[actor_ref]["daowen_options"]
                    if option["name"] == dao_choice["name"]
                )
                if (prepared_option["requires_target"]
                        and dao_choice.get("target_ref") not in {
                            target["ref"] for target in prepared_option["target_options"]
                        }):
                    raise ValueError(f"{monster.name}提交的道纹目标不在prepare合法选项中")
                dao_result = self._resolve_monster_daowen_choice(
                    monster, dao_choice, refs, activated, prepared_option,
                )
                results.append(dao_result)
                if not monster.is_alive:
                    continue

            attack_actions = choice.get("attack_actions")
            # 出手数按prepare快照校验：2026-08-17疯狂全局裁定后，状态在本actor
            # 道纹结算中即盖到全场，若此处按当前状态重算会把"自下回合生效"提前到
            # 本回合，导致按prepare提交必然失败；快照即契约（两处必须一致）。
            expected_actions = expected[actor_ref]["base_attack_actions"]
            if not isinstance(attack_actions, list) or len(attack_actions) != expected_actions:
                raise ValueError(f"{monster.name}必须提交{expected_actions}个attack_actions")
            hits_per_action = max(0, monster.attack_count - monster.get_status_value("手雷减攻"))
            for action_index, attack_action in enumerate(attack_actions):
                if not isinstance(attack_action, dict) or not isinstance(attack_action.get("hits"), list):
                    raise ValueError("每个attack_action必须包含hits列表")
                hits = attack_action["hits"]
                if len(hits) != hits_per_action:
                    raise ValueError(f"{monster.name}每个攻击出手必须提交{hits_per_action}次命中选择")
                monster.actions_used_this_round += 1
                legal_attack_options = {
                    target["ref"]: target for target in expected[actor_ref]["attack_target_options"]
                }
                for hit_index, hit in enumerate(hits):
                    if (not isinstance(hit, dict) or not isinstance(hit.get("dodge"), bool)
                            or not isinstance(hit.get("blood_shadow"), bool)):
                        raise ValueError("每次攻击必须显式提交target_ref、dodge与blood_shadow")
                    if hit["dodge"] and hit["blood_shadow"]:
                        raise ValueError("同一次判定不能同时闪避并使用血影")
                    if hit.get("target_ref") not in legal_attack_options:
                        raise ValueError("怪物攻击目标不在prepare合法选项中")
                    target = refs.get(hit.get("target_ref", ""))
                    if target is None or not self.state.on_player_side(target):
                        raise ValueError("怪物攻击target_ref必须是prepare列出的己方目标")
                    if not self.is_targetable(monster, target):
                        raise ValueError(f"{target.name}当前不可被{monster.name}选中")
                    if not target.is_alive:
                        results.append({"attacker": monster.name, "target": target.name,
                                        "skipped": "预选目标已命零", "hit_index": hit_index + 1})
                        continue
                    must_hit = self.bizhong_remaining(monster) > 0
                    if hit["dodge"] and not must_hit and target.current_speed < 1:
                        raise ValueError(f"{target.name}速度不足，不能选择闪避")
                    option = legal_attack_options[hit["target_ref"]]
                    if hit["blood_shadow"] and not option.get("can_blood_shadow"):
                        raise ValueError(f"{target.name}不能使用血影")
                    if hit["dodge"] and not must_hit and self.state.side_has(target, "回锋刀"):
                        allowed = {entry["ref"] for entry in option["dodge_relic_target_options"]}
                        if hit.get("dodge_relic_target_ref") not in allowed:
                            raise ValueError("回锋刀触发必须显式提交合法目标")
                    self.validate_spell_reaction_submission(
                        target, monster, hit.get("spell_choices"), refs,
                    )
                    attack_target = monster if monster.has_status("无神") else target
                    # 无神重定向（规则正文：目标强制选自身）：受击方已变为怪物自身，
                    # 但 hit["spell_choices"] 描述的是名义目标（玩家侧）的反应法术——
                    # resolve_attack 会按受击方资格集校验（见 1294 行），键集错配
                    # 必然报"必须逐一覆盖[]"，且此矛盾无法由提交方调和（同一字典需
                    # 同时匹配玩家与怪物的资格集）——引擎契约缺陷，曾占平衡模拟
                    # 无效局 46+/8000（2026-08-22 定位修复）。
                    # 重定向时受击反应按空提交校验（怪物无 spells，资格集恒空）；
                    # 若将来怪物可持反应法术，应新增 hit["self_spell_choices"] 契约字段。
                    reaction_choices = (hit.get("spell_choices")
                                        if attack_target is target else {"before": {}, "after": {}})
                    resolved = self.resolve_attack(
                        monster, attack_target, dodge=hit["dodge"], blood_shadow=hit["blood_shadow"],
                        spell_choices=reaction_choices, entity_refs=refs,
                        dodge_relic_target_ref=hit.get("dodge_relic_target_ref"),
                        cost_share_target_ref=hit.get("cost_share_target_ref", ""),
                    )
                    resolved.update({"hit_index": hit_index + 1, "hit_total": hits_per_action,
                                     "attack_action_index": action_index + 1,
                                     "new_action": (hit_index == 0)})
                    results.append(resolved)
                    if not monster.is_alive:
                        break
                if not monster.is_alive:
                    break
        return results

