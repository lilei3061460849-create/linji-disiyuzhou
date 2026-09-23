# 效果链现状调查（Phase 2 · 2026-09-20）

> **本文件只做调查，不改代码。** 目的是在引入任何「统一结算」之前，先拿到
> 第四宇宙引擎里 Action / Effect / Trigger / Damage / Heal / Death / Revive /
> Evolution / Split / DaoWen / Resonance 之间**真实**的调用关系。
>
> 复现命令（全部在仓库根目录，`.venv/bin/python`）：
>
> ```bash
> python sim/effect_chain_audit.py sinks       # 汇点清单 + 谁调用它
> python sim/effect_chain_audit.py back _apply_hostile_damage
> python sim/effect_chain_audit.py fanout      # 出度最高（最像巨石）的函数
> python sim/effect_chain_audit.py depth       # 27 局实测嵌套深度（约 3 分钟）
> ```
>
> 本文的每个数字都是上面脚本跑出来的，不是估的。

---

## 一、真实调用链（自上而下）

```
玩家/AI 提交 action
  │
  ├─ GameEngine.execute_action(action, params)            engine/api.py
  │    · 门禁/合法校验（phase、pending、token）
  │    · transaction=True 时：deepcopy state + 快照 combat runtime + dice + 中断
  │    └─ GameEngine._execute_action_core(action, params)   ← 唯一执行实现
  │         └─ GameEngine._dispatch_action(action, params)  ← 63 分支的 action 路由表
  │              └─ self._action_*（api.py 内 60+ 个处理器）
  │
  ├─ 战斗类 action 进入 CombatEngine（self.combat，MRO 由 engine/combat.py 门面
  │   + engine/combat_parts/* 六个 Mixin 组成）：
  │     use_daowen   → combat.apply_daowen_effect(...)      daowen_effect.py
  │     resolve_attack / attack → combat.resolve_attack()  combat.py（核心结算）
  │     round_start / round_end → combat.round_start/round_end()
  │     prepare/resolve_monster_phase → monster_phase.py
  │
  ├─ 效果真正落地（**汇点 / sink**，见第二节）
  │
  ├─ 汇点内部产生「副作用」与「二次触发」：
  │     · CombatHookManager（combat_hooks.py）——遗物/法器/血脉的相位钩子
  │     · TriggerBus.dispatch（mechanisms/triggers.py）——事件型机制订阅
  │     · GameState.emit_combat_event（models.py）——事件流登记 + 观察者分发
  │
  └─ 二次触发回到汇点 ⇒ 形成嵌套（实测最深 5 层，见第四节）
```

**两条并行的分发机制**（这是当前结构里最容易看错的一点）：

| 机制 | 位置 | 触发词汇 | 订阅者 |
| --- | --- | --- | --- |
| `CombatHookManager` | `engine/combat_hooks.py` | 相位（乘区/加减区/重定向/伤害前/伤害后/闪避/回始） | 遗物、法器、龙族血脉等硬编码 Hook |
| `TriggerBus` | `engine/mechanisms/triggers.py` | 事件（`CombatEventType`）+ 相位（`Phase.*`，经 `MechanismHookAdapter` 挂到 Hook 路径上） | 已迁移的 13 个声明式 Mechanism |

两者**不重复触发**：已迁移机制经 `MechanismHookAdapter` 挂在 Hook 列表原位置，
priority 不变（顺序即规则，见 `combat_hooks.py` 的注释）。

---

## 二、状态变更汇点（实测调用者数量）

| 汇点 | 位置 | 调用者数 | 说明 |
| --- | --- | --- | --- |
| `add_status` | models.py（Entity） | **26** | 状态授予的唯一实体级入口；调用方极分散 |
| `_apply_hostile_damage` | damage_death.py | **17** | 敌对伤害统一入口（含 Hook 全生命周期、重定向、反噬） |
| `pay_numeric_cost` | cost_payment.py | **15** | 代价统一入口（法力/血限/速度/碎片/龙性…） |
| `apply_heal` | models.py（GameState） | **13** | 回复统一入口（含龙血瓶溢出转化） |
| `_check_hp_zero_death` | damage_death.py | **12** | 命零判定 + 死亡通知 |
| `gain_shield` | models.py（Entity） | 11 | 格挡统一入口 |
| `add_mutation` | models.py（Entity） | 8 | 异变层数 |
| `_on_entity_death` | damage_death.py | 8 | 死亡后续（战报/掉落/机制分发） |
| `_record_hp_loss_event` | damage_death.py | 5 | 失血事件（供「失去生命后」类机制） |
| `take_damage` | models.py（Entity） | 4 | **原始扣血**，绕过伤害 Hook（代价/流血专用） |
| `_spawn_fenlie_clones` | damage_death.py | 1 | 【分裂】创生 |
| `_raw_hp_loss` | damage_death.py | 3 | 直接失血（回始/凡庸等） |
| `_apply_blood_limit_change` | damage_death.py | 3 | 血限变更（一等事件：伤害→血限→依赖血限的效果） |
| `emit_combat_event` | models.py（GameState） | **2** | 事件流**唯一**写入点（`combat._emit` 与 `apply_heal`） |
| `TriggerBus.dispatch` | mechanisms/triggers.py | 2 | 事件型机制分发（无订阅者时零开销返回） |

**结论：统一入口已经存在，而且覆盖了四个核心动词**（伤害 / 回复 / 代价 / 命零）。
问题不在「没有入口」，而在「入口不是强制的」（下一节）。

---

## 三、绕过统一入口的直接写入（改造清单的原始数据）

引擎内直接改 `current_hp / current_mana / shield / current_speed` 的语句共 **74 处**。
按性质分类：

| 位置 | 处数 | 性质 | 评价 |
| --- | --- | --- | --- |
| `models.py` | 11 | 实体级原语（`take_damage` / `apply_heal` / `gain_shield` / `add_status` 的实现体） | ✅ 这就是汇点本身 |
| `combat_parts/*` | 13 | 汇点实现体内部（damage_death / cost_payment / daowen_effect / monster_life） | ✅ 汇点内部 |
| `combat.py` | 14 | 回合结算 / 钳制 / 变形 / 凡庸本身 | ⚠️ 部分应走汇点（`current_hp = 0` 有 3 处） |
| `api.py` | **19** | 事件选项、遗物、法器、员工、黑卡等处理器**直接加血/加盾/改法力** | ❌ 绕过汇点（如 `player.current_hp *= 2`） |
| `events.py` | **7** | 事件正文数值直接落到实体 | ❌ 绕过汇点 |
| `combat_hooks.py` | **5** | Hook 内部直接扣血/加盾/加法力 | ❌ 绕过汇点（`attacker.current_hp = max(0, …)`） |
| `monsters.py` | 2 | 怪物模板初始化 | ✅ 构造期赋值 |
| `handlers/duel.py` | 1 | 决斗收尾 `current_hp = 0` | ⚠️ |
| `mechanisms/verbs.py` | 2 | `mana` 动词实现体（**2026-08-19 已成为统一入口**） | ✅ |

> **判断**：不算「到处都是野路子」，但也不是「只有一套」。真正需要收拢的是
> `api.py`(19) + `events.py`(7) + `combat_hooks.py`(5) + `combat.py`(3) ≈ **34 处**，
> 其余都在汇点自身或构造期。

---

## 四、递归与嵌套的实测事实

`sim/effect_chain_audit.py depth`（3 区域 × 3 构筑 × 3 种子 = 27 局）：

| 指标 | 实测 |
| --- | --- |
| 最大调用嵌套深度 | **5**（`TriggerBus.dispatch`） |
| `_on_entity_death` 最深 | 4 |
| `apply_heal` / `add_mutation` / `_apply_blood_limit_change` / `_check_hp_zero_death` | 3 |
| `_apply_hostile_damage` / `pay_numeric_cost` / `add_status` | 2 |
| `_effect_chain_depth` 峰值 | **0**（本轮 27 局中从未发生「伤害内再伤害」） |
| 保险丝阈值 `MAX_EFFECT_CHAIN_DEPTH` | **64** |

调用次数（同期）：`dispatch` 120,615 次、`_apply_hostile_damage` 109,492 次、
`apply_daowen_effect` 63,483 次、`pay_numeric_cost` 32,530 次。

**现状**：

1. `_effect_chain_depth` 目前**只保护一个汇点**（`_apply_hostile_damage`），
   唯一作用是「伤害重定向（嫁祸/背负）成环」时抛 `RecursionError`。
2. 其余汇点靠**语义性再入保护**收敛，而不是靠深度计数：
   - `_resolving_life_lost_reactions > 0`：正在结算反应法术 → 不再连锁；
   - `_hp_loss_recording > 0`：失血由既有入口记账 → 抑制兜底钩子；
   - 每回合每道纹一次（`_monster_daowen_round_used`）、每场一次（`_monster_evolved`）、
     唯一（`spent_unique`）、冷却（`cooldown_remaining`）。
3. 因此**没有**「广度型」失控的保护：如果某个 future 机制在同一次结算里
   反复产生新效果（深度不涨、数量暴涨），当前没有任何闸门会拦。

---

## 五、是否适合引入统一的 EffectResolutionContext / 统一结算

**结论：适合，但只能「渐进收拢」，不能推倒重来。** 依据：

### 支持改造的事实

1. **汇点已经存在且集中**（第二节）：伤害/回复/代价/命零四个核心动词各有一个
   唯一实现，`mechanisms/verbs.py` 已把它们注册成 12 个动词（`apply_verb`）。
   这意味着「统一入口」不是从零造，而是**把已有的入口变成强制入口**。
2. **实测嵌套很浅（5 层）**：引入一层结算上下文（push/pop）对现有调用栈的影响很小，
   风险可控。
3. **事件流已是不可变事实源**（`combat_events.py` 契约 + `sandbox.py` 依赖它），
   天然适合做「因果链」的载体：`EffectContext.parent_event_id` 机制已经在用。
4. **`EffectContext` 已存在**（`engine/effect_context.py`），并且已经贯穿
   伤害/回复/血限/代价的 ctx 参数——**不需要新造上下文类型，只需要让它与
   「一次结算的生命周期」对齐**。

### 阻碍改造的事实（必须如实报告）

1. **34 处绕过**（第三节）：这些点分布在事件系统、遗物系统、Hook 系统里。
   把它们全部改成走汇点，是**行为敏感**的改动（汇点会引入 Hook/机制分发，
   顺序即规则）——不能一次性替换，必须逐点迁移 + 逐点 parity 验证。
2. **`apply_daowen_effect` 是 59 次出度的巨石**：道纹效果本体尚未拆成
   「效果清单 → 逐个结算」，而是「一个大 if/elif 链里直接改状态」。
   把道纹改成声明式效果清单 = 触及规则表达方式，**超出本次任务范围**（且必然改行为）。
3. **顺序即规则**：Hook priority、机制 priority、道纹内部的结算顺序都被文档冻结。
   任何"重排"都是规则修改，禁止。

### 建议方案（分阶段，不改规则）

| 阶段 | 做什么 | 不做什么 |
| --- | --- | --- |
| Phase 3 | 引入**结算上下文栈**（`ResolutionContext`）：只记录 `depth / root_action / chain[] / budget`，由汇点在进出时 push/pop。任何规则逻辑都不读它做判断——只用于保险丝与 trace。 | 不把 GameEngine/AI/DB 塞进去；不做万能 Context |
| Phase 4 | **增量收拢**：把第三节里标 ❌ 的 34 处按「风险 × 收益」排序，分批改为调用汇点。每批一个 commit + parity 测试。第一批建议是 `combat.py` 的 3 处 `current_hp = 0`（离汇点最近、语义最清楚）。 | 不碰 `apply_daowen_effect` 的内部结构；不重排任何 priority |
| Phase 5 | 把深度保险丝从「单汇点」推广到「所有汇点」，并加**预算保险丝**（单次 resolution 内的效果总次数上限）。 | 不做「visited 集合去重」——会破坏合法重复（见第六节） |
| Phase 6 | 轻量 Resolution Trace（环形缓冲，默认关闭）。 | 不做日志系统当规则系统 |

---

## 六、关于「合法重复结算」与「无限循环」的判定方式（**先报告，不自行设计规则**）

任务书第九节点名的场景：

```
失去生命 → 再生 → 血债 → 再次失去生命
```

这是**合法**的重复；不能写 `if effect in visited: return`。

**调查结论**：本引擎里，合法链与死循环的**可判定差别不在「效果身份」，
而在「是否消耗资源/状态」**：

| 维度 | 合法重复链 | 失控循环 |
| --- | --- | --- |
| 终止机制 | 由**状态**收敛：法力耗尽、代价付不出、每回合每道纹一次、每场一次、唯一、冷却、状态层数归零 | 无状态变化，或状态单调增长且不再被读取 |
| 实测证据 | 27 局中 `_effect_chain_depth` 峰值 0、`dispatch` 12 万次全部收敛 | 现有代码里未观察到；`_resolving_life_lost_reactions` 等保护是**预防性**的 |

因此当前系统适合的判定方式（**建议，未实施**）：

1. **深度保险丝**（已有先例，扩到所有汇点）：实测峰值 5，阈值设为 64 有 12 倍余量；
   触发即抛错，因为合法玩法**不可能**达到该深度。
2. **预算保险丝**（新增候选）：一次顶层 action 内的效果总次数上限（例如
   `_apply_hostile_damage` 单次 resolution 内不超过 N 次）。同样按实测峰值定，
   触发即抛错。这能拦住「深度不涨、数量爆炸」的失控。
3. **不做**：不按 effect 身份去重、不按名字去重、不按目标去重。
   这些都会直接破坏合法的重复结算（如 再生→血债→再生）。
4. 若将来仍发现合法玩法能触发的失控形态，**停下来报告**，由规则侧决定语义
   （例如「同一次结算内同一道纹最多触发 K 次」如果成为规则，那是规则变更，
   不是引擎加固）。

---

## 七、给下一阶段的风险提示

1. `add_status` 有 26 个调用者，是「状态授予顺序」的最大不确定源；
   如果要收拢，必须**逐点**核对顺序（顺序即规则）。
2. `TriggerBus.dispatch` 被调用 12 万次/27 局，是热路径。任何加在里面的
   记账/trace 都必须可关闭且零分配（否则性能回退）。
3. `models.py::take_damage`（原始扣血）有 4 个调用者，**故意**绕过伤害 Hook
   （代价/流血不受格挡与加害影响）。收拢时必须保留这一语义，不能"顺手统一"。
4. `lose_hp` 是 0 调用的死方法（`models.py`），清理它属于可选卫生工作。
5. 事件系统 `resolve_option_effect`（`events.py`，50 次出度）里的 7 处直接写入，
   是「事件正文 → 数值」的隐式通道，收拢收益高但需要逐条读正文。
