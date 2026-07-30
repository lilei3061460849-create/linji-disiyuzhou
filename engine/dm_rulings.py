"""
DM裁定系统
核心机制：
1. 程序无法判定的特殊事件触发时，抛出中断（Interrupt）
2. DM做出裁定后计入数据库
3. 下次出现类似情况按数据库解决
"""
from __future__ import annotations
import sqlite3
import json
import time
import os
from typing import Optional, Any
from .enums import InterruptType


class DMRuling:
    """DM裁定记录"""
    
    def __init__(
        self,
        ruling_id: int = 0,
        interrupt_type: str = "",
        context: dict = None,
        ruling_text: str = "",
        ruling_data: dict = None,
        tags: list[str] = None,
        created_at: float = 0,
        match_count: int = 0
    ):
        self.ruling_id = ruling_id
        self.interrupt_type = interrupt_type
        self.context = context or {}
        self.ruling_text = ruling_text
        self.ruling_data = ruling_data or {}
        self.tags = tags or []
        self.created_at = created_at
        self.match_count = match_count
    
    def to_dict(self) -> dict:
        return {
            "ruling_id": self.ruling_id,
            "interrupt_type": self.interrupt_type,
            "context": self.context,
            "ruling_text": self.ruling_text,
            "ruling_data": self.ruling_data,
            "tags": self.tags,
            "created_at": self.created_at,
            "match_count": self.match_count
        }


class Interrupt:
    """
    中断信号
    当程序遇到无法判定的情况时抛出此对象
    调用方必须将此对象提交给DM，等待裁定
    """
    
    def __init__(
        self,
        interrupt_type: InterruptType,
        context: dict,
        description: str,
        options: list[dict] = None,
        state_snapshot: dict = None
    ):
        self.interrupt_type = interrupt_type
        self.context = context
        self.description = description
        self.options = options or []  # 可选的裁定方案
        self.state_snapshot = state_snapshot or {}  # 触发时的完整状态快照
        self.resolved = False
        self.ruling: Optional[DMRuling] = None
        self.timestamp = time.time()
    
    def to_dict(self) -> dict:
        return {
            "interrupt_type": self.interrupt_type.value,
            "context": self.context,
            "description": self.description,
            "options": self.options,
            "state_snapshot": self.state_snapshot,
            "resolved": self.resolved,
            "ruling": self.ruling.to_dict() if self.ruling else None,
            "instruction": (
                f"【中断：{self.interrupt_type.value}】\n"
                f"{self.description}\n"
                f"需要DM裁定。请将裁定结果通过 engine.submit_ruling() 提交。"
            )
        }
    
    def __str__(self):
        return f"[中断:{self.interrupt_type.value}] {self.description}"


class DMRulingsDB:
    """
    DM裁定数据库
    存储所有DM裁定，支持按场景匹配查询
    """
    
    def __init__(self, db_path: str = "data/dm_rulings.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rulings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                interrupt_type TEXT NOT NULL,
                context_json TEXT NOT NULL,
                ruling_text TEXT NOT NULL,
                ruling_data_json TEXT DEFAULT '{}',
                tags_json TEXT DEFAULT '[]',
                created_at REAL NOT NULL,
                match_count INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_interrupt_type 
            ON rulings(interrupt_type)
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS rulings_fts 
            USING fts5(ruling_text, context_json, content=rules, content_rowid=id)
        """)
        conn.commit()
        conn.close()
    
    def save_ruling(self, ruling: DMRuling) -> int:
        """保存裁定，返回ID"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            """INSERT INTO rulings 
               (interrupt_type, context_json, ruling_text, ruling_data_json, tags_json, created_at, match_count)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                ruling.interrupt_type,
                json.dumps(ruling.context, ensure_ascii=False),
                ruling.ruling_text,
                json.dumps(ruling.ruling_data, ensure_ascii=False),
                json.dumps(ruling.tags, ensure_ascii=False),
                ruling.created_at or time.time(),
                0
            )
        )
        ruling_id = cursor.lastrowid
        
        # 同步到FTS索引
        conn.execute(
            "INSERT INTO rulings_fts(rowid, ruling_text, context_json) VALUES (?, ?, ?)",
            (ruling_id, ruling.ruling_text, json.dumps(ruling.context, ensure_ascii=False))
        )
        
        conn.commit()
        conn.close()
        return ruling_id
    
    def find_similar(
        self, 
        interrupt_type: str, 
        context: dict,
        limit: int = 5
    ) -> list[DMRuling]:
        """
        查找类似场景的裁定
        先按类型过滤，再按关键词匹配
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        
        # 构建搜索关键词
        search_terms = []
        for key, value in context.items():
            if isinstance(value, str):
                search_terms.append(value)
            elif isinstance(value, (int, float)):
                search_terms.append(str(value))
        search_query = " ".join(search_terms)
        
        results = []
        
        # 先尝试FTS全文搜索
        if search_query.strip():
            try:
                rows = conn.execute(
                    """SELECT r.*, rank 
                       FROM rulings_fts fts
                       JOIN rulings r ON r.id = fts.rowid
                       WHERE rulings_fts MATCH ? AND r.interrupt_type = ?
                       ORDER BY rank
                       LIMIT ?""",
                    (search_query, interrupt_type, limit)
                ).fetchall()
                
                for row in rows:
                    results.append(self._row_to_ruling(row))
            except sqlite3.OperationalError:
                pass
        
        # 如果FTS没有结果，回退到类型匹配
        if not results:
            rows = conn.execute(
                """SELECT * FROM rulings 
                   WHERE interrupt_type = ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (interrupt_type, limit)
            ).fetchall()
            
            for row in rows:
                results.append(self._row_to_ruling(row))
        
        conn.close()
        return results
    
    def get_ruling(self, ruling_id: int) -> Optional[DMRuling]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM rulings WHERE id = ?", (ruling_id,)).fetchone()
        conn.close()
        if row:
            return self._row_to_ruling(row)
        return None
    
    def increment_match_count(self, ruling_id: int):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE rulings SET match_count = match_count + 1 WHERE id = ?",
            (ruling_id,)
        )
        conn.commit()
        conn.close()
    
    def get_all_rulings(self, interrupt_type: Optional[str] = None) -> list[DMRuling]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        if interrupt_type:
            rows = conn.execute(
                "SELECT * FROM rulings WHERE interrupt_type = ? ORDER BY created_at DESC",
                (interrupt_type,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM rulings ORDER BY created_at DESC").fetchall()
        conn.close()
        return [self._row_to_ruling(row) for row in rows]
    
    def _row_to_ruling(self, row) -> DMRuling:
        return DMRuling(
            ruling_id=row["id"],
            interrupt_type=row["interrupt_type"],
            context=json.loads(row["context_json"]),
            ruling_text=row["ruling_text"],
            ruling_data=json.loads(row["ruling_data_json"]),
            tags=json.loads(row["tags_json"]),
            created_at=row["created_at"],
            match_count=row["match_count"]
        )
    
    def export_to_json(self, filepath: str):
        """导出所有裁定为JSON"""
        rulings = self.get_all_rulings()
        data = [r.to_dict() for r in rulings]
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def import_from_json(self, filepath: str) -> int:
        """从JSON导入裁定"""
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        count = 0
        for item in data:
            ruling = DMRuling(
                interrupt_type=item.get("interrupt_type", ""),
                context=item.get("context", {}),
                ruling_text=item.get("ruling_text", ""),
                ruling_data=item.get("ruling_data", {}),
                tags=item.get("tags", []),
                created_at=item.get("created_at", time.time())
            )
            self.save_ruling(ruling)
            count += 1
        return count
