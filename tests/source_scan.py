"""战斗引擎源码扫描口径（拆分成片后必读）。

`engine/combat.py` 已按 Mixin 拆分：怪物回合两阶段、法术与反应、道纹效果结算、
怪物生态这四块的方法体分别住在 `engine/combat_parts/*.py`，combat.py 只保留门面
（类声明、__init__、伤害/回合/代价等核心结算）。

凡「读 combat.py 源码做断言」的守卫都必须改读本模块给出的**全家族**源码，否则代码
搬进分片之后守卫会静默失效：「旧实现不得复活」变成永真、「新实现必须存在」变成永假。
新增分片不需要改这里——按目录 glob 自动纳入。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMBAT_MAIN = ROOT / "engine" / "combat.py"
PARTS_DIR = ROOT / "engine" / "combat_parts"


def combat_source_files() -> list[Path]:
    """combat.py ＋ 全部分片（文件名排序，保证断言消息里的顺序稳定）。"""
    files = [COMBAT_MAIN]
    if PARTS_DIR.is_dir():
        files += sorted(p for p in PARTS_DIR.glob("*.py") if p.name != "__init__.py")
    return files


def combat_source() -> str:
    """全家族源码拼接，每段前带 `# ===== 相对路径 =====` 标记，便于断言失败时定位。"""
    return "\n".join(
        f"# ===== {p.relative_to(ROOT)} =====\n{p.read_text(encoding='utf-8')}"
        for p in combat_source_files()
    )


def combat_relative_paths() -> list[str]:
    """全家族的仓库相对路径（给按路径清单扫描的守卫用）。"""
    return [str(p.relative_to(ROOT)).replace("\\", "/") for p in combat_source_files()]


COMBAT_SOURCE = combat_source()
