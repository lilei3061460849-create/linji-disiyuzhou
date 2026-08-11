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

    # 怪物转化道纹（原始怪物道纹经残韵变化后的19个分支，与README"道纹归属规则"一致）
    # 用于"雇佣"后"发现并选择一种转化道纹"等需要从此类别中随机抽取的场景
    TRANSFORMED_DAOWEN = [
        "愤怒", "自残", "无神", "借力", "弱化", "自食", "兴奋", "无力", "迟滞",
        "急速", "加速", "眩晕", "洞察", "蒙蔽", "滋养", "衰败", "寄生", "滑翔", "坠落",
    ]

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
    def calculate_shaifa(x: int, target: Entity) -> dict:
        """杀伐X：消耗X。对[目标]造成2X点伤害"""
        cost = x
        damage = 2 * x
        return {
            "dao_wen": "杀伐",
            "x": x,
            "cost_type": "消耗",
            "cost": cost,
            "target_damage": damage,
            "damage_type": "普通",
            "summary": f"消耗{x}法力，对{target.name}造成{damage}点伤害"
        }
    
    @staticmethod
    def calculate_zaisheng(x: int, target: Entity) -> dict:
        """再生X：消耗X。为[目标]回复3X点生命"""
        cost = x
        heal = 3 * x
        return {
            "dao_wen": "再生",
            "x": x,
            "cost_type": "消耗",
            "cost": cost,
            "target_heal": heal,
            "summary": f"消耗{x}法力，为{target.name}回复{heal}点生命"
        }
    
    @staticmethod
    def calculate_bihu(x: int, target: Entity) -> dict:
        """庇护X：消耗X。使[目标]获得4X点格挡"""
        cost = x
        shield = 4 * x
        return {
            "dao_wen": "庇护",
            "x": x,
            "cost_type": "消耗",
            "cost": cost,
            "target_shield": shield,
            "summary": f"消耗{x}法力，使{target.name}获得{shield}点格挡"
        }
    
    @staticmethod
    def calculate_guzhi(x: int) -> dict:
        """固执X：代价：冷却X。自身单次失去生命最高为1，持续X"""
        return {
            "dao_wen": "固执",
            "x": x,
            "cost_type": "冷却",
            "cost": x,
            "duration": x,
            "max_life_loss_per_hit": 1,
            "summary": f"冷却{x}场，自身单次失去生命最高为1，持续{x}回合"
        }
    
    @staticmethod
    def calculate_xuezhai(x: int, target: Entity) -> dict:
        """血债X：代价：流血X。对[目标]造成2X次1点伤害"""
        cost_hp = x
        hits = 2 * x
        damage_per_hit = 1
        return {
            "dao_wen": "血债",
            "x": x,
            "cost_type": "流血",
            "cost_hp": cost_hp,
            "hits": hits,
            "damage_per_hit": damage_per_hit,
            "total_damage": hits * damage_per_hit,
            "summary": f"流血{x}，对{target.name}造成{hits}次{damage_per_hit}点伤害"
        }
    
    @staticmethod
    def calculate_chongji(x: int) -> dict:
        """冲击X：消耗X。对所有敌对[目标]造成X点伤害"""
        return {
            "dao_wen": "冲击",
            "x": x,
            "cost_type": "消耗",
            "cost": x,
            "aoe_damage": x,
            "target": "all_enemies",
            "summary": f"消耗{x}法力，对所有敌方造成{x}点伤害"
        }
    
    @staticmethod
    def calculate_cibei(x: int, target: Entity) -> dict:
        """慈悲X：代价：流血X。为[目标]回复X点生命"""
        return {
            "dao_wen": "慈悲",
            "x": x,
            "cost_type": "流血",
            "cost_hp": x,
            "target_heal": x,
            "summary": f"流血{x}，为{target.name}回复{x}点生命"
        }
    
    # ---- 锐利闭环 ----
    
    @staticmethod
    def calculate_ruili(x: int, target: Entity) -> dict:
        """锐利X：消耗3X。[目标]血限及当前生命同时-4X"""
        cost = 3 * x
        reduction = 4 * x
        return {
            "dao_wen": "锐利",
            "x": x,
            "cost_type": "消耗",
            "cost": cost,
            "blood_limit_reduction": reduction,
            "hp_reduction": reduction,
            "summary": f"消耗{cost}法力，{target.name}血限与当前生命各-{reduction}"
        }
    
    @staticmethod
    def calculate_zengzhi(x: int, target: Entity) -> dict:
        """增殖X：消耗5X。[目标]血限+2X"""
        cost = 5 * x
        increase = 2 * x
        return {
            "dao_wen": "增殖",
            "x": x,
            "cost_type": "消耗",
            "cost": cost,
            "blood_limit_increase": increase,
            "summary": f"消耗{cost}法力，{target.name}血限+{increase}"
        }
    
    @staticmethod
    def calculate_shufu(x: int, target: Entity) -> dict:
        """束缚X：代价：冷却2X。使[目标]无法行动，持续X"""
        return {
            "dao_wen": "束缚",
            "x": x,
            "cost_type": "冷却",
            "cost": 2 * x,
            "duration": x,
            "effect": "无法行动",
            "summary": f"冷却{2*x}场，使{target.name}无法行动，持续{x}回合"
        }
    
    @staticmethod
    def calculate_touzhi(x: int) -> dict:
        """透支X：代价：衰老X。获得4X点法力"""
        return {
            "dao_wen": "透支",
            "x": x,
            "cost_type": "衰老",
            "cost_blood_limit": x,
            "mana_gain": 4 * x,
            "summary": f"衰老{x}(血限-{x})，获得{4*x}点法力"
        }
    
    @staticmethod
    def calculate_guanchuan(x: int) -> dict:
        """贯穿X：消耗5X。你造成的伤害无视格挡，持续X"""
        return {
            "dao_wen": "贯穿",
            "x": x,
            "cost_type": "消耗",
            "cost": 5 * x,
            "duration": x,
            "effect": "伤害无视格挡",
            "summary": f"消耗{5*x}法力，造成的伤害无视格挡，持续{x}回合"
        }
    
    @staticmethod
    def calculate_fengyin(x: int) -> dict:
        """封印X：消耗10X。使X个目标怪物移出本场战斗"""
        return {
            "dao_wen": "封印",
            "x": x,
            "cost_type": "消耗",
            "cost": 10 * x,
            "targets_removed": x,
            "note": "被移出的怪物不提供任何碎片收益",
            "summary": f"消耗{10*x}法力，使{x}个目标怪物移出本场战斗"
        }
    
    @staticmethod
    def calculate_manqian(x: int, target: Entity, target_action_count: int) -> dict:
        """缓慢X：消耗10X。本回合若[目标]单轮出手次数≤X，则其无法出手"""
        cost = 10 * x
        effective = target_action_count <= x
        return {
            "dao_wen": "缓慢",
            "x": x,
            "cost_type": "消耗",
            "cost": cost,
            "target_action_count": target_action_count,
            "effective": effective,
            "summary": f"消耗{cost}法力，{'生效' if effective else '未生效'}（{target.name}出手{target_action_count}次，阈值{x}）"
        }
    
    # ---- 怪物原始道纹 ----
    
    @staticmethod
    def calculate_kuangbao(x: int) -> dict:
        """狂暴X：代价：异变5X。回始发动一轮额外攻击，持续X"""
        return {
            "dao_wen": "狂暴",
            "x": x,
            "cost_type": "异变",
            "cost_mutation": 5 * x,
            "duration": x,
            "effect": "回始发动一轮额外攻击",
            "summary": f"异变+{5*x}，回始发动一轮额外攻击，持续{x}回合"
        }
    
    @staticmethod
    def calculate_qianghua(x: int, target: Entity) -> dict:
        """强化X：代价：异变5X。使[目标]攻击力+X，持续∞"""
        return {
            "dao_wen": "强化",
            "x": x,
            "cost_type": "异变",
            "cost_mutation": 5 * x,
            "attack_boost": x,
            "duration": -1,  # ∞
            "summary": f"异变+{5*x}，使{target.name}攻击力+{x}，永久"
        }
    
    @staticmethod
    def calculate_huoli(x: int, target: Entity) -> dict:
        """活力X：代价：异变5X。使[目标]出手次数+X，持续∞"""
        return {
            "dao_wen": "活力",
            "x": x,
            "cost_type": "异变",
            "cost_mutation": 5 * x,
            "action_boost": x,
            "duration": -1,
            "summary": f"异变+{5*x}，使{target.name}出手次数+{x}，永久"
        }
    
    @staticmethod
    def calculate_jiansu(x: int, target: Entity) -> dict:
        """减速X：代价：异变5X。使[目标]速度减半，持续X"""
        return {
            "dao_wen": "减速",
            "x": x,
            "cost_type": "异变",
            "cost_mutation": 5 * x,
            "speed_halved": True,
            "duration": x,
            "summary": f"异变+{5*x}，使{target.name}速度减半，持续{x}回合"
        }
    
    @staticmethod
    def calculate_bizhong(x: int) -> dict:
        """必中X：代价：异变5X。自身下X次攻击附带必中"""
        return {
            "dao_wen": "必中",
            "x": x,
            "cost_type": "异变",
            "cost_mutation": 5 * x,
            "guaranteed_hits": x,
            "summary": f"异变+{5*x}，自身下{x}次攻击附带必中"
        }
    
    @staticmethod
    def calculate_ziyu(x: int) -> dict:
        """自愈X：代价：异变5X。回始获得自身血限10X%的回复，持续∞"""
        return {
            "dao_wen": "自愈",
            "x": x,
            "cost_type": "异变",
            "cost_mutation": 5 * x,
            "heal_percent": 10 * x,
            "duration": -1,
            "summary": f"异变+{5*x}，回始获得自身血限{10*x}%的回复，永久"
        }
    
    @staticmethod
    def calculate_feixing(x: int) -> dict:
        """飞行X：代价：异变5X。无法被非飞行角色选为目标，持续X"""
        return {
            "dao_wen": "飞行",
            "x": x,
            "cost_type": "异变",
            "cost_mutation": 5 * x,
            "duration": x,
            "effect": "无法被非飞行角色选为目标",
            "summary": f"异变+{5*x}，无法被非飞行角色选为目标，持续{x}回合"
        }
    
    # ---- 怪物转化道纹 ----
    
    @staticmethod
    def calculate_fennu(x: int, target: Entity) -> dict:
        """愤怒X：消耗5X。使[目标]法力消耗减半，持续X"""
        return {
            "dao_wen": "愤怒",
            "x": x,
            "cost_type": "消耗",
            "cost": 5 * x,
            "mana_cost_halved": True,
            "duration": x,
            "summary": f"消耗{5*x}法力，使{target.name}法力消耗减半，持续{x}回合"
        }
    
    @staticmethod
    def calculate_zican(x: int, target: Entity) -> dict:
        """自残X：消耗10X。使[目标]对其自身打出X次攻击"""
        return {
            "dao_wen": "自残",
            "x": x,
            "cost_type": "消耗",
            "cost": 10 * x,
            "self_attack_count": x,
            "summary": f"消耗{10*x}法力，使{target.name}对自身打出{x}次攻击"
        }
    
    @staticmethod
    def calculate_wushen(x: int, target: Entity) -> dict:
        """无神X：消耗20X。使[目标]选择目标时强制改为自身，持续X"""
        return {
            "dao_wen": "无神",
            "x": x,
            "cost_type": "消耗",
            "cost": 20 * x,
            "duration": x,
            "effect": "选择目标时强制改为自身",
            "summary": f"消耗{20*x}法力，使{target.name}选择目标时强制改为自身，持续{x}回合"
        }
    
    @staticmethod
    def calculate_jieli(x: int, target: Entity) -> dict:
        """借力X：消耗10X。使[目标]造成伤害+10X%，持续∞"""
        return {
            "dao_wen": "借力",
            "x": x,
            "cost_type": "消耗",
            "cost": 10 * x,
            "damage_boost_percent": 10 * x,
            "duration": -1,
            "summary": f"消耗{10*x}法力，使{target.name}造成伤害+{10*x}%，永久"
        }
    
    @staticmethod
    def calculate_ruhua(x: int, target: Entity) -> dict:
        """弱化X：消耗3X。使[目标]攻击力-X，持续∞"""
        return {
            "dao_wen": "弱化",
            "x": x,
            "cost_type": "消耗",
            "cost": 3 * x,
            "attack_reduction": x,
            "duration": -1,
            "summary": f"消耗{3*x}法力，使{target.name}攻击力-{x}，永久"
        }
    
    @staticmethod
    def calculate_zishi(x: int, target: Entity) -> dict:
        """自食X：消耗X。将自身X点攻击力转化为等量回复"""
        return {
            "dao_wen": "自食",
            "x": x,
            "cost_type": "消耗",
            "cost": x,
            "attack_reduction": x,
            "heal": x,
            "summary": f"消耗{x}法力，将自身{x}攻击力转化为{x}点回复"
        }
    
    @staticmethod
    def calculate_xingfen(x: int, target: Entity) -> dict:
        """兴奋X：消耗5X。使[目标]每次出手后速度+1，持续X"""
        return {
            "dao_wen": "兴奋",
            "x": x,
            "cost_type": "消耗",
            "cost": 5 * x,
            "speed_gain_per_action": 1,
            "duration": x,
            "summary": f"消耗{5*x}法力，使{target.name}每次出手后速度+1，持续{x}回合"
        }
    
    @staticmethod
    def calculate_wuli(x: int, target: Entity) -> dict:
        """无力X：消耗10X。回始使[目标]出手次数-X，持续∞"""
        return {
            "dao_wen": "无力",
            "x": x,
            "cost_type": "消耗",
            "cost": 10 * x,
            "action_reduction": x,
            "duration": -1,
            "summary": f"消耗{10*x}法力，回始使{target.name}出手次数-{x}，永久"
        }
    
    @staticmethod
    def calculate_chizhi(x: int, target: Entity) -> dict:
        """迟滞X：代价：冷却X。使[目标]攻击次数固定为1，持续X"""
        return {
            "dao_wen": "迟滞",
            "x": x,
            "cost_type": "冷却",
            "cost": x,
            "attack_count_fixed": 1,
            "duration": x,
            "summary": f"冷却{x}场，使{target.name}攻击次数固定为1，持续{x}回合"
        }
    
    @staticmethod
    def calculate_jisu(x: int, target: Entity) -> dict:
        """急速X：消耗20X。使[目标]每闪避两次速度+1，持续X"""
        return {
            "dao_wen": "急速",
            "x": x,
            "cost_type": "消耗",
            "cost": 20 * x,
            "speed_per_2_dodges": 1,
            "duration": x,
            "summary": f"消耗{20*x}法力，使{target.name}每闪避两次速度+1，持续{x}回合"
        }
    
    @staticmethod
    def calculate_jiasu(x: int, target: Entity) -> dict:
        """加速X：消耗20X。使[目标]获得的速度翻倍，持续X"""
        return {
            "dao_wen": "加速",
            "x": x,
            "cost_type": "消耗",
            "cost": 20 * x,
            "speed_doubled": True,
            "duration": x,
            "summary": f"消耗{20*x}法力，使{target.name}获得的速度翻倍，持续{x}回合"
        }
    
    @staticmethod
    def calculate_xuanyun(x: int, target: Entity) -> dict:
        """眩晕X：消耗20X。使[目标]无法出手，受到伤害后解除，持续X"""
        return {
            "dao_wen": "眩晕",
            "x": x,
            "cost_type": "消耗",
            "cost": 20 * x,
            "duration": x,
            "effect": "无法出手，受到伤害后解除",
            "summary": f"消耗{20*x}法力，使{target.name}无法出手，受伤害后解除，持续{x}回合"
        }
    
    @staticmethod
    def calculate_dongcha(x: int, target: Entity) -> dict:
        """洞察X：代价：疲惫X。使[目标]每次闪避后下回合法力+10，持续X"""
        return {
            "dao_wen": "洞察",
            "x": x,
            "cost_type": "疲惫",
            "cost_speed": x,
            "mana_per_dodge": 10,
            "duration": x,
            "summary": f"疲惫{x}，使{target.name}每次闪避后下回合法力+10，持续{x}回合"
        }
    
    @staticmethod
    def calculate_mengbi(x: int, target: Entity) -> dict:
        """蒙蔽X：消耗5X。使[目标]下X次造成的伤害无效"""
        return {
            "dao_wen": "蒙蔽",
            "x": x,
            "cost_type": "消耗",
            "cost": 5 * x,
            "invalid_damage_hits": x,
            "summary": f"消耗{5*x}法力，使{target.name}下{x}次造成的伤害无效"
        }
    
    @staticmethod
    def calculate_ziyang(x: int, target: Entity) -> dict:
        """滋养X：消耗5X。使[目标]获得血限10X%的回复"""
        cost = 5 * x
        blood_limit = target.blood_limit
        heal = DaoWenEngine.ceil(blood_limit * 10 * x / 100)
        return {
            "dao_wen": "滋养",
            "x": x,
            "cost_type": "消耗",
            "cost": cost,
            "heal": heal,
            "summary": f"消耗{cost}法力，使{target.name}获得{heal}点回复（血限{blood_limit}的{10*x}%）"
        }
    
    @staticmethod
    def calculate_shuaibai(x: int, target: Entity) -> dict:
        """衰败X：消耗15X。对[目标]造成10X%当前生命的伤害，持续∞"""
        cost = 15 * x
        current_hp = target.current_hp
        damage = DaoWenEngine.ceil(current_hp * 10 * x / 100)
        return {
            "dao_wen": "衰败",
            "x": x,
            "cost_type": "消耗",
            "cost": cost,
            "damage": damage,
            "duration": -1,
            "summary": f"消耗{cost}法力，对{target.name}造成{damage}点伤害（当前生命{current_hp}的{10*x}%），永久"
        }
    
    @staticmethod
    def calculate_jisheng(x: int, target: Entity, caster: Entity) -> dict:
        """寄生X：消耗10X。使[目标]受到的伤害20X%转化为施法者的回复，持续∞"""
        return {
            "dao_wen": "寄生",
            "x": x,
            "cost_type": "消耗",
            "cost": 10 * x,
            "drain_percent": 20 * x,
            "duration": -1,
            "summary": f"消耗{10*x}法力，使{target.name}受到伤害的{20*x}%转化为{caster.name}的回复，永久"
        }
    
    @staticmethod
    def calculate_huaxiang(x: int) -> dict:
        """滑翔X：消耗5X。获得飞行，持续X"""
        return {
            "dao_wen": "滑翔",
            "x": x,
            "cost_type": "消耗",
            "cost": 5 * x,
            "duration": x,
            "effect": "获得飞行",
            "summary": f"消耗{5*x}法力，获得飞行，持续{x}回合"
        }
    
    @staticmethod
    def calculate_zhuiluo(x: int) -> dict:
        """坠落X：消耗X。所有飞行角色无法飞行且造成伤害减半，持续X"""
        return {
            "dao_wen": "坠落",
            "x": x,
            "cost_type": "消耗",
            "cost": x,
            "duration": x,
            "effect": "所有飞行角色无法飞行且造成伤害减半",
            "summary": f"消耗{x}法力，所有飞行角色无法飞行且造成伤害减半，持续{x}回合"
        }
    
    # ---- 扭曲都市专属道纹 ----
    
    @staticmethod
    def calculate_bianxing(x: int, target: Entity) -> dict:
        """变形X：消耗X。使自身攻击力与攻击次数互换，持续X"""
        return {
            "dao_wen": "变形",
            "x": x,
            "cost_type": "消耗",
            "cost": x,
            "duration": x,
            "effect": "攻击力与攻击次数互换",
            "summary": f"消耗{x}法力，攻击力与攻击次数互换，持续{x}回合"
        }
    
    @staticmethod
    def calculate_dingxing(x: int, target: Entity) -> dict:
        """定型X：消耗3X。使[目标]攻击次数与攻击力无法被改变，持续X"""
        return {
            "dao_wen": "定型",
            "x": x,
            "cost_type": "消耗",
            "cost": 3 * x,
            "duration": x,
            "effect": "攻击次数与攻击力无法被改变",
            "summary": f"消耗{3*x}法力，使{target.name}攻击次数与攻击力无法被改变，持续{x}回合"
        }
    
    @staticmethod
    def calculate_jibian(x: int, target: Entity) -> dict:
        """畸变X：代价：冷却X。回终使[目标]失去(攻击力×攻击次数)的血限，持续X"""
        atk = target.attack_count
        ap = target.attack_power
        blood_loss = atk * ap
        return {
            "dao_wen": "畸变",
            "x": x,
            "cost_type": "冷却",
            "cost": x,
            "blood_loss_per_round": blood_loss,
            "duration": x,
            "summary": f"冷却{x}场，回终使{target.name}失去{blood_loss}血限（{atk}×{ap}），持续{x}回合"
        }
    
    @staticmethod
    def calculate_jianghua(x: int, target: Entity) -> dict:
        """僵化X：消耗5X。使[目标]攻击力固定为1，持续X"""
        return {
            "dao_wen": "僵化",
            "x": x,
            "cost_type": "消耗",
            "cost": 5 * x,
            "attack_fixed": 1,
            "duration": x,
            "summary": f"消耗{5*x}法力，使{target.name}攻击力固定为1，持续{x}回合"
        }
    
    @staticmethod
    def calculate_chaopin(x: int) -> dict:
        """超频X：消耗2X。使自身速度+X"""
        return {
            "dao_wen": "超频",
            "x": x,
            "cost_type": "消耗",
            "cost": 2 * x,
            "speed_boost": x,
            "summary": f"消耗{2*x}法力，自身速度+{x}"
        }
    
    @staticmethod
    def calculate_huaisi(x: int, target: Entity) -> dict:
        """坏死X：消耗5X。使[目标]无法获得回复，持续X"""
        return {
            "dao_wen": "坏死",
            "x": x,
            "cost_type": "消耗",
            "cost": 5 * x,
            "duration": x,
            "effect": "无法获得回复",
            "summary": f"消耗{5*x}法力，使{target.name}无法获得回复，持续{x}回合"
        }
    
    @staticmethod
    def calculate_baolie(x: int) -> dict:
        """爆裂X：消耗3X。受到伤害后，攻击者失去等量生命，持续X"""
        return {
            "dao_wen": "爆裂",
            "x": x,
            "cost_type": "消耗",
            "cost": 3 * x,
            "duration": x,
            "effect": "受到伤害后，攻击者失去等量生命",
            "summary": f"消耗{3*x}法力，受到伤害后攻击者失去等量生命，持续{x}回合"
        }
    
    @staticmethod
    def calculate_tuihua(x: int, target: Entity) -> dict:
        """退化X：消耗5X。使[目标]每次发动道纹时该次数值-X(最低0)，持续∞"""
        return {
            "dao_wen": "退化",
            "x": x,
            "cost_type": "消耗",
            "cost": 5 * x,
            "dao_wen_reduction": x,
            "duration": -1,
            "summary": f"消耗{5*x}法力，使{target.name}每次发动道纹数值-{x}(最低0)，永久"
        }
    
    # ---- 罪孽都市专属道纹 ----
    
    @staticmethod
    def calculate_xijie(x: int, target: Entity) -> dict:
        """洗劫X：消耗3X。造成伤害时夺取目标等量碎片，持续X"""
        return {
            "dao_wen": "洗劫",
            "x": x,
            "cost_type": "消耗",
            "cost": 3 * x,
            "duration": x,
            "effect": "造成伤害时夺取等量碎片",
            "summary": f"消耗{3*x}法力，造成伤害时夺取{target.name}等量碎片，持续{x}回合"
        }
    
    # ---- 罪孽都市专属道纹 ----
    
    @staticmethod
    def calculate_bizhai(x: int, target: Entity) -> dict:
        """逼债X：消耗X。回始使目标失去X点碎片，否则失去2X点血限，持续∞"""
        return {
            "dao_wen": "逼债", "x": x, "cost_type": "消耗", "cost": x,
            "shard_drain": x, "blood_limit_penalty": 2 * x, "duration": -1,
            "summary": f"消耗{x}法力，回始使{target.name}失去{x}碎片或{2*x}血限，永久"
        }
    
    @staticmethod
    def calculate_dikou(x: int, target: Entity) -> dict:
        """抵扣X：消耗10X。封印目标拥有的一件遗物，持续X"""
        return {
            "dao_wen": "抵扣", "x": x, "cost_type": "消耗", "cost": 10 * x,
            "relic_seal": 1, "duration": x,
            "summary": f"消耗{10*x}法力，封印{target.name}一件遗物，持续{x}回合"
        }
    
    @staticmethod
    def calculate_qingsuan(x: int, target: Entity, caster_shards: int = 0) -> dict:
        """清算X：消耗5X。回始使目标失去你碎片点格挡，持续X"""
        return {
            "dao_wen": "清算", "x": x, "cost_type": "消耗", "cost": 5 * x,
            "shield_drain": caster_shards, "duration": x,
            "summary": f"消耗{5*x}法力，回始使{target.name}失去{caster_shards}格挡，持续{x}回合"
        }
    
    @staticmethod
    def calculate_shujin(x: int, target: Entity) -> dict:
        """赎金X：消耗10X。夺取目标10X碎片；若无碎片则失去X点速度"""
        return {
            "dao_wen": "赎金", "x": x, "cost_type": "消耗", "cost": 10 * x,
            "shard_steal": 10 * x, "speed_penalty": x,
            "summary": f"消耗{10*x}法力，夺取{target.name} {10*x}碎片或{x}速度"
        }
    
    @staticmethod
    def calculate_jiachao(x: int) -> dict:
        """假钞X：消耗X。获得10X假碎片"""
        return {
            "dao_wen": "假钞", "x": x, "cost_type": "消耗", "cost": x,
            "fake_shards": 10 * x,
            "summary": f"消耗{x}法力，获得{10*x}假碎片"
        }
    
    @staticmethod
    def calculate_duming(x: int) -> dict:
        """赌命X：消耗X假碎片。回始按存活角色投随机数，对应目标失去30%血限当前生命"""
        return {
            "dao_wen": "赌命", "x": x, "cost_type": "消耗", "cost": x,
            "hp_percent_loss": 30, "duration": x,
            "summary": f"消耗{x}假碎片，回始随机目标失去30%血限生命，持续{x}回合"
        }
    
    @staticmethod
    def calculate_xiaozai(x: int) -> dict:
        """消灾X：消耗50X假碎片或5X碎片。重置随机数X次"""
        return {
            "dao_wen": "消灾", "x": x, "cost_type": "消耗", "cost_shards": 5 * x,
            "rerolls": x,
            "summary": f"消耗{5*x}碎片，重置随机数{x}次"
        }
    
    # ---- 龙心谷专属道纹 ----
    
    @staticmethod
    def calculate_longlin(x: int, target: Entity) -> dict:
        """龙鳞X：消耗5X。使目标每次受到伤害-X，最低为0，持续∞"""
        return {
            "dao_wen": "龙鳞", "x": x, "cost_type": "消耗", "cost": 5 * x,
            "damage_reduction": x, "duration": -1,
            "summary": f"消耗{5*x}法力，{target.name}每次受伤-{x}(最低0)，永久"
        }
    
    @staticmethod
    def calculate_nilin(x: int, target: Entity) -> dict:
        """逆鳞X：代价：流血X。目标每失去1生命获得1层逆鳞，下次伤害+全部层数，持续X"""
        return {
            "dao_wen": "逆鳞", "x": x, "cost_type": "流血", "cost_hp": x,
            "stack_per_hp": 1, "duration": x,
            "summary": f"流血{x}，{target.name}每掉1HP积1层逆鳞，下次伤害+全部层数"
        }
    
    @staticmethod
    def calculate_huoxue(x: int, target: Entity) -> dict:
        """活血X：消耗2X。目标每累计失去2生命，回终获得回复1，持续X"""
        return {
            "dao_wen": "活血", "x": x, "cost_type": "消耗", "cost": 2 * x,
            "heal_per_2hp": 1, "duration": x,
            "summary": f"消耗{2*x}法力，{target.name}每失去2HP回终回复1，持续{x}回合"
        }
    
    @staticmethod
    def calculate_liebian(x: int, target: Entity) -> dict:
        """裂变X：消耗3X。使目标受到伤害改为分X次结算，持续∞"""
        return {
            "dao_wen": "裂变", "x": x, "cost_type": "消耗", "cost": 3 * x,
            "split_count": x, "duration": -1,
            "summary": f"消耗{3*x}法力，{target.name}受伤分{x}次结算，永久"
        }
    
    @staticmethod
    def calculate_jiahuo(x: int, target: Entity) -> dict:
        """嫁祸X：消耗15X。自身下X次受到伤害由目标承担"""
        return {
            "dao_wen": "嫁祸", "x": x, "cost_type": "消耗", "cost": 15 * x,
            "redirect_count": x,
            "summary": f"消耗{15*x}法力，自身下{x}次受伤由{target.name}承担"
        }
    
    @staticmethod
    def calculate_beifu(x: int, target: Entity) -> dict:
        """背负X：消耗5X。选择目标，其下X次受到伤害由自身承担"""
        return {
            "dao_wen": "背负", "x": x, "cost_type": "消耗", "cost": 5 * x,
            "absorb_count": x,
            "summary": f"消耗{5*x}法力，{target.name}下{x}次受伤由自身承担"
        }
    
    @staticmethod
    def calculate_shanghen(x: int, target: Entity) -> dict:
        """伤痕X：消耗5X。使目标每次失去生命后血限-X，持续∞"""
        return {
            "dao_wen": "伤痕", "x": x, "cost_type": "消耗", "cost": 5 * x,
            "blood_limit_loss": x, "duration": -1,
            "summary": f"消耗{5*x}法力，{target.name}每次掉血后血限-{x}，永久"
        }
    
    # ... 其他道纹可按需添加
    
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
            "冲击": cls.calculate_chongji,
            "慈悲": cls.calculate_cibei,
            "锐利": cls.calculate_ruili,
            "增殖": cls.calculate_zengzhi,
            "束缚": cls.calculate_shufu,
            "透支": cls.calculate_touzhi,
            "贯穿": cls.calculate_guanchuan,
            "封印": cls.calculate_fengyin,
            "缓慢": cls.calculate_manqian,
            # 怪物原始
            "狂暴": cls.calculate_kuangbao,
            "强化": cls.calculate_qianghua,
            "活力": cls.calculate_huoli,
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
            "迟滞": cls.calculate_chizhi,
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
            "僵化": cls.calculate_jianghua,
            "超频": cls.calculate_chaopin,
            "坏死": cls.calculate_huaisi,
            "爆裂": cls.calculate_baolie,
            "退化": cls.calculate_tuihua,
            # 罪孽都市
            "洗劫": cls.calculate_xijie,
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
        }
    
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
            ("血债", "转换", "冲击"),
            ("冲击", "曲解", "慈悲"),
            ("慈悲", "反转", "杀伐"),
        ],
        "锐利闭环": [
            ("锐利", "反转", "增殖"),
            ("增殖", "曲解", "透支"),
            ("透支", "转换", "贯穿"),
            ("贯穿", "曲解", "缓慢"),
            ("缓慢", "反转", "束缚"),
            ("束缚", "曲解", "封印"),
            ("封印", "反转", "锐利"),
        ],
        # ---- 副本专属闭环（README 第615/706/788行）----
        "扭曲都市闭环": [
            ("变形", "转换", "定型"),
            ("定型", "反转", "畸变"),
            ("畸变", "曲解", "僵化"),
            ("僵化", "转换", "超频"),
            ("超频", "反转", "坏死"),
            ("坏死", "曲解", "爆裂"),
            ("爆裂", "曲解", "退化"),
            ("退化", "转换", "变形"),
        ],
        "罪孽都市闭环": [
            ("洗劫", "转换", "逼债"),
            ("逼债", "反转", "抵扣"),
            ("抵扣", "曲解", "清算"),
            ("清算", "反转", "赎金"),
            ("赎金", "转换", "假钞"),
            ("假钞", "曲解", "赌命"),
            ("赌命", "反转", "消灾"),
            ("消灾", "曲解", "洗劫"),
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
        # ---- 原始怪物道纹 → 转化道纹（README 第469-490行）----
        # 非闭环，是以原始道纹为根的分支树；怪物面板上的道纹多属此类，
        # 补齐后残韵才能作用于怪物（此前对必中/狂暴/飞行发动必然失败）。
        "怪物原始道纹": [
            ("狂暴", "转换", "愤怒"),
            ("狂暴", "反转", "自残"),
            ("狂暴", "曲解", "无神"),
            ("强化", "转换", "借力"),
            ("强化", "反转", "弱化"),
            ("强化", "曲解", "自食"),
            ("活力", "转换", "兴奋"),
            ("活力", "反转", "无力"),
            ("活力", "曲解", "迟滞"),
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
        1. 残韵作用于非轮回者拥有的道纹时，仅改变本次发动的道纹结算
        2. 残韵作用于轮回者拥有的道纹时，该轮回者拥有的对应道纹永久变为变化后的道纹
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
            "permanent_change": caster_has_daowen,
            "caster_gets_daowen": not caster_has_daowen and target_has_daowen,
            "summary": f"【{resonance_type}】{source_daowen} → {target}"
        }


# 初始化注册
DaoWenEngine.register_all()
