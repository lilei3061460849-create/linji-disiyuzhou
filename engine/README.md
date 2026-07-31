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
2. **中断机制** — 程序无法判定时（急中生智/进化/逃跑等），抛出Interrupt等待DM
3. **先例数据库** — DM裁定存入SQLite，下次类似场景自动匹配
4. **自由控X** — 道纹的X值由AI在合法范围内自由指定
5. **诚实门禁** — 未实装的机制（事件/消耗品/部分遗物/部分道纹）在 API 层如实标记 `unavailable` 并拒绝结算，**绝不返回看似成功的空壳结果**

## 文件结构

```
engine/
├── __init__.py          # 包初始化
├── enums.py             # 枚举定义（阶段、触发时点、代价类型等）
├── models.py            # 数据模型（Entity, Spell, Relic, StatusEffect等）
├── gamedata.py          # 静态数据（怪物池×36、遗物池、法术库、出怪公式、实装白名单）
├── dice.py              # 随机数引擎（池系统，AI不自行生成随机数）
├── daowen.py            # 道纹系统（全部道纹效果的数学公式）
├── combat.py            # 战斗结算（伤害矩阵、闪避、承伤链、回合钩子）
├── dm_rulings.py        # DM裁定数据库（SQLite + 全文搜索）
├── rule_sync.py         # 规则文件（README）反向提取与同步核对
├── validator.py         # 规则校验器（20条内置合规检查）
├── ai_player.py         # AI玩家控制器与免费LLM后端
└── api.py               # GameEngine主类 — AI的唯一交互入口

根目录补充：
├── simulate.py          # 真实压力模拟器（所有数字可用种子复现）
└── tests/test_engine.py # 真实结算测试（公开行动接口驱动）
```

## AI交互流程

### 1. 获取状态
```python
state = engine.get_state()
# 返回：当前状态、可用行动（含 unavailable 标记）、待处理中断、待解决随机请求
```

### 2. 执行行动
```python
result = engine.execute_action("use_daowen", {
    "daowen_name": "杀伐",
    "x": 5,
    "target": "千手蜈蚣"
})
```

### 3. 随机数规则
```python
# 战始抽怪、遗物发现等随机流程：
# 引擎返回 range（如1~12）→ 玩家给出数字 → 引擎结算并继续
result = engine.execute_action("battle_start", {})
# result["range"] == "1~12", spawn_count 只怪
result = engine.execute_action("random_number", {"pool_name": "spawn_battle_1", "number": 7})
```

### 4. 处理中断
```python
if result.get("interrupt"):
    engine.submit_ruling(
        interrupt_type="急中生智",
        ruling_text="利用蒸汽遮蔽视线",
        ruling_data={"effect": "怪物下回合无法选中目标"}
    )
```

### 5. 查询先例
```python
precedent = engine.check_precedent("急中生智", {"target": "千手蜈蚣"})
```

## 行动类型一览

| 行动类型 | 状态 | 说明 |
|---------|------|------|
| `setup_attributes` | ✅ | 分配初始25属性点 |
| `setup_choose_daowen` | ✅ | 选择初始道纹（杀伐/锐利） |
| `setup_choose_resonance` | ✅ | 选择初始残韵 |
| `setup_choose_region` | ✅ | 选择副本 |
| `discover_relic_setup` | ✅ | 遗物发现（抽3候选，自选1件） |
| `pre_battle_action` | ✅ | 局外行动：领悟/休整/修行/学习/共鸣/忘忧(遗物忘忧香)均真实生效；**探索/维修/雇佣/炼心如实返回 unavailable** |
| `spend_attribute_points` | ✅ | 属性点分配（1点=1速限 或 1点=2法限） |
| `battle_start` | ✅ | 战始：真实出怪（场数-2公式）+随机数抽怪 |
| `use_daowen` | ✅ | 发动道纹（白名单内58种机制真实生效） |
| `use_spell` | ✅ | 发动法术（9种，积木/循环/中断法则） |
| `use_resonance` | ✅ | 残韵变化（闭环路径校验，永久/临时两种规则） |
| `attack` | ✅ | 普通攻击（目标可闪避） |
| `dodge_decision` | ✅ | 闪避决策（消耗1速） |
| `monster_turn` | ✅ | 怪物出手轮（出手数=回合÷3向上取整，面板道纹主动发动） |
| `retreat` | ✅ | 买路财撤退（20%血限等价碎片） |
| `declare_wit` / `declare_escape` / `declare_evolution` | ✅ | 急中生智/逃跑/进化（中断→DM） |
| `round_start` / `round_end` | ✅ | 回始/回终（法力、格挡、持续效果、衰败/清算/逼债/活血等钩子） |
| `battle_end` | ✅ | 战终（碎片、清理、冷却推进、叛变检查、冠冕触发） |
| `consume_item` | ❌ 已下线 | 消耗品获取依赖未实装的事件系统，如实移除 |
| `random_number` | ✅ | 提交随机数 |

## 运行

```bash
python main.py              # 交互式命令行
python main.py --api        # API演示
python tests/test_engine.py # 12项真实结算测试
python simulate.py --runs 200  # 真实压力测试（可复现数据见 AI_EXPERIENCE.md）
```
