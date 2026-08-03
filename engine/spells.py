"""
法术库与法术执行器

规则依据（README）：
- 积木规则：法术不得凭空创造新机制，必须完全由已解锁道纹的原版效果组合而成。
  每一步结算必须严格遵守对应道纹的代数公式。
- 循环规则：允许法术生效流程的终点结算触发该法术自身的启动条件，
  从而在法力充足时自动循环。
- 中断规则：法术一旦法力耗尽或中间流程失效则生效流程中断。
- 法术阶级规则：法术使用X种不同道纹即为X阶（最高9阶）。
  X阶法术最多拥有X种自定义条件。
- 法术必须通过局外【学习】行动方可掌握并开启。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from .models import Spell


# 循环安全上限：防止「循环规则」在数值异常时造成死循环。
# 正常情况下循环由法力/生命耗尽自然中断（中断规则）。
MAX_SPELL_LOOPS = 50


@dataclass
class SpellStep:
    """
    法术生效流程中的一步 = 发动一次道纹

    daowen:      道纹名（必须是已有道纹，积木规则）
    var:         该步使用的变量名（X/Y/Z），由施法者自由控X
    coefficient: 变量系数，例如「血债3X」coefficient=3
    condition:   条件门（None=无条件）
                 "target_flying"      → 目标处于飞行才执行
                 "no_damage_dealt"    → 本次流程此前未造成伤害才执行
    """
    daowen: str
    var: str = "X"
    coefficient: int = 1
    condition: Optional[str] = None


@dataclass
class SpellDef:
    """法术定义（含可执行的生效流程）"""
    name: str
    required_daowen: list[str]
    trigger_condition: str
    effect_flow: str
    steps: list[SpellStep] = field(default_factory=list)
    # 循环规则：终点结算是否重新触发自身启动条件
    loops: bool = False
    # 循环前的额外自伤（不死不休：「失去X点生命」作为流程起点）
    self_life_loss_var: Optional[str] = None

    @property
    def rank(self) -> int:
        """法术阶级 = 使用的不同道纹种数（最高9阶）"""
        return min(9, len(set(self.required_daowen)))

    @property
    def max_custom_conditions(self) -> int:
        """X阶法术最多拥有X种自定义条件"""
        return self.rank

    def variables(self) -> list[str]:
        """该法术需要施法者指定的变量列表（去重保序）"""
        seen = []
        if self.self_life_loss_var and self.self_life_loss_var not in seen:
            seen.append(self.self_life_loss_var)
        for s in self.steps:
            if s.var not in seen:
                seen.append(s.var)
        return seen

    def to_spell(self) -> Spell:
        """转换为可挂在 Entity 上的 Spell 实例"""
        return Spell(
            name=self.name,
            required_daowen=list(self.required_daowen),
            trigger_condition=self.trigger_condition,
            effect_flow=self.effect_flow,
            rank=self.rank,
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "required_daowen": self.required_daowen,
            "trigger_condition": self.trigger_condition,
            "effect_flow": self.effect_flow,
            "rank": self.rank,
            "variables": self.variables(),
            "loops": self.loops,
        }


# ==================== 可学法术（README 正文 9 个） ====================

SPELL_LIBRARY: dict[str, SpellDef] = {
    "先发制人": SpellDef(
        name="先发制人",
        required_daowen=["杀伐"],
        trigger_condition="受到伤害前",
        effect_flow="受到伤害前→发动杀伐 X",
        steps=[SpellStep("杀伐", "X")],
    ),
    "临界泄压": SpellDef(
        name="临界泄压",
        required_daowen=["锐利"],
        trigger_condition="受到伤害前",
        effect_flow="受到伤害前→发动锐利 X",
        steps=[SpellStep("锐利", "X")],
    ),
    "生生不息": SpellDef(
        name="生生不息",
        required_daowen=["再生"],
        trigger_condition="失去生命后",
        effect_flow="失去生命后→发动再生 X",
        steps=[SpellStep("再生", "X")],
    ),
    "后发制人": SpellDef(
        name="后发制人",
        required_daowen=["庇护"],
        trigger_condition="受到伤害前",
        effect_flow="受到伤害前→发动庇护 X",
        steps=[SpellStep("庇护", "X")],
    ),
    "以牙还牙": SpellDef(
        name="以牙还牙",
        required_daowen=["杀伐", "再生"],
        trigger_condition="失去生命后",
        effect_flow="失去生命后→发动再生 X→发动杀伐 Y",
        steps=[SpellStep("再生", "X"), SpellStep("杀伐", "Y")],
    ),
    "借力打力": SpellDef(
        name="借力打力",
        required_daowen=["杀伐", "庇护"],
        trigger_condition="受到伤害前",
        effect_flow="受到伤害前→发动庇护 X→发动杀伐 Y",
        steps=[SpellStep("庇护", "X"), SpellStep("杀伐", "Y")],
    ),
    "不死不休": SpellDef(
        name="不死不休",
        required_daowen=["血债"],
        trigger_condition="失去生命后",
        effect_flow="失去 X 点生命→发动血债 X→付出代价→失去 X 点生命（循环）",
        steps=[SpellStep("血债", "X")],
        loops=True,
        self_life_loss_var="X",
    ),
    "千刀万剐": SpellDef(
        name="千刀万剐",
        required_daowen=["血债", "再生"],
        trigger_condition="失去生命后",
        effect_flow="失去生命后→发动再生 X→发动血债3X→付出代价→失去生命后（循环）",
        steps=[SpellStep("再生", "X"), SpellStep("血债", "X", coefficient=3)],
        loops=True,
    ),
    "咎由自取": SpellDef(
        name="咎由自取",
        required_daowen=["坠落", "杀伐", "血债"],
        trigger_condition="[目标]发动道纹前",
        effect_flow="[目标]发动道纹前→若其处于飞行，发动坠落X，否则跳过"
                    "→发动杀伐Y→若未造成伤害，发动血债Z，否则跳过",
        steps=[
            SpellStep("坠落", "X", condition="target_flying"),
            SpellStep("杀伐", "Y"),
            SpellStep("血债", "Z", condition="no_damage_dealt"),
        ],
    ),
}


def get_spell(name: str) -> Optional[SpellDef]:
    return SPELL_LIBRARY.get(name)


def list_spells() -> list[str]:
    return list(SPELL_LIBRARY.keys())


def validate_building_blocks(spell: SpellDef, known_daowen: set[str]) -> dict:
    """
    积木规则校验：法术的每一步都必须是施法者已解锁的道纹。
    """
    missing = [s.daowen for s in spell.steps if s.daowen not in known_daowen]
    if missing:
        return {
            "valid": False,
            "missing_daowen": sorted(set(missing)),
            "error": f"积木规则：法术【{spell.name}】需要道纹 {sorted(set(missing))}，施法者未持有",
        }
    return {"valid": True}


def create_custom_spell(
    name: str,
    steps: list[dict],
    trigger_condition: str,
    owned_daowen: set[str],
    custom_conditions: Optional[list[str]] = None,
) -> dict:
    """
    自创法术（局外【学习】：自创一种法术）

    规则：
    - 积木规则：必须完全由创建时已拥有的道纹组装
    - 法术阶级规则：X种不同道纹=X阶（最高9阶），最多X种自定义条件
    """
    if not name:
        return {"success": False, "error": "法术必须有名称"}
    if name in SPELL_LIBRARY:
        return {"success": False, "error": f"法术名【{name}】已存在（命名唯一性）"}
    if not steps:
        return {"success": False, "error": "法术生效流程不得为空"}

    parsed: list[SpellStep] = []
    for i, raw in enumerate(steps, 1):
        dw = raw.get("daowen", "")
        if dw not in owned_daowen:
            return {
                "success": False,
                "error": f"积木规则：第{i}步使用了未拥有的道纹【{dw}】",
                "owned_daowen": sorted(owned_daowen),
            }
        parsed.append(SpellStep(
            daowen=dw,
            var=raw.get("var", "X"),
            coefficient=int(raw.get("coefficient", 1)),
            condition=raw.get("condition"),
        ))

    used = [s.daowen for s in parsed]
    rank = min(9, len(set(used)))
    conds = custom_conditions or []
    if len(conds) > rank:
        return {
            "success": False,
            "error": f"法术阶级规则：{rank}阶法术最多{rank}种自定义条件，当前{len(conds)}种",
        }

    flow = "→".join(
        f"{'若' + s.condition + '，' if s.condition else ''}"
        f"发动{s.daowen}{s.coefficient if s.coefficient != 1 else ''}{s.var}"
        for s in parsed
    )
    definition = SpellDef(
        name=name,
        required_daowen=sorted(set(used)),
        trigger_condition=trigger_condition,
        effect_flow=f"{trigger_condition}→{flow}",
        steps=parsed,
    )
    return {
        "success": True,
        "spell": definition,
        "rank": rank,
        "max_custom_conditions": rank,
        "custom_conditions": conds,
    }
