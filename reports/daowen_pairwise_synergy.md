# 道纹两两协同穷举分析（观测报告）

> 仓库 HEAD：`85033d5741b6257130ed2588da1543535e74c7d6`｜分析对象：**当前生产道纹词汇表**（`engine/daowen.py` `DaoWenEngine._registry`）
> 道纹 N = **70**｜无序对 C(N,2) = **2415**｜统一发动 X = 3｜耗时见 `--all` 运行日志（本文件由场景缓存重算，分类口径不变）
> 复现：`python sim/daowen_pairwise_analysis.py --all`
> 被分析的引擎源码指纹：`engine_src_sha256=e1a336ecdd1ebc0c`（对 `engine/**/*.py` 排序后拼接取 sha256；用于核对分析对象没被换过）

> 本文件只做**测**，不做改。未修改任何道纹定义、数值、代价或规则。

## 1. 方法与口径

* **词汇表来源**：`engine/daowen.py::DaoWenEngine._registry`（生产注册表）。文档（`全道纹索引.md`）只用于交叉核对，凡与代码不一致处按代码记录，差异在第 9 节列出。
* **执行**：生产 `GameEngine`。发动走 `use_daowen`；伤害走 `CombatEngine._apply_hostile_damage`（引擎统一敌对伤害入口）；代价走 `pay_numeric_cost`；回复走 `GameState.apply_heal`；速度/血限走 `_lose_current_speed` / `_apply_blood_limit_change`；敌方攻击走真实两阶段怪物阶段 `prepare_monster_phase` + `resolve_monster_phase`。
* **没有第二套战斗引擎**：分析脚本只调用上述生产入口并读取生产返回值。
* **受控沙盒**（常量，非规则）：轮回者 生命 200/400、法力 55/60（留缺口使「回复/得法力」可见，且够两次最贵发动）、速度 5/5；敌方 生命 3000/3000、法力 20/20、速度 3/3（其道纹清空，使普攻恒为 3 击 × 20 点）。
* **探针**（17 个，每个跑在独立 deepcopy 上）：`incoming_big`、`incoming_multi`、`enemy_phase`、`enemy_phase_dodge`、`outgoing_big`、`outgoing_multi`、`bleed_cost`、`heal`、`kill_enemy`、`rounds`、`speed_loss`、`bl_loss`、`mana_cycle`、`player_attacks`、`player_attacks_dodged`、`hurt_then_attack`、`dodge_then_round`
* **发动侧（场景参数）**：按生产 `summary` 里「效果的受益方」定侧——summary 明确把伤害/减益落在未选定目标身上的 29 个道纹对敌发动（`HOSTILE_SIDE`），其余对自身发动；两侧各跑一次 solo，两侧可见度都记进 `daowen_synergy_graph.json`（`visibility`/`alt_visibility`）。**这是场景选择，不是规则改动**：生产引擎没有任何目标限制元数据，「对敌放庇护」这类非打法不进入判定，否则会造出与道纹对无关的假交互。
* **发动消耗出手**：生产规则里每次 `use_daowen` 都吃 1 次出手（`api.py::_action_budget_of`），所以任何两次独立发动都会等比减少本回合普攻次数。攻击类探针先把
  `actions_used_this_round` 归零再打（只归零已用计数，不改 `action_count`），把「独立发动的公共代价」从效果层交互里剥离。
* **执行门禁**：若某次发动被引擎拒绝、或发动后留下待 DM 裁定的中断（`api.py:1084` 会挡住同场景后续动作），该对记 `confidence=UNVERIFIED`，不计入结论统计，只在第 6 节列出原因。
* **分类判据**（对每个探针 × 每个可观测通道）：
  * 设 A 独发、B 独发相对空基线的增量为 `a`、`b`，并施为 `ab`。
  * `ab == a + b` → 可加；两者都动同一通道 → **1 加和型**；从不共触同一通道 → **0 独立型**。
  * `ab != a + b`（或事件类型/计数出现新东西）→ **2 非平凡协同**，并记录因果串。
* **分类 2 不等于「设计得好」**：本文件只报告测量结果。

## 2. 总量

| 分类 | 对数 | 占比 |
| --- | ---: | ---: |
| 0 独立型 | 666 | 27.6% |
| 1 加和型 | 1015 | 42.0% |
| 2 非平凡协同（可验证） | 635 | 26.3% |
| 2' 判定为 2 但执行受限（UNVERIFIED，不计入结论） | 99 | 4.1% |
| 合计 | 2415 | 100% |

> 每个对的 `classification` 只取 0/1/2 一个值；`2'` 是把「分类 2 但执行没有忠实复现」的对单独列出来（CSV 里 `confidence=UNVERIFIED`），它们不参与第 10 节任何计数。

参战双方分属互斥副本、单局内不可能同时持有（仍照跑，只作可达性标注）的对：
**384** 对。

## 3. 交互类型分布（仅分类 2，可验证）

| 类型标签 | 对数 | 含义 |
| --- | ---: | --- |
| OUTPUT_ARITHMETIC | 631 | 结算层数值耦合（族 F：乘性/上限/下限；本引擎里 攻击力=当前法力、攻击次数=当前速度） |
| CAST_EXECUTION | 138 | 发动本身的代价/数值/结果被同伴改变（大纲族 A：输出→输入） |
| ORDER_SENSITIVE | 90 | A→B 与 B→A 结果不同（族 D：结算顺序；不视为 bug） |
| RESOURCE_FEEDBACK | 65 | 偏离发生在资源通道上（族 E：法力/格挡/碎片/出手） |
| EVENT_CONVERSION | 14 | 出现两侧独发都没有的新事件类型（族 C） |
| EVENT_MULTIPLICATION | 13 | 事件计数超出两侧独发之和（族 B） |

按**首个命中层级**分层（每个对只归一类，用于区分「结算层数值耦合」与「结构层交互」）：

| 层级 | 对数 | 判据 |
| --- | ---: | --- |
| EVENT_LEVEL | 27 | 出现新事件类型或事件计数超可加 |
| CAST_EXECUTION | 134 | 某次发动的自身增量被同伴改变（代价/数值/结果） |
| ORDER_ONLY | 5 | 两序结果不同且无上述两类 |
| SCALAR_COUPLING | 469 | 只有结算层数值耦合（无事件、无发动期差异、两序一致） |

> `SCALAR_COUPLING` 之所以仍计入分类 2：本引擎的攻击力=当前法力、攻击次数=当前速度是**乘法关系**，两次发动对同一池的加减在结算上不可加（大纲族 F）。这不是「另一个数值修正符」——后者指两次互不相干的独立加成。

命中的探针分布（一个对可命中多个）：

| 探针 | 命中对数 |
| --- | ---: |
| `hurt_then_attack` | 345 |
| `player_attacks` | 296 |
| `rounds` | 277 |
| `dodge_then_round` | 202 |
| `__cast__` | 138 |
| `enemy_phase` | 127 |
| `enemy_phase_dodge` | 64 |
| `kill_enemy` | 18 |
| `incoming_big` | 17 |
| `incoming_multi` | 16 |
| `player_attacks_dodged` | 14 |
| `outgoing_big` | 6 |
| `outgoing_multi` | 6 |
| `speed_loss` | 4 |
| `mana_cycle` | 2 |
| `heal` | 1 |

## 4. 度数分布（Cat2 图）

* 平均度 **18.14**｜中位度 **12**｜最大度 **64**
* 零 Cat2 伙伴的道纹 **1** 个：尸爆

| 道纹 | Cat2 伙伴数 | 副本归属 |
| --- | ---: | --- |
| 洞察 | 64 | 通用/怪物 |
| 搏命 | 64 | 扭曲都市 |
| 寄生 | 54 | 通用/怪物 |
| 全力 | 54 | 通用/怪物 |
| 愤怒 | 53 | 通用/怪物 |
| 借力 | 53 | 通用/怪物 |
| 赌命 | 51 | 罪孽都市 |
| 疯狂 | 46 | 通用/怪物 |
| 封印 | 37 | 通用/怪物 |
| 无神 | 32 | 通用/怪物 |
| 眩晕 | 29 | 通用/怪物 |
| 滑翔 | 28 | 通用/怪物 |
| 嫁祸 | 28 | 龙心谷 |
| 活血 | 27 | 龙心谷 |
| 飞行 | 24 | 通用/怪物 |
| 变形 | 24 | 扭曲都市 |
| 龙鳞 | 23 | 龙心谷 |
| 裂变 | 23 | 龙心谷 |
| 蒙蔽 | 23 | 通用/怪物 |
| 爆裂 | 23 | 扭曲都市 |
| 弱化 | 22 | 通用/怪物 |
| 束缚 | 21 | 通用/怪物 |
| 自愈 | 20 | 通用/怪物 |
| 波及 | 20 | 通用/怪物 |
| 庇护 | 20 | 通用/怪物 |

## 5. 发动顺序敏感的对

共 **90** 对在 A→B 与 B→A 下结果不同（**不判定为 bug**，只记录分歧发生在哪个探针）；其中执行受限的对已剔除。

| A | B | 分歧探针 | 类型 |
| --- | --- | --- | --- |
| 杀伐 | 愤怒 | `player_attacks（共3个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC|RESOURCE_FEEDBACK |
| 杀伐 | 借力 | `outgoing_big（共4个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 杀伐 | 眩晕 | `enemy_phase（共9个通道分歧）` | ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 杀伐 | 寄生 | `incoming_big（共7个通道分歧）` | ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 杀伐 | 加害 | `outgoing_big（共4个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 杀伐 | 瓦解 | `outgoing_big（共4个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 再生 | 愤怒 | `player_attacks（共3个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC|RESOURCE_FEEDBACK |
| 庇护 | 波及 | `rounds（共3个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 庇护 | 愤怒 | `player_attacks（共3个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC|RESOURCE_FEEDBACK |
| 血债 | 眩晕 | `enemy_phase（共9个通道分歧）` | ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 血债 | 寄生 | `incoming_big（共7个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 血债 | 加害 | `outgoing_big（共4个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 血债 | 伤痕 | `outgoing_big（共4个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 血债 | 瓦解 | `outgoing_big（共4个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 波及 | 全力 | `rounds（共3个通道分歧）` | CAST_EXECUTION|EVENT_CONVERSION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 波及 | 愤怒 | `rounds（共6个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC|RESOURCE_FEEDBACK |
| 波及 | 借力 | `enemy_phase（共5个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 波及 | 兴奋 | `rounds（共3个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 波及 | 全速 | `rounds（共3个通道分歧）` | CAST_EXECUTION|EVENT_CONVERSION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 波及 | 急速 | `enemy_phase_dodge（共4个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 波及 | 加速 | `rounds（共3个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 波及 | 洞察 | `rounds（共2个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 波及 | 定型 | `rounds（共3个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 波及 | 爆裂 | `incoming_big（共12个通道分歧）` | CAST_EXECUTION|EVENT_CONVERSION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 波及 | 赌命 | `rounds（共3个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 波及 | 龙鳞 | `incoming_big（共10个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 波及 | 逆鳞 | `rounds（共4个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 波及 | 活血 | `rounds（共3个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 波及 | 裂变 | `enemy_phase（共5个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 波及 | 背负 | `__cast__` | CAST_EXECUTION|ORDER_SENSITIVE |
| 波及 | 缄默 | `rounds（共3个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 增殖 | 愤怒 | `player_attacks（共3个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC|RESOURCE_FEEDBACK |
| 增殖 | 滋养 | `incoming_big（共5个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE |
| 贯穿 | 愤怒 | `player_attacks（共3个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC|RESOURCE_FEEDBACK |
| 封印 | 疯狂 | `__cast__` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 净化 | 愤怒 | `player_attacks（共3个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC|RESOURCE_FEEDBACK |
| 减速 | 变形 | `enemy_phase（共4个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC|RESOURCE_FEEDBACK |
| 减速 | 冥气 | `__cast__` | CAST_EXECUTION|ORDER_SENSITIVE |
| 飞行 | 坠落 | `enemy_phase（共7个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC |
| 愤怒 | 自残 | `player_attacks（共3个通道分歧）` | CAST_EXECUTION|ORDER_SENSITIVE|OUTPUT_ARITHMETIC|RESOURCE_FEEDBACK |
| … | 其余 50 对见 CSV | | |

## 6. 执行受限的对（UNVERIFIED，不计入结论）

原因只有两类：某次发动被生产引擎拒绝，或发动后留下待 DM 裁定的中断（`api.py:1084` 门禁会挡住同场景后续动作）。这类对**不做猜测**，只登记原因。

| 原因 | 对数 |
| --- | ---: |
| 尸爆（自毁型，`self_destruct: true`）发动后[命零]触发死之传承中断，同场景后续发动被门禁挡住 | 207 |
| 后手发动时目标已不合法（先手改变/移除了目标集合） | 28 |
| 波及的显式目标数在提交瞬间与合法目标数不一致 | 1 |
| 搏命发动失败(甲无法完整承担疲惫3（可支付2）) | 1 |
| 洞察发动失败(甲无法完整承担疲惫3（可支付2）) | 1 |

| A | B | 涉及场景 |
| --- | --- | --- |
| 杀伐 | 封印 | 后手发动时目标已不合法（先手改变/移除了目标集合） |
| 杀伐 | 尸爆 | 尸爆（自毁型，`self_destruct: true`）发动后[命零]触发死之传承中断，同场景后续发动被门禁挡住 |
| 再生 | 尸爆 | 尸爆（自毁型，`self_destruct: true`）发动后[命零]触发死之传承中断，同场景后续发动被门禁挡住 |
| 庇护 | 尸爆 | 尸爆（自毁型，`self_destruct: true`）发动后[命零]触发死之传承中断，同场景后续发动被门禁挡住 |
| 固执 | 尸爆 | 尸爆（自毁型，`self_destruct: true`）发动后[命零]触发死之传承中断，同场景后续发动被门禁挡住 |
| 血债 | 封印 | 后手发动时目标已不合法（先手改变/移除了目标集合） |
| 血债 | 尸爆 | 尸爆（自毁型，`self_destruct: true`）发动后[命零]触发死之传承中断，同场景后续发动被门禁挡住 |
| 波及 | 封印 | 波及的显式目标数在提交瞬间与合法目标数不一致 |
| 波及 | 尸爆 | 尸爆（自毁型，`self_destruct: true`）发动后[命零]触发死之传承中断，同场景后续发动被门禁挡住 |
| 增殖 | 尸爆 | 尸爆（自毁型，`self_destruct: true`）发动后[命零]触发死之传承中断，同场景后续发动被门禁挡住 |
| 束缚 | 封印 | 后手发动时目标已不合法（先手改变/移除了目标集合） |
| 束缚 | 尸爆 | 尸爆（自毁型，`self_destruct: true`）发动后[命零]触发死之传承中断，同场景后续发动被门禁挡住 |
| 透支 | 尸爆 | 尸爆（自毁型，`self_destruct: true`）发动后[命零]触发死之传承中断，同场景后续发动被门禁挡住 |
| 贯穿 | 尸爆 | 尸爆（自毁型，`self_destruct: true`）发动后[命零]触发死之传承中断，同场景后续发动被门禁挡住 |
| 封印 | 减速 | 后手发动时目标已不合法（先手改变/移除了目标集合） |
| 封印 | 自残 | 后手发动时目标已不合法（先手改变/移除了目标集合） |
| 封印 | 无神 | 后手发动时目标已不合法（先手改变/移除了目标集合） |
| 封印 | 弱化 | 后手发动时目标已不合法（先手改变/移除了目标集合） |
| 封印 | 无力 | 后手发动时目标已不合法（先手改变/移除了目标集合） |
| 封印 | 眩晕 | 后手发动时目标已不合法（先手改变/移除了目标集合） |
| 封印 | 蒙蔽 | 后手发动时目标已不合法（先手改变/移除了目标集合） |
| 封印 | 衰败 | 后手发动时目标已不合法（先手改变/移除了目标集合） |
| 封印 | 寄生 | 后手发动时目标已不合法（先手改变/移除了目标集合） |
| 封印 | 坠落 | 后手发动时目标已不合法（先手改变/移除了目标集合） |
| 封印 | 变形 | 后手发动时目标已不合法（先手改变/移除了目标集合） |
| 封印 | 畸变 | 后手发动时目标已不合法（先手改变/移除了目标集合） |
| 封印 | 坏死 | 后手发动时目标已不合法（先手改变/移除了目标集合） |
| 封印 | 退化 | 后手发动时目标已不合法（先手改变/移除了目标集合） |
| 封印 | 加害 | 后手发动时目标已不合法（先手改变/移除了目标集合） |
| 封印 | 逼债 | 后手发动时目标已不合法（先手改变/移除了目标集合） |
| … | 其余 69 对见 CSV | |

## 7. 代表样本（含事件序列）

### 杀伐（发动侧 enemy:0）+ 加害（发动侧 enemy:0）｜分类 2

因果串：

```
__cast__ / e_hp：基线=0（增量 0）｜杀伐独发=-9（-9）｜加害独发=0（+0）｜A→B=-9（-9）｜B→A=-12（-12）｜可加预期增量=-9
```

事件序列对照（命中通道）：

```
  基线      | __cast__/e_hp : 0
  杀伐独发  | __cast__/e_hp : -9
  加害独发  | __cast__/e_hp : 0
  杀伐→加害 | __cast__/e_hp : -9
  加害→杀伐 | __cast__/e_hp : -12

  基线      | kill_enemy/e_hp : -3000
  杀伐独发  | kill_enemy/e_hp : -2991
  加害独发  | kill_enemy/e_hp : -3000
  杀伐→加害 | kill_enemy/e_hp : -2991
  加害→杀伐 | kill_enemy/e_hp : -2988

```

### 杀伐（发动侧 enemy:0）+ 瓦解（发动侧 enemy:0）｜分类 2

因果串：

```
__cast__ / e_hp：基线=0（增量 0）｜杀伐独发=-9（-9）｜瓦解独发=-900（-900）｜A→B=-900（-900）｜B→A=-909（-909）｜可加预期增量=-909
```

事件序列对照（命中通道）：

```
  基线      | __cast__/e_hp : 0
  杀伐独发  | __cast__/e_hp : -9
  瓦解独发  | __cast__/e_hp : -900
  杀伐→瓦解 | __cast__/e_hp : -900
  瓦解→杀伐 | __cast__/e_hp : -909

  基线      | kill_enemy/e_hp : -3000
  杀伐独发  | kill_enemy/e_hp : -2991
  瓦解独发  | kill_enemy/e_hp : -2100
  杀伐→瓦解 | kill_enemy/e_hp : -2100
  瓦解→杀伐 | kill_enemy/e_hp : -2091

```

### 血债（发动侧 enemy:0）+ 加害（发动侧 enemy:0）｜分类 2

因果串：

```
__cast__ / e_hp：基线=0（增量 0）｜血债独发=-3（-3）｜加害独发=0（+0）｜A→B=-3（-3）｜B→A=-12（-12）｜可加预期增量=-3
```

事件序列对照（命中通道）：

```
  基线      | __cast__/e_hp : 0
  血债独发  | __cast__/e_hp : -3
  加害独发  | __cast__/e_hp : 0
  血债→加害 | __cast__/e_hp : -3
  加害→血债 | __cast__/e_hp : -12

  基线      | kill_enemy/e_hp : -3000
  血债独发  | kill_enemy/e_hp : -2997
  加害独发  | kill_enemy/e_hp : -3000
  血债→加害 | kill_enemy/e_hp : -2997
  加害→血债 | kill_enemy/e_hp : -2988

```

### 血债（发动侧 enemy:0）+ 伤痕（发动侧 enemy:0）｜分类 2

因果串：

```
__cast__ / e_hp：基线=0（增量 0）｜血债独发=-3（-3）｜伤痕独发=0（+0）｜A→B=-3（-3）｜B→A=-9（-9）｜可加预期增量=-3
```

事件序列对照（命中通道）：

```
  基线      | __cast__/e_hp : 0
  血债独发  | __cast__/e_hp : -3
  伤痕独发  | __cast__/e_hp : 0
  血债→伤痕 | __cast__/e_hp : -3
  伤痕→血债 | __cast__/e_hp : -9

  基线      | kill_enemy/e_hp : -3000
  血债独发  | kill_enemy/e_hp : -2997
  伤痕独发  | kill_enemy/e_hp : -3000
  血债→伤痕 | kill_enemy/e_hp : -2997
  伤痕→血债 | kill_enemy/e_hp : -2991

```

### 血债（发动侧 enemy:0）+ 瓦解（发动侧 enemy:0）｜分类 2

因果串：

```
__cast__ / e_hp：基线=0（增量 0）｜血债独发=-3（-3）｜瓦解独发=-900（-900）｜A→B=-900（-900）｜B→A=-903（-903）｜可加预期增量=-903
```

事件序列对照（命中通道）：

```
  基线      | __cast__/e_hp : 0
  血债独发  | __cast__/e_hp : -3
  瓦解独发  | __cast__/e_hp : -900
  血债→瓦解 | __cast__/e_hp : -900
  瓦解→血债 | __cast__/e_hp : -903

  基线      | kill_enemy/e_hp : -3000
  血债独发  | kill_enemy/e_hp : -2997
  瓦解独发  | kill_enemy/e_hp : -2100
  血债→瓦解 | kill_enemy/e_hp : -2100
  瓦解→血债 | kill_enemy/e_hp : -2097

```

### 自残（发动侧 enemy:0）+ 弱化（发动侧 enemy:0）｜分类 2

因果串：

```
__cast__ / e_hp：基线=0（增量 0）｜自残独发=-60（-60）｜弱化独发=0（+0）｜A→B=-60（-60）｜B→A=-51（-51）｜可加预期增量=-60
```

事件序列对照（命中通道）：

```
  基线      | __cast__/e_hp : 0
  自残独发  | __cast__/e_hp : -60
  弱化独发  | __cast__/e_hp : 0
  自残→弱化 | __cast__/e_hp : -60
  弱化→自残 | __cast__/e_hp : -51

  基线      | kill_enemy/e_hp : -3000
  自残独发  | kill_enemy/e_hp : -2940
  弱化独发  | kill_enemy/e_hp : -3000
  自残→弱化 | kill_enemy/e_hp : -2940
  弱化→自残 | kill_enemy/e_hp : -2949

```

### 杀伐（发动侧 enemy:0）+ 再生（发动侧 player:0）｜分类 1

因果串：

```
（无偏离，判为 0/1 型）
```

事件序列对照（命中通道）：

```
  基线      | player_attacks/e_hp : -550
  杀伐独发  | player_attacks/e_hp : -520
  再生独发  | player_attacks/e_hp : -520
  杀伐→再生 | player_attacks/e_hp : -490
  再生→杀伐 | player_attacks/e_hp : -490

```

### 杀伐（发动侧 enemy:0）+ 固执（发动侧 player:0）｜分类 0

因果串：

```
（无偏离，判为 0/1 型）
```

事件序列对照（命中通道）：

```
  基线      | player_attacks/e_hp : -550
  杀伐独发  | player_attacks/e_hp : -520
  固执独发  | player_attacks/e_hp : -550
  杀伐→固执 | player_attacks/e_hp : -520
  固执→杀伐 | player_attacks/e_hp : -520

```

## 8. 校验

* 对数 = C(70,2) = 2415，实际生成 2415 对，无重复（见第 10 节校验输出）。
* 每个道纹都出现在矩阵中（`出现次数 = N-1`）。
* 分类 2 的对全部带可执行证据 ID（`A+B@探针`），可用 `--pair A,B` 单对重放。
* 执行受限（发不出手/留下待裁定中断）的对 **99** 个，全部标 `confidence=UNVERIFIED`，未计入任何结论数字（清单见第 6 节）。

## 9. 与文档的交叉核对

* 生产注册表 70 个道纹中，索引文档未列出的：无
* 索引文档列出但生产注册表没有的：无
* `summary` 文案与实际返回字段不一致的道纹 **32** 个（文案只是提示串，结算读的是字段值）：
  * `波及.cost=6 vs summary「消耗9法力，选择3个目标建立/解除波及效果（持续∞）」`
  * `贯穿.cost=6 vs summary「消耗15法力，造成的伤害无视格挡，持续3回合」`
  * `净化.cost=6 vs summary「消耗15法力，使未选定目标【异变】-3层」`
  * `愤怒.cost=6 vs summary「消耗15法力，使未选定目标法力消耗减半，持续3回合」`
  * `自残.cost=9 vs summary「消耗30法力，使未选定目标对自身打出3次攻击」`
  * `无神.cost=15 vs summary「消耗60法力，使未选定目标选择目标时强制改为自身，持续3回合」`
  * `借力.cost=9 vs summary「消耗30法力，使未选定目标造成伤害+30%，永久」`
  * `弱化.cost=6 vs summary「消耗9法力，使未选定目标攻击力-3，永久」`
  * `兴奋.cost=6 vs summary「消耗15法力，使未选定目标每次出手后速度+1，持续3回合」`
  * `无力.cost=9 vs summary「消耗30法力，回始使未选定目标出手次数-3，永久」`
  * `急速.cost=15 vs summary「消耗60法力，使未选定目标每闪避两次速度+1，持续3回合」`
  * `加速.cost=15 vs summary「消耗60法力，使未选定目标获得的速度翻倍，持续3回合」`

## 10. 结论（可测量事实）

* 70 个道纹产出 2415 组无序对。
* 观测到分类 2（非平凡协同）**635** 对（可验证），密度 **26.3%**；另有 **99** 对因执行受限标 UNVERIFIED，未计入。
* 分类 1（加和型）**1015** 对；分类 0（独立型）**666** 对。
* 有 **1** 个道纹没有任何分类 2 伙伴。
* 最大交互枢纽 **洞察**，分类 2 伙伴 **64** 个。
* 分类 2 的图中：平均度 18.14，中位度 12，度>0 的节点 69/70。
* 顺序敏感对 90 对（A→B 与 B→A 结果不同的可验证 Cat2）。
* 分类 2 的层级拆分：事件级 **27** 对、发动期被改变 **134** 对、仅顺序不同 **5** 对、纯结算层数值耦合 **469** 对。

### 解释（假设，不是结论）

> 以下条目是**从数据出发的假设**，用于后续验证，不构成本次测量的事实。

* H1：「组合稀少」可能是**采样假象**——本次无差别穷举在 2415 对中观测到 734 对非平凡交互；人类/报告样本通常只看少数几对，若未覆盖高连接节点（如枢纽道纹的邻域）会低估总量。
* H2：交互高度**不均衡**——度中位数 12、最大度 64，说明协同更可能集中在少数「机制枢纽」上，而不是均匀铺开；可在这些枢纽上做定向设计或定向验证。
* H3：1 个道纹零 Cat2 伙伴，可能是**机制上确实孤立**（只读写自己的私有状态），也可能是**探针未覆盖其触发条件**（本分析的探针是有限集合，不是全部可达状态）。二者需分别用「单道纹可达状态」分析区分。
* H4：69 对分类 2 的双方分属互斥副本，单局无法同时持有——若把「可达性」也算进「有多少组合」，实际密度会低于本表；反之若允许跨副本（朋友/员工/怪物侧），本表就是可达的。

