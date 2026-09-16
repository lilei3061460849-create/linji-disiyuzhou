"""
怪物池解析与出怪(战始抽怪)系统
从全副本索引指向的独立副本文档中解析每副本12只怪物的固定面板，供[战始]随机抽取。
"""
from __future__ import annotations
import re
from pathlib import Path
from typing import Optional

from .dungeons import load_dungeon_documents
# 每只怪物出厂自带的遗物【某人的偏爱】——常量与授予都在 engine/models.py
# （授予点在 Entity.__post_init__，覆盖所有构造路径），此处仅再导出便于就近引用。
from .models import MONSTER_MANA_RELIC


def parse_monster_pool(index_path: str | Path) -> dict:
    """从索引登记的副本文档解析怪物池。
    返回 {region: [{"name","attack_count","attack_power","blood_limit","dao_wen"}, ...]}"""
    documents = load_dungeon_documents(index_path)
    # 2026-09-16 用户令：怪物/微光者面板与轮回者同口径，写作「名字（[血限]/[法限]/[速限]，道纹…）」。
    # 旧格式「攻次×攻力/血限」废止——[攻击次数]=[当前速度]、[攻击力]=[当前法力] 对全体生效后，
    # 攻次/攻力已不是独立面板，再单独印一遍属于冗余且会与上限打架。
    pat = re.compile(r'^([\u4e00-\u9fff\w·]+)[（(](\d+)/(\d+)/(\d+)(?:[，,]([^)）\n]*))?[）)]')
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
                # 新口径：血限 / 法限 / 速限（与轮回者「[血限]/[法限]/[速限]」同序）
                hp, mana_limit, speed_limit = int(m.group(2)), int(m.group(3)), int(m.group(4))
                dw_str = m.group(5) or ""
                dao_wen = {n: int(v) for n, v in re.findall(r'([\u4e00-\u9fff]{2})(\d+)', dw_str)}
                monsters.append({
                    "name": name, "blood_limit": hp,
                    "mana_limit": mana_limit, "speed_limit": speed_limit,
                    # 兼容旧键：攻次=速限、攻力=法限（统一换算后二者不再是独立面板）
                    "attack_count": speed_limit, "attack_power": mana_limit,
                    "dao_wen": dao_wen, "region": region,
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
    乱葬岗等二阶及以上副本与一阶共用同一公式。
    2026-09-11起此数量为上限：R1只进场第1只，R4/R7/R10…回始各增援1只（见 combat.round_start）。"""
    return max(1, battle_number - 3)


def make_monster_entity(monster_def: dict):
    """按怪物定义构造Entity（延迟导入避免循环依赖）"""
    from .models import Entity, DaoWen, DaoWenInstance
    # 2026-09-16 用户令：怪物与轮回者同口径，持有[速限]/[法限]；[攻击次数]=[当前速度]、[攻击力]=[当前法力]。
    # 战始当前值给满，[回始]由 combat.round_start 再次回满（怪物专属；微光者不回满）。
    _speed_limit = monster_def.get("speed_limit", monster_def.get("attack_count", 0))
    _mana_limit = monster_def.get("mana_limit", monster_def.get("attack_power", 0))
    m = Entity(name=monster_def["name"], entity_type="怪物",
               blood_limit=monster_def["blood_limit"], current_hp=monster_def["blood_limit"],
               attack_count=_speed_limit, attack_power=_mana_limit,
               speed_limit=_speed_limit, mana_limit=_mana_limit)
    m.current_speed = _speed_limit
    m.current_mana = _mana_limit
    # 遗物【某人的偏爱】由 Entity.__post_init__ 统一授予，此处不再重复挂载。
    for dw_name, x in monster_def["dao_wen"].items():
        m.dao_wen[dw_name] = DaoWenInstance(
            dao_wen=DaoWen(name=dw_name, formula="", cost_type="", cost_formula="", effect_formula=""),
            x_value=x)
    from .gamedata import ORIGINAL_MONSTER_DAOWEN
    if any(name in ORIGINAL_MONSTER_DAOWEN for name in m.dao_wen):
        m._had_monster_daowen = True
    # 注意：不在此设 is_flying——飞行是道纹的结算结果，须在出手轮主动发动
    # 【飞行】道纹后才生效（combat 发动道纹时设 is_flying），与出生回合无关
    # （2026-09-15 用户令已删除"登场回合不出道纹"的白板限制）。
    return m
