# 千人千面·长期分化验证报告(2026-08-26)

**验收结论:B**(A=明显千人千面 B=有分化仍趋同 C=局部差异整体公式化 D=基本无分化)

- 角色:100(有效 100),决策总数 3535,耗时 101.6s
- 验收检查项:{"A_entropy": false, "A_max_share": false, "A_trait_dims": true, "A_behavior_dist": true, "A_closure": true, "B_entropy_or_share": true, "B_trait_dims": true}

## 一、最终人格分布(九维)

| 维度 | 成型数 | 均值 | 标准差 | 最小 | 最大 | 直方图 |
|---|---|---|---|---|---|---|
| risk_preference | 32 | -0.6747 | 0.2994 | -0.9995 | 0.35 | [-1,-0.6):21 [-0.6,-0.2):9 [-0.2,0.2]:1 (0.2,0.6]:1 (0.6,1]:0 |
| interpersonal_tendency | 0 | - | - | - | - | 无角色形成该维度 |
| moral_baseline | 0 | - | - | - | - | 无角色形成该维度 |
| resource_view | 18 | 0.3606 | 0.4348 | -0.7254 | 0.884 | [-1,-0.6):1 [-0.6,-0.2):2 [-0.2,0.2]:1 (0.2,0.6]:9 (0.6,1]:5 |
| exploration_desire | 58 | -0.4386 | 0.4732 | -0.9968 | 0.7254 | [-1,-0.6):24 [-0.6,-0.2):18 [-0.2,0.2]:12 (0.2,0.6]:0 (0.6,1]:4 |
| emotional_stability | 2 | 0.7254 | 0.0 | 0.7254 | 0.7254 | [-1,-0.6):0 [-0.6,-0.2):0 [-0.2,0.2]:0 (0.2,0.6]:0 (0.6,1]:2 |
| decision_habit | 23 | -0.2554 | 0.7238 | -0.9943 | 0.9246 | [-1,-0.6):13 [-0.6,-0.2):0 [-0.2,0.2]:0 (0.2,0.6]:7 (0.6,1]:3 |
| expression_style | 57 | -0.0919 | 0.7595 | -0.9995 | 0.9943 | [-1,-0.6):26 [-0.6,-0.2):6 [-0.2,0.2]:1 (0.2,0.6]:4 (0.6,1]:20 |
| reaction_pattern | 0 | - | - | - | - | 无角色形成该维度 |

## 二、行为分布与角色间距离

- 平均行为距离:5.2261(最大 16.6857,4950 对)
- 首手行动分布:{"杀伐": 28, "再生": 15, "庇护": 15, "封印": 8, "束缚": 7, "贯穿": 6, "固执": 5, "增殖": 5, "透支": 4, "残韵【反转】必中 → 蒙蔽": 2, "残韵【反转】超频 → 坏死": 2, "残韵【反转】强化 → 弱化": 1, "残韵【反转】定型 → 畸变": 1, "残韵【反转】减速 → 加速": 1}

## 三、同局面决策分布(相同局面,保留各自性格+经历)

- 不同行动序列数:3,最大群体占比 65%,归一熵 0.1917
- 分布:{"再生 > 杀伐 > 杀伐": 65, "再生 > 庇护 > 杀伐": 21, "杀伐 > 杀伐 > 杀伐": 14}
- 无性格基线(对照):最大占比 100%,熵 -0.0

## 四、长期稳定性(高压窗口防御率,按风险人格分桶)

{
 "求稳(risk<-0.3)": {
  "n": 30,
  "avg_high_pressure_defense_rate": 0.5323,
  "all_defensive": false,
  "all_offensive": false
 },
 "中间": {
  "n": 1,
  "avg_high_pressure_defense_rate": 0.1333,
  "all_defensive": false,
  "all_offensive": false
 },
 "冒险(risk>0.3)": {
  "n": 1,
  "avg_high_pressure_defense_rate": 0.0,
  "all_defensive": false,
  "all_offensive": true
 },
 "_monotonic_defense_by_risk": true,
 "_no_absolute_lock": false
}

## 五、行为→性格→行为闭环

{
 "lagged_risk_vs_next_battle_risky_rate": null,
 "lag_pairs": 43,
 "same_situation_shift_rate_vs_no_personality": 0.35,
 "note": "shift_rate>0 且滞后相关同号 → 行为塑造性格、性格改变后续行为"
}

## 六、新公式化源扫描

- 从未形成证据的维度:['interpersonal_tendency', 'moral_baseline', 'reaction_pattern']
- 无性格基线同局面:{"再生 > 杀伐 > 杀伐": 100}

## 七、趋同归因(如未达 A)

- 人格 std<0.10 的维度:interpersonal_tendency、moral_baseline、emotional_stability、reaction_pattern —— 证据源稀疏或观察规则触发率低
- 从未形成证据的维度:interpersonal_tendency、moral_baseline、reaction_pattern —— 单人副本无社交/牺牲场景,环境限制

## 附:行为→性格证据规则表

**risk_preference**
- 自伤≥3 或 血限损失>0 的行动 → +1(承担风险)
- 威胁≥50%血限的窗口中选择防御/回复(无损) → -1(选择安全)
**exploration_desire**
- 首次使用某道纹 → +1(探索新手段)
- 同一场内第4次起重复同一张已用≥3次的牌 → -1(路径依赖)
**resource_view**
- 单手消耗≥70%法限 或 ≥5碎片 → -1(大额支出)
- 法力<30%时选择 X≤2 的低费行动 → +1(紧缩用度)
**decision_habit**
- 高压窗口首手选择 X≤2 试探性输出 → +1(先观察)
- 高压窗口满预算(≥80%预算)输出 → -1(果断全押)
**emotional_stability**
- 血<35%仍执行自伤行动 → -1(低血不稳)
- 血<35%选择防御/回复 → +1(低血自稳)
**expression_style**
- 直接输出(敌方掉血) → +1(直球)
- 战术/增益牌(敌方无损、自身无损) → -1(迂回)
**reaction_pattern**
- 威胁较上回合跳升≥50%后的首手为无损防御 → +1(从容应对)
- 威胁跳升≥50%后的首手为自伤强攻 → -1(应激硬拼)
**interpersonal_tendency**
- 接纳救赎朋友 → +1(信任陌生人);本实验单人无友军互动,证据稀疏属环境限制
**moral_baseline**
- 喂养/利用敌方道纹(残韵反转强化自身) → -1(功利);无友军牺牲场景,证据稀疏属环境限制
