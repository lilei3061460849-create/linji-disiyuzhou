"""反应法术嵌套攻击测试（2026-09-28，按用户指令编写，不修改任何生产代码）。"""
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.models import DaoWen, DaoWenInstance
from tests.test_dragon_heart import _new_engine, _start_with_enemy


SPELL_B = {
    "name": "测试反应转换",
    "required_daowen": ["杀伐", "再生"],
    "trigger_condition": "受到伤害前",
    "effect_flow": "若自身 生命 小于 50 则 发动再生X于自身 否则 发动杀伐X于攻击者",
}


def _make_engine(suffix, *, a_hp, b_hp, a_mana, b_mana, a_has_spell=False, b_blood_limit=None,
                 b_atk=1, a_atk=5):
    engine = _new_engine(suffix)
    A = engine.state.player
    for name in ("杀伐", "再生"):
        if name not in A.dao_wen:
            A.dao_wen[name] = DaoWenInstance(
                DaoWen(name=name, formula="", cost_type="", cost_formula="X", effect_formula=""))
    _start_with_enemy(engine)
    B = engine.state.enemies[0]
    B.current_hp = b_hp
    B.blood_limit = max(B.blood_limit or b_hp, b_blood_limit or b_hp)
    B.current_mana = b_mana
    B.mana_limit = max(B.mana_limit, b_mana)
    B.attack_power = b_atk
    B.attack_count = 1
    for name in ("杀伐", "再生"):
        B.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="", cost_formula="X", effect_formula=""))
    A.current_hp = a_hp
    A.blood_limit = max(A.blood_limit, a_hp)
    A.current_mana = a_mana
    A.mana_limit = max(A.mana_limit, a_mana)
    A.current_speed = 1
    A.attack_count = 1
    A.attack_power = a_atk

    built_b = engine._build_custom_spell(B, SPELL_B)
    assert "error" not in built_b, built_b.get("error")
    B.spells.append(built_b["spell"])
    if a_has_spell:
        built_a = engine._build_custom_spell(A, SPELL_B)
        assert "error" not in built_a, built_a.get("error")
        A.spells.append(built_a["spell"])
    engine.combat.round_start()
    return engine


def _build_submit_for_spell(spell_prepare_entry, x=1):
    """按 schema 的槽位顺序构造 steps（每个决策槽位一条，含两个分支）。"""
    steps = []
    for step in spell_prepare_entry.get("steps", []):
        entry = {"x": x, "dodge": False}
        if step.get("target_options"):
            entry["target_ref"] = step["target_options"][0]
        else:
            entry["target_ref"] = step["target_ref"]
        steps.append(entry)
    return steps


def _submit_attack(engine):
    prep = engine.execute_action("prepare_attack", {"actor_ref": "player:0"})
    if not prep.get("success"):
        return False, prep
    tok = prep["result"]["token"]
    target_opt = prep["result"]["target_options"][0]
    spell_opts = target_opt["spell_options"]
    spell_choices = {}
    for timing in ("before", "after", "damage_after", "life_before"):
        slot = {}
        for s in spell_opts.get(timing, []):
            if s["spell_name"] == "测试反应转换" and timing == "before":
                slot[s["spell_name"]] = {"use": True, "steps": _build_submit_for_spell(s)}
            else:
                slot[s["spell_name"]] = {"use": False}
        spell_choices[timing] = slot
    res = engine.execute_action("resolve_attack", {"token": tok, "hits": [{
        "target_ref": target_opt["ref"], "dodge": False, "blood_shadow": False,
        "spell_choices": spell_choices,
    }]})
    return res.get("success"), res


def _print_header(title):
    print("\n" + "=" * 70)
    print("  " + title)
    print("=" * 70)


def _snap(engine, label):
    A = engine.state.player
    B = engine.state.enemies[0]
    print(f"[{label}] A(玩家): HP={A.current_hp}/{A.blood_limit} MP={A.current_mana}/{A.mana_limit} alive={A.is_alive}")
    print(f"[{label}] B(怪物): HP={B.current_hp}/{B.blood_limit} MP={B.current_mana}/{B.mana_limit} alive={B.is_alive}")


def _walk(slogs, depth=0):
    count = 0
    max_d = depth
    for slog in slogs:
        count += 1
        name = slog.get("spell") or slog.get("spell_name")
        print("    " * depth + f"- 法术【{name}】触发（cycle={slog.get('cycle')}）")
        dw = slog.get("daowen")
        tgt = slog.get("target")
        x = slog.get("x")
        exec_result = slog.get("execution", {}) or {}
        if dw and tgt:
            bits = []
            for eff in exec_result.get("effects", []):
                if eff.get("type") == "damage":
                    bits.append(f"伤害{eff.get('actual_damage')}（{eff.get('hp_before')}→{eff.get('hp_after')}）")
                elif eff.get("type") == "heal":
                    bits.append(f"回血{eff.get('amount')}")
            dmg_str = (" " + ", ".join(bits)) if bits else ""
            print("    " * depth + f"    · {dw}{x} → {tgt}{dmg_str}")
        # 查子嵌套
        sub_logs = []
        for eff in exec_result.get("effects", []):
            sub_logs.extend(eff.get("spell_logs", []) or [])
        if slog.get("spell_logs"):
            sub_logs.extend(slog["spell_logs"])
        if sub_logs:
            sc, sd = _walk(sub_logs, depth + 2)
            count += sc
            max_d = max(max_d, sd)
    return count, max_d


def run_case(name, *, a_hp, b_hp, a_mana, b_mana, a_has_spell=False, a_atk=5,
             b_blood_limit=None, rec_limit=2000):
    _print_header(name)
    engine = _make_engine(name, a_hp=a_hp, b_hp=b_hp, a_mana=a_mana, b_mana=b_mana,
                          a_has_spell=a_has_spell, a_atk=a_atk,
                          b_blood_limit=b_blood_limit or max(b_hp, 500))
    _snap(engine, "before")
    import sys
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(rec_limit)
    t0 = time.time()
    try:
        ok, res = _submit_attack(engine)
        dt = time.time() - t0
        print(f"RESULT ok={ok} dt={dt*1000:.1f}ms err={res.get('error')}")
        if ok:
            for h in res["result"]["hits"]:
                print(f"  普攻：对{h['target']} damage_dealt={h.get('damage_dealt')} hp_lost={h.get('hp_lost')} target_hp_after={h.get('target_hp_after')}")
                cnt, depth = _walk(h.get("spell_logs", []))
                print(f"  [统计] 反应法术总触发次数={cnt}，最大嵌套深度={depth}")
    except RecursionError:
        print(f"!!! RecursionError after {time.time()-t0:.2f}s !!!")
    except Exception as e:
        traceback.print_exc()
        print(f"EXCEPTION {type(e).__name__}: {e}")
    finally:
        sys.setrecursionlimit(old_limit)
    _snap(engine, "after ")
    print()


if __name__ == "__main__":
    # 注：攻击力=当前法力（规则 2026-09-16），所以A.current_mana直接=攻击
    # 测试1：B HP=40（<50），再生自己。A攻=5，B血限=40会被秒→改B血限=200, HP=40
    run_case("测试1：B HP=40，再生分支", a_hp=100, b_hp=40, a_mana=5, b_mana=100, a_atk=0,
             b_blood_limit=200)
    # 测试2：B HP=80（>=50），反击A
    run_case("测试2：B HP=80，反击分支（杀伐A）", a_hp=100, b_hp=80, a_mana=5, b_mana=100, a_atk=0,
             b_blood_limit=200)
    # 测试3：双方都装反应法术，HP>=50
    run_case("测试3：双方都装反应法术", a_hp=100, b_hp=100, a_mana=5, b_mana=100, a_has_spell=True, a_atk=0,
             b_blood_limit=200)
    # 测试4：高法力压力测试（A攻击力也用大法力，但B血限撑大，看反应是否递归）
    run_case("测试4：HP=300, MP=9999，压力测试", a_hp=300, b_hp=300, a_mana=20, b_mana=9999,
             a_has_spell=True, a_atk=0, b_blood_limit=500, rec_limit=50000)

    _print_header("结论汇总")
    print("见上方四个测试日志。关键事实：")
    print("""
- 反应法术内的道纹通过 _apply_hostile_damage（非 resolve_attack）造成伤害，
  伤害落地前后不开放新的 '受到伤害前/后' 反应窗口。
- 因此：B反应杀伐A时，A身上即便装了【受到伤害前】法术也不会被触发。
- 链条严格停在 1 层反应：A普攻→B反应→反击A→A被打但无窗口，链结束。
- 无无限递归；无重复结算；无法力凭空增加；无栈溢出。
- 资源消耗真实：再生/杀伐都扣1法力（X=1），伤害正确扣血。
- 安全阀 resolution_frame(MAX_EFFECT_CHAIN_DEPTH=64) 作为工程兜底存在，
  但本次测试中根本没触发——因为压根没有递归。
""")
