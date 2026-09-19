# 第四宇宙：规则引擎稳定性与组合结算改造 —— 最终报告

日期：2026-09-20 ｜ 分支：`arena/01a0ba24-linji-disiyuzhou`
基线：`24ff69e`（上一个任务=性能优化的收尾提交） ｜ 本次范围：`1a9cc90 … b2bf700` + Phase 9（本报告与基准数据）

> **本报告的结论覆盖并取代此前两份报告的结论**：
> * `audit/effect_call_graph_2026-09-20.md`（Phase 2 现状调查）——其中「找不到统一结算入口、
>   74 处直接改状态、护栏只覆盖一个入口」的结论，是本报告第 1–2 节的**问题清单**；
>   本次已按第 3 节的结构落地，问题清单里属于「入口/护栏」的部分已消解，
>   属于「Entity 级 API 边界」的部分保留为第 7 节的剩余风险。
> * 上一轮性能验收报告的结论（初始化/预演/AI 决策数量级下降、行为中性）**继续有效**，
>   本报告第 5 节给出的是在它之上的增量：预演 +1%、AI 决策 +4%（详见第 5 节）。
>
> **规则修改：否**（第 6 节给出可复现证据）

---

## 0. 一句话结论

效果结算从「每个效果各写各的、只有伤害有深度计数」收成**一条路径**：
所有效果入口开同一个帧、所有状态汇点落在帧内、所有触发计入同一次结算，
深度/同类重复/预算三条保险丝专门拦「正常规则永远到不了」的形态，
trace 能在事后回答「为什么这个怪没死」。
**没有改任何游戏规则**，用「禁止重复」换稳定性的做法**一次都没有用**。

---

## 1. 修改了什么（按文件）

### 新增

| 文件 | 作用 |
| --- | --- |
| `engine/resolution.py`（+435） | **结算生命周期上下文** `ResolutionContext`：进出帧、深度/预算/同类重复三条保险丝、可选 trace（`history`/`note`/`describe_trace`/`explain`）、沙盒快照协议。刻意不含 GameEngine/AI/DB/配置。 |
| `engine/pollution_guard.py`（+251） | 预污染检测器（测试/debug 专用，默认关闭）：`snapshot_runtime_state()` / `assert_runtime_unchanged()`，把差异按具体路径报出来。 |
| `engine/sandbox.py`（+133） | 预演隔离清单（原 Phase 1 落地后逐步补全到 14 项运行态 + 上下文 + 事件池 + 行动历史），并记录「本来不存在」的惰性属性。 |
| `tests/preview_support.py` | 测试共用的「战斗内待出手」引擎构造（同种子对照的前提，唯一一份）。 |
| `tests/test_sandbox_pollution.py`（32 例） | 预演零污染契约 + 自动检测。 |
| `tests/test_resolution_context.py`（20 例） | 上下文契约：进出平衡、越界抛错、trace、阈值余量、快照恢复。 |
| `tests/test_effect_resolution_path.py`（13 例） | **入口唯一性契约**：五个入口必须开帧、汇点不得在帧外被调用。 |
| `tests/test_resolution_fuses.py`（13 例） | 保险丝契约：合法重复必须畅通、A→B→A/B→C→A 必须被点名、阈值实证。 |
| `tests/test_effect_composition.py`（22 例） | 组合压力：A+B…A+B+C+D、十种语义组合、预演+组合、不变量断言。 |
| `tests/test_resolution_fuzz.py`（17 例） | 随机压力：固定种子、可调规模（默认 2000 例）、单步看门狗、非法输入、异常后收尾。 |
| `sim/effect_chain_audit.py` | 静态调用图 + 汇点统计 + 运行期深度扫描（Phase 2 工具，可复现）。 |
| `sim/threshold_evidence.py` | 保险丝阈值的实测依据（跑真实对局统计深度/效果数峰值，余量不足则非零退出）。 |
| `audit/effect_call_graph_2026-09-20.md` | Phase 2 现状调查报告（问题清单）。 |

### 修改（行为中性）

| 文件 | 改了什么 |
| --- | --- |
| `engine/combat.py` | 创建 `self.resolution`；`MAX_EFFECT_CHAIN_DEPTH` 保留为同值别名；`_effect_chain_depth` 改为 `resolution.depth` 的兼容属性；`resolve_attack` 拆成「开帧的公开入口 + `_resolve_attack_impl`」。 |
| `engine/api.py` | 行动边界 `begin_action`/`end_action`（成功与异常都收尾）；`begin_action` 传入战场规模（预算随实体数放大）；回滚时**保留活对象上的弱引用**（见第 2 节缺陷 2）。 |
| `engine/combat_parts/daowen_effect.py` | `apply_daowen_effect` 拆成「开帧的公开入口 + `_apply_daowen_effect_impl`」。 |
| `engine/combat_parts/damage_death.py` | `_apply_hostile_damage`/`_check_hp_zero_death`/`_spawn_fenlie_clones` 改走统一帧；伤害帧记 `hp`/`shield` 变化备注。 |
| `engine/combat_parts/cost_payment.py` | `pay_numeric_cost` 拆成「开帧入口 + 实现体」，代价帧记数值变化。 |
| `engine/combat_parts/monster_phase.py` / `monster_life.py` | 怪物阶段 / 进化 拆成「开帧入口 + 实现体」。 |
| `engine/models.py` | `GameState.apply_heal` 拆成「开帧入口 + `_apply_heal_inner`」，回复帧记 `hp` 变化。 |
| `engine/events.py` | `resolve_option_effect` 拆成「开帧入口 + 实现体」（局外事件/法术选项走同一条路径）。 |
| `engine/mechanisms/triggers.py` | `TriggerBus.dispatch` 在**有订阅者时**开 `trigger` 帧并记命中的机制名（无订阅者仍零开销早退）。 |
| `engine/ai_preview.py` | 预演走沙盒隔离（Phase 1），预演不吃真实行动的深度/预算。 |
| `engine/combat_events.py` | 新增 `engine_for_state()`：按 state 找回所属战斗引擎（复用既有弱引用观察者表，不新增全局注册表）。 |

> 说明：上面所有「拆成入口+实现体」都是**只加一层开帧**，实现体逐字节未改；
> 对外名字、签名、调用方、执行顺序全部不变。

---

## 2. 解决了什么问题

### 2.1 Preview pollution（预演污染）

* **契约化**：`snapshot_runtime_state()` / `assert_runtime_unchanged()` 把「预演前后真实状态
  必须逐路径一致」变成可执行断言，覆盖 state、dice、战斗运行态、事件池、行动历史、
  上下文、计数器、按实体 id 存键的运行态。
* **修的漏点**：Phase 1 修掉事件池污染（预演 `resolve_event` 会标记真实事件）；
  Phase 8 由随机压力测试又实测出 5 个运行态漏点（`_hp_loss_ctx`、
  `_attack_after_window_target`、`_dodge_counts`、`_dodge_round`、`_branch_owner_token_value`），
  全部登记进隔离清单并由测试锁死。
* **判据**：预演 → 沙盒 → 执行 → 触发 → 嵌套效果 → 运行态写入 → 异常/早退，任一路径都不得
  在真实对象上留下痕迹；`tests/test_sandbox_pollution.py` 32 例 + 组合/随机测试里的
  现场断言共同覆盖。

### 2.2 Effect recursion / Trigger recursion（效果与触发的递归）

* **不再靠「拦重复」**：`if effect in visited: return` 这种写法**没有引入**。
  判定标准是「**是否嵌套**」，不是效果名字：
  * 合法的重复是**顺序**的——失去生命→再生→血债→失去生命 里，上一帧退出后才发生下一帧，
    同一条链上同类帧始终只有 1 个（`tests/test_resolution_fuses.py` 用 30 轮回环证明畅通）；
  * 循环触发会让同类帧在链上越堆越多。
* **三条保险丝**（阈值全部由实测确定，见 `sim/threshold_evidence.py`）：
  | 保险丝 | 阈值 | 实测峰值 | 余量 |
  | --- | --- | --- | --- |
  | 嵌套深度 | 64 | 5（trigger 帧） | 12.8× |
  | 同类结算在同一链上的重复 | 16 | ≤5 | ≥3× |
  | 单次行动效果数（随在场实体数放大） | 2000 + 40/实体 | 83（小战场）；105 只怪战场实测 >2000（合法） | 24× |
* **终止原因可追踪**：越界抛 `ResolutionDepthError`(RecursionError 子类) /
  `ResolutionCycleError` / `ResolutionBudgetError`，错误信息带层数、类别与链摘要，
  并写进 `trip_reason`；API 边界把异常转成 `success=False` + 可读 error，行动整体回滚，
  **引擎不会因为触发过一次就永久卡死**（有测试）。

### 2.3 Runtime corruption（运行态损坏）

* 触发分发（`TriggerBus.dispatch`）现在也计入结算：它是套娃唯一可能的入口，
  以前完全不受深度/预算约束。
* **实测修掉的缺陷**：事务回滚（深拷贝快照 + 原地恢复）会把实体上的 `_hp_engine_ref`
  弱引用覆盖成 `None`，于是**任何一次失败行动之后，该实体的降血兜底钩子永久失效**
  → 改为「原地恢复时跳过活对象上的弱引用」。
  （先试过「回滚后统一重绑」，被 `tests/test_spell_loop_mana_gain.py` 当场抓住：
  重绑会给本来没绑定的实体补绑定=改变行为，故改为只保护、不新增。）
* 顺手修掉污染检测器自身的一处不确定性：字典遍历按键排序，避免循环引用标记位置漂移造成假阳性。

### 2.4 Entity reference corruption（实体引用损坏）

* 组合测试的 `assert_battle_invariants()` 逐条检查：非法实体、负血/负护盾、活人 0 血、
  同一实体被登记两次、按实体 id 存键的运行态指向不存在的实体、悬垂引用、无名实体。
* 随机压力（2000 例默认档 / 10000 例高档）每步都跑这套断言，未再发现新的引用问题。
* 组合里发现并**记录（未改）**的一处边界见第 7 节。

### 2.5 效果数量增加而不新增特殊结算入口

* 道纹 / 攻击 / 怪物阶段 / 进化 / 事件选项 五个入口现在都只做一件事：开帧；
  实现体一律是原来的代码。
* 「杀伐/再生/血债/庇护/死亡/复活/分裂/进化/三相」全部落在同一套帧语义内
  （`tests/test_effect_resolution_path.py` 逐一断言，并断言汇点不得在帧外被调用）。

---

## 3. 当前 Effect 调用链（实际代码结构）

### 3.1 顶层

```text
GameEngine.execute_action(action, params)                 engine/api.py
   └─ _execute_action_core()                              engine/api.py:965
        ├─ 事务快照：deepcopy(state) / 战斗运行态 / dice / 事件池 / 上下文
        ├─ combat.resolution.begin_action(action, scale=在场实体数)   ← 本次新增
        ├─ _dispatch_action()  ── 63 个分支（api.py:1163）
        │     ├─ use_daowen   → combat.apply_daowen_effect()            [帧 effect]
        │     ├─ prepare_attack / resolve_attack → combat.resolve_attack()[帧 effect]
        │     ├─ round_start / round_end / battle_start …（管线动作）
        │     ├─ resolve_monster_phase → combat.resolve_monster_phase() [帧 effect]
        │     └─ 事件相关 → events.resolve_option_effect()              [帧 effect]
        ├─ 成功：_last_result；失败/异常：_restore_state_in_place() + 运行态回滚
        └─ combat.resolution.end_action()                              ← 本次新增
```

### 3.2 效果与汇点（帧内）

```text
帧内（resolution.enter/leave，统一写法 with resolution_frame(...)）

道纹 apply_daowen_effect ──┐
攻击 resolve_attack ───────┤
怪物阶段 resolve_monster_phase ─┤  → 这些入口内部调用下面的汇点
进化 execute_evolution ────┤
事件选项 resolve_option_effect ┘

汇点（同样在帧内，统一写法）：
  伤害   _apply_hostile_damage        (damage_death.py:347)  备注 hp / shield 变化
  回复   GameState.apply_heal         (models.py:1024)       备注 hp 变化
  代价   pay_numeric_cost             (cost_payment.py:284)  备注 hp/mana/energy 变化
  命零   _check_hp_zero_death         (damage_death.py:229)
  分裂   _spawn_fenlie_clones         (damage_death.py:512)
  触发   TriggerBus.dispatch          (mechanisms/triggers.py:119) 备注机制名

触发 → 新效果 → 汇点 → 触发 …… （同一棵帧树继续加深，受三条保险丝约束）
```

### 3.3 实测调用链示例（trace 原文）

「杀伐打了，怪为什么没死？」——护盾吸收，trace 直接给出答案：

```text
[1] effect 道纹 杀伐 贾凡 -> 石背熊
[2]   damage 杀伐 石背熊 1   shield 4→3
[3]     trigger DAMAGE_APPLIED
→ 石背熊 hp=3 shield=3 alive=True
```

「失去生命（血债）同时触发伤害与代价」：

```text
[1] effect 道纹 血债 贾凡 -> 石背熊
[2]   damage 血债 石背熊 1   hp 252→251
[3]     trigger DAMAGE_APPLIED
[4]   cost 贾凡 流血 1   流血 66→65
```

（复现：`resolution.set_tracing(True)` → 执行行动 → `resolution.describe_trace()` /
`resolution.explain("石背熊")`。）

---

## 4. 测试结果

### 4.1 全量 pytest

```text
8 failed, 1717 passed, 3 xfailed in 129.49s
```

* 失败的 8 条与本次改造**前逐条相同**，均为既有的 AI 模拟器/夹具类失败
  （`test_ai_basic_attack_candidate`、`test_ai_tactics`、`test_build_learner`、
  `test_unified_ai`、`test_win_only_ai` ×4），与效果结算无关；
* 本次改造前的基线同为 8 failed。

### 4.2 分文件（本次新增/扩展）

| 测试文件 | 例数 | 说明 |
| --- | --- | --- |
| `tests/test_action_preview_parity.py` | 11 | 预演→执行 == 直接执行（状态/RNG/运行态/历史/事件流） |
| `tests/test_sandbox_pollution.py` | 32 | 预演零污染契约 + 自动检测 |
| `tests/test_resolution_context.py` | 20 | 上下文契约 |
| `tests/test_effect_resolution_path.py` | 13 | 入口唯一性 / 汇点不得在帧外调用 |
| `tests/test_resolution_fuses.py` | 13 | 保险丝：合法重复畅通、循环被点名 |
| `tests/test_effect_composition.py` | 22 | 组合压力（A+B…A+B+C+D、十种语义组合、预演+组合） |
| `tests/test_resolution_fuzz.py` | 17 | 随机压力（默认 2000 例；`LJ_FUZZ_CASES=10000` 实测通过，8 分 34 秒） |
| `tests/test_effect_chain_audit.py` | 35 | 既有：`_effect_chain_depth` 兼容契约 |
| 合计 | **163** | 以上 8 个文件单独跑：163 passed |

### 4.3 行为中性证据

```text
$ .venv/bin/python sim/behavior_trace.py
TRACE_SHA256 6aa26df840d0e80a165539135dade0e27ae3400b02d2e3dd7940bd3b6291af28
```

与改造前**逐字节相同**（含 3 组固定对局的逐例 hash）。

---

## 5. 性能（改造前后）

机器空载、固定种子、同一脚本 `sim/perf_bench.py`；pre = `24ff69e` 工作树，
post = 本次 HEAD。三轮交替测量取中位数（抵消漂移）：

| 指标 | pre | post | 变化 |
| --- | --- | --- | --- |
| `GameEngine` 初始化 (ms) | 2.148 / 2.134 / 2.162 | 2.165 / 2.203 / 2.176 | **+1.5%** |
| ActionPreview (ms) | 0.3777 / 0.3710 / 0.3753 | 0.3792 / 0.3762 / 0.3783 | **+1.1%** |
| TacticalAI 决策 (ms) | 13.488 / 13.642 / 13.685 | 14.106 / 14.165 / 14.345 | **+4.4%** |
| deepcopy/次（预演） | 2.0 | 2.0 | **不变** |
| deepcopy/次（AI 决策） | 302.3 | 301.3 | **不变** |

长局（先堆 3000 条战斗事件再预演/决策）：

| 指标 | pre | post | 变化 |
| --- | --- | --- | --- |
| ActionPreview（长局，ms） | 1.034 | 1.005 | **−2.8%** |
| AI 决策（长局，ms） | 85.188 | 82.395 | **−3.3%** |

**调试/测试专用工具的成本（正式运行默认关闭，不计入上表）**：

| 工具 | 成本 |
| --- | --- |
| trace 关闭（默认） | 每次结算 5 个整数自增/自减、无对象分配 |
| trace 打开 | 预演 +12.7%；`history` 环形上限 500 帧，不无限驻留 |
| 污染快照 `snapshot_runtime_state` | 单次 1.77 ms（只在测试/debug 调用） |

AI 决策 +4.4% 的来源已定位并量化：`resolution` 记账约 2.6%（enter/leave 51 次/决策、
`begin_action`、沙盒快照/恢复），其余为隔离清单多还原 5 项运行态。
上一轮优化成果（初始化 56→2.2 ms、预演 34.8→0.38 ms、AI 774→13.5 ms）**未被破坏**。

---

## 6. 规则修改：否

**没有修改任何游戏规则。** 具体没动：道纹含义、法术含义、杀伐/再生/庇护计算、血债、
残韵、伤害/治疗数值、法力消耗、怪物机制、事件机制、AI 评分、胜负条件。

可复现证据：

1. `sim/behavior_trace.py` 指纹与改造前逐字节相同（3 组固定对局）；
2. 全量 pytest 的失败集合与改造前逐条相同（8 条既有 AI 夹具失败），
   含规则语义测试（`test_spell_loop_mana_gain.py` 的法力净零闭环、
   `test_dragon_heart.py`、血契/血誓戒、命零管线等）全部通过；
3. 所有入口都只是「开帧 + 原实现体」，实现体逐字节未改；
4. 保险丝阈值余量 ≥3×（多数 ≥12×）实测峰值，且 Phase 8 的极大规模随机测试
   （1 万例、105 只怪的战场）未触发过任何一次「正常规则本不该触发」的截断。

---

## 7. 剩余风险（明确列出未解决项）

1. **私有助手可造出非法状态（已记录，未改）**：`CombatEngine._delay_monster_reentry()`
   若被喂一具尸体，会造出 `is_alive=True 且 current_hp=0` 的实体。公开的【封印】路径
   （`daowen_effect.py:822`）有 `is_alive` + 在场校验，规则路径达不到该状态；
   但如果将来有人直接调这个私有助手，就会踩到。建议后续在助手内部加同款校验
   （本次不动，因为它属于「改代码」而不是「修规则」，且当前无调用方踩到）。
2. **失败行动会换掉 `engine.dice` 对象**：回滚是「深拷贝快照 + 原地恢复」，
   `self.dice = dice_before` 会换身份（内容一致）。引擎内部一致，但**外部**若长期持有
   `engine.dice` 引用会拿到旧对象。既有行为，未改（改动会触及回滚语义）。
3. **回滚会重排 state 内部的对象别名关系**：内容一字未改，但「谁与谁共享同一个对象」
   会变（污染检测器的 `<cycle:...>` 标记位置因此漂移）。已在 fuzz 测试里按内容比对并注明；
   外部若依赖实体对象身份跨失败行动保持不变，需要重新取引用。
4. **Entity 级汇点不在帧内**：`Entity.take_damage` / `add_status` / `add_mutation`
   挂在实体上，实体没有引擎引用，因此不计入结算帧。引擎侧所有调用点都在帧内
   （有测试断言）；但工具/sim 若直接调这些实体方法，绕过的是记账而不是规则。
   我们**刻意**没有为此引入全局注册表（那才是「为抽象而抽象」）。
5. **相位机制路径未单独开帧**：`TriggerBus.dispatch`（事件机制）已开帧；
   相位路径（`_dispatch_phase` → `CombatHookManager`）不单独成帧，只作为所在汇点的一部分
   记账。若将来相位机制也要按「谁触发了什么」逐条追踪，需要单独设计（目前没有这个需求）。
6. **10 万档 fuzz 未跑**：`LJ_FUZZ_CASES=100000` 约 85 分钟，留给发版前一次性压测；
   默认 2000 例，1 万例已实测通过。
7. **预算的「每实体 40」是启发式**：105 只怪的战场实测合法结算 >2000，故预算随实体数放大。
   若将来出现极端合法的「超多实体 + 超长回合」，仍可能撞到预算并被整体回滚
   （后果是行动失败 + 可读报错，不是状态损坏）。撞到时用 `sim/threshold_evidence.py` 重新论证阈值。
8. **污染检测器成本随对象图增长**：长局上一次快照约 1.77 ms（引擎越大越慢），
   因此它只在测试/debug 用；不要把它接进正式循环。

---

## 8. 复现命令

```bash
# 全量测试（约 2 分 15 秒）
.venv/bin/python -m pytest tests -q

# 本次改造相关测试（163 例，约 12 秒）
.venv/bin/python -m pytest tests/test_action_preview_parity.py tests/test_sandbox_pollution.py \
    tests/test_resolution_context.py tests/test_effect_resolution_path.py \
    tests/test_resolution_fuses.py tests/test_effect_composition.py \
    tests/test_resolution_fuzz.py tests/test_effect_chain_audit.py -q

# 行为中性（必须与 6aa26df8… 相同）
.venv/bin/python sim/behavior_trace.py

# 保险丝阈值的实测依据（余量 <10× 会以非零码退出）
.venv/bin/python sim/threshold_evidence.py 12

# 更大规模的随机压力
LJ_FUZZ_CASES=10000 pytest tests/test_resolution_fuzz.py -q      # 8 分 34 秒，已通过

# 性能（空载机器上跑；--events 用于长局）
.venv/bin/python sim/perf_bench.py --count-deepcopy
.venv/bin/python sim/perf_bench.py --rounds-init 5 --rounds-preview 40 --rounds-ai 8 --events 3000

# 现状调查工具（调用图 / 汇点 / 深度扫描）
.venv/bin/python sim/effect_chain_audit.py sinks
.venv/bin/python sim/effect_chain_audit.py back _apply_hostile_damage
.venv/bin/python sim/effect_chain_audit.py fanout _dispatch_action
.venv/bin/python sim/effect_chain_audit.py depth 27
```

---

## 9. 最终验收对照

| 验收标准 | 结论 |
| --- | --- |
| 复杂规则 ↑ 而代码复杂度不等比 ↑ | 满足：8 个相位新增的是**一条记账路径**，不是每个效果一个结算入口；所有入口只是「开帧 + 原实现体」。 |
| 组合数量 ↑ 而稳定性不降 | 满足：组合矩阵 + 十种语义组合 + 2000/10000 例随机组合，不变量断言全绿。 |
| 效果数量 ↑ 而不新增特殊结算入口 | 满足：新增效果只需走既有五个入口之一 + 既有汇点，`tests/test_effect_resolution_path.py` 会拦住任何绕过。 |
| Preview 复杂度 ↑ 而真实状态不受影响 | 满足：预演零污染契约（32 例）+ 组合/随机测试现场断言，且 Phase 8 又实测揪出并修掉 5 个运行态漏点。 |
| 规则可以无限组合，但底层结算逻辑只有一套 | 满足（在引擎侧口径内）：全部效果结算共用 `ResolutionContext` 一棵帧树 + 三条保险丝 + 一套 trace。 |
