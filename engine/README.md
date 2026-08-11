# 第四宇宙 · 临济·第四宇宙 游戏引擎

## 核心架构

```
AI（决策者）──→ GameEngine API ──→ 计算/随机数
                    │
                    ├── 遇到可判定情况 → 返回结果 → AI继续决策
                    │
                    └── 遇到特殊事件 → 抛出Interrupt → DM裁定
                                                      │
                                                      ├── 存入数据库
                                                      └── 下次自动匹配
```

## 核心原则

1. **AI是决策者，程序是事实源** — 所有数值计算、随机数必须通过引擎
2. **中断机制** — 程序无法判定时（急中生智/逃跑等），抛出Interrupt等待DM
3. **先例数据库** — DM裁定存入SQLite，下次类似场景自动匹配
4. **自由控X** — 道纹的X值由AI在合法范围内自由指定

## 文件结构

```
engine/
├── __init__.py          # 包初始化
├── enums.py             # 枚举定义（阶段、触发时点、代价类型等）
├── models.py            # 数据模型（Entity, Spell, Relic等）
├── dice.py              # 随机数引擎（池系统，auto_roll默认由引擎自身生成随机数并结算，可传seed复现）
├── monsters.py          # 怪物池解析(从README解析36怪物面板)与出怪(战始抽怪)公式
├── daowen.py            # 道纹系统（所有道纹效果的数学计算）
├── combat.py            # 战斗计算引擎（伤害、回合、闪避等）
├── dm_rulings.py        # DM裁定数据库（SQLite + 全文搜索）
└── api.py               # GameEngine主类 — AI的唯一交互入口
```

## AI交互流程

### 1. 获取状态
```python
state = engine.get_state()
# 返回：当前状态、可用行动、待处理中断、上次结果
```

### 2. 执行行动
```python
result = engine.execute_action("use_daowen", {
    "daowen_name": "杀伐",
    "x": 5,
    "target": "千手蜈蚣"
})
```

### 3. 处理中断
```python
# 如果result中有interrupt，需要DM裁定
if result.get("interrupt"):
    # 提交DM裁定
    engine.submit_ruling(
        interrupt_type="急中生智",
        ruling_text="利用蒸汽遮蔽视线",
        ruling_data={"effect": "怪物下回合无法选中目标"}
    )
```

### 4. 查询先例
```python
# AI可以在触发特殊事件前查询是否有先例
precedent = engine.check_precedent("急中生智", {"target": "千手蜈蚣"})
if precedent["found"]:
    # 直接应用先例
    ...
```

### 5. 随机数（2026-08-09起：引擎自动结算，不再要求玩家提供数字）
```python
# 游戏内实际随机行动（探索/共鸣/开局遗物等）均由引擎内部调用 DiceEngine.auto_roll()
# 直接生成随机数并结算，AI拿到的是已经确定的结果，无需再向玩家索要数字：
result = engine.execute_action("pre_battle_action", {"sub_action": "探索"})
# result["result"]["event"] 已经是引擎自动摇出的具体事件名

# 历史遗留的手动流程仍保留，仅用于DM需要强制指定结果的调试/裁定场景：
pool = engine.request_random("event_pool", ["事件A", "事件B", "事件C"])
# pool["range"] = "1~3"
result = engine.execute_action("random_number", {"pool_name": "event_pool", "number": 2})

# 需要可复现的随机结果时（例如回归测试），在构造引擎时传入固定种子：
engine = GameEngine(rng_seed=12345)
```

## 撤退机制

已实现，详见 AI_EXPERIENCE.md（设计要点/取舍记录）。要点：仅[朋友]/[员工]适用，
`has_retreated`标记生效期为"本场战斗"，[战终]重置。

## 出手预算校验

已实现，详见 AI_EXPERIENCE.md。要点：`action_count`按entity_type分流公式，
消耗/不消耗出手的动作清单见下表备注。

## 最终的冠冕 / 第8场死斗

已实现，详见 AI_EXPERIENCE.md。要点：`GameEngine(sealed_candidate_path=...)`指定跨实例共享的
封存候选人JSON路径；[战终]第7场自动判定"封存"或"进入死斗"，无需额外调用。

## 三副本终音法器 / 初拥之夜 / 真龙之心

已实现，详见 AI_EXPERIENCE.md。要点：死斗胜利后按`current_region`从`GameEngine.TERMINAL_ARTIFACTS`
对应列表中选1件（`choose_terminal_artifact`），选到"猩红尖牙"会先强制触发初拥之夜
（`GameEngine.FIRST_EMBRACE_OPTIONS`9选1，`choose_first_embrace`）完成后才真正封存；
"真龙之心"解锁后进入独立的龙性资源/8遗物系统（`GameEngine.DRAGON_NATURE_RATE`/`DRAGON_TRAITS`）。

## 行动类型一览

| 行动类型 | 说明 |
|---------|------|
| `setup_attributes` | 分配初始25属性点 |
| `setup_choose_daowen` | 选择初始道纹（杀伐/锐利） |
| `setup_choose_resonance` | 选择初始残韵 |
| `setup_choose_region` | 选择副本 |
| `pre_battle_action` | 局外行动（领悟/休整/修行/学习/共鸣/探索/忘忧(需持有忘忧香)/献祭(需持有红头绳)） |
| `use_daowen` | 发动道纹（可选actor：留空=玩家自身法力制发动；指定[朋友]/[员工]名=听从指令发动，免法力只消耗出手，且必须指定非自身目标） |
| `use_spell` | 发动法术 |
| `use_resonance` | 使用残韵 |
| `attack` | 普通攻击（attacker可指定为已部署[朋友]/[员工]，目标自动限定为对方阵营） |
| `deploy_employee` | 派遣[员工]出战(出战支援，消耗1出手) |
| `dismiss_employee` | 解雇[员工](自由行动，无代价) |
| `pay_employee_wage` | 战终对某[员工]的工资做出pay/refuse决策 |
| `choose_hired_daowen` | 雇佣diy后，从3个发现的转化道纹候选中选择1个 |
| `suppress_rebellion` | 员工叛变·镇压：叛变员工搬入state.enemies开战 |
| `resolve_rebellion_battle` | 镇压结算(outcome=victory/defeat) |
| `appease_rebellion` | 员工叛变·让利：全局工资+5，平息叛乱 |
| `negotiate_rebellion` | 员工叛变·急中生智谈判：抛Interrupt交DM裁定 |
| `resolve_final_duel` | 最终的冠冕·第8场死斗结算(outcome=victory/defeat)，[战终]第7场自动触发死斗本身 |
| `choose_terminal_artifact` | 死斗胜利后按副本领取终音法器(choice=序号) |
| `choose_first_embrace` | 初拥之夜9选1(choice=1~9，1~8限选1次，9可重复) |
| `use_black_card` | 黑金名片(罪孽都市终音)：敌方血限减半，等量碎片(允许负债≤50) |
| `use_crime_vault` | 罪业金库(罪孽都市终音)：消耗X碎片(≤2%当前碎片)换2X格挡 |
| `fire_godfather_revolver` | 教父左轮(罪孽都市终音)：对target打出30%血限×本场使用次数的必中伤害 |
| `select_shared_dragon_heart` | 共心环(龙心谷终音)：选定本场共享的龙心类型 |
| `declare_fuyuebei_toll` | 负岳碑(龙心谷终音)：预声明保护某[朋友]/[员工]下次撤退 |
| `pay_for_dragon_nature` | 真龙之心：支付衰老/枯竭/萎缩换龙性(cost_type,x) |
| `unlock_dragon_trait` | 真龙之心：花12龙性解锁1种龙族遗物(trait) |
| `activate_dragon_body` | 震岳龙躯：花6X龙性，激活X回合的15点伤害上限护体 |
| `devour_monster` | 吞骸龙胃：吞噬已命零的怪物，回复12+可选龙心+6耐久 |
| `declare_tail_sacrifice` | 断尾求生：预声明命零时愿意移除的其他龙族遗物 |
| `use_dragon_wings` | 烬翼：花3X龙性获得飞行X |
| `use_blood_wings` | 初拥之夜·鲜血之翼：流血5X获得飞行X |
| `enslave_as_chizu` | 初拥之夜·血族尖牙：衰老20，将生命低于自身的目标转化为赤族 |
| `use_truth_eye` | 初拥之夜·真理眼：抛Interrupt交DM裁定，冷却2场 |
| `blood_feast` | 初拥之夜·血食：命零一名赤族，回复等量生命 |
| `retreat_via_toll` | 买路财：真正执行安全撤退(需持有该遗物) |
| `dodge_decision` | 闪避决策 |
| `consume_item` | 使用消耗品 |
| `declare_wit` | 声明急中生智 |
| `declare_escape` | 声明逃跑 |
| `declare_evolution` | 怪物进化：发动【原初X】借用原始怪物道纹（引擎直接结算） |
| `round_start` | 回始结算 |
| `round_end` | 回终结算 |
| `battle_start` | 战始（自动出怪：数量=战斗场数-3(最低1)，从当前副本12怪物池随机抽取，允许重复；战斗背景纯叙事不做机制化） |
| `battle_end` | 战终 |
| `random_number` | 手动提交随机数（历史遗留手动覆盖入口，非默认流程；默认流程由引擎自动摇号） |

## 运行

```bash
# 交互式命令行
python main.py

# API演示
python main.py --api

# 运行测试
python tests/test_engine.py
```
