"""CombatEngine 的分片实现（Mixin）。

拆分只为可维护性：方法体从 engine/combat.py 原样搬来，行为与 API 不变。
CombatEngine 仍是唯一入口（门面类继承这些 Mixin）；引用方（含 self.combat.X）
一律经 MRO 解析，不需要改任何调用点。

新增分片时请同步 engine/validator.py 的机制护栏扫描范围，
否则「扫 combat.py 源码」的守卫会静默失效。
"""
