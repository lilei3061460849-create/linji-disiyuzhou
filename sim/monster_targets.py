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
from typing import Optional

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


# ---------------------------------------------------------------------------
# 怪物道纹优先级分组（DM裁定2026-08-18；2026-08-22 起全仓唯一口径）：
#   1.自保 → 2.输出 → 3.控制/削弱 → 4.机制（最后手段）
#
# 背景：此前该分组在 build_learner / duel_common / handplay_dungeon_with_winner /
# pick_best_report / produce_real_winners 五处各抄一份，2026-08-21 版本变更
# （冲击改名波及、删除缓慢/慈悲/切割）后其中三处仍引用已删除道纹，导致怪物把
# 新道纹（如【波及】）误归"机制组"当最后手段。现全部收拢到本模块，
# 新增道纹只改这里；未知道纹按"机制组"兜底，绝不因表外道纹导致选择异常。
# 分组口径：按道纹对**施法怪物**的战术价值划分（自保/自强化=自保；对敌压力=
# 输出或控制；纯自身机制=机制）。目标选择仍由 pick_monster_daowen_target 负责。
MONSTER_DAOWEN_SELF = {
    # 原始（自用）
    "自愈", "疯狂",
    # 转化（自用增益/续航）
    "借力", "自食", "兴奋", "滋养", "急速", "加速", "洞察", "寄生",
    # 副本专属（自保/自强化/自经济）
    "超频", "爆裂", "假钞", "龙鳞", "逆鳞", "活血", "嫁祸", "背负",
}
MONSTER_DAOWEN_OUTPUT = {
    # 原始
    "狂暴", "强化",
    # 转化
    "自残",
    # 杀伐闭环（怪物可经事件/原初持有）
    "杀伐", "血债", "波及",
    # 副本专属（对敌压力）
    "加害", "裂变", "洗劫", "赎金", "逼债", "赌命",
}
MONSTER_DAOWEN_CONTROL = {
    # 原始
    "减速",
    # 转化
    "愤怒", "无神", "弱化", "无力", "迟滞", "眩晕", "蒙蔽", "衰败", "坠落",
    # 杀伐闭环
    "束缚", "封印",
    # 副本专属
    "僵化", "定型", "畸变", "坏死", "退化", "伤痕", "冥气", "勾魂", "镇尸",
    "瓦解", "缄默", "清算", "抵扣",
}
MONSTER_DAOWEN_MECH = {
    # 原始（自身机制）
    "必中", "飞行",
    # 转化（自身机制）
    "滑翔",
    # 副本专属（纯机制：无直接对敌伤害/控制收益）
    "变形", "分裂", "尸爆", "消灾", "招魂",
}


def monster_daowen_group(name: str) -> int:
    """道纹优先级组：0=自保 1=输出 2=控制 3=机制（表外道纹按机制兜底）。"""
    if name in MONSTER_DAOWEN_SELF:
        return 0
    if name in MONSTER_DAOWEN_OUTPUT:
        return 1
    if name in MONSTER_DAOWEN_CONTROL:
        return 2
    return 3


def pick_monster_daowen_option(cands: list[dict], *, player_low: bool = False,
                               monster_low: bool = False,
                               blocked: bool = False) -> Optional[dict]:
    """按 DM裁定2026-08-18 的固定优先级为怪物选道纹（全仓唯一入口）。

    cands：prepare 列出的合法道纹选项（调用方应已排除本回合已激活项；
    若 cands 为空而原选项非空，由调用方回退首个选项）。
    固定优先级：自保→输出→控制→机制（最后手段）。上下文修正：
      - monster_low：怪物半血以下，强制优先自保；
      - player_low：玩家半血以下，优先输出/控制（收割窗口）；
      - blocked：怪物已连续≥2回合未能让敌方掉血（如被格挡全吸收），
        满足裁定原文"完全无法对敌方造成任何影响"的字面情形→允许动用机制组。
    """
    if not cands:
        return None
    if monster_low:
        self_cands = [o for o in cands if monster_daowen_group(o["name"]) == 0]
        if self_cands:
            return self_cands[0]
    if player_low:
        kill_cands = [o for o in cands
                      if monster_daowen_group(o["name"]) in (1, 2)]
        if kill_cands:
            return kill_cands[0]
    if blocked:
        mech_cands = [o for o in cands if monster_daowen_group(o["name"]) == 3]
        if mech_cands:
            return mech_cands[0]
    return min(cands, key=lambda o: monster_daowen_group(o["name"]))


def pick_wave_dodge_targets(option: dict) -> list[dict]:
    """波及X：从prepare的dodge_target_options中恰好选X个目标（对侧优先）。

    规则要求显式提交恰好X个不重复目标：此前各解析器把dodge_target_options全量
    提交，候选数大于X时必然被resolve拒收（2026-08-22 BUG-01配套修复）。
    DM裁定2026-08-23自适应降X：prepare在面板X>合法目标数时把有效X降到
    wave_effective_x=min(面板X, 候选数)，此处必须按有效X取目标，否则提交数≠
    结算侧mark_count必被拒。
    优先选怪物对侧（玩家方）目标——把后续道纹扩散打到敌方才符合怪物意图；
    对侧不足X时以其余合法目标补齐。
    """
    candidates = list(option.get("dodge_target_options") or [])
    need = int(option.get("wave_effective_x") or 0)
    if not need:
        need = min(int(option.get("x", 0) or 0), len(candidates))
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
