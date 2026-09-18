"""机制验证探针：删除怪物「家族税」＋【必中】降价（异变5X→X）后的口径（2026-09-18 用户令）。

**这是机制验证，不是轮回数据**（铁律6：脚本轮回数据视为虚假数据）——只用来确认
引擎在删税／降价后仍按预期结算，不得当成平衡样本引用。跑法：

    .venv/bin/python sim/probe_family_tax_removal.py

五条读数（2026-09-18 实跑，必中降价后刷新）：
  1. prepare 的可负担 X 上限：必中＝**30**（`_DAOWEN_X_PROBE_CAP` 试探封顶，异变30/50）、
     减速／飞行＝9（异变45，崩解线封顶）、变形＝9（法限9）、自愈＝7（`X_LIMITS["冷却"]=7`
     才是紧约束，删税前家族税算出的 9 从来没生效过）；
  2. 发动自愈：异变 **0** 层（删税前是 5X）、冷却 7 场、已损生命 175% 回复照常；
  3. 发动必中9：异变 **9** 层（降价前是 45 层＝崩解 45/50）——反闪避的硬解现在付得起了；
     同阶段两次攻击各吃一层 → 必中余数 7；
  4. 崩解中断仍成立：面板写死 X=2 的减速在 40 层上发动 → 50 层命零、玩家无伤、
     怪物阶段返回值仍带 `collapsed: 减速`（`_monster_collapse_marker`）；
  5. **必中不提交 X 时引擎回退到上限 30**（combat.py `if missing: effective_x = max_x`）
     → 一发就是异变 30/50，再动一次异变型道纹即崩解。这就是报告 Q10 的落点：战术选 X
     （`sim/monster_targets.py::pick_monster_daowen_x`）必须接进怪物阶段驱动
     （`sim/build_learner.py::_resolve_monster_turn` 本轮已接），否则降价只是把
     「必中开满即自爆」换成「必中开满即半自爆」。
"""
import sys; sys.path.insert(0,'.')
from engine.models import GameState, Entity, DaoWen, DaoWenInstance
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.daowen import DaoWenEngine
DaoWenEngine.register_all()

def mk(name,dw,mana=9,speed=2,hp=228):
    m=Entity(name=name,entity_type="怪物",blood_limit=hp,current_hp=hp,
             attack_count=speed,attack_power=mana,mana_limit=mana,current_mana=mana,
             speed_limit=speed,current_speed=speed)
    for n in dw:
        m.dao_wen[n]=DaoWenInstance(dao_wen=DaoWen(name=n,formula="",cost_type="代价",cost_formula="",
                                                   effect_formula="",is_monster_original=True),
                                    x_value=0,x_free=True)
    return m

def bed(m,rounds=1):
    st=GameState()
    st.player=Entity(name="贾凡",entity_type="轮回者",blood_limit=60,current_hp=60,
                     speed_limit=8,current_speed=8,mana_limit=8,current_mana=8)
    st.enemies.append(m); st.phase="in_combat"
    c=CombatEngine(st,DiceEngine()); c.reset_monster_activation()
    for _ in range(rounds): c.round_start()
    return st,c

def submit(c,name,x=None,target_self=True,atk=True):
    prep=c.prepare_monster_phase()
    a=prep["actors"][0]
    opt=next(o for o in a["daowen_options"] if o["name"]==name)
    dao={"name":name,"dodge":False,"blood_shadow":False,"trigger_spell_choices":{}}
    if x is not None: dao["x"]=x
    if opt["requires_target"] or (target_self and opt.get("target_options")):
        refs=[t["ref"] for t in opt["target_options"]]
        dao["target_ref"]="enemy:0" if "enemy:0" in refs else refs[0]
    n=a.get("base_hits_per_attack",1)
    hit={"target_ref":"player:0","dodge":False,"blood_shadow":False,
         "spell_choices":{"before":{},"after":{}}}
    acts=[{"hits":[dict(hit) for _ in range(n)]} for _ in range(a["base_attack_actions"])]
    return c.resolve_monster_phase([{"actor_ref":a["actor_ref"],"daowen":dao,"attack_actions":acts}],prep)

print("=== 1) prepare：可负担 X 上限（家族税删除后）===")
m=mk("奇美拉",["必中","自愈","减速","变形","飞行"])
st,c=bed(m)
for o in c.prepare_monster_phase()["actors"][0]["daowen_options"]:
    print(f"   {o['name']:<3} max_x={o.get('x')}  {o.get('summary','')[:56]}")

print("=== 2) 发动自愈（自身代价＝冷却X）：不得再计异变 ===")
m2=mk("自愈鱼",["自愈"],mana=8,speed=1,hp=100); m2.current_hp=40
st2,c2=bed(m2)
r=submit(c2,"自愈")
print(f"   异变={m2.mutation_count} 冷却={m2.dao_wen['自愈'].cooldown_remaining} 场 HP={m2.current_hp}/100 存活={m2.is_alive}")

print("=== 3) 发动必中9（自身代价＝异变X，2026-09-18 用户令由5X降价）：只 9 层 ===")
m3=mk("必中怪",["必中"],mana=9,speed=2)
st3,c3=bed(m3)
r=submit(c3,"必中",x=9)
print(f"   异变={m3.mutation_count} → 崩解 {m3.mutation_count}/{Entity.MUTATION_COLLAPSE_THRESHOLD}  存活={m3.is_alive}")
print(f"   必中余数={c3.bizhong_remaining(m3)}（下X次选择[目标]无法闪避）")

print("=== 4) 崩解中断仍然成立（面板写死X=2 的减速：40+10=50）===")
m4=mk("临界怪",["减速"],mana=9,speed=2)
m4.dao_wen["减速"].x_free=False; m4.dao_wen["减速"].x_value=2
m4.mutation_count=Entity.MUTATION_COLLAPSE_THRESHOLD-10
st4,c4=bed(m4)
r=submit(c4,"减速")
print(f"   异变={m4.mutation_count} 存活={m4.is_alive} 玩家HP={st4.player.current_hp}/60")
print(f"   collapsed 标记={[e.get('collapsed') for e in r if e.get('collapsed')]}")
print("=== 5) 必中开满（引擎缺省回退＝可负担上限）：这就是 Q10 战术选X必须接线的原因 ===")
m5=mk("开满怪",["必中"],mana=9,speed=2)
st5,c5=bed(m5)
mx=next(o.get("x") for o in c5.prepare_monster_phase()["actors"][0]["daowen_options"]
        if o["name"]=="必中")
r=submit(c5,"必中")          # 不提交 x → 引擎回退到上限
print(f"   prepare 上限 max_x={mx}；缺省回退后 异变={m5.mutation_count}"
      f" → 崩解 {m5.mutation_count}/{Entity.MUTATION_COLLAPSE_THRESHOLD}  存活={m5.is_alive}")
print(f"   collapsed 标记={[e.get('collapsed') for e in r if e.get('collapsed')]}")
