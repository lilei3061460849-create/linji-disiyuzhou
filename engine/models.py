"""
核心数据模型
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Any
import math
import json
import uuid

from .enums import EffectScope, EffectPolarity
from .effect_context import EffectContext, make_context, normalize_context
from .combat_events import CombatEvent, CombatEventType, get_combat_event_observer
from .personality import export_for_ai as personality_export_for_ai


@dataclass
class DaoWen:
    """道纹定义"""
    name: str                    # 道纹名称（两字）
    formula: str                 # 公式描述
    cost_type: str               # 代价/消耗类型
    cost_formula: str            # 消耗公式
    effect_formula: str          # 效果公式
    trigger_timing: str = ""     # 触发时点
    is_monster_original: bool = False  # 是否原始怪物道纹
    is_monster_transform: bool = False  # 是否怪物转化道纹
    tags: list[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "formula": self.formula,
            "cost_type": self.cost_type,
            "cost_formula": self.cost_formula,
            "effect_formula": self.effect_formula,
            "trigger_timing": self.trigger_timing,
            "is_monster_original": self.is_monster_original,
            "is_monster_transform": self.is_monster_transform,
            "tags": self.tags
        }


@dataclass
class DaoWenInstance:
    """道纹实例 - 角色持有的道纹"""
    dao_wen: DaoWen
    x_value: int = 0            # 当前X值（自由控X规则）
    cooldown_remaining: int = 0 # 冷却剩余
    is_frozen: bool = False     # 是否被封印
    sha_qi: str = ""            # 乱葬岗附煞：法煞/魂煞/冥煞/血煞/锁煞/心煞
    
    def can_use(self) -> bool:
        return not self.is_frozen and self.cooldown_remaining <= 0


@dataclass
class Spell:
    """法术定义"""
    name: str
    required_daowen: list[str]   # 所需道纹列表
    trigger_condition: str       # 触发条件
    effect_flow: str             # 生效流程
    rank: int = 1                # 阶级 = 所需道纹种数
    custom_conditions: list[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "required_daowen": self.required_daowen,
            "trigger_condition": self.trigger_condition,
            "effect_flow": self.effect_flow,
            "rank": self.rank,
            "custom_conditions": self.custom_conditions
        }


@dataclass
class Relic:
    """遗物"""
    name: str
    effect: str
    tags: list[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return {"name": self.name, "effect": self.effect, "tags": self.tags}


@dataclass
class Consumable:
    """消耗品"""
    name: str
    effect: str
    current_uses: int = 1
    max_uses: int = 1
    # kind: normal(普通) / sculpture(雕塑，1耐久=15伤害或20格挡)
    #       / dragon_heart(龙心谷"炼心"产出，耐久=可抵消的同类型代价点数)
    kind: str = "normal"
    # dragon_heart 专用：可抵消的代价类型（流血/衰老/疲惫）
    dragon_heart_type: str = ""

    @property
    def is_depleted(self) -> bool:
        return self.current_uses <= 0

    def use(self) -> int:
        if self.is_depleted:
            return 0
        self.current_uses -= 1
        return self.current_uses

    def merge(self, other: 'Consumable') -> bool:
        """合并相同消耗品（雕塑等绑定特定怪物的不可合并）"""
        if self.name != other.name or self.effect != other.effect:
            return False
        if self.kind != "normal" or other.kind != "normal":
            return False
        self.current_uses += other.current_uses
        self.max_uses += other.max_uses
        return True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "effect": self.effect,
            "current_uses": self.current_uses,
            "max_uses": self.max_uses,
            "is_depleted": self.is_depleted,
            "kind": self.kind,
            "dragon_heart_type": self.dragon_heart_type,
        }


@dataclass
class StatusEffect:
    """持续效果。scope 与 polarity 显式区分生命周期和增减益极性。"""
    name: str
    remaining_rounds: int        # 剩余回合（-1=∞；仍只代表本场战斗内的无限）
    value: int = 0               # 效果数值
    source: str = ""             # 来源
    scope: str = EffectScope.BATTLE.value
    polarity: str = EffectPolarity.NEUTRAL.value
    
    def __post_init__(self):
        # 即使调用方直接append而不经过Entity.add_status，代价标记也不能被战终误清。
        if (self.name in {"流血", "衰老", "枯竭", "萎缩", "疲惫", "异变", "崩解"}
                and self.scope == EffectScope.BATTLE.value):
            self.scope = EffectScope.COST.value

    @property
    def is_permanent(self) -> bool:
        return self.remaining_rounds == -1
    
    @property
    def is_expired(self) -> bool:
        return not self.is_permanent and self.remaining_rounds <= 0
    
    def tick(self) -> bool:
        """回合递减，返回是否仍有效"""
        if self.is_permanent:
            return True
        self.remaining_rounds -= 1
        return self.remaining_rounds > 0
    
    def merge_with(self, other: 'StatusEffect') -> bool:
        """合并同名效果：数值相加"""
        if self.name != other.name:
            return False
        if self.is_permanent or other.is_permanent:
            self.remaining_rounds = -1
        else:
            self.remaining_rounds += other.remaining_rounds
        self.value += other.value
        return True


@dataclass
class LongJiXin:
    """龙心（消耗品）"""
    cost_type: str               # 代价类型（流血/衰老/枯竭/萎缩）
    current_durability: int
    max_durability: int
    
    def consume(self, amount: int) -> int:
        """消耗耐久，返回实际抵消的代价"""
        actual = min(amount, self.current_durability)
        self.current_durability -= actual
        return actual
    
    @property
    def is_depleted(self) -> bool:
        return self.current_durability <= 0
    
    def to_dict(self) -> dict:
        return {
            "cost_type": self.cost_type,
            "current_durability": self.current_durability,
            "max_durability": self.max_durability
        }


@dataclass
class Entity:
    """游戏实体（轮回者/怪物/朋友/员工通用基础）"""
    name: str
    entity_type: str
    runtime_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    
    # 基础属性
    blood_limit: int = 0         # 血限
    current_hp: int = 0          # 当前生命
    mana_limit: int = 0          # 法限
    current_mana: int = 0        # 当前法力
    speed_limit: int = 0         # 速限
    current_speed: int = 0       # 当前速度
    
    # 攻击
    attack_count: int = 0        # 攻击次数
    attack_power: int = 0        # 攻击力
    
    # 道纹与法术
    dao_wen: dict[str, DaoWenInstance] = field(default_factory=dict)
    spells: list[Spell] = field(default_factory=list)
    # 残韵库存：每个轮回者实体独立持有（{转换: n, 反转: n, 曲解: n}）。
    # 早期版本残韵只挂在 State（仅玩家侧），导致守擂者同为轮回者却无残韵可用。
    # 现下放为实体级，使挑战者/守擂者共用同一套残韵机制（玩家侧仍经 State.resonance 兼容）。
    resonance: dict[str, int] = field(default_factory=dict)
    relics: list[Relic] = field(default_factory=list)  # 由正文明确授予该角色的随身物品（如防弹插板）
    
    # 状态
    shield: int = 0              # 格挡
    status_effects: list[StatusEffect] = field(default_factory=list)
    is_flying: bool = False      # 飞行状态

    # 非击杀移出战斗标记（封印等；不产碎片）
    removed_without_kill: bool = False
    # 统一【离场】标记（DM裁定 2026-08-18）：一切使角色脱离本场战斗的特殊事件
    # （雕塑/癌变/还债/救赎/封印/逃跑及未来新增）一律经 depart_battle() 置位。
    # 战斗胜利判定只看 命零(is_alive=False) 或 离场(is_departed)，新事件无须再改判定。
    is_departed: bool = False
    departure_reason: str = ""   # 离场原因（雕塑/癌变/还债/救赎/封印/逃跑/...）
    hp_lost_this_round: int = 0   # 本回合累计失去的生命（活血用，回始归零）
    actions_used_this_round: int = 0  # 本回合已消耗的出手次数（回始归零，用于出手预算校验）

    # 多路径胜利追踪
    shards: int = 0              # 怪物自带碎片（罪孽都市）/ 负值表示负债（还债）
    fake_shards: int = 0         # 假碎片（罪孽都市：假钞产出；战斗中失去碎片时优先失去假碎片）
    total_healed: int = 0        # 累计受到的恢复量（癌变；含过量部分，按原值计，双倍机制已删）
    is_sculptured: bool = False  # 已化为雕塑（攻击次数或攻击力归0）
    is_proliferated: bool = False  # 已被癌变吸收进死者之书（旧名 增生，已统一为 癌变；保留字段名兼容）
    is_debt_bound: bool = False  # 已因还债成为员工

    # ---- 罪孽都市专属道纹的回始记账（F2 全量） ----
    # 逼债/清算：目标侧挂账 [{x, caster}]，[回始]逐条结算，状态消失即清账
    _bizhai: list = field(default_factory=list)
    _qingsuan: list = field(default_factory=list)
    # 抵扣：被封印的遗物 {遗物名: 剩余回合}，[回终]-1，归零解封
    sealed_relics: dict = field(default_factory=dict)

    def lose_shards(self, amount: int) -> int:
        """失去碎片（假碎片优先，余额不足则不足额损失）。返回实际失去的真碎片数。"""
        amount = max(0, amount)
        use_fake = min(self.fake_shards, amount)
        self.fake_shards -= use_fake
        real = amount - use_fake
        self.shards -= real
        return real

    def depart_battle(self, reason: str):
        """统一【离场】入口：任何使角色脱离本场战斗的特殊事件都必须调用此方法。

        离场不是击杀：is_alive 置 False 仅表示脱离战场，不产生[碎片]奖励；
        奖励与分类逻辑可读取 departure_reason。新增特殊事件只需调用本方法，
        无须改动任何战斗胜利判定。
        """
        self.is_departed = True
        self.departure_reason = reason
        self.is_alive = False
        self.removed_without_kill = True  # 兼容旧字段：离场一律不视为击杀

    # 兼容：is_proliferated 旧名（增生）→ 现名 癌变，is_cancer 为别名
    @property
    def is_cancer(self) -> bool:
        return self.is_proliferated

    @is_cancer.setter
    def is_cancer(self, value: bool):
        self.is_proliferated = value

    # 异变计数（特殊事件【崩解】：达到阈值直接命零）
    mutation_count: int = 0

    # 存活
    is_alive: bool = True

    # 出战支援（罪孽都市专属机制1，全局对所有[员工]生效）：
    # [员工]默认待命不占场；is_deployed=True 才计入战场与工资结算。
    # 玩家/怪物/[朋友]/[临时朋友]与"还债"转化来的[员工]默认直接为True，保持既有行为不变。
    is_deployed: bool = True
    deployed_at_round: int = 0  # 派遣时 state.current_round 的原始值（用于结算"实际出场回合数"）

    # 撤退（任意[朋友]/[员工]即将受到足以使当前命零的伤害时自动触发）：
    # 保留当前生命，不再计入本场战斗(get_all_player_side排除)，无法再次加入本场战斗；
    # 但未死亡，[战终]后随存活[朋友]/[员工]一同留存，下一场重置为False可正常参战。
    has_retreated: bool = False

    # 血誓戒：本回合是否已经触发过"首次主动支付流血代价"奖励（回始归零）
    blood_oath_used_this_round: bool = False
    # [战始]时的血限快照，用于击杀碎片按战始血限结算（增殖等战斗中改变血限不影响此值）
    battle_start_blood_limit: int = 0

    # 寒冰法力（初拥之夜遗物）：本回合内，持有该遗物者对我方发动道纹累计消耗的法力（含对自己发动）
    # 回始归零；每满10点使当前回合出手次数-1（以叠加"无力"状态实现）
    mana_inflicted_this_round: int = 0

    # 血族血脉（初拥之夜遗物）：本回合是否已造成过伤害（[回终]判定：造成过则回复等量，否则流血20）
    damage_dealt_this_round: int = 0
    # 特殊事件【凡庸】：连续五回合未出手 / 五回合未能使敌对角色生命减少时触发。
    # 两个条件是"或"关系，故分别计数。多个角色同时达阈值时，非轮回者优先结算。
    no_action_rounds: int = 0   # 连续未出手回合数
    no_damage_rounds: int = 0   # 连续未使敌方生命减少的回合数
    # 本场战斗内累计获得的[回复]量。README第304行：[战终]清除局内增益(包括回复)，
    # 故战终须把这部分回血扣除（不低于进场时的生命）。
    healed_this_battle: int = 0
    # 本场[战始]时的当前生命，作为回复清除后的生命下限
    battle_start_hp: int = 0
    # 赤族诅咒标记：entity_type=="赤族"的实体[回终]固定流血20；is_chizu_of记录其主人名字(仅用于血食校验)
    is_chizu_of: str = ""

    def __post_init__(self):
        if self.battle_start_blood_limit == 0:
            self.battle_start_blood_limit = self.blood_limit

    # ---- 「失去生命后」统一拦截 (2026-08-30) ---- 
    # 用户要求：不要再逐个效果开窗调 _fire_after_life_lost，只要当前生命
    # 下降就统一触发。因此 current_hp 的每次写入都经 __setattr__ 捕获：
    # 若数值变小，则把 (old, new) 报给所属战斗引擎（若有绑定）。
    # 引擎侧以 _hp_loss_recording 计数器表示“这段降血正由既有入口
    # （_record_hp_loss_event / _apply_blood_limit_change 等）接管”，
    # 期间本钩子被抑制，避免双发；未被接管、或未来全新效果的直接
    # current_hp-= 写法则由本钩子兜底自动触发，无需再手工接线。
    # _hp_engine_ref 为可选弱引用（非字段，不参与序列化/深拷贝判定）。
    def __setattr__(self, name, value):
        old = self.__dict__.get(name, None)
        object.__setattr__(self, name, value)
        if name == "current_hp" and old is not None and value < old:
            self._fire_hp_loss(old, value)

    def _fire_hp_loss(self, old: int, new: int) -> None:
        eng = getattr(self, "_hp_engine_ref", None)
        if eng is None:
            return  # 未绑定引擎（模型层/局外/深拷贝快照）→ 纯数据，不触发战斗反应
        owner = eng()
        if owner is None:
            return
        # _resolving_life_lost_reactions>0 表示正结算反应自身 → 不再连锁。
        if getattr(owner, "_resolving_life_lost_reactions", 0) > 0:
            return
        owner._on_entity_hp_fallen(self, old, new)

    def __getstate__(self):
        """存档/快照序列化时剔除战斗期引擎弱引用（不可 pickle）。"""
        state = self.__dict__.copy()
        state.pop("_hp_engine_ref", None)
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.__dict__["_hp_engine_ref"] = None

    
    @property
    def action_count(self) -> int:
        """出手次数：轮回者=速限/3向上取整；[朋友]/[员工](微光者，面板无速限)=攻击次数/3向上取整。
        怪物行动由CombatEngine的prepare/resolve两阶段接口独立计算，不使用本属性。
        疯狂+X、无力-X 对本属性的两种口径均生效。"""
        if self.entity_type in ("朋友", "员工"):
            base = math.ceil(self.attack_count / 3) if self.attack_count > 0 else 0
        else:
            base = math.ceil(self.speed_limit / 3) if self.speed_limit > 0 else 0
        base += self.get_status_value("疯狂")
        base -= self.get_status_value("无力")
        return max(0, base)

    @property
    def hp_ratio(self) -> float:
        return self.current_hp / self.blood_limit if self.blood_limit > 0 else 0
    
    def take_damage(self, amount: int, damage_type: str = "普通") -> dict:
        """
        受到伤害，返回结算详情
        规则：格挡仅能抵消外部【伤害】，代价绝对无法被格挡吸收
        """
        detail = {
            "raw_damage": amount,
            "shield_absorbed": 0,
            "actual_damage": 0,
            "hp_before": self.current_hp,
            "hp_after": self.current_hp,
            "blood_limit_before": self.blood_limit,
            "died": False,
            "damage_type": damage_type
        }
        
        if amount <= 0:
            return detail
        
        remaining = amount
        
        # 格挡抵消（代价类型的伤害不被格挡抵消；"无视格挡"为【贯穿】等效果，同样跳过格挡）
        if self.shield > 0 and damage_type not in ("代价", "无视格挡"):
            absorbed = min(self.shield, remaining)
            self.shield -= absorbed
            remaining -= absorbed
            detail["shield_absorbed"] = absorbed
        
        # 固执：自身单次失去生命最高为 1。代价不被格挡，也不被固执压帽。
        if remaining > 0 and damage_type != "代价" and self.has_status("固执"):
            remaining = min(remaining, 1)
            detail["capped_by"] = "固执"

        # 扣除生命
        self.current_hp = max(0, self.current_hp - remaining)
        detail["actual_damage"] = remaining
        detail["hp_after"] = self.current_hp
        self.hp_lost_this_round += remaining  # 活血追踪
        
        if self.current_hp <= 0:
            # 模型层只翻标记，不知道战斗上下文。命零的“通知 + 死后效果”必须由调用方
            # 交给 CombatEngine._check_hp_zero_death()（唯一统一死亡入口）。
            detail["died"] = True
            self.is_alive = False
        elif remaining > 0 and self.has_status("眩晕"):
            # 眩晕：失去生命后立刻苏醒
            self.status_effects = [s for s in self.status_effects if s.name != "眩晕"]
            detail["xuanyun_broken"] = True
        
        return detail
    
    MUTATION_COLLAPSE_THRESHOLD = 50  # 特殊事件【崩解】阈值：异变达到50层直接命零；原始道纹仅首次发动支付异变5X

    def add_mutation(self, layers: int) -> dict:
        """
        增减异变层数。正值累加，负值削减，可降到负数。
        特殊事件【崩解】：任一角色异变达到阈值（当前50层）时直接[命零]死亡；
        正在结算的效果是否中断由调用方判定（与中断规则同精神：代价先付）。
        """
        if not isinstance(layers, int) or isinstance(layers, bool):
            raise ValueError("异变层数必须是整数")
        if layers != 0:
            self.mutation_count += layers
        collapsed = self.mutation_count >= self.MUTATION_COLLAPSE_THRESHOLD
        if collapsed and self.is_alive:
            # 同 take_damage：调用方必须在拿到 collapsed=True 后走
            # CombatEngine._on_entity_death(..., ctx=_collapse_context(...))，
            # 否则崩解死者不会触发任何[命零]效果。
            # 先翻 is_alive 再置 0：崩解=命零死因，不触发「失去生命后」反应。
            self.is_alive = False
            self.current_hp = 0
        return {
            "mutation_added": layers,
            "mutation_total": self.mutation_count,
            "collapsed": collapsed,
        }
    
    def heal(self, amount: int) -> dict:
        """回复生命"""
        before = self.current_hp
        self.current_hp = min(self.blood_limit, self.current_hp + amount)
        actual = self.current_hp - before
        overheal = amount - actual
        # 癌变追踪：DM裁定（2026-08-18）删除过量回复双倍计入机制，
        # 受到的全部回复（含过量部分）一律按原值计入累计恢复量。
        self.total_healed += amount
        self.healed_this_battle += actual
        return {
            "heal_amount": amount,
            "actual_heal": actual,
            "hp_before": before,
            "hp_after": self.current_hp,
            "overheal": overheal
        }
    
    def gain_shield(self, amount: int) -> int:
        """获得格挡"""
        self.shield += amount
        return self.shield
    
    def clear_shield(self):
        """清空格挡（回终）"""
        self.shield = 0
    
    def spend_mana(self, amount: int) -> bool:
        """消耗法力。愤怒：法力消耗减半（向上取整）。"""
        if amount > 0 and self.has_status("愤怒"):
            amount = math.ceil(amount / 2)
        if self.current_mana < amount:
            return False
        self.current_mana -= amount
        return True

    def get_status_effects(self, name: str) -> list[StatusEffect]:
        return [s for s in self.status_effects if s.name == name]
    
    def has_status(self, name: str) -> bool:
        return any(s.name == name and not s.is_expired for s in self.status_effects)
    
    def get_status_value(self, name: str) -> int:
        return sum(s.value for s in self.status_effects if s.name == name and not s.is_expired)
    
    def tick_status_effects(self, skip_names: tuple = ()) -> list[str]:
        """回合递减，返回已过期的效果名。skip_names 本拍不减（爆裂改走敌回终）。"""
        expired = []
        remaining = []
        for s in self.status_effects:
            if s.name in skip_names:
                remaining.append(s)
                continue
            if not s.tick():
                expired.append(s.name)
            else:
                remaining.append(s)
        self.status_effects = remaining
        return expired
    
    def add_status(self, effect: StatusEffect):
        """添加状态效果；未显式给出极性时按规则表标注，生命周期仍由scope独立决定。"""
        if effect.polarity == EffectPolarity.NEUTRAL.value:
            buffs = {
                "固执", "贯穿", "急速", "洞察", "兴奋", "飞行", "滑翔", "狂暴",
                "强化", "疯狂", "必中", "自愈", "洗劫", "逆鳞", "嫁祸", "背负",
                "负岳索", "加速", "愤怒",
            }
            debuffs = {
                "弱化", "无力", "减速", "迟滞", "束缚", "封印", "坠落",
                "坏死", "爆裂", "退化", "定型", "畸变", "僵化", "加害", "伤痕",
                "寄生", "蒙蔽", "眩晕", "手雷减攻", "衰败", "被背负",
            }
            if effect.name in buffs:
                effect.polarity = EffectPolarity.BUFF.value
            elif effect.name in debuffs:
                effect.polarity = EffectPolarity.DEBUFF.value
        for existing in self.status_effects:
            if existing.merge_with(effect):
                return
        self.status_effects.append(effect)
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "entity_type": self.entity_type,
            "runtime_id": self.runtime_id,
            "blood_limit": self.blood_limit,
            "current_hp": self.current_hp,
            "mana_limit": self.mana_limit,
            "current_mana": self.current_mana,
            "speed_limit": self.speed_limit,
            "current_speed": self.current_speed,
            "attack_count": self.attack_count,
            "attack_power": self.attack_power,
            "action_count": self.action_count,
            "shield": self.shield,
            "is_flying": self.is_flying,
            "is_alive": self.is_alive,
            "removed_without_kill": self.removed_without_kill,
            "is_departed": self.is_departed,
            "departure_reason": self.departure_reason,
            "is_deployed": self.is_deployed,
            "has_retreated": self.has_retreated,
            "battle_start_blood_limit": self.battle_start_blood_limit,
            "deployed_at_round": self.deployed_at_round,
            "shards": self.shards,
            "fake_shards": self.fake_shards,
            "total_healed": self.total_healed,
            "hp_ratio": round(self.hp_ratio, 2),
            "dao_wen": {k: v.dao_wen.name for k, v in self.dao_wen.items()},
            "spells": [s.name for s in self.spells],
            "relics": [r.to_dict() for r in self.relics],
            "status_effects": [
                {"name": s.name, "value": s.value, "rounds": s.remaining_rounds,
                 "source": s.source, "scope": s.scope, "polarity": s.polarity}
                for s in self.status_effects
            ]
        }


@dataclass
class ScopedStatDelta:
    """一笔可逆的字段变化；使用稳定实体引用，存档/事务回滚后仍指向当前状态对象。"""
    entity_id: str
    entity_name: str
    field_name: str
    delta: int
    scope: str
    polarity: str
    source: str

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "entity": self.entity_name,
            "field": self.field_name,
            "delta": self.delta,
            "scope": self.scope,
            "polarity": self.polarity,
            "source": self.source,
        }


@dataclass
class GameState:
    """完整游戏状态"""
    # 游戏元数据
    game_id: str = ""
    phase: str = "setup"
    # 战斗内强制顺序；正式战始会置为 await_round_start。
    combat_subphase: str = "player_actions"
    current_round: int = 0
    current_battle: int = 0     # 第几场战斗
    current_region: str = ""    # 当前副本
    energy: int = 3             # 局外精力
    
    # 轮回者
    player: Optional[Entity] = None
    
    # 队友
    friends: list[Entity] = field(default_factory=list)
    employees: list[Entity] = field(default_factory=list)
    temp_friends: list[Entity] = field(default_factory=list)
    
    # 敌方
    enemies: list[Entity] = field(default_factory=list)

    # 角色性格特征（2026-08-26）：{runtime_id: {name, traits:{dimension: entry}}}
    # 实例级数据（键=Entity.runtime_id，同名不同实例互不共享）；
    # 只由 engine/personality.py 读写：行为推断写入、命零时经统一死亡管线删除；
    # 纯 dict 结构，随本类 deepcopy 快照 / pickle 存档自然往返，不写入任何角色模板。
    personality_traits: dict[str, dict] = field(default_factory=dict)
    
    # 资源
    shards: int = 20            # 碎片
    fake_shards: int = 0        # 假碎片（罪孽都市假钞X产出；战斗中失去碎片时优先失去假碎片）

    def lose_shards(self, amount: int) -> int:
        """玩家失去碎片（假碎片优先）。返回实际失去的真碎片数。"""
        amount = max(0, amount)
        use_fake = min(self.fake_shards, amount)
        self.fake_shards -= use_fake
        real = amount - use_fake
        self.shards -= real
        return real
    
    # 遗物与消耗品
    relics: list[Relic] = field(default_factory=list)
    relics_pool: list[Relic] = field(default_factory=list)  # 遗物池（未获取的）
    relic_pool_initialized: bool = False
    # 【发现】：随机列出3个未持有候选后，必须由角色显式选1件。
    pending_relic_choices: list[str] = field(default_factory=list)
    pending_relic_source: str = ""
    # 开局从杀伐闭环【发现】初始道纹。
    pending_initial_daowen_choices: list[str] = field(default_factory=list)
    pending_initial_daowen_source: str = ""
    # 救赎：怪物融化后等待接纳/无视。
    pending_redemption: dict = field(default_factory=dict)
    # 消耗品【发现】候选（如扭曲都市完成事件后的工具发现）。
    pending_item_choices: list[str] = field(default_factory=list)
    pending_item_source: str = ""
    # 两阶段怪物决策：prepare产生的合法选项与一次性token；resolve后清空。
    pending_monster_phase: dict = field(default_factory=dict)
    # 两阶段攻击：prepare绑定行动者、逐击合法目标与反应选项；resolve后清空。
    pending_attack: dict = field(default_factory=dict)
    # 二档【探索】一次抽取两个未遇事件，首个结算后按顺序激活其余事件。
    pending_event_queue: list[str] = field(default_factory=list)
    # 事件产生的跨行动/跨战斗确定性状态，键均由事件handler登记。
    event_modifiers: dict[str, Any] = field(default_factory=dict)
    # 显式作用域账本：只登记需要按回合/战斗回滚的面板字段变化。
    # 代价、伤害、资源支出与永久成长不进入可逆账本。
    scoped_effect_ledger: list[ScopedStatDelta] = field(default_factory=list)
    # 死斗对手自己的遗物。可选效果由对手决定是否发动，不跟挑战者的 state.relics 混用。
    opponent_relics: list[Relic] = field(default_factory=list)
    opponent_artifacts_owned: list[str] = field(default_factory=list)
    opponent_dragon_body_shield_rounds: int = 0
    opponent_dragon_tail_sacrifice_declared: str = ""
    consumables: list[Consumable] = field(default_factory=list)
    # 抵扣X封印的玩家遗物 {遗物名: 剩余回合}，[回终]-1，归零解封（封印期间不触发 process_relics）
    sealed_relics: dict = field(default_factory=dict)
    
    # 残韵
    resonance: dict[str, int] = field(default_factory=dict)  # {转换: 1, 反转: 2, ...}
    
    # 龙心
    dragon_hearts: list[LongJiXin] = field(default_factory=list)
    
    # 法器
    artifacts: list[dict] = field(default_factory=list)
    
    # 死者之书
    # 系统记录（如癌变强化）与玩家遗言分开保存，避免结构化遗言退化为日志字符串。
    # 遗言的事实源是 死者之书.md；death_book_legacies 只是启动/审核后从文件装回的缓存。
    death_book_wisdom: list[str] = field(default_factory=list)
    # 癌变怪物被吸收后对【休整】的永久恢复量加成；每只+8，跨战斗/轮回保留。
    rest_heal_bonus: int = 0
    death_book_legacies: list[dict[str, str]] = field(default_factory=list)
    death_book_capacity: int = 20  # 遗言每段字数上限
    death_inheritance_queued: bool = False
    pending_death_draft: dict[str, str] = field(default_factory=dict)
    last_death_cause: str = ""
    
    # 封存候选人（最终的冠冕）
    sealed_candidate: Optional[dict] = None
    # 死斗规则（2026-08-21）：通过死斗的角色进入"进阶封存"，封存按阶级分槽存放。
    # 每个阶级封存槽是一份先来后到的候选队列；该阶级的挑战者依次与队首死斗。
    # 当前正在进行的死斗所属阶级（1=一阶挑战者/胜者死斗，2=二阶…；0=非死斗）。
    sealed_candidates: dict = field(default_factory=dict)  # {阶级: [候选快照, ...]}
    duel_tier: int = 0
    # 当前死斗中被挑战的擂主原始快照（触发时从队列取出后暂存）：
    # 挑战者落败（擂主卫冕成功）时须按 README 550 规则放回队首重新封存。
    duel_defending_snapshot: dict = field(default_factory=dict)
    
    # 员工相关
    blacklist_level: int = 0     # 黑名单计数（每累计3名员工因拒付工资/解雇/死亡离队+1，≥3触发is_blacklisted）
    is_blacklisted: bool = False

    # 战终工资结算（出战支援）：{员工名: 应付工资}，非空时 battle_end 阻塞，需先逐个 pay_employee_wage 决策
    pending_wage_decisions: dict[str, int] = field(default_factory=dict)

    # 雇佣后"发现并选择一种转化道纹"：{员工名: [3个候选道纹名]}，未选择前该员工暂不持有转化道纹
    pending_daowen_choices: dict[str, list[str]] = field(default_factory=dict)

    # 事件登记的"下一场战斗额外出现的怪物"（如龙心谷"追求者·拿走口粮"），[战始]出怪时读取并额外加入
    forced_monsters_next_battle: list[dict] = field(default_factory=list)

    # 员工叛变：待处理标记（[战终]检查命中后置真，三个处理分支任一生效后清空）
    rebellion_active: bool = False
    # 员工叛变·镇压子战斗：进行中标记（employees已搬入enemies，需resolve_rebellion_battle结算）
    rebellion_in_progress: bool = False
    # 员工叛变·让利：每场工资在原公式基础上的固定加成（本次轮回持续生效）
    wage_bonus: int = 0

    # 最终的冠冕/第8场死斗：进行中标记 + 当前该谁出手("player_side"/"opponent_side")
    in_final_duel: bool = False
    duel_turn: str = ""
    # 四项先手属性完全相同时：首回合随机，之后每回合交换先手方。
    duel_tie_alternating: bool = False
    duel_round_first: str = ""
    duel_rounds_started: int = 0

    # 龙心谷"炼心"：待生效标记；下一次玩家实际支付数值型代价后转化为对应类型的【××龙心】消耗品
    pending_lianxin: bool = False
    pending_sha_qi_choices: list = field(default_factory=list)
    dead_monsters: list = field(default_factory=list)  # 乱葬岗招魂：本场已命零的怪物尸体
    # 战斗事件流（唯一事实源，只增不改）。CombatEngine.event_stream 是它的别名视图。
    # 只登记“发生了什么”，每条事件的 ctx 字段挂 EffectContext 快照回答“为什么发生”。
    # 纯观测用：任何战斗规则都不得依赖本列表做判定。
    combat_events: list = field(default_factory=list)
    pending_sha_qi_mode: str = ""
    # 炼心在战斗中发动时不消耗出手，改为"下次局外行动多消耗1点精力"，此处累计待结算的额外精力消耗
    pending_energy_penalty: int = 0

    # 熔谷终音"真龙之心"：龙性资源池。
    # 已解锁的龙族项目本身以【遗物】形式存放在 self.relics（tags含"龙族"），
    # 不再维护独立名单——dragon_traits 是对 relics 的只读视图，见下方 property。
    dragon_nature: int = 0
    # 震岳龙躯剩余持续回合数（0=未激活）；断尾求生已消耗的遗物记录见dragon_traits变化
    dragon_body_shield_rounds: int = 0
    # 断尾求生：预先声明"若本次伤害会使自身命零，移除该龙族遗物来抵消伤害"；为空=未声明保护
    dragon_tail_sacrifice_declared: str = ""

    # 终音法器（三选一/四选一后获得的具名法器，跨副本共享同一个列表）
    artifacts_owned: list[str] = field(default_factory=list)
    # 红头绳解锁的局外行动"献祭"
    has_sacrifice_action: bool = False
    # 罪业金库/教父左轮等法器自身状态
    godfather_revolver_uses: int = 0
    # 死斗胜利后待选择的终音法器所属副本（非空=等待choose_terminal_artifact）
    pending_terminal_region: str = ""
    # 共心环：本场战斗选定的可共享龙心类型（空=未选定/未持有共心环）
    shared_dragon_heart_type: str = ""
    # 负岳碑：预先声明保护的稳定实体引用（friend:index / employee:index）。
    fuyuebei_declared: list[str] = field(default_factory=list)

    # 初拥之夜：待选择标记 + 已选过的1~8号遗物(每项限一次，9号不计入) + 已获得的赤族
    pending_first_embrace: bool = False
    # 仅当初拥之夜是由死斗胜利(领取猩红尖牙)触发时为True：完成本次选择(非"封存血脉")后应紧接着完整封存
    seal_pending_after_embrace: bool = False
    # 初拥之夜所得同样以【遗物】形式存放在 self.relics（tags含"血族"）；
    # first_embrace_traits 是对 relics 的只读视图，见下方 property。
    chizu_names: list[str] = field(default_factory=list)
    # 真理眼冷却：剩余需要经过的战斗场数(战终-1，0=可用)
    truth_eye_cooldown: int = 0

    
    # 属性点
    attribute_points: int = 0
    allocated_blood: int = 0     # 已分配血限（从属性点）
    
    # ---- 遗物视图：血族/龙族项目一律以遗物形式存放，遗物是唯一事实源 ----
    # 这样它们自动继承遗物的全部通用规则（可被销毁、交换、封印、计入"一件当前遗物"）。

    def _relic_names_by_tag(self, tag: str) -> list[str]:
        return [r.name for r in self.relics if tag in r.tags]

    @property
    def dragon_traits(self) -> list[str]:
        """龙族遗物名单（对 relics 的只读视图）。"""
        return self._relic_names_by_tag("龙族")

    @property
    def first_embrace_traits(self) -> list[str]:
        """血族遗物名单（对 relics 的只读视图）。"""
        return self._relic_names_by_tag("血族")

    @property
    def opponent_dragon_traits(self) -> list[str]:
        """死斗对手的龙族遗物名单（对 opponent_relics 的只读视图）。"""
        return [r.name for r in self.opponent_relics if "龙族" in r.tags]

    @property
    def opponent_first_embrace_traits(self) -> list[str]:
        """死斗对手的血族遗物名单（对 opponent_relics 的只读视图）。"""
        return [r.name for r in self.opponent_relics if "血族" in r.tags]

    def apply_heal(
        self, entity: Entity, amount: int,
        ctx: Optional[EffectContext | dict] = None,
    ) -> dict:
        """统一回复入口；玩家侧溢出回复会被【龙血瓶】转为可提取耐久。

        ctx 为兼容层来源上下文；未传时保持原回复行为，并在返回明细中给出 warning。
        """
        heal_ctx = normalize_context(ctx)
        if heal_ctx is None:
            heal_ctx = make_context(
                timing=self.phase or "unknown", source="legacy_heal", source_type="legacy",
                target=entity, mechanic="heal", subtype="legacy", amount=amount,
                tags={"legacy_context"},
            )
            legacy = True
        else:
            legacy = False
            if heal_ctx.mechanic != "heal":
                heal_ctx = make_context(
                    timing=heal_ctx.timing, source=heal_ctx.source,
                    source_type=heal_ctx.source_type, actor=heal_ctx.actor,
                    target=heal_ctx.target or entity, owner=heal_ctx.owner,
                    mechanic="heal", subtype=heal_ctx.subtype, amount=heal_ctx.amount or amount,
                    tags=heal_ctx.tags, event_id=heal_ctx.event_id,
                    parent_event_id=heal_ctx.parent_event_id,
                )
        detail = entity.heal(amount)
        detail["heal_ctx"] = heal_ctx.to_dict()
        heal_events = getattr(entity, "_heal_events", None)
        if heal_events is None:
            entity._heal_events = []
            heal_events = entity._heal_events
        heal_events.append(detail["heal_ctx"])
        if legacy:
            detail["context_warning"] = "回复缺少EffectContext；已按legacy来源兼容记录"
        overheal = detail.get("overheal", 0)
        if overheal > 0 and self.on_player_side(entity):
            bottle = next((item for item in self.consumables if item.name == "龙血瓶"), None)
            if bottle is not None:
                bottle.current_uses += overheal
                bottle.max_uses += overheal
                detail["dragon_blood_bottle_stored"] = overheal
                detail["dragon_blood_bottle_ctx"] = make_context(
                    timing=heal_ctx.timing, source="龙血瓶", source_type="consumable",
                    actor=entity, target=entity, owner=entity,
                    mechanic="heal_storage", subtype="overheal", amount=overheal,
                    tags={"overheal", "storage"}, parent_event_id=heal_ctx.event_id,
                ).to_dict()
        self.emit_combat_event(
            CombatEventType.HEAL_APPLIED,
            actor=heal_ctx.actor, target=entity, ctx=detail["heal_ctx"],
            heal_amount=amount,
            actual_heal=detail.get("actual_heal", 0),
            overheal=detail.get("overheal", 0),
            hp_before=detail.get("hp_before"),
            hp_after=detail.get("hp_after"),
        )
        return detail

    def emit_combat_event(
        self, event_type: CombatEventType, *,
        actor: Any = None, target: Any = None,
        ctx: Optional[dict] = None, **data: Any,
    ) -> CombatEvent:
        """向战斗事件流登记一条“发生了什么”。纯观测，不参与任何规则判定。"""
        def _name(ref: Any) -> str:
            # ref 可能是实体，也可能已经是 EffectContext.to_dict() 降级后的名字字符串
            # （例如 Hook 用 normalize_context(detail["ctx"]) 还原出来的上下文）。
            if ref is None:
                return ""
            if isinstance(ref, str):
                return ref
            return getattr(ref, "name", "")

        event = CombatEvent(
            event_type=event_type,
            battle_no=self.current_battle,
            round_no=self.current_round,
            actor_name=_name(actor),
            target_name=_name(target),
            data=data,
            ctx=ctx,
        )
        self.combat_events.append(event)
        # 通用事件分发观察者（CombatEngine 在战斗实例构造时注册）：
        # 所有事件类型经此进入机制系统；无观察者（局外/无战斗上下文）时零行为变化。
        observer = get_combat_event_observer(self)
        if observer is not None:
            observer(event, raw_actor=actor, raw_target=target)
        return event

    def on_player_side(self, entity: Entity) -> bool:
        """必须用 is，不能用 dataclass 相等。"""
        if entity is None:
            return False
        if self.player is not None and entity is self.player:
            return True
        for e in self.friends:
            if e is entity:
                return True
        for e in self.employees:
            if e is entity:
                return True
        for e in self.temp_friends:
            if e is entity:
                return True
        return False

    def on_enemy_side(self, entity: Entity) -> bool:
        """必须用 is，不能用 dataclass 相等。"""
        if entity is None:
            return False
        for e in self.enemies:
            if e is entity:
                return True
        return False

    def side_has(self, entity: Entity, name: str) -> bool:
        """该实体所属轮回者是否持有该终音/初拥/龙族项目。朋友/员工不继承。"""
        if entity is None:
            return False
        if entity is self.player:
            return (name in self.dragon_traits
                    or name in self.first_embrace_traits
                    or name in self.artifacts_owned
                    or any(r.name == name for r in self.relics))
        if entity.entity_type == "轮回者" and self.on_enemy_side(entity):
            return (name in self.opponent_dragon_traits
                    or name in self.opponent_first_embrace_traits
                    or name in self.opponent_artifacts_owned
                    or any(r.name == name for r in self.opponent_relics))
        return False

    def side_body_shield(self, entity: Entity) -> int:
        if entity is self.player:
            return self.dragon_body_shield_rounds
        if entity is not None and entity.entity_type == "轮回者" and self.on_enemy_side(entity):
            return self.opponent_dragon_body_shield_rounds
        return 0

    def side_tail_declared(self, entity: Entity) -> str:
        if entity is self.player:
            return self.dragon_tail_sacrifice_declared
        if entity is not None and entity.entity_type == "轮回者" and self.on_enemy_side(entity):
            return self.opponent_dragon_tail_sacrifice_declared
        return ""

    def clear_side_tail_declared(self, entity: Entity) -> None:
        if entity is self.player:
            self.dragon_tail_sacrifice_declared = ""
        elif entity is not None and entity.entity_type == "轮回者" and self.on_enemy_side(entity):
            self.opponent_dragon_tail_sacrifice_declared = ""

    def remove_side_relic(self, entity: Entity, name: str) -> bool:
        if entity is self.player:
            return self.remove_relic(name)
        if entity is not None and entity.entity_type == "轮回者" and self.on_enemy_side(entity):
            for i, r in enumerate(self.opponent_relics):
                if r.name == name:
                    del self.opponent_relics[i]
                    return True
        return False

    def entity_ref(self, entity: Entity) -> str:
        if entity is self.player:
            return "player:0"
        for prefix, entities in (
            ("friend", self.friends), ("employee", self.employees),
            ("temp_friend", self.temp_friends), ("enemy", self.enemies),
        ):
            for index, candidate in enumerate(entities):
                if candidate is entity:
                    return f"{prefix}:{index}"
        raise ValueError(f"实体{entity.name}不在当前GameState中")
    def entity_by_runtime_id(self, runtime_id: str) -> Optional[Entity]:
        entities = (([self.player] if self.player else []) + self.friends + self.employees
                    + self.temp_friends + self.enemies)
        return next((entity for entity in entities if entity.runtime_id == runtime_id), None)

    def apply_scoped_delta(
        self,
        entity: Entity,
        field_name: str,
        delta: int,
        *,
        scope: str = EffectScope.BATTLE.value,
        polarity: str = EffectPolarity.NEUTRAL.value,
        source: str,
    ) -> int:
        """应用并登记一笔可逆面板变化；仅 round/battle 作用域允许进入账本。"""
        if scope not in (EffectScope.ROUND.value, EffectScope.BATTLE.value):
            raise ValueError("可逆作用域账本只接受round或battle")
        if field_name not in {"blood_limit", "mana_limit", "speed_limit", "attack_count", "attack_power"}:
            raise ValueError(f"字段{field_name}不允许进入作用域账本")
        if not isinstance(delta, int) or isinstance(delta, bool):
            raise ValueError("作用域delta必须是整数")
        before = getattr(entity, field_name)
        after = before + delta
        if field_name in {"blood_limit", "mana_limit", "speed_limit", "attack_count", "attack_power"}:
            after = max(0, after)
        actual_delta = after - before
        setattr(entity, field_name, after)
        if actual_delta:
            self.scoped_effect_ledger.append(ScopedStatDelta(
                entity_id=entity.runtime_id,
                entity_name=entity.name,
                field_name=field_name,
                delta=actual_delta,
                scope=scope,
                polarity=polarity,
                source=source,
            ))
        return actual_delta

    def rollback_scoped_effects(self, scope: str) -> list[dict]:
        """逆序回滚指定作用域，保留代价/伤害/资源与其他作用域。"""
        rolled_back: list[dict] = []
        remaining: list[ScopedStatDelta] = []
        for entry in reversed(self.scoped_effect_ledger):
            if entry.scope != scope:
                remaining.append(entry)
                continue
            entity = self.entity_by_runtime_id(entry.entity_id)
            if entity is None:
                remaining.append(entry)
                continue
            current = getattr(entity, entry.field_name)
            setattr(entity, entry.field_name, max(0, current - entry.delta))
            if entry.field_name in ("blood_limit", "mana_limit", "speed_limit"):
                current_field = {
                    "blood_limit": "current_hp",
                    "mana_limit": "current_mana",
                    "speed_limit": "current_speed",
                }[entry.field_name]
                setattr(entity, current_field,
                        min(getattr(entity, current_field), getattr(entity, entry.field_name)))
            rolled_back.append(entry.to_dict())
        self.scoped_effect_ledger = list(reversed(remaining))
        return rolled_back

    def rollback_scoped_sources(self, entity: Entity, sources: set[str]) -> list[dict]:
        """持续效果到期时回滚同一实体、同一来源的局内面板变化。"""
        rolled_back: list[dict] = []
        remaining: list[ScopedStatDelta] = []
        entity_id = entity.runtime_id
        for entry in reversed(self.scoped_effect_ledger):
            if entry.entity_id != entity_id or entry.source not in sources:
                remaining.append(entry)
                continue
            current = getattr(entity, entry.field_name)
            setattr(entity, entry.field_name, max(0, current - entry.delta))
            if entry.field_name in ("blood_limit", "mana_limit", "speed_limit"):
                current_field = {
                    "blood_limit": "current_hp",
                    "mana_limit": "current_mana",
                    "speed_limit": "current_speed",
                }[entry.field_name]
                setattr(entity, current_field,
                        min(getattr(entity, current_field), getattr(entity, entry.field_name)))
            rolled_back.append(entry.to_dict())
        self.scoped_effect_ledger = list(reversed(remaining))
        return rolled_back

    def grant_relic(self, name: str, effect: str, tag: str = "") -> Relic:
        """授予一件遗物；tag 用于标记 血族/龙族 等来源。"""
        r = Relic(name=name, effect=effect, tags=[tag] if tag else [])
        self.relics.append(r)
        return r

    def remove_relic(self, name: str) -> bool:
        """销毁/移除一件遗物（断尾求生、熔掉遗物等）。"""
        for i, r in enumerate(self.relics):
            if r.name == name:
                del self.relics[i]
                return True
        return False

    def to_dict(self) -> dict:
        """导出完整状态（供AI读取）"""
        return {
            "game_id": self.game_id,
            "phase": self.phase,
            "combat_subphase": self.combat_subphase,
            "current_round": self.current_round,
            "current_battle": self.current_battle,
            "current_region": self.current_region,
            "energy": self.energy,
            "shards": self.shards,
            "fake_shards": self.fake_shards,
            "blacklist_level": self.blacklist_level,
            "is_blacklisted": self.is_blacklisted,
            "pending_wage_decisions": self.pending_wage_decisions,
            "pending_daowen_choices": self.pending_daowen_choices,
            "pending_relic_choices": list(self.pending_relic_choices),
            "pending_relic_source": self.pending_relic_source,
            "pending_initial_daowen_choices": list(self.pending_initial_daowen_choices),
            "pending_initial_daowen_source": self.pending_initial_daowen_source,
            "pending_redemption": dict(self.pending_redemption),
            "pending_item_choices": list(self.pending_item_choices),
            "pending_item_source": self.pending_item_source,
            "pending_monster_phase": self.pending_monster_phase,
            "pending_attack": self.pending_attack,
            "pending_event_queue": list(self.pending_event_queue),
            "event_modifiers": self.event_modifiers,
            "scoped_effect_ledger": [entry.to_dict() for entry in self.scoped_effect_ledger],
            "forced_monsters_next_battle": self.forced_monsters_next_battle,
            "rebellion_active": self.rebellion_active,
            "rebellion_in_progress": self.rebellion_in_progress,
            "wage_bonus": self.wage_bonus,
            "in_final_duel": self.in_final_duel,
            "duel_turn": self.duel_turn,
            "duel_tie_alternating": self.duel_tie_alternating,
            "duel_round_first": self.duel_round_first,
            "sealed_candidates": self.sealed_candidates,
            "duel_tier": self.duel_tier,
            "attribute_points": self.attribute_points,
            "player": self.player.to_dict() if self.player else None,
            "friends": [f.to_dict() for f in self.friends],
            "employees": [e.to_dict() for e in self.employees],
            "temp_friends": [t.to_dict() for t in self.temp_friends],
            "enemies": [e.to_dict() for e in self.enemies],
            "relics": [r.to_dict() for r in self.relics],
            "consumables": [c.to_dict() for c in self.consumables],
            "resonance": self.resonance,
            "dragon_hearts": [d.to_dict() for d in self.dragon_hearts],
            "artifacts": self.artifacts,
            "death_book_wisdom": self.death_book_wisdom,
            "rest_heal_bonus": self.rest_heal_bonus,
            "death_book_legacies": self.death_book_legacies,
            "sealed_candidate": self.sealed_candidate,
            # 角色性格特征（只导出仍被追踪的存活角色，见 engine/personality.py）
            "personality_traits": personality_export_for_ai(self),
        }
    
    def get_all_player_side(self) -> list[Entity]:
        """获取己方所有实体（[员工]需 is_deployed=True 才计入战场；已【撤退】者不再计入本场战斗）"""
        entities = []
        if self.player and self.player.is_alive:
            entities.append(self.player)
        entities.extend(f for f in self.friends if f.is_alive and not f.has_retreated)
        entities.extend(e for e in self.employees if e.is_alive and e.is_deployed and not e.has_retreated)
        entities.extend(t for t in self.temp_friends if t.is_alive and not t.has_retreated)
        return entities
    
    def get_all_enemy_side(self) -> list[Entity]:
        """获取敌方所有存活实体"""
        return [e for e in self.enemies if e.is_alive]

    # ==================== 战斗结束与胜利·统一判定（DM裁定 2026-08-18） ====================
    # 引擎内一切"战斗是否结束/能否战终"的判断必须走以下四个方法，禁止再散落写 is_alive 组合。

    def enemy_combat_active(self, enemy: Entity) -> bool:
        """该敌人是否仍构成战斗障碍（阻塞战终）。

        DM裁定（2026-08-18）：战斗胜利＝敌方全部角色【命零】或【离场】。
        一切特殊事件（雕塑/癌变/还债/救赎/封印/逃跑及未来新增）一律经
        Entity.depart_battle() 记为离场——新增事件无须改动本判定。
        离场不视为击杀、不产碎片（分类见 battle_end 读取 departure_reason）。
        """
        return enemy.is_alive and not enemy.is_departed

    def active_enemies(self) -> list[Entity]:
        """仍构成战斗障碍的敌人列表。"""
        return [e for e in self.enemies if self.enemy_combat_active(e)]

    def battle_won(self) -> bool:
        """战斗胜利＝敌方全部角色均已经由任一合法路径移出战场。"""
        return not self.active_enemies()

    def battle_lost(self) -> bool:
        """战斗失败＝轮回者非存活（无论死因：伤害/凡庸/癌变/崩解/代价）。"""
        return self.player is None or not self.player.is_alive

    def battle_over(self) -> bool:
        """战斗胜负已定（胜或负任一成立）。"""
        return self.battle_lost() or self.battle_won()
