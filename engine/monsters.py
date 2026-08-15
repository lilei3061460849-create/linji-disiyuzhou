"""
怪物池解析与出怪(战始抽怪)系统
从全副本索引指向的独立副本文档中解析每副本12只怪物的固定面板，供[战始]随机抽取。
"""
from __future__ import annotations
import re
from pathlib import Path
from typing import Optional

from .dungeons import load_dungeon_documents


def parse_monster_pool(index_path: str | Path) -> dict:
    """从索引登记的副本文档解析怪物池。
    返回 {region: [{"name","attack_count","attack_power","blood_limit","dao_wen"}, ...]}"""
    documents = load_dungeon_documents(index_path)
    pat = re.compile(r'^([\u4e00-\u9fff\w·]+)[（(](\d+)[×x](\d+)/(\d+)(?:[，,]([^)）\n]*))?[）)]')
    pools: dict[str, list[dict]] = {}
    for region, document in documents.items():
        lines = document.split("\n")
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


def compute_draw_count(battle_number: int) -> int:
    """出怪数量公式（全部副本统一，DM裁定采用记录版"-3"）：
    数量 = 战斗场数 - 3，最低为1。（7场序列：1/1/1/1/2/3/4）
    乱葬岗等二阶及以上副本与一阶共用同一公式。"""
    return max(1, battle_number - 3)


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
    # 注意：不在此设 is_flying——README 358 白板开局：怪物第1回合非飞行，
    # 须在出手轮主动发动"飞行"道纹后才生效（combat 发动道纹时设 is_flying）。
    return m
