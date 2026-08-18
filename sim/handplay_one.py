import sys, os, tempfile, json
from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.api import GameEngine
from sim.handplay_dungeon_with_winner import load_winner
from sim.optional_actions import start_round, start_battle
from sim.alt_path_test import resolve_monster_turn

def handplay(winner, seed=7, battles=1, verbose=True, spell_plan=None):
    with open(f'data/real_winners/{winner}', encoding='utf-8') as f:
        snap = json.load(f)
    e = GameEngine(db_path=tempfile.mktemp(suffix='.db'), rng_seed=seed, sealed_candidate_path='/tmp/hm.json')
    p0 = snap['player']
    e.execute_action('setup_attributes', {'name':p0['name'],'blood_points':10,'speed_points':8,'mana_points':7})
    finish_initial_daowen(e)
    e.execute_action('setup_choose_resonance', {'resonance_type':'反转'})
    setup = e.execute_action('setup_choose_region', {'region':'乱葬岗'})
    e.execute_action('choose_discovered_relic', {'relic_name': setup['result']['relic_choices'][0]})
    load_winner(e, snap)
    # 若胜者无朋友，补3护卫者（真实一阶可经事件/雇佣获得；护卫命令的载体）
    from engine.models import Entity
    if not e.state.friends:
        for i in range(3):
            e.state.friends.append(Entity(f"护卫者{i+1}", "朋友", blood_limit=54, current_hp=54,
                                          attack_count=2, attack_power=4))
    # 手操局外：休整回满→附煞冥煞→修行（不领悟——乱葬岗怪飞行道纹实战不生效，
    # 残韵反转无用武之地；领悟只会占精力削弱战力）
    while e.state.energy > 0:
        p = e.state.player

        if p and p.current_hp < p.blood_limit:
            r = e.execute_action('pre_battle_action', {'sub_action':'休整','tier':3,'heal_allocations':[{'target_ref':'player:0','amount':48+e.state.rest_heal_bonus}]})
            if r.get('success'): continue
            # 血差<48且碎片不够3档→1档
            r2 = e.execute_action('pre_battle_action', {'sub_action':'休整','tier':1,'heal_allocations':[{'target_ref':'player:0','amount':8+e.state.rest_heal_bonus}]})
            if r2.get('success'): continue
        r = e.execute_action('pre_battle_action', {'sub_action':'附煞','mode':'选择','sha_qi':'冥煞','daowen_name':'杀伐'})
        if r.get('success'): continue
        e.execute_action('pre_battle_action', {'sub_action':'修行','tier':1,'allocations':{'speed_points':0,'mana_points':1}})
    cleared = 0
    for b in range(1, battles+1):
        p = e.state.player
        if not p or not p.is_alive: break
        e.state.energy = 0
        bs, _bs_artifacts = start_battle(e)
        if not bs.get('success'):
            if verbose: print(f'  battle_start失败: {bs.get("error")[:50]}')
            break
        if verbose: print(f'第{b}场出怪: {bs.get("enemies")}')
        won = False
        for rnd in range(1, 30):
            p = e.state.player
            if not p or not p.is_alive: break
            if not [x for x in e.state.enemies if x.is_alive]: won=True; break
            rs, _rs_artifacts = start_round(e)
            if verbose: print(f'  R{rnd} 回始: hp={p.current_hp}/{p.blood_limit} 法={p.current_mana} 盾={p.shield} | 敌={[(m.name,m.current_hp) for m in e.state.enemies if m.is_alive]}')
            # 手操决策：命令全部存活护卫者护卫（无消耗强制挡伤，怪物打玩家→转给护卫者）
            for idx, fr in enumerate(e.state.friends):
                if fr.is_alive and not fr.has_retreated:
                    r = e.execute_action('command_ally', {'ally_ref': f'friend:{idx}', 'instruction': '护卫 9'})
                    if r.get('success') and verbose:
                        print(f'    决策: 命令{fr.name}护卫(挡9次)')
            # 手操决策循环：每击显式决策——满法输出(与脚本版一致：3次出手全杀伐)
            for _ in range(max(1,(p.speed_limit+2)//3)):
                p = e.state.player
                if not p or not p.is_alive: break
                enemies = [x for x in e.state.enemies if x.is_alive]
                if not enemies: break
                threat = sum(x.attack_count*x.attack_power for x in enemies)
                # 决策0: 对飞行怪用残韵反转→坠落（用户指出：把飞行变坠落）
                flying = [x for x in enemies if e.combat._is_flying(x)]
                if flying and e.state.resonance.get('反转', 0) > 0:
                    r = e.execute_action('use_resonance', {'source_daowen':'飞行','resonance_type':'反转','target_ref':f'enemy:{e.state.enemies.index(flying[0])}'})
                    if r.get('success'):
                        if verbose: print(f'    决策: 残韵反转 飞行→坠落 ({flying[0].name}不再飞行)')
                        continue
                # 决策1: 庇护仅当"下回合必死"（威胁-护卫挡伤 > 血+盾 且 一回合杀不光）
                # 否则全力输出清怪——庇护浪费输出只会拖到被磨死
                guard_absorb = 9 * sum(
                    1 for fr in e.state.friends
                    if fr.is_alive and not fr.has_retreated)
                lethal_next = threat - guard_absorb > p.current_hp + p.shield
                # 3次出手能打死至少1只就全力输出（逐只清），否则才考虑庇护
                per_hit = max(1, p.current_mana - 3)
                acts = max(1, (p.speed_limit + 2) // 3)
                can_kill_one = any(per_hit * acts >= x.current_hp for x in enemies)
                if lethal_next and not can_kill_one and '庇护' in p.dao_wen and p.current_mana >= 10:
                    r = e.execute_action('use_daowen', {'daowen_name':'庇护','x':2,'target_ref':'player:0','trigger_spell_choices':{}})
                    if r.get('success'):
                        if verbose: print(f'    决策: 庇护X=2 (盾{p.shield})')
                        continue
                # 决策2: 杀伐打血最少(全力输出,每次X=当前法力-3保留法术)
                target = min(enemies, key=lambda x: x.current_hp)
                x = max(1, p.current_mana - 3)
                if x >= 1:
                    r = e.execute_action('use_daowen', {'daowen_name':'杀伐','x':x,'target_ref':f'enemy:{e.state.enemies.index(target)}','trigger_spell_choices':{}})
                    if r.get('success'):
                        dmg = sum(ef.get('actual_damage',0) for ef in (r.get('execution',{}).get('effects') or []))
                        if verbose: print(f'    决策: 杀伐X={x} 打{target.name} → {dmg}伤')
                        continue
                break
            if not [x for x in e.state.enemies if x.is_alive]: won=True; break
            if not p.is_alive: break
            e.execute_action('resolve_ally_phases', {})
            mp = resolve_monster_turn(e, [])
            if verbose:
                # 读引擎真实输出：怪物每只的激活道纹 + 命中详情
                for m in e.state.enemies:
                    if m.is_alive or True:
                        act = e.combat._monster_activated.get(id(m), set())
                        print(f'  [怪阶段] {m.name} 已激活道纹={sorted(act)} is_flying={e.combat._is_flying(m)}')
                for d in (mp.get('result',{}).get('details') or []):
                    dw = d.get('daowen') or {}
                    dwname = dw.get('name') if isinstance(dw, dict) else ''
                    if dwname:
                        print(f'  [怪阶段] {d.get("attacker")} 发动道纹 {dwname}')
                    for h in (d.get('hits') or []):
                        print(f'  [怪阶段] {d.get("attacker")}→{h.get("target")} 伤{h.get("damage_dealt")}')
            if mp.get('result',{}).get('player_dead'):
                if verbose: print(f'  R{rnd} 玩家阵亡于怪物阶段')
                break
            e.execute_action('round_end', {})
            if verbose: print(f'  ← 怪物阶段后: hp={p.current_hp}')
        if won and e.state.player and e.state.player.is_alive:
            be = e.execute_action('battle_end', {})
            # 工资待决会阻塞战终（completed=False，phase未回局外）——逐件结算
            guard = 0
            while (be.get("success") and be.get("completed") is False
                   and be.get("pending_wage_decisions")):
                pending = {k: v for k, v in e.state.pending_wage_decisions.items() if v is not None}
                name = next(iter(pending)); wage = pending[name]
                if e.state.shards >= wage:
                    e.execute_action("pay_employee_wage", {"name": name, "decision": "pay"})
                else:
                    e.execute_action("pay_employee_wage", {"name": name, "decision": "refuse"})
                be = e.execute_action('battle_end', {})
                guard += 1
                if guard > 5: break
            if not be.get("success"):
                if verbose: print(f'第{b}场战终失败: {be.get("error")[:50]}')
                break
            cleared += 1
            if verbose: print(f'第{b}场✅ 碎片+{be.get("result",{}).get("shard_reward",0)}')
        else:
            if verbose: print(f'第{b}场❌')
            break
    return cleared

if __name__ == '__main__':
    import sys as _s
    w = _s.argv[1] if len(_s.argv)>1 else 'winner_01.json'
    c = handplay(w, battles=int(_s.argv[2]) if len(_s.argv)>2 else 7)
    print(f'结果: {w} 通关{c}/7')
