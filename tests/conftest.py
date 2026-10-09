"""测试公共夹具。

D3（清单 2026-09-16）：《死者之书》默认路径是仓库里的 `死者之书.md`，
任何未显式传 `death_book_path` 的 GameEngine 一旦写入遗言，就会把测试
数据追加进版本库文件，污染工作区。

本夹具对**全部测试**生效，隔离两个会跨测试泄漏状态的默认路径：

1. `death_book_path`（D3，2026-09-16）
   重定向到本次 pytest 的临时目录**副本**（仓库文件存在则先拷过去），
   测试写遗言只写临时文件；仓库里的 `死者之书.md` 保持只读。
   需要读真实档案的用例（如校验自爆警示仍在）请显式传路径。

2. `sealed_candidate_path`（2026-10-09）
   `data/sealed_candidate.json` 存的是【最终的冠冕】封存候选 = 死斗的
   守擂擂主。它**跨进程持久化且被 .gitignore 忽略**，谁跑完第 7 场就往里
   写，下一个跑的进程就拿它当对手。同一个 seed 会因上一次运行留下的擂主
   不同而走出完全不同的结局（实测：seed 901 在两种残留状态下分别得到
   won=True/耗时 300s 与 won=False/耗时 21s，严格 A-B 交替）。
   引擎侧早已按「测试会隔离此路径」来写（engine/api.py:5084 注释），但
   conftest 此前只隔离了死者之书，129 处未显式传参的 GameEngine( 全部共用
   仓库里这一个文件 → 用例之间互相污染、结果依赖执行顺序。

   注意与死者之书**不同**：这里**不**拷贝仓库文件，每个测试从空开始。
   因为该文件不在版本库里，任何用例都不可能合法地依赖它的既有内容；
   拷过来等于把本地残留状态固化成"基线"，隔离就失去意义。
   全新 clone（无此文件）的行为才是可复现的基线。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_BOOK = ROOT / "死者之书.md"
DEFAULT_SEALED = ROOT / "data" / "sealed_candidate.json"


@pytest.fixture(autouse=True)
def _isolate_death_book(tmp_path, monkeypatch):
    """把 GameEngine 的两个跨测试泄漏路径换成本次测试的临时文件。

    · 死者之书 → 临时副本（保留仓库档案供只读用例查阅）
    · 封存候选 → 临时空文件（不拷贝，见模块 docstring 第 2 条）
    """
    from engine.api import GameEngine

    original = GameEngine.__init__

    def patched_init(self, *args, **kwargs):
        path = kwargs.get("death_book_path")
        if path is None or str(path) == str(DEFAULT_BOOK) or path == "死者之书.md":
            sandbox = tmp_path / "死者之书.md"
            if not sandbox.exists() and DEFAULT_BOOK.exists():
                shutil.copyfile(DEFAULT_BOOK, sandbox)
            kwargs["death_book_path"] = str(sandbox)

        sealed = kwargs.get("sealed_candidate_path")
        if sealed is None or str(sealed) == str(DEFAULT_SEALED) or sealed == "data/sealed_candidate.json":
            kwargs["sealed_candidate_path"] = str(tmp_path / "sealed_candidate.json")

        original(self, *args, **kwargs)

    monkeypatch.setattr(GameEngine, "__init__", patched_init)
    yield
