"""
怪物池解析与出怪(战始抽怪)系统
从README.md的"XXX怪物池"标题下解析每副本12只怪物的固定面板，供[战始]随机抽取。
"""
from __future__ import annotations
import re
from typing import Optional


def parse_monster_pool(readme_path: str) -> dict:
    """按"XXX怪物池"标题精确切分，每池12只，正确打region标签（排除事件怪）。
    返回 {region: [{"name","attack_count","attack_power","blood_limit","dao_wen"}, ...]}"""
    with open(readme_path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    pat = re.compile(r'^([\u4e00-\u9fff\w·]+)[（(](\d+)[×x](\d+)/(\d+)(?:[，,]([^)）\n]*))?[）)]')
    pools: dict[str, list[dict]] = {}
    for region in ["扭曲都市", "罪孽都市", "龙心谷"]:
        header = region + "怪物池"
        hidx = next((i for i, l in enumerate(lines) if l.startswith(header)), None)
        if hidx is None:
            pools[region] = []
            continue
        monsters = []
        for j in range(hidx + 1, len(lines)):
            m = pat.match(lines[j].strip())
            if m and len(monsters) < 12:
                name = m.group(1).strip()
                attack_count, attack_power, hp = int(m.group(2)), int(m.group(3)), int(m.group(4))
                dw_str = m.group(5) or ""
                dao_wen = {n: int(v) for n, v in re.findall(r'([\u4e00-\u9fff]{2})(\d+)', dw_str)}
                monsters.append({
                    "name": name, "attack_count": attack_count, "attack_power": attack_power,
                    "blood_limit": hp, "dao_wen": dao_wen, "region": region,
                })
                if len(monsters) >= 12:
                    break
            elif monsters and not m:
                break
        pools[region] = monsters
    return pools


def compute_draw_count(battle_number: int, is_tier_one: bool = True) -> int:
    """出怪数量公式（一阶副本，DM裁定采用记录版"-3"，README已同步更正）：
    数量 = 战斗场数 - 3，最低为1。（一阶7场序列：1/1/1/1/2/3/4）
    非一阶副本目前未设计/未实现，暂按同一公式退化处理，遇到时应重新裁定。"""
    reduction = 3 if is_tier_one else 0
    return max(1, battle_number - reduction)


def make_monster_entity(monster_def: dict):
    """按怪物定义构造Entity（延迟导入避免循环依赖）"""
    from .models import Entity, DaoWen, DaoWenInstance
    m = Entity(name=monster_def["name"], entity_type="怪物",
               blood_limit=monster_def["blood_limit"], current_hp=monster_def["blood_limit"],
               attack_count=monster_def["attack_count"], attack_power=monster_def["attack_power"])
    for dw_name, x in monster_def["dao_wen"].items():
        m.dao_wen[dw_name] = DaoWenInstance(
            dao_wen=DaoWen(name=dw_name, formula="", cost_type="", cost_formula="", effect_formula=""),
            x_value=x)
    return m
