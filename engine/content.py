"""
内容库：遗物 / 消耗品 / 怪物池 / 通用事件

本模块是 README 正文内容的结构化镜像。
规则依据：「正文文件是唯一事实源」——本文件所有数值均逐条抄录自 README.md，
任何改动必须先改正文，再同步此处。

效果文本保持与正文逐字一致；带有可程序化结算的部分通过 tags 标注，
无法程序化的部分由 AI/DM 依据 effect 文本裁定。
"""
from __future__ import annotations
from dataclasses import dataclass, field
import math

from .models import Entity, Relic, Consumable, DaoWenInstance
from .daowen import DaoWenEngine
from .enums import EntityType


# ==================== 遗物池 ====================
# 获取机制：凡是获取遗物（初始配置、共鸣行动及事件），均采用"发现"机制。
# 事件遗物不加入遗物池。

RELIC_POOL: list[dict] = [
    {"name": "血誓戒",
     "effect": "［回始］首次主动支付流血代价时，获得等同于本次流血的格挡；若支付后生命≤30%，改为获得等量生命",
     "tags": ["回始", "流血联动"]},
    {"name": "买路财",
     "effect": "战斗中可以失去等同于怪物20%[血限]的[碎片]安全撤退；碎片不足时，可以其他代价补足（1[碎片]=2生命=1[血限]）。",
     "tags": ["撤退", "碎片"]},
    {"name": "同魂笔",
     "effect": "当你对[目标]发动残韵时，可以选择另一个[目标]，使其拥有的一种道纹受到同种残韵影响。",
     "tags": ["残韵"]},
    {"name": "回锋刀",
     "effect": "每失去1点速度后，对[目标]造成3点伤害；[回始]，对[目标]造成3×（你的[速限]-你的当前速度）的伤害。",
     "tags": ["回始", "速度联动"]},
    {"name": "折速法印",
     "effect": "[战始]可以疲惫X，获得6X点法力。",
     "tags": ["战始", "疲惫"]},
    {"name": "三相残韵盘",
     "effect": "[战始]，可以消耗自身拥有的一种残韵；[战终]获得另外两种残韵各1个。",
     "tags": ["战始", "战终", "残韵"]},
    {"name": "鲜血契约",
     "effect": "[战始]，可以流血X，使首回合法力+X（X≤自身20%[血限]）",
     "tags": ["战始", "流血"]},
    {"name": "避风铃",
     "effect": "每次闪避后获得3点格挡，当前速度归零时，获得15点格挡",
     "tags": ["闪避", "格挡"]},
    {"name": "守夜灯",
     "effect": "[敌回始]，获得等同于[法限]50%的法力，该法力[敌回终]清空，每回合一次",
     "tags": ["敌回始", "法力"]},
    {"name": "钱袋",
     "effect": "每当敌方[目标][命零]，额外获得等同于其[战始][血限]2%的[碎片]。",
     "tags": ["命零", "碎片"]},
    {"name": "卖身契",
     "effect": "[战始]，可以指定一名[朋友]或[员工]；本场你支付的【代价】改由其承担，其[命零]后本效果失效。",
     "tags": ["战始", "代价转移"]},
    {"name": "无所求",
     "effect": '每当你在事件中选择"拒绝"类选项，永久获得1点属性点。',
     "tags": ["事件", "属性点"]},
    {"name": "忘忧香",
     "effect": "局外行动你可以选择\u201c忘忧\u201d（失忆1/2/3，获得30/55/80［碎片］）",
     "tags": ["局外", "失忆", "碎片"]},
]


def build_relic_pool() -> list[Relic]:
    """构造完整遗物池对象列表"""
    return [Relic(name=r["name"], effect=r["effect"], tags=list(r["tags"])) for r in RELIC_POOL]


def get_relic(name: str) -> Relic | None:
    for r in RELIC_POOL:
        if r["name"] == name:
            return Relic(name=r["name"], effect=r["effect"], tags=list(r["tags"]))
    return None


# ==================== 怪物池 ====================

@dataclass
class MonsterDef:
    """
    怪物面板定义
    面板格式：名称（攻击次数×攻击力/[血限]，道纹）
    """
    name: str
    attack_count: int
    attack_power: int
    blood_limit: int
    daowen: dict[str, int] = field(default_factory=dict)

    def to_entity(self, suffix: str = "") -> Entity:
        """
        实例化为战场实体。

        白板开局规则：怪物在[战始]的第一回合，仅具备基础攻击次数、攻击力和生命值。
        道纹须在其出手轮主动发动后方可生效，因此此处只登记道纹与其X值，
        不预先施加任何状态效果。
        """
        e = Entity(
            name=self.name + suffix,
            entity_type=EntityType.MONSTER.value,
            blood_limit=self.blood_limit,
            current_hp=self.blood_limit,
            mana_limit=0,      # 怪物不持有法力，也没有[法限]
            current_mana=0,
            speed_limit=0,
            current_speed=0,
            attack_count=self.attack_count,
            attack_power=self.attack_power,
        )
        for dw_name, x in self.daowen.items():
            try:
                inst = DaoWenInstance(dao_wen=DaoWenEngine.get_definition(dw_name))
            except ValueError:
                continue
            inst.x_value = x
            e.dao_wen[dw_name] = inst
        return e

    def panel(self) -> str:
        dw = "，".join(f"{k}{v}" for k, v in self.daowen.items())
        return f"{self.name}（{self.attack_count}×{self.attack_power}/{self.blood_limit}，{dw}）"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "attack_count": self.attack_count,
            "attack_power": self.attack_power,
            "blood_limit": self.blood_limit,
            "daowen": dict(self.daowen),
            "panel": self.panel(),
        }


# 各副本怪物池（每池12种，无重复组合）
MONSTER_POOLS: dict[str, list[MonsterDef]] = {
    "扭曲都市": [
        MonsterDef("千手蜈蚣", 6, 8, 120, {"畸变": 2, "狂暴": 2, "活力": 4}),
        MonsterDef("骨天使", 4, 12, 120, {"变形": 2, "活力": 3, "飞行": 3}),
        MonsterDef("肠水母", 3, 18, 108, {"僵化": 2, "庇护": 4, "自愈": 2}),
        MonsterDef("奇美拉", 2, 16, 120, {"变形": 1, "强化": 5, "飞行": 2}),
        MonsterDef("眼树", 1, 26, 96, {"定型": 2, "必中": 4, "再生": 2}),
        MonsterDef("缝合鱼", 3, 10, 132, {"退化": 4, "狂暴": 2, "衰败": 2}),
        MonsterDef("人头气球", 1, 22, 108, {"僵化": 2, "飞行": 3, "必中": 3}),
        MonsterDef("脑蜘蛛", 3, 14, 120, {"坏死": 1, "强化": 2, "减速": 5}),
        MonsterDef("血肉巨囊", 2, 20, 150, {"爆裂": 3, "增殖": 4, "庇护": 1}),
        MonsterDef("爬行者", 4, 10, 108, {"超频": 3, "急速": 2, "狂暴": 3}),
        MonsterDef("孢子母体", 1, 18, 132, {"坏死": 2, "衰败": 3, "寄生": 3}),
        MonsterDef("畸变行者", 3, 14, 120, {"爆裂": 3, "冲击": 3, "必中": 2}),
    ],
    "罪孽都市": [
        MonsterDef("毒枭", 2, 10, 108, {"洗劫": 3, "贯穿": 3, "借力": 2}),
        MonsterDef("窃贼", 2, 12, 108, {"洗劫": 3, "狂暴": 2, "必中": 3}),
        MonsterDef("打手", 4, 6, 120, {"逼债": 2, "狂暴": 3, "强化": 3}),
        MonsterDef("看门犬", 3, 6, 90, {"逼债": 2, "狂暴": 3, "血债": 3}),
        MonsterDef("狂徒", 3, 8, 120, {"抵扣": 2, "狂暴": 3, "固执": 3}),
        MonsterDef("打手头目", 5, 8, 132, {"抵扣": 3, "强化": 3, "自愈": 2}),
        MonsterDef("军火商", 2, 14, 108, {"清算": 3, "愤怒": 2, "兴奋": 3}),
        MonsterDef("狙击手", 1, 28, 96, {"清算": 2, "必中": 4, "减速": 2}),
        MonsterDef("杀手", 1, 20, 96, {"赎金": 3, "必中": 3, "减速": 2}),
        MonsterDef("暗哨", 1, 16, 108, {"赎金": 2, "缓慢": 3, "蒙蔽": 3}),
        MonsterDef("赌鬼", 2, 12, 120, {"假钞": 3, "赌命": 2, "借力": 3}),
        MonsterDef("通缉犯", 3, 12, 120, {"假钞": 2, "消灾": 3, "狂暴": 3}),
    ],
    "龙心谷": [
        MonsterDef("熔岩蜥", 3, 10, 114, {"加害": 2, "狂暴": 3, "冲击": 3}),
        MonsterDef("石背熊", 2, 13, 138, {"龙鳞": 3, "庇护": 2, "固执": 3}),
        MonsterDef("断角羊", 4, 7, 108, {"逆鳞": 2, "活力": 3, "狂暴": 3}),
        MonsterDef("余烬侍者", 2, 15, 120, {"活血": 3, "衰败": 2, "必中": 3}),
        MonsterDef("碎岩鸮", 1, 20, 102, {"裂变": 2, "飞行": 3, "减速": 3}),
        MonsterDef("墓门卫", 3, 10, 132, {"嫁祸": 2, "强化": 3, "庇护": 3}),
        MonsterDef("背碑人", 2, 14, 144, {"背负": 3, "固执": 2, "自愈": 3}),
        MonsterDef("烙痕祭司", 1, 22, 114, {"伤痕": 2, "必中": 3, "蒙蔽": 3}),
        MonsterDef("火山猿", 5, 8, 126, {"加害": 3, "狂暴": 3, "血债": 2}),
        MonsterDef("灰甲骑士", 2, 16, 126, {"龙鳞": 2, "逆鳞": 3, "贯穿": 3}),
        MonsterDef("熔洞蛛", 3, 12, 120, {"活血": 2, "裂变": 3, "急速": 3}),
        MonsterDef("断翼巨像", 2, 20, 150, {"嫁祸": 2, "背负": 2, "伤痕": 4}),
    ],
}


def get_monster_pool(region: str) -> list[MonsterDef]:
    return MONSTER_POOLS.get(region, [])


def get_monster(region: str, name: str) -> MonsterDef | None:
    for m in MONSTER_POOLS.get(region, []):
        if m.name == name:
            return m
    return None


def monster_count_for_battle(battle_number: int, region_tier: int = 1) -> int:
    """
    出怪数量 = 战斗场数，一阶副本直接-2，最低为1。
    允许重复抽选同一怪物种族。
    """
    count = battle_number
    if region_tier == 1:
        count -= 2
    return max(1, count)


# ==================== 通用事件池 ====================
# 当前事件池由所有未遇到的通用事件，以及当前区域中符合条件且未遇到的专属事件
# 共同组成；通用事件排列在前，专属事件排列在后。

GENERAL_EVENTS: list[dict] = [
    {
        "name": "无名冢",
        "description": "一片插满残破兵器的荒地，每一把兵器下都埋葬着一个失败的轮回者。风吹过，兵器发出呜咽。",
        "options": [
            {"id": 1, "text": "拔出兵器", "effect": "失去10[碎片]，随机获得一件遗物",
             "costs": {"shards": 10}, "requires_random": True},
            {"id": 2, "text": "为你而战", "effect": "销毁一件当前遗物（包括遗物池中），设计一种新的遗物加入遗物池，获得15[碎片]。",
             "gains": {"shards": 15}, "requires_dm": True},
            {"id": 3, "text": "拒绝", "effect": "无事发生。", "is_refusal": True},
        ],
    },
    {
        "name": "遗忘书屋",
        "description": "在一处坍塌的街角，一间散发着霉味与干涸血迹的旧书店静静伫立。",
        "options": [
            {"id": 1, "text": "阅读《战争残卷》", "effect": "流血15，选择学会两种法术。",
             "costs": {"bleed": 15}, "gains": {"spells": 2}},
            {"id": 2, "text": "阅读《禁忌法典》", "effect": "失忆1，自选一件遗物与20[碎片]。",
             "costs": {"amnesia": 1}, "gains": {"relic_choice": 1, "shards": 20}},
            {"id": 3, "text": "阅读《自我剖析》", "effect": "枯竭1，自选残韵×1",
             "costs": {"exhaust": 1}, "gains": {"resonance_choice": 1}},
            {"id": 4, "text": "拒绝", "effect": "无事发生", "is_refusal": True},
        ],
    },
    {
        "name": "祭坛",
        "description": "一座扭曲的黑石祭坛，中央有一柄干瘪的石刃。冰冷的声音响起：以你之重，换吾之赐。",
        "options": [
            {"id": 1, "text": "献祭血肉", "effect": "衰老8，获得1点[速限]",
             "costs": {"aging": 8}, "gains": {"speed_limit": 1}},
            {"id": 2, "text": "献祭神智", "effect": "枯竭5，获得2点[速限]",
             "costs": {"exhaust": 5}, "gains": {"speed_limit": 2}},
            {"id": 3, "text": "拒绝", "effect": "无事发生。", "is_refusal": True},
        ],
    },
    {
        "name": "过路商人",
        "description": "一个拖着骡车的独眼商人在岔路口支起了摊子，车上的货物用破布盖着。",
        "options": [
            {"id": 1, "text": "限制选择权", "effect": "失去 8 [碎片]，限制下一场战斗怪物的选择权",
             "costs": {"shards": 8}, "requires_dm": True},
            {"id": 2, "text": "以物易物", "effect": "失去一件当前遗物，随机获得一件新遗物",
             "requires_random": True},
            {"id": 3, "text": "拒绝", "effect": "无事发生。", "is_refusal": True},
        ],
    },
    {
        "name": "猩红暴雨",
        "description": "粘稠如血的暴雨从铅灰色天空倾泻而下，带有腐蚀性的雨水拍打在你的皮肤上。",
        "options": [
            {"id": 1, "text": "硬扛", "effect": "流血20", "costs": {"bleed": 20}},
            {"id": 2, "text": "用法力撑起屏障", "effect": "枯竭3", "costs": {"exhaust": 3}},
            {"id": 3, "text": "躲入漏雨的废墟", "effect": "失去1次精力", "costs": {"energy": 1}},
        ],
    },
    {
        "name": "无名碑林",
        "description": "在一片长满青苔的石碑群中，碑上刻满了扭曲的符文，记录着无数死者的执念。",
        "options": [
            {"id": 1, "text": "触摸", "effect": "流血15。获得【残韵：曲解×1】与15[碎片]。",
             "costs": {"bleed": 15}, "gains": {"resonance": {"曲解": 1}, "shards": 15}},
            {"id": 2, "text": "拒绝", "effect": "无事发生。", "is_refusal": True},
        ],
    },
    {
        "name": "回音长廊",
        "description": "一条两侧挂满破碎镜子的长廊，镜子里映出的是你前几次轮回中惨死的画面。",
        "options": [
            {"id": 1, "text": "聆听安魂曲", "effect": "在《死者之书》中留下一条错误遗言，获得10[碎片]。",
             "gains": {"shards": 10}, "requires_dm": True},
            {"id": 2, "text": "打碎镜子", "effect": "流血5，清除《死者之书》中一条遗言",
             "costs": {"bleed": 5}, "requires_dm": True},
            {"id": 3, "text": "捂住耳朵", "effect": "无事发生。", "is_refusal": True},
        ],
    },
    {
        "name": "回忆当铺",
        "description": "一家没有门的当铺，柜台后坐着一团由无数眼球和脑髓组成的阴影。",
        "options": [
            {"id": 1, "text": "典当", "effect": "获得10[碎片]，本次轮回无法再获得前世记忆",
             "gains": {"shards": 10}},
            {"id": 2, "text": "赎回", "effect": "消耗10[碎片]，获得一段前世记忆",
             "costs": {"shards": 10}, "requires_dm": True},
            {"id": 3, "text": "拒绝", "effect": "无事发生。", "is_refusal": True},
        ],
    },
    {
        "name": "手术",
        "description": "一个废弃的地下手术室，旁边的培养皿里有着一颗跳动的、长满触手的心脏正散发着微光。",
        "options": [
            {"id": 1, "text": "强制移植", "effect": "使一名微光者队友被植入一种随机的怪物道纹",
             "requires_random": True, "requires_dm": True},
            {"id": 2, "text": "抽取灵魂", "effect": "失去一名微光者队友，获得其[血限]50%的[碎片]",
             "requires_dm": True},
            {"id": 3, "text": "拒绝", "effect": "无事发生", "is_refusal": True},
        ],
    },
    {
        "name": "无魂泥潭",
        "description": "一处散发着胶皮烧焦气味的沥青深坑。黑色的淤泥在缓缓蠕动，能彻底隔绝灵魂的波动。",
        "options": [
            {"id": 1, "text": '采集"绝息淤泥"', "effect": "流血10。获得\u201c绝息淤泥\u201d",
             "costs": {"bleed": 10}, "gains": {"consumable": "绝息淤泥"}},
            {"id": 2, "text": "拒绝", "effect": "无事发生。", "is_refusal": True},
        ],
    },
]


def get_event(name: str) -> dict | None:
    for e in GENERAL_EVENTS:
        if e["name"] == name:
            return e
    return None


def list_general_events() -> list[str]:
    return [e["name"] for e in GENERAL_EVENTS]


# ==================== 消耗品 ====================

CONSUMABLES: dict[str, dict] = {
    "绝息淤泥": {
        "effect": "可随时使用，使用后屏蔽自身灵魂位置，可使本次[战终]，立刻逃脱",
        "uses": 1,
    },
}


def make_consumable(name: str, uses: int | None = None) -> Consumable | None:
    spec = CONSUMABLES.get(name)
    if not spec:
        return None
    n = uses if uses is not None else spec["uses"]
    return Consumable(name=name, effect=spec["effect"], current_uses=n, max_uses=n)
