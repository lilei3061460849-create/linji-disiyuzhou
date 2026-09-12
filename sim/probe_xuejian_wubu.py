import sys
sys.path.insert(0,'.'); sys.path.insert(0,'tests')
import tests.test_spell_loop_mana_gain as M
from engine.models import DaoWen, DaoWenInstance
SP={"name":"血溅五步","required_daowen":["再生","搏命","杀伐"],
 "trigger_condition":"失去生命后",
 "effect_flow":"发动再生X于自身→发动搏命X于自身→发动杀伐X于攻击者→循环"}
from tests.test_dragon_heart import _new_engine, _start_with_enemy
def run(cycles,x=3,mana=30,speed=None):
    e=_new_engine(f"wj{cycles}_{x}_{mana}"); p=e.state.player
    for d in SP["required_daowen"]:
        p.dao_wen[d]=DaoWenInstance(DaoWen(name=d,formula="",cost_type="",cost_formula="X",effect_formula=""))
    e.state.energy=3
    e.execute_action("pre_battle_action",{"sub_action":"学习","sub":"custom_spell","spell":SP})
    r=e.execute_action("pre_battle_action",{"sub_action":"学习","sub":"custom_spell","spell":SP,"dm_approved":True})
    assert r["success"], r.get("error")
    _start_with_enemy(e)
    foe0=e.state.enemies[0]; foe0.current_hp=99999; foe0.attack_power=5; foe0.attack_count=1
    p.current_hp=40; p.current_mana=mana
    if speed: p.current_speed=speed
    s0,m0=p.current_speed,p.current_mana
    c=e.combat; foe=e.state.enemies[0]; hp0=foe.current_hp
    prep=c.prepare_monster_phase(); a=prep["actors"][0]; t=a["attack_target_options"][0]; o=t["spell_options"]
    steps=[{"x":x,"target_ref":"player:0"},{"x":x,"target_ref":"player:0"},{"x":x,"target_ref":"enemy:0","dodge":False}]
    af={s["spell_name"]:{"use":False} for s in o["after"]}
    af["血溅五步"]={"use":True,"cycles":[list(steps) for _ in range(cycles)]}
    sc={"before":{s["spell_name"]:{"use":False} for s in o["before"]},"after":af,
        "damage_after":{s["spell_name"]:{"use":False} for s in o["damage_after"]},
        "life_before":{s["spell_name"]:{"use":False} for s in o["life_before"]}}
    ch=[{"actor_ref":a["actor_ref"],"daowen":None,"attack_actions":[{"hits":[{"target_ref":t["ref"],"dodge":False,"blood_shadow":False,"spell_choices":sc}]}]}]
    try:
        c.resolve_monster_phase(ch,prepared=prep)
        print(f"X={x} {cycles:2d}轮 速{s0}->{p.current_speed:2d} 法{m0}->{p.current_mana:2d} 生命{p.current_hp:3d} 累计回复{p.total_healed:3d} | 对怪伤害{hp0-foe.current_hp:4d} 存活{p.is_alive}")
    except Exception as ex:
        print(f"X={x} {cycles:2d}轮 -> 拒绝: {ex}")
for c in (1,2,3,4,5):
    run(c,speed=12)
print("--- 速度是闸门：速4 ---")
for c in (1,2,3):
    run(c,speed=4)
