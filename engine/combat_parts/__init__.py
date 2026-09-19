"""CombatEngine 的分片实现（Mixin）。

拆分只为可维护性：方法体从 engine/combat.py 原样搬来，行为与 API 不变。
新增分片时请同步 tests/source_scan.py 与 engine/validator.py 的扫描范围，
否则「扫 combat.py 源码」的守卫会静默失效。
"""
