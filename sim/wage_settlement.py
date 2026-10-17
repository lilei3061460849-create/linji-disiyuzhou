"""共享员工工资结算助手。

轮回战斗与死斗回放共用同一条正式工资结算路径。此模块仅负责处理
engine 已经提交的 pending_wage_decisions，不承载任何一次性完整跑局策略。
"""
from __future__ import annotations


def settle_wages(engine, logs: list[str] | None = None) -> None:
    """逐笔结算待决员工工资：碎片足够则支付，不足则拒付。"""
    if logs is None:
        logs = []
    while True:
        pending = {
            name: wage
            for name, wage in engine.state.pending_wage_decisions.items()
            if wage is not None
        }
        if not pending:
            break
        name = next(iter(pending))
        wage = pending[name]
        if engine.state.shards >= wage:
            result = engine.execute_action(
                "pay_employee_wage", {"name": name, "decision": "pay"}
            )
            logs.append(f"支付{name}工资{wage}")
        else:
            result = engine.execute_action(
                "pay_employee_wage", {"name": name, "decision": "refuse"}
            )
            logs.append(f"拒付{name}工资{wage}（碎片不足，{name}离队）")
        if not result.get("success"):
            logs.append(f"工资结算异常：{result.get('error')}")
            break
