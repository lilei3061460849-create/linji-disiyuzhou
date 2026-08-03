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

## 文件结构

```
engine/
├── __init__.py          # 包初始化
├── enums.py             # 枚举定义（阶段、触发时点、代价类型等）
├── models.py            # 数据模型（Entity, Spell, Relic等）
├── dice.py              # 随机数引擎（池系统，AI不自行生成随机数）
├── daowen.py            # 道纹系统（所有道纹效果的数学计算）
├── spells.py            # 法术库与法术执行器（积木/循环/中断/阶级规则）
├── content.py           # 内容库（遗物池、怪物池、通用事件池、消耗品）
├── combat.py            # 战斗计算引擎（伤害、回合、闪避等）
├── battle_flow.py       # 完整战斗流程（怪物出手、回合管理）
├── validator.py         # 规则校验器（违规检测与落库）
├── rule_sync.py         # 正文规则热同步
├── ai_player.py         # AI后端接入层
├── dm_rulings.py        # DM裁定数据库（SQLite + 全文搜索）
└── api.py               # GameEngine主类 — AI的唯一交互入口
```

## AI交互流程

### 1. 获取状态
```python
state = engine.get_state()
# 返回：当前状态、可用行动、待处理中断、上次结果
```

### 2. 执行行动
```python
result = engine.execute_action("use_daowen", {
    "daowen_name": "杀伐",
    "x": 5,
    "target": "千手蜈蚣"
})
```

### 3. 处理中断
```python
# 如果result中有interrupt，需要DM裁定
if result.get("interrupt"):
    # 提交DM裁定
    engine.submit_ruling(
        interrupt_type="急中生智",
        ruling_text="利用蒸汽遮蔽视线",
        ruling_data={"effect": "怪物下回合无法选中目标"}
    )
```

### 4. 查询先例
```python
# AI可以在触发特殊事件前查询是否有先例
precedent = engine.check_precedent("急中生智", {"target": "千手蜈蚣"})
if precedent["found"]:
    # 直接应用先例
    ...
```

### 5. 随机数
```python
# 需要随机时，引擎返回池范围
pool = engine.request_random("event_pool", ["事件A", "事件B", "事件C"])
# pool.range = "1~3"
# AI必须向玩家索取数字，然后提交
result = engine.execute_action("random_number", {"pool_name": "event_pool", "number": 2})
```

## 行动类型一览

| 行动类型 | 说明 |
|---------|------|
| `setup_attributes` | 分配初始25属性点（参数：blood_points/speed_points/mana_points，总和须为25） |
| `setup_choose_daowen` | 选择初始道纹（杀伐/锐利） |
| `setup_choose_resonance` | 选择初始残韵 |
| `setup_choose_region` | 选择副本 |
| `pre_battle_action` | 局外行动（领悟/休整/修行/学习/共鸣/探索） |
| `use_daowen` | 发动道纹（自动结算冷却/唯一/异变等代价） |
| `use_spell` | 发动法术（参数：spell_name + variables，如 `{"X":2,"Y":3}`） |
| `use_resonance` | 使用残韵 |
| `attack` | 普通攻击 |
| `dodge_decision` | 闪避决策 |
| `consume_item` | 使用消耗品（不消耗出手；参数：item_name） |
| `declare_wit` | 声明急中生智 |
| `declare_escape` | 声明逃跑 |
| `declare_evolution` | 怪物进化 |
| `round_start` | 回始结算 |
| `round_end` | 回终结算 |
| `battle_start` | 战始（自动计算出怪数量并开启抽怪随机池） |
| `battle_end` | 战终 |
| `random_number` | 提交随机数 |

## 运行

```bash
# 交互式命令行
python main.py

# API演示
python main.py --api

# 运行测试
python tests/test_engine.py
```
