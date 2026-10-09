"""项目文档事实源的稳定文件名。

README 只作入口与 AI/维护速览（2026-10-09 迁移）；不得再把精确规则正文放回 README。
这里集中声明运行时/同步器真正读取的规范文档，避免 `events.py`、`rule_sync.py`、测试
各自硬编码文件名后迁移漂移。

- ``规则正文.md``：通用规则、道纹、通用事件的精确 Markdown 事实源；
- ``推演规范.md``：开局/战斗/死斗/战报的操作与记录规范；
- ``README.md``：入口与非规范速览，不承载精确规则。
"""

README_FILE = "README.md"
AI_CONTEXT_FILE = "AI_CONTEXT.md"  # 生成聚合物，不是事实源
COMMON_RULES_FILE = "规则正文.md"
PLAYBOOK_FILE = "推演规范.md"
DEATH_BOOK_FILE = "死者之书.md"
ITEM_INDEX_FILE = "物品索引.md"
DUNGEON_INDEX_FILE = "副本索引.md"
