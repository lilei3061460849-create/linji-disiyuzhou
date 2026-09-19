# 第四宇宙：规则引擎稳定性与组合结算改造 —— 最终报告

日期：2026-09-20 ｜ 分支：`arena/01a0ba24-linji-disiyuzhou`
基线：`24ff69e`（上一个任务=性能优化的收尾提交） ｜ 本次范围：`1a9cc90 … f85fa11` 共 9 个相位提交，
外加本报告所在提交（扫参门禁 `--cases`、三树复测数据、第 10 节逐条对照）

> **本报告的结论覆盖并取代此前两份报告的结论**：
> * `报告.md`（上一轮：引擎性能优化 + `combat.py` 拆分，2026-09-19）——**第 10 节逐条对照**：
>   它的性能倍数、行为中性口径、测试/护栏数字、范围边界（§四）、下一步建议 5 条与遗留项，
>   逐条给出「仍成立 / 已被更新 / 已被覆盖」以及本次的复测证据；
>   其中「把三个种子扩成扫参」那条建议已在本次落地（第 4.4 节），
>   「行为中性」也从 3 例验收升级成 60 例发版门禁（`SWEEP_SHA256`）。
> * `audit/effect_call_graph_2026-09-20.md`（Phase 2 现状调查）——其中「找不到统一结算入口、
>   74 处直接改状态、护栏只覆盖一个入口」的结论，是本报告第 1–2 节的**问题清单**；
>   本次已按第 3 节的结构落地，问题清单里属于「入口/护栏」的部分已消解，
>   属于「Entity 级 API 边界」的部分保留为第 7 节的剩余风险。
>
> **规则修改：否**（第 6 节给出可复现证据；第 10 节给出与上一轮报告的逐条对照）

---

## 0. 一句话结论

效果结算从「每个效果各写各的、只有伤害有深度计数」收成**一条路径**：
所有效果入口开同一个帧、所有状态汇点落在帧内、所有触发计入同一次结算，
深度/同类重复/预算三条保险丝专门拦「正常规则永远到不了」的形态，
trace 能在事后回答「为什么这个怪没死」。
**没有改任何游戏规则**，用「禁止重复」换稳定性的做法**一次都没有用**。

上一轮报告（性能优化 + `combat.py` 拆分）的结论在**第 10 节**逐条复核：
性能倍数在原口径下复现、行为指纹不变、迁移护栏与失败名单不变、遗留项与 5 条建议的现状逐条更新。

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
| `tests/test_behavior_trace_sweep.py`（5 例） | 扫参用例表契约（种子恰为 1..N、三组构筑/地区轮换、前缀稳定）：改动用例表即失败，避免已发布的 `SWEEP_SHA256` 被静默换掉。 |
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
| `sim/behavior_trace.py` | 新增 `--cases N` 扫参门禁（默认关闭）：内置 3 组构筑/地区轮流用、种子 1..N，打印 `SWEEP_SHA256`；3 例验收指纹与输出格式不变。 |

> 说明：上面所有「拆成入口+实现体」都是**只加一层开帧**，实现体逐字节未改；
> 对外名字、签名、调用方、执行顺序全部不变。
> 9 个相位提交合计 27 个文件、**+4329 / −149**（不含本报告所在提交；含新增测试与调查工具）。

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
8 failed, 1722 passed, 3 xfailed in 139.22s
```

* 失败的 8 条与本次改造**前逐条同名**，均为既有的 AI 模拟器/夹具类失败
  （`test_ai_basic_attack_candidate`、`test_ai_tactics`、`test_build_learner`、
  `test_unified_ai`、`test_win_only_ai` ×4），与效果结算无关；
* 本次改造前的基线（`24ff69e`，即上一轮性能报告收尾时的 1602 passed）同为这 8 条失败，
  逐条名单见第 10.3 节；本次 1602 → **1722 passed**（+120）来自 9 个相位的新增/扩展测试。

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
| `tests/test_behavior_trace_sweep.py` | 5 | 扫参用例表契约（种子 1..N、三例轮换、前缀稳定；不跑对局，0.2 秒） |
| 合计 | **168** | 以上 9 个文件单独跑：168 passed in 12.59s |

### 4.3 行为中性证据

```text
$ .venv/bin/python sim/behavior_trace.py
TRACE_SHA256 6aa26df840d0e80a165539135dade0e27ae3400b02d2e3dd7940bd3b6291af28
```

与改造前**逐字节相同**（含 3 组固定对局的逐例 hash）。今天复测仍然如此，且
`--force-preview-transaction`（把预演强制回旧口径）给出**同一个指纹**：

```text
$ .venv/bin/python sim/behavior_trace.py --force-preview-transaction
TRACE_SHA256 6aa26df840d0e80a165539135dade0e27ae3400b02d2e3dd7940bd3b6291af28
```

### 4.4 扫参行为门禁（上一轮报告「下一步建议 2」的落地）

3 例验收指纹覆盖面太窄（单元测试看不到「AI 决策链在实盘上的漂移」），
本次给 `sim/behavior_trace.py` 加了默认关闭的扫参模式：内置 3 组「起手道纹/学习表/地区」
轮流用、种子取 1..N，另打一个指纹，与 3 例验收指纹**分开打印**：

```text
$ .venv/bin/python sim/behavior_trace.py --cases 60        # 约 8.5 分钟
SWEEP_SHA256 85d2830dd00d533e0d5add048b9354a283848ba5aa9fb643c0b2370152ca8aec
SWEEP_CASES 60 cleared=23 won=0 invalid=7
```

* 同一命令在 `24ff69e`（上一轮收尾提交）工作树里跑出**同一个指纹**：工作树
  `/tmp/linji-pre`，脚本用本次 HEAD 的 `sim/behavior_trace.py` 原样拷入（脚本自带 root 定位，
  基线树源码一行未动）。也就是说：上一轮那 5 个性能提交 + 本次 9 个相位的改造，
  在 60 例实盘轨迹上都没有可观测行为差异；
* 与上一轮报告的口径关系：那条「3 例 → 50~200 例发版门禁」的建议**已落地**，
  且默认仍是 3 例（CI 与日常验收不受影响）；
* 用例表（种子恰为 1..N、三组构筑/地区轮换、前缀稳定）由 `tests/test_behavior_trace_sweep.py`
  锁死：改动用例表会让该测试立刻失败，避免已发布的 `SWEEP_SHA256` 被静默换掉。

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

### 5.1 三树复测：逐条复核上一轮报告的性能数字

上一轮报告的性能数字是「`adfd9d8`（优化前）→ 它自己的 HEAD」两棵树、2000 事件长局；
本节把口径统一成**一棵脚本（`sim/perf_bench.py`，自 `fe738b8` 起未改动）、一套参数、同机交替三轮**，
并把三棵树放在一起：`adfd9d8`（优化前基线）→ `24ff69e`（上一轮收尾）→ `f85fa11`（本次 HEAD）。
原始数据：`audit/bench_resolution_2026-09-20.json` 的 `three_way_pref_recheck`。

小局（`--rounds-init 15 --rounds-preview 400 --rounds-ai 50 --count-deepcopy`）：

| 项 | `adfd9d8` | `24ff69e` | `f85fa11`（HEAD） | 基线→HEAD 倍数 |
| --- | --- | --- | --- | --- |
| `GameEngine` 初始化 (ms) | 73.15 | 2.179 | 2.316 | **×31.6** |
| ActionPreview (ms) | 1.3136 | 0.3904 | 0.4036 | **×3.26** |
| TacticalAI 决策 (ms) | 32.66 | 14.47 | 14.63 | **×2.23** |
| deepcopy / 预演 | 6.0 | 2.0 | 2.0 | **×3** |
| deepcopy / 决策 | 1232.3 | 302.3 | 301.3 | **×4.1** |

长局（`--events 3000 --rounds-init 5 --rounds-preview 40 --rounds-ai 8`）：

| 项 | `adfd9d8` | `24ff69e` | `f85fa11`（HEAD） | 基线→HEAD 倍数 |
| --- | --- | --- | --- | --- |
| `GameEngine` 初始化 (ms) | 68.21 | 2.210 | 2.347 | **×29.1** |
| ActionPreview (ms) | 58.06 | 1.0296 | 1.0591 | **×54.8** |
| TacticalAI 决策 (ms) | 1358.36 | 89.33 | 94.19 | **×14.4** |

读法（四条）：

1. **上一轮报告的倍数复现**：init ×31.6（旧报告 ×27）、小局预演 ×3.26（×3.2）、
   小局 AI ×2.23（×2.3）、长局预演 ×54.8（×52）、长局 AI ×14.4（×14）；
   deepcopy 计数更是逐位相同（预演 6.0→2.0，决策 1232.3→302.3/301.3）。
   ⇒ 上一轮报告的性能结论**成立**，且没有被本次改造破坏。
2. **绝对值不要跨会话引用**：旧报告的 53.75 / 2.00 / 34.83 / 774.71 ms 与今天的
   73.15 / 2.18 / 58.06 / 1358.36 ms 是不同会话、不同机器状态的数（今天这台慢约 1.2–1.8×），
   长局事件数也由 2000 变成 3000。**引用倍数，不引用绝对毫秒**。
3. **本次改造的成本**：`24ff69e → HEAD` 在三树复测里是 初始化 +6.3%、预演 +3.4%、决策 +1.1%
   （小局）与 初始化 +6.2%、预演 +2.9%、决策 +5.4%（长局）；第 5 节那组三轮交替测量给出的是
   +1.5% / +1.1% / +4.4% 与 −2.8% / −3.3%。两组数据都在 ±5% 以内、符号甚至不稳定
   ⇒ 结论只能下到「**没有数量级变化、没有两位数百分比的退化**」，不能精确到某一个百分点。
4. 上一轮那次优化本身的收益（初始化 ×30、长局预演 ×55、长局 AI ×14）量级完好，
   本次新增的记账 / 保险丝 / 沙盒快照**没有把成本压回玩家可感知的量级**。

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
   （1 万例、105 只怪的战场）未触发过任何一次「正常规则本不该触发」的截断；
5. 60 例扫参门禁（`--cases 60`）在 `24ff69e`（上一轮收尾）与本次 HEAD 上给出
   同一个 `SWEEP_SHA256`（第 4.4 节）——比 3 例验收覆盖面大一个量级。

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
# 全量测试（约 2 分 19 秒）：8 failed / 1722 passed / 3 xfailed
.venv/bin/python -m pytest tests -q

# 本次改造相关测试（168 例，约 12.6 秒）
.venv/bin/python -m pytest tests/test_action_preview_parity.py tests/test_sandbox_pollution.py \
    tests/test_resolution_context.py tests/test_effect_resolution_path.py \
    tests/test_resolution_fuses.py tests/test_effect_composition.py \
    tests/test_resolution_fuzz.py tests/test_effect_chain_audit.py \
    tests/test_behavior_trace_sweep.py -q

# 行为中性（必须与 6aa26df8… 相同）
.venv/bin/python sim/behavior_trace.py
.venv/bin/python sim/behavior_trace.py --force-preview-transaction   # 应给出同一个指纹

# 扫参行为门禁（约 8.5 分钟；与上一版对比 SWEEP_SHA256，见第 4.4 节）
.venv/bin/python sim/behavior_trace.py --cases 60

# 保险丝阈值的实测依据（余量 <10× 会以非零码退出）
.venv/bin/python sim/threshold_evidence.py 12

# 更大规模的随机压力
LJ_FUZZ_CASES=10000 pytest tests/test_resolution_fuzz.py -q      # 8 分 34 秒，已通过

# 性能（空载机器上跑；--events 用于长局）
.venv/bin/python sim/perf_bench.py --count-deepcopy
.venv/bin/python sim/perf_bench.py --rounds-init 5 --rounds-preview 40 --rounds-ai 8 --events 3000

# 三树复测（复核上一轮报告的性能倍数，见第 5.1 节；机器空载）
git worktree add --detach /tmp/linji-base adfd9d8
cp sim/perf_bench.py /tmp/linji-base/sim/perf_bench.py     # 基线树没有这个脚本，原样拷入
for tree in /tmp/linji-base /tmp/linji-pre .; do (cd $tree && \
    /home/user/linji-disiyuzhou/.venv/bin/python sim/perf_bench.py \
    --rounds-init 15 --rounds-preview 400 --rounds-ai 50 --count-deepcopy \
    --json /tmp/bench_$(basename $tree)-small.json); done
# 长局：同上，参数换成 --events 3000 --rounds-init 5 --rounds-preview 40 --rounds-ai 8

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

---

## 10. 逐条覆盖上一轮报告（`报告.md`）的结论

上一轮报告 `报告.md`（引擎性能优化 + `combat.py` 拆分，2026-09-19；
提交链 `fe738b8 → 84d166f → 967dbe4 → a2e2161 → 80d6cce`，基线 `adfd9d8`）的每条结论在下面逐条给出状态：

* ✅ **仍成立**：本次按同一口径复测，结论不变；
* 🔄 **已被更新**：方向正确，但数字/范围被本次复测替换（以后引用新数字）；
* ⬆️ **已被覆盖**：本次给出了更强或更晚的结论，旧表述不再单独引用。

`报告.md` 原文保持不动（历史留档）；**本节取代它作为「当前结论」**。

### 10.1 性能结论（`报告.md` §〇 / §二）

| `报告.md` 的结论 | 旧数值（2026-09-19） | 本次复测（第 5.1 节，三树同脚本同口径） | 状态 |
| --- | --- | --- | --- |
| 开局 `GameEngine()` 数量级下降 | 53.75 → **2.00 ms**（×27，小局） | 73.15（`adfd9d8`）→ 2.18（`24ff69e`）→ 2.32（HEAD）ms，**×31.6** | ✅ |
| 一次预演数量级下降 | 1.107 → **0.347 ms**（×3.2，小局）<br>34.83 → **0.669 ms**（×52，2000 事件长局） | 1.314 → 0.390 → 0.404 ms，**×3.26**<br>58.06 → 1.030 → 1.059 ms，**×54.8**（3000 事件） | ✅ |
| 一次 AI 决策数量级下降 | 27.67 → **12.13 ms**（×2.3，小局）<br>774.71 → **53.94 ms**（×14，长局） | 32.66 → 14.47 → 14.63 ms，**×2.23**<br>1358.36 → 89.33 → 94.19 ms，**×14.4** | ✅ |
| deepcopy 次数下降 | 预演 6.0 → **2.0**；小局决策 1232.3 → **302.3**；长局决策 50832.3 → 1902.3 | 预演 6.0 → 2.0 → 2.0（**逐位相同**）；小局决策 1232.3 → 302.3 → 301.3（**逐位相同**）；长局决策本次未测（长局只量时延） | ✅（长局 deepcopy 项标为未复测） |
| 长局收益大于小局的原因（`combat_events` 占深拷贝 95%+） | 4200 事件时 `deepcopy(state)` 33.7 ms | 未复测分解；与本次「长局预演 ×54.8」的观测一致，未被反驳 | ✅ |
| 「各改动贡献」只给估算区间、未逐项拆分 | 未测量（明确标注为估算） | 仍未逐项拆分；本次只新增一条归因估算（AI 决策 +4.4% 中 resolution 记账约 2.6%） | 🔄 口径保持「估算」，建议 3 仍未做（见 10.5） |
| §2.3 未测量项：分片拆分收益、并发/多进程、内存占用 | 未测量 | 仍未测量；本次也没有引入新的并发/内存口径 | ✅ |

### 10.2 行为中性结论（`报告.md` §〇 / §1.2 / §3.3）

| `报告.md` 的结论 | 本次状态 | 状态 |
| --- | --- | --- |
| 性能改动**行为中性**；并给出 4 条读法（基线 `98f2bfe2…`；基线+补丁 `6aa26df8…`；优化树默认 = `--force-preview-transaction` = `6aa26df8…`；`--patch-runtime-leak` 在已修复树上是 no-op） | 今天在 HEAD 复测：默认指纹 = `--force-preview-transaction` = `6aa26df8…`（第 4.3 节），判定链依然成立 | ⬆️ 已升级为 **60 例扫参门禁**（第 4.4 节） |
| 「`adfd9d8` → HEAD 的全部行为差额 = `80d6cce` 那一处预演副作用修复」 | 适用范围到 `24ff69e`；从 `f85fa11` 起更强的表述：`24ff69e` 与 `f85fa11` 的 3 例指纹（`6aa26df8…`）与 60 例扫参指纹（`85d2830d…`）**全部相同** | ⬆️ |
| 「**不声称** AI 行为与 `adfd9d8` 逐字节相同」 | 仍然不要这样声称：本次扫参也只跑在 `24ff69e` 与 `f85fa11` 上，`adfd9d8` 仍是靠 `--patch-runtime-leak` 把差额单独摘出来 | ✅ |
| 顺带修掉的那处预演副作用（combat 运行态按 `id(entity)` 建索引造成的"老键"） | 本次 Phase 1–3 把它升级为通用契约：`engine/sandbox.py` 隔离清单（14 项运行态 + 上下文 + 事件池 + 行动历史 + 惰性属性）+ `engine/pollution_guard.py` 逐路径快照比对 + 32 例契约测试；Phase 8 的随机压力又实测揪出 5 个漏点并补入清单 | ⬆️ 已被覆盖（从「补一处」变成「有检测器的契约」） |
| 预演等价性与性能护栏（`tests/test_action_preview_parity.py`） | 今天复测 **11 passed**（0.38 s）；本次又新增 32 例零污染契约与组合/随机测试里的现场断言 | ⬆️ 已被覆盖（更强） |

### 10.3 测试与护栏结论（`报告.md` §〇 / §3.1 / §3.4 / §3.5）

| `报告.md` 的结论 | 本次复测 | 状态 |
| --- | --- | --- |
| 全量 `8 failed / 1602 passed / 3 xfailed in 99.31s` | `8 failed / **1722 passed** / 3 xfailed in 139.22s`；8 条失败**逐条同名**：`test_ai_basic_attack_candidate::test_at_one_by_one_daowen_still_wins`、`test_ai_tactics::test_ai_can_declare_parry_under_lethal_threat`、`test_build_learner::test_valid_and_invalid_are_separated`、`test_unified_ai::test_ai_player_is_the_combat_and_high_level_entrypoint`、`test_win_only_ai` ×4 | 🔄 已被更新（pass +120，失败集合不变） |
| 8 条失败是既有 AI/mock 类，与本轮无关、不在范围内 | 仍成立：同一份名单，本次同样未处理 | ✅ |
| 迁移护栏 `10 []`（自动 glob `engine/combat_parts/*.py`，新分片自动纳入） | 今天复测 `10 []` | ✅ |
| §3.5 确定性与可复现性（同树连跑相同；身份标识在对比前规范化剔除） | 仍成立；本次两处新增的不确定源已单独处理：污染检测器字典遍历按键排序、trace `history` 环形上限 500（都是 debug 工具） | ✅（补充） |

### 10.4 范围边界（`报告.md` §四）逐行

| `报告.md` 声明「未改动」 | 本次状态 |
| --- | --- |
| 规则与数值：伤害/回复/代价/道纹结算公式一行未改 | ✅ 仍成立。`24ff69e..HEAD` 对 `engine/combat_parts/**` 的全部改动是 **+97 / −13**；这 13 行删除是 `damage_death.py` 里那段旧的私有深度熔断（搬进 `ResolutionContext`，阈值同为 64，异常类仍是 `RecursionError` 子类）。没有任何公式或数值行被改；「入口只是开帧 + 原实现体」由 `tests/test_effect_resolution_path.py` 与第 6 节证据锁定 |
| `engine/dice.py`、事件池语义 | ✅ 语义未改。补充两点：预演不再写真实事件池/运行态（Phase 1 修的是**预演副作用缺陷**，实盘指纹不变）；失败行动回滚会换掉 `engine.dice` 的对象身份（内容一致，既有行为，见第 7 节风险 2） |
| `combat.py` 的对外契约（`CombatEngine` 仍是唯一入口，调用点一处未动） | ✅ 仍成立：调用点未动，公开方法名/签名/MRO 不变；新增的只是 `self.resolution`（新属性）与「开帧入口 + `_xxx_impl`」写法（`MAX_EFFECT_CHAIN_DEPTH` 仍是同值别名，`_effect_chain_depth` 仍是兼容属性） |
| `sim/` 既有工具（连击/池化预计算/对局工具/`balance_sim`/`ai_tactics` 评分权重） | ✅ 仍成立：评分权重与既有工具未改；本次只新增 2 个调查/阈值工具，并给 `behavior_trace.py` 加了一个**默认关闭**的 `--cases` |
| 既有 8 项失败未处理 | ✅ 仍成立（同一名单，仍未处理） |
| 已知遗留：`dm_rulings` 的 fts5 小瑕疵、Phase 4 池去重 | ✅ 仍成立：`engine/dm_rulings.py:138` 的 fts5 建表与 Phase 4 池去重（审计工具口径）原样保留 |

### 10.5 `报告.md` §六「下一步建议」5 条的现状

| # | 上一轮的建议 | 现状 |
| --- | --- | --- |
| 1 | 把预演副作用纳入常驻护栏（沙盒写入白名单 / 运行态沙盒化） | ⬆️ **已落地且更彻底**：`engine/sandbox.py` 隔离清单（14 项运行态 + 上下文 + 事件池 + 行动历史 + 惰性属性）+ `engine/pollution_guard.py`（`snapshot_runtime_state()` / `assert_runtime_unchanged()`，逐路径报差异）+ `tests/test_sandbox_pollution.py` 32 例。不是白名单而是快照比对，因此「忘了登记某一项」会被测试抓住（Phase 8 用它实测揪出 5 项） |
| 2 | 把三个种子扩成扫参（`--cases N`，50~200 例，发版前行为回归门禁） | ✅ **本次已落地**：`sim/behavior_trace.py --cases 60`（第 4.4 节），`24ff69e` 与 HEAD 同为 `85d2830d…`；用例表契约由 `tests/test_behavior_trace_sweep.py` 锁死。建议区间上限（200 例）可按发版节奏再放大 |
| 3 | 逐项拆分性能收益（规则缓存 / 事务快照 / 事件流共享 / `play_turn` 顺序各自量化） | 🔄 **仍未做**：本次只给了整体三树对比与 AI 决策 +4.4% 的归因估算（resolution 记账约 2.6%），未做按提交 checkout 的逐项拆分 |
| 4 | `dm_rulings` fts5 瑕疵与 Phase 4 池去重（既有遗留） | 🔄 **仍未处理**：保持原样（不在本次范围内） |
| 5 | 若继续拆 `api.py`（约 5.7k 行），沿用分片 + 护栏自动 glob 模式 | 🔄 **仍未做**：`engine/api.py` 现在 5760 行；本次只在其中加了 `begin_action` / `end_action` 两个边界与失败路径收尾 |

### 10.6 `报告.md` §五 验收清单今天的输出

| 上一轮验收项 | `报告.md` 当时的输出 | 今天（`f85fa11`）的输出 |
| --- | --- | --- |
| ① 全量 pytest | `8 failed / 1602 passed / 3 xfailed / 99.31s` | `8 failed / 1722 passed / 3 xfailed / 139.22s`，失败名单逐条同名 |
| ② `tests/test_action_preview_parity.py` | `11 passed` | `11 passed in 0.38s` |
| ③ 迁移护栏 | `10 []` | `10 []` |
| ④ `sim/behavior_trace.py` | `TRACE_SHA256 6aa26df8…` | `TRACE_SHA256 6aa26df8…`（逐字节相同） |
| ⑤ `--force-preview-transaction`（应等于 ④） | `6aa26df8…` | `6aa26df8…` |
| ⑥ `sim/perf_bench.py` 小局 | 对照 `/tmp/bench_before_small.json`（53.75 / 1.107 / 27.67 ms） | 见第 5.1 节：同口径三树对照（绝对值跨会话不可比，倍数复现） |
| （本次新增）扫参门禁 | —— | `SWEEP_SHA256 85d2830d…`（60 例，`24ff69e` 与 HEAD 相同） |

### 10.7 不应再引用的旧口径（防止以后混用）

1. **绝对毫秒**：旧报告的 53.75 / 2.00 / 34.83 / 0.669 / 774.71 / 53.94 ms 只属于 2026-09-19 那次会话的机器状态；
   今天同脚本同口径复测得到的是 73.15 / 2.18 / 58.06 / 1.03 / 1358.36 / 89.33 ms（机器慢约 1.2–1.8×，长局事件数也不同）。
   以后**引用倍数（第 5.1 节），不引用绝对毫秒**。
2. 「`adfd9d8 → HEAD` 全部行为差额 = `80d6cce` 那一处」的表述**适用范围到 `24ff69e`**；当前口径是第 4.3 / 4.4 节（3 例 + 60 例指纹在 `24ff69e` 与 HEAD 相同）。
3. 「`8 failed / 1602 passed / 3 xfailed`」→ 现在是 **`8 failed / 1722 passed / 3 xfailed in 139.22s`**。
4. 「建议 2（扫参）未做」不再成立——已落地（第 4.4 节）。
5. `报告.md` 附表里 `sim/behavior_trace.py` 只有两个诊断开关——现在多了一个**默认关闭**的 `--cases`（第 4.4 节），日常验收输出格式未变。
6. 「预演副作用」这类缺陷现在的常驻防线是 `tests/test_sandbox_pollution.py`（32 例）+ 组合/随机测试里的现场断言，不再依赖「记得补断言」。
