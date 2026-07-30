"""
规则自动同步系统
核心功能：
1. 读取规则文件（README.md / ALL），提取结构化数据
2. 检测规则文件变更，自动同步到引擎
3. 当引擎运行中发现规则与实际不符时，自动提出修改建议
4. DM确认后自动更新规则文件和引擎内部状态
"""
from __future__ import annotations
import os
import re
import json
import hashlib
import time
import sqlite3
from typing import Optional
from .daowen import DaoWenEngine
from .dm_rulings import DMRulingsDB


class RuleFile:
    """规则文件跟踪"""
    
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.content = ""
        self.hash = ""
        self.last_modified = 0.0
        self._load()
    
    def _load(self):
        if os.path.exists(self.filepath):
            with open(self.filepath, 'r', encoding='utf-8') as f:
                self.content = f.read()
            self.hash = hashlib.md5(self.content.encode()).hexdigest()
            self.last_modified = os.path.getmtime(self.filepath)
    
    def reload(self) -> bool:
        """重新加载，返回是否有变更"""
        old_hash = self.hash
        self._load()
        return self.hash != old_hash
    
    def save(self):
        """保存内容到文件"""
        os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
        with open(self.filepath, 'w', encoding='utf-8') as f:
            f.write(self.content)
        self.hash = hashlib.md5(self.content.encode()).hexdigest()
        self.last_modified = time.time()


class RuleSync:
    """
    规则同步引擎
    管理规则文件与引擎之间的双向同步
    """
    
    def __init__(
        self, 
        rule_files: list[str] = None,
        db_path: str = "data/rule_sync.db",
        rules_dir: str = "."
    ):
        self.rules_dir = rules_dir
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        
        # 规则文件列表
        self._rule_files: dict[str, RuleFile] = {}
        if rule_files:
            for f in rule_files:
                self.add_rule_file(f)
        
        # 同步记录
        self._init_db()
        
        # 提取的规则缓存
        self._extracted_rules: dict = {}
    
    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                change_type TEXT NOT NULL,
                section TEXT DEFAULT '',
                old_content TEXT DEFAULT '',
                new_content TEXT DEFAULT '',
                synced INTEGER DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rule_patches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                section TEXT NOT NULL,
                patch_type TEXT NOT NULL,
                old_text TEXT DEFAULT '',
                new_text TEXT NOT NULL,
                reason TEXT DEFAULT '',
                applied INTEGER DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()
    
    def add_rule_file(self, filepath: str):
        """添加规则文件"""
        full_path = os.path.join(self.rules_dir, filepath)
        self._rule_files[filepath] = RuleFile(full_path)
    
    # ========== 变更检测 ==========
    
    def check_for_changes(self) -> list[dict]:
        """检查所有规则文件是否有变更"""
        changes = []
        for name, rf in self._rule_files.items():
            if rf.reload():
                change = {
                    "file": name,
                    "old_hash": rf.hash,
                    "new_hash": rf.hash,
                    "detected_at": time.time()
                }
                changes.append(change)
                self._log_change(name, "file_modified", "", "", rf.content[:500])
        return changes
    
    def _log_change(self, file_path: str, change_type: str, section: str, old_content: str, new_content: str):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO sync_log (file_path, change_type, section, old_content, new_content, synced, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            (file_path, change_type, section, old_content[:2000], new_content[:2000], time.time())
        )
        conn.commit()
        conn.close()
    
    # ========== 规则提取 ==========
    
    def extract_daowen_from_file(self, filepath: str) -> list[dict]:
        """从规则文件中提取道纹定义"""
        full_path = os.path.join(self.rules_dir, filepath)
        if not os.path.exists(full_path):
            return []
        
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        daowen_list = []
        
        # 匹配道纹定义模式
        # 格式：道纹名X：描述
        patterns = [
            # 核心道纹：杀伐X（...）：消耗X。效果
            r'([\u4e00-\u9fff]{2})X[（(].*?[）)].*?[：:](.+?)(?:\n|$)',
            # 简化格式：道纹名X：效果
            r'([\u4e00-\u9fff]{2})X[：:](.+?)(?:\n|$)',
        ]
        
        for pattern in patterns:
            for match in re.finditer(pattern, content):
                name = match.group(1)
                description = match.group(2).strip()
                
                # 排除非道纹的两字词
                if name in ["基础", "核心", "规则", "效果", "代价", "消耗", "持续", "目标"]:
                    continue
                
                daowen = {
                    "name": name,
                    "description": description,
                    "source": filepath,
                    "line": content[:match.start()].count('\n') + 1
                }
                daowen_list.append(daowen)
        
        return daowen_list
    
    def extract_events_from_file(self, filepath: str) -> list[dict]:
        """从规则文件中提取事件定义"""
        full_path = os.path.join(self.rules_dir, filepath)
        if not os.path.exists(full_path):
            return []
        
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        events = []
        
        # 匹配事件名（带引号的名称）
        pattern = r'["""]([^"""]+)["""]'
        for match in re.finditer(pattern, content):
            name = match.group(1)
            # 获取后续描述
            start = match.end()
            # 找到下一个事件或段落结束
            next_section = re.search(r'\n["""]|\n【', content[start:start+500])
            end = start + (next_section.start() if next_section else min(500, len(content) - start))
            desc = content[start:end].strip()
            
            events.append({
                "name": name,
                "description_preview": desc[:200],
                "source": filepath,
                "position": match.start()
            })
        
        return events
    
    def extract_monsters_from_file(self, filepath: str) -> list[dict]:
        """从规则文件中提取怪物定义"""
        full_path = os.path.join(self.rules_dir, filepath)
        if not os.path.exists(full_path):
            return []
        
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        monsters = []
        
        # 匹配怪物格式：名称（攻击次数×攻击力/血限，道纹1，道纹2）
        pattern = r'([\u4e00-\u9fff\w]+)[（(](\d+)[×x](\d+)/(\d+)(?:[，,]([^\n)）]*))?[）)]'
        for match in re.finditer(pattern, content):
            name = match.group(1)
            atk_count = int(match.group(2))
            atk_power = int(match.group(3))
            hp = int(match.group(4))
            daowen_str = match.group(5) or ""
            
            # 解析道纹
            daowen_parts = re.findall(r'([\u4e00-\u9fff]{2})(\d+)', daowen_str)
            daowen = {name: int(val) for name, val in daowen_parts}
            
            monsters.append({
                "name": name,
                "attack_count": atk_count,
                "attack_power": atk_power,
                "blood_limit": hp,
                "daowen": daowen,
                "daowen_raw": daowen_str.strip(),
                "source": filepath
            })
        
        return monsters
    
    def extract_relics_from_file(self, filepath: str) -> list[dict]:
        """从规则文件中提取遗物定义"""
        full_path = os.path.join(self.rules_dir, filepath)
        if not os.path.exists(full_path):
            return []
        
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        relics = []
        
        # 匹配遗物格式：名称：效果描述
        pattern = r'([\u4e00-\u9fff·]+)[：:](.+?)(?:\n|$)'
        in_relic_section = False
        
        for line in content.split('\n'):
            if '遗物' in line and ('池' in line or '定义' in line or '表' in line):
                in_relic_section = True
                continue
            if in_relic_section and line.strip().startswith('【') and '遗物' not in line:
                in_relic_section = False
                continue
            
            if in_relic_section:
                match = re.match(r'([\u4e00-\u9fff·]+)[：:](.+)', line.strip())
                if match:
                    relics.append({
                        "name": match.group(1),
                        "effect": match.group(2).strip(),
                        "source": filepath
                    })
        
        return relics
    
    # ========== 差异检测 ==========
    
    def diff_daowen(self, filepath: str) -> dict:
        """比较文件中的道纹与引擎注册的道纹"""
        file_daowen = self.extract_daowen_from_file(filepath)
        registered = DaoWenEngine.list_all()
        
        file_names = {d["name"] for d in file_daowen}
        reg_names = set(registered)
        
        return {
            "in_file_only": list(file_names - reg_names),
            "in_engine_only": list(reg_names - file_names),
            "in_both": list(file_names & reg_names),
            "file_daowen": file_daowen,
        }
    
    # ========== 自动修改建议 ==========
    
    def generate_patch_suggestions(self, filepath: str) -> list[dict]:
        """根据差异生成修改建议"""
        suggestions = []
        
        # 道纹差异
        daowen_diff = self.diff_daowen(filepath)
        for name in daowen_diff["in_file_only"]:
            suggestions.append({
                "type": "new_daowen",
                "name": name,
                "file": filepath,
                "suggestion": f"新道纹【{name}】在规则文件中发现但引擎未注册，需要添加计算函数",
                "auto_fixable": False,
                "action_required": "在 daowen.py 中添加 calculate_{name} 函数"
            })
        
        for name in daowen_diff["in_engine_only"]:
            suggestions.append({
                "type": "orphan_daowen",
                "name": name,
                "file": filepath,
                "suggestion": f"引擎中注册了道纹【{name}】但规则文件中未找到定义",
                "auto_fixable": True,
                "action_required": "确认是否应移除或补充规则文件"
            })
        
        return suggestions
    
    def generate_sync_report(self) -> dict:
        """生成完整的同步报告"""
        report = {
            "timestamp": time.time(),
            "files_tracked": list(self._rule_files.keys()),
            "changes_detected": [],
            "daowen_diffs": {},
            "suggestions": [],
            "unresolved_violations": 0,
        }
        
        # 检查变更
        changes = self.check_for_changes()
        report["changes_detected"] = changes
        
        # 道纹差异
        for filepath in self._rule_files:
            if filepath.endswith('.md'):
                diff = self.diff_daowen(filepath)
                report["daowen_diffs"][filepath] = {
                    "new": len(diff["in_file_only"]),
                    "missing": len(diff["in_engine_only"]),
                    "synced": len(diff["in_both"]),
                    "details": diff
                }
                suggestions = self.generate_patch_suggestions(filepath)
                report["suggestions"].extend(suggestions)
        
        return report
    
    # ========== 自动修改 ==========
    
    def auto_apply_patch(
        self, 
        file_path: str, 
        section: str, 
        old_text: str, 
        new_text: str,
        reason: str = ""
    ) -> dict:
        """
        自动应用补丁到规则文件
        会先创建补丁记录，然后应用
        """
        # 保存补丁
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            """INSERT INTO rule_patches (file_path, section, patch_type, old_text, new_text, reason, applied, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
            (file_path, section, "replace", old_text, new_text, reason, time.time())
        )
        patch_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        # 应用补丁
        full_path = os.path.join(self.rules_dir, file_path)
        if not os.path.exists(full_path):
            return {"success": False, "error": f"文件不存在: {full_path}"}
        
        rf = self._rule_files.get(file_path)
        if not rf:
            rf = RuleFile(full_path)
            self._rule_files[file_path] = rf
        
        if old_text and old_text in rf.content:
            rf.content = rf.content.replace(old_text, new_text, 1)
            rf.save()
            
            # 标记已应用
            conn = sqlite3.connect(self.db_path)
            conn.execute("UPDATE rule_patches SET applied = 1 WHERE id = ?", (patch_id,))
            conn.commit()
            conn.close()
            
            return {
                "success": True,
                "patch_id": patch_id,
                "file": file_path,
                "change": f"已替换 '{old_text[:50]}...' → '{new_text[:50]}...'"
            }
        elif not old_text:
            # 追加模式
            rf.content += "\n" + new_text
            rf.save()
            
            conn = sqlite3.connect(self.db_path)
            conn.execute("UPDATE rule_patches SET applied = 1 WHERE id = ?", (patch_id,))
            conn.commit()
            conn.close()
            
            return {
                "success": True,
                "patch_id": patch_id,
                "file": file_path,
                "change": f"已追加内容"
            }
        else:
            return {
                "success": False,
                "error": f"未找到匹配文本: '{old_text[:100]}'",
                "patch_id": patch_id
            }
    
    def add_rule_section(
        self,
        file_path: str,
        section_header: str,
        content: str,
        after_section: str = None
    ) -> dict:
        """在规则文件中添加新章节"""
        full_path = os.path.join(self.rules_dir, file_path)
        if not os.path.exists(full_path):
            return {"success": False, "error": f"文件不存在: {full_path}"}
        
        rf = self._rule_files.get(file_path)
        if not rf:
            rf = RuleFile(full_path)
            self._rule_files[file_path] = rf
        
        new_section = f"\n\n{section_header}\n{content}"
        
        if after_section and after_section in rf.content:
            # 在指定章节后插入
            idx = rf.content.index(after_section) + len(after_section)
            # 找到该章节的结束位置
            next_section = re.search(r'\n【', rf.content[idx:])
            if next_section:
                insert_pos = idx + next_section.start()
                rf.content = rf.content[:insert_pos] + new_section + rf.content[insert_pos:]
            else:
                rf.content += new_section
        else:
            rf.content += new_section
        
        rf.save()
        
        return {"success": True, "file": file_path, "section": section_header}
    
    # ========== 同步历史 ==========
    
    def get_sync_history(self, limit: int = 50) -> list[dict]:
        """获取同步历史"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM sync_log ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def get_pending_patches(self) -> list[dict]:
        """获取待应用的补丁"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM rule_patches WHERE applied = 0 ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def get_patch_history(self, limit: int = 50) -> list[dict]:
        """获取补丁历史"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM rule_patches ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]
