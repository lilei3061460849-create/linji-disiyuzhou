"""
事件池数据（README「通用事件池」579-627行、扭曲都市专属事件663-700行、
罪孽都市专属事件749-782行、龙心谷专属事件824-851行，逐字忠实转录）。

数据结构：
- id / name / desc：场景描述（原文）
- condition：事件入池条件（README 233行：条件不满足的事件不进入当前池）
    "has_glimmer_friend"：拥有微光者队友（手术：使一名微光者队友……）
    "daowen_ge_5"：拥有五种道纹及以上（尖叫下水道）
- options：选项列表（原文每个选项一条）
    label / text：原文
    cost：代价 {"shards"碎片, "hp"流血, "aging"衰老, "exhaustion"枯竭,
                "fatigue"疲惫, "energy"精力, "amnesia"失忆(需 forget_names),
                "relic_destroy"销毁一件当前遗物(需 relic_name)}
    effect：收益 {"shards", "relic_random"随机发现遗物, "relic_choose"自选发现遗物,
                  "event_relic"事件遗物(不入遗物池), "consumable"消耗品(+qty),
                  "learn_spells"学会N种法术(需 spell_names), "resonance"残韵(类型或choose),
                  "attr"属性点dict, "blood_limit", "friend"朋友模板, "heal_pool"恢复量,
                  "next_battle"下一场战斗修饰dict, "debt"高利贷债务, "gamble"赌局dict,
                  "wrong_lastword"错误遗言(需 text), "remove_lastword"清除遗言,
                  "no_more_memory"无法再获前世记忆, "read_memory"读前世记忆,
                  "letter"寄信(需 text≤40字), "implant_daowen"为朋友植入随机怪物转化道纹,
                  "lose_friend_for_shards"失去朋友换其血限50%碎片(需 friend_name),
                  "monster_next_battle"下一场作为怪物额外出现, "shield_friend"防弹插板(需 friend_name),
                  "info_next_battle"下场怪物情报,
                  "active_soil"活性土壤(diy朋友)
    refuse=True：拒绝类选项（README 300行：无所求——选择拒绝类选项永久+1属性点）
    needs_dm：需DM创造性裁定的选项（程序如实拒绝，绝不假装成功）
    unavailable：依赖未实装体系的选项（程序如实拒绝）

【待DM裁定假设】“捂住耳朵/无视/绕桥而行/避开井口/离开/视而不见/目送其远去”等
  离场且无代价无收益的选项按“拒绝类”处理，可触发无所求。
"""

EVENT_POOL_UNIVERSAL = [
    {
        "id": "无名冢", "source": "universal",
        "desc": "一片插满残破兵器的荒地，每一把兵器下都埋葬着一个失败的轮回者。风吹过，兵器发出呜咽，似乎在诉说着它们生前的荣光与诅咒。",
        "options": [
            {"label": "拔出兵器", "text": "失去10[碎片]，随机获得一件遗物",
             "cost": {"shards": 10}, "effect": {"relic_random": 1}},
            {"label": "为你而战", "text": "销毁一件当前遗物（包括遗物池中），设计一种新的遗物加入遗物池，获得15[碎片]。",
             "needs_dm": "设计一种新遗物属创造性机制（README复杂度原则），程序抛中断交DM裁定"},
            {"label": "拒绝", "text": "无事发生。", "refuse": True},
        ],
    },
    {
        "id": "遗忘书屋", "source": "universal",
        "desc": "在一处坍塌的街角，一间散发着霉味与干涸血迹的旧书店静静伫立。无光的柜台后，一个没有五官的店员指了指桌上三本由皮革缝制的古籍。",
        "options": [
            {"label": "阅读《战争残卷》", "text": "流血15，选择学会两种法术。",
             "cost": {"hp": 15}, "effect": {"learn_spells": 2}},
            {"label": "阅读《禁忌法典》", "text": "失忆1，自选一件遗物与20[碎片]。",
             "cost": {"amnesia": 1}, "effect": {"relic_choose": 1, "shards": 20}},
            {"label": "阅读《自我剖析》", "text": "枯竭1，自选残韵×1",
             "cost": {"exhaustion": 1}, "effect": {"resonance": "choose"}},
            {"label": "拒绝", "text": "无事发生", "refuse": True},
        ],
    },
    {
        "id": "祭坛", "source": "universal",
        "desc": "你来到了一座扭曲的黑石祭坛前，祭坛中央有一柄干瘪的石刃，冰冷的声音在你脑海中响起：“以你之重，换吾之轻。”",
        "options": [
            {"label": "献祭血肉", "text": "衰老8，获得1点[速限]",
             "cost": {"aging": 8}, "effect": {"attr": {"速限": 1}}},
            {"label": "献祭神智", "text": "枯竭5，获得2点[速限]",
             "cost": {"exhaustion": 5}, "effect": {"attr": {"速限": 2}}},
            {"label": "拒绝", "text": "无事发生", "refuse": True},
        ],
    },
    {
        "id": "过路商人", "source": "universal",
        "desc": "一个拖着骡车的独眼商人在岔路口支起了摊子，车上的货物用破布盖着，隐约能看到金属反光和跳动的微光。",
        "options": [
            {"label": "限制选择权", "text": "失去 8 [碎片]，限制下一场战斗怪物的选择权（例如：使怪物陷入困境时只能选择【逃跑】或者使其每回合最多只能发动一次道纹）。",
             "cost": {"shards": 8},
             "needs_dm": "限制怪物选择权的具体形式是开放式裁定（“例如”非封闭枚举），程序抛中断交DM裁定"},
            {"label": "以物易物", "text": "失去一件当前遗物，随机获得一件新遗物",
             "cost": {"relic_destroy": True}, "effect": {"relic_random": 1}},
            {"label": "拒绝", "text": "无事发生", "refuse": True},
        ],
    },
    {
        "id": "猩红暴雨", "source": "universal",
        "desc": "粘稠如血的暴雨从铅灰色天空倾泻而下，地面的积水散发着令人作呕的铁锈味。带有腐蚀性的雨水拍打在你的皮肤上，发出滋滋的声响。",
        "options": [
            {"label": "硬扛", "text": "流血20", "cost": {"hp": 20}},
            {"label": "用法力撑起屏障", "text": "枯竭3", "cost": {"exhaustion": 3}},
            {"label": "躲入漏雨的废墟", "text": "失去1次精力", "cost": {"energy": 1}},
        ],
    },
    {
        "id": "无名碑林", "source": "universal",
        "desc": "在一片长满青苔的石碑群中，碑上刻满了扭曲的符文，记录着无数死者的执念。当你靠近时，石碑产生了共鸣。",
        "options": [
            {"label": "触摸", "text": "流血15。获得【残韵：曲解×1】与15[碎片]。",
             "cost": {"hp": 15}, "effect": {"resonance": "曲解", "shards": 15}},
            {"label": "拒绝", "text": "无事发生", "refuse": True},
        ],
    },
    {
        "id": "回音长廊", "source": "universal",
        "desc": "一条两侧挂满破碎镜子的长廊，镜子里映出的是你前几次轮回中惨死的画面。走廊尽头放着一个生锈的八音盒。",
        "options": [
            {"label": "聆听安魂曲", "text": "在《死者之书》中留下一条错误遗言，获得10[碎片]。",
             "effect": {"shards": 10, "wrong_lastword": True}},
            {"label": "打碎镜子", "text": "流血5，清除《死者之书》中一条遗言",
             "cost": {"hp": 5}, "effect": {"remove_lastword": True}},
            {"label": "捂住耳朵", "text": "无事发生。", "refuse": True},
        ],
    },
    {
        "id": "回忆当铺", "source": "universal",
        "desc": "一家没有门的当铺，柜台后坐着一团由无数眼球和脑髓组成的阴影。它用沙哑的声音说：“过去的碎片，在这里能称出重量。你要典当，还是赎回？”",
        "options": [
            {"label": "典当", "text": "获得10[碎片]，本次轮回无法再获得前世记忆",
             "effect": {"shards": 10, "no_more_memory": True}},
            {"label": "赎回", "text": "消耗10[碎片]，获得一段前世记忆",
             "cost": {"shards": 10}, "effect": {"read_memory": True}},
            {"label": "拒绝", "text": "无事发生。", "refuse": True},
        ],
    },
    {
        "id": "手术", "source": "universal",
        "desc": "一个废弃的地下手术室，旁边的培养皿里有着一颗跳动的、长满触手的心脏正散发着微光。",
        "condition": "has_glimmer_friend",
        "options": [
            {"label": "强制移植", "text": "使一名微光者队友被植入一种随机的怪物道纹（该道纹每场战斗三回合后若保持原样，则仍变为怪物）",
             "effect": {"implant_daowen": True}},
            {"label": "抽取灵魂", "text": "失去一名微光者队友，获得其[血限]50%的[碎片]",
             "effect": {"lose_friend_for_shards": True}},
            {"label": "拒绝", "text": "无事发生", "refuse": True},
        ],
    },
    {
        "id": "无魂泥潭", "source": "universal",
        "desc": "一处散发着胶皮烧焦气味的沥青深坑。黑色的淤泥在缓缓蠕动，这种物质能彻底隔绝灵魂的波动，但采集它需要付出肉体被腐蚀的代价。",
        "options": [
            {"label": "采集“绝息淤泥”", "text": "流血10。获得“绝息淤泥”（可随时使用，使用后屏蔽自身灵魂位置，可使本次[战终]，立刻逃脱）。",
             "cost": {"hp": 10}, "effect": {"consumable": "绝息淤泥"}},
            {"label": "拒绝", "text": "无事发生。", "refuse": True},
        ],
    },
]

EVENT_POOL_REGION = {
    "扭曲都市": [
        {
            "id": "医生", "source": "扭曲都市",
            "desc": "一个神色癫狂、浑身长满肉瘤的微光者拦住了你，他挥舞着生锈的手术刀，神经质地尖叫：“我可以帮你‘改良’身体！只需要一点点小小的材料……”",
            "options": [
                {"label": "接受改造", "text": "流血12，[血限]+6",
                 "cost": {"hp": 12}, "effect": {"blood_limit": 6}},
                {"label": "拒绝改造", "text": "流血6，获得8[碎片]",
                 "cost": {"hp": 6}, "effect": {"shards": 8}},
                {"label": "雇佣医生", "text": "失去10[碎片]，获得“医生”（1×1/50，可消耗5[碎片]为其提升1攻击次数/2攻击力）",
                 "cost": {"shards": 10},
                 "unavailable": "员工体系（工资/叛变/强化）未实装，获得员工后无法真实结算，程序拒绝"},
            ],
        },
        {
            "id": "乞丐", "source": "扭曲都市",
            "desc": "一个蜷缩在街角发抖的乞丐向你伸出干枯的手。他的眼中闪烁着微弱的光，但他的身体已经开始出现向怪物变异的异化特征。",
            "options": [
                {"label": "给予庇护", "text": "流血10，获得乞丐（2×3/50，狂暴2（异变3））",
                 "cost": {"hp": 10}, "effect": {"friend": "乞丐"}},
                {"label": "施舍碎片", "text": "失去5[碎片]，获得一件随机遗物。",
                 "cost": {"shards": 5}, "effect": {"relic_random": 1}},
                {"label": "冷酷终结", "text": "获得8[碎片]。", "effect": {"shards": 8}},
                {"label": "无视", "text": "无事发生", "refuse": True},
            ],
        },
        {
            "id": "血肉温室", "source": "扭曲都市",
            "desc": "废墟中伫立着一座巨大的玻璃温室，内部长满了如心脏般搏动的猩红植物。温室中央悬挂着几颗成熟的果实，散发着诱人的甜香。",
            "options": [
                {"label": "采摘“遗物·猩红果实”", "text": "获得遗物·猩红果实（每场[战始]可选择是否流血10；若选择，则[战终][血限]+2）",
                 "effect": {"event_relic": "猩红果实"}},
                {"label": "采摘“遗物·苍白之花”", "text": "获得遗物·苍白之花（每场[战始]可选择是否疲惫5；若选择，则[战终]精力+1）",
                 "effect": {"event_relic": "苍白之花"}},
                {"label": "收集活性养分", "text": "获得【活性土壤】（消耗品（耐久1）：[战始]可失去X点法力，声明培育生命时可用X点基础预算打造一名[朋友]（名字A×B/C，自定义非重复特性）遵守进化特性，由玩家设计，DM确认）。",
                 "effect": {"consumable": "活性土壤"}},
                {"label": "拒绝", "text": "无事发生", "refuse": True},
            ],
        },
        {
            "id": "绝望来电", "source": "扭曲都市",
            "desc": "在街角一处被猩红真菌覆盖的铸铁公用电话亭里，已经断线多年的话筒正发出刺耳的铃声。话筒中传出一个冰冷、无感情但似乎熟知你过去的低语。",
            "options": [
                {"label": "接听", "text": "流血10，得到一个问题的答案",
                 "cost": {"hp": 10},
                 "needs_dm": "“一个问题的答案”是开放式叙事裁定，程序抛中断交DM裁定"},
                {"label": "拒绝", "text": "无事发生", "refuse": True},
            ],
        },
        {
            "id": "皮衣店", "source": "扭曲都市",
            "desc": "橱窗里陈列着由活体皮肤缝制、点缀着牙齿与瞳孔的华丽时装。一位手指被改造成缝衣针、面部完全由皮尺缠绕的裁缝微笑着向你躬身。",
            "options": [
                {"label": "试穿“皮衣”", "text": "流血10，下场战斗第一[回始]获得30点格挡。",
                 "cost": {"hp": 10}, "effect": {"next_battle": {"first_round_shield": 30}}},
                {"label": "购买“皮衣”", "text": "失去20[碎片]，获得“皮衣”（上回合失去生命时，下回合获得等量格挡）",
                 "cost": {"shards": 20}, "effect": {"event_relic": "皮衣"}},
                {"label": "交换物品", "text": "失去一件当前遗物，获得“皮衣”",
                 "cost": {"relic_destroy": True}, "effect": {"event_relic": "皮衣"}},
                {"label": "拒绝", "text": "无事发生", "refuse": True},
            ],
        },
        {
            "id": "生锈邮筒", "source": "扭曲都市",
            "desc": "倒塌的建筑旁立着一个生锈的邮筒，里面传来指甲抓挠的声音。空洞的声音告诉你，它可以替你向“过去的自己”寄一封信",
            "options": [
                {"label": "写信", "text": "流血5，可以写一封最多40字的信，寄给下一场轮回的你",
                 "cost": {"hp": 5}, "effect": {"letter": True}},
                {"label": "拒绝", "text": "无事发生", "refuse": True},
            ],
        },
        {
            "id": "尖叫下水道", "source": "扭曲都市",
            "desc": "一口井盖被顶开的有轨下水道中不断溢出黑色的多发油性物质。深处传来类似人类的尖叫声（拥有五种道纹及以上才能触发）",
            "condition": "daowen_ge_5",
            "options": [
                {"label": "献出声音", "text": "失忆X，获得遗物·缄默面具（无法再使用任何附带“代价”的道纹，每场[战始]获得20X点法力）",
                 "cost": {"amnesia": "X"}, "effect": {"event_relic_x": "缄默面具"}},
                {"label": "法力净化", "text": "枯竭3，获得遗物·焦黑发丝（每当场上有一个怪物死亡时，你的速度+2）",
                 "cost": {"exhaustion": 3}, "effect": {"event_relic": "焦黑发丝"}},
                {"label": "避开井口", "text": "无事发生", "refuse": True},
            ],
        },
    ],
    "罪孽都市": [
        {
            "id": "遗落的赌局", "source": "罪孽都市",
            "desc": "三个说不清是人是鬼的身影围坐在篝火旁掷骰，见你靠近，为首者头也不抬地推出一颗骰子：“坐，还是不坐？”",
            "options": [
                {"label": "下注[碎片]", "text": "押注X点[碎片]，50%获得双倍[碎片]，50%扣除双倍[碎片]（允许负债，负债≤50）。",
                 "effect": {"gamble": {"kind": "shards"}}},
                {"label": "下注生命", "text": "流血X。50%获得2X[碎片]，50%无事发生。",
                 "effect": {"gamble": {"kind": "hp"}}},
                {"label": "观棋不语", "text": "无事发生。", "refuse": True},
            ],
        },
        {
            "id": "高利贷钱庄", "source": "罪孽都市",
            "desc": "昏暗的地下钱庄桌前，一个满嘴金牙的瘦高老者正拨打着铁算盘，桌上堆满滴血欠条与[碎片]箱。",
            "options": [
                {"label": "获得债务", "text": "立刻获得50[碎片]，[战始]失去10[碎片]；若[战始]手头[碎片]<0（处于负债），每负债10[碎片]，强扣5点[血限]利息。",
                 "effect": {"shards": 50, "debt": {"battle_start_cost": 10}}},
                {"label": "强砸记账铁盘", "text": "流血15。获得【假钞贴】（消耗品（耐久2）：使用后获得20[假碎片]）。",
                 "cost": {"hp": 15}, "effect": {"consumable": "假钞贴"}},
                {"label": "离开账房", "text": "无事发生。", "refuse": True},
            ],
        },
        {
            "id": "地下角斗场", "source": "罪孽都市",
            "desc": "铁笼围成的狂热角斗场，裁判抛出一柄血铁匕首：“下场打一场，赢了全拿，输了留命！”",
            "options": [
                {"label": "签署下场打擂", "text": "使下一场战斗敌方所有[目标][血限]+20%，但击灭敌方后获得双倍[碎片]战利品。",
                 "effect": {"next_battle": {"enemy_blood_pct": 20, "bounty_double": True}}},
                {"label": "押注盘外博彩", "text": "失去15[碎片]。若下一场战斗在3回合内结束，获得45[碎片]；否则损失全部押注。",
                 "cost": {"shards": 15}, "effect": {"next_battle": {"win_in_3_rounds_bonus": 45}}},
                {"label": "拒绝下注", "text": "无事发生。", "refuse": True},
            ],
        },
        {
            "id": "黑市军火贩", "source": "罪孽都市",
            "desc": "倾覆的军用装甲车旁，披着防弹风衣的军火商打开后备箱，露出各式改造型枪械与禁忌弹药。",
            "options": [
                {"label": "购买穿甲弹", "text": "失去12[碎片]。获得【穿甲弹】（消耗品（耐久2）：对[目标]打出15点忽略【格挡】与【闪避】的伤害）。",
                 "cost": {"shards": 12}, "effect": {"consumable": "穿甲弹"}},
                {"label": "购买安保雇佣", "text": "失去15[碎片]。使一名[朋友]获得【防弹插板】（[血限]+10，且[战始]获得15格挡）。",
                 "cost": {"shards": 15}, "effect": {"shield_friend": True}},
                {"label": "摆手离开", "text": "无事发生。", "refuse": True},
            ],
        },
        {
            "id": "通缉悬赏榜", "source": "罪孽都市",
            "desc": "街角满是弹孔的公告栏上，钉着数张通缉令，画着黑帮头目的画像与丰厚血酬。",
            "options": [
                {"label": "撕下巨头悬赏令", "text": "使下一场战斗遭遇的怪物增加1头（额外加入1种帮派怪物），但[战终]结算额外获得30[碎片]悬赏金。",
                 "effect": {"next_battle": {"extra_monster": 1, "battle_end_shards": 30}}},
                {"label": "举报黑帮线索", "text": "失去1次精力。获得下一场战斗怪物的完整情报与10[碎片]。",
                 "cost": {"energy": 1}, "effect": {"shards": 10, "info_next_battle": True}},
                {"label": "视而不见", "text": "无事发生。", "refuse": True},
            ],
        },
        {
            "id": "假钞印钞厂", "source": "罪孽都市",
            "desc": "废弃印刷厂深处，胶印机轰鸣，一张张印着精美符文的假[碎片]如瀑布般从传送带滚落。",
            "options": [
                {"label": "启动印钞机", "text": "枯竭3。下场战斗获得50［假碎片］。",
                 "cost": {"exhaustion": 3}, "effect": {"next_battle": {"fake_shards": 50}}},
                {"label": "销毁印钞模板", "text": "获得【残韵：曲解×1】。",
                 "effect": {"resonance": "曲解"}},
            ],
        },
        {
            "id": "帮派断指酒吧", "source": "罪孽都市",
            "desc": "乌烟障气的酒吧吧台上，一把带血的剁骨刀扎在木案上。几个帮派成员冷笑着推来一杯烈酒。",
            "options": [
                {"label": "断指入会", "text": "流血10。获得遗物【帮派令】（[战始]获得【洗劫3】）。",
                 "cost": {"hp": 10}, "effect": {"event_relic": "帮派令"}},
                {"label": "缴纳保护费", "text": "失去10[碎片]。获得【洗劫面具】（消耗品（耐久2）：使自身下2次攻击附带【必中】）。",
                 "cost": {"shards": 10}, "effect": {"consumable": "洗劫面具"}},
                {"label": "转身离开", "text": "无事发生。", "refuse": True},
            ],
        },
    ],
    "龙心谷": [
        {
            "id": "断桥余烬", "source": "龙心谷",
            "desc": "熔岩断桥上，一名微光者背着失去行动能力的同伴，铁索已经烧得通红。他没有开口求救，只是死死抓住那截快要熔断的铁索。",
            "options": [
                {"label": "接过伤者", "text": "流血10，获得“岩行者”（2×4/54，背负1）作为[朋友]。",
                 "cost": {"hp": 10}, "effect": {"friend": "岩行者"}},
                {"label": "拆下负岳索", "text": "疲惫4，获得遗物【负岳索】（［战始］选择一名[朋友]或[员工]；其首次受到伤害时，自身[回复]等量生命）。",
                 "cost": {"fatigue": 4}, "effect": {"event_relic": "负岳索"}},
                {"label": "绕桥而行", "text": "无事发生。", "refuse": True},
            ],
        },
        {
            "id": "熔炉余火", "source": "龙心谷",
            "desc": "半埋在火山灰里的铁炉仍有余温，炉旁摆着一排无人认领的断剑。炉壁刻着一句话：“留下什么，便会铸成什么。”",
            "options": [
                {"label": "熔掉遗物", "text": "销毁一件当前遗物，获得遗物【炉心坠】（［战始］选择一枚自身拥有的【××龙心】，使其当前耐久+10）。",
                 "cost": {"relic_destroy": True}, "effect": {"event_relic": "炉心坠"}},
                {"label": "钉入铁砧", "text": "衰老10，获得遗物【烙痕钉】（）。",
                 "cost": {"aging": 10}, "effect": {"event_relic": "烙痕钉"}},
                {"label": "让炉火熄灭", "text": "无事发生。", "refuse": True},
            ],
        },
        {
            "id": "逆行者", "source": "龙心谷",
            "desc": "一名微光者独自逆着逃难人群走向火山口。他说自己只是终于不想再把代价推给别人。",
            "options": [
                {"label": "让他同行", "text": "失去10[碎片]，获得“赴火者”（3×3/60，逆鳞1）作为[朋友]。",
                 "cost": {"shards": 10}, "effect": {"friend": "赴火者"}},
                {"label": "接过余火", "text": "流血10，获得遗物【余火印】（［回始］，可消耗自身一枚【××龙心】X点当前耐久，获得2X点法力；1≤X≤该龙心当前耐久）。",
                 "cost": {"hp": 10}, "effect": {"event_relic": "余火印"}},
                {"label": "目送其远去", "text": "无事发生。", "refuse": True},
            ],
        },
        {
            "id": "裂隙温泉", "source": "龙心谷",
            "desc": "火山裂隙中涌出赤红温泉，泉底沉着大量骨骸。石刻写着：“伤口会合拢，代价不会。”",
            "options": [
                {"label": "封存泉眼", "text": "获得【赤泉囊】（消耗品（耐久6/6）：局外使用后产生8点恢复量；自身下两场战斗［战始］失去4点生命）。",
                 "effect": {"consumable": "赤泉囊"}},
                {"label": "饮下泉水", "text": "产生48点恢复量，可自由分配；下次行动精力-1。",
                 "effect": {"heal_pool": 48, "energy_delta": -1}},
                {"label": "深入泉眼", "text": "获得龙血瓶：耐久10/10，当自身或队友获得的回复量超出[血限]时，超出的回复量提升等量耐久；局外【休整】或战中时可随时自由提取储存的回复量分配给自身或队友。",
                 "effect": {"consumable": "龙血瓶"}},
                {"label": "离开裂隙", "text": "无事发生。", "refuse": True},
            ],
        },
        {
            "id": "追求者", "source": "龙心谷",
            "desc": "山道边坐着一名脸色灰白的微光者。他的嘴唇干裂，掌心攥着几枚已经磨损的[碎片]。他盯着你看了很久，像是在确认什么，随后垂下眼：“我能做事。给我口粮，或者让我跟着你。”",
            "options": [
                {"label": "雇佣", "text": "失去10[碎片]，获得“追求者”作为[员工]。追求者（8×2/96，逆鳞2，活血3，固执3）",
                 "cost": {"shards": 10},
                 "unavailable": "员工体系（工资/叛变）未实装，获得员工后无法真实结算，程序拒绝"},
                {"label": "拿走口粮", "text": "获得50[碎片]。下一场战斗中，“追求者”作为怪物额外出现。追求者（8×2/96，逆鳞2，活血3，固执3）",
                 "effect": {"shards": 50, "monster_next_battle": "追求者"}},
                {"label": "离开", "text": "无事发生。", "refuse": True},
            ],
        },
    ],
}

# ==================== 事件朋友模板（原文面板逐字登记） ====================
# 【假设待DM裁定】面板“狂暴2（异变3）”按“道纹狂暴(默认X=2)，初始异变3层”装载，
#   微光者承异变50变怪物的规则不变。发动时按原始怪物道纹惯例支付异变5X。
EVENT_FRIENDS = {
    "乞丐": {"attack_count": 2, "attack_power": 3, "blood_limit": 50,
             "daowen": {"狂暴": 2}, "mutation": 3},
    "岩行者": {"attack_count": 2, "attack_power": 4, "blood_limit": 54,
               "daowen": {"背负": 1}, "mutation": 0},
    "赴火者": {"attack_count": 3, "attack_power": 3, "blood_limit": 60,
               "daowen": {"逆鳞": 1}, "mutation": 0},
}

# ==================== 事件遗物（README 288行：事件遗物不加入遗物池） ====================
# implemented=False 的：如实登记在场、可作交换/销毁对象，但无效果钩子，绝不假装生效。
EVENT_RELICS = {
    "猩红果实": {"implemented": True,
                 "effect": "每场[战始]可选择是否流血10；若选择，则[战终][血限]+2"},
    "苍白之花": {"implemented": True,
                 "effect": "每场[战始]可选择是否疲惫5；若选择，则[战终]精力+1"},
    "缄默面具": {"implemented": True,
                 "effect": "无法再使用任何附带“代价”的道纹，每场[战始]获得20X点法力（X=献出声音时的失忆量）"},
    "焦黑发丝": {"implemented": True,
                 "effect": "每当场上有一个怪物死亡时，你的速度+2"},
    "皮衣":     {"implemented": True,
                 "effect": "上回合失去生命时，下回合获得等量格挡"},
    "防弹插板": {"implemented": True,
                 "effect": "（持有者为[朋友]）[血限]+10，且[战始]获得15格挡"},
    "帮派令":   {"implemented": True,
                 "effect": "[战始]获得【洗劫3】"},
    "负岳索":   {"implemented": True,
                 "effect": "[战始]选择一名[朋友]或[员工]；其首次受到伤害时，自身[回复]等量生命"},
    "炉心坠":   {"implemented": False,
                 "effect": "[战始]选择一枚自身拥有的【××龙心】，使其当前耐久+10",
                 "reason": "龙心体系未实装"},
    "余火印":   {"implemented": False,
                 "effect": "[回始]，可消耗自身一枚【××龙心】X点当前耐久，获得2X点法力",
                 "reason": "龙心体系未实装"},
    "烙痕钉":   {"implemented": False,
                 "effect": "（README原文效果为空括号，规则未定义）",
                 "reason": "原文未给出效果，如实登记为无效果遗物"},
}

# ==================== 消耗品（README 各处，耐久归零后彻底消耗销毁） ====================
# usage: "battle"战斗中使用 / "anytime"战斗中任意时刻 / "pre_battle"局外使用 /
#        "passive"被动生效 / "battle_start"战始使用
CONSUMABLES = {
    # 通用事件
    "绝息淤泥": {"durability": 1, "usage": "anytime",
                 "effect": "使用后屏蔽自身灵魂位置，可使本次[战终]，立刻逃脱"},
    # 罪孽都市事件
    "假钞贴":   {"durability": 2, "usage": "battle",
                 "effect": "使用后获得20[假碎片]"},
    "穿甲弹":   {"durability": 2, "usage": "battle",
                 "effect": "对[目标]打出15点忽略【格挡】与【闪避】的伤害"},
    "洗劫面具": {"durability": 2, "usage": "battle",
                 "effect": "使自身下2次攻击附带【必中】"},
    # 扭曲都市事件 / 工具库
    "活性土壤": {"durability": 1, "usage": "battle_start", "needs_dm": True,
                 "effect": "[战始]可失去X点法力，声明培育生命时可用X点基础预算打造一名[朋友]（自定义特性由玩家设计，DM确认）"},
    "反怪物电击枪": {"durability": 3, "usage": "battle",
                     "effect": "对一个[目标]造成25点伤害；若[目标]处于【飞行】，额外造成15点伤害并施加【坠落1】"},
    "备用血泵": {"durability": 3, "usage": "battle",
                 "effect": "使自身获得20点[回复]；若自身当前生命≤30%，额外获得30点格挡"},
    "强光探照灯": {"durability": 2, "usage": "battle",
                   "effect": "使一个[目标]陷入【蒙蔽2】"},
    "高压水枪": {"durability": 2, "usage": "battle",
                 "effect": "清除全场所有敌方[目标]身上的所有“持续X”效果"},
    "储能电池": {"durability": 3, "usage": "round_start_auto",
                 "effect": "[回始]本回合额外获得12点法力"},
    "急救箱":   {"durability": 2, "usage": "battle",
                 "effect": "使自身获得[回复25]，并清除自身身上一种“持续X”的负面减益"},
    "干扰仪":   {"durability": 2, "usage": "battle",
                 "effect": "使全场所有敌方[目标]本回合无法发动自身道纹"},
    "高爆手雷": {"durability": 2, "usage": "battle",
                 "effect": "对一个[目标]造成15点伤害，并使其本回合攻击次数-1"},
    # 龙心谷事件
    "赤泉囊":   {"durability": 6, "usage": "pre_battle",
                 "effect": "局外使用后产生8点恢复量；自身下两场战斗[战始]失去4点生命"},
    "龙血瓶":   {"durability": 10, "usage": "passive",
                 "effect": "自身或队友获得的回复量超出[血限]时，超出的回复量提升等量耐久；局外【休整】或战中时可随时自由提取储存的回复量分配给自身或队友"},
}

# 扭曲都市废墟设施工具库（README 653行：在【扭曲都市】执行【探索】行动
# 并完成事件后附赠【发现】获得——每次探索结算完成后从本库【发现】一件）
TOOL_LIBRARY = ["反怪物电击枪", "备用血泵", "强光探照灯", "高压水枪", "储能电池", "急救箱", "干扰仪", "高爆手雷"]
