"""最小可行机制系统（MVP）。

五个基础抽象：Verb / Mechanism / Trigger / Condition / Target。
目标：普通新机制尽量只描述【什么时候 / 对谁 / 满足什么条件 / 做什么】，
而不是给核心管线加新的 if。当前已迁移：【加害】【龙鳞】（伤害加减区，
同一相位共存、priority 决定顺序）、【自愈】（ROUND_START 相位，经
CombatEngine._dispatch_phase 分发——机制系统已不止是伤害 Hook 的替代品）。

刻意边界（不要做成框架）：无 DSL、无 JSON 配置、无脚本系统、无 Action Queue、
无通用推理引擎、无冲突自动解决、无反射。机制声明就是 Python 数据结构。

顺序即规则：机制 priority 与 CombatHook.priority 同义（数字小先执行），
迁移只平移原顺序，绝不重排（详见审计报告 H 节冻结清单）。
"""
from .conditions import (  # noqa: F401
    Condition, all_, any_, amount_positive, damage_type_not, entity_type,
    events_this_round, has_status, hp_at_least, is_alive, not_, side_has,
)
from .registry import (  # noqa: F401
    MECHANISMS, Mechanism, MechanismHookAdapter, MechanismRegistry,
)
from .targets import (  # noqa: F401
    ALL, ALL_ALLIES, ALL_ENEMIES, DEAD_ENTITY, RANDOM_ENEMY, SELF, SOURCE,
    TARGET, TargetSelector, custom,
)
from .triggers import Phase, Trigger, TriggerBus, TriggerContext  # noqa: F401
from .verbs import apply_verb, get_verb, register_verb, verb_names  # noqa: F401

# 导入即注册已迁移机制（当前：加害、龙鳞、自愈）。
from . import builtins  # noqa: E402,F401
