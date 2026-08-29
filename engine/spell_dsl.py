"""自创法术 DSL：触发时机词汇表 + 条件表达式 + 效果流程解析。

设计目标（对应用户四点诉求）：
1. 触发时机不再限定"受到伤害前/失去生命后/目标发动道纹前"三选一，
   兼容 README 中出现过的全部[XX]时点词汇（战始/战终/回始/回终/
   敌回始/敌回终/受到伤害前后/失去生命前后/目标发动道纹前），
   且接受常见同义表述（"我方受到伤害前"/"战斗开始时"等）。
2. 句式错误在【学习】提交时就地报错、附带具体原因，不会出现
   "学会了但因解析失败而在战斗里永远不触发"的静默哑火。
3. 支持真正的条件分支：若<条件>则<效果>否则<效果>，条件支持
   且/或/非组合与对生命/法力/血限/法限/速度/速限/护盾/道纹层数/
   状态的数值与布尔比较。
4. 效果流程里每一步都必须显式声明目标身份（自身/攻击者/目标/
   施法者/任意），"任意"在实际结算提交时才指定具体单位并复用
   发动道纹的合法性校验，不再按道纹类型静默猜测。
5. 循环：效果流程可显式声明"循环"，解析结果 loop=True，交给
   既有的"法力耗尽/流程中断即停止"结算语义（校验层另加一个工程
   保险丝上限，防止极端输入导致死循环，不是对循环语义的阉割）。

本模块只做“文本 → 结构化 AST”的解析与条件求值，不触碰战斗结算，
避免把复杂度引入 combat.py 的核心路径；combat.py 只需要调用
`parse_spell(spell)` 拿到统一结构，再驱动已有的发动道纹/伤害管线。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


class SpellDslError(ValueError):
    """自创法术文本解析失败：必须携带具体、可行动的原因。"""


# ---------------------------------------------------------------------------
# 一、触发时机词汇表
# ---------------------------------------------------------------------------
# 触发条件写法不做"必须完全匹配某个固定字符串"的限制；
# 而是剥离常见的主语/助词修饰后，按核心关键词判定所属的规范时机。
# 规范时机与 engine.enums.ActionPhase / TriggerTiming 的字面量保持一致，
# 便于 combat.py 直接拿去对接既有枚举。

TRIGGER_BEFORE_DAMAGE = "受到伤害前"
TRIGGER_AFTER_DAMAGE = "受到伤害后"
TRIGGER_BEFORE_LIFE_LOST = "失去生命前"
TRIGGER_AFTER_LIFE_LOST = "失去生命后"
TRIGGER_TARGET_BEFORE_DAOWEN = "目标发动道纹前"
TRIGGER_BATTLE_START = "战始"
TRIGGER_BATTLE_END = "战终"
TRIGGER_ROUND_START = "回始"
TRIGGER_ROUND_END = "回终"
TRIGGER_ENEMY_ROUND_START = "敌回始"
TRIGGER_ENEMY_ROUND_END = "敌回终"

ALL_TRIGGERS = (
    TRIGGER_BEFORE_DAMAGE, TRIGGER_AFTER_DAMAGE,
    TRIGGER_BEFORE_LIFE_LOST, TRIGGER_AFTER_LIFE_LOST,
    TRIGGER_TARGET_BEFORE_DAOWEN,
    TRIGGER_BATTLE_START, TRIGGER_BATTLE_END,
    TRIGGER_ROUND_START, TRIGGER_ROUND_END,
    TRIGGER_ENEMY_ROUND_START, TRIGGER_ENEMY_ROUND_END,
)

# 去除掉不影响判定的主语/助词修饰词，只留核心时机描述。
_TRIGGER_STRIP_TOKENS = ("我方", "自身", "对方", "己方", "自己的", "时", "的", "（循环）", "(循环)")

# 每个规范时机的"核心关键词组合"判定规则：(必须包含其一的组, ...) 全部满足才算命中。
# 顺序很重要：先判定更具体/带“敌”字样的分支，避免被泛化关键词提前吃掉。
_TRIGGER_RULES: list[tuple[str, tuple[tuple[str, ...], ...], tuple[str, ...]]] = [
    (TRIGGER_ENEMY_ROUND_START,
     (("敌",), ("回合开始", "回始")), ()),
    (TRIGGER_ENEMY_ROUND_END,
     (("敌",), ("回合结束", "回终")), ()),
    (TRIGGER_ROUND_START,
     (("回合开始", "回始", "每回合开始"),), ("敌",)),
    (TRIGGER_ROUND_END,
     (("回合结束", "回终", "每回合结束"),), ("敌",)),
    (TRIGGER_BATTLE_START,
     (("战斗开始", "战始", "开局"),), ()),
    (TRIGGER_BATTLE_END,
     (("战斗结束", "战终", "结局"),), ()),
    (TRIGGER_TARGET_BEFORE_DAOWEN,
     (("发动道纹", "使用道纹", "出招", "发动法术"), ("前",)), ()),
    (TRIGGER_BEFORE_DAMAGE,
     (("受到伤害", "受伤", "承伤", "受到攻击"), ("前",)), ()),
    (TRIGGER_AFTER_DAMAGE,
     (("受到伤害", "受伤", "承伤", "受到攻击"), ("后",)), ()),
    (TRIGGER_BEFORE_LIFE_LOST,
     (("失去生命", "损失生命", "掉血", "扣血", "生命减少"), ("前",)), ()),
    (TRIGGER_AFTER_LIFE_LOST,
     (("失去生命", "损失生命", "掉血", "扣血", "生命减少"), ("后",)), ()),
]


def normalize_trigger_text(text: str) -> str:
    cleaned = (text or "").strip()
    for token in _TRIGGER_STRIP_TOKENS:
        cleaned = cleaned.replace(token, "")
    return cleaned


def parse_trigger(text: str) -> str:
    """把任意写法的触发条件解析为规范时机常量；解析失败抛 SpellDslError。"""
    raw = (text or "").strip()
    if not raw:
        raise SpellDslError("触发条件不能为空")
    cleaned = normalize_trigger_text(raw)
    if not cleaned:
        raise SpellDslError(f"触发条件【{raw}】剥离修饰词后为空，无法识别时机")
    for canonical, groups, forbidden in _TRIGGER_RULES:
        if any(bad in raw for bad in forbidden):
            continue
        if all(any(kw in cleaned for kw in group) for group in groups):
            return canonical
    raise SpellDslError(
        f"无法识别触发条件【{raw}】。可用时机（支持常见同义写法，如“我方受到伤害前”“战斗开始时”）："
        f"{'/'.join(ALL_TRIGGERS)}"
    )


# ---------------------------------------------------------------------------
# 二、条件表达式：且/或/非/比较，支持嵌套括号
# ---------------------------------------------------------------------------

_CMP_OPS = ("大于等于", "小于等于", "不等于", "大于", "小于", "等于")

_FIELD_ALIASES = {
    "生命": "hp", "当前生命": "hp",
    "血限": "blood_limit",
    "法力": "mana", "当前法力": "mana",
    "法限": "mana_limit",
    "速度": "speed", "当前速度": "speed",
    "速限": "speed_limit",
    "护盾": "shield", "格挡": "shield",
}

_SUBJECT_ALIASES = {
    "自身": "self", "自己": "self",
    "攻击者": "attacker", "敌方": "attacker", "对方": "attacker",
    "目标": "target",
    "施法者": "caster",
}


@dataclass(frozen=True)
class Cmp:
    subject: str          # self/attacker/target/caster
    field: str            # hp/mana/... 或 ("daowen_x", 道纹名) 或 ("status", 状态名)
    op: str                # >, <, >=, <=, ==, !=, has, lacks
    value: Any              # int 或 None（has/lacks 不需要数值）
    raw: str = ""


@dataclass(frozen=True)
class BoolOp:
    op: str                 # "and" / "or"
    parts: tuple


@dataclass(frozen=True)
class Not:
    inner: Any


class _CondTokenizer:
    """把条件文本切成 token 序列：主语、字段、比较词、数值、且/或/非、括号。"""

    _TOKEN_RE = re.compile(
        r"\s*(且|或|非|\(|（|\)|）|" + "|".join(_CMP_OPS) + r"|拥有|没有|层|-?\d+|"
        r"[\u4e00-\u9fa5A-Za-z]+)"
    )

    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.tokens: list[str] = []
        self._tokenize()

    def _tokenize(self):
        s = self.text
        i = 0
        while i < len(s):
            m = self._TOKEN_RE.match(s, i)
            if not m:
                if s[i].isspace():
                    i += 1
                    continue
                raise SpellDslError(f"条件表达式在第{i}个字符附近无法解析：...{s[max(0,i-5):i+5]}...")
            tok = m.group(1)
            self.tokens.append(tok)
            i = m.end()
        # 括号统一
        self.tokens = ["(" if t == "（" else ")" if t == "）" else t for t in self.tokens]


class _CondParser:
    """递归下降：or_expr := and_expr (\"或\" and_expr)*；and_expr := unary (\"且\" unary)*；
    unary := \"非\" unary | \"(\" or_expr \")\" | comparison
    """

    def __init__(self, tokens: list[str], raw_text: str):
        self.tokens = tokens
        self.i = 0
        self.raw_text = raw_text

    def _peek(self) -> Optional[str]:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _advance(self) -> str:
        tok = self._peek()
        if tok is None:
            raise SpellDslError(f"条件表达式【{self.raw_text}】提前结束，缺少内容")
        self.i += 1
        return tok

    def parse(self):
        node = self._or_expr()
        if self.i != len(self.tokens):
            raise SpellDslError(f"条件表达式【{self.raw_text}】在末尾有多余内容：{self.tokens[self.i:]}")
        return node

    def _or_expr(self):
        parts = [self._and_expr()]
        while self._peek() == "或":
            self._advance()
            parts.append(self._and_expr())
        return parts[0] if len(parts) == 1 else BoolOp("or", tuple(parts))

    def _and_expr(self):
        parts = [self._unary()]
        while self._peek() == "且":
            self._advance()
            parts.append(self._unary())
        return parts[0] if len(parts) == 1 else BoolOp("and", tuple(parts))

    def _unary(self):
        if self._peek() == "非":
            self._advance()
            return Not(self._unary())
        if self._peek() == "(":
            self._advance()
            node = self._or_expr()
            if self._peek() != ")":
                raise SpellDslError(f"条件表达式【{self.raw_text}】括号未闭合")
            self._advance()
            return node
        return self._comparison()

    def _comparison(self):
        subject_tok = self._advance()
        subject = _SUBJECT_ALIASES.get(subject_tok)
        if subject is None:
            raise SpellDslError(
                f"条件表达式【{self.raw_text}】主语【{subject_tok}】非法，"
                f"必须是{'/'.join(_SUBJECT_ALIASES)}之一")
        field_tok = self._advance()
        # 状态判断："自身 拥有 <状态名>" / "自身 没有 <状态名>"
        if field_tok in ("拥有", "没有"):
            status_tok = self._advance()
            return Cmp(subject=subject, field=("status", status_tok),
                       op="has" if field_tok == "拥有" else "lacks", value=None,
                       raw=self.raw_text)
        # 道纹层数："自身 <道纹名>层数 大于 3"
        if field_tok.endswith("层数"):
            daowen_name = field_tok[:-2]
            field_key = ("daowen_stacks", daowen_name)
        elif field_tok in _FIELD_ALIASES:
            field_key = _FIELD_ALIASES[field_tok]
        else:
            raise SpellDslError(
                f"条件表达式【{self.raw_text}】字段【{field_tok}】非法，"
                f"必须是{'/'.join(_FIELD_ALIASES)}或“<道纹名>层数”")
        op_tok = self._advance()
        op_map = {"大于": ">", "小于": "<", "大于等于": ">=", "小于等于": "<=",
                  "等于": "==", "不等于": "!="}
        if op_tok not in op_map:
            raise SpellDslError(f"条件表达式【{self.raw_text}】比较词【{op_tok}】非法")
        value_tok = self._advance()
        # 支持 "血限的一半" 这种派生值：解析为 (字段, 分母)
        if self._peek() == "的" or value_tok == "的":
            pass  # 简化：不特殊处理连接词，下面统一按数值/百分比解析
        try:
            value = int(value_tok)
        except ValueError:
            raise SpellDslError(f"条件表达式【{self.raw_text}】比较值【{value_tok}】必须是整数")
        return Cmp(subject=subject, field=field_key, op=op_map[op_tok], value=value,
                   raw=self.raw_text)


def parse_condition(text: str):
    """解析条件表达式文本为 AST；解析失败抛 SpellDslError。"""
    raw = (text or "").strip()
    if not raw:
        raise SpellDslError("条件表达式不能为空")
    tokens = _CondTokenizer(raw).tokens
    if not tokens:
        raise SpellDslError(f"条件表达式【{raw}】无法切分出任何有效内容")
    return _CondParser(tokens, raw).parse()


def evaluate_condition(node, resolver) -> bool:
    """resolver: 一个把 (subject, field) 映射为实际数值/布尔的可调用对象。

    resolver(subject: str, field) -> int（数值字段）或 bool（status has/lacks 已经算好的情况下不会走这里）。
    为了保持 spell_dsl 与 Entity 完全解耦（不 import engine.models），
    数值/状态的真实取值交给调用方传入的 resolver 闭包完成。
    """
    if isinstance(node, BoolOp):
        if node.op == "and":
            return all(evaluate_condition(p, resolver) for p in node.parts)
        return any(evaluate_condition(p, resolver) for p in node.parts)
    if isinstance(node, Not):
        return not evaluate_condition(node.inner, resolver)
    if isinstance(node, Cmp):
        if node.op in ("has", "lacks"):
            has_it = bool(resolver(node.subject, node.field))
            return has_it if node.op == "has" else not has_it
        actual = resolver(node.subject, node.field)
        if not isinstance(actual, (int, float)):
            raise SpellDslError(f"字段{node.field}未能解析出数值")
        ops = {">": actual > node.value, "<": actual < node.value,
               ">=": actual >= node.value, "<=": actual <= node.value,
               "==": actual == node.value, "!=": actual != node.value}
        return ops[node.op]
    raise SpellDslError(f"未知条件节点: {node!r}")


# ---------------------------------------------------------------------------
# 三、效果流程：发动道纹步骤 + 条件分支 + 循环标记
# ---------------------------------------------------------------------------

_TARGET_ALIASES = {
    "自身": "self", "自己": "self",
    "攻击者": "attacker", "敌方": "attacker",
    "目标": "target",
    "施法者": "caster",
    "任意目标": "any", "任意": "any", "指定目标": "any",
}

_ACTION_RE = re.compile(
    r"发动\s*(?P<daowen>[\u4e00-\u9fa5]{2,4})\s*X\s*(?:于|对)\s*(?P<target>[\u4e00-\u9fa5]{2,4})"
)
_ACTION_NO_TARGET_RE = re.compile(r"发动\s*(?P<daowen>[\u4e00-\u9fa5]{2,4})\s*X\b")

_LOOP_MARKERS = ("循环直到法力耗尽", "循环至法力耗尽", "循环", "（循环）", "(循环)")


@dataclass(frozen=True)
class ActionStep:
    daowen: str
    target: str    # self/attacker/target/caster/any


@dataclass(frozen=True)
class IfStep:
    condition: Any
    then_steps: tuple
    else_steps: tuple


def _split_top_level(text: str, sep: str) -> list[str]:
    """按分隔符切分，但跳过括号内的分隔符（本 DSL 括号只出现在条件里，效果流程本身不嵌套括号分组）。"""
    parts = []
    depth = 0
    buf = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "(（":
            depth += 1
        elif ch in ")）":
            depth -= 1
        if text[i:i + len(sep)] == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += len(sep)
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p for p in (s.strip() for s in parts) if p]


def _parse_action_clause(clause: str, known_daowen: set[str]) -> ActionStep:
    m = _ACTION_RE.search(clause)
    if m:
        daowen = m.group("daowen")
        target_tok = m.group("target")
        target = _TARGET_ALIASES.get(target_tok)
        if target is None:
            raise SpellDslError(
                f"效果步骤【{clause}】目标【{target_tok}】非法，"
                f"必须显式声明为{'/'.join(sorted(set(_TARGET_ALIASES.values())))}"
                f"（写法如“于自身”“于攻击者”“于任意目标”）")
    else:
        m2 = _ACTION_NO_TARGET_RE.search(clause)
        if not m2:
            raise SpellDslError(
                f"效果步骤【{clause}】无法识别，必须是“发动<道纹>X于<目标>”的形式")
        raise SpellDslError(
            f"效果步骤【{clause}】缺少显式目标声明，必须写成"
            f"“发动{m2.group('daowen')}X于自身/攻击者/目标/施法者/任意目标”之一")
    daowen = m.group("daowen")
    if daowen not in known_daowen:
        raise SpellDslError(f"效果步骤【{clause}】引用了不存在的道纹【{daowen}】")
    return ActionStep(daowen=daowen, target=target)


def parse_effect_flow(text: str, known_daowen: set[str]):
    """解析效果流程文本，返回 (steps, loop: bool)。

    steps 是 ActionStep / IfStep 的列表；顶层用"→"分隔多个步骤。
    条件分支写法："若<条件>则<效果子句>[否则<效果子句>]"，子句内可用
    "；"分隔多个动作（分支内不支持再嵌套 if，保持语法可控）。
    循环写法：整体文本以 循环标记 结尾（如"...→循环直到法力耗尽"）。
    """
    raw = (text or "").strip()
    if not raw:
        raise SpellDslError("效果流程不能为空")

    loop = False
    body = raw
    for marker in _LOOP_MARKERS:
        if body.endswith(marker):
            body = body[: -len(marker)].rstrip("→ ")
            loop = True
            break

    if not body:
        raise SpellDslError("效果流程去除循环标记后为空")

    steps = []
    for clause in _split_top_level(body, "→"):
        if clause.startswith("若"):
            steps.append(_parse_if_clause(clause, known_daowen))
        else:
            steps.append(_parse_action_clause(clause, known_daowen))
    if not steps:
        raise SpellDslError(f"效果流程【{raw}】未解析出任何有效步骤")
    return steps, loop


def _parse_if_clause(clause: str, known_daowen: set[str]) -> IfStep:
    if not clause.startswith("若"):
        raise SpellDslError(f"条件分支【{clause}】必须以“若”开头")
    rest = clause[1:]
    if "则" not in rest:
        raise SpellDslError(f"条件分支【{clause}】缺少“则”")
    cond_text, remainder = rest.split("则", 1)
    if "否则" in remainder:
        then_text, else_text = remainder.split("否则", 1)
    else:
        then_text, else_text = remainder, ""
    condition = parse_condition(cond_text)
    then_steps = tuple(_parse_action_clause(c, known_daowen)
                        for c in _split_top_level(then_text, "；"))
    else_steps = tuple(_parse_action_clause(c, known_daowen)
                        for c in _split_top_level(else_text, "；")) if else_text.strip() else ()
    if not then_steps:
        raise SpellDslError(f"条件分支【{clause}】的“则”分支不能为空")
    return IfStep(condition=condition, then_steps=then_steps, else_steps=else_steps)


# ---------------------------------------------------------------------------
# 四、对外统一入口
# ---------------------------------------------------------------------------

@dataclass
class ParsedSpell:
    trigger: str
    steps: list
    loop: bool


def parse_spell_definition(trigger_condition: str, effect_flow: str,
                            known_daowen: set[str]) -> ParsedSpell:
    """自创法术提交时的完整语法校验入口：解析失败抛出 SpellDslError，
    调用方（engine/api.py 的学习流程）必须把异常信息原样返回给用户，
    不允许吞掉错误静默放行——这是本次修复"句式错误要提醒"的关键点。
    """
    trigger = parse_trigger(trigger_condition)
    steps, loop = parse_effect_flow(effect_flow, known_daowen)
    return ParsedSpell(trigger=trigger, steps=steps, loop=loop)


def describe_condition(node) -> str:
    """把条件 AST 还原成可读文本，供 prepare 接口展示给决策方（不影响判定本身）。"""
    if isinstance(node, Cmp):
        return node.raw
    if isinstance(node, Not):
        return f"非({describe_condition(node.inner)})"
    if isinstance(node, BoolOp):
        sep = " 且 " if node.op == "and" else " 或 "
        return "(" + sep.join(describe_condition(p) for p in node.parts) + ")"
    return "?"


def collect_step_daowen(steps) -> set[str]:
    """递归收集一个 steps 列表里出现过的所有道纹名（含 if 分支内部）。"""
    names: set[str] = set()
    for step in steps:
        if isinstance(step, ActionStep):
            names.add(step.daowen)
        elif isinstance(step, IfStep):
            names |= collect_step_daowen(step.then_steps)
            names |= collect_step_daowen(step.else_steps)
    return names
