"""
事件系统
解析README中的通用事件池与各副本专属事件，构建事件池，触发与结算。
规则：当前事件池 = 所有未遇到的通用事件 + 当前区域中符合条件且未遇到的专属事件（通用在前）。
"""
from __future__ import annotations
import os
import re
import math
from pathlib import Path
from typing import Optional

from .dungeons import load_dungeon_documents


# 各池事件名（与README一致）
EVENT_NAMES = {
    "通用": ["无名冢", "遗忘书屋", "祭坛", "过路商人", "猩红暴雨", "无名碑林", "回音长廊", "回忆当铺", "手术", "无魂泥潭"],
    "扭曲都市": ["医生", "乞丐", "血肉温室", "绝望来电", "皮衣店", "生锈邮筒", "尖叫下水道"],
    "罪孽都市": ["遗落的赌局", "高利贷钱庄", "地下角斗场", "黑市军火贩", "通缉悬赏榜", "假钞印钞厂", "帮派断指酒吧"],
    "龙心谷": ["断桥余烬", "熔炉余火", "逆行者", "裂隙温泉", "追求者"],
}


def parse_events(index_path: str | Path) -> dict:
    """从全副本索引及副本文档解析事件。通用事件仍位于 README。"""
    index = Path(index_path)
    root = index.parent
    content = (root / "README.md").read_text(encoding="utf-8")
    documents = load_dungeon_documents(index)
    lines = content.split("\n")
    # 每个专属副本独立文档追加到解析输入；事件名白名单阻止标题被误判。
    for document in documents.values():
        lines.extend(["", *document.split("\n")])
    # 建立 name→region 反查
    name_region = {}
    for region, names in EVENT_NAMES.items():
        for n in names:
            name_region[n] = region
    all_names = set(name_region.keys())
    events = {}
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # 检测事件名行：含某事件名 + 描述符（：或换行后描述）
        matched_name = None
        for n in all_names:
            # 行以 "name"： 或 name： 开头，或行就是 name（无冒号，描述在下一行）
            if re.match(rf'^["“]?{re.escape(n)}["”]?\s*[：:]', line) or line.strip('“"') == n:
                matched_name = n
                break
        if matched_name:
            region = name_region[matched_name]
            desc = line
            options = []
            j = i + 1
            # 读描述（若上行只有名字）+ 选项
            while j < len(lines):
                lj = lines[j].strip()
                if not lj:
                    # 空行：若已有选项则事件结束；否则可能是描述段间空行
                    if options:
                        break
                    j += 1
                    continue
                om = re.match(r'^(\d+)\.\s*(.+)', lj)
                if om:
                    options.append({"id": int(om.group(1)), "text": om.group(2)})
                    j += 1
                    continue
                # 非选项非空行：若遇到下一个事件名则结束；否则视为描述续行
                next_name = None
                for n in all_names:
                    if re.match(rf'^["“]?{re.escape(n)}["”]?\s*[：:]', lj) or lj.strip('“"') == n:
                        next_name = n; break
                if next_name:
                    break
                if not options:
                    desc += lj  # 描述续行
                j += 1
            events[matched_name] = {"region": region, "desc": desc, "options": options}
            i = j
        else:
            i += 1
    return events


class EventPool:
    """事件池：跟踪已触发，按区域构建当前池"""
    def __init__(self, events: dict):
        self.events = events
        self.triggered: set[str] = set()
        self.current: Optional[str] = None  # 当前待结算的事件

    def build_pool(self, region: str) -> list[str]:
        """当前池 = 未触发通用 + 未触发本区域专属（通用在前）"""
        pool = [n for n in EVENT_NAMES["通用"] if n in self.events and n not in self.triggered]
        pool += [n for n in EVENT_NAMES.get(region, []) if n in self.events and n not in self.triggered]
        return pool

    def trigger(self, region: str, rng) -> Optional[str]:
        """随机抽取一个事件（按随机数规则：池中随机）"""
        pool = self.build_pool(region)
        if not pool:
            return None
        name = pool[rng.randrange(len(pool))]
        self.current = name
        return name

    def resolve(self, name: str):
        """标记事件已触发"""
        self.triggered.add(name)
        if self.current == name:
            self.current = None


def resolve_option_effect(text: str, engine, event_name: str = "") -> dict:
    """
    结算事件选项效果（关键字解释器）。
    自动扣除常见代价（流血/失去碎片/衰老/枯竭/失去精力）与应用常见收益（获碎片/血限/残韵/遗物/法术）。
    无法解析的特殊效果返回 instruction 交DM。
    event_name不为空时，优先匹配需要精确具名结算的专属事件分支(如龙心谷"追求者")，
    这类事件的收益包含固定面板与固定道纹数值，无法用通用正则安全推断，需按事件名单独处理。
    """
    from .models import Relic, DaoWen, DaoWenInstance, Spell, Entity
    player = engine.state.player
    applied = []
    instructions = []

    # ---- 专属具名事件：龙心谷"追求者"（面板与道纹数值均为文档写死的固定值，不走通用正则） ----
    if event_name == "追求者":
        if text.startswith("雇佣"):
            engine.state.shards -= 10
            applied.append("失去10碎片")
            emp = Entity(name="追求者", entity_type="员工", blood_limit=96, current_hp=96,
                         attack_count=8, attack_power=2, is_deployed=False)
            for dw_name, x in (("逆鳞", 2), ("活血", 3), ("固执", 3)):
                emp.dao_wen[dw_name] = DaoWenInstance(
                    DaoWen(name=dw_name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
                    x_value=x)
            engine.state.employees.append(emp)
            applied.append("获得追求者(8×2/96，逆鳞2，活血3，固执3)作为员工，默认待命，需deploy_employee派遣")
            return {"applied": applied, "instructions": instructions}
        elif text.startswith("拿走口粮"):
            engine.state.shards += 50
            applied.append("获得50碎片")
            engine.state.forced_monsters_next_battle.append({
                "name": "追求者", "attack_count": 8, "attack_power": 2, "blood_limit": 96,
                "dao_wen": {"逆鳞": 2, "活血": 3, "固执": 3},
            })
            applied.append("已登记：下一场战斗追求者将作为怪物额外出现"
                            "(记录于 state.forced_monsters_next_battle，出怪流程本身另行接入时读取)")
            return {"applied": applied, "instructions": instructions}
        # 选项3"离开"落入下方通用的"无事发生"分支，无需特殊处理

    # ---- 回音长廊：错误遗言 / 清除遗言直接改《死者之书.md》 ----
    if event_name == "回音长廊":
        store = getattr(engine, "death_book", None)
        if "错误遗言" in text or text.startswith("聆听"):
            engine.state.shards += 10
            applied.append("获得10碎片")
            if store is not None:
                from .death_book import CAUSE_DRAFTS, validate_legacy
                written = store.append(validate_legacy(CAUSE_DRAFTS["echo_error"]))
                engine.state.death_book_legacies = store.load()
                applied.append(f"写入错误遗言：{written['trigger_point']}")
            return {"applied": applied, "instructions": instructions}
        if "清除" in text and "遗言" in text:
            if player:
                player.take_damage(5, "代价")
                applied.append("流血5")
            if store is not None:
                removed = store.remove_last()
                engine.state.death_book_legacies = store.load()
                applied.append("清除一条遗言" if removed else "无遗言可清除")
            return {"applied": applied, "instructions": instructions}

    def hurt(hp):
        if player:
            player.take_damage(hp, "代价")
            applied.append(f"流血{hp}")

    # 流血
    for m in re.finditer(r'流血\s*(\d+)', text):
        hurt(int(m.group(1)))
    # 衰老（血限-X）
    for m in re.finditer(r'衰老\s*(\d+)', text):
        x = int(m.group(1)); player.blood_limit = max(1, player.blood_limit - x)
        player.current_hp = min(player.current_hp, player.blood_limit); applied.append(f"衰老{x}(血限-{x})")
    # 枯竭（法限-X）
    for m in re.finditer(r'枯竭\s*(\d+)', text):
        x = int(m.group(1)); player.mana_limit = max(0, player.mana_limit - x)
        player.current_mana = min(player.current_mana, player.mana_limit); applied.append(f"枯竭{x}(法限-{x})")
    # 失去精力
    if '失去1次精力' in text or '精力-1' in text:
        engine.state.energy = max(0, engine.state.energy - 1); applied.append("失去1精力")
    # 失去碎片
    for m in re.finditer(r'失去\s*(\d+)\s*\[?碎片\]?', text):
        x = int(m.group(1)); engine.state.shards -= x; applied.append(f"失去{x}碎片")
    # 获得碎片（含"获得X碎片"/"获得X[碎片]"；跳过"双倍"等需随机的）
    for m in re.finditer(r'获得\s*(\d+)\s*\[?碎片\]?', text):
        x = int(m.group(1)); engine.state.shards += x; applied.append(f"获得{x}碎片")
    # 血限+X
    for m in re.finditer(r'\[?血限\]?\s*\+\s*(\d+)', text):
        x = int(m.group(1)); player.blood_limit += x; player.current_hp += x; applied.append(f"血限+{x}")
    # 获得残韵
    for rtype in ["曲解", "反转", "转换"]:
        if rtype in text and ('残韵' in text or '获得' in text):
            engine.state.resonance[rtype] = engine.state.resonance.get(rtype, 0) + 1
            applied.append(f"获得{rtype}残韵")
    # 随机/获得遗物（引擎自动生成随机数并结算，见 DiceEngine.auto_roll）
    if ('遗物' in text) and ('获得' in text or '随机' in text):
        engine._init_relic_pool()
        if engine.state.relics_pool:
            names = [r.name for r in engine.state.relics_pool]
            roll = engine.dice.auto_roll("event_relic_pool", names, context="事件获得遗物")
            idx = roll["record"]["selected_index"]
            r = engine.state.relics_pool.pop(idx)
            engine.state.relics.append(r); applied.append(f"获得遗物·{r.name}")
        else:
            instructions.append("遗物池空，无法获得遗物")
    # 学会法术
    if '法术' in text and ('学会' in text or '学习' in text):
        known = list(engine.SPELL_REGISTRY.keys())
        instructions.append(f"学会法术（可选：{known}）—请指定")
    # 销毁遗物
    if '销毁' in text and '遗物' in text:
        if engine.state.relics:
            r = engine.state.relics.pop(); applied.append(f"销毁遗物·{r.name}")
    # 获得N点[速限]/[法限]（属性点直接分配）
    for m in re.finditer(r'获得(\d+)点\s*\[?速限\]?', text):
        x = int(m.group(1)); player.speed_limit += x; player.current_speed = player.speed_limit; applied.append(f"获得{x}速限")
    for m in re.finditer(r'获得(\d+)点\s*\[?法限\]?', text):
        x = int(m.group(1)); player.mana_limit += 2 * x; player.current_mana = player.mana_limit; applied.append(f"获得{x}法限")
    # 属性点
    if '属性点' in text and ('获得' in text or '+' in text):
        player.speed_limit += 1; player.current_speed = player.speed_limit; applied.append("获得1速限(属性点)")
    # 拒绝/无事
    if '无事发生' in text or text.startswith('拒绝') or text.startswith('观棋') or text.startswith('无视') or text.startswith('离开') or text.startswith('目送') or text.startswith('绕桥') or text.startswith('让炉'):
        applied.append("无事发生")
    # 特殊效果（下注/设计/限制/移植/抽取/雇佣/自定义等）→交DM
    special_kw = ['下注', '设计', '限制选择权', '强制移植', '抽取灵魂', '雇佣', 'diy', '定制', '押注', '负债', '双倍', '随机数', '写信', '寄']
    if any(k in text for k in special_kw) and not applied:
        instructions.append("含随机/自定义效果，需DM裁定")
    return {"applied": applied, "instructions": instructions}
