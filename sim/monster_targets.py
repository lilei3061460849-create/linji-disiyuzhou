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
    "自愈", "庇护", "再生", "固执", "疯狂", "全力", "借力", "兴奋", "滋养", "龙鳞",
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
    "愤怒", "全速", "无力", "点金", "逼债", "清算", "赎金", "赌命", "波及",
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
    "狂暴", "全力",
    # 转化
    "自残",
    # 杀伐闭环（怪物可经事件/原初持有）
    "杀伐", "血债", "波及",
    # 副本专属（对敌压力）
    "加害", "裂变", "点金", "赎金", "逼债", "赌命",
}
MONSTER_DAOWEN_CONTROL = {
    # 原始
    "减速",
    # 转化
    "愤怒", "无神", "弱化", "无力", "全速", "眩晕", "蒙蔽", "衰败", "坠落",
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


def pick_monster_daowen_x(engine, monster, option, choice_tpl: dict, token: str,
                          all_choices: list | None = None) -> int:
    """x_free 道纹：用**战术预演评分**为怪物挑一个 X（2026-09-16 用户令，选案 C）。

    面板不写死 X，能开多大只受[法限]或代价限制——但"能开多大"不等于"该开多大"。
    X 越大代价越高（法力即[攻击力]，异变更是会把自己推向【崩解】），
    因此逐个候选档位真实预演一遍，按后果评分取最优。

    实现要点：
    - 预演走 engine/ai_preview.py 的 ActionPreview（deepcopy state 后真实执行再丢弃），
      不复制任何伤害/反伤规则，预演与结算口径天然一致
    - 评分走 TacticalAI._score_candidate，用 actor=monster 把视角翻到怪物侧
      （_split_diff 按名字在 diff["enemies"] 里找自己，怪物侧同样成立）
    - 只取 1 / 中档 / 上限 三个代表值，不枚举全部 X——预演要 deepcopy 整个
      state，全枚举在长模拟里开销过大
    - 评分返回 None 表示该档位会把自己玩死（如异变逼近崩解线），直接跳过
    - 全部候选都不可用时回退到上限，与引擎侧缺 x 的回退口径一致

    choice_tpl 是已拼装好的完整提交模板（含攻击块），这里只替换 daowen["x"]；
    保留攻击块是因为引擎要求 attack_actions 的逐击命中数必须提交完整，
    只提交道纹会被拒（攻击部分的后果在各候选间是常量，不影响 X 之间的比较）。

    all_choices：PVE 怪物阶段要求**全体 actor 一起提交**（combat.py:6002
    `set(submitted) != set(expected)` 即拒），死斗才是部分提交。传完整 choices 列表
    （其中必须含 choice_tpl 这个对象本身）时，评分按整份提交预演、只替换本 actor 的 X；
    其余 actor 的选择原样带入——它们是常量，不影响 X 之间的相对比较。
    """
    import copy

    max_x = int(option.get("max_x") or option.get("x") or 1)
    # 波及：可标记目标数就是发动方能开的硬上限（引擎只给「场上角色总数」这个粗上限）。
    # 不收的话逐档预演会因「提交数≠X」全部失败，评分退化成一律回退到上限。
    is_wave = option.get("dodge_submission") == "per_target"
    if is_wave:
        max_x = clamp_wave_x(option, max_x)
    if max_x <= 1:
        return max(1, max_x)

    from engine.ai_preview import ActionPreview
    from engine.ai_tactics import TacticalAI

    foes = [x for x in (engine.state.get_all_player_side() or []) if x.is_alive]
    ai = TacticalAI(engine, actor=monster, enemies=foes)
    preview = ActionPreview(engine)

    best, best_score = max_x, None
    for x in sorted({1, max(1, max_x // 2), max_x}):
        trial = copy.deepcopy(choice_tpl)
        trial["daowen"]["x"] = x
        if is_wave:
            # 目标提交数必须跟着这一档的X一起变，否则预演必被拒（引擎不再降X）
            apply_wave_submission(trial["daowen"], option, x)
        if all_choices is None:
            submitted = [trial]
        else:
            submitted = [trial if c is choice_tpl else copy.deepcopy(c) for c in all_choices]
        out = preview.preview("resolve_monster_phase",
                              {"token": token, "choices": submitted})
        res = out.get("result") or {}
        if not res.get("success"):
            continue
        score = ai._score_candidate(out.get("diff") or {},
                                    "%sX=%d" % (option.get("name", "?"), x))
        if score is None:
            continue
        score += _persistent_duration_value(engine, monster, option, x, ai)
        if best_score is None or score > best_score:
            best_score, best = score, x
    return best


def _persistent_duration_value(engine, monster, option, x, ai) -> float:
    """跨回合生效的**时长型**道纹（duration == X）补一项期望收益。

    典型：【狂暴】「异变+5X，回始发动一轮额外攻击，持续X回合」。效果从**下一
    回合**才兑现，单步预演只看得到异变代价、一分收益都没有，于是恒选 X=1。
    但按回合摊薄，X=1 与 X=9 的异变单价完全相同（都是 5/回合），真正的差别在
    **道纹出手位**——X=1 每回合都要重放、占掉一次道纹机会；X=9 一次买断九回合。

    收益按"每回合产出 × 有效回合数 × 折价"估：
      · 每回合产出 = 攻次 × 攻力（该 buff 额外一轮攻击的伤害）
      · 有效回合数 = min(X, 预期剩余回合)——打不完那么多回合，多买的时长是浪费
      · 折价 0.5：战斗进程不确定（怪物可能先死、目标可能换），保守折扣
    只作用于 duration == X 的道纹；【加害】这类 duration=-1 的持续状态不在此列
    （它的 +X 在**当回合**的攻击里就已结算，预演看得到，不需要补）。
    """
    try:
        from engine.daowen import DaoWenEngine
        calc = DaoWenEngine.resolve(option.get("name", ""), x, caster=monster)
    except Exception:
        return 0.0
    if int(calc.get("duration") or 0) != x:
        return 0.0

    per_round = monster.effective_attack_count() * monster.effective_attack_power()
    if per_round <= 0:
        return 0.0
    try:
        incoming = max(1.0, float(ai.incoming_damage()))
    except Exception:
        incoming = 1.0
    expected_rounds = monster.current_hp / incoming
    effective = min(float(x), expected_rounds)
    return 0.5 * per_round * effective


def wave_candidate_count(option: dict) -> int:
    """波及此刻可标记的目标数（prepare 枚举的 dodge_target_options）。"""
    return len(list(option.get("dodge_target_options") or []))


def clamp_wave_x(option: dict, want: int) -> int:
    """发动方自己把 X 收到「可标记目标数」以内（用户裁定 2026-09-19）。

    引擎侧 X 上限＝场上当前角色总数（含发动者自己），而波及不能选自己，
    所以真正能标记的只有 dodge_target_options 这些人。引擎已不再替发动方降X，
    提交数≠本次X 会被直接拒收——收 X 是发动方（AI/操作者）自己的事。
    返回 0 表示此刻一个都标记不了（prepare 对怪物本就不给出该道纹）。
    """
    n = wave_candidate_count(option)
    if n <= 0:
        return 0
    return max(1, min(int(want or 1), n))


def apply_wave_submission(dao: dict, option: dict, x: int | None = None) -> int:
    """把「本次发动的X」与「恰好X个目标提交」写成一对，返回该X。

    x_free 面板会同时写 dao["x"]（未写则引擎回退到可负担上限，与提交数对不上）；
    固定X面板的 dao["x"] 引擎不读，只保证目标数尽量对齐（目标真不够时结算报错，
    这是面板写死X的固有后果，现行副本面板的波及一律不写X）。
    """
    want = int(x if x is not None else (option.get("max_x") or option.get("x") or 1))
    n = clamp_wave_x(option, want)
    if option.get("x_free"):
        dao["x"] = n
    dao["dodge_targets"] = pick_wave_dodge_targets(option, n)
    return n


def pick_wave_dodge_targets(option: dict, x: int | None = None) -> list[dict]:
    """波及X：从prepare的dodge_target_options中恰好选X个目标（对侧优先）。

    规则要求显式提交恰好X个不重复目标：此前各解析器把dodge_target_options全量
    提交，候选数大于X时必然被resolve拒收（2026-08-22 BUG-01配套修复）。
    不传 x 时按本次能开的上限（max_x/x）再收到可标记目标数以内，见 clamp_wave_x。
    优先选怪物对侧（玩家方）目标——把后续道纹扩散打到敌方才符合怪物意图；
    对侧不足X时以其余合法目标补齐。
    """
    candidates = list(option.get("dodge_target_options") or [])
    want = int(x if x is not None else (option.get("max_x") or option.get("x") or 1))
    need = clamp_wave_x(option, want)
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
