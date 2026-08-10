"""
完整战斗流程 - 写入引擎
包括：怪物出手、怪物道纹、闪避、回合管理
"""
import random
from .models import Entity, StatusEffect, GameState
from .enums import EntityType
from .combat import CombatEngine
from .dice import DiceEngine


class BattleFlow:
    """
    战斗流程管理器
    负责完整的回合制战斗流程
    """
    
    def __init__(self, state: GameState):
        self.state = state
        self.round_num = 0
        self.battle_log = []
        # 复用战斗引擎的多路径胜利结算逻辑
        self.combat = CombatEngine(state, DiceEngine())
    
    # ==================== 怪物道纹效果 ====================
    
    def apply_monster_passive(self, monster: Entity) -> list[str]:
        """怪物回始被动效果"""
        effects = []
        
        for dw_name, dw_inst in monster.dao_wen.items():
            if dw_inst is None:
                continue
            
            # 自愈：回始回复10%血限
            if dw_name == "自愈":
                heal = int(monster.blood_limit * 0.1)
                old_hp = monster.current_hp
                monster.current_hp = min(monster.blood_limit, monster.current_hp + heal)
                if monster.current_hp > old_hp:
                    effects.append(f"{monster.name}自愈: +{monster.current_hp - old_hp}HP")
            
            # 庇护：回始获得格挡（怪物×3）
            if dw_name == "庇护":
                shield = 4 * 3  # 庇护4×3=12
                monster.shield += shield
                effects.append(f"{monster.name}庇护: +{shield}格挡")
            
            # 狂暴：回始额外攻击标记
            if dw_name == "狂暴":
                effects.append(f"{monster.name}狂暴: 本回合额外攻击")
        
        return effects
    
    def get_monster_actions(self, monster: Entity, round_num: int = 0) -> dict:
        """
        计算怪物出手（攻击出手与道纹出手分离，互不抢夺，均固定不随回合增加）
        返回 {"attack": 攻击出手数, "daowen": 道纹出手数}
        攻击出手基础1（活力+X、狂暴+1）；道纹出手固定1
        """
        attack = 1
        # 活力X：出手次数+X
        if "活力" in monster.dao_wen:
            attack += self._daowen_value(monster, "活力", default=3)
        # 狂暴：一轮额外攻击
        if "狂暴" in monster.dao_wen:
            attack += 1
        return {"attack": max(1, attack), "daowen": 1}

    @staticmethod
    def _daowen_value(monster: Entity, name: str, default: int = 0) -> int:
        """取怪物道纹的X值，缺失或为0时返回default"""
        inst = monster.dao_wen.get(name)
        if inst is None:
            return default
        x = getattr(inst, "x_value", None)
        return x if (isinstance(x, int) and x > 0) else default
    
    # ==================== 怪物攻击 ====================
    
    def monster_attack(self, monster: Entity, player: Entity, attack_num: int, dodge: bool = False) -> dict:
        """
        怪物单次攻击
        返回攻击结果
        """
        result = {
            "monster": monster.name,
            "attack_num": attack_num,
            "base_damage": monster.attack_power,
            "dodge_attempted": dodge,
            "dodge_success": False,
            "actual_damage": 0,
            "shield_absorbed": 0,
            "player_hp_before": player.current_hp,
            "player_hp_after": player.current_hp,
        }
        
        # 必中检查
        is_must_hit = "必中" in monster.dao_wen
        
        # 闪避判定
        if dodge:
            if is_must_hit:
                result["dodge_success"] = False
                result["dodge_fail_reason"] = "必中攻击无法闪避"
            elif player.current_speed >= 1:
                player.current_speed -= 1
                result["dodge_success"] = True
                result["speed_after"] = player.current_speed
                return result
            else:
                result["dodge_success"] = False
                result["dodge_fail_reason"] = "速度不足"
        
        # 伤害计算
        damage = monster.attack_power
        
        # 检查固执（单次最多掉1HP）
        if player.has_status("固执"):
            damage = min(damage, 1)
        
        # 格挡吸收
        absorbed = min(player.shield, damage)
        player.shield -= absorbed
        actual = damage - absorbed
        
        # 扣血
        player.current_hp = max(0, player.current_hp - actual)
        
        result["actual_damage"] = actual
        result["shield_absorbed"] = absorbed
        result["player_hp_after"] = player.current_hp
        
        # 爆裂反伤
        if "爆裂" in monster.dao_wen and actual > 0:
            monster.current_hp = max(0, monster.current_hp - actual)
            result["reflect_damage"] = actual
        
        # 伤痕效果
        if "伤痕" in monster.dao_wen and actual > 0:
            monster.blood_limit = max(1, monster.blood_limit - 2)
            monster.current_hp = min(monster.current_hp, monster.blood_limit)
            result["wound_effect"] = True
        
        # 逆鳞积累
        if "逆鳞" in monster.dao_wen and actual > 0:
            # 逆鳞层数由玩家侧管理
            pass
        
        return result
    
    # ==================== 完整回合流程 ====================
    
    def execute_round(self, player: Entity, monsters: list[Entity], round_num: int, player_actions: list[dict]) -> dict:
        """
        执行一个完整回合
        
        参数:
            player: 玩家实体
            monsters: 怪物列表
            round_num: 回合数
            player_actions: 玩家行动列表 [{"type":"daowen","name":"杀伐","x":7,"target":0}, ...]
        
        返回:
            完整的回合结算结果
        """
        self.round_num = round_num
        round_result = {
            "round": round_num,
            "player_actions": [],
            "monster_actions": [],
            "effects": [],
            "player_hp_start": player.current_hp,
            "player_hp_end": player.current_hp,
            "monster_hp": {m.name: m.current_hp for m in monsters},
        }
        
        # === 1. 回始 ===
        # 法力补满
        old_mana = player.current_mana
        player.current_mana = player.mana_limit
        round_result["effects"].append(f"法力补满: {old_mana}→{player.mana_limit}")
        
        # 格挡清空（新回合）
        player.shield = 0
        
        # 怪物回始被动
        for m in monsters:
            if m.is_alive:
                effects = self.apply_monster_passive(m)
                round_result["effects"].extend(effects)
        
        # === 2. 玩家行动 ===
        for action in player_actions:
            action_result = self._execute_player_action(player, monsters, action)
            round_result["player_actions"].append(action_result)
        
        # === 3. 怪物行动 ===
        for m in monsters:
            if not m.is_alive:
                continue

            actions = self.get_monster_actions(m, round_num)
            m_result = {
                "monster": m.name,
                "actions": actions,
                "attacks": [],
                "daowen_uses": []
            }

            # 攻击出手：每点攻击出手发动一轮攻击（attack_count次）
            for i in range(actions["attack"]):
                if not player.current_hp > 0:
                    break
                for hit in range(m.attack_count):
                    if not player.current_hp > 0:
                        break
                    should_dodge = m.attack_power > 15 and player.current_speed >= 1
                    attack_result = self.monster_attack(m, player, i * m.attack_count + hit + 1, dodge=should_dodge)
                    m_result["attacks"].append(attack_result)

            # 道纹出手：独立于攻击出手，怪物可发动一个道纹（具体效果由AI/DM结算）
            if actions["daowen"] > 0 and m.dao_wen:
                dw_name = next(iter(m.dao_wen))
                m_result["daowen_uses"].append({
                    "daowen": dw_name,
                    "note": "道纹出手（攻击出手之外独立发动，效果由AI/DM按道纹公式结算）"
                })

            round_result["monster_actions"].append(m_result)
        
        # === 4. 回终 ===
        # 格挡清空
        player.shield = 0
        for m in monsters:
            m.shield = 0
        
        # 畸变效果
        for m in monsters:
            if "畸变" in m.dao_wen and m.is_alive:
                blood_loss = m.attack_count * m.attack_power
                player.blood_limit = max(1, player.blood_limit - blood_loss)
                player.current_hp = min(player.current_hp, player.blood_limit)
                round_result["effects"].append(f"{m.name}畸变: 血限-{blood_loss}")
        
        # 逆鳞反击
        # (由玩家侧管理，此处简化)

        # 多路径胜利结算（雕塑/增生/还债）
        settled = self.combat.settle_victory_paths()
        if settled:
            round_result["victory_paths"] = settled
            round_result["effects"].extend([t["note"] for t in settled])

        round_result["player_hp_end"] = player.current_hp
        round_result["monster_hp_end"] = {m.name: m.current_hp for m in monsters}

        return round_result
    
    def _execute_player_action(self, player: Entity, monsters: list[Entity], action: dict) -> dict:
        """执行玩家单次行动"""
        action_type = action.get("type", "")
        
        if action_type == "daowen":
            return self._player_use_daowen(player, monsters, action)
        elif action_type == "attack":
            return self._player_attack(player, monsters, action)
        elif action_type == "dodge":
            return {"type": "dodge", "result": "dodge_decision_recorded"}
        
        return {"type": action_type, "result": "unknown_action"}
    
    def _player_use_daowen(self, player: Entity, monsters: list[Entity], action: dict) -> dict:
        """玩家发动道纹"""
        name = action.get("name", "")
        x = action.get("x", 1)
        target_idx = action.get("target", 0)
        
        if name not in player.dao_wen:
            return {"type": "daowen", "name": name, "error": "未持有道纹"}
        
        # 计算消耗
        cost = x  # 大部分道纹消耗X法力
        
        if player.current_mana < cost:
            return {"type": "daowen", "name": name, "error": f"法力不足({player.current_mana}<{cost})"}
        
        # 扣法力
        player.current_mana -= cost
        
        # 执行效果
        if name == "杀伐":
            damage = 2 * x
            target = monsters[target_idx] if target_idx < len(monsters) else None
            if target and target.is_alive:
                # 龙鳞减伤
                actual_dmg = max(0, damage - getattr(target, 'damage_reduction', 0))
                target.current_hp = max(0, target.current_hp - actual_dmg)
                return {"type": "daowen", "name": name, "x": x, "damage": actual_dmg, "target": target.name,
                        "target_hp": target.current_hp}
        
        elif name == "庇护":
            shield = 4 * x
            player.shield += shield
            return {"type": "daowen", "name": name, "x": x, "shield": shield}
        
        elif name == "再生":
            heal = 3 * x
            player.current_hp = min(player.blood_limit, player.current_hp + heal)
            return {"type": "daowen", "name": name, "x": x, "heal": heal}
        
        return {"type": "daowen", "name": name, "x": x, "result": "executed"}
    
    def _player_attack(self, player: Entity, monsters: list[Entity], action: dict) -> dict:
        """玩家普通攻击"""
        target_idx = action.get("target", 0)
        target = monsters[target_idx] if target_idx < len(monsters) else None
        
        if target and target.is_alive:
            damage = player.attack_power
            target.current_hp = max(0, target.current_hp - damage)
            return {"type": "attack", "damage": damage, "target": target.name, "target_hp": target.current_hp}
        
        return {"type": "attack", "error": "无效目标"}
