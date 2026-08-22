#!/usr/bin/env python3
"""怪物道纹目标选择的共享辅助（sim 侧"最优策略"解析器专用，非引擎规则）。

背景（2026-08-21 修复）：此前的解析器一律取 target_options[0]（=玩家），
导致 再生/增殖 等增益道纹被怪物打给玩家，污染模拟数据（实测：眼树再生奶玩家、
血肉巨囊增殖给玩家加血限）。规则本身允许怪物错误选择目标；但"最优策略"模拟器
必须按目标类型理性选择：

  SELF    → 自身（增益/回复/自强化）
  HOSTILE → 玩家（输出/控制/削弱）
  波及等 per_target 道纹走 dodge_targets，不走本函数。

本模块是叶子模块（不 import 其它 sim 模块），供各解析器/测试直接引用，
避免 build_learner ↔ duel_common 的循环导入。
"""

MONSTER_SELF_DAOWEN = {
    "自愈", "庇护", "再生", "固执", "疯狂", "强化", "借力", "兴奋", "滋养", "龙鳞",
    "狂暴", "必中", "超频", "急速", "加速", "滑翔", "飞行", "自食", "招魂", "变形",
    "净化", "消灾", "增殖", "假钞",
    # 怪物面板实测补充（2026-08-21 分类覆盖审计）：
    "分裂",  # 分裂：命零创造自身复制体 → 自用
    "活血",  # 活血：目标累计失血→回终回复 → 怪物自用（自续航）
    "逆鳞",  # 逆鳞：目标受伤积层→下次伤害加成 → 怪物自用（自强化）
    "背负",  # 背负：目标受伤由自身承担 → 怪物无友方，自施=无操作（不帮玩家）
}

MONSTER_HOSTILE_DAOWEN = {
    "杀伐", "血债", "衰败", "减速", "束缚", "眩晕", "僵化", "蒙蔽", "弱化", "无神",
    "愤怒", "迟滞", "无力", "洗劫", "逼债", "清算", "赎金", "赌命", "波及",
    "定型", "畸变", "坏死", "爆裂", "退化", "加害", "裂变", "嫁祸", "伤痕", "冥气",
    "勾魂", "镇尸", "缄默", "瓦解", "尸爆", "坠落", "自残", "寄生", "封印", "贯穿",
    "洞察",
    "抵扣",  # 抵扣：封印目标一件遗物 → 削玩家（怪物面板实测补充）
}


def pick_wave_dodge_targets(option: dict) -> list[dict]:
    """波及X：从prepare的dodge_target_options中恰好选X个目标（对侧优先）。

    规则要求显式提交恰好X个不重复目标：此前各解析器把dodge_target_options全量
    提交，候选数大于X时必然被resolve拒收（2026-08-22 BUG-01配套修复）。引擎
    prepare侧已保证候选数≥X（不足X时该道纹不会出现在选项里）。
    优先选怪物对侧（玩家方）目标——把后续道纹扩散打到敌方才符合怪物意图；
    对侧不足X时以其余合法目标补齐。
    """
    need = int(option.get("x", 0) or 0)
    candidates = list(option.get("dodge_target_options") or [])
    hostiles = [t for t in candidates if not str(t.get("ref", "")).startswith("enemy:")]
    others = [t for t in candidates if str(t.get("ref", "")).startswith("enemy:")]
    picked = (hostiles + others)[:need]
    return [{"target_ref": t["ref"], "dodge": False, "blood_shadow": False}
            for t in picked]


def pick_monster_daowen_target(engine, actor_ref: str, option: dict) -> str:
    """按道纹目标类型为怪物选择道纹目标（最优策略，不依赖 target_options[0]）。

    SELF 类：优先自身；自身不在合法目标中时回退到第一个合法目标
    （不强行选择玩家）。
    其余（HOSTILE/未分类）：优先玩家（威胁最高）；玩家不可选时回退到
    第一个合法目标。
    """
    name = option.get("resolves_as") or option["name"]
    targets = option["target_options"] or []
    if not targets:
        return ""
    if name in MONSTER_SELF_DAOWEN:
        for t in targets:
            if t["ref"] == actor_ref:
                return t["ref"]
        return targets[0]["ref"]
    for t in targets:
        if t["ref"] == "player:0":
            return t["ref"]
    return targets[0]["ref"]
