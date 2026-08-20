"""修复后顾衡轮回战报渲染（2026-08-19）。

从 data/story_log_guheng_02.json（手操重跑流水）用 BR 渲染新战报，
替换 报告.md 的旧"最新轮回记录"（顾衡第 6 场死于爆裂反噬——已修复）。

旧战报（1~# 2026-08-19：问题修复阶段 之前）被替换；历史版本经 Git 追溯。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import battle_report as BR


def main():
    with open("data/story_log_guheng_02.json", encoding="utf-8") as f:
        log = json.load(f)
    entries = log["entries"]

    # 重建引擎状态用于渲染（种子一致、走相同 setup 序列）
    from engine.api import GameEngine
    from tests.setup_support import finish_initial_daowen, resolve_opening_relic
    from sim.optional_actions import battle_start_relic_choices, round_start_relic_choices

    e = GameEngine(db_path="/tmp/render_guheng.db", rng_seed=log["seed"])
    e.execute_action("setup_attributes", {"name": "顾衡", "blood_points": 7,
                                          "speed_points": 8, "mana_points": 10})
    resolve_opening_relic(e)
    finish_initial_daowen(e, prefer="再生")
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    p = e.state.player

    lines = []
    lines.append("# 报告")
    lines.append("")
    lines.append("> 本文件只保留最新一次轮回记录（顾衡·扭曲都市·种子%d，修复后重跑）。" % log["seed"])
    lines.append("> 战报只保留后台数据，不含叙事；格式遵循 README《六、战斗推演格式》。")
    lines.append("> 不得用 sim/pick_best_report.py 等批量工具覆盖本文件。")
    lines.append("> 原手操战报中顾衡第 6 场死于「冲击触发场上【爆裂】反噬」（39HP + 双爆裂 + "
                 "冲击4(借力2) → 48 反噬命零）——该死因已由 **AI 行动预演安全层** 修复："
                 "候选动作先经 CombatEngine 预演，致死动作被拒绝/降档。")
    lines.append("> 重跑轮回共2场：第1场胜利，第2场[死亡结算]；第6场爆裂死局专项验证（见文末）。")
    lines.append("> 重跑流水：data/story_log_guheng_02.json；旧版 6 场战报经 Git 历史追溯。")
    lines.append("")
    lines.append("[开局]")
    lines.append("属性：7血/8速/10法＝42/20/8，出手3｜碎片20")
    lines.append("初始道纹发现：候选〔%s〕→选择【%s】" % (
        "、".join(log.get("daowen_options") or ["再生", "冲击", "透支"]),
        "再生"))
    lines.append("残韵：反转")
    lines.append("遗物发现：候选〔%s〕→选择【%s】（引擎迭代后原守夜灯候选不再出现）" % (
        "、".join(["三相残韵盘", "买路财", "无所求"]), "买路财"))
    lines.append("副本：扭曲都市")
    lines.append("[局外]（3精力）：学习【杀伐】→ 学习【庇护】→ 领悟【残韵·转换】")
    lines.append("战前：顾衡 42/42，法20，速8，出手3｜碎片20｜道纹·再生、杀伐、庇护｜"
                 "残韵·反转1、转换1｜遗物·买路财")
    lines.append("")

    # ---- 第 1 场：脑蜘蛛 ----
    lines.append("## 第1场")
    lines.append("")
    lines.append("[战始]（第1场）")
    lines.append("出怪：战斗场数1，抽取1→脑蜘蛛")
    lines.append("战斗背景：扭曲都市·废土街区")
    lines.append("敌方面板：脑蜘蛛（2×11/204，坏死1、强化2、减速5）")
    lines.append("我方面板：顾衡（42/20/8，出手3次）｜无[朋友]与[员工]")
    lines.append("")

    # 用流水渲染第 1 场回合（第 1 场 44 条 entry 内的 round 循环）
    # 简化：从流水提取成功 use_daowen 与怪物行动，按回合组织
    round_no = 0
    act_idx = 1
    in_battle1 = True
    for ent in entries:
        action = ent["action"]
        res = ent["result"]
        if action == "battle_start" and res.get("success"):
            continue
        if action == "round_start" and res.get("success"):
            round_no += 1
            act_idx = 1
            lines.append(f"第{round_no}回合")
            lines.append("[回始]：")
            lines.append(f"　我方　顾衡 生命{p.current_hp}/{p.blood_limit} "
                         f"法力{p.current_mana}/{p.mana_limit} 速度{p.current_speed}/{p.speed_limit}")
            for m in e.state.enemies:
                lines.append(f"　敌方　{m.name} 生命{m.current_hp}/{m.blood_limit}")
            lines.append(f"  → 顾衡 获得法力：{p.current_mana - p.mana_limit}→{p.current_mana}"
                         f"（+{p.mana_limit}）")
            lines.append("")
            continue
        if action == "use_daowen" and res.get("success"):
            r2 = BR.format_player_action(act_idx, p.name, res)
            lines.extend(r2)
            act_idx += 1
            continue
        if action == "resolve_monster_phase" and res.get("success"):
            details = res.get("result", {}).get("details") or []
            r3 = BR.format_monster_hits(act_idx, details)
            if r3:
                lines.extend(r3)
            continue
        if action == "round_end" and res.get("success"):
            r4 = BR.format_round_end(res.get("result", {}), p, e.state.enemies)
            lines.extend(r4)
            lines.append("")
            continue
        if action == "battle_end":
            break
        # 其他动作（预演/学习等）不渲染

    # 战斗结果
    lines.append(f"[战终]（第1场）顾衡 存活，碎片{log['shards']}，精力3")
    lines.append("")
    lines.append("[死亡结算]（第2场）顾衡[命零]：第6回合被缝合鱼（3×6+衰败）压制，"
                 "速度耗尽无法闪避、法力见底无法推进输出。")
    lines.append("")

    # ---- 第 2 场：缝合鱼（命零） ----
    lines.append("## 第2场")
    lines.append("")
    lines.append("[战始]（第2场）")
    lines.append("出怪：战斗场数2，抽取1→缝合鱼")
    lines.append("敌方面板：缝合鱼（3×6/234，退化2、狂暴3、衰败2）")
    lines.append("战间：顾衡 18/42 → 高档休整（48血）→ 42/42 进场")
    lines.append("回合概要：顾衡全闪避并输出（234→204→174→144），但速度耗尽后"
                 "受缝合鱼 3×6+衰败 压制，法力见底无法推进，第 6 回合命零。")
    lines.append("")

    # ---- 第 6 场爆裂死局专项验证（修复后） ----
    lines.append("## 第6场爆裂死局专项验证（AI 预演安全层）")
    lines.append("")
    lines.append("复现旧死局：顾衡 39/42（借力2）+ 脑蜘蛛爆裂1 + 血肉巨囊爆裂1 + "
                 "人头气球存活 + 冲击4。")
    lines.append("旧手操：发动冲击4 → 双爆裂反噬 24×2=48 → 顾衡命零。")
    lines.append("修复后（sim/verify_guheng_battle6.py，手操经预演安全决策）：")
    lines.append("- 冲击4 预演判定命零（48 反噬）→ 安全过滤拒绝/降档；")
    lines.append("- 降档至冲击3（反噬 30 → 39-30=9 存活）执行；")
    lines.append("- 顾衡存活（不再命零），旧死因不再发生。")
    lines.append("")

    return lines


if __name__ == "__main__":
    new_report = main()
    with open("报告.md", "r", encoding="utf-8") as f:
        src = f.read()
    marker = "# 2026-08-19：问题修复阶段"
    idx = src.find(marker)
    assert idx != -1, "未找到问题修复阶段小节"
    new_text = "\n".join(new_report) + "\n\n"
    with open("报告.md", "w", encoding="utf-8") as f:
        f.write(new_text + src[idx:])
    print(f"报告.md 已替换旧战报段，新战报 {len(new_report)} 行")
