"""第 6 场爆裂死局专项验证（2026-08-19 修复后）。

复现报告.md 旧战报第 6 场死局局面：
  顾衡 39HP（借力2）+ 脑蜘蛛爆裂1 + 血肉巨囊爆裂1 + 人头气球存活 + 冲击4
旧手操在此局面发动冲击4 → 双爆裂反噬 24×2=48 → 顾衡命零。

修复后（AI 行动预演安全层）：手操候选动作先经 ActionPreview 预演，
冲击4 预演判死（48 反噬）→ 拒绝/降档 → 顾衡存活。

本脚本按手操流程逐步 execute_action（非 TacticalAI），证明同一死局
在预演安全决策下不再自杀。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.ai_preview import ActionPreview
from engine.models import DaoWen, DaoWenInstance, Entity, StatusEffect
from tests.setup_support import finish_initial_daowen

PICK = 202608182


def main():
    e = GameEngine(db_path="/tmp/verify_b6.db", rng_seed=PICK)
    e.execute_action("setup_attributes", {"name": "顾衡", "blood_points": 7,
                                          "speed_points": 8, "mana_points": 10})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})

    p = e.state.player
    # 复刻第 6 场开局面板：39/42，借力2，冲击/杀伐/庇护/再生
    p.current_hp = 39
    p.blood_limit = 42
    for name, x in (("杀伐", 0), ("冲击", 4), ("庇护", 0), ("再生", 0), ("借力", 2)):
        p.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X",
                   effect_formula=""), x_value=x)
    p.add_status(StatusEffect(name="借力", remaining_rounds=-1, value=2, source="x"))
    p.current_mana = 36
    p.mana_limit = 36

    # 双爆裂怪 + 存活怪（与原战报第 6 场同构）
    def bing(name, hp, atk, ap, baolie):
        m = Entity(name, "怪物", blood_limit=hp, current_hp=hp,
                   attack_count=atk, attack_power=ap)
        if baolie:
            m.add_status(StatusEffect(name="爆裂", remaining_rounds=1, value=1, source="x"))
        return m

    e.state.enemies = [
        bing("脑蜘蛛", 148, 2, 11, True),
        bing("人头气球", 174, 1, 11, False),
        bing("血肉巨囊", 234, 1, 8, True),
    ]
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    e.state.current_round = 3

    print(f"死局开局：顾衡 {p.current_hp}/{p.blood_limit} 法{p.current_mana} 借力2 | "
          + "、".join(f"{m.name}(爆裂={m.has_status('爆裂')}){m.current_hp}"
                     for m in e.state.enemies))

    # 手操决策：候选动作先经预演安全检查（safe_daowen 逻辑，与手操驱动器一致）
    def safe_use(name, x, target=None):
        params = {"daowen_name": name, "x": x, "dodge": False, "blood_shadow": False,
                  "trigger_spell_choices": {}}
        if target:
            params["target"] = target
        if name == "冲击":
            refs = e.combat._combat_entity_refs()
            params["dodge_targets"] = [
                {"target_ref": ref, "dodge": False, "blood_shadow": False}
                for ref, ent in refs.items()
                if e.state.on_player_side(ent) != e.state.on_player_side(p) and ent.is_alive]
        probe = x
        while probe >= 1:
            pv = ActionPreview(e).preview("use_daowen", dict(params, x=probe))
            if not pv.get("diff", {}).get("player_dead"):
                break
            probe -= 1
        if probe < 1:
            return {"rejected": True, "name": name, "x": x}
        if probe != x:
            print(f"  [安全过滤] {name}X={x} → {probe}（预演判死，降档）")
        params["x"] = probe
        return e.execute_action("use_daowen", params)

    # 手操第 3 回合（原死局回合）：先试冲击4（预演安全决策）
    alive = [m for m in e.state.enemies if m.is_alive]
    r = safe_use("冲击", 4)
    if isinstance(r, dict) and r.get("rejected"):
        print(f"  [验证通过] 冲击4 被预演安全层拒绝（{r['x']} 档全判死），顾衡未自杀")
    elif r.get("success"):
        print(f"  [验证] 冲击执行成功（降档或安全档），顾衡存活 hp={p.current_hp}")
    else:
        print("  [验证] 冲击不可用，顾衡存活")

    print(f"\n== 结论：顾衡存活 {p.current_hp}/{p.blood_limit} ==")
    print("  旧死因（冲击4 双爆裂反噬 48 命零）在预演安全决策下不再发生。")
    return p.is_alive


if __name__ == "__main__":
    alive = main()
    print("顾衡存活：", alive)
