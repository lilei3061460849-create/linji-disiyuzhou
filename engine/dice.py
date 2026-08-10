"""
随机数引擎
核心规则（2026-08-09 起）：需要随机时，先统计当前池中符合条件的选项总数，
由引擎自身在该范围内生成随机数并直接结算对应选项，过程记录种子与序号以便追溯复现。
玩家与DM不再需要手动提供数字。

历史遗留的手动流程（create_pool 计算范围 → 外部提供数字 → resolve_pool 结算）
仍然保留，供需要人工指定结果的调试/回归测试/DM强制裁定场景使用，
但游戏内实际玩法（探索、共鸣、开局遗物等）一律改为调用 auto_roll。
"""
import hashlib
import random
import time
from typing import Any, Optional


class DiceEngine:
    """随机数引擎 - 池系统；auto_roll 为默认自动结算入口，create_pool/resolve_pool 为手动入口"""
    
    def __init__(self, seed: Optional[int] = None):
        self._pools: dict[str, list] = {}  # 命名池
        self._history: list[dict] = []     # 历史记录（可追溯）
        self._rng = random.Random(seed)    # 引擎自身的随机源；传入seed可复现
        self._seed = seed
    
    def create_pool(self, pool_name: str, options: list[Any]) -> dict:
        """
        创建随机池
        返回：{pool_name, count, range: "1~N", options_preview}
        AI拿到这个range后，必须向用户索取数字
        """
        if not options:
            raise ValueError(f"池 '{pool_name}' 不能为空")
        
        self._pools[pool_name] = list(options)
        count = len(options)
        
        return {
            "pool_name": pool_name,
            "count": count,
            "range": f"1~{count}",
            "options_preview": [
                {"index": i + 1, "value": str(opt)} 
                for i, opt in enumerate(options)
            ],
            "instruction": f"请在 1~{count} 中选择一个数字（必须由玩家提供，AI禁止自行选择）"
        }
    
    def resolve_pool(self, pool_name: str, player_number: int) -> dict:
        """
        用玩家提供的数字解析池
        返回选定结果
        """
        if pool_name not in self._pools:
            raise ValueError(f"池 '{pool_name}' 不存在")
        
        pool = self._pools[pool_name]
        count = len(pool)
        
        if not 1 <= player_number <= count:
            raise ValueError(f"数字 {player_number} 超出范围 1~{count}")
        
        selected_index = player_number - 1
        selected = pool[selected_index]
        
        # 记录历史
        record = {
            "pool_name": pool_name,
            "player_number": player_number,
            "pool_size": count,
            "selected_index": selected_index,
            "selected_value": str(selected),
            "timestamp": time.time()
        }
        self._history.append(record)
        
        # 从池中移除已选（可选，取决于场景）
        pool.pop(selected_index)
        
        return {
            "pool_name": pool_name,
            "player_number": player_number,
            "selected": selected,
            "remaining_in_pool": len(pool),
            "record": record
        }
    
    def randrange(self, n: int) -> int:
        """委托给引擎自身的随机源，返回 [0, n) 之间的随机整数（供需要rng对象的调用方使用）"""
        return self._rng.randrange(n)

    def auto_roll(self, pool_name: str, options: list[Any], context: str = "") -> dict:
        """
        自动结算入口（默认规则）：统计范围→引擎自身生成随机数→直接结算，全程记录种子与序号。
        返回结构与 resolve_pool 一致，额外附带 auto=True、context、rolled_number、seed。
        """
        if not options:
            raise ValueError(f"池 '{pool_name}' 不能为空")
        pool_info = self.create_pool(pool_name, options)
        count = pool_info["count"]
        number = self._rng.randrange(count) + 1  # 复用 resolve_pool 的 1-based 约定
        result = self.resolve_pool(pool_name, number)
        result["auto"] = True
        result["context"] = context
        result["seed"] = self._seed
        if self._history:
            self._history[-1]["auto"] = True
            self._history[-1]["context"] = context
            self._history[-1]["seed"] = self._seed
        return result

    def get_pool_status(self, pool_name: str) -> Optional[dict]:
        """获取池状态"""
        if pool_name not in self._pools:
            return None
        pool = self._pools[pool_name]
        return {
            "pool_name": pool_name,
            "count": len(pool),
            "range": f"1~{len(pool)}" if pool else "空池"
        }
    
    def remove_from_pool(self, pool_name: str, predicate) -> int:
        """按条件从池中移除不满足条件的选项，返回移除数量"""
        if pool_name not in self._pools:
            return 0
        before = len(self._pools[pool_name])
        self._pools[pool_name] = [x for x in self._pools[pool_name] if not predicate(x)]
        return before - len(self._pools[pool_name])
    
    def get_history(self) -> list[dict]:
        """获取所有历史记录"""
        return list(self._history)
    
    def clear_pool(self, pool_name: str):
        """清除指定池"""
        self._pools.pop(pool_name, None)
    
    def clear_all(self):
        """清除所有池和历史"""
        self._pools.clear()
        self._history.clear()


class EventPool:
    """
    事件池管理器
    当前事件池由所有未遇到的通用事件，以及当前区域中符合条件且未遇到的专属事件共同组成
    通用事件排列在前，专属事件排列在后
    """
    
    def __init__(self):
        self._encountered: set[str] = set()  # 已遇到的事件
    
    def mark_encountered(self, event_id: str):
        """标记事件已遇到"""
        self._encountered.add(event_id)
    
    def is_encountered(self, event_id: str) -> bool:
        return event_id in self._encountered
    
    def build_event_pool(
        self, 
        universal_events: list[dict], 
        region_events: list[dict],
        current_region: str
    ) -> list[dict]:
        """
        构建当前可用事件池
        通用事件在前，专属事件在后
        已遇到的排除
        """
        pool = []
        
        for event in universal_events:
            eid = event.get("id", "")
            if eid not in self._encountered:
                pool.append({**event, "source": "universal"})
        
        for event in region_events:
            eid = event.get("id", "")
            region = event.get("region", "")
            if eid not in self._encountered and region == current_region:
                pool.append({**event, "source": "region"})
        
        return pool
    
    def get_encountered(self) -> set:
        return set(self._encountered)
    
    def reset(self):
        self._encountered.clear()


# 延迟结算的随机数请求
class RandomRequest:
    """
    表示一个等待玩家输入随机数的请求
    引擎在需要随机时返回此对象，调用方必须将range告知玩家并获取数字
    """
    
    def __init__(self, context: str, pool_name: str, pool_size: int, callback):
        self.context = context          # 上下文说明
        self.pool_name = pool_name
        self.pool_size = pool_size
        self.range = f"1~{pool_size}"
        self._callback = callback       # 接收数字后的回调
        self.resolved = False
        self.result = None
    
    def submit_number(self, number: int) -> dict:
        """提交玩家选择的数字"""
        if self.resolved:
            raise RuntimeError("此随机请求已解决")
        if not 1 <= number <= self.pool_size:
            raise ValueError(f"数字 {number} 超出范围 {self.range}")
        
        self.result = self._callback(number)
        self.resolved = True
        return self.result
    
    def to_dict(self) -> dict:
        return {
            "context": self.context,
            "pool_name": self.pool_name,
            "range": self.range,
            "pool_size": self.pool_size,
            "resolved": self.resolved,
            "instruction": f"【需要随机数】{self.context}，范围：{self.range}，请玩家提供数字"
        }
