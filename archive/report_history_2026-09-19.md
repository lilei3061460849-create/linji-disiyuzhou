# 归档·报告历史 2026-09-19（引擎性能优化 + combat.py 拆分）

> 归档：2026-09-23 新一轮任务（正文内容调整 / 【第一杯】重做 / 巴别塔草案修订）出结论时，
> 原《报告.md》全文迁此留痕。后续只看根目录《报告.md》。
> 该轮结论的**逐条覆盖与复测**见 `audit/final_report_2026-09-20.md` 第 10 节
> （性能倍数、行为中性指纹、测试与护栏数字、范围边界、下一步建议现状），
> 本文保留原始记录，不再单独维护。

---

# 报告：引擎性能优化 + combat.py 拆分（2026-09-19）

> 本报告是**验收说明**：每一条结论都给出「怎么自己跑出来」。
> 分支 `arena/01a0ba24-linji-disiyuzhou`，基线 `adfd9d8`，提交链
> `fe738b8 → 84d166f → 967dbe4 → a2e2161 → 80d6cce`。
> 全部命令都在仓库根目录用 `.venv/bin/python` 执行。

---

## 〇、一句话结论

- **性能**：开局 53.7ms → 2.0ms，长局（2000 事件）单次预演 34.8ms → 0.67ms，
  AI 一次决策 774.7ms → 53.9ms（deepcopy 次数 50832 → 1902）。
- **行为**：性能改动**没有**改变对外可观测行为——这一点已从两条独立路径证明
  （第 三 节）。实盘轨迹里唯一的变化来自一处**顺带发现的预演副作用缺陷**，本次
  一并修掉（`80d6cce`），修好后「基线 + 该修复」与「优化树」的轨迹指纹**逐字节相同**。
- **测试**：`8 failed / 1602 passed / 3 xfailed`，8 项失败与基线**逐条相同**（既有
  失败，全是 AI/mock 类，与本轮无关）。

---

## 一、修改了什么

### 1.1 五次提交

| 提交 | 内容 | 主要文件 |
| --- | --- | --- |
| `fe738b8` | 预演不再重复建立事务快照：`execute_action` 拆成「事务层」+ `_execute_action_core`（唯一执行实现），预演在自带沙盒里直接调 core | `engine/api.py`、`engine/ai_preview.py` |
| `84d166f` | 静态规则只解析一次：`RuleRepository` 按输入正文内容摘要缓存事件表/怪物池（每次返回结构副本）；裁定库建表推迟到首次读写 | `engine/rule_repository.py`(新)、`engine/events.py`、`engine/dm_rulings.py`、`engine/api.py` |
| `967dbe4` | 沙盒按用途拆两种复制：战斗事件流是只追加的不可变事实源（容器复制、元素共享引用），预演用轻量随机源副本（不复制 roll 历史）；`AIPlayer.play_turn` 只在真正的高层决策前序列化状态 | `engine/sandbox.py`(新)、`engine/ai_preview.py`、`engine/ai_player.py` |
| `a2e2161` | 拆分 `engine/combat.py`：门面 6050 → 1532 行 + `engine/combat_parts/` 6 个 Mixin 分片（方法体逐字未改）；护栏与源码断言同步扩到分片 | `engine/combat.py`、`engine/combat_parts/*`、`engine/validator.py`、`tests/test_xijie_migration.py`、`tests/test_f7_doc_consistency.py` |
| `80d6cce` | 修复预演副作用：预演进出沙盒时保存/恢复 combat 运行态（详见 1.2）；新增实盘轨迹探针 | `engine/ai_preview.py`、`tests/test_action_preview_parity.py`、`sim/behavior_trace.py`(新) |

改动规模：`adfd9d8..HEAD` 共 21 个文件，+5600 / −4730 行（净增主要来自拆分后
的分片文件与新增测试/工具）。

### 1.2 顺带修掉的缺陷（本轮唯一的行为变化）

`ActionPreview` 的沙盒只换 `state` / `dice`，而 `combat` 仍是**真实对象**；结算会写
combat 自己的运行态字典：`_monster_activated`、`_monster_daowen_round_used`、
`_resonance_rewrites`、`_sanxiang_consumed`、`_split_clones_spawned`、
`_monster_evolved`、`_effect_chain_depth`。这些字典**按 `id(entity)` 建索引**，而沙盒
实体是深拷贝副本——写进去就是一批「老键」；沙盒随即被丢弃，其内存地址极可能被
下一次沙盒的副本实体复用，于是**下一次预演会「看见」上一次预演已经用过的道纹**
（每回合每道纹至多一次），候选集在预演里静默缩水。

- 旧口径（预演内部再建事务快照）：失败路径回滚、成功路径留下残渣；
- 优化后口径（预演直接调 core）：残渣全留。

两者残渣集合不同 ⇒ 实盘轨迹偏移（`罪孽都市 seed=3` 由 `cleared=1` 变 `0`）。
修复即「进出沙盒整体换回运行态」，符合本模块自己写明的契约：**预演零副作用**。
另加两条测试锁死：运行态零改动、真实实体之外不得出现任何「老键」。

---

## 二、性能变化

测量工具 `sim/perf_bench.py`（同机、同进程、固定种子，只测引擎实现开销）。
**跑基准前请确保机器空闲**，否则数字会虚高。

### 2.1 小局（默认场景）

```bash
# 基线
cd /tmp/linji-pre && .venv/bin/python sim/perf_bench.py \
    --rounds-init 20 --rounds-preview 100 --rounds-ai 60 --count-deepcopy \
    --json /tmp/bench_before_small.json --label before-small
# 优化后（仓库根）
.venv/bin/python sim/perf_bench.py \
    --rounds-init 20 --rounds-preview 100 --rounds-ai 60 --count-deepcopy \
    --json /tmp/bench_after_small.json --label after-small
```

| 项 | 基线 adfd9d8 | 优化后 HEAD | 倍数 |
| --- | --- | --- | --- |
| 开局 `GameEngine()` | 53.75 ms | **2.00 ms** | ×27 |
| 一次预演 `ActionPreview.preview` | 1.107 ms | **0.347 ms** | ×3.2 |
| 一次 AI 决策 `TacticalAI` | 27.67 ms | **12.13 ms** | ×2.3 |
| deepcopy / 预演 | 6.0 | **2.0** | ×3 |
| deepcopy / 决策 | 1232.3 | **302.3** | ×4 |

### 2.2 长局（预演前先堆 2000 条战斗事件）

```bash
cd /tmp/linji-pre && .venv/bin/python sim/perf_bench.py --events 2000 \
    --rounds-init 20 --rounds-preview 40 --rounds-ai 30 --count-deepcopy \
    --json /tmp/bench_before_2000.json --label before-2000
.venv/bin/python sim/perf_bench.py --events 2000 \
    --rounds-init 20 --rounds-preview 40 --rounds-ai 30 --count-deepcopy \
    --json /tmp/bench_after_2000.json --label after-2000
```

| 项 | 基线 adfd9d8 | 优化后 HEAD | 倍数 |
| --- | --- | --- | --- |
| 开局 | 56.09 ms | **2.16 ms** | ×26 |
| 一次预演 | 34.83 ms | **0.669 ms** | ×52 |
| 一次 AI 决策 | 774.71 ms | **53.94 ms** | ×14 |
| deepcopy / 预演 | 6.0 | **2.0** | ×3 |
| deepcopy / 决策 | 50832.3 | **1902.3** | ×27 |

**为什么长局收益远大于小局**：`combat_events` 只追加，长局里它占整份状态深拷贝的
95% 以上（4200 条事件时 `deepcopy(state)` 33.7ms，其中 32.7ms 是事件本身）；沙盒
改为「容器复制、元素共享引用」后，这份成本直接消失。

**各改动的贡献（未逐项拆分测量，标注为估算区间）**：开局 ≈ 90% 来自规则解析缓存
（`parse_events` 单次约 45ms）；预演 ≈ 一半来自不再重复建事务快照、一半来自事件流
共享引用；AI 决策 ≈ 预演收益 × 候选数（每个候选都要预演一次）。

### 2.3 未测量项

- 分片拆分（`a2e2161`）**未做性能测量**：结构拆分改变的是 `MRO` 解析层级，理论上
  只影响属性查找（可忽略量级），本次**未测量**，不声称收益。
- 并发/多进程场景**未测量**（`RuleRepository` 的缓存是进程内缓存，跨进程不共享）。
- 内存占用**未测量**（只测了 deepcopy 次数这一代理指标）。

---

## 三、正确性

### 3.1 全量测试

```bash
.venv/bin/python -m pytest tests -q --no-header
# → 8 failed, 1602 passed, 3 xfailed in 99.31s
```

8 项失败与基线**逐条相同**（`diff` 失败名单为空），均为既有失败（AI/mock 类）：

```
test_ai_basic_attack_candidate.py::test_at_one_by_one_daowen_still_wins
test_ai_tactics.py::test_ai_can_declare_parry_under_lethal_threat
test_build_learner.py::test_valid_and_invalid_are_separated
test_unified_ai.py::test_ai_player_is_the_combat_and_high_level_entrypoint
test_win_only_ai.py  ×4
```

（pass 数由 1601 变 1602，是因为本轮新增 1 条预演副作用测试。）

### 3.2 预演等价性与性能护栏

`tests/test_action_preview_parity.py`：同种子双引擎对照——「先预演再正式执行」与
「只正式执行」的返回与最终状态**逐字段相同**；预演零副作用（状态、行动历史、
combat 运行态、不得留下沙盒实体老键）；同时锁死**一次预演不得再建事务快照**
（deepcopy 次数护栏 ≤4，修复后仍为 2）。

```bash
.venv/bin/python -m pytest tests/test_action_preview_parity.py -q --no-header
# → 11 passed
```

### 3.3 实盘轨迹：证明性能改动行为中性

```bash
# 优化树（仓库根）
.venv/bin/python sim/behavior_trace.py
.venv/bin/python sim/behavior_trace.py --force-preview-transaction
.venv/bin/python sim/behavior_trace.py --patch-runtime-leak       # no-op 自检
# 基线树（worktree）
cd /tmp/linji-pre && .venv/bin/python /home/user/linji-disiyuzhou/sim/behavior_trace.py
cd /tmp/linji-pre && .venv/bin/python /home/user/linji-disiyuzhou/sim/behavior_trace.py --patch-runtime-leak
```

| 树 | 开关 | TRACE_SHA256 | 罪孽都市 seed=3 |
| --- | --- | --- | --- |
| `adfd9d8` 基线 | — | `98f2bfe2…` | cleared=**1** |
| `adfd9d8` 基线 | `--patch-runtime-leak` | `6aa26df8…` | cleared=0 |
| 优化树 HEAD | — | `6aa26df8…` | cleared=0 |
| 优化树 HEAD | `--force-preview-transaction` | `6aa26df8…` | cleared=0 |
| 优化树 HEAD | `--patch-runtime-leak`（应为 no-op） | `6aa26df8…` | cleared=0 |

**读法**：

1. 第 3 行 = 第 4 行 ⇒ 把预演内部强制回「再建一次事务快照」的旧口径，轨迹**一字不差**：
   该性能提交本身没有观测差异（这正是 `fe738b8` 想证明的事，现在被证明）。
2. 第 1 行 ≠ 第 2 行 ⇒ 基线在**同一条用例上**对「预演副作用」敏感。
3. 第 2 行 = 第 3 行 ⇒ 基线只补这一个修复补丁，就得到优化树的轨迹：
   **adfd9d8 → HEAD 的全部行为差额 = 这一处副作用修复**。
4. 第 5 行 = 第 3 行 ⇒ 修复在已修复的树上是 no-op（自检：修复完整，没有遗漏字段）。

> 所以本报告**不声称**「AI 行为与 adfd9d8 逐字节相同」。它声称的是更强、也更
> 精确的一句：**性能改动行为中性；与基线的差额全部来自一处已修复的预演副作用缺陷**，
> 该差额可以单独摘出来复现（上面第 2 行）。

### 3.4 迁移护栏

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from engine.validator import _MIGRATION_GUARD_PROTECTED_FILES, check_migrated_mechanism_guards
print(len(_MIGRATION_GUARD_PROTECTED_FILES), check_migrated_mechanism_guards())"
# → 10 []
```

护栏扫描范围由写死三元组改为自动 glob `engine/combat_parts/*.py`：拆分后**新分片会
自动纳入**，不会再出现「方法搬走、护栏静默失效」。10 个文件 = 门面 + 7 个分片 +
`combat_hooks.py` + `api.py`；故意植入一处违规可被扫出（1 条 violation）。

### 3.5 确定性与可复现性

- 轨迹探针同一棵树连跑两次结果相同（第 3.3 节命令可自查）。
- 基准脚本固定种子、固定初始状态，只反映实现开销。
- 已知的**非**确定性来源：`runtime_id`/`event_id`/`token` 等身份标识每次构造都变，
  跨树对比时已在探针里规范化剔除（`sim/behavior_trace.py::_norm`）。

---

## 四、哪些地方没有动（明确的范围边界）

| 未改动 | 说明 |
| --- | --- |
| 规则与数值 | 伤害/回复/代价/道纹结算公式**一行未改**；拆分是方法体原样搬家 |
| `engine/dice.py`、事件池语义 | 随机源与事件池语义未改；只是沙盒复制口径变了（元素共享引用） |
| `combat.py` 的对外契约 | `CombatEngine` 仍是唯一入口，`self.combat.X` / `CombatEngine.X` 经 MRO 解析不变，调用点一处未动 |
| `sim/` 的既有工具 | 连击/池化预计算/对局工具/`balance_sim` 内部/`ai_tactics` 评分权重均未改 |
| 既有 8 项失败 | 全部是既有的 AI/mock 类失败，本轮**未处理**（不在范围内） |
| 已知遗留 | `dm_rulings` 的 fts5 小瑕疵、Phase 4 池去重（仅审计工具口径）保持原样 |

---

## 五、验收清单（可直接复制执行）

```bash
cd /home/user/linji-disiyuzhou
export PY=.venv/bin/python          # 若 .venv 丢失：python3 -m venv .venv && \
                                    #   .venv/bin/python -m pip install -q -r requirements-dev.txt

# ① 全量测试：应为 8 failed / 1602 passed / 3 xfailed，失败名单与基线逐条相同
$PY -m pytest tests -q --no-header

# ② 预演契约（等价性 + 零副作用 + 不得重复建快照）：应为 11 passed
$PY -m pytest tests/test_action_preview_parity.py -q --no-header

# ③ 迁移护栏：应为 "10 []"
$PY -c "import sys;sys.path.insert(0,'.');from engine.validator import _MIGRATION_GUARD_PROTECTED_FILES as F, check_migrated_mechanism_guards as C;print(len(F), C())"

# ④ 实盘轨迹（约 10s）：应为 TRACE_SHA256 6aa26df840d0e80a165539135dade0e27ae3400b02d2e3dd7940bd3b6291af28
$PY sim/behavior_trace.py

# ⑤ 行为中性（约 10s）：指纹应与 ④ 完全相同
$PY sim/behavior_trace.py --force-preview-transaction

# ⑥ 性能（约 5s；机器空闲时跑）：对比 /tmp/bench_before_small.json
$PY sim/perf_bench.py --rounds-init 20 --rounds-preview 100 --rounds-ai 60 \
    --count-deepcopy --json /tmp/bench_after_small.json --label after-small
```

基线对照树若已不存在，重建方式：

```bash
git worktree add --detach /tmp/linji-pre adfd9d8
```

---

## 六、下一步建议

1. **把预演副作用纳入常驻护栏**：本轮只补了两条断言（运行态零改动 + 无老键）。建议
   在 `ActionPreview` 上加一个「沙盒写入白名单」自检（或把 combat 运行态也做成沙盒
   对象），从结构上杜绝「预演写真实引擎」这一类问题再犯。
2. **把三个种子扩成扫参**：`sim/behavior_trace.py` 目前 3 例（约 10s）。建议加
   `--cases N` 扫 50~200 个种子，作为发版前的**行为回归门禁**（现在只有单元测试，
   覆盖不到「AI 决策链在实盘上的漂移」）。
3. **逐项拆分性能收益**：本轮只给了整体前后对比。建议按提交逐个 checkout 打基准，
   把「规则缓存 / 事务快照 / 事件流共享 / play_turn 顺序」四项收益分别量化。
4. **`dm_rulings` fts5 瑕疵**与 **Phase 4 池去重**仍是既有遗留项，见 `audit/`。
5. 若后续要继续拆 `api.py`（约 5.7k 行），可直接沿用本轮的分片 + 护栏自动 glob 模式。

---

## 附：本报告涉及的文件

| 路径 | 作用 |
| --- | --- |
| `engine/rule_repository.py` | 规则解析缓存（内容摘要为键，返回结构副本） |
| `engine/sandbox.py` | 沙盒复制口径（事件流共享引用 / 轻量随机源） |
| `engine/combat_parts/` | `CombatEngine` 的 6 个 Mixin 分片（方法体原样） |
| `engine/ai_preview.py` | 预演器（含本轮副作用修复） |
| `sim/perf_bench.py` | 性能基准 CLI（第二节数字的产出工具） |
| `sim/behavior_trace.py` | 实盘轨迹指纹 + 两个归因开关（第三节数字的产出工具） |
| `tests/test_action_preview_parity.py` | 预演契约与性能护栏 |
