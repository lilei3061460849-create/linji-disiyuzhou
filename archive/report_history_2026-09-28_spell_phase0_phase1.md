# 报告：法术系统重构 Phase 1 完成（2026-09-28）

> **归档说明（2026-09-29）**：本文件是法术重构 Phase 0/Phase 1 版 `报告.md` 的原文快照（远端提交 `255c513`），Phase 2 完成后归档。当前有效状态见根目录 [`报告.md`](../报告.md)。

> 本文件是**当前有效状态**统一文档（口径见 `AI_EXPERIENCE.md#文档管理规则`）：
> 只记录最新一次任务的完整结论、规则变更与待办。上一轮（动作层重构+聚能/蓄锐+初版法术审查）
> 已合并在本文第三、四、五节保留为基线，不出 archive（同日同一任务线，滚动维护）。
> 本轮**只读审计**，未修改任何生产代码。分支 `arena/01a0e397-linji-disiyuzhou`。
> 严格按用户令：Phase 0 只做架构审计与模型冻结，不实现新玩法、不改规则、不删 API。

---

## 📌 当前待办（置顶）

> 待用户确认后推进。最新、最高优先级在最上。

| 编号 | 内容 | 状态 | 等待 |
|---|---|---|---|
| Phase 2 | **瞬发+lifecycle**：`trigger=immediate`+`lifecycle=instant`；cast(flow=...)入口；瞬发双步/动态X/资源不足中断；每步执行时重新校验资源；消耗1次出手 | 待启动 | Phase 1 已落地，等待确认进入 Phase 2 |
| Phase 2 | 瞬发（`trigger=immediate/lifecycle=instant`）+ lifecycle 三态 + cast(flow=...) | 待 Phase 1 |
| Phase 3 | IfStep 改执行期求值，保留"同次反应多命中"的事件级分支冻结 | 待 Phase 2 |
| Phase 4 | LoopStep 由执行器迭代；取消调用方手工展开 cycles；资源不足 = interrupted 不回滚 | 待 Phase 3 |
| Phase 5 | define_spell 默认 lifecycle=battle；battle_end 清战场绑定；存档不序列化 battle 绑定 | 待 Phase 4 |
| Phase 6 | 8 个压力测试 + 反应边界回归 | 待 Phase 5 |
| KB-1 | **知识库矛盾修复**：AI_EXP/RME/engine.RME 里「出手固定2次·唯一额外来源是遗物」的旧条目须补注「蓄锐（rest）+1出手」也是合法来源；怪物 action_count 描述需改为"按面板，大多数怪物为1攻+1道纹" | 待处理（文档改动，可与 Phase 1 同批提交）|
| KB-2 | 通读全库排查更多规则矛盾（道纹代价/遗物叠加/自动反应 X 选择/必中 self-buff 口径等），统一进 AI_EXPERIENCE「当前执行与验证口径」 | 持续进行 |
| ④ 副本 | 四阶副本【怠惰之罪·温柔乡】正文/怪物/事件/遗物名未定 | 挂待办 | 用户提供输入 |
| 狂暴/兴奋/急速/坠落/避风铃/守夜灯/残骸/冥婚 | 上一轮列出的 8 项规则/怪物/遗物待核对条目 | 挂待办 | 用户确认口径 |
| Phase 0 未解决问题 | 瞬发 X 提交形式、Loop termination 语法、自动路径 X 策略、镇魔印自动策略、老存档 lifecycle 兼容、IfStep 深度上限、新反应提交结构——共 7 项，已在 §六 明确列出 | Phase 1 前/中逐项确认 |

### 已知知识库矛盾（待修正，不先动代码）

| 位置 | 旧文 | 当前代码/最新裁定 |
|---|---|---|
| AI_EXP:179、AI_EXP:1160、README:43、engine/README:169 | 「轮回者固定2次出手」「唯一额外来源是遗物」 | 2026-09-28 裁定 `rest（蓄锐）` 是新的基础动作：消耗1出手，下回 action_count+1（蓄锐·增 buff 维持1回）。蓄锐与遗物并列，同为 action_count 的+1来源。代码已实装（Entity.action_count 叠加「蓄锐·增」），文档待更新。 |
| AI_EXP:1161 | 「怪物每回合固定2次出手=1攻+1道纹」 | 代码侧怪物按面板 attack_count/dao_wen_uses 计算，多数怪物确实是1攻+1道纹，但多击怪/道纹怪可突破；文档应改为"默认2次，怪物面板可覆盖"。 |

---

# Phase 1 交付报告

## Commit
（提交后填入）

## 一、改了什么

新增文件：
- `engine/spell_execution.py`：新增五个核心数据类
  - `TriggerType`（13个触发时机枚举，IMMEDIATE 预留）
  - `Lifecycle`（instant/battle/permanent）
  - `ExecutionStatus`（running/completed/interrupted/failed）
  - `StepStatus`（completed/dodged/skipped/interrupted/failed）
  - `InterruptReason`（mana_insufficient/target_invalid/target_untargetable/daowen_unusable/speed_insufficient/invalid_x/invalid_input）
  - `SpellDefinition`：静态定义（name/required_daowen/trigger/lifecycle/body/rank/automatic/loop/...），不持有任何运行时状态
  - `SpellBinding`：definition+armed+source（Phase 1 仅定义，未替换 entity.spells/armed_spells；Phase 5 接线）
  - `SpellCastRequest`：cycles+branch_signature+extra，把"本次施法决策输入"从 submission dict 结构化
  - `StepRequest`：x/target_ref/dodge/dodge_relic_target_ref/extra
  - `SpellExecution`：caster/attacker/refs/request/flat_steps/status/results/cycle_index/step_index/target_resolver/skip_predicate/on_step + run_all() 主驱动
  - `StepResult`：status/daowen/x/target_ref/target_name/cost_paid/mana_gained/execution/reason/detail + to_log() 转为旧日志格式

修改文件：
- `engine/combat_parts/spells.py`
  - 顶部 import spell_execution 模块的所有公开类
  - 新增 `_execute_single_daowen_step(...) -> StepResult`：单步执行核心，统一处理道纹可用性/X 合法性/目标解析/目标失效/法力消耗/dodge/apply_daowen_effect；正常中断（法力不足/目标失效/道纹不可用/速度不足）返回 StepResult(status=INTERRUPTED/SKIPPED)，不再 raise ValueError 跨函数冒泡
  - 新增 `_build_spell_definition_from_flow(name, flow, trigger_str) -> SpellDefinition` 辅助，从现有 SPELL_FLOWS / _parse_custom_spell 返回的 dict 构造 SpellDefinition
  - **重构 `_resolve_spell_reactions`**：内部改为构造 SpellDefinition→SpellCastRequest→SpellExecution，调用 run_all() 驱动，结果通过 execution.logs() 输出；反应法术路径法力不足等中断不再 raise ValueError（保留已结算步骤），契约错误仍抛 FAILED→ValueError
  - **重构 `resolve_global_trigger_spells`**：同上；通过 target_resolver 注入 `_resolve_global_entry_target` 适配全局时点身份映射
  - **重构 `resolve_daowen_trigger_spells`**（咎由自取/目标发动道纹前）：同上；通过 target_resolver 注入 actor/holder 身份映射；通过 skip_predicate 处理"坠落目标不飞行跳过""血债无前序伤害跳过"；通过 on_step 回调更新 previous_damage（而不是事后 zip 日志）
  - validate 函数保持原样（仍抛 ValueError 作为契约错误——这是正确的，因为校验失败属于"调用方违反提交契约"，不是正常游戏流程）

## 二、为什么这么改

对应 Phase 0 审计报告 §1.3 的混用 #4/#5/#6/#8：
- 混用#4"没有 SpellExecution"——现在有了 SpellExecution 作为唯一运行时状态持有者；
- 混用#5"循环由调用方 cycles 展开"——Phase 1 仍暂时保留 cycles 数组结构（因为真正的 LoopStep 执行器接管留到 Phase 4），但已把"每 cycle 每 step 的执行"收束到 _execute_single_daowen_step 一个函数里，Phase 4 只需改 run_all() 的循环控制即可，不用动三个 resolver；
- 混用#6"正常法力不足用 ValueError"——现在法力不足/速度不足/目标无法选中返回 StepResult(status=INTERRUPTED)，SpellExecution.run_all() 置状态并停机，不再跨 combat action 抛 ValueError（真正的契约错误如非法 AST/entry 非 dict 仍抛 ValueError）；
- 混用#8"三套 resolver 重复执行逻辑"——三套 resolver 的内部 for/for 循环里那段 resolve→spend_mana→dodge→apply 的 ~30 行代码已收束为 `_execute_single_daowen_step` + SpellExecution.run_all()，resolver 本身变成"构造上下文→构造 Request→run→转日志"的薄壳。

## 三、当前法术模型（Phase 1 后）

```
  ┌────────────────────┐
  │  SpellDefinition   │  静态，可缓存/序列化/复用
  │  name/trigger/     │
  │  lifecycle/body/…  │
  └─────────┬──────────┘
            │ _build_spell_definition_from_flow()
            │  （Phase 5 会直接从 DSL/内置表产出）
            ▼
  ┌────────────────────┐
  │  SpellBinding      │  定义了但暂未接入 entity.spell_bindings
  │  (Phase 5 接线)    │  Phase 1 仍用现有 spells/armed_spells 字段
  └─────────┬──────────┘
            │ prepare/validate 产出候选
            ▼
  ┌────────────────────┐
  │ SpellCastRequest   │  一次施法的决策输入
  │ use/cycles/branch  │  (cycles 暂保留，Phase 4 移除)
  └─────────┬──────────┘
            │ 入口不同（反应/全局/道纹前/瞬发）
            ▼
  ┌────────────────────────────────┐
  │  SpellExecution                │ 唯一运行时状态
  │  caster/attacker/refs/request  │ run_all() → 逐 step 调用：
  │  status/results/cycle_idx      │   _execute_single_daowen_step()
  │  target_resolver/skip_pred/    │     → StepResult
  │  on_step                       │     → 追加到 results
  └─────────┬──────────────────────┘
            ▼
  ┌────────────────────┐
  │    StepResult      │ completed/dodged/skipped/interrupted/failed
  └────────────────────┘
```

三套 resolver 现在都是：构造 ctx + （可选）resolver/predicate 适配 → SpellExecution.run_all() → logs()。Phase 2 加瞬发只需新增一个入口（cast flow→构造 immediate 定义→run），不再复制第四套 resolver。

## 四、哪些旧 API 保留

所有对外 API 签名保持不变：
- use_spell / define_spell / cast / use_daowen
- prepare_spell_reactions / validate_spell_reaction_submission / resolve_spell_reactions
- prepare_daowen_trigger_spells / validate_daowen_trigger_spells / resolve_daowen_trigger_spells
- prepare_global_trigger_spells / validate_global_trigger_spells / resolve_global_trigger_spells
- 自动反应路径 _fire_auto_reaction / _auto_after_life_lost_decision（仍生成 dict 决策，传进 _resolve_spell_reactions）

返回给调用方的 log 格式（{"spell":..., "daowen":..., "x":..., "target":..., "execution":...} 等）保持一致，测试断言无需修改。

## 五、哪些规则没有改变

Phase 1 只做执行架构统一，未改变任何游戏规则：
- 道纹效果公式/伤害/治疗/法力/速度/闪避/必中/招架/坠落/血债前序伤害/癌变/雕塑/封印延迟/遗物触发/反应嵌套/分支冻结/共享法力预算/自动反应 X 选择/全局时点/自动镇魔印/道纹前触发等行为完全保持；
- 反应窗口 prepare→validate→resolve 三段契约保持（包括分支冻结签名 _engine_branch_signature 的写入与校验——仍在 validate 里通过 _freeze_spell_decision_branch 完成）；
- _resolving_life_lost_reactions 防连锁计数器保留，嵌套深度仍严格 1 层。

## 六、测试结果

```
非 AI 规则测试：1641 passed, 1 xfailed（与 Phase 0 基线一致，0 新增/0 丢失）
AI/mock/sim 目录：5 failed（历史已知，本轮无关）
重点法术相关测试全部通过：
  test_spell_dsl.py                     18 passed
  test_spell_loop_mana_gain.py          11 passed
  test_reaction_nesting.py               4 passed
  test_other_reaction_windows_non_attack.py 5 passed
  test_after_life_lost_any_hp_loss.py   10 passed
  test_shouyedeng_reaction_spells.py     5 passed
  test_automatic_seal_spell.py           5 passed
  test_guard_command.py                 16 passed
  test_learn_spell_prerequisite.py       4 passed
  test_open_trigger_extensibility.py     1 passed
  test_bizhong_daowen_paths.py          12 passed
  test_luanzang_target_daowens.py        8 passed
  test_reaction_spell_save_roundtrip.py  4 passed
  test_r39_r47_contract.py（含咎由自取伤害断言）全部通过
```

## 七、失败测试

无新增失败。5 个历史 AI/sim 失败：
- test_ai_basic_attack_candidate::test_at_one_by_one_daowen_still_wins
- test_ai_tactics::test_ai_can_declare_parry_under_lethal_threat
- test_win_only_ai::test_win_only_includes_parry_in_real_candidate_path
- test_build_learner::test_valid_and_invalid_are_separated
- test_unified_ai::test_ai_player_is_the_combat_and_high_level_entrypoint
  （均为 AI 策略/评分模型问题，与法术执行核心无关）

## 八、剩余风险

1. **ValueError 退出正常流程的改造只做了 resolver 主路径**：`_resolve_entry_target` 里"target_ref非法""目标在飞行"两处仍抛 ValueError，但这两处只在"role=any 且提交方给出非法 ref"时触发——这属于调用方契约错误（不是正常游戏流程），保留异常合理。全局路径 `_resolve_global_entry_target` 同理。Phase 2-4 会逐步审视是否需要更柔和的处理。
2. **循环仍由调用方展开为 cycles**：Phase 1 暂保留此契约以避免破坏现有提交结构。Phase 4 真正引入 LoopStep 后，cycles 退化为只包含 1 个模板 cycle（瞬发）或由 AI/自动决策给出当轮所有步骤的 X/Target（反应）。
3. **SpellBinding 数据类已定义但未接线到 Entity**：目前 entity.spells / armed_spells 两个平列字段仍是事实源。Phase 5 才做 Binding 统一+存档兼容。
4. **自动反应路径（_auto_after_life_lost_decision）仍自己组装 dict 决策**：它不经过 SpellCastRequest/StepRequest，而是构造 dict 传给 _resolve_spell_reactions，后者在新实现里也能接收 dict（_execute_single_daowen_step 对 dict 和 StepRequest 都兼容）。Phase 2-3 会把自动路径也改为构造 SpellCastRequest。
5. **IfStep 在进入 SpellExecution 之前就被 _flatten_flow_steps 展开**：Phase 1 刻意不改变这一行为（以避免破坏分支冻结的既有契约）。Phase 3 改为"执行期求值"时，会把 IfStep 真正作为控制流节点放进 Execution.control_stack。
6. **mana_gain 在 StepResult 中记录但 apply_daowen_effect 内已实际到账**：当前 StepResult 只是报告字段，不影响实际结算（与重构前一致）。

## 九、是否具备 Phase 2 开始条件

✅ 是。统一单步核心已就位，三个 resolver 的内部 for/for 都走同一执行路径。Phase 2（瞬发+lifecycle）只需要：
- 给 spell_dsl 加 TRIGGER_INSTANT 识别；
- 在 api.py::_action_cast 加 flow 参数，构造 immediate/instant 定义 + Request + Execution 驱动；
- SpellDefinition/SpellBinding 加 lifecycle 字段实际接线（instant 不建 Binding）。
不需要再复制任何 resolver。


---

## 一、一句话结论（Phase 0）

当前法术系统能跑、测试全绿，但**五个核心概念（Definition / Ownership-Binding / Execution / Runtime-State / StepResult）在代码里被严重混在一起**：
- `models.Spell` 既是**定义载体**又挂 `_parsed_flow` 这个**解析缓存**（运行时产物）；
- 内置法术根本没有 Spell 实例，由 `SPELL_FLOWS` 字典 + `spell_definition()` 运行时合成临时对象，定义和执行流是两份同构数据；
- "法术被装备/激活"状态用 `list[str]`（`armed_spells`）表示自创法术却不需要装配、只要 append 进 `entity.spells` 就永远生效，Ownership 和 Active 两个概念用两个不同机制表达；
- 执行层面有**三套并行 resolver**（反应 `_resolve_spell_reactions`、道纹前 `resolve_daowen_trigger_spells`、全局 `resolve_global_trigger_spells`），各自复制相同的循环/扣法力/dodge/apply 逻辑，还**用 ValueError 异常做正常资源不足流程控制**；
- 没有 SpellExecution 对象，一次施法的运行时状态（当前步骤、法力滚动预算、已结算效果日志）全部散落在局部变量里，循环由调用方手工展开成 N 份 cycles 数组提交——调用方就是执行器的一部分；
- 没有 StepResult，单步结果要么 raise（中断整个 combat action），要么 `continue`（跳过该步），要么 append 一个 dict 到 logs；
- Trigger 和 Lifecycle 完全没分离：所有法术都被隐式视为"永久被动触发器"，没有 trigger/lifecycle 字段；瞬发（immediate）、一次性（instant）、战场级（battle）三种语义根本没有数据结构表达。

**结论：需要分 Phase 1–6 按用户给定路径重构，但 Phase 1 的统一执行器是最紧迫的——先抽出唯一的 step→StepResult 执行核心，让三个 resolver 合并到一条路径；Phase 0 本身不改代码，本报告冻结现状与目标模型。**

---

## 二、Phase 0 审计 · 当前架构图

### 1.1 当前代码数据流（被动反应路径）

```
  use_spell (装配内置)       define_spell (自创)
        │                          │
        ▼                          ▼
  armed_spells: list[str]    entity.spells: list[Spell]  ← Ownership/Active 混在列表里
                                           │
                                           ▼ （首次触发时懒解析）
                                     spell._parsed_flow = {...}  ← 运行时缓存挂到定义对象上
                                                  │
        ┌─────────────────────────────────────────┴───────────────────────────────┐
        ▼                                                                         ▼
  _builtin_spell_flows(holder)                                       _parse_custom_spell(spell)
  → 读 SPELL_FLOWS + BUILTIN_SPELL_DAOWEN                          → spell_dsl.parse_spell_definition(text)
  → 按 armed_spells + can_use() 过滤                               → ParsedSpell(trigger, steps:ActionStep/IfStep, loop)
        │                                                                         │
        └───────────────── _eligible_spell_flows(holder, trigger) ────────────────┘
                                    │
                    ┌───────────────┼───────────────────────────────┐
                    ▼               ▼                               ▼
      反应窗口（受伤前/后/失前/失后）  道纹前窗口                全局窗口（战始/战终/回始/回终/敌回始/敌回终/自身回合结束）
                    │               │                               │
                    ▼               ▼                               ▼
      prepare_spell_reactions  prepare_daowen_trigger_spells  prepare_global_trigger_spells
      （_flatten_flow_steps 按  （同上+角色映射）               （同上+holder扫描+automatic）
       当前局面求值分支、列
       target_options、x 需求）
                    │               │                               │
                    ▼               ▼                               ▼
      调用方(AI/玩家/自动)按 prepare 结果手工构建提交 dict：
        {use: bool, cycles: [[{x, target_ref, dodge}], ...],
         _engine_branch_signature: [...冻结签名...]}
                    │               │                               │
                    ▼               ▼                               ▼
      validate_spell_reaction_   validate_daowen_trigger_     validate_global_trigger_spells
       submission                  spells                        （三个校验器互相独立复制）
                    │               │                               │
                    ▼               ▼                               ▼
      _resolve_spell_reactions    resolve_daowen_trigger_     resolve_global_trigger_spells
       （三套独立 resolver 互    _spells                       （三套 resolver 都在复制：
        相复制 for cycle/for step                                    for cycle in cycles:
        + spend_mana+dodge+apply）                                     for entry,step in zip(cycle,steps):
                                                                              spend_mana/apply/logs）
                    │               │                               │
                    ▼               ▼                               ▼
                         （道纹伤害路径：_apply_hostile_damage）
                    _fire_auto_reaction（非攻击路径失血自动触发，绕过调用方）
                         → _auto_after_life_lost_decision 自动选 X/dodge
                         → _resolve_spell_reactions 复用
```

**瞬发法术路径：目前不存在。** 玩家只能通过 `use_daowen`（单道纹）一次性发一道，无法一次施法执行多步；`cast` 动作只是 use_daowen/use_spell 的转发壳，没有独立执行路径。

### 1.2 关键类/函数职责表

| 概念层 | 文件 | 符号 | 当前实际职责 | 混了什么 |
|---|---|---|---|---|
| 静态定义 | `engine/models.py` | `class Spell` | name / required_daowen / trigger_condition / effect_flow / rank / automatic | 挂 `_parsed_flow`（懒解析缓存，运行时产物）；无法表达 trigger 类型（缺 immediate）、lifecycle、loop 字段、params、条件 AST |
| 静态定义（内置）| `engine/combat_parts/spells.py` | `SPELL_FLOWS` + `BUILTIN_SPELL_DAOWEN` | 9 个内置法术的 `{trigger, steps, loop?, effect_flow?, automatic?}` + 道纹清单 | 内置法术没有 Spell 实例，靠 `spell_definition()` 运行时临时合成，定义的"事实源"有两份：一是 Spell 文本字段，二是 SPELL_FLOWS dict |
| 文本→AST | `engine/spell_dsl.py` | `parse_spell_definition / ParsedSpell / ActionStep / IfStep` | 纯文本解析：trigger 归一、条件递归解析、效果流程拆分、loop 标记 | AST 只保存在 `spell._parsed_flow` 或 _builtin 临时 dict，没有独立的 SpellDefinition 数据类长期承载 AST |
| 所有权/拥有 | `engine/models.py::Entity` | `spells: list[Spell]` | 角色"学会/自创"了哪些法术 | **自创永远 append 进这个列表，battle_end 不清理，等同于永久学会；且列表同时表达"拥有"和"激活"——自创法术不需要 armed 直接生效** |
| 装配/激活（内置）| `engine/models.py::Entity` | `armed_spells: list[str]` | 内置法术的"装配/激活"状态 | 与 spells 列表两套语义：内置靠 armed_spells 激活、自创靠 append 进 spells 就激活；disarm=true 只能卸内置、自创无卸路径 |
| 候选筛选 | `engine/combat_parts/spells.py` | `_builtin_spell_flows / _eligible_spell_flows` | 按时点+can_use() 列出当前可触发的法术 | 同时承担"读定义/解析/过滤"三件事；返回的是 flow dict 而非结构化对象 |
| 条件求值（触发瞬间）| `engine/combat_parts/spells.py` | `_flatten_flow_steps / _condition_resolver / evaluate_condition` | 用当前局面求值 IfStep，输出 ActionStep 列表 | 求值结果通过 `_freeze_spell_decision_branch` 冻结到提交 dict（`_engine_branch_signature`），运行时状态写进了调用方提交 dict |
| 校验 | 同文件 | `validate_spell_reaction_submission / validate_daowen_trigger_spells / validate_global_trigger_spells` | 检查 target/x/法力预算/dodge 速度 | **三个 validator 是三份并行代码，结构几乎一致**；校验期法力预算已经循环累加 mana_gain，但执行期法力不足直接 raise ValueError |
| 执行（反应）| 同文件 | `_resolve_spell_reactions` | 遍历 cycles 执行每步：resolve→spend_mana→dodge→apply_daowen_effect→log | 与另外两个 resolver 重复；缺 StepResult，失败靠 raise ValueError 中断整个战斗动作（不是中断单个法术）|
| 执行（道纹前）| 同文件 | `resolve_daowen_trigger_spells` | 同上 + 坠落/血债等 hardcode skip | 重复；坠落/血债 skip 逻辑硬编码在此，不属于通用执行层 |
| 执行（全局）| 同文件 | `resolve_global_trigger_spells` | 同上，holder 换成多角色扫描 | 重复 |
| 自动反应 | 同文件 | `_fire_auto_reaction / _auto_after_life_lost_decision / _max_auto_life_lost_x / _auto_reaction_dodge_decision` | 非攻击路径自动选 X/dodge，构造提交 dict 后调 `_resolve_spell_reactions` | 自动路径同时承担"选 X/dodge/组装提交"三件事，没有统一的 decision 结构 |
| 玩家动作 | `engine/api.py` | `_action_use_daowen / _action_use_spell / _action_define_spell / _action_cast` | 发动单道纹 / 装配法术 / 自创法术 / 合并入口 | cast 仅为壳转发；define_spell 立即 append 到 player.spells（永久生效）；没有"立即执行一串 steps"的入口 |
| 分支冻结 | `engine/combat_parts/spells.py` | `_freeze_spell_decision_branch / _flow_step_variants / _steps_for_spell_decision` | 首次校验后把分支选择签名写入 decision dict，后续命中间不漂移 | 把"控制流决策"作为 dict 字段存在提交里，不是 SpellExecution 对象的一部分 |

### 1.3 当前概念混用的具体位置（按用户令逐项列出）

#### 混用 1：Spell 同时是 Definition + Runtime Cache
- 位置：`engine/models.py:69 Spell` 数据类 + `engine/combat_parts/spells.py:190 spell._parsed_flow`。
- 问题：Spell 是 dataclass（应当是静态 Definition），但 `_eligible_spell_flows` 用 `setattr` 风格的 `getattr(spell, "_parsed_flow", None)` / `spell._parsed_flow = flow` 把解析后的 AST 缓存挂在 Spell 实例上。这个缓存是战斗运行时产物，不属于 Definition。
- 后果：同一 Spell 对象跨战斗携带状态；存档 pickle 时 `_parsed_flow`（含 ActionStep/IfStep）也要被序列化；测试中若复用同一 Spell 可能在不同战斗里拿到陈旧解析。

#### 混用 2：内置法术没有 Definition 实例，SPELL_FLOWS 同时是 Definition + 执行流
- 位置：`engine/combat_parts/spells.py:26 SPELL_FLOWS`、`:52 BUILTIN_SPELL_DAOWEN`、`:105 spell_definition`。
- 问题：内置法术从来没有 Spell 对象；`spell_definition()` 每次调用都 new 一个临时 Spell（name/required_daowen 从 BUILTIN_SPELL_DAOWEN 填，trigger_condition/effect_flow 从 SPELL_FLOWS.effect_flow 填），但执行时读的是 SPELL_FLOWS.steps（直接的 (daowen, role) 元组），不是 Spell 本身的字段。
- 后果：Definition 有两份表示——Spell 文本字段 和 SPELL_FLOWS dict；维护时必须同步改两处。ParsedSpell 和 SPELL_FLOWS entry 是同构但不同类型。

#### 混用 3：Ownership 与 Active 用两套完全不同的机制
- 位置：`engine/models.py:242 spells: list[Spell]` + `engine/models.py:246 armed_spells: list[str]`。
- 问题：
  - 内置法术：ownership 由"持有所需全部道纹 `can_use()`" 实时判定（`_builtin_spell_flows`），active 由 `armed_spells` 显式装配决定；
  - 自创法术：ownership 是 "spell in entity.spells"，active 没有单独状态——append 进 spells 就同时是"拥有+激活"，disarm 路径不处理自创；
- 后果：用户无法"自创一个法术但暂时不激活"；无法卸下自创法术；自创和内置的生命周期管理分叉。

#### 混用 4：没有 SpellExecution，一次施法的运行时状态散落在三处
- 位置：`_resolve_spell_reactions` 函数内的 `logs / cycle_index / flat_steps` 等局部变量；`submitted[spell_name]` 里的 `_engine_branch_signature / cycles`；holder 本身的 current_mana/current_hp。
- 问题：没有独立的 SpellExecution 对象来承载"这一次施法"的全部状态；连"本次施法到了第几步/为什么停"都是用 raise ValueError 向外抛，调用方无法拿到结构化的中断原因。
- 后果：瞬发法术要进来时没有位置放"当前控制流位置/当前循环计数/已结算的步骤结果/中断原因"。

#### 混用 5：循环由调用方展开成 cycles 数组，执行器不拥有控制权
- 位置：所有 resolver 的 `for cycle_index, cycle in enumerate(decision["cycles"])`；validate 期检查 `len(cycles)`。
- 问题：`loop=True` 只是一个标记，真正的循环次数由调用方在 `decision["cycles"]` 里预先放 N 个拷贝决定。`_auto_after_life_lost_decision` 只会放 1 个 cycle（自动反应不循环），AI/玩家必须手工预估 N。
- 后果：用户要求的"循环直到法力耗尽由执行器自然停止"不存在；执行器在校验期预扣 N 次成本，若 N 估少了法术提前结束（少打），估多了在结算期法力不足抛 ValueError（异常）。

#### 混用 6：没有 StepResult，用异常/continue/日志dict三态混搭表示结果
- 位置：三个 resolver 的内部循环：
  - 目标失效 → `logs.append({skipped: "目标已失效"})` 然后 `continue`（该步跳过，法术继续）；
  - 法力不足 → `raise ValueError("法术结算法力不足")`（整个 battle action 抛错，向上冒泡）；
  - 速度不足闪避 → `raise ValueError("速度不足")`；
  - 成功 → `logs.append({execution})` 继续。
- 问题：没有 `completed/interrupted/failed/skipped` 的统一 StepResult；"资源不足"被抛成 ValueError（跨整个战斗动作的异常），属于用异常做正常流程控制。
- 后果：Phase 2-4 要求"中途资源不足保留已结算效果、中断法术"目前无法实现——一旦法力不足直接 raise，整个 combat action 崩溃，已经 append 到 logs 的内容不会成为正式结果（未返回前就异常）。

#### 混用 7：Trigger 和 Lifecycle 完全没分离，只有"永久被动触发器"一种
- 位置：所有 Spell/SPELL_FLOWS 都只有 trigger_condition 文本/SPELL_FLOWS["trigger"] 字段，没有 lifecycle 字段；所有自创法术都写入永久列表 player.spells，battle_end 不清。
- 问题：
  - 没有 trigger=immediate（瞬发）的概念；
  - 没有 lifecycle=instant（一次性不入库）；
  - 没有 lifecycle=battle（战斗结束清）；
  - 所有自创都是 lifecycle=permanent + trigger=某个事件；
- 后果：用户示例 1（瞬发连击）无法表达；示例 2 的条件反击在当前可以用 trigger="受到伤害前"表达，但会永久写进构筑污染存档（Phase 5 要改）。

#### 混用 8：三套 resolver 重复实现
- 位置：`_resolve_spell_reactions` / `resolve_daowen_trigger_spells` / `resolve_global_trigger_spells` 三段代码几乎逐行一致（target 解析 / mana 扣减 / dodge / apply_daowen_effect / logs.append）；三个 validator 同理。
- 问题：通用执行核心没有被抽出来，每新增一种触发类型（如瞬发）就要复制第四份 resolver/validator。
- 后果：Phase 1 统一执行器是重构的关键节点。

#### 混用 9：条件求值时机被"分支冻结"机制绑死
- 位置：`_flatten_flow_steps` 在 prepare 期就把 IfStep 求值成 ActionStep 列表；`_freeze_spell_decision_branch` 把结果签名写入提交。
- 问题：这个设计是为了修复"同一次攻击多命中之间分支漂移"的 bug（test_blood_splash_low_mana_branch_stays_frozen_across_hits）——意思是"同一触发事件"里条件判定一次锁定，这是对的。但在瞬发+循环语义里，用户要求"**每执行一个动作都重新求值**、每轮循环重新判定"——当前 IfStep 只在触发瞬间求一次，求值完 IfStep 就被展开掉了，后续执行路径没有机会再看到条件节点。
- 后果：Phase 3/4 需要重构条件求值时机——不是删掉冻结机制，而是要区分"同一次触发内的多命中"（保持冻结）和"瞬发程序里执行过步骤后条件重判"（不冻结，每到条件节点就重算）。

### 1.4 目标概念模型（Phase 0 冻结，不写代码只描述）

```
SpellDefinition（静态，可缓存、可序列化、可复用）
  ├─ name: str
  ├─ trigger: TriggerImmediate | TriggerEvent(name)
  ├─ lifecycle: LifecycleInstant | LifecycleBattle | LifecyclePermanent
  ├─ body: list[Stmt]
  │     Stmt = ActionStep(daowen, target_expr, params_expr)
  │          | IfStep(cond: Expr, then_body, else_body)
  │          | LoopStep(body, termination: Expr)
  └─ 所需元数据（rank/automatic/...）

SpellBinding（某个角色的法术状态，挂 Entity）
  ├─ definition: SpellDefinition
  ├─ state: armed | disarmed
  └─ （lifecycle=permanent 时随存档序列化；lifecycle=battle 不进存档；lifecycle=instant 根本不建立 binding）

SpellExecution（一次具体施法的运行时，栈结构，不入存档）
  ├─ caster / trigger_ctx（含 attacker/target/actor 等角色映射）
  ├─ definition 引用
  ├─ control_stack: list[Frame]     （Sequence/If/Loop 的栈帧，支持真正的嵌套）
  ├─ current_step_index
  ├─ status: running | completed | interrupted | failed
  ├─ interrupt_reason: enum + detail（mana_insufficient / target_invalid / daowen_unusable / ...）
  ├─ results: list[StepResult]      （按执行顺序累积，不回滚）
  └─ step() -> StepResult            （单步推进，返回结构化结果）

StepResult
  ├─ status: completed | dodged | skipped | interrupted | failed
  ├─ daowen / x / target
  ├─ effects: list[effect 结果]（伤害/治疗/状态/法力变化 等）
  ├─ cost_paid
  └─ detail（跳过原因/中断原因）
```

设计原则：
- SpellDefinition 不得持有任何本次战斗/本次施法的运行时字段（解析缓存放引擎侧 LRU 或 definition 自身一次解析冻结）；
- SpellBinding 不得混入执行状态；
- SpellExecution 是唯一的执行上下文，所有 resolver（反应/道纹前/全局/瞬发）最终都构造一个 SpellExecution 然后驱动它 step()；
- 循环由 LoopStep + 执行器内部迭代实现，禁止调用方展开 N 份 cycles；
- 资源不足/道纹不可用/目标失效 → StepResult(status=interrupted/skipped)，执行器决定是跳过该步还是终止整个法术，**不抛 ValueError 作为正常流程**（真正的编程错误/契约违例仍可抛）；
- 条件在控制流实际到达节点时重新求值（Evaluate-and-step，不是提前 flatten）。

### 1.5 新旧概念映射表

| 当前代码 | 目标模型 | 迁移方向 |
|---|---|---|
| `models.Spell`（含 `_parsed_flow` 缓存）| SpellDefinition（去缓存、加 trigger/lifecycle/body AST 字段）| 保留 Spell 类作为 Definition 载体；`_parsed_flow` 改为 definition.body，在 parse/构造时一次填充；移除 setattr 动态字段 |
| `SPELL_FLOWS` 字典 | SpellDefinition 常量表 | 把 9 个内置法术直接定义为 SpellDefinition 常量；`spell_definition()` 直接返回常量引用，不再每次 new |
| `BUILTIN_SPELL_DAOWEN` | SpellDefinition.required_daowen（Definition 自带）| 合并进 SpellDefinition |
| `entity.spells: list[Spell]` | `entity.spell_bindings: dict[name, SpellBinding]`（lifecycle=battle/permanent 都在这里）| 自创法术 append 时 lifecycle=battle（Phase 5）；内置法术也通过 Binding 表达（armed 是 state 字段，不是独立列表）|
| `entity.armed_spells: list[str]` | SpellBinding.state | 合入 Binding；内置法术默认 state=disarmed，use_spell 切 armed；自创法术 define 后默认 state=armed |
| `ParsedSpell / ActionStep / IfStep`（spell_dsl.py）| SpellDefinition.body 直接用这些 AST 节点（扩展 LoopStep / 参数表达式）| spell_dsl 现有 AST 就是 Definition 的 body，加 LoopStep 节点和参数表达式即可 |
| `_flatten_flow_steps`（触发时求值展开）| SpellExecution 内 Evaluate-and-step（条件节点在执行到的时候求值）| 保留 `_flatten_flow_steps` 的 evaluate_condition + resolver，但改为每到 IfStep 时调，不是 prepare 期一次展开；保留"同一触发事件内多命中"的分支冻结机制（改存到 SpellExecution 或 ctx 上，不存到 submitted dict）|
| `_resolve_spell_reactions` / `resolve_daowen_trigger_spells` / `resolve_global_trigger_spells` | 统一 `SpellExecution.step()` 驱动循环；三个 resolver 退化为"构造 ctx + 驱动 execution 直到结束"的薄壳 | Phase 1 核心重构 |
| `validate_spell_reaction_submission` / `validate_daowen_trigger_spells` / `validate_global_trigger_spells` | 统一"预演 SpellExecution" 或 "静态校验 Definition 结构 + 不预演结果"；瞬发路径不需要提前校验（执行期自然出 interrupted 结果）| Phase 1-2 决定：用户令明确说"每步执行时验证 X 上限"，所以校验应当就是执行期 step() 本身，无需单独的静态预算校验（避免校验与结算口径再次不一致） |
| `decision["cycles"]` 由调用方展开 N 份 | LoopStep 在 Definition.body 里；执行器自己迭代直到 termination 为真或资源中断 | Phase 4：取消 cycles 数组；提交只需要 use=true 与每步的 X 选择策略（或 X 由玩家在瞬发提交时给一个列表/函数；反应 X 由 AI/自动决策在触发时给出）|
| `_freeze_spell_decision_branch` 把签名写进 submitted dict | 同一次触发事件的 branch 快照存进 SpellExecution.ctx（或 TriggerContext）| 保留行为，换存放位置 |
| `raise ValueError("法力不足/速度不足")` 用于正常中断 | StepResult(status=interrupted, reason=...) | ValueError 只用于真正的契约违例（提交结构错、target_ref 不存在等编程错误）；正常游戏中断走 StepResult；中断时不抛跨法术异常，只是该法术终止 |
| `continue on 目标失效`（单步跳过，法术继续）| StepResult(status=skipped)；执行器根据情况决定"终止"还是"跳过继续"——目标失效终止，坠落/血债等特殊 skip 保留为 skipped 但继续 |
| logs.append({execution}) | StepResult 列表作为 SpellExecution.results；logs 可以从 results 派生 |
| `_fire_auto_reaction` 的 `_auto_after_life_lost_decision` 自动构造提交 | 自动路径改为构造 SpellExecution + caster-side X 选择策略，驱动 execution |

### 1.6 哪些旧 API 可以保留兼容层

| 旧 API | 保留/迁移 | 兼容策略 |
|---|---|---|
| `use_spell(spell_name, disarm=bool)` | **保留** | 改为操作 SpellBinding.state；对内置法术从 SPELL_REGISTRY 查 Definition 后建/切 Binding；对自创法术切换 Binding.state 即可 |
| `define_spell(spell)` | **保留签名**，语义微调 | 仍然 1 出手；当前行为"自创永久被动法术"改为默认 lifecycle=battle；加可选字段 lifecycle/trigger 支持 instant |
| `cast(kind="daowen", daowen_name=..., x=..., target_ref=...)` | **保留** | 转发到新执行器：构造一个 lifecycle=instant+trigger=immediate 的单步 SpellDefinition 驱动 SpellExecution |
| `cast(kind="spell", spell_name=..., disarm=...)` | **保留** | 委托给 use_spell |
| `cast(flow=...)` 新形式 | **新增** | 瞬发法术入口，接收 flow 文本（或结构化 steps），构造即时 SpellDefinition 驱动 Execution，结果返回 StepResult 列表 |
| `use_daowen(daowen_name, x, target_ref)` | **保留（薄壳）**| 等价于 cast(kind="daowen",...)；内部统一走新执行器 |
| `prepare_spell_reactions` / `validate_spell_reaction_submission` / `resolve_spell_reactions`（AI 和测试用）| **保留签名** | 返回结构不变；内部改为 SpellExecution 驱动。注意 validate_* 的返回契约需要调整——因为循环不再静态预算，validate 改为"结构校验+单步预演一次"而不是"N 次法力预算" |
| `prepare_daowen_trigger_spells` / `validate_daowen_trigger_spells` / `resolve_daowen_trigger_spells` | **保留签名** | 同上 |
| `prepare_global_trigger_spells` / `validate_global_trigger_spells` / `resolve_global_trigger_spells` | **保留签名** | 同上；automatic 法术路径同样用 SpellExecution |
| `_flatten_flow_steps` / `_resolve_step_subject` / `_condition_resolver` / `evaluate_condition` | **内部保留，调用点改写** | 从"prepare 期一次性展开所有 IfStep"改成"执行期每到 IfStep 调 evaluate_condition"；_resolve_step_subject 通用化（给瞬发用）|
| `_auto_after_life_lost_decision` / `_max_auto_life_lost_x` / `_auto_reaction_dodge_decision` | **保留** | 改为自动 X 选择策略（传入 SpellExecution，每步取 X）|
| `entity.spells` 直接访问（测试/存档）| **兼容一段时间** | 保留 property 向后兼容，内部代理到 spell_bindings；存档读老版本时把 spells 列表迁移成 Bindings |
| `entity.armed_spells` 直接访问 | **兼容一段时间** | 保留 property，内部代理到 Bindings.state==armed |
| `SPELL_FLOWS` / `BUILTIN_SPELL_DAOWEN`（外部/AI 读）| **保留可读** | 改为从 SpellDefinition 常量表派生（不重复存储）|

### 1.7 最小重构方案（按 Phase 顺序给代码路径）

**Phase 1 最小切入点**：
1. 在 `engine/spell_dsl.py` 加顶层节点 `LoopStep(body, termination)`（termination 先允许 `True/False/Expr`，对应"无条件循环/条件循环"）；加 `TriggerImmediate` 常量；在 ActionStep 加 `params` 字段。
2. 新建 `engine/spell_execution.py`：
   - `SpellDefinition`（迁移/包装现有 Spell 类；或者给现有 Spell 加 body/trigger/lifecycle 字段）；
   - `SpellBinding`；
   - `SpellExecution`（含 step() 主循环）；
   - `StepResult`（completed/skipped/interrupted/failed + effects/cost/detail）。
3. 抽出内部 `_execute_single_daowen_step(holder, daowen, x, target, ...) -> StepResult`，把三个 resolver 里重复的 DaoWenEngine.resolve + spend_mana + dodge + apply_daowen_effect 逻辑收进去。
4. 把 `_resolve_spell_reactions` 改为构造 SpellExecution 驱动；另外两个 resolver 先暂时保留复制但内部调 `_execute_single_daowen_step`，Phase 1 结束时三个 resolver 的核心 step 已经统一。
5. ValueError 仅用于契约违例，法力/速度/目标失效改为 StepResult。

**Phase 2 切入点**：
- trigger=immediate：`_action_cast` 加 flow 参数；构造 lifecycle=instant/trigger=immediate 的 SpellDefinition，驱动 Execution，消耗 1 出手，返回 results。
- lifecycle 字段进 SpellDefinition；battle_end 遍历 Bindings 清 lifecycle=battle 的。

**Phase 3 切入点**：
- IfStep 不再在 prepare 期 flatten，而是在 SpellExecution 内真正作为节点存在；执行到 IfStep 用当前 runtime 状态 evaluate 后 push then/else body 到 control_stack；
- 分支冻结机制改为"同一次 reaction ctx 第一次命中时把条件结果存 ctx，后续命中间复用"——注意这是"事件级冻结"不是"法术级冻结"。

**Phase 4 切入点**：
- LoopStep 真正由执行器迭代（while True: 执行 body → 检查 termination → 检查中断条件）；
- 移除调用方 cycles 数组展开契约；提交改为 `{use: true, per_step_strategy: ...}` 或每步的 X 列表。

**Phase 5 切入点**：
- define_spell 默认 lifecycle=battle；spell_bindings 在 battle_end 清理；存档序列化排除 battle 绑定。

**Phase 6 切入点**：
- 新增 8 个压力测试（瞬发双步、动态 X、中途资源不足、分支真假、循环多轮、循环资源耗尽中断、循环重判条件、battle lifecycle 清理）+ 反应边界回归（test_reaction_nesting / test_spell_loop_mana_gain / test_shouyedeng_reaction_spells / test_after_life_lost_any_hp_loss / test_automatic_seal_spell 必须保持全绿）。

### 1.8 测试现状（Phase 0 基线）

```
非 AI 规则测试：1698 passed, 1 xfailed
AI/mock/sim 目录：5 failed
  - test_ai_basic_attack_candidate::test_at_one_by_one_daowen_still_wins
  - test_ai_tactics::test_ai_can_declare_parry_under_lethal_threat
  - test_win_only_ai::test_win_only_includes_parry_in_real_candidate_path
  - test_build_learner::test_valid_and_invalid_are_separated（sim 自学习器）
  - test_unified_ai::test_ai_player_is_the_combat_and_high_level_entrypoint（统一 AI 入口对招架文本）
  前三个是历史已知 AI 策略/评分问题，后两个 sim/AI 入口测试与法术架构无关。
```
关键法术相关测试（必须在 Phase 1-6 全程保持绿）：
  tests/test_spell_dsl.py                      — DSL 解析
  tests/test_spell_loop_mana_gain.py           — 循环（含产法力入预算、分支锁定）
  tests/test_reaction_nesting.py               — 1层嵌套
  tests/test_reaction_spell_save_roundtrip.py  — 存档回读
  tests/test_other_reaction_windows_non_attack.py — 非攻击路径触发
  tests/test_after_life_lost_any_hp_loss.py    — 任意失血触发
  tests/test_shouyedeng_reaction_spells.py     — 守夜灯/任意目标
  tests/test_automatic_seal_spell.py           — 镇魔印/自动触发
  tests/test_guard_command.py                  — define_spell in combat
  tests/test_learn_spell_prerequisite.py       — 道纹前置
  tests/test_open_trigger_extensibility.py     — 扩展时点
  tests/test_bizhong_daowen_paths.py           — 必中+法术交互
  tests/test_luanzang_target_daowens.py        — 乱葬岗目标
```

---

## 三、现有状态基线（动作层 + 反应嵌套结论，滚动保留）

> 本节为前序结论（动作层重构+聚能/蓄锐），保留为当前有效状态基线，不重复归档。

### 2.1 已落地的玩家回合动作

| action_type | 效果 | 代价 |
| --- | --- | --- |
| prepare_attack + resolve_attack | 普通攻击（两阶段）| 1 出手 |
| cast(kind="daowen"/"spell") | 合并入口：发动道纹 / 装配法术 | 1 出手 |
| focus（聚能）| 立即 `ceil(20%·法限)` 法力 | 1 出手 |
| rest（蓄锐）| 下回合 action_count +1（「蓄锐·增」1回buff）| 1 出手 |
| declare_parry | 招架姿态 | 0 出手 |

撤销了错误的「聚能 X 蓄能道纹→回始爆发超载」设计。反应法术链严格 1 层（管线分层契约 + `_resolving_life_lost_reactions` 计数器），tests/test_reaction_nesting.py 锁定。他人回合仍是闪避/不闪避/招架。

### 2.2 关键回归结果（本节为已通过基线，Phase 1-6 不得破坏）

- 非 AI 规则测试 1698 passed；
- focus 法限10回2法力；法限0回0+浪费出手提示；
- rest 后回始 action_count 2→3，「蓄锐·增」1回过期；
- cast(kind=daowen, 杀伐) 正常伤害；
- 道纹所有权校验 / 封印失效 / 任意目标 / 攻击者动态绑定 / 产法力入循环预算 / 分支冻结 / 自动反应 / 全局时点自动法术全部已验证。

---

## 四、Phase 0 验收核对

| 验收项 | 状态 |
|---|---|
| 当前法术架构图 | 见 §1.1 |
| 当前代码中各法术相关类/函数职责 | 见 §1.2 |
| 概念混用逐项列出 | 见 §1.3（9 处混用）|
| 新旧概念映射表 | 见 §1.5 |
| 推荐最小重构方案（按 Phase）| 见 §1.7 |
| 必须迁移的代码 | §1.5 右列 + 三套 resolver/validator 统一、_parsed_flow 从 Spell 移除、armed_spells 并入 Binding、SpellDefinition 加 trigger/lifecycle/body |
| 可保留的旧 API 兼容层 | §1.6（use_spell / define_spell / cast / use_daowen / 三个 prepare/validate/resolve 接口全部签名保留）|
| 完整测试结果 | §1.8（1704 passed，3 AI 失败非本轮引入）|

---

## 五、遗留待办（含 Phase 1-6 路线）

| 编号 | 内容 | 状态 |
|---|---|---|
| Phase 1 | 抽 SpellDefinition / SpellBinding / SpellExecution / StepResult；统一三套 resolver 核心；ValueError 退出正常流程控制 | 待开始（Phase 0 已冻结设计，待用户确认后动代码）|
| Phase 2 | trigger=immediate 瞬发 + lifecycle 三态；cast(flow=...) 入口；battle_end 清 battle 绑定 | 待 Phase 1 后 |
| Phase 3 | IfStep 改为执行期求值（控制流节点）；保留"同次反应多命中"的事件级分支冻结 | 待 Phase 2 后 |
| Phase 4 | LoopStep 由执行器迭代；取消调用方 cycles 手工展开；中断=interrupted 不回滚 | 待 Phase 3 后 |
| Phase 5 | define_spell 默认 lifecycle=battle；存档排除 battle 绑定；补 undefine_spell | 待 Phase 4 后 |
| Phase 6 | 8 个压力测试 + 反应边界全回归 | 待 Phase 5 后 |
| ④（副本）| 四阶副本【怠惰之罪·温柔乡】正文/怪物/事件/遗物名未定 | 挂待办（与法术重构正交）|
| 狂暴/兴奋/急速/坠落/避风铃/守夜灯/残骸/冥婚 | 上一轮列出的 8 项规则核对条目 | 等用户确认口径 |

---

## 六、未解决问题（Phase 0 阶段明确列出，不藏到"未来可扩展"）

1. **瞬发法术中 X 的提交形式**：反应法术的 X 由调用方在 cycles 数组里每步提交；瞬发法术的 X 是否在 cast 提交时一次性给所有步骤（类似旧 cycles 一份）？还是在每步执行时回调/询问？Phase 2 开始前需确认。倾向：cast 提交时必须给齐 steps（每个步骤的 x、target_ref、dodge），执行器按顺序执行，不给中途改 X 的机会——与"玩家在自己回合主动施法，一次性把所有决策做完"的回合制契约一致；条件触发法术的分支内 X 由 AI/自动决策器在触发时按 flat_steps 给齐。
2. **LoopStep 的 termination 语义**：DSL 里现在的"循环直到法力耗尽"只是标记；需要明确终止条件是"法力不足"硬判定，还是允许用户写"循环直到 自身法力小于2"这样的自定义终止 Expr。倾向：先支持"无条件循环（由执行器在资源不足/目标失效/道纹不可用时中断）"和"循环直到 <条件>"两种，条件语法复用现有条件表达式语法。
3. **自动反应路径在新 Execution 下的 X 选择策略**：当前 `_max_auto_life_lost_x` 按预算挑最大可支付 X。Phase 4 循环由执行器接管后，每轮循环 X 如何选？倾向：自动路径每轮都重新用 `_max_auto_life_lost_x`（以当前法力为预算）选 X，直到某轮选不出 X→interrupted。
4. **全局自动法术（镇魔印/自身回合结束）的 X 选择**：当前 automatic_global_trigger_choices 里硬编码 X=1 选第一个合法敌对怪物。新模型下仍可保持这个策略，但应写成自动 X/target 选择策略而非硬编码在 resolver 里。
5. **存档兼容**：老存档里 player.spells 是永久写入的自创法术（文本 trigger_condition/effect_flow）。Phase 5 改 lifecycle=battle 时，旧存档里已有的自创应当视为 permanent（避免老构筑突然丢失）还是 battle（符合新语义，战斗结束清）？倾向：从 Phase 5 版本起**新** define 的法术默认 lifecycle=battle，读老存档时按 spell 上有没有 lifecycle 字段推断——无字段=permanent（保持旧存档行为）；新加的 spell 都带显式 lifecycle。
6. **IfStep 嵌套深度**：当前 DSL 只支持单层分支，Phase 3 扩展为递归；建议工程上限=10 层（防止恶意输入）。
7. **反应"提交"在新模型下的形态**：目前 prepare→validate→resolve 三段式是为了让 AI/玩家在一个窗口里做决策。新 Execution 模型下，瞬发不需要 prepare/validate（直接 execute），反应法术仍需要 prepare 列出候选供 AI 选 use/不 use、选 X 和 target；但"循环 N 次"不再由提交方给出，因此提交结构会简化成 `{use: bool, steps_x_target_dodge: [...]}`（只给 1 轮的步骤决策），循环次数由执行器在每轮末尾检查终止条件决定。
