"""探针：开局拿到【回锋刀】/【避风铃】后，AI 会不会为了【超频】【搏命】进扭曲都市？

结论（2026-09-13 实测）：**不会，而且不止是"没选"，是压根没有这个决策。**

1) 选区硬编码。engine/ai_player.py::PlaceholderBackend.decide 中
   `setup_choose_region` 固定返回"罪孽都市"，不读遗物/属性/任何状态。
   本探针 29 个"开局确实拿到回锋刀或避风铃"的样本，选扭曲都市 0 次。

2) 即便身处扭曲都市，也不会定向获取这两个道纹。副本专属道纹必须先经残韵
   从本副本怪物身上转化获得，而 ai_tactics._resonance_candidates 的打分只看
   **敌方道纹的威胁度**（damage/control/debuff 权重 × 威胁占比），从不评估
   "转化结果对我的构筑有没有用"，也不回看自己持有什么遗物。
   sim/build_learner 的学习清单同样写死（透支/封印两张终点牌），无遗物联动。
   实测扭曲都市 18 局：超频 0 次、搏命 1 次（残韵按威胁打分的副产物，非有意）。

协同本身是成立的（这正是可惜之处）：遗物在选区之前就已确定（见
engine/handlers/setup.py::handle_setup_choose_region 的前置校验），
【搏命】代价疲惫X → 回锋刀每失速1点打3伤、避风铃速度归零得15格挡，
信息与时序都允许做这个联动，只是决策层没实现。

用法：python3 sim/probe_region_choice_synergy.py
"""
import sys; sys.path.insert(0,'.')
from engine.api import GameEngine
from engine.ai_player import PlaceholderBackend
import collections
picks=collections.Counter(); speedpicks=collections.Counter(); got=0
be=PlaceholderBackend()
for seed in range(1,61):
    e=GameEngine(db_path=f"/tmp/rpx_{seed}.db", rng_seed=seed)
    r=e.execute_action("setup_attributes",{"name":"探针","blood_points":11,"speed_points":8,"mana_points":6})
    if not r.get("success"): continue
    cands=e.get_state()["state"].get("pending_relic_choices") or []
    if not cands: continue
    pick=next((c for c in cands if c in ("回锋刀","避风铃")), None)
    if pick is None: continue
    got+=1
    e.execute_action("choose_discovered_relic",{"relic_name":pick})
    ch=e.get_state()["state"].get("pending_initial_daowen_choices") or []
    if ch: e.execute_action("setup_choose_initial_daowen",{"daowen_name":ch[0]})
    e.execute_action("setup_choose_resonance",{"resonance_type":"转换"})
    be.engine=e
    d=be.decide(e.get_state(), e.get_available_actions(), "")
    if d.action_type=="setup_choose_region":
        picks[(pick,d.params["region"])]+=1; speedpicks[d.params["region"]]+=1
print(f"开局拿到 回锋刀/避风铃 的样本数: {got}")
print("（遗物, AI选的副本）→次数")
for k,v in picks.most_common(): print("   ",k,"→",v)
print("\n选区分布:", dict(speedpicks))
print("扭曲都市占比:", f"{speedpicks.get('扭曲都市',0)}/{sum(speedpicks.values())}")
