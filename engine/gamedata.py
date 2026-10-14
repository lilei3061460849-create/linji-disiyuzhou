"""生产运行时仍需共享的最小规则集合。

怪物面板、遗物与法术不在这里重复维护：它们分别从副本文档、物品正文和
死者之书读取，或由对应运行时注册表提供。
"""

REGION_EXCLUSIVE_DAOWEN = {
    "扭曲都市": {"变形", "定型", "畸变", "超频", "坏死", "退化"},   # 2026-10-03 删【爆裂】；2026-10-14 删【搏命】（并入【透支】）
    # 2026-10-13 用户令：删【逼债】，【典当】顶替其闭环格。
    "罪孽都市": {"点金", "典当", "豪夺", "清算", "赎金", "假钞", "消灾", "勒索"},  # 2026-10-14 +勒索
    "龙心谷": {"加害", "龙鳞", "逆鳞", "裂变", "背负", "伤痕"},   # 2026-10-03 删【活血】；2026-10-11 删【嫁祸】
    # 2026-10-08 删【镇尸】（与【坏死】硬重复）。注意：闭环上那一格留成空占位符
    # （见 engine/daowen.py::RESERVED_SLOT），不计入本集合——它不是道纹。
    "乱葬岗": {"分裂", "缄默", "瓦解", "冥气", "勾魂", "招魂"},   # 2026-10-03 删【尸爆】
}

ORIGINAL_MONSTER_DAOWEN = {"全力", "疯狂", "减速", "必中", "飞行"}   # 2026-10-03 删【狂暴】

# 副本阶级（道纹递增机制已于 2026-09-16 用户令废止；阶级仍用于出怪公式上界等）
REGION_TIERS = {
    "罪孽都市": 1, "扭曲都市": 1, "龙心谷": 1,
    "乱葬岗": 2, "永夜庭": 2, "沉沦海": 2,
    "荒疫古城": 3,
    "巴别塔": 4,
}

MONSTER_TRANSFORM_DAOWEN = {
    # 2026-10-03 删【兴奋】【滑翔】【坠落】；2026-10-08 再删一条无残韵路径的孤儿转化道纹；
    # 2026-10-11 用户令删【无神】【借力】【衰败】
    "愤怒", "弱化", "自食", "无力", "全速",
    "急速", "加速", "洞察", "蒙蔽", "寄生",
}

# 杀伐闭环 11 节点；开局【发现】初始道纹只从这里抽。
# （2026-08-21：冲击改名波及；删除缓慢、慈悲、切割）
SHAFA_LOOP_DAOWEN = (
    "杀伐", "再生", "庇护", "固执", "血债", "波及",
    "增殖", "透支", "贯穿", "束缚", "封印",
)

# 原始 ∪ 转化。学习门禁与残韵归属用此并集。救赎只看六种原始怪物道纹（2026-10-03 删【狂暴】），不含转化。
MONSTER_DAOWEN = ORIGINAL_MONSTER_DAOWEN | MONSTER_TRANSFORM_DAOWEN

# 所属副本尚未接入运行时，但计算已实现；局外学习仍按专属道纹拒绝。
UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN = {
    "净化": "荒疫古城",
}


def learnability_error(name, *, owned, region, known) -> str | None:
    """局外【学习】口径的单名校验（2026-10-13 从 api._pre_battle_xuexi 原样抽出）。

    返回 None = 可学；否则返回拒绝文案（逐字沿用旧实现）。
    【典当】的发现池与【学习】同口径，共用本函数——改学习门禁时两处一起变。
    """
    if name not in known:
        return f"未知道纹: {name}"
    if name in owned:
        return f"已经掌握道纹: {name}"
    owner = next((r for r, pool in REGION_EXCLUSIVE_DAOWEN.items() if name in pool), None)
    if owner is not None:
        if owner != region:
            return f"【{name}】是{owner}专属道纹，当前副本无法习得"
        if not (REGION_EXCLUSIVE_DAOWEN[owner] & set(owned)):
            return f"【{name}】须先经残韵获得本副本一种专属道纹后才能学习"
    if name in MONSTER_TRANSFORM_DAOWEN:
        return f"【{name}】是怪物转化道纹，只能由自身已有道纹经残韵获得"
    # 2026-10-07 用户令：原始怪物道纹不再是人类禁区（残韵双向 + 可永久持有），
    # 局外【学习】不再以「人类无法承受」为由拒绝。
    unimpl_owner = UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN.get(name)
    if unimpl_owner is not None:
        return f"【{name}】是{unimpl_owner}专属道纹，当前副本无法习得"
    return None


def learnable_daowen_pool(*, owned, region, known) -> list:
    """学习口径下可获得的道纹名（排序稳定；调用方抽样前不再过滤）。"""
    return sorted(n for n in known if learnability_error(n, owned=owned, region=region, known=known) is None)
