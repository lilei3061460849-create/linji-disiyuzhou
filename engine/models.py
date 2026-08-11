"""
核心数据模型
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Any
import math
import json


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
            "panel": self.panel,
        }


@dataclass
class StatusEffect:
    """持续效果"""
    name: str
    remaining_rounds: int        # 剩余回合（-1=∞）
    value: int = 0               # 效果数值
    source: str = ""             # 来源
    
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
    
    # 状态
    shield: int = 0              # 格挡
    status_effects: list[StatusEffect] = field(default_factory=list)
    is_flying: bool = False      # 飞行状态

    # 非击杀移出战斗标记（封印等；不产碎片）
    removed_without_kill: bool = False
    hp_lost_this_round: int = 0   # 本回合累计失去的生命（活血用，回始归零）
    actions_used_this_round: int = 0  # 本回合已消耗的出手次数（回始归零，用于出手预算校验）

    # 多路径胜利追踪
    shards: int = 0              # 怪物自带碎片（罪孽都市）/ 负值表示负债（还债）
    total_healed: int = 0        # 累计受到的恢复量（增生；超出血限部分按双倍计）
    is_sculptured: bool = False  # 已化为雕塑（攻击次数或攻击力归0）
    is_proliferated: bool = False  # 已被增生吸收进死者之书
    is_debt_bound: bool = False  # 已因还债成为员工

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
    # 钱袋：[战始]时的血限快照，用于按"[战始][血限]×2%"结算额外碎片（增殖等战斗中改变血限不影响此值）
    battle_start_blood_limit: int = 0

    # 寒冰法力（初拥之夜遗物）：本回合内，持有该遗物者对我方发动道纹累计消耗的法力（含对自己发动）
    # 回始归零；每满10点使当前回合出手次数-1（以叠加"无力"状态实现）
    mana_inflicted_this_round: int = 0

    # 血族血脉（初拥之夜遗物）：本回合是否已造成过伤害（[回终]判定：造成过则回复等量，否则流血20）
    damage_dealt_this_round: int = 0
    # 赤族诅咒标记：entity_type=="赤族"的实体[回终]固定流血20；is_chizu_of记录其主人名字(仅用于血食校验)
    is_chizu_of: str = ""

    def __post_init__(self):
        if self.battle_start_blood_limit == 0:
            self.battle_start_blood_limit = self.blood_limit

    
    @property
    def action_count(self) -> int:
        """出手次数：轮回者=速限/3向上取整；[朋友]/[员工](微光者，面板无速限)=攻击次数/3向上取整；
        怪物走独立的"固定1次攻击+1次道纹"规则(见battle_flow.get_monster_actions)，不使用本属性。
        活力+X、无力-X 对两种口径均生效。"""
        if self.entity_type in ("朋友", "员工"):
            base = math.ceil(self.attack_count / 3) if self.attack_count > 0 else 0
        else:
            base = math.ceil(self.speed_limit / 3) if self.speed_limit > 0 else 0
        base += self.get_status_value("活力")
        base -= self.get_status_value("无力")
        return max(0, base)
    
    @property
    def is_full_hp(self) -> bool:
        return self.current_hp >= self.blood_limit
    
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
        
        # 格挡抵消（代价类型的伤害不被格挡抵消）
        if self.shield > 0 and damage_type != "代价":
            absorbed = min(self.shield, remaining)
            self.shield -= absorbed
            remaining -= absorbed
            detail["shield_absorbed"] = absorbed
        
        # 扣除生命
        self.current_hp = max(0, self.current_hp - remaining)
        detail["actual_damage"] = remaining
        detail["hp_after"] = self.current_hp
        self.hp_lost_this_round += remaining  # 活血追踪
        
        if self.current_hp <= 0:
            detail["died"] = True
            self.is_alive = False
        
        return detail
    
    MUTATION_COLLAPSE_THRESHOLD = 50  # 特殊事件【崩解】阈值：异变达到50层直接命零（阈值定稿；计费粒度=持续型每回始5X后存在真实牙齿）

    def add_mutation(self, layers: int) -> dict:
        """
        累加异变层数。
        特殊事件【崩解】：任一角色异变达到阈值（当前50层）时直接[命零]死亡；
        正在结算的效果是否中断由调用方判定（与中断规则同精神：代价先付）。
        """
        if layers > 0:
            self.mutation_count += layers
        collapsed = self.mutation_count >= self.MUTATION_COLLAPSE_THRESHOLD
        if collapsed and self.is_alive:
            self.current_hp = 0
            self.is_alive = False
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
        # 增生追踪：超出血限的恢复按双倍计入累计恢复量
        self.total_healed += actual + overheal * 2
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
        """消耗法力"""
        if self.current_mana < amount:
            return False
        self.current_mana -= amount
        return True
    
    def spend_speed(self, amount: int) -> bool:
        """消耗当前速度"""
        if self.current_speed < amount:
            return False
        self.current_speed -= amount
        return True
    
    def get_status_effects(self, name: str) -> list[StatusEffect]:
        return [s for s in self.status_effects if s.name == name]
    
    def has_status(self, name: str) -> bool:
        return any(s.name == name and not s.is_expired for s in self.status_effects)
    
    def get_status_value(self, name: str) -> int:
        return sum(s.value for s in self.status_effects if s.name == name and not s.is_expired)
    
    def tick_status_effects(self) -> list[str]:
        """回合递减，返回已过期的效果名"""
        expired = []
        remaining = []
        for s in self.status_effects:
            if not s.tick():
                expired.append(s.name)
            else:
                remaining.append(s)
        self.status_effects = remaining
        return expired
    
    def add_status(self, effect: StatusEffect):
        """添加状态效果，同名合并"""
        for existing in self.status_effects:
            if existing.merge_with(effect):
                return
        self.status_effects.append(effect)
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "entity_type": self.entity_type,
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
            "is_deployed": self.is_deployed,
            "has_retreated": self.has_retreated,
            "battle_start_blood_limit": self.battle_start_blood_limit,
            "deployed_at_round": self.deployed_at_round,
            "shards": self.shards,
            "total_healed": self.total_healed,
            "hp_ratio": round(self.hp_ratio, 2),
            "dao_wen": {k: v.dao_wen.name for k, v in self.dao_wen.items()},
            "spells": [s.name for s in self.spells],
            "status_effects": [
                {"name": s.name, "value": s.value, "rounds": s.remaining_rounds}
                for s in self.status_effects
            ]
        }


@dataclass
class GameState:
    """完整游戏状态"""
    # 游戏元数据
    game_id: str = ""
    phase: str = "setup"
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
    
    # 资源
    shards: int = 20            # 碎片
    
    # 遗物与消耗品
    relics: list[Relic] = field(default_factory=list)
    relics_pool: list[Relic] = field(default_factory=list)  # 遗物池（未获取的）
    consumables: list[Consumable] = field(default_factory=list)
    
    # 残韵
    resonance: dict[str, int] = field(default_factory=dict)  # {转换: 1, 反转: 2, ...}
    
    # 龙心
    dragon_hearts: list[LongJiXin] = field(default_factory=list)
    
    # 法器
    artifacts: list[dict] = field(default_factory=list)
    
    # 死者之书
    death_book_wisdom: list[str] = field(default_factory=list)  # 遗言
    death_book_capacity: int = 20  # 遗言字数上限
    
    # 封存候选人（最终的冠冕）
    sealed_candidate: Optional[dict] = None
    
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

    # 龙心谷"炼心"：待生效标记；下一次玩家实际支付数值型代价后转化为对应类型的【××龙心】消耗品
    pending_lianxin: bool = False
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
    # 负岳碑(终音法器)：预先声明"下一次这些[朋友]/[员工]即将撤退时，改为流血20取消撤退"的名单
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
    
    # 法器/遗物记录
    relic_of_choice: Optional[str] = None  # 当前选择的遗物
    
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
            "current_round": self.current_round,
            "current_battle": self.current_battle,
            "current_region": self.current_region,
            "energy": self.energy,
            "shards": self.shards,
            "blacklist_level": self.blacklist_level,
            "is_blacklisted": self.is_blacklisted,
            "pending_wage_decisions": self.pending_wage_decisions,
            "pending_daowen_choices": self.pending_daowen_choices,
            "forced_monsters_next_battle": self.forced_monsters_next_battle,
            "rebellion_active": self.rebellion_active,
            "rebellion_in_progress": self.rebellion_in_progress,
            "wage_bonus": self.wage_bonus,
            "in_final_duel": self.in_final_duel,
            "duel_turn": self.duel_turn,
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
            "sealed_candidate": self.sealed_candidate,
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
