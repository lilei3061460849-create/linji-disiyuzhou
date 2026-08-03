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
    # 降服品专用：记录被降服怪物的当前面板，使用后作为临时朋友作战
    panel: Optional[dict] = None
    is_taming: bool = False
    
    @property
    def is_depleted(self) -> bool:
        return self.current_uses <= 0
    
    def use(self) -> int:
        if self.is_depleted:
            return 0
        self.current_uses -= 1
        return self.current_uses
    
    def merge(self, other: 'Consumable') -> bool:
        """合并相同消耗品"""
        if self.name != other.name or self.effect != other.effect:
            return False
        # 降服品记录的是特定怪物面板，不可与普通消耗品合并
        if self.is_taming or other.is_taming:
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
            "is_taming": self.is_taming,
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

    # 降服追踪：连续未能对轮回者造成伤害的回合数
    no_damage_streak: int = 0
    is_subdued: bool = False     # 是否已被降服（移出战斗）

    # 存活
    is_alive: bool = True
    
    @property
    def action_count(self) -> int:
        """出手次数 = 速限 / 3，向上取整"""
        return math.ceil(self.speed_limit / 3) if self.speed_limit > 0 else 0
    
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
        
        if self.current_hp <= 0:
            detail["died"] = True
            self.is_alive = False
        
        return detail
    
    def heal(self, amount: int) -> dict:
        """回复生命"""
        before = self.current_hp
        self.current_hp = min(self.blood_limit, self.current_hp + amount)
        actual = self.current_hp - before
        return {
            "heal_amount": amount,
            "actual_heal": actual,
            "hp_before": before,
            "hp_after": self.current_hp,
            "overheal": amount - actual
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
            "no_damage_streak": self.no_damage_streak,
            "is_subdued": self.is_subdued,
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
    blacklist_level: int = 0     # 黑名单计数（每累计3名员工离队加入黑名单）
    is_blacklisted: bool = False
    
    # 异变计数
    mutation_count: int = 0
    
    # 属性点
    attribute_points: int = 0
    allocated_blood: int = 0     # 已分配血限（从属性点）
    
    # 法器/遗物记录
    relic_of_choice: Optional[str] = None  # 当前选择的遗物
    
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
            "mutation_count": self.mutation_count,
            "blacklist_level": self.blacklist_level,
            "is_blacklisted": self.is_blacklisted,
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
        """获取己方所有实体"""
        entities = []
        if self.player and self.player.is_alive:
            entities.append(self.player)
        entities.extend(f for f in self.friends if f.is_alive)
        entities.extend(e for e in self.employees if e.is_alive)
        entities.extend(t for t in self.temp_friends if t.is_alive)
        return entities
    
    def get_all_enemy_side(self) -> list[Entity]:
        """获取敌方所有存活实体"""
        return [e for e in self.enemies if e.is_alive]
