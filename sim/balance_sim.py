#!/usr/bin/env python3
"""
实战平衡模拟器
解析README全部怪物面板，用不同胜利路径策略跑单场战斗，统计胜率。
用于把占位阈值（增生/还债/雕塑）调到目标胜率。

用法:
    python sim/balance_sim.py            # 跑全部策略 × 全部怪物
    python sim/balance_sim.py kill 200   # 单策略 × 200局/怪
"""
import sys, os, re, math, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.models import Entity, GameState, DaoWen, DaoWenInstance
from engine.combat import CombatEngine
from engine.dice import DiceEngine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ===== 占位阈值（与 combat.py 保持一致，供调参覆盖） =====
TUNING = {
    "PROLIF_THRESHOLD": 1.0,   # 增生：累计恢复 ≥ 血限×N
    "DEBT_THRESHOLD": 10,      # 还债：负债 ≤ -N
    "SCULPTURE_DAMAGE": 15,
    "SCULPTURE_SHIELD": 20,
    "TAMING_TURNS": 3,
}

REGION_EXCLUSIVE = {
    "扭曲都市": {"变形","定型","畸变","僵化","超频","坏死","爆裂","退化"},
    "罪孽都市": {"洗劫","逼债","抵扣","清算","赎金","假钞","赌命","消灾"},
    "龙心谷":   {"加害","龙鳞","逆鳞","活血","裂变","嫁祸","背负","伤痕"},
}

# 各副本怪物池的行范围（用于归属判定）——按面板出现顺序解析即可
def parse_monsters():
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
        content = f.read()
    # 先确定每个副本的怪物池段落，归属region
    region_map = {}
    for region, anchor in [("扭曲都市","扭曲都市怪物池"),("罪孽都市","罪孽都市怪物池"),("龙心谷","龙心谷怪物池")]:
        idx = content.find(anchor)
        region_map[region] = idx
    pattern = re.compile(r'([\u4e00-\u9fff\w·]+)[（(](\d+)[×x](\d+)/(\d+)(?:[，,]([^)）\n]*))?[）)]')
    monsters = []
    seen = set()
    for m in pattern.finditer(content):
        name = m.group(1).strip()
        if name in seen:
            continue
        # 判定region：找该位置之前的最近副本池锚点
        pos = m.start()
        region = "扭曲都市"
        best = -1
        for r, idx in region_map.items():
            if idx >= 0 and idx < pos and idx > best:
                best, region = idx, r
        ac, ap, hp = int(m.group(2)), int(m.group(3)), int(m.group(4))
        dw_str = m.group(5) or ""
        dw = {n: int(v) for n, v in re.findall(r'([\u4e00-\u9fff]{2})(\d+)', dw_str)}
        monsters.append({"name": name, "ac": ac, "ap": ap, "hp": hp, "dw": dw, "region": region})
        seen.add(name)
    return monsters


def make_monster(md):
    m = Entity(name=md["name"], entity_type="怪物",
               blood_limit=md["hp"], current_hp=md["hp"],
               attack_count=md["ac"], attack_power=md["ap"])
    for n, x in md["dw"].items():
        m.dao_wen[n] = DaoWenInstance(
            dao_wen=DaoWen(name=n, formula="", cost_type="", cost_formula="", effect_formula=""),
            x_value=x)
    return m


def make_player(extra_dw=None):
    # 标准构筑：10血/7速/8法 → 60HP/14法/8速(3出手)，杀伐+庇护+再生
    p = Entity(name="轮回者", entity_type="轮回者",
               blood_limit=60, current_hp=60,
               mana_limit=14, current_mana=14,
               speed_limit=8, current_speed=8,
               attack_count=1, attack_power=1)
    for n in ["杀伐", "庇护", "再生"] + (extra_dw or []):
        p.dao_wen[n] = DaoWenInstance(
            dao_wen=DaoWen(name=n, formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
            x_value=0)
    return p


# ===== 道纹效果（模拟版，忠实公式） =====
def cast_shaifa(player, monster, x):
    if player.current_mana < x: return 0
    player.current_mana -= x
    dmg = 2 * x
    monster.current_hp = max(0, monster.current_hp - dmg)
    return dmg

def cast_bihu(player, x):
    if player.current_mana < x: return 0
    player.current_mana -= x
    player.shield += 4 * x
    return 4 * x

def cast_zaisheng(player, target, x):
    if player.current_mana < x: return 0
    player.current_mana -= x
    before = target.current_hp
    target.current_hp = min(target.blood_limit, target.current_hp + 3*x)
    target.total_healed += (target.current_hp - before)  # 再生不计过量双倍（实恢）
    return target.current_hp - before

def cast_ziyang(player, monster, x):  # 滋养X：治疗血限10X%
    if player.current_mana < 5*x: return 0
    player.current_mana -= 5*x
    heal = math.ceil(monster.blood_limit * 10 * x / 100)
    monster.heal(heal)  # heal() 自动按过量双倍计入total_healed
    return heal

def cast_ruohua(player, monster, x):  # 弱化X：攻击力-X（消耗3X）
    if player.current_mana < 3*x: return 0
    player.current_mana -= 3*x
    monster.attack_power = max(0, monster.attack_power - x)
    return x

def cast_shujin(player, monster, x):  # 赎金X：夺10X碎片（消耗10X）
    if player.current_mana < 10*x: return 0
    player.current_mana -= 10*x
    monster.shards -= 10*x
    return 10*x


def monster_round_start(m, activated):
    """怪物回始被动：自愈/庇护（须已激活才生效，白板第1回合无）"""
    if "自愈" in activated:
        x = m.dao_wen["自愈"].x_value
        heal = math.ceil(m.blood_limit * 10 * x / 100)
        m.heal(heal)
    if "庇护" in activated:
        x = m.dao_wen["庇护"].x_value
        m.shield += 4 * x * 3  # 怪物×3


def monster_activate(m, activated, rng):
    """
    怪物道纹出手：激活一个尚未激活的成长型道纹（白板第1回合后开始激活）
    成长道纹效果：活力X攻击出手+X；强化X攻击力+X；狂暴每回合+1攻击出手；
    必中攻击不可闪避；自愈/庇护回始生效（标记激活）
    """
    growth_priority = ["活力", "强化", "狂暴", "必中", "自愈", "庇护", "飞行", "减速"]
    for g in growth_priority:
        if g in m.dao_wen and g not in activated:
            activated.add(g)
            if g == "强化":
                m.attack_power += m.dao_wen[g].x_value
            return


def get_monster_attack_actions(m, activated):
    """怪物攻击出手数 = 1 + 活力X(若激活) + 狂暴1(若激活)"""
    n = 1
    if "活力" in activated:
        n += m.dao_wen["活力"].x_value
    if "狂暴" in activated:
        n += 1
    return n


def monster_attack_round(m, player, combat, rng, must_hit):
    """怪物1轮攻击出手（attack_count次），玩家逐次决定闪避。返回对玩家造成的生命损失。"""
    hp_before = player.current_hp
    for _ in range(m.attack_count):
        if not player.is_alive or not m.is_alive:
            break
        dmg = m.attack_power
        will_break = dmg > player.shield
        if will_break and player.current_speed > 0 and not must_hit:
            player.current_speed -= 1  # 闪避成功
            continue
        if dmg <= 0:
            continue
        absorbed = min(player.shield, dmg)
        player.shield -= absorbed
        player.current_hp = max(0, player.current_hp - (dmg - absorbed))
        if player.current_hp <= 0:
            player.is_alive = False
    lost = hp_before - player.current_hp
    combat.record_monster_damage(m, lost)
    return lost


def run_battle(md, policy, rng, player_dw=None):
    """跑一场战斗，返回 {'win':bool, 'path':str, 'rounds':int}"""
    state = GameState()
    state.current_region = md["region"]
    player = make_player(player_dw)
    state.player = player
    monster = make_monster(md)
    state.enemies.append(monster)
    combat = CombatEngine(state, DiceEngine())
    combat.PROLIFERATION_THRESHOLD = TUNING["PROLIF_THRESHOLD"]
    combat.DEBT_THRESHOLD = TUNING["DEBT_THRESHOLD"]
    combat.SCULPTURE_DAMAGE = TUNING["SCULPTURE_DAMAGE"]
    combat.SCULPTURE_SHIELD = TUNING["SCULPTURE_SHIELD"]
    combat.TAMING_REQUIRED_TURNS = TUNING["TAMING_TURNS"]
    combat.init_monster_shards(monster)

    max_rounds = 25
    activated = set()  # 怪物已激活的道纹（白板第1回合为空）
    for rnd in range(1, max_rounds+1):
        if not player.is_alive:
            return {"win": False, "path": "death", "rounds": rnd}
        if not monster.is_alive:
            break
        # 回始
        player.current_mana = player.mana_limit
        player.shield = 0
        monster_round_start(monster, activated)
        # 怪物道纹出手（第1回合白板不激活）
        if rnd > 1:
            monster_activate(monster, activated, rng)

        # 玩家出手（3次），按策略
        actions = 3
        policy(state, player, monster, combat, actions, rng)

        if not monster.is_alive:
            break
        # 怪物攻击出手
        must_hit = "必中" in activated
        for _ in range(get_monster_attack_actions(monster, activated)):
            if not player.is_alive:
                break
            monster_attack_round(monster, player, combat, rng, must_hit)
        if not player.is_alive:
            return {"win": False, "path": "death", "rounds": rnd}

        # 回终：格挡/法力清空，结算胜利路径
        player.shield = 0
        player.current_mana = 0
        monster.shield = 0
        paths = combat.settle_victory_paths()
        if paths:
            return {"win": True, "path": paths[0]["type"], "rounds": rnd}
        if not monster.is_alive:
            return {"win": True, "path": "kill", "rounds": rnd}
    # 超时判定
    if not monster.is_alive:
        return {"win": True, "path": "kill", "rounds": max_rounds}
    return {"win": False, "path": "timeout", "rounds": max_rounds}


# ===== 策略 =====
def strategy_kill(state, p, m, combat, actions, rng):
    # 每回合：够杀就全杀伐；否则庇护挡住下回合伤害，剩余杀伐
    monster_dmg = m.attack_count * m.attack_power
    mana = p.current_mana
    # 致死判定：剩余出手全杀伐能否击杀
    need = math.ceil(m.current_hp / 2)  # 杀伐2X
    if need <= mana and need <= actions * mana:  # 法力够大致击杀
        # 集中杀伐
        while actions > 0 and m.is_alive and p.current_mana > 0:
            x = min(p.current_mana, math.ceil(m.current_hp / 2))
            if x < 1: break
            cast_shaifa(p, m, x)
            actions -= 1
        return
    # 防御：庇护挡住monster_dmg
    need_shield_x = max(1, math.ceil(monster_dmg / 4)) if monster_dmg > 0 else 0
    if monster_dmg > 0 and p.current_hp <= monster_dmg + 5:
        cast_bihu(p, need_shield_x)
        actions -= 1
    # 剩余杀伐
    while actions > 0 and m.is_alive and p.current_mana > 0:
        x = min(p.current_mana, 7)
        cast_shaifa(p, m, x)
        actions -= 1


def strategy_tame(state, p, m, combat, actions, rng):
    # 每回合庇护挡满怪物伤害→3回合0伤降服；剩余杀伐
    monster_dmg = m.attack_count * m.attack_power
    if monster_dmg > 0:
        need_x = math.ceil(monster_dmg / 4)
        if p.current_mana >= need_x:
            cast_bihu(p, need_x)
            actions -= 1
        # 否则靠闪避（在monster_attack_round里自动）
    while actions > 0 and m.is_alive and p.current_mana > 0:
        cast_shaifa(p, m, min(p.current_mana, 7))
        actions -= 1


def strategy_sculpt(state, p, m, combat, actions, rng):
    # 弱化把攻击力打到0→雕塑；期间庇护/闪避生存
    monster_dmg = m.attack_count * m.attack_power
    # 先保证生存
    if monster_dmg > 0:
        need_x = math.ceil(monster_dmg / 4)
        if p.current_mana >= need_x and actions > 0:
            cast_bihu(p, need_x)
            actions -= 1
    # 弱化
    while actions > 0 and m.attack_power > 0 and p.current_mana >= 3:
        cast_ruohua(p, m, min(m.attack_power, p.current_mana // 3))
        actions -= 1
    while actions > 0 and p.current_mana > 0 and m.is_alive:
        cast_shaifa(p, m, min(p.current_mana, 7))
        actions -= 1


def strategy_proliferate(state, p, m, combat, actions, rng):
    # 滋养/再生把怪物奶过阈值→增生；期间庇护生存
    monster_dmg = m.attack_count * m.attack_power
    if monster_dmg > 0:
        need_x = math.ceil(monster_dmg / 4)
        if p.current_mana >= need_x and actions > 0:
            cast_bihu(p, need_x)
            actions -= 1
    # 滋养（大奶）
    while actions > 0 and p.current_mana >= 5:
        cast_ziyang(p, m, 1)
        actions -= 1
    while actions > 0 and p.current_mana >= 1:
        cast_zaisheng(p, m, min(p.current_mana, 5))
        actions -= 1


def strategy_debt(state, p, m, combat, actions, rng):
    # 赎金把碎片打到负债→还债（仅罪孽都市怪物有碎片）
    monster_dmg = m.attack_count * m.attack_power
    if monster_dmg > 0:
        need_x = math.ceil(monster_dmg / 4)
        if p.current_mana >= need_x and actions > 0:
            cast_bihu(p, need_x)
            actions -= 1
    while actions > 0 and p.current_mana >= 10:
        cast_shujin(p, m, 1)
        actions -= 1
    while actions > 0 and p.current_mana > 0 and m.is_alive:
        cast_shaifa(p, m, min(p.current_mana, 7))
        actions -= 1


STRATEGIES = {
    "kill": (strategy_kill, None),
    "tame": (strategy_tame, None),
    "sculpt": (strategy_sculpt, ["弱化"]),
    "proliferate": (strategy_proliferate, ["滋养"]),
    "debt": (strategy_debt, ["赎金"]),
}


def main():
    monsters = parse_monsters()
    print(f"解析到 {len(monsters)} 个怪物\n")
    rng = random.Random(42)
    only = sys.argv[1] if len(sys.argv) > 1 else None
    reps = int(sys.argv[2]) if len(sys.argv) > 2 else 50

    strats = [only] if only else list(STRATEGIES.keys())
    for sname in strats:
        policy, extra = STRATEGIES[sname]
        wins = 0; total = 0; path_counts = {}
        per_monster = []
        for md in monsters:
            # 还债只对有碎片的罪孽都市怪有意义；其他策略全跑
            ww = 0
            for _ in range(reps):
                r = run_battle(md, policy, rng, player_dw=extra)
                if r["win"]:
                    ww += 1
                    path_counts[r["path"]] = path_counts.get(r["path"], 0) + 1
            total += reps
            wins += ww
            per_monster.append((md["name"], md["region"], ww, reps))
        rate = wins / total * 100 if total else 0
        print(f"=== 策略 [{sname}] ===  胜率 {rate:.1f}% ({wins}/{total})")
        print(f"   路径分布: {path_counts}")
        # 各怪胜率简表（只列<20%和>80%的极端）
        hard = [x for x in per_monster if x[2]/x[3] < 0.2]
        easy = [x for x in per_monster if x[2]/x[3] > 0.8]
        if hard: print(f"   极难(<20%): {[(x[0],x[2]/x[3]) for x in hard]}")
        if easy: print(f"   极易(>80%): {[(x[0],x[2]/x[3]) for x in easy]}")
        print()


if __name__ == "__main__":
    main()
