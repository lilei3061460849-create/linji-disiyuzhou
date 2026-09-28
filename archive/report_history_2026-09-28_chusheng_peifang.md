# 报告：待办处理（2026-09-28）

> 本文件是**当前有效状态**统一文档（口径见 `AI_EXPERIENCE.md#文档管理规则`）：
> 只记录最新一次任务的完整结论、规则变更与待办。上一轮（第一杯重做 + 巴别塔草案修订）
> 全文已并入 `archive/report_history_2026-09-23.md`。
> 分支 `arena/01a0e397-linji-disiyuzhou`，基线 `e4aad81`。命令在仓库根目录用 `python3` 执行。

---

## 〇、一句话结论

用户就待办面板给出四项裁定，本轮据此落地：
1. **①出怪配方** 按阶级递增：一阶上界保留旧「战斗场数-3」（变成随机上限而非确定值），二阶及以上上界=怪物池规模 12；旧固定波次 R4/R7/R10 废止，改为配方式随机增援（一波可多只）。
2. **②阶级推进+无尽模式** 进引擎：通过某阶最终死斗→解锁下一阶级；五阶死斗胜利→进入无尽模式（怪物池=全部副本、面板逐轮放大、精力 3→2→1 封底、【探索】禁用）。五阶副本「启示录」正文未接入运行时，引擎先落分支契约+持久化+门禁。
3. **③巴别塔** 用户裁定【公正天平】为独立新法器（不替换【不公天平】）、【否认】不调整已见/已杀总数；巴别塔在副本索引里仍是未实现草案，本轮只把裁定落实到正文，引擎不加结算路径。
4. **⑤员工背叛** 用户明确"保留机制"，待办关闭，引擎与正文不删。
5. **④怠惰之罪·温柔乡** 仍缺副本正文支撑，继续挂待办。

回归：非 AI 测试 **1685 通过、3 xfailed**；AI 目录 8 个失败与改动前一致（2026-09-23 基线就存在的 mock/AI 策略问题），本轮未引入新的失败。

---

## 一、改动清单（按文件分组）

### 1.1 出怪配方（待办①）

| 文件 | 改动 |
| --- | --- |
| `engine/monsters.py` | 重写事实源：删除旧 `compute_draw_count`（确定值公式），新增 `compute_draw_cap(N上界, tier)`、`roll_spawn_plan(dice,battle,tier)`、`scale_monster_def_for_endless(def,cycle)`、`merge_monster_pools(pools)`。配方常量 `MONSTER_POOL_LIMIT=12 / SPAWN_WAVE_GAP_LIMIT=5 / SPAWN_TIER1_BATTLE_BIAS=3`。 |
| `engine/api.py::_action_battle_start` | 从"全抽+第1只进场+其余无 schedule 入队"改为"先 `roll_spawn_plan`，S=首发，其余按 `queue_rounds` 给每只打上 `arrive_round` 入队"；返回值新增 `first_wave_count / spawn_plan / reinforcement_waves`，指令文案改为"首发S只已进场…R_r 进c只…"；赏金/强制/赤泉等额外怪物同样走无尽强度放大。 |
| `engine/combat.py::round_start` | 固定波次 `R4/R7/R10…回始各增援1只` 改为"每回合 pop 全部 `arrive_round ≤ current_round` 的条目"（一波可多只）。 |
| `engine/models.py::monster_reinforcements` 注释 | 更新为"配方式，arrive_round 字段必填"。 |
| `tests/test_monster_draw.py` | 旧断言 `1/1/1/1/2/3/4 == 确定值` / `len(all_names)==13` 改为"一阶上界序列"、"配方四条不变量（对 20 个种子都成立）"、"battle_start 按 spawn_plan 放首发+排队"、"有放回抽取"、"上界=1 不占随机流"。 |
| `tests/test_wave_spawn.py` | 旧断言 `seen_rounds==[4,7,10]` 改为"逐波按 spawn_plan 公布的回合进场"、"一波可多只"、"到点增援进场当回合可发动道纹（2026-09-15 白板废止）"、"增援未到齐战终门禁拦"、"B1 无增援与旧版逐位一致"、"二阶上界=12"。 |
| 文档 | `README.md` [战始]行；`AI_EXPERIENCE.md` 工程约束条目；`engine/README.md` battle_start 行。同步说明"旧公式确定值→新公式上界"。 |

配方四条不变量（锁定在 `test_spawn_recipe_invariants`）：
- `1 ≤ N ≤ cap`，`1 ≤ first_count ≤ total`；
- `Σ waves.count == total - first_count`（R_i 之和 = N-S）；
- 每波 `1 ≤ count`（R_i≥1），`1 ≤ arrive_round - prev ≤ 5`（T_i∈[1,5]）；
- `queue_rounds` 长度 = `total - first_count`，按波次展开。

### 1.2 阶级推进与无尽模式（待办②）

| 文件 | 改动 |
| --- | --- |
| `engine/models.py` | 新增字段 `unlocked_tier: int = MAX_TIER(5)`、`endless_mode: bool`、`endless_cycle: int`、常量 `MAX_TIER=5`、计算属性 `energy_budget`（常规 3、无尽 3→2→1 封底）；`to_dict()` 导出新字段。 |
| `engine/api.py` | 新增 `_read_seal_payload / _write_seal_payload / _progression_payload`，把跨轮回进度与封存槽同 JSON 文件存（`progression` 段）；空阶级槽不落盘，未推进过阶级不落 `progression` 段，保留"无进度痕迹→无文件"的旧语义。 |
|  | 新增 `_load_progression / _save_progression / _advance_region_unlock / _advance_endless_cycle / unlocked_regions / _monster_pool_for_battle / _current_tier`。 |
|  | `_action_battle_start` 出怪池在无尽模式下走 `merge_monster_pools`；怪物面板按 `scale_monster_def_for_endless(cycle)` 放大。 |
|  | `_action_battle_end` / 买路财的精力恢复走 `state.energy_budget`；战终第7场时无尽模式推进 `endless_cycle`；`_finalize_victory_seal` 胜利后调用 `_advance_region_unlock(duel_tier)`。 |
|  | `_replace_state_preserving_death_book_progress` 新轮回者保留 `unlocked_tier/endless_mode/endless_cycle`，无尽模式新轮回者开局精力按 `energy_budget` 算。 |
|  | `_pre_battle_tansuo` 无尽模式下拒绝探索并退还精力；`_get_pre_battle_actions` 无尽模式不出探索 action；`_get_setup_actions` 用 `unlocked_regions()` 限定可选副本。 |
| `engine/handlers/setup.py` | `setup_choose_region` 按阶级门禁拒绝：未实现→"尚未接入运行时"、阶级不够→"需通过 N 阶死斗解锁"；返回里带上 `unlocked_tier/endless_mode/endless_cycle` 与提示。 |
| `tests/test_progression_endless.py` | **新增** 10 个用例：`scale_monster_def_for_endless` 单测（cycle1=identity / cycle2=×1.25+3 / cycle4=×1.75）、`merge_monster_pools` 去重、`energy_budget` 递减封底、默认全阶级解锁兼容旧夹具、`unlocked_tier=1` 门禁、首封不落 progression 段、一阶胜→持久解锁二阶、MAX_TIER 胜→endless_mode 开启并持久、无尽模式选所有副本+禁探索+退精力+精力预算、无尽战始合并池并放大面板。 |

默认值兼容选择：跨轮回持久文件不存在/无 `progression` 段时 `unlocked_tier = MAX_TIER`（旧行为保留：开局可选二阶乱葬岗）；只有从持久文件显式读出 `unlocked_tier=1`（新轮回从头开荒）或通过 `_advance_region_unlock` 逐步推进时，门禁才生效。

### 1.3 巴别塔裁定（待办③）

- 用户裁定【公正天平】为独立新法器、【否认】不调整"已见/已杀总数"。
- 本轮**未**把巴别塔接入运行时（巴别塔仍是"未实现草案"，8 条声明类道纹尚未进入道纹注册表，引擎不做结算路径），只按裁定更新正文。

| 文件 | 改动 |
| --- | --- |
| `物品索引.md` | 在草案遗物区新增 **【公正天平】**：进入巴别塔时自动获得；任何人出手前必须声明，声明正确使目标回复10%血限，声明错误使目标-10%血限。【不公天平】保留为塔顶终音三选一，不改名（二者并存）。 |
| `副本/巴别塔.md` | 移除"否认是否调整已见/已杀计数 待裁定"注记，明确【否认】只从怪物池移除，不调面板公式的两项计数；新增"进入巴别塔时自动获得【公正天平】"一行。 |

### 1.4 员工背叛（待办⑤）

用户裁定**保留机制**：引擎、README 自动触发清单、`[战终]`检查、`suppress/appease/negotiate`、`tests/test_rebellion.py` 全部不动；待办⑤从报告里移除。

### 1.5 怠惰之罪·温柔乡（待办④）

本轮无副本正文/怪物池支撑（【？？的偏爱】免疫凡庸的遗物名未命名、温柔乡 12 只怪物/事件/专属道纹未写），继续挂待办。

### 1.6 测试夹具适配（非行为改动）

- `tests/test_optional_actions.py`：终音法器夹具从乱葬岗改成扭曲都市（二阶配方式出怪首发 S 随机，旧"单怪+无增援"前提不成立；本文件验的是可选法器本身，与副本阶级正交）。
- `tests/test_mana_one_pool.py`：同理从乱葬岗改到扭曲都市（一池制不依赖副本）。
- `tests/test_guard_command.py::_start_battle_with`：原来手工 `enemies.append(monster)` 再 battle_start 会被配方清空，改为 battle_start 后清空首发+增援、再塞指定单怪，保留"场上恰好就是这只怪"的单步断言语义。
- `tests/test_sealed_candidate_in_dungeon.py::test_sealed_candidate_dungeon_growth_applies`：battle_start 后清掉多余首发与增援，只留断言所需的一只目标怪。

---

## 二、遗留待办

| 编号 | 内容 | 状态 |
| --- | --- | --- |
| ④ | 四阶副本【怠惰之罪·温柔乡】：所有怪物攻击改为等量回复、自带【？？的偏爱】（"你免疫凡庸"的偏爱系遗物）。副本正文、12 只怪物面板、事件池、遗物名未定，引擎落地缺输入。 | 挂待办（本轮未改） |
| （②-后续） | 五阶副本【启示录】正文与怪物池接入；接入后 `test_fifth_tier_victory_enters_endless_mode` 可以从分支契约测升级到端到端走 7 场→死斗→终音→封印→无尽路径。 | 挂待办（本轮只落分支契约与门禁） |
| （③-后续） | 巴别塔声明机制（8 条声明类道纹：傲慢/真诚/藐视/独断/料敌/看破/攻心/读心，及专属行动【否认】）正式接入引擎；届时同步加入【公正天平】的战斗结算。 | 挂待办（本轮只改正文） |

---

## 三、回归结果

```
非 AI 测试：1685 passed, 3 xfailed
AI/mock 目录：8 failed（与 2026-09-23 基线相同，非本轮引入）
  - test_ai_basic_attack_candidate::test_at_one_by_one_daowen_still_wins
  - test_ai_tactics::test_ai_can_declare_parry_under_lethal_threat
  - test_build_learner::test_valid_and_invalid_are_separated
  - test_unified_ai::test_ai_player_is_the_combat_and_high_level_entrypoint
  - test_win_only_ai ×4
文档链接/锚点校验：tests/test_document_structure.py 通过。
```

### 事实源一览

| 口径 | 文件 | 符号 |
| --- | --- | --- |
| 一阶 N 上界 | `engine/monsters.py` | `compute_draw_cap(battle, tier)` |
| 出怪配方 | `engine/monsters.py` | `roll_spawn_plan(dice, battle, tier)` |
| 无尽面板放大 | `engine/monsters.py` | `scale_monster_def_for_endless(def, cycle)` |
| 无尽合并池 | `engine/monsters.py` | `merge_monster_pools(pools)` |
| 阶级推进 | `engine/api.py` | `_advance_region_unlock(won_tier)` |
| 无尽轮次推进 | `engine/api.py` | `_advance_endless_cycle()` |
| 可选副本 | `engine/api.py` | `unlocked_regions()` |
| 无尽精力预算 | `engine/models.py` | `GameState.energy_budget` |
| 战始出怪 | `engine/api.py` | `_action_battle_start`（读 spawn_plan） |
| 波次进场 | `engine/combat.py::round_start`（按 arrive_round pop） |
