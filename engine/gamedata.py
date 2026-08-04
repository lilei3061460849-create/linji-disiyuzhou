"""
游戏数据 - 从 README.md（唯一事实源）逐字转录的静态数据

维护规则（对应 README【十三、AI协作规范】二、事实源与同步）：
1. README.md 正文是唯一事实源，本文件为衍生物；与正文冲突时以正文为准。
2. 任何怪物面板、遗物、法术改动必须先改 README，再同步本文件，改完逐行比对。
3. engine/rule_sync.py 可从 README 反向提取怪物定义，用于核对本文件零差异。
"""

# ==================== 怪物池 ====================
# 一阶副本各12种，面板格式：攻击次数×攻击力/血限，道纹（数值）
# 副本参数：可分配法力30（设计度量衡），道纹数量3，数量总值8（仅设计阶段约束，
# 面板数值为 README 最终定稿，引擎不再二次校验配额——那是设计阶段的事）

MONSTER_POOLS = {
    "扭曲都市": [
        {"name": "千手蜈蚣",   "attack_count": 6, "attack_power": 8,  "blood_limit": 120, "daowen": {"畸变": 2, "狂暴": 2, "活力": 4}},
        {"name": "骨天使",     "attack_count": 4, "attack_power": 12, "blood_limit": 120, "daowen": {"变形": 2, "活力": 3, "飞行": 3}},
        {"name": "肠水母",     "attack_count": 3, "attack_power": 18, "blood_limit": 108, "daowen": {"僵化": 2, "庇护": 4, "自愈": 2}},
        {"name": "奇美拉",     "attack_count": 2, "attack_power": 16, "blood_limit": 120, "daowen": {"变形": 1, "强化": 5, "飞行": 2}},
        {"name": "眼树",       "attack_count": 1, "attack_power": 26, "blood_limit": 96,  "daowen": {"定型": 2, "必中": 4, "再生": 2}},
        {"name": "缝合鱼",     "attack_count": 3, "attack_power": 10, "blood_limit": 132, "daowen": {"退化": 4, "狂暴": 2, "衰败": 2}},
        {"name": "人头气球",   "attack_count": 1, "attack_power": 22, "blood_limit": 108, "daowen": {"僵化": 2, "飞行": 3, "必中": 3}},
        {"name": "脑蜘蛛",     "attack_count": 3, "attack_power": 14, "blood_limit": 120, "daowen": {"坏死": 1, "强化": 2, "减速": 5}},
        {"name": "血肉巨囊",   "attack_count": 2, "attack_power": 20, "blood_limit": 150, "daowen": {"爆裂": 3, "增殖": 4, "庇护": 1}},
        {"name": "爬行者",     "attack_count": 4, "attack_power": 10, "blood_limit": 108, "daowen": {"超频": 3, "急速": 2, "狂暴": 3}},
        {"name": "孢子母体",   "attack_count": 1, "attack_power": 18, "blood_limit": 132, "daowen": {"坏死": 2, "衰败": 3, "寄生": 3}},
        {"name": "畸变行者",   "attack_count": 3, "attack_power": 14, "blood_limit": 120, "daowen": {"爆裂": 3, "冲击": 3, "必中": 2}},
    ],
    "罪孽都市": [
        {"name": "毒枭",       "attack_count": 2, "attack_power": 10, "blood_limit": 108, "daowen": {"洗劫": 3, "贯穿": 3, "借力": 2}},
        {"name": "窃贼",       "attack_count": 2, "attack_power": 12, "blood_limit": 108, "daowen": {"洗劫": 3, "狂暴": 2, "必中": 3}},
        {"name": "打手",       "attack_count": 4, "attack_power": 6,  "blood_limit": 120, "daowen": {"逼债": 2, "狂暴": 3, "强化": 3}},
        {"name": "看门犬",     "attack_count": 3, "attack_power": 6,  "blood_limit": 90,  "daowen": {"逼债": 2, "狂暴": 3, "血债": 3}},
        {"name": "狂徒",       "attack_count": 3, "attack_power": 8,  "blood_limit": 120, "daowen": {"抵扣": 2, "狂暴": 3, "固执": 3}},
        {"name": "打手头目",   "attack_count": 5, "attack_power": 8,  "blood_limit": 132, "daowen": {"抵扣": 3, "强化": 3, "自愈": 2}},
        {"name": "军火商",     "attack_count": 2, "attack_power": 14, "blood_limit": 108, "daowen": {"清算": 3, "愤怒": 2, "兴奋": 3}},
        {"name": "狙击手",     "attack_count": 1, "attack_power": 28, "blood_limit": 96,  "daowen": {"清算": 2, "必中": 4, "减速": 2}},
        {"name": "杀手",       "attack_count": 1, "attack_power": 20, "blood_limit": 96,  "daowen": {"赎金": 3, "必中": 3, "减速": 2}},
        {"name": "暗哨",       "attack_count": 1, "attack_power": 16, "blood_limit": 108, "daowen": {"赎金": 2, "缓慢": 3, "蒙蔽": 3}},
        {"name": "赌鬼",       "attack_count": 2, "attack_power": 12, "blood_limit": 120, "daowen": {"假钞": 3, "赌命": 2, "借力": 3}},
        {"name": "通缉犯",     "attack_count": 3, "attack_power": 12, "blood_limit": 120, "daowen": {"假钞": 2, "消灾": 3, "狂暴": 3}},
    ],
    "龙心谷": [
        {"name": "熔岩蜥",     "attack_count": 3, "attack_power": 10, "blood_limit": 114, "daowen": {"加害": 2, "狂暴": 3, "冲击": 3}},
        {"name": "石背熊",     "attack_count": 2, "attack_power": 13, "blood_limit": 138, "daowen": {"龙鳞": 3, "庇护": 2, "固执": 3}},
        {"name": "断角羊",     "attack_count": 4, "attack_power": 7,  "blood_limit": 108, "daowen": {"逆鳞": 2, "活力": 3, "狂暴": 3}},
        {"name": "余烬侍者",   "attack_count": 2, "attack_power": 15, "blood_limit": 120, "daowen": {"活血": 3, "衰败": 2, "必中": 3}},
        {"name": "碎岩鸮",     "attack_count": 1, "attack_power": 20, "blood_limit": 102, "daowen": {"裂变": 2, "飞行": 3, "减速": 3}},
        {"name": "墓门卫",     "attack_count": 3, "attack_power": 10, "blood_limit": 132, "daowen": {"嫁祸": 2, "强化": 3, "庇护": 3}},
        {"name": "背碑人",     "attack_count": 2, "attack_power": 14, "blood_limit": 144, "daowen": {"背负": 3, "固执": 2, "自愈": 3}},
        {"name": "烙痕祭司",   "attack_count": 1, "attack_power": 22, "blood_limit": 114, "daowen": {"伤痕": 2, "必中": 3, "蒙蔽": 3}},
        {"name": "火山猿",     "attack_count": 5, "attack_power": 8,  "blood_limit": 126, "daowen": {"加害": 3, "狂暴": 3, "血债": 2}},
        {"name": "灰甲骑士",   "attack_count": 2, "attack_power": 16, "blood_limit": 126, "daowen": {"龙鳞": 2, "逆鳞": 3, "贯穿": 3}},
        {"name": "熔洞蛛",     "attack_count": 3, "attack_power": 12, "blood_limit": 120, "daowen": {"活血": 2, "裂变": 3, "急速": 3}},
        {"name": "断翼巨像",   "attack_count": 2, "attack_power": 20, "blood_limit": 150, "daowen": {"嫁祸": 2, "背负": 2, "伤痕": 4}},
    ],
}

# 副本阶级（用于出怪公式：数量=战斗场数，一阶副本直接-2，最低为1）
REGION_TIERS = {
    "扭曲都市": 1,
    "罪孽都市": 1,
    "龙心谷": 1,
}

# 副本专属道纹归属表（怪物池道纹合法性校验用）——与 combat.py 保持一致
REGION_EXCLUSIVE_DAOWEN = {
    "扭曲都市": {"变形", "定型", "畸变", "僵化", "超频", "坏死", "爆裂", "退化"},
    "罪孽都市": {"洗劫", "逼债", "抵扣", "清算", "赎金", "假钞", "赌命", "消灾"},
    "龙心谷":   {"加害", "龙鳞", "逆鳞", "活血", "裂变", "嫁祸", "背负", "伤痕"},
}

# 每个1阶副本的战斗场数（第8场为最终死斗）
REGION_BATTLE_COUNT = 7


# ==================== 法力池 / 遗物池（共12件，README 原文效果）====================
# implemented=False 的遗物：引擎尚未实现其效果，卡牌数据如实登记，
# 获取后如实在场、可作为交换/销毁对象，但其效果钩子不存在，绝不假装生效。

RELIC_POOL = [
    {"name": "血誓戒",   "implemented": True,
     "effect": "［回始］首次主动支付流血代价时，获得等同于本次流血的格挡；若支付后生命≤30%，改为获得等量生命"},
    {"name": "买路财",   "implemented": True,
     "effect": "战斗中可以失去等同于怪物20%[血限]的[碎片]安全撤退；碎片不足时，可以其他代价补足（1[碎片]=2生命=1[血限]）"},
    {"name": "同魂笔",   "implemented": True,
     "effect": "当你对[目标]发动残韵时，可以选择另一个[目标]，使其拥有的一种道纹受到同种残韵影响"},
    {"name": "回锋刀",   "implemented": True,
     "effect": "每失去1点速度后，对[目标]造成3点伤害；[回始]，对[目标]造成3×（你的[速限]-你的当前速度）的伤害"},
    {"name": "折速法印", "implemented": True,
     "effect": "[战始]可以疲惫X，获得6X点法力"},
    {"name": "三相残韵盘", "implemented": True,
     "effect": "[战始]，可以消耗自身拥有的一种残韵；[战终]获得另外两种残韵各1个"},
    {"name": "鲜血契约", "implemented": True,
     "effect": "[战始]，可以流血X，使首回合法力+X（X≤自身20%[血限]）"},
    {"name": "避风铃",   "implemented": True,
     "effect": "每次闪避后获得3点格挡，当前速度归零时，获得15点格挡"},
    {"name": "守夜灯",   "implemented": True,
     "effect": "[敌回始]，获得等同于[法限]50%的法力，该法力[敌回终]清空，每回合一次"},
    {"name": "钱袋",     "implemented": True,
     "effect": "每当敌方[目标][命零]，额外获得等同于其[战始][血限]2%的[碎片]"},
    {"name": "卖身契",   "implemented": True,
     "effect": "[战始]，可以指定一名[朋友]或[员工]；本场你支付的【代价】改由其承担，其[命零]后本效果失效"},
    {"name": "无所求",   "implemented": True,
     "effect": "每当你在事件中选择“拒绝”类选项，永久获得1点属性点"},
    {"name": "忘忧香",   "implemented": True,
     "effect": "局外行动你可以选择“忘忧”（失忆1/2/3，获得30/55/80［碎片］）"},
]


# ==================== 法术库（README「可学法术」一节，结构化）====================
# 每条法术拆解为可机械执行的步骤：
#   - daowen: 该步骤调用的道纹
#   - x_param: 施法者传入的X参数名（自由控X）
#   - target: "enemy" / "self" / "target"
#   - optional / condition: 条件步骤（不满足则跳过该步，不是失败）
#   - loop: 该组步骤按【循环规则】循环，直到法力耗尽或中断
# 触发条件（受到伤害前/失去生命后等）由API调用方在对应时点声明，
# 引擎校验该法术的 trigger 与声明时点一致后才执行流程。

SPELL_LIBRARY = {
    "先发制人": {
        "required_daowen": ["杀伐"],
        "trigger": "受到伤害前",
        "costs_action": False,   # 反应型法术：在触发时点插队发动，不消耗出手
        "steps": [{"daowen": "杀伐", "x_param": "x", "target": "enemy"}],
    },
    "临界泄压": {
        "required_daowen": ["锐利"],
        "trigger": "受到伤害前",
        "costs_action": False,
        "steps": [{"daowen": "锐利", "x_param": "x", "target": "enemy"}],
    },
    "生生不息": {
        "required_daowen": ["再生"],
        "trigger": "失去生命后",
        "costs_action": False,
        "steps": [{"daowen": "再生", "x_param": "x", "target": "self"}],
    },
    "后发制人": {
        "required_daowen": ["庇护"],
        "trigger": "受到伤害前",
        "costs_action": False,
        "steps": [{"daowen": "庇护", "x_param": "x", "target": "self"}],
    },
    "以牙还牙": {
        "required_daowen": ["杀伐", "再生"],
        "trigger": "失去生命后",
        "costs_action": False,
        "steps": [
            {"daowen": "再生", "x_param": "x", "target": "self"},
            {"daowen": "杀伐", "x_param": "y", "target": "enemy"},
        ],
    },
    "借力打力": {
        "required_daowen": ["杀伐", "庇护"],
        "trigger": "受到伤害前",
        "costs_action": False,
        "steps": [
            {"daowen": "庇护", "x_param": "x", "target": "self"},
            {"daowen": "杀伐", "x_param": "y", "target": "enemy"},
        ],
    },
    "不死不休": {
        "required_daowen": ["血债"],
        "trigger": "失去生命后",
        "costs_action": False,
        "steps": [
            {"daowen": "血债", "x_param": "x", "target": "enemy"},
        ],
        "loop": True,   # 付出代价→失去生命后再次触发，直到生命/出手条件中断
    },
    "千刀万剐": {
        "required_daowen": ["血债", "再生"],
        "trigger": "失去生命后",
        "costs_action": False,
        "steps": [
            {"daowen": "再生", "x_param": "x", "target": "self"},
            {"daowen": "血债", "x_param": "3x", "x_fixed_multiplier": 3, "target": "enemy"},
        ],
        "loop": True,
    },
    "咎由自取": {
        "required_daowen": ["坠落", "杀伐", "血债"],
        "trigger": "[目标]发动道纹前",
        "costs_action": False,
        "steps": [
            {"daowen": "坠落", "x_param": "x", "target": "enemy",
             "condition": "target_flying"},                       # 若其处于飞行
            {"daowen": "杀伐", "x_param": "y", "target": "enemy"},
            {"daowen": "血债", "x_param": "z", "target": "enemy",
             "condition": "previous_step_no_damage"},             # 若未造成伤害
        ],
    },
}

# 反应型法术的合法触发时点（use_spell 时校验）
VALID_SPELL_TRIGGERS = {
    "受到伤害前", "失去生命后", "[目标]发动道纹前",
}


# ==================== 已实装道纹（诚实白名单）====================
# 原则：只有机制已被引擎真实实装的道纹才允许发动；
# 未实装的道纹即使存在公式，也一律拒绝发动并如实说明，绝不允许假装生效。
# 未实装原因统一为：机制交互纵深过大（随机目标/重置随机数/跨实体承伤链），
# 待DM裁定语义后再补装。

# 数值公式已注册（daowen.py）但机制未实装的道纹
UNIMPLEMENTED_DAOWEN = {
    "赌命",   # 依赖"从轮回者方开始发放数字投随机数"的跨阵营随机流程
    "消灾",   # 依赖重置随机数机制，须与随机数规则联动设计
}

# 真正已实装的道纹集合（可用 use_daowen / monster_turn 发动）
IMPLEMENTED_DAOWEN = {
    # 杀伐闭环
    "杀伐", "再生", "庇护", "固执", "血债", "冲击", "慈悲",
    # 锐利闭环
    "锐利", "增殖", "束缚", "透支", "贯穿", "封印", "缓慢",
    # 怪物原始道纹
    "狂暴", "强化", "活力", "减速", "必中", "自愈", "飞行",
    # 怪物转化道纹
    "愤怒", "自残", "无神", "借力", "弱化", "自食", "兴奋", "无力",
    "迟滞", "急速", "加速", "眩晕", "洞察", "蒙蔽", "滋养", "衰败",
    "寄生", "滑翔", "坠落",
    # 扭曲都市
    "定型", "畸变", "僵化", "超频", "坏死", "爆裂", "退化",
    # 罪孽都市
    "洗劫", "逼债", "抵扣", "清算", "赎金", "假钞",
    # 龙心谷
    "加害", "龙鳞", "逆鳞", "活血", "裂变", "嫁祸", "背负", "伤痕",
    # 变形：攻击力与攻击次数互换（含回复原状的持续管理）已实装
    "变形",
} - UNIMPLEMENTED_DAOWEN


# ==================== 出怪公式 ====================

def monster_spawn_count(battle_number: int, region: str) -> int:
    """
    出怪数量 = 战斗场数，一阶副本战斗场数直接-2，最低为1。
    一阶7场：1/1/1/2/3/4/5只。允许重复抽选同一怪物种族。
    """
    tier = REGION_TIERS.get(region, 1)
    count = battle_number
    if tier == 1:
        count = battle_number - 2
    return max(1, count)
