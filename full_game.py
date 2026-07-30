"""扭曲都市完整轮回 - 详细日志版"""
import random, math
random.seed(2025)

class Consumable:
    def __init__(self, name, uses):
        self.name = name; self.uses = uses; self.max_uses = uses
    @property
    def empty(self): return self.uses <= 0
    def use(self):
        if self.uses > 0: self.uses -= 1; return True
        return False
    def repair(self, n): self.uses = min(self.max_uses, self.uses + n)

MONSTERS = [
    ("千手蜈蚣",6,8,120,["畸变","狂暴","活力"]),
    ("骨天使",4,12,120,["变形","活力","飞行"]),
    ("肠水母",3,18,108,["僵化","庇护","自愈"]),
    ("奇美拉",2,16,120,["变形","强化","飞行"]),
    ("眼树",1,26,96,["定型","必中","再生"]),
    ("缝合鱼",3,10,132,["退化","狂暴","衰败"]),
    ("人头气球",1,22,108,["僵化","飞行","必中"]),
    ("脑蜘蛛",3,14,120,["坏死","强化","减速"]),
    ("血肉巨囊",2,20,150,["爆裂","增殖","庇护"]),
    ("爬行者",4,10,108,["超频","急速","狂暴"]),
    ("孢子母体",1,18,132,["坏死","衰败","寄生"]),
    ("畸变行者",3,14,120,["爆裂","冲击","必中"]),
]

class Monster:
    def __init__(self, t):
        self.name=t[0]; self.base_cnt=t[1]; self.base_atk=t[2]
        self.hp=t[3]; self.hp_max=t[3]; self.daowen=list(t[4])
        self.shield=0; self.atk_buf=0; self.cnt_buf=0; self.extra_atk=0
        self.dr=0; self.atk_reduction=0; self.stun=0; self.cannot_act=False
        self.reflect=False; self.degen=False; self.flying=False; self.must_hit=False
        self.heal_per_round=0; self.shield_per_round=0
        for dw in self.daowen:
            if dw=="强化": self.atk_buf+=3
            if dw=="活力": self.cnt_buf+=3
            if dw=="龙鳞": self.dr+=3
            if dw=="自愈": self.heal_per_round=int(self.hp_max*0.1)
            if dw=="庇护": self.shield_per_round=12
            if dw=="飞行": self.flying=True
            if dw=="必中": self.must_hit=True
            if dw=="爆裂": self.reflect=True
            if dw=="畸变": self.degen=True
            if dw=="变形": self.base_atk,self.base_cnt=self.base_cnt,self.base_atk
    @property
    def atk(self): return max(1,self.base_atk+self.atk_buf-self.atk_reduction)
    @property
    def cnt(self): return max(1,self.base_cnt+self.cnt_buf)
    @property
    def alive(self): return self.hp>0
    def round_start(self):
        lines=[]
        if self.heal_per_round>0:
            old=self.hp; self.hp=min(self.hp_max,self.hp+self.heal_per_round)
            if self.hp>old: lines.append(f"    {self.name}自愈: +{self.hp-old}HP -> {self.hp}/{self.hp_max}")
        if self.shield_per_round>0:
            self.shield+=self.shield_per_round
            lines.append(f"    {self.name}庇护: +{self.shield_per_round}格挡")
        if "狂暴" in self.daowen:
            self.extra_atk+=1
            lines.append(f"    {self.name}狂暴: 额外攻击+1")
        return lines
    def round_end(self):
        degen=0
        if self.degen: degen=self.cnt*self.atk
        self.shield=0; self.extra_atk=0; self.stun=max(0,self.stun-1); self.cannot_act=False
        return degen
    def get_actions(self,rd):
        base=max(1,(rd+2)//3)+self.extra_atk+self.cnt_buf
        if self.stun>0 or self.cannot_act: base=0
        return max(0,base)

MONSTER_ORIGINAL={"狂暴","强化","活力","减速","必中","自愈","飞行"}
RES_MAP={("狂暴","反转"):"自残",("强化","反转"):"弱化",("自愈","反转"):"衰败",("活力","反转"):"无力",("必中","反转"):"蒙蔽",("飞行","反转"):"坠落"}

class Player:
    def __init__(self):
        self.hp_max=48; self.hp=48; self.mp_max=20; self.mp=20
        self.speed_limit=7; self.current_speed=7; self.actions=3
        self.shield=0; self.daowen=["杀伐","庇护","再生"]
        self.spells=[{"name":"铁壁反击"},{"name":"先发制人"},{"name":"以牙还牙"},{"name":"生生不息"}]
        self.relic="守夜灯"; self.shards=20; self.res={"反转":2,"曲解":1,"转换":1}
        self.guzhi=0; self.dr=0; self.nielin=0
        self.consumables=[
            Consumable("电击枪",3), Consumable("血泵",3),
            Consumable("电池",3), Consumable("急救箱",2),
            Consumable("干扰仪",2), Consumable("手雷",2),
        ]
    @property
    def alive(self): return self.hp>0
    def has_spell(self,n): return any(s.get("name")==n for s in self.spells)
    def has_item(self,n): return any(c.name==n and not c.empty for c in self.consumables)
    def use_item(self,n):
        for c in self.consumables:
            if c.name==n and not c.empty: c.use(); return True
        return False

def battle(p, m, bn):
    log(f"\n{'='*60}")
    log(f"  vs {m.name} ({m.base_cnt}x{m.base_atk}/{m.hp_max})")
    log(f"  道纹: {', '.join(m.daowen)}")
    log(f"  玩家: HP={p.hp}/{p.hp_max} MP={p.mp_max} SPD={p.current_speed}")
    log(f"  消耗品: {', '.join(f'{c.name}({c.uses})' for c in p.consumables if not c.empty)}")
    log(f"{'='*60}")

    # 残韵
    for tw,rt in [("狂暴","反转"),("强化","反转"),("自愈","反转"),("活力","反转"),("必中","反转")]:
        if tw in m.daowen and tw in MONSTER_ORIGINAL and p.res.get(rt,0)>0:
            t=RES_MAP.get((tw,rt))
            if t and t not in p.daowen:
                p.daowen.append(t); p.res[rt]-=1
                log(f"  [残韵] {tw}--{rt}--> {t}")

    def qd(hp_lost=0):
        if not p.has_spell("千刀万剐") or "再生" not in p.daowen or "血债" not in p.daowen or hp_lost<=0: return
        cyc=0; tot=0
        while p.mp>=1 and p.hp>3 and m.alive:
            p.hp=min(p.hp_max,p.hp+2); p.mp-=1
            a=max(0,2-m.dr); m.hp=max(0,m.hp-a); p.hp-=1; tot+=a; cyc+=1
            if not m.alive or p.hp<=0 or p.mp<1: break
        if cyc>0: log(f"    [千刀万剐] {cyc}循环={tot}伤害 HP={p.hp} MP={p.mp}")

    rd=0
    while p.alive and m.alive and rd<25:
        rd+=1; p.mp=p.mp_max; p.shield=0
        log(f"\n  --- 回合{rd} ---")
        log(f"  玩家: HP={p.hp}/{p.hp_max} MP={p.mp} SPD={p.current_speed} 格挡={p.shield}")
        log(f"  {m.name}: HP={m.hp}/{m.hp_max} 格挡={m.shield}")

        # 电池
        if p.has_item("电池"):
            p.use_item("电池"); p.mp+=12
            log(f"    [电池] 法力+12 -> {p.mp}")
        # 守夜灯
        if p.relic=="守夜灯":
            b=int(p.mp_max*0.5); p.mp+=b
            log(f"    [守夜灯] 法力+{b} -> {p.mp}")
        # 怪物回始
        for l in m.round_start(): log(l)
        # 龙鳞/退化
        if "龙鳞" in p.daowen: p.dr+=2; log(f"    [龙鳞] 减伤+2 -> {p.dr}")
        if "退化" in p.daowen: m.atk_reduction+=2; log(f"    [退化] 怪物攻击-2")
        # 固执
        if "固执" in p.daowen and p.guzhi<=0:
            p.hp_max=max(1,p.hp_max-15); p.hp=min(p.hp,p.hp_max); p.guzhi=5
            log(f"    [固执5] 血限->{p.hp_max}")
        if "庇护" in p.daowen and "固执" not in p.daowen and p.res.get("曲解",0)>0:
            p.daowen[p.daowen.index("庇护")]="固执"; p.res["曲解"]-=1
            p.hp_max=max(1,p.hp_max-15); p.hp=min(p.hp,p.hp_max); p.guzhi=5
            log(f"    [残韵] 庇护->固执, 血限->{p.hp_max}")

        ac=p.actions

        # 干扰仪
        if p.has_item("干扰仪") and bn>=4:
            p.use_item("干扰仪"); m.cannot_act=True
            log(f"    [干扰仪] {m.name}沉默!")
        # 手雷
        if p.has_item("手雷") and m.alive:
            p.use_item("手雷"); m.hp=max(0,m.hp-15)
            log(f"    [手雷] 15伤害 -> {m.name}HP={m.hp}")
        # 电击枪
        if p.has_item("电击枪") and m.alive:
            p.use_item("电击枪"); d=25
            if m.flying: d+=15; m.flying=False
            m.hp=max(0,m.hp-d)
            log(f"    [电击枪] {d}伤害 -> {m.name}HP={m.hp}")

        if p.guzhi>0:
            if "血债" in p.daowen:
                a=max(0,2-m.dr); m.hp=max(0,m.hp-a); p.hp-=1
                log(f"    [血债1] {a}伤害, -1HP -> 玩家{p.hp} {m.name}{m.hp}")
                qd(1)
            for _ in range(ac):
                if not m.alive or p.mp<=0: break
                x=min(7,p.mp); a=max(0,2*x-m.dr); m.hp=max(0,m.hp-a); p.mp-=x
                log(f"    [杀伐{x}] {a}伤害 -> {m.name}HP={m.hp}")
        else:
            if p.has_spell("铁壁反击") and "庇护" in p.daowen and "杀伐" in p.daowen and ac>0:
                sx=min(5,p.mp); p.shield+=3*sx; p.mp-=sx
                ay=min(7,p.mp); a=0
                if ay>0: a=max(0,2*ay-m.dr); m.hp=max(0,m.hp-a); p.mp-=ay
                ac-=1
                log(f"    [借力打力] 庇护{sx}(+{3*sx}) 杀伐{ay}({a}伤) {m.name}HP={m.hp}")
            if p.hp<p.hp_max*0.6 and "再生" in p.daowen and ac>0:
                rx=min(4,p.mp); h=2*rx; p.hp=min(p.hp_max,p.hp+h); p.mp-=rx; ac-=1
                log(f"    [再生{rx}] +{h}HP -> {p.hp}")
            if p.has_item("血泵") and p.hp<p.hp_max*0.5:
                p.use_item("血泵"); p.hp=min(p.hp_max,p.hp+20)
                log(f"    [血泵] +20HP -> {p.hp}")
            if p.has_item("急救箱") and p.hp<p.hp_max*0.4:
                p.use_item("急救箱"); p.hp=min(p.hp_max,p.hp+25)
                log(f"    [急救箱] +25HP -> {p.hp}")
            for _ in range(ac):
                if not m.alive or p.mp<=0: break
                x=min(7,p.mp)
                if x>0: a=max(0,2*x-m.dr); m.hp=max(0,m.hp-a); p.mp-=x
                log(f"    [杀伐{x}] {a}伤害 -> {m.name}HP={p.mp}")
                if not m.alive: break

        if not m.alive: log(f"\n  *** {m.name}击败! ***"); break

        # 怪物行动
        hp_b=p.hp; ma=m.get_actions(rd)
        log(f"    怪物出手{ma}次:")
        for i in range(ma):
            if not p.alive: break
            d=m.atk
            if not m.must_hit and p.current_speed>=1 and d>15:
                p.current_speed-=1
                log(f"      [{i+1}] {d}伤 -> 闪避! SPD->{p.current_speed}")
                if p.relic=="避风铃": p.shield+=3
                continue
            if p.guzhi>0: d=min(d,1)
            a=max(0,d-p.dr); p.hp=max(0,p.hp-a)
            log(f"      [{i+1}] {d}伤(减伤{p.dr})={a} -> HP={p.hp}")
            if m.reflect and a>0: m.hp=max(0,m.hp-a); log(f"      [爆裂] 反伤{a}")
            if "逆鳞" in p.daowen and a>0: p.nielin+=a
            if "伤痕" in p.daowen and a>0: m.hp_max=max(1,m.hp_max-2); m.hp=min(m.hp,m.hp_max)

        if p.nielin>0 and m.alive:
            ni=max(0,p.nielin-m.dr); m.hp=max(0,m.hp-ni)
            log(f"    [逆鳞] {p.nielin}层反击{ni}伤害 -> {m.name}HP={m.hp}")
            p.nielin=0

        degen=m.round_end()
        if degen>0:
            p.hp_max=max(1,p.hp_max-degen); p.hp=min(p.hp,p.hp_max)
            log(f"    [畸变] 血限-{degen} -> {p.hp_max}")

        hl=hp_b-p.hp
        if hl>0 and p.alive: qd(hl)
        if p.guzhi>0: p.guzhi-=1
        log(f"  回终: 玩家{p.hp}/{p.hp_max} {m.name}{m.hp}/{m.hp_max}")

    if not p.alive: log(f"\n  *** 玩家阵亡 ***")
    return {"won":not m.alive,"hp":p.hp,"hp_max":p.hp_max}

# 主循环
log_lines = []
def log(msg): log_lines.append(msg); print(msg)

log("="*60)
log("  扭曲都市 · 完整轮回记录")
log("  combo: 杀伐+庇护+再生 (残韵获取转化道纹)")
log("  遗物: 守夜灯")
log("  消耗品: 电击枪/血泵/电池/急救箱/干扰仪/手雷")
log("="*60)

p=Player()

for bn in range(1,8):
    if bn>1:
        log(f"\n{'='*60}")
        log(f"  局外阶段 (第{bn}场前) HP={p.hp}/{p.hp_max} 碎片={p.shards}")
        log(f"{'='*60}")
        for c in p.consumables:
            if not c.empty: c.repair(1)
        log(f"  [维修] 耐久+1")
        p.mp_max+=2; p.hp_max+=2; p.hp+=2
        log(f"  [修行] 法限->{p.mp_max} 血限->{p.hp_max}")
        for i in range(2):
            if p.shards>=10:
                p.shards-=10; h=min(24,p.hp_max-p.hp); p.hp+=h
                log(f"  [休整] -10碎片 +{h}HP -> HP={p.hp} 碎片={p.shards}")
            else:
                h=min(8,p.hp_max-p.hp); p.hp+=h
                log(f"  [休整] +{h}HP -> HP={p.hp}")
        p.actions=max(1,math.ceil(p.speed_limit/3))

    cnt=max(1,bn-2)
    mts=[random.choice(MONSTERS) for _ in range(cnt)]
    log(f"\n{'#'*60}")
    log(f"  第{bn}场 ({cnt}怪): {', '.join(t[0] for t in mts)}")
    log(f"{'#'*60}")

    ok=True
    for mt in mts:
        m=Monster(mt)
        r=battle(p,m,bn)
        if r["won"]:
            sh=max(1,int(mt[3]*0.02)); p.shards+=sh; p.hp=r["hp"]; p.hp_max=r["hp_max"]
            log(f"  碎片+{sh} -> {p.shards}")
        else:
            log(f"\n  === 轮回终结于第{bn}场 ===")
            ok=False; break
    if not ok: break

log(f"\n{'='*60}")
log(f"  最终: HP={p.hp}/{p.hp_max} 碎片={p.shards}")
log(f"  道纹: {p.daowen}")
log(f"  消耗品: {', '.join(f'{c.name}({c.uses}/{c.max_uses})' for c in p.consumables)}")
log(f"{'='*60}")

with open("battle_log.txt","w") as f:
    f.write("\n".join(log_lines))
print("\n日志已保存到 battle_log.txt")
