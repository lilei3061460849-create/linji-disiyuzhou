# 8种未接线触发时机补全接线 —— 真实引擎实测报告

生成日期：2026-08-30
探针脚本：
- `sim/probe_global_trigger_spells.py`（战始/战终/回始/回终/敌回始/敌回终，共6+1个用例，含死斗对手视角验证）
- `sim/probe_damage_pipeline_triggers.py`（受到伤害后/失去生命前，共4个用例，含边界与兼容性验证）
- `sim/probe_custom_spell_triggers.py`（原第三/四项任务遗留，已更新为覆盖三个历史时机+全局时点DSL边界校验）
运行方式：
```
python3 sim/probe_global_trigger_spells.py
python3 sim/probe_damage_pipeline_triggers.py
python3 sim/probe_custom_spell_triggers.py
```
全量回归：`python3 -m pytest tests/ -q` → **1320 passed**（改动全程保持绿灯，未破坏任何既有测试）

---

## 一、结论

11种触发时机（受到伤害前/受到伤害后/失去生命前/失去生命后/目标发动道纹前/
战始/战终/回始/回终/敌回始/敌回终）**现已全部真实接线**，全部通过真实引擎
调用验证（不是纸面解析、不是"能学会但不会触发"）。原来README里"XX还没
装"的8种时机——战始/战终/回始/回终/敌回始/敌回终/受到伤害后/失去生命前——
逐一补全。补线方式：**完全复用引擎里现有的专门法术运行程序**（`SPELL_FLOWS`
注册表 + `_eligible_spell_flows` + `prepare/validate/resolve_spell_reactions`
反应型法术流水线），没有另建平行小程序。

## 二、原本法术是否有专门程序运行？—— 是，且本轮延用同一套

`engine/combat.py` 里的法术分发机制（`SPELL_FLOWS`→`_eligible_spell_flows`→
`prepare_spell_reactions`/`prepare_daowen_trigger_spells`→
`validate_spell_reaction_submission`→`_resolve_spell_reactions`/
`resolve_daowen_trigger_spells`）就是那套"专门程序"。自创法术走同一条管线
（`_parse_custom_spell`把文本解析成同构结构后混入同一个分发表）。

本轮新增两类机制，均为该套体系的**同构扩展**：
1. **全局时点法术**（战始/战终/回始/回终/敌回始/敌回终）：新增
   `prepare_global_trigger_spells`/`validate_global_trigger_spells`/
   `resolve_global_trigger_spells` 三个方法，与既有 `prepare_spell_
   reactions`/`validate_spell_reaction_submission`/`_resolve_spell_
   reactions` 同构，区别仅在于扫描对象从"单一受击者"改为"当前场上全体
   持有法术的实体"（因为这六个时点没有"攻击者/目标"这个天然对手）。
2. **伤害管线内两个新时点**（受到伤害后/失去生命前）：直接复用
   `_REACTION_SPELL_SLOTS` 元组扩展现有的 `prepare_spell_reactions`/
   `validate_spell_reaction_submission`/`_resolve_spell_reactions`，
   新增两个 key（`damage_after`/`life_before`），与历史的 `before`/
   `after` 走完全相同的代码路径，不新建函数。

## 三、DSL 层新增边界校验

全局时点没有"攻击者/目标"这个对手身份，`engine/spell_dsl.py` 新增
`_check_global_trigger_targets`：若自创法术在这六个时点上写"发动XX于
攻击者"或"发动XX于目标"、或条件表达式以"攻击者/目标"为主语，**学习阶段
直接报错拒绝**，不允许学会一个语义不成立的写法。

```
学习「战始」法术写"发动再生X于攻击者" → 拒绝：
"触发时机【战始】没有"攻击者/目标"这个对手身份（不像受到伤害前/失去生命
后那样天然存在一个触发对方），效果步骤【发动再生X于...】只能声明"于自
身""于施法者"或"于任意目标""
```

## 四、六个全局时点真实触发验证（`probe_global_trigger_spells.py`）

| 时机 | 学得wired | 真实触发 | 验证方式 |
|---|---|---|---|
| 战始 | True | True | 持有【折速法印】遗物获得法力后，法术真实回血（50→60） |
| 战终 | True | True | `spell_logs`里真实apply_daowen_effect执行记录（[战终]规则本身会清除局内回复，故看执行日志而非最终hp） |
| 回始 | True | True | 法术真实回血（50→60） |
| 回终 | True | True | `spell_logs`里真实执行记录（回终本身会清空法力/格挡，同上用执行日志判定） |
| 敌回始（普通战斗） | True | True | 结算于`prepare_monster_phase`，怪物尚未行动时法术已回血（50→59） |
| 敌回终（普通战斗） | True | True | 结算于`resolve_monster_phase`真正完成之后，`spell_logs`证明法术真实执行（净值可能因怪物伤害更高仍下降，不代表未触发） |
| 回始（死斗对手视角=敌回始映射） | True | True | 死斗对手（轮回者）通过完整封存携带的自创法术在`round_start`真实触发（50→59），证明扫描机制覆盖死斗对手而非玩家专属通道 |

映射设计（已与用户确认）：
- 战始/战终/回始/回终：新增可选`spell_choices`参数挂在对应action（battle_start/
  battle_end/round_start/round_end）的params_schema上，仿照`relic_choices`
  "存在候选就必须显式提交"的既有契约。
- 敌回始/敌回终（普通战斗）：分别映射到"即将进入怪物阶段前"
  （`prepare_monster_phase`）与"怪物阶段真正结算完毕后"（`resolve_monster_
  phase`）——怪物没有独立的"回合开始/结束"动作，这是最接近的真实执行点。
- 敌回始/敌回终（死斗）：死斗双方都是轮回者，各自有独立的round_start/
  round_end，故不走怪物阶段路径；"敌回始"即对方视角的round_start，"敌回终"
  即对方视角的round_end——由通用的持有者扫描机制（按`_combat_entity_refs()`
  遍历，不区分玩家侧/对手侧）自动覆盖，无需额外代码。

## 五、伤害管线内两个新时点真实触发验证（`probe_damage_pipeline_triggers.py`）

| 时机 | 学得wired | 真实触发 | 验证方式 |
|---|---|---|---|
| 受到伤害后 | True | True | 怪物普攻命中后，法术立即反打攻击者（敌方hp 222→210） |
| 受到伤害后（格挡全吸收边界） | True | True | 玩家格挡=999全额吸收伤害（actual_damage=0），但仍判定"受到了一次伤害"（damage>0）而触发（敌方hp 222→216） |
| 失去生命前 | True | True | 法术抢在扣血前对自身发动庇护，实际生命损失从预期的10点降到0点 |
| 兼容性（旧调用点只提交before/after） | N/A | True | 只提交历史两个key、不知道新key存在的旧调用点，在没有对应候选法术时依然正常结算，不因"未覆盖新key"而报错 |

关键设计点：
- **受到伤害后 vs 失去生命后的区别**：前者判据是`damage`（这一击最终确定
  的伤害数值，不受格挡是否吸收影响），后者判据是`actual_damage`（真实扣减
  的生命值）——即使格挡把伤害全部吸收，"受到了一次伤害"仍然成立，但"失去
  了生命"不成立。已用边界用例验证两者严格区分。
- **失去生命前 vs 受到伤害前的区别**：前者挂接点更靠后（伤害数值已确定、
  受到伤害前反应已结算完毕，但生命尚未真正扣减），后者更靠前（伤害数值
  刚计算出来，反应可能改变伤害本身，如借力打力）。
- **向后兼容**：`validate_spell_reaction_submission`对新增的`damage_after`/
  `life_before`两个key采用"存在候选才强制要求"的策略——若该时机确实没有
  可发动法术，即使调用方没有提交这两个key也不报错；一旦真的存在候选，仍
  必须显式覆盖，不允许静默跳过。这样保证了仓库里16+处历史调用点
  （`{"before": {...}, "after": {...}}`旧结构）无需改动即可继续工作，已用
  专门的兼容性探针验证。

## 六、涉及文件

- `engine/spell_dsl.py` —— 新增`GLOBAL_TRIGGERS`常量、`_check_global_trigger_targets`/
  `_check_condition_subjects_no_attacker`两个校验函数，在`parse_spell_definition`
  里调用。
- `engine/combat.py` —— `_WIRED_TRIGGERS`扩展到全部11种；新增
  `_global_trigger_holders`/`_resolve_global_entry_target`/
  `prepare_global_trigger_spells`/`validate_global_trigger_spells`/
  `resolve_global_trigger_spells`五个方法；`_REACTION_SPELL_SLOTS`元组
  统一四个反应型法术挂接点；`resolve_attack`里新增两处
  `_resolve_spell_reactions`调用（受到伤害后/失去生命前）；
  `validate_spell_reaction_submission`增加新key的兼容判断。
- `engine/api.py` —— `_action_battle_start`/`_action_battle_end`/
  `_action_round_start`/`_action_round_end`/`_action_prepare_monster_phase`/
  `_action_resolve_monster_phase`六个action新增全局法术结算调用；
  `_get_pre_battle_actions`/`_get_battle_start_actions`/`_get_battle_end_actions`
  三处params_schema新增候选提示；新增`_resolve_global_trigger_spells_for_action`
  公共辅助方法。
- `engine/ai_player.py` —— 两处旧的`spell_choices`构造字典补齐新增的
  `damage_after`/`life_before`两个key。
- `sim/probe_global_trigger_spells.py` —— 新建，6个全局时点+1个死斗对手
  视角验证。
- `sim/probe_damage_pipeline_triggers.py` —— 新建，受到伤害后/失去生命前
  4个用例（含边界+兼容性）。
- `sim/probe_custom_spell_triggers.py` —— 更新，`probe_unwired`重构为DSL
  边界校验（验证"于攻击者"写法在全局时点上被正确拒绝），main函数说明更新
  指向新的两个探针文件。
- `法术索引.md` —— 第五节"未接线"表格与第六节说明需要同步更新为"已全部
  接线"（本报告完成后处理）。
