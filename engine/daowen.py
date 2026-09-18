"""
道纹系统 - 所有道纹效果的数学计算
规则：道纹是概念，不是技能。两字词，世界最小规则单元。
"""
from __future__ import annotations
from typing import Optional, Any
from .models import Entity, StatusEffect, DaoWen, DaoWenInstance
from .enums import CostType
import math




class DaoWenEngine:
    """道纹计算引擎"""

    # 怪物转化道纹（原始怪物道纹经残韵变化后的19个分支，与规则正文《原始怪物道纹与转化道纹》一致）
    # 用于"雇佣"后"发现并选择一种转化道纹"等需要从此类别中随机抽取的场景
    TRANSFORMED_DAOWEN = [
        "愤怒", "自残", "无神", "借力", "弱化", "自食", "兴奋", "无力", "全速",
        "急速", "加速", "眩晕", "洞察", "蒙蔽", "滋养", "衰败", "寄生", "滑翔", "坠落",
    ]

    # 可选目标道纹（2026-09-17）：正文口径是「[目标]可选，不填则自身」。
    # 这两条的 calculate_* **故意不声明 target 形参**——api.py 的判定是"声明了 target
    # 就必须显式指定目标，禁止静默改为自身"（见 _action_use_daowen），声明了反而
    # 会把"不填则自身"这条口径堵死。
    # 代价是怪物侧曾把它们一律当成"无目标道纹"：prepare 不给 target_options、
    # resolve 直接拒绝 target_ref，于是怪物**永远只能自施**。对【变形】这是致命的：
    # 互换后超出[速限]的部分蒸发，"攻力>攻次"的怪自施即自残（骨天使 7法/3速 →
    # 3击×3），而"喝汤"用法（对法力>速度的轮回者施放）在接口层根本不可达。
    # 本名单由 combat.py::_daowen_target_mode 消费：怪物侧同样给出目标候选并接受
    # 显式 target_ref，不填仍回落自身。新增同类道纹只改这份数据，不改判定代码。
    OPTIONAL_TARGET_DAOWEN = {"变形", "超频"}

    # X上限规则（代价类型 → 最大值函数）
    X_LIMITS = {
        "消耗": lambda state: float('inf'),     # 无上限，受法力限制
        "冷却": lambda state: 7,                 # 0≤X≤7
        "流血": lambda state: state.get("current_hp", 999),    # 0≤X≤当前生命
        "衰老": lambda state: state.get("blood_limit", 999),   # 0≤X≤当前血限
        "枯竭": lambda state: state.get("mana_limit", 999),    # 0≤X≤当前法限
        "萎缩": lambda state: state.get("speed_limit", 999),   # 0≤X≤当前速限
        "疲惫": lambda state: state.get("current_speed", 999), # 0≤X≤当前速度
        "失忆": lambda state: state.get("daowen_count", 999),  # 0≤X≤当前道纹数量
        "异变": lambda state: 50,                # 0≤X≤50
    }
    
    # ========== 核心道纹效果表 ==========
    # 每个道纹返回标准化的计算结果字典
    
    @staticmethod
    def ceil(value: float) -> int:
        """整数规则：所有计算都向上取整"""
        return math.ceil(value)
    
    # ---- 杀伐闭环 ----
    
    @staticmethod
    def calculate_shaifa(x: int, target: Entity = None) -> dict:
        """杀伐X：消耗X。对[目标]造成X²点伤害

        DM裁定 2026-09-10：法力改一池制后（战终才复原，不再每回合回填），道纹数值
        的约束从「速率」变成「预算」，可以放开。
        用户裁定 2026-09-13：由线性 5X 改为平方 X²——小X时弱于旧值（X≤4），
        大X时远强（X=10 打 100），把「攒法力一次性爆发」从习惯变成硬性最优解，
        与一池制预算的设计意图一致。
        """
        target_name = target.name if target is not None else "未选定目标"
        cost = x
        damage = x * x
        return {
            "dao_wen": "杀伐",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": cost,
            "target_damage": damage,
            "damage_type": "普通",
            "summary": f"消耗{x}法力，对{target_name}造成{damage}点伤害"
        }
    
    @staticmethod
    def calculate_zaisheng(x: int, target: Entity = None) -> dict:
        """再生X：消耗X。为[目标]回复4X点生命（2026-09-11 用户令：3X→4X，再生被庇护完爆）"""
        target_name = target.name if target is not None else "未选定目标"
        cost = x
        heal = 4 * x
        return {
            "dao_wen": "再生",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": cost,
            "target_heal": heal,
            "summary": f"消耗{x}法力，为{target_name}回复{heal}点生命"
        }
    
    @staticmethod
    def calculate_bihu(x: int, target: Entity = None) -> dict:
        """庇护X：消耗X。使[目标]获得2X点格挡（可抵消等量伤害），持续1"""
        target_name = target.name if target is not None else "未选定目标"
        cost = x
        shield = 2 * x
        return {
            "dao_wen": "庇护",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": cost,
            "target_shield": shield,
            "duration": 1,
            # DM裁定 2026-09-10：格挡不再[敌回终]清除，故文案不再声称"持续1回合"
            "summary": f"消耗{x}法力，使{target_name}获得{shield}点格挡（可抵消等量伤害，保留到被打掉或战终）"
        }
    
    @staticmethod
    def calculate_guzhi(x: int) -> dict:
        """固执X：代价：冷却X。自身单次失去生命最高为1，持续X"""
        return {
            "dao_wen": "固执",
            "x": x,
            "cost_type": CostType.COOLDOWN.value,
            "cost": x,
            "duration": x,
            "max_life_loss_per_hit": 1,
            "summary": f"冷却{x}场，自身单次失去生命最高为1，持续{x}回合"
        }
    
    @staticmethod
    def calculate_xuezhai(x: int, target: Entity = None) -> dict:
        """血债X：代价：流血X。选择[目标] X 次，每次对其造成 1 点伤害"""
        target_name = target.name if target is not None else "未选定目标"
        cost_hp = x
        hits = x
        damage_per_hit = 1
        return {
            "dao_wen": "血债",
            "x": x,
            "cost_type": CostType.BLEED.value,
            "cost_hp": cost_hp,
            "hits": hits,
            "damage_per_hit": damage_per_hit,
            "total_damage": hits * damage_per_hit,
            "summary": f"流血{x}，选择{target_name} {hits}次，每次造成{damage_per_hit}点伤害"
        }
    
    @staticmethod
    def calculate_boba(x: int) -> dict:
        """波及X：消耗2X。选择X个[目标]建立/解除波及效果，持续∞。你发动的道纹同时作用于所有拥有波及效果的目标；数值平分。"""
        return {
            "dao_wen": "波及",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "mark_targets": x,
            "duration": -1,  # ∞
            "summary": f"消耗{2 * x}法力，选择{x}个目标建立/解除波及效果（持续∞）"
        }
    
    # ---- 杀伐11节点闭环后半（增殖至封印）----
    
    @staticmethod
    def calculate_zengzhi(x: int, target: Entity = None) -> dict:
        """增殖X：消耗X。［目标］［血限］+X"""
        target_name = target.name if target is not None else "未选定目标"
        cost = x
        increase = x
        return {
            "dao_wen": "增殖",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": cost,
            "blood_limit_increase": increase,
            "summary": f"消耗{cost}法力，{target_name}血限+{increase}"
        }
    
    @staticmethod
    def calculate_shufu(x: int, target: Entity = None) -> dict:
        """束缚X：代价：冷却2X。使[目标]无法行动，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "束缚",
            "x": x,
            "cost_type": CostType.COOLDOWN.value,
            "cost": 2 * x,
            "duration": x,
            "effect": "无法行动",
            "summary": f"冷却{2*x}场，使{target_name}无法行动，持续{x}回合"
        }
    
    @staticmethod
    def calculate_touzhi(x: int) -> dict:
        """透支X：代价：流血4X。你获得X点法力

        用户裁定 2026-09-12：与【再生X】构成生命⇄法力闭环，净值不产生免费资源；
        实际上限由【癌变】(本场累计回复=2×血限) 自然封死。
        用户裁定 2026-09-13：3X→4X。与【再生X】(消耗X法力→回复4X生命) 对齐为
        严格 4:1 双向汇率，闭环净值归零，不再每轮白赚生命。
        """
        return {
            "dao_wen": "透支",
            "x": x,
            "cost_type": CostType.BLEED.value,
            "cost_hp": 4 * x,
            "mana_gain": x,
            "summary": f"流血{4*x}，获得{x}点法力"
        }
    
    @staticmethod
    def calculate_guanchuan(x: int) -> dict:
        """贯穿X：消耗2X。你造成的伤害无视格挡，持续X"""
        return {
            "dao_wen": "贯穿",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "duration": x,
            "effect": "伤害无视格挡",
            "summary": f"消耗{2 * x}法力，造成的伤害无视格挡，持续{x}回合"
        }
    
    @staticmethod
    def calculate_fengyin(x: int, target: Entity = None) -> dict:
        """封印X：代价：异变X，使一个目标怪物延后X回合再入场。"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "封印",
            "x": x,
            "cost_type": CostType.MUTATION.value,
            "cost_mutation": x,
            "delay_monster_reentry": True,
            "delay_rounds": x,
            "target_name": target_name,
            "summary": f"异变+{x}，使{target_name}延后{x}回合再入场"
        }
    
    # ---- 怪物原始道纹 ----
    
    @staticmethod
    def calculate_kuangbao(x: int) -> dict:
        """狂暴X：代价：异变5X。回始发动一轮额外攻击，持续X"""
        return {
            "dao_wen": "狂暴",
            "x": x,
            "cost_type": CostType.MUTATION.value,
            "cost_mutation": 5 * x,
            "duration": x,
            "effect": "回始发动一轮额外攻击",
            "summary": f"异变+{5*x}，回始发动一轮额外攻击，持续{x}回合"
        }
    
    @staticmethod
    def calculate_quanli(x: int, target: Entity = None) -> dict:
        """全力X：代价：异变5X。使[目标]攻击力等同其法限，持续X

        2026-09-17 用户令重做。旧版「攻击力+X，持续∞」写的是遗留字段 attack_power，
        属性模型统一后（攻击力=当前法力）对不写穿的轮回者完全无效。

        新版改为**锁定**：生效期间[目标]的攻击力恒等于其[法限]，不再随当前法力
        下降而下降——即"不用担心法力降低导致攻击输出降低"。法力本身照常被消耗
        （它仍是施法资源），只是攻击力不再跟着掉。

        实现走状态层（状态名"全力"），由 models.py::effective_attack_power 读取，
        故对轮回者/怪物/朋友/员工同口径生效。
        """
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "全力",
            "x": x,
            "cost_type": CostType.MUTATION.value,
            "cost_mutation": 5 * x,
            "attack_power_to_mana_limit": True,
            "duration": x,
            "summary": f"异变+{5*x}，使{target_name}攻击力等同其法限，持续{x}回合"
        }
    
    @staticmethod
    def calculate_huoli(x: int) -> dict:
        """疯狂X：代价：异变5X。所有角色出手次数+X，持续∞（2026-08-17裁定：全局生效，变相平衡）"""
        return {
            "dao_wen": "疯狂",
            "x": x,
            "cost_type": CostType.MUTATION.value,
            "cost_mutation": 5 * x,
            "action_boost": x,
            "duration": -1,
            "summary": f"异变+{5*x}，所有角色出手次数+{x}，永久"
        }

    @staticmethod
    def calculate_jinghua(x: int, target: Entity = None) -> dict:
        """净化X：消耗2X。使[目标]【异变】-X层"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "净化",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "mutation_reduction": x,
            "summary": f"消耗{2 * x}法力，使{target_name}【异变】-{x}层",
        }
    
    @staticmethod
    def calculate_jiansu(x: int, target: Entity = None) -> dict:
        """减速X：代价：异变5X。使[目标]失去其当前速度的10X%

        改版（2026-09-18，DM 裁定：冥气的倍率必须小于减速，否则没人会用减速）：
        旧版为「速度减半，持续X」，而 `duration` 在实现里**从未被读取**——没有挂任何
        状态，就是一次性把当前速度砍半。于是 X 只把异变代价从 5 涨到 25、效果一点不变，
        减速X=2…5 被减速X=1 严格支配（这正是"没人会用减速"的根因）。
        新版让 X 成为**幅度**参数：10X%，X=5 即"失去一半"。
        取整按正文「整数规则：所有计算都向上取整」——`ceil(当前速度 × 10X / 100)`，
        因此奇数速度上 X=5 比旧版减半多削 1 点（旧式 `cur - ceil(cur/2)`＝floor）。
        不封 X 上限：异变是累加计数、达 50 层【崩解】直接命零，怪物侧探测上限
        `_monster_max_daowen_x` 已按生存线卡在 9（45 层＝离崩解只差一次代价），
        高 X 的代价本身就是刹车，不需要再钉一道数值封顶。
        速度是一池制（[回始]不回填、[战终]复原），所以本效果没有"持续"可言：
        砍掉的就是整场速度池的一部分，正文因此不再写「持续X」。
        """
        target_name = target.name if target is not None else "未选定目标"
        pct = 10 * x
        # 只在 summary 里给发动方看一个预览值；真正扣多少由 combat 在结算那一刻
        # 按各目标自己的当前速度算（波及目标各有其值），不落进 calc 当第二个事实源。
        preview = DaoWenEngine.ceil(target.current_speed * pct / 100) if target is not None else 0
        return {
            "dao_wen": "减速",
            "x": x,
            "cost_type": CostType.MUTATION.value,
            "cost_mutation": 5 * x,
            "speed_loss_pct": pct,
            "summary": f"异变+{5*x}，使{target_name}失去其当前速度的{pct}%（{preview}点）"
        }
    
    @staticmethod
    def calculate_bizhong(x: int) -> dict:
        """必中X：代价：异变5X。自身下X次选择[目标]（攻击与道纹通用，共用层数）时其无法闪避"""
        return {
            "dao_wen": "必中",
            "x": x,
            "cost_type": CostType.MUTATION.value,
            "cost_mutation": 5 * x,
            "guaranteed_hits": x,
            "summary": f"异变+{5*x}，自身下{x}次选择[目标]（攻击/道纹共用层数）时其无法闪避"
        }
    
    @staticmethod
    def calculate_ziyu(x: int, target: Entity = None) -> dict:
        """自愈X：代价：冷却X。恢复[目标]25X%已损生命。

        2026-09-18 用户令重做。旧版「代价：异变5X。[回始]获得自身血限10X%的回复，持续∞」是 ROUND_START 机制，
        在现行规则下是一台**自杀定时器**：回复走统一 heal 动词，total_healed 连过量部分
        一起按原值累计，而癌变阈值只有 ceil(血限×2) → 承载怪每回合自我奶 ceil(血限×10X%)，
        ceil(20/X) 回合后必然自我癌变（X=3→7、X=5→4、X=9→3），永久离场且不给[碎片]，
        只白送局外【休整】+8；叠加异变5X/次与崩解线50，第二次发动还会直接崩解。

        新版：主动、单体、按**已损生命**计价（不浪费在满血目标上）、代价【冷却X】
        （X 场战斗，由 combat 的冷却分支写 cooldown_remaining，无需新结算代码）。
        与【滋养】（使目标受到的恢复量翻倍）组成 combo：滋养 + 自愈2 = 50%×2 = 满血复活。
        癌变没有被绕开——恢复量照原值计入 total_healed，唯一免疫仍是遗物【第一杯】。
        """
        target_name = target.name if target is not None else "未选定目标"
        missing = max(0, target.blood_limit - target.current_hp) if target is not None else 0
        heal = DaoWenEngine.ceil(missing * 25 * x / 100)
        return {
            "dao_wen": "自愈",
            "x": x,
            "cost_type": CostType.COOLDOWN.value,
            "cost": x,
            "heal_missing_percent": 25 * x,
            "summary": f"冷却{x}场，恢复{target_name}已损生命的{25*x}%（{heal}点）"
        }
    
    @staticmethod
    def calculate_feixing(x: int) -> dict:
        """飞行X：代价：异变5X。无法被非飞行角色选为目标，持续X"""
        return {
            "dao_wen": "飞行",
            "x": x,
            "cost_type": CostType.MUTATION.value,
            "cost_mutation": 5 * x,
            "duration": x,
            "effect": "无法被非飞行角色选为目标",
            "summary": f"异变+{5*x}，无法被非飞行角色选为目标，持续{x}回合"
        }
    
    # ---- 怪物转化道纹 ----
    
    @staticmethod
    def calculate_fennu(x: int, target: Entity = None) -> dict:
        """愤怒X：消耗2X。使[目标]法力消耗减半，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "愤怒",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "mana_cost_halved": True,
            "duration": x,
            "summary": f"消耗{2 * x}法力，使{target_name}法力消耗减半，持续{x}回合"
        }
    
    @staticmethod
    def calculate_zican(x: int, target: Entity = None) -> dict:
        """自残X：消耗3X。使[目标]对其自身打出X次攻击"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "自残",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 3 * x,
            "self_attack_count": x,
            "summary": f"消耗{3 * x}法力，使{target_name}对自身打出{x}次攻击"
        }
    
    @staticmethod
    def calculate_wushen(x: int, target: Entity = None) -> dict:
        """无神X：消耗5X。使[目标]选择目标时强制改为自身，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "无神",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 5 * x,
            "duration": x,
            "effect": "选择目标时强制改为自身",
            "summary": f"消耗{5 * x}法力，使{target_name}选择目标时强制改为自身，持续{x}回合"
        }
    
    @staticmethod
    def calculate_jieli(x: int, target: Entity = None) -> dict:
        """借力X：消耗3X。使[目标]造成伤害+10X%，持续∞"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "借力",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 3 * x,
            "damage_boost_percent": 10 * x,
            "duration": -1,
            "summary": f"消耗{3 * x}法力，使{target_name}造成伤害+{10*x}%，永久"
        }
    
    @staticmethod
    def calculate_ruhua(x: int, target: Entity = None) -> dict:
        """弱化X：消耗2X。使[目标]攻击力-X，持续∞"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "弱化",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "attack_reduction": x,
            "duration": -1,
            "summary": f"消耗{2 * x}法力，使{target_name}攻击力-{x}，永久"
        }
    
    @staticmethod
    def calculate_zishi(x: int) -> dict:
        """自食X：消耗X。将自身X点攻击力转化为等量回复"""
        return {
            "dao_wen": "自食",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": x,
            "attack_reduction": x,                       # 面板量纲，不放大
            "target_heal": x,
            "summary": f"消耗{x}法力，将自身{x}攻击力转化为{x}点回复"
        }
    
    @staticmethod
    def calculate_xingfen(x: int, target: Entity = None) -> dict:
        """兴奋X：消耗2X。使[目标]每次出手后速度+1，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "兴奋",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "speed_gain_per_action": 1,
            "duration": x,
            "summary": f"消耗{2 * x}法力，使{target_name}每次出手后速度+1，持续{x}回合"
        }
    
    @staticmethod
    def calculate_wuli(x: int, target: Entity = None) -> dict:
        """无力X：消耗3X。回始使[目标]出手次数-X，持续∞"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "无力",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 3 * x,
            "action_reduction": x,
            "duration": -1,
            "summary": f"消耗{3 * x}法力，回始使{target_name}出手次数-{x}，永久"
        }
    
    @staticmethod
    def calculate_quansu(x: int, target: Entity = None) -> dict:
        """全速X（原名【迟滞】）：代价：冷却X。使[目标]攻击次数等同其速限，持续X

        2026-09-17 用户令重做并改名。旧版「攻击次数固定为1」是减益，写的是遗留字段
        attack_count，属性模型统一后（攻击次数=当前速度）对轮回者无效。

        新版改为**锁定为[速限]**：生效期间[目标]的攻击次数恒等于其[速限]。
        由于 2026-09-13 全局钳制规则（clamp_immortal_body）已让「当前速度≤[速限]」
        无条件成立，本效果实为**增益**：把被削的速度补满到上限，并免疫后续减速。
        因语义由减益翻转为增益，原名「迟滞」名不副实，故改名【全速】。

        走状态层（状态名"全速"），由 models.py::effective_attack_count 读取，
        对轮回者/怪物/朋友/员工同口径生效。
        """
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "全速",
            "x": x,
            "cost_type": CostType.COOLDOWN.value,
            "cost": x,
            "attack_count_to_speed_limit": True,
            "duration": x,
            "summary": f"冷却{x}场，使{target_name}攻击次数等同其速限，持续{x}回合"
        }
    
    @staticmethod
    def calculate_jisu(x: int, target: Entity = None) -> dict:
        """急速X：消耗5X。使[目标]每闪避两次速度+1，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "急速",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 5 * x,
            "speed_per_2_dodges": 1,
            "duration": x,
            "summary": f"消耗{5 * x}法力，使{target_name}每闪避两次速度+1，持续{x}回合"
        }
    
    @staticmethod
    def calculate_jiasu(x: int, target: Entity = None) -> dict:
        """加速X：消耗5X。使[目标]获得的速度翻倍，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "加速",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 5 * x,
            "speed_doubled": True,
            "duration": x,
            "summary": f"消耗{5 * x}法力，使{target_name}获得的速度翻倍，持续{x}回合"
        }
    
    @staticmethod
    def calculate_xuanyun(x: int, target: Entity = None) -> dict:
        """眩晕X：消耗5X。使[目标]无法出手，受到伤害后解除，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "眩晕",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 5 * x,
            "duration": x,
            "effect": "无法出手，受到伤害后解除",
            "summary": f"消耗{5 * x}法力，使{target_name}无法出手，受伤害后解除，持续{x}回合"
        }
    
    @staticmethod
    def calculate_dongcha(x: int, target: Entity = None) -> dict:
        """洞察X：代价：疲惫X。使[目标]每次闪避后下回合法力+10，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "洞察",
            "x": x,
            "cost_type": CostType.FATIGUE.value,
            "cost_speed": x,
            "mana_per_dodge": 10,
            "duration": x,
            "summary": f"疲惫{x}，使{target_name}每次闪避后下回合法力+10，持续{x}回合"
        }
    
    @staticmethod
    def calculate_mengbi(x: int, target: Entity = None) -> dict:
        """蒙蔽X：消耗2X。使[目标]下X次造成的伤害无效"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "蒙蔽",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "invalid_damage_hits": x,
            "summary": f"消耗{2 * x}法力，使{target_name}下{x}次造成的伤害无效"
        }
    
    @staticmethod
    def calculate_ziyang(x: int, target: Entity = None) -> dict:
        """滋养X：消耗2X。使[目标]受到的恢复量翻倍，持续X。

        2026-09-18 用户令重做。旧版「使[目标]获得血限10X%的回复」是一次性大奶，且与母道纹【自愈】同形。
        新版改成**放大器**：本身不回复任何生命，只在持续期间让目标受到的每一笔
        恢复量×2——结算点在 models.GameState.apply_heal（统一回复入口），
        因此覆盖**战斗内**的一切来源（道纹／消耗品／寄生…）。
        它不加成局外行动：滋养是局内状态（StatusEffect scope=BATTLE、持续X回合），
        [战终]统一清除，而【休整】是战前行动，两者永不同时在场。

        过量部分同样翻倍计入 total_healed，所以滋养同时把癌变进度×2：
        对怪＝更快癌变（无[碎片]、局外休整+8），对轮回者/同伴＝更快直接[命零]。
        用户裁定：能避开癌变的方法有且只有遗物【第一杯】，这就是本道纹的强度上限。

        与【自愈】的 combo：滋养（×2）+ 自愈2（已损生命50%）= 100% 已损 = 满血复活。
        calc 只带 duration → 走 combat 通用状态块挂【滋养】状态（value=X、持续X回合）；
        同名合并按正文规则：状态不增强倍率（恒为×2），只叠加持续时间。
        """
        target_name = target.name if target is not None else "未选定目标"
        cost = 2 * x
        return {
            "dao_wen": "滋养",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": cost,
            "duration": x,
            "summary": f"消耗{cost}法力，使{target_name}受到的恢复量翻倍，持续{x}回合"
        }
    
    @staticmethod
    def calculate_shuaibai(x: int, target: Entity = None) -> dict:
        """衰败X：消耗4X。使[目标][回始]失去10X%当前生命，持续∞；发动时不立即触发。"""
        target_name = target.name if target is not None else "未选定目标"
        cost = 4 * x
        return {
            "dao_wen": "衰败",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": cost,
            "duration": -1,
            "summary": f"消耗{cost}法力，使{target_name}[回始]失去{10*x}%当前生命，持续∞"
        }
    
    @staticmethod
    def calculate_jisheng(x: int, target: Entity = None, caster: Entity = None) -> dict:
        """寄生X：消耗3X。使[目标]受到的伤害20X%转化为施法者的回复，持续∞"""
        target_name = target.name if target is not None else "未选定目标"
        caster_name = caster.name if caster is not None else "未知施法者"
        return {
            "dao_wen": "寄生",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 3 * x,
            "drain_percent": 20 * x,
            "duration": -1,
            "summary": f"消耗{3 * x}法力，使{target_name}受到伤害的{20*x}%转化为{caster_name}的回复，永久"
        }
    
    @staticmethod
    def calculate_huaxiang(x: int) -> dict:
        """滑翔X：消耗2X。获得飞行，持续X"""
        return {
            "dao_wen": "滑翔",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "duration": x,
            "effect": "获得飞行",
            "summary": f"消耗{2 * x}法力，获得飞行，持续{x}回合"
        }
    
    @staticmethod
    def calculate_zhuiluo(x: int) -> dict:
        """坠落X：消耗X。所有飞行角色无法飞行且造成伤害减半，持续X"""
        return {
            "dao_wen": "坠落",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": x,
            "duration": x,
            "effect": "所有飞行角色无法飞行且造成伤害减半",
            "summary": f"消耗{x}法力，所有飞行角色无法飞行且造成伤害减半，持续{x}回合"
        }
    
    # ---- 扭曲都市专属道纹 ----
    
    @staticmethod
    def calculate_bianxing(x: int) -> dict:
        """变形X：消耗X。使[目标]当前速度与当前法力互换，持续X

        注意**故意不声明 target 形参**：api.py 的判定是"计算函数声明了 target 就
        强制要求显式指定目标，禁止静默改为自身"。而 2026-09-17 用户令要的是
        **可选目标**：指定了就作用于该目标，不指定则作用于自身。故此处不声明，
        由 combat.py 的 ``swap_target = target if target else caster`` 兜底。

        2026-09-17 用户令重做。旧版「使自身攻击力与攻击次数互换」写的是遗留字段
        attack_power / attack_count，属性模型统一后（攻击力=当前法力、攻击次数=
        当前速度）对不写穿的轮回者无效，且只能对自己用。

        新版直接互换**当前速度**与**当前法力**，可指定目标（不指定时默认自身）。
        互换后两者各自被上限钳制（当前速度≤[速限]、当前法力≤[法限]），
        **被钳掉的部分凭空消失**——这正是本道纹的收益来源：
            例：敌方 20/3/10（血限/速度/法力，速限3）
                互换 → 速度10、法力3 → 速度被速限钳回3
                结果 20/3/3 —— 目标凭空失去 7 点法力（攻击力同步下降 7）。
        持续X结束后还原互换前的当前速度/当前法力，但被钳掉的部分不返还。
        """
        return {
            "dao_wen": "变形",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": x,
            "duration": x,
            "effect": "当前速度与当前法力互换",
            "summary": f"消耗{x}法力，使[目标]当前速度与当前法力互换（超出上限部分蒸发），持续{x}回合"
        }
    
    @staticmethod
    def calculate_dingxing(x: int, target: Entity = None) -> dict:
        """定型X：消耗2X。使[目标]攻击次数与攻击力无法被改变，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "定型",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "duration": x,
            "effect": "攻击次数与攻击力无法被改变",
            "summary": f"消耗{2 * x}法力，使{target_name}攻击次数与攻击力无法被改变，持续{x}回合"
        }
    
    @staticmethod
    def calculate_jibian(x: int, target: Entity = None) -> dict:
        """畸变X：代价：冷却X。回终使[目标]失去(攻击力×攻击次数)的血限，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        if target is not None:
            atk = target.attack_count
            ap = target.attack_power
            blood_loss = atk * ap
        else:
            blood_loss = 0
        return {
            "dao_wen": "畸变",
            "x": x,
            "cost_type": CostType.COOLDOWN.value,
            "cost": x,
            "blood_loss_per_round": blood_loss,
            "duration": x,
            "summary": f"冷却{x}场，回终使{target_name}失去{blood_loss}血限（{atk if target is not None else 0}×{ap if target is not None else 0}），持续{x}回合"
        }
    
    @staticmethod
    def calculate_boming(x: int) -> dict:
        """搏命X：代价：疲惫X。你获得X点法力

        用户裁定 2026-09-13：放弃闪避换法力、拼死一搏。倍率被【超频】
        (消耗2X法力→速度+X) 反向锁死——设倍率为k，卖X速度得kX法力可经
        超频买回 kX/2 速度，净变化 X(k/2-1)：k≥2 即永动或速度无限暴涨。
        故取 k=1，每卖1点速度净亏0.5点，循环必然收敛。
        （遗物【折速法印】原为6X，因[战始]一次性且不可复发才安全；改为
        可反复发动的道纹后必须砍到1X，该遗物同步删除，不再双份存在。）
        """
        return {
            "dao_wen": "搏命",
            "x": x,
            "cost_type": CostType.FATIGUE.value,
            "cost_speed": x,
            "mana_gain": x,
            "summary": f"疲惫{x}，获得{x}点法力"
        }
    
    @staticmethod
    def calculate_chaopin(x: int) -> dict:
        """超频X：消耗2X。使[目标]速度+X（2026-09-17 用户令：改为自由选择目标）

        目标由发动方自由指定，选到谁就给谁加速度——可以给自己，也可以给队友
        或敌人。旧版写作"使自身速度+X"，但实现一直是给 target 加速，文案与
        行为不符；现按用户裁定统一为"自由选择目标"。
        """
        return {
            "dao_wen": "超频",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "speed_boost": x,
            "summary": f"消耗{2*x}法力，[目标]速度+{x}"
        }
    
    @staticmethod
    def calculate_huaisi(x: int, target: Entity = None) -> dict:
        """坏死X：消耗2X。使[目标]无法获得回复，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "坏死",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "duration": x,
            "effect": "无法获得回复",
            "summary": f"消耗{2 * x}法力，使{target_name}无法获得回复，持续{x}回合"
        }
    
    @staticmethod
    def calculate_baolie(x: int) -> dict:
        """爆裂X：消耗2X。受到伤害后，攻击者失去等量生命，持续X"""
        return {
            "dao_wen": "爆裂",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "duration": x,
            "effect": "受到伤害后，攻击者失去等量生命",
            "summary": f"消耗{2 * x}法力，受到伤害后攻击者失去等量生命，持续{x}回合"
        }
    
    @staticmethod
    def calculate_tuihua(x: int, target: Entity = None) -> dict:
        """退化X：消耗2X。使[目标]每次发动道纹时该次数值-X(最低0)，持续∞"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "退化",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": 2 * x,
            "dao_wen_reduction": x,
            "duration": -1,
            "summary": f"消耗{2 * x}法力，使{target_name}每次发动道纹数值-{x}(最低0)，永久"
        }
    
    # ---- 罪孽都市专属道纹 ----
    
    @staticmethod
    def calculate_jiahai(x: int, target: Entity = None) -> dict:
        """加害X：消耗2X。使[目标]每次受到伤害+X，持续∞（龙心谷闭环起点）"""
        target_name = target.name if target is not None else "未选定目标"
        cost = 2 * x
        return {
            "dao_wen": "加害",
            "x": x,
            "cost_type": CostType.MANA.value,
            "cost": cost,
            "duration": -1,
            "status": {"name": "加害", "value": x, "duration": -1},
            "summary": f"消耗{cost}法力，使{target_name}每次受到伤害+{x}，持续∞",
        }

    @staticmethod
    def calculate_dianjin(x: int) -> dict:
        """点金X：消耗8X法力，获得X个碎片

        DM裁定 2026-09-10：前身【洗劫】是"造成伤害时夺取目标等量碎片"，挂在杀伐
        伤害上，等于白送的经济水龙头。改为与伤害彻底脱钩——想要钱就得花法力，
        而攻力=当前法力，花法力直接压低普攻输出，于是这是一笔明码标价的转换。
        刻意不返回 duration 键：通用状态块以 "duration" in calc 为前提
        （combat.py:3388），带上就会凭空长出一个【点金】状态。
        注意：状态【洗劫】及其"夺碎片"机制**保留**，仍由【帮派令】在[战始]发放；
        事件收益在 报告.md「当前禁区清单」内，不动。
        """
        return {
            "dao_wen": "点金",
            "x": x,
            "cost_type": CostType.MANA.value,
            # 2026-09-17 用户令：定为 8X（历史曾一度下调为 3X，现按用户裁定改回，
            # 与 docstring/summary/正文 的 8X 一致）。DM裁定 2026-09-10 设计意图：
            # 想要钱就得花法力，而[攻击力]=当前法力，花法力直接压低普攻输出，
            # 是一笔明码标价的转换。
            "cost": 8 * x,
            "shard_gain": x,
            "summary": f"消耗{8*x}法力，获得{x}个碎片"
        }
    
    # ---- 罪孽都市专属道纹 ----
    
    @staticmethod
    def calculate_bizhai(x: int, target: Entity = None) -> dict:
        """逼债X：消耗X。[回始]使目标失去X点碎片，无力支付的部分记为负债（碎片扣负），持续∞（DM裁定D 2026-08-22：旧"否则失去2X点血限"废止）"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "逼债", "x": x, "cost_type": CostType.MANA.value, "cost": x,
            "bizhai_register": x, "duration": -1,
            "summary": f"消耗{x}法力，[回始]使{target_name}失去{x}碎片（无力支付部分记为负债），持续∞"
        }
    
    @staticmethod
    def calculate_dikou(x: int, target: Entity = None) -> dict:
        """抵扣X：消耗3X。封印目标拥有的一件遗物，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "抵扣", "x": x, "cost_type": CostType.MANA.value, "cost": 3 * x,
            "relic_seal": 1, "duration": x,
            "summary": f"消耗{3 * x}法力，封印{target_name}一件遗物，持续{x}回合"
        }
    
    @staticmethod
    def calculate_qingsuan(x: int, target: Entity = None, caster_shards: int = 0) -> dict:
        """清算X：消耗2X。[回始]使目标失去你碎片点格挡，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "清算", "x": x, "cost_type": CostType.MANA.value, "cost": 2 * x,
            "qingsuan_register": True, "duration": x,
            "summary": f"消耗{2 * x}法力，[回始]使{target_name}失去{caster_shards}格挡，持续{x}回合"
        }
    
    @staticmethod
    def calculate_shujin(x: int, target: Entity = None) -> dict:
        """赎金X：消耗3X。夺取目标10X碎片；若无碎片则失去X点速度"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "赎金", "x": x, "cost_type": CostType.MANA.value, "cost": 3 * x,
            "shard_steal": 10 * x, "speed_penalty": x,
            "summary": f"消耗{3 * x}法力，夺取{target_name} {10*x}碎片或{x}速度"
        }
    
    @staticmethod
    def calculate_jiachao(x: int) -> dict:
        """假钞X：消耗X。获得10X假碎片"""
        return {
            "dao_wen": "假钞", "x": x, "cost_type": CostType.MANA.value, "cost": x,
            "fake_shards": 10 * x,
            "summary": f"消耗{x}法力，获得{10*x}假碎片"
        }
    
    @staticmethod
    def calculate_duming(x: int) -> dict:
        """赌命X：消耗X假碎片。[回始]按存活角色投随机数，对应目标失去30%当前生命，持续X"""
        return {
            "dao_wen": "赌命", "x": x, "cost_type": "假碎片", "fake_cost": x,
            "duming_hp_pct": 30, "duration": x,
            "summary": f"消耗{x}假碎片，[回始]随机目标失去30%当前生命，持续{x}回合"
        }
    
    @staticmethod
    def calculate_xiaozai(x: int) -> dict:
        """消灾X：消耗50X假碎片/5X碎片（局外发动消耗×2）。重置随机数X次以改变结果"""
        return {
            "dao_wen": "消灾", "x": x, "cost_type": "碎片",
            "fake_cost": 50 * x, "real_cost": 5 * x,
            "rerolls": x,
            "summary": f"消耗{50*x}假碎片或{5*x}碎片（局外×2），重置随机数{x}次"
        }
    
    # ---- 龙心谷专属道纹 ----
    
    @staticmethod
    def calculate_longlin(x: int, target: Entity = None) -> dict:
        """龙鳞X：消耗2X。使目标每次受到伤害-X，最低为0，持续∞"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "龙鳞", "x": x, "cost_type": CostType.MANA.value, "cost": 2 * x,
            "damage_reduction": x, "duration": -1,
            "summary": f"消耗{2 * x}法力，{target_name}每次受伤-{x}(最低0)，永久"
        }
    
    @staticmethod
    def calculate_nilin(x: int, target: Entity = None) -> dict:
        """逆鳞X：代价：流血X。目标每失去1生命获得1层逆鳞，下次伤害+全部层数，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "逆鳞", "x": x, "cost_type": CostType.BLEED.value, "cost_hp": x,
            "stack_per_hp": 1, "duration": x,
            "summary": f"流血{x}，{target_name}每掉1HP积1层逆鳞，下次伤害+全部层数"
        }
    
    @staticmethod
    def calculate_huoxue(x: int, target: Entity = None) -> dict:
        """活血X：消耗X。目标每累计失去2生命，回终获得回复1，持续X"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "活血", "x": x, "cost_type": CostType.MANA.value, "cost": x,
            "heal_per_2hp": 1, "duration": x,
            "summary": f"消耗{x}法力，{target_name}每失去2HP回终回复1，持续{x}回合"
        }
    
    @staticmethod
    def calculate_liebian(x: int, target: Entity = None) -> dict:
        """裂变X：消耗2X。使目标受到伤害改为分X次结算，持续∞"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "裂变", "x": x, "cost_type": CostType.MANA.value, "cost": 2 * x,
            "split_count": x, "duration": -1,
            "summary": f"消耗{2 * x}法力，{target_name}受伤分{x}次结算，永久"
        }
    
    @staticmethod
    def calculate_jiahuo(x: int, target: Entity = None) -> dict:
        """嫁祸X：消耗4X。自身下X次受到伤害由目标承担"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "嫁祸", "x": x, "cost_type": CostType.MANA.value, "cost": 4 * x,
            "redirect_count": x,
            "summary": f"消耗{4 * x}法力，自身下{x}次受伤由{target_name}承担"
        }
    
    @staticmethod
    def calculate_beifu(x: int, target: Entity = None) -> dict:
        """背负X：消耗2X。选择目标，其下X次受到伤害由自身承担"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "背负", "x": x, "cost_type": CostType.MANA.value, "cost": 2 * x,
            "absorb_count": x,
            "summary": f"消耗{2 * x}法力，{target_name}下{x}次受伤由自身承担"
        }
    
    @staticmethod
    def calculate_shanghen(x: int, target: Entity = None) -> dict:
        """伤痕X：消耗2X。使目标每次失去生命后血限-X，持续∞"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "伤痕", "x": x, "cost_type": CostType.MANA.value, "cost": 2 * x,
            "blood_limit_loss": x, "duration": -1,
            "summary": f"消耗{2 * x}法力，{target_name}每次掉血后血限-{x}，永久"
        }
    
    # ... 其他道纹可按需添加

    # ========== 乱葬岗（二阶）专属道纹 ==========

    @staticmethod
    def calculate_fenlie(x: int, y: int = 1) -> dict:
        """分裂X/Y：代价：衰老X×10Y。创造X个10Y[血限]的自身复制体（2026-09-17 用户令重做）。

        双参数道纹（引擎首个）：
          X = 复制体**数量**
          Y = 单个复制体的**规模档**，每个血限/生命 = 10Y
        代价【衰老】= X×10Y，恰好等于造出来的**总血限**——造多少血就付多少
        血限，不会凭空增殖，也不会因为本体血限高低而白赚或白亏。

        旧版是「代价：冷却X；[命零]时创造X个本体血限20%的复制体」：触发时机
        绑死在[命零]（只能死后发动，本体血限越高越赚，且无法主动使用），
        代价冷却与产出无关，本体血限高的怪能无限白嫖。新版改为即时结算。

        调用：DaoWenEngine.resolve("分裂", x, y=Y)；不传 y 时默认 1。
        """
        clone_hp = 10 * y
        return {
            "dao_wen": "分裂", "x": x, "y": y,
            "cost_type": CostType.AGING.value, "cost_blood_limit": x * clone_hp,
            "split_clones": x, "clone_hp": clone_hp,
            "summary": f"衰老{x * clone_hp}，创造{x}个{clone_hp}血限的自身复制体"
        }

    @staticmethod
    def calculate_shibao(x: int) -> dict:
        """尸爆X：消耗3X。[命零]对所有敌方[目标]打出自身[血限]的10X%伤害。"""
        return {
            "dao_wen": "尸爆", "x": x,
            "cost_type": CostType.MANA.value, "cost": 3 * x,
            "self_destruct": True, "aoe_pct": 10 * x,
            "summary": f"消耗{3 * x}法力，[命零]对全体敌造成自身血限{10*x}%伤害"
        }

    @staticmethod
    def calculate_qianmo(x: int) -> dict:
        """缄默X：消耗X。使场上所有由[命零]触发的效果无法触发，持续X。"""
        return {
            "dao_wen": "缄默", "x": x,
            "cost_type": CostType.MANA.value, "cost": x,
            "duration": x, "silence_death_triggers": True,
            "summary": f"消耗{x}法力，封禁全场[命零]触发效果，持续{x}回合"
        }

    @staticmethod
    def calculate_wajie(x: int, target: Entity = None) -> dict:
        """瓦解X：消耗3X。使一个[目标]的[血限]减少10X%。"""
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "瓦解", "x": x,
            "cost_type": CostType.MANA.value, "cost": 3 * x,
            "blood_limit_pct": 10 * x,
            "summary": f"消耗{3 * x}法力，{target_name}血限-{10*x}%"
        }

    @staticmethod
    def calculate_mingqi(x: int, target: Entity = None) -> dict:
        """冥气X：消耗2X。[目标]每失去一次速度[速限]-2，持续X。

        修复（2026-08-21）：补上 target 参数使该道纹正确声明需要[目标]，
        否则 requires_target=False 导致怪物只能自施（实战：红嫁衣鬼冥气自施）。
        效果数值与消耗不变。
        """
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "冥气", "x": x,
            "cost_type": CostType.MANA.value, "cost": 2 * x,
            "speed_loss_speed_limit": 2, "duration": x,
            "summary": f"消耗{2 * x}法力，{x}回合内{target_name}每失去速度速限-2"
        }

    @staticmethod
    def calculate_gouhun(x: int, target: Entity = None) -> dict:
        """勾魂X：消耗X。使[目标]无法获得[法力]，持续X。

        改版（2026-08-30，DM 裁定见 报告.md 硬伤2-C）：
        旧版为「[回始]使[目标]失去2X点当前法力，持续∞」——永久扣法力对输出决策
        是单向碾压，且玩家只能靠残韵改掉怪物道纹来止损。新版改为**持续X回合
        无法获得法力**：[回始]法力回填被压制（不扣已有法力），X 回合后自然恢复。
        这样威胁是"暂时断蓝"而非"永久死刑"，且期限明确（与【镇尸】禁回复同构）。

        修复（2026-08-21）：补上 target 参数使该道纹正确声明需要[目标]，
        否则 requires_target=False 导致怪物只能自施（寄骨蝇勾魂自吸无法力=空放）。
        """
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "勾魂", "x": x,
            "cost_type": CostType.MANA.value, "cost": x,
            # DM裁定 2026-09-09：法力改一池制（[战始]给满、[回始]不回填、[战终]复原）后，
            # 「[回始]无法获得法力」失去作用对象，改为**目标消耗法力翻倍**。
            "mana_cost_multiplier": 2, "duration": x,
            "summary": f"消耗{x}法力，{target_name}法力消耗翻倍，持续{x}回合"
        }

    @staticmethod
    def calculate_zhenshi(x: int, target: Entity = None) -> dict:
        """镇尸X：消耗2X。使一个[目标]无法获得[回复]，持续X。

        修复（2026-08-21）：补上 target 参数使该道纹正确声明需要[目标]，
        否则 requires_target=False 导致怪物只能自施（实战：血僵镇尸自禁回复）。
        效果数值与消耗不变。
        """
        target_name = target.name if target is not None else "未选定目标"
        return {
            "dao_wen": "镇尸", "x": x,
            "cost_type": CostType.MANA.value, "cost": 2 * x,
            "duration": x, "no_heal": True,
            "summary": f"消耗{2 * x}法力，{target_name}无法获得回复，持续{x}回合"
        }

    @staticmethod
    def calculate_zhaohun(x: int) -> dict:
        """招魂X：消耗3X。唤回1具已击灭的怪物尸体作为[临时朋友]，生命为20X。"""
        return {
            "dao_wen": "招魂", "x": x,
            "cost_type": CostType.MANA.value, "cost": 3 * x,
            "revive_temp_friend": True, "temp_hp": 20 * x,
            "summary": f"消耗{3 * x}法力，唤回1具已灭怪物作为临时朋友（生命{20*x}）"
        }

    # ========== 统一调度入口 ==========
    
    _registry: dict = {}  # 运行时注册
    
    @classmethod
    def register_all(cls):
        """注册所有道纹计算函数"""
        cls._registry = {
            "杀伐": cls.calculate_shaifa,
            "再生": cls.calculate_zaisheng,
            "庇护": cls.calculate_bihu,
            "固执": cls.calculate_guzhi,
            "血债": cls.calculate_xuezhai,
            "波及": cls.calculate_boba,
            "增殖": cls.calculate_zengzhi,
            "束缚": cls.calculate_shufu,
            "透支": cls.calculate_touzhi,
            "贯穿": cls.calculate_guanchuan,
            "封印": cls.calculate_fengyin,
            # 怪物原始
            "狂暴": cls.calculate_kuangbao,
            "全力": cls.calculate_quanli,
            "疯狂": cls.calculate_huoli,
            "净化": cls.calculate_jinghua,
            "减速": cls.calculate_jiansu,
            "必中": cls.calculate_bizhong,
            "自愈": cls.calculate_ziyu,
            "飞行": cls.calculate_feixing,
            # 怪物转化
            "愤怒": cls.calculate_fennu,
            "自残": cls.calculate_zican,
            "无神": cls.calculate_wushen,
            "借力": cls.calculate_jieli,
            "弱化": cls.calculate_ruhua,
            "自食": cls.calculate_zishi,
            "兴奋": cls.calculate_xingfen,
            "无力": cls.calculate_wuli,
            "全速": cls.calculate_quansu,
            "急速": cls.calculate_jisu,
            "加速": cls.calculate_jiasu,
            "眩晕": cls.calculate_xuanyun,
            "洞察": cls.calculate_dongcha,
            "蒙蔽": cls.calculate_mengbi,
            "滋养": cls.calculate_ziyang,
            "衰败": cls.calculate_shuaibai,
            "寄生": cls.calculate_jisheng,
            "滑翔": cls.calculate_huaxiang,
            "坠落": cls.calculate_zhuiluo,
            # 扭曲都市
            "变形": cls.calculate_bianxing,
            "定型": cls.calculate_dingxing,
            "畸变": cls.calculate_jibian,
            "搏命": cls.calculate_boming,
            "超频": cls.calculate_chaopin,
            "坏死": cls.calculate_huaisi,
            "爆裂": cls.calculate_baolie,
            "退化": cls.calculate_tuihua,
            # 罪孽都市
            "加害": cls.calculate_jiahai,
            "点金": cls.calculate_dianjin,
            "逼债": cls.calculate_bizhai,
            "抵扣": cls.calculate_dikou,
            "清算": cls.calculate_qingsuan,
            "赎金": cls.calculate_shujin,
            "假钞": cls.calculate_jiachao,
            "赌命": cls.calculate_duming,
            "消灾": cls.calculate_xiaozai,
            # 龙心谷
            "龙鳞": cls.calculate_longlin,
            "逆鳞": cls.calculate_nilin,
            "活血": cls.calculate_huoxue,
            "裂变": cls.calculate_liebian,
            "嫁祸": cls.calculate_jiahuo,
            "背负": cls.calculate_beifu,
            "伤痕": cls.calculate_shanghen,
            # ---- 乱葬岗（二阶）----
            "分裂": cls.calculate_fenlie,
            "尸爆": cls.calculate_shibao,
            "缄默": cls.calculate_qianmo,
            "瓦解": cls.calculate_wajie,
            "冥气": cls.calculate_mingqi,
            "勾魂": cls.calculate_gouhun,
            "镇尸": cls.calculate_zhenshi,
            "招魂": cls.calculate_zhaohun,
        }
    
    @staticmethod
    def single_round_action_count(entity: Entity) -> int:
        """本回合单轮出手预算（供判定类效果使用），禁止用攻击次数冒充。"""
        if entity is None:
            return 0
        if getattr(entity, "entity_type", "") == "怪物":
            n = 2  # 1 攻 + 1 纹
            n += entity.get_status_value("疯狂")
            if entity.has_status("狂暴"):
                n += 1
            n -= entity.get_status_value("无力")
            return max(0, n)
        # 【无力】已在 Entity.action_count 属性内扣减（【高爆手雷】也走这条），此处不重复扣。
        return max(0, entity.action_count)

    @classmethod
    def resolve(cls, dao_wen_name: str, x: int, **kwargs) -> dict:
        """
        统一道纹计算入口
        AI必须通过此接口调用，禁止自行计算
        自动检查X上限
        """
        if not cls._registry:
            cls.register_all()
        
        if dao_wen_name not in cls._registry:
            raise ValueError(f"未知道纹: {dao_wen_name}。可用道纹: {list(cls._registry.keys())}")
        
        # 退化X（扭曲都市专属，持续∞）：使目标每次发动道纹时该次数值-X（最低0）。
        # 在 resolve 统一入口削减（玩家/怪物/同伴所有发动路径都经此），与 sim/balance_sim eff_x 同口径。
        caster = kwargs.get("caster")
        if caster is not None and hasattr(caster, "has_status") and caster.has_status("退化"):
            x = max(0, x - caster.get_status_value("退化"))

        # 获取该道纹的代价类型，检查X上限
        # 先调用一次获取cost_type
        func = cls._registry[dao_wen_name]
        import inspect
        sig = inspect.signature(func)
        params = {}
        for param_name in sig.parameters:
            if param_name in kwargs:
                params[param_name] = kwargs[param_name]
            elif param_name == 'x':
                params['x'] = x
        
        result = func(**params)
        
        # 检查X上限
        cost_type = result.get("cost_type", "消耗")
        if cost_type in cls.X_LIMITS:
            state = kwargs.get("_state", {})
            x_max = cls.X_LIMITS[cost_type](state)
            if x > x_max:
                raise ValueError(
                    f"X={x}超过上限{cost_type}≤{x_max}。"
                    f"道纹: {dao_wen_name}, 代价类型: {cost_type}"
                )
        
        return result
    
    @classmethod
    def list_all(cls) -> list[str]:
        """列出所有已注册道纹"""
        if not cls._registry:
            cls.register_all()
        return list(cls._registry.keys())


# 残韵系统
class ResonanceEngine:
    """
    残韵计算引擎
    转换（平向支流）：平移法则维度
    反转（极性对冲）：逆转因果极性
    曲解（概念腐化）：扭曲代数逻辑
    """
    
    # 闭环结构定义
    CLOSED_LOOPS = {
        "杀伐闭环": [
            ("杀伐", "反转", "再生"),
            ("再生", "曲解", "庇护"),
            ("庇护", "曲解", "固执"),
            ("固执", "反转", "血债"),
            ("血债", "转换", "波及"),
            # 删除慈悲/切割/缓慢后直连（2026-08-21）：
            # 波及→(反转)增殖：扩散伤害与成长的极性对冲；贯穿→(曲解)束缚：概念腐化。
            ("波及", "反转", "增殖"),
            ("增殖", "曲解", "透支"),
            ("透支", "转换", "贯穿"),
            ("贯穿", "曲解", "束缚"),
            ("束缚", "曲解", "封印"),
            ("封印", "反转", "杀伐"),
        ],
        # ---- 副本专属闭环（规则正文·副本专属道纹网络）----
        "扭曲都市闭环": [
            ("变形", "转换", "定型"),
            ("定型", "反转", "畸变"),
            ("畸变", "曲解", "超频"),
            ("超频", "反转", "搏命"),
            ("搏命", "转换", "坏死"),
            ("坏死", "曲解", "爆裂"),
            ("爆裂", "曲解", "退化"),
            ("退化", "转换", "变形"),
        ],
        "罪孽都市闭环": [
            ("点金", "转换", "逼债"),
            ("逼债", "反转", "抵扣"),
            ("抵扣", "曲解", "清算"),
            ("清算", "反转", "赎金"),
            ("赎金", "转换", "假钞"),
            ("假钞", "曲解", "赌命"),
            ("赌命", "反转", "消灾"),
            ("消灾", "曲解", "点金"),
        ],
        "龙心谷闭环": [
            ("加害", "反转", "龙鳞"),
            ("龙鳞", "曲解", "逆鳞"),
            ("逆鳞", "转换", "活血"),
            ("活血", "曲解", "裂变"),
            ("裂变", "转换", "嫁祸"),
            ("嫁祸", "反转", "背负"),
            ("背负", "曲解", "伤痕"),
            ("伤痕", "转换", "加害"),
        ],
        "乱葬岗闭环": [
            ("分裂", "转换", "尸爆"),
            ("尸爆", "反转", "缄默"),
            ("缄默", "曲解", "瓦解"),
            ("瓦解", "转换", "冥气"),
            ("冥气", "反转", "勾魂"),
            ("勾魂", "曲解", "镇尸"),
            ("镇尸", "曲解", "招魂"),
            ("招魂", "转换", "分裂"),
        ],
        # ---- 原始怪物道纹 → 转化道纹（规则正文）----
        # 非闭环，是以原始道纹为根的分支树；怪物面板上的道纹多属此类，
        # 补齐后残韵才能作用于怪物（此前对必中/狂暴/飞行发动必然失败）。
        "怪物原始道纹": [
            ("狂暴", "转换", "愤怒"),
            ("狂暴", "反转", "自残"),
            ("狂暴", "曲解", "无神"),
            ("全力", "转换", "借力"),
            ("全力", "反转", "弱化"),
            ("全力", "曲解", "自食"),
            ("疯狂", "转换", "兴奋"),
            ("疯狂", "反转", "无力"),
            ("疯狂", "曲解", "全速"),
            ("减速", "转换", "急速"),
            ("减速", "反转", "加速"),
            ("减速", "曲解", "眩晕"),
            ("必中", "转换", "洞察"),
            ("必中", "反转", "蒙蔽"),
            ("自愈", "转换", "滋养"),
            ("自愈", "反转", "衰败"),
            ("自愈", "曲解", "寄生"),
            ("飞行", "转换", "滑翔"),
            ("飞行", "反转", "坠落"),
        ],
        # ---- 转化道纹 → 原始怪物道纹（同种残韵回溯；2026-09-18 用户裁定A）----
        # 这是裁定B「人类只能从怪物身上获得原始怪物道纹」的落地路径：两步残韵。
        #   第一步：对持有原始道纹的怪物发动残韵 → 该怪物的原始道纹永久变为转化道纹，
        #           施法者同时永久获得该转化道纹（怪物 _had_monster_daowen 已在
        #           _permanently_convert_daowen 里标记，救赎判定不受影响）。
        #   第二步：施法者对**自身持有的**该转化道纹发动同种残韵 → 它永久变回原始怪物道纹。
        #           此时 holder is actor：_permanently_convert_daowen 就地改写施法者面板，
        #           _grant_transformed_daowen 因「同名不重复」返回 False，不会产生第二份。
        # 裁定B 不被绕过：转化道纹自身也无法【学习】（api.daowen_error 的
        # MONSTER_TRANSFORM_DAOWEN 门禁），玩家手上任何一条转化道纹都只能源自某只怪物。
        # 19 条＝上面 19 条正向边的一一镜像（必中/飞行 没有「曲解」产物，故无对应回溯边）。
        "怪物原始道纹回溯": [
            ("愤怒", "转换", "狂暴"),
            ("自残", "反转", "狂暴"),
            ("无神", "曲解", "狂暴"),
            ("借力", "转换", "全力"),
            ("弱化", "反转", "全力"),
            ("自食", "曲解", "全力"),
            ("兴奋", "转换", "疯狂"),
            ("无力", "反转", "疯狂"),
            ("全速", "曲解", "疯狂"),
            ("急速", "转换", "减速"),
            ("加速", "反转", "减速"),
            ("眩晕", "曲解", "减速"),
            ("洞察", "转换", "必中"),
            ("蒙蔽", "反转", "必中"),
            ("滋养", "转换", "自愈"),
            ("衰败", "反转", "自愈"),
            ("寄生", "曲解", "自愈"),
            ("滑翔", "转换", "飞行"),
            ("坠落", "反转", "飞行"),
        ],
    }
    
    @classmethod
    def find_transformation(cls, source_daowen: str, resonance_type: str) -> Optional[str]:
        """
        查找残韵变化结果
        source_daowen: 源道纹名
        resonance_type: 残韵类型（转换/反转/曲解）
        返回：变化后的道纹名，或None（如果路径不存在）
        """
        for loop_name, edges in cls.CLOSED_LOOPS.items():
            for src, rtype, dst in edges:
                if src == source_daowen and rtype == resonance_type:
                    return dst
        return None
    
    @classmethod
    def get_available_resonance(cls, source_daowen: str) -> list[dict]:
        """获取某个道纹可用的残韵变化"""
        results = []
        for loop_name, edges in cls.CLOSED_LOOPS.items():
            for src, rtype, dst in edges:
                if src == source_daowen:
                    results.append({
                        "resonance_type": rtype,
                        "target_daowen": dst,
                        "loop": loop_name
                    })
        return results
    
    @classmethod
    def apply_resonance(
        cls, 
        source_daowen: str, 
        resonance_type: str,
        caster_has_daowen: bool,
        target_has_daowen: bool,
        resonance_stock: dict = None
    ) -> dict:
        """
        应用残韵变化
        规则：
        1. 残韵作用于任意角色拥有的道纹时，将其永久变为变化后的道纹
        2. 施法者同时永久获得变化后的道纹
        3. 通过残韵获得的道纹，X值按施法者自由控X规则自定义
        """
        # 检查路径是否存在
        target = cls.find_transformation(source_daowen, resonance_type)
        if target is None:
            return {
                "success": False,
                "error": f"道纹'{source_daowen}'不存在'{resonance_type}'路径"
            }
        
        # 检查玩家是否拥有该类型残韵
        if resonance_stock is not None:
            available = resonance_stock.get(resonance_type, 0)
            if available <= 0:
                return {
                    "success": False,
                    "error": f"没有可用的{resonance_type}残韵（当前：{resonance_stock}）"
                }
        
        return {
            "success": True,
            "source": source_daowen,
            "resonance_type": resonance_type,
            "target": target,
            "permanent_change": True,
            "caster_gets_daowen": True,
            "summary": f"【{resonance_type}】{source_daowen} → {target}"
        }


# 初始化注册
DaoWenEngine.register_all()
