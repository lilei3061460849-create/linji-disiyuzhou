"""测试公共夹具。

D3（清单 2026-09-16）：《死者之书》默认路径是仓库里的 `死者之书.md`，
任何未显式传 `death_book_path` 的 GameEngine 一旦写入遗言，就会把测试
数据追加进版本库文件，污染工作区。

本夹具对**全部测试**生效：把默认路径重定向到本次 pytest 的临时目录副本，
测试写遗言只写临时文件；仓库里的 `死者之书.md` 保持只读。
需要读真实档案的用例（如校验自爆警示仍在）请显式传路径。
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


@pytest.fixture(autouse=True)
def _isolate_death_book(tmp_path, monkeypatch):
    """把 GameEngine 的默认死者之书路径换成本次测试的临时副本。"""
    from engine.api import GameEngine

    original = GameEngine.__init__

    def patched_init(self, *args, **kwargs):
        path = kwargs.get("death_book_path")
        if path is None or str(path) == str(DEFAULT_BOOK) or path == "死者之书.md":
            sandbox = tmp_path / "死者之书.md"
            if not sandbox.exists() and DEFAULT_BOOK.exists():
                shutil.copyfile(DEFAULT_BOOK, sandbox)
            kwargs["death_book_path"] = str(sandbox)
        original(self, *args, **kwargs)

    monkeypatch.setattr(GameEngine, "__init__", patched_init)
    yield
